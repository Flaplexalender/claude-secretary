# Claude Secretary — Deep Research Cycle Notes

## Summary of Prior Analysis (Cycles 1-3, Jan-Mar 2025)

### Files Analyzed
- **direct_agent.py**: Conversation priming prefix, context pruning (30 pairs), asyncio.to_thread
- **watcher.py**: Campaign loop, retry/escalation, heartbeat, dedup, dependency tracking, quota pause
- **memory.py**: Fuzzy dedup (0.85), decay pruning (14d), atomic write, pattern promotion
- **direct_tools.py**: File sandboxing (_safe_path), retry with backoff, 200KB cap, email/calendar
- **self_improve.py**: Sandbox-test-promote pipeline, venv isolation, change detection
- **run_log.py**: Analytics engine (audit/analyze/forecast), O(N) full-file reads
- **router.py**: Keyword scoring, tier thresholds (≥4=high, ≤0=low), premium costs
- **campaign.py**: YAML validation, circular dep detection, schedule validation
- **config.py**: Pydantic validation, env var interpolation, dual file access modes
- **agent.py**: Legacy SDK wrapper (streaming MCP, permission bypass) — kept for --sdk fallback
- **service.py**: SIGBREAK handler, wait_for_proxy (120s)

### Known Issues (not yet fixed)
- No locks on memory.json / dedup_history.json / run_log.jsonl
- ~~No log rotation for run_log.jsonl~~ ✅ IMPLEMENTED (10MB rotation, 3 archives)
- No health check endpoint (heartbeat is passive file)
- Google OAuth token refresh race (no file locking)
- ~~run_log recent(1000) cap limits long-term analytics~~ — analyzed Cycle 9
- ~~No campaign hot-reload for config.yaml (requires restart)~~ — analyzed Cycle 8
- gmail.modify OAuth scope overly broad

### Implemented Improvements
1. ✅ self_improve.py → direct_agent.run()
2. ✅ Post-promote test + auto-rollback
3. ✅ _EXCLUDE_DIRS expanded (.eggs, .tox, dist, build, .ruff_cache, .egg-info suffix)
4. ✅ direct_agent.py: context pairs 12→30, pacing delay, transient 500 retry, cost tracking
5. ✅ Premium not charged on timeout/exception (watcher.py fix)

### Open Items
- Timestamped backup directories (LOW)
- Tier override for self-improvement tasks (MEDIUM)
- Incremental sandbox / git worktree (MEDIUM)
- Self-improvement logging to run_log.jsonl (MEDIUM)
- Google service object caching per cycle (LOW)

## Cycle 4 — watcher.py Deep Dive (24/7 Reliability)

**Focus**: 24/7 autonomous operation bugs, crashes, wasted tokens, autonomy gaps.

### BUG 1: dedup_history.json grows unbounded
`_success_history` maps prompt_hash → last cycle number, but entries are NEVER pruned.
After months of 24/7 operation (30min cycles = ~1440 cycles/month), old hashes accumulate.
Only the most recent cycle matters for skip_if_recent logic. Entries where
`last_success < current_cycle - 1` are dead weight.
**Fix**: Prune entries older than current_cycle - 2 in `_save_dedup_history()`.

### BUG 2: tool registry rebuilt per-task, not per-cycle
Lines ~255-263: `build_tool_registry()` is called inside the task loop for every task.
This creates new Google service objects and re-reads config each time. For a campaign
with 10 tasks, that's 10 redundant rebuilds per cycle.
**Fix**: Move `build_tool_registry()` call to the top of `_run_cycle()`, reuse across tasks.

### BUG 3: premium charged on timeout/exception even if task didn't reach API
✅ FIXED by Copilot — premium_cost=0 in both exception handlers now.

### BUG 4: `_notify_failures` imports at call time, not module level
`build_gmail_service`, `base64`, `MIMEText` are imported inside the method body every call.
Not a crash risk but adds latency on failure paths. Minor.

### AUTONOMY GAP: No campaign hot-reload
`_load_campaign()` reads YAML files every cycle — good. BUT if config.yaml changes
(e.g., new interval, new premium cap), the Watcher __init__ values are stale.
Config changes require full restart. Hot-reloading config.yaml per cycle would enable
remote tuning of a running daemon.

### WASTED TOKENS: Cross-cycle dedup skip counts as "passed"
Line ~245: When a task is skipped due to cross-cycle dedup (`skip_if_recent`),
it's counted as `passed += 1`. This inflates success metrics in heartbeat/summary.
Should be a separate `skipped` counter to distinguish real successes from dedup skips.

