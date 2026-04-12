"""Tests for Layer 17 — Tool Policy for Goal Tasks.

Covers: tool classifications, filter_tools(), config additions,
dry-run tool policy display.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from secretary.tool_policy import (
    READ_ONLY_TOOLS,
    WRITE_LOCAL_TOOLS,
    SEND_TOOLS,
    POLICY_TOOLS,
    VALID_POLICIES,
    filter_tools,
)


# ── Tool classification completeness ────────────────────────


def test_classifications_no_overlap():
    """Tool sets are disjoint — no tool appears in multiple categories."""
    assert not (READ_ONLY_TOOLS & WRITE_LOCAL_TOOLS)
    assert not (READ_ONLY_TOOLS & SEND_TOOLS)
    assert not (WRITE_LOCAL_TOOLS & SEND_TOOLS)


def test_classifications_cover_all_known_tools():
    """All 17 known tools are classified."""
    all_classified = READ_ONLY_TOOLS | WRITE_LOCAL_TOOLS | SEND_TOOLS
    expected = {
        "gmail_search", "gmail_read", "gmail_draft", "gmail_send",
        "gmail_list_drafts", "gmail_get_draft",
        "calendar_today", "calendar_list", "calendar_search", "calendar_create",
        "file_read", "file_write", "file_list", "file_edit",
        "grep_search", "run_command", "run_python",
    }
    assert all_classified == expected


def test_policy_levels_are_cumulative():
    """Each policy level includes all tools from lower levels."""
    assert READ_ONLY_TOOLS <= POLICY_TOOLS["read-only"]
    assert POLICY_TOOLS["read-only"] <= POLICY_TOOLS["supervised"]
    assert POLICY_TOOLS["supervised"] <= POLICY_TOOLS["full"]


# ── filter_tools() ──────────────────────────────────────────


def _make_registry(*names: str) -> dict[str, dict]:
    """Create a fake tool registry with the given names."""
    return {name: {"name": name, "func": None} for name in names}


def test_filter_read_only():
    reg = _make_registry(
        "gmail_search", "gmail_read", "gmail_send",
        "file_read", "file_write", "run_command",
    )
    result = filter_tools(reg, policy="read-only")
    assert set(result.keys()) == {"gmail_search", "gmail_read", "file_read"}


def test_filter_supervised():
    reg = _make_registry(
        "gmail_search", "gmail_read", "gmail_draft", "gmail_send",
        "file_read", "file_write", "file_edit", "run_command",
        "calendar_create",
    )
    result = filter_tools(reg, policy="supervised")
    assert "gmail_send" not in result
    assert "calendar_create" not in result
    assert "gmail_draft" in result
    assert "file_write" in result
    assert "run_command" in result


def test_filter_full():
    reg = _make_registry(
        "gmail_search", "gmail_send", "calendar_create",
        "file_read", "file_write", "run_python",
    )
    result = filter_tools(reg, policy="full")
    assert set(result.keys()) == set(reg.keys())


def test_filter_unknown_policy_falls_back():
    reg = _make_registry("gmail_search", "gmail_send", "file_read")
    result = filter_tools(reg, policy="nonexistent")
    # Should fall back to read-only
    assert "gmail_send" not in result
    assert "gmail_search" in result


def test_filter_empty_registry():
    result = filter_tools({}, policy="read-only")
    assert result == {}


def test_filter_unknown_tools_excluded():
    """Tools not in any classification are excluded."""
    reg = _make_registry("gmail_search", "custom_unknown_tool")
    result = filter_tools(reg, policy="full")
    assert "gmail_search" in result
    assert "custom_unknown_tool" not in result


# ── GoalConfig tool_policy ───────────────────────────────────


def test_config_default_tool_policy():
    from secretary.config import GoalConfig
    gc = GoalConfig()
    assert gc.tool_policy == "read-only"


def test_config_custom_tool_policy():
    from secretary.config import GoalConfig
    gc = GoalConfig(tool_policy="supervised")
    assert gc.tool_policy == "supervised"


# ── dry-run shows tool policy ────────────────────────────────


def _setup_goals_env(tmp_path: Path) -> dict:
    """Create goals.yaml, goal_state.json, run log for testing."""
    import yaml
    from secretary.run_log import RunLog, RunLogEntry

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
        "last_reviewed": "2026-03-18T00:00:00Z",
        "sub_goal_status": {},
        "progress_notes": [],
        "progress_snapshots": [],
        "escalation_state": {},
        "step_plans": {},
    }
    state_file = tmp_path / "goal_state.json"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    rl = RunLog(tmp_path / "run_log.jsonl")
    rl.append(RunLogEntry(
        timestamp="2026-03-18T00:00:00Z", cycle=0, task="t1",
        tier="low", model="m", success=True, output_preview="",
        source="goals", goal_id="test-goal-1",
    ))

    return {"goals_file": str(goals_yaml), "data_dir": tmp_path}


def test_dry_run_shows_tool_policy(tmp_path, capsys):
    """secretary goals --dry-run shows tool_policy and allowed tools."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.goals.review_interval_hours = 8
    config.goals.max_tasks_per_review = 3
    config.goals.max_tier = "medium"
    config.goals.max_tasks_per_cycle = 5
    config.goals.tool_policy = "read-only"
    config.goals.enabled = False
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False, dry_run=True)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "tool_policy: read-only" in out
    assert "Tool policy 'read-only'" in out
    assert "gmail_search" in out
    assert "gmail_send" not in out


def test_dry_run_shows_supervised_policy(tmp_path, capsys):
    """Dry-run with supervised policy shows write tools."""
    from unittest.mock import MagicMock
    from secretary.__main__ import _cmd_goals

    env = _setup_goals_env(tmp_path)
    config = MagicMock()
    config.goals.goals_file = env["goals_file"]
    config.goals.review_interval_hours = 8
    config.goals.max_tasks_per_review = 3
    config.goals.max_tier = "medium"
    config.goals.max_tasks_per_cycle = 5
    config.goals.tool_policy = "supervised"
    config.goals.enabled = False
    config.data_path = env["data_dir"]

    args = argparse.Namespace(json_out=False, dry_run=True)
    _cmd_goals(args, config)

    out = capsys.readouterr().out
    assert "tool_policy: supervised" in out
    assert "file_write" in out
    assert "gmail_send" not in out
