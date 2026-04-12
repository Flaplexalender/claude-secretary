"""Tests for self-improvement pipeline — offline, no API calls.

Tests the sandbox copy, change detection, promotion logic, and the
full improve() async pipeline with mocked agent + test runner.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from secretary.self_improve import (
    _copy_to_sandbox, _detect_changes, _promote_changes,
    _backup_originals, rollback, ImprovementResult, improve,
    _filter_allowed_changes, _map_changed_to_tests,
    _git_commit_promoted, _extract_failure_summary,
    _generate_change_plan, _retry_with_failure_context,
    _MAX_FOCUSED_RETRIES,
)
from secretary.goal_self_improve import _parse_json_response


# ══════════════════════════════════════════════════════════════
#  Unit tests — sandbox helpers (existing)
# ══════════════════════════════════════════════════════════════


def test_copy_to_sandbox(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("print('hello')")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "junk.pyc").write_text("junk")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)

    assert (sandbox / "main.py").exists()
    assert not (sandbox / "__pycache__").exists()


def test_detect_new_file(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("original")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)
    (sandbox / "new_file.py").write_text("new content")

    changes = _detect_changes(source, sandbox)
    assert any("NEW: new_file.py" in c for c in changes)


def test_detect_modified_file(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("original")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)
    (sandbox / "main.py").write_text("modified")

    changes = _detect_changes(source, sandbox)
    assert any("MOD: main.py" in c for c in changes)


def test_detect_deleted_file(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("original")
    (source / "to_delete.py").write_text("delete me")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)
    (sandbox / "to_delete.py").unlink()

    changes = _detect_changes(source, sandbox)
    assert any("DEL: to_delete.py" in c for c in changes)


def test_promote_new_file(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "new.py").write_text("new content")

    changes = ["NEW: new.py"]
    _promote_changes(source, sandbox, changes)
    assert (source / "new.py").read_text() == "new content"


def test_promote_modified_file(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("original")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "main.py").write_text("modified")

    changes = ["MOD: main.py"]
    _promote_changes(source, sandbox, changes)
    assert (source / "main.py").read_text() == "modified"


def test_promote_deleted_file(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "old.py").write_text("delete me")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    changes = ["DEL: old.py"]
    _promote_changes(source, sandbox, changes)
    assert not (source / "old.py").exists()


def test_no_changes_detected(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("same")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)

    changes = _detect_changes(source, sandbox)
    assert changes == []


def test_backup_originals(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("original content")
    (source / "utils.py").write_text("util content")

    changes = ["MOD: main.py", "NEW: brand_new.py"]
    backup = _backup_originals(source, changes)

    assert backup.exists()
    assert (backup / "main.py").read_text() == "original content"
    # NEW files don't get backed up (nothing to restore)
    assert not (backup / "brand_new.py").exists()


def test_rollback_restores_originals(tmp_path: Path):
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("original content")

    # Create a backup
    changes = ["MOD: main.py"]
    backup = _backup_originals(source, changes)

    # Simulate promotion (overwrite)
    (source / "main.py").write_text("modified by agent")
    assert (source / "main.py").read_text() == "modified by agent"

    # Rollback
    restored = rollback(source, backup)
    assert "main.py" in restored
    assert (source / "main.py").read_text() == "original content"
    assert not backup.exists()  # backup is cleaned up


def test_copy_to_sandbox_removes_stale(tmp_path: Path):
    """If sandbox already exists from a previous run, it's removed and recreated."""
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("v2")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "stale.txt").write_text("leftover from failed run")

    _copy_to_sandbox(source, sandbox)

    # Stale file should be gone, new content present
    assert not (sandbox / "stale.txt").exists()
    assert (sandbox / "main.py").read_text() == "v2"


# ══════════════════════════════════════════════════════════════
#  Additional unit tests — edge cases
# ══════════════════════════════════════════════════════════════


def test_detect_changes_in_subdirectory(tmp_path: Path):
    """Changes in nested subdirectories are detected correctly."""
    source = tmp_path / "project"
    (source / "src" / "pkg").mkdir(parents=True)
    (source / "src" / "pkg" / "mod.py").write_text("original")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)
    (sandbox / "src" / "pkg" / "mod.py").write_text("modified")
    (sandbox / "src" / "pkg" / "new_mod.py").write_text("new")

    changes = _detect_changes(source, sandbox)
    rel_changes = [c.replace("\\", "/") for c in changes]
    assert any("MOD: src/pkg/mod.py" in c for c in rel_changes)
    assert any("NEW: src/pkg/new_mod.py" in c for c in rel_changes)


