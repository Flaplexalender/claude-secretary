"""Adaptive Replanning Engine — failure recovery for step plans.

When a decomposed step fails, don't just block — adapt.  This is Layer 7
of the planning architecture, addressing the core weakness identified by
research: "LLMs struggle to adjust plans when faced with unexpected errors"
(Weng 2023).

Research basis:
- Kambhampati (NeurIPS 2023, "LLM-Modulo"): External verifiers provide
  feedback and back-prompt the LLM for better plan generation.  Autonomous
  plan success rate is ~12%, but verifier-in-the-loop raises it dramatically.
- Reflexion (Shinn 2023): Failed trajectories + verbal reflection → retry
  with revised strategy.  Up to 3 reflections in working memory.
- LATS (Zhou 2023): Environment feedback → adaptive problem-solving via
  Monte Carlo Tree Search over action space.
- RAP (Hao 2023): LLM as world model, "anticipating future states and
  rewards, iteratively refining existing reasoning steps."
- Anthropic "Building Effective Agents": Evaluator-optimizer workflow —
  one LLM generates, another evaluates in a loop.

Strategy ladder (escalation on repeated failure):
  1. RETRY   — same step, flag error context (transient failures)
  2. REVISE  — Haiku rewrites step action using failure analysis
  3. RECOMPOSE — regenerate remaining steps from the sub-goal
  4. BLOCK   — mark sub-goal blocked, move to next goal

Budget: max 2 retries per step, max 1 recomposition per plan.

    goal_state.json["step_plans"][sub_goal_id] = {
        ...existing fields...,
        "retry_counts": {"<step_id>": int},   # per-step retry count
        "recompositions": int,                  # times the plan was recomposed
        "failure_log": [                        # failure analysis history
            {
                "step_id": str,
                "strategy": "retry|revise|recompose|block",
                "analysis": str,
                "ts": str,
            }
        ],
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

from .config import SecretaryConfig

log = logging.getLogger("secretary.goal_replanner")

REPLAN_MODEL = "claude-haiku-4.5"
REPLAN_MAX_TOKENS = 1024
MAX_RETRIES_PER_STEP = 2
MAX_RECOMPOSITIONS_PER_PLAN = 1

# ---------------------------------------------------------------------------
# Strategy decision
# ---------------------------------------------------------------------------

_ANALYSIS_SYSTEM = """\
You are a failure-analysis engine for an AI secretary's step plans.
A step in a plan just failed.  Your job: analyse WHY and recommend a recovery strategy.

Input: the step that failed, its action, its verification criteria, \
the failure evidence (output/error), and the broader plan context.

Respond with ONLY JSON (no markdown fences):
{
  "root_cause": "Brief analysis of why this step failed (1-2 sentences)",
  "is_transient": true/false,
  "is_capability_failure": true/false,
  "strategy": "retry|revise|recompose",
  "revised_action": "New action text IF strategy is 'revise' (null otherwise)",
  "revised_verification": "New verification IF strategy is 'revise' (null otherwise)",
  "revised_tier": "low|medium|high (only if strategy is 'revise', null otherwise)",
  "rationale": "Why this strategy is best (1 sentence)"
}

Guidelines:
- "retry": Use when the failure is transient (network error, timeout, \
race condition) and the same action should succeed on retry.
- "revise": Use when the approach was wrong or too vague.  Rewrite the \
action to fix the identified problem.  Be specific and concrete.
- "recompose": Use when the remaining plan is invalidated (e.g. a \
prerequisite assumption was wrong).  The whole plan from this step \
onward will be regenerated.
- NEVER choose "retry" if the failure evidence shows a logic error or \
missing capability — that needs "revise".
- If the step has already been retried 2+ times, prefer "revise" or "recompose".
- Set "is_capability_failure" to true when the failure shows the model \
lacked the intelligence or knowledge to complete the task correctly — \
e.g. used wrong OS commands (Linux on Windows), hallucinated directories, \
produced incoherent output, made basic reasoning errors, or was too \
weak for multi-step reasoning.  These failures won't be fixed by retrying \
with the same model tier.
- PLATFORM: This is a Windows system. If the step used Unix/bash commands \
(ls, cat, grep, find, etc.), that's a capability/knowledge failure.
"""

_RECOMPOSE_SYSTEM = """\
You are a task recomposition engine for an AI secretary.
A step plan partially completed, then a step failed and requires replanning.
Generate NEW steps to replace the remaining (incomplete) steps.

