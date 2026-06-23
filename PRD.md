# 📋 Product Requirements Document
## Uptime Monitor — MVP Assignment (Final Merged Version)

> **Stack:** Python 3.11 + FastAPI + SQLAlchemy (async) + PostgreSQL 16 · Next.js · Docker Compose · AWS Terraform sketch

> **Purpose:** This is the final, submission-ready PRD. It merges the complete, runnable structure of the original draft with the architectural fixes (per-monitor scheduling, worker separation, SSRF protection, failure-threshold logic) needed to close real correctness and security gaps. Every section states *why* a decision was made, not just *what* it is — that reasoning is what the AI collaboration log should reference.

---

## 1. Overview & Objectives

### 1.1 Scope recap
A lightweight, full-stack uptime monitor: register URLs, ping them on a schedule, store status code + response time + timestamp per check, and display live up/down state on a dashboard. Strict MVP — a few dozen URLs, checked roughly every minute.

### 1.2 Stack decisions

| Dimension | Decision |
|---|---|
| Backend language | Python 3.11 + FastAPI |
| ORM | SQLAlchemy 2.0, **async engine** |
| Database | PostgreSQL 16 |
| Scheduler | **Dedicated worker process** (separate container), DB-driven via `next_check_at` per monitor |
| Frontend | Next.js (App Router) |
| Containers | 4 — `db`, `api`, `worker`, `frontend` |
| IaC | Terraform — ECS Fargate (api + worker services) + RDS + ALB + S3/CloudFront |

### 1.3 Why these choices

- **FastAPI** — async-native HTTP client support (needed for concurrent pinging), automatic OpenAPI/Swagger docs at `/docs` for free, and a Pydantic validation layer that does double duty as the request-safety boundary.
- **Async SQLAlchemy throughout (not sync)** — the entire pinging path is async (`httpx.AsyncClient`, `asyncio.gather`); mixing in synchronous DB calls inside an async function blocks the event loop during what should be concurrent I/O. This is corrected from an earlier draft that used a sync `SessionLocal()` inside an `async def` scheduler job — a subtle but real bug.
- **Separate worker process instead of in-process scheduling (the single biggest architecture decision in this PRD)** — an in-process scheduler ties the health-check loop's lifecycle to the API server's. A slow ping or a crash in the checking loop can affect API request latency, and a container restart resets all in-memory schedule state. Splitting `api` and `worker` into two processes — same codebase, two entrypoints, not two repos — means the API stays responsive regardless of what the checker is doing, and each can restart independently. Cost: one extra container. No extra infrastructure (still just Postgres, no Redis/queue).
- **Per-monitor `next_check_at` instead of one global fixed tick** — a single `IntervalTrigger` that pings *every* URL on the same 60-second clock has no way to support different intervals per monitor and loses all scheduling state on restart. A `next_check_at` timestamp column per monitor, polled by the worker, survives restarts and scales naturally to per-monitor intervals without adding infrastructure.
- **PostgreSQL over SQLite** — marginal extra setup effort, but proves a real multi-container DB wiring instead of a single embedded file, which is what a reviewer expects from "production-shaped" thinking.
- **Next.js over plain Vite/React** — Server Components give a fast first paint with no loading-spinner flash on initial dashboard load; the live-updating table is isolated to one small Client Component island.

### 1.4 Non-goals (explicit, not apologetic)

- No auth / multi-tenancy
- No alerting (email / Slack / PagerDuty)
- No horizontal worker scaling — `FOR UPDATE SKIP LOCKED` makes running multiple worker replicas *safe* later; it is not needed at this scale today
- No message queue / Redis — Postgres-as-queue is the correct-sized answer for a few dozen URLs checked once a minute
- No WebSockets — polling every 5–10s is simpler, sufficient, and avoids a connection-management layer that adds no real value at this check frequency

---

## 2. Repository Structure

