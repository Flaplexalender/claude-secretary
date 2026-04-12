"""Goal Scheduling & Trust Scoring — curriculum-gated goal selection + data-driven trust.

Layer 20 of the goal actualization stack (extended by Layer 22 and Layer 23).

Research basis:
- Eureka (Ma 2023): Curriculum learning — start with easier tasks, graduate
  to harder ones as competence is demonstrated.
- Anthropic "Building Effective Agents": Progressive trust — "extensive testing
  in sandboxed environments, along with appropriate guardrails."
- LLM Planning Survey (Huang 2024): Task prioritization and plan selection
  are key capabilities for autonomous agents.
- Reflexion (Shinn 2023): Verbal reinforcement learning — agents learn from
  trial outcomes without weight updates.  The auto-graduation system closes the
  feedback loop: trust data drives real policy changes.

Architecture:
    goals.yaml (priority field)
    + goal_state.json (verification_log, approval_queue, step_plans)
    + run_log (source="goals", goal_id)
    → Goal Scheduler: select_active_goals() filters by curriculum + priority
    → Trust Scorer: compute_trust_score() → per-goal trust from history
    → suggest_policy() → recommended approval_mode + tool_policy
    → Auto-Graduation: apply_auto_graduation() → real config changes

    Curriculum levels gate which goals are eligible:
        0 = observation-only (no goals active)
        1 = safe (priority 1-2 only, max 2 active)
        2 = standard (priority 1-4, max 3 active)
        3 = full autonomy (all goals, max 5 active)

    Trust scoring uses 4 signals:
        - Verification pass rate (from verification_log)
        - Approval acceptance rate (from approval_queue)
        - Task success rate (from run_log entries with source=goals)
        - Step completion rate (from step_plans)

    Auto-graduation (Layer 23, extended Layer 26 per-goal):
        - Per-goal trust levels: each goal graduates independently
        - One level at a time (no jumping untrusted→trusted)
        - Cooldown: GRADUATION_COOLDOWN_CYCLES between upgrades (per-goal)
        - Stability: MIN_STABLE_SNAPSHOTS consecutive snapshots at level
        - Rollback: auto-downgrade if trust drops (bypasses cooldown)
        - Per-goal overrides in goal_state.json["graduation_overrides"][goal_id]
        - get_goal_policy() returns effective policy for any goal

    goal_state.json["trust_snapshots"] = [
        {
            "ts": str,
            "scores": {"goal-id": {"trust_score": float, ...}, ...}
        }
    ]
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .goal_dependencies import has_any_unblocked_sub_goal

log = logging.getLogger("secretary.goal_scheduler")

# ── Curriculum Levels ─────────────────────────────────────────

CURRICULUM_LEVELS: dict[int, dict[str, Any]] = {
    0: {"max_priority": 0, "max_active": 0, "description": "Observation only"},
    1: {"max_priority": 2, "max_active": 2, "description": "Safe goals (priority 1-2)"},
    2: {"max_priority": 4, "max_active": 3, "description": "Standard (priority 1-4)"},
    3: {"max_priority": 5, "max_active": 5, "description": "Full autonomy"},
}

MAX_TRUST_SNAPSHOTS = 50


def select_active_goals(
    goals: list[dict[str, Any]],
    curriculum_level: int = 1,
    max_active: int | None = None,
    sub_goal_overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Select goals eligible for this cycle based on curriculum + priority.

    Args:
        goals: All goal definitions from goals.yaml.
        curriculum_level: 0-3 gating which priorities are eligible.
        max_active: Override for max active goals (uses curriculum default if None).
        sub_goal_overrides: State overrides from goal_state.json (for dependency checks).

    Returns:
        Filtered and priority-sorted list of active goals.
    """
    gate = CURRICULUM_LEVELS.get(curriculum_level, CURRICULUM_LEVELS[1])
    max_priority = gate["max_priority"]
    effective_max = max_active if max_active is not None else gate["max_active"]

    if max_priority == 0 or effective_max == 0:
        return []

    _overrides = sub_goal_overrides or {}

    # Filter by priority gate + exclude "done" goals
    eligible = [
        g for g in goals
        if g.get("priority", 5) <= max_priority
        and g.get("status", "not-started") != "done"
    ]

    # Layer 25: skip goals where ALL sub-goals are dependency-blocked
    eligible = [
        g for g in eligible
        if not g.get("sub_goals") or has_any_unblocked_sub_goal(g, goals, _overrides)
    ]

    # Sort by priority (lower = more critical)
    eligible.sort(key=lambda g: g.get("priority", 5))

    selected = eligible[:effective_max]
    if selected:
        ids = [g.get("id", "?") for g in selected]
        log.info(
            "Goal scheduler: curriculum L%d → %d/%d goals active: %s",
            curriculum_level, len(selected), len(goals), ", ".join(ids),
        )
    return selected


