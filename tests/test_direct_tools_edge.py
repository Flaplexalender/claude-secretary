"""Edge case tests for direct_tools — file tool boundaries, validation corners."""
import asyncio
from pathlib import Path

import pytest

from secretary.direct_tools import build_file_registry, build_tool_registry, _MAX_FILE_BYTES


# ---------------------------------------------------------------------------
# file_read edge cases
# ---------------------------------------------------------------------------

def test_file_read_on_directory(tmp_path: Path):
    """file_read on a directory should return an error."""
    (tmp_path / "subdir").mkdir()
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_read"]["func"]({"path": "subdir"}))
    assert result.get("is_error") is True
    assert "Not a file" in result["content"][0]["text"]


def test_file_read_too_large(tmp_path: Path):
    """file_read on a file larger than _MAX_FILE_BYTES should error."""
    big = tmp_path / "huge.txt"
    big.write_bytes(b"x" * (_MAX_FILE_BYTES + 1))
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_read"]["func"]({"path": "huge.txt"}))
    assert result.get("is_error") is True
    assert "too large" in result["content"][0]["text"].lower()


def test_file_read_empty_file(tmp_path: Path):
    """file_read on an empty file should return the header with 0 bytes."""
    (tmp_path / "empty.txt").write_text("", encoding="utf-8")
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_read"]["func"]({"path": "empty.txt"}))
    assert not result.get("is_error")
    assert "0 bytes" in result["content"][0]["text"]


def test_file_read_binary_content(tmp_path: Path):
    """file_read with non-UTF8 bytes should use error replacement."""
    (tmp_path / "binary.bin").write_bytes(b"\x80\x81\x82Hello\xff")
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_read"]["func"]({"path": "binary.bin"}))
    assert not result.get("is_error")
    assert "Hello" in result["content"][0]["text"]


def test_file_read_missing_path_param(tmp_path: Path):
    """file_read without 'path' parameter should return helpful error."""
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_read"]["func"]({}))
    assert result.get("is_error") is True
    assert "Missing" in result["content"][0]["text"]


