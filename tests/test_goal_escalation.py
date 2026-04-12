"""Tests for goal_escalation.py — Stall Escalation Engine (Layer 8)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.goal_escalation import (
    ESCALATION_COOLDOWN_SNAPSHOTS,
    LEVEL_DIAGNOSE,
    LEVEL_REDECOMPOSE,
    LEVEL_REPRIORITIZE,
    LEVEL_SHELVE,
    MAX_DIAGNOSES,
    MAX_REDECOMPOSITIONS,
    EscalationAction,
    _build_diagnosis_prompt,
    _collect_failure_logs,
    _find_blocked_sub_goals,
    _get_escalation_state,
    _parse_json_response,
    _should_escalate,
    choose_escalation_level,
    evaluate_escalation,
    evaluate_escalations,
)
from secretary.goal_progress import GoalProgress


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_goal(
    goal_id: str = "g1",
    sub_goals: list[dict] | None = None,
) -> dict[str, Any]:
    if sub_goals is None:
        sub_goals = [
            {"id": "sg1", "description": "Sub-goal 1", "status": "in-progress"},
            {"id": "sg2", "description": "Sub-goal 2", "status": "not-started"},
        ]
    return {
        "id": goal_id,
        "description": f"Goal {goal_id}",
        "priority": 2,
        "success_criteria": f"Criteria for {goal_id}",
        "sub_goals": sub_goals,
    }


def _make_progress(
    goal_id: str = "g1",
    stalled: bool = True,
    completion: float = 0.3,
    velocity: float = 0.0,
) -> GoalProgress:
    return GoalProgress(
        goal_id=goal_id,
        completion=completion,
        total_sub_goals=2,
        done_sub_goals=0,
        success_rate=0.5,
        total_tasks=4,
        velocity=velocity,
        stalled=stalled,
    )


def _make_state(
    snapshots: int = 5,
    blocked_sg: str | None = None,
    step_plans: dict | None = None,
) -> dict[str, Any]:
    """Build a minimal goal_state dict."""
    state: dict[str, Any] = {
        "sub_goal_status": {},
        "progress_snapshots": [
            {"ts": f"2026-01-0{i+1}T00:00:00Z", "completions": {"g1": 0.3}}
            for i in range(snapshots)
        ],
        "progress_notes": [],
        "step_plans": step_plans or {},
    }
    if blocked_sg:
        state["sub_goal_status"][blocked_sg] = {
            "status": "blocked",
            "evidence": "test block",
            "updated": "2026-01-01T00:00:00Z",
        }
    return state


# ---------------------------------------------------------------------------
# _get_escalation_state
# ---------------------------------------------------------------------------


class TestGetEscalationState:
    def test_creates_default_state(self):
        state: dict[str, Any] = {}
        esc = _get_escalation_state(state, "g1")
        assert esc["level"] == LEVEL_DIAGNOSE
        assert esc["diagnoses"] == 0
        assert esc["redecompositions"] == 0
        assert esc["history"] == []
        assert "escalation_state" in state

    def test_returns_existing_state(self):
        state = {"escalation_state": {"g1": {"level": 2, "diagnoses": 1,
                 "redecompositions": 0, "last_escalation_ts": None,
                 "last_escalation_snapshot": -1, "diagnosis": "",
                 "history": []}}}
        esc = _get_escalation_state(state, "g1")
        assert esc["level"] == 2
        assert esc["diagnoses"] == 1

    def test_separate_goals_get_separate_state(self):
        state: dict[str, Any] = {}
        esc1 = _get_escalation_state(state, "g1")
        esc2 = _get_escalation_state(state, "g2")
        esc1["diagnoses"] = 5
        assert esc2["diagnoses"] == 0


# ---------------------------------------------------------------------------
# _should_escalate (cooldown check)
# ---------------------------------------------------------------------------


class TestShouldEscalate:
    def test_first_time_always_true(self):
        esc = {"last_escalation_snapshot": -1}
        assert _should_escalate(esc, 0) is True

    def test_within_cooldown_false(self):
        esc = {"last_escalation_snapshot": 3}
        assert _should_escalate(esc, 4) is False

    def test_after_cooldown_true(self):
        esc = {"last_escalation_snapshot": 3}
        assert _should_escalate(esc, 3 + ESCALATION_COOLDOWN_SNAPSHOTS) is True

    def test_exact_boundary_true(self):
        esc = {"last_escalation_snapshot": 0}
        assert _should_escalate(esc, ESCALATION_COOLDOWN_SNAPSHOTS) is True


# ---------------------------------------------------------------------------
# choose_escalation_level
# ---------------------------------------------------------------------------


class TestChooseEscalationLevel:
    def test_fresh_state_returns_diagnose(self):
        esc = {"diagnoses": 0, "redecompositions": 0, "level": 0}
        assert choose_escalation_level(esc) == LEVEL_DIAGNOSE

    def test_after_one_diagnose_still_diagnose(self):
        esc = {"diagnoses": 1, "redecompositions": 0, "level": 0}
        assert choose_escalation_level(esc) == LEVEL_DIAGNOSE

    def test_after_max_diagnoses_goes_to_redecompose(self):
        esc = {"diagnoses": MAX_DIAGNOSES, "redecompositions": 0, "level": 0}
        assert choose_escalation_level(esc) == LEVEL_REDECOMPOSE

    def test_after_redecompose_goes_to_reprioritize(self):
        esc = {"diagnoses": MAX_DIAGNOSES, "redecompositions": MAX_REDECOMPOSITIONS,
               "level": LEVEL_REDECOMPOSE}
        assert choose_escalation_level(esc) == LEVEL_REPRIORITIZE

    def test_after_reprioritize_goes_to_shelve(self):
        esc = {"diagnoses": MAX_DIAGNOSES, "redecompositions": MAX_REDECOMPOSITIONS,
               "level": LEVEL_REPRIORITIZE}
        assert choose_escalation_level(esc) == LEVEL_SHELVE

    def test_full_ladder_terminates_at_shelve(self):
        esc = {"diagnoses": MAX_DIAGNOSES, "redecompositions": MAX_REDECOMPOSITIONS,
               "level": LEVEL_SHELVE}
        assert choose_escalation_level(esc) == LEVEL_SHELVE


# ---------------------------------------------------------------------------
# _collect_failure_logs
# ---------------------------------------------------------------------------


class TestCollectFailureLogs:
    def test_empty_state_returns_empty(self):
        state = {"step_plans": {}}
        goal = _make_goal()
        assert _collect_failure_logs(state, goal) == {}

    def test_gathers_from_step_plans(self):
        logs = [{"step_id": "sg1.1", "strategy": "retry", "analysis": "transient"}]
        state = {"step_plans": {"sg1": {"failure_log": logs}}}
        goal = _make_goal()
        result = _collect_failure_logs(state, goal)
        assert "sg1" in result
        assert result["sg1"] == logs

    def test_ignores_plans_without_failures(self):
        state = {"step_plans": {"sg1": {"steps": []}, "sg2": {"failure_log": []}}}
        goal = _make_goal()
        result = _collect_failure_logs(state, goal)
        assert result == {}


# ---------------------------------------------------------------------------
# _find_blocked_sub_goals
# ---------------------------------------------------------------------------


class TestFindBlockedSubGoals:
    def test_no_blocked(self):
        state = {"sub_goal_status": {}, "step_plans": {}}
        goal = _make_goal()
        assert _find_blocked_sub_goals(goal, state) == []

    def test_blocked_via_status_override(self):
        state = {
            "sub_goal_status": {
                "sg1": {"status": "blocked", "evidence": "test"},
            },
            "step_plans": {},
        }
        goal = _make_goal()
        assert _find_blocked_sub_goals(goal, state) == ["sg1"]

    def test_blocked_via_step_plan(self):
        state = {
            "sub_goal_status": {},
            "step_plans": {"sg2": {"blocked": True}},
        }
        goal = _make_goal()
        assert _find_blocked_sub_goals(goal, state) == ["sg2"]

    def test_both_override_and_plan_blocked(self):
        state = {
            "sub_goal_status": {
                "sg1": {"status": "blocked", "evidence": "test"},
            },
            "step_plans": {"sg2": {"blocked": True}},
        }
        goal = _make_goal()
        blocked = _find_blocked_sub_goals(goal, state)
        assert "sg1" in blocked
        assert "sg2" in blocked


# ---------------------------------------------------------------------------
# _build_diagnosis_prompt
# ---------------------------------------------------------------------------


class TestBuildDiagnosisPrompt:
    def test_includes_goal_info(self):
        goal = _make_goal()
        progress = _make_progress()
        prompt = _build_diagnosis_prompt(
            goal, {}, {}, {}, progress, [],
        )
        assert "g1" in prompt
        assert "Goal g1" in prompt
        assert "30%" in prompt

    def test_includes_sub_goal_statuses(self):
        goal = _make_goal()
        progress = _make_progress()
        prompt = _build_diagnosis_prompt(
            goal, {"sg1": {"status": "blocked"}}, {}, {}, progress, [],
        )
        assert "sg1" in prompt
        assert "blocked" in prompt

    def test_includes_failure_logs(self):
        goal = _make_goal()
        progress = _make_progress()
        failure_logs = {
            "sg1": [{"strategy": "retry", "analysis": "file not found"}],
        }
        prompt = _build_diagnosis_prompt(
            goal, {}, {}, failure_logs, progress, [],
        )
        assert "file not found" in prompt

    def test_includes_step_plan_summary(self):
        goal = _make_goal()
        progress = _make_progress()
        step_plans = {
            "sg1": {
                "blocked": True,
                "block_reason": "retries exhausted",
                "steps": [
                    {"step_id": "sg1.1", "status": "completed"},
                    {"step_id": "sg1.2", "status": "failed"},
                ],
            }
        }
        prompt = _build_diagnosis_prompt(
            goal, {}, step_plans, {}, progress, [],
        )
        assert "BLOCKED" in prompt
        assert "retries exhausted" in prompt

    def test_includes_escalation_history(self):
        goal = _make_goal()
        progress = _make_progress()
        history = [{"strategy": "diagnose", "summary": "Tried before"}]
        prompt = _build_diagnosis_prompt(
            goal, {}, {}, {}, progress, history,
        )
        assert "Tried before" in prompt


# ---------------------------------------------------------------------------
# _parse_json_response
# ---------------------------------------------------------------------------


class TestParseJsonResponse:
    def test_plain_json(self):
        result = _parse_json_response('{"root_cause": "test"}')
        assert result == {"root_cause": "test"}

    def test_markdown_fenced_json(self):
        result = _parse_json_response('```json\n{"root_cause": "test"}\n```')
        assert result == {"root_cause": "test"}

    def test_text_before_json(self):
        result = _parse_json_response('Some text {"root_cause": "test"} more text')
        assert result == {"root_cause": "test"}

    def test_invalid_json_returns_none(self):
        assert _parse_json_response("not json at all") is None


# ---------------------------------------------------------------------------
# evaluate_escalation — async tests
# ---------------------------------------------------------------------------


class TestEvaluateEscalation:
    """Test the main escalation entry point with mocked LLM calls."""

    @pytest.mark.asyncio
    async def test_cooldown_returns_none(self):
        """Escalation within cooldown returns None."""
        goal = _make_goal()
        progress = _make_progress()
        state = _make_state(snapshots=5)
        # Prime escalation state with recent escalation
        state["escalation_state"] = {
            "g1": {
                "level": 0,
                "diagnoses": 0,
                "redecompositions": 0,
                "last_escalation_ts": "2026-01-01T00:00:00Z",
                "last_escalation_snapshot": 4,  # same as current
                "diagnosis": "",
                "history": [],
            }
        }
        result = await evaluate_escalation(goal, progress, state)
        assert result is None

    @pytest.mark.asyncio
    async def test_shelve_no_llm_call(self):
        """Shelve strategy doesn't call LLM."""
        goal = _make_goal()
        progress = _make_progress()
        state = _make_state(snapshots=10)
        state["escalation_state"] = {
            "g1": {
                "level": LEVEL_REPRIORITIZE,
                "diagnoses": MAX_DIAGNOSES,
                "redecompositions": MAX_REDECOMPOSITIONS,
                "last_escalation_ts": None,
                "last_escalation_snapshot": -1,
                "diagnosis": "",
                "history": [],
            }
        }
        result = await evaluate_escalation(goal, progress, state)
        assert result is not None
        assert result.strategy == "shelve"
        assert result.goal_id == "g1"
        assert len(result.sub_goal_updates) > 0
        # All non-done sub-goals should be blocked
        for u in result.sub_goal_updates:
            assert u["new_status"] == "blocked"

    @pytest.mark.asyncio
    async def test_diagnose_with_llm(self):
        """Diagnose calls LLM and returns corrective tasks."""
        goal = _make_goal()
        progress = _make_progress()
        state = _make_state(snapshots=5)

        diagnosis_response = {
            "root_cause": "Tests are failing due to missing config",
            "blocked_by": ["missing config file"],
            "recommendation": "diagnose_deeper",
            "corrective_tasks": [
                {"prompt": "Create missing config file", "tier": "low"},
            ],
            "sub_goal_changes": [],
        }

        with patch(
            "secretary.goal_escalation._run_diagnosis",
            new_callable=AsyncMock,
            return_value=diagnosis_response,
        ):
            result = await evaluate_escalation(goal, progress, state)

        assert result is not None
        assert result.strategy == "diagnose"
        assert len(result.tasks) == 1
        assert result.tasks[0]["prompt"] == "Create missing config file"
        assert result.tasks[0]["source"] == "escalation"
        # Check escalation state was updated
        esc = state["escalation_state"]["g1"]
        assert esc["diagnoses"] == 1
        assert esc["diagnosis"] == "Tests are failing due to missing config"

    @pytest.mark.asyncio
    async def test_diagnose_with_sub_goal_changes(self):
        """Diagnosis can recommend sub-goal status changes."""
        goal = _make_goal()
        progress = _make_progress()
        state = _make_state(snapshots=5)

        diagnosis_response = {
            "root_cause": "Wrong approach",
            "blocked_by": [],
            "recommendation": "redecompose",
            "corrective_tasks": [],
            "sub_goal_changes": [
                {"sub_goal_id": "sg1", "new_status": "not-started",
                 "reason": "needs fresh approach"},
            ],
        }

        with patch(
            "secretary.goal_escalation._run_diagnosis",
            new_callable=AsyncMock,
            return_value=diagnosis_response,
        ):
            result = await evaluate_escalation(goal, progress, state)

        assert result is not None
        assert len(result.sub_goal_updates) == 1
        assert result.sub_goal_updates[0]["sub_goal_id"] == "sg1"
        assert result.sub_goal_updates[0]["new_status"] == "not-started"

    @pytest.mark.asyncio
    async def test_redecompose_clears_blocked_plans(self):
        """Redecompose clears blocked step plans and resets sub-goals."""
        goal = _make_goal()
        progress = _make_progress()
        state = _make_state(
            snapshots=10,
            blocked_sg="sg1",
            step_plans={
                "sg1": {
                    "goal_id": "g1",
                    "steps": [],
                    "blocked": True,
                    "block_reason": "exhausted",
                },
            },
        )
        # Set escalation state so we're at redecompose level
        state["escalation_state"] = {
            "g1": {
                "level": LEVEL_DIAGNOSE,
                "diagnoses": MAX_DIAGNOSES,
                "redecompositions": 0,
                "last_escalation_ts": None,
                "last_escalation_snapshot": -1,
                "diagnosis": "",
                "history": [],
            }
        }

        diagnosis_response = {
            "root_cause": "Approach was fundamentally wrong",
            "blocked_by": ["wrong assumptions"],
            "recommendation": "redecompose",
            "corrective_tasks": [
                {"prompt": "Try alternative approach", "tier": "medium"},
            ],
            "sub_goal_changes": [],
        }

        with patch(
            "secretary.goal_escalation._run_diagnosis",
            new_callable=AsyncMock,
            return_value=diagnosis_response,
        ):
            result = await evaluate_escalation(goal, progress, state)

        assert result is not None
        assert result.strategy == "redecompose"
        # Blocked plan should be removed
        assert "sg1" not in state.get("step_plans", {})
        # Sub-goal reset to not-started
        assert any(
            u["sub_goal_id"] == "sg1" and u["new_status"] == "not-started"
            for u in result.sub_goal_updates
        )
        # Corrective task included
        assert len(result.tasks) == 1
        esc = state["escalation_state"]["g1"]
        assert esc["redecompositions"] == 1

    @pytest.mark.asyncio
    async def test_reprioritize_generates_review_task(self):
        """Reprioritize generates a feasibility review task."""
        goal = _make_goal()
        progress = _make_progress()
        state = _make_state(snapshots=10)
        state["escalation_state"] = {
            "g1": {
                "level": LEVEL_REDECOMPOSE,
                "diagnoses": MAX_DIAGNOSES,
                "redecompositions": MAX_REDECOMPOSITIONS,
                "last_escalation_ts": None,
                "last_escalation_snapshot": -1,
                "diagnosis": "",
                "history": [],
            }
        }

        diagnosis_response = {
            "root_cause": "External dependency not met",
            "blocked_by": ["waiting on API access"],
            "recommendation": "reprioritize",
            "corrective_tasks": [],
            "sub_goal_changes": [],
        }

        with patch(
            "secretary.goal_escalation._run_diagnosis",
            new_callable=AsyncMock,
            return_value=diagnosis_response,
        ):
            result = await evaluate_escalation(goal, progress, state)

        assert result is not None
        assert result.strategy == "reprioritize"
        assert len(result.tasks) == 1
        assert "feasibility" in result.tasks[0]["prompt"].lower()
        esc = state["escalation_state"]["g1"]
        assert esc["level"] == LEVEL_REPRIORITIZE

    @pytest.mark.asyncio
    async def test_diagnosis_failure_returns_none(self):
        """When LLM diagnosis fails, return None (defer to next cycle)."""
        goal = _make_goal()
        progress = _make_progress()
        state = _make_state(snapshots=5)

        with patch(
            "secretary.goal_escalation._run_diagnosis",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await evaluate_escalation(goal, progress, state)

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_goal_id_returns_none(self):
        goal = {"id": "", "sub_goals": []}
        progress = _make_progress(goal_id="")
        state = _make_state()
        result = await evaluate_escalation(goal, progress, state)
        assert result is None


# ---------------------------------------------------------------------------
# evaluate_escalations — batch evaluation
# ---------------------------------------------------------------------------


class TestEvaluateEscalations:
    @pytest.mark.asyncio
    async def test_skips_non_stalled_goals(self):
        """Only stalled goals get evaluated."""
        goals = [_make_goal("g1"), _make_goal("g2")]
        progress_map = {
            "g1": _make_progress("g1", stalled=True),
            "g2": _make_progress("g2", stalled=False),
        }
        state = _make_state(snapshots=5)

        with patch(
            "secretary.goal_escalation.evaluate_escalation",
            new_callable=AsyncMock,
            return_value=EscalationAction(
                goal_id="g1", strategy="diagnose", summary="test",
                tasks=[], sub_goal_updates=[], note="test",
            ),
        ) as mock_eval:
            actions = await evaluate_escalations(goals, progress_map, state)

        assert len(actions) == 1
        assert actions[0].goal_id == "g1"
        # evaluate_escalation should only be called for g1
        mock_eval.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_stalled_goals(self):
        """All stalled goals get evaluated."""
        goals = [_make_goal("g1"), _make_goal("g2")]
        progress_map = {
            "g1": _make_progress("g1", stalled=True),
            "g2": _make_progress("g2", stalled=True),
        }
        state = _make_state(snapshots=5)

        with patch(
            "secretary.goal_escalation.evaluate_escalation",
            new_callable=AsyncMock,
            side_effect=[
                EscalationAction(
                    goal_id="g1", strategy="diagnose", summary="diag g1",
                    tasks=[], sub_goal_updates=[], note="test",
                ),
                EscalationAction(
                    goal_id="g2", strategy="diagnose", summary="diag g2",
                    tasks=[], sub_goal_updates=[], note="test",
                ),
            ],
        ):
            actions = await evaluate_escalations(goals, progress_map, state)

        assert len(actions) == 2

    @pytest.mark.asyncio
    async def test_no_stalled_goals_returns_empty(self):
        goals = [_make_goal("g1")]
        progress_map = {"g1": _make_progress("g1", stalled=False)}
        state = _make_state(snapshots=5)
        actions = await evaluate_escalations(goals, progress_map, state)
        assert actions == []

    @pytest.mark.asyncio
    async def test_cooldown_filtered_out(self):
        """When escalation returns None (cooldown), it's not in the result."""
        goals = [_make_goal("g1")]
        progress_map = {"g1": _make_progress("g1", stalled=True)}
        state = _make_state(snapshots=5)

        with patch(
            "secretary.goal_escalation.evaluate_escalation",
            new_callable=AsyncMock,
            return_value=None,
        ):
            actions = await evaluate_escalations(goals, progress_map, state)

        assert actions == []


# ---------------------------------------------------------------------------
# Escalation ladder progression (end-to-end budget tracking)
# ---------------------------------------------------------------------------


class TestEscalationLadderProgression:
    """Verify the full escalation ladder: diagnose → diagnose → redecompose → reprioritize → shelve."""

    @pytest.mark.asyncio
    async def test_full_ladder(self):
        """Walk through the entire escalation ladder."""
        goal = _make_goal()
        progress = _make_progress()

        diagnosis_response = {
            "root_cause": "persistent issue",
            "blocked_by": [],
            "recommendation": "diagnose_deeper",
            "corrective_tasks": [],
            "sub_goal_changes": [],
        }

        state = _make_state(snapshots=20)
        state["step_plans"] = {
            "sg1": {"goal_id": "g1", "steps": [], "blocked": True,
                    "block_reason": "test"},
        }
        state["sub_goal_status"] = {
            "sg1": {"status": "blocked", "evidence": "test",
                    "updated": "2026-01-01T00:00:00Z"},
        }

        with patch(
            "secretary.goal_escalation._run_diagnosis",
            new_callable=AsyncMock,
            return_value=diagnosis_response,
        ):
            # Step 1: First diagnose
            r1 = await evaluate_escalation(goal, progress, state)
            assert r1 is not None
            assert r1.strategy == "diagnose"
            esc = state["escalation_state"]["g1"]
            assert esc["diagnoses"] == 1

            # Advance snapshot counter past cooldown
            state["progress_snapshots"].extend([
                {"ts": "2026-02-01T00:00:00Z", "completions": {"g1": 0.3}}
                for _ in range(ESCALATION_COOLDOWN_SNAPSHOTS)
            ])

            # Step 2: Second diagnose
            r2 = await evaluate_escalation(goal, progress, state)
            assert r2 is not None
            assert r2.strategy == "diagnose"
            assert esc["diagnoses"] == 2

            # Advance past cooldown again
            state["progress_snapshots"].extend([
                {"ts": "2026-03-01T00:00:00Z", "completions": {"g1": 0.3}}
                for _ in range(ESCALATION_COOLDOWN_SNAPSHOTS)
            ])

            # Step 3: Redecompose (diagnoses exhausted)
            r3 = await evaluate_escalation(goal, progress, state)
            assert r3 is not None
            assert r3.strategy == "redecompose"
            assert esc["redecompositions"] == 1

            # Advance past cooldown
            state["progress_snapshots"].extend([
                {"ts": "2026-04-01T00:00:00Z", "completions": {"g1": 0.3}}
                for _ in range(ESCALATION_COOLDOWN_SNAPSHOTS)
            ])

            # Step 4: Reprioritize
            r4 = await evaluate_escalation(goal, progress, state)
            assert r4 is not None
            assert r4.strategy == "reprioritize"

            # Advance past cooldown
            state["progress_snapshots"].extend([
                {"ts": "2026-05-01T00:00:00Z", "completions": {"g1": 0.3}}
                for _ in range(ESCALATION_COOLDOWN_SNAPSHOTS)
            ])

            # Step 5: Shelve (no LLM call)
            r5 = await evaluate_escalation(goal, progress, state)
            assert r5 is not None
            assert r5.strategy == "shelve"
            assert esc["level"] == LEVEL_SHELVE

        # History should have 5 entries
        assert len(esc["history"]) == 5
        strategies = [h["strategy"] for h in esc["history"]]
        assert strategies == [
            "diagnose", "diagnose", "redecompose", "reprioritize", "shelve",
        ]


# ---------------------------------------------------------------------------
# EscalationAction dataclass
# ---------------------------------------------------------------------------


class TestEscalationAction:
    def test_fields(self):
        action = EscalationAction(
            goal_id="g1",
            strategy="diagnose",
            summary="Test diagnosis",
            tasks=[{"prompt": "test", "tier": "low"}],
            sub_goal_updates=[{"sub_goal_id": "sg1", "new_status": "blocked"}],
            note="[ESCALATION] test",
        )
        assert action.goal_id == "g1"
        assert action.strategy == "diagnose"
        assert len(action.tasks) == 1
        assert len(action.sub_goal_updates) == 1