### Summary of Findings
| Issue | Severity | Effort |
|-------|----------|--------|
| dedup_history unbounded growth | MEDIUM | LOW (add prune in save) |
| tool registry rebuilt per-task | MEDIUM | LOW (move 10 lines up) |
| premium charged on failed API calls | ✅ FIXED | — |
| no config hot-reload | LOW | MEDIUM (reload per cycle) |
| skipped tasks inflating pass count | LOW | LOW (add skipped counter) |

## Cycle 5 — watcher.py Integration Audit (Cross-Module Seams)

**Focus**: How watcher.py interacts with direct_agent, memory, tools, and run_log.
Looking for: race conditions, data loss paths, error propagation gaps.

### FINDING 1: tool_registry still built per-task (CONFIRMED — not yet fixed)
Lines ~287-294 in `_run_cycle()`: `build_tool_registry()` is called INSIDE the
`for task_def in tasks:` loop. The dropbox listed this as Priority #1 but it has
NOT been implemented yet. Each task creates fresh Google service objects, re-reads
config, and rebuilds the full tool dict. With 10 tasks/cycle × 48 cycles/day =
480 redundant rebuilds daily.
**Fix**: Hoist the 8-line block (lines ~287-294) above the `for` loop. The `_tools`
dict is already passed to `direct_agent.run()` — no interface change needed.

### FINDING 2: dedup pruning still missing (CONFIRMED — not yet fixed)
`_save_dedup_history()` writes `self._success_history` as-is. No pruning logic.
The dropbox listed this as Priority #2 but it was never implemented.
**Fix**: Add 2 lines before the `json_mod.dumps()` call:
```python
cutoff = self._runs_completed - 2
self._success_history = {k: v for k, v in self._success_history.items() if v >= cutoff}
```

### FINDING 3: memory.save() skipped on quota exhaustion path
When `self._quota_exhausted = True`, the cycle `continue`s to the 60-min pause.
But the `memory.save()` call at ~L392 is AFTER the `if self._quota_exhausted:` block.
The `continue` on L404 jumps back to the top of the while loop, SKIPPING:
- `memory.save()` — any memory updates from tasks that ran before quota hit are LOST
- `self._save_dedup_history()` — dedup state lost
- `self._write_heartbeat()` — heartbeat goes stale for 60 min
**Severity**: MEDIUM-HIGH. If 5 tasks run before quota hits on task 6, all 5 tasks'
memory context is discarded. On next cycle those tasks may re-run unnecessarily.
**Fix**: Move `memory.save()`, `_save_dedup_history()`, and `_write_heartbeat()`
BEFORE the quota-exhaustion check block, or add them inside the quota block before
the `continue`.

### FINDING 4: `_notify_failures` silently swallows all exceptions
Line ~L430: The entire method is wrapped in `except Exception as e: log.warning(...)`.
If Gmail auth is permanently broken, the watcher will silently fail to notify on
every single cycle — no escalation, no alert. After 100 cycles of silent failures,
the operator has no idea tasks are failing.
**Fix**: Count consecutive notification failures. After 3, log.error with a prominent
marker or write a flag file that external monitoring can detect.

### FINDING 5: No validation that `direct_agent.run()` result has expected fields
`_run_cycle()` accesses `result.routing.tier`, `result.routing.model`, `result.text`,
`result.error`, `result.cost_usd`, `result.num_turns`, `result.tools_used`. If
`direct_agent.run()` returns an object with a missing attribute (e.g., after a code
change), the task loop crashes mid-cycle with AttributeError. All remaining tasks
in the cycle are skipped.
**Severity**: LOW (dataclass enforces fields), but worth noting for resilience.

### Summary
| Issue | Severity | Effort | Status |
|-------|----------|--------|--------|
| tool_registry per-task | MEDIUM | LOW | Still open |
| dedup pruning missing | MEDIUM | LOW | Still open |
| memory.save() skipped on quota | **MED-HIGH** | LOW | **NEW** |
| silent notification failures | LOW | LOW | NEW |
| No result field validation | LOW | LOW | Informational |

## Cycle 6 — memory.py Deep Dive (Data Integrity & Performance)

**Focus**: Bugs, data loss paths, performance under 24/7 operation, autonomy gaps.

### BUG 1: O(N²) dedup in consolidate() — quadratic blowup
`consolidate()` does TWO O(N²) passes over `_long_entries`:
1. Pattern grouping: nested loop over `tasks` with `_is_similar()` — O(T²)
2. Dedup pass: for each entry, scans all `seen` entries — O(L²)
`SequenceMatcher.ratio()` is itself O(N) on string length.
With long_max=50, this is ~2500 comparisons × string matching per consolidate.
Not a crash bug, but consolidate() is called every cycle from watcher.py.
**Severity**: LOW now, but scales poorly if long_max is ever raised.

