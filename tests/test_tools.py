"""Tests for MCP tool builders — offline, uses mocked Google APIs."""
import asyncio
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from secretary.tools import build_mcp_servers, _build_gmail_tools, _build_calendar_tools


def test_build_mcp_servers_no_token(tmp_path: Path):
    """Returns empty dict when no Google OAuth token exists."""
    servers = build_mcp_servers(tmp_path)
    assert servers == {}


def test_build_mcp_servers_with_token(tmp_path: Path):
    """Returns gmail + calendar servers when token exists."""
    (tmp_path / "google_token.json").write_text("{}")
    with patch("secretary.tools.build_gmail_service"), \
         patch("secretary.tools.build_calendar_service"):
        servers = build_mcp_servers(tmp_path)
    assert "gmail" in servers
    assert "calendar" in servers


def test_gmail_tools_count(tmp_path: Path):
    """Gmail should provide 6 tools."""
    tools = _build_gmail_tools(tmp_path)
    assert len(tools) == 6
    names = {t.name for t in tools}
    assert names == {"gmail_search", "gmail_read", "gmail_draft", "gmail_send", "gmail_list_drafts", "gmail_get_draft"}


def test_calendar_tools_count(tmp_path: Path):
    """Calendar should provide 4 tools."""
    tools = _build_calendar_tools(tmp_path)
    assert len(tools) == 4
    names = {t.name for t in tools}
    assert names == {"calendar_today", "calendar_list", "calendar_search", "calendar_create"}


def test_gmail_tool_schemas(tmp_path: Path):
    """Gmail tools should have proper JSON schemas with required fields."""
    tools = _build_gmail_tools(tmp_path)
    search = next(t for t in tools if t.name == "gmail_search")
    assert "query" in search.input_schema["properties"]
    assert "query" in search.input_schema["required"]

    send = next(t for t in tools if t.name == "gmail_send")
    assert "to" in send.input_schema["required"]


# ── Email validation ──────────────────────────────────────────

def test_validate_email_valid():
    from secretary.tools import _validate_email
    assert _validate_email("user@example.com") is None
    assert _validate_email("test+tag@sub.domain.org") is None


def test_validate_email_invalid():
    from secretary.tools import _validate_email
    assert _validate_email("not-an-email") is not None
    assert _validate_email("@missing-local.com") is not None
    assert _validate_email("no-domain@") is not None
    assert _validate_email("") is not None


# ── Email body size limit ─────────────────────────────────────

def test_validate_body_ok():
    from secretary.tools import _validate_body
    assert _validate_body("Hello, world!") is None


def test_validate_body_too_large():
    from secretary.tools import _validate_body
    big_body = "x" * 300_000
    result = _validate_body(big_body)
    assert result is not None
    assert "too large" in result


def test_gmail_draft_rejects_large_body(tmp_path: Path):
    """gmail_draft should reject oversized body before calling API."""
    tools = _build_gmail_tools(tmp_path)
    draft_tool = next(t for t in tools if t.name == "gmail_draft")
    big_body = "x" * 300_000
    result = asyncio.run(
        draft_tool.handler({"to": "test@example.com", "subject": "Hi", "body": big_body})
    )
    assert result.get("is_error") is True
    assert "too large" in result["content"][0]["text"]


def test_gmail_send_rejects_large_body(tmp_path: Path):
    """gmail_send should reject oversized body before calling API."""
    tools = _build_gmail_tools(tmp_path)
    send_tool = next(t for t in tools if t.name == "gmail_send")
    big_body = "x" * 300_000
    result = asyncio.run(
        send_tool.handler({"to": "test@example.com", "subject": "Hi", "body": big_body})
    )
    assert result.get("is_error") is True
    assert "too large" in result["content"][0]["text"]


# ── extract_body safety ───────────────────────────────────────

def test_extract_body_bad_base64():
    from secretary.tools import _extract_body
    # Trigger a decode error by passing a non-string data value
    payload = {"mimeType": "text/plain", "body": {"data": 12345}}
    result = _extract_body(payload)
    assert result == "[Failed to decode email body]"