def test_detect_changes_ignores_pycache_in_subdirs(tmp_path: Path):
    """__pycache__ dirs inside subdirectories are excluded from diff."""
    source = tmp_path / "project"
    (source / "src").mkdir(parents=True)
    (source / "src" / "main.py").write_text("code")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)
    # Simulate pycache appearing in sandbox
    (sandbox / "src" / "__pycache__").mkdir()
    (sandbox / "src" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00")

    changes = _detect_changes(source, sandbox)
    assert changes == []  # pycache should be ignored


def test_detect_changes_ignores_egg_info(tmp_path: Path):
    """*.egg-info directories are excluded from diff."""
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("code")

    sandbox = tmp_path / "sandbox"
    _copy_to_sandbox(source, sandbox)
    # Simulate egg-info appearing
    (sandbox / "mypackage.egg-info").mkdir()
    (sandbox / "mypackage.egg-info" / "PKG-INFO").write_text("metadata")

    changes = _detect_changes(source, sandbox)
    assert changes == []


def test_backup_originals_replaces_existing_backup(tmp_path: Path):
    """If a backup dir already exists (stale), it's replaced."""
    source = tmp_path / "project"
    source.mkdir()
    (source / "main.py").write_text("v1")

    # Create first backup
    changes = ["MOD: main.py"]
    backup1 = _backup_originals(source, changes)
    assert (backup1 / "main.py").read_text() == "v1"

    # Modify source and create second backup
    (source / "main.py").write_text("v2")
    backup2 = _backup_originals(source, changes)
    assert backup2 == backup1  # same path
    assert (backup2 / "main.py").read_text() == "v2"


def test_promote_nested_new_file(tmp_path: Path):
    """Promoting a new file in a nested dir creates parent dirs."""
    source = tmp_path / "project"
    source.mkdir()

    sandbox = tmp_path / "sandbox"
    (sandbox / "new_pkg" / "sub").mkdir(parents=True)
    (sandbox / "new_pkg" / "sub" / "mod.py").write_text("new nested module")

    changes = ["NEW: new_pkg/sub/mod.py"]
    _promote_changes(source, sandbox, changes)
    assert (source / "new_pkg" / "sub" / "mod.py").read_text() == "new nested module"


def test_improvement_result_defaults():
    """ImprovementResult has sensible defaults."""
    r = ImprovementResult(task="test task", sandbox_dir="/tmp/sandbox")
    assert r.tests_passed is False
    assert r.promoted is False
    assert r.error is None
    assert r.changed_files == []
    assert r.cost_usd == 0.0
    assert r.num_turns == 0


# ══════════════════════════════════════════════════════════════
#  Async pipeline tests — improve() with mocked agent + tests
# ══════════════════════════════════════════════════════════════


@dataclass
class _FakeAgentResult:
    """Fake result from direct_agent.run."""
    text: str = "I made some improvements."
    error: str | None = None
    cost_usd: float = 0.01
    num_turns: int = 3
    tools_used: list = field(default_factory=list)


def _make_project(tmp_path: Path) -> Path:
    """Create a minimal project structure for testing improve()."""
    project = tmp_path / "project"
    (project / "src" / "secretary").mkdir(parents=True)
    (project / "src" / "secretary" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (project / "src" / "secretary" / "main.py").write_text("def hello():\n    return 'hi'\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_main.py").write_text("def test_hello():\n    pass\n")
    return project


def _make_config(tmp_path: Path, auto_promote: bool = False, keep_sandbox: bool = False) -> MagicMock:
    """Create a mock SecretaryConfig for improve() tests."""
    from secretary.config import SecretaryConfig, SelfImproveConfig, RoutingConfig, ModelTier
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.self_improve.auto_promote = auto_promote
    config.self_improve.test_timeout = 60
    config.self_improve.sandbox_dir = str(tmp_path / "sandbox")
    config.self_improve.keep_sandbox = keep_sandbox
    return config


@pytest.mark.asyncio
async def test_improve_agent_makes_changes_tests_pass(tmp_path: Path):
    """Full pipeline: agent modifies file → tests pass → not promoted (auto_promote=False)."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=False)

    async def fake_agent_run(**kwargs):
        """Simulate agent modifying a file in the sandbox."""
        # The tools registry workspace_root points to sandbox
        tools = kwargs.get("tools", {})
        # Find workspace root from config (sandbox)
        sandbox = Path(config.self_improve.sandbox_dir)
        if sandbox.exists():
            # Modify a file
            target = sandbox / "src" / "secretary" / "main.py"
            target.write_text("def hello():\n    return 'hello world'\n")
        return _FakeAgentResult(text="Updated hello() return value")

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", return_value=(True, "4 passed")), \
         patch("secretary.self_improve._run_tests_subset", return_value=(True, "pass")):

        result = await improve(
            task="Improve hello function",
            project_dir=project,
            config=config,
        )

    assert result.error is None
    assert result.tests_passed is True
    assert result.promoted is False  # auto_promote=False
    assert len(result.changed_files) > 0
    assert any("MOD:" in c for c in result.changed_files)
    assert result.cost_usd == 0.01
    assert result.num_turns == 3
    # Source should be unchanged (not promoted)
    assert (project / "src" / "secretary" / "main.py").read_text() == "def hello():\n    return 'hi'\n"


@pytest.mark.asyncio
async def test_improve_auto_promote_on_pass(tmp_path: Path):
    """With auto_promote=True and passing tests, changes are promoted to source."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=True)

    async def fake_agent_run(**kwargs):
        sandbox = Path(config.self_improve.sandbox_dir)
        if sandbox.exists():
            target = sandbox / "src" / "secretary" / "main.py"
            target.write_text("def hello():\n    return 'improved'\n")
        return _FakeAgentResult(text="Improved function")

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", return_value=(True, "4 passed")), \
         patch("secretary.self_improve._run_tests_subset", return_value=(True, "pass")), \
         patch("secretary.self_improve._git_commit_promoted", return_value="abc123"):

        result = await improve(
            task="Improve hello function",
            project_dir=project,
            config=config,
        )

    assert result.error is None
    assert result.tests_passed is True
    assert result.promoted is True
    assert result.backup_dir != ""
    # Source should now have the changes
    assert "improved" in (project / "src" / "secretary" / "main.py").read_text()


@pytest.mark.asyncio
async def test_improve_auto_promote_rollback_on_post_test_fail(tmp_path: Path):
    """Auto-promote rolls back if post-promote tests fail."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=True)

    async def fake_agent_run(**kwargs):
        sandbox = Path(config.self_improve.sandbox_dir)
        if sandbox.exists():
            target = sandbox / "src" / "secretary" / "main.py"
            target.write_text("def hello():\n    return 'broken'\n")
        return _FakeAgentResult(text="Made changes")

    call_count = 0

    async def fake_run_tests(test_dir, source, timeout):
        """First call (sandbox tests) passes, second call (post-promote) fails."""
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return True, "4 passed"
        else:
            return False, "1 FAILED — test_hello AssertionError"

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", side_effect=fake_run_tests), \
         patch("secretary.self_improve._run_tests_subset", return_value=(True, "pass")):

        result = await improve(
            task="Break hello function",
            project_dir=project,
            config=config,
        )

    assert result.promoted is False
    assert result.error is not None
    assert "auto-rolled back" in result.error
    # Source should be restored to original
    assert (project / "src" / "secretary" / "main.py").read_text() == "def hello():\n    return 'hi'\n"


@pytest.mark.asyncio
async def test_improve_git_commit_failure_rolls_back(tmp_path: Path):
    """If git commit fails after promotion, changes are rolled back."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=True)

    async def fake_agent_run(**kwargs):
        sandbox = Path(config.self_improve.sandbox_dir)
        if sandbox.exists():
            target = sandbox / "src" / "secretary" / "main.py"
            target.write_text("def hello():\n    return 'improved'\n")
        return _FakeAgentResult(text="Improved function")

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", return_value=(True, "4 passed")), \
         patch("secretary.self_improve._run_tests_subset", return_value=(True, "pass")), \
         patch("secretary.self_improve._git_commit_promoted", return_value=None):

        result = await improve(
            task="Improve hello function",
            project_dir=project,
            config=config,
        )

    assert result.promoted is False
    assert "git commit failed" in result.error
    # Source should be restored to original (rolled back)
    assert (project / "src" / "secretary" / "main.py").read_text() == "def hello():\n    return 'hi'\n"


@pytest.mark.asyncio
async def test_improve_agent_no_changes(tmp_path: Path):
    """Pipeline exits early if agent makes no changes."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path)

    async def fake_agent_run(**kwargs):
        # Agent does nothing to the sandbox
        return _FakeAgentResult(text="I reviewed but found nothing to change")

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        result = await improve(
            task="Find improvements",
            project_dir=project,
            config=config,
        )

    assert result.error is not None and result.error.startswith("Agent made no changes")
    assert result.changed_files == []
    assert result.tests_passed is False
    assert result.promoted is False


@pytest.mark.asyncio
async def test_improve_agent_returns_error(tmp_path: Path):
    """Pipeline exits early if agent returns an error."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path)

    async def fake_agent_run(**kwargs):
        return _FakeAgentResult(error="API error: 500 Internal Server Error")

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        result = await improve(
            task="Improve something",
            project_dir=project,
            config=config,
        )

    assert result.error == "API error: 500 Internal Server Error"
    assert result.tests_passed is False


@pytest.mark.asyncio
async def test_improve_tests_fail_no_promote(tmp_path: Path):
    """When sandbox tests fail, changes are not promoted even with auto_promote=True."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=True)

    async def fake_agent_run(**kwargs):
        sandbox = Path(config.self_improve.sandbox_dir)
        if sandbox.exists():
            target = sandbox / "src" / "secretary" / "main.py"
            target.write_text("def hello():\n    syntax error here\n")
        return _FakeAgentResult(text="Made changes")

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", return_value=(False, "ERRORS! SyntaxError")), \
         patch("secretary.self_improve._run_tests_subset", return_value=(False, "ERRORS! SyntaxError")):

        result = await improve(
            task="Break something",
            project_dir=project,
            config=config,
        )

    assert result.tests_passed is False
    assert result.promoted is False
    # Source should be unchanged
    assert (project / "src" / "secretary" / "main.py").read_text() == "def hello():\n    return 'hi'\n"


@pytest.mark.asyncio
async def test_improve_sandbox_cleanup(tmp_path: Path):
    """Sandbox is cleaned up after improve() unless keep_sandbox=True."""
    project = _make_project(tmp_path)
    sandbox_dir = tmp_path / "sandbox"
    config = _make_config(tmp_path, keep_sandbox=False)

    async def fake_agent_run(**kwargs):
        return _FakeAgentResult()

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        result = await improve(
            task="Do something",
            project_dir=project,
            config=config,
        )

    # Sandbox should be cleaned up
    assert not sandbox_dir.exists()


@pytest.mark.asyncio
async def test_improve_keep_sandbox(tmp_path: Path):
    """Sandbox is preserved when keep_sandbox=True."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, keep_sandbox=True)

    async def fake_agent_run(**kwargs):
        return _FakeAgentResult()

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        result = await improve(
            task="Do something",
            project_dir=project,
            config=config,
            keep_sandbox=True,
        )

    # Sandbox should still exist
    sandbox = Path(config.self_improve.sandbox_dir)
    assert sandbox.exists()


@pytest.mark.asyncio
async def test_improve_exception_in_agent(tmp_path: Path):
    """Exception during agent run is caught and stored in result.error."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path)

    async def exploding_agent(**kwargs):
        raise RuntimeError("Unexpected network failure")

    with patch("secretary.self_improve.direct_agent.run", side_effect=exploding_agent), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        result = await improve(
            task="Do something",
            project_dir=project,
            config=config,
        )

    assert result.error == "Unexpected network failure"
    assert result.tests_passed is False
    assert result.promoted is False


@pytest.mark.asyncio
async def test_improve_overrides_config_values(tmp_path: Path):
    """Override parameters take precedence over config values."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=False)

    async def fake_agent_run(**kwargs):
        sandbox = Path(config.self_improve.sandbox_dir)
        if sandbox.exists():
            (sandbox / "src" / "secretary" / "main.py").write_text("modified\n")
        return _FakeAgentResult()

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", return_value=(True, "passed")), \
         patch("secretary.self_improve._run_tests_subset", return_value=(True, "pass")), \
         patch("secretary.self_improve._git_commit_promoted", return_value="abc123"):

        # Override auto_promote to True even though config says False
        result = await improve(
            task="Do something",
            project_dir=project,
            config=config,
            auto_promote=True,
        )

    assert result.promoted is True


@pytest.mark.asyncio
async def test_improve_max_turns_passed_to_agent(tmp_path: Path):
    """max_turns parameter is forwarded to direct_agent.run."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path)

    captured_kwargs = {}

    async def capture_agent(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeAgentResult()

    with patch("secretary.self_improve.direct_agent.run", side_effect=capture_agent), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        await improve(
            task="Do something",
            project_dir=project,
            config=config,
            max_turns=5,
        )

    assert captured_kwargs.get("max_turns") == 5


@pytest.mark.asyncio
async def test_improve_logs_to_run_log(tmp_path: Path):
    """improve() appends a RunLog entry for analytics."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path)
    # Ensure data dir exists for run_log
    config.data_path.mkdir(parents=True, exist_ok=True)

    async def fake_agent_run(**kwargs):
        return _FakeAgentResult()

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        result = await improve(
            task="Log this improvement",
            project_dir=project,
            config=config,
        )

    # Check that run_log.jsonl was created
    log_path = config.data_path / "run_log.jsonl"
    assert log_path.exists()
    import json
    entries = [json.loads(line) for line in log_path.read_text().strip().split("\n") if line.strip()]
    assert len(entries) >= 1
    last = entries[-1]
    assert "[self-improve]" in last["task"]
    assert last["tier"] == "high"


@pytest.mark.asyncio
async def test_improve_default_sandbox_path(tmp_path: Path):
    """When sandbox_dir is empty, sandbox is created next to project as project_sandbox."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path)
    config.self_improve.sandbox_dir = ""  # auto-generate

    async def fake_agent_run(**kwargs):
        return _FakeAgentResult()

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        result = await improve(
            task="Auto sandbox path",
            project_dir=project,
            config=config,
        )

    # Sandbox dir should have been set to project_sandbox
    expected_sandbox = str(project.parent / f"{project.name}_sandbox")
    assert result.sandbox_dir == expected_sandbox


# ══════════════════════════════════════════════════════════════
#  Tests for change filtering and focused testing
# ══════════════════════════════════════════════════════════════


def test_filter_allowed_changes_keeps_src_secretary():
    changes = [
        "MOD: src/secretary/goals.py",
        "MOD: src/secretary/watcher.py",
    ]
    filtered = _filter_allowed_changes(changes)
    assert filtered == changes


def test_filter_allowed_changes_allows_test_files():
    """tests/ is now in allowed scope for long-horizon scaffolding."""
    changes = [
        "MOD: src/secretary/goals.py",
        "NEW: tests/test_new.py",
        "DEL: tests/test_agent.py",
    ]
    filtered = _filter_allowed_changes(changes)
    assert filtered == changes


def test_filter_allowed_changes_rejects_data_files():
    changes = [
        "MOD: src/secretary/goals.py",
        "MOD: data/memory.json",
        "MOD: data/watch_test.log",
        "NEW: cycle_output.txt",
    ]
    filtered = _filter_allowed_changes(changes)
    assert filtered == ["MOD: src/secretary/goals.py"]


def test_filter_allowed_changes_handles_backslash_paths():
    changes = [
        "MOD: src\\secretary\\goals.py",
        "MOD: data\\memory.json",
    ]
    filtered = _filter_allowed_changes(changes)
    assert len(filtered) == 1
    assert "goals.py" in filtered[0]


def test_filter_allowed_changes_empty():
    assert _filter_allowed_changes([]) == []


def test_map_changed_to_tests(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "tests").mkdir()
    (sandbox / "tests" / "test_goals.py").write_text("# test")
    (sandbox / "tests" / "test_watcher.py").write_text("# test")

    changes = ["MOD: src/secretary/goals.py", "MOD: src/secretary/watcher.py"]
    tests = _map_changed_to_tests(changes, sandbox)
    assert "tests/test_goals.py" in tests
    assert "tests/test_watcher.py" in tests


def test_map_changed_to_tests_no_matching_tests(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "tests").mkdir()

    changes = ["MOD: src/secretary/some_module.py"]
    tests = _map_changed_to_tests(changes, sandbox)
    assert tests == []


def test_map_changed_to_tests_deduplicates(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "tests").mkdir()
    (sandbox / "tests" / "test_goals.py").write_text("# test")

    changes = ["MOD: src/secretary/goals.py", "NEW: src/secretary/goals.py"]
    tests = _map_changed_to_tests(changes, sandbox)
    assert tests == ["tests/test_goals.py"]


@pytest.mark.asyncio
async def test_improve_filters_out_of_scope_changes(tmp_path):
    """Agent modifying data/ files should have those filtered out."""
    project = _make_project(tmp_path)

    async def fake_agent_run(**kwargs):
        # Agent modifies a src file AND a data file
        sandbox = Path(kwargs.get("tools", {}).get("__sandbox", str(project)))
        # We'll simulate by directly writing to sandbox after copy
        return _FakeAgentResult()

    config = _make_config(tmp_path)

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._detect_changes") as mock_detect, \
         patch("secretary.self_improve._run_tests", return_value=(True, "all pass")), \
         patch("secretary.self_improve._run_tests_subset", return_value=(True, "pass")):

        # Simulate agent changing data files + source files
        mock_detect.return_value = [
            "MOD: src/secretary/goals.py",
            "MOD: data/memory.json",
            "NEW: cycle_output.txt",
        ]

        result = await improve(
            task="Test filter",
            project_dir=project,
            config=config,
            auto_promote=False,
        )

    # Only src/secretary/ change should remain
    assert result.changed_files == ["MOD: src/secretary/goals.py"]


@pytest.mark.asyncio
async def test_improve_target_files_in_prompt(tmp_path):
    """When target_files is passed, it should appear in the agent prompt."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path)
    captured_task = {}

    async def fake_agent_run(**kwargs):
        captured_task["prompt"] = kwargs.get("task", "")
        return _FakeAgentResult()

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}):

        await improve(
            task="Make a change",
            project_dir=project,
            config=config,
            target_files=["src/secretary/goals.py", "src/secretary/watcher.py"],
        )

    assert "TARGET FILES" in captured_task["prompt"]
    assert "src/secretary/goals.py" in captured_task["prompt"]
    assert "src/secretary/watcher.py" in captured_task["prompt"]