# ── Trust Scoring ─────────────────────────────────────────────

def _build_sub_goal_to_goal_map(goals: list[dict[str, Any]]) -> dict[str, str]:
    """Build mapping from sub_goal_id → parent goal_id."""
    mapping: dict[str, str] = {}
    for goal in goals:
        gid = goal.get("id", "")
        for sg in goal.get("sub_goals", []):
            sg_id = sg.get("id", "")
            if sg_id:
                mapping[sg_id] = gid
    return mapping


def compute_trust_score(
    goal_id: str,
    state: dict[str, Any],
    goals: list[dict[str, Any]],
    run_log_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute trust score for a goal from verification + approval + success history.

    Returns dict with trust_score (0.0-1.0) and component metrics.
    """
    sg_map = _build_sub_goal_to_goal_map(goals)

    # 1. Verification pass rate
    v_log = state.get("verification_log", [])
    goal_verifications = [
        v for v in v_log
        if sg_map.get(v.get("sub_goal_id", ""), "") == goal_id
        or v.get("goal_id") == goal_id  # Layer 30: match non-step goal tasks
    ]
    # Sliding window: only use recent entries to prevent stale data from
    # dominating (e.g. old entries from before pipeline bug fixes).
    goal_verifications = goal_verifications[-20:]
    v_count = len(goal_verifications)
    if v_count > 0:
        passes = sum(1 for v in goal_verifications if v.get("verdict") == "pass")
        v_rate = passes / v_count
    else:
        v_rate = _NO_DATA_NEUTRAL  # no evidence of problems → optimistic

    # 2. Approval acceptance rate
    queue = state.get("approval_queue", [])
    goal_queue = [e for e in queue if e.get("goal_id") == goal_id]
    decided = [e for e in goal_queue if e.get("status") in ("approved", "rejected", "executed", "notified")]
    if decided:
        rejected = sum(1 for e in decided if e.get("status") == "rejected")
        a_rate = 1.0 - (rejected / len(decided))
    else:
        a_rate = _NO_DATA_NEUTRAL

    # 3. Task success rate from run_log
    if run_log_entries is not None:
        goal_entries = [e for e in run_log_entries if e.get("goal_id") == goal_id]
        s_count = len(goal_entries)
        if s_count > 0:
            s_rate = sum(1 for e in goal_entries if e.get("success")) / s_count
        else:
            s_rate = _NO_DATA_NEUTRAL
    else:
        goal_entries = []
        s_count = 0
        s_rate = _NO_DATA_NEUTRAL

    # 4. Step completion rate
    step_plans = state.get("step_plans", {})
    total_steps = 0
    completed_steps = 0
    for sg_id, plan in step_plans.items():
        if plan.get("goal_id") == goal_id:
            steps = plan.get("steps", [])
            total_steps += len(steps)
            completed_steps += sum(
                1 for s in steps if s.get("status") == "done"
            )
    if total_steps > 0:
        step_rate = completed_steps / total_steps
    else:
        step_rate = _NO_DATA_NEUTRAL

    # Weighted: verification (35%) > approval (25%) > success (25%) > steps (15%)
    score = 0.35 * v_rate + 0.25 * a_rate + 0.25 * s_rate + 0.15 * step_rate

    return {
        "goal_id": goal_id,
        "trust_score": round(score, 3),
        "verification_rate": round(v_rate, 3),
        "approval_rate": round(a_rate, 3),
        "success_rate": round(s_rate, 3),
        "step_rate": round(step_rate, 3),
        "sample_sizes": {
            "verifications": v_count,
            "approvals": len(decided),
            "tasks": s_count,
            "steps": total_steps,
        },
    }


def compute_all_trust_scores(
    goals: list[dict[str, Any]],
    state: dict[str, Any],
    run_log_entries: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Compute trust scores for all goals."""
    return {
        g.get("id", "?"): compute_trust_score(
            g.get("id", "?"), state, goals, run_log_entries,
        )
        for g in goals
    }


# ── Trust Levels & Policy Suggestions ────────────────────────

TRUST_LEVELS = [
    # (min, max, level_name, suggested_approval, suggested_tool_policy)
    (0.0, 0.3, "untrusted", "review", "read-only"),
    (0.3, 0.6, "cautious", "review", "supervised"),
    (0.6, 0.8, "trusted", "notify", "supervised"),
    (0.8, 1.01, "autonomous", "auto", "full"),
]


def suggest_policy(trust_score: float) -> dict[str, str]:
    """Suggest approval_mode and tool_policy based on trust score."""
    for low, high, level, mode, policy in TRUST_LEVELS:
        if low <= trust_score < high:
            return {"level": level, "approval_mode": mode, "tool_policy": policy}
    return {"level": "autonomous", "approval_mode": "auto", "tool_policy": "full"}


# ── Trust Snapshot Persistence ────────────────────────────────

def record_trust_snapshot(
    state: dict[str, Any],
    trust_scores: dict[str, dict[str, Any]],
) -> None:
    """Record trust scores for trend tracking in goal_state.json."""
    snapshots = state.setdefault("trust_snapshots", [])
    # Store only the scores (not full dicts) to save space
    compact = {
        gid: data["trust_score"]
        for gid, data in trust_scores.items()
    }
    snapshots.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "scores": compact,
    })
    if len(snapshots) > MAX_TRUST_SNAPSHOTS:
        state["trust_snapshots"] = snapshots[-MAX_TRUST_SNAPSHOTS:]


