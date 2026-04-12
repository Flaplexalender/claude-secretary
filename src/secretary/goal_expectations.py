"""Goal Expectations — deterministic environment assertions for step verification.

Research-backed layer that bridges the gap between "the agent says it did
something" and "the environment confirms the change happened."

Problem:
    The verification judge (Haiku) only sees the agent's OUTPUT TEXT — not
    the actual filesystem/environment state.  An agent can claim "I created
    config.yaml" when the file was never created, and the text judge passes it.

Solution:
    Steps gain structured `preconditions` (checked before execution) and
    `expected_effects` (checked after execution).  These are deterministic,
    read-only filesystem checks — no command execution, no network calls.

Assertion types:
    - file_exists: os.path.exists(path) — files or directories
    - file_contains: read file, check substring present
    - json_field: read JSON file, check dotted field path has expected value

Integration:
    - Pre-execution: check preconditions → if fail, skip step + replan
    - Post-execution: check expected_effects → if fail, override LLM verdict

References:
    - Anthropic "Building Effective Agents": "ground truth from environment"
    - Kwa et al. (NeurIPS 2025): "reliability and ability to adapt to mistakes"
    - RAP (Hao et al.): LLMs lack world model for state prediction
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("secretary.goal_expectations")

ASSERTION_TYPES = {"file_exists", "file_contains", "json_field"}

# Maximum file size we'll read for file_contains/json_field (1 MB)
_MAX_READ_BYTES = 1_048_576


def parse_assertions(raw: list[Any]) -> list[dict[str, Any]]:
    """Validate and normalise assertion dicts from LLM output.

    Silently drops malformed entries — the LLM output may be imperfect.
    Returns only valid, recognised assertions.
    """
    if not isinstance(raw, list):
        return []

    valid: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        atype = item.get("type", "")
        if atype not in ASSERTION_TYPES:
            continue
        path = item.get("path", "")
        if not isinstance(path, str) or not path:
            continue

        entry: dict[str, Any] = {"type": atype, "path": path}

        if atype == "file_contains":
            pattern = item.get("pattern", "")
            if not isinstance(pattern, str) or not pattern:
                continue
            entry["pattern"] = pattern

        elif atype == "json_field":
            field = item.get("field", "")
            if not isinstance(field, str) or not field:
                continue
            entry["field"] = field
            # 'value' is the expected value — can be any JSON type
            if "value" in item:
                entry["value"] = item["value"]

        valid.append(entry)
    return valid


def _resolve_path(path: str, base_dir: str) -> str:
    """Resolve a relative path against the base directory.

    Returns absolute path. Prevents path traversal outside base_dir.
    """
    if os.path.isabs(path):
        resolved = os.path.normpath(path)
    else:
        resolved = os.path.normpath(os.path.join(base_dir, path))

    # Prevent traversal outside base_dir
    base = os.path.normpath(base_dir)
    if not resolved.startswith(base + os.sep) and resolved != base:
        return ""
    return resolved


def _check_file_exists(path: str) -> tuple[bool, str]:
    """Check whether a file or directory exists."""
    exists = os.path.exists(path)
    detail = "file exists" if exists else "file not found"
    return exists, detail


def _check_file_contains(path: str, pattern: str) -> tuple[bool, str]:
    """Check whether a file contains a substring."""
    if not os.path.isfile(path):
        return False, "file not found"
    try:
        size = os.path.getsize(path)
        if size > _MAX_READ_BYTES:
            return False, f"file too large ({size} bytes, limit {_MAX_READ_BYTES})"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if pattern in content:
            return True, f"pattern found in file"
        return False, f"pattern not found in file"
    except OSError as e:
        return False, f"read error: {e}"


def _get_nested_field(data: Any, field_path: str) -> tuple[bool, Any]:
    """Traverse a dotted field path into nested data.

    Returns (found, value). Supports dict keys and integer list indices.
    """
    parts = field_path.split(".")
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, current


def _check_json_field(path: str, field: str, expected: Any = None) -> tuple[bool, str]:
    """Check a JSON field has an expected value (or just exists)."""
    if not os.path.isfile(path):
        return False, "file not found"
    try:
        size = os.path.getsize(path)
        if size > _MAX_READ_BYTES:
            return False, f"file too large ({size} bytes)"
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return False, f"read/parse error: {e}"

    found, actual = _get_nested_field(data, field)
    if not found:
        return False, f"field '{field}' not found"

    if expected is None:
        # Just checking field exists
        return True, f"field '{field}' exists (value: {str(actual)[:100]})"

    if actual == expected:
        return True, f"field '{field}' == {json.dumps(expected)}"
    return False, f"field '{field}' = {str(actual)[:100]}, expected {json.dumps(expected)}"


def check_assertions(
    assertions: list[dict[str, Any]],
    base_dir: str,
) -> list[dict[str, Any]]:
    """Run assertions against the environment.

    Returns a list of results: [{assertion, passed, detail}].
    All checks are read-only and safe.
    """
    results: list[dict[str, Any]] = []
    for assertion in assertions:
        atype = assertion.get("type", "")
        raw_path = assertion.get("path", "")

        resolved = _resolve_path(raw_path, base_dir)
        if not resolved:
            results.append({
                "assertion": assertion,
                "passed": False,
                "detail": f"path '{raw_path}' resolves outside base directory",
            })
            continue

        passed = False
        detail = "unknown assertion type"

        if atype == "file_exists":
            passed, detail = _check_file_exists(resolved)
        elif atype == "file_contains":
            passed, detail = _check_file_contains(resolved, assertion.get("pattern", ""))
        elif atype == "json_field":
            passed, detail = _check_json_field(
                resolved, assertion.get("field", ""),
                assertion.get("value"),
            )

        results.append({
            "assertion": assertion,
            "passed": passed,
            "detail": detail,
        })

    return results


def format_assertion_results(results: list[dict[str, Any]]) -> str:
    """Format assertion results as text for injection into verification context.

    Returns empty string if no results.
    """
    if not results:
        return ""

    lines: list[str] = ["## Environment Assertions (ground truth)"]
    all_passed = all(r["passed"] for r in results)
    passed_count = sum(1 for r in results if r["passed"])
    lines.append(f"Result: {passed_count}/{len(results)} passed")

    for r in results:
        icon = "PASS" if r["passed"] else "FAIL"
        atype = r["assertion"].get("type", "?")
        path = r["assertion"].get("path", "?")
        detail = r.get("detail", "")
        lines.append(f"  [{icon}] {atype}: {path} — {detail}")

    if not all_passed:
        lines.append(
            "\nWARNING: Environment assertions failed. "
            "The agent's text output may not reflect actual state."
        )

    return "\n".join(lines)