# ══════════════════════════════════════════════════════════════
#  Git auto-commit after promotion
# ══════════════════════════════════════════════════════════════


def test_git_commit_promoted_creates_commit(tmp_path: Path):
    """_git_commit_promoted should git add + commit promoted files."""
    import subprocess

    project = tmp_path / "proj"
    project.mkdir()
    subprocess.run(["git", "init"], cwd=str(project), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(project), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(project), check=True, capture_output=True,
    )
    # Create initial commit
    (project / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(project), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(project), check=True, capture_output=True,
    )

    # Simulate a promoted change
    src_dir = project / "src" / "secretary"
    src_dir.mkdir(parents=True)
    (src_dir / "foo.py").write_text("# improved")

    changes = ["NEW: src/secretary/foo.py"]
    result = _git_commit_promoted(project, changes, "Add foo module")
    assert result is not None

    # Verify git log contains our commit
    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=str(project), capture_output=True, text=True,
    )
    assert "[self-improve]" in log.stdout


def test_git_commit_promoted_no_git_repo(tmp_path: Path):
    """_git_commit_promoted returns None gracefully if not a git repo."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "foo.py").write_text("# new")

    result = _git_commit_promoted(project, ["NEW: foo.py"], "Test task")
    assert result is None


# ══════════════════════════════════════════════════════════════
#  Promotion includes git commit in full pipeline
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_improve_promotion_calls_git_commit(tmp_path: Path):
    """When auto_promote=True and tests pass, git commit should be called."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=True)

    async def fake_agent_run(**kwargs):
        # Write to sandbox (same path as _make_config sets)
        sandbox = Path(config.self_improve.sandbox_dir)
        f = sandbox / "src" / "secretary" / "new_mod.py"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("# agent wrote this")
        return _FakeAgentResult()

    async def fake_tests(sandbox, source, timeout=120):
        return True, "all passed"

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", side_effect=fake_tests), \
         patch("secretary.self_improve._run_tests_subset", side_effect=fake_tests), \
         patch("secretary.self_improve._git_commit_promoted", return_value="abc1234") as mock_git:

        result = await improve(
            task="Add new module",
            project_dir=project,
            config=config,
            auto_promote=True,
        )

    assert result.promoted is True
    mock_git.assert_called_once()