```
uptime-monitor/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI app bootstrap, middleware, routers only
│   │   ├── api/
│   │   │   └── routes/
│   │   │       ├── monitors.py      # HTTP layer — thin, calls services
│   │   │       └── health.py
│   │   ├── schemas.py               # Pydantic request/response models + validation
│   │   ├── services/
│   │   │   └── monitor_service.py   # business rules: SSRF guard, failure-threshold logic
│   │   ├── repositories/
│   │   │   └── monitor_repo.py      # all SQL/ORM queries — shared by api AND worker
│   │   ├── models.py                # SQLAlchemy ORM models
│   │   ├── database.py              # async engine + session factory
│   │   └── core/
│   │       ├── config.py            # env vars via pydantic-settings
│   │       └── logging.py           # structured JSON logging setup
│   ├── worker/
│   │   └── main.py                  # scheduler loop — imports services/repositories, no duplicate logic
│   ├── tests/
│   │   └── test_monitor_service.py  # unit tests for failure-threshold logic
│   ├── alembic/                      # DB migrations
│   ├── requirements.txt
│   └── Dockerfile                    # one image, two entrypoints (api / worker)
├── frontend/
│   ├── app/
│   │   ├── page.tsx                  # dashboard (Server Component, initial fetch)
│   │   └── monitors/[id]/page.tsx    # detail + check history
│   ├── components/
│   │   ├── UrlTable.tsx              # Client Component — polls every 7-10s
│   │   ├── AddUrlForm.tsx
│   │   └── StatusBadge.tsx
│   ├── lib/
│   │   └── api.ts                    # fetch client, base URL from env
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml
├── README.md
└── AI_LOG.md
```

**Why this layering matters:** the worker and the API both need the same "is this check up or down" and "is this URL safe to ping" logic. Putting that in `services/` + `repositories/` means both processes import the exact same functions instead of duplicating pinger/CRUD logic in two places — which is exactly the kind of drift bug that appears when an API layer and a worker layer are AI-generated from two separate, unlinked prompts and never reconciled.

---

## 3. Data Model

### 3.1 `monitors` table

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` | Avoids sequential ID guessing |
| `url` | VARCHAR(2048) | NOT NULL, UNIQUE | The URL to ping |
| `label` | VARCHAR(255) | NULLABLE | Human-friendly name |
| `interval_seconds` | INT | NOT NULL, DEFAULT 60 | Per-monitor cadence — not a fixed global tick |
| `timeout_ms` | INT | NOT NULL, DEFAULT 5000 | Per-monitor request timeout |
| `next_check_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Drives the worker's poll query; survives restarts |
| `consecutive_failures` | INT | NOT NULL, DEFAULT 0 | Backs the failure-threshold logic (§4.3) |
| `current_state` | TEXT | NOT NULL, DEFAULT 'unknown' | `unknown` \| `up` \| `down` |
| `is_active` | BOOLEAN | NOT NULL, DEFAULT TRUE | Soft-disable without deleting; **wired to a toggle endpoint** (§5) |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | Registration time |

### 3.2 `health_checks` table

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `id` | BIGSERIAL | PK | High-volume insert friendly |
| `url_id` | UUID | FK → monitors.id, CASCADE | Cascade delete with parent monitor |
| `checked_at` | TIMESTAMPTZ | NOT NULL, DEFAULT now() | When the ping ran |
| `status_code` | INTEGER | NULLABLE | NULL = connection failed / timeout |
| `response_time_ms` | FLOAT | NULLABLE | NULL if the request never completed |
| `is_up` | BOOLEAN | NOT NULL | True if status 2xx/3xx and no exception |
| `error` | TEXT | NULLABLE | `timeout` \| `dns_error` \| `connection_refused` \| `http_error` |

**Index:**
```sql
CREATE INDEX idx_health_checks_url_id_checked_at
  ON health_checks(url_id, checked_at DESC);
```
Makes "latest check per monitor" queries fast even as the table grows.

**Why the `error` column is worth it:** "down" is not one thing. DNS failure, connection refused, timeout, and HTTP 5xx are all distinct failure modes with different operational meanings. Surfacing *why* a monitor is down is a single nullable text column with real diagnostic value — cheap to add, easy for a reviewer to notice is missing if it isn't there.

### 3.3 SQLAlchemy models (`models.py`)

```python
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Boolean, Integer, Float, BigInteger,
    DateTime, ForeignKey
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from .database import Base

class Monitor(Base):
    __tablename__ = "monitors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    url = Column(String(2048), nullable=False, unique=True)
    label = Column(String(255), nullable=True)
    interval_seconds = Column(Integer, nullable=False, default=60)
    timeout_ms = Column(Integer, nullable=False, default=5000)
    next_check_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    current_state = Column(String(16), nullable=False, default="unknown")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    checks = relationship("HealthCheck", back_populates="monitor",
                           cascade="all, delete-orphan")


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url_id = Column(UUID(as_uuid=True), ForeignKey("monitors.id"), nullable=False)
    checked_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    status_code = Column(Integer, nullable=True)
    response_time_ms = Column(Float, nullable=True)
    is_up = Column(Boolean, nullable=False)
    error = Column(String(32), nullable=True)

    monitor = relationship("Monitor", back_populates="checks")
```

---

## 4. Backend Logic

### 4.1 Pydantic schemas (`schemas.py`)