# ── Formatting Helpers ────────────────────────────────────────

def format_trust_section(
    trust_scores: dict[str, dict[str, Any]],
) -> str:
    """Format trust scores for CLI display or prompt injection."""
    if not trust_scores:
        return ""

    lines: list[str] = ["## Goal Trust Scores"]
    for gid, data in sorted(trust_scores.items()):
        score = data["trust_score"]
        policy = suggest_policy(score)
        level = policy["level"]
        samples = data["sample_sizes"]
        total_samples = sum(samples.values())

        # Visual bar
        filled = int(score * 10)
        bar = "█" * filled + "░" * (10 - filled)

        lines.append(
            f"  {gid}: [{bar}] {score:.2f} ({level})"
            f"  [v:{data['verification_rate']:.0%} a:{data['approval_rate']:.0%}"
            f" s:{data['success_rate']:.0%} st:{data['step_rate']:.0%}]"
            f"  ({total_samples} samples)"
        )
        if total_samples == 0:
            lines.append(f"    → no data yet, default neutral ({_NO_DATA_NEUTRAL:.2f})")
        elif policy["approval_mode"] != "review" or policy["tool_policy"] != "read-only":
            lines.append(
                f"    → suggests: approval={policy['approval_mode']}"
                f", tools={policy['tool_policy']}"
            )

    return "\n".join(lines)