# ─────────────────────────────────────────────────────────────
#  Test failure output captured in ImprovementResult
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_test_failure_output_captured_in_result(tmp_path: Path):
    """When sandbox tests fail, the full test output is stored in result.test_output."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=True)

    async def fake_agent_run(**kwargs):
        sandbox = Path(config.self_improve.sandbox_dir)
        f = sandbox / "src" / "secretary" / "main.py"
        f.write_text(f.read_text() + "\n# modified")
        return _FakeAgentResult()

    _failure_output = "FAILED tests/test_main.py::test_foo - AssertionError: expected 1 got 2"

    async def fake_tests_fail(*args, **kwargs):
        return False, _failure_output

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", side_effect=fake_tests_fail), \
         patch("secretary.self_improve._run_tests_subset", side_effect=fake_tests_fail):

        result = await improve(
            task="Break something",
            project_dir=project,
            config=config,
            auto_promote=True,
        )

    assert result.tests_passed is False
    assert result.promoted is False
    assert "Sandbox tests failed after code changes" in result.error
    assert "AssertionError" in result.test_output
    assert "FAILED" in result.test_output


@pytest.mark.asyncio
async def test_test_pass_output_also_captured(tmp_path: Path):
    """When tests pass, the test output is still stored in result.test_output."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=False)

    async def fake_agent_run(**kwargs):
        sandbox = Path(config.self_improve.sandbox_dir)
        f = sandbox / "src" / "secretary" / "main.py"
        f.write_text(f.read_text() + "\n# modified")
        return _FakeAgentResult()

    _pass_output = "3 passed in 0.5s"

    async def fake_tests_pass(*args, **kwargs):
        return True, _pass_output

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests", side_effect=fake_tests_pass), \
         patch("secretary.self_improve._run_tests_subset", side_effect=fake_tests_pass):

        result = await improve(
            task="Improve something",
            project_dir=project,
            config=config,
            auto_promote=False,
        )

    assert result.tests_passed is True
    assert "3 passed" in result.test_output


