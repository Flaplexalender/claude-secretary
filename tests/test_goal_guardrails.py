"""Tests for Layer 16 — Goal Safety Guardrails & Dry-Run.

Covers: validate_goal_task(), apply_guardrails(), config additions,
watcher integration, and secretary goals --dry-run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from secretary.goal_guardrails import (
    GuardrailResult,
    apply_guardrails,
    validate_goal_task,
)


# ── validate_goal_task() ────────────────────────────────────


def test_valid_task_passes():
    task = {"prompt": "Check email inbox for new messages", "tier": "low", "source": "goals", "goal_id": "g1"}
    ok, reason = validate_goal_task(task, max_tier="medium")
    assert ok is True
    assert reason == ""


def test_empty_prompt_rejected():
    task = {"prompt": "", "tier": "low", "source": "goals"}
    ok, reason = validate_goal_task(task)
    assert ok is False
    assert "too short" in reason


def test_short_prompt_rejected():
    task = {"prompt": "hello", "tier": "low", "source": "goals"}
    ok, reason = validate_goal_task(task)
    assert ok is False
    assert "too short" in reason


def test_long_prompt_rejected():
    task = {"prompt": "x" * 5000, "tier": "low", "source": "goals"}
    ok, reason = validate_goal_task(task)
    assert ok is False
    assert "too long" in reason


def test_tier_exceeds_max():
    task = {"prompt": "Research the latest AI papers", "tier": "high", "source": "goals"}
    ok, reason = validate_goal_task(task, max_tier="medium")
    assert ok is False
    assert "tier" in reason and "exceeds" in reason


def test_tier_at_max_passes():
    task = {"prompt": "Research the latest AI papers", "tier": "medium", "source": "goals"}
    ok, reason = validate_goal_task(task, max_tier="medium")
    assert ok is True


def test_tier_below_max_passes():
    task = {"prompt": "Research the latest AI papers", "tier": "low", "source": "goals"}
    ok, reason = validate_goal_task(task, max_tier="high")
    assert ok is True


def test_missing_source_rejected():
    task = {"prompt": "Research the latest AI papers", "tier": "low"}
    ok, reason = validate_goal_task(task)
    assert ok is False
    assert "source" in reason


def test_oracle_tier_rejected_default():
    task = {"prompt": "Run complex analysis task", "tier": "oracle", "source": "goals"}
    ok, reason = validate_goal_task(task, max_tier="medium")
    assert ok is False


def test_deep_tier_rejected_default():
    task = {"prompt": "Deep research coding session", "tier": "deep", "source": "goals"}
    ok, reason = validate_goal_task(task, max_tier="medium")
    assert ok is False


def test_max_tier_high_allows_high():
    task = {"prompt": "Complex multi-step reasoning task", "tier": "high", "source": "goals"}
    ok, reason = validate_goal_task(task, max_tier="high")
    assert ok is True


# ── apply_guardrails() ──────────────────────────────────────


def _make_task(prompt="Check email inbox for updates", tier="low", source="goals", goal_id="g1"):
    return {"prompt": prompt, "tier": tier, "source": source, "goal_id": goal_id}


def test_guardrails_all_pass():
    tasks = [_make_task(), _make_task(prompt="Draft weekly summary report")]
    result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=5)
    assert len(result.accepted) == 2
    assert len(result.rejected) == 0


def test_guardrails_tier_downgrade():
    """Tasks with too-high tier get downgraded, not rejected."""
    tasks = [_make_task(tier="high"), _make_task(tier="oracle")]
    result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=5)
    assert len(result.accepted) == 2
    assert result.accepted[0]["tier"] == "medium"
    assert result.accepted[1]["tier"] == "medium"
    assert any("downgraded" in w for w in result.warnings)


def test_guardrails_task_count_cap():
    tasks = [_make_task(prompt=f"Task number {i} do something useful") for i in range(8)]
    result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=3)
    assert len(result.accepted) == 3
    assert len(result.rejected) == 5
    assert any("cap" in w for w in result.warnings)


def test_guardrails_reject_bad_prompt():
    tasks = [_make_task(prompt="hi"), _make_task()]
    result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=5)
    assert len(result.accepted) == 1
    assert len(result.rejected) == 1


def test_guardrails_add_missing_source():
    """Tasks without source get source='goals' added."""
    tasks = [{"prompt": "Check email inbox for updates", "tier": "low", "goal_id": "g1"}]
    result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=5)
    assert len(result.accepted) == 1
    assert result.accepted[0]["source"] == "goals"
    assert any("missing source" in w for w in result.warnings)


def test_guardrails_empty_input():
    result = apply_guardrails([], max_tier="medium", max_tasks_per_cycle=5)
    assert len(result.accepted) == 0
    assert len(result.rejected) == 0


def test_guardrails_mixed_rejection():
    """Mix of valid, tier-too-high (downgraded), and invalid (short prompt)."""
    tasks = [
        _make_task(prompt="Valid task that should pass easily"),
        _make_task(prompt="High tier task that gets downgraded", tier="high"),
        _make_task(prompt="bad"),  # too short
    ]
    result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=5)
    assert len(result.accepted) == 2
    assert len(result.rejected) == 1


# ── GoalConfig additions ────────────────────────────────────


def test_config_defaults():
    from secretary.config import GoalConfig
    gc = GoalConfig()
    assert gc.max_tier == "medium"
    assert gc.max_tasks_per_cycle == 5
    assert gc.enabled is False


def test_config_custom_values():
    from secretary.config import GoalConfig
    gc = GoalConfig(max_tier="high", max_tasks_per_cycle=10)
    assert gc.max_tier == "high"
    assert gc.max_tasks_per_cycle == 10


# ── secretary goals --dry-run ────────────────────────────────


def _setup_goals_env(tmp_path: Path) -> dict:
    """Create goals.yaml, goal_state.json, run log for testing."""
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
    ]}), encoding="utf-8")

    state = {
        "last_reviewed": "2026-03-10T00:00:00Z",
        "sub_goal_status": {},
        "progress_notes": [],
        "progress_snapshots": [],
        "escalation_state": {},
        "step_plans": {},
    }
    state_file = tmp_path / "goal_state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    from secretary.run_log import RunLog, RunLogEntry
    rl = RunLog(tmp_path / "run_log.jsonl")
    rl.append(RunLogEntry(
        timestamp="2026-03-18T00:00:00Z", cycle=0, task="t1",
        tier="low", model="m", success=True, output_preview="",
        source="goals", goal_id="test-goal-1",
    ))

    return {"goals_file": str(goals_yaml), "data_dir": tmp_path}


def test_cmd_goals_dry_run(tmp_path, capsys):
    """secretary goals --dry-run shows simulation output."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.goals.review_interval_hours = 8
    config.goals.max_tasks_per_review = 3
    config.goals.max_tier = "medium"
    config.goals.max_tasks_per_cycle = 5
    config.goals.enabled = False
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False, dry_run=True)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "Dry-Run Simulation" in out
    assert "Review due" in out
    assert "Guardrail config" in out
    assert "max_tier: medium" in out