```python
from pydantic import BaseModel, HttpUrl, Field
from typing import Optional
from datetime import datetime
import uuid

class UrlCreate(BaseModel):
    url: HttpUrl
    label: Optional[str] = Field(default=None, max_length=255)
    interval_seconds: int = Field(default=60, ge=30, le=3600)
    timeout_ms: int = Field(default=5000, ge=1000, le=30000)

class UrlResponse(BaseModel):
    id: uuid.UUID
    url: str
    label: Optional[str]
    interval_seconds: int
    is_active: bool
    current_state: str
    created_at: datetime

    class Config:
        from_attributes = True

class HealthCheckResponse(BaseModel):
    id: int
    url_id: uuid.UUID
    checked_at: datetime
    status_code: Optional[int]
    response_time_ms: Optional[float]
    is_up: bool
    error: Optional[str]

    class Config:
        from_attributes = True

class UrlWithStatus(UrlResponse):
    latest_check: Optional[HealthCheckResponse]

class UrlUpdate(BaseModel):
    is_active: Optional[bool] = None
```

`ge=30` on `interval_seconds` stops a monitor from being registered to hammer a target every second; `HttpUrl` rejects malformed input at the schema boundary before it ever reaches business logic — but note this validates *shape*, not *safety* (see §4.4).

### 4.2 Pinger logic (`services/pinger.py`)

```python
import httpx
import socket
import time
from datetime import datetime, timezone

async def ping_url(url: str, timeout_ms: int = 5000) -> dict:
    """Ping a URL and return a structured check result."""
    start = time.monotonic()
    timeout_s = timeout_ms / 1000
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
            response = await client.get(url)
        elapsed_ms = (time.monotonic() - start) * 1000
        is_up = response.status_code < 400
        return {
            "status_code": response.status_code,
            "response_time_ms": round(elapsed_ms, 2),
            "is_up": is_up,
            "error": None if is_up else "http_error",
            "checked_at": datetime.now(timezone.utc),
        }
    except httpx.TimeoutException:
        return {
            "status_code": None, "response_time_ms": float(timeout_ms),
            "is_up": False, "error": "timeout",
            "checked_at": datetime.now(timezone.utc),
        }
    except (httpx.ConnectError, socket.gaierror):
        return {
            "status_code": None, "response_time_ms": None,
            "is_up": False, "error": "connection_refused",
            "checked_at": datetime.now(timezone.utc),
        }
```

**Key design decisions:**
- `httpx.AsyncClient` (not `requests`) — fully async, allows concurrent pinging of many monitors in one worker tick without blocking.
- `follow_redirects=True` — avoids a false "down" for sites that 301/302.
- Per-monitor `timeout_ms` (not a hardcoded global) — slow targets don't need the same budget as fast ones.
- Status `< 400` = up — 2xx and 3xx pass, 4xx/5xx fail.
- Distinct exception handling (`timeout` vs `connection_refused`) instead of one catch-all `except Exception` — this is what populates the `error` column with something diagnostically useful.

### 4.3 Consecutive-failure state logic (`services/monitor_service.py`)

```python
def apply_check_result(monitor, success: bool) -> str:
    """
    Returns the new current_state for a monitor given one check result.
    A single failed check does not flip state to 'down' — a dropped
    packet is not an outage. Two consecutive failures is the threshold.
    """
    if success:
        monitor.consecutive_failures = 0
        return "up"
    monitor.consecutive_failures += 1
    return "down" if monitor.consecutive_failures >= 2 else monitor.current_state
```

This lives in `services/`, isolated and unit-tested, because it has a real edge case worth guarding: a brand-new monitor's very first failed check should not jump straight to `down` from `unknown` — it should require the same two-strikes threshold as any other monitor.

```python
# tests/test_monitor_service.py
def test_new_monitor_first_failure_does_not_flip_to_down():
    monitor = Monitor(current_state="unknown", consecutive_failures=0)
    new_state = apply_check_result(monitor, success=False)
    assert new_state == "unknown"
    assert monitor.consecutive_failures == 1

def test_second_consecutive_failure_flips_to_down():
    monitor = Monitor(current_state="unknown", consecutive_failures=1)
    new_state = apply_check_result(monitor, success=False)
    assert new_state == "down"

def test_success_resets_failure_count():
    monitor = Monitor(current_state="down", consecutive_failures=3)
    new_state = apply_check_result(monitor, success=True)
    assert new_state == "up"
    assert monitor.consecutive_failures == 0
```

### 4.4 SSRF guard (`services/monitor_service.py`)

