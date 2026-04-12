"""Tests for Layer 15 — Goal Actualization Observability.

Covers: run_log.summary() by_source/autonomous_ratio, heartbeat enrichment,
_cmd_health goal check, _cmd_goals dashboard.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from secretary.run_log import RunLog, RunLogEntry


# ── run_log.summary() by_source & autonomous_ratio ──────────


def _make_entries(tmp_path: Path, specs: list[tuple[str, bool, str]]) -> RunLog:
    """Create a RunLog with entries from (tier, success, source) tuples."""
    rl = RunLog(tmp_path / "log.jsonl")
    for i, (tier, success, source) in enumerate(specs):
        rl.append(RunLogEntry(
            timestamp=f"2026-07-15T00:0{i % 10}:00Z",
            cycle=i,
            task=f"task-{i}",
            tier=tier,
            model="claude-haiku-4.5",
            success=success,
            output_preview="ok",
            source=source,
        ))
    return rl


def test_summary_by_source_empty(tmp_path):
    rl = RunLog(tmp_path / "log.jsonl")
    s = rl.summary()
    assert s["total"] == 0


def test_summary_by_source_campaign_only(tmp_path):
    rl = _make_entries(tmp_path, [
        ("low", True, "campaign"),
        ("low", False, "campaign"),
        ("medium", True, "campaign"),
    ])
    s = rl.summary()
    assert s["by_source"]["campaign"]["total"] == 3
    assert s["by_source"]["campaign"]["passed"] == 2
    assert s["autonomous_ratio"] == 0.0


def test_summary_by_source_mixed(tmp_path):
    rl = _make_entries(tmp_path, [
        ("low", True, "campaign"),
        ("low", True, "campaign"),
        ("low", True, "goals"),
        ("low", False, "goals"),
        ("medium", True, "ooda"),
    ])
    s = rl.summary()
    assert s["by_source"]["campaign"]["total"] == 2
    assert s["by_source"]["goals"]["total"] == 2
    assert s["by_source"]["goals"]["passed"] == 1
    assert s["by_source"]["ooda"]["total"] == 1
    # autonomous = goals(2) + ooda(1) = 3 out of 5
    assert s["autonomous_ratio"] == 0.6


def test_summary_by_source_all_autonomous(tmp_path):
    rl = _make_entries(tmp_path, [
        ("low", True, "goals"),
        ("low", True, "ooda"),
    ])
    s = rl.summary()
    assert s["autonomous_ratio"] == 1.0


def test_summary_by_source_goal_id(tmp_path):
    """Entries with goal_id still group correctly by source."""
    rl = RunLog(tmp_path / "log.jsonl")
    rl.append(RunLogEntry(
        timestamp="2026-07-15T00:00:00Z", cycle=0, task="g1",
        tier="low", model="m", success=True, output_preview="",
        source="goals", goal_id="prefix-survival",
    ))
    rl.append(RunLogEntry(
        timestamp="2026-07-15T00:01:00Z", cycle=1, task="g2",
        tier="low", model="m", success=False, output_preview="",
        source="goals", goal_id="self-improvement",
    ))
    s = rl.summary()
    assert s["by_source"]["goals"]["total"] == 2
    assert s["by_source"]["goals"]["passed"] == 1


# ── _cmd_goals dashboard ────────────────────────────────────


def _setup_goals_env(tmp_path: Path, *, with_entries: bool = True) -> dict:
    """Create goals.yaml, goal_state.json, run log for _cmd_goals testing."""
    import yaml

    goals_yaml = tmp_path / "goals.yaml"
    goals_yaml.write_text(yaml.dump({"goals": [
        {
            "id": "test-goal-1",
            "title": "Test Goal One",
            "sub_goals": [
                {"id": "sg-1a", "status": "done"},
                {"id": "sg-1b", "status": "in-progress"},
            ],
        },
        {
            "id": "test-goal-2",
            "title": "Test Goal Two",
            "sub_goals": [
                {"id": "sg-2a", "status": "not-started"},
            ],
        },
    ]}), encoding="utf-8")

    state = {
        "last_reviewed": "2026-07-15T00:00:00Z",
        "sub_goal_status": {},
        "progress_notes": [],
        "progress_snapshots": [],
        "escalation_state": {
            "test-goal-2": {
                "level": 1,
                "diagnoses": 1,
                "redecompositions": 0,
                "last_escalation_ts": "2026-07-15T00:00:00Z",
            },
        },
    }
    state_file = tmp_path / "goal_state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    if with_entries:
        rl = RunLog(tmp_path / "run_log.jsonl")
        rl.append(RunLogEntry(
            timestamp="2026-07-15T00:00:00Z", cycle=0, task="t1",
            tier="low", model="m", success=True, output_preview="",
            source="goals", goal_id="test-goal-1",
        ))
        rl.append(RunLogEntry(
            timestamp="2026-07-15T00:01:00Z", cycle=1, task="t2",
            tier="low", model="m", success=False, output_preview="",
            source="campaign",
        ))

    return {
        "goals_file": str(goals_yaml),
        "state_file": state_file,
        "data_dir": tmp_path,
    }


def test_cmd_goals_text_output(tmp_path, capsys):
    """_cmd_goals prints formatted dashboard with progress bars."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "Goal Actualization Dashboard" in out
    assert "Test Goal One" in out
    assert "Test Goal Two" in out
    assert "50%" in out  # test-goal-1: 1/2 sub-goals done
    assert "Autonomous ratio" in out