def test_gmail_tool_schemas_required_fields(tmp_path: Path):
    """Gmail send tool should require subject and body."""
    tools = _build_gmail_tools(tmp_path)
    send = next(t for t in tools if t.name == "gmail_send")
    assert "subject" in send.input_schema["required"]
    assert "body" in send.input_schema["required"]


def test_calendar_tool_schemas(tmp_path: Path):
    """Calendar tools should have proper JSON schemas."""
    tools = _build_calendar_tools(tmp_path)
    create = next(t for t in tools if t.name == "calendar_create")
    assert "summary" in create.input_schema["required"]
    assert "start_time" in create.input_schema["required"]
    assert "end_time" in create.input_schema["required"]

    today = next(t for t in tools if t.name == "calendar_today")
    assert "max_results" in today.input_schema["properties"]


# ── _extract_body recursion limit ─────────────────────────────

def test_extract_body_deeply_nested():
    """Deeply nested payloads should hit recursion limit gracefully."""
    from secretary.tools import _extract_body
    # Build a payload nested 15 levels deep (exceeds max_depth=10)
    payload: dict = {"mimeType": "text/plain", "body": {"data": "aGVsbG8="}}
    for _ in range(15):
        payload = {"mimeType": "multipart/mixed", "parts": [payload]}
    result = _extract_body(payload)
    assert result == "[Email body too deeply nested]"


def test_extract_body_normal_nesting():
    """Normal nesting should work fine."""
    from secretary.tools import _extract_body
    import base64
    data = base64.urlsafe_b64encode(b"Hello world").decode()
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [{"mimeType": "text/plain", "body": {"data": data}}],
    }
    result = _extract_body(payload)
    assert result == "Hello world"


# ── _call_with_retry ──────────────────────────────────────────

def test_call_with_retry_success():
    """Should return result on first success."""
    from secretary.tools import _call_with_retry
    calls = []

    def succeeds():
        calls.append(1)
        return "ok"

    result = asyncio.run(_call_with_retry(succeeds))
    assert result == "ok"
    assert len(calls) == 1


def test_call_with_retry_transient_then_success():
    """Should retry on transient 429 and succeed."""
    from secretary.tools import _call_with_retry
    attempt = [0]

    def flaky():
        attempt[0] += 1
        if attempt[0] < 3:
            raise Exception("HTTP 429 rate limit exceeded")
        return "recovered"

    result = asyncio.run(
        _call_with_retry(flaky, max_retries=3)
    )
    assert result == "recovered"
    assert attempt[0] == 3


def test_call_with_retry_permanent_error():
    """Non-transient errors should not be retried."""
    from secretary.tools import _call_with_retry
    import pytest

    def permanent_fail():
        raise ValueError("Invalid argument")

    with pytest.raises(ValueError, match="Invalid argument"):
        asyncio.run(
            _call_with_retry(permanent_fail, max_retries=3)
        )


# ── _format_headers case-insensitive ──────────────────────────

def test_format_headers_canonical_case():
    """Headers with standard casing should be returned as-is."""
    from secretary.tools import _format_headers
    headers = [
        {"name": "Subject", "value": "Hello"},
        {"name": "From", "value": "test@example.com"},
        {"name": "To", "value": "recipient@example.com"},
        {"name": "Date", "value": "Mon, 1 Jan 2026"},
    ]
    result = _format_headers(headers)
    assert result["Subject"] == "Hello"
    assert result["To"] == "recipient@example.com"


def test_format_headers_lowercase():
    """Headers with lowercase names (from gmail_draft) should be normalized."""
    from secretary.tools import _format_headers
    headers = [
        {"name": "subject", "value": "My Draft"},
        {"name": "to", "value": "help@example.com"},
        {"name": "date", "value": "Wed, 11 Mar 2026"},
    ]
    result = _format_headers(headers)
    assert result["Subject"] == "My Draft"
    assert result["To"] == "help@example.com"
    assert result["Date"] == "Wed, 11 Mar 2026"


