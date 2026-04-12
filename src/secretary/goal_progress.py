"""Goal Progress Scoring — quantitative progress metrics for goal planning.

Computes a numerical "loss function" for each goal so the planner can see
how close it is to completion and whether progress has stalled.

Inspired by:
- TextGrad (Yuksekgonul 2024): loss signal as backward pass for planning
- Kambhampati (ICML 2024): LLMs need external verifiers / quantitative feedback
- Anthropic "Building Effective Agents": ground truth at each step

Architecture:
    goal_state.json + run_log  →  compute_progress()  →  GoalProgress per goal
    GoalProgress includes:
      - completion   (0.0-1.0: fraction of sub-goals done)
      - success_rate (0.0-1.0: pass rate of goal-originated tasks)
      - velocity     (change in completion since last snapshot)
      - stalled      (True if no progress across recent snapshots)
    →  rendered into planner prompt alongside reflections
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

from .run_log import RunLog

log = logging.getLogger("secretary.goal_progress")

MAX_SNAPSHOTS = 20
STALL_THRESHOLD = 3  # snapshots with zero velocity → stalled


@dataclass
class GoalProgress:
    """Quantitative progress metrics for a single goal."""

    goal_id: str
    completion: float  # 0.0-1.0: fraction of sub-goals done
    total_sub_goals: int
    done_sub_goals: int
    success_rate: float  # 0.0-1.0: task pass rate (NaN → -1.0 if no tasks)
    total_tasks: int
    velocity: float  # change in completion since last snapshot
    stalled: bool  # True if zero velocity for STALL_THRESHOLD snapshots


def compute_progress(
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
    run_log: RunLog,
    snapshots: list[dict[str, Any]],
) -> dict[str, GoalProgress]:
    """Compute progress metrics for all goals.

    Parameters
    ----------
    goals : list of goal dicts from GoalStore.goals
    sub_goal_overrides : GoalStore._state["sub_goal_status"]
    run_log : RunLog instance for task outcome data
    snapshots : previous progress snapshots from goal_state.json
    """
    # Gather all recent log entries with source="goals"
    all_entries = run_log.recent(200)
    goal_entries: dict[str, list] = {}
    for entry in all_entries:
        if entry.source == "goals" and entry.goal_id:
            goal_entries.setdefault(entry.goal_id, []).append(entry)

    # Previous snapshot for velocity calculation
    prev_snapshot = snapshots[-1] if snapshots else {}
    prev_completions = prev_snapshot.get("completions", {})

    result: dict[str, GoalProgress] = {}
    for goal in goals:
        gid = goal.get("id", "")
        if not gid:
            continue

        # 1. Sub-goal completion ratio
        sub_goals = goal.get("sub_goals", [])
        total = len(sub_goals)
        done = 0
        for sg in sub_goals:
            sg_id = sg.get("id", "")
            sg_status = sg.get("status", "not-started")
            override = sub_goal_overrides.get(sg_id)
            if override:
                sg_status = override.get("status", sg_status)
            if sg_status == "done":
                done += 1
        completion = done / total if total > 0 else 0.0

        # 2. Task success rate
        entries = goal_entries.get(gid, [])
        total_tasks = len(entries)
        if total_tasks > 0:
            success_rate = sum(1 for e in entries if e.success) / total_tasks
        else:
            success_rate = -1.0  # sentinel: no data

        # 3. Velocity (delta from previous snapshot)
        prev_completion = prev_completions.get(gid, 0.0)
        velocity = completion - prev_completion

        # 4. Stall detection
        stalled = _is_stalled(gid, completion, snapshots)

        result[gid] = GoalProgress(
            goal_id=gid,
            completion=round(completion, 3),
            total_sub_goals=total,
            done_sub_goals=done,
            success_rate=round(success_rate, 3) if success_rate >= 0 else -1.0,
            total_tasks=total_tasks,
            velocity=round(velocity, 3),
            stalled=stalled,
        )

    return result


def _is_stalled(
    goal_id: str,
    current_completion: float,
    snapshots: list[dict[str, Any]],
) -> bool:
    """Check if a goal has been stuck across recent snapshots."""
    if len(snapshots) < STALL_THRESHOLD:
        return False

    recent = snapshots[-STALL_THRESHOLD:]
    for snap in recent:
        prev = snap.get("completions", {}).get(goal_id, 0.0)
        if abs(current_completion - prev) > 0.001:
            return False
    return True


def record_snapshot(
    state: dict[str, Any],
    progress: dict[str, GoalProgress],
) -> None:
    """Record a progress snapshot into goal_state for trend tracking."""
    snapshots = state.setdefault("progress_snapshots", [])
    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "completions": {gid: gp.completion for gid, gp in progress.items()},
        "success_rates": {
            gid: gp.success_rate
            for gid, gp in progress.items()
            if gp.success_rate >= 0
        },
    }
    snapshots.append(snapshot)
    if len(snapshots) > MAX_SNAPSHOTS:
        state["progress_snapshots"] = snapshots[-MAX_SNAPSHOTS:]


def format_progress_section(progress: dict[str, GoalProgress]) -> str:
    """Render progress metrics as text for the planner prompt."""
    if not progress:
        return ""

    lines = [
        "## Goal Progress Metrics",
        "(Quantitative signals — use these to prioritize stalled goals "
        "and validate that tasks are actually moving the needle.)",
    ]

    for gp in sorted(progress.values(), key=lambda g: g.completion):
        # Status indicator
        if gp.stalled:
            indicator = "STALLED"
        elif gp.velocity > 0:
            indicator = "ADVANCING"
        elif gp.velocity < 0:
            indicator = "REGRESSING"
        else:
            indicator = "STEADY"

        # Completion bar (visual)
        filled = int(gp.completion * 10)
        bar = "#" * filled + "-" * (10 - filled)

        line = f"- **{gp.goal_id}**: [{bar}] {gp.completion:.0%}"
        line += f" ({gp.done_sub_goals}/{gp.total_sub_goals} sub-goals)"

        if gp.success_rate >= 0:
            line += f" | tasks: {gp.success_rate:.0%} pass ({gp.total_tasks})"
        else:
            line += " | tasks: no data yet"

        line += f" | {indicator}"
        if gp.velocity != 0:
            sign = "+" if gp.velocity > 0 else ""
            line += f" ({sign}{gp.velocity:.0%})"

        lines.append(line)

    # Highlight stalled goals
    stalled = [gp.goal_id for gp in progress.values() if gp.stalled]
    if stalled:
        lines.append(
            f"\n**Warning**: {', '.join(stalled)} "
            f"{'has' if len(stalled) == 1 else 'have'} shown no progress "
            f"for {STALL_THRESHOLD}+ reviews. Consider changing strategy."
        )

    return "\n".join(lines)