### BUG 2: add_long() truncates oldest entries on overflow — NOT by relevance
When `len(_long_entries) > long_max`, line: `self._long_entries[-long_max:]`
keeps the NEWEST entries. But newest ≠ most valuable. High-access-count entries
that are old get evicted in favor of brand-new zero-access entries.
**Fix**: Sort by access_count (desc) before truncating, or evict the entry with
the lowest access_count instead of the oldest.

### BUG 3: consolidate() dedup removes entries but ignores access_count
The dedup pass in consolidate() keeps the LAST occurrence (reversed iteration,
keeps first seen). If entry A (access_count=15) is similar to entry B
(access_count=0), and B comes after A, then A is discarded and B survives.
This destroys the access_count signal built up over weeks.
**Fix**: When deduplicating similar entries, keep the one with higher access_count
(or merge: keep newer text but sum the access_counts).

### BUG 4: access_long() is never called anywhere in the codebase
The `access_long(idx)` method exists to track which long-term memories are used,
but NOTHING in direct_agent.py, watcher.py, or anywhere else calls it.
The `access_count` field is always 0 for all entries. This means:
- Decay pruning's `_DECAY_MIN_ACCESSES = 2` check ALWAYS prunes entries >14d old
- The access_count-based logic is entirely dead code
**Severity**: MEDIUM. Valuable long-term learnings are being pruned after 14 days
regardless of how useful they are, because access_count never increments.
**Fix**: In direct_agent.py, when memory.long entries are injected into the system
prompt, call `memory.access_long(i)` for each included entry.

### BUG 5: Pattern promotion text includes raw count that blocks dedup
Pattern entries look like: `"Recurring pattern (4x): What is 2 plus 2?"`
If the same pattern is detected again with 5 occurrences, the new text is
`"Recurring pattern (5x): ..."` — similarity ratio with `(4x)` version is ~0.95,
which passes the 0.85 threshold, so the new one IS correctly deduped. Good.
BUT the old entry retains the stale count. Not a bug, just a cosmetic issue.

### WASTED TOKENS: Full long-term memory injected every call
All long-term entries are dumped into the system prompt via `get_long()`.
With 50 entries × ~50 tokens each = ~2500 tokens per API call, even for
trivial tasks ("What is 2+2?"). No relevance filtering.
**Fix**: Score long entries by relevance to the current task (keyword overlap
or embedding similarity) and inject only top-K.

### Summary
| Issue | Severity | Effort | Fix |
|-------|----------|--------|-----|
| access_long() never called | **MEDIUM** | LOW | Call in direct_agent.py prompt injection |
| add_long() evicts by age not value | MEDIUM | LOW | Evict min access_count entry |
| consolidate() dedup drops high-access entries | MEDIUM | LOW | Keep higher access_count on merge |
| O(N²) consolidate | LOW | MEDIUM | Acceptable at long_max=50 |
| Full memory injected (no relevance filter) | LOW | MEDIUM | Future optimization |

## Cycle 7 — Phase 2 Reliability Audit: self_improve.py → run_log Integration

**Focus**: Issue C — self_improve.py results don't log to run_log.jsonl.
**Files analyzed**: self_improve.py (11.7KB), run_log.py (16.8KB), watcher.py (29.9KB)

### Key Observation: Log rotation already implemented
run_log.py has `_rotate()` with `_MAX_BYTES=10MB`, `_MAX_ARCHIVES=3`. Phase 2
Issue A is resolved. Updated Known Issues above to reflect this.

### GAP: self_improve.improve() is a blind spot in run_log
`improve()` returns `ImprovementResult` with cost_usd, num_turns, error,
tests_passed, promoted, changed_files — but NEVER creates a `RunLogEntry`
or calls `RunLog.append()`. This means:
- `secretary logs` shows no self-improvement activity
- `run_log.audit()` can't analyze improvement costs
- `run_log.forecast()` underestimates actual API spend
- No record of which improvements were promoted vs rolled back

### Call Sites
1. **CLI** (`__main__.py`): `secretary improve "task"` — calls `improve()` directly
2. **Campaign tasks**: watcher.py logs the `direct_agent.run()` call that happens
   INSIDE `improve()`, but NOT the sandbox/test/promote outcomes. If a campaign
   task calls `improve()`, only the inner agent run is logged — the test results,
   promotion status, and rollback events are invisible.

### Recommended Fix (2 files, LOW risk)
**File: `src/secretary/self_improve.py`**, end of `improve()`, before `return result`:

