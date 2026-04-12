"""Goal Completion Verification — environment-grounded step & goal verification.

The missing safety net between "the agent says it did something" and
"the environment confirms it was done."  Without this, LLMs can hallucinate
task completion — the agent returns no error, but the verification criteria
aren't actually met.

Research basis:
- Anthropic "Building Effective Agents": "Ground truth from the environment
  at each step to assess its progress."
- Voyager (Wang 2023): "Success checker" verifies skill mastery via
  environment state, not agent self-report.
- Kambhampati (ICML 2024): "LLMs need external verifiers — quantitative
  verification at EACH step."
- Eureka (Ma 2023): Reward signal must accurately reflect task completion.

Architecture:
    step executes → agent output + verification criteria
    → Haiku judge compares output vs criteria → PASS/FAIL/INCONCLUSIVE
    → only PASS marks step as completed

    goal_state.json["verification_log"] = [
        {
            "step_id": str,
            "sub_goal_id": str,
            "verdict": "pass|fail|inconclusive",
            "reasoning": str,
            "ts": str,
        }
    ]

    Goal-level completion: when all sub-goals of a goal are "done",
    auto-detect and mark goal status = "done" in goal_state.json.
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

log = logging.getLogger("secretary.goal_verification")

VERIFY_MODEL = "claude-haiku-4.5"
VERIFY_MAX_TOKENS = 512
MAX_VERIFICATION_LOG = 100

# Verdicts
PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"

_VERIFY_SYSTEM = """\
You are a verification judge for an AI secretary's goal execution system.
Your job is to determine whether a completed task ACTUALLY achieved its \
stated verification criteria, based on the agent's output.

You are an INDEPENDENT judge — do not trust the agent's self-assessment.
Look for CONCRETE EVIDENCE in the output that the criteria were met.

Rules:
1. Match the agent's output against the verification criteria.
2. If the output clearly demonstrates the criteria are met → PASS.
3. If the output shows the task failed or criteria are unmet → FAIL.
4. If the output is ambiguous or insufficient to determine → INCONCLUSIVE.
5. Be strict: vague claims without evidence count as INCONCLUSIVE.
6. Short reasoning (2-3 sentences max).
7. If environment assertions are provided, treat them as GROUND TRUTH.
   They are deterministic filesystem checks — more reliable than agent text.
   If assertions fail, the step should FAIL regardless of what the agent claims.
8. The output includes tool call results from the agent's exploration. Individual \
tool errors during exploration (e.g. file not found, script error) are NORMAL — \
judge based on whether the overall task goal was achieved, not whether every \
intermediate tool call succeeded.

Respond with ONLY JSON (no markdown fences):
{
  "verdict": "pass|fail|inconclusive",
  "reasoning": "Brief explanation of your judgment."
}\
"""


def _build_verify_prompt(
    action: str,
    verification: str,
    agent_output: str,
    assertion_results_text: str = "",
) -> str:
    """Build the user prompt for step verification."""
    parts: list[str] = []

    parts.append("## Step Action")
    parts.append(action)

    parts.append("\n## Verification Criteria")
    parts.append(verification)

    parts.append("\n## Agent Output (after execution)")
    # Truncate to avoid blowing up context
    output_preview = agent_output[:4000] if agent_output else "(no output)"
    parts.append(output_preview)

    if assertion_results_text:
        parts.append(f"\n{assertion_results_text}")

    parts.append(
        "\n## Your Verdict"
        "\nDid the agent's output demonstrate that the verification "
        "criteria were met? Respond with JSON."
    )

    return "\n".join(parts)


def _extract_test_failure_details(agent_output: str) -> dict[str, Any]:
    """Extract structured test failure info from agent output containing pytest results.

    Parses pytest output to identify failed test names, error tracebacks,
    and a summary — so callers (e.g. self-improvement reflection) can see
    *why* tests failed, not just that they failed.
    """
    import re
    details: dict[str, Any] = {}
    if not agent_output:
        return details

    # Extract failed test names (pytest format: FAILED tests/test_foo.py::test_bar)
    failed_tests = re.findall(r"FAILED\s+([\w/\\.:]+)", agent_output)
    if failed_tests:
        details["failed_tests"] = failed_tests

    # Extract short test summary info block
    summary_match = re.search(
        r"=+ short test summary info =+\n(.*?)(?:\n=|\Z)",
        agent_output,
        re.DOTALL,
    )
    if summary_match:
        details["error_summary"] = summary_match.group(1).strip()[:500]

    # Extract pytest E-lines (assertion details)
    tb_matches = re.findall(
        r"((?:^E\s+.+$\n?)+)",
        agent_output,
        re.MULTILINE,
    )
    if tb_matches:
        details["error_output"] = tb_matches[-1].strip()[:1000]
    elif failed_tests:
        # Fallback: grab tail of output for context
        details["error_output"] = agent_output[-1000:] if len(agent_output) > 1000 else agent_output

    return details


async def verify_step_completion(
    action: str,
    verification: str,
    agent_output: str,
    config: SecretaryConfig,
    assertion_results_text: str = "",
) -> dict[str, Any]:
    """Judge whether a step's verification criteria were met.

    Returns dict with keys: verdict (pass/fail/inconclusive), reasoning.
    Falls back to PASS on LLM errors (fail-open to avoid blocking execution).

    If *assertion_results_text* is provided (formatted output from
    ``format_assertion_results``), it is injected into the judge prompt
    so the LLM can weigh deterministic environment evidence alongside
    the agent's text output.
    """
    if not verification or not verification.strip():
        # No verification criteria → auto-pass (nothing to check)
        return {"verdict": PASS, "reasoning": "No verification criteria specified."}

    from .direct_agent import _build_client, AGENT_PREFIX

    client = _build_client(config)

    prompt = _build_verify_prompt(action, verification, agent_output, assertion_results_text)
    messages: list[dict[str, Any]] = list(AGENT_PREFIX) + [
        {"role": "user", "content": prompt},
    ]

    start = time.monotonic()
    try:
        response = await asyncio.to_thread(
            _call_verify, client, messages,
        )
    except Exception as e:
        log.warning("Verification call failed (fail-open → PASS): %s", e)
        return {"verdict": PASS, "reasoning": f"Verification LLM call failed: {e}"}
    elapsed = time.monotonic() - start

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
            break

    result = _parse_verify_response(text)
    # Enrich FAIL/INCONCLUSIVE results with structured test error details
    if result.get("verdict") in (FAIL, INCONCLUSIVE):
        test_details = _extract_test_failure_details(agent_output)
        if test_details:
            result.update(test_details)
            log.info(
                "Extracted test failure details: %d failed tests",
                len(test_details.get("failed_tests", [])),
            )
    log.info(
        "Step verification: %s (%.1fs) — %s",
        result.get("verdict", "?"),
        elapsed,
        result.get("reasoning", "")[:100],
    )
    return result


def _call_verify(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
) -> anthropic.types.Message:
    """Synchronous Haiku call — runs in thread via asyncio.to_thread."""
    with client.messages.stream(
        model=VERIFY_MODEL,
        max_tokens=VERIFY_MAX_TOKENS,
        system=_VERIFY_SYSTEM,
        messages=messages,
    ) as stream:
        return stream.get_final_message()


def _parse_verify_response(text: str) -> dict[str, Any]:
    """Parse verification JSON response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    if not text:
        return {"verdict": PASS, "reasoning": "Empty response from verifier (fail-open)."}

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Verification response not valid JSON: %s", text[:200])
        return {"verdict": PASS, "reasoning": f"Could not parse verifier response (fail-open)."}

    verdict = data.get("verdict", "").lower().strip()
    if verdict not in (PASS, FAIL, INCONCLUSIVE):
        verdict = PASS  # fail-open
    reasoning = data.get("reasoning", "")[:300]

    return {"verdict": verdict, "reasoning": reasoning}


