"""Tests for the self-improve ex-ante promotion guards.

These pin down the behaviour added in commit 703c0e9, which closes the
sandbox-green/master-red blind spot preemptively (complementing the ex-post
empirical feedback loop from proposal_outcomes.py in commit 4925e54).

Two guards are tested:
    1. ``_public_symbols`` — AST-based extraction of public top-level names.
    2. ``_check_public_symbols_preserved`` — blocks promotions that remove
       public symbols still referenced elsewhere in the sandbox tree.
       This is the exact failure mode from commit ce11f1e, where
       ``goal_harness.py`` was gutted from ~500 to ~90 lines, removing
       ``run_harness_test`` / ``validate_harness`` / ``HarnessResult`` etc.
       while ``watcher.py`` still imported them.
"""
from __future__ import annotations

from pathlib import Path

from secretary.self_improve import (
    _check_public_symbols_preserved,
    _public_symbols,
)


# ── _public_symbols ────────────────────────────────────────────────────────


def test_public_symbols_extracts_functions_classes_and_assignments() -> None:
    text = (
        "import os\n"
        "_PRIVATE = 1\n"
        "PUBLIC = 2\n"
        "def _helper():\n    pass\n"
        "def exposed():\n    pass\n"
        "class Foo:\n    pass\n"
        "class _Bar:\n    pass\n"
    )
    assert _public_symbols(text) == {"PUBLIC", "exposed", "Foo"}


def test_public_symbols_handles_async_functions() -> None:
    text = "async def fetch():\n    pass\nasync def _private():\n    pass\n"
    assert _public_symbols(text) == {"fetch"}


def test_public_symbols_handles_annotated_assignments() -> None:
    text = "COUNT: int = 5\n_HIDDEN: int = 9\n"
    assert _public_symbols(text) == {"COUNT"}


def test_public_symbols_tolerates_syntax_errors() -> None:
    # Must not raise — guard should never crash the pipeline.
    assert _public_symbols("def bad(:") == set()


def test_public_symbols_ignores_nested_definitions() -> None:
    text = (
        "def outer():\n"
        "    def inner():\n"
        "        pass\n"
        "    INNER_CONST = 1\n"
    )
    # Only ``outer`` is top-level public.
    assert _public_symbols(text) == {"outer"}


# ── _check_public_symbols_preserved ────────────────────────────────────────


def _make_tree(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_guard_blocks_removed_symbol_still_imported_by_sibling(tmp_path: Path) -> None:
    """Reproduction of the ce11f1e failure mode.

    ``goal_harness.py`` is gutted in the sandbox — ``run_harness_test`` is
    removed — while ``watcher.py`` still imports it. The guard must block.
    """
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"

    _make_tree(source, {
        "src/secretary/goal_harness.py":
            "def run_harness_test():\n    pass\n"
            "def validate_harness():\n    pass\n"
            "CONSTANT = 1\n",
        "src/secretary/watcher.py":
            "from secretary.goal_harness import run_harness_test, CONSTANT\n",
    })
    _make_tree(sandbox, {
        "src/secretary/goal_harness.py":
            "def validate_harness():\n    pass\n"
            "CONSTANT = 1\n",  # run_harness_test REMOVED
        "src/secretary/watcher.py":
            "from secretary.goal_harness import run_harness_test, CONSTANT\n",
    })

    ok, msg = _check_public_symbols_preserved(
        source, sandbox, ["MOD: src/secretary/goal_harness.py"],
    )
    assert ok is False
    assert "run_harness_test" in msg
    assert "watcher.py" in msg


def test_guard_allows_additions_only(tmp_path: Path) -> None:
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    _make_tree(source, {
        "src/secretary/m.py": "def a():\n    pass\n",
    })
    _make_tree(sandbox, {
        "src/secretary/m.py": "def a():\n    pass\ndef b():\n    pass\n",
    })
    ok, _msg = _check_public_symbols_preserved(
        source, sandbox, ["MOD: src/secretary/m.py"],
    )
    assert ok is True


def test_guard_allows_removing_unreferenced_symbols(tmp_path: Path) -> None:
    """If a removed public symbol has NO references anywhere in the sandbox,
    the guard lets the promotion through. Over-strict rejection would block
    legitimate cleanup.
    """
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    _make_tree(source, {
        "src/secretary/m.py": "def keep():\n    pass\ndef dead():\n    pass\n",
        "src/secretary/other.py": "from secretary.m import keep\n",
    })
    _make_tree(sandbox, {
        "src/secretary/m.py": "def keep():\n    pass\n",  # dead REMOVED
        "src/secretary/other.py": "from secretary.m import keep\n",
    })
    ok, _msg = _check_public_symbols_preserved(
        source, sandbox, ["MOD: src/secretary/m.py"],
    )
    assert ok is True


def test_guard_detects_attribute_access_usage(tmp_path: Path) -> None:
    """Usage via ``module.Symbol`` (not just ``from`` import) must also block."""
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    _make_tree(source, {
        "src/secretary/m.py": "def used():\n    pass\n",
        "src/secretary/caller.py": "from secretary import m\nm.used()\n",
    })
    _make_tree(sandbox, {
        "src/secretary/m.py": "pass\n",  # used REMOVED
        "src/secretary/caller.py": "from secretary import m\nm.used()\n",
    })
    ok, msg = _check_public_symbols_preserved(
        source, sandbox, ["MOD: src/secretary/m.py"],
    )
    assert ok is False
    assert "used" in msg


def test_guard_detects_usage_in_tests_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    _make_tree(source, {
        "src/secretary/m.py": "def api():\n    pass\n",
        "tests/test_m.py": "from secretary.m import api\n",
    })
    _make_tree(sandbox, {
        "src/secretary/m.py": "pass\n",
        "tests/test_m.py": "from secretary.m import api\n",
    })
    ok, msg = _check_public_symbols_preserved(
        source, sandbox, ["MOD: src/secretary/m.py"],
    )
    assert ok is False
    assert "test_m.py" in msg


def test_guard_only_applies_to_src_secretary(tmp_path: Path) -> None:
    """Changes to test files, docs, or config should not trigger the guard."""
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    _make_tree(source, {
        "tests/test_something.py": "def test_a():\n    pass\n",
    })
    _make_tree(sandbox, {
        "tests/test_something.py": "def test_b():\n    pass\n",  # test_a removed
    })
    ok, _msg = _check_public_symbols_preserved(
        source, sandbox, ["MOD: tests/test_something.py"],
    )
    assert ok is True  # guard scoped to src/secretary/*.py


def test_guard_ignores_new_files(tmp_path: Path) -> None:
    """NEW files have no old version to diff against — no regression possible."""
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    _make_tree(source, {})
    _make_tree(sandbox, {
        "src/secretary/new_mod.py": "def fresh():\n    pass\n",
    })
    ok, _msg = _check_public_symbols_preserved(
        source, sandbox, ["NEW: src/secretary/new_mod.py"],
    )
    assert ok is True


def test_guard_ignores_non_python_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    _make_tree(source, {"src/secretary/data.json": '{"a": 1}\n'})
    _make_tree(sandbox, {"src/secretary/data.json": '{"a": 2}\n'})
    ok, _msg = _check_public_symbols_preserved(
        source, sandbox, ["MOD: src/secretary/data.json"],
    )
    assert ok is True


def test_guard_empty_changes_list(tmp_path: Path) -> None:
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    source.mkdir()
    sandbox.mkdir()
    ok, msg = _check_public_symbols_preserved(source, sandbox, [])
    assert ok is True
    assert "preserved" in msg.lower()
