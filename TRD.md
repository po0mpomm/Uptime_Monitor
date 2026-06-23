# Technical Requirements Document
## Uptime Monitor — MVP

> **Companion to:** PRD.md (final merged version)
> **Scope:** This TRD specifies *how* the system is built to the level required to implement, test, and operate it without re-deriving design decisions. Where the PRD answers "what and why," this document answers "exactly how, and what happens at the edges."

---

## 1. Document Purpose & Conventions

### 1.1 Purpose
This TRD is the implementation contract. Any engineer (or AI assistant) building from this document should be able to produce a system that matches the PRD's architecture without making unstated judgment calls on schema types, error codes, timeouts, concurrency limits, or failure handling — all of those are pinned down here.

### 1.2 Conventions used in this document
- **MUST / MUST NOT** — hard requirement, a deviation is a defect
- **SHOULD / SHOULD NOT** — strong default, deviation requires a documented reason
- **MAY** — optional, implementer's discretion
- All timestamps are UTC, stored as `TIMESTAMPTZ` in Postgres, serialized as ISO 8601 with `Z` suffix over the wire
- All durations in this document are in milliseconds unless explicitly stated otherwise (`interval_seconds` is the one named exception, kept in seconds for human readability in the API)

---

## 2. System Context

### 2.1 Components

| Component | Responsibility | MUST NOT do |
|---|---|---|
| `api` | Handle HTTP requests: CRUD on monitors, manual check trigger, health reporting | MUST NOT run the scheduling loop or perform background pings |
| `worker` | Poll for due monitors, execute HTTP checks, persist results, advance schedule | MUST NOT serve HTTP traffic (no port binding except an optional internal-only metrics port if added later) |
| `frontend` | Render dashboard, poll `api` for state, submit new monitors | MUST NOT talk to Postgres directly |
| `db` (Postgres) | Single source of truth for monitor config, schedule state, and check history | — |

### 2.2 Process boundary rule
`api` and `worker` MUST share the same Python package (`app/`) for all domain logic (`services/`, `repositories/`, `models.py`). They MUST NOT duplicate business logic across two independent codebases. The only difference between the two containers is the process entrypoint (`uvicorn app.main:app` vs `python -m worker.main`).

---

## 3. Data Model — Full DDL

### 3.1 Entity-relationship overview

```mermaid
erDiagram
  MONITORS ||--o{ HEALTH_CHECKS : has
  MONITORS {
    uuid id PK
    string url
    string label
    int interval_seconds
    int timeout_ms
    timestamptz next_check_at
    int consecutive_failures
    string current_state
    boolean is_active
    timestamptz created_at
  }
  HEALTH_CHECKS {
    bigint id PK
    uuid url_id FK
    timestamptz checked_at
    int status_code
    float response_time_ms
    boolean is_up
    string error
  }
  WORKER_STATUS {
    int id PK
    timestamptz last_tick_at
    int monitors_checked_last_tick
  }
```

### 3.2 Complete DDL (as Alembic migration `0001_initial.py` equivalent SQL)

```sql
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE monitors (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url                    VARCHAR(2048) NOT NULL,
    label                  VARCHAR(255),
    interval_seconds       INTEGER NOT NULL DEFAULT 60
                           CHECK (interval_seconds BETWEEN 30 AND 3600),
    timeout_ms             INTEGER NOT NULL DEFAULT 5000
                           CHECK (timeout_ms BETWEEN 1000 AND 30000),
    next_check_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    consecutive_failures   INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    current_state          VARCHAR(16) NOT NULL DEFAULT 'unknown'
                           CHECK (current_state IN ('unknown', 'up', 'down')),
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_monitors_url UNIQUE (url)
);

CREATE TABLE health_checks (
    id                  BIGSERIAL PRIMARY KEY,
    url_id              UUID NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    status_code         INTEGER,
    response_time_ms    DOUBLE PRECISION,
    is_up               BOOLEAN NOT NULL,
    error               VARCHAR(32)
                        CHECK (error IS NULL OR error IN
                               ('timeout', 'dns_error', 'connection_refused', 'http_error', 'unknown_error'))
);

CREATE TABLE worker_status (
    id                          INTEGER PRIMARY KEY DEFAULT 1,
    last_tick_at                TIMESTAMPTZ,
    monitors_checked_last_tick  INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT singleton_row CHECK (id = 1)
);

INSERT INTO worker_status (id, last_tick_at, monitors_checked_last_tick)
VALUES (1, NULL, 0)
ON CONFLICT (id) DO NOTHING;

CREATE INDEX idx_health_checks_url_id_checked_at
    ON health_checks (url_id, checked_at DESC);

CREATE INDEX idx_monitors_next_check_at
    ON monitors (next_check_at)
    WHERE is_active = TRUE;
```

