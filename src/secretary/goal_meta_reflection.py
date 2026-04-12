"""Cross-Goal Meta-Reflection Engine — Layer 24.

Inspired by Generative Agents (Park et al. 2023): synthesize observations
across ALL goals to identify cross-cutting patterns invisible to per-goal
reflection.

Per-goal reflection (goal_reflection.py) asks: "how did THIS goal's tasks do?"
Meta-reflection asks: "what patterns emerge ACROSS all goals?"

    recent observations  →  3 salient questions  →  answer  →  meta_reflections
       (per-goal)              (cross-cutting)       (LLM)    (goal_state.json)

Detects:
- Cross-goal dependencies (failure cascades, shared blockers)
- Temporal patterns (time-of-day, day-of-week effects)
- Systematic issues (auth, network, policy, tool limitations)
- Trust trend anomalies (multiple goals declining simultaneously)

Stored in goal_state.json["meta_reflections"] (last 5 kept).
Injected into goal planner prompt to inform cross-goal awareness.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

from .config import SecretaryConfig
from .goals import GoalStore
from .run_log import RunLog

log = logging.getLogger("secretary.goal_meta_reflection")

META_REFLECTION_MODEL = "claude-haiku-4.5"
META_REFLECTION_MAX_TOKENS = 2048
MAX_META_REFLECTIONS = 5

_META_SYSTEM = """\
You are the meta-reflection engine for an AI secretary that manages multiple \
long-horizon goals simultaneously. Your job is to synthesise observations \
ACROSS ALL goals and find cross-cutting patterns that per-goal reflection misses.

Think like a systems analyst: you see the whole board, not just one piece.

Process:
1. Review the cross-goal observations provided.
2. Generate 3 salient questions about patterns you notice.
3. Answer each question with specific evidence.
4. Summarise actionable cross-goal patterns.

