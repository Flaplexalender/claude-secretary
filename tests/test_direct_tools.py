"""Tests for direct_tools module — tool registry builder."""
from pathlib import Path
from unittest.mock import patch, MagicMock
import asyncio

import pytest

from secretary.direct_tools import (
    build_tool_registry,
    build_file_registry,
    _validate_email,
    _validate_body,
    _extract_body,
    _format_headers,
    _format_event,
    _text,
    _error,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_text_helper():
    result = _text("hello")
    assert result == {"content": [{"type": "text", "text": "hello"}]}


def test_error_helper():
    result = _error("bad")
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "bad"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_email_valid():
    assert _validate_email("user@example.com") is None


def test_validate_email_invalid():
    assert _validate_email("not-an-email") is not None


def test_validate_email_empty():
    assert _validate_email("") is not None


def test_validate_body_ok():
    assert _validate_body("short body") is None


def test_validate_body_too_large():
    huge = "x" * 300_000
    result = _validate_body(huge)
    assert result is not None
    assert "too large" in result


# ---------------------------------------------------------------------------
# _extract_body
# ---------------------------------------------------------------------------

def test_extract_body_plain_text():
    import base64
    data = base64.urlsafe_b64encode(b"Hello world").decode()
    payload = {"mimeType": "text/plain", "body": {"data": data}}
    assert _extract_body(payload) == "Hello world"


def test_extract_body_nested():
    import base64
    data = base64.urlsafe_b64encode(b"Nested body").decode()
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": data}},
        ],
    }
    assert _extract_body(payload) == "Nested body"


def test_extract_body_too_deep():
    result = _extract_body({}, _depth=11)
    assert "too deeply nested" in result


def test_extract_body_empty():
    assert _extract_body({}) == ""


# ---------------------------------------------------------------------------
# _format_headers
# ---------------------------------------------------------------------------

def test_format_headers_standard():
    headers = [
        {"name": "Subject", "value": "Test"},
        {"name": "From", "value": "a@b.com"},
        {"name": "Date", "value": "2026-01-01"},
    ]
    result = _format_headers(headers)
    assert result["Subject"] == "Test"
    assert result["From"] == "a@b.com"


def test_format_headers_case_insensitive():
    headers = [{"name": "subject", "value": "lower case"}]
    result = _format_headers(headers)
    assert result["Subject"] == "lower case"


# ---------------------------------------------------------------------------
# _format_event
# ---------------------------------------------------------------------------

def test_format_event_basic():
    event = {
        "summary": "Meeting",
        "start": {"dateTime": "2026-01-01T10:00:00"},
        "end": {"dateTime": "2026-01-01T11:00:00"},
        "id": "ev123",
    }
    text = _format_event(event)
    assert "Meeting" in text
    assert "10:00" in text


def test_format_event_with_location():
    event = {
        "summary": "Lunch",
        "start": {"dateTime": "2026-01-01T12:00:00"},
        "end": {"dateTime": "2026-01-01T13:00:00"},
        "location": "Cafe",
        "id": "ev456",
    }
    text = _format_event(event)
    assert "Cafe" in text


# ---------------------------------------------------------------------------
# build_tool_registry
# ---------------------------------------------------------------------------

def test_registry_empty_without_token(tmp_path: Path):
    """No Google token → empty registry."""
    registry = build_tool_registry(tmp_path)
    assert registry == {}


def test_registry_has_all_tools(tmp_path: Path):
    """With a token file, all 10 tools should be registered."""
    (tmp_path / "google_token.json").write_text("{}")
    registry = build_tool_registry(tmp_path)
    expected = {
        "gmail_search", "gmail_read", "gmail_draft", "gmail_send",
        "gmail_list_drafts", "gmail_get_draft",
        "calendar_today", "calendar_list", "calendar_search", "calendar_create",
    }
    assert set(registry.keys()) == expected


def test_registry_tool_structure(tmp_path: Path):
    """Each tool should have name, description, input_schema, func."""
    (tmp_path / "google_token.json").write_text("{}")
    registry = build_tool_registry(tmp_path)
    for name, tool in registry.items():
        assert tool["name"] == name
        assert isinstance(tool["description"], str)
        assert isinstance(tool["input_schema"], dict)
        assert callable(tool["func"])


