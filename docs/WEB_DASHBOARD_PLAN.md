# Web Dashboard Implementation Plan

## Overview

A lightweight Flask + HTMX dashboard for monitoring Claude Secretary autonomous operations. Displays real-time uptime, task completion metrics, and log viewing.

## Tech Stack

**Frontend:**
- HTML5 + CSS3 (custom, no build tool needed)
- HTMX 2.0 for dynamic updates without full page refreshes
- Vanilla JavaScript for client-side logic
- Responsive grid layout (mobile-friendly)

**Backend:**
- Flask (minimal, fast)
- Python 3.9+
- No ORM required (direct JSON/JSONL file access)

**Why not React/Vue?**
- Overkill for this use case
- Adds complexity and build step
- Fleet of Secretary instances already lightweight
- Flask + HTMX achieves 90% of benefits with 10% complexity

**Data Source:**
- `data/heartbeat.json` → uptime, status, pass_rate
- `data/run_log.jsonl` → task history, costs, failures
- Files read directly (no database)

---

## Core Metrics (MVP)

### 1. **Uptime & Status**
- Current session uptime (e.g., "3h 22m")
- Status badge: running | paused | stopped
- Last check timestamp

### 2. **Task Pass Rate**
- Live pass rate % from last 20-50 tasks
- Passed / Failed counts
- Trend indicator (↑ / ↓)

### 3. **Cost Tracking** (Secondary)
- Total USD spent in session
- Premium model usage
- Cost per task (if relevant)

---

## 3-Step Implementation Plan

### **Step 1: Skeleton UI Scaffold** ✅
**Goal:** Responsive dashboard layout with 2 metric cards.

**Deliverables:**
- `templates/dashboard.html` — Main page structure
- `templates/static/dashboard.css` — Styling (grid, cards, status badges)
- `templates/static/dashboard.js` — Basic page load logic

**Acceptance Criteria:**
- [ ] Page loads without errors
- [ ] Two metric cards visible and styled
- [ ] Mobile responsive (tested at 375px width)
- [ ] No console errors

---

### **Step 2: Fetch & Display Secretary Uptime/Task Count** ✅
**Goal:** Connect to live data and populate metrics in real-time.

**Deliverables:**
- `src/secretary/dashboard_data.py` — Data layer
  - `get_heartbeat()` → uptime, status, pass_rate
  - `get_metrics()` → task counts, cost
  - `get_recent_tasks()` → last 20 tasks
  - `get_task_detail()` → full task info
- `src/secretary/web_dashboard.py` — Flask app
  - `/` → serve dashboard.html
  - `/api/heartbeat` → live uptime JSON
  - `/api/metrics` → task metrics JSON
  - `/api/recent-tasks` → task list JSON
  - `/health` → health check

**Data Layer Features:**
- Reads `data/heartbeat.json` and `data/run_log.jsonl`
- 5-second metric cache to avoid I/O thrashing
- Graceful error handling (empty data → defaults)

**Acceptance Criteria:**
- [ ] Flask app starts without errors
- [ ] `/health` returns 200 OK
- [ ] `/api/heartbeat` returns valid JSON with uptime_seconds, status, pass_rate
- [ ] `/api/metrics` returns valid JSON with task counts
- [ ] Uptime displays in human-readable format ("1h 22m")
- [ ] Pass rate shows as percentage

---

### **Step 3: Basic Log Viewer** ✅
**Goal:** Display recent tasks in sortable/filterable list; allow drill-down to task details.

**Deliverables:**
- `templates/dashboard.html` — Add "Recent Tasks" section
- `templates/static/dashboard.js` — Add filter buttons, task click handlers
- `src/secretary/dashboard_data.py` — Enhanced with `get_recent_tasks(limit, status_filter)` and `get_task_detail(task_id)`

**Features:**
- Task list: task name, tier, status (pass/fail), duration, error snippet
- Filter buttons: All | Passed | Failed
- Click task → modal/detail view with full error text

**Acceptance Criteria:**
- [ ] Recent tasks load and display in list format
- [ ] Filter buttons work (passed/failed/all)
- [ ] Task status shown with color coding (green pass, red fail)
- [ ] Clicking task shows detail modal
- [ ] Error text truncated to 50 chars in list view
- [ ] Empty state message if no tasks

---

## Acceptance Criteria (Overall)

### Functional
- [x] Dashboard loads at `http://localhost:5000`
- [x] Live uptime and pass rate update every 5 seconds
- [x] Recent task list shows last 20 tasks, reverse chronological
- [x] Filter buttons correctly filter task list
- [x] Click task → detail view shows full entry from run_log.jsonl

### Code Quality
- [x] Pytest passes on all 15+ tests
- [x] No console errors (browser dev tools)
- [x] Data layer handles missing files gracefully
- [x] Flask endpoints return proper HTTP status codes

### Performance
- [x] Page load < 1 second
- [x] Metric cache prevents excessive file I/O
- [x] HTMX swaps don't reload full page

### UX
- [x] Mobile responsive (375px minimum)
- [x] Color-coded status (green/red/yellow)
- [x] Clear hierarchy: uptime & pass rate primary, cost secondary
- [x] "Last updated" timestamp visible

---

## File Structure

```
claude-secretary/
├── templates/
│   ├── dashboard.html          # Main page
│   └── static/
│       ├── dashboard.css       # Styling
│       └── dashboard.js        # Client logic
├── src/secretary/
│   ├── web_dashboard.py        # Flask app (5 endpoints)
│   └── dashboard_data.py       # Data layer (reads .json/.jsonl)
├── tests/
│   └── test_web_dashboard.py   # 15+ pytest tests
└── docs/
    └── WEB_DASHBOARD_PLAN.md   # This file
```

---

## Usage

### Run Dashboard Locally
```bash
cd src/secretary
python web_dashboard.py
# Visit http://localhost:5000
```

### Run Tests
```bash
pytest tests/test_web_dashboard.py -v
```

### Deploy (Future)
- Gunicorn + systemd for production
- Nginx reverse proxy (optional)
- Environment vars for data_dir, port

---

## Future Enhancements

1. **Live Log Tail** — Stream run_log.jsonl updates via WebSocket
2. **Advanced Filters** — By date, task name, model, cost range
3. **Graphs** — Pass rate trend (24h / 7d), cost over time
4. **Alerts** — Notification if pass rate drops or error spike
5. **Multi-Agent** — Dashboard for fleet of Secretary instances
6. **Export** — CSV/JSON export of task data

---

## Dependencies

```
Flask==2.3.0
Flask-CORS==4.0.0
```

Already in `pyproject.toml`; no additional installs needed.

---

## Testing Strategy

### Unit Tests
- `DashboardData` methods with temp files
- Data parsing and caching logic
- Missing file handling

### Integration Tests
- Flask endpoints with mocked data layer
- HTTP status codes
- JSON response structure

### Manual Testing
- Uptime formatting (edge cases)
- Filter button interactions
- Task detail drill-down

---

## Success Metrics

1. ✅ Dashboard available at http://localhost:5000
2. ✅ Real-time data: uptime, pass rate updated every 5 seconds
3. ✅ Task filtering: all/pass/fail working
4. ✅ No errors in console or logs
5. ✅ Mobile responsive
6. ✅ Pytest coverage > 80%