# ══════════════════════════════════════════════════════════════
#  JSON parse fallback tests
# ══════════════════════════════════════════════════════════════


def test_parse_json_response_clean():
    """Normal JSON parses correctly."""
    result = _parse_json_response('{"proposals": [], "analysis_summary": "ok"}')
    assert result["analysis_summary"] == "ok"


def test_parse_json_response_markdown_fences():
    """JSON wrapped in markdown code fences."""
    text = '```json\n{"proposals": [{"x": 1}]}\n```'
    result = _parse_json_response(text)
    assert len(result["proposals"]) == 1


def test_parse_json_response_trailing_text():
    """JSON followed by trailing LLM commentary."""
    text = '{"proposals": []} I hope this helps!'
    result = _parse_json_response(text)
    assert result["proposals"] == []


def test_parse_json_response_truncated_string():
    """Truncated JSON with unclosed string — falls back to brace-depth."""
    text = '{"proposals": [], "summary": "ok"}\n{"broken": "no close'
    result = _parse_json_response(text)
    assert result["proposals"] == []


def test_parse_json_response_no_json():
    """No JSON at all raises JSONDecodeError."""
    import json
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response("This has no JSON at all.")


# ══════════════════════════════════════════════════════════════
#  _extract_failure_summary
# ══════════════════════════════════════════════════════════════


