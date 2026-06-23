# AI Collaboration Log

> **Project:** Uptime Monitor MVP
> **AI Tool:** Antigravity (Claude Sonnet 4.6) via Antigravity IDE
> **Approach:** PRD + TRD were written first, then used as base for all prompts.

---

## 1. AI Tools Used

| Tool | Role |
|---|---|
| **Antigravity (Claude Sonnet 4.6)** | Architecture, full backend, frontend, Docker, Terraform, debugging |

---

## 2. Prompts That Shipped It

---

### Prompt 1 — Initial handoff & project setup

```
here is my PRD and TRD for uptime monitor project, read it fully
dont generate any code yet, just tell me the implementation plan and confirm
you understood the architecture

(Given full PRD.md and TRD.md in the project folder)
```

---

### Prompt 2 — Backend scaffold

```
ok now generate the backend for this
use fastapi, postgresql

folder structure like PRD says:
- models.py, schemas.py
- repositories/monitor_repo.py
- services/monitor_service.py
- services/pinger.py
- worker/main.py as a separate process

follow the PRD and TRD for the logic
```

---

### Prompt 3 — Frontend

```
now make the frontend in nextjs 15

dashboard page should load monitors server side first (no loading flash on first render)
then a UrlTable client component that polls /api/monitors every 8 seconds

each row needs:
- status badge green/red/gray
- response time color coded
- check now button, pause toggle, delete button
AddUrlForm with url field, optional label, advanced options for interval and timeout

dark theme, no tailwind, vanilla css
```

---

### Prompt 4 — Docker setup

```
now make docker-compose.yml and both dockerfiles

4 services - db, api, worker, frontend
api and worker same dockerfile different command

frontend dockerfile should be two stage build
```

---

### Prompt 5 — Terraform sketch

```
add infra/main.tf
hypothetical aws deployment, not production config
ecs fargate for api and worker, rds postgres, alb, s3 cloudfront
cost estimate at bottom
```

---

## 3. Course Corrections

### 3.1 Worker was using sync SQLAlchemy inside async function

**What AI generated (wrong):**
```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

engine = create_engine(os.getenv("DATABASE_URL"))
SessionLocal = sessionmaker(bind=engine)

async def tick():
    session = SessionLocal()   # blocks event loop
    monitors = session.query(Monitor).filter(...).all()
    await asyncio.gather(*[check_one(m) for m in monitors])
    session.close()
```

**My prompt to fix:**
```
bro this is wrong
you used sync session inside async function, every db call is blocking the event loop
asyncio.gather does nothing if db itself is blocking

also worker is copy pasting pinger logic from services
it should just import from app/services and app/repositories, not duplicate

fix both:
- create_async_engine with postgresql+asyncpg url
- async_sessionmaker
- async with async_session_factory() as session
- remove psycopg2, only asyncpg
```

---

### 3.2 Used APScheduler for worker instead of per-monitor scheduling

**What AI generated (wrong):**
```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

async def check_all_monitors():
    # pings every URL on same 60s clock
    monitors = await get_all_monitors()
    await asyncio.gather(*[ping(m) for m in monitors])

scheduler.add_job(check_all_monitors, 'interval', seconds=60)
scheduler.start()
```

**My prompt to fix:**
```
bro APScheduler is wrong here

different monitors have different interval_seconds in schema
your global 60s trigger ignores this completely, all monitors get same interval

also if worker restarts all schedule is gone, its only in memory

use next_check_at column, worker should poll postgres every 5 sec
where next_check_at <= now(), after check update next_check_at = now() + interval
this way schedule survives restarts

remove APScheduler
```

---

### 3.3 SSRF guard was missing

**What AI generated (wrong):**
```python
@router.post("/monitors")
async def create_monitor(payload: UrlCreate, session=Depends(get_session)):
    # only Pydantic HttpUrl validation — no network safety check
    monitor = await monitor_repo.create(session, payload)
    await session.commit()
    return monitor
```

