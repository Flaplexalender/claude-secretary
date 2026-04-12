"""Advanced tests for campaign.py — untested edge cases.

Cycle 9: Non-dict tasks, 'task' key alias, non-numeric priority/timeout,
file read errors, non-dict YAML root, schedule edge cases.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

from secretary.campaign import validate_campaign, ValidationResult, _validate_schedule


def _write_campaign(tmp_path: Path, data: dict | list | str) -> Path:
    p = tmp_path / "campaign.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")
    return p


# ── Non-dict YAML root ─────────────────────────────────────────


def test_non_dict_root_list(tmp_path: Path):
    """YAML root that's a list (not dict) should error."""
    p = _write_campaign(tmp_path, [{"prompt": "a"}])
    r = validate_campaign(p)
    assert not r.valid
    assert any("must be a YAML mapping" in e for e in r.errors)


def test_non_dict_root_string(tmp_path: Path):
    """YAML root that's a string should error."""
    p = tmp_path / "campaign.yaml"
    p.write_text("just a string", encoding="utf-8")
    r = validate_campaign(p)
    assert not r.valid
    assert any("must be a YAML mapping" in e for e in r.errors)


def test_non_dict_root_number(tmp_path: Path):
    """YAML root that's a number should error."""
    p = tmp_path / "campaign.yaml"
    p.write_text("42", encoding="utf-8")
    r = validate_campaign(p)
    assert not r.valid
    assert any("must be a YAML mapping" in e for e in r.errors)


# ── tasks not a list ───────────────────────────────────────────


def test_tasks_is_string(tmp_path: Path):
    """tasks: 'not a list' should error."""
    p = _write_campaign(tmp_path, {"tasks": "not a list"})
    r = validate_campaign(p)
    assert not r.valid
    assert any("must be a list" in e for e in r.errors)


def test_tasks_is_dict(tmp_path: Path):
    """tasks: {key: val} should error."""
    p = _write_campaign(tmp_path, {"tasks": {"key": "val"}})
    r = validate_campaign(p)
    assert not r.valid
    assert any("must be a list" in e for e in r.errors)


# ── Non-dict task in list ──────────────────────────────────────


def test_non_dict_task_string(tmp_path: Path):
    """A string item in tasks list should error."""
    p = _write_campaign(tmp_path, {"tasks": ["just a string"]})
    r = validate_campaign(p)
    assert not r.valid
    assert any("must be a mapping" in e for e in r.errors)


def test_non_dict_task_number(tmp_path: Path):
    """A number item in tasks list should error."""
    p = _write_campaign(tmp_path, {"tasks": [42]})
    r = validate_campaign(p)
    assert not r.valid
    assert any("must be a mapping" in e for e in r.errors)


def test_non_dict_task_mixed(tmp_path: Path):
    """Mix of valid dict and invalid non-dict tasks."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "valid task"},
            "invalid string task",
            {"prompt": "another valid"},
        ]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("Task 2" in e and "must be a mapping" in e for e in r.errors)


# ── 'task' key as alias for 'prompt' ──────────────────────────


def test_task_key_alias(tmp_path: Path):
    """The 'task' key should work as an alias for 'prompt'."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"task": "Do something"}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_empty_task_key(tmp_path: Path):
    """Empty 'task' key (no 'prompt') should error."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"task": ""}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("missing or empty 'prompt'" in e for e in r.errors)


def test_whitespace_only_prompt(tmp_path: Path):
    """Whitespace-only prompt should error."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "   "}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("missing or empty 'prompt'" in e for e in r.errors)


# ── Non-numeric priority ──────────────────────────────────────


def test_string_priority(tmp_path: Path):
    """String priority should error."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "priority": "high"}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("priority" in e and "number" in e for e in r.errors)


def test_numeric_priority_int(tmp_path: Path):
    """Integer priority should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "priority": 5}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_numeric_priority_float(tmp_path: Path):
    """Float priority should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "priority": 2.5}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_negative_priority_valid(tmp_path: Path):
    """Negative priority is allowed (no validation against it)."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "priority": -1}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


# ── Timeout edge cases ────────────────────────────────────────


def test_string_timeout(tmp_path: Path):
    """String timeout should error."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "timeout": "fast"}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("non-negative number" in e for e in r.errors)


def test_zero_timeout_valid(tmp_path: Path):
    """Zero timeout should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "timeout": 0}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_float_timeout_valid(tmp_path: Path):
    """Float timeout should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "timeout": 30.5}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


# ── Tier edge cases ───────────────────────────────────────────


def test_valid_tiers(tmp_path: Path):
    """All three valid tiers should be accepted."""
    for tier in ["low", "medium", "high"]:
        p = _write_campaign(tmp_path, {
            "tasks": [{"prompt": "a", "tier": tier}]
        })
        r = validate_campaign(p)
        assert r.valid, f"tier '{tier}' should be valid: {r.errors}"


def test_no_tier_valid(tmp_path: Path):
    """Task without tier should be valid (default applied elsewhere)."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a"}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


