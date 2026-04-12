"""Tests for goal_progress — quantitative progress scoring."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from secretary.goal_progress import (
    GoalProgress,
    MAX_SNAPSHOTS,
    STALL_THRESHOLD,
    _is_stalled,
    compute_progress,
    format_progress_section,
    record_snapshot,
)
from secretary.run_log import RunLogEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_goal(
    gid: str,
    sub_goals: list[tuple[str, str]] | None = None,
    status: str = "in-progress",
    priority: int = 2,
) -> dict[str, Any]:
    """Quick-build a goal dict matching goals.yaml format."""
    goal: dict[str, Any] = {
        "id": gid,
        "description": f"Test goal {gid}",
        "status": status,
        "priority": priority,
        "success_criteria": f"Criteria for {gid}",
    }
    if sub_goals:
        goal["sub_goals"] = [
            {"id": sg_id, "description": f"Sub-goal {sg_id}", "status": sg_status}
            for sg_id, sg_status in sub_goals
        ]
    return goal


def _make_entry(
    task: str = "test task",
    success: bool = True,
    source: str = "goals",
    goal_id: str = "g1",
) -> RunLogEntry:
    return RunLogEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        cycle=1,
        task=task,
        tier="low",
        model="haiku",
        success=success,
        output_preview="ok",
        source=source,
        goal_id=goal_id,
    )


def _mock_run_log(entries: list[RunLogEntry]) -> MagicMock:
    """Create a mock RunLog that returns the given entries from recent()."""
    mock = MagicMock()
    mock.recent.return_value = entries
    return mock


# ---------------------------------------------------------------------------
# compute_progress
# ---------------------------------------------------------------------------

class TestComputeProgress:
    """Tests for the core progress computation function."""

    def test_empty_goals(self):
        progress = compute_progress([], {}, _mock_run_log([]), [])
        assert progress == {}

    def test_single_goal_no_subgoals(self):
        goals = [_make_goal("g1", sub_goals=[])]
        progress = compute_progress(goals, {}, _mock_run_log([]), [])
        assert "g1" in progress
        gp = progress["g1"]
        assert gp.completion == 0.0
        assert gp.total_sub_goals == 0
        assert gp.done_sub_goals == 0

    def test_completion_ratio(self):
        goals = [_make_goal("g1", sub_goals=[
            ("sg1", "done"),
            ("sg2", "in-progress"),
            ("sg3", "not-started"),
        ])]
        progress = compute_progress(goals, {}, _mock_run_log([]), [])
        gp = progress["g1"]
        assert gp.completion == pytest.approx(1 / 3, abs=0.01)
        assert gp.done_sub_goals == 1
        assert gp.total_sub_goals == 3

    def test_completion_with_state_overrides(self):
        goals = [_make_goal("g1", sub_goals=[
            ("sg1", "not-started"),
            ("sg2", "not-started"),
        ])]
        overrides = {
            "sg1": {"status": "done", "evidence": "test"},
            "sg2": {"status": "done", "evidence": "test"},
        }
        progress = compute_progress(goals, overrides, _mock_run_log([]), [])
        assert progress["g1"].completion == 1.0
        assert progress["g1"].done_sub_goals == 2

    def test_success_rate_with_tasks(self):
        entries = [
            _make_entry(success=True, goal_id="g1"),
            _make_entry(success=True, goal_id="g1"),
            _make_entry(success=False, goal_id="g1"),
        ]
        goals = [_make_goal("g1", sub_goals=[("sg1", "in-progress")])]
        progress = compute_progress(goals, {}, _mock_run_log(entries), [])
        gp = progress["g1"]
        assert gp.success_rate == pytest.approx(2 / 3, abs=0.01)
        assert gp.total_tasks == 3

    def test_success_rate_no_tasks(self):
        goals = [_make_goal("g1", sub_goals=[("sg1", "in-progress")])]
        progress = compute_progress(goals, {}, _mock_run_log([]), [])
        assert progress["g1"].success_rate == -1.0
        assert progress["g1"].total_tasks == 0

    def test_ignores_non_goal_tasks(self):
        entries = [
            _make_entry(source="campaign", goal_id=""),
            _make_entry(source="ooda", goal_id=""),
            _make_entry(source="goals", goal_id="g1", success=True),
        ]
        goals = [_make_goal("g1", sub_goals=[("sg1", "in-progress")])]
        progress = compute_progress(goals, {}, _mock_run_log(entries), [])
        assert progress["g1"].total_tasks == 1
        assert progress["g1"].success_rate == 1.0

    def test_velocity_from_previous_snapshot(self):
        goals = [_make_goal("g1", sub_goals=[
            ("sg1", "done"),
            ("sg2", "done"),
            ("sg3", "not-started"),
            ("sg4", "not-started"),
        ])]
        snapshots = [{"completions": {"g1": 0.25}, "ts": "2026-01-01T00:00:00+00:00"}]
        progress = compute_progress(goals, {}, _mock_run_log([]), snapshots)
        # Current: 2/4 = 0.5, previous: 0.25, velocity: 0.25
        assert progress["g1"].velocity == pytest.approx(0.25, abs=0.01)

    def test_velocity_zero_when_no_snapshots(self):
        goals = [_make_goal("g1", sub_goals=[("sg1", "done"), ("sg2", "not-started")])]
        progress = compute_progress(goals, {}, _mock_run_log([]), [])
        assert progress["g1"].velocity == 0.5  # 0.5 - 0.0

    def test_multiple_goals_separate_metrics(self):
        goals = [
            _make_goal("g1", sub_goals=[("sg1", "done"), ("sg2", "done")]),
            _make_goal("g2", sub_goals=[("sg3", "not-started")]),
        ]
        entries = [
            _make_entry(goal_id="g1", success=True),
            _make_entry(goal_id="g2", success=False),
        ]
        progress = compute_progress(goals, {}, _mock_run_log(entries), [])
        assert progress["g1"].completion == 1.0
        assert progress["g1"].success_rate == 1.0
        assert progress["g2"].completion == 0.0
        assert progress["g2"].success_rate == 0.0

    def test_goal_without_id_skipped(self):
        goals = [{"description": "no id"}]
        progress = compute_progress(goals, {}, _mock_run_log([]), [])
        assert progress == {}


# ---------------------------------------------------------------------------
# _is_stalled
# ---------------------------------------------------------------------------

class TestIsStalled:
    """Tests for stall detection logic."""

    def test_not_stalled_with_few_snapshots(self):
        assert not _is_stalled("g1", 0.5, [])
        assert not _is_stalled("g1", 0.5, [{"completions": {"g1": 0.5}}])

    def test_stalled_when_same_completion(self):
        snapshots = [
            {"completions": {"g1": 0.5}} for _ in range(STALL_THRESHOLD)
        ]
        assert _is_stalled("g1", 0.5, snapshots)

    def test_not_stalled_when_progress_made(self):
        snapshots = [
            {"completions": {"g1": 0.3}},
            {"completions": {"g1": 0.4}},
            {"completions": {"g1": 0.5}},
        ]
        assert not _is_stalled("g1", 0.6, snapshots)

    def test_stalled_only_checks_recent(self):
        # Old progress + recent stall
        snapshots = [
            {"completions": {"g1": 0.1}},
            {"completions": {"g1": 0.2}},
            {"completions": {"g1": 0.5}},
            {"completions": {"g1": 0.5}},
            {"completions": {"g1": 0.5}},
        ]
        assert _is_stalled("g1", 0.5, snapshots)

    def test_not_stalled_if_goal_missing_from_snapshot(self):
        snapshots = [
            {"completions": {}} for _ in range(STALL_THRESHOLD)
        ]
        # goal_id not in snapshots → prev defaults to 0.0, current is 0.5 → not stalled
        assert not _is_stalled("g1", 0.5, snapshots)


# ---------------------------------------------------------------------------
# record_snapshot
# ---------------------------------------------------------------------------

class TestRecordSnapshot:
    """Tests for snapshot persistence."""

    def test_records_snapshot(self):
        state: dict[str, Any] = {}
        progress = {
            "g1": GoalProgress("g1", 0.5, 2, 1, 0.8, 5, 0.1, False),
            "g2": GoalProgress("g2", 0.0, 3, 0, -1.0, 0, 0.0, False),
        }
        record_snapshot(state, progress)
        assert "progress_snapshots" in state
        assert len(state["progress_snapshots"]) == 1
        snap = state["progress_snapshots"][0]
        assert snap["completions"]["g1"] == 0.5
        assert snap["completions"]["g2"] == 0.0
        assert "ts" in snap

    def test_only_includes_positive_success_rates(self):
        state: dict[str, Any] = {}
        progress = {
            "g1": GoalProgress("g1", 0.5, 2, 1, 0.8, 5, 0.0, False),
            "g2": GoalProgress("g2", 0.0, 3, 0, -1.0, 0, 0.0, False),
        }
        record_snapshot(state, progress)
        snap = state["progress_snapshots"][0]
        assert "g1" in snap["success_rates"]
        assert "g2" not in snap["success_rates"]

    def test_caps_at_max_snapshots(self):
        state: dict[str, Any] = {
            "progress_snapshots": [
                {"ts": f"t{i}", "completions": {"g1": i * 0.01}}
                for i in range(MAX_SNAPSHOTS)
            ]
        }
        progress = {"g1": GoalProgress("g1", 0.99, 1, 1, 1.0, 1, 0.0, False)}
        record_snapshot(state, progress)
        assert len(state["progress_snapshots"]) == MAX_SNAPSHOTS
        # Last entry should be the new one
        assert state["progress_snapshots"][-1]["completions"]["g1"] == 0.99

    def test_appends_incrementally(self):
        state: dict[str, Any] = {}
        for i in range(3):
            progress = {"g1": GoalProgress("g1", i * 0.1, 1, 0, -1.0, 0, 0.0, False)}
            record_snapshot(state, progress)
        assert len(state["progress_snapshots"]) == 3


# ---------------------------------------------------------------------------
# format_progress_section
# ---------------------------------------------------------------------------

class TestFormatProgressSection:
    """Tests for prompt text rendering."""

    def test_empty_progress(self):
        assert format_progress_section({}) == ""

    def test_renders_basic_goal(self):
        progress = {
            "g1": GoalProgress("g1", 0.5, 4, 2, 0.75, 8, 0.1, False),
        }
        text = format_progress_section(progress)
        assert "g1" in text
        assert "50%" in text
        assert "2/4" in text
        assert "75% pass" in text
        assert "ADVANCING" in text

    def test_stalled_warning(self):
        progress = {
            "g1": GoalProgress("g1", 0.3, 3, 1, 0.5, 4, 0.0, True),
        }
        text = format_progress_section(progress)
        assert "STALLED" in text
        assert "Warning" in text
        assert "g1" in text

    def test_regressing_goal(self):
        progress = {
            "g1": GoalProgress("g1", 0.2, 5, 1, 0.4, 3, -0.1, False),
        }
        text = format_progress_section(progress)
        assert "REGRESSING" in text
        assert "-10%" in text

    def test_no_task_data(self):
        progress = {
            "g1": GoalProgress("g1", 0.0, 2, 0, -1.0, 0, 0.0, False),
        }
        text = format_progress_section(progress)
        assert "no data yet" in text

    def test_sorted_by_completion(self):
        progress = {
            "high": GoalProgress("high", 0.8, 5, 4, 1.0, 10, 0.0, False),
            "low": GoalProgress("low", 0.1, 5, 0, 0.5, 2, 0.0, False),
            "mid": GoalProgress("mid", 0.5, 4, 2, 0.7, 5, 0.0, False),
        }
        text = format_progress_section(progress)
        lines = text.split("\n")
        goal_lines = [l for l in lines if l.startswith("- **")]
        assert "low" in goal_lines[0]
        assert "mid" in goal_lines[1]
        assert "high" in goal_lines[2]

    def test_multiple_stalled_plural(self):
        progress = {
            "g1": GoalProgress("g1", 0.0, 1, 0, -1.0, 0, 0.0, True),
            "g2": GoalProgress("g2", 0.5, 2, 1, -1.0, 0, 0.0, True),
        }
        text = format_progress_section(progress)
        assert "have" in text  # plural
        assert "g1" in text
        assert "g2" in text

    def test_single_stalled_singular(self):
        progress = {
            "g1": GoalProgress("g1", 0.0, 1, 0, -1.0, 0, 0.0, True),
            "g2": GoalProgress("g2", 0.5, 2, 1, -1.0, 0, 0.0, False),
        }
        text = format_progress_section(progress)
        assert "has" in text  # singular


# ---------------------------------------------------------------------------
# Integration with goals.py
# ---------------------------------------------------------------------------

class TestGoalPromptWithProgress:
    """Test that _build_goal_prompt renders progress section."""

    def test_prompt_includes_progress(self):
        from secretary.goals import _build_goal_prompt

        goals = [_make_goal("g1", sub_goals=[("sg1", "done")])]
        section = "## Goal Progress Metrics\n- **g1**: [##########] 100% (1/1)"

        prompt = _build_goal_prompt(
            goals=goals,
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            progress_section=section,
        )
        assert "Goal Progress Metrics" in prompt
        assert "100%" in prompt

    def test_prompt_without_progress(self):
        from secretary.goals import _build_goal_prompt

        prompt = _build_goal_prompt(
            goals=[_make_goal("g1")],
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            progress_section="",
        )
        assert "Goal Progress Metrics" not in prompt

    def test_prompt_progress_before_reflections(self):
        from secretary.goals import _build_goal_prompt

        section = "## Goal Progress Metrics\n- test"
        reflections = [{"reflection": "test reflection", "patterns": {}}]

        prompt = _build_goal_prompt(
            goals=[_make_goal("g1")],
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            reflections=reflections,
            progress_section=section,
        )
        progress_pos = prompt.index("Goal Progress Metrics")
        reflection_pos = prompt.index("Reflections from Previous")
        assert progress_pos < reflection_pos


# ---------------------------------------------------------------------------
# GoalProgress dataclass
# ---------------------------------------------------------------------------

class TestGoalProgressDataclass:
    """Verify GoalProgress serialization."""

    def test_serializable(self):
        gp = GoalProgress("g1", 0.5, 4, 2, 0.8, 10, 0.1, False)
        from dataclasses import asdict
        d = asdict(gp)
        assert d["goal_id"] == "g1"
        assert d["completion"] == 0.5
        assert d["stalled"] is False
        # JSON-serializable
        json.dumps(d)