**Rationale for the partial index on `next_check_at`:** the worker's due-monitor query always filters `WHERE is_active = TRUE AND next_check_at <= now()`. A partial index that only indexes active rows keeps the index smaller and the due-monitor scan fast even as inactive/soft-deleted monitors accumulate.

### 3.3 Column-level constraints and rationale

| Table | Column | Constraint | Why |
|---|---|---|---|
| `monitors` | `interval_seconds` | CHECK 30–3600 | Floor prevents accidental hammering of a target; ceiling keeps the MVP's "checked every minute or so" framing honest |
| `monitors` | `timeout_ms` | CHECK 1000–30000 | Floor avoids false-negative timeouts on slow-but-healthy targets; ceiling bounds worst-case worker tick time |
| `monitors` | `url` | UNIQUE | One monitor per distinct URL string; duplicate registration is a 409, not a second row |
| `monitors` | `current_state` | CHECK enum | Application-level invariant enforced at the DB layer too, defends against a bug writing an invalid string |
| `health_checks` | `url_id` | ON DELETE CASCADE | Deleting a monitor MUST remove its history — no orphaned check rows |
| `health_checks` | `error` | CHECK enum (nullable) | Closed vocabulary of failure reasons; `unknown_error` is the explicit fallback so the column is never silently NULL for an actual failure |
| `worker_status` | `id` | CHECK (id = 1) | Enforces a true singleton row at the DB level, not just by convention |

---

## 4. API Contract — Full Specification

### 4.1 Conventions
- Base path: `/api`
- All request/response bodies are `application/json`
- All endpoints return `Content-Type: application/json; charset=utf-8`
- Every response includes an `X-Request-Id` header (see §8.2)

### 4.2 `POST /api/monitors`

**Request body:**
```json
{
  "url": "https://example.com",
  "label": "Example site",
  "interval_seconds": 60,
  "timeout_ms": 5000
}
```

| Field | Type | Required | Default | Validation |
|---|---|---|---|---|
| `url` | string | yes | — | Must parse as `http://` or `https://`; max 2048 chars |
| `label` | string \| null | no | `null` | max 255 chars |
| `interval_seconds` | int | no | `60` | 30 ≤ x ≤ 3600 |
| `timeout_ms` | int | no | `5000` | 1000 ≤ x ≤ 30000 |

**Success — `201 Created`:**
```json
{
  "id": "b3f1c2e4-...",
  "url": "https://example.com",
  "label": "Example site",
  "interval_seconds": 60,
  "is_active": true,
  "current_state": "unknown",
  "created_at": "2026-06-22T10:00:00Z"
}
```

**Error responses:**

| Status | Condition | Body shape |
|---|---|---|
| `400` | Malformed URL, scheme not http/https, or out-of-range field | `{"detail": "<field>: <reason>"}` |
| `400` | URL resolves to a blocked network (SSRF guard) | `{"detail": "target resolves to a blocked network"}` |
| `409` | URL already registered | `{"detail": "This URL is already registered"}` |
| `422` | Pydantic schema validation failure (wrong type, missing required field) | FastAPI default validation error shape |
| `429` | Rate limit exceeded (>10 req/min/IP) | `{"detail": "Rate limit exceeded, try again later"}` |
| `503` | Database unreachable | `{"detail": "Service temporarily unavailable"}` |

### 4.3 `GET /api/monitors`