**My prompt to fix:**
```
you forgot SSRF guard

pydantic HttpUrl only checks if url looks valid, doesnt check if its safe
someone can register http://169.254.169.254/latest/meta-data/ and server will ping aws metadata endpoint every minute

add validate_target() in services/monitor_service.py
resolve ALL dns addresses for the hostname (not just first one)
block these ranges: 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16, ::1/128, fc00::/7
raise 400 if any ip hits blocked range
call this before the INSERT in POST /monitors
```

---

### 3.4 Duplicate URL gave 500 error instead of 409

**What AI generated (wrong):**
```python
@router.post("/monitors")
async def create_monitor(payload: UrlCreate, session=Depends(get_session)):
    monitor = await monitor_repo.create(session, payload)
    await session.commit()  # crashes with IntegrityError if URL exists
    return monitor
```

Running same curl command from README twice → `500 Internal Server Error`

**My prompt to fix:**
```
when i register same url twice i get 500 error
the unique constraint is there in schema but route has no error handling

wrap the create call:
try:
    monitor = await monitor_repo.create(session, payload)
    await session.commit()
except IntegrityError:
    await session.rollback()
    raise HTTPException(status_code=409, detail="This URL is already registered")
```

---

### 3.5 No CORS + docker compose crashing on startup

**What AI generated (wrong):**
```yaml
# docker-compose.yml
services:
  api:
    depends_on:
      - db    # only waits for container start, not postgres ready
```

```python
# main.py — no CORSMiddleware
app = FastAPI()
app.include_router(monitors.router)
```

**My prompt to fix:**
```
see two things are broken

frontend is getting CORS error, cant reach the api at all
add CORSMiddleware in main.py, allow localhost:3000

aur api and worker startup pe crash ho rahe hain
your depends_on just waits for container start, postgres is not ready at that point
change to condition: service_healthy
aur db pe healthcheck bhi daalo pg_isready wala
```

---

### 3.6 Next.js build was failing in Docker

**What AI generated (wrong):**
```tsx
// app/page.tsx - Server Component
export default async function DashboardPage() {
  const initial = await getInitialMonitors();
  return (
    <main>
      <AddUrlForm onAdded={() => { /* refresh after add */ }} />
      <UrlTable initial={initial} />
    </main>
  );
}
```

**My prompt to fix:**
```
see this, docker build is failing

target frontend: failed to solve: process "/bin/sh -c npm run build" did not complete successfully: exit code 1

is there something error? then fix this
```

**What happened:** nextjs 15 doesnt allow passing functions from server component to client component, build fails. AI removed the onAdded prop, UrlTable already polls every 8s anyway so it wasnt needed.

---

### 3.7 Frontend design looked too basic and AI generated

**What AI generated (wrong):**
ai gave dark theme with purple gradient, looked like every other ai generated ui

**My prompt to fix:**
```
make the frontend light coloured, not in dark colour 

and also change the blue clour as its looks like AI generated UI

design looks too basic, 
make it more eyecatchy and beutiful
```

**What happened:** AI rewrote globals.css — light theme, removed the purple, added better shadows and gradient. looks much better now.

---

## 4. What AI Did That Would Have Taken Me Days

- `FOR UPDATE SKIP LOCKED` exact syntax in SQLAlchemy 2.0 async context
- `LATERAL` join for "latest check per monitor" in one query — I would have done N+1
- Complete RFC 1918 + link-local + IPv6 ULA network ranges for SSRF guard
- Next.js `output: 'standalone'` mode — didn't know this existed for minimal Docker builds
- ECS Fargate task definition JSON with CloudWatch log config in Terraform

---

## 5. What I Had to Override AI On

| Decision | Why I overrode |
|---|---|
| 2 failure threshold, not 1 | One failed ping is not a real outage |
| No Redis or Celery | Postgres is enough for a few dozen monitors at 60s interval |
| Polling every 8s not WebSockets | WebSockets adds complexity for no real benefit at this check frequency |
| SSRF check only at registration | DNS rebinding is out of scope for MVP — noted in README explicitly |
| SIGTERM sets event, doesn't sys.exit() | Need to finish current tick cleanly before shutting down |
