"""Tests for goal pruning logic."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from secretary.goals import GoalStore


@pytest.fixture
def goal_store(tmp_path):
    gs = GoalStore(tmp_path / "goals.yaml", tmp_path / "state.json")
    gs._state = {
        "last_reviewed": None,
        "sub_goal_status": {},
        "progress_notes": [],
        "step_plans": {},
    }
    return gs


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


class TestPruneStaleGoals:
    def test_prunes_old_blocked(self, goal_store):
        goal_store._state["sub_goal_status"] = {
            "old-blocked": {"status": "blocked", "updated": _ts(10)},
            "recent-blocked": {"status": "blocked", "updated": _ts(2)},
            "in-progress": {"status": "in-progress", "updated": _ts(10)},
        }
        pruned = goal_store.prune_stale_goals(max_blocked_days=7)
        assert pruned == 1
        assert "old-blocked" not in goal_store._state["sub_goal_status"]
        assert "recent-blocked" in goal_store._state["sub_goal_status"]
        assert "in-progress" in goal_store._state["sub_goal_status"]

    def test_prunes_associated_step_plans(self, goal_store):
        goal_store._state["sub_goal_status"] = {
            "stale": {"status": "blocked", "updated": _ts(10)},
        }
        goal_store._state["step_plans"] = {
            "stale": {"goal_id": "g1", "steps": [], "completed": False},
            "other": {"goal_id": "g2", "steps": [], "completed": False},
        }
        goal_store.prune_stale_goals(max_blocked_days=7)
        assert "stale" not in goal_store._state["step_plans"]
        assert "other" in goal_store._state["step_plans"]

    def test_no_prune_when_all_fresh(self, goal_store):
        goal_store._state["sub_goal_status"] = {
            "fresh": {"status": "blocked", "updated": _ts(1)},
        }
        pruned = goal_store.prune_stale_goals(max_blocked_days=7)
        assert pruned == 0
        assert "fresh" in goal_store._state["sub_goal_status"]

    def test_no_prune_empty(self, goal_store):
        assert goal_store.prune_stale_goals() == 0
