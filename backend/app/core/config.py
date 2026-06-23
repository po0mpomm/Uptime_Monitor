from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://uptime:uptime@localhost:5432/uptime"

    # Logging
    LOG_LEVEL: str = "INFO"

    # Worker tuning
    WORKER_POLL_INTERVAL_SECONDS: int = 5
    WORKER_TICK_BATCH_SIZE: int = 50
    WORKER_CONCURRENCY: int = 10
    WORKER_HEARTBEAT_STALE_THRESHOLD_SECONDS: int = 60

    # Rate limiting (POST /monitors)
    RATE_LIMIT_PER_MINUTE: int = 10

    # CORS
    CORS_ALLOWED_ORIGINS: str = "http://localhost:3000,http://frontend:3000"

    @property
    def cors_origins_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