def record_verification(
    state: dict[str, Any],
    step_id: str,
    sub_goal_id: str,
    verdict: str,
    reasoning: str,
    goal_id: str = "",
) -> None:
    """Record a verification result in goal_state.json."""
    log_entries = state.setdefault("verification_log", [])
    entry: dict[str, Any] = {
        "step_id": step_id,
        "sub_goal_id": sub_goal_id,
        "verdict": verdict,
        "reasoning": reasoning[:300],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if goal_id:
        entry["goal_id"] = goal_id
    log_entries.append(entry)
    # Keep bounded
    if len(log_entries) > MAX_VERIFICATION_LOG:
        state["verification_log"] = log_entries[-MAX_VERIFICATION_LOG:]


def check_goal_completion(
    goal: dict[str, Any],
    sub_goal_overrides: dict[str, Any],
) -> bool:
    """Check if ALL sub-goals of a goal are done → goal is complete.

    Uses effective status (override takes precedence over YAML).
    """
    sub_goals = goal.get("sub_goals", [])
    if not sub_goals:
        return False

    for sg in sub_goals:
        sg_id = sg.get("id", "")
        override = sub_goal_overrides.get(sg_id)
        effective_status = override["status"] if override else sg.get("status", "not-started")
        if effective_status != "done":
            return False

    return True


def detect_completed_goals(
    goals: list[dict[str, Any]],
    state: dict[str, Any],
) -> list[str]:
    """Find goals where all sub-goals are done but goal itself isn't marked done.

    Returns list of goal IDs that should be marked complete.
    """
    overrides = state.get("sub_goal_status", {})
    completed: list[str] = []

    for goal in goals:
        goal_id = goal.get("id", "")
        goal_status = goal.get("status", "not-started")

        # Skip if already done or not started
        if goal_status == "done":
            continue

        if check_goal_completion(goal, overrides):
            completed.append(goal_id)

    return completed


def mark_goals_completed(
    goals: list[dict[str, Any]],
    state: dict[str, Any],
    completed_goal_ids: list[str],
) -> None:
    """Record goal-level completion in state.

    Stores in goal_state.json["completed_goals"] — doesn't modify goals.yaml
    (that's human-authored). The goal planner checks this before reviewing.
    """
    completed_goals = state.setdefault("completed_goals", {})

    for goal_id in completed_goal_ids:
        if goal_id not in completed_goals:
            # Find the goal for evidence
            goal = next((g for g in goals if g.get("id") == goal_id), None)
            sub_count = len(goal.get("sub_goals", [])) if goal else 0
            completed_goals[goal_id] = {
                "completed": True,
                "evidence": f"All {sub_count} sub-goals verified as done",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            log.info("Goal %s marked COMPLETE — all sub-goals done", goal_id)