```python
# Log to run_log.jsonl for analytics visibility
try:
    from .run_log import RunLog, RunLogEntry
    run_log = RunLog(config.data_path / "run_log.jsonl")
    run_log.append(RunLogEntry(
        timestamp=RunLog.now(),
        cycle=0,  # 0 = one-shot / CLI invocation
        task=f"[self-improve] {task[:180]}",
        tier="medium",  # self_improve always uses medium
        model=config.routing.medium_model,
        success=result.tests_passed and not result.error,
        output_preview=(
            f"changes={len(result.changed_files)}, "
            f"tests={'PASS' if result.tests_passed else 'FAIL'}, "
            f"promoted={result.promoted}, "
            f"files={','.join(result.changed_files[:5])}"
        )[:500],
        error=result.error,
        duration_s=0.0,  # not tracked yet — add timer if needed
        cost_usd=result.cost_usd,
        num_turns=result.num_turns,
        tools_used=["file_read", "file_write"],  # sandbox tools
    ))
except Exception as e:
    _log.warning("Failed to log self-improvement result: %s", e)
```

Insert at line ~237 (after the `finally:` cleanup block, before `return result`).
The `[self-improve]` prefix in `task` makes these entries filterable in analytics.

### Why this is safe
- Wrapped in try/except — logging failure can't crash the pipeline
- RunLog.append() is atomic (single line write)
- No new dependencies (run_log.py is already in the package)
- `cycle=0` distinguishes CLI runs from watcher cycles

### Summary
| Finding | Severity | Effort | Risk |
|---------|----------|--------|------|
| self_improve invisible in run_log | MEDIUM | LOW (15 lines, 1 file) | LOW |
| Log rotation already done | ✅ | — | — |

## Cycle 8 — Phase 2 Issue B: Config Hot-Reload Feasibility

**Focus**: Issue B — config.yaml changes require watcher restart.
**Files analyzed**: watcher.py (29.9KB), config.py (6.2KB)

### Problem: __init__ copies config values, never refreshes them
`Watcher.__init__()` copies 6 config values into instance attributes:
- `self.interval` ← `config.watcher.interval_minutes`
- `self.max_premium_per_cycle` ← `config.watcher.max_premium_per_cycle`
- `self.max_retries` ← `config.watcher.max_retries`
- `self.retry_base_delay` ← `config.watcher.retry_base_delay`
- `self.pause_on_failure` ← `config.watcher.pause_on_failure`
- `self.max_runs` ← `config.watcher.max_runs`
These are stale for the daemon's entire lifetime. Changing `interval_minutes`
from 30→15 in config.yaml has no effect until full restart.

### Where these stale values are consumed
1. `self.interval` → `run()` L416: `await asyncio.sleep(wait_minutes * 60)`
2. `self.max_premium_per_cycle` → `_run_cycle()` L296: budget gate
3. `self.max_retries` → `_run_cycle()` L310: `attempts = 1 + self.max_retries`
4. `self.pause_on_failure` → `run()` L412: doubles sleep on failure
5. `self.max_runs` → `run()` L407: stop condition
Note: `self.config` itself is passed to `direct_agent.run()` and
`build_tool_registry()`, so routing tiers/models are ALSO stale.

### What already hot-reloads
Campaign YAML files reload every cycle via `_load_campaign()` — ✅ good.
But config.yaml does NOT.

### Recommended Fix: _reload_config() method (1 file, LOW risk)
**File: `src/secretary/watcher.py`**

**Step 1**: Add method after `_save_dedup_history()` (~line 178):
```python
def _reload_config(self) -> None:
    """Re-read config.yaml and update mutable watcher settings."""
    try:
        fresh = SecretaryConfig.load()  # uses default config.yaml path
        self.interval = fresh.watcher.interval_minutes
        self.max_premium_per_cycle = fresh.watcher.max_premium_per_cycle
        self.max_retries = fresh.watcher.max_retries
        self.retry_base_delay = fresh.watcher.retry_base_delay
        self.pause_on_failure = fresh.watcher.pause_on_failure
        self.max_runs = fresh.watcher.max_runs
        self.config = fresh  # propagates routing/model changes too
        log.debug("Config reloaded: interval=%dm, premium_cap=%.1f",
                  self.interval, self.max_premium_per_cycle)
    except Exception as e:
        log.warning("Config reload failed (keeping previous): %s", e)
```

**Step 2**: Call at top of each cycle in `run()`, after `self._runs_completed += 1` (~line 379):
```python
self._reload_config()
```