```python
import socket
from ipaddress import ip_address, ip_network

BLOCKED_NETWORKS = [ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "127.0.0.0/8", "169.254.0.0/16", "::1/128",
)]

class ValidationError(Exception):
    pass

async def validate_target(url) -> None:
    if url.scheme not in ("http", "https"):
        raise ValidationError("only http/https URLs are allowed")

    try:
        addrs = await asyncio.get_event_loop().getaddrinfo(url.host, None)
    except socket.gaierror:
        raise ValidationError("could not resolve host")

    for family, _, _, _, sockaddr in addrs:
        ip = ip_address(sockaddr[0])
        if any(ip in net for net in BLOCKED_NETWORKS):
            raise ValidationError("target resolves to a blocked network")
```

**Why this is necessary, not optional:** the entire feature is "fetch a URL a user gives you, on a server-controlled schedule, forever." Pydantic's `HttpUrl` validates that the string *looks like* a URL — it does not stop someone from registering `http://169.254.169.254/latest/meta-data/iam/security-credentials/`, which is the AWS/GCP cloud metadata endpoint, and having the server dutifully poll it once a minute from inside its own VPC. This is called once at registration time, before insert. Noted limitation for the README: this does not protect against DNS rebinding *after* registration (the resolved IP could change later) — correctly scoped as out of bounds for an MVP, but worth naming explicitly so it reads as a conscious tradeoff rather than an oversight.

### 4.5 Worker loop (`worker/main.py`)

```python
import asyncio
import signal
from app.repositories import monitor_repo
from app.services import monitor_service
from app.services.pinger import ping_url
from app.database import async_session_factory

shutdown_event = asyncio.Event()

def _handle_sigterm(*_):
    shutdown_event.set()

signal.signal(signal.SIGTERM, _handle_sigterm)

async def check_one(monitor):
    result = await ping_url(monitor.url, monitor.timeout_ms)
    new_state = monitor_service.apply_check_result(monitor, result["is_up"])
    async with async_session_factory() as session:
        await monitor_repo.record_check(session, monitor.id, result)
        await monitor_repo.update_schedule(session, monitor.id, new_state,
                                            monitor.consecutive_failures)
        await session.commit()

async def tick():
    async with async_session_factory() as session:
        due = await monitor_repo.fetch_due_monitors(session, limit=50)
        # uses SELECT ... FOR UPDATE SKIP LOCKED — safe if a second
        # worker replica is ever added, costs nothing today
    sem = asyncio.Semaphore(10)
    async def bounded(m):
        async with sem:
            await check_one(m)
    await asyncio.gather(*(bounded(m) for m in due))
    async with async_session_factory() as session:
        await monitor_repo.upsert_worker_heartbeat(session)
        await session.commit()

async def main():
    while not shutdown_event.is_set():
        await tick()
        await asyncio.sleep(5)
    # finish current tick, then exit — no new ticks start after SIGTERM
    print("worker shutting down gracefully")

if __name__ == "__main__":
    asyncio.run(main())
```

**Why a 5-second outer loop, not a 60-second one:** the worker polls Postgres every 5 seconds for monitors whose `next_check_at <= now()`, not once a minute. This means a monitor with a 30-second interval is actually checked close to every 30 seconds, not snapped to a single global 60-second grid — each monitor's schedule is independent and accurate to within the 5-second poll granularity.

**Why `next_check_at` is updated *after* the check completes, not before:** if it were set before dispatch, a slow or hanging check could still be "in flight" when the next tick's query runs, and would not be re-queued — but worse, if it crashed before updating, the monitor would never get a new `next_check_at` and would be checked on every single tick forever. Updating after completion, inside the same transaction as recording the result, keeps the schedule and the check history consistent.

**`FOR UPDATE SKIP LOCKED`** costs one SQL clause and buys correctness for free if a second worker replica is ever added — without it, two workers would race to check the same due monitors.

**Graceful shutdown:** `SIGTERM` sets an event the main loop checks between ticks, so `docker compose down` or a redeploy doesn't kill a check mid-write and leave a monitor's `next_check_at` stale.

### 4.6 Duplicate-URL handling (`api/routes/monitors.py`)

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

router = APIRouter()

@router.post("/monitors", response_model=UrlResponse, status_code=201)
async def create_monitor(payload: UrlCreate, session = Depends(get_session)):
    await monitor_service.validate_target(payload.url)
    try:
        monitor = await monitor_repo.create(session, payload)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="This URL is already registered")
    return monitor
```

Without this, registering the same URL twice (an easy accident when re-running a `curl` command from a README) would surface as an unhandled 500 from the database's `UNIQUE` constraint instead of a clean, expected 409.

### 4.7 Toggle-active endpoint

```python
@router.patch("/monitors/{monitor_id}", response_model=UrlResponse)
async def update_monitor(monitor_id: uuid.UUID, payload: UrlUpdate, session = Depends(get_session)):
    monitor = await monitor_repo.update(session, monitor_id, payload)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    await session.commit()
    return monitor
