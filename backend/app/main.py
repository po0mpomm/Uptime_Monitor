"""
FastAPI application bootstrap.

Responsibilities:
- Configure logging
- Create DB tables on startup (create_all + seed worker_status singleton)
- Register middleware: CORS, RequestId, RateLimit
- Register global exception handler
- Mount routers
"""
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .api.routes import health, monitors
from .core.config import get_settings
from .core.logging import configure_logging
from .core.middleware import RateLimitMiddleware, RequestIdMiddleware
from .database import Base, async_session_factory, engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)

    # Create all tables (create_all is idempotent)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed the worker_status singleton row (ON CONFLICT DO NOTHING)
    async with async_session_factory() as session:
        await session.execute(
            text("""
                INSERT INTO worker_status (id, last_tick_at, monitors_checked_last_tick)
                VALUES (1, NULL, 0)
                ON CONFLICT (id) DO NOTHING
            """)
        )
        await session.commit()

    logger.info("Uptime Monitor API started")
    yield
    logger.info("Uptime Monitor API shutting down")
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Uptime Monitor",
        version="1.0.0",
        description="Lightweight URL uptime monitoring service",
        lifespan=lifespan,
    )

    # --- CORS (explicit allowlist, not wildcard) ---
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # --- Request ID (must be added after CORS so it runs on all requests) ---
    app.add_middleware(RequestIdMiddleware)

    # --- Rate limiting ---
    app.add_middleware(RateLimitMiddleware)

    # --- Routers ---
    app.include_router(monitors.router, prefix="/api")
    app.include_router(health.router)

    # --- Global exception handler (TRD §7.3) ---
    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "Unhandled exception: %s\n%s",
            exc,
            traceback.format_exc(),
            extra={"request_id": request_id},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
            headers={"X-Request-Id": request_id},
        )

    return app


app = create_app()
