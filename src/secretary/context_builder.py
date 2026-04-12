"""Context builder — construct system prompts from workspace identity files.

Reads the workspace/ directory (IDENTITY.md, SOUL.md, USER.md, AGENTS.md,
TOOLS.md, MEMORY.md) and composes them into a system prompt. Replaces the
hardcoded prompt strings in agent.py / direct_agent.py / oracle.py.

Graceful fallback: if workspace_dir doesn't exist or files are missing,
returns None so callers can fall back to their existing hardcoded prompts.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Files read in order. Each becomes a section in the system prompt.
_PILLAR_FILES = [
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
]

# Files whose content is injected only when relevant (not every prompt).
_REFERENCE_FILES = [
    "TOOLS.md",
    "AGENTS.md",
    "HEARTBEAT.md",
]


def _read_md(path: Path) -> str | None:
    """Read a markdown file, return stripped content or None."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        log.debug("Could not read %s", path)
    return None


def _parse_skill_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML frontmatter (name, description) from a skill file."""
    m = re.match(r"^---\s*\n(.+?)\n---", text, re.DOTALL)
    if not m:
        return {}
    result = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip()
    return result


def load_workspace_identity(workspace_dir: Path) -> str | None:
    """Load core identity files and return composed prompt section.

    Returns None if workspace_dir doesn't exist.
    """
    if not workspace_dir.is_dir():
        return None

    parts: list[str] = []

    # Identity is special — extract the key-value pairs as a one-liner
    identity = _read_md(workspace_dir / "IDENTITY.md")
    if identity:
        parts.append(identity)

    for fname in _PILLAR_FILES:
        content = _read_md(workspace_dir / fname)
        if content:
            parts.append(content)

    # Inject recent daily logs (today + yesterday) if they exist
    from .memory import MarkdownMemory
    md_mem = MarkdownMemory(workspace_dir)
    daily = md_mem.read_daily()
    if daily:
        parts.append(f"# Recent Activity\n\n{daily}")

    if not parts:
        return None

    return "\n\n".join(parts)


def load_reference_context(workspace_dir: Path) -> str | None:
    """Load reference files (TOOLS, AGENTS) — used for full-context prompts."""
    if not workspace_dir.is_dir():
        return None

    parts: list[str] = []
    for fname in _REFERENCE_FILES:
        content = _read_md(workspace_dir / fname)
        if content:
            parts.append(content)

    return "\n\n".join(parts) if parts else None


def match_skills(workspace_dir: Path | str, task: str) -> list[str]:
    """Find skill files whose name/description matches the task.

    Returns list of skill file contents (full markdown).
    """
    skills_dir = Path(workspace_dir) / "skills"
    if not skills_dir.is_dir():
        return []

    task_lower = task.lower()
    matched: list[str] = []

    for skill_dir in sorted(skills_dir.iterdir()):
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            continue

        content = _read_md(skill_file)
        if not content:
            continue

        fm = _parse_skill_frontmatter(content)
        name = fm.get("name", skill_dir.name).lower()
        desc = fm.get("description", "").lower()

        # Match if skill name or description keywords appear in task
        if name in task_lower or any(
            word in task_lower
            for word in desc.split()
            if len(word) > 3  # skip short words
        ):
            matched.append(content)

    return matched


def build_identity_prompt(
    workspace_dir: Path | str | None,
) -> str | None:
    """Build the identity portion of the system prompt from workspace files.

    Returns the composed identity string, or None if workspace is unavailable.
    This replaces the hardcoded "You are a capable research assistant..." lines.
    """
    if workspace_dir is None:
        return None

    ws = Path(workspace_dir)
    return load_workspace_identity(ws)


def build_skill_context(
    workspace_dir: Path | str | None,
    task: str,
) -> str | None:
    """Build skill-specific context for a task.

    Returns matched skill content, or None if no skills match.
    """
    if workspace_dir is None or not task:
        return None

    ws = Path(workspace_dir)
    skills = match_skills(ws, task)
    if not skills:
        return None

    return "\n\n".join(skills)