def format_schedule_section(
    active_goals: list[dict[str, Any]],
    all_goals: list[dict[str, Any]],
    curriculum_level: int,
) -> str:
    """Format scheduling info for CLI display."""
    gate = CURRICULUM_LEVELS.get(curriculum_level, CURRICULUM_LEVELS[1])
    active_ids = {g.get("id") for g in active_goals}

    lines: list[str] = [
        f"## Goal Schedule (curriculum L{curriculum_level}: {gate['description']})",
    ]
    for g in all_goals:
        gid = g.get("id", "?")
        priority = g.get("priority", 5)
        status = g.get("status", "not-started")
        if gid in active_ids:
            lines.append(f"  ● {gid} (P{priority}, {status}) — ACTIVE")
        else:
            reason = "done" if status == "done" else f"P{priority} > gate P{gate['max_priority']}"
            lines.append(f"  ○ {gid} (P{priority}, {status}) — excluded ({reason})")

    return "\n".join(lines)


# ── Trust-Based Graduation ────────────────────────────────────

# Minimum samples before graduation recommendations are considered meaningful.
# Lowered from 3 → 2: stability check (2 consecutive snapshots) + cooldown (5 cycles)
# already guard against premature graduation.  The old value caused a deadlock
# where untrusted goals couldn't generate enough tasks to accumulate 3 samples.
MIN_GRADUATION_SAMPLES = 2

# Default trust value for signals with zero data.  0.5 was overly conservative:
# absence of evidence (no failures, no rejections) ≠ evidence of absence.
# 0.75 = optimistic neutral ("no news is good news").  MIN_GRADUATION_SAMPLES
# still prevents graduation with truly zero evidence.
_NO_DATA_NEUTRAL = 0.75


