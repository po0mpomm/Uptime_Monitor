import logging
import time
import uuid
from collections import defaultdict
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .config import get_settings

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Generates a UUID4 per incoming request (or accepts inbound X-Request-Id).
    Attaches it to the logging context and echoes it in the response header.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
        request.state.request_id = request_id

        start = time.monotonic()
        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 2)

        response.headers["X-Request-Id"] = request_id

        logger.info(
            "%s %s %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "request_id": request_id,
                "duration_ms": duration_ms,
            },
        )
        return response


# ---------------------------------------------------------------------------
# In-process token-bucket rate limiter for POST /monitors
# Single-instance only (as stated in the PRD/README).
# ---------------------------------------------------------------------------

class _Bucket:
    __slots__ = ("tokens", "last_refill")

    def __init__(self, capacity: int) -> None:
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def consume(self, capacity: int) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        # Refill at `capacity` tokens per 60 seconds
        refill = (elapsed / 60.0) * capacity
        self.tokens = min(capacity, self.tokens + refill)
        self.last_refill = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


_buckets: dict[str, _Bucket] = defaultdict(lambda: _Bucket(get_settings().RATE_LIMIT_PER_MINUTE))


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Token-bucket rate limiter: 10 POST /monitors requests per minute per IP.
    Configurable via RATE_LIMIT_PER_MINUTE env var.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        settings = get_settings()
        if request.method == "POST" and request.url.path.rstrip("/") == "/api/monitors":
            client_ip = request.client.host if request.client else "unknown"
            bucket = _buckets[client_ip]
            if not bucket.consume(settings.RATE_LIMIT_PER_MINUTE):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded, try again later"},
                    headers={"X-Request-Id": getattr(request.state, "request_id", "")},
                )
        return await call_next(request)
