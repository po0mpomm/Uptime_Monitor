<div align="center">
  <h1>🟢 Uptime Monitor MVP</h1>
  <p><strong>A lightweight, high-performance, full-stack URL monitoring service built for speed and reliability.</strong></p>

  [![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](#)
  [![Next.js](https://img.shields.io/badge/Next.js_15-000000?style=for-the-badge&logo=next.js&logoColor=white)](#)
  [![PostgreSQL](https://img.shields.io/badge/PostgreSQL_16-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](#)
  [![Docker](https://img.shields.io/badge/Docker_Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)](#)
</div>

<br/>

Register URLs, ping them on a configurable schedule, and watch their live up/down status on a responsive real-time dashboard. Designed with an emphasis on execution velocity and clean architecture over complex over-engineering.

---

## 🚀 One-Line Setup

Get the entire ecosystem running locally right out of the box:

```bash
docker compose up --build
```

> **Note:** Wait ~30–60 seconds for all 4 containers to start. The API and Worker containers automatically wait for the Postgres database to become fully healthy before connecting.

---

## 📍 Access Points

| Component | URL | Description |
|---|---|---|
| 🖥️ **Frontend Dashboard** | [http://localhost:3000](http://localhost:3000) | Next.js Real-time UI |
| ⚙️ **Backend API** | [http://localhost:8000](http://localhost:8000) | FastAPI Base URL |
| 📖 **API Docs** | [http://localhost:8000/docs](http://localhost:8000/docs) | Interactive Swagger UI |
| 🩺 **Health Probe** | [http://localhost:8000/health](http://localhost:8000/health) | DB Status & Worker Heartbeat |

---

## 🧪 Testing UP and DOWN States

Verify the system's core state machine and behavior directly from your terminal.

### Step 1 — Add a working URL (Should show UP)
```bash
curl -X POST http://localhost:8000/api/monitors \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "label": "Example (should be UP)"}'
```

### Step 2 — Add a broken URL (Should show DOWN)
```bash
curl -X POST http://localhost:8000/api/monitors \
  -H "Content-Type: application/json" \
  -d '{"url": "https://this-domain-does-not-exist-xyz.com", "label": "Broken (should be DOWN)"}'
```

### Step 3 — Trigger immediate checks 
Bypass the scheduled worker to test immediately:
```bash
# Replace {id} with the UUID from Step 1 or Step 2's JSON response
curl -X POST http://localhost:8000/api/monitors/{id}/check
```

> ⚠️ **Important (The 2-Failure Rule):** A single failed check immediately records `is_up: false` on the history row. However, the monitor's `current_state` strictly flips to `"down"` only after **two consecutive failures**. Run the `/check` endpoint twice on the broken URL to see the dashboard badge turn red!

### Step 4 — View the Dashboard
Navigate to **[http://localhost:3000](http://localhost:3000)**. The dashboard auto-polls every 8 seconds, dynamically updating the status and response times without a manual page reload.

---

## 🏗️ Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│ docker-compose.yml                                              │
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐   │
│  │  db      │◄───│  api     │    │  worker  │    │ frontend │   │
│  │ postgres │    │ fastapi  │    │ asyncio  │    │ next.js  │   │
│  │  :5432   │    │  :8000   │    │ loop     │    │  :3000   │   │
│  └──────────┘    └──────────┘    └──────────┘    └──────────┘   │
│       ▲               ▲               ▲               │         │
│       │               │               │               │         │
│       └───────────────┴───────────────┘               │         │
│                  PostgreSQL (single source of truth)  │         │
│                                                       ▼         │
│                                              browser polling    │
└─────────────────────────────────────────────────────────────────┘
```

### 🧠 Key Design Decisions

| Decision | Rationale |
|---|---|
| 🔄 **Separate `worker` process** | Worker crashes don't affect API latency; API restarts don't reset schedule state. |
| ⏱️ **Per-monitor `next_check_at`** | Survives restarts; enables different intervals per monitor; accurate within ±5s. |
| 🔒 **`FOR UPDATE SKIP LOCKED`** | Safe for future multi-replica worker scaling at zero cost today. |
| 🚦 **2-Failure Threshold** | A dropped packet is not an outage. Flips to `down` only on consecutive failures. |
| 🛡️ **SSRF Guard Validation** | Prevents the server from polling internal/metadata endpoints from inside its own VPC. |
| ⚡ **`LATERAL` SQL Joins** | Fetches the latest check embedded in the monitor list. Exactly one query — no N+1. |
| 🐘 **Postgres over Redis/Celery** | Postgres-as-queue is perfectly right-sized for a few dozen URLs. |

---

## 🌐 API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/monitors` | Register a URL (SSRF-checked, validated, deduped) |
| `GET` | `/api/monitors` | List all active monitors with latest check embedded |
| `GET` | `/api/monitors/{id}/history` | Last N checks for one monitor (`?limit=50`) |
| `POST` | `/api/monitors/{id}/check` | Trigger an immediate check, bypassing the schedule |
| `PATCH` | `/api/monitors/{id}` | Toggle `is_active` (soft disable without deleting history) |
| `DELETE` | `/api/monitors/{id}` | Remove monitor + all history (Cascade) |
| `GET` | `/health` | Liveness probe — DB status + worker heartbeat |

Explore the full interactive documentation at **[http://localhost:8000/docs](http://localhost:8000/docs)**.

---

## ⚙️ Configuration

All settings are environment variables. See `docker-compose.yml` for local development defaults.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Async Postgres connection string |
| `WORKER_POLL_INTERVAL_SECONDS` | `5` | How often the worker polls for due monitors |
| `WORKER_HEARTBEAT_STALE_THRESHOLD_SECONDS` | `60` | `/health` returns 503 if worker hasn't ticked recently |
| `RATE_LIMIT_PER_MINUTE` | `10` | Max `POST /monitors` requests per IP per minute |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:3000,...` | Comma-separated CORS allowlist |

---

## ☁️ Deployment Sketch

The included `infra/main.tf` outlines a hypothetical, low-cost deployment on AWS using Infrastructure-as-Code. 

**Topology:**
- **Compute:** ECS Fargate (running the API and Worker containers)
- **Database:** RDS PostgreSQL 16 (in private subnets)
- **Network:** Application Load Balancer (ALB)
- **Frontend:** Next.js static export hosted on S3 + CloudFront

*Estimated cost: ~$25–40/month for an MVP-scale deployment.*

---

## 📁 Repository Structure

```text
uptime-monitor/
├── backend/
│   ├── app/                 # FastAPI Application
│   │   ├── api/             # HTTP endpoints
│   │   ├── services/        # Business logic & SSRF guards
│   │   ├── repositories/    # Database queries
│   │   └── models.py        # SQLAlchemy ORM
│   ├── worker/              # Standalone Asyncio Scheduler
│   └── tests/               # Pytest suite
├── frontend/
│   ├── app/                 # Next.js App Router
│   └── components/          # React Components (UrlTable, StatusBadge)
├── infra/                   # Terraform IaC Deployment Sketch
├── AI_LOG.md                # Detailed AI Collaboration Log
└── docker-compose.yml       # Local Environment Orchestration
```