```

Wires up `is_active` to something reachable — a soft-disable without deleting check history.

### 4.8 Manual check-now endpoint

```python
@router.post("/monitors/{monitor_id}/check", response_model=HealthCheckResponse)
async def trigger_check(monitor_id: uuid.UUID, session = Depends(get_session)):
    monitor = await monitor_repo.get(session, monitor_id)
    if monitor is None:
        raise HTTPException(status_code=404, detail="Monitor not found")
    result = await ping_url(monitor.url, monitor.timeout_ms)
    new_state = monitor_service.apply_check_result(monitor, result["is_up"])
    check = await monitor_repo.record_check(session, monitor.id, result)
    await monitor_repo.update_schedule(session, monitor.id, new_state, monitor.consecutive_failures)
    await session.commit()
    return check
```

Lets a reviewer verify up/down state immediately instead of waiting up to 60 seconds — this directly answers the assignment's literal instruction to *"document the exact steps to reproduce this test so our team can instantly verify it locally."*

### 4.9 Structured logging (`core/logging.py`)

```python
import logging
import json
import sys

class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "request_id"):
            payload["request_id"] = record.request_id
        if hasattr(record, "monitor_id"):
            payload["monitor_id"] = record.monitor_id
        return json.dumps(payload)

def configure_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.getLogger().handlers = [handler]
    logging.getLogger().setLevel(logging.INFO)
```

A request-ID middleware injects a `request_id` into each API request's logs (and returns it in a response header), and the worker tags each check log line with `monitor_id` — enough to grep one request's or one monitor's full lifecycle out of the logs without adding a logging stack.

### 4.10 Rate limiting (`core/middleware.py`)

A simple in-process token-bucket limiter on `POST /monitors` (e.g. 10 requests/minute per IP) — no Redis required at single-instance scale. Explicitly noted in the README: a multi-instance deployment would need this moved to a shared store; not needed for this MVP's single `api` container.

---

## 5. API Surface

| Method | Path | Description | Request | Response |
|---|---|---|---|---|
| `POST` | `/api/monitors` | Register a URL — schema validation + SSRF check + duplicate handling | `UrlCreate` | `UrlResponse` |
| `GET` | `/api/monitors` | List all, latest check embedded via one query (no N+1) | — | `List[UrlWithStatus]` |
| `GET` | `/api/monitors/{id}/history` | Last N checks for one monitor | `?limit=20` | `List[HealthCheckResponse]` |
| `POST` | `/api/monitors/{id}/check` | Trigger an immediate check, bypassing the schedule | — | `HealthCheckResponse` |
| `PATCH` | `/api/monitors/{id}` | Toggle `is_active` | `UrlUpdate` | `UrlResponse` |
| `DELETE` | `/api/monitors/{id}` | Remove a monitor and its history | — | `204 No Content` |
| `GET` | `/health` | Liveness probe, includes worker heartbeat | — | `{"status": "ok", "db": "ok", "worker_last_tick_at": "..."}` |

**List endpoint query (LATERAL join, avoids N+1):**

```sql
SELECT m.*, c.status_code, c.response_time_ms, c.is_up, c.error, c.checked_at
FROM monitors m
LEFT JOIN LATERAL (
    SELECT * FROM health_checks
    WHERE url_id = m.id
    ORDER BY checked_at DESC
    LIMIT 1
) c ON true
WHERE m.is_active = true
ORDER BY m.created_at DESC;
```

A naive implementation queries all monitors, then loops and queries the latest check per monitor inside the route handler — one extra round-trip per row. The `LATERAL` join does it in one query regardless of how many monitors exist.

### 5.1 `main.py` (FastAPI bootstrap)

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from .database import engine, Base
from .core.logging import configure_logging
from .core.middleware import RequestIdMiddleware, RateLimitMiddleware
from .api.routes import monitors, health

@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield

app = FastAPI(title="Uptime Monitor", lifespan=lifespan)

app.add_middleware(CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://frontend:3000"],
    allow_methods=["*"], allow_headers=["*"])
app.add_middleware(RequestIdMiddleware)
app.add_middleware(RateLimitMiddleware)

app.include_router(monitors.router, prefix="/api")
app.include_router(health.router)
```

Note `main.py` no longer starts a scheduler — that responsibility moved entirely to the `worker` process. The API process only ever does request/response work.

---

## 6. Frontend (Next.js)

### 6.1 Page / component structure

