# Web Dashboard — Detailed Breakdown

## STEP 1: Tech Stack Selection

### Frontend: React + TypeScript
**Rationale:**
- Large ecosystem for monitoring dashboards (Recharts, react-table, TanStack Query)
- TypeScript for API contract safety
- Vite for fast dev build times
- Seamless WebSocket upgrade for real-time metrics

**Stack:**
- **Framework**: React 18 + TypeScript 5
- **Build**: Vite 5 (dev: 300ms HMR, prod: single-file bundle)
- **State**: TanStack Query v5 (server state sync) + Zustand (UI state)
- **Charting**: Recharts (lightweight, composable, no D3 required)
- **Tables**: TanStack Table (headless, 100KB gzipped, accessible)
- **UI Components**: Radix UI (unstyled, accessible) + Tailwind CSS
- **HTTP**: fetch API (native, no axios bloat)
- **WebSocket**: native WebSocket API for real-time streams

### Backend: FastAPI + async Python
**Rationale:**
- Shares codebase with Claude Secretary (same Python runtime)
- Native async/await for concurrent event streams
- Zero-config OpenAPI docs
- Sub-100ms REST response times

**Stack:**
- **Framework**: FastAPI 0.104+
- **Server**: uvicorn (ASGI, 4 workers = 400 req/sec capacity)
- **Database**: SQLite + async SQLAlchemy (same data as run_log.jsonl)
- **Real-time**: Server-Sent Events (SSE) for metrics streams
- **Auth**: JWT tokens (sign with secretary config key)
- **CORS**: Same-origin in prod, localhost:* in dev

---

## STEP 2: Required Endpoints & Data Models

### A. Data Models

```python
# backend/models.py

@dataclass
class TaskRunRecord:
    """Mirrors run_log.jsonl entry"""
    id: str                    # UUID
    campaign_id: str          # campaign name
    task_id: str              # task.id from YAML
    prompt: str               # first 500 chars
    model: str                # haiku|sonnet|opus
    tier: str                 # low|medium|high
    status: str               # pending|running|success|failure
    result_text: str          # output (if success)
    error: str | None         # error (if failure)
    turns: int                # agent conversation turns
    input_tokens: int         # prompt tokens
    output_tokens: int        # completion tokens
    cost_usd: float           # raw cost
    cost_cad: float           # converted cost
    duration_ms: int          # execution time
    created_at: datetime      # start time
    completed_at: datetime    # end time
    tags: list[str]           # ["urgent", "retry", "dedup", ...]
    instance_id: str          # "worker-1" (multi-instance)
    retry_count: int          # 0, 1, 2, ...

@dataclass
class InstanceMetrics:
    """Per-instance efficiency stats"""
    instance_id: str
    active: bool              # heartbeat < 5min
    role: str | None          # "researcher" | "triager" | None
    runs_total: int
    runs_success: int
    pass_rate: float          # 0-1
    avg_turns: float          # mean turns/run
    avg_duration_ms: float    # mean execution time
    tokens_per_turn: float    # efficiency metric
    cost_total_cad: float     # cumulative cost
    cost_per_success: float   # CAD per successful task
    last_heartbeat: datetime

@dataclass
class AggregateMetrics:
    """System-wide snapshot"""
    timestamp: datetime
    active_instances: int
    total_runs_24h: int
    success_rate_24h: float
    avg_cost_per_task_cad: float
    peak_concurrency: int
    slowest_task_name: str
    slowest_task_duration_ms: int
    most_expensive_task_name: str
    most_expensive_task_cost_cad: float

@dataclass
class AlertEvent:
    """Real-time notifications"""
    id: str
    severity: str             # "info"|"warning"|"error"|"critical"
    type: str                 # "task_failure"|"quota_exhausted"|"cost_spike"|...
    instance_id: str
    message: str
    timestamp: datetime
    resolved: bool
    metadata: dict            # context-specific fields
```

### B. API Endpoints

