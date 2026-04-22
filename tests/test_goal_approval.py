"""Tests for Layer 18: goal_approval.py — Approval Queue + Progressive Autonomy."""

from __future__ import annotations

import time

import pytest

from secretary.goal_approval import (
    MODES,
    approve_all,
    approve_task,
    get_approved,
    get_pending,
    mark_executed,
    mark_notified,
    prune_old_entries,
    queue_to_tasks,
    reject_task,
    submit_tasks,
)


@pytest.fixture()
def state() -> dict:
    """Empty goal state."""
    return {}


# ── submit_tasks ────────────────────────────────────────────────

def test_submit_creates_queue(state: dict) -> None:
    tasks = [{"prompt": "Check email", "tier": "low", "goal_id": "g1"}]
    ids = submit_tasks(state, tasks)
    assert len(ids) == 1
    assert "approval_queue" in state
    assert state["approval_queue"][0]["status"] == "pending"
    assert state["approval_queue"][0]["id"] == ids[0]


def test_submit_multiple_tasks(state: dict) -> None:
    tasks = [
        {"prompt": "Task A", "tier": "low"},
        {"prompt": "Task B", "tier": "medium", "goal_id": "g2"},
    ]
    ids = submit_tasks(state, tasks)
    assert len(ids) == 2
    assert ids[0] != ids[1]  # Unique IDs
    assert len(state["approval_queue"]) == 2


def test_submit_preserves_metadata(state: dict) -> None:
    tasks = [{"prompt": "X", "tier": "low", "_step_id": "s1", "_sub_goal_id": "sg1", "priority": 2}]
    submit_tasks(state, tasks)
    entry = state["approval_queue"][0]
    assert entry["_meta"]["_step_id"] == "s1"
    assert entry["_meta"]["_sub_goal_id"] == "sg1"
    assert entry["_meta"]["priority"] == 2


# ── approve / reject ────────────────────────────────────────────

def test_approve_pending(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "Go", "tier": "low"}])
    assert approve_task(state, ids[0])
    assert state["approval_queue"][0]["status"] == "approved"
    assert state["approval_queue"][0]["decided"] is not None


def test_approve_nonexistent(state: dict) -> None:
    assert not approve_task(state, "bogus-id")


def test_approve_already_decided(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "Go", "tier": "low"}])
    approve_task(state, ids[0])
    # Second approval should fail (already approved).
    assert not approve_task(state, ids[0])


def test_reject_pending(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "Nope", "tier": "low"}])
    assert reject_task(state, ids[0])
    assert state["approval_queue"][0]["status"] == "rejected"


def test_reject_nonexistent(state: dict) -> None:
    assert not reject_task(state, "bogus-id")


def test_approve_all_tasks(state: dict) -> None:
    submit_tasks(state, [
        {"prompt": "A", "tier": "low"},
        {"prompt": "B", "tier": "low"},
        {"prompt": "C", "tier": "low"},
    ])
    count = approve_all(state)
    assert count == 3
    for entry in state["approval_queue"]:
        assert entry["status"] == "approved"


def test_approve_all_skips_decided(state: dict) -> None:
    ids = submit_tasks(state, [
        {"prompt": "A", "tier": "low"},
        {"prompt": "B", "tier": "low"},
    ])
    reject_task(state, ids[0])
    count = approve_all(state)
    assert count == 1  # Only B approved
    assert state["approval_queue"][0]["status"] == "rejected"  # A stays rejected
    assert state["approval_queue"][1]["status"] == "approved"  # B approved


# ── get_pending / get_approved ──────────────────────────────────

def test_get_pending(state: dict) -> None:
    submit_tasks(state, [{"prompt": "A", "tier": "low"}, {"prompt": "B", "tier": "low"}])
    assert len(get_pending(state)) == 2
    approve_task(state, state["approval_queue"][0]["id"])
    assert len(get_pending(state)) == 1


def test_get_approved(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "A", "tier": "low"}, {"prompt": "B", "tier": "low"}])
    assert len(get_approved(state)) == 0
    approve_task(state, ids[0])
    assert len(get_approved(state)) == 1


# ── mark_executed / mark_notified ───────────────────────────────

def test_mark_executed(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "X", "tier": "low"}])
    approve_task(state, ids[0])
    assert mark_executed(state, ids[0])
    assert state["approval_queue"][0]["status"] == "executed"


def test_mark_executed_pending_fails(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "X", "tier": "low"}])
    # Can't mark as executed if not yet approved.
    assert not mark_executed(state, ids[0])


