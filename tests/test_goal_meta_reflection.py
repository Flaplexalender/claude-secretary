"""Tests for goal_meta_reflection — cross-goal pattern synthesis (Layer 24)."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from secretary.goal_meta_reflection import (
    MAX_META_REFLECTIONS,
    _build_meta_prompt,
    _parse_meta_response,
    format_meta_for_prompt,
    format_meta_reflection_section,
    run_meta_reflection,
)
from secretary.goals import GoalStore
from secretary.run_log import RunLogEntry


# ── Helpers ───────────────────────────────────────────────────


SAMPLE_GOALS = [
    {"id": "goal-1", "description": "Build email integration", "status": "in-progress"},
    {"id": "goal-2", "description": "Improve test coverage", "status": "in-progress"},
]


def _make_entry(
    task: str = "goal task",
    success: bool = True,
    source: str = "goals",
    goal_id: str = "goal-1",
) -> RunLogEntry:
    return RunLogEntry(
        timestamp="2026-03-18T10:00:00+00:00",
        cycle=1,
        task=task,
        tier="medium",
        model="claude-haiku-4.5",
        success=success,
        output_preview="done",
        error=None if success else "failed",
        source=source,
        goal_id=goal_id,
    )


def _make_goal_store(
    goals: list[dict] | None = None,
    state: dict | None = None,
) -> GoalStore:
    gs = MagicMock(spec=GoalStore)
    gs.goals = goals or list(SAMPLE_GOALS)
    gs._state = state or {}
    gs.save_state = MagicMock()
    return gs


def _make_run_log(entries: list[RunLogEntry] | None = None):
    rl = MagicMock()
    rl.recent.return_value = entries or []
    return rl


# ── _build_meta_prompt ────────────────────────────────────────


class TestBuildMetaPrompt:
    def test_returns_none_with_no_data(self) -> None:
        gs = _make_goal_store()
        rl = _make_run_log()
        assert _build_meta_prompt(gs, rl) is None

    def test_includes_reflections(self) -> None:
        gs = _make_goal_store(state={
            "reflections": [
                {"reflection": "Email auth keeps failing", "patterns": {"failing": ["auth timeout"], "working": []}},
            ],
        })
        rl = _make_run_log()
        prompt = _build_meta_prompt(gs, rl)
        assert prompt is not None
        assert "Email auth keeps failing" in prompt
        assert "auth timeout" in prompt

    def test_includes_trust_trends(self) -> None:
        gs = _make_goal_store(state={
            "reflections": [{"reflection": "Some progress", "patterns": {}}],
            "trust_snapshots": [
                {"ts": "t1", "scores": {"goal-1": 0.5, "goal-2": 0.8}},
                {"ts": "t2", "scores": {"goal-1": 0.3, "goal-2": 0.85}},
            ],
        })
        rl = _make_run_log()
        prompt = _build_meta_prompt(gs, rl)
        assert prompt is not None
        assert "goal-1" in prompt
        assert "\u2193" in prompt  # goal-1 dropped

    def test_includes_execution_reports(self) -> None:
        gs = _make_goal_store(state={
            "execution_reports": [
                {"cycle": 1, "tasks_generated": 3, "tasks_executed": 2,
                 "verification_pass": 1, "verification_fail": 1},
            ],
        })
        rl = _make_run_log()
        prompt = _build_meta_prompt(gs, rl)
        assert prompt is not None
        assert "Cycle 1" in prompt

    def test_includes_verification_failures(self) -> None:
        gs = _make_goal_store(state={
            "reflections": [{"reflection": "test", "patterns": {}}],
            "verification_log": [
                {"verdict": "pass", "goal_id": "goal-1", "reasoning": "ok"},
                {"verdict": "fail", "goal_id": "goal-2", "reasoning": "Auth token expired"},
            ],
        })
        rl = _make_run_log()
        prompt = _build_meta_prompt(gs, rl)
        assert prompt is not None
        assert "Auth token expired" in prompt

    def test_includes_graduation_events(self) -> None:
        gs = _make_goal_store(state={
            "reflections": [{"reflection": "test", "patterns": {}}],
            "graduation_history": [
                {"action": "upgrade", "old_level": "untrusted", "new_level": "cautious", "cycle": 10},
            ],
        })
        rl = _make_run_log()
        prompt = _build_meta_prompt(gs, rl)
        assert prompt is not None
        assert "upgrade" in prompt
        assert "cautious" in prompt

    def test_includes_goal_task_outcomes(self) -> None:
        entries = [
            _make_entry(task="Send email report", success=True, goal_id="goal-1"),
            _make_entry(task="Run test suite", success=False, goal_id="goal-2"),
        ]
        gs = _make_goal_store()
        rl = _make_run_log(entries)
        prompt = _build_meta_prompt(gs, rl)
        assert prompt is not None
        assert "Send email report" in prompt
        assert "[goal-2]" in prompt

    def test_includes_active_goals(self) -> None:
        gs = _make_goal_store(state={
            "reflections": [{"reflection": "test", "patterns": {}}],
        })
        rl = _make_run_log()
        prompt = _build_meta_prompt(gs, rl)
        assert prompt is not None
        assert "Build email integration" in prompt


# ── _parse_meta_response ──────────────────────────────────────


class TestParseMetaResponse:
    def test_valid_json(self) -> None:
        text = json.dumps({
            "questions": ["Q1", "Q2", "Q3"],
            "answers": ["A1", "A2", "A3"],
            "cross_patterns": [
                {"pattern": "Auth blocks both goals", "affected_goals": ["goal-1", "goal-2"],
                 "severity": "high", "recommendation": "Fix auth first"},
            ],
            "summary": "Auth is the primary blocker.",
        })
        result = _parse_meta_response(text)
        assert len(result["questions"]) == 3
        assert len(result["answers"]) == 3
        assert len(result["cross_patterns"]) == 1
        assert result["cross_patterns"][0]["severity"] == "high"
        assert result["summary"] == "Auth is the primary blocker."

    def test_empty_input(self) -> None:
        assert _parse_meta_response("") == {}

    def test_invalid_json(self) -> None:
        assert _parse_meta_response("not json at all") == {}

    def test_strips_markdown_fences(self) -> None:
        inner = json.dumps({
            "questions": ["Q1"],
            "answers": ["A1"],
            "cross_patterns": [],
            "summary": "All good.",
        })
        text = f"```json\n{inner}\n```"
        result = _parse_meta_response(text)
        assert result["summary"] == "All good."

    def test_sanitizes_long_fields(self) -> None:
        text = json.dumps({
            "questions": ["Q" * 500],
            "answers": ["A" * 1000],
            "cross_patterns": [
                {"pattern": "P" * 500, "affected_goals": ["g1"],
                 "severity": "high", "recommendation": "R" * 500}
            ],
            "summary": "S" * 1000,
        })
        result = _parse_meta_response(text)
        assert len(result["questions"][0]) <= 300
        assert len(result["answers"][0]) <= 500
        assert len(result["cross_patterns"][0]["pattern"]) <= 200
        assert len(result["cross_patterns"][0]["recommendation"]) <= 200
        assert len(result["summary"]) <= 500

    def test_invalid_severity_defaults_to_low(self) -> None:
        text = json.dumps({
            "questions": [],
            "answers": [],
            "cross_patterns": [
                {"pattern": "test", "severity": "critical", "affected_goals": []}
            ],
            "summary": "",
        })
        result = _parse_meta_response(text)
        assert result["cross_patterns"][0]["severity"] == "low"

    def test_max_patterns_capped(self) -> None:
        text = json.dumps({
            "questions": [],
            "answers": [],
            "cross_patterns": [
                {"pattern": f"p{i}", "severity": "low", "affected_goals": []}
                for i in range(10)
            ],
            "summary": "",
        })
        result = _parse_meta_response(text)
        assert len(result["cross_patterns"]) <= 5


# ── run_meta_reflection ───────────────────────────────────────


class TestRunMetaReflection:
    async def test_skips_when_no_data(self) -> None:
        gs = _make_goal_store()
        rl = _make_run_log()
        config = MagicMock()
        result = await run_meta_reflection(gs, rl, config)
        assert result == {}
        gs.save_state.assert_not_called()

    @patch("secretary.goal_meta_reflection._call_meta_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [])
    async def test_stores_result_in_state(self, mock_client, mock_call) -> None:
        response_json = json.dumps({
            "questions": ["Why does auth fail?"],
            "answers": ["Token expires after 1h"],
            "cross_patterns": [
                {"pattern": "Auth blocks all", "affected_goals": ["goal-1"],
                 "severity": "high", "recommendation": "Refresh token proactively"}
            ],
            "summary": "Auth is the bottleneck.",
        })
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=response_json)]
        mock_call.return_value = mock_msg

        state: dict[str, Any] = {
            "reflections": [{"reflection": "test", "patterns": {}}],
        }
        gs = _make_goal_store(state=state)
        rl = _make_run_log()
        config = MagicMock()

        result = await run_meta_reflection(gs, rl, config)
        assert result["summary"] == "Auth is the bottleneck."
        assert len(state["meta_reflections"]) == 1
        assert "ts" in state["meta_reflections"][0]
        gs.save_state.assert_called_once()

    @patch("secretary.goal_meta_reflection._call_meta_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [])
    async def test_bounded_at_max(self, mock_client, mock_call) -> None:
        response_json = json.dumps({
            "questions": ["Q"],
            "answers": ["A"],
            "cross_patterns": [],
            "summary": "ok",
        })
        mock_msg = MagicMock()
        mock_msg.content = [MagicMock(text=response_json)]
        mock_call.return_value = mock_msg

        state: dict[str, Any] = {
            "reflections": [{"reflection": "test", "patterns": {}}],
            "meta_reflections": [
                {"questions": [], "answers": [], "cross_patterns": [], "summary": f"old-{i}", "ts": f"t{i}"}
                for i in range(MAX_META_REFLECTIONS)
            ],
        }
        gs = _make_goal_store(state=state)
        rl = _make_run_log()
        config = MagicMock()

        await run_meta_reflection(gs, rl, config)
        assert len(state["meta_reflections"]) == MAX_META_REFLECTIONS
        # Latest should be the new one
        assert state["meta_reflections"][-1]["summary"] == "ok"


# ── format_meta_reflection_section ─────────────────────────────


class TestFormatMetaReflectionSection:
    def test_no_data(self) -> None:
        result = format_meta_reflection_section({})
        assert "No cross-goal" in result

    def test_with_data(self) -> None:
        state = {
            "meta_reflections": [{
                "summary": "Auth is a shared blocker.",
                "questions": ["Why does auth affect all goals?"],
                "answers": ["Token expires globally"],
                "cross_patterns": [
                    {"pattern": "Auth failure cascade", "affected_goals": ["goal-1", "goal-2"],
                     "severity": "high", "recommendation": "Auto-refresh tokens"},
                ],
                "ts": "2026-03-18T12:00:00Z",
            }],
        }
        result = format_meta_reflection_section(state)
        assert "Auth is a shared blocker" in result
        assert "Auth failure cascade" in result
        assert "goal-1" in result
        assert "Auto-refresh" in result

    def test_multiple_reflections_shows_latest(self) -> None:
        state = {
            "meta_reflections": [
                {"summary": "old", "questions": [], "answers": [], "cross_patterns": [], "ts": "t1"},
                {"summary": "latest", "questions": [], "answers": [], "cross_patterns": [], "ts": "t2"},
            ],
        }
        result = format_meta_reflection_section(state)
        assert "latest" in result
        assert "Total meta-reflections: 2" in result


# ── format_meta_for_prompt ─────────────────────────────────────


class TestFormatMetaForPrompt:
    def test_empty_when_no_data(self) -> None:
        assert format_meta_for_prompt({}) == ""

    def test_includes_patterns_for_planner(self) -> None:
        state = {
            "meta_reflections": [{
                "summary": "Two goals share a network dependency.",
                "questions": [],
                "answers": [],
                "cross_patterns": [
                    {"pattern": "Network timeout", "affected_goals": ["goal-1", "goal-2"],
                     "severity": "medium", "recommendation": "Add retry logic"},
                ],
                "ts": "2026-03-18T12:00:00Z",
            }],
        }
        result = format_meta_for_prompt(state)
        assert "Cross-Goal Patterns" in result
        assert "Network timeout" in result
        assert "Add retry logic" in result
        assert "goal-1, goal-2" in result

    def test_severity_shown(self) -> None:
        state = {
            "meta_reflections": [{
                "summary": "test",
                "questions": [],
                "answers": [],
                "cross_patterns": [
                    {"pattern": "test pattern", "severity": "high",
                     "affected_goals": [], "recommendation": ""},
                ],
                "ts": "t1",
            }],
        }
        result = format_meta_for_prompt(state)
        assert "[high]" in result
