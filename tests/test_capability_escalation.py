"""Tests for capability-failure detection and automatic tier escalation.

Covers:
- _record_failure with capability_failure flag
- _maybe_escalate_tier auto-escalation logic
- get_tier_override retrieval
- apply_guardrails with tier_overrides
- step_to_task with tier_override
"""
from __future__ import annotations

import pytest

from secretary.goal_replanner import (
    _maybe_escalate_tier,
    _record_failure,
    get_tier_override,
)
from secretary.goal_guardrails import apply_guardrails, validate_goal_task
from secretary.goal_decomposition import step_to_task


# ── Helpers ─────────────────────────────────────────────────────


def _make_state(sub_goal_id: str = "test-sg") -> dict:
    return {
        "step_plans": {
            sub_goal_id: {
                "steps": [
                    {"step_id": f"{sub_goal_id}.1", "action": "do thing", "tier": "low", "status": "failed"},
                ],
                "failure_log": [],
            }
        },
    }


# ── _record_failure with capability flag ────────────────────────


def test_record_failure_no_capability_flag() -> None:
    state = _make_state()
    _record_failure(state, "test-sg", "test-sg.1", "retry", "transient error")
    fl = state["step_plans"]["test-sg"]["failure_log"]
    assert len(fl) == 1
    assert "capability_failure" not in fl[0]


def test_record_failure_with_capability_flag() -> None:
    state = _make_state()
    _record_failure(
        state, "test-sg", "test-sg.1", "revise", "used Linux commands",
        is_capability_failure=True, step_tier="low",
    )
    fl = state["step_plans"]["test-sg"]["failure_log"]
    assert len(fl) == 1
    assert fl[0]["capability_failure"] is True
    assert fl[0]["step_tier"] == "low"


# ── _maybe_escalate_tier ───────────────────────────────────────


def test_no_escalation_on_single_failure() -> None:
    state = _make_state()
    _record_failure(
        state, "test-sg", "test-sg.1", "revise", "wrong OS",
        is_capability_failure=True, step_tier="low",
    )
    # Only 1 capability failure — should NOT escalate
    assert get_tier_override(state, "test-sg") is None


def test_escalation_on_two_capability_failures() -> None:
    state = _make_state()
    _record_failure(
        state, "test-sg", "test-sg.1", "retry", "bad commands",
        is_capability_failure=True, step_tier="low",
    )
    _record_failure(
        state, "test-sg", "test-sg.1", "revise", "hallucinated dirs",
        is_capability_failure=True, step_tier="low",
    )
    # 2 capability failures at "low" → override to "medium"
    assert get_tier_override(state, "test-sg") == "medium"


def test_escalation_medium_to_high() -> None:
    state = _make_state()
    _record_failure(
        state, "test-sg", "test-sg.1", "retry", "err1",
        is_capability_failure=True, step_tier="medium",
    )
    _record_failure(
        state, "test-sg", "test-sg.1", "revise", "err2",
        is_capability_failure=True, step_tier="medium",
    )
    assert get_tier_override(state, "test-sg") == "high"


def test_no_escalation_beyond_high() -> None:
    state = _make_state()
    _record_failure(
        state, "test-sg", "test-sg.1", "retry", "err1",
        is_capability_failure=True, step_tier="high",
    )
    _record_failure(
        state, "test-sg", "test-sg.1", "revise", "err2",
        is_capability_failure=True, step_tier="high",
    )
    # Already at max tier — no override
    assert get_tier_override(state, "test-sg") is None


def test_non_capability_failures_dont_escalate() -> None:
    state = _make_state()
    for _ in range(5):
        _record_failure(state, "test-sg", "test-sg.1", "retry", "network timeout")
    assert get_tier_override(state, "test-sg") is None


# ── apply_guardrails with tier_overrides ────────────────────────


def test_guardrails_tier_override_allows_higher_tier() -> None:
    tasks = [
        {
            "prompt": "Analyse the codebase structure and generate report",
            "tier": "medium",
            "source": "goals",
            "_sub_goal_id": "analysis-sg",
        }
    ]
    result = apply_guardrails(
        tasks,
        max_tier="low",
        tier_overrides={"analysis-sg": "medium"},
    )
    assert len(result.accepted) == 1
    assert result.accepted[0]["tier"] == "medium"


def test_guardrails_no_override_caps_tier() -> None:
    tasks = [
        {
            "prompt": "Analyse the codebase structure and generate report",
            "tier": "medium",
            "source": "goals",
            "_sub_goal_id": "analysis-sg",
        }
    ]
    result = apply_guardrails(tasks, max_tier="low")
    # Should downgrade to "low"
    assert len(result.accepted) == 1
    assert result.accepted[0]["tier"] == "low"


def test_guardrails_override_doesnt_affect_other_subgoals() -> None:
    tasks = [
        {
            "prompt": "Task for sub-goal A with sufficient length",
            "tier": "medium",
            "source": "goals",
            "_sub_goal_id": "sg-a",
        },
        {
            "prompt": "Task for sub-goal B with sufficient length",
            "tier": "medium",
            "source": "goals",
            "_sub_goal_id": "sg-b",
        },
    ]
    result = apply_guardrails(
        tasks,
        max_tier="low",
        tier_overrides={"sg-a": "medium"},
    )
    assert len(result.accepted) == 2
    # sg-a keeps medium, sg-b gets capped to low
    a_task = next(t for t in result.accepted if t["_sub_goal_id"] == "sg-a")
    b_task = next(t for t in result.accepted if t["_sub_goal_id"] == "sg-b")
    assert a_task["tier"] == "medium"
    assert b_task["tier"] == "low"


# ── step_to_task with tier_override ─────────────────────────────


def test_step_to_task_applies_tier_override() -> None:
    step = {"step_id": "sg.1", "action": "do something", "verification": "check it", "tier": "low"}
    task = step_to_task(step, "sg", "goal1", tier_override="medium")
    assert task["tier"] == "medium"


def test_step_to_task_keeps_higher_step_tier() -> None:
    step = {"step_id": "sg.1", "action": "do something", "verification": "check it", "tier": "high"}
    task = step_to_task(step, "sg", "goal1", tier_override="medium")
    assert task["tier"] == "high"  # step tier already higher


def test_step_to_task_no_override() -> None:
    step = {"step_id": "sg.1", "action": "do something", "verification": "check it", "tier": "low"}
    task = step_to_task(step, "sg", "goal1")
    assert task["tier"] == "low"