Respond with ONLY JSON (no markdown fences):
{
  "questions": [
    "Question 1 about a cross-goal pattern",
    "Question 2 about a cross-goal pattern",
    "Question 3 about a cross-goal pattern"
  ],
  "answers": [
    "Answer 1 with evidence from specific goals",
    "Answer 2 with evidence from specific goals",
    "Answer 3 with evidence from specific goals"
  ],
  "cross_patterns": [
    {
      "pattern": "Short description of the cross-cutting pattern",
      "affected_goals": ["goal_id_1", "goal_id_2"],
      "severity": "low|medium|high",
      "recommendation": "Concrete action to take"
    }
  ],
  "summary": "1-2 sentence overall assessment of cross-goal health"
}\
"""


def _build_meta_prompt(
    goal_store: GoalStore,
    run_log: RunLog,
) -> str | None:
    """Build the cross-goal observation prompt.

    Returns None if insufficient data for useful meta-reflection.
    """
    parts: list[str] = []
    state = goal_store._state

    # --- Per-goal reflections (the primary input) ---
    reflections = state.get("reflections", [])
    if reflections:
        parts.append("## Per-Goal Reflections (recent)")
        for ref in reflections[-5:]:
            parts.append(f"- {ref.get('reflection', '')[:300]}")
            patterns = ref.get("patterns", {})
            failing = patterns.get("failing", [])
            if failing:
                parts.append(f"  Failing: {'; '.join(f[:100] for f in failing[:3])}")
            working = patterns.get("working", [])
            if working:
                parts.append(f"  Working: {'; '.join(w[:100] for w in working[:3])}")

    # --- Trust snapshots (trend data) ---
    snapshots = state.get("trust_snapshots", [])
    if len(snapshots) >= 2:
        parts.append("\n## Trust Score Trends")
        latest = snapshots[-1].get("scores", {})
        previous = snapshots[-2].get("scores", {})
        for gid in sorted(set(latest) | set(previous)):
            cur = latest.get(gid, 0.0)
            prev = previous.get(gid, 0.0)
            delta = cur - prev
            arrow = "\u2191" if delta > 0.05 else ("\u2193" if delta < -0.05 else "\u2192")
            parts.append(f"- {gid}: {prev:.2f} \u2192 {cur:.2f} ({arrow} {delta:+.2f})")

    # --- Execution reports (cycle-level summaries) ---
    reports = state.get("execution_reports", [])
    if reports:
        parts.append("\n## Recent Execution Reports")
        for report in reports[-3:]:
            gen = report.get("tasks_generated", 0)
            exc = report.get("tasks_executed", 0)
            v_pass = report.get("verification_pass", 0)
            v_fail = report.get("verification_fail", 0)
            parts.append(
                f"- Cycle {report.get('cycle', '?')}: "
                f"generated={gen}, executed={exc}, "
                f"verify_pass={v_pass}, verify_fail={v_fail}"
            )

    # --- Verification failures (cross-goal) ---
    v_log = state.get("verification_log", [])
    failures = [v for v in v_log[-20:] if v.get("verdict") == "fail"]
    if failures:
        parts.append("\n## Recent Verification Failures")
        for f in failures[-5:]:
            gid = f.get("goal_id", "?")
            reasoning = f.get("reasoning", "")[:150]
            parts.append(f"- [{gid}] {reasoning}")

    # --- Graduation events ---
    grad_hist = state.get("graduation_history", [])
    if grad_hist:
        parts.append("\n## Graduation Events")
        for ev in grad_hist[-3:]:
            parts.append(
                f"- {ev.get('action', '?')}: "
                f"{ev.get('old_level', '?')} \u2192 {ev.get('new_level', '?')} "
                f"(cycle {ev.get('cycle', '?')})"
            )

    # --- Run log: goal-originated tasks ---
    recent = run_log.recent(50)
    goal_entries = [e for e in recent if e.source == "goals"]
    if goal_entries:
        parts.append("\n## Goal Task Outcomes (recent)")
        by_goal: dict[str, list[str]] = {}
        for e in goal_entries[-15:]:
            gid = e.goal_id or "unknown"
            status = "PASS" if e.success else "FAIL"
            by_goal.setdefault(gid, []).append(f"{status}: {e.task[:80]}")
        for gid, entries in sorted(by_goal.items()):
            parts.append(f"\n  [{gid}]")
            for entry in entries[-5:]:
                parts.append(f"    - {entry}")

    # --- Active goals summary ---
    parts.append("\n## Active Goals")
    for goal in goal_store.goals:
        gid = goal.get("id", "?")
        desc = goal.get("description", "")[:100]
        parts.append(f"- [{gid}] {desc}")

    # Need at least some data to be useful
    if not reflections and not reports and not goal_entries:
        return None

    parts.append(
        "\n## Your Analysis"
        "\nSynthesise across ALL the goals above. "
        "What cross-cutting patterns emerge? "
        "Are there shared blockers, temporal patterns, or cascading failures? "
        "Generate 3 salient questions and answer them with evidence."
    )

    return "\n".join(parts)


def _call_meta_reflection(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
) -> anthropic.types.Message:
    """Synchronous Haiku call for meta-reflection."""
    with client.messages.stream(
        model=META_REFLECTION_MODEL,
        max_tokens=META_REFLECTION_MAX_TOKENS,
        system=_META_SYSTEM,
        messages=messages,
    ) as stream:
        return stream.get_final_message()


def _parse_meta_response(text: str) -> dict[str, Any]:
    """Parse meta-reflection JSON response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
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
        # Close any open string
        if patched.count('"') % 2 == 1:
            patched += '"'
        # Close open arrays/objects
        for ch in reversed(patched):
            if ch in "{}[]":
                break
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
            log.warning("Meta-reflection: failed to parse response: %s", text[:200])
            return {}

    if not isinstance(data, dict):
        return {}

    result: dict[str, Any] = {}

    # Questions and answers
    questions = data.get("questions", [])
    answers = data.get("answers", [])
    if isinstance(questions, list):
        result["questions"] = [str(q)[:300] for q in questions[:3]]
    else:
        result["questions"] = []
    if isinstance(answers, list):
        result["answers"] = [str(a)[:500] for a in answers[:3]]
    else:
        result["answers"] = []

    # Cross-goal patterns
    raw_patterns = data.get("cross_patterns", [])
    patterns: list[dict[str, Any]] = []
    if isinstance(raw_patterns, list):
        valid_severities = {"low", "medium", "high"}
        for item in raw_patterns[:5]:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity", "low"))
            if severity not in valid_severities:
                severity = "low"
            affected = item.get("affected_goals", [])
            if not isinstance(affected, list):
                affected = []
            patterns.append({
                "pattern": str(item.get("pattern", ""))[:200],
                "affected_goals": [str(g)[:50] for g in affected[:10]],
                "severity": severity,
                "recommendation": str(item.get("recommendation", ""))[:200],
            })
    result["cross_patterns"] = patterns

    result["summary"] = str(data.get("summary", ""))[:500]

    return result


