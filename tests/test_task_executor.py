"""Unit tests for src/secretary/task_executor.py

Covers:
- validate_scope(): allowed paths, out-of-scope paths, mixed batches,
  edge-cases (empty list, non-file_edit calls, path normalization).
- execute_task(): scope check fires BEFORE any tool runs; clean batches
  execute fully; results are returned in order.
- ScopeViolationError: message format, attributes.
"""
from __future__ import annotations

import pytest

from secretary.task_executor import (
    ALLOWED_WRITE_PREFIXES,
    ScopeViolationError,
    _extract_file_edit_paths,
    _is_allowed_path,
    _normalize_path,
    execute_task,
    validate_scope,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _edit(path: str, old: str = "x", new: str = "y") -> dict:
    """Return a minimal file_edit tool-call dict."""
    return {"name": "file_edit", "input": {"path": path, "old_string": old, "new_string": new}}


def _read(path: str) -> dict:
    """Return a minimal file_read tool-call dict (always allowed)."""
    return {"name": "file_read", "input": {"path": path}}


def _grep(pattern: str = "TODO", path: str = ".") -> dict:
    return {"name": "grep_search", "input": {"pattern": pattern, "path": path}}


# ── _normalize_path ───────────────────────────────────────────────────────────

class TestNormalizePath:
    def test_forward_slashes_unchanged(self):
        assert _normalize_path("src/secretary/foo.py") == "src/secretary/foo.py"

    def test_backslashes_converted(self):
        assert _normalize_path("src\\secretary\\foo.py") == "src/secretary/foo.py"

    def test_leading_dot_slash_stripped(self):
        assert _normalize_path("./src/secretary/foo.py") == "src/secretary/foo.py"

    def test_leading_dot_backslash_stripped(self):
        assert _normalize_path(".\\src\\secretary\\foo.py") == "src/secretary/foo.py"

    def test_mixed_slashes(self):
        assert _normalize_path(".\\src/secretary\\foo.py") == "src/secretary/foo.py"

    def test_no_leading_dot(self):
        assert _normalize_path("tests/test_foo.py") == "tests/test_foo.py"

    def test_empty_string(self):
        assert _normalize_path("") == ""


# ── _is_allowed_path ─────────────────────────────────────────────────────────

class TestIsAllowedPath:
    # ── allowed ──────────────────────────────────────────────────────────────
    def test_src_secretary_allowed(self):
        assert _is_allowed_path("src/secretary/module.py") is True

    def test_tests_allowed(self):
        assert _is_allowed_path("tests/test_foo.py") is True

    def test_nested_under_src_secretary(self):
        assert _is_allowed_path("src/secretary/subdir/deep.py") is True

    def test_leading_dotslash_src_secretary(self):
        assert _is_allowed_path("./src/secretary/bar.py") is True

    def test_backslash_src_secretary(self):
        assert _is_allowed_path("src\\secretary\\baz.py") is True

    def test_backslash_tests(self):
        assert _is_allowed_path("tests\\test_bar.py") is True

    # ── not allowed ──────────────────────────────────────────────────────────
    def test_config_yaml_denied(self):
        assert _is_allowed_path("config.yaml") is False

    def test_root_py_file_denied(self):
        assert _is_allowed_path("setup.py") is False

    def test_src_only_denied(self):
        # "src/" alone is not under src/secretary/
        assert _is_allowed_path("src/main.py") is False

    def test_data_dir_denied(self):
        assert _is_allowed_path("data/scratchpad.md") is False

    def test_dotenv_denied(self):
        assert _is_allowed_path(".env") is False

    def test_readme_denied(self):
        assert _is_allowed_path("README.md") is False

    def test_partial_prefix_denied(self):
        # Must not allow "src/secretary_extra/foo.py" — prefix match is exact dir
        assert _is_allowed_path("src/secretary_extra/foo.py") is False

    def test_tests_prefix_in_subdir_denied(self):
        # "tests_extra/foo.py" should NOT be allowed
        assert _is_allowed_path("tests_extra/foo.py") is False


# ── _extract_file_edit_paths ─────────────────────────────────────────────────

class TestExtractFileEditPaths:
    def test_empty_list(self):
        assert _extract_file_edit_paths([]) == []

    def test_single_edit(self):
        assert _extract_file_edit_paths([_edit("src/secretary/foo.py")]) == ["src/secretary/foo.py"]

    def test_multiple_edits(self):
        calls = [_edit("src/secretary/a.py"), _edit("tests/test_b.py")]
        assert _extract_file_edit_paths(calls) == ["src/secretary/a.py", "tests/test_b.py"]

    def test_non_edit_calls_excluded(self):
        calls = [_read("config.yaml"), _grep("TODO")]
        assert _extract_file_edit_paths(calls) == []

    def test_mixed_calls_only_edits_returned(self):
        calls = [_read("README.md"), _edit("src/secretary/foo.py"), _grep()]
        assert _extract_file_edit_paths(calls) == ["src/secretary/foo.py"]

    def test_call_missing_input_key_skipped(self):
        bad = {"name": "file_edit"}  # no "input" key
        assert _extract_file_edit_paths([bad]) == []

    def test_call_missing_path_in_input_skipped(self):
        bad = {"name": "file_edit", "input": {"old_string": "x", "new_string": "y"}}
        assert _extract_file_edit_paths([bad]) == []

    def test_non_dict_entries_skipped(self):
        assert _extract_file_edit_paths(["not_a_dict", 42, None]) == []  # type: ignore[list-item]


# ── validate_scope ────────────────────────────────────────────────────────────

class TestValidateScope:
    # ── should pass silently ─────────────────────────────────────────────────
    def test_empty_batch_passes(self):
        validate_scope([])  # no exception

    def test_read_only_batch_passes(self):
        validate_scope([_read("config.yaml"), _grep("BUG")])

    def test_allowed_edit_passes(self):
        validate_scope([_edit("src/secretary/utils.py")])

    def test_allowed_test_edit_passes(self):
        validate_scope([_edit("tests/test_foo.py")])

    def test_mixed_allowed_edits_pass(self):
        validate_scope([
            _read("README.md"),
            _edit("src/secretary/a.py"),
            _edit("tests/test_a.py"),
            _grep("TODO"),
        ])

    # ── should raise ScopeViolationError ─────────────────────────────────────
    def test_config_yaml_raises(self):
        with pytest.raises(ScopeViolationError) as exc_info:
            validate_scope([_edit("config.yaml")])
        assert exc_info.value.path == "config.yaml"

    def test_root_py_raises(self):
        with pytest.raises(ScopeViolationError) as exc_info:
            validate_scope([_edit("setup.py")])
        assert exc_info.value.path == "setup.py"

    def test_data_file_raises(self):
        with pytest.raises(ScopeViolationError) as exc_info:
            validate_scope([_edit("data/scratchpad.md")])
        assert exc_info.value.path == "data/scratchpad.md"

    def test_src_non_secretary_raises(self):
        with pytest.raises(ScopeViolationError) as exc_info:
            validate_scope([_edit("src/main.py")])
        assert exc_info.value.path == "src/main.py"

    def test_mixed_batch_one_bad_raises(self):
        """Even if the first edit is allowed, a later bad one should raise."""
        with pytest.raises(ScopeViolationError) as exc_info:
            validate_scope([
                _edit("src/secretary/good.py"),
                _edit("config.yaml"),          # bad
            ])
        assert exc_info.value.path == "config.yaml"

    def test_bad_edit_before_good_raises(self):
        with pytest.raises(ScopeViolationError) as exc_info:
            validate_scope([
                _edit(".env"),                 # bad
                _edit("src/secretary/ok.py"),
            ])
        assert exc_info.value.path == ".env"

    def test_backslash_path_outside_scope_raises(self):
        with pytest.raises(ScopeViolationError):
            validate_scope([_edit("data\\notes.md")])

    def test_partial_prefix_raises(self):
        with pytest.raises(ScopeViolationError):
            validate_scope([_edit("src/secretary_extra/foo.py")])

    def test_tests_prefix_lookalike_raises(self):
        with pytest.raises(ScopeViolationError):
            validate_scope([_edit("tests_extra/foo.py")])


# ── ScopeViolationError ───────────────────────────────────────────────────────

class TestScopeViolationError:
    def test_path_attribute(self):
        err = ScopeViolationError("bad/path.py")
        assert err.path == "bad/path.py"

    def test_allowed_attribute_default(self):
        err = ScopeViolationError("bad/path.py")
        assert err.allowed == ALLOWED_WRITE_PREFIXES

    def test_allowed_attribute_custom(self):
        custom = ("only/this/",)
        err = ScopeViolationError("bad/path.py", allowed=custom)
        assert err.allowed == custom

    def test_message_contains_path(self):
        err = ScopeViolationError("config.yaml")
        assert "config.yaml" in str(err)

    def test_message_contains_allowed_prefixes(self):
        err = ScopeViolationError("config.yaml")
        for prefix in ALLOWED_WRITE_PREFIXES:
            assert prefix in str(err)

    def test_is_exception(self):
        assert isinstance(ScopeViolationError("x"), Exception)


# ── execute_task ──────────────────────────────────────────────────────────────

class TestExecuteTask:
    def _make_tracker(self):
        """Return an executor_fn that records which calls it received."""
        calls: list[dict] = []

        def executor_fn(call: dict):
            calls.append(call)
            return f"result:{call.get('name')}"

        return executor_fn, calls

    # ── happy path ───────────────────────────────────────────────────────────
    def test_empty_batch_returns_empty(self):
        fn, log = self._make_tracker()
        result = execute_task([], fn)
        assert result == []
        assert log == []

    def test_allowed_edit_executes(self):
        fn, log = self._make_tracker()
        call = _edit("src/secretary/foo.py")
        results = execute_task([call], fn)
        assert results == ["result:file_edit"]
        assert log == [call]

    def test_read_only_executes(self):
        fn, log = self._make_tracker()
        calls = [_read("README.md"), _grep("TODO")]
        results = execute_task(calls, fn)
        assert len(results) == 2
        assert log == calls

    def test_results_in_order(self):
        """Results must be returned in the same order as tool_calls."""
        counter = {"n": 0}

        def ordered_fn(call):
            counter["n"] += 1
            return counter["n"]

        batch = [_read("a"), _read("b"), _edit("src/secretary/c.py"), _read("d")]
        results = execute_task(batch, ordered_fn)
        assert results == [1, 2, 3, 4]

    # ── scope check fires BEFORE any execution ───────────────────────────────
    def test_out_of_scope_edit_raises_before_execution(self):
        fn, log = self._make_tracker()
        batch = [
            _read("README.md"),           # would be fine
            _edit("config.yaml"),         # out of scope — must abort whole batch
            _edit("src/secretary/ok.py"), # never reached
        ]
        with pytest.raises(ScopeViolationError):
            execute_task(batch, fn)
        # executor_fn must NOT have been called at all
        assert log == [], "executor_fn was called despite scope violation"

    def test_out_of_scope_atomic_rejection(self):
        """The batch is rejected atomically: zero tools run on any violation."""
        fn, log = self._make_tracker()
        batch = [
            _edit("src/secretary/good.py"),  # allowed
            _edit("data/bad.md"),             # violates scope
        ]
        with pytest.raises(ScopeViolationError) as exc_info:
            execute_task(batch, fn)
        assert exc_info.value.path == "data/bad.md"
        assert log == []

    def test_scope_error_propagates_correctly(self):
        fn, _ = self._make_tracker()
        with pytest.raises(ScopeViolationError) as exc_info:
            execute_task([_edit(".env")], fn)
        assert ".env" in str(exc_info.value)

    def test_executor_fn_not_called_on_violation(self):
        """Regression: executor_fn must be a pure side-effect gate."""
        executed = []

        def strict_fn(call):
            executed.append(call["name"])
            return "ok"

        with pytest.raises(ScopeViolationError):
            execute_task([_edit("src/secretary/ok.py"), _edit("README.md")], strict_fn)
        assert executed == [], f"Expected no executions, got {executed}"