def evaluate_trust_graduation(
    trust_scores: dict[str, dict[str, Any]],
    current_approval_mode: str,
    current_tool_policy: str,
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compare current config against trust-suggested policy for each goal.

    Returns a list of graduation recommendations (may be empty).
    Each entry: {goal_id, current_level, suggested_level, action, reason}.
    Actions: "upgrade", "downgrade", or "hold".
    """
    recommendations: list[dict[str, Any]] = []
    level_order = ["untrusted", "cautious", "trusted", "autonomous"]

    for gid, data in trust_scores.items():
        score = data["trust_score"]
        samples = data.get("sample_sizes", {})
        total_samples = sum(samples.values())

        suggested = suggest_policy(score)

        # Determine current level from per-goal overrides, then config
        if state is not None:
            gpol = get_goal_policy(
                gid, state, current_approval_mode, current_tool_policy,
            )
            current_level = gpol["level"]
        else:
            current_level = "untrusted"
            for low, high, level, mode, policy in TRUST_LEVELS:
                if mode == current_approval_mode and policy == current_tool_policy:
                    current_level = level
                    break

        suggested_level = suggested["level"]
        cur_idx = level_order.index(current_level) if current_level in level_order else 0
        sug_idx = level_order.index(suggested_level) if suggested_level in level_order else 0

        if sug_idx > cur_idx and total_samples >= MIN_GRADUATION_SAMPLES:
            recommendations.append({
                "goal_id": gid,
                "current_level": current_level,
                "suggested_level": suggested_level,
                "suggested_approval_mode": suggested["approval_mode"],
                "suggested_tool_policy": suggested["tool_policy"],
                "trust_score": score,
                "action": "upgrade",
                "reason": (
                    f"Trust {score:.2f} ({total_samples} samples) "
                    f"warrants {suggested_level} (currently {current_level})"
                ),
            })
        elif sug_idx < cur_idx and total_samples >= MIN_GRADUATION_SAMPLES:
            recommendations.append({
                "goal_id": gid,
                "current_level": current_level,
                "suggested_level": suggested_level,
                "suggested_approval_mode": suggested["approval_mode"],
                "suggested_tool_policy": suggested["tool_policy"],
                "trust_score": score,
                "action": "downgrade",
                "reason": (
                    f"Trust {score:.2f} ({total_samples} samples) "
                    f"suggests regression to {suggested_level} (currently {current_level})"
                ),
            })

    return recommendations


def record_graduation_recommendations(
    state: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> None:
    """Persist graduation recommendations to goal_state.json."""
    if not recommendations:
        return
    recs = state.setdefault("graduation_recommendations", [])
    recs.append({
        "ts": datetime.now(timezone.utc).isoformat(),
        "recommendations": recommendations,
    })
    # Keep bounded (last 20 evaluations)
    if len(recs) > 20:
        state["graduation_recommendations"] = recs[-20:]


# ── Execution Reports ─────────────────────────────────────────

def build_execution_report(
    state: dict[str, Any],
    cycle: int,
    tasks_generated: int,
    tasks_approved: int,
    tasks_executed: int,
    trust_scores: dict[str, dict[str, Any]],
    graduation_recs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a structured post-cycle goal execution report."""
    # Verification stats from this cycle
    v_log = state.get("verification_log", [])
    recent_verdicts = v_log[-tasks_executed:] if tasks_executed > 0 else []
    pass_count = sum(1 for v in recent_verdicts if v.get("verdict") == "pass")
    fail_count = sum(1 for v in recent_verdicts if v.get("verdict") == "fail")

    # Step progress
    step_plans = state.get("step_plans", {})
    completed_steps = []
    failed_steps = []
    for sg_id, plan in step_plans.items():
        for s in plan.get("steps", []):
            if s.get("status") == "completed":
                completed_steps.append(f"{sg_id}.{s.get('step_id', '?')}")
            elif s.get("status") == "failed":
                failed_steps.append({
                    "step": f"{sg_id}.{s.get('step_id', '?')}",
                    "result": (s.get("result") or "")[:100],
                })

    # Compact trust summary
    trust_summary = {
        gid: data["trust_score"]
        for gid, data in trust_scores.items()
    }

    return {
        "cycle": cycle,
        "ts": datetime.now(timezone.utc).isoformat(),
        "tasks_generated": tasks_generated,
        "tasks_approved": tasks_approved,
        "tasks_executed": tasks_executed,
        "verification": {"pass": pass_count, "fail": fail_count},
        "trust_scores": trust_summary,
        "completed_steps": len(completed_steps),
        "failed_steps": len(failed_steps),
        "graduation_recommendations": graduation_recs or [],
    }


def record_execution_report(
    state: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """Persist execution report to goal_state.json."""
    reports = state.setdefault("execution_reports", [])
    reports.append(report)
    # Keep bounded (last 50 cycle reports)
    if len(reports) > 50:
        state["execution_reports"] = reports[-50:]


# ── Auto-Graduation (Layer 23) ────────────────────────────────

# Maps trust level names to concrete config values.
GRADUATION_POLICIES: dict[str, dict[str, str]] = {
    "untrusted": {"approval_mode": "review", "tool_policy": "read-only"},
    "cautious":  {"approval_mode": "review", "tool_policy": "supervised"},
    "trusted":   {"approval_mode": "notify", "tool_policy": "supervised"},
    "autonomous": {"approval_mode": "auto", "tool_policy": "full"},
}

GRADUATION_LEVEL_ORDER = ["untrusted", "cautious", "trusted", "autonomous"]

# Safety constraints for auto-graduation.
GRADUATION_COOLDOWN_CYCLES = 5   # Min cycles between upgrades
MIN_STABLE_SNAPSHOTS = 2         # Consecutive snapshots trust must hold
MAX_GRADUATION_HISTORY = 30      # Bounded history


def get_current_level_from_config(
    approval_mode: str,
    tool_policy: str,
) -> str:
    """Determine graduation level from config values."""
    for level, policy in GRADUATION_POLICIES.items():
        if policy["approval_mode"] == approval_mode and policy["tool_policy"] == tool_policy:
            return level
    # Partial match: fall back to approval_mode
    for level, policy in GRADUATION_POLICIES.items():
        if policy["approval_mode"] == approval_mode:
            return level
    return "untrusted"


def compute_effective_level(
    trust_scores: dict[str, dict[str, Any]],
) -> str:
    """Compute the minimum suggested trust level across goals with sufficient data.

    Returns the most conservative level among goals that have enough samples.
    If no goals have enough data, returns "untrusted".
    """
    levels: list[str] = []
    for gid, data in trust_scores.items():
        total = sum(data.get("sample_sizes", {}).values())
        if total >= MIN_GRADUATION_SAMPLES:
            policy = suggest_policy(data["trust_score"])
            levels.append(policy["level"])

    if not levels:
        return "untrusted"

    return min(levels, key=lambda l: GRADUATION_LEVEL_ORDER.index(l))


def check_graduation_eligibility(
    state: dict[str, Any],
    current_cycle: int,
    proposed_level: str,
    current_level: str,
    goal_id: str = "",
) -> tuple[bool, str]:
    """Check if auto-graduation is safe to apply.

    Returns (eligible, reason).
    When goal_id is provided, cooldown and stability checks are per-goal.
    """
    cur_idx = (
        GRADUATION_LEVEL_ORDER.index(current_level)
        if current_level in GRADUATION_LEVEL_ORDER else 0
    )
    prop_idx = (
        GRADUATION_LEVEL_ORDER.index(proposed_level)
        if proposed_level in GRADUATION_LEVEL_ORDER else 0
    )

    # No change needed
    if cur_idx == prop_idx:
        return False, "Already at suggested level"

    is_upgrade = prop_idx > cur_idx

    # Only one level at a time for upgrades
    if is_upgrade and (prop_idx - cur_idx) > 1:
        return False, (
            f"Multi-level upgrade not allowed ({current_level}→{proposed_level})"
        )

    # Cooldown only applies to upgrades (downgrades are safety-critical)
    if is_upgrade:
        history = state.get("graduation_history", [])
        # Per-goal cooldown: only check this goal's last graduation
        if goal_id:
            goal_history = [h for h in history if h.get("goal_id") == goal_id]
        else:
            goal_history = history
        if goal_history:
            cycles_since = current_cycle - goal_history[-1].get("cycle", 0)
            if cycles_since < GRADUATION_COOLDOWN_CYCLES:
                return False, (
                    f"Cooldown: {cycles_since}/{GRADUATION_COOLDOWN_CYCLES} cycles"
                )

    # Trust stability: recent snapshots must all support proposed level
    snapshots = state.get("trust_snapshots", [])
    if len(snapshots) < MIN_STABLE_SNAPSHOTS:
        return False, (
            f"Need {MIN_STABLE_SNAPSHOTS} trust snapshots "
            f"(have {len(snapshots)})"
        )

    if is_upgrade:
        recent = snapshots[-MIN_STABLE_SNAPSHOTS:]
        for snap in recent:
            for gid, trust_val in snap.get("scores", {}).items():
                # Per-goal stability: only check the goal being graduated
                if goal_id and gid != goal_id:
                    continue
                if isinstance(trust_val, (int, float)):
                    suggested = suggest_policy(trust_val)
                    sug_idx = (
                        GRADUATION_LEVEL_ORDER.index(suggested["level"])
                        if suggested["level"] in GRADUATION_LEVEL_ORDER else 0
                    )
                    if sug_idx < prop_idx:
                        return False, (
                            f"Trust unstable: {gid} at "
                            f"{suggested['level']} in recent snapshot"
                        )

    return True, "Eligible"


def apply_auto_graduation(
    state: dict[str, Any],
    current_cycle: int,
    new_level: str,
    old_level: str,
    trust_scores: dict[str, dict[str, Any]],
    goal_id: str = "",
) -> dict[str, Any]:
    """Apply graduation and record in history.

    Updates state with graduation_history entry and per-goal graduation_overrides.
    If goal_id is provided, stores override per-goal. Otherwise falls back to
    legacy global override (backward compat).
    Returns the graduation event dict.
    """
    new_policy = GRADUATION_POLICIES.get(
        new_level, GRADUATION_POLICIES["untrusted"],
    )
    old_idx = (
        GRADUATION_LEVEL_ORDER.index(old_level)
        if old_level in GRADUATION_LEVEL_ORDER else 0
    )
    new_idx = (
        GRADUATION_LEVEL_ORDER.index(new_level)
        if new_level in GRADUATION_LEVEL_ORDER else 0
    )

    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "cycle": current_cycle,
        "goal_id": goal_id,
        "old_level": old_level,
        "new_level": new_level,
        "action": "upgrade" if new_idx > old_idx else "downgrade",
        "approval_mode": new_policy["approval_mode"],
        "tool_policy": new_policy["tool_policy"],
        "trust_snapshot": {
            gid: data["trust_score"]
            for gid, data in trust_scores.items()
        },
    }

    # Record in history (bounded)
    history = state.setdefault("graduation_history", [])
    history.append(event)
    if len(history) > MAX_GRADUATION_HISTORY:
        state["graduation_history"] = history[-MAX_GRADUATION_HISTORY:]

    # Per-goal override (Layer 26)
    if goal_id:
        per_goal = state.setdefault("graduation_overrides", {})
        per_goal[goal_id] = {
            "level": new_level,
            "approval_mode": new_policy["approval_mode"],
            "tool_policy": new_policy["tool_policy"],
            "applied_at": event["ts"],
            "applied_cycle": current_cycle,
        }
    else:
        # Legacy global override (backward compat)
        state["graduation_overrides"] = {
            "level": new_level,
            "approval_mode": new_policy["approval_mode"],
            "tool_policy": new_policy["tool_policy"],
            "applied_at": event["ts"],
            "applied_cycle": current_cycle,
        }

    return event


def get_graduation_overrides(
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Read current graduation overrides from state, if any.

    Returns the overrides dict which may be:
    - Per-goal format (Layer 26): {goal_id: {level, approval_mode, ...}, ...}
    - Legacy global format: {level, approval_mode, ...}
    """
    overrides = state.get("graduation_overrides")
    if not overrides:
        return None
    # Per-goal format: dict of dicts where values have "level" key
    if any(isinstance(v, dict) and "level" in v for v in overrides.values()):
        return overrides
    # Legacy global format: single dict with "level" at top
    if "level" in overrides:
        return overrides
    return None


def is_per_goal_overrides(overrides: dict[str, Any]) -> bool:
    """Check if overrides dict uses per-goal format (Layer 26)."""
    if not overrides:
        return False
    return any(isinstance(v, dict) and "level" in v for v in overrides.values())


def get_goal_policy(
    goal_id: str,
    state: dict[str, Any],
    default_approval_mode: str = "review",
    default_tool_policy: str = "read-only",
) -> dict[str, str]:
    """Get effective policy for a specific goal.

    Checks per-goal graduation overrides first, then falls back to
    global overrides (legacy), then config defaults.

    Returns:
        {"approval_mode": str, "tool_policy": str, "level": str}
    """
    overrides = state.get("graduation_overrides", {})

    # Per-goal override (Layer 26)
    if goal_id and goal_id in overrides:
        goal_ov = overrides[goal_id]
        if isinstance(goal_ov, dict) and "level" in goal_ov:
            return {
                "approval_mode": goal_ov.get("approval_mode", default_approval_mode),
                "tool_policy": goal_ov.get("tool_policy", default_tool_policy),
                "level": goal_ov["level"],
            }

    # Legacy global override (backward compat)
    if "level" in overrides:
        return {
            "approval_mode": overrides.get("approval_mode", default_approval_mode),
            "tool_policy": overrides.get("tool_policy", default_tool_policy),
            "level": overrides["level"],
        }

    # Config defaults
    return {
        "approval_mode": default_approval_mode,
        "tool_policy": default_tool_policy,
        "level": get_current_level_from_config(default_approval_mode, default_tool_policy),
    }


def check_graduation_rollback(
    state: dict[str, Any],
    trust_scores: dict[str, dict[str, Any]],
    current_cycle: int,
) -> dict[str, Any] | None:
    """Check if current graduation level should be rolled back (legacy global).

    Returns rollback event if trust has degraded, None otherwise.
    Downgrades bypass cooldown (safety mechanism).
    """
    overrides = state.get("graduation_overrides")
    if not overrides:
        return None

    current_level = overrides.get("level", "untrusted")
    cur_idx = (
        GRADUATION_LEVEL_ORDER.index(current_level)
        if current_level in GRADUATION_LEVEL_ORDER else 0
    )

    if cur_idx == 0:
        return None  # Already at minimum

    effective = compute_effective_level(trust_scores)
    eff_idx = (
        GRADUATION_LEVEL_ORDER.index(effective)
        if effective in GRADUATION_LEVEL_ORDER else 0
    )

    if eff_idx < cur_idx:
        # Trust dropped — step down one level (conservative rollback)
        new_level = GRADUATION_LEVEL_ORDER[max(0, cur_idx - 1)]
        return apply_auto_graduation(
            state, current_cycle, new_level, current_level, trust_scores,
        )

    return None


def check_goal_graduation_rollback(
    state: dict[str, Any],
    goal_id: str,
    trust_data: dict[str, Any],
    current_cycle: int,
) -> dict[str, Any] | None:
    """Check if a specific goal's graduation should be rolled back (per-goal).

    Returns rollback event if trust dropped below current level.
    Downgrades bypass cooldown (safety mechanism).
    """
    overrides = state.get("graduation_overrides", {})
    goal_ov = overrides.get(goal_id)
    if not goal_ov or not isinstance(goal_ov, dict):
        return None

    current_level = goal_ov.get("level", "untrusted")
    cur_idx = (
        GRADUATION_LEVEL_ORDER.index(current_level)
        if current_level in GRADUATION_LEVEL_ORDER else 0
    )

    if cur_idx == 0:
        return None  # Already at minimum

    suggested = suggest_policy(trust_data["trust_score"])
    sug_idx = (
        GRADUATION_LEVEL_ORDER.index(suggested["level"])
        if suggested["level"] in GRADUATION_LEVEL_ORDER else 0
    )

    if sug_idx < cur_idx:
        new_level = GRADUATION_LEVEL_ORDER[max(0, cur_idx - 1)]
        return apply_auto_graduation(
            state, current_cycle, new_level, current_level,
            {goal_id: trust_data}, goal_id=goal_id,
        )

    return None


def format_graduation_history(
    state: dict[str, Any],
) -> str:
    """Format graduation history for CLI display."""
    history = state.get("graduation_history", [])
    overrides = state.get("graduation_overrides")

    lines: list[str] = ["## Auto-Graduation"]

    if overrides and is_per_goal_overrides(overrides):
        lines.append("  Per-goal overrides (Layer 26):")
        for gid, ov in sorted(overrides.items()):
            if isinstance(ov, dict) and "level" in ov:
                lines.append(
                    f"    {gid}: {ov.get('level', '?')} "
                    f"(approval={ov.get('approval_mode', '?')}, "
                    f"tools={ov.get('tool_policy', '?')}) "
                    f"cycle {ov.get('applied_cycle', '?')}"
                )
    elif overrides and "level" in overrides:
        level = overrides.get("level", "?")
        lines.append(
            f"  Active override: {level} "
            f"(approval={overrides.get('approval_mode', '?')}, "
            f"tools={overrides.get('tool_policy', '?')})"
        )
        lines.append(
            f"  Applied: cycle {overrides.get('applied_cycle', '?')} "
            f"@ {overrides.get('applied_at', '?')[:19]}"
        )
    else:
        lines.append("  No active graduation override (using config defaults)")

    if history:
        lines.append("")
        lines.append("  Recent graduation events:")
        for event in history[-5:]:
            arrow = "⬆" if event.get("action") == "upgrade" else "⬇"
            lines.append(
                f"    {arrow} cycle {event.get('cycle', '?')}: "
                f"{event.get('old_level', '?')} → {event.get('new_level', '?')} "
                f"({event.get('ts', '?')[:19]})"
            )

    return "\n".join(lines)