def test_file_read_symlink_within_workspace(tmp_path: Path):
    """file_read should handle symlinks that resolve within workspace."""
    import os
    target = tmp_path / "real.txt"
    target.write_text("real content", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks not supported on this platform")
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_read"]["func"]({"path": "link.txt"}))
    assert not result.get("is_error")
    assert "real content" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# file_write edge cases
# ---------------------------------------------------------------------------

def test_file_write_too_large_content(tmp_path: Path):
    """file_write with content exceeding _MAX_FILE_BYTES should error."""
    reg = build_file_registry(tmp_path)
    huge_content = "x" * (_MAX_FILE_BYTES + 1)
    result = asyncio.run(reg["file_write"]["func"]({"path": "big.txt", "content": huge_content}))
    assert result.get("is_error") is True
    assert "too large" in result["content"][0]["text"].lower()


def test_file_write_missing_path(tmp_path: Path):
    """file_write without 'path' should return helpful error."""
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_write"]["func"]({"content": "hello"}))
    assert result.get("is_error") is True
    assert "Missing" in result["content"][0]["text"]


def test_file_write_missing_content(tmp_path: Path):
    """file_write without 'content' should return helpful error."""
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_write"]["func"]({"path": "test.txt"}))
    assert result.get("is_error") is True
    assert "Missing" in result["content"][0]["text"]


def test_file_write_overwrites_existing(tmp_path: Path):
    """file_write should overwrite existing file content."""
    (tmp_path / "exist.txt").write_text("old content", encoding="utf-8")
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_write"]["func"]({"path": "exist.txt", "content": "new content"}))
    assert not result.get("is_error")
    assert (tmp_path / "exist.txt").read_text() == "new content"


def test_file_write_unicode_content(tmp_path: Path):
    """file_write should handle unicode content correctly."""
    reg = build_file_registry(tmp_path)
    content = "Hello 🌍 — résumé with émojis: ✅🚀"
    result = asyncio.run(reg["file_write"]["func"]({"path": "unicode.txt", "content": content}))
    assert not result.get("is_error")
    assert (tmp_path / "unicode.txt").read_text(encoding="utf-8") == content


def test_file_write_empty_content(tmp_path: Path):
    """file_write with empty string creates an empty file."""
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_write"]["func"]({"path": "empty.txt", "content": ""}))
    assert not result.get("is_error")
    assert (tmp_path / "empty.txt").read_text() == ""


# ---------------------------------------------------------------------------
# file_list edge cases
# ---------------------------------------------------------------------------

def test_file_list_empty_directory(tmp_path: Path):
    """file_list on empty directory should show '(empty directory)'."""
    (tmp_path / "empty_dir").mkdir()
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_list"]["func"]({"path": "empty_dir"}))
    assert not result.get("is_error")
    assert "empty directory" in result["content"][0]["text"]


def test_file_list_nonexistent_path(tmp_path: Path):
    """file_list on non-existent path should error."""
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_list"]["func"]({"path": "nope"}))
    assert result.get("is_error") is True
    assert "not found" in result["content"][0]["text"].lower()


def test_file_list_on_file_not_dir(tmp_path: Path):
    """file_list on a file (not directory) should error."""
    (tmp_path / "file.txt").write_text("hello")
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_list"]["func"]({"path": "file.txt"}))
    assert result.get("is_error") is True
    assert "Not a directory" in result["content"][0]["text"]


def test_file_list_shows_sizes(tmp_path: Path):
    """file_list should show file sizes."""
    (tmp_path / "small.txt").write_text("abc")
    (tmp_path / "bigger.txt").write_text("x" * 100)
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_list"]["func"]({}))
    text = result["content"][0]["text"]
    assert "small.txt" in text
    assert "bigger.txt" in text
    assert "bytes" in text


def test_file_list_default_path(tmp_path: Path):
    """file_list with no path arg uses '.' (workspace root)."""
    (tmp_path / "root_file.txt").write_text("hello")
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_list"]["func"]({}))
    assert not result.get("is_error")
    assert "root_file.txt" in result["content"][0]["text"]


def test_file_list_sorted(tmp_path: Path):
    """file_list entries should be sorted alphabetically."""
    for name in ["zebra.txt", "alpha.txt", "middle.txt"]:
        (tmp_path / name).write_text("x")
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_list"]["func"]({}))
    text = result["content"][0]["text"]
    a_pos = text.index("alpha.txt")
    m_pos = text.index("middle.txt")
    z_pos = text.index("zebra.txt")
    assert a_pos < m_pos < z_pos


# ---------------------------------------------------------------------------
# Sandbox path security — more traversal tests
# ---------------------------------------------------------------------------

def test_file_read_absolute_path_blocked(tmp_path: Path):
    """Absolute paths should be rejected when workspace is set."""
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_read"]["func"]({"path": "/etc/passwd"}))
    assert result.get("is_error") is True


def test_file_write_absolute_path_blocked(tmp_path: Path):
    """Absolute path writes should be rejected."""
    reg = build_file_registry(tmp_path)
    result = asyncio.run(reg["file_write"]["func"]({"path": "/tmp/evil.txt", "content": "bad"}))
    assert result.get("is_error") is True


def test_file_read_dotdot_segments(tmp_path: Path):
    """Various .. traversal attempts should be blocked."""
    reg = build_file_registry(tmp_path)
    for bad_path in ["../outside", "sub/../../outside", "a/b/c/../../../.."]:
        result = asyncio.run(reg["file_read"]["func"]({"path": bad_path}))
        assert result.get("is_error") is True, f"Should block: {bad_path}"


# ---------------------------------------------------------------------------
# Unrestricted file registry
# ---------------------------------------------------------------------------

def test_unrestricted_file_write(tmp_path: Path):
    """Unrestricted registry can write to absolute paths."""
    reg = build_file_registry(None)
    target = tmp_path / "unrestricted.txt"
    result = asyncio.run(reg["file_write"]["func"]({"path": str(target), "content": "free"}))
    assert not result.get("is_error")
    assert target.read_text() == "free"


def test_unrestricted_file_list(tmp_path: Path):
    """Unrestricted registry can list absolute paths."""
    (tmp_path / "item.txt").write_text("x")
    reg = build_file_registry(None)
    result = asyncio.run(reg["file_list"]["func"]({"path": str(tmp_path)}))
    assert not result.get("is_error")
    assert "item.txt" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# build_tool_registry integration
# ---------------------------------------------------------------------------

def test_registry_with_workspace_includes_file_tools(tmp_path: Path):
    """workspace_root adds file tools even without Google token."""
    reg = build_tool_registry(tmp_path, workspace_root=tmp_path)
    assert "file_read" in reg
    assert "file_write" in reg
    assert "file_list" in reg
    # No Google tools
    assert "gmail_search" not in reg


def test_registry_unrestricted_and_token(tmp_path: Path):
    """unrestricted_files + token → all Google + file tools."""
    (tmp_path / "google_token.json").write_text("{}")
    reg = build_tool_registry(tmp_path, unrestricted_files=True)
    assert "gmail_search" in reg
    assert "file_read" in reg
    # Google: gmail_search, gmail_read, gmail_draft, gmail_send, gmail_list_drafts, gmail_get_draft (6)
    # + calendar_today, calendar_list, calendar_search, calendar_create (4) = 10
    # File: file_read, file_write, file_list, file_edit, grep_search, run_command, run_python, web_fetch (8)
    # Total: 18
    assert len(reg) == 18


def test_registry_workspace_overrides_unrestricted(tmp_path: Path):
    """When both workspace_root and unrestricted set, unrestricted wins."""
    # Based on code: unrestricted is checked first
    reg = build_tool_registry(tmp_path, workspace_root=tmp_path, unrestricted_files=True)
    # Should have file tools (unrestricted path)
    assert "file_read" in reg