def test_mark_notified(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "X", "tier": "low"}])
    assert mark_notified(state, ids[0])
    assert state["approval_queue"][0]["status"] == "notified"


# ── queue_to_tasks ──────────────────────────────────────────────

def test_queue_to_tasks_roundtrip(state: dict) -> None:
    original = [
        {"prompt": "Search Gmail for invoices", "tier": "low", "goal_id": "g1",
         "source": "goals", "_step_id": "s2", "priority": 3},
    ]
    submit_tasks(state, original)
    approve_task(state, state["approval_queue"][0]["id"])
    approved = get_approved(state)
    tasks = queue_to_tasks(approved)
    assert len(tasks) == 1
    t = tasks[0]
    assert t["prompt"] == "Search Gmail for invoices"
    assert t["tier"] == "low"
    assert t["goal_id"] == "g1"
    assert t["_step_id"] == "s2"
    assert t["priority"] == 3
    assert "_approval_id" in t


# ── prune_old_entries ───────────────────────────────────────────

def test_prune_removes_old_decided(state: dict) -> None:
    ids = submit_tasks(state, [{"prompt": "Old", "tier": "low"}])
    approve_task(state, ids[0])
    mark_executed(state, ids[0])
    # Fake old timestamp.
    state["approval_queue"][0]["submitted"] = time.time() - 8 * 86400
    pruned = prune_old_entries(state, max_age_seconds=7 * 86400)
    assert pruned == 1
    assert len(state["approval_queue"]) == 0


def test_prune_keeps_pending(state: dict) -> None:
    """Fresh pending entries (age < stale_pending_seconds) survive pruning."""
    submit_tasks(state, [{"prompt": "New", "tier": "low"}])
    # Back-date within the stale-pending window so it is not auto-rejected.
    state["approval_queue"][0]["submitted"] = time.time() - 1 * 86400  # 1 day old
    pruned = prune_old_entries(state, max_age_seconds=7 * 86400)
    assert pruned == 0
    assert len(state["approval_queue"]) == 1
    assert state["approval_queue"][0]["status"] == "pending"


def test_prune_max_entries(state: dict) -> None:
    # Add 10 executed entries.
    for i in range(10):
        ids = submit_tasks(state, [{"prompt": f"Task {i}", "tier": "low"}])
        approve_task(state, ids[0])
        mark_executed(state, ids[0])
    # 1 pending.
    submit_tasks(state, [{"prompt": "Pending", "tier": "low"}])
    assert len(state["approval_queue"]) == 11
    pruned = prune_old_entries(state, max_entries=5)
    assert len(state["approval_queue"]) <= 5
    # Pending entry survives.
    assert any(e["status"] == "pending" for e in state["approval_queue"])


# ── MODES constant ──────────────────────────────────────────────

def test_modes_defined() -> None:
    assert MODES == ("review", "notify", "auto")


# ── Config default ──────────────────────────────────────────────

def test_config_default_approval_mode() -> None:
    from secretary.config import GoalConfig
    gc = GoalConfig()
    assert gc.approval_mode == "review"


# ── Integration: review mode prevents direct execution ──────────

def test_review_mode_queues_instead_of_executing(state: dict) -> None:
    """In review mode, tasks should land in queue, not execution list."""
    tasks_to_approve = [
        {"prompt": "Analyze cost trends", "tier": "low", "goal_id": "g1", "source": "goals"},
        {"prompt": "Check calendar", "tier": "low", "goal_id": "g2", "source": "goals"},
    ]
    # Simulate: submit to queue.
    ids = submit_tasks(state, tasks_to_approve)
    assert len(ids) == 2
    assert len(get_pending(state)) == 2
    assert len(get_approved(state)) == 0

    # User approves one.
    approve_task(state, ids[0])
    assert len(get_pending(state)) == 1
    assert len(get_approved(state)) == 1

    # Convert approved to executable tasks.
    executable = queue_to_tasks(get_approved(state))
    assert len(executable) == 1
    assert executable[0]["prompt"] == "Analyze cost trends"

    # Mark as executed.
    mark_executed(state, ids[0])
    assert len(get_approved(state)) == 0


# ── Integration: notify mode logs but allows execution ──────────

def test_notify_mode_flow(state: dict) -> None:
    """Notify mode: tasks execute AND get logged in queue."""
    tasks = [{"prompt": "Check inbox", "tier": "low", "source": "goals"}]
    ids = submit_tasks(state, tasks)
    # In notify mode, mark as notified immediately.
    mark_notified(state, ids[0])
    assert state["approval_queue"][0]["status"] == "notified"
    # Pending should be empty (notified = decided).
    assert len(get_pending(state)) == 0
