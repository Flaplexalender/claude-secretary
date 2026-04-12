"""Goal Reflection Engine — verbal feedback loop for long-horizon goals.

Inspired by Reflexion (Shinn et al. 2023) and TextGrad (Yuksekgonul et al. 2024).
After goal-originated tasks execute, this module analyzes outcomes and generates
verbal reflections that feed into the NEXT goal review — closing the loop:

    goals → tasks → execution → **reflection** → better goals/tasks

Architecture:
    run_log (source="goals")  →  reflection prompt (Haiku)  →  stored reflections
        ↑                                                           ↓
    goal_state.json ←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←←

The reflection engine answers:
1. Which goal-tasks succeeded/failed and why?
2. What strategy adjustments should the planner make?
3. Which sub-goals should change status based on evidence?
4. What worked that should be repeated?

Reflections are stored in goal_state.json["reflections"] and injected into
the goal planner prompt on the next review cycle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import anthropic

from .config import SecretaryConfig
from .goals import GoalStore
from .run_log import RunLog, RunLogEntry

log = logging.getLogger("secretary.goal_reflection")

REFLECTION_MODEL = "claude-haiku-4.5"
REFLECTION_MAX_TOKENS = 2048

_REFLECTION_SYSTEM = """\
You are the reflection engine for an AI secretary's strategic goal system.
Your job is to analyze the outcomes of goal-driven tasks and produce verbal \
feedback that will help the goal planner generate BETTER tasks next time.

Think like a coach reviewing game tape: what worked, what failed, and why?

Rules:
1. Be specific — reference actual task prompts and outcomes.
2. Identify PATTERNS, not just individual failures.
3. Suggest concrete strategy adjustments (not vague advice).
4. If a sub-goal seems complete/blocked based on evidence, say so.
5. Keep reflections concise — they'll be injected into the next planning prompt.

