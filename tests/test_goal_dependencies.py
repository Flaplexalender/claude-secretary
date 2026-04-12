"""Tests for Layer 25: goal_dependencies.py — Goal Dependency & Prerequisite Enforcement."""

from __future__ import annotations

import pytest

from secretary.goal_dependencies import (
    build_dependency_graph,
    check_prerequisites_met,
    filter_blocked_sub_goals,
    format_dependency_section,
    get_unmet_prerequisites,
    has_any_unblocked_sub_goal,
    _get_effective_status,
)


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture()
def goals_with_deps() -> list[dict]:
    """Goals with depends_on fields matching the real goals.yaml pattern."""
    return [
        {
            "id": "self-sustaining-autonomy",
            "priority": 2,
            "status": "in-progress",
            "sub_goals": [
                {"id": "event-bus", "status": "done"},
                {"id": "ooda-loop", "status": "done"},
                {"id": "goal-planner", "status": "in-progress"},
                {
                    "id": "live-integration-test",
                    "status": "not-started",
                    "depends_on": ["event-bus", "ooda-loop", "goal-planner"],
                },
                {
                    "id": "autonomous-ratio",
                    "status": "not-started",
                    "depends_on": ["live-integration-test"],
                },
            ],
        },
        {
            "id": "self-improvement",
            "priority": 3,
            "status": "in-progress",
            "sub_goals": [
                {"id": "failure-analysis", "status": "in-progress"},
                {
                    "id": "code-review",
                    "status": "not-started",
                    "depends_on": ["failure-analysis"],
                },
                {
                    "id": "test-coverage",
                    "status": "not-started",
                    "depends_on": ["failure-analysis"],
                },
            ],
        },
    ]


@pytest.fixture()
def goals_no_deps() -> list[dict]:
    """Goals with no depends_on fields."""
    return [
        {
            "id": "prefix-survival",
            "priority": 1,
            "status": "in-progress",
            "sub_goals": [
                {"id": "oracle-production", "status": "done"},
                {"id": "learned-router", "status": "done"},
                {"id": "cost-monitoring", "status": "not-started"},
            ],
        },
    ]


# ── build_dependency_graph ───────────────────────────────────

class TestBuildDependencyGraph:
    def test_extracts_depends_on(self, goals_with_deps):
        graph = build_dependency_graph(goals_with_deps)
        assert graph["live-integration-test"] == ["event-bus", "ooda-loop", "goal-planner"]
        assert graph["autonomous-ratio"] == ["live-integration-test"]
        assert graph["code-review"] == ["failure-analysis"]
        assert graph["test-coverage"] == ["failure-analysis"]

    def test_empty_for_no_deps(self, goals_with_deps):
        graph = build_dependency_graph(goals_with_deps)
        assert graph["event-bus"] == []
        assert graph["ooda-loop"] == []
        assert graph["goal-planner"] == []

    def test_empty_goals(self):
        assert build_dependency_graph([]) == {}

    def test_goals_without_depends_on_field(self, goals_no_deps):
        graph = build_dependency_graph(goals_no_deps)
        assert all(v == [] for v in graph.values())

    def test_non_list_depends_on_ignored(self):
        """If depends_on is somehow not a list, treat as no deps."""
        goals = [{"id": "g", "sub_goals": [
            {"id": "sg1", "depends_on": "not-a-list"},
        ]}]
        graph = build_dependency_graph(goals)
        assert graph["sg1"] == []


# ── _get_effective_status ────────────────────────────────────

class TestGetEffectiveStatus:
    def test_yaml_status(self, goals_with_deps):
        assert _get_effective_status("event-bus", goals_with_deps, {}) == "done"
        assert _get_effective_status("goal-planner", goals_with_deps, {}) == "in-progress"
        assert _get_effective_status(
            "live-integration-test", goals_with_deps, {},
        ) == "not-started"

    def test_override_takes_precedence(self, goals_with_deps):
        overrides = {"goal-planner": {"status": "done"}}
        assert _get_effective_status("goal-planner", goals_with_deps, overrides) == "done"

    def test_unknown_id_defaults(self, goals_with_deps):
        assert _get_effective_status("nonexistent", goals_with_deps, {}) == "not-started"


# ── check_prerequisites_met ──────────────────────────────────

