"""Stall Escalation Engine — goal-level recovery when progress stalls.

Layer 8 of the planning architecture.  While the replanner (Layer 7) handles
step-level failures within a single sub-goal's plan, this module handles
goal-level stalls — when a goal shows zero progress across multiple review
cycles despite the planner's advisory warnings.

Research basis:
- Reflexion (Shinn 2023): Detect inefficient trajectories (too long without
  success) → generate verbal reflection → retry with revised strategy.
  Our analogy: stall detection → diagnosis → revised approach.
- LATS (Zhou 2023): Balance exploration vs exploitation.  When one path
  is stuck, explore alternatives.  Our analogy: redecompose with fresh
  approach rather than retrying the same blocked plan.
- LLM-Modulo (Kambhampati 2024): External quantitative verifiers trigger
  the LLM for plan revision — not the LLM deciding on its own.
  Our analogy: GoalProgress.stalled flag (numeric) triggers escalation,
  not the planner's self-assessment.
- Self-Contrast (Zhang 2024): Multiple perspectives to overcome stubborn
  biases.  Our analogy: diagnosis asks a fresh LLM call to analyze the
  stall from an outside perspective, not the same planner that created
  the original tasks.

Strategy ladder (escalation on persistent stall):
  1. DIAGNOSE      — Haiku analyses WHY the goal is stalled
  2. REDECOMPOSE   — Clear blocked sub-goal plans, regenerate fresh
  3. REPRIORITIZE  — Suggest deprioritizing, reassign resources
  4. SHELVE        — Mark goal as shelved (deliberate pause, not abandoned)

Budget: max 2 diagnoses, max 1 redecompose, max 1 reprioritize, then shelve.
Cooldown: don't escalate same goal within ESCALATION_COOLDOWN_SNAPSHOTS.

    goal_state.json["escalation_state"] = {
        "<goal_id>": {
            "level": 0-3,           # current ladder position
            "diagnoses": int,       # diagnose attempts
            "redecompositions": int, # redecompose attempts
            "last_escalation_ts": str,
            "last_escalation_snapshot": int,  # snapshot index
            "diagnosis": str,       # latest diagnosis text
            "history": [
                {"level": int, "strategy": str, "summary": str, "ts": str}
            ],
        }
    }
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import anthropic

from .config import SecretaryConfig
from .goal_progress import GoalProgress

log = logging.getLogger("secretary.goal_escalation")

ESCALATION_MODEL = "claude-haiku-4.5"
ESCALATION_MAX_TOKENS = 1024

MAX_DIAGNOSES = 2
MAX_REDECOMPOSITIONS = 1
ESCALATION_COOLDOWN_SNAPSHOTS = 2  # min snapshots between escalations

# Strategy levels
LEVEL_DIAGNOSE = 0
LEVEL_REDECOMPOSE = 1
LEVEL_REPRIORITIZE = 2
LEVEL_SHELVE = 3

STRATEGY_NAMES = {
    LEVEL_DIAGNOSE: "diagnose",
    LEVEL_REDECOMPOSE: "redecompose",
    LEVEL_REPRIORITIZE: "reprioritize",
    LEVEL_SHELVE: "shelve",
}


@dataclass
class EscalationAction:
    """A single escalation action to take on a stalled goal."""

    goal_id: str
    strategy: str  # diagnose | redecompose | reprioritize | shelve
    summary: str  # human-readable summary of what was decided
    tasks: list[dict[str, Any]]  # tasks to inject into the watcher cycle
    sub_goal_updates: list[dict[str, Any]]  # apply_updates format
    note: str  # progress note to record


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_DIAGNOSIS_SYSTEM = """\
You are a goal stall diagnostic engine for an AI secretary.
A long-horizon goal has shown NO progress across multiple review cycles.
Your job: analyse the root cause and recommend concrete corrective action.

Inputs: the stalled goal, its sub-goals + statuses, blocked step plans, \
failure logs, and recent task outcomes.

Respond with ONLY JSON (no markdown fences):
{
  "root_cause": "Why this goal is stalled (2-3 sentences)",
  "blocked_by": ["list of specific blockers: missing prerequisites, \
wrong approach, external dependency, etc."],
  "recommendation": "diagnose_deeper|redecompose|reprioritize|shelve",
  "corrective_tasks": [
    {"prompt": "Specific actionable task to unblock progress", \
"tier": "low|medium|high"}
  ],
  "sub_goal_changes": [
    {"sub_goal_id": "...", "new_status": "not-started|blocked", \
"reason": "..."}
  ]
}