async def run_meta_reflection(
    goal_store: GoalStore,
    run_log: RunLog,
    config: SecretaryConfig,
) -> dict[str, Any]:
    """Run cross-goal meta-reflection.

    Returns the parsed meta-reflection dict, or empty dict if skipped.
    Stores result in goal_state.json["meta_reflections"].
    """
    prompt = _build_meta_prompt(goal_store, run_log)
    if prompt is None:
        log.debug("Meta-reflection: insufficient data, skipping")
        return {}

    from .direct_agent import _build_client, AGENT_PREFIX

    client = _build_client(config)

    messages: list[dict[str, Any]] = list(AGENT_PREFIX) + [
        {"role": "user", "content": prompt},
    ]

    start = time.monotonic()
    try:
        response = await asyncio.to_thread(
            _call_meta_reflection, client, messages,
        )
    except Exception as e:
        log.warning("Meta-reflection call failed: %s", e)
        return {}
    elapsed = time.monotonic() - start

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
            break

    result = _parse_meta_response(text)
    if not result:
        return {}

    # Store in goal_state.json
    meta_reflections = goal_store._state.setdefault("meta_reflections", [])
    meta_reflections.append({
        **result,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    # Keep only last N
    if len(meta_reflections) > MAX_META_REFLECTIONS:
        goal_store._state["meta_reflections"] = meta_reflections[
            -MAX_META_REFLECTIONS:
        ]

    goal_store.save_state()

    pattern_count = len(result.get("cross_patterns", []))
    high_sev = sum(
        1 for p in result.get("cross_patterns", [])
        if p.get("severity") == "high"
    )
    log.info(
        "Meta-reflection: %d question(s), %d cross-pattern(s) "
        "(%d high severity) (%.1fs)",
        len(result.get("questions", [])),
        pattern_count,
        high_sev,
        elapsed,
    )
    return result


def format_meta_reflection_section(state: dict[str, Any]) -> str:
    """Format meta-reflections for CLI display or prompt injection."""
    meta = state.get("meta_reflections", [])
    if not meta:
        return "No cross-goal meta-reflections recorded."

    parts: list[str] = []
    latest = meta[-1]

    parts.append("## Cross-Goal Meta-Reflection (latest)")
    parts.append(f"  Summary: {latest.get('summary', 'N/A')}")

    questions = latest.get("questions", [])
    answers = latest.get("answers", [])
    if questions:
        parts.append("  Salient questions:")
        for i, q in enumerate(questions):
            parts.append(f"    Q{i+1}: {q}")
            if i < len(answers):
                parts.append(f"    A{i+1}: {answers[i][:200]}")

    patterns = latest.get("cross_patterns", [])
    if patterns:
        parts.append("  Cross-goal patterns:")
        for p in patterns:
            sev = p.get("severity", "?")
            icon = {"high": "\u26a0\ufe0f", "medium": "\u26ab", "low": "\u2022"}.get(
                sev, "\u2022",
            )
            goals = ", ".join(p.get("affected_goals", []))
            parts.append(f"    {icon} [{sev}] {p.get('pattern', '?')}")
            if goals:
                parts.append(f"      Affects: {goals}")
            rec = p.get("recommendation", "")
            if rec:
                parts.append(f"      Rec: {rec}")

    parts.append(f"  Total meta-reflections: {len(meta)}")

    return "\n".join(parts)


def format_meta_for_prompt(state: dict[str, Any]) -> str:
    """Format the latest meta-reflection for injection into the goal planner prompt."""
    meta = state.get("meta_reflections", [])
    if not meta:
        return ""

    latest = meta[-1]
    parts: list[str] = []
    parts.append("## Cross-Goal Patterns (from meta-reflection)")
    parts.append(
        "(Patterns observed ACROSS all your goals. "
        "Consider these when generating tasks.)"
    )

    summary = latest.get("summary", "")
    if summary:
        parts.append(f"\n**Overall**: {summary}")

    patterns = latest.get("cross_patterns", [])
    for p in patterns:
        sev = p.get("severity", "low")
        parts.append(
            f"\n- **[{sev}]** {p.get('pattern', '')}"
        )
        goals = p.get("affected_goals", [])
        if goals:
            parts.append(f"  Goals: {', '.join(goals)}")
        rec = p.get("recommendation", "")
        if rec:
            parts.append(f"  Action: {rec}")

    return "\n".join(parts)