### Why this is safe
- **Pydantic validates** the reload — malformed YAML is caught, old config kept
- **try/except** prevents crash — worst case is stale config for one more cycle
- **No mid-cycle inconsistency** — config is refreshed BEFORE `_run_cycle()`
- **SecretaryConfig.load() is stateless** — no side effects, safe to call repeatedly
- `self.config = fresh` atomically replaces the entire config object

### Edge Cases Considered
- **Deleted config.yaml**: `SecretaryConfig.load()` returns defaults → safe
- **Invalid YAML syntax**: `yaml.safe_load` raises → caught → old config kept
- **Pydantic validation failure** (e.g., interval=-1): caught → old config kept
- **Config path**: `SecretaryConfig.load()` defaults to `"config.yaml"` in CWD.
  The watcher is always started from project root, so this matches __init__.
  If custom config path is needed, store `self._config_path` in __init__.

### Summary
| Finding | Severity | Effort | Risk |
|---------|----------|--------|------|
| Config hot-reload missing | LOW-MED | LOW (15 lines, 1 file) | LOW |
| Campaign YAML already hot-reloads | ✅ | — | — |

## Cycle 9 — Phase 2 Issue D: run_log recent(1000) O(N) Read Impact

**Focus**: Issue D — recent(1000) cap limits analytics; O(N) full-file reads.
**File analyzed**: run_log.py (16.8KB, 295 lines)

### Problem 1: O(N) full-file scan on every analytics call
`recent(n)` uses `collections.deque(f, maxlen=n)` — reads ALL lines, keeps last N.
At 10MB rotation threshold (~25K lines at ~400B/entry), every `summary()`,
`audit()`, `analyze()`, `forecast()` call reads ~25K lines to keep 1000.
That's 96% wasted I/O per call.

### Problem 2: 1000-entry cap limits long-term analytics
All four analytics methods call `recent(1000)`. At ~2 tasks/cycle, 48 cycles/day,
1000 entries ≈ 10 days of history. `forecast()` needs monthly data for accuracy.
`analyze()` cycle_trend shows only ~10 days. Seasonal patterns invisible.

### Recommended Fix: Reverse-seek `_tail()` in recent() (~20 lines, LOW risk)
**File: `src/secretary/run_log.py`**, replace `recent()` body (~lines 56-72):
```python
def recent(self, n: int = 20) -> list[RunLogEntry]:
    """Read the last N entries via seek-from-end (O(K) not O(file))."""
    if not self.path.exists() or n <= 0:
        return []
    try:
        size = self.path.stat().st_size
        buf_size = min(size, n * 600)  # ~600B/line worst case
        with open(self.path, "rb") as f:
            f.seek(max(size - buf_size, 0))
            if f.tell() > 0:
                f.readline()  # discard partial first line
            lines = f.readlines()
        entries = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                entries.append(RunLogEntry(**d))
            except (json.JSONDecodeError, TypeError) as e:
                log.warning("Skipping corrupted log entry: %s", e)
        return entries
    except OSError as e:
        log.error("Failed to read run log: %s", e)
        return []
```
**How it works**: Seeks to `(file_size - n*600)`, reads forward from there.
Discards the first partial line. Keeps last N complete lines.
- O(K) where K = n × 600B. For n=1000: reads ~600KB vs 10MB (17x faster).
- If buf_size ≥ file_size: reads entire file — identical to current behavior.
- `rb` mode avoids platform-dependent newline translation edge cases.

### Secondary Fix: Raise analytics cap from 1000 → 5000
In `summary()`, `audit()`, `analyze()`, `forecast()` — change `recent(1000)` →
`recent(5000)`. With O(K) tail reads, 5000 × 600B = 3MB — still fast.
Gives ~52 days of history. Enough for monthly trend analysis.

### Edge Cases
- **Empty file**: `size=0`, `buf_size=0`, `seek(0)`, `readlines()=[]` → `[]` ✓
- **File smaller than buf_size**: `seek(0)`, reads entire file → same as before ✓
- **Single very long line (>600B)**: `n*600` heuristic undershoots. Fix: if
  `len(entries) < n` and `buf_size < size`, retry with `buf_size *= 2`. Optional
  enhancement — unlikely given JSONL entries average ~400B.
- **Concurrent append during read**: `readlines()` may get a partial last line →
  `json.loads` fails → warning + skip. Same resilience as current code.

### Summary
| Finding | Severity | Effort | Risk |
|---------|----------|--------|------|
| O(N) full-file reads in recent() | LOW-MED | LOW (~20 lines) | LOW |
| 1000-entry analytics cap | LOW-MED | TRIVIAL (4 constants) | LOW |