class TestCheckPrerequisitesMet:
    def test_no_deps_always_met(self, goals_with_deps):
        assert check_prerequisites_met("event-bus", goals_with_deps, {}) is True
        assert check_prerequisites_met("goal-planner", goals_with_deps, {}) is True

    def test_deps_not_met(self, goals_with_deps):
        # goal-planner is in-progress, so live-integration-test is blocked
        assert check_prerequisites_met(
            "live-integration-test", goals_with_deps, {},
        ) is False

    def test_deps_met_via_yaml(self):
        goals = [{"id": "g", "sub_goals": [
            {"id": "a", "status": "done"},
            {"id": "b", "status": "done"},
            {"id": "c", "status": "not-started", "depends_on": ["a", "b"]},
        ]}]
        assert check_prerequisites_met("c", goals, {}) is True

    def test_deps_met_via_override(self, goals_with_deps):
        overrides = {"goal-planner": {"status": "done"}}
        assert check_prerequisites_met(
            "live-integration-test", goals_with_deps, overrides,
        ) is True

    def test_chain_blocked(self, goals_with_deps):
        # autonomous-ratio depends on live-integration-test which is not-started
        assert check_prerequisites_met(
            "autonomous-ratio", goals_with_deps, {},
        ) is False

    def test_chain_unblocked_via_override(self, goals_with_deps):
        overrides = {"live-integration-test": {"status": "done"}}
        assert check_prerequisites_met(
            "autonomous-ratio", goals_with_deps, overrides,
        ) is True


# ── get_unmet_prerequisites ──────────────────────────────────

class TestGetUnmetPrerequisites:
    def test_no_deps_empty(self, goals_with_deps):
        assert get_unmet_prerequisites("event-bus", goals_with_deps, {}) == []

    def test_returns_unmet_with_status(self, goals_with_deps):
        unmet = get_unmet_prerequisites(
            "live-integration-test", goals_with_deps, {},
        )
        assert len(unmet) == 1
        assert unmet[0] == {"id": "goal-planner", "status": "in-progress"}

    def test_all_met_empty(self):
        goals = [{"id": "g", "sub_goals": [
            {"id": "a", "status": "done"},
            {"id": "b", "status": "not-started", "depends_on": ["a"]},
        ]}]
        assert get_unmet_prerequisites("b", goals, {}) == []

    def test_multiple_unmet(self):
        goals = [{"id": "g", "sub_goals": [
            {"id": "a", "status": "not-started"},
            {"id": "b", "status": "in-progress"},
            {"id": "c", "status": "not-started", "depends_on": ["a", "b"]},
        ]}]
        unmet = get_unmet_prerequisites("c", goals, {})
        assert len(unmet) == 2


# ── filter_blocked_sub_goals ─────────────────────────────────

class TestFilterBlockedSubGoals:
    def test_removes_blocked(self, goals_with_deps):
        sg_lit = goals_with_deps[0]["sub_goals"][3]  # live-integration-test
        sg_ar = goals_with_deps[0]["sub_goals"][4]   # autonomous-ratio
        sg_gp = goals_with_deps[0]["sub_goals"][2]   # goal-planner (no deps)
        parent = goals_with_deps[0]

        candidates = [(sg_lit, parent), (sg_ar, parent), (sg_gp, parent)]
        result = filter_blocked_sub_goals(candidates, goals_with_deps, {})

        ids = [sg.get("id") for sg, _ in result]
        assert "goal-planner" in ids
        assert "live-integration-test" not in ids
        assert "autonomous-ratio" not in ids

    def test_keeps_all_when_met(self, goals_with_deps):
        overrides = {
            "goal-planner": {"status": "done"},
            "live-integration-test": {"status": "done"},
        }
        sg_lit = goals_with_deps[0]["sub_goals"][3]
        sg_ar = goals_with_deps[0]["sub_goals"][4]
        parent = goals_with_deps[0]

        candidates = [(sg_lit, parent), (sg_ar, parent)]
        result = filter_blocked_sub_goals(candidates, goals_with_deps, overrides)
        assert len(result) == 2

    def test_empty_candidates(self, goals_with_deps):
        assert filter_blocked_sub_goals([], goals_with_deps, {}) == []


# ── has_any_unblocked_sub_goal ───────────────────────────────

