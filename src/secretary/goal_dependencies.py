"""Goal Dependencies — prerequisite enforcement for sub-goal ordering.

Layer 25 of the goal actualization stack.

Research basis:
- HuggingGPT (Shen 2023): Task decomposition with explicit ``dep`` field
  for inter-task dependencies — planner can't reorder past prerequisites.
- LLM-Modulo (Kambhampati 2024): LLMs can't reliably self-enforce constraints;
  external verifiers must check dependency satisfaction before execution.
- Anthropic "Building Effective Agents": Ground truth from the environment at
  each step — don't trust advisory prompts alone.

Architecture:
    goals.yaml ``depends_on`` field per sub-goal
    + goal_state.json ``sub_goal_status`` overrides
    → build_dependency_graph() → per-sub-goal prerequisite list
    → check_prerequisites_met() → bool per sub-goal
    → filter_blocked_sub_goals() → removes ineligible from decomposition
    → format_dependency_section() → planner prompt injection

    Enforcement points:
    1. goal_decomposition.find_decomposable_sub_goals() — skip decomposition
       for sub-goals whose depends_on items aren't done.
    2. goal_scheduler.select_active_goals() — skip goals where ALL sub-goals
       are dependency-blocked (nothing can advance).
    3. goals._build_goal_prompt() — add dependency status section so the LLM
       planner sees what's blocked and why.

    Prior art in this codebase: watcher.py campaign tasks already have a
    ``depends_on`` field with hard enforcement (lines 1018-1040).  This module
    brings the same pattern to the goal/sub-goal layer.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("secretary.goal_dependencies")


def build_dependency_graph(
    goals: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Build mapping from sub_goal_id → list of prerequisite sub_goal_ids.

    Reads the ``depends_on`` field from each sub-goal in goals.yaml.
    Returns empty list for sub-goals with no dependencies.
    """
    graph: dict[str, list[str]] = {}
    for goal in goals:
        for sg in goal.get("sub_goals", []):
            sg_id = sg.get("id", "")
            if sg_id:
                deps = sg.get("depends_on", [])
                graph[sg_id] = list(deps) if isinstance(deps, list) else []
    return graph


def _get_effective_status(
    sg_id: str,
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
) -> str:
    """Resolve effective status for a sub-goal (state override > YAML)."""
    override = sub_goal_overrides.get(sg_id)
    if override:
        return override.get("status", "not-started")
    for goal in goals:
        for sg in goal.get("sub_goals", []):
            if sg.get("id") == sg_id:
                return sg.get("status", "not-started")
    return "not-started"


def check_prerequisites_met(
    sg_id: str,
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
) -> bool:
    """Return True if all depends_on prerequisites are 'done'."""
    graph = build_dependency_graph(goals)
    deps = graph.get(sg_id, [])
    if not deps:
        return True
    return all(
        _get_effective_status(d, goals, sub_goal_overrides) == "done"
        for d in deps
    )


def get_unmet_prerequisites(
    sg_id: str,
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
) -> list[dict[str, str]]:
    """Return list of unmet prerequisites with their current status.

    Each entry: {"id": "...", "status": "..."}.
    Empty list means all prerequisites met (or no dependencies).
    """
    graph = build_dependency_graph(goals)
    deps = graph.get(sg_id, [])
    unmet = []
    for d in deps:
        status = _get_effective_status(d, goals, sub_goal_overrides)
        if status != "done":
            unmet.append({"id": d, "status": status})
    return unmet


def filter_blocked_sub_goals(
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Filter out sub-goals whose dependencies aren't met.

    Args:
        candidates: list of (sub_goal_dict, parent_goal_dict) from
            find_decomposable_sub_goals().
        goals: all goal definitions for dependency graph.
        sub_goal_overrides: from goal_state.json["sub_goal_status"].

    Returns:
        Filtered list with only sub-goals whose prerequisites are satisfied.
    """
    result = []
    for sg, parent in candidates:
        sg_id = sg.get("id", "")
        if check_prerequisites_met(sg_id, goals, sub_goal_overrides):
            result.append((sg, parent))
        else:
            unmet = get_unmet_prerequisites(sg_id, goals, sub_goal_overrides)
            unmet_ids = [u["id"] for u in unmet]
            log.info(
                "Dependency block: %s waiting on %s",
                sg_id, ", ".join(unmet_ids),
            )
    return result


def has_any_unblocked_sub_goal(
    goal: dict[str, Any],
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
) -> bool:
    """Return True if the goal has at least one sub-goal that can advance.

    A sub-goal can advance if:
    - It's not done or blocked
    - Its dependencies are met
    """
    for sg in goal.get("sub_goals", []):
        sg_id = sg.get("id", "")
        status = _get_effective_status(sg_id, goals, sub_goal_overrides)
        if status in ("done", "blocked"):
            continue
        if check_prerequisites_met(sg_id, goals, sub_goal_overrides):
            return True
    return False


def format_dependency_section(
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
) -> str:
    """Render a dependency status section for the planner prompt.

    Shows which sub-goals are blocked by unmet prerequisites so the
    LLM planner can focus on unblocked work.
    """
    graph = build_dependency_graph(goals)

    # Only include sub-goals that actually have dependencies
    blocked_entries: list[str] = []
    ready_entries: list[str] = []

    for goal in goals:
        for sg in goal.get("sub_goals", []):
            sg_id = sg.get("id", "")
            deps = graph.get(sg_id, [])
            if not deps:
                continue  # No dependencies to show

            unmet = get_unmet_prerequisites(sg_id, goals, sub_goal_overrides)
            if unmet:
                unmet_str = ", ".join(
                    f"{u['id']} ({u['status']})" for u in unmet
                )
                blocked_entries.append(
                    f"  - **{sg_id}** BLOCKED — waiting on: {unmet_str}"
                )
            else:
                ready_entries.append(
                    f"  - **{sg_id}** READY — all prerequisites met"
                )

    if not blocked_entries and not ready_entries:
        return ""

    parts = ["## Sub-Goal Dependencies"]
    parts.append(
        "(Sub-goals with declared prerequisites. "
        "Do NOT generate tasks for BLOCKED sub-goals — "
        "work on their prerequisites first.)"
    )
    if blocked_entries:
        parts.append("\nBlocked:")
        parts.extend(blocked_entries)
    if ready_entries:
        parts.append("\nReady (prerequisites satisfied):")
        parts.extend(ready_entries)

    return "\n".join(parts)