def test_format_headers_mixed_case():
    """Mix of canonical and lowercase headers."""
    from secretary.tools import _format_headers
    headers = [
        {"name": "Subject", "value": "Proper"},
        {"name": "to", "value": "lower@example.com"},
        {"name": "X-Custom", "value": "ignored"},
    ]
    result = _format_headers(headers)
    assert result["Subject"] == "Proper"
    assert result["To"] == "lower@example.com"
    assert "X-Custom" not in result


# ── gmail_list_drafts / gmail_get_draft schemas ──────────────

def test_gmail_list_drafts_schema(tmp_path: Path):
    """gmail_list_drafts should have max_results in schema."""
    tools = _build_gmail_tools(tmp_path)
    tool = next(t for t in tools if t.name == "gmail_list_drafts")
    assert "max_results" in tool.input_schema["properties"]


def test_gmail_get_draft_schema(tmp_path: Path):
    """gmail_get_draft should require draft_id."""
    tools = _build_gmail_tools(tmp_path)
    tool = next(t for t in tools if t.name == "gmail_get_draft")
    assert "draft_id" in tool.input_schema["required"]


def test_gmail_draft_invalid_email(tmp_path: Path):
    """gmail_draft should reject invalid email."""
    tools = _build_gmail_tools(tmp_path)
    draft_tool = next(t for t in tools if t.name == "gmail_draft")
    result = asyncio.run(
        draft_tool.handler({"to": "not-valid", "subject": "Hi", "body": "test"})
    )
    assert result.get("is_error") is True
    assert "Invalid email" in result["content"][0]["text"]


def test_gmail_send_invalid_email(tmp_path: Path):
    """gmail_send should reject invalid email."""
    tools = _build_gmail_tools(tmp_path)
    send_tool = next(t for t in tools if t.name == "gmail_send")
    result = asyncio.run(
        send_tool.handler({"to": "bad", "subject": "Hi", "body": "test"})
    )
    assert result.get("is_error") is True


# ── gmail_draft/send MIMEText ─────────────────────────────────

def test_gmail_draft_creates_message(tmp_path: Path):
    """gmail_draft should create a MIMEText message (not reference undefined var)."""
    tools = _build_gmail_tools(tmp_path)
    draft_tool = next(t for t in tools if t.name == "gmail_draft")

    mock_svc = MagicMock()
    mock_svc.users.return_value.drafts.return_value.create.return_value.execute.return_value = {"id": "d123"}

    with patch("secretary.tools.build_gmail_service", return_value=mock_svc):
        result = asyncio.run(
            draft_tool.handler({"to": "test@example.com", "subject": "Hi", "body": "Hello"})
        )
    assert "d123" in str(result)


def test_gmail_send_creates_message(tmp_path: Path):
    """gmail_send should create a MIMEText message (not reference undefined var)."""
    tools = _build_gmail_tools(tmp_path)
    send_tool = next(t for t in tools if t.name == "gmail_send")

    mock_svc = MagicMock()
    mock_svc.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "m456"}

    with patch("secretary.tools.build_gmail_service", return_value=mock_svc):
        result = asyncio.run(
            send_tool.handler({"to": "test@example.com", "subject": "Hi", "body": "Hello"})
        )
    assert "m456" in str(result)


# ── gmail_read body truncation ────────────────────────────────

def test_gmail_read_truncates_long_body(tmp_path: Path):
    """gmail_read should truncate message bodies longer than 15000 characters."""
    import base64

    tools = _build_gmail_tools(tmp_path)
    read_tool = next(t for t in tools if t.name == "gmail_read")

    long_body = "x" * 20_000
    encoded_body = base64.urlsafe_b64encode(long_body.encode()).decode()

    mock_msg = {
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Long email"},
                {"name": "From", "value": "sender@example.com"},
            ],
            "body": {"data": encoded_body},
        },
        "labelIds": ["INBOX"],
    }

    mock_svc = MagicMock()
    mock_svc.users.return_value.messages.return_value.get.return_value.execute.return_value = mock_msg

    with patch("secretary.tools.build_gmail_service", return_value=mock_svc):
        result = asyncio.run(
            read_tool.handler({"message_id": "msg123"})
        )

    text = result["content"][0]["text"]
    assert "...[truncated]" in text
    assert len(text) < 20_000