#### Authentication
```
POST /api/auth/login
  Body: { api_key: "secretary-xyz..." }
  Response: { access_token: "jwt...", expires_in: 3600 }

POST /api/auth/refresh
  Headers: { Authorization: "Bearer <refresh_token>" }
  Response: { access_token: "jwt..." }
```

#### Dashboard — Status View
```
GET /api/status
  Response: { 
    watcher_active: bool,
    watcher_uptime_sec: int,
    instances: [InstanceMetrics],
    active_tasks: [TaskRunRecord],           # currently running
    pending_tasks: [TaskRunRecord],          # queued
    next_cycle_in_sec: int
  }

GET /api/status/instances?role=researcher&active=true
  Response: [InstanceMetrics]

GET /api/status/instances/:instance_id
  Response: {
    ...InstanceMetrics,
    recent_tasks: [TaskRunRecord],           # last 10 tasks
    hourly_cost_cad: float,                  # cost this hour
    error_rate_1h: float                     # failures this hour
  }
```

#### Dashboard — Logs View
```
GET /api/logs?limit=100&offset=0&status=failure&search=email&instance=worker-1&order=-created_at
  Response: {
    total: 10420,
    page: [TaskRunRecord],
    page_count: 105,
    filters_applied: { status, search, instance }
  }

GET /api/logs/:run_id
  Response: TaskRunRecord (full details + formatted_output: str, formatted_error: str)

GET /api/logs/export?format=csv&start_date=2025-01-01&end_date=2025-01-31&status=success
  Response: CSV stream (Content-Disposition: attachment)

GET /api/logs/stream
  Response: Server-Sent Events stream
  Event: { type: "task_start", run_id: "...", task: {...} }
  Event: { type: "task_complete", run_id: "...", result: {...} }
  Event: { type: "alert", severity: "error", message: "..." }
```

#### Dashboard — Metrics View
```
GET /api/metrics?period=24h|7d|30d&instance=all|worker-1|worker-2
  Response: AggregateMetrics + {
    hourly_runs: [{ hour: "2025-01-15T14:00Z", count: 42, success_count: 40, cost_cad: 1.23 }],
    model_distribution: { haiku: 60%, sonnet: 35%, opus: 5% },
    tier_distribution: { low: 50%, medium: 40%, high: 10% },
    cost_by_instance: { "worker-1": 12.34, "worker-2": 5.67 },
    slowest_10_tasks: [{ task_id, avg_duration_ms, model, runs_count }],
    most_expensive_10: [{ task_id, cost_cad, model, runs_count }],
    efficiency_scores: { "worker-1": 0.92, "worker-2": 0.85 }
  }

GET /api/metrics/cost-forecast?days_ahead=7
  Response: {
    baseline_daily_cad: 12.34,
    projected_7day_cost_cad: 86.38,
    confidence: 0.92,
    factors: ["high_volume_campaign", "peak_hours_active"],
    recommendations: ["pause_low_priority_tasks", "scale_to_haiku"]
  }

GET /api/metrics/benchmarks?campaign=efficiency-sprint&start_date=2025-01-10&end_date=2025-01-20
  Response: {
    version_a: { pass_rate: 0.89, cost_per_task_cad: 1.23, avg_turns: 4.2 },
    version_b: { pass_rate: 0.94, cost_per_task_cad: 0.98, avg_turns: 3.1 },
    winner: "version_b",
    p_value: 0.003,
    recommendation: "Deploy version_b (6% cheaper, 5% higher quality)"
  }

POST /api/metrics/alerts
  Body: { severity: "warning", type: "cost_spike", threshold_cad: 25.0 }
  Response: { alert_id: "...", created_at: "..." }

GET /api/metrics/alerts?severity=critical&resolved=false
  Response: [AlertEvent]

PATCH /api/metrics/alerts/:alert_id
  Body: { resolved: true }
  Response: AlertEvent
```

