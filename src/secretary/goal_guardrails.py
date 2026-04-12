"""Goal Safety Guardrails — validate and constrain LLM-generated goal tasks.

Layer 16 of the planning architecture.  Campaign tasks are human-authored YAML;
goal tasks are LLM-generated — fundamentally different trust level.  This module
sits between task generation and execution to enforce safety constraints.

Research basis:
- Anthropic "Building Effective Agents": "recommend extensive testing in
  sandboxed environments, along with appropriate guardrails"
- MAST (Cemri 2025): task verification is 1 of 3 top failure categories
  in multi-agent systems
- Kambhampati (ICML 2024): external verifiers needed, not LLM self-assessment

Guardrails applied:
1. Tier cap: goal tasks capped at configurable max_tier (default: medium)
2. Task count cap: max tasks per cycle across ALL goal sources
3. Prompt length limit: reject suspiciously long/short prompts
4. Source tagging: ensure all goal tasks have source + goal_id set
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("secretary.goal_guardrails")

# Tier ordering for comparison
_TIER_ORDER = {"low": 0, "medium": 1, "high": 2, "oracle": 3, "deep": 4}

# Prompt sanity bounds
MIN_PROMPT_LENGTH = 10
MAX_PROMPT_LENGTH = 4000


@dataclass
class GuardrailResult:
    """Result of applying guardrails to a batch of goal tasks."""

    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    warnings: list[str]


def validate_goal_task(task: dict[str, Any], *, max_tier: str = "medium") -> tuple[bool, str]:
    """Validate a single goal-generated task dict.

    Returns (ok, reason).  If ok=False, reason explains why.
    """
    prompt = task.get("prompt", task.get("task", ""))
    if not prompt or len(prompt.strip()) < MIN_PROMPT_LENGTH:
        return False, f"prompt too short ({len(prompt.strip())} chars, min {MIN_PROMPT_LENGTH})"

    if len(prompt) > MAX_PROMPT_LENGTH:
        return False, f"prompt too long ({len(prompt)} chars, max {MAX_PROMPT_LENGTH})"

    tier = task.get("tier", "medium")
    if _TIER_ORDER.get(tier, 99) > _TIER_ORDER.get(max_tier, 1):
        return False, f"tier '{tier}' exceeds max_tier '{max_tier}'"

    source = task.get("source", "")
    if not source:
        return False, "missing 'source' field"

    return True, ""


def apply_guardrails(
    tasks: list[dict[str, Any]],
    *,
    max_tier: str = "medium",
    max_tasks_per_cycle: int = 5,
    tier_overrides: dict[str, str] | None = None,
) -> GuardrailResult:
    """Apply safety guardrails to a batch of goal-generated tasks.

    Parameters
    ----------
    tasks : list of task dicts from goal review / step plans / escalation / self-improve
    max_tier : maximum tier allowed for goal tasks (tasks with higher tier get downgraded)
    max_tasks_per_cycle : hard cap on total goal tasks per watcher cycle
    tier_overrides : per-sub-goal tier overrides from capability-failure detection

    Returns
    -------
    GuardrailResult with accepted tasks, rejected tasks, and warnings.
    """
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    warnings: list[str] = []

    for task in tasks:
        # Ensure source tagging
        if not task.get("source"):
            task["source"] = "goals"
            warnings.append(f"task '{task.get('id', '?')}': added missing source='goals'")

        # Per-sub-goal tier override from capability-failure auto-escalation
        effective_max_tier = max_tier
        sub_goal_id = task.get("_sub_goal_id", "")
        if sub_goal_id and tier_overrides and sub_goal_id in tier_overrides:
            override_tier = tier_overrides[sub_goal_id]
            if _TIER_ORDER.get(override_tier, 0) > _TIER_ORDER.get(max_tier, 0):
                effective_max_tier = override_tier
                warnings.append(
                    f"task '{task.get('id', '?')}': tier cap raised to '{override_tier}' "
                    f"(capability-failure auto-escalation for {sub_goal_id})"
                )

        ok, reason = validate_goal_task(task, max_tier=effective_max_tier)
        if not ok:
            # Try to salvage by downgrading tier
            tier = task.get("tier", "medium")
            if "tier" in reason and _TIER_ORDER.get(tier, 99) > _TIER_ORDER.get(effective_max_tier, 1):
                task["tier"] = effective_max_tier
                warnings.append(
                    f"task '{task.get('id', '?')}': downgraded tier '{tier}' → '{effective_max_tier}'"
                )
                ok, reason = validate_goal_task(task, max_tier=effective_max_tier)

            if not ok:
                rejected.append(task)
                log.warning("Goal task rejected: %s — %s", task.get("prompt", "")[:60], reason)
                continue

        accepted.append(task)

    # Enforce task count cap — but prioritize self-improvement tasks
    if len(accepted) > max_tasks_per_cycle:
        # Separate self-improvement tasks (they should not be dropped)
        si_tasks = [t for t in accepted if t.get("_self_improve")]
        regular_tasks = [t for t in accepted if not t.get("_self_improve")]
        # Keep all self-improvement tasks + fill remaining slots with regular
        remaining_slots = max(0, max_tasks_per_cycle - len(si_tasks))
        kept = si_tasks + regular_tasks[:remaining_slots]
        overflow = regular_tasks[remaining_slots:]
        accepted = kept
        for t in overflow:
            rejected.append(t)
        if overflow:
            warnings.append(
                f"task count cap: kept {len(kept)} (incl {len(si_tasks)} self-improve), "
                f"rejected {len(overflow)} overflow"
            )
            log.info("Goal guardrails: capped at %d tasks (rejected %d)", len(kept), len(overflow))

    if rejected:
        log.info(
            "Goal guardrails: %d accepted, %d rejected, %d warnings",
            len(accepted), len(rejected), len(warnings),
        )

    return GuardrailResult(accepted=accepted, rejected=rejected, warnings=warnings)