**Query params:** none required. `?active_only=true` MAY be supported (default `true`).

**Success — `200 OK`:**
```json
[
  {
    "id": "b3f1c2e4-...",
    "url": "https://example.com",
    "label": "Example site",
    "interval_seconds": 60,
    "is_active": true,
    "current_state": "up",
    "created_at": "2026-06-22T10:00:00Z",
    "latest_check": {
      "id": 4821,
      "url_id": "b3f1c2e4-...",
      "checked_at": "2026-06-22T10:14:02Z",
      "status_code": 200,
      "response_time_ms": 184.32,
      "is_up": true,
      "error": null
    }
  }
]
```
`latest_check` is `null` if the monitor has never been checked (i.e. `current_state` is still `unknown`).

**Performance requirement:** this endpoint MUST execute exactly one SQL query regardless of the number of monitors (see §6.1 LATERAL join). It MUST NOT issue a per-row follow-up query.

### 4.4 `GET /api/monitors/{id}/history`

**Query params:** `limit` (int, default 20, max 200)

**Success — `200 OK`:** array of `HealthCheckResponse`, ordered `checked_at DESC`.

**Error:** `404` if `id` doesn't exist — `{"detail": "Monitor not found"}`

### 4.5 `POST /api/monitors/{id}/check`

Synchronously executes one ping against the monitor's URL, using its configured `timeout_ms`, applies the consecutive-failure state transition, persists the result, and returns it.

**Success — `200 OK`:** a single `HealthCheckResponse` object.

**Latency contract:** this endpoint's response time is bounded by `timeout_ms + ~50ms` overhead. With the default 5000ms timeout, the worst case is ~5.05s. The frontend MUST show a loading state on the "check now" button for the duration of this call.

**Error:** `404` if monitor doesn't exist.

### 4.6 `PATCH /api/monitors/{id}`

**Request body:** `{"is_active": false}` (only `is_active` is mutable via this endpoint in the MVP scope)

**Success — `200 OK`:** updated `UrlResponse`.
**Error:** `404` if monitor doesn't exist.

### 4.7 `DELETE /api/monitors/{id}`

**Success — `204 No Content`**, empty body.
**Error:** `404` if monitor doesn't exist.
**Side effect:** cascades to delete all `health_checks` rows for this monitor (enforced by the FK, not application code).

### 4.8 `GET /health`

**Success — `200 OK`:**
```json
{
  "status": "ok",
  "db": "ok",
  "worker_last_tick_at": "2026-06-22T10:14:05Z",
  "worker_seconds_since_tick": 4
}
```

**Degraded — `503 Service Unavailable`:** returned if the DB ping fails, or if `worker_seconds_since_tick` exceeds a threshold of `60` (configurable), signaling the worker process appears to be dead.

```json
{
  "status": "degraded",
  "db": "ok",
  "worker_last_tick_at": "2026-06-22T09:58:00Z",
  "worker_seconds_since_tick": 974,
  "reason": "worker heartbeat stale"
}
```

This threshold check is what makes the heartbeat operationally useful rather than decorative — a reviewer or an orchestrator's liveness probe gets an actual signal, not just a static `{"status": "ok"}` that's true even if the worker container silently died.

---

## 5. Sequence Specifications

### 5.1 Monitor creation and dashboard polling

The diagram above traces the full path: schema validation → SSRF guard → insert with duplicate handling → 201, followed by the dashboard's recurring `GET /api/monitors` using a single LATERAL-join query. Both error exits (400 for a blocked target, 409 for a duplicate URL) return before any row is committed — there is no partial-write state to clean up.

### 5.2 Worker tick and the consecutive-failure transition

The second diagram traces one full worker tick at the level needed to implement it exactly: the `FOR UPDATE SKIP LOCKED` due-monitor query, the bounded-concurrency dispatch (semaphore = 10), the three-way state decision (success / first failure / second-plus failure), and the ordering guarantee that `next_check_at` is written only after the check result is already durable — not before dispatch.

### 5.3 Why ordering inside `check_one()` matters