def test_cmd_goals_json_output(tmp_path, capsys):
    """_cmd_goals --json produces valid JSON with all expected fields."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=True)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data["goals"]) == 2
    assert data["goals"][0]["id"] == "test-goal-1"
    assert data["goals"][0]["progress"]["completion"] == 0.5
    assert data["goals"][1]["escalation_level"] == 1
    assert data["goals"][1]["escalation_strategy"] == "redecompose"
    assert "autonomous_ratio" in data
    assert "strategies" in data


def test_cmd_goals_no_goals(tmp_path, capsys):
    """_cmd_goals with empty goals.yaml prints 'No goals defined.'."""
    import yaml
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    goals_yaml = tmp_path / "goals.yaml"
    goals_yaml.write_text(yaml.dump({"goals": []}), encoding="utf-8")

    config = MagicMock()
    config.goals.goals_file = str(goals_yaml)
    config.data_path = tmp_path

    args = argparse.Namespace(json_out=False)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "No goals defined" in out


def test_cmd_goals_escalation_display(tmp_path, capsys):
    """Goals with escalation > 0 show escalation info in text output."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "Escalation" in out
    assert "redecompose" in out


def test_cmd_goals_strategy_display(tmp_path, capsys):
    """When strategies exist, they appear in the dashboard."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals
    from secretary.strategy_library import Strategy, StrategyLibrary

    env = _setup_goals_env(tmp_path)

    # Write strategies
    lib = StrategyLibrary(env["data_dir"] / "strategies.json")
    lib.add_strategy(Strategy(
        category="email", description="Always use friendly tone",
        source_task="draft email",
    ))
    lib.add_strategy(Strategy(
        category="research", description="Search arxiv first",
        source_task="research task",
    ))

    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "Strategies: 2 total" in out
    assert "email" in out


# ── heartbeat enrichment (unit-level) ───────────────────────


def test_heartbeat_subsystems_in_summary(tmp_path):
    """Verify run_log.summary() returns by_source and autonomous_ratio keys."""
    rl = _make_entries(tmp_path, [
        ("low", True, "campaign"),
        ("low", True, "goals"),
    ])
    s = rl.summary()
    assert "by_source" in s
    assert "autonomous_ratio" in s
    assert isinstance(s["by_source"], dict)
    assert isinstance(s["autonomous_ratio"], float)