```
app/page.tsx                     — Server Component, fetches initial monitor list
  ├── <AddUrlForm />              — Client Component, posts to /api/monitors
  └── <UrlTable client>           — Client Component, polls /api/monitors every 7-10s
        └── <UrlRow>
              ├── <StatusBadge /> — green UP / red DOWN / gray UNKNOWN
              └── "Check now" button → POST /api/monitors/{id}/check
app/monitors/[id]/page.tsx        — detail view, fetches /api/monitors/{id}/history
```

### 6.2 Data fetching strategy

- Initial load happens server-side (Next.js Server Component) — no loading-spinner flash on first paint.
- The live table is an isolated Client Component that polls every 7–10 seconds via `fetch` + `useEffect`/`setInterval` (or `swr` with `refetchInterval`, which adds request deduplication for free at the cost of one dependency).
- On add/delete: optimistic UI update, then re-fetch to reconcile.

```tsx
// UrlTable.tsx — core polling pattern
"use client";
import { useEffect, useState } from "react";

export function UrlTable({ initial }: { initial: Monitor[] }) {
  const [monitors, setMonitors] = useState(initial);

  useEffect(() => {
    const fetchMonitors = () =>
      fetch("/api/monitors").then(r => r.json()).then(setMonitors);
    const interval = setInterval(fetchMonitors, 8000);
    return () => clearInterval(interval);
  }, []);

  return (/* table rendering monitors, one <UrlRow> per monitor */);
}
```

### 6.3 UI design decisions

- Dark, infra-tool-appropriate theme — charcoal background, monospace font for URLs.
- Color semantics: `up` = green, `down` = red, `unknown` = gray.
- Response time column color-coded: <200ms green, 200–800ms amber, >800ms red.
- "Last checked" shown as relative time ("2 minutes ago").
- "Check now" button per row calls the manual-check endpoint directly — no full-page wait to verify a newly added monitor.
- No pagination — the assignment scope is "a few dozen URLs."

---

## 7. Containerization

### 7.1 `docker-compose.yml`

```yaml
version: "3.9"

services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: uptime
      POSTGRES_PASSWORD: uptime
      POSTGRES_DB: uptime
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U uptime"]
      interval: 5s
      timeout: 5s
      retries: 5

  api:
    build: ./backend
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql+asyncpg://uptime:uptime@db:5432/uptime
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  worker:
    build: ./backend           # same image as api, different command
    command: python -m worker.main
    environment:
      DATABASE_URL: postgresql+asyncpg://uptime:uptime@db:5432/uptime
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped

  frontend:
    build: ./frontend
    ports:
      - "3000:3000"
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:8000/api
    depends_on:
      - api

volumes:
  pgdata:
```

**Key points:**
- `depends_on: condition: service_healthy` on `db` for both `api` and `worker` — eliminates the most common Docker Compose failure in take-home projects (app containers connecting before Postgres finishes initializing).
- `api` and `worker` build from the **same Dockerfile and image**, differing only in `command` — one build, two processes, cleanly separable later without being two separate codebases today.
- `restart: unless-stopped` keeps both services alive if either crashes.

### 7.2 Backend Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ./app ./app
COPY ./worker ./worker
# Default command is overridden per-service in docker-compose.yml
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 7.3 Frontend Dockerfile

```dockerfile
FROM node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/package.json ./
COPY --from=builder /app/node_modules ./node_modules
EXPOSE 3000
CMD ["npm", "start"]
```

Multi-stage build: builder stage compiles the Next.js app; final stage runs only the production server output.

### 7.4 `requirements.txt`

```
fastapi==0.111.0
uvicorn[standard]==0.29.0
sqlalchemy==2.0.30
asyncpg==0.29.0
httpx==0.27.0
pydantic[email]==2.7.1
pydantic-settings==2.2.1
python-dotenv==1.0.1
alembic==1.13.1
pytest==8.2.0
pytest-asyncio==0.23.6
```

Note `asyncpg` replaces `psycopg2-binary` — the async driver matching the async SQLAlchemy engine, not the sync one.

---

## 8. README.md — Required Content

### 8.1 One-line setup

```bash
docker compose up --build
```

### 8.2 Access points

| Service | URL |
|---|---|
| Frontend dashboard | http://localhost:3000 |
| Backend API | http://localhost:8000 |
| API docs (Swagger) | http://localhost:8000/docs |

### 8.3 Testing UP and DOWN states

**Step 1 — Add a working URL (should show UP):**
```bash
curl -X POST http://localhost:8000/api/monitors \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "label": "Example (should be UP)"}'
```