This sequence MUST be followed exactly, in this order, within a single check's lifecycle:

1. Execute the HTTP check (`ping_url`) — no DB write yet
2. Compute the new `current_state` via `apply_check_result()` — pure function, no DB write yet
3. `INSERT` the `health_checks` row
4. `UPDATE monitors SET next_check_at = now() + interval, current_state = ..., consecutive_failures = ...`
5. Commit (steps 3 and 4 MUST be in the same transaction)

**Why this exact order is a hard requirement, not a style preference:** if step 4 ran *before* step 1 (i.e., the schedule advanced before the check executed), a worker crash between steps would leave the monitor's `next_check_at` advanced with no corresponding check ever recorded — silently skipping a check with no trace of why. Running step 4 last, and only after step 3 has succeeded in the same transaction, guarantees that every schedule advance has a matching check row, and that a crash before commit leaves the monitor exactly as due as it was — it will simply be picked up on the next tick.

---

## 6. Query Specifications

### 6.1 List endpoint — LATERAL join (exact form)

```sql
SELECT
    m.id, m.url, m.label, m.interval_seconds, m.is_active,
    m.current_state, m.created_at,
    c.id AS check_id, c.status_code, c.response_time_ms,
    c.is_up, c.error, c.checked_at
FROM monitors m
LEFT JOIN LATERAL (
    SELECT *
    FROM health_checks
    WHERE url_id = m.id
    ORDER BY checked_at DESC
    LIMIT 1
) c ON true
WHERE m.is_active = TRUE
ORDER BY m.created_at DESC;
```

**MUST NOT** be implemented as: fetch all monitors, then loop and query `health_checks` per row. That pattern is O(N) queries for N monitors and is explicitly disallowed by §4.3's performance requirement.

### 6.2 Worker due-monitor query (exact form)

```sql
SELECT id, url, timeout_ms, interval_seconds,
       consecutive_failures, current_state
FROM monitors
WHERE is_active = TRUE
  AND next_check_at <= now()
ORDER BY next_check_at ASC
LIMIT 50
FOR UPDATE SKIP LOCKED;
```

This query MUST run inside its own transaction that is committed (releasing the row locks) before the HTTP checks begin — row locks MUST NOT be held for the duration of an outbound HTTP request, only for the duration of the SELECT itself, otherwise a slow external target would hold a Postgres lock open for the length of its timeout.

### 6.3 SSRF resolution check (exact logic)

```python
BLOCKED_NETWORKS = [
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("127.0.0.0/8"),
    ip_network("169.254.0.0/16"),   # covers the cloud metadata endpoint 169.254.169.254
    ip_network("::1/128"),
    ip_network("fc00::/7"),         # IPv6 unique local addresses
]
```
All resolved addresses for a hostname (not just the first) MUST be checked — `getaddrinfo` can return multiple records, and a hostname that resolves to both a public and a private address MUST still be rejected.

---

## 7. Error Taxonomy

### 7.1 Check-level errors (`health_checks.error` column)

| Value | Triggering condition | HTTP status equivalent | `status_code` value |
|---|---|---|---|
| `null` | Request completed, `status_code < 400` | — | actual code (200–399) |
| `http_error` | Request completed, `status_code >= 400` | — | actual code (400–599) |
| `timeout` | No response within `timeout_ms` | — | `null` |
| `dns_error` | Hostname does not resolve | — | `null` |
| `connection_refused` | TCP connection actively refused, or reset | — | `null` |
| `unknown_error` | Any other exception (SSL error, too-many-redirects, malformed response) | — | `null` |

