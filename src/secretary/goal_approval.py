"""Layer 18: Human-in-the-loop approval queue for goal-generated tasks.

Three approval modes supporting progressive autonomy:
  - "review"  : Tasks queued for human approval; execute only after approved.
  - "notify"  : Tasks execute immediately but logged for post-hoc review.
  - "auto"    : Tasks execute silently (fully trusted operation).

State lives in goal_state.json under the "approval_queue" key.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

log = logging.getLogger(__name__)

# Approval modes — ordered by increasing autonomy.
MODES = ("review", "notify", "auto")


def _task_id(task: dict[str, Any]) -> str:
    """Generate a short deterministic ID from task content + timestamp."""
    content = f"{task.get('prompt', '')}{task.get('tier', '')}{time.time()}"
    return "ga-" + hashlib.sha256(content.encode()).hexdigest()[:10]


def submit_tasks(
    goal_state: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> list[str]:
    """Add tasks to the approval queue with status 'pending'.

    Returns list of assigned task IDs.
    """
    queue = goal_state.setdefault("approval_queue", [])
    ids: list[str] = []
    for t in tasks:
        tid = _task_id(t)
        entry = {
            "id": tid,
            "prompt": t.get("prompt", t.get("task", "")),
            "tier": t.get("tier", "low"),
            "goal_id": t.get("goal_id", ""),
            "source": t.get("source", "goals"),
            "status": "pending",
            "submitted": time.time(),
            "decided": None,
            # Preserve internal metadata for execution.
            "_meta": {
                k: v
                for k, v in t.items()
                if k.startswith("_") or k in ("priority",)
            },
        }
        queue.append(entry)
        ids.append(tid)
    return ids


def approve_task(goal_state: dict[str, Any], task_id: str) -> bool:
    """Mark a pending task as approved.  Returns True if found and updated."""
    for entry in goal_state.get("approval_queue", []):
        if entry["id"] == task_id and entry["status"] == "pending":
            entry["status"] = "approved"
            entry["decided"] = time.time()
            return True
    return False


def approve_all(goal_state: dict[str, Any]) -> int:
    """Approve all pending tasks.  Returns count approved."""
    count = 0
    for entry in goal_state.get("approval_queue", []):
        if entry["status"] == "pending":
            entry["status"] = "approved"
            entry["decided"] = time.time()
            count += 1
    return count


def reject_task(goal_state: dict[str, Any], task_id: str) -> bool:
    """Mark a pending task as rejected.  Returns True if found and updated."""
    for entry in goal_state.get("approval_queue", []):
        if entry["id"] == task_id and entry["status"] == "pending":
            entry["status"] = "rejected"
            entry["decided"] = time.time()
            return True
    return False


def get_pending(goal_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all pending (awaiting human approval) tasks."""
    return [
        e for e in goal_state.get("approval_queue", []) if e["status"] == "pending"
    ]


def get_approved(goal_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return approved tasks that haven't been executed yet."""
    return [
        e for e in goal_state.get("approval_queue", []) if e["status"] == "approved"
    ]


def mark_executed(goal_state: dict[str, Any], task_id: str) -> bool:
    """Mark an approved task as executed.  Returns True if found."""
    for entry in goal_state.get("approval_queue", []):
        if entry["id"] == task_id and entry["status"] == "approved":
            entry["status"] = "executed"
            entry["decided"] = time.time()
            return True
    return False


def mark_notified(goal_state: dict[str, Any], task_id: str) -> bool:
    """Mark a task as executed in notify mode (auto-approved + logged)."""
    for entry in goal_state.get("approval_queue", []):
        if entry["id"] == task_id:
            entry["status"] = "notified"
            entry["decided"] = time.time()
            return True
    return False


def queue_to_tasks(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert approved queue entries back into executable task dicts."""
    tasks: list[dict[str, Any]] = []
    for e in entries:
        task: dict[str, Any] = {
            "prompt": e["prompt"],
            "tier": e["tier"],
            "goal_id": e.get("goal_id", ""),
            "source": e.get("source", "goals"),
            "_approval_id": e["id"],
        }
        # Restore internal metadata.
        meta = e.get("_meta", {})
        task.update(meta)
        tasks.append(task)
    return tasks


def prune_old_entries(
    goal_state: dict[str, Any],
    max_age_seconds: float = 7 * 86400,
    max_entries: int = 200,
) -> int:
    """Remove old decided entries to prevent unbounded growth.

    Keeps pending and approved entries regardless of age.
    Returns count of pruned entries.
    """
    queue = goal_state.get("approval_queue", [])
    now = time.time()
    keep: list[dict[str, Any]] = []
    pruned = 0
    for entry in queue:
        status = entry["status"]
        if status in ("pending", "approved"):
            keep.append(entry)
        elif now - entry.get("submitted", 0) < max_age_seconds:
            keep.append(entry)
        else:
            pruned += 1
    # Hard cap on total entries.
    if len(keep) > max_entries:
        # Remove oldest decided entries first.
        decided = [e for e in keep if e["status"] not in ("pending", "approved")]
        active = [e for e in keep if e["status"] in ("pending", "approved")]
        decided.sort(key=lambda e: e.get("submitted", 0))
        overflow = len(keep) - max_entries
        pruned += min(overflow, len(decided))
        decided = decided[min(overflow, len(decided)):]
        keep = active + decided
    goal_state["approval_queue"] = keep
    return pruned