Guidelines:
- "diagnose_deeper": Need more investigation (e.g. run a test, check config).
- "redecompose": The step plans are wrong — clear and regenerate.
- "reprioritize": This goal depends on something else; focus there first.
- "shelve": Goal is not achievable with current resources/capabilities.
- Generate at most 2 corrective tasks. Be specific and actionable.
- If sub-goals are blocked due to wrong approach, suggest resetting them.
- If failures are tagged [CAPABILITY], the model tier is too weak. \
Recommend "redecompose" so steps get regenerated with appropriate tiers. \
The system will auto-escalate tiers for sub-goals with capability failures.\
"""


def _build_diagnosis_prompt(
    goal: dict[str, Any],
    sub_goal_overrides: dict[str, Any],
    step_plans: dict[str, Any],
    failure_logs: dict[str, list[dict[str, Any]]],
    progress: GoalProgress,
    escalation_history: list[dict[str, Any]],
) -> str:
    """Build the prompt for stall diagnosis."""
    parts: list[str] = []

    gid = goal.get("id", "?")
    parts.append(f"## Stalled Goal: {gid}")
    parts.append(f"Description: {goal.get('description', '')}")
    parts.append(f"Priority: {goal.get('priority', 5)}")
    parts.append(
        f"Progress: {progress.completion:.0%} "
        f"({progress.done_sub_goals}/{progress.total_sub_goals} sub-goals)"
    )
    if progress.success_rate >= 0:
        parts.append(
            f"Task success rate: {progress.success_rate:.0%} "
            f"({progress.total_tasks} tasks)"
        )
    parts.append(f"Velocity: {progress.velocity:+.0%} (stalled for 3+ snapshots)")

    # Sub-goals with effective status
    parts.append("\n## Sub-Goals")
    for sg in goal.get("sub_goals", []):
        sg_id = sg.get("id", "?")
        sg_desc = sg.get("description", "")
        sg_status = sg.get("status", "not-started")
        override = sub_goal_overrides.get(sg_id)
        if override:
            sg_status = override.get("status", sg_status)
        parts.append(f"  - [{sg_id}] {sg_desc} — {sg_status}")

        # Show step plan state if exists
        plan = step_plans.get(sg_id)
        if plan:
            blocked = plan.get("blocked", False)
            completed = plan.get("completed", False)
            steps = plan.get("steps", [])
            done = sum(1 for s in steps if s.get("status") == "completed")
            failed = sum(1 for s in steps if s.get("status") == "failed")
            pending = sum(1 for s in steps if s.get("status") == "pending")
            tag = "BLOCKED" if blocked else "DONE" if completed else "ACTIVE"
            parts.append(
                f"    Step plan [{tag}]: {done} done, {failed} failed, "
                f"{pending} pending"
            )
            if blocked:
                parts.append(
                    f"    Block reason: {plan.get('block_reason', '?')[:200]}"
                )

        # Failure log for this sub-goal
        sg_failures = failure_logs.get(sg_id, [])
        if sg_failures:
            parts.append(f"    Recent failures ({len(sg_failures)}):")
            cap_count = 0
            for fl in sg_failures[-3:]:
                cap_tag = " [CAPABILITY]" if fl.get("capability_failure") else ""
                tier_tag = f" (tier={fl.get('step_tier', '?')})" if fl.get("step_tier") else ""
                parts.append(
                    f"      - {fl.get('strategy', '?')}{cap_tag}{tier_tag}: "
                    f"{fl.get('analysis', '')[:100]}"
                )
                if fl.get("capability_failure"):
                    cap_count += 1
            if cap_count:
                parts.append(
                    f"    ⚠ {cap_count}/{len(sg_failures[-3:])} recent failures are "
                    f"CAPABILITY failures (model too weak for task). "
                    f"Consider recommending tier escalation."
                )

    # Previous escalation history
    if escalation_history:
        parts.append("\n## Previous Escalation History")
        for h in escalation_history[-3:]:
            parts.append(
                f"  - [{h.get('strategy', '?')}] {h.get('summary', '')[:150]}"
            )

    return "\n".join(parts)


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """Parse JSON from LLM response, handling markdown fences."""
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        clean = "\n".join(lines).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(clean[start : end + 1])
            except json.JSONDecodeError:
                pass
    return None


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _get_escalation_state(
    state: dict[str, Any], goal_id: str,
) -> dict[str, Any]:
    """Get or initialize escalation state for a goal."""
    esc = state.setdefault("escalation_state", {})
    if goal_id not in esc:
        esc[goal_id] = {
            "level": LEVEL_DIAGNOSE,
            "diagnoses": 0,
            "redecompositions": 0,
            "last_escalation_ts": None,
            "last_escalation_snapshot": -1,
            "diagnosis": "",
            "history": [],
        }
    return esc[goal_id]


def _should_escalate(
    esc_state: dict[str, Any],
    current_snapshot_count: int,
) -> bool:
    """Check if enough time has passed since last escalation."""
    last_snap = esc_state.get("last_escalation_snapshot", -1)
    if last_snap < 0:
        return True
    return (current_snapshot_count - last_snap) >= ESCALATION_COOLDOWN_SNAPSHOTS


def choose_escalation_level(esc_state: dict[str, Any]) -> int:
    """Decide escalation level based on budget consumed."""
    diagnoses = esc_state.get("diagnoses", 0)
    redecompositions = esc_state.get("redecompositions", 0)

    if diagnoses < MAX_DIAGNOSES:
        return LEVEL_DIAGNOSE
    if redecompositions < MAX_REDECOMPOSITIONS:
        return LEVEL_REDECOMPOSE
    # After redecompose, reprioritize once then shelve
    level = esc_state.get("level", LEVEL_DIAGNOSE)
    if level < LEVEL_REPRIORITIZE:
        return LEVEL_REPRIORITIZE
    return LEVEL_SHELVE


def _record_escalation(
    esc_state: dict[str, Any],
    strategy: str,
    summary: str,
    snapshot_count: int,
) -> None:
    """Record an escalation event."""
    esc_state["last_escalation_ts"] = datetime.now(timezone.utc).isoformat()
    esc_state["last_escalation_snapshot"] = snapshot_count
    history = esc_state.setdefault("history", [])
    history.append({
        "level": esc_state.get("level", 0),
        "strategy": strategy,
        "summary": summary[:300],
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    # Keep last 10
    if len(history) > 10:
        esc_state["history"] = history[-10:]


async def _run_diagnosis(
    goal: dict[str, Any],
    sub_goal_overrides: dict[str, Any],
    step_plans: dict[str, Any],
    failure_logs: dict[str, list[dict[str, Any]]],
    progress: GoalProgress,
    escalation_history: list[dict[str, Any]],
    config: SecretaryConfig | None = None,
) -> dict[str, Any] | None:
    """Ask Haiku to diagnose why a goal is stalled."""
    prompt = _build_diagnosis_prompt(
        goal, sub_goal_overrides, step_plans,
        failure_logs, progress, escalation_history,
    )

    base_url = None
    if config:
        from .config import _interpolate_env
        base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")

    client = anthropic.AsyncAnthropic(
        base_url=base_url, api_key="copilot-proxy",
    )

    try:
        resp = await client.messages.create(
            model=ESCALATION_MODEL,
            max_tokens=ESCALATION_MAX_TOKENS,
            system=_DIAGNOSIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text if resp.content else ""
        return _parse_json_response(text)
    except Exception as e:
        log.warning("Escalation diagnosis LLM call failed: %s", e)
        return None


def _collect_failure_logs(
    state: dict[str, Any],
    goal: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Gather failure logs for all sub-goals of a goal."""
    result: dict[str, list[dict[str, Any]]] = {}
    plans = state.get("step_plans", {})
    for sg in goal.get("sub_goals", []):
        sg_id = sg.get("id", "")
        plan = plans.get(sg_id)
        if plan:
            flog = plan.get("failure_log", [])
            if flog:
                result[sg_id] = flog
    return result


