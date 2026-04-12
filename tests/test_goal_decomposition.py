"""Tests for goal_decomposition.py — Sub-Goal Decomposition Engine."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.goal_decomposition import (
    MAX_STEPS_PER_PLAN,
    _build_decomp_prompt,
    _parse_decomp_response,
    find_decomposable_sub_goals,
    format_step_plans_section,
    get_next_step,
    get_step_plans,
    record_step_result,
    save_step_plan,
    step_to_task,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_goal(
    goal_id: str = "g1",
    status: str = "in-progress",
    sub_goals: list[dict] | None = None,
) -> dict[str, Any]:
    return {
        "id": goal_id,
        "description": f"Goal {goal_id}",
        "success_criteria": f"Criteria for {goal_id}",
        "priority": 2,
        "status": status,
        "sub_goals": sub_goals or [],
    }


def _make_sub_goal(sg_id: str = "sg1", status: str = "not-started") -> dict[str, Any]:
    return {
        "id": sg_id,
        "description": f"Sub-goal {sg_id}",
        "status": status,
    }


def _make_step(
    step_id: str = "sg1.1",
    action: str = "Do something",
    verification: str = "Check it",
    tier: str = "medium",
    status: str = "pending",
    result: str | None = None,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "action": action,
        "verification": verification,
        "tier": tier,
        "status": status,
        "result": result,
        "ts": None,
    }


def _make_state(plans: dict | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "last_reviewed": None,
        "sub_goal_status": {},
        "progress_notes": [],
    }
    if plans is not None:
        state["step_plans"] = plans
    return state


# ---------------------------------------------------------------------------
# _parse_decomp_response
# ---------------------------------------------------------------------------


class TestParseDecompResponse:
    def test_valid_json(self):
        text = json.dumps({
            "steps": [
                {"action": "Step 1", "verification": "Check 1", "tier": "low"},
                {"action": "Step 2", "verification": "Check 2", "tier": "high"},
            ],
            "rationale": "Because reasons.",
        })
        result = _parse_decomp_response(text)
        assert len(result["steps"]) == 2
        assert result["steps"][0]["action"] == "Step 1"
        assert result["steps"][0]["tier"] == "low"
        assert result["steps"][1]["tier"] == "high"
        assert result["rationale"] == "Because reasons."

    def test_strips_markdown_fences(self):
        text = "```json\n" + json.dumps({
            "steps": [{"action": "A", "verification": "V", "tier": "medium"}],
            "rationale": "R",
        }) + "\n```"
        result = _parse_decomp_response(text)
        assert len(result["steps"]) == 1
        assert result["steps"][0]["action"] == "A"

    def test_invalid_json(self):
        result = _parse_decomp_response("not valid json")
        assert result["steps"] == []
        assert result["rationale"] == ""

    def test_empty_input(self):
        result = _parse_decomp_response("")
        assert result["steps"] == []

    def test_invalid_tier_defaults_medium(self):
        text = json.dumps({
            "steps": [{"action": "A", "verification": "V", "tier": "ultra"}],
        })
        result = _parse_decomp_response(text)
        assert result["steps"][0]["tier"] == "medium"

    def test_missing_action_skipped(self):
        text = json.dumps({
            "steps": [
                {"verification": "V", "tier": "low"},  # no action
                {"action": "B", "verification": "V2", "tier": "low"},
            ],
        })
        result = _parse_decomp_response(text)
        assert len(result["steps"]) == 1
        assert result["steps"][0]["action"] == "B"

    def test_caps_at_max_steps(self):
        steps = [{"action": f"S{i}", "verification": f"V{i}", "tier": "low"} for i in range(20)]
        text = json.dumps({"steps": steps})
        result = _parse_decomp_response(text)
        assert len(result["steps"]) == MAX_STEPS_PER_PLAN

    def test_non_dict_top_level(self):
        text = json.dumps([1, 2, 3])
        result = _parse_decomp_response(text)
        assert result["steps"] == []

    def test_non_list_steps(self):
        text = json.dumps({"steps": "not a list"})
        result = _parse_decomp_response(text)
        assert result["steps"] == []


# ---------------------------------------------------------------------------
# _build_decomp_prompt
# ---------------------------------------------------------------------------


class TestBuildDecompPrompt:
    def test_includes_sub_goal_info(self):
        sg = _make_sub_goal("learned-router")
        parent = _make_goal("prefix-survival", sub_goals=[sg])
        prompt = _build_decomp_prompt(sg, parent, "")
        assert "learned-router" in prompt
        assert "prefix-survival" in prompt

    def test_includes_sibling_context(self):
        sg1 = _make_sub_goal("done-sg", status="done")
        sg2 = _make_sub_goal("target-sg")
        parent = _make_goal("g1", sub_goals=[sg1, sg2])
        prompt = _build_decomp_prompt(sg2, parent, "")
        assert "DONE" in prompt
        assert "done-sg" in prompt

    def test_includes_additional_context(self):
        sg = _make_sub_goal()
        parent = _make_goal()
        prompt = _build_decomp_prompt(sg, parent, "Extra info here")
        assert "Extra info here" in prompt

    def test_no_context_section_when_empty(self):
        sg = _make_sub_goal()
        parent = _make_goal()
        prompt = _build_decomp_prompt(sg, parent, "")
        assert "Additional Context" not in prompt


# ---------------------------------------------------------------------------
# save_step_plan / get_step_plans
# ---------------------------------------------------------------------------


class TestStepPlanStorage:
    def test_save_and_retrieve(self):
        state = _make_state()
        steps = [_make_step("sg1.1"), _make_step("sg1.2")]
        save_step_plan(state, "sg1", "g1", steps)
        plans = get_step_plans(state)
        assert "sg1" in plans
        assert plans["sg1"]["goal_id"] == "g1"
        assert len(plans["sg1"]["steps"]) == 2
        assert not plans["sg1"]["completed"]
        assert "created" in plans["sg1"]

    def test_empty_state(self):
        state = _make_state()
        assert get_step_plans(state) == {}

    def test_multiple_plans(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [_make_step("sg1.1")])
        save_step_plan(state, "sg2", "g2", [_make_step("sg2.1")])
        plans = get_step_plans(state)
        assert len(plans) == 2


# ---------------------------------------------------------------------------
# get_next_step
# ---------------------------------------------------------------------------


class TestGetNextStep:
    def test_first_pending_step(self):
        state = _make_state()
        steps = [_make_step("sg1.1"), _make_step("sg1.2")]
        save_step_plan(state, "sg1", "g1", steps)
        nxt = get_next_step(state, "sg1")
        assert nxt is not None
        assert nxt["step_id"] == "sg1.1"

    def test_second_step_after_first_completed(self):
        state = _make_state()
        steps = [
            _make_step("sg1.1", status="completed"),
            _make_step("sg1.2"),
        ]
        save_step_plan(state, "sg1", "g1", steps)
        nxt = get_next_step(state, "sg1")
        assert nxt is not None
        assert nxt["step_id"] == "sg1.2"

    def test_blocked_by_failed_step(self):
        state = _make_state()
        steps = [
            _make_step("sg1.1", status="failed"),
            _make_step("sg1.2"),
        ]
        save_step_plan(state, "sg1", "g1", steps)
        nxt = get_next_step(state, "sg1")
        assert nxt is None  # Blocked by failed step

    def test_all_completed(self):
        state = _make_state()
        steps = [
            _make_step("sg1.1", status="completed"),
            _make_step("sg1.2", status="completed"),
        ]
        save_step_plan(state, "sg1", "g1", steps)
        nxt = get_next_step(state, "sg1")
        assert nxt is None

    def test_completed_plan(self):
        state = _make_state({
            "sg1": {
                "goal_id": "g1",
                "steps": [_make_step("sg1.1", status="completed")],
                "completed": True,
                "created": "2024-01-01",
            }
        })
        nxt = get_next_step(state, "sg1")
        assert nxt is None

    def test_nonexistent_plan(self):
        state = _make_state()
        nxt = get_next_step(state, "bogus")
        assert nxt is None

    def test_empty_steps(self):
        state = _make_state({
            "sg1": {
                "goal_id": "g1",
                "steps": [],
                "completed": False,
                "created": "2024-01-01",
            }
        })
        nxt = get_next_step(state, "sg1")
        assert nxt is None


# ---------------------------------------------------------------------------
# record_step_result
# ---------------------------------------------------------------------------


class TestRecordStepResult:
    def test_marks_step_completed(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [_make_step("sg1.1"), _make_step("sg1.2")])
        record_step_result(state, "sg1", "sg1.1", True, "It worked")
        step = state["step_plans"]["sg1"]["steps"][0]
        assert step["status"] == "completed"
        assert step["result"] == "It worked"
        assert step["ts"] is not None
        assert not state["step_plans"]["sg1"]["completed"]

    def test_marks_step_failed(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [_make_step("sg1.1")])
        record_step_result(state, "sg1", "sg1.1", False, "Error!")
        step = state["step_plans"]["sg1"]["steps"][0]
        assert step["status"] == "failed"
        assert step["result"] == "Error!"

    def test_completes_plan_when_all_done(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [_make_step("sg1.1"), _make_step("sg1.2")])
        record_step_result(state, "sg1", "sg1.1", True)
        assert not state["step_plans"]["sg1"]["completed"]
        record_step_result(state, "sg1", "sg1.2", True)
        assert state["step_plans"]["sg1"]["completed"]

    def test_not_completed_if_any_failed(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [_make_step("sg1.1"), _make_step("sg1.2")])
        record_step_result(state, "sg1", "sg1.1", True)
        record_step_result(state, "sg1", "sg1.2", False)
        assert not state["step_plans"]["sg1"]["completed"]

    def test_noop_for_nonexistent_plan(self):
        state = _make_state()
        record_step_result(state, "bogus", "bogus.1", True)  # Should not raise

    def test_truncates_long_evidence(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [_make_step("sg1.1")])
        record_step_result(state, "sg1", "sg1.1", True, "x" * 1000)
        step = state["step_plans"]["sg1"]["steps"][0]
        assert len(step["result"]) == 500


# ---------------------------------------------------------------------------
# step_to_task
# ---------------------------------------------------------------------------


class TestStepToTask:
    def test_basic_conversion(self):
        step = _make_step("sg1.1", action="Analyse data", verification="File exists")
        task = step_to_task(step, "sg1", "g1")
        assert task["prompt"].startswith("Analyse data")
        assert "File exists" in task["prompt"]
        assert task["tier"] == "medium"
        assert task["goal_id"] == "g1"
        assert task["source"] == "goals"
        assert task["id"] == "step-sg1.1"
        assert task["_step_id"] == "sg1.1"
        assert task["_sub_goal_id"] == "sg1"

    def test_no_verification(self):
        step = _make_step("sg1.1", verification="")
        task = step_to_task(step, "sg1", "g1")
        assert "Verification" not in task["prompt"]

    def test_preserves_tier(self):
        step = _make_step(tier="high")
        task = step_to_task(step, "sg1", "g1")
        assert task["tier"] == "high"


# ---------------------------------------------------------------------------
# find_decomposable_sub_goals
# ---------------------------------------------------------------------------


class TestFindDecomposableSubGoals:
    def test_finds_not_started(self):
        sg = _make_sub_goal("sg1", status="not-started")
        goal = _make_goal("g1", status="in-progress", sub_goals=[sg])
        result = find_decomposable_sub_goals([goal], {}, {})
        assert len(result) == 1
        assert result[0][0]["id"] == "sg1"

    def test_finds_in_progress(self):
        sg = _make_sub_goal("sg1", status="in-progress")
        goal = _make_goal("g1", sub_goals=[sg])
        result = find_decomposable_sub_goals([goal], {}, {})
        assert len(result) == 1

    def test_skips_done(self):
        sg = _make_sub_goal("sg1", status="done")
        goal = _make_goal("g1", sub_goals=[sg])
        result = find_decomposable_sub_goals([goal], {}, {})
        assert len(result) == 0

    def test_skips_blocked(self):
        sg = _make_sub_goal("sg1", status="blocked")
        goal = _make_goal("g1", sub_goals=[sg])
        result = find_decomposable_sub_goals([goal], {}, {})
        assert len(result) == 0

    def test_skips_done_parent_goal(self):
        sg = _make_sub_goal("sg1", status="not-started")
        goal = _make_goal("g1", status="done", sub_goals=[sg])
        result = find_decomposable_sub_goals([goal], {}, {})
        assert len(result) == 0

    def test_skips_already_decomposed(self):
        sg = _make_sub_goal("sg1", status="not-started")
        goal = _make_goal("g1", sub_goals=[sg])
        existing_plans = {"sg1": {"goal_id": "g1", "steps": [], "completed": False}}
        result = find_decomposable_sub_goals([goal], {}, existing_plans)
        assert len(result) == 0

    def test_respects_state_override(self):
        sg = _make_sub_goal("sg1", status="not-started")
        goal = _make_goal("g1", sub_goals=[sg])
        # Override says it's done
        overrides = {"sg1": {"status": "done", "evidence": "test"}}
        result = find_decomposable_sub_goals([goal], overrides, {})
        assert len(result) == 0

    def test_multiple_goals_and_sub_goals(self):
        g1 = _make_goal("g1", sub_goals=[
            _make_sub_goal("sg1", status="done"),
            _make_sub_goal("sg2", status="not-started"),
        ])
        g2 = _make_goal("g2", sub_goals=[
            _make_sub_goal("sg3", status="in-progress"),
        ])
        result = find_decomposable_sub_goals([g1, g2], {}, {})
        ids = [r[0]["id"] for r in result]
        assert "sg2" in ids
        assert "sg3" in ids
        assert len(ids) == 2


# ---------------------------------------------------------------------------
# format_step_plans_section
# ---------------------------------------------------------------------------


class TestFormatStepPlansSection:
    def test_empty_plans(self):
        state = _make_state()
        assert format_step_plans_section(state, []) == ""

    def test_no_step_plans_key(self):
        state = {"last_reviewed": None}
        assert format_step_plans_section(state, []) == ""

    def test_renders_active_plan(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [
            _make_step("sg1.1", status="completed"),
            _make_step("sg1.2", action="Implement router"),
            _make_step("sg1.3"),
        ])
        output = format_step_plans_section(state, [])
        assert "sg1" in output
        assert "1/3 steps done" in output
        assert "Implement router" in output
        assert "[done]" in output
        assert "[...]" in output

    def test_skips_completed_plans(self):
        state = _make_state({
            "sg1": {
                "goal_id": "g1",
                "steps": [_make_step("sg1.1", status="completed")],
                "completed": True,
                "created": "2024-01-01",
            }
        })
        output = format_step_plans_section(state, [])
        assert output == ""

    def test_shows_failed_status(self):
        state = _make_state()
        save_step_plan(state, "sg1", "g1", [
            _make_step("sg1.1", status="failed"),
            _make_step("sg1.2"),
        ])
        output = format_step_plans_section(state, [])
        assert "[FAILED]" in output
        # Current step should be "blocked" since prior step failed
        assert "blocked" in output


# ---------------------------------------------------------------------------
# Goal prompt with step plans
# ---------------------------------------------------------------------------


class TestGoalPromptWithStepPlans:
    def test_includes_step_plans_section(self):
        from secretary.goals import _build_goal_prompt

        prompt = _build_goal_prompt(
            goals=[_make_goal("g1", sub_goals=[_make_sub_goal("sg1")])],
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            step_plans_section="## Active Step Plans\nsg1: 1/3 done",
        )
        assert "Active Step Plans" in prompt
        assert "sg1: 1/3 done" in prompt

    def test_excludes_step_plans_when_empty(self):
        from secretary.goals import _build_goal_prompt

        prompt = _build_goal_prompt(
            goals=[_make_goal("g1")],
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            step_plans_section="",
        )
        assert "Active Step Plans" not in prompt

    def test_step_plans_before_reflections(self):
        from secretary.goals import _build_goal_prompt

        prompt = _build_goal_prompt(
            goals=[_make_goal("g1")],
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            reflections=[{"reflection": "Test reflection"}],
            step_plans_section="## Active Step Plans\ntest plan",
        )
        plans_pos = prompt.index("Active Step Plans")
        reflections_pos = prompt.index("Reflections from Previous")
        assert plans_pos < reflections_pos


# ---------------------------------------------------------------------------
# decompose_sub_goal (async, mocked API)
# ---------------------------------------------------------------------------


class TestDecomposeSubGoal:
    @pytest.mark.asyncio
    async def test_returns_steps_on_success(self):
        from secretary.goal_decomposition import decompose_sub_goal

        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.text = json.dumps({
            "steps": [
                {"action": "Analyse data", "verification": "Output exists", "tier": "low"},
                {"action": "Implement change", "verification": "Tests pass", "tier": "medium"},
            ],
            "rationale": "Start with analysis.",
        })
        mock_response.content = [mock_block]

        mock_config = MagicMock()
        mock_config.anthropic_base_url = "http://localhost:4141"

        with patch("secretary.goal_decomposition._call_decomp", return_value=mock_response):
            with patch("secretary.direct_agent._build_client"):
                steps = await decompose_sub_goal(
                    _make_sub_goal("learned-router"),
                    _make_goal("prefix-survival"),
                    mock_config,
                )

        assert len(steps) == 2
        assert steps[0]["step_id"] == "learned-router.1"
        assert steps[0]["action"] == "Analyse data"
        assert steps[0]["status"] == "pending"
        assert steps[1]["step_id"] == "learned-router.2"

    @pytest.mark.asyncio
    async def test_returns_empty_on_api_failure(self):
        from secretary.goal_decomposition import decompose_sub_goal

        mock_config = MagicMock()
        mock_config.anthropic_base_url = "http://localhost:4141"

        with patch("secretary.direct_agent._build_client"):
            with patch(
                "secretary.goal_decomposition._call_decomp",
                side_effect=Exception("API error"),
            ):
                steps = await decompose_sub_goal(
                    _make_sub_goal("sg1"),
                    _make_goal("g1"),
                    mock_config,
                )

        assert steps == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_parse_failure(self):
        from secretary.goal_decomposition import decompose_sub_goal

        mock_response = MagicMock()
        mock_block = MagicMock()
        mock_block.text = "not valid json"
        mock_response.content = [mock_block]

        mock_config = MagicMock()
        mock_config.anthropic_base_url = "http://localhost:4141"

        with patch("secretary.goal_decomposition._call_decomp", return_value=mock_response):
            with patch("secretary.direct_agent._build_client"):
                steps = await decompose_sub_goal(
                    _make_sub_goal("sg1"),
                    _make_goal("g1"),
                    mock_config,
                )

        assert steps == []


# ---------------------------------------------------------------------------
# System prompt update check
# ---------------------------------------------------------------------------


class TestSystemPromptUpdate:
    def test_system_prompt_mentions_step_plans(self):
        from secretary.goals import _GOAL_PLANNER_SYSTEM

        assert "Step Plan" in _GOAL_PLANNER_SYSTEM
        assert "step" in _GOAL_PLANNER_SYSTEM.lower()


# ---------------------------------------------------------------------------
# _has_mutation_step + enforcement tests
# ---------------------------------------------------------------------------

class TestHasMutationStep:
    def test_empty_returns_false(self):
        from secretary.goal_decomposition import _has_mutation_step
        assert _has_mutation_step([]) is False

    def test_read_only_steps_false(self):
        from secretary.goal_decomposition import _has_mutation_step
        steps = [
            {"action": "Read the config file", "verification": "file_read returns content"},
            {"action": "Investigate the logs", "verification": "grep returns matches"},
        ]
        assert _has_mutation_step(steps) is False

    def test_file_edit_step_true(self):
        from secretary.goal_decomposition import _has_mutation_step
        steps = [{"action": "Use file_edit to patch the bug", "verification": "ok"}]
        assert _has_mutation_step(steps) is True

    def test_file_write_step_true(self):
        from secretary.goal_decomposition import _has_mutation_step
        steps = [{"action": "file_write new module", "verification": "module exists"}]
        assert _has_mutation_step(steps) is True

    def test_run_command_step_true(self):
        from secretary.goal_decomposition import _has_mutation_step
        steps = [{"action": "run_command to apply changes", "verification": "exit 0"}]
        assert _has_mutation_step(steps) is True

    def test_implement_keyword_true(self):
        from secretary.goal_decomposition import _has_mutation_step
        steps = [{"action": "implement the feature", "verification": "tests pass"}]
        assert _has_mutation_step(steps) is True

    def test_keyword_in_verification_counts(self):
        from secretary.goal_decomposition import _has_mutation_step
        steps = [{"action": "Analyse the report", "verification": "run_command pytest passes"}]
        assert _has_mutation_step(steps) is True

    def test_case_insensitive(self):
        from secretary.goal_decomposition import _has_mutation_step
        steps = [{"action": "FILE_WRITE the output", "verification": "check it"}]
        assert _has_mutation_step(steps) is True


# ---------------------------------------------------------------------------
# TestPriorInvestigationSimilarity -- Rule 7 recursive-loop fix
# ---------------------------------------------------------------------------

class TestPriorInvestigationSimilarity:
    """Tests for the prior_investigation_similarity helper that guards Rule 7."""

    def test_identical_strings_return_1(self):
        from secretary.goal_decomposition import prior_investigation_similarity
        assert prior_investigation_similarity("fix Rule 7 loop", "fix Rule 7 loop") == 1.0

    def test_empty_current_returns_0(self):
        from secretary.goal_decomposition import prior_investigation_similarity
        assert prior_investigation_similarity("", "fix Rule 7 loop") == 0.0

    def test_empty_last_cycle_returns_0(self):
        from secretary.goal_decomposition import prior_investigation_similarity
        assert prior_investigation_similarity("fix Rule 7 loop", "") == 0.0

    def test_both_empty_returns_0(self):
        from secretary.goal_decomposition import prior_investigation_similarity
        assert prior_investigation_similarity("", "") == 0.0

    def test_completely_different_below_threshold(self):
        from secretary.goal_decomposition import prior_investigation_similarity
        score = prior_investigation_similarity("add calendar event", "fix billing.py")
        assert score < 0.90

    def test_very_similar_meets_threshold(self):
        from secretary.goal_decomposition import prior_investigation_similarity
        # Identical modulo minor whitespace normalisation
        score = prior_investigation_similarity(
            "fix Rule 7 recursive investigation trap",
            "fix  Rule 7  recursive investigation trap",  # double spaces
        )
        assert score >= 0.90

    def test_case_insensitive(self):
        from secretary.goal_decomposition import prior_investigation_similarity
        score = prior_investigation_similarity(
            "Fix RULE 7 Recursive Trap",
            "fix rule 7 recursive trap",
        )
        assert score == 1.0

    def test_threshold_constant_is_0_90(self):
        from secretary.goal_decomposition import _INVESTIGATION_SIMILARITY_THRESHOLD
        assert _INVESTIGATION_SIMILARITY_THRESHOLD == 0.90

    def test_high_similarity_triggers_skip(self):
        """Verify the threshold comparison used inside decompose_sub_goal."""
        from secretary.goal_decomposition import (
            prior_investigation_similarity,
            _INVESTIGATION_SIMILARITY_THRESHOLD,
        )
        current = "Implement similarity check to break re-diagnosis loop"
        last    = "Implement similarity check to break re-diagnosis loop"
        assert (
            prior_investigation_similarity(current, last)
            >= _INVESTIGATION_SIMILARITY_THRESHOLD
        )

    def test_partial_overlap_below_threshold(self):
        from secretary.goal_decomposition import (
            prior_investigation_similarity,
            _INVESTIGATION_SIMILARITY_THRESHOLD,
        )
        score = prior_investigation_similarity(
            "investigate billing module for errors",
            "fix calendar sync issue",
        )
        assert score < _INVESTIGATION_SIMILARITY_THRESHOLD


class TestDecomposeSubGoalSkipsInvestigation:
    """Integration tests: decompose_sub_goal skips Rule 7 when similarity >= 0.90."""

    def _make_sub_goal(self, desc: str = "Fix Rule 7 loop in decomposition.py") -> dict:
        return {
            "id": "sg-test-1",
            "title": "Fix Rule 7",
            "description": desc,
            "status": "not-started",
            "tier": "medium",
        }

    def _make_parent(self) -> dict:
        return {"id": "g-test", "title": "Self-Improve", "description": "Improve secretary"}

    def test_skip_flag_injected_into_context_when_similar(self):
        """When similarity >= threshold, context must contain the SYSTEM NOTE."""
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio
        from secretary.goal_decomposition import (
            prior_investigation_similarity,
            _INVESTIGATION_SIMILARITY_THRESHOLD,
        )

        desc = "Fix Rule 7 loop in decomposition.py"
        last = "Fix Rule 7 loop in decomposition.py"  # identical -> similarity = 1.0

        assert prior_investigation_similarity(desc, last) >= _INVESTIGATION_SIMILARITY_THRESHOLD

        captured_context = []

        def fake_build_prompt(sg, parent, ctx):
            captured_context.append(ctx)
            return "PROMPT"

        fake_client = MagicMock()
        fake_msg = MagicMock()
        fake_msg.content = [MagicMock(text='{"steps": [{"action": "file_edit billing.py", "verification": "tests pass", "tier": "medium"}]}')]
        fake_client.messages.create = AsyncMock(return_value=fake_msg)

        async def run():
            from secretary.goal_decomposition import decompose_sub_goal
            from secretary.config import SecretaryConfig
            cfg = SecretaryConfig()
            with (
                patch("secretary.goal_decomposition._build_decomp_prompt", side_effect=fake_build_prompt),
                patch("secretary.direct_agent._build_client", return_value=fake_client),
            ):
                return await decompose_sub_goal(
                    self._make_sub_goal(desc),
                    self._make_parent(),
                    cfg,
                    last_cycle_task=last,
                )

        asyncio.run(run())
        assert captured_context, "context was not captured"
        assert "[SYSTEM NOTE]" in captured_context[0], (
            f"Expected [SYSTEM NOTE] in context, got: {captured_context[0]!r}"
        )

    def test_no_skip_when_tasks_differ(self):
        """When tasks differ, context must NOT contain the SYSTEM NOTE."""
        from unittest.mock import AsyncMock, MagicMock, patch
        import asyncio

        desc = "Fix Rule 7 loop in decomposition.py"
        last = "Add calendar event for team meeting"  # unrelated -> low similarity

        captured_context = []

        def fake_build_prompt(sg, parent, ctx):
            captured_context.append(ctx)
            return "PROMPT"

        fake_client = MagicMock()
        fake_msg = MagicMock()
        fake_msg.content = [MagicMock(text='{"steps": [{"action": "investigate the file", "verification": "done", "tier": "low"}]}')]
        fake_client.messages.create = AsyncMock(return_value=fake_msg)

        async def run():
            from secretary.goal_decomposition import decompose_sub_goal
            from secretary.config import SecretaryConfig
            cfg = SecretaryConfig()
            with (
                patch("secretary.goal_decomposition._build_decomp_prompt", side_effect=fake_build_prompt),
                patch("secretary.direct_agent._build_client", return_value=fake_client),
            ):
                return await decompose_sub_goal(
                    self._make_sub_goal(desc),
                    self._make_parent(),
                    cfg,
                    last_cycle_task=last,
                )

        asyncio.run(run())
        assert captured_context, "context was not captured"
        assert "[SYSTEM NOTE]" not in captured_context[0], (
            f"[SYSTEM NOTE] should NOT be in context for dissimilar tasks"
        )

    def test_no_skip_when_last_cycle_empty(self):
        """Empty last_cycle_task must never trigger the skip."""
        from secretary.goal_decomposition import (
            prior_investigation_similarity,
            _INVESTIGATION_SIMILARITY_THRESHOLD,
        )
        score = prior_investigation_similarity("anything", "")
        assert score == 0.0
        assert score < _INVESTIGATION_SIMILARITY_THRESHOLD
