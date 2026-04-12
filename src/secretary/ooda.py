"""OODA decision loop — Observe, Orient, Decide, Act.

When the event bus detects real-world changes (new emails, calendar updates,
file modifications), the OODA loop asks a cheap LLM to assess the situation
and decide which ad-hoc tasks to generate — dynamically, not from static YAML.

This is the bridge between "react to predefined triggers" (event bus) and
"reason about what's happening and decide what to do" (true autonomy).

Architecture:
    Events  →  OODA prompt (Haiku)  →  structured JSON decisions  →  ad-hoc tasks
    (cheap)            ^                injected into watcher's task list
                context from:
                - event summaries
                - recent run_log (what just ran)
                - memory highlights

The planner uses Haiku (0.33x) for cost efficiency.

Example output::

    [
      {"prompt": "Read and triage the new urgent email from boss@company.com",
       "tier": "medium", "priority": 1},
      {"prompt": "Check if tomorrow's 9am meeting has updated materials",
       "tier": "low", "priority": 3}
    ]
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import anthropic

from .config import SecretaryConfig
from .event_bus import Event, EventBus
from .memory import MemoryStore
from .run_log import RunLog

log = logging.getLogger("secretary.ooda")

# The model used for planning decisions.  Haiku is cheapest (0.33x).
PLANNER_MODEL = "claude-haiku-4.5"
PLANNER_MAX_TOKENS = 1024

# System prompt for the OODA planner.
_PLANNER_SYSTEM = """\
You are a scheduler for an AI secretary that manages email, calendar, files, \
and tasks for a busy person.  Your job is to look at what just happened \
(events) and decide what tasks the secretary should do RIGHT NOW.

Rules:
1. Only generate tasks that respond to the events.  No busywork.
2. Each task needs: prompt (what to do), tier (low/medium/high), priority (1=urgent, 5=routine).
3. Be specific in prompts — include names, subjects, times from the events.
4. Don't duplicate tasks that already ran recently (check the recent history).
5. If nothing interesting happened, return an empty list.
6. Maximum 5 tasks per decision cycle.

Tiers:
- low: simple lookups, quick reads (cheap model)
- medium: email drafting, calendar management, moderate reasoning
- high: complex analysis, multi-step research, important decisions

Respond with ONLY a JSON array.  No explanation, no markdown fences.\
"""


def _build_ooda_prompt(
    events: list[Event],
    recent_log: list[dict[str, Any]],
    memory_summary: str,
) -> str:
    """Build the user prompt for the OODA planner."""
    parts: list[str] = []

    # Observe: what happened
    parts.append("## Events This Cycle")
    if events:
        for ev in events[:10]:
            parts.append(f"- [{ev.type}] {ev.summary}")
            raw = ev.payload.get("raw_text", "")
            if raw:
                parts.append(f"  Preview: {raw[:500]}")
    else:
        parts.append("- No new events detected.")

    # Orient: what context do we have
    parts.append("\n## Recent Task History (last 5)")
    if recent_log:
        for entry in recent_log[:5]:
            status = "PASS" if entry.get("success") else "FAIL"
            parts.append(f"- [{status}] {entry.get('task', '')[:100]}")
    else:
        parts.append("- No recent history.")

    if memory_summary:
        parts.append(f"\n## Key Memory\n{memory_summary[:500]}")

    # Decide: what should we do
    parts.append("\n## Your Decision")
    parts.append(
        "Based on the events above, generate a JSON array of tasks "
        "the secretary should execute now.  Return [] if nothing needs doing."
    )

    return "\n".join(parts)


async def run_ooda_cycle(
    event_bus: EventBus,
    run_log: RunLog,
    memory: MemoryStore,
    config: SecretaryConfig,
) -> list[dict[str, Any]]:
    """Run one OODA decision cycle.  Returns a list of ad-hoc task dicts.

    Each dict has: ``prompt``, ``tier``, ``priority``, and ``source: "ooda"``.
    Returns an empty list if the planner decides nothing needs doing,
    or if there are no events to reason about.
    """
    events = event_bus.events
    if not events:
        log.debug("OODA: no events, skipping decision cycle")
        return []

    # Gather context
    recent = run_log.recent(5)
    recent_dicts = [
        {"task": e.task, "success": e.success, "tier": e.tier}
        for e in recent
    ]
    mem_summary = ""
    if memory.short:
        mem_summary = "\n".join(
            f"- {m}" for m in list(memory.short)[-5:]
        )

    prompt = _build_ooda_prompt(events, recent_dicts, mem_summary)

    # Build the Anthropic client and make a single cheap call
    from .direct_agent import _build_client, AGENT_PREFIX

    client = _build_client(config)

    # Prepend few-shot prefix for tool-use priming
    messages: list[dict[str, Any]] = list(AGENT_PREFIX) + [
        {"role": "user", "content": prompt},
    ]

    start = time.monotonic()
    try:
        response = await asyncio.to_thread(
            _call_planner, client, messages,
        )
    except Exception as e:
        log.warning("OODA planner call failed: %s", e)
        return []
    elapsed = time.monotonic() - start

    # Parse the response
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
            break

    tasks = _parse_planner_response(text)
    log.info(
        "OODA: %d event(s) → %d task(s) generated (%.1fs, model=%s)",
        len(events), len(tasks), elapsed, PLANNER_MODEL,
    )
    return tasks


def _call_planner(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
) -> anthropic.types.Message:
    """Synchronous Haiku call — runs in thread via asyncio.to_thread."""
    with client.messages.stream(
        model=PLANNER_MODEL,
        max_tokens=PLANNER_MAX_TOKENS,
        system=_PLANNER_SYSTEM,
        messages=messages,
    ) as stream:
        return stream.get_final_message()


def _parse_planner_response(text: str) -> list[dict[str, Any]]:
    """Parse the planner's JSON response into task dicts.

    Tolerates markdown fences, trailing commas, and other LLM quirks.
    Returns empty list on any parse failure.
    """
    text = text.strip()
    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    if not text:
        return []

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("OODA: failed to parse planner response: %s", text[:200])
        return []

    if not isinstance(data, list):
        log.warning("OODA: planner returned non-list: %s", type(data).__name__)
        return []

    # Validate and normalize each task
    tasks: list[dict[str, Any]] = []
    valid_tiers = {"low", "medium", "high"}
    for item in data[:5]:  # cap at 5
        if not isinstance(item, dict):
            continue
        prompt = item.get("prompt", "")
        if not prompt or not isinstance(prompt, str):
            continue
        tier = item.get("tier", "medium")
        if tier not in valid_tiers:
            tier = "medium"
        priority = item.get("priority", 3)
        if not isinstance(priority, (int, float)) or priority < 1:
            priority = 3
        tasks.append({
            "prompt": prompt,
            "tier": tier,
            "priority": int(priority),
            "source": "ooda",
            "id": f"ooda-{len(tasks) + 1}",
        })

    return tasks