def _find_blocked_sub_goals(
    goal: dict[str, Any],
    state: dict[str, Any],
) -> list[str]:
    """Find sub-goals that are blocked (via state or step plans)."""
    blocked = []
    overrides = state.get("sub_goal_status", {})
    plans = state.get("step_plans", {})
    for sg in goal.get("sub_goals", []):
        sg_id = sg.get("id", "")
        # Check state override
        override = overrides.get(sg_id)
        if override and override.get("status") == "blocked":
            blocked.append(sg_id)
            continue
        # Check step plan
        plan = plans.get(sg_id)
        if plan and plan.get("blocked"):
            blocked.append(sg_id)
    return blocked


async def evaluate_escalation(
    goal: dict[str, Any],
    progress: GoalProgress,
    state: dict[str, Any],
    config: SecretaryConfig | None = None,
) -> EscalationAction | None:
    """Evaluate whether a stalled goal needs escalation and take action.

    Parameters
    ----------
    goal : goal dict from GoalStore.goals
    progress : GoalProgress for this goal (must have stalled=True)
    state : GoalStore._state (modified in-place)
    config : SecretaryConfig for API access

    Returns
    -------
    EscalationAction if an escalation was performed, None if cooldown active.
    """
    gid = goal.get("id", "")
    if not gid:
        return None

    esc_state = _get_escalation_state(state, gid)
    snapshot_count = len(state.get("progress_snapshots", []))

    # Check cooldown
    if not _should_escalate(esc_state, snapshot_count):
        log.debug("Escalation cooldown active for %s", gid)
        return None

    level = choose_escalation_level(esc_state)
    strategy = STRATEGY_NAMES[level]
    log.info("Escalating goal %s: level=%d strategy=%s", gid, level, strategy)

    sub_goal_overrides = state.get("sub_goal_status", {})
    step_plans = state.get("step_plans", {})

    if level == LEVEL_SHELVE:
        # No LLM call needed — just shelve
        esc_state["level"] = LEVEL_SHELVE
        summary = (
            f"Goal {gid} shelved after exhausting all escalation strategies "
            f"({esc_state.get('diagnoses', 0)} diagnoses, "
            f"{esc_state.get('redecompositions', 0)} redecompositions)"
        )
        _record_escalation(esc_state, strategy, summary, snapshot_count)
        return EscalationAction(
            goal_id=gid,
            strategy="shelve",
            summary=summary,
            tasks=[],
            sub_goal_updates=[{
                "sub_goal_id": sg.get("id", ""),
                "new_status": "blocked",
                "evidence": f"Parent goal shelved: {summary}",
            } for sg in goal.get("sub_goals", [])
              if sub_goal_overrides.get(sg.get("id", ""), {}).get("status") != "done"],
            note=summary,
        )

    # Run diagnosis for all non-shelve levels
    failure_logs = _collect_failure_logs(state, goal)
    diagnosis = await _run_diagnosis(
        goal, sub_goal_overrides, step_plans,
        failure_logs, progress, esc_state.get("history", []),
        config,
    )

    if not diagnosis:
        # LLM call failed — record attempt, don't escalate further
        log.warning("Diagnosis failed for %s — deferring escalation", gid)
        return None

    root_cause = diagnosis.get("root_cause", "Unknown")
    recommendation = diagnosis.get("recommendation", "diagnose_deeper")
    corrective_tasks = diagnosis.get("corrective_tasks", [])
    sub_goal_changes = diagnosis.get("sub_goal_changes", [])

    esc_state["diagnosis"] = root_cause

    if level == LEVEL_DIAGNOSE:
        esc_state["diagnoses"] = esc_state.get("diagnoses", 0) + 1
        esc_state["level"] = LEVEL_DIAGNOSE

        # Build corrective tasks
        tasks = []
        for ct in corrective_tasks[:2]:
            task_prompt = ct.get("prompt", "")
            if task_prompt:
                tasks.append({
                    "prompt": task_prompt,
                    "tier": ct.get("tier", "medium"),
                    "priority": 1,
                    "source": "escalation",
                    "goal_id": gid,
                })

        # Sub-goal status changes
        updates = []
        for sc in sub_goal_changes:
            sg_id = sc.get("sub_goal_id", "")
            new_status = sc.get("new_status", "")
            if sg_id and new_status in ("not-started", "blocked", "in-progress"):
                updates.append({
                    "sub_goal_id": sg_id,
                    "new_status": new_status,
                    "evidence": f"Escalation diagnosis: {sc.get('reason', root_cause)[:200]}",
                })

        summary = f"Diagnosed stall for {gid}: {root_cause[:200]}"
        _record_escalation(esc_state, strategy, summary, snapshot_count)

        return EscalationAction(
            goal_id=gid,
            strategy="diagnose",
            summary=summary,
            tasks=tasks,
            sub_goal_updates=updates,
            note=f"[ESCALATION] {summary}",
        )

    if level == LEVEL_REDECOMPOSE:
        esc_state["redecompositions"] = esc_state.get("redecompositions", 0) + 1
        esc_state["level"] = LEVEL_REDECOMPOSE

        # Clear blocked step plans to allow fresh decomposition
        blocked = _find_blocked_sub_goals(goal, state)
        updates = []
        for sg_id in blocked:
            # Remove the blocked step plan
            if sg_id in step_plans:
                del step_plans[sg_id]
            # Reset sub-goal status to not-started
            updates.append({
                "sub_goal_id": sg_id,
                "new_status": "not-started",
                "evidence": f"Escalation redecompose: clearing blocked plan ({root_cause[:150]})",
            })

        # Also generate corrective tasks from diagnosis
        tasks = []
        for ct in corrective_tasks[:2]:
            task_prompt = ct.get("prompt", "")
            if task_prompt:
                tasks.append({
                    "prompt": task_prompt,
                    "tier": ct.get("tier", "medium"),
                    "priority": 1,
                    "source": "escalation",
                    "goal_id": gid,
                })

        summary = (
            f"Redecompose for {gid}: cleared {len(blocked)} blocked plans. "
            f"Cause: {root_cause[:150]}"
        )
        _record_escalation(esc_state, strategy, summary, snapshot_count)

        return EscalationAction(
            goal_id=gid,
            strategy="redecompose",
            summary=summary,
            tasks=tasks,
            sub_goal_updates=updates,
            note=f"[ESCALATION] {summary}",
        )

    if level == LEVEL_REPRIORITIZE:
        esc_state["level"] = LEVEL_REPRIORITIZE

        summary = (
            f"Reprioritize: goal {gid} persistently stalled. "
            f"Diagnosis: {root_cause[:150]}. "
            f"Recommending reduced priority — focus on achievable goals."
        )
        _record_escalation(esc_state, strategy, summary, snapshot_count)

        # Generate a single diagnostic task to review priority
        tasks = [{
            "prompt": (
                f"Review the feasibility of goal '{gid}': "
                f"{goal.get('description', '')}. "
                f"Recent diagnosis: {root_cause[:200]}. "
                f"Determine if this goal should be deprioritized, "
                f"its approach fundamentally changed, or resources "
                f"redirected to other goals. Write findings to "
                f"data/scratchpad.md."
            ),
            "tier": "medium",
            "priority": 1,
            "source": "escalation",
            "goal_id": gid,
        }]

        return EscalationAction(
            goal_id=gid,
            strategy="reprioritize",
            summary=summary,
            tasks=tasks,
            sub_goal_updates=[],
            note=f"[ESCALATION] {summary}",
        )

    return None


async def evaluate_escalations(
    goals: list[dict[str, Any]],
    progress_map: dict[str, GoalProgress],
    state: dict[str, Any],
    config: SecretaryConfig | None = None,
) -> list[EscalationAction]:
    """Evaluate all stalled goals and return escalation actions.

    This is the main entry point, called from the watcher after
    progress scoring identifies stalled goals.
    """
    actions: list[EscalationAction] = []
    for goal in goals:
        gid = goal.get("id", "")
        gp = progress_map.get(gid)
        if not gp or not gp.stalled:
            continue

        # Also check for goals with ALL sub-goals blocked
        if not gp.stalled:
            blocked = _find_blocked_sub_goals(goal, state)
            total_sg = len(goal.get("sub_goals", []))
            if total_sg > 0 and len(blocked) == total_sg:
                pass  # will be caught by stalled check above eventually
            else:
                continue

        action = await evaluate_escalation(goal, gp, state, config)
        if action:
            actions.append(action)
            log.info(
                "Escalation for %s: %s — %s",
                gid, action.strategy, action.summary[:100],
            )

    return actions
