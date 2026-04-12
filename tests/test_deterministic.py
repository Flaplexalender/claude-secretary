"""Tests for deterministic pipelines — zero-LLM code paths."""
from __future__ import annotations

import pytest

from secretary.deterministic import try_deterministic, _PATTERNS


# ── Pattern matching ──────────────────────────────────────────


@pytest.mark.parametrize("prompt", [
    "check my unread emails",
    "Check unread email",
    "show my inbox",
    "list new messages",
    "what's in my inbox",
    "any new emails?",
    "get my recent mail",
    "What are my unread emails",
])
def test_unread_email_patterns_match(prompt):
    matched = any(p.search(prompt) for p, _ in _PATTERNS if _ == "unread_emails")
    assert matched, f"Pattern should match: {prompt}"


@pytest.mark.parametrize("prompt", [
    "check my calendar",
    "show today's schedule",
    "what's on my calendar",
    "list my meetings",
    "any events today",
    "Check my agenda",
    "show my events",
])
def test_calendar_patterns_match(prompt):
    matched = any(p.search(prompt) for p, _ in _PATTERNS if _ == "calendar_today")
    assert matched, f"Pattern should match: {prompt}"


@pytest.mark.parametrize("prompt", [
    "how many unread emails do I have",
    "how many new messages",
])
def test_count_unread_patterns_match(prompt):
    matched = any(p.search(prompt) for p, _ in _PATTERNS if _ == "count_unread")
    assert matched, f"Pattern should match: {prompt}"


@pytest.mark.parametrize("prompt", [
    "Draft an email to Bob about the project",
    "Analyze the quarterly sales data",
    "Write a Python function that sorts",
    "Fix the bug in router.py",
    "Research the best approach to caching",
])
def test_complex_prompts_do_not_match(prompt):
    matched = any(p.search(prompt) for p, _ in _PATTERNS)
    assert not matched, f"Pattern should NOT match complex task: {prompt}"


# ── Handler execution ────────────────────────────────────────


@pytest.fixture
def mock_tools():
    """Build tool dicts with mock async functions."""
    async def gmail_search(args):
        return {"content": [{"type": "text", "text": "Found 3 messages:\n\nID: abc\n  Subject: Test\n  From: alice@x.com\n  Date: today"}]}

    async def calendar_today(args):
        return {"content": [{"type": "text", "text": "Today's events:\n- 10:00 AM: Standup\n- 2:00 PM: Review"}]}

    return {
        "gmail_search": {"func": gmail_search},
        "calendar_today": {"func": calendar_today},
    }


async def test_unread_emails_handler(mock_tools):
    result = await try_deterministic("check my unread emails", mock_tools)
    assert result is not None
    assert result.routing.tier == "deterministic"
    assert result.routing.model == "none"
    assert result.num_turns == 0
    assert result.cost_usd == 0.0
    assert "Found 3 messages" in result.text
    assert result.premium_requests == 0.0


async def test_calendar_today_handler(mock_tools):
    result = await try_deterministic("what's on my calendar", mock_tools)
    assert result is not None
    assert result.routing.tier == "deterministic"
    assert "Standup" in result.text
    assert result.num_turns == 0


async def test_count_unread_handler(mock_tools):
    result = await try_deterministic("how many unread emails", mock_tools)
    assert result is not None
    assert "3" in result.text


async def test_count_unread_zero():
    async def gmail_search(args):
        return {"content": [{"type": "text", "text": "No messages found for: is:unread"}]}

    tools = {"gmail_search": {"func": gmail_search}}
    result = await try_deterministic("how many unread emails", tools)
    assert result is not None
    assert "0" in result.text


async def test_no_match_returns_none(mock_tools):
    result = await try_deterministic("Draft an email to Bob about the project", mock_tools)
    assert result is None


async def test_missing_tool_returns_none():
    """When required tool isn't in registry, handler should decline."""
    result = await try_deterministic("check my unread emails", {})
    assert result is None


async def test_handler_error_returns_none(mock_tools):
    """If a handler raises, try_deterministic returns None (fall through to LLM)."""
    async def broken_search(args):
        raise RuntimeError("API error")

    tools = {"gmail_search": {"func": broken_search}}
    result = await try_deterministic("check my unread emails", tools)
    assert result is None


async def test_quality_score_is_perfect(mock_tools):
    result = await try_deterministic("check my unread emails", mock_tools)
    assert result is not None
    assert result.quality_score == 1.0


async def test_tools_used_records_handler(mock_tools):
    result = await try_deterministic("check my unread emails", mock_tools)
    assert result is not None
    assert "unread_emails" in result.tools_used
