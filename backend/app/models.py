import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .database import Base


def _now_utc():
    return datetime.now(timezone.utc)


class Monitor(Base):
    __tablename__ = "monitors"
    __table_args__ = (
        UniqueConstraint("url", name="uq_monitors_url"),
        CheckConstraint("interval_seconds BETWEEN 30 AND 3600", name="ck_monitors_interval"),
        CheckConstraint("timeout_ms BETWEEN 1000 AND 30000", name="ck_monitors_timeout"),
        CheckConstraint("consecutive_failures >= 0", name="ck_monitors_consec_failures"),
        CheckConstraint(
            "current_state IN ('unknown', 'up', 'down')",
            name="ck_monitors_current_state",
        ),
        # Partial index: only index active monitors for the worker's due-monitor query
        Index(
            "idx_monitors_next_check_at",
            "next_check_at",
            postgresql_where="is_active = TRUE",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url = Column(String(2048), nullable=False)
    label = Column(String(255), nullable=True)
    interval_seconds = Column(Integer, nullable=False, default=60)
    timeout_ms = Column(Integer, nullable=False, default=5000)
    next_check_at = Column(DateTime(timezone=True), nullable=False, default=_now_utc)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    current_state = Column(String(16), nullable=False, default="unknown")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now_utc)

    checks = relationship(
        "HealthCheck",
        back_populates="monitor",
        cascade="all, delete-orphan",
        lazy="select",
    )


class HealthCheck(Base):
    __tablename__ = "health_checks"
    __table_args__ = (
        CheckConstraint(
            "error IS NULL OR error IN "
            "('timeout', 'dns_error', 'connection_refused', 'http_error', 'unknown_error')",
            name="ck_health_checks_error",
        ),
        # Composite index for fast "latest check per monitor" lookups
        Index("idx_health_checks_url_id_checked_at", "url_id", "checked_at"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url_id = Column(
        UUID(as_uuid=True),
        ForeignKey("monitors.id", ondelete="CASCADE"),
        nullable=False,
    )
    checked_at = Column(DateTime(timezone=True), nullable=False, default=_now_utc)
    status_code = Column(Integer, nullable=True)
    response_time_ms = Column(Float, nullable=True)
    is_up = Column(Boolean, nullable=False)
    error = Column(String(32), nullable=True)

    monitor = relationship("Monitor", back_populates="checks")


class WorkerStatus(Base):
    """Singleton row that tracks the worker's last heartbeat."""

    __tablename__ = "worker_status"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_worker_status_singleton"),
    )

    id = Column(Integer, primary_key=True, default=1)
    last_tick_at = Column(DateTime(timezone=True), nullable=True)
    monitors_checked_last_tick = Column(Integer, nullable=False, default=0)
