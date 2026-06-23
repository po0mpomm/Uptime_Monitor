"""
All SQL / ORM queries — shared by both the `api` and `worker` processes.
No business logic lives here; that belongs in services/.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import HealthCheck, Monitor, WorkerStatus
from ..schemas import UrlCreate, UrlUpdate


# ---------------------------------------------------------------------------
# Monitor CRUD
# ---------------------------------------------------------------------------

async def create(session: AsyncSession, payload: UrlCreate) -> Monitor:
    monitor = Monitor(
        url=str(payload.url),
        label=payload.label,
        interval_seconds=payload.interval_seconds,
        timeout_ms=payload.timeout_ms,
    )
    session.add(monitor)
    await session.flush()  # get the generated id without committing
    await session.refresh(monitor)
    return monitor


async def get(session: AsyncSession, monitor_id: uuid.UUID) -> Optional[Monitor]:
    result = await session.execute(
        select(Monitor).where(Monitor.id == monitor_id)
    )
    return result.scalar_one_or_none()


async def update_monitor(
    session: AsyncSession, monitor_id: uuid.UUID, payload: UrlUpdate
) -> Optional[Monitor]:
    monitor = await get(session, monitor_id)
    if monitor is None:
        return None
    if payload.is_active is not None:
        monitor.is_active = payload.is_active
    session.add(monitor)
    await session.flush()
    await session.refresh(monitor)
    return monitor


async def delete_monitor(session: AsyncSession, monitor_id: uuid.UUID) -> bool:
    monitor = await get(session, monitor_id)
    if monitor is None:
        return False
    await session.delete(monitor)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# List with latest check — LATERAL join, single query (TRD §6.1)
# ---------------------------------------------------------------------------

async def list_with_latest_check(session: AsyncSession) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            m.id,
            m.url,
            m.label,
            m.interval_seconds,
            m.timeout_ms,
            m.is_active,
            m.current_state,
            m.created_at,
            c.id          AS check_id,
            c.status_code,
            c.response_time_ms,
            c.is_up,
            c.error,
            c.checked_at
        FROM monitors m
        LEFT JOIN LATERAL (
            SELECT *
            FROM health_checks
            WHERE url_id = m.id
            ORDER BY checked_at DESC
            LIMIT 1
        ) c ON true
        WHERE m.is_active = TRUE
        ORDER BY m.created_at DESC
    """)
    result = await session.execute(sql)
    return [dict(row._mapping) for row in result]


# ---------------------------------------------------------------------------
# Check history
# ---------------------------------------------------------------------------

async def get_history(
    session: AsyncSession, monitor_id: uuid.UUID, limit: int = 20
) -> Sequence[HealthCheck]:
    result = await session.execute(
        select(HealthCheck)
        .where(HealthCheck.url_id == monitor_id)
        .order_by(HealthCheck.checked_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Worker: fetch due monitors (FOR UPDATE SKIP LOCKED — TRD §6.2)
# ---------------------------------------------------------------------------

async def fetch_due_monitors(
    session: AsyncSession, limit: int = 50
) -> list[Monitor]:
    """
    Returns monitors whose next_check_at <= now(), locked for update.
    The lock is released when the caller commits/closes the session.
    Caller MUST commit before dispatching HTTP checks (TRD §6.2).
    """
    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Monitor)
        .where(Monitor.is_active == True)  # noqa: E712
        .where(Monitor.next_check_at <= now)
        .order_by(Monitor.next_check_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Worker: record check result (TRD §5.3 step 3)
# ---------------------------------------------------------------------------

async def record_check(
    session: AsyncSession, monitor_id: uuid.UUID, result: dict[str, Any]
) -> HealthCheck:
    check = HealthCheck(
        url_id=monitor_id,
        checked_at=result["checked_at"],
        status_code=result.get("status_code"),
        response_time_ms=result.get("response_time_ms"),
        is_up=result["is_up"],
        error=result.get("error"),
    )
    session.add(check)
    await session.flush()
    await session.refresh(check)
    return check


# ---------------------------------------------------------------------------
# Worker: advance schedule (TRD §5.3 step 4 — same transaction as step 3)
# ---------------------------------------------------------------------------

async def update_schedule(
    session: AsyncSession,
    monitor_id: uuid.UUID,
    new_state: str,
    consecutive_failures: int,
    interval_seconds: int,
) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Monitor)
        .where(Monitor.id == monitor_id)
        .values(
            next_check_at=now + timedelta(seconds=interval_seconds),
            current_state=new_state,
            consecutive_failures=consecutive_failures,
        )
    )


# ---------------------------------------------------------------------------
# Worker heartbeat (TRD §8.2)
# ---------------------------------------------------------------------------

async def upsert_worker_heartbeat(
    session: AsyncSession, monitors_checked: int = 0
) -> None:
    now = datetime.now(timezone.utc)
    await session.execute(
        text("""
            INSERT INTO worker_status (id, last_tick_at, monitors_checked_last_tick)
            VALUES (1, :now, :count)
            ON CONFLICT (id) DO UPDATE
                SET last_tick_at = :now,
                    monitors_checked_last_tick = :count
        """),
        {"now": now, "count": monitors_checked},
    )


async def get_worker_heartbeat(session: AsyncSession) -> Optional[WorkerStatus]:
    result = await session.execute(select(WorkerStatus).where(WorkerStatus.id == 1))
    return result.scalar_one_or_none()