**Rule:** every row in `health_checks` MUST have either `error IS NULL` (meaning the request completed end-to-end) or a non-null `error` from the closed set above. The application layer MUST NOT leave `error` null on a failed check (`is_up = false` with `error IS NULL` is an invalid state and indicates a bug in the pinger's exception handling).

### 7.2 API-level errors

| Status | Class | Examples |
|---|---|---|
| `400` | Client sent semantically invalid input that passed type-checking | Bad scheme, SSRF-blocked target |
| `404` | Referenced resource does not exist | Unknown monitor `id` |
| `409` | Conflict with existing state | Duplicate URL |
| `422` | FastAPI/Pydantic schema-level validation failure | Missing field, wrong type |
| `429` | Rate limit | More than 10 `POST /monitors` per minute per IP |
| `500` | Unhandled server exception | Should never reach the client uncaught — see §7.3 |
| `503` | Dependency unavailable | DB connection pool exhausted or DB down; reported also via `/health` |

### 7.3 Unhandled exception policy

The API MUST register a global exception handler that catches any exception not explicitly handled by a route, logs it with full traceback at `ERROR` level (with `request_id`), and returns a generic `500 {"detail": "Internal server error"}` to the client — internal exception messages and stack traces MUST NOT be returned in the response body.

### 7.4 Worker error handling policy

A single monitor's check failing (including an exception inside `ping_url` that escapes its own try/except) MUST NOT crash the worker process or block other monitors in the same tick. Each `check_one()` call MUST be wrapped such that an unexpected exception is caught, logged with `monitor_id`, and treated as an `unknown_error` check result — the worker's `asyncio.gather` MUST be called with exception isolation (e.g. wrapping each coroutine so one failure doesn't propagate and cancel sibling tasks).

---

## 8. Non-Functional Requirements

### 8.1 Performance

| Requirement | Target | Rationale / measurement |
|---|---|---|
| Worker tick sweep time | A full sweep of 50 due monitors at 10x concurrency and 5s timeout MUST complete in ≤ 25s | `50 / 10 × 5s = 25s` worst case; gives headroom inside the minimum 30s interval floor |
| `GET /api/monitors` p95 latency | ≤ 150ms at 50 monitors | Single indexed LATERAL-join query, no N+1 |
| `POST /api/monitors` p95 latency | ≤ 300ms (excludes SSRF DNS resolution variance) | Schema validation + one DNS lookup + one insert |
| `POST /api/monitors/{id}/check` latency | Bounded by `timeout_ms + 50ms` | Synchronous, single outbound HTTP call |
| Worker poll granularity | 5s | Determines real-world accuracy of `interval_seconds`; a 30s-interval monitor is checked within ±5s of due, not snapped to a coarser grid |

### 8.2 Observability

**Structured logging — required fields per log line:**

| Field | Present on | Purpose |
|---|---|---|
| `level` | every line | filter by severity |
| `message` | every line | human-readable summary |
| `logger` | every line | originating module |
| `request_id` | every API request's log lines | trace one HTTP request end-to-end |
| `monitor_id` | every worker check log line | trace one monitor's check history through logs |
| `duration_ms` | request completion log lines | latency tracking without a metrics stack |

**Request ID propagation:** the API MUST generate a UUID4 per incoming request (or accept an inbound `X-Request-Id` header if present, for cross-service tracing), attach it to the logging context for the duration of the request, and echo it back in the `X-Request-Id` response header.

**Worker heartbeat:** the `worker_status` singleton row MUST be upserted at the end of every tick, regardless of whether any monitors were due that tick. A tick with zero due monitors MUST still update `last_tick_at` — this is what allows `/health` to distinguish "worker alive, nothing due" from "worker process is dead."

### 8.3 Security

| Requirement | Implementation |
|---|---|
| SSRF protection | DNS-resolve-then-check against the blocked-network list, on every registration (§6.3) |
| Input validation | Pydantic schema boundary on every mutating endpoint; DB-level CHECK constraints as defense in depth |
| Rate limiting | In-process token bucket, 10 req/min/IP on `POST /monitors`; explicitly NOT distributed (single `api` instance assumption stated in README) |
| No secrets in logs | `DATABASE_URL` and any future credentials MUST NOT appear in structured log output — log the fact of a DB connection event, never the connection string |
| CORS | Explicit origin allowlist (`localhost:3000`, `frontend:3000`), not `allow_origins=["*"]` |

**Explicitly out of scope (state in README, not a gap to silently leave):** DNS-rebinding protection after registration, authentication/authorization, TLS termination (delegated to the load balancer in the deployment sketch), secrets-manager integration for `DATABASE_URL` in local dev.

### 8.4 Reliability

| Requirement | Mechanism |
|---|---|
| Survives `worker` container restart | All schedule state (`next_check_at`) lives in Postgres, not in process memory |
| Survives `api` container restart | Stateless; no in-memory session state |
| No double-checking under multi-replica worker (future-proofing, not active today) | `FOR UPDATE SKIP LOCKED` on the due-monitor query |
| Graceful shutdown | `worker` MUST trap `SIGTERM`, finish the in-flight tick, and exit without starting a new tick — MUST NOT hard-kill mid-write |
| No silent check skipping | §5.3 ordering guarantee — `next_check_at` advances only after the check is durably recorded |

### 8.5 Scalability boundaries (explicitly bounded, not solved)

This system is explicitly scoped to **tens of monitors at ~60s+ intervals**. The following are the documented ceilings, not bugs:

| Dimension | Current ceiling | What would need to change beyond it |
|---|---|---|
| Monitor count | Low hundreds | Worker's 50-per-tick `LIMIT` and 10x concurrency would need tuning; still no architecture change required up to ~500 monitors at 60s+ intervals |
| Check frequency | 30s floor (enforced by CHECK constraint) | Sub-30s checking at scale would need the worker's 5s poll loop tightened, increasing DB query load |
| Worker throughput | Single process, single replica | `SKIP LOCKED` already supports horizontal scaling to N worker replicas without further changes |
| Write volume | `health_checks` grows unbounded | No retention policy in MVP scope; a production system would need a TTL/archival job — explicitly noted as future work |

---

## 9. Test Plan

### 9.1 Unit tests (`tests/test_monitor_service.py`)

| Test | Asserts |
|---|---|
| `test_new_monitor_first_failure_does_not_flip_to_down` | A monitor in `unknown` state with 0 prior failures, given one failed check, ends in `unknown` state with `consecutive_failures = 1` |
| `test_second_consecutive_failure_flips_to_down` | A monitor with `consecutive_failures = 1`, given another failed check, ends in `down` state |
| `test_success_resets_failure_count` | A monitor in `down` state with `consecutive_failures = 3`, given a successful check, ends in `up` state with `consecutive_failures = 0` |
| `test_third_consecutive_failure_stays_down` | A monitor already `down`, given another failure, remains `down` (idempotent — doesn't matter how many failures pile up) |
| `test_ssrf_guard_blocks_private_ip_literal` | Registering `http://192.168.1.1` raises `ValidationError` |
| `test_ssrf_guard_blocks_resolved_metadata_endpoint` | A mock DNS resolver returning `169.254.169.254` for a public-looking hostname is still blocked |
| `test_ssrf_guard_allows_public_target` | `https://example.com` (mocked to resolve to a public IP) passes validation |
| `test_ssrf_guard_rejects_non_http_scheme` | `ftp://example.com` raises `ValidationError` before any DNS lookup occurs |

### 9.2 Integration tests (`tests/test_api_monitors.py`, against a real test Postgres instance)

| Test | Asserts |
|---|---|
| `test_create_monitor_returns_201` | Valid payload → `201`, response body matches schema |
| `test_create_duplicate_url_returns_409` | Same URL posted twice → second call returns `409`, exactly one row exists in `monitors` |
| `test_create_blocked_target_returns_400` | URL resolving to a private range → `400`, no row inserted |
| `test_list_monitors_single_query` | `GET /api/monitors` with N monitors issues exactly 1 SQL statement (assert via SQLAlchemy query-count fixture) |
| `test_manual_check_updates_state` | `POST /monitors/{id}/check` against a mocked-down target, called twice, results in `current_state = "down"` on the third related read |
| `test_delete_cascades_history` | Deleting a monitor with existing `health_checks` rows removes both; verified via direct DB query post-delete |
| `test_health_endpoint_reports_stale_worker` | With `worker_status.last_tick_at` manually set to >60s ago, `/health` returns `503` with `"reason": "worker heartbeat stale"` |

### 9.3 Worker tests (`tests/test_worker_tick.py`, using a mocked HTTP layer)

| Test | Asserts |
|---|---|
| `test_tick_picks_up_due_monitors_only` | A monitor with `next_check_at` in the future is not included in a tick's dispatch list |
| `test_tick_respects_concurrency_limit` | With 20 due monitors and a semaphore of 10, no more than 10 concurrent `ping_url` calls are in flight at once (assert via a counting mock) |
| `test_next_check_at_advances_after_completion` | `next_check_at` is only updated once the corresponding `health_checks` row exists — verified by checking DB state mid-mocked-delay |
| `test_skip_locked_excludes_locked_rows` | Two concurrent transactions selecting due monitors with `FOR UPDATE SKIP LOCKED` never select the same row (requires two real DB connections in the test) |
| `test_one_failing_monitor_does_not_block_others` | A monitor whose `ping_url` mock raises an unexpected exception still allows sibling monitors in the same tick to complete and be recorded |
| `test_heartbeat_updates_even_with_zero_due_monitors` | A tick with no due monitors still updates `worker_status.last_tick_at` |

### 9.4 End-to-end manual verification (the brief's literal requirement)

This is the sequence documented in the README (§8.3 of the PRD) and MUST be re-verified before submission:

1. `docker compose up --build` from a clean clone — MUST succeed with no manual intervention
2. Register `https://example.com` — MUST eventually show `current_state: "up"`
3. Register an unreachable domain — MUST show `current_state: "down"` after two consecutive failed checks (use the manual `/check` endpoint twice to avoid waiting on the schedule)
4. Dashboard at `localhost:3000` MUST reflect both states without a page refresh, within one polling interval
5. `DELETE` one monitor — MUST disappear from the dashboard on the next poll, and its `health_checks` rows MUST no longer exist in the DB

### 9.5 Load/sizing sanity check (manual, not automated)

With 50 monitors registered at the default 60s interval and 5s timeout, observe via logs that:
- A full worker tick sweep completes in well under 25s (per §8.1's target)
- `GET /api/monitors` p95 latency stays under 150ms throughout

---

## 10. Configuration Reference

| Env var | Used by | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | `api`, `worker` | — (required) | Format: `postgresql+asyncpg://user:pass@host:5432/db` |
| `LOG_LEVEL` | `api`, `worker` | `INFO` | |
| `WORKER_POLL_INTERVAL_SECONDS` | `worker` | `5` | Outer loop sleep between ticks |
| `WORKER_TICK_BATCH_SIZE` | `worker` | `50` | `LIMIT` on the due-monitor query |
| `WORKER_CONCURRENCY` | `worker` | `10` | Semaphore size for concurrent HTTP checks |
| `WORKER_HEARTBEAT_STALE_THRESHOLD_SECONDS` | `api` | `60` | Used by `/health` to decide `degraded` status |
| `RATE_LIMIT_PER_MINUTE` | `api` | `10` | Per-IP limit on `POST /monitors` |
| `NEXT_PUBLIC_API_URL` | `frontend` | `http://localhost:8000/api` | Build-time env for the Next.js client |
| `CORS_ALLOWED_ORIGINS` | `api` | `http://localhost:3000,http://frontend:3000` | Comma-separated allowlist |

---

## 11. Traceability — PRD Requirement → TRD Section

| PRD requirement | Specified in TRD section |
|---|---|
| Register a URL | §4.2, §6.3 |
| List with current status | §4.3, §6.1 |
| Per-monitor history | §4.4 |
| Periodic background checking | §5.2, §6.2 |
| Manual check-now | §4.5 |
| Consecutive-failure down-detection | §5.3, §9.1 |
| SSRF protection | §6.3, §8.3 |
| Worker liveness in `/health` | §4.8, §8.2 |
| Graceful shutdown | §8.4 |
| Rate limiting | §8.3 |
| Structured logging | §8.2 |
| Containerization | (see PRD §7 — unchanged) |
| Deployment sketch | (see PRD §9 — unchanged) |

---

*TRD — companion to the final merged PRD. Implementation should treat every MUST in this document as binding.*
