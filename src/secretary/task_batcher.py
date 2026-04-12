"""Task batching — group compatible campaign tasks into single agent calls.

Instead of running 3 separate agent calls (check email, check calendar, update notes),
batch_compatible tasks with the same tier get merged into ONE call with a combined prompt.
This saves 2-3 agent invocations per watcher cycle for monitoring campaigns.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class TaskBatch:
    """A group of 1+ tasks to run as a single agent call."""
    tasks: list[dict[str, Any]]
    merged_prompt: str
    tier: str
    is_batch: bool  # True if >1 task was merged

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    @property
    def task_ids(self) -> list[str]:
        return [t.get("id", f"task-{i}") for i, t in enumerate(self.tasks)]


def group_into_batches(
    tasks: list[dict[str, Any]],
    *,
    enabled: bool = True,
    max_batch_size: int = 3,
    default_tier: str = "medium",
) -> list[TaskBatch]:
    """Group consecutive batch_compatible tasks of the same tier into batches.

    Rules:
    - Only tasks with ``batch_compatible: true`` can be batched together.
    - Tasks must have the same tier to be grouped.
    - Consecutive means no non-batchable task interrupts the sequence.
    - ``max_batch_size`` caps how many tasks go into one batch.
    - Non-batchable tasks always become solo batches (backwards compatible).
    - When ``enabled=False``, every task becomes its own batch (no-op mode).

    Args:
        tasks: List of task dicts from campaign YAML (must have 'prompt' or 'task' key).
        enabled: Whether batching is active. False = every task is solo.
        max_batch_size: Maximum tasks per batch.
        default_tier: Tier to assume when task has no explicit tier.

    Returns:
        List of TaskBatch objects in execution order.
    """
    if not tasks:
        return []

    if not enabled:
        return [_make_solo_batch(t, default_tier) for t in tasks]

    batches: list[TaskBatch] = []
    current_group: list[dict[str, Any]] = []
    current_tier: str | None = None

    for task in tasks:
        task_tier = task.get("tier", default_tier)
        is_batchable = bool(task.get("batch_compatible", False))

        if is_batchable and current_group:
            # Continue existing batch if same tier and under size limit
            if task_tier == current_tier and len(current_group) < max_batch_size:
                current_group.append(task)
                continue
            else:
                # Flush current group (tier changed or size limit reached)
                batches.append(_make_batch(current_group, current_tier or default_tier))
                current_group = [task]
                current_tier = task_tier
                continue

        if is_batchable and not current_group:
            # Start a new batch group
            current_group = [task]
            current_tier = task_tier
            continue

        # Non-batchable task: flush any pending group, then add solo
        if current_group:
            batches.append(_make_batch(current_group, current_tier or default_tier))
            current_group = []
            current_tier = None

        batches.append(_make_solo_batch(task, default_tier))

    # Flush remaining group
    if current_group:
        batches.append(_make_batch(current_group, current_tier or default_tier))

    merged_count = sum(1 for b in batches if b.is_batch)
    if merged_count > 0:
        total_merged_tasks = sum(b.task_count for b in batches if b.is_batch)
        log.info(
            "Task batching: %d tasks → %d batches (%d merged groups containing %d tasks)",
            len(tasks), len(batches), merged_count, total_merged_tasks,
        )

    return batches


def _get_prompt(task: dict[str, Any]) -> str:
    """Extract prompt text from a task dict, falling back to 'task' key if 'prompt' is absent."""
    return str(task.get("prompt", task.get("task", ""))).strip()


def _make_solo_batch(task: dict[str, Any], default_tier: str) -> TaskBatch:
    """Wrap a single task as a solo batch."""
    return TaskBatch(
        tasks=[task],
        merged_prompt=_get_prompt(task),
        tier=task.get("tier", default_tier),
        is_batch=False,
    )


def _make_batch(group: list[dict[str, Any]], tier: str) -> TaskBatch:
    """Merge multiple tasks into a single batch with combined prompt."""
    if len(group) == 1:
        return TaskBatch(
            tasks=group,
            merged_prompt=_get_prompt(group[0]),
            tier=tier,
            is_batch=False,
        )

    # Merge prompts with --- separators and task numbering
    parts: list[str] = []
    for i, task in enumerate(group, 1):
        task_id = task.get("id", f"subtask-{i}")
        prompt = _get_prompt(task)
        parts.append(f"## Task {i} [{task_id}]\n{prompt}")

    header = (
        f"You have {len(group)} tasks to complete in this single call. "
        f"Complete ALL of them. Batch tool calls where possible.\n\n"
    )
    merged = header + "\n\n---\n\n".join(parts)

    return TaskBatch(
        tasks=group,
        merged_prompt=merged,
        tier=tier,
        is_batch=True,
    )