class TestHasAnyUnblockedSubGoal:
    def test_goal_with_unblocked(self, goals_with_deps):
        # self-sustaining-autonomy has goal-planner (in-progress, no deps)
        assert has_any_unblocked_sub_goal(
            goals_with_deps[0], goals_with_deps, {},
        ) is True

    def test_goal_all_blocked(self):
        """Goal where all non-done sub-goals have unmet dependencies."""
        goals = [{
            "id": "test-goal",
            "sub_goals": [
                {"id": "a", "status": "not-started"},  # dep on "z" which doesn't exist → not-started
                {"id": "b", "status": "not-started", "depends_on": ["a"]},
            ],
        }]
        # "a" has no deps, so it's unblocked
        assert has_any_unblocked_sub_goal(goals[0], goals, {}) is True

    def test_fully_done_goal(self):
        goals = [{
            "id": "done-goal",
            "sub_goals": [
                {"id": "a", "status": "done"},
                {"id": "b", "status": "done", "depends_on": ["a"]},
            ],
        }]
        # All done → no sub-goal can advance → False
        assert has_any_unblocked_sub_goal(goals[0], goals, {}) is False

    def test_all_blocked_by_deps(self):
        """All non-done sub-goals depend on something not done."""
        goals = [{
            "id": "stuck",
            "sub_goals": [
                {"id": "x", "status": "blocked"},
                {"id": "y", "status": "not-started", "depends_on": ["x"]},
            ],
        }]
        # x is blocked, y depends on x → nothing can advance
        assert has_any_unblocked_sub_goal(goals[0], goals, {}) is False

    def test_goal_no_sub_goals(self):
        goals = [{"id": "empty", "sub_goals": []}]
        assert has_any_unblocked_sub_goal(goals[0], goals, {}) is False


# ── format_dependency_section ────────────────────────────────

class TestFormatDependencySection:
    def test_shows_blocked(self, goals_with_deps):
        section = format_dependency_section(goals_with_deps, {})
        assert "## Sub-Goal Dependencies" in section
        assert "live-integration-test" in section
        assert "BLOCKED" in section
        assert "goal-planner (in-progress)" in section

    def test_shows_ready_when_met(self, goals_with_deps):
        overrides = {
            "goal-planner": {"status": "done"},
        }
        section = format_dependency_section(goals_with_deps, overrides)
        assert "live-integration-test" in section
        assert "READY" in section

    def test_empty_when_no_deps(self, goals_no_deps):
        section = format_dependency_section(goals_no_deps, {})
        assert section == ""

    def test_instruction_text(self, goals_with_deps):
        section = format_dependency_section(goals_with_deps, {})
        assert "Do NOT generate tasks for BLOCKED sub-goals" in section


# ── Integration: select_active_goals with dependencies ───────

class TestSelectActiveGoalsWithDeps:
    def test_skips_fully_blocked_goals(self):
        """Goal where all sub-goals are dependency-blocked gets excluded."""
        from secretary.goal_scheduler import select_active_goals

        goals = [
            {
                "id": "unblocked",
                "priority": 1,
                "status": "in-progress",
                "sub_goals": [
                    {"id": "a", "status": "not-started"},  # no deps, can advance
                ],
            },
            {
                "id": "all-blocked",
                "priority": 2,
                "status": "in-progress",
                "sub_goals": [
                    {"id": "x", "status": "blocked"},
                    {"id": "y", "status": "not-started", "depends_on": ["x"]},
                ],
            },
        ]
        result = select_active_goals(goals, curriculum_level=2)
        ids = [g["id"] for g in result]
        assert "unblocked" in ids
        assert "all-blocked" not in ids

    def test_keeps_goal_with_some_unblocked(self):
        from secretary.goal_scheduler import select_active_goals

        goals = [{
            "id": "partial",
            "priority": 1,
            "status": "in-progress",
            "sub_goals": [
                {"id": "a", "status": "in-progress"},  # no deps
                {"id": "b", "status": "not-started", "depends_on": ["a"]},
            ],
        }]
        result = select_active_goals(goals, curriculum_level=2)
        assert len(result) == 1

    def test_overrides_applied(self):
        from secretary.goal_scheduler import select_active_goals

        goals = [{
            "id": "g",
            "priority": 1,
            "status": "in-progress",
            "sub_goals": [
                {"id": "x", "status": "blocked"},
                {"id": "y", "status": "not-started", "depends_on": ["x"]},
            ],
        }]
        # Without override: blocked
        assert len(select_active_goals(goals, curriculum_level=2)) == 0
        # With override marking x as done: unblocked
        overrides = {"x": {"status": "done"}}
        result = select_active_goals(
            goals, curriculum_level=2, sub_goal_overrides=overrides,
        )
        assert len(result) == 1