# ── Schedule validation edge cases ─────────────────────────────


def test_schedule_weekends(tmp_path: Path):
    """'weekends' schedule rule should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "weekends"}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_schedule_weekdays(tmp_path: Path):
    """'weekdays' schedule rule should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "weekdays"}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_schedule_unknown_rule_warns(tmp_path: Path):
    """Unknown schedule rule should warn, not error."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "monthly"}]
    })
    r = validate_campaign(p)
    assert r.valid  # warning only
    assert any("unknown schedule rule" in w for w in r.warnings)


def test_schedule_multiple_hour_ranges(tmp_path: Path):
    """Multiple comma-separated hour ranges should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "hours:8-12,14-18"}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_schedule_invalid_non_integer_hours(tmp_path: Path):
    """Non-integer hours should error."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "hours:abc-def"}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("not integers" in e for e in r.errors)


def test_schedule_invalid_single_number(tmp_path: Path):
    """Single number (not a range) should error."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "hours:8"}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("expected 'start-end'" in e for e in r.errors)


def test_schedule_combined_rules(tmp_path: Path):
    """Combined rules with semicolons should parse correctly."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "hours:9-17;weekdays"}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_schedule_boundary_hours(tmp_path: Path):
    """hours:0-24 is the valid range boundary."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "hours:0-24"}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


# ── Dependency edge cases ─────────────────────────────────────


def test_dependency_with_no_id(tmp_path: Path):
    """Task with depends_on but no id should still work (just creates unresolved dep)."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "a", "id": "setup"},
            {"prompt": "b", "depends_on": "setup"},  # No id on this task
        ]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_multiple_tasks_same_dependency(tmp_path: Path):
    """Multiple tasks depending on the same task should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "base", "id": "base"},
            {"prompt": "a", "depends_on": "base"},
            {"prompt": "b", "depends_on": "base"},
        ]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


# ── ValidationResult helper ────────────────────────────────────


def test_validation_result_error_sets_invalid():
    r = ValidationResult()
    assert r.valid is True
    r.error("something broke")
    assert r.valid is False
    assert "something broke" in r.errors


def test_validation_result_warn_keeps_valid():
    r = ValidationResult()
    r.warn("heads up")
    assert r.valid is True
    assert "heads up" in r.warnings


def test_validation_result_multiple_errors():
    r = ValidationResult()
    r.error("error 1")
    r.error("error 2")
    assert len(r.errors) == 2
    assert not r.valid


# ── _validate_schedule unit tests ──────────────────────────────


def test_validate_schedule_empty_rules():
    """Empty schedule string should not error."""
    r = ValidationResult()
    _validate_schedule("", "Task 1", r)
    assert r.valid


def test_validate_schedule_semicolons_only():
    """Only semicolons = empty rules after split."""
    r = ValidationResult()
    _validate_schedule(";;;", "Task 1", r)
    assert r.valid


def test_validate_schedule_hours_triple_range():
    """Three comma-separated ranges."""
    r = ValidationResult()
    _validate_schedule("hours:6-9,12-14,18-22", "Task 1", r)
    assert r.valid, r.errors


# ── File read error ────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="chmod not reliable on Windows")
def test_unreadable_file(tmp_path: Path):
    """File that exists but can't be read should error."""
    p = tmp_path / "campaign.yaml"
    p.write_text("tasks: []", encoding="utf-8")
    p.chmod(0o000)
    try:
        r = validate_campaign(p)
        assert not r.valid
        assert any("Cannot read" in e for e in r.errors)
    finally:
        p.chmod(0o644)


# ── Large campaign ─────────────────────────────────────────────


def test_many_tasks_valid(tmp_path: Path):
    """Campaign with many tasks should validate correctly."""
    tasks = [{"prompt": f"Task {i}", "id": f"t{i}", "tier": "low"} for i in range(50)]
    p = _write_campaign(tmp_path, {"tasks": tasks})
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_many_tasks_chained_deps(tmp_path: Path):
    """Long chain of dependencies should be valid (no cycle)."""
    tasks = [{"prompt": "first", "id": "t0", "tier": "low"}]
    for i in range(1, 20):
        tasks.append({
            "prompt": f"task {i}",
            "id": f"t{i}",
            "depends_on": f"t{i-1}",
        })
    p = _write_campaign(tmp_path, {"tasks": tasks})
    r = validate_campaign(p)
    assert r.valid, r.errors


# ── skip_if_recent and escalate_on_retry (valid keys) ─────────


def test_skip_if_recent_valid_key(tmp_path: Path):
    """skip_if_recent should be a recognized key (no warning)."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "skip_if_recent": True}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors
    assert not r.warnings


def test_escalate_on_retry_valid_key(tmp_path: Path):
    """escalate_on_retry should be a recognized key (no warning)."""
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "escalate_on_retry": True}]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors
    assert not r.warnings