#### Configuration
```
GET /api/config
  Response: {
    watcher_interval_sec: 300,
    currency_rate_cad_to_usd: 1.44,
    tiers: { low: {...}, medium: {...}, high: {...} },
    campaigns: [{ id, name, schedule, task_count, enabled }]
  }

PATCH /api/config
  Body: { watcher_interval_sec: 600, currency_rate_cad_to_usd: 1.45 }
  Response: updated config

GET /api/config/campaigns
  Response: [{ id, name, version, created_at, last_modified, enabled, task_count, avg_cost_cad }]

PATCH /api/config/campaigns/:campaign_id
  Body: { enabled: false }
  Response: updated campaign
```

#### Health & Debugging
```
GET /api/health
  Response: { status: "ok", uptime_sec: 86400, db_responsive: true, watcher_pid: 1234 }

GET /api/debug/schema
  Response: { models: {...}, endpoints: {...} }  # Full OpenAPI spec
```

---

## STEP 3: Wireframes & UI Views

### View 1: Status Dashboard (Home)
```
┌─────────────────────────────────────────────────────────────┐
│  Claude Secretary Dashboard              🔄 Refresh    ⚙️     │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  System Status Card:                                         │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ Watcher: 🟢 ACTIVE (uptime: 48h)    Next Cycle: 4m 32s │ │
│  │ Instances: 3 active | 1 idle      Cost This Hour: $3.12│ │
│  │ Tasks This Hour: 24 success | 1 failure | 2 pending    │ │
│  └────────────────────────────────────────────────────────┘ │
│                                                               │
│  Instance Cards (3 columns):                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ worker-1     │  │ worker-2     │  │ worker-3     │       │
│  │ 🟢 Active    │  │ 🟢 Active    │  │ 🟡 Idle      │       │
│  │ Role: -      │  │ Role: email  │  │ Role: -      │       │
│  │              │  │              │  │              │       │
│  │ Runs: 234    │  │ Runs: 198    │  │ Runs: 45     │       │
│  │ Pass: 98%    │  │ Pass: 96%    │  │ Pass: 100%   │       │
│  │ Cost: $8.92  │  │ Cost: $5.67  │  │ Cost: $1.23  │       │
│  │ Avg Turn: 3.1│  │ Avg Turn: 2.8│  │ Avg Turn: 2.5│       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│                                                               │
│  Alerts (Real-time SSE):                                     │
│  🔴 [ERROR] Task 'email-batch-001' failed after 3 retries    │
│  🟡 [WARNING] Cost spike: $25/hour (baseline: $12/hour)      │
│  🟢 [INFO] Task 'research-query' succeeded in 2 turns        │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

### View 2: Logs View
```
┌─────────────────────────────────────────────────────────────┐
│  Run History                                                 │
├─────────────────────────────────────────────────────────────┤
│ Filters: [Status ▼] [Instance ▼] [Model ▼] [Search] [Export ▼]│
│                                                               │
│ Page 1 of 105 | 100 results per page                         │
│                                                               │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Task ID      │ Model   │ Status  │ Duration │ Cost   │   │
│ ├─────────────────────────────────────────────────────────┤ │
│ │ research-001 │ Sonnet  │ ✓ Pass  │ 4m 32s   │ $0.98  │◄─ │
│ │              │         │ Run ID: abc123                  │ │
│ │              │         │ Instance: worker-1 | Turns: 3  │ │
│ │ email-batch  │ Haiku   │ ✓ Pass  │ 1m 08s   │ $0.12  │   │
│ │ research-002 │ Opus    │ ✗ Fail  │ 2m 45s   │ $1.45  │   │
│ │              │         │ Error: API quota exceeded        │ │
│ │ monitor-task │ Sonnet  │ ⏳ Running│ 0m 58s  │ $0.32* │   │
│ │ alert-001    │ Haiku   │ ⏸ Queued │ -       │ -      │   │
│ │ ...          │ ...     │ ...     │ ...      │ ...    │   │
│ └─────────────────────────────────────────────────────────┘ │
│                                                               │
│ Detail View (click row):                                     │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ Task: research-001                                      │ │
│ │ Status: ✓ Success | Instance: worker-1 | Tier: medium  │ │
│ │ Model: Sonnet | Turns: 3 | Tokens: 1,234 in / 892 out  │ │
│ │ Cost: $0.98 CAD ($0.68 USD) | Duration: 4m 32s         │ │
│ │ Tags: #urgent #retry                                    │ │
│ │                                                          │ │
│ │ Prompt (first 200 chars):                               │ │
│ │ "Search for research papers on efficiency in LLM agent" │ │
│ │                                                          │ │
│ │ Output:                                                  │ │
│ │ "Found 15 papers. Top paper: 'Optimizing Turns in Multi  │ │
│ │  Agent Systems' (2025)..."                              │ │
│ │ [Show Full Output] [Copy Output] [Re-run Task]          │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                               │
│ Download: CSV | JSON                                         │
└─────────────────────────────────────────────────────────────┘
```

### View 3: Metrics View
```
┌─────────────────────────────────────────────────────────────┐
│  Metrics & Analytics                                         │
├─────────────────────────────────────────────────────────────┤
│ Period: [24 Hours ▼] | Instance: [All ▼] | [Refresh]        │
│                                                               │
│  Key Stats:                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ Runs         │  │ Pass Rate    │  │ Total Cost   │       │
│  │ 478          │  │ 96.4%        │  │ $15.23 CAD   │       │
│  │ +12 (last h) │  │ +2.1% (Ø)    │  │ +$3.12 (▲)   │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│                                                               │
│  Hourly Trends (24h):                                        │
│  Cost ($CAD)                                                 │
│  2.0 │     ┌─┐                                               │
│  1.5 │  ┌──┘ └──┐    ┌──┐                                   │
│  1.0 │──┘       └────┘  └──┐    ┌───────┐                   │
│  0.5 │                      └────┘       └────┐              │
│      └─────────────────────────────────────────┐             │
│      00 04 08 12 16 20 24                      Current       │
│                                                               │
│  Model Distribution:                                         │
│  Haiku:  ██████████░░░░░░░░░░░░  60% (288 runs, $2.88)      │
│  Sonnet: █████░░░░░░░░░░░░░░░░░░  35% (167 runs, $9.22)     │
│  Opus:   ██░░░░░░░░░░░░░░░░░░░░░  5%  (23 runs, $3.13)      │
│                                                               │
│  Slowest Tasks (by avg duration):                            │
│  1. research-deep-dive     4m 32s  (Opus, 12 runs)           │
│  2. email-batch-process    3m 18s  (Sonnet, 45 runs)         │
│  3. knowledge-graph-build  2m 45s  (Opus, 8 runs)            │
│                                                               │
│  Most Expensive Tasks:                                       │
│  1. knowledge-graph-build  $2.34 CAD (Opus, 8 runs)          │
│  2. research-deep-dive     $1.89 CAD (Opus, 12 runs)         │
│  3. email-batch-process    $1.45 CAD (Sonnet, 45 runs)       │
│                                                               │
│  Cost Forecast (7 days):                                     │
│  Baseline: $12.34/day | Projected: $86.38/week (✓ on track) │
│  High confidence (92%). Risk factors: none.                  │
│                                                               │
│  A/B Benchmark (efficiency-sprint):                          │
│  Metric              │ v1.0      │ v1.1        │ Winner      │
│  Pass Rate          │ 89.2%     │ 94.1%       │ v1.1 (+5%)  │
│  Cost per Task (CAD)│ $1.23     │ $0.98       │ v1.1 (-20%) │
│  Avg Turns         │ 4.2       │ 3.1         │ v1.1 (-26%) │
│  Recommendation: Deploy v1.1 (better quality, cheaper)      │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## STEP 4: Acceptance Criteria