**Step 2 — Add a broken URL (should show DOWN):**
```bash
curl -X POST http://localhost:8000/api/monitors \
  -H "Content-Type: application/json" \
  -d '{"url": "https://this-domain-does-not-exist-xyz.com", "label": "Broken (should be DOWN)"}'
```

**Step 3 — Trigger an immediate check (don't wait for the schedule):**
```bash
# Get the id from step 1/2's response, then:
curl -X POST http://localhost:8000/api/monitors/{id}/check
```
Note: a single failed check shows `is_up: false` on that check row immediately, but `current_state` on the monitor only flips to `down` after **two consecutive** failures (see §4.3) — call `/check` twice on the broken URL to see the dashboard badge turn red, or simply wait for the next two scheduled ticks.

**Step 4 — View the dashboard** at http://localhost:3000. Expect one green UP row and, after the second failed check, one red DOWN row.

### 8.4 What's explicitly excluded (and why)

No Redis, no message queue, no multi-replica worker (`SKIP LOCKED` makes that safe to add later, not necessary now), no alerting integrations, no WebSockets — each omitted because the stated scale (a few dozen URLs, ~60s interval) doesn't justify the added complexity, not because they were overlooked.

---

## 9. Deployment Sketch (Terraform / AWS)

```hcl
# Hypothetical AWS deployment — ECS Fargate (api + worker) + RDS + ALB
# Not hardening security groups or secrets management here — out of scope per the assignment brief.

provider "aws" {
  region = "us-east-1"
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"
  name    = "uptime-monitor-vpc"
  cidr    = "10.0.0.0/16"
  azs     = ["us-east-1a", "us-east-1b"]
  public_subnets  = ["10.0.1.0/24", "10.0.2.0/24"]
  private_subnets = ["10.0.3.0/24", "10.0.4.0/24"]
  enable_nat_gateway = true
}

resource "aws_db_subnet_group" "main" {
  name       = "uptime-monitor-db-subnets"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_db_instance" "postgres" {
  identifier           = "uptime-monitor-db"
  engine               = "postgres"
  engine_version       = "16"
  instance_class       = "db.t3.micro"   # ~$15/mo, fine for MVP
  allocated_storage    = 20
  db_name              = "uptime"
  username             = "uptime"
  password             = var.db_password # from secrets manager / env, not committed
  db_subnet_group_name = aws_db_subnet_group.main.name
  skip_final_snapshot  = true
}

resource "aws_ecs_cluster" "main" {
  name = "uptime-monitor"
}

resource "aws_ecr_repository" "backend" {
  name = "uptime-monitor-backend"
}

resource "aws_iam_role" "ecs_exec" {
  name = "uptime-monitor-ecs-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })
}

# --- ECS Task: api ---
resource "aws_ecs_task_definition" "api" {
  family                   = "uptime-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.backend.repository_url}:latest"
    command   = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
    portMappings = [{ containerPort = 8000 }]
    environment = [
      { name = "DATABASE_URL", value = "postgresql+asyncpg://uptime:${var.db_password}@${aws_db_instance.postgres.endpoint}/uptime" }
    ]
  }])
}

# --- ECS Task: worker — same image, no ALB target, different command ---
resource "aws_ecs_task_definition" "worker" {
  family                   = "uptime-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.ecs_exec.arn

  container_definitions = jsonencode([{
    name    = "worker"
    image   = "${aws_ecr_repository.backend.repository_url}:latest"
    command = ["python", "-m", "worker.main"]
    environment = [
      { name = "DATABASE_URL", value = "postgresql+asyncpg://uptime:${var.db_password}@${aws_db_instance.postgres.endpoint}/uptime" }
    ]
  }])
}

resource "aws_ecs_service" "api" {
  name            = "uptime-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets = module.vpc.private_subnets
  }
}

resource "aws_ecs_service" "worker" {
  name            = "uptime-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = 1
  launch_type     = "FARGATE"
  network_configuration {
    subnets = module.vpc.private_subnets
  }
}

resource "aws_lb" "main" {
  name               = "uptime-alb"
  load_balancer_type = "application"
  subnets            = module.vpc.public_subnets
}
# ALB target group points only at the `api` service — `worker` is not HTTP-facing.

# Frontend served from S3 + CloudFront (Next.js static export, or SSR on a small Fargate service if dynamic routes are needed)
resource "aws_s3_bucket" "frontend" {
  bucket = "uptime-monitor-frontend"
}

resource "aws_cloudfront_distribution" "frontend" {
  # ... origin = S3 bucket, default root object = index.html,
  # custom error response 404 -> /index.html for SPA-style routing
}
```

**Deployment topology summary:**
- **Frontend** — S3 + CloudFront (or a small Fargate service if Next.js SSR routes are used in production)
- **API** — ECS Fargate service, behind the ALB
- **Worker** — ECS Fargate service, same image as API with a different command, **no ALB target** since it's not HTTP-facing — mirrors the local `docker-compose` split exactly
- **Database** — RDS Postgres `db.t3.micro`
- **Estimated MVP cost:** ~$25–40/month

---

## 10. AI Collaboration Log (`AI_LOG.md`) — Required Structure

This is the most critical deliverable per the assignment brief. Structure as follows, replacing the illustrative entries below with your actual raw interactions:

### 10.1 AI tech stack used
- **Claude (Sonnet)** — architecture design, backend scaffolding, schema design, debugging
- **GitHub Copilot** — inline autocomplete during manual refinement

### 10.2 Prompts that shipped it

Document the actual prompts used to generate:
1. The backend scaffold (models, schemas, repositories, services, worker loop)
2. The frontend (Next.js dashboard, polling table, add-monitor form)
3. The `docker-compose.yml` and both Dockerfiles

### 10.3 Course corrections — real findings to document

1. **Sync DB calls inside an async worker job.** An early draft mixed a synchronous SQLAlchemy session inside an `async def` scheduler function alongside `await asyncio.gather(...)` for concurrent HTTP pings — blocking the event loop during what should have been non-blocking I/O. Fixed by switching to the async SQLAlchemy engine and `asyncpg` driver throughout.
2. **A single global scheduler tick instead of per-monitor scheduling.** The first scheduling approach used one fixed interval that pinged every URL on the same clock, with no way to give different monitors different intervals and no state that survived a restart. Replaced with a `next_check_at` column per monitor, polled by a dedicated worker process.
3. **No SSRF consideration in the first draft.** `Pydantic`'s `HttpUrl` validates that a string is shaped like a URL, not that it's *safe* to repeatedly fetch from inside the server's own network. Added a DNS-resolve-then-check step blocking private/loopback/link-local ranges before a monitor's first check.
4. **Duplicate URL registration caused an unhandled 500.** The `UNIQUE` constraint on `url` existed in the schema with no corresponding `IntegrityError` handling in the route — re-running the same `curl` command from the README would crash instead of returning a clean conflict response.
5. **CORS / startup ordering.** AI-generated `main.py` initially omitted CORS middleware, and `docker-compose.yml` initially used a plain `depends_on` list without `condition: service_healthy`, causing the API to attempt a DB connection before Postgres finished initializing on first boot.

---

## 11. Execution Checklist (Build Order)

1. `mkdir uptime-monitor && cd uptime-monitor`
2. Scaffold backend: prompt AI for the FastAPI app with the layered structure in §2 — `models.py`, `schemas.py`, `repositories/`, `services/`, `worker/main.py`
3. Scaffold frontend: prompt AI for the Next.js dashboard described in §6
4. Write `docker-compose.yml` and both Dockerfiles per §7
5. Run `docker compose up --build` — expect `db` healthy first (~5s), then `api` and `worker`, then `frontend`
6. Open http://localhost:8000/docs — verify all 7 endpoints appear in Swagger
7. Run the test sequence from §8.3
8. Confirm the dashboard at http://localhost:3000 shows one green UP and (after a second failed check) one red DOWN row
9. Write `AI_LOG.md` using §10 as the template, with actual prompts and a real course-correction example
10. Write `README.md` using §8 content
11. `git init && git add . && git commit -m "initial: uptime monitor MVP" && git push`

---

## 12. What Makes This Stand Out to a Reviewer

| What reviewers look for | How this PRD covers it |
|---|---|
| Does it actually run, first try? | `condition: service_healthy` on both `api` and `worker` eliminates the most common Compose failure |
| Is the architecture sensible for this scale? | Worker separated from API, but still just 4 containers and zero new infrastructure (no Redis/queue) |
| Does the down-state logic reflect real operational thinking? | Consecutive-failure threshold instead of flipping state on one bad ping |
| Is the code clean and layered? | `services/` + `repositories/` shared by both `api` and `worker` — no duplicated logic |
| Was a real security gap considered? | SSRF guard on URL registration, explicitly scoped (DNS rebinding noted as out-of-bounds) |
| Can a reviewer verify quickly? | Manual `/check` endpoint avoids a 60-second wait during evaluation |
| Swagger docs? | FastAPI auto-generates them at `/docs` — zero extra code |
| Cloud sketch? | Terraform mirrors the local `api`/`worker` split, with a realistic cost estimate |
| AI log quality? | Real course corrections (async/sync bug, duplicate-URL crash, missing SSRF check) — not sanitized fluff |

---

*PRD — Final Merged Version, ready for implementation.*
