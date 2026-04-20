"""Tests for the self-improve dead-binding filter.

Guards against the 2026-04-20 cargo-cult regression where self-improve
agents promote ``_FLAG = True`` module constants with ``"\u2705 ACTIVE"`` comments
but zero call-sites.  See ``shared/snags/general.md``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from secretary.self_improve import (
    _find_dead_literal_bindings,
    _reject_dead_binding_changes,
    _rhs_is_pure_literal,
    _top_level_literal_names,
)


# ── Pure-literal detection ─────────────────────────────────────


def _parse_value(src: str):
    import ast
    tree = ast.parse(src)
    assign = tree.body[0]
    assert hasattr(assign, "value")
    return assign.value  # type: ignore[attr-defined]


@pytest.mark.parametrize("expr", [
    "x = True",
    "x = False",
    "x = None",
    "x = 42",
    "x = 3.14",
    "x = -1",
    "x = 'hello'",
    "x = [1, 2, 3]",
    "x = (1, 2, 3)",
    "x = {'a': 1, 'b': 2}",
    "x = {1, 2, 3}",
    "x = 'f-string no expr'",
])
def test_rhs_is_pure_literal_true(expr: str) -> None:
    assert _rhs_is_pure_literal(_parse_value(expr)) is True


@pytest.mark.parametrize("expr", [
    "x = func()",
    "x = obj.attr",
    "x = obj[0]",
    "x = other_name",
    "x = [func()]",
    "x = {'k': func()}",
])
def test_rhs_is_pure_literal_false(expr: str) -> None:
    assert _rhs_is_pure_literal(_parse_value(expr)) is False


def test_top_level_literal_names_skips_dunders_and_calls() -> None:
    src = (
        "__version__ = '1.0'\n"
        "_FLAG = True\n"
        "_COUNT: int = 5\n"
        "_LOG = logging.getLogger(__name__)\n"
        "def f():\n    _LOCAL = True\n    return _LOCAL\n"
    )
    names = _top_level_literal_names(src)
    assert set(names) == {"_FLAG", "_COUNT"}


def test_top_level_literal_names_handles_syntax_error() -> None:
    assert _top_level_literal_names("def broken(:\n") == {}


# ── End-to-end filter behaviour ────────────────────────────────


@pytest.fixture
def sandbox_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Build a source/sandbox tree pair with secretary package + tests dirs."""
    source = tmp_path / "source"
    sandbox = tmp_path / "sandbox"
    for root in (source, sandbox):
        (root / "src" / "secretary").mkdir(parents=True)
        (root / "tests").mkdir(parents=True)
    return source, sandbox


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_rejects_pure_dead_flag_addition(sandbox_pair: tuple[Path, Path]) -> None:
    source, sandbox = sandbox_pair
    _write(source, "src/secretary/mod.py", "def foo():\n    return 1\n")
    _write(
        sandbox, "src/secretary/mod.py",
        "def foo():\n    return 1\n\n# Safety guard\n_EMAIL_VALIDATION = True\n_MAX_RETRIES = 5\n",
    )
    changes = ["MOD: src/secretary/mod.py"]
    dead = _find_dead_literal_bindings(changes, source, sandbox)
    assert dead == {"src/secretary/mod.py": ["_EMAIL_VALIDATION", "_MAX_RETRIES"]}
    assert _reject_dead_binding_changes(changes, source, sandbox) == []


def test_allows_flag_with_call_site(sandbox_pair: tuple[Path, Path]) -> None:
    source, sandbox = sandbox_pair
    _write(source, "src/secretary/mod.py", "def foo():\n    return 1\n")
    _write(
        sandbox, "src/secretary/mod.py",
        "_THRESHOLD = 10\n\ndef foo():\n    return _THRESHOLD * 2\n",
    )
    changes = ["MOD: src/secretary/mod.py"]
    assert _find_dead_literal_bindings(changes, source, sandbox) == {}
    assert _reject_dead_binding_changes(changes, source, sandbox) == changes


def test_keeps_mixed_real_plus_dead(sandbox_pair: tuple[Path, Path]) -> None:
    source, sandbox = sandbox_pair
    _write(source, "src/secretary/mod.py", "def foo():\n    return 1\n")
    _write(
        sandbox, "src/secretary/mod.py",
        "_THRESHOLD = 10\n_DEAD = False\n\n"
        "def foo():\n    return _THRESHOLD * 2\n\n"
        "def bar():\n    # substantive new logic\n    for i in range(_THRESHOLD):\n        print(i)\n",
    )
    changes = ["MOD: src/secretary/mod.py"]
    dead = _find_dead_literal_bindings(changes, source, sandbox)
    assert dead == {"src/secretary/mod.py": ["_DEAD"]}
    # Mixed content: kept, warning logged
    assert _reject_dead_binding_changes(changes, source, sandbox) == changes


def test_allows_flag_referenced_from_another_file(sandbox_pair: tuple[Path, Path]) -> None:
    source, sandbox = sandbox_pair
    _write(source, "src/secretary/mod.py", "def foo():\n    return 1\n")
    _write(sandbox, "src/secretary/mod.py", "_SHARED = 99\n\ndef foo():\n    return 1\n")
    # Consumer file exists in sandbox and references _SHARED
    _write(
        sandbox, "src/secretary/consumer.py",
        "from . import mod\n\ndef use():\n    return mod._SHARED\n",
    )
    changes = ["MOD: src/secretary/mod.py"]
    # _SHARED is referenced in consumer.py → not dead
    assert _find_dead_literal_bindings(changes, source, sandbox) == {}


def test_ignores_non_python_changes(sandbox_pair: tuple[Path, Path]) -> None:
    source, sandbox = sandbox_pair
    _write(source, "goals.yaml", "x: 1\n")
    _write(sandbox, "goals.yaml", "x: 1\ny: 2\n")
    changes = ["MOD: goals.yaml"]
    assert _find_dead_literal_bindings(changes, source, sandbox) == {}
    assert _reject_dead_binding_changes(changes, source, sandbox) == changes


def test_new_file_with_all_dead_flags_rejected(sandbox_pair: tuple[Path, Path]) -> None:
    source, sandbox = sandbox_pair
    _write(
        sandbox, "src/secretary/flags.py",
        "# Feature flags\n_ENABLE_A = True\n_ENABLE_B = True\n_ENABLE_C = True\n",
    )
    changes = ["NEW: src/secretary/flags.py"]
    dead = _find_dead_literal_bindings(changes, source, sandbox)
    assert dead == {"src/secretary/flags.py": ["_ENABLE_A", "_ENABLE_B", "_ENABLE_C"]}
    assert _reject_dead_binding_changes(changes, source, sandbox) == []