def test_cmd_goals_dry_run_review_due(tmp_path, capsys):
    """Dry-run detects when review is due (old last_reviewed)."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    # Set last_reviewed to very old date → review should be due
    state_file = tmp_path / "goal_state.json"
    state = json.loads(state_file.read_text())
    state["last_reviewed"] = "2026-01-01T00:00:00Z"
    state_file.write_text(json.dumps(state))

    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.goals.review_interval_hours = 8
    config.goals.max_tasks_per_review = 3
    config.goals.max_tier = "medium"
    config.goals.max_tasks_per_cycle = 5
    config.goals.enabled = False
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False, dry_run=True)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "Review due: YES" in out
    assert "run_goal_review()" in out


def test_cmd_goals_dry_run_with_step_plans(tmp_path, capsys):
    """Dry-run shows step plans that would execute."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    # Add a step plan to goal_state.json
    state_file = tmp_path / "goal_state.json"
    state = json.loads(state_file.read_text())
    state["step_plans"] = {
        "sg-1b": {
            "goal_id": "test-goal-1",
            "steps": [
                {"step_id": "s1", "action": "Write unit tests for module X", "verification": "pytest passes", "tier": "medium", "status": "pending"},
                {"step_id": "s2", "action": "Refactor module X", "verification": "All tests still pass", "tier": "low", "status": "pending"},
            ],
            "completed": False,
        },
    }
    state_file.write_text(json.dumps(state))

    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.goals.review_interval_hours = 8
    config.goals.max_tasks_per_review = 3
    config.goals.max_tier = "medium"
    config.goals.max_tasks_per_cycle = 5
    config.goals.enabled = False
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False, dry_run=True)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "Active step plans: 1" in out
    assert "Write unit tests" in out
    assert "Guardrail check: 1 accepted" in out


# ── GuardrailResult dataclass ────────────────────────────────


def test_guardrail_result_dataclass():
    gr = GuardrailResult(accepted=[{"a": 1}], rejected=[{"b": 2}], warnings=["w1"])
    assert len(gr.accepted) == 1
    assert len(gr.rejected) == 1
    assert gr.warnings == ["w1"]
