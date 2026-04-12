# Implementation Log

_Tracks what has been implemented (or planned) to avoid duplicate work across cycles._

## Planned
| # | Date | Improvement | Status |
|---|------|------------|--------|
| 1 | 2025-03-12 | Port self_improve.py from agent.run() to direct_agent.run() for 0-premium billing | ALREADY DONE |
| 2 | 2025-03-13 | Fix premium budget inflation on failed API calls | DONE |
| 3 | 2025-03-14 | Fix memory/dedup/heartbeat loss on quota exhaustion | DONE |
| 4 | 2025-03-15 | Prune dedup_history.json to prevent unbounded growth | DONE |
| 5 | 2025-03-15 | Hoist tool_registry build above task loop | ALREADY DONE (by Copilot) |
| 6 | 2025-03-16 | self_improve.py run_log integration (Issue C) | DONE |
| 7 | 2025-03-16 | Call access_long() when memory is injected | ALREADY DONE |

## Completed

### #1: Port self_improve.py to direct_agent — 2025-03-12
### Changed: src/secretary/self_improve.py (imports + improve() function)
### What: Already implemented in a prior cycle. self_improve.py imports direct_agent + build_tool_registry and calls direct_agent.run() with sandboxed file tools. No changes needed.
### Risk: low (already live)
### Tests: python -m pytest tests/ -q
### Notes: The plan in improvement-plan.md described a diff, but the actual code already reflects the final state. This was likely implemented during a previous autonomous cycle before the plan was formally written, or the plan was written after implementation.

### #2: Fix premium budget inflation on failed API calls — 2025-03-13
### Changed: src/secretary/watcher.py (_run_cycle method, TimeoutError + Exception handlers)
### What:
- **TimeoutError handler**: Changed `premium_cost=task_cost` → `premium_cost=0` and removed `cycle_premium_spent += task_cost`
- **Exception handler**: Changed `premium_cost=task_cost` → `premium_cost=0` and removed `cycle_premium_spent += task_cost`
- Added comment `# Don't charge premium — no confirmed API usage` in both handlers
- SUCCESS path left unchanged (correctly charges after confirmed API response)
### Risk: LOW — 4 line changes in 1 file, logic is straightforward
### Tests: python -m pytest tests/ -q
### Benefit: Prevents false budget exhaustion from counting failed/timed-out calls as premium spend, which was causing legitimate tasks to be skipped.

### #3: Fix memory/dedup/heartbeat loss on quota exhaustion — 2025-03-14
### Changed: src/secretary/watcher.py (run() method, ~lines 350-380)
### What:
- Moved `memory.consolidate()` (conditional), `memory.save()`, `_save_dedup_history()`, and `_write_heartbeat()` **above** the `if self._quota_exhausted:` block
- Previously, the `continue` statement after the 60-min quota sleep skipped all housekeeping
- Added comment: `# === Housekeeping: ALWAYS runs, even on quota exhaustion ===`
- The `if self._quota_exhausted:` block with `continue` now only skips the normal sleep/max_runs check (correct behavior)
### Risk: LOW — reordering ~10 lines in 1 file, no logic change
### Tests: python -m pytest tests/ -q
### Benefit: Eliminates silent memory loss, stale heartbeat, and dedup state loss on every quota exhaustion event.

### #4: Prune dedup_history.json to prevent unbounded growth — 2025-03-15
### Changed: src/secretary/watcher.py (`_save_dedup_history()` method)
### What:
- Added 2 lines before `json_mod.dumps()` in `_save_dedup_history()`:
  ```python
  cutoff = self._runs_completed - 2
  self._success_history = {k: v for k, v in self._success_history.items() if v >= cutoff}
  ```
- Added comment: `# Fix #4: Prune stale entries to prevent unbounded growth`
- Removes entries older than 2 cycles, which are irrelevant to `skip_if_recent` logic
### Risk: LOW — 2 lines added in 1 file, pure additive before existing write
### Tests: python -m pytest tests/ -q
### Benefit: Caps dedup_history.json to ~task_count entries instead of unbounded growth (1440+/month). Prevents eventual OOM / slow JSON parse after months of 24/7 runtime.

### #5: Hoist tool_registry build above task loop — 2025-03-16
### Changed: (none — already implemented by Copilot)
### What: improvement-plan.md described moving `build_tool_registry()` above the task loop in `_run_cycle()`, but the code already has this fix in place. The tool registry is built once per cycle, before the `for task_def in tasks:` loop.
### Risk: N/A (already done)
### Notes: Confirmed by code inspection. Dropbox already listed this as "DONE (Copilot implemented)".

### #6: self_improve.py run_log integration (Issue C) — 2025-03-16
### Changed: src/secretary/self_improve.py (end of `improve()` function)
### What:
- Added 16 lines after the `finally:` cleanup block, before `return result`
- Creates a `RunLogEntry` with `[self-improve]` prefix for filterability
- `cycle=0` distinguishes CLI/one-shot runs from watcher cycles
- Logs: task, tier, model, success, changed files count, test status, promotion status, cost, turns
- Wrapped in try/except so logging failure can't crash the pipeline
### Risk: LOW — 16 lines added, 1 file, no function signature changes, try/except wrapped
### Tests: python -m pytest tests/ -q
### Benefit: Self-improvement runs now visible in `secretary logs`, `run_log.audit()`, and `run_log.forecast()`. Cost tracking includes improvement runs. Operators can see which improvements were promoted vs rolled back.

### #7: Call access_long() when memory is injected — 2025-03-16
### Changed: (none — already implemented)
### What: improvement-plan.md proposed adding `memory.access_long(i)` in `_build_system_prompt()` in `direct_agent.py`. Code inspection shows this is already present with correct index offset handling:
  ```python
  for i, entry in enumerate(long_mem[-10:]):
      parts.append(f"- {entry}")
      offset = max(0, len(long_mem) - 10)
      memory.access_long(offset + i)
  ```
### Risk: N/A (already done)
### Notes: The fix was likely applied during a prior cycle or by Copilot. access_count is now correctly incremented each time a long-term memory entry is included in the system prompt.