"""Shared helpers for Gmail/Calendar tool implementations.

Extracted from tools.py and direct_tools.py to eliminate ~300 lines of
duplication. Both modules import from here.

Functions:
    _text, _error       — Format tool results
    _in_executor         — Run blocking calls in thread pool
    _call_with_retry     — Retry Google API calls on transient errors
    _extract_body        — Extract text/plain body from Gmail payload
    _format_headers      — Normalize email headers
    _validate_email      — Validate email address format
    _validate_body       — Validate email body size
    _format_event        — Format a calendar event for display
    _is_token_error      — Detect expired/revoked OAuth tokens
"""
from __future__ import annotations

import asyncio
import base64
import re
from functools import partial
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_EMAIL_BODY_BYTES = 256_000  # ~256 KB limit for email body

_TRANSIENT = ("429", "503", "rate limit", "rate_limit", "connection", "timeout")
_TOKEN_ERRORS = (
    "invalid_grant",
    "token has been expired",
    "token has been revoked",
    "invalid_client",
    "refresh token",
    "credentials",
)


# ---------------------------------------------------------------------------
# Response formatters
# ---------------------------------------------------------------------------

def _text(s: str) -> dict[str, Any]:
    """Format a string as an MCP/direct tool result."""
    return {"content": [{"type": "text", "text": s}]}


def _error(s: str) -> dict[str, Any]:
    """Format a string as an MCP/direct tool error result."""
    return {"content": [{"type": "text", "text": s}], "is_error": True}


# ---------------------------------------------------------------------------
# Executor wrapper
# ---------------------------------------------------------------------------

async def _in_executor(func, *args, **kwargs):
    """Run a blocking call in the default executor.

    Uses asyncio.get_running_loop() (not the deprecated alternative)
    to obtain the current event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


# ---------------------------------------------------------------------------
# Token error detection
# ---------------------------------------------------------------------------

def _is_token_error(err_str: str) -> bool:
    """Check if an error indicates an expired/revoked OAuth token."""
    lower = err_str.lower()
    return any(p in lower for p in _TOKEN_ERRORS)


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

async def _call_with_retry(func, *args, max_retries: int = 3, **kwargs):
    """Retry Google API calls on transient failures (429, 503, network errors).

    Token errors (expired/revoked) raise immediately as RuntimeError.
    Non-transient errors re-raise without retry.
    Uses exponential backoff: 2^attempt seconds (1s, 2s, 4s, ...).
    """
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            return await _in_executor(func, *args, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if _is_token_error(str(e)):
                raise RuntimeError(
                    f"Google OAuth token error: {e}. "
                    f"Run 'secretary auth' to re-authenticate."
                ) from e
            if any(p in err_str for p in _TRANSIENT) and attempt < max_retries - 1:
                backoff = 2.0 ** attempt  # exponential backoff: 1s, 2s, 4s
                await asyncio.sleep(backoff)
                last_err = e
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("Retry exhausted with no error to report")


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _extract_body(payload: dict, _depth: int = 0) -> str:
    """Extract text/plain body from a Gmail message payload.

    Recursively traverses multipart MIME payloads up to depth 10.
    """
    if _depth > 10:
        return "[Email body too deeply nested]"
    try:
        if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )
        for part in payload.get("parts", []):
            body = _extract_body(part, _depth + 1)
            if body:
                return body
    except Exception:
        return "[Failed to decode email body]"
    return ""


def _format_headers(headers: list[dict]) -> dict[str, str]:
    """Normalize email headers — case-insensitive matching of wanted fields.

    Returns dict with canonical keys: Subject, From, To, Date.
    """
    wanted = {"Subject", "From", "To", "Date"}
    wanted_lower = {w.lower(): w for w in wanted}
    result: dict[str, str] = {}
    for h in headers:
        name = h["name"]
        # Accept both canonical (Subject) and lowercase (subject) headers
        if name in wanted:
            result[name] = h["value"]
        elif name.lower() in wanted_lower:
            result[wanted_lower[name.lower()]] = h["value"]
    return result


def _validate_email(email: str) -> str | None:
    """Return an error message if the email is invalid, else None."""
    if not _EMAIL_RE.match(email):
        return f"Invalid email address: {email}"
    return None


def _validate_body(body: str) -> str | None:
    """Return an error message if the email body is too large, else None."""
    encoded_size = len(body.encode("utf-8"))
    if encoded_size > _MAX_EMAIL_BODY_BYTES:
        return f"Email body too large ({encoded_size} bytes, max {_MAX_EMAIL_BODY_BYTES})"
    return None


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------

def _format_event(event: dict) -> str:
    """Format a Google Calendar event for display."""
    summary = event.get("summary", "(no title)")
    start = event.get("start", {})
    end = event.get("end", {})
    start_str = start.get("dateTime", start.get("date", "?"))
    end_str = end.get("dateTime", end.get("date", "?"))
    location = event.get("location", "")
    parts = [f"• {summary}", f"  When: {start_str} → {end_str}"]
    if location:
        parts.append(f"  Where: {location}")
    desc = event.get("description", "")
    if desc:
        parts.append(f"  Details: {desc[:200]}")
    parts.append(f"  ID: {event.get('id', '')}")
    return "\n".join(parts)