def test_extract_failure_summary_extracts_failed_tests():
    output = (
        "tests/test_foo.py::test_one PASSED\n"
        "tests/test_foo.py::test_two FAILED\n"
        "FAILED tests/test_foo.py::test_two - AssertionError\n"
        "FAILED tests/test_bar.py::test_three - KeyError\n"
        "=== 1 passed, 2 failed ===\n"
    )
    summary = _extract_failure_summary(output)
    assert "FAILING TESTS:" in summary
    assert "FAILED tests/test_foo.py::test_two" in summary
    assert "FAILED tests/test_bar.py::test_three" in summary


def test_extract_failure_summary_extracts_e_lines():
    output = (
        "E       assert 42 == 43\n"
        "E       KeyError: 'missing_key'\n"
        "some other line\n"
    )
    summary = _extract_failure_summary(output)
    assert "KEY ERRORS:" in summary
    assert "assert 42 == 43" in summary
    assert "KeyError: 'missing_key'" in summary


def test_extract_failure_summary_always_has_raw_tail():
    output = "just some output without structured failures\n" * 5
    summary = _extract_failure_summary(output)
    assert "RAW OUTPUT (last 1000 chars):" in summary


# ══════════════════════════════════════════════════════════════
#  Pre-test baseline (step 2b)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_improve_baseline_failure_aborts_early(tmp_path: Path):
    """If target-related tests already fail before agent runs, improve() aborts."""
    project = _make_project(tmp_path)
    config = _make_config(tmp_path, auto_promote=False)

    agent_called = False

    async def fake_agent_run(**kwargs):
        nonlocal agent_called
        agent_called = True
        return _FakeAgentResult(text="Should never run")

    async def fake_run_tests_subset(sandbox, proj, test_files, timeout):
        return (False, "FAILED tests/test_main.py::test_hello - AssertionError")

    with patch("secretary.self_improve.direct_agent.run", side_effect=fake_agent_run), \
         patch("secretary.self_improve.build_tool_registry", return_value={}), \
         patch("secretary.self_improve._run_tests_subset", side_effect=fake_run_tests_subset), \
         patch("secretary.self_improve._run_tests", return_value=(True, "ok")):

        result = await improve(
            task="Improve hello function",
            project_dir=project,
            config=config,
            target_files=["src/secretary/main.py"],
        )

    assert result.error is not None
    assert "Pre-test baseline FAILED" in result.error
    assert not agent_called, "Agent should NOT run when baseline fails"