You receive: the sub-goal, completed steps (context), the failed step + \
failure analysis, and the original remaining steps.

Rules:
1. Each step: single focused action, verification criterion, tier.
2. Learn from what went wrong — don't repeat the same approach.
3. Keep total remaining steps to 2-5 (don't over-plan).
4. First new step should address the specific failure.
5. The agent has: file_read, file_write, file_edit, grep_search, \
run_command, run_python, Gmail/Calendar tools.
6. This runs on Windows (PowerShell). Use Windows-compatible commands \
(e.g. Get-ChildItem not ls, Get-Content not cat). Never use bash/Linux commands.
7. The project root is the current working directory. Source code is \
under src/secretary/. Tests are under tests/. Config is config.yaml.

Respond with ONLY JSON (no markdown fences):
{
  "steps": [
    {
      "action": "...",
      "verification": "...",
      "tier": "low|medium|high"
    }
  ],
  "rationale": "How this plan addresses the previous failure."
}\
"""


# ---------------------------------------------------------------------------
# Failure analysis
# ---------------------------------------------------------------------------


def _build_analysis_prompt(
    step: dict[str, Any],
    evidence: str,
    plan_context: dict[str, Any],
    retry_count: int,
) -> str:
    """Build the prompt for failure analysis."""
    parts: list[str] = []

    parts.append("## Failed Step")
    parts.append(f"Step ID: {step.get('step_id', '?')}")
    parts.append(f"Action: {step.get('action', '')}")
    parts.append(f"Verification: {step.get('verification', '')}")
    parts.append(f"Tier: {step.get('tier', 'medium')}")
    parts.append(f"Previous retries: {retry_count}")

    parts.append("\n## Failure Evidence")
    parts.append(evidence[:1500] if evidence else "(no output captured)")

    # Show completed steps for context
    steps = plan_context.get("steps", [])
    completed = [s for s in steps if s.get("status") == "completed"]
    if completed:
        parts.append("\n## Previously Completed Steps")
        for s in completed:
            parts.append(f"  - [{s.get('step_id', '?')}] {s.get('action', '')[:80]}")

    # Show remaining steps
    remaining = [s for s in steps if s.get("status") == "pending"]
    if remaining:
        parts.append(f"\n## Remaining Steps ({len(remaining)} pending)")
        for s in remaining:
            parts.append(f"  - [{s.get('step_id', '?')}] {s.get('action', '')[:80]}")

    return "\n".join(parts)


def _build_recompose_prompt(
    sub_goal: dict[str, Any],
    parent_goal: dict[str, Any],
    completed_steps: list[dict[str, Any]],
    failed_step: dict[str, Any],
    failure_analysis: str,
    original_remaining: list[dict[str, Any]],
) -> str:
    """Build prompt for plan recomposition."""
    parts: list[str] = []

    parts.append("## Sub-Goal")
    parts.append(f"ID: {sub_goal.get('id', '?')}")
    parts.append(f"Description: {sub_goal.get('description', '')}")

    parts.append("\n## Parent Goal")
    parts.append(f"Description: {parent_goal.get('description', '')}")

    if completed_steps:
        parts.append("\n## Completed Steps (keep as context)")
        for s in completed_steps:
            parts.append(f"  - [{s.get('step_id', '?')}] {s.get('action', '')[:80]} — DONE")

    parts.append("\n## Failed Step")
    parts.append(f"Action: {failed_step.get('action', '')}")
    parts.append(f"Failure analysis: {failure_analysis}")

    if original_remaining:
        parts.append(f"\n## Original Remaining Steps (now invalidated, {len(original_remaining)} steps)")
        for s in original_remaining:
            parts.append(f"  - {s.get('action', '')[:80]}")

    return "\n".join(parts)


_VALID_STRATEGIES = {"retry", "revise", "recompose"}


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """Parse JSON from LLM response, handling markdown fences.

    Validates:
    - strategy must be one of retry/revise/recompose
    - 'revise' strategy must include revised_action (falls back to 'retry' if missing)
    """
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        lines = lines[1:]  # drop ```json or ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    parsed = None
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        # Try extracting first JSON object
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(clean[start:end + 1])
            except json.JSONDecodeError:
                pass
    if parsed is None:
        return None
    # Validate strategy field
    strategy = parsed.get("strategy", "").lower().strip()
    if strategy not in _VALID_STRATEGIES:
        log.warning("Invalid strategy %r from LLM, falling back to 'retry'", strategy)
        parsed["strategy"] = "retry"
    # Validate revise requires revised_action
    if parsed["strategy"] == "revise" and not parsed.get("revised_action"):
        log.warning("'revise' strategy missing revised_action, falling back to 'retry'")
        parsed["strategy"] = "retry"
    return parsed


# ---------------------------------------------------------------------------
# Core replanning logic
# ---------------------------------------------------------------------------


def choose_strategy(
    state: dict[str, Any],
    sub_goal_id: str,
    step_id: str,
) -> str:
    """Decide replanning strategy based on retry/recompose budget.

    Returns: "retry", "revise", "recompose", or "block".
    This is the BUDGET check — the LLM analysis may override within budget.
    """
    plan = state.get("step_plans", {}).get(sub_goal_id, {})
    retry_counts = plan.get("retry_counts", {})
    retries = retry_counts.get(step_id, 0)
    recompositions = plan.get("recompositions", 0)

    if retries < MAX_RETRIES_PER_STEP:
        return "retry"  # Budget allows retry or revise
    if recompositions < MAX_RECOMPOSITIONS_PER_PLAN:
        return "recompose"
    return "block"


def apply_retry(
    state: dict[str, Any],
    sub_goal_id: str,
    step_id: str,
    revised_action: str | None = None,
    revised_verification: str | None = None,
    revised_tier: str | None = None,
) -> None:
    """Reset a failed step to pending (with optional revised action).

    Increments retry count.  If revised_action is provided, this is a
    "revise" strategy — the step action is rewritten.
    """
    plan = state.get("step_plans", {}).get(sub_goal_id)
    if not plan:
        return

    retry_counts = plan.setdefault("retry_counts", {})
    retry_counts[step_id] = retry_counts.get(step_id, 0) + 1

    for step in plan.get("steps", []):
        if step.get("step_id") == step_id:
            step["status"] = "pending"
            step.pop("result", None)
            step.pop("ts", None)
            if revised_action:
                step["action"] = revised_action
            if revised_verification:
                step["verification"] = revised_verification
            if revised_tier:
                step["tier"] = revised_tier
            break

    log.info(
        "Step %s reset to pending (retry #%d)%s",
        step_id,
        retry_counts[step_id],
        " with revised action" if revised_action else "",
    )


def apply_recompose(
    state: dict[str, Any],
    sub_goal_id: str,
    failed_step_id: str,
    new_steps: list[dict[str, Any]],
) -> None:
    """Replace remaining steps from the failed step onward with new steps.

    Preserves completed steps and increments the recomposition counter.
    """
    plan = state.get("step_plans", {}).get(sub_goal_id)
    if not plan:
        return

    old_steps = plan.get("steps", [])
    # Keep all completed steps before the failed one
    preserved: list[dict[str, Any]] = []
    for step in old_steps:
        if step.get("step_id") == failed_step_id:
            break
        if step.get("status") == "completed":
            preserved.append(step)

    # Assign step_ids to new steps  (continuing from preserved count)
    base_idx = len(preserved)
    for i, step in enumerate(new_steps):
        step["step_id"] = f"{sub_goal_id}.{base_idx + i + 1}"
        step.setdefault("status", "pending")
        step.setdefault("result", None)
        step.setdefault("ts", None)

    plan["steps"] = preserved + new_steps
    plan["recompositions"] = plan.get("recompositions", 0) + 1
    # Reset retry counts for new steps
    plan.setdefault("retry_counts", {})
    plan["completed"] = False

    log.info(
        "Recomposed plan for %s: %d preserved + %d new steps (recomposition #%d)",
        sub_goal_id,
        len(preserved),
        len(new_steps),
        plan["recompositions"],
    )


def apply_block(
    state: dict[str, Any],
    sub_goal_id: str,
    reason: str,
) -> None:
    """Mark a plan as blocked — all recovery strategies exhausted."""
    plan = state.get("step_plans", {}).get(sub_goal_id)
    if plan:
        plan["blocked"] = True
        plan["block_reason"] = reason[:500]
    log.warning("Sub-goal %s BLOCKED: %s", sub_goal_id, reason[:200])


def _record_failure(
    state: dict[str, Any],
    sub_goal_id: str,
    step_id: str,
    strategy: str,
    analysis: str,
    *,
    is_capability_failure: bool = False,
    step_tier: str = "",
) -> None:
    """Append to the plan's failure log."""
    plan = state.get("step_plans", {}).get(sub_goal_id, {})
    failure_log = plan.setdefault("failure_log", [])
    entry: dict[str, Any] = {
        "step_id": step_id,
        "strategy": strategy,
        "analysis": analysis[:500],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if is_capability_failure:
        entry["capability_failure"] = True
        entry["step_tier"] = step_tier
    failure_log.append(entry)
    # Keep last 10 entries to avoid bloat
    if len(failure_log) > 10:
        plan["failure_log"] = failure_log[-10:]

    # Auto-escalate tier when repeated capability failures detected
    if is_capability_failure:
        _maybe_escalate_tier(state, sub_goal_id, step_tier)


# ---------------------------------------------------------------------------
# Tier auto-escalation on capability failures
# ---------------------------------------------------------------------------

_TIER_ORDER = ["low", "medium", "high"]


def _maybe_escalate_tier(
    state: dict[str, Any],
    sub_goal_id: str,
    failed_tier: str,
) -> None:
    """If 2+ capability failures at the same tier, grant a tier override.

    Stores in state["tier_overrides"][sub_goal_id] = next_tier.
    The guardrails module reads this to allow higher tiers for this sub-goal.
    """
    plan = state.get("step_plans", {}).get(sub_goal_id, {})
    failure_log = plan.get("failure_log", [])
    cap_failures_at_tier = sum(
        1 for f in failure_log
        if f.get("capability_failure") and f.get("step_tier") == failed_tier
    )
    if cap_failures_at_tier < 2:
        return

    if failed_tier not in _TIER_ORDER:
        return
    idx = _TIER_ORDER.index(failed_tier)
    if idx >= len(_TIER_ORDER) - 1:
        return  # already at max tier

    next_tier = _TIER_ORDER[idx + 1]
    overrides = state.setdefault("tier_overrides", {})
    current = overrides.get(sub_goal_id, failed_tier)
    if _TIER_ORDER.index(next_tier) > _TIER_ORDER.index(current) if current in _TIER_ORDER else True:
        overrides[sub_goal_id] = next_tier
        log.info(
            "Tier auto-escalation for %s: %s → %s (after %d capability failures)",
            sub_goal_id, failed_tier, next_tier, cap_failures_at_tier,
        )


def get_tier_override(state: dict[str, Any], sub_goal_id: str) -> str | None:
    """Get the tier override for a sub-goal, if any."""
    return state.get("tier_overrides", {}).get(sub_goal_id)


# ---------------------------------------------------------------------------
# Async entry point — called after step failure
# ---------------------------------------------------------------------------


async def handle_step_failure(
    state: dict[str, Any],
    sub_goal_id: str,
    step_id: str,
    evidence: str,
    sub_goal: dict[str, Any] | None = None,
    parent_goal: dict[str, Any] | None = None,
    config: SecretaryConfig | None = None,
) -> str:
    """Analyse a step failure and apply the best recovery strategy.

    Called by the watcher after a step task fails.  Modifies state in-place.

    Returns the strategy applied: "retry", "revise", "recompose", or "block".
    """
    plan = state.get("step_plans", {}).get(sub_goal_id)
    if not plan:
        log.warning("No plan for %s — cannot replan", sub_goal_id)
        return "block"

    if plan.get("blocked"):
        return "block"

    # Find the failed step
    failed_step = None
    for step in plan.get("steps", []):
        if step.get("step_id") == step_id:
            failed_step = step
            break
    if not failed_step:
        log.warning("Step %s not found in plan for %s", step_id, sub_goal_id)
        return "block"

    retry_counts = plan.get("retry_counts", {})
    retries = retry_counts.get(step_id, 0)

    # Check budget first
    budget_strategy = choose_strategy(state, sub_goal_id, step_id)
    if budget_strategy == "block":
        apply_block(state, sub_goal_id, f"Exhausted all recovery strategies for step {step_id}")
        _record_failure(state, sub_goal_id, step_id, "block", "Budget exhausted")
        return "block"

    # Ask the LLM to analyse the failure
    try:
        analysis = await _analyse_failure(
            failed_step, evidence, plan, retries, config,
        )
    except Exception as e:
        log.warning("Failure analysis LLM call failed: %s — defaulting to retry", e)
        # Fallback: simple retry if budget allows
        if retries < MAX_RETRIES_PER_STEP:
            apply_retry(state, sub_goal_id, step_id)
            _record_failure(state, sub_goal_id, step_id, "retry", f"LLM analysis failed: {e}")
            return "retry"
        apply_block(state, sub_goal_id, f"LLM analysis failed and retries exhausted: {e}")
        _record_failure(state, sub_goal_id, step_id, "block", f"LLM analysis failed: {e}")
        return "block"

    if not analysis:
        # Parse failure — simple retry
        if retries < MAX_RETRIES_PER_STEP:
            apply_retry(state, sub_goal_id, step_id)
            _record_failure(state, sub_goal_id, step_id, "retry", "Analysis parse failed")
            return "retry"
        apply_block(state, sub_goal_id, "Analysis parse failed and retries exhausted")
        _record_failure(state, sub_goal_id, step_id, "block", "Analysis parse failed")
        return "block"

    root_cause = analysis.get("root_cause", "unknown")
    llm_strategy = analysis.get("strategy", "retry")
    rationale = analysis.get("rationale", "")
    is_cap_fail = bool(analysis.get("is_capability_failure", False))
    step_tier = failed_step.get("tier", "medium")

    # Common kwargs for _record_failure when we have analysis
    _rec_kw = dict(is_capability_failure=is_cap_fail, step_tier=step_tier)

    # Honour LLM recommendation within budget constraints
    if llm_strategy == "retry" and retries < MAX_RETRIES_PER_STEP:
        apply_retry(state, sub_goal_id, step_id)
        _record_failure(state, sub_goal_id, step_id, "retry", root_cause, **_rec_kw)
        return "retry"

    if llm_strategy == "revise" and retries < MAX_RETRIES_PER_STEP:
        apply_retry(
            state, sub_goal_id, step_id,
            revised_action=analysis.get("revised_action"),
            revised_verification=analysis.get("revised_verification"),
            revised_tier=analysis.get("revised_tier"),
        )
        _record_failure(state, sub_goal_id, step_id, "revise", root_cause, **_rec_kw)
        return "revise"

    if llm_strategy == "recompose" or retries >= MAX_RETRIES_PER_STEP:
        recompositions = plan.get("recompositions", 0)
        if recompositions < MAX_RECOMPOSITIONS_PER_PLAN:
            # Do the recomposition
            try:
                new_steps = await _recompose_plan(
                    sub_goal or {},
                    parent_goal or {},
                    plan,
                    failed_step,
                    root_cause,
                    config,
                )
                if new_steps:
                    apply_recompose(state, sub_goal_id, step_id, new_steps)
                    _record_failure(state, sub_goal_id, step_id, "recompose", root_cause, **_rec_kw)
                    return "recompose"
            except Exception as e:
                log.warning("Recomposition LLM call failed: %s", e)

        # Recompose budget exhausted or failed
        apply_block(state, sub_goal_id, f"Recompose failed/exhausted for step {step_id}: {root_cause}")
        _record_failure(state, sub_goal_id, step_id, "block", root_cause, **_rec_kw)
        return "block"

    # Fallback
    if retries < MAX_RETRIES_PER_STEP:
        apply_retry(state, sub_goal_id, step_id)
        _record_failure(state, sub_goal_id, step_id, "retry", root_cause, **_rec_kw)
        return "retry"

    apply_block(state, sub_goal_id, f"All strategies exhausted for {step_id}: {root_cause}")
    _record_failure(state, sub_goal_id, step_id, "block", root_cause, **_rec_kw)
    return "block"


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------


def _build_client(config: SecretaryConfig | None) -> anthropic.Anthropic:
    """Build an Anthropic client (same as goal_decomposition)."""
    from .direct_agent import _build_client as build_client
    return build_client(config or SecretaryConfig())


async def _analyse_failure(
    step: dict[str, Any],
    evidence: str,
    plan: dict[str, Any],
    retry_count: int,
    config: SecretaryConfig | None,
) -> dict[str, Any] | None:
    """Ask Haiku to analyse a step failure and recommend strategy."""
    prompt = _build_analysis_prompt(step, evidence, plan, retry_count)
    client = _build_client(config)

    def _sync_call() -> anthropic.types.Message:
        with client.messages.stream(
            model=REPLAN_MODEL,
            max_tokens=REPLAN_MAX_TOKENS,
            system=_ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            return stream.get_final_message()

    msg = await asyncio.to_thread(_sync_call)
    text = msg.content[0].text if msg.content else ""
    return _parse_json_response(text)


async def _recompose_plan(
    sub_goal: dict[str, Any],
    parent_goal: dict[str, Any],
    plan: dict[str, Any],
    failed_step: dict[str, Any],
    failure_analysis: str,
    config: SecretaryConfig | None,
) -> list[dict[str, Any]] | None:
    """Ask Haiku to generate new steps replacing the remaining plan."""
    steps = plan.get("steps", [])
    completed = [s for s in steps if s.get("status") == "completed"]
    failed_idx = next(
        (i for i, s in enumerate(steps) if s.get("step_id") == failed_step.get("step_id")),
        len(steps),
    )
    remaining = steps[failed_idx + 1:]

    prompt = _build_recompose_prompt(
        sub_goal, parent_goal, completed, failed_step, failure_analysis, remaining,
    )
    client = _build_client(config)

    def _sync_call() -> anthropic.types.Message:
        with client.messages.stream(
            model=REPLAN_MODEL,
            max_tokens=REPLAN_MAX_TOKENS,
            system=_RECOMPOSE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            return stream.get_final_message()

    msg = await asyncio.to_thread(_sync_call)
    text = msg.content[0].text if msg.content else ""
    parsed = _parse_json_response(text)
    if not parsed or "steps" not in parsed:
        return None

    raw = parsed["steps"]
    if not isinstance(raw, list) or len(raw) < 1:
        return None

    # Limit to 5 replacement steps
    raw = raw[:5]
    result: list[dict[str, Any]] = []
    for step in raw:
        if not isinstance(step, dict) or "action" not in step:
            continue
        result.append({
            "action": str(step["action"]),
            "verification": str(step.get("verification", "")),
            "tier": step.get("tier", "medium") if step.get("tier") in ("low", "medium", "high") else "medium",
            "status": "pending",
            "result": None,
            "ts": None,
        })
    return result if result else None
