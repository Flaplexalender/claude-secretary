"""Tool Policy — restrict which tools are available per task source.

Layer 17 of the planning architecture.  Goal-generated tasks are LLM-authored
and should not have unrestricted access to write tools by default.

Policy levels (cumulative):
  read-only  — observation tools only (search, read, list)
  supervised — adds local-write tools (file_write, file_edit, gmail_draft)
  full       — all tools including send/create (gmail_send, calendar_create)

Research basis:
- Anthropic: "extensive testing in sandboxed environments" before go-live
- MAST: task verification failures are a top category in multi-agent systems
- Layer 16 guardrails validate task *structure*; Layer 17 restricts *capabilities*
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("secretary.tool_policy")

# ── Tool classifications ────────────────────────────────────
# Every tool in the registry must appear in exactly one set.

READ_ONLY_TOOLS = frozenset({
    "gmail_search",
    "gmail_read",
    "gmail_list_drafts",
    "gmail_get_draft",
    "calendar_today",
    "calendar_list",
    "calendar_search",
    "file_read",
    "file_list",
    "grep_search",
})

WRITE_LOCAL_TOOLS = frozenset({
    "gmail_draft",
    "file_write",
    "file_edit",
    "run_command",
    "run_python",
})

SEND_TOOLS = frozenset({
    "gmail_send",
    "calendar_create",
})

# Policy levels → cumulative tool sets
POLICY_TOOLS: dict[str, frozenset[str]] = {
    "read-only": READ_ONLY_TOOLS,
    "supervised": READ_ONLY_TOOLS | WRITE_LOCAL_TOOLS,
    "full": READ_ONLY_TOOLS | WRITE_LOCAL_TOOLS | SEND_TOOLS,
}

VALID_POLICIES = frozenset(POLICY_TOOLS.keys())

# ── File path validation ────────────────────────────────────

ALLOWED_WRITE_DIRS = frozenset({
    "src/secretary/",
    "tests/",
    "campaigns/",
    "docs/",
})

# goals.yaml is allowed as a special case (goal authoring)
ALLOWED_WRITE_FILES = frozenset({
    "goals.yaml",
})

FORBIDDEN_PATTERNS = frozenset({
    "data/",
    "_tmp_",
    "_temp_",
    ".tmp",
    "config.yaml",
    ".env",
    "google_credentials",
    "google_token",
})


def validate_file_write_path(file_path: str) -> tuple[bool, str]:
    """Validate that a file write operation targets an allowed path.

    Only allows writes to src/secretary/*.py files. Rejects writes to:
    - tests/, data/, campaigns/ directories
    - Files matching _tmp_*, _temp_*, .tmp patterns
    - Any path outside src/secretary/

    Parameters
    ----------
    file_path : str
        The file path to validate (relative or absolute)

    Returns
    -------
    (is_allowed: bool, message: str)
        is_allowed=True if path is safe to write to
        message explains the decision (used for logging or error reporting)
    """
    # Normalize path separators to forward slashes for consistent checking
    normalized = file_path.replace("\\", "/")
    # Strip leading ./
    if normalized.startswith("./"):
        normalized = normalized[2:]

    # Check forbidden patterns first (fast rejection)
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in normalized:
            return False, (
                f"Write rejected: path contains forbidden pattern '{pattern}'. "
                f"Forbidden: {', '.join(sorted(FORBIDDEN_PATTERNS))}."
            )

    # Check explicit allowed files (e.g. goals.yaml at root)
    for allowed_file in ALLOWED_WRITE_FILES:
        if normalized == allowed_file or normalized.endswith(f"/{allowed_file}"):
            return True, f"Write allowed (explicit file): {file_path}"

    # Check if path is under allowed directory
    for allowed_dir in ALLOWED_WRITE_DIRS:
        if normalized.startswith(allowed_dir):
            # src/secretary/ requires .py extension
            if allowed_dir == "src/secretary/" and not normalized.endswith(".py"):
                return False, (
                    f"Write rejected: only .py files allowed in src/secretary/. "
                    f"Cannot write: {file_path}"
                )
            # campaigns/ requires .yaml or .yml
            if allowed_dir == "campaigns/" and not (
                normalized.endswith(".yaml") or normalized.endswith(".yml")
            ):
                return False, (
                    f"Write rejected: only .yaml/.yml files allowed in campaigns/. "
                    f"Cannot write: {file_path}"
                )
            # tests/ requires .py
            if allowed_dir == "tests/" and not normalized.endswith(".py"):
                return False, (
                    f"Write rejected: only .py files allowed in tests/. "
                    f"Cannot write: {file_path}"
                )
            return True, f"Write allowed: {file_path}"

    # Not in any allowed directory
    return False, (
        f"Write rejected: path '{file_path}' is outside allowed scope. "
        f"Allowed: {', '.join(sorted(ALLOWED_WRITE_DIRS))}. "
    )


def filter_tools(
    tools: dict[str, Any],
    *,
    policy: str = "read-only",
) -> dict[str, Any]:
    """Return a restricted copy of the tool registry based on policy.

    Parameters
    ----------
    tools : full tool registry from build_tool_registry()
    policy : one of "read-only", "supervised", "full"

    Returns
    -------
    Subset of `tools` containing only tools allowed by the policy.
    Unknown tools not in any classification are excluded.
    """
    allowed = POLICY_TOOLS.get(policy)
    if allowed is None:
        log.warning("Unknown tool policy '%s', falling back to read-only", policy)
        allowed = POLICY_TOOLS["read-only"]

    filtered = {name: spec for name, spec in tools.items() if name in allowed}

    if filtered != tools:
        removed = set(tools.keys()) - set(filtered.keys())
        if removed:
            log.info(
                "Tool policy '%s': %d/%d tools available (removed: %s)",
                policy, len(filtered), len(tools), ", ".join(sorted(removed)),
            )

    return filtered
