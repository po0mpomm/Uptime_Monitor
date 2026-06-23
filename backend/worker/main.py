"""
Dedicated worker process — polls Postgres for due monitors and checks them.

Architecture decisions (from PRD §4.5, TRD §5.2):
- Separate process from the API: crashes/slowness here don't affect API latency
- Per-monitor `next_check_at` survives restarts (no in-memory schedule state)
- FOR UPDATE SKIP LOCKED on due-monitor query enables safe future multi-replica scaling
- Semaphore(WORKER_CONCURRENCY) bounds concurrent outbound HTTP connections
- TRD §5.3 ordering: ping → apply_check_result → INSERT health_check → UPDATE schedule → COMMIT
- Each check_one() wrapped to isolate failures (TRD §7.4)
- SIGTERM trapped for graceful shutdown: finish current tick, don't start a new one
"""
import asyncio
import logging
import signal
import sys

# Bootstrap path so worker can import from the shared `app` package
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database import async_session_factory
from app.repositories import monitor_repo
from app.services import monitor_service
from app.services.pinger import ping_url

settings = get_settings()
configure_logging(settings.LOG_LEVEL)
logger = logging.getLogger("worker")

shutdown_event = asyncio.Event()


def _handle_sigterm(*_):
    """Signal handler: set the shutdown event so the main loop exits cleanly."""
    logger.info("SIGTERM received — worker will shut down after this tick")
    shutdown_event.set()


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


# ---------------------------------------------------------------------------
# check_one: ping one monitor and persist the result (TRD §5.3 exact order)
# ---------------------------------------------------------------------------

async def check_one(monitor) -> bool:
    """
    Execute a single monitor check.

    Order (MUST NOT be changed — TRD §5.3):
    1. HTTP ping (no DB write yet)
    2. Apply state machine (pure, no DB write yet)
    3. INSERT health_checks row
    4. UPDATE monitor (next_check_at, current_state, consecutive_failures)
    5. COMMIT (steps 3+4 in same transaction)

    Returns True on success, False if an unexpected exception escaped.
    """
    monitor_id = monitor.id
    try:
        # Step 1: HTTP check
        result = await ping_url(monitor.url, monitor.timeout_ms)

        # Step 2: state machine (mutates monitor.consecutive_failures in-place)
        new_state = monitor_service.apply_check_result(monitor, result["is_up"])

        # Steps 3 + 4 + 5: single transaction
        async with async_session_factory() as session:
            await monitor_repo.record_check(session, monitor_id, result)
            await monitor_repo.update_schedule(
                session,
                monitor_id,
                new_state,
                monitor.consecutive_failures,
                monitor.interval_seconds,
            )
            await session.commit()

        logger.info(
            "Checked %s → %s (%.0f ms)",
            monitor.url,
            new_state,
            result.get("response_time_ms") or 0,
            extra={"monitor_id": str(monitor_id)},
        )
        return True

    except Exception as exc:
        # TRD §7.4: one monitor failing must NOT crash the worker or block others.
        # Record as unknown_error if possible, otherwise just log.
        logger.error(
            "Unexpected error checking monitor %s: %s",
            monitor_id,
            exc,
            exc_info=True,
            extra={"monitor_id": str(monitor_id)},
        )
        try:
            from datetime import datetime, timezone
            fallback_result = {
                "status_code": None,
                "response_time_ms": None,
                "is_up": False,
                "error": "unknown_error",
                "checked_at": datetime.now(timezone.utc),
            }
            monitor_service.apply_check_result(monitor, False)
            async with async_session_factory() as session:
                await monitor_repo.record_check(session, monitor_id, fallback_result)
                await monitor_repo.update_schedule(
                    session,
                    monitor_id,
                    monitor.current_state,
                    monitor.consecutive_failures,
                    monitor.interval_seconds,
                )
                await session.commit()
        except Exception:
            pass  # Logging already done above; don't cascade the error
        return False


# ---------------------------------------------------------------------------
# tick: one full worker sweep
# ---------------------------------------------------------------------------

async def tick() -> int:
    """
    One worker tick:
    1. Fetch due monitors (row locks released before HTTP checks begin — TRD §6.2)
    2. Dispatch bounded-concurrency checks
    3. Update heartbeat
    Returns the count of monitors processed.
    """
    # Step 1: fetch + immediately commit to release FOR UPDATE locks
    async with async_session_factory() as session:
        due_monitors = await monitor_repo.fetch_due_monitors(
            session, limit=settings.WORKER_TICK_BATCH_SIZE
        )
        # Expunge so objects are usable outside this session
        for m in due_monitors:
            session.expunge(m)
        await session.commit()  # releases FOR UPDATE SKIP LOCKED row locks

    if not due_monitors:
        logger.debug("No monitors due this tick")
    else:
        logger.info("Tick: %d monitor(s) due", len(due_monitors))

    # Step 2: bounded concurrent dispatch
    sem = asyncio.Semaphore(settings.WORKER_CONCURRENCY)

    async def bounded(m):
        async with sem:
            await check_one(m)

    await asyncio.gather(*(bounded(m) for m in due_monitors))

    # Step 3: heartbeat — update even if zero monitors were due (TRD §8.2)
    async with async_session_factory() as session:
        await monitor_repo.upsert_worker_heartbeat(session, monitors_checked=len(due_monitors))
        await session.commit()

    return len(due_monitors)


# ---------------------------------------------------------------------------
# main: outer loop
# ---------------------------------------------------------------------------

async def main():
    logger.info(
        "Worker started (poll_interval=%ds, concurrency=%d, batch=%d)",
        settings.WORKER_POLL_INTERVAL_SECONDS,
        settings.WORKER_CONCURRENCY,
        settings.WORKER_TICK_BATCH_SIZE,
    )

    while not shutdown_event.is_set():
        try:
            count = await tick()
        except Exception as exc:
            logger.error("Tick-level error (will retry): %s", exc, exc_info=True)

        # Wait for poll interval OR until shutdown is signalled
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=settings.WORKER_POLL_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            pass  # Normal: timeout means "time for next tick"

    logger.info("Worker shut down gracefully")


if __name__ == "__main__":
    asyncio.run(main())