### Phase 1: MVP (Weeks 1–2)
- [ ] **AC1.1**: Backend FastAPI server running on `localhost:8000` with auth endpoint
- [ ] **AC1.2**: All 15 API endpoints defined + OpenAPI docs at `/api/docs`
- [ ] **AC1.3**: SQLite DB schema created with `task_runs` table (from run_log.jsonl migration)
- [ ] **AC1.4**: React frontend builds with Vite, loads at `localhost:5173`
- [ ] **AC1.5**: Status dashboard renders with mock data (no API calls yet)
- [ ] **AC1.6**: Logs table renders with pagination, sorting, search
- [ ] **AC1.7**: Metrics dashboard shows charts (Recharts) with mock data
- [ ] **AC1.8**: JWT auth flow implemented (login → token → protected endpoints)
- [ ] **AC1.9**: 100% of API endpoints return correct schema (Pydantic validation)
- [ ] **AC1.10**: Watcher can write to SQLite (async writes don't block CLI)

### Phase 2: Real-time & Polish (Weeks 3–4)
- [ ] **AC2.1**: Server-Sent Events stream active; logs update in real-time
- [ ] **AC2.2**: Alerts display in dashboard (connected to watcher events)
- [ ] **AC2.3**: Cost forecast endpoint functional (7-day projection ±10% accuracy)
- [ ] **AC2.4**: Benchmark comparison view shows A/B test results
- [ ] **AC2.5**: CSV/JSON export working; downloads complete in <2s
- [ ] **AC2.6**: Multi-instance support: dashboard shows 3+ instances with separate metrics
- [ ] **AC2.7**: Dark mode toggle persists in localStorage
- [ ] **AC2.8**: Mobile responsive (< 768px width): tables collapse to card view
- [ ] **AC2.9**: E2E test: login → view logs → export CSV (Playwright)
- [ ] **AC2.10**: Performance: all API responses <100ms p95 (with 1000+ rows in DB)

### Phase 3: Integration (Week 5)
- [ ] **AC3.1**: Watcher writes to SQLite on every task completion
- [ ] **AC3.2**: CLI: `secretary web` command starts FastAPI + React in one process
- [ ] **AC3.3**: Config file: web server port/host configurable
- [ ] **AC3.4**: Watcher restarts don't break SSE connections (graceful reconnect)
- [ ] **AC3.5**: Cost calculations match `cost.py` exactly (CAD/USD conversion verified)
- [ ] **AC3.6**: Dashboard survives 48h continuous watcher run with <1% error rate
- [ ] **AC3.7**: All secrets (API keys, JWTs) never logged or exposed in DOM

---

## STEP 5: First Implementation Task (Week 1, Days 1–3)

### Task: Backend Foundation + Auth

**Acceptance Criteria for Task:**
- [ ] **T1.1**: FastAPI server boots on port 8000, responds to `GET /health` with `{ status: "ok" }`
- [ ] **T1.2**: SQLite DB created at `data/dashboard.db` with schema:
  ```sql
  CREATE TABLE task_runs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT,
    task_id TEXT,
    model TEXT,
    status TEXT,
    created_at TIMESTAMP,
    cost_usd REAL,
    cost_cad REAL,
    turns INT,
    duration_ms INT,
    ...
  );
  ```
- [ ] **T1.3**: POST `/api/auth/login` accepts `{ api_key: "..." }` → returns `{ access_token: "...", expires_in: 3600 }`
- [ ] **T1.4**: GET `/api/status` protected by JWT auth; returns `{ watcher_active: bool, instances: [...] }`
- [ ] **T1.5**: OpenAPI docs at `/api/docs` (auto-generated by FastAPI)
- [ ] **T1.6**: `GET /api/health` public endpoint (no auth required)
- [ ] **T1.7**: Unit tests: 8 tests for auth, schema validation, DB writes (pytest, >80% coverage)
- [ ] **T1.8**: Environment: server runs with `SECRETARY_ADMIN_KEY=xyz` env var for initial auth

**Implementation Checklist:**
```
backend/
  main.py               # FastAPI app, routes, middleware
  models.py             # Pydantic models (TaskRunRecord, InstanceMetrics, etc.)
  database.py           # SQLAlchemy setup, async migrations
  auth.py               # JWT token generation/validation
  routes/
    status.py           # GET /api/status, GET /api/status/instances
    logs.py             # GET /api/logs, GET /api/logs/:run_id
    metrics.py          # GET /api/metrics (stub)
    config.py           # GET /api/config (stub)
    auth.py             # POST /api/auth/login
    health.py           # GET /api/health
  requirements.txt      # fastapi, uvicorn, sqlalchemy, pydantic, python-jose

tests/
  test_auth.py          # auth endpoint tests
  test_models.py        # schema validation tests
  test_database.py      # DB connection + migration tests
  conftest.py           # pytest fixtures (in-memory SQLite)

frontend/
  src/
    api/
      client.ts         # fetch wrapper with JWT auth
    pages/
      _app.tsx          # Auth provider setup
      login.tsx         # Simple login form (mock for now)
```

**Deliverables:**
1. `backend/` directory with FastAPI app + auth
2. SQLite schema migration script
3. 8+ passing unit tests
4. OpenAPI documentation (auto-generated)
5. README: "Run `uvicorn backend.main:app --reload` to start"

**Dependencies to add:**
```
fastapi==0.104.1
uvicorn==0.24.0
sqlalchemy==2.0.23
pydantic==2.5.0
python-jose[cryptography]==3.3.0
python-multipart==0.0.6
```

**Success Criteria:**
- [ ] Backend server starts in <2s
- [ ] Auth flow: login → token → protected API works end-to-end
- [ ] All tests pass: `pytest tests/ -v`
- [ ] No blocking errors in watcher + dashboard concurrent operation

---

## STEP 6: Development Timeline

| Week | Task | Owner | Status |
|------|------|-------|--------|
| W1 D1-3 | **[FIRST TASK]** Backend foundation + auth | Backend Dev | 🚀 START |
| W1 D4-5 | Database sync + run_log migration | Backend Dev | Blocked on W1 |
| W2 D1-3 | React UI setup + Status dashboard | Frontend Dev | Blocked on W1 |
| W2 D4-5 | Logs table + pagination + search | Frontend Dev | Blocked on W1 |
| W3 D1-2 | Real-time SSE + alert streaming | Backend Dev | Blocked on W1 |
| W3 D3-5 | Metrics charts + cost forecast | Frontend + Backend | Blocked on W2 |
| W4 D1-3 | A/B benchmarks view | Backend + Frontend | Blocked on W3 |
| W4 D4-5 | Dark mode + mobile responsive | Frontend Dev | Parallel with W3 |
| W5 D1-3 | Integration: `secretary web` CLI cmd | Infra Dev | Blocked on W4 |
| W5 D4-5 | E2E tests + performance tuning | QA | Blocked on W5 |

---

## STEP 7: Tech Debt & Future Enhancements

### Phase 2 (Q2 2025):
- [ ] WebSocket upgrade (for lower latency than SSE)
- [ ] Grafana integration (export metrics as Prometheus)
- [ ] Alert routing to Slack/PagerDuty
- [ ] Advanced filtering: date range picker, regex search
- [ ] Task replay: re-run from logs with same prompt

### Phase 3 (Q3 2025):
- [ ] Multi-user dashboard with role-based access control (RBAC)
- [ ] Time-series DB (ClickHouse) for 1M+ row retention
- [ ] ML-powered anomaly detection (cost spikes, latency outliers)
- [ ] Drift analysis: compare model outputs over time
- [ ] Custom dashboards: drag-drop widget builder

---

## Files to Create/Modify

**New:**
- `backend/main.py` (FastAPI server)
- `backend/models.py` (data models)
- `backend/database.py` (SQLAlchemy + async)
- `backend/auth.py` (JWT)
- `backend/routes/` (endpoint modules)
- `frontend/` (React Vite project)
- `WEB_DASHBOARD_BREAKDOWN.md` (this file)

**Modify:**
- `src/secretary/__main__.py` — add `secretary web` command
- `config.yaml` — add web server config section
- `requirements.txt` — add fastapi, uvicorn, etc.

---

## Success Metrics

After launch (end of W5):
1. **Watcher uptime**: 99.9% over 30 days
2. **API p95 latency**: <100ms (all endpoints)
3. **SSE reconnect**: <1s
4. **Cost accuracy**: 100% match vs. CLI calculations
5. **User adoption**: 80%+ of watcher runs tracked in dashboard
6. **Cost savings visibility**: Identify 2+ optimization opportunities/week via metrics view
