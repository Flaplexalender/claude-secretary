"""Tests for campaign YAML validation."""
from __future__ import annotations

from pathlib import Path

import yaml

from secretary.campaign import validate_campaign


def _write_campaign(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "campaign.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")
    return p


def test_valid_campaign(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "Check email", "tier": "low"},
            {"prompt": "Write report", "tier": "medium"},
        ]
    })
    r = validate_campaign(p)
    assert r.valid
    assert not r.errors


def test_missing_file(tmp_path: Path):
    r = validate_campaign(tmp_path / "nope.yaml")
    assert not r.valid
    assert "not found" in r.errors[0]


def test_invalid_yaml(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("{{{invalid", encoding="utf-8")
    r = validate_campaign(p)
    assert not r.valid
    assert "Invalid YAML" in r.errors[0]


def test_missing_tasks_key(tmp_path: Path):
    p = _write_campaign(tmp_path, {"name": "no-tasks"})
    r = validate_campaign(p)
    assert not r.valid
    assert "missing 'tasks'" in r.errors[0]


def test_empty_tasks_warns(tmp_path: Path):
    p = _write_campaign(tmp_path, {"tasks": []})
    r = validate_campaign(p)
    assert r.valid  # warning, not error
    assert any("no tasks" in w for w in r.warnings)


def test_invalid_tier(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "test", "tier": "ultra"}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert "invalid tier" in r.errors[0]


def test_missing_prompt(tmp_path: Path):
    p = _write_campaign(tmp_path, {"tasks": [{"tier": "low"}]})
    r = validate_campaign(p)
    assert not r.valid
    assert "missing or empty 'prompt'" in r.errors[0]


def test_duplicate_id(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "a", "id": "task1"},
            {"prompt": "b", "id": "task1"},
        ]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert "duplicate id" in r.errors[0]


def test_unresolved_dependency(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "depends_on": "missing_task"}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert "Unresolved dependencies" in r.errors[0]


def test_valid_dependency(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "a", "id": "step1"},
            {"prompt": "b", "depends_on": "step1"},
        ]
    })
    r = validate_campaign(p)
    assert r.valid


def test_unknown_keys_warns(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "foo": "bar"}]
    })
    r = validate_campaign(p)
    assert r.valid  # warning, not error
    assert any("unknown keys" in w for w in r.warnings)


def test_invalid_schedule_hours(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "hours:25-30"}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert "hours must be 0-24" in r.errors[0]


def test_valid_schedule(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "schedule": "hours:8-17;weekdays"}]
    })
    r = validate_campaign(p)
    assert r.valid


def test_invalid_timeout(tmp_path: Path):
    p = _write_campaign(tmp_path, {
        "tasks": [{"prompt": "a", "timeout": -5}]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert "non-negative" in r.errors[0]


def test_circular_dependency_direct(tmp_path: Path):
    """A → B → A should be detected as a circular dependency."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "a", "id": "task_a", "depends_on": "task_b"},
            {"prompt": "b", "id": "task_b", "depends_on": "task_a"},
        ]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("Circular dependency" in e for e in r.errors)


def test_circular_dependency_self(tmp_path: Path):
    """Task depending on itself is a circular dependency."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "a", "id": "task_a", "depends_on": "task_a"},
        ]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("Circular dependency" in e for e in r.errors)


def test_circular_dependency_chain(tmp_path: Path):
    """A → B → C → A is a 3-node cycle."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "a", "id": "x", "depends_on": "z"},
            {"prompt": "b", "id": "y", "depends_on": "x"},
            {"prompt": "c", "id": "z", "depends_on": "y"},
        ]
    })
    r = validate_campaign(p)
    assert not r.valid
    assert any("Circular dependency" in e for e in r.errors)


def test_no_circular_dependency_linear(tmp_path: Path):
    """A → B → C (no cycle) should be valid."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "a", "id": "step1"},
            {"prompt": "b", "id": "step2", "depends_on": "step1"},
            {"prompt": "c", "id": "step3", "depends_on": "step2"},
        ]
    })
    r = validate_campaign(p)
    assert r.valid


def test_depends_on_list_form(tmp_path: Path):
    """depends_on: [task_id] (YAML list) should be normalized to string, not crash."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "first", "id": "setup"},
            {"prompt": "second", "id": "run", "depends_on": ["setup"]},
        ]
    })
    r = validate_campaign(p)
    assert r.valid, r.errors


def test_depends_on_empty_list(tmp_path: Path):
    """depends_on: [] (empty list) should be ignored gracefully."""
    p = _write_campaign(tmp_path, {
        "tasks": [
            {"prompt": "standalone", "id": "x", "depends_on": []},
        ]
    })
    r = validate_campaign(p)
    assert r.valid
