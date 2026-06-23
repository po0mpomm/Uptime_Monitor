import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class UrlCreate(BaseModel):
    url: HttpUrl
    label: Optional[str] = Field(default=None, max_length=255)
    interval_seconds: int = Field(default=60, ge=30, le=3600)
    timeout_ms: int = Field(default=5000, ge=1000, le=30000)


class UrlUpdate(BaseModel):
    is_active: Optional[bool] = None


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class HealthCheckResponse(BaseModel):
    id: int
    url_id: uuid.UUID
    checked_at: datetime
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    is_up: bool
    error: Optional[str] = None

    model_config = {"from_attributes": True}


class UrlResponse(BaseModel):
    id: uuid.UUID
    url: str
    label: Optional[str] = None
    interval_seconds: int
    timeout_ms: int
    is_active: bool
    current_state: str
    created_at: datetime

    model_config = {"from_attributes": True}


class UrlWithStatus(UrlResponse):
    latest_check: Optional[HealthCheckResponse] = None
