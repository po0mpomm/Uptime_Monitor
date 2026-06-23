"""
Health endpoint — liveness probe that reports DB status and worker heartbeat.
Returns 200 if healthy, 503 if DB is down or worker heartbeat is stale.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ...core.config import get_settings
from ...database import async_session_factory
from ...repositories import monitor_repo

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check():
    settings = get_settings()
    db_ok = False
    worker_last_tick_at = None
    worker_seconds_since_tick = None

    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True

            heartbeat = await monitor_repo.get_worker_heartbeat(session)
            if heartbeat and heartbeat.last_tick_at:
                worker_last_tick_at = heartbeat.last_tick_at
                now = datetime.now(timezone.utc)
                # Ensure both are timezone-aware
                tick_at = worker_last_tick_at
                if tick_at.tzinfo is None:
                    tick_at = tick_at.replace(tzinfo=timezone.utc)
                worker_seconds_since_tick = int((now - tick_at).total_seconds())
    except Exception as exc:
        logger.error("Health check DB error: %s", exc)

    # Determine overall status
    worker_stale = (
        worker_seconds_since_tick is None
        or worker_seconds_since_tick > settings.WORKER_HEARTBEAT_STALE_THRESHOLD_SECONDS
    )

    if not db_ok or worker_stale:
        reason = []
        if not db_ok:
            reason.append("database unreachable")
        if worker_stale:
            reason.append("worker heartbeat stale")

        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "db": "ok" if db_ok else "error",
                "worker_last_tick_at": worker_last_tick_at.isoformat() if worker_last_tick_at else None,
                "worker_seconds_since_tick": worker_seconds_since_tick,
                "reason": ", ".join(reason),
            },
        )

    return {
        "status": "ok",
        "db": "ok",
        "worker_last_tick_at": worker_last_tick_at.isoformat() if worker_last_tick_at else None,
        "worker_seconds_since_tick": worker_seconds_since_tick,
    }
