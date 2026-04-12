"""Deterministic pipelines — zero-LLM code paths for simple tasks.

Some tasks don't need an LLM at all: "check unread emails", "what's on my
calendar today", "count unread messages".  These can be handled by pattern-
matching the prompt and directly calling the relevant tool function.

Saves ~100% of API cost for these tasks (0 tokens, 0 latency from LLM).
Falls back to None when no pattern matches → caller uses normal agent path.

Usage::

    result = await try_deterministic(prompt, tools, config)
    if result is not None:
        # Task handled without LLM
        return result
    # else: fall through to direct_agent.run()
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from .direct_agent import RunResult
from .router import RoutingDecision

log = logging.getLogger("secretary.deterministic")


# ── Pattern definitions ──────────────────────────────────────
# Each entry: (compiled_regex, handler_name)
# Patterns are tried in order; first match wins.

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Count unread (must come before unread_emails — more specific)
    (re.compile(
        r"^how many\s+(new |unread )?(email|mail|message)s?\b",
        re.I,
    ), "count_unread"),

    # Unread email check / inbox summary
    (re.compile(
        r"^(check|show|list|get|read|what(?:['’]?s| are| is)|any)\s+"
        r"(in\s+)?(my\s+)?(new |unread |recent )?(email|mail|inbox|message)s?\b",
        re.I,
    ), "unread_emails"),

    # Today's calendar / schedule
    (re.compile(
        r"^(check|show|list|get|what(?:['’]?s| is)|any)\s+"
        r"(on\s+|in\s+)?(my\s+)?(today(?:['']?s)?\s+)?(calendar|schedule|meeting|event|agenda)s?\b",
        re.I,
    ), "calendar_today"),
]


async def try_deterministic(
    prompt: str,
    tools: dict[str, Any],
    config: Any | None = None,
) -> RunResult | None:
    """Try to handle the prompt deterministically (no LLM).

    Returns a RunResult if handled, None if the prompt doesn't match
    any deterministic pattern (caller should use the normal agent path).
    """
    prompt_stripped = prompt.strip()

    for pattern, handler_name in _PATTERNS:
        if pattern.search(prompt_stripped):
            handler = _HANDLERS.get(handler_name)
            if handler is None:
                continue
            try:
                t0 = time.monotonic()
                text = await handler(prompt_stripped, tools)
                if text is None:
                    # Handler declined (e.g. tool not available)
                    continue
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                log.info(
                    "Deterministic pipeline: '%s' handled by %s (%dms)",
                    handler_name, handler_name, elapsed_ms,
                )
                return RunResult(
                    task=prompt_stripped,
                    routing=RoutingDecision(
                        tier="deterministic",
                        model="none",
                        max_turns=0,
                        max_budget_usd=0.0,
                        reason=f"deterministic:{handler_name}",
                        premium_multiplier=0.0,
                    ),
                    text=text,
                    num_turns=0,
                    duration_ms=elapsed_ms,
                    tools_used=[handler_name],
                    quality_score=1.0,
                )
            except Exception as e:
                log.warning("Deterministic handler '%s' failed: %s", handler_name, e)
                # Fall through to LLM path
                return None

    return None


# ── Handlers ─────────────────────────────────────────────────

def _extract_text(result: Any) -> str:
    """Pull text from tool result dict."""
    if isinstance(result, dict) and "content" in result:
        parts = [
            b.get("text", "")
            for b in result.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(result)


async def _handle_unread_emails(prompt: str, tools: dict[str, Any]) -> str | None:
    """List unread emails — direct gmail_search call."""
    if "gmail_search" not in tools:
        return None
    func = tools["gmail_search"]["func"]
    result = await func({"query": "is:unread newer_than:1d", "max_results": 10})
    return _extract_text(result)


async def _handle_calendar_today(prompt: str, tools: dict[str, Any]) -> str | None:
    """Show today's calendar — direct calendar_today call."""
    if "calendar_today" not in tools:
        return None
    func = tools["calendar_today"]["func"]
    result = await func({"max_results": 10})
    return _extract_text(result)


async def _handle_count_unread(prompt: str, tools: dict[str, Any]) -> str | None:
    """Count unread emails — gmail_search + count."""
    if "gmail_search" not in tools:
        return None
    func = tools["gmail_search"]["func"]
    result = await func({"query": "is:unread", "max_results": 50})
    text = _extract_text(result)
    # Parse "Found N messages" from the tool output
    m = re.search(r"Found (\d+) messages?", text)
    if m:
        count = int(m.group(1))
        return f"You have {count} unread email{'s' if count != 1 else ''}."
    if "No messages found" in text:
        return "You have 0 unread emails."
    # Couldn't parse — return raw
    return text


_HANDLERS: dict[str, Any] = {
    "unread_emails": _handle_unread_emails,
    "calendar_today": _handle_calendar_today,
    "count_unread": _handle_count_unread,
}