# ══════════════════════════════════════════════════════════════
#  _generate_change_plan — test-file context
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_generate_change_plan_includes_test_context(tmp_path: Path):
    """Change planner sends relevant test file content to LLM."""
    project = tmp_path / "project"
    (project / "src" / "secretary").mkdir(parents=True)
    (project / "src" / "secretary" / "watcher.py").write_text("def watch(): pass\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_watcher.py").write_text(
        "def test_watch_cycle():\n    assert True\n"
    )

    captured_prompt = {}

    class FakeContent:
        def __init__(self):
            self.text = '{"file":"src/secretary/watcher.py","function":"watch","description":"add logging","before":"def watch(): pass","after":"def watch():\\n    import logging\\n    logging.info(\\"watching\\")\\n    pass"}'

    class FakeResponse:
        content = [FakeContent()]

    async def capture_create(**kwargs):
        captured_prompt["messages"] = kwargs.get("messages", [])
        return FakeResponse()

    mock_client = MagicMock()
    mock_client.messages.create = capture_create

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        from secretary.config import SecretaryConfig
        config = SecretaryConfig(data_root=str(tmp_path / "data"))
        result = await _generate_change_plan(
            task="Add logging to watch",
            target_files=["src/secretary/watcher.py"],
            project=project,
            config=config,
        )

    assert result is not None
    # Verify the prompt included test file context
    user_msg = captured_prompt["messages"][-1]["content"]
    assert "test_watcher.py" in user_msg
    assert "test_watch_cycle" in user_msg
    assert "DO NOT BREAK" in user_msg


@pytest.mark.asyncio
async def test_generate_change_plan_no_test_file_still_works(tmp_path: Path):
    """Change planner works even when no matching test file exists."""
    project = tmp_path / "project"
    (project / "src" / "secretary").mkdir(parents=True)
    (project / "src" / "secretary" / "utils.py").write_text("x = 1\n")

    class FakeContent:
        def __init__(self):
            self.text = '{"file":"src/secretary/utils.py","function":"x","description":"fix","before":"x = 1","after":"x = 2"}'

    class FakeResponse:
        content = [FakeContent()]

    async def fake_create(**kwargs):
        return FakeResponse()

    mock_client = MagicMock()
    mock_client.messages.create = fake_create

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        from secretary.config import SecretaryConfig
        config = SecretaryConfig(data_root=str(tmp_path / "data"))
        result = await _generate_change_plan(
            task="Fix utils",
            target_files=["src/secretary/utils.py"],
            project=project,
            config=config,
        )

    # Should still return a plan even without test context
    assert result is not None
    assert "utils.py" in result


# ══════════════════════════════════════════════════════════════
#  Retry loop — _MAX_FOCUSED_RETRIES and _retry_with_failure_context
# ══════════════════════════════════════════════════════════════


def test_max_focused_retries_is_two():
    """Constant _MAX_FOCUSED_RETRIES should be 2."""
    assert _MAX_FOCUSED_RETRIES == 2


@pytest.mark.asyncio
async def test_retry_with_failure_context_attempt_warning(tmp_path: Path):
    """Retry attempt > 1 includes a WARNING preamble in the agent prompt."""
    sandbox = tmp_path / "sandbox"
    (sandbox / "src" / "secretary").mkdir(parents=True)
    project = tmp_path / "project"
    (project / "src" / "secretary").mkdir(parents=True)
    # Create a file so _detect_changes finds a diff
    (sandbox / "src" / "secretary" / "foo.py").write_text("fixed = True\n")
    (project / "src" / "secretary" / "foo.py").write_text("fixed = False\n")

    captured_task = {}

    @dataclass
    class FakeRunResult:
        error: str | None = None
        num_turns: int = 1

    async def fake_run(task, **kwargs):
        captured_task["prompt"] = task
        return FakeRunResult()

    from secretary.config import SecretaryConfig
    config = SecretaryConfig(data_root=str(tmp_path / "data"))

    with patch("secretary.direct_agent.run", side_effect=fake_run):
        # attempt=1 should NOT have warning
        await _retry_with_failure_context(
            sandbox, project, config, "Fix the bug",
            None, "FAILED test_foo", ["src/secretary/foo.py"],
            "medium", ["file_read"], 5, 300, attempt=1,
        )
        assert "WARNING" not in captured_task["prompt"]

        # attempt=2 SHOULD have warning
        await _retry_with_failure_context(
            sandbox, project, config, "Fix the bug",
            None, "FAILED test_foo", ["src/secretary/foo.py"],
            "medium", ["file_read"], 5, 300, attempt=2,
        )
        assert "WARNING" in captured_task["prompt"]
        assert "retry attempt 2" in captured_task["prompt"]
        assert "MORE careful" in captured_task["prompt"]


