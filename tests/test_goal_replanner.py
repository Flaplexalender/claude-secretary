"""Tests for goal_replanner.py — Adaptive Replanning Engine."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.goal_replanner import (
    MAX_RECOMPOSITIONS_PER_PLAN,
    MAX_RETRIES_PER_STEP,
    _build_analysis_prompt,
    _build_recompose_prompt,
    _parse_json_response,
    _record_failure,
    apply_block,
    apply_recompose,
    apply_retry,
    choose_strategy,
    handle_step_failure,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_state(
    sub_goal_id: str = "sg1",
    steps: list[dict] | None = None,
    retry_counts: dict | None = None,
    recompositions: int = 0,
    blocked: bool = False,
) -> dict[str, Any]:
    """Build a minimal goal_state dict with a step plan."""
    if steps is None:
        steps = [
            {"step_id": "sg1.1", "action": "Analyse data", "verification": "report exists",
             "tier": "low", "status": "completed", "result": None, "ts": None},
            {"step_id": "sg1.2", "action": "Implement change", "verification": "tests pass",
             "tier": "medium", "status": "failed", "result": "Error: file not found", "ts": None},
            {"step_id": "sg1.3", "action": "Write tests", "verification": "coverage > 80%",
             "tier": "medium", "status": "pending", "result": None, "ts": None},
        ]
    plan = {
        "goal_id": "g1",
        "steps": steps,
        "created": "2026-01-01T00:00:00Z",
        "completed": False,
    }
    if retry_counts:
        plan["retry_counts"] = retry_counts
    if recompositions:
        plan["recompositions"] = recompositions
    if blocked:
        plan["blocked"] = True
        plan["block_reason"] = "test block"
    return {"step_plans": {sub_goal_id: plan}}


def _make_goal(goal_id: str = "g1") -> dict[str, Any]:
    return {
        "id": goal_id,
        "description": f"Goal {goal_id}",
        "success_criteria": f"Criteria for {goal_id}",
        "sub_goals": [{"id": "sg1", "description": "Sub-goal sg1"}],
    }


# ---------------------------------------------------------------------------
# choose_strategy tests
# ---------------------------------------------------------------------------


class TestChooseStrategy:
    """Budget-based strategy selection."""

    def test_first_failure_allows_retry(self):
        state = _make_state()
        assert choose_strategy(state, "sg1", "sg1.2") == "retry"

    def test_after_max_retries_goes_to_recompose(self):
        state = _make_state(retry_counts={"sg1.2": MAX_RETRIES_PER_STEP})
        assert choose_strategy(state, "sg1", "sg1.2") == "recompose"

    def test_after_max_retries_and_recompositions_blocks(self):
        state = _make_state(
            retry_counts={"sg1.2": MAX_RETRIES_PER_STEP},
            recompositions=MAX_RECOMPOSITIONS_PER_PLAN,
        )
        assert choose_strategy(state, "sg1", "sg1.2") == "block"

    def test_nonexistent_plan_defaults_retry(self):
        state = {"step_plans": {}}
        # No plan → retry count is 0, so budget allows retry
        assert choose_strategy(state, "sg1", "sg1.2") == "retry"

    def test_partial_retries_still_allows_retry(self):
        state = _make_state(retry_counts={"sg1.2": 1})
        assert choose_strategy(state, "sg1", "sg1.2") == "retry"


# ---------------------------------------------------------------------------
# apply_retry tests
# ---------------------------------------------------------------------------


class TestApplyRetry:
    """Step reset and optional revision."""

    def test_basic_retry_resets_status(self):
        state = _make_state()
        apply_retry(state, "sg1", "sg1.2")
        step = state["step_plans"]["sg1"]["steps"][1]
        assert step["status"] == "pending"
        assert step["action"] == "Implement change"  # unchanged

    def test_retry_increments_count(self):
        state = _make_state()
        apply_retry(state, "sg1", "sg1.2")
        assert state["step_plans"]["sg1"]["retry_counts"]["sg1.2"] == 1
        apply_retry(state, "sg1", "sg1.2")
        assert state["step_plans"]["sg1"]["retry_counts"]["sg1.2"] == 2

    def test_revise_changes_action(self):
        state = _make_state()
        apply_retry(state, "sg1", "sg1.2", revised_action="Use absolute path")
        step = state["step_plans"]["sg1"]["steps"][1]
        assert step["action"] == "Use absolute path"
        assert step["status"] == "pending"

    def test_revise_changes_verification(self):
        state = _make_state()
        apply_retry(state, "sg1", "sg1.2", revised_verification="file exists at /abs/path")
        step = state["step_plans"]["sg1"]["steps"][1]
        assert step["verification"] == "file exists at /abs/path"

    def test_revise_changes_tier(self):
        state = _make_state()
        apply_retry(state, "sg1", "sg1.2", revised_tier="high")
        step = state["step_plans"]["sg1"]["steps"][1]
        assert step["tier"] == "high"

    def test_retry_clears_old_result(self):
        state = _make_state()
        state["step_plans"]["sg1"]["steps"][1]["result"] = "old error"
        state["step_plans"]["sg1"]["steps"][1]["ts"] = "2026-01-01"
        apply_retry(state, "sg1", "sg1.2")
        step = state["step_plans"]["sg1"]["steps"][1]
        assert "result" not in step
        assert "ts" not in step

    def test_retry_nonexistent_plan_is_noop(self):
        state = {"step_plans": {}}
        apply_retry(state, "sg1", "sg1.2")  # Should not raise


# ---------------------------------------------------------------------------
# apply_recompose tests
# ---------------------------------------------------------------------------


class TestApplyRecompose:
    """Plan recomposition — replacing remaining steps."""

    def test_preserves_completed_steps(self):
        state = _make_state()
        new_steps = [
            {"action": "New approach", "verification": "works", "tier": "medium"},
            {"action": "Finish up", "verification": "done", "tier": "low"},
        ]
        apply_recompose(state, "sg1", "sg1.2", new_steps)
        plan = state["step_plans"]["sg1"]
        # First step was completed, should be preserved
        assert plan["steps"][0]["step_id"] == "sg1.1"
        assert plan["steps"][0]["status"] == "completed"
        # New steps start after preserved
        assert plan["steps"][1]["step_id"] == "sg1.2"
        assert plan["steps"][1]["action"] == "New approach"
        assert plan["steps"][2]["step_id"] == "sg1.3"
        assert plan["steps"][2]["action"] == "Finish up"

    def test_increments_recomposition_counter(self):
        state = _make_state()
        new_steps = [{"action": "Try again", "verification": "ok", "tier": "low"}]
        apply_recompose(state, "sg1", "sg1.2", new_steps)
        assert state["step_plans"]["sg1"]["recompositions"] == 1

    def test_new_steps_get_correct_ids(self):
        state = _make_state()
        new_steps = [
            {"action": "A", "verification": "v1", "tier": "low"},
            {"action": "B", "verification": "v2", "tier": "medium"},
        ]
        apply_recompose(state, "sg1", "sg1.2", new_steps)
        plan = state["step_plans"]["sg1"]
        # 1 preserved (sg1.1) + 2 new
        assert len(plan["steps"]) == 3
        # New steps numbered from preserved count
        assert plan["steps"][1]["step_id"] == "sg1.2"
        assert plan["steps"][2]["step_id"] == "sg1.3"

    def test_resets_completed_flag(self):
        state = _make_state()
        state["step_plans"]["sg1"]["completed"] = True
        new_steps = [{"action": "Fix", "verification": "done", "tier": "low"}]
        apply_recompose(state, "sg1", "sg1.2", new_steps)
        assert state["step_plans"]["sg1"]["completed"] is False

    def test_nonexistent_plan_is_noop(self):
        state = {"step_plans": {}}
        apply_recompose(state, "sg1", "sg1.2", [])  # Should not raise


# ---------------------------------------------------------------------------
# apply_block tests
# ---------------------------------------------------------------------------


class TestApplyBlock:
    """Mark plan as blocked."""

    def test_sets_blocked_flag(self):
        state = _make_state()
        apply_block(state, "sg1", "All retries exhausted")
        assert state["step_plans"]["sg1"]["blocked"] is True

    def test_stores_reason(self):
        state = _make_state()
        apply_block(state, "sg1", "Test reason")
        assert state["step_plans"]["sg1"]["block_reason"] == "Test reason"

    def test_truncates_long_reason(self):
        state = _make_state()
        long_reason = "x" * 1000
        apply_block(state, "sg1", long_reason)
        assert len(state["step_plans"]["sg1"]["block_reason"]) == 500


# ---------------------------------------------------------------------------
# _record_failure tests
# ---------------------------------------------------------------------------


class TestRecordFailure:
    """Failure log management."""

    def test_appends_entry(self):
        state = _make_state()
        _record_failure(state, "sg1", "sg1.2", "retry", "transient error")
        log = state["step_plans"]["sg1"]["failure_log"]
        assert len(log) == 1
        assert log[0]["step_id"] == "sg1.2"
        assert log[0]["strategy"] == "retry"
        assert "ts" in log[0]

    def test_caps_at_10_entries(self):
        state = _make_state()
        for i in range(15):
            _record_failure(state, "sg1", "sg1.2", "retry", f"error {i}")
        log = state["step_plans"]["sg1"]["failure_log"]
        assert len(log) == 10
        # Last entry should be the most recent
        assert log[-1]["analysis"].startswith("error 14")


# ---------------------------------------------------------------------------
# _parse_json_response tests
# ---------------------------------------------------------------------------


class TestParseJson:
    """JSON extraction from LLM output."""

    def test_plain_json(self):
        text = '{"root_cause": "timeout", "strategy": "retry"}'
        result = _parse_json_response(text)
        assert result["strategy"] == "retry"

    def test_markdown_fenced(self):
        text = '```json\n{"strategy": "revise", "root_cause": "bug", "revised_action": "fix the bug"}\n```'
        result = _parse_json_response(text)
        assert result["strategy"] == "revise"

    def test_revise_without_revised_action_falls_back(self):
        text = '{"strategy": "revise", "root_cause": "bug"}'
        result = _parse_json_response(text)
        assert result["strategy"] == "retry"

    def test_extra_text_around_json(self):
        text = 'Here is my analysis:\n{"strategy": "recompose"}\nDone.'
        result = _parse_json_response(text)
        assert result["strategy"] == "recompose"

    def test_invalid_json_returns_none(self):
        assert _parse_json_response("not json at all") is None

    def test_empty_string(self):
        assert _parse_json_response("") is None


# ---------------------------------------------------------------------------
# _build_analysis_prompt tests
# ---------------------------------------------------------------------------


class TestBuildAnalysisPrompt:
    """Analysis prompt construction."""

    def test_includes_step_info(self):
        step = {"step_id": "sg1.2", "action": "Do thing", "verification": "check", "tier": "medium"}
        plan = {"steps": [step]}
        prompt = _build_analysis_prompt(step, "Error occurred", plan, 0)
        assert "sg1.2" in prompt
        assert "Do thing" in prompt
        assert "Error occurred" in prompt

    def test_includes_retry_count(self):
        step = {"step_id": "sg1.2", "action": "X", "verification": "Y", "tier": "low"}
        prompt = _build_analysis_prompt(step, "", {"steps": []}, 2)
        assert "2" in prompt

    def test_shows_completed_steps(self):
        completed = {"step_id": "sg1.1", "action": "First", "status": "completed", "verification": "", "tier": "low"}
        failed = {"step_id": "sg1.2", "action": "Second", "status": "failed", "verification": "", "tier": "medium"}
        plan = {"steps": [completed, failed]}
        prompt = _build_analysis_prompt(failed, "err", plan, 0)
        assert "Previously Completed Steps" in prompt
        assert "First" in prompt

    def test_truncates_long_evidence(self):
        step = {"step_id": "s1", "action": "X", "verification": "Y", "tier": "low"}
        long_evidence = "x" * 3000
        prompt = _build_analysis_prompt(step, long_evidence, {"steps": []}, 0)
        # Evidence should be truncated to 1500
        assert len(prompt) < 3000


# ---------------------------------------------------------------------------
# _build_recompose_prompt tests
# ---------------------------------------------------------------------------


class TestBuildRecomposePrompt:
    """Recomposition prompt construction."""

    def test_includes_sub_goal(self):
        prompt = _build_recompose_prompt(
            {"id": "sg1", "description": "Do stuff"},
            {"description": "Parent goal"},
            [],
            {"action": "Failed action"},
            "it broke",
            [],
        )
        assert "sg1" in prompt
        assert "Do stuff" in prompt

    def test_includes_completed_steps(self):
        completed = [{"step_id": "sg1.1", "action": "Did this"}]
        prompt = _build_recompose_prompt(
            {"id": "sg1", "description": "X"},
            {"description": "Y"},
            completed,
            {"action": "Failed"},
            "reason",
            [],
        )
        assert "Did this" in prompt
        assert "DONE" in prompt

    def test_includes_failure_analysis(self):
        prompt = _build_recompose_prompt(
            {"id": "sg1", "description": "X"},
            {"description": "Y"},
            [],
            {"action": "Failed"},
            "file not found because config path was wrong",
            [],
        )
        assert "file not found" in prompt


# ---------------------------------------------------------------------------
# handle_step_failure integration tests (mocked LLM)
# ---------------------------------------------------------------------------


def _mock_analysis_response(strategy: str = "retry", root_cause: str = "test error") -> dict:
    return {
        "root_cause": root_cause,
        "is_transient": strategy == "retry",
        "strategy": strategy,
        "revised_action": "Use correct path" if strategy == "revise" else None,
        "revised_verification": "file at /correct/path" if strategy == "revise" else None,
        "revised_tier": "medium" if strategy == "revise" else None,
        "rationale": "test rationale",
    }


def _mock_recompose_response() -> dict:
    return {
        "steps": [
            {"action": "New step 1", "verification": "v1", "tier": "low"},
            {"action": "New step 2", "verification": "v2", "tier": "medium"},
        ],
        "rationale": "addressing failure",
    }


class TestHandleStepFailure:
    """End-to-end replanning with mocked LLM calls."""

    @pytest.mark.asyncio
    async def test_retry_on_first_failure(self):
        state = _make_state()
        with patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse:
            mock_analyse.return_value = _mock_analysis_response("retry")
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error output")
        assert strategy == "retry"
        assert state["step_plans"]["sg1"]["steps"][1]["status"] == "pending"
        assert state["step_plans"]["sg1"]["retry_counts"]["sg1.2"] == 1

    @pytest.mark.asyncio
    async def test_revise_changes_step_action(self):
        state = _make_state()
        with patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse:
            mock_analyse.return_value = _mock_analysis_response("revise")
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "revise"
        step = state["step_plans"]["sg1"]["steps"][1]
        assert step["action"] == "Use correct path"
        assert step["status"] == "pending"

    @pytest.mark.asyncio
    async def test_recompose_after_max_retries(self):
        state = _make_state(retry_counts={"sg1.2": MAX_RETRIES_PER_STEP})
        with (
            patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse,
            patch("secretary.goal_replanner._recompose_plan", new_callable=AsyncMock) as mock_recompose,
        ):
            mock_analyse.return_value = _mock_analysis_response("recompose")
            mock_recompose.return_value = [
                {"action": "New A", "verification": "v1", "tier": "low", "status": "pending", "result": None, "ts": None},
                {"action": "New B", "verification": "v2", "tier": "medium", "status": "pending", "result": None, "ts": None},
            ]
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "persistent error")
        assert strategy == "recompose"
        plan = state["step_plans"]["sg1"]
        assert plan["recompositions"] == 1
        # First step preserved, then new steps
        assert plan["steps"][0]["step_id"] == "sg1.1"
        assert plan["steps"][1]["action"] == "New A"

    @pytest.mark.asyncio
    async def test_block_after_all_budgets_exhausted(self):
        state = _make_state(
            retry_counts={"sg1.2": MAX_RETRIES_PER_STEP},
            recompositions=MAX_RECOMPOSITIONS_PER_PLAN,
        )
        strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "block"
        assert state["step_plans"]["sg1"]["blocked"] is True

    @pytest.mark.asyncio
    async def test_block_on_already_blocked_plan(self):
        state = _make_state(blocked=True)
        strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "block"

    @pytest.mark.asyncio
    async def test_block_on_missing_plan(self):
        state = {"step_plans": {}}
        strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "block"

    @pytest.mark.asyncio
    async def test_block_on_missing_step(self):
        state = _make_state()
        strategy = await handle_step_failure(state, "sg1", "nonexistent", "error")
        assert strategy == "block"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_retry(self):
        state = _make_state()
        with patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse:
            mock_analyse.side_effect = Exception("API timeout")
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "retry"
        assert state["step_plans"]["sg1"]["retry_counts"]["sg1.2"] == 1

    @pytest.mark.asyncio
    async def test_llm_failure_blocks_when_retries_exhausted(self):
        state = _make_state(retry_counts={"sg1.2": MAX_RETRIES_PER_STEP})
        with patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse:
            mock_analyse.side_effect = Exception("API timeout")
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "block"

    @pytest.mark.asyncio
    async def test_parse_failure_falls_back_to_retry(self):
        state = _make_state()
        with patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse:
            mock_analyse.return_value = None  # Parse failed
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "retry"

    @pytest.mark.asyncio
    async def test_recompose_failure_blocks(self):
        state = _make_state(retry_counts={"sg1.2": MAX_RETRIES_PER_STEP})
        with (
            patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse,
            patch("secretary.goal_replanner._recompose_plan", new_callable=AsyncMock) as mock_recompose,
        ):
            mock_analyse.return_value = _mock_analysis_response("recompose")
            mock_recompose.side_effect = Exception("Recompose API error")
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "block"

    @pytest.mark.asyncio
    async def test_recompose_returns_none_blocks(self):
        state = _make_state(retry_counts={"sg1.2": MAX_RETRIES_PER_STEP})
        with (
            patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse,
            patch("secretary.goal_replanner._recompose_plan", new_callable=AsyncMock) as mock_recompose,
        ):
            mock_analyse.return_value = _mock_analysis_response("recompose")
            mock_recompose.return_value = None
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "block"

    @pytest.mark.asyncio
    async def test_failure_log_populated(self):
        state = _make_state()
        with patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse:
            mock_analyse.return_value = _mock_analysis_response("retry", "file not found")
            await handle_step_failure(state, "sg1", "sg1.2", "error")
        log = state["step_plans"]["sg1"]["failure_log"]
        assert len(log) == 1
        assert log[0]["analysis"] == "file not found"

    @pytest.mark.asyncio
    async def test_llm_recommends_revise_but_strategy_allows_retry(self):
        """LLM says revise, retries under budget → apply revise (within retry budget)."""
        state = _make_state(retry_counts={"sg1.2": 1})  # 1 retry used, 1 left
        with patch("secretary.goal_replanner._analyse_failure", new_callable=AsyncMock) as mock_analyse:
            mock_analyse.return_value = _mock_analysis_response("revise")
            strategy = await handle_step_failure(state, "sg1", "sg1.2", "error")
        assert strategy == "revise"


# ---------------------------------------------------------------------------
# get_next_step blocked plan test (integration with decomposition)
# ---------------------------------------------------------------------------


class TestGetNextStepBlocked:
    """Verify blocked plans are skipped by get_next_step."""

    def test_blocked_plan_returns_none(self):
        from secretary.goal_decomposition import get_next_step
        state = _make_state(blocked=True)
        assert get_next_step(state, "sg1") is None


# ---------------------------------------------------------------------------
# format_step_plans_section blocked rendering
# ---------------------------------------------------------------------------


class TestFormatStepPlansBlocked:
    """Verify blocked plans rendered properly."""

    def test_blocked_plan_shows_blocked_label(self):
        from secretary.goal_decomposition import format_step_plans_section
        state = _make_state(blocked=True)
        state["step_plans"]["sg1"]["block_reason"] = "All retries exhausted"
        text = format_step_plans_section(state, [])
        assert "BLOCKED" in text
        assert "All retries exhausted" in text
