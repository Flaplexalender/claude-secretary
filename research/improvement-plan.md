# Improvement Plan — #8: Config Hot-Reload per Cycle

## What to change
**File:** `src/secretary/watcher.py`

**Step 1 — Add `_reload_config()` method** (~line 178, after `_save_dedup_history()`):
```python
def _reload_config(self) -> None:
    """Re-read config.yaml and update mutable watcher settings."""
    try:
        fresh = SecretaryConfig.load()
        self.interval = fresh.watcher.interval_minutes
        self.max_premium_per_cycle = fresh.watcher.max_premium_per_cycle
        self.max_retries = fresh.watcher.max_retries
        self.retry_base_delay = fresh.watcher.retry_base_delay
        self.pause_on_failure = fresh.watcher.pause_on_failure
        self.max_runs = fresh.watcher.max_runs
        self.config = fresh
        log.debug("Config reloaded: interval=%dm, premium_cap=%.1f",
                  self.interval, self.max_premium_per_cycle)
    except Exception as e:
        log.warning("Config reload failed (keeping previous): %s", e)
```

**Step 2 — Call at top of each cycle** in `run()`, after `self._runs_completed += 1`:
```python
self._reload_config()
```

## Risk Level: **LOW**
- Pydantic validates reload; malformed YAML → caught → old config kept
- try/except prevents crash; worst case = stale config for one more cycle
- No mid-cycle inconsistency (reload happens BEFORE `_run_cycle()`)
- SecretaryConfig.load() is stateless, no side effects

## Expected Benefit
- Operators can tune interval, premium cap, retries, models on a live daemon
- No restart needed — change takes effect next cycle (~30 min max delay)
- Campaign YAML already hot-reloads; this closes the gap for config.yaml
