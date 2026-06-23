"""
Monitor routes — all 6 monitor-related endpoints.
Thin HTTP layer: input validation + service calls + response shaping only.
"""
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_session
from ...repositories import monitor_repo
from ...schemas import HealthCheckResponse, UrlCreate, UrlResponse, UrlUpdate, UrlWithStatus
from ...services import monitor_service
from ...services.monitor_service import ValidationError
from ...services.pinger import ping_url

router = APIRouter(tags=["monitors"])
logger = logging.getLogger(__name__)


def _build_url_with_status(row: dict) -> UrlWithStatus:
    """Map a LATERAL-join row dict → UrlWithStatus schema."""
    latest_check = None
    if row.get("check_id") is not None:
        latest_check = HealthCheckResponse(
            id=row["check_id"],
            url_id=row["id"],
            checked_at=row["checked_at"],
            status_code=row.get("status_code"),
            response_time_ms=row.get("response_time_ms"),
            is_up=row["is_up"],
            error=row.get("error"),
        )
    return UrlWithStatus(
        id=row["id"],
        url=row["url"],
        label=row.get("label"),
        interval_seconds=row["interval_seconds"],
        timeout_ms=row["timeout_ms"],
        is_active=row["is_active"],
        current_state=row["current_state"],
        created_at=row["created_at"],
        latest_check=latest_check,
    )


# ---------------------------------------------------------------------------
# POST /api/monitors — register a new monitor
# ---------------------------------------------------------------------------

@router.post("/monitors", response_model=UrlResponse, status_code=201)
async def create_monitor(
    payload: UrlCreate,
    session: AsyncSession = Depends(get_session),
):
    # SSRF guard — before any DB write
    try:
        await monitor_service.validate_target(payload.url)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        monitor = await monitor_repo.create(session, payload)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="This URL is already registered")

    logger.info("Monitor created: %s", monitor.url)
    return UrlResponse.model_validate(monitor)


# ---------------------------------------------------------------------------
# GET /api/monitors — list all active monitors with latest check (single query)
# ---------------------------------------------------------------------------

@router.get("/monitors", response_model=list[UrlWithStatus])
async def list_monitors(session: AsyncSession = Depends(get_session)):
    rows = await monitor_repo.list_with_latest_check(session)
    return [_build_url_with_status(row) for row in rows]


# ---------------------------------------------------------------------------
# GET /api/monitors/{id}/history — paginated check history
# ---------------------------------------------------------------------------

@router.get("/monitors/{monitor_id}/history", response_model=list[HealthCheckResponse])
async def get_history(
    monitor_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    monitor = await monitor_repo.get(session, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    checks = await monitor_repo.get_history(session, monitor_id, limit)
    return [HealthCheckResponse.model_validate(c) for c in checks]


# ---------------------------------------------------------------------------
# POST /api/monitors/{id}/check — trigger an immediate check
# ---------------------------------------------------------------------------

@router.post("/monitors/{monitor_id}/check", response_model=HealthCheckResponse)
async def trigger_check(
    monitor_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    monitor = await monitor_repo.get(session, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")

    result = await ping_url(monitor.url, monitor.timeout_ms)
    new_state = monitor_service.apply_check_result(monitor, result["is_up"])

    # TRD §5.3: record check first, then advance schedule, both in same transaction
    check = await monitor_repo.record_check(session, monitor_id, result)
    await monitor_repo.update_schedule(
        session,
        monitor_id,
        new_state,
        monitor.consecutive_failures,
        monitor.interval_seconds,
    )
    await session.commit()

    logger.info(
        "Manual check: %s → %s",
        monitor.url,
        "up" if result["is_up"] else "down",
        extra={"monitor_id": str(monitor_id)},
    )
    return HealthCheckResponse.model_validate(check)


# ---------------------------------------------------------------------------
# PATCH /api/monitors/{id} — toggle is_active
# ---------------------------------------------------------------------------

@router.patch("/monitors/{monitor_id}", response_model=UrlResponse)
async def update_monitor(
    monitor_id: uuid.UUID,
    payload: UrlUpdate,
    session: AsyncSession = Depends(get_session),
):
    monitor = await monitor_repo.update_monitor(session, monitor_id, payload)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await session.commit()
    return UrlResponse.model_validate(monitor)


# ---------------------------------------------------------------------------
# DELETE /api/monitors/{id} — remove monitor + history (cascade)
# ---------------------------------------------------------------------------

@router.delete("/monitors/{monitor_id}", status_code=204)
async def delete_monitor(
    monitor_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    deleted = await monitor_repo.delete_monitor(session, monitor_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await session.commit()
