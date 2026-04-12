"""Task Executor — pre-execution scope validator for file_edit calls.

Implements the 'read-first, scope-check, then-edit' pattern.

Before any file_edit tool call is dispatched, ``validate_scope()`` inspects
every pending tool call and raises ``ScopeViolationError`` for any file_edit
whose target path falls outside the two allowed write directories:

    src/secretary/   — production source code
    tests/           — unit tests

``execute_task()`` is the single entry-point callers should use: it calls
``validate_scope()`` **before** dispatching any tool, so a violated batch is
rejected atomically (no partial execution).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("secretary.task_executor")

# ── Allowed write scope ──────────────────────────────────────────────────────
ALLOWED_WRITE_PREFIXES: tuple[str, ...] = (
    "src/secretary/",
    "tests/",
)


# ── Errors ───────────────────────────────────────────────────────────────────

class ScopeViolationError(Exception):
    """Raised when a file_edit targets a path outside the allowed scope.

    Attributes
    ----------
    path : str
        The offending file path extracted from the tool call.
    allowed : tuple[str, ...]
        The allowed directory prefixes at the time of the check.
    """

    def __init__(
        self,
        path: str,
        allowed: tuple[str, ...] = ALLOWED_WRITE_PREFIXES,
    ) -> None:
        self.path = path
        self.allowed = allowed
        allowed_str = ", ".join(allowed)
        super().__init__(
            f"Scope violation: file_edit target '{path}' is outside allowed "
            f"directories [{allowed_str}]. Refusing to execute."
        )


# ── Internal helpers ─────────────────────────────────────────────────────────

def _normalize_path(path: str) -> str:
    """Normalize path separators to forward slashes and strip a leading ./"""
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_allowed_path(path: str) -> bool:
    """Return ``True`` iff *path* is under an allowed write directory."""
    normalized = _normalize_path(path)
    return any(normalized.startswith(prefix) for prefix in ALLOWED_WRITE_PREFIXES)


def _extract_file_edit_paths(tool_calls: list[dict[str, Any]]) -> list[str]:
    """Return the ``path`` argument of every ``file_edit`` call in *tool_calls*."""
    paths: list[str] = []
    for call in tool_calls:
        if isinstance(call, dict) and call.get("name") == "file_edit":
            inp = call.get("input") or {}
            if "path" in inp:
                paths.append(str(inp["path"]))
    return paths


# ── Public API ────────────────────────────────────────────────────────────────

def validate_scope(tool_calls: list[dict[str, Any]]) -> None:
    """Validate that every ``file_edit`` in *tool_calls* targets an allowed path.

    Iterates through all tool calls and raises ``ScopeViolationError`` on the
    **first** ``file_edit`` whose target path is not under one of
    ``ALLOWED_WRITE_PREFIXES``.  Non-``file_edit`` calls (reads, searches,
    run_command, etc.) are always permitted.

    Parameters
    ----------
    tool_calls:
        List of tool-call dicts, each with ``"name"`` and ``"input"`` keys.
        This is the batch the agent intends to execute next.

    Raises
    ------
    ScopeViolationError
        If any ``file_edit`` targets a path outside ``ALLOWED_WRITE_PREFIXES``.

    Examples
    --------
    >>> validate_scope([{"name": "file_edit", "input": {"path": "src/secretary/foo.py", ...}}])
    # passes silently

    >>> validate_scope([{"name": "file_edit", "input": {"path": "config.yaml", ...}}])
    # raises ScopeViolationError("config.yaml")
    """
    edit_paths = _extract_file_edit_paths(tool_calls)
    for path in edit_paths:
        if not _is_allowed_path(path):
            raise ScopeViolationError(path)
    log.debug(
        "validate_scope: %d tool call(s), %d file_edit(s) — all within scope",
        len(tool_calls),
        len(edit_paths),
    )


def execute_task(
    tool_calls: list[dict[str, Any]],
    executor_fn: Any,
) -> list[Any]:
    """Execute *tool_calls* after pre-flight scope validation.

    Calls ``validate_scope()`` **before** dispatching any tool.  If the scope
    check fails the ``ScopeViolationError`` propagates immediately and
    *executor_fn* is never called — the batch is rejected atomically.

    Parameters
    ----------
    tool_calls:
        Ordered list of tool-call dicts to execute.
    executor_fn:
        ``callable(tool_call) -> result`` — the actual tool dispatcher
        (e.g. ``DirectAgent._run_tool``).

    Returns
    -------
    list[Any]
        Execution results in the same order as *tool_calls*.

    Raises
    ------
    ScopeViolationError
        If any ``file_edit`` in *tool_calls* targets an out-of-scope path.
        No tools are executed when this is raised.
    """
    # ── SCOPE CHECK (must pass before any tool runs) ─────────────────────────
    validate_scope(tool_calls)

    # ── Dispatch ─────────────────────────────────────────────────────────────
    results: list[Any] = []
    for call in tool_calls:
        results.append(executor_fn(call))
    return results
