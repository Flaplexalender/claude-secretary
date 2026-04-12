# Watcher Integration for Task Batching

## Files Created/Modified

| File | Status | Description |
|------|--------|-------------|
| `src/secretary/task_batcher.py` | ✅ NEW | Core batching module: `group_into_batches()` |
| `src/secretary/config.py` | ✅ MODIFIED | Added `task_batching: bool = True`, `max_batch_size: int = 3` to OptimizationConfig |
| `src/secretary/campaign.py` | ✅ MODIFIED | Added `batch_compatible` to `_VALID_TASK_KEYS`, type validation |
| `tests/test_task_batcher.py` | ✅ NEW | 14 tests covering grouping, disabling, prompt format, config, campaign validation |

## Watcher.py Integration (manual — 3 changes needed)

### 1. Add import at top of watcher.py
```python
from .task_batcher import group_into_batches
```

### 2. In `_run_cycle()`, before the task iteration loop, add batching:
```python
# BEFORE (original):
for task in campaign_tasks:
    result = await self._run_task(task, ...)

# AFTER (with batching):
batches = group_into_batches(
    campaign_tasks,
    enabled=self.config.optimizations.task_batching,
    max_batch_size=self.config.optimizations.max_batch_size,
    default_tier=self.config.routing.default_tier,
)

for batch in batches:
    # Use merged prompt for batched tasks, original prompt for solo
    task_prompt = batch.merged_prompt
    task_tier = batch.tier

    # Run as single agent call
    result = await self._run_task(
        {"prompt": task_prompt, "tier": task_tier},
        ...
    )

    # Count as N tasks passed/failed
    if result and not result.error:
        passed += batch.task_count
    else:
        failed += batch.task_count
```

### 3. Campaign YAML — mark tasks as batchable:
```yaml
tasks:
  - id: check-email
    prompt: "Check for urgent unread emails..."
    tier: low
    batch_compatible: true

  - id: check-calendar
    prompt: "Check today's calendar events..."
    tier: low
    batch_compatible: true

  - id: update-notes
    prompt: "Update the daily notes file..."
    tier: low
    batch_compatible: true

  - id: deep-analysis
    prompt: "Analyze codebase for improvements..."
    tier: high
    batch_compatible: false  # complex tasks should run solo
```

## How It Works

1. `group_into_batches()` scans the task list sequentially
2. Consecutive tasks with `batch_compatible: true` AND same `tier` get merged
3. Merged prompts use `---` separators with numbered sub-task headers
4. The agent sees ONE combined prompt and handles all sub-tasks in a single call
5. Non-batchable tasks pass through unchanged (backwards compatible)
6. When `task_batching: false` in config, all tasks run solo (no-op mode)

## Expected Savings

- 3 batch_compatible low-tier tasks → 1 agent call instead of 3
- Saves ~2 premium requests per cycle (at Haiku rate: 0.66 premium saved)
- Over 48 daily cycles: ~32 premium saved/day (~$1.28 USD)

## Run Tests
```bash
python -m pytest tests/test_task_batcher.py -x -q --tb=short
python -m pytest tests/test_campaign.py -x -q --tb=short
python -m pytest tests/ -x -q --tb=short
```