def test_registry_schemas_are_valid(tmp_path: Path):
    """Tool schemas should have type: object."""
    (tmp_path / "google_token.json").write_text("{}")
    registry = build_tool_registry(tmp_path)
    for name, tool in registry.items():
        schema = tool["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema


# ---------------------------------------------------------------------------
# OAuth token error detection
# ---------------------------------------------------------------------------

def test_is_token_error_expired():
    from secretary.direct_tools import _is_token_error
    assert _is_token_error("Token has been expired or revoked") is True


def test_is_token_error_invalid_grant():
    from secretary.direct_tools import _is_token_error
    assert _is_token_error("invalid_grant: Token has been revoked") is True


def test_is_token_error_credentials():
    from secretary.direct_tools import _is_token_error
    assert _is_token_error("Could not refresh credentials") is True


def test_is_token_error_false_for_transient():
    from secretary.direct_tools import _is_token_error
    assert _is_token_error("429 Rate limit exceeded") is False
    assert _is_token_error("Connection timeout") is False


def test_call_with_retry_raises_on_token_error():
    """Token errors should raise immediately without retrying."""
    from secretary.direct_tools import _call_with_retry

    call_count = 0

    def failing_func():
        nonlocal call_count
        call_count += 1
        raise Exception("Token has been expired or revoked")

    with pytest.raises(RuntimeError, match="Run 'secretary auth'"):
        asyncio.run(_call_with_retry(failing_func, max_retries=3))

    assert call_count == 1  # Should fail immediately, no retries


# ---------------------------------------------------------------------------
# Calendar time validation
# ---------------------------------------------------------------------------

def test_calendar_create_rejects_end_before_start(tmp_path: Path):
    """calendar_create should reject end_time before start_time."""
    (tmp_path / "google_token.json").write_text("{}")
    registry = build_tool_registry(tmp_path)
    create_fn = registry["calendar_create"]["func"]
    result = asyncio.run(create_fn({
        "summary": "Test",
        "start_time": "2025-06-15T11:00:00",
        "end_time": "2025-06-15T10:00:00",
    }))
    assert "error" in str(result).lower() or "End time must be after" in str(result)


def test_calendar_create_rejects_equal_times(tmp_path: Path):
    """calendar_create should reject equal start and end times."""
    (tmp_path / "google_token.json").write_text("{}")
    registry = build_tool_registry(tmp_path)
    create_fn = registry["calendar_create"]["func"]
    result = asyncio.run(create_fn({
        "summary": "Test",
        "start_time": "2025-06-15T10:00:00",
        "end_time": "2025-06-15T10:00:00",
    }))
    assert "End time must be after" in str(result)


# ---------------------------------------------------------------------------
# build_file_registry
# ---------------------------------------------------------------------------

def test_file_registry_has_three_tools(tmp_path: Path):
    registry = build_file_registry(tmp_path)
    assert set(registry.keys()) == {"file_read", "file_write", "file_list", "file_edit", "grep_search", "run_command", "run_python", "web_fetch"}


def test_file_registry_tool_structure(tmp_path: Path):
    registry = build_file_registry(tmp_path)
    for name, tool in registry.items():
        assert tool["name"] == name
        assert isinstance(tool["description"], str)
        assert callable(tool["func"])


def test_file_read_returns_content(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("Hello from file!", encoding="utf-8")
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_read"]["func"]({"path": "hello.txt"}))
    assert "Hello from file!" in result["content"][0]["text"]


def test_file_read_missing_file(tmp_path: Path):
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_read"]["func"]({"path": "nope.txt"}))
    assert result.get("is_error")
    assert "not found" in result["content"][0]["text"].lower()


# ---------------------------------------------------------------------------
# write_scope enforcement
# ---------------------------------------------------------------------------

def test_write_scope_allows_in_scope_write(tmp_path: Path):
    """file_write should succeed when path is within write_scope."""
    (tmp_path / "src" / "secretary").mkdir(parents=True)
    registry = build_file_registry(tmp_path, write_scope="src/secretary")
    result = asyncio.run(registry["file_write"]["func"]({
        "path": "src/secretary/new.py",
        "content": "# ok",
    }))
    assert not result.get("is_error")
    assert (tmp_path / "src" / "secretary" / "new.py").read_text() == "# ok"


def test_write_scope_blocks_out_of_scope_write(tmp_path: Path):
    """file_write should reject paths outside write_scope."""
    registry = build_file_registry(tmp_path, write_scope="src/secretary")
    result = asyncio.run(registry["file_write"]["func"]({
        "path": "tests/test_new.py",
        "content": "# bad",
    }))
    assert result.get("is_error")
    assert "Write blocked" in result["content"][0]["text"]
    assert not (tmp_path / "tests" / "test_new.py").exists()


def test_write_scope_blocks_out_of_scope_edit(tmp_path: Path):
    """file_edit should reject paths outside write_scope."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "existing.py").write_text("old", encoding="utf-8")
    registry = build_file_registry(tmp_path, write_scope="src/secretary")
    result = asyncio.run(registry["file_edit"]["func"]({
        "path": "tests/existing.py",
        "old_string": "old",
        "new_string": "new",
    }))
    assert result.get("is_error")
    assert "Write blocked" in result["content"][0]["text"]
    assert (tmp_path / "tests" / "existing.py").read_text() == "old"


def test_write_scope_none_allows_all(tmp_path: Path):
    """No write_scope → all writes allowed."""
    registry = build_file_registry(tmp_path, write_scope=None)
    result = asyncio.run(registry["file_write"]["func"]({
        "path": "anywhere.py",
        "content": "# fine",
    }))
    assert not result.get("is_error")


def test_write_scope_read_unaffected(tmp_path: Path):
    """file_read should work even for paths outside write_scope."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "data.txt").write_text("readable", encoding="utf-8")
    registry = build_file_registry(tmp_path, write_scope="src/secretary")
    result = asyncio.run(registry["file_read"]["func"]({"path": "tests/data.txt"}))
    assert not result.get("is_error")
    assert "readable" in result["content"][0]["text"]


def test_file_read_blocks_traversal(tmp_path: Path):
    """Path traversal outside workspace must be rejected."""
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_read"]["func"]({"path": "../../../etc/passwd"}))
    assert result.get("is_error")
    assert "Invalid path" in result["content"][0]["text"]


def test_file_write_creates_file(tmp_path: Path):
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_write"]["func"]({"path": "out.txt", "content": "written!"}))
    assert not result.get("is_error")
    assert (tmp_path / "out.txt").read_text() == "written!"


def test_file_write_creates_parent_dirs(tmp_path: Path):
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_write"]["func"]({"path": "sub/dir/file.txt", "content": "deep"}))
    assert not result.get("is_error")
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "deep"


def test_file_write_blocks_traversal(tmp_path: Path):
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_write"]["func"]({"path": "../../evil.txt", "content": "bad"}))
    assert result.get("is_error")
    assert "Invalid path" in result["content"][0]["text"]


def test_file_list_returns_entries(tmp_path: Path):
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "subdir").mkdir()
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_list"]["func"]({}))
    text = result["content"][0]["text"]
    assert "a.txt" in text
    assert "b.txt" in text
    assert "subdir/" in text


def test_file_list_blocks_traversal(tmp_path: Path):
    registry = build_file_registry(tmp_path)
    result = asyncio.run(registry["file_list"]["func"]({"path": "../.."}))
    assert result.get("is_error")
    assert "Invalid path" in result["content"][0]["text"]


def test_build_tool_registry_with_workspace_no_token(tmp_path: Path):
    """No Google token + workspace_root → only file tools."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_tool_registry(tmp_path, workspace_root=workspace)
    assert set(registry.keys()) == {"file_read", "file_write", "file_list", "file_edit", "grep_search", "run_command", "run_python", "web_fetch"}


def test_build_tool_registry_with_workspace_and_token(tmp_path: Path):
    """Google token + workspace_root → Google tools + file tools."""
    (tmp_path / "google_token.json").write_text("{}")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    registry = build_tool_registry(tmp_path, workspace_root=workspace)
    assert "gmail_search" in registry
    assert "file_read" in registry
    assert "file_write" in registry
    assert "file_list" in registry


def test_build_tool_registry_without_workspace_no_file_tools(tmp_path: Path):
    """No workspace_root → no file tools in registry."""
    (tmp_path / "google_token.json").write_text("{}")
    registry = build_tool_registry(tmp_path)
    assert "file_read" not in registry
    assert "file_write" not in registry


def test_build_file_registry_unrestricted_reads_absolute(tmp_path: Path):
    """No workspace_root → absolute paths work."""
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    registry = build_file_registry(None)
    result = asyncio.run(registry["file_read"]["func"]({"path": str(f)}))
    assert not result.get("is_error")
    assert "hello" in result["content"][0]["text"]


def test_build_tool_registry_unrestricted_files_no_token(tmp_path: Path):
    """No token + unrestricted_files=True → only file tools."""
    registry = build_tool_registry(tmp_path, unrestricted_files=True)
    assert set(registry.keys()) == {"file_read", "file_write", "file_list", "file_edit", "grep_search", "run_command", "run_python", "web_fetch"}


def test_build_tool_registry_unrestricted_files_with_token(tmp_path: Path):
    """Token + unrestricted_files=True → Google + file tools."""
    (tmp_path / "google_token.json").write_text("{}")
    registry = build_tool_registry(tmp_path, unrestricted_files=True)
    assert "gmail_search" in registry
    assert "file_read" in registry
