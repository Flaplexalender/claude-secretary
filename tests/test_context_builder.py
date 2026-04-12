"""Tests for context_builder — workspace identity and skill loading."""
from pathlib import Path

from secretary.context_builder import (
    build_identity_prompt,
    build_skill_context,
    load_workspace_identity,
    load_reference_context,
    match_skills,
    _parse_skill_frontmatter,
)


def test_build_identity_prompt_missing_dir(tmp_path: Path):
    """Returns None when workspace dir doesn't exist."""
    result = build_identity_prompt(str(tmp_path / "nonexistent"))
    assert result is None


def test_build_identity_prompt_empty_dir(tmp_path: Path):
    """Returns None when workspace dir exists but has no files."""
    ws = tmp_path / "ws"
    ws.mkdir()
    result = build_identity_prompt(str(ws))
    assert result is None


def test_build_identity_prompt_with_files(tmp_path: Path):
    """Composes identity from available files."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "IDENTITY.md").write_text("# Identity\nName: TestBot", encoding="utf-8")
    (ws / "SOUL.md").write_text("# Soul\nBe helpful.", encoding="utf-8")
    (ws / "USER.md").write_text("# User\nName: Alice", encoding="utf-8")
    result = build_identity_prompt(str(ws))
    assert "TestBot" in result
    assert "Be helpful" in result
    assert "Alice" in result


def test_build_identity_prompt_partial_files(tmp_path: Path):
    """Works with only some files present."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "SOUL.md").write_text("Act concisely.", encoding="utf-8")
    result = build_identity_prompt(str(ws))
    assert "Act concisely" in result


def test_load_reference_context(tmp_path: Path):
    """Loads TOOLS and AGENTS reference files."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "TOOLS.md").write_text("# Tools\ngmail_search", encoding="utf-8")
    (ws / "AGENTS.md").write_text("# Agents\nSession startup", encoding="utf-8")
    result = load_reference_context(ws)
    assert "gmail_search" in result
    assert "Session startup" in result


def test_load_reference_context_missing(tmp_path: Path):
    """Returns None when no reference files exist."""
    ws = tmp_path / "ws"
    ws.mkdir()
    result = load_reference_context(ws)
    assert result is None


def test_parse_skill_frontmatter():
    """Parses YAML frontmatter from skill files."""
    text = "---\nname: gmail\ndescription: Email tools\n---\n## Usage"
    fm = _parse_skill_frontmatter(text)
    assert fm["name"] == "gmail"
    assert fm["description"] == "Email tools"


def test_parse_skill_frontmatter_missing():
    """Returns empty dict when no frontmatter."""
    fm = _parse_skill_frontmatter("# No frontmatter here")
    assert fm == {}


def test_match_skills_by_name(tmp_path: Path):
    """Matches skills by name appearing in task."""
    ws = tmp_path / "ws"
    skill_dir = ws / "skills" / "gmail"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: gmail\ndescription: Email tools\n---\n## How to use",
        encoding="utf-8",
    )
    matched = match_skills(str(ws), "check gmail for new messages")
    assert len(matched) == 1
    assert "Email tools" in matched[0]


def test_match_skills_by_description(tmp_path: Path):
    """Matches skills by description keywords in task."""
    ws = tmp_path / "ws"
    skill_dir = ws / "skills" / "self-improve"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: self-improve\ndescription: Autonomous code improvement pipeline\n---\n## Steps",
        encoding="utf-8",
    )
    matched = match_skills(str(ws), "improve the code autonomously")
    assert len(matched) == 1


def test_match_skills_no_match(tmp_path: Path):
    """Returns empty list when no skills match."""
    ws = tmp_path / "ws"
    skill_dir = ws / "skills" / "gmail"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: gmail\ndescription: Email tools\n---\n## How",
        encoding="utf-8",
    )
    matched = match_skills(str(ws), "what is the weather today")
    assert matched == []


def test_match_skills_no_skills_dir(tmp_path: Path):
    """Returns empty list when skills dir doesn't exist."""
    matched = match_skills(str(tmp_path), "anything")
    assert matched == []


def test_build_skill_context_none_workspace():
    """Returns None when workspace_dir is None."""
    result = build_skill_context(None, "test")
    assert result is None


def test_build_skill_context_empty_task(tmp_path: Path):
    """Returns None when task is empty."""
    result = build_skill_context(str(tmp_path), "")
    assert result is None
