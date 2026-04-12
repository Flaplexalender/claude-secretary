"""Tests for _tool_helpers — shared Gmail/Calendar helper functions.

Pure unit tests — no API calls, no network. Tests the canonical implementations
that both tools.py and direct_tools.py depend on.
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import MagicMock, patch

import pytest

from secretary._tool_helpers import (
    _text,
    _error,
    _extract_body,
    _format_headers,
    _validate_email,
    _validate_body,
    _format_event,
    _is_token_error,
    _call_with_retry,
    _in_executor,
    _EMAIL_RE,
    _MAX_EMAIL_BODY_BYTES,
)


# ══════════════════════════════════════════════════════════════
#  _text / _error
# ══════════════════════════════════════════════════════════════


def test_text_format():
    result = _text("hello world")
    assert result == {"content": [{"type": "text", "text": "hello world"}]}


def test_error_format():
    result = _error("something went wrong")
    assert result["content"][0]["text"] == "something went wrong"
    assert result["is_error"] is True


def test_text_empty_string():
    result = _text("")
    assert result["content"][0]["text"] == ""


# ══════════════════════════════════════════════════════════════
#  _extract_body
# ══════════════════════════════════════════════════════════════


def test_extract_body_text_plain():
    """Extract text/plain body from simple payload."""
    body_text = "Hello, this is the email body."
    encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
    payload = {
        "mimeType": "text/plain",
        "body": {"data": encoded},
    }
    assert _extract_body(payload) == body_text


def test_extract_body_multipart():
    """Extract text/plain from multipart payload."""
    body_text = "Nested body content"
    encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": encoded}},
            {"mimeType": "text/html", "body": {"data": "aHRtbA=="}},
        ],
    }
    assert _extract_body(payload) == body_text


def test_extract_body_empty_payload():
    """Empty payload returns empty string."""
    assert _extract_body({}) == ""


def test_extract_body_no_data():
    """Payload with mimeType but no body data returns empty."""
    payload = {"mimeType": "text/plain", "body": {}}
    assert _extract_body(payload) == ""


def test_extract_body_deeply_nested():
    """Very deeply nested payloads are capped at depth 10."""
    # Build a 12-level deep nesting
    payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"deep").decode()}}
    for _ in range(12):
        payload = {"mimeType": "multipart/mixed", "parts": [payload]}

    result = _extract_body(payload)
    assert result == "[Email body too deeply nested]"


def test_extract_body_unicode():
    """Unicode content in email body is handled correctly."""
    body_text = "Héllo wörld 🚀"
    encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
    payload = {"mimeType": "text/plain", "body": {"data": encoded}}
    assert _extract_body(payload) == body_text


# ══════════════════════════════════════════════════════════════
#  _format_headers
# ══════════════════════════════════════════════════════════════


def test_format_headers_standard():
    headers = [
        {"name": "Subject", "value": "Test Email"},
        {"name": "From", "value": "alice@example.com"},
        {"name": "To", "value": "bob@example.com"},
        {"name": "Date", "value": "Mon, 1 Jan 2026"},
    ]
    result = _format_headers(headers)
    assert result == {
        "Subject": "Test Email",
        "From": "alice@example.com",
        "To": "bob@example.com",
        "Date": "Mon, 1 Jan 2026",
    }


def test_format_headers_case_insensitive():
    """Lowercase header names are normalized to canonical form."""
    headers = [
        {"name": "subject", "value": "Lower Case"},
        {"name": "from", "value": "sender@test.com"},
    ]
    result = _format_headers(headers)
    assert result["Subject"] == "Lower Case"
    assert result["From"] == "sender@test.com"


def test_format_headers_ignores_unwanted():
    """Headers not in the wanted set are ignored."""
    headers = [
        {"name": "Subject", "value": "Test"},
        {"name": "X-Custom", "value": "ignored"},
        {"name": "Content-Type", "value": "ignored"},
    ]
    result = _format_headers(headers)
    assert "X-Custom" not in result
    assert "Content-Type" not in result
    assert len(result) == 1


def test_format_headers_empty():
    assert _format_headers([]) == {}


# ══════════════════════════════════════════════════════════════
#  _validate_email
# ══════════════════════════════════════════════════════════════


def test_validate_email_valid():
    assert _validate_email("user@example.com") is None
    assert _validate_email("a.b+c@d.e.f") is None


def test_validate_email_invalid():
    assert _validate_email("not-an-email") is not None
    assert _validate_email("@missing-local.com") is not None
    assert _validate_email("missing-domain@") is not None
    assert _validate_email("") is not None
    assert _validate_email("spaces in@email.com") is not None


# ══════════════════════════════════════════════════════════════
#  _validate_body
# ══════════════════════════════════════════════════════════════


def test_validate_body_ok():
    assert _validate_body("Normal email body") is None


def test_validate_body_too_large():
    huge_body = "x" * (_MAX_EMAIL_BODY_BYTES + 1)
    result = _validate_body(huge_body)
    assert result is not None
    assert "too large" in result.lower()


def test_validate_body_exactly_at_limit():
    """Body exactly at the limit should pass."""
    body = "x" * _MAX_EMAIL_BODY_BYTES
    assert _validate_body(body) is None


# ══════════════════════════════════════════════════════════════
#  _format_event
# ══════════════════════════════════════════════════════════════


def test_format_event_basic():
    event = {
        "summary": "Team Meeting",
        "start": {"dateTime": "2026-01-15T10:00:00Z"},
        "end": {"dateTime": "2026-01-15T11:00:00Z"},
        "id": "abc123",
    }
    result = _format_event(event)
    assert "Team Meeting" in result
    assert "10:00:00Z" in result
    assert "abc123" in result


def test_format_event_with_location():
    event = {
        "summary": "Offsite",
        "start": {"dateTime": "2026-01-15T09:00:00Z"},
        "end": {"dateTime": "2026-01-15T17:00:00Z"},
        "location": "Conference Room B",
        "id": "xyz",
    }
    result = _format_event(event)
    assert "Conference Room B" in result


def test_format_event_with_description():
    event = {
        "summary": "Planning",
        "start": {"dateTime": "2026-01-15T14:00:00Z"},
        "end": {"dateTime": "2026-01-15T15:00:00Z"},
        "description": "Quarterly planning session",
        "id": "qrs",
    }
    result = _format_event(event)
    assert "Quarterly planning" in result


def test_format_event_all_day():
    """All-day events use date instead of dateTime."""
    event = {
        "summary": "Holiday",
        "start": {"date": "2026-01-15"},
        "end": {"date": "2026-01-16"},
        "id": "hol",
    }
    result = _format_event(event)
    assert "2026-01-15" in result
    assert "Holiday" in result


def test_format_event_no_title():
    event = {
        "start": {"dateTime": "2026-01-15T10:00:00Z"},
        "end": {"dateTime": "2026-01-15T11:00:00Z"},
        "id": "notitle",
    }
    result = _format_event(event)
    assert "(no title)" in result


def test_format_event_long_description_truncated():
    """Very long descriptions are truncated to 200 chars."""
    event = {
        "summary": "Event",
        "start": {"dateTime": "2026-01-15T10:00:00Z"},
        "end": {"dateTime": "2026-01-15T11:00:00Z"},
        "description": "A" * 500,
        "id": "long",
    }
    result = _format_event(event)
    # Description should be present but truncated
    assert "AAAA" in result
    # The full 500 chars should NOT be in the output
    assert "A" * 500 not in result


# ══════════════════════════════════════════════════════════════
#  _is_token_error
# ══════════════════════════════════════════════════════════════


def test_is_token_error_expired():
    assert _is_token_error("Token has been expired or revoked") is True


def test_is_token_error_invalid_grant():
    assert _is_token_error("invalid_grant: token expired") is True


def test_is_token_error_revoked():
    assert _is_token_error("token has been revoked by the user") is True


def test_is_token_error_credentials():
    assert _is_token_error("Invalid credentials provided") is True


def test_is_token_error_refresh():
    assert _is_token_error("Refresh token has been revoked") is True


def test_is_token_error_not_token():
    assert _is_token_error("Connection timeout") is False
    assert _is_token_error("Rate limit exceeded") is False
    assert _is_token_error("Internal server error") is False


# ══════════════════════════════════════════════════════════════
#  _call_with_retry
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_call_with_retry_success():
    """Successful call returns immediately."""
    result = await _call_with_retry(lambda: "ok")
    assert result == "ok"


@pytest.mark.asyncio
async def test_call_with_retry_transient_then_success():
    """Transient errors are retried."""
    calls = [0]

    def flaky():
        calls[0] += 1
        if calls[0] < 2:
            raise Exception("503 Service Unavailable")
        return "recovered"

    result = await _call_with_retry(flaky, max_retries=3)
    assert result == "recovered"
    assert calls[0] == 2


@pytest.mark.asyncio
async def test_call_with_retry_token_error_no_retry():
    """Token errors raise immediately (no retry)."""
    def token_fail():
        raise Exception("invalid_grant: Token has been expired")

    with pytest.raises(RuntimeError, match="OAuth token error"):
        await _call_with_retry(token_fail, max_retries=3)


@pytest.mark.asyncio
async def test_call_with_retry_non_transient_raises():
    """Non-transient, non-token errors raise immediately."""
    def permanent_fail():
        raise ValueError("Invalid argument: bad query")

    with pytest.raises(ValueError):
        await _call_with_retry(permanent_fail, max_retries=3)


@pytest.mark.asyncio
async def test_call_with_retry_exhausted():
    """After max_retries transient failures, the last error is raised."""
    def always_fail():
        raise Exception("503 Service Unavailable")

    with pytest.raises(Exception, match="503"):
        await _call_with_retry(always_fail, max_retries=2)


# ══════════════════════════════════════════════════════════════
#  _in_executor
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_in_executor_runs_blocking():
    """_in_executor runs a blocking function in a thread."""
    result = await _in_executor(lambda: 42)
    assert result == 42


@pytest.mark.asyncio
async def test_in_executor_passes_args():
    """_in_executor passes positional and keyword arguments."""
    def add(a, b, offset=0):
        return a + b + offset

    result = await _in_executor(add, 3, 4, offset=10)
    assert result == 17


# ══════════════════════════════════════════════════════════════
#  _EMAIL_RE
# ══════════════════════════════════════════════════════════════


def test_email_regex_valid():
    assert _EMAIL_RE.match("user@example.com")
    assert _EMAIL_RE.match("a@b.c")
    assert _EMAIL_RE.match("complex+tag@sub.domain.org")


def test_email_regex_invalid():
    assert not _EMAIL_RE.match("not-email")
    assert not _EMAIL_RE.match("@no-local.com")
    assert not _EMAIL_RE.match("no-domain@")
    assert not _EMAIL_RE.match("")