Respond with ONLY JSON (no markdown fences):
{
  "reflection": "2-4 sentence summary of what happened and what to change.",
  "strategy_adjustments": [
    "Specific adjustment 1",
    "Specific adjustment 2"
  ],
  "status_updates": [
    {"sub_goal_id": "...", "new_status": "done|blocked|in-progress", "evidence": "..."}
  ],
  "patterns": {
    "working": ["What's succeeding and should continue"],
    "failing": ["What's failing and should be changed"]
  }
}\
"""

MAX_REFLECTIONS = 10  # Keep last N reflections in state


def _get_goal_task_outcomes(
    run_log: RunLog,
    since_entries: int = 50,
) -> list[RunLogEntry]:
    """Get recent run_log entries that originated from the goal planner."""
    recent = run_log.recent(since_entries)
    return [e for e in recent if e.source == "goals"]


def _build_reflection_prompt(
    goal_outcomes: list[RunLogEntry],
    goals: list[dict[str, Any]],
    previous_reflections: list[dict[str, Any]],
) -> str:
    """Build the user prompt for the reflection engine."""
    parts: list[str] = []

    # Task outcomes
    parts.append("## Goal-Task Outcomes (most recent)")
    if goal_outcomes:
        for entry in goal_outcomes[-10:]:
            status = "SUCCESS" if entry.success else "FAILED"
            goal_tag = f" [goal: {entry.goal_id}]" if entry.goal_id else ""
            parts.append(f"- [{status}]{goal_tag} {entry.task[:150]}")
            if entry.error:
                parts.append(f"  Error: {entry.error[:200]}")
            if entry.output_preview and entry.success:
                parts.append(f"  Output: {entry.output_preview[:200]}")
        # Stats
        total = len(goal_outcomes)
        passed = sum(1 for e in goal_outcomes if e.success)
        parts.append(f"\nSuccess rate: {passed}/{total} ({100*passed/total:.0f}%)")
    else:
        parts.append("- No goal-originated tasks have been executed yet.")
        parts.append("\nThis is the first reflection — focus on what the planner should try.")

    # Goal context (brief)
    parts.append("\n## Active Goals")
    for goal in goals:
        gid = goal.get("id", "?")
        desc = goal.get("description", "")
        status = goal.get("status", "?")
        parts.append(f"- [{gid}] {desc} — {status}")

    # Previous reflections (for continuity)
    if previous_reflections:
        parts.append("\n## Previous Reflections")
        for ref in previous_reflections[-3:]:
            if isinstance(ref, dict):
                parts.append(f"- {ref.get('reflection', '')[:200]}")
            else:
                parts.append(f"- {str(ref)[:200]}")

    parts.append("\n## Your Reflection")
    parts.append(
        "Analyze the outcomes above. What patterns do you see? "
        "What should the goal planner do differently next time? "
        "Be specific and action-oriented."
    )

    return "\n".join(parts)


async def run_goal_reflection(
    goal_store: GoalStore,
    run_log: RunLog,
    config: SecretaryConfig,
) -> dict[str, Any]:
    """Run a reflection cycle on goal-task outcomes.

    Returns the parsed reflection dict, or empty dict if nothing to reflect on.
    Also stores the reflection in goal_state.json for future use.
    """
    goal_outcomes = _get_goal_task_outcomes(run_log)

    # Even with no outcomes, generate a "bootstrap" reflection if we have goals
    # but haven't reflected yet (helps the planner on first run)
    previous_reflections = goal_store._state.get("reflections", [])
    if not goal_outcomes and previous_reflections:
        log.debug("Reflection: no new goal-task outcomes and already reflected, skipping")
        return {}

    prompt = _build_reflection_prompt(
        goal_outcomes,
        goal_store.goals,
        previous_reflections,
    )

    from .direct_agent import _build_client, AGENT_PREFIX

    client = _build_client(config)

    messages: list[dict[str, Any]] = list(AGENT_PREFIX) + [
        {"role": "user", "content": prompt},
    ]

    start = time.monotonic()
    try:
        response = await asyncio.to_thread(
            _call_reflection, client, messages,
        )
    except Exception as e:
        log.warning("Goal reflection call failed: %s", e)
        return {}
    elapsed = time.monotonic() - start

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
            break

    result = _parse_reflection_response(text)
    if not result:
        return {}

    # Apply status updates
    status_updates = result.get("status_updates", [])
    if status_updates:
        goal_store.apply_updates(status_updates)

    # Store reflection
    reflections = goal_store._state.setdefault("reflections", [])
    reflections.append({
        "reflection": result.get("reflection", ""),
        "strategy_adjustments": result.get("strategy_adjustments", []),
        "patterns": result.get("patterns", {}),
        "task_count": len(goal_outcomes),
        "success_rate": (
            sum(1 for e in goal_outcomes if e.success) / len(goal_outcomes)
            if goal_outcomes else 0.0
        ),
        "ts": goal_store._state.get("last_reviewed", ""),
    })

    # Keep only last N reflections
    if len(reflections) > MAX_REFLECTIONS:
        goal_store._state["reflections"] = reflections[-MAX_REFLECTIONS:]

    goal_store.save_state()

    log.info(
        "Goal reflection: %d outcome(s) analyzed, %d strategy adjustment(s), "
        "%d status update(s) (%.1fs)",
        len(goal_outcomes),
        len(result.get("strategy_adjustments", [])),
        len(status_updates),
        elapsed,
    )
    return result


def _call_reflection(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
) -> anthropic.types.Message:
    """Synchronous Haiku call — runs in thread via asyncio.to_thread."""
    with client.messages.stream(
        model=REFLECTION_MODEL,
        max_tokens=REFLECTION_MAX_TOKENS,
        system=_REFLECTION_SYSTEM,
        messages=messages,
    ) as stream:
        return stream.get_final_message()


def _parse_reflection_response(text: str) -> dict[str, Any]:
    """Parse reflection JSON response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try to extract JSON object if model added preamble text
    if text and not text.startswith("{"):
        brace = text.find("{")
        if brace != -1:
            text = text[brace:]

    # Find matching closing brace, respecting strings
    if text and text.startswith("{"):
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(text):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    text = text[: i + 1]
                    break

    if not text:
        return {}

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Truncated response — try closing open brackets/braces
        patched = text.rstrip()
        if patched.count('"') % 2 == 1:
            patched += '"'
        opens = []
        in_str = False
        esc = False
        for ch in patched:
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch in "{[":
                opens.append("}" if ch == "{" else "]")
            elif ch in "}]" and opens:
                opens.pop()
        patched += "".join(reversed(opens))
        try:
            data = json.loads(patched)
        except json.JSONDecodeError:
            log.warning("Goal reflection: failed to parse response: %s", text[:200])
            return {}

    if not isinstance(data, dict):
        return {}

    # Validate and sanitize
    result: dict[str, Any] = {}
    result["reflection"] = str(data.get("reflection", ""))[:500]

    adjustments = data.get("strategy_adjustments", [])
    if isinstance(adjustments, list):
        result["strategy_adjustments"] = [
            str(a)[:200] for a in adjustments[:5]
        ]
    else:
        result["strategy_adjustments"] = []

    # Status updates
    raw_updates = data.get("status_updates", [])
    valid_statuses = {"done", "in-progress", "blocked", "not-started"}
    updates: list[dict[str, Any]] = []
    if isinstance(raw_updates, list):
        for item in raw_updates[:10]:
            if not isinstance(item, dict):
                continue
            sub_id = item.get("sub_goal_id", "")
            new_status = item.get("new_status", "")
            if sub_id and new_status in valid_statuses:
                updates.append({
                    "sub_goal_id": sub_id,
                    "new_status": new_status,
                    "evidence": str(item.get("evidence", ""))[:200],
                })
    result["status_updates"] = updates

    # Patterns
    patterns = data.get("patterns", {})
    if isinstance(patterns, dict):
        result["patterns"] = {
            "working": [str(p)[:200] for p in patterns.get("working", [])[:5]],
            "failing": [str(p)[:200] for p in patterns.get("failing", [])[:5]],
        }
    else:
        result["patterns"] = {"working": [], "failing": []}

    return result