# ---------- prefix-based test mapping ----------


def test_map_changed_to_tests_prefix_match(tmp_path):
    """_map_changed_to_tests picks up test_foo_*.py variants, not just test_foo.py."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    tests_dir = sandbox / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_oracle.py").write_text("# base")
    (tests_dir / "test_oracle_voting.py").write_text("# variant")
    (tests_dir / "test_oracle_ensemble.py").write_text("# variant2")
    (tests_dir / "test_other.py").write_text("# unrelated")

    changes = ["MOD: src/secretary/oracle.py"]
    tests = _map_changed_to_tests(changes, sandbox)
    assert "tests/test_oracle.py" in tests
    assert "tests/test_oracle_voting.py" in tests
    assert "tests/test_oracle_ensemble.py" in tests
    assert "tests/test_other.py" not in tests


def test_map_changed_to_tests_no_tests_dir(tmp_path):
    """Returns empty list when tests/ directory doesn't exist."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    changes = ["MOD: src/secretary/foo.py"]
    assert _map_changed_to_tests(changes, sandbox) == []


# ---------- test context deduplication in planner ----------


@pytest.mark.asyncio
async def test_generate_change_plan_deduplicates_test_context(tmp_path: Path):
    """Planner includes each test file only once even when multiple targets map to it."""
    project = tmp_path / "project"
    (project / "src" / "secretary").mkdir(parents=True)
    (project / "src" / "secretary" / "oracle.py").write_text("def vote(): pass\n")
    (project / "src" / "secretary" / "oracle_voting.py").write_text("def tally(): pass\n")
    (project / "tests").mkdir()
    # Both targets share the same test file stem prefix
    (project / "tests" / "test_oracle.py").write_text(
        "def test_vote():\n    assert True\n"
    )
    (project / "tests" / "test_oracle_voting.py").write_text(
        "def test_tally():\n    assert True\n"
    )

    captured_prompt: dict = {}

    class FakeContent:
        text = '{"file":"src/secretary/oracle.py","function":"vote","description":"fix","before":"def vote(): pass","after":"def vote():\\n    return 1"}'

    class FakeResponse:
        content = [FakeContent()]

    async def capture_create(**kwargs):
        captured_prompt["messages"] = kwargs.get("messages", [])
        return FakeResponse()

    mock_client = MagicMock()
    mock_client.messages.create = capture_create

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        from secretary.config import SecretaryConfig
        config = SecretaryConfig(data_root=str(tmp_path / "data"))
        await _generate_change_plan(
            task="Fix oracle voting",
            target_files=["src/secretary/oracle.py", "src/secretary/oracle_voting.py"],
            project=project,
            config=config,
        )

    user_msg = captured_prompt["messages"][-1]["content"]
    # test_oracle.py should appear exactly once (via oracle.py target)
    assert user_msg.count("### tests/test_oracle.py") == 1
    # test_oracle_voting.py should appear exactly once (not duplicated)
    assert user_msg.count("### tests/test_oracle_voting.py") == 1


@pytest.mark.asyncio
async def test_generate_change_plan_prefix_discovers_extra_tests(tmp_path: Path):
    """Planner discovers test_foo_bar.py when given foo.py as target."""
    project = tmp_path / "project"
    (project / "src" / "secretary").mkdir(parents=True)
    (project / "src" / "secretary" / "oracle.py").write_text("def vote(): pass\n")
    (project / "tests").mkdir()
    (project / "tests" / "test_oracle.py").write_text("def test_base(): pass\n")
    (project / "tests" / "test_oracle_ensemble.py").write_text(
        "def test_ensemble(): pass\n"
    )

    captured_prompt: dict = {}

    class FakeContent:
        text = '{"file":"src/secretary/oracle.py","function":"vote","description":"fix","before":"def vote(): pass","after":"def vote():\\n    return 1"}'

    class FakeResponse:
        content = [FakeContent()]

    async def capture_create(**kwargs):
        captured_prompt["messages"] = kwargs.get("messages", [])
        return FakeResponse()

    mock_client = MagicMock()
    mock_client.messages.create = capture_create

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        from secretary.config import SecretaryConfig
        config = SecretaryConfig(data_root=str(tmp_path / "data"))
        await _generate_change_plan(
            task="Fix oracle",
            target_files=["src/secretary/oracle.py"],
            project=project,
            config=config,
        )

    user_msg = captured_prompt["messages"][-1]["content"]
    assert "test_oracle.py" in user_msg
    assert "test_oracle_ensemble.py" in user_msg
