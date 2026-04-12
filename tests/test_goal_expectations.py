"""Tests for Layer 27: goal_expectations.py — Environment Assertions."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from secretary.goal_expectations import (
    ASSERTION_TYPES,
    _check_file_contains,
    _check_file_exists,
    _check_json_field,
    _get_nested_field,
    _resolve_path,
    check_assertions,
    format_assertion_results,
    parse_assertions,
)


# ── parse_assertions ────────────────────────────────────────────


def test_parse_assertions_valid_file_exists() -> None:
    raw = [{"type": "file_exists", "path": "config.yaml"}]
    result = parse_assertions(raw)
    assert len(result) == 1
    assert result[0] == {"type": "file_exists", "path": "config.yaml"}


def test_parse_assertions_valid_file_contains() -> None:
    raw = [{"type": "file_contains", "path": "config.yaml", "pattern": "enabled: true"}]
    result = parse_assertions(raw)
    assert len(result) == 1
    assert result[0]["pattern"] == "enabled: true"


def test_parse_assertions_valid_json_field() -> None:
    raw = [{"type": "json_field", "path": "data/state.json", "field": "status", "value": "ready"}]
    result = parse_assertions(raw)
    assert len(result) == 1
    assert result[0]["field"] == "status"
    assert result[0]["value"] == "ready"


def test_parse_assertions_json_field_no_value() -> None:
    """json_field without 'value' just checks field existence."""
    raw = [{"type": "json_field", "path": "data/state.json", "field": "status"}]
    result = parse_assertions(raw)
    assert len(result) == 1
    assert "value" not in result[0]


def test_parse_assertions_drops_unknown_type() -> None:
    raw = [{"type": "command_exec", "path": "rm -rf /"}]
    result = parse_assertions(raw)
    assert len(result) == 0


def test_parse_assertions_drops_missing_path() -> None:
    raw = [{"type": "file_exists"}]
    result = parse_assertions(raw)
    assert len(result) == 0


def test_parse_assertions_drops_non_dict() -> None:
    raw = ["file_exists", 42, None]
    result = parse_assertions(raw)
    assert len(result) == 0


def test_parse_assertions_not_list() -> None:
    result = parse_assertions("not a list")
    assert result == []


def test_parse_assertions_file_contains_missing_pattern() -> None:
    raw = [{"type": "file_contains", "path": "config.yaml"}]
    result = parse_assertions(raw)
    assert len(result) == 0


def test_parse_assertions_json_field_missing_field() -> None:
    raw = [{"type": "json_field", "path": "data.json"}]
    result = parse_assertions(raw)
    assert len(result) == 0


def test_parse_assertions_mixed_valid_invalid() -> None:
    raw = [
        {"type": "file_exists", "path": "ok.txt"},
        {"type": "bad_type", "path": "nope"},
        {"type": "file_contains", "path": "ok.txt", "pattern": "hello"},
    ]
    result = parse_assertions(raw)
    assert len(result) == 2
    assert result[0]["type"] == "file_exists"
    assert result[1]["type"] == "file_contains"


# ── _resolve_path ───────────────────────────────────────────────


def test_resolve_path_relative() -> None:
    base = tempfile.gettempdir()
    resolved = _resolve_path("sub/file.txt", base)
    assert resolved == os.path.normpath(os.path.join(base, "sub", "file.txt"))


def test_resolve_path_prevents_traversal() -> None:
    base = os.path.join(tempfile.gettempdir(), "sandbox")
    resolved = _resolve_path("../../etc/passwd", base)
    assert resolved == ""


def test_resolve_path_absolute_inside_base() -> None:
    base = tempfile.gettempdir()
    inside = os.path.join(base, "sub", "file.txt")
    resolved = _resolve_path(inside, base)
    assert resolved == os.path.normpath(inside)


def test_resolve_path_absolute_outside_base() -> None:
    base = os.path.join(tempfile.gettempdir(), "sandbox")
    outside = os.path.join(tempfile.gettempdir(), "other", "file.txt")
    resolved = _resolve_path(outside, base)
    assert resolved == ""


# ── _check_file_exists ──────────────────────────────────────────


def test_check_file_exists_true(tmp_path) -> None:
    f = tmp_path / "exists.txt"
    f.write_text("hi")
    passed, detail = _check_file_exists(str(f))
    assert passed is True
    assert "exists" in detail


def test_check_file_exists_false(tmp_path) -> None:
    passed, detail = _check_file_exists(str(tmp_path / "nope.txt"))
    assert passed is False
    assert "not found" in detail


def test_check_file_exists_directory(tmp_path) -> None:
    """Directories should pass file_exists (LLM preconditions reference dirs like 'src')."""
    passed, detail = _check_file_exists(str(tmp_path))
    assert passed is True
    assert "exists" in detail


# ── _check_file_contains ────────────────────────────────────────


def test_check_file_contains_found(tmp_path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("hello world\nfoo bar baz")
    passed, detail = _check_file_contains(str(f), "foo bar")
    assert passed is True
    assert "found" in detail


def test_check_file_contains_not_found(tmp_path) -> None:
    f = tmp_path / "data.txt"
    f.write_text("hello world")
    passed, detail = _check_file_contains(str(f), "missing")
    assert passed is False
    assert "not found" in detail


def test_check_file_contains_missing_file() -> None:
    passed, detail = _check_file_contains("/nonexistent/file.txt", "x")
    assert passed is False
    assert "not found" in detail


# ── _get_nested_field ───────────────────────────────────────────


def test_get_nested_field_simple() -> None:
    found, val = _get_nested_field({"a": 1}, "a")
    assert found is True
    assert val == 1


def test_get_nested_field_dotted() -> None:
    found, val = _get_nested_field({"a": {"b": {"c": 42}}}, "a.b.c")
    assert found is True
    assert val == 42


def test_get_nested_field_list_index() -> None:
    found, val = _get_nested_field({"items": [10, 20, 30]}, "items.1")
    assert found is True
    assert val == 20


def test_get_nested_field_missing() -> None:
    found, _ = _get_nested_field({"a": 1}, "b")
    assert found is False


def test_get_nested_field_missing_nested() -> None:
    found, _ = _get_nested_field({"a": {"b": 1}}, "a.c")
    assert found is False


# ── _check_json_field ───────────────────────────────────────────


def test_check_json_field_exists(tmp_path) -> None:
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"status": "ready", "count": 5}))
    passed, detail = _check_json_field(str(f), "status")
    assert passed is True
    assert "exists" in detail


def test_check_json_field_value_match(tmp_path) -> None:
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"status": "ready"}))
    passed, detail = _check_json_field(str(f), "status", "ready")
    assert passed is True


def test_check_json_field_value_mismatch(tmp_path) -> None:
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"status": "blocked"}))
    passed, detail = _check_json_field(str(f), "status", "ready")
    assert passed is False
    assert "expected" in detail


def test_check_json_field_nested(tmp_path) -> None:
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"meta": {"version": 3}}))
    passed, detail = _check_json_field(str(f), "meta.version", 3)
    assert passed is True


def test_check_json_field_missing_field(tmp_path) -> None:
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"a": 1}))
    passed, detail = _check_json_field(str(f), "missing")
    assert passed is False
    assert "not found" in detail


def test_check_json_field_invalid_json(tmp_path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("not json{{{")
    passed, detail = _check_json_field(str(f), "field")
    assert passed is False
    assert "error" in detail


def test_check_json_field_missing_file() -> None:
    passed, detail = _check_json_field("/nonexistent/file.json", "field")
    assert passed is False
    assert "not found" in detail


# ── check_assertions (integration) ──────────────────────────────


def test_check_assertions_all_pass(tmp_path) -> None:
    (tmp_path / "config.yaml").write_text("enabled: true\nport: 8080")
    (tmp_path / "state.json").write_text(json.dumps({"status": "ready"}))

    assertions = [
        {"type": "file_exists", "path": "config.yaml"},
        {"type": "file_contains", "path": "config.yaml", "pattern": "enabled: true"},
        {"type": "json_field", "path": "state.json", "field": "status", "value": "ready"},
    ]
    results = check_assertions(assertions, str(tmp_path))
    assert all(r["passed"] for r in results)
    assert len(results) == 3


def test_check_assertions_mixed(tmp_path) -> None:
    (tmp_path / "exists.txt").write_text("hello")

    assertions = [
        {"type": "file_exists", "path": "exists.txt"},
        {"type": "file_exists", "path": "missing.txt"},
    ]
    results = check_assertions(assertions, str(tmp_path))
    assert results[0]["passed"] is True
    assert results[1]["passed"] is False


def test_check_assertions_path_traversal(tmp_path) -> None:
    assertions = [{"type": "file_exists", "path": "../../etc/passwd"}]
    results = check_assertions(assertions, str(tmp_path))
    assert results[0]["passed"] is False
    assert "outside" in results[0]["detail"]


def test_check_assertions_empty() -> None:
    results = check_assertions([], "/tmp")
    assert results == []


# ── format_assertion_results ────────────────────────────────────


def test_format_assertion_results_all_pass() -> None:
    results = [
        {"assertion": {"type": "file_exists", "path": "f.txt"}, "passed": True, "detail": "file exists"},
    ]
    text = format_assertion_results(results)
    assert "PASS" in text
    assert "1/1 passed" in text
    assert "WARNING" not in text


def test_format_assertion_results_with_failure() -> None:
    results = [
        {"assertion": {"type": "file_exists", "path": "f.txt"}, "passed": True, "detail": "file exists"},
        {"assertion": {"type": "file_exists", "path": "g.txt"}, "passed": False, "detail": "file not found"},
    ]
    text = format_assertion_results(results)
    assert "1/2 passed" in text
    assert "FAIL" in text
    assert "WARNING" in text


def test_format_assertion_results_empty() -> None:
    text = format_assertion_results([])
    assert text == ""


# ── Decomposition integration (parse_assertions in _parse_decomp_response) ──


def test_decomp_response_preserves_assertions() -> None:
    """Verify that _parse_decomp_response handles preconditions/expected_effects."""
    from secretary.goal_decomposition import _parse_decomp_response

    response_json = json.dumps({
        "steps": [
            {
                "action": "Read the config file",
                "verification": "Config values extracted",
                "tier": "low",
                "preconditions": [
                    {"type": "file_exists", "path": "config.yaml"},
                ],
                "expected_effects": [
                    {"type": "file_exists", "path": "data/output.json"},
                    {"type": "json_field", "path": "data/output.json", "field": "status", "value": "done"},
                ],
            },
        ],
        "rationale": "Start with reading config.",
    })
    result = _parse_decomp_response(response_json)
    steps = result["steps"]
    assert len(steps) == 1
    assert steps[0]["preconditions"] == [{"type": "file_exists", "path": "config.yaml"}]
    assert len(steps[0]["expected_effects"]) == 2


def test_decomp_response_no_assertions() -> None:
    """Steps without assertions should work fine (backward compat)."""
    from secretary.goal_decomposition import _parse_decomp_response

    response_json = json.dumps({
        "steps": [{"action": "Do thing", "verification": "Thing done", "tier": "low"}],
        "rationale": "Simple.",
    })
    result = _parse_decomp_response(response_json)
    steps = result["steps"]
    assert len(steps) == 1
    assert "preconditions" not in steps[0]
    assert "expected_effects" not in steps[0]


def test_decomp_response_invalid_assertions_dropped() -> None:
    """Invalid assertion entries should be silently dropped."""
    from secretary.goal_decomposition import _parse_decomp_response

    response_json = json.dumps({
        "steps": [
            {
                "action": "Do thing",
                "verification": "Thing done",
                "tier": "low",
                "preconditions": [
                    {"type": "invalid_type", "path": "x"},
                    {"type": "file_exists", "path": "valid.txt"},
                ],
                "expected_effects": [
                    {"type": "file_contains"},  # missing path + pattern
                ],
            },
        ],
        "rationale": "Test.",
    })
    result = _parse_decomp_response(response_json)
    steps = result["steps"]
    assert len(steps) == 1
    # Only valid assertion preserved
    assert steps[0]["preconditions"] == [{"type": "file_exists", "path": "valid.txt"}]
    # All invalid effects dropped → no key
    assert "expected_effects" not in steps[0]


# ── step_to_task preserves assertion fields ─────────────────────


def test_step_to_task_includes_assertions() -> None:
    from secretary.goal_decomposition import step_to_task

    step = {
        "step_id": "g1.sg1.1",
        "action": "Read config",
        "verification": "Config read",
        "tier": "low",
        "status": "pending",
        "preconditions": [{"type": "file_exists", "path": "config.yaml"}],
        "expected_effects": [{"type": "file_exists", "path": "output.json"}],
    }
    task = step_to_task(step, "g1.sg1", "g1")
    assert task["_preconditions"] == step["preconditions"]
    assert task["_expected_effects"] == step["expected_effects"]


def test_step_to_task_no_assertions() -> None:
    from secretary.goal_decomposition import step_to_task

    step = {
        "step_id": "g1.sg1.1",
        "action": "Do thing",
        "verification": "Done",
        "tier": "low",
        "status": "pending",
    }
    task = step_to_task(step, "g1.sg1", "g1")
    assert task["_preconditions"] == []
    assert task["_expected_effects"] == []
