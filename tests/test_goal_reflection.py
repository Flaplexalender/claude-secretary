"""Tests for goal_reflection — Reflexion-style verbal feedback loop."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from secretary.goal_reflection import (
    _build_reflection_prompt,
    _get_goal_task_outcomes,
    _parse_reflection_response,
    run_goal_reflection,
    MAX_REFLECTIONS,
)
from secretary.goals import GoalStore
from secretary.run_log import RunLogEntry


# ── Helpers ───────────────────────────────────────────────────


SAMPLE_GOALS = [
    {
        "id": "prefix-survival",
        "description": "Ensure proxy routing resilience",
        "priority": 1,
        "status": "in-progress",
        "sub_goals": [
            {"id": "learned-router", "description": "Train cost-aware router", "status": "not-started"},
        ],
    },
    {
        "id": "self-improvement",
        "description": "Autonomous code review and testing",
        "priority": 3,
        "status": "not-started",
    },
]


def _make_entry(
    task: str = "goal task",
    success: bool = True,
    source: str = "goals",
    goal_id: str = "prefix-survival",
    error: str | None = None,
    output_preview: str = "done",
) -> RunLogEntry:
    return RunLogEntry(
        timestamp="2026-07-17T10:00:00+00:00",
        cycle=1,
        task=task,
        tier="medium",
        model="claude-haiku-4.5",
        success=success,
        output_preview=output_preview,
        error=error,
        source=source,
        goal_id=goal_id,
    )


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.proxy_url = "http://localhost:4141"
    cfg.api_key = "test-key"
    cfg.agent_prefix = True
    return cfg


class FakeRunLog:
    """Fake RunLog that returns provided entries from recent()."""
    def __init__(self, entries: list[RunLogEntry] | None = None):
        self._entries = entries or []

    def recent(self, n: int = 20) -> list[RunLogEntry]:
        return self._entries[-n:]


# ── _get_goal_task_outcomes ───────────────────────────────────


class TestGetGoalTaskOutcomes:
    def test_filters_source_goals(self):
        entries = [
            _make_entry(task="campaign task", source="campaign"),
            _make_entry(task="ooda task", source="ooda"),
            _make_entry(task="goal task 1", source="goals"),
            _make_entry(task="goal task 2", source="goals"),
        ]
        log = FakeRunLog(entries)
        outcomes = _get_goal_task_outcomes(log)
        assert len(outcomes) == 2
        assert all(e.source == "goals" for e in outcomes)

    def test_empty_when_no_goal_tasks(self):
        entries = [_make_entry(source="campaign")]
        log = FakeRunLog(entries)
        assert _get_goal_task_outcomes(log) == []

    def test_empty_on_empty_log(self):
        log = FakeRunLog([])
        assert _get_goal_task_outcomes(log) == []


# ── _build_reflection_prompt ─────────────────────────────────


class TestBuildReflectionPrompt:
    def test_includes_outcomes(self):
        outcomes = [
            _make_entry(task="Train router model", success=True),
            _make_entry(task="Fix budget alerts", success=False, error="API timeout"),
        ]
        prompt = _build_reflection_prompt(outcomes, SAMPLE_GOALS, [])
        assert "Train router model" in prompt
        assert "Fix budget alerts" in prompt
        assert "SUCCESS" in prompt
        assert "FAILED" in prompt
        assert "API timeout" in prompt
        assert "50%" in prompt  # 1/2 success rate

    def test_includes_goal_context(self):
        prompt = _build_reflection_prompt([], SAMPLE_GOALS, [])
        assert "prefix-survival" in prompt
        assert "self-improvement" in prompt

    def test_includes_previous_reflections(self):
        refs = [{"reflection": "Focus on stalled goals next time"}]
        prompt = _build_reflection_prompt([], SAMPLE_GOALS, refs)
        assert "Focus on stalled goals" in prompt

    def test_no_outcomes_message(self):
        prompt = _build_reflection_prompt([], SAMPLE_GOALS, [])
        assert "No goal-originated tasks" in prompt

    def test_includes_goal_id_tag(self):
        outcomes = [_make_entry(task="Do X", goal_id="self-improvement")]
        prompt = _build_reflection_prompt(outcomes, SAMPLE_GOALS, [])
        assert "[goal: self-improvement]" in prompt


# ── _parse_reflection_response ───────────────────────────────


class TestParseReflectionResponse:
    def test_valid_full_response(self):
        resp = json.dumps({
            "reflection": "Goal tasks had 60% success rate. Budget alerts failing.",
            "strategy_adjustments": [
                "Use simpler tier for alerts",
                "Pre-fetch data before complex tasks",
            ],
            "status_updates": [
                {"sub_goal_id": "oracle-production", "new_status": "done", "evidence": "All tests pass"},
            ],
            "patterns": {
                "working": ["Email tasks succeed reliably"],
                "failing": ["API-dependent tasks time out"],
            },
        })
        result = _parse_reflection_response(resp)
        assert "60% success rate" in result["reflection"]
        assert len(result["strategy_adjustments"]) == 2
        assert result["status_updates"][0]["sub_goal_id"] == "oracle-production"
        assert len(result["patterns"]["working"]) == 1
        assert len(result["patterns"]["failing"]) == 1

    def test_empty_text_returns_empty(self):
        assert _parse_reflection_response("") == {}

    def test_invalid_json_returns_empty(self):
        assert _parse_reflection_response("not json at all") == {}

    def test_markdown_fences_stripped(self):
        resp = '```json\n' + json.dumps({
            "reflection": "test", "strategy_adjustments": [],
            "status_updates": [], "patterns": {"working": [], "failing": []},
        }) + '\n```'
        result = _parse_reflection_response(resp)
        assert result["reflection"] == "test"

    def test_truncates_long_reflection(self):
        resp = json.dumps({
            "reflection": "x" * 1000,
            "strategy_adjustments": [],
            "status_updates": [],
            "patterns": {"working": [], "failing": []},
        })
        result = _parse_reflection_response(resp)
        assert len(result["reflection"]) <= 500

    def test_caps_strategy_adjustments(self):
        resp = json.dumps({
            "reflection": "ok",
            "strategy_adjustments": [f"adj_{i}" for i in range(10)],
            "status_updates": [],
            "patterns": {"working": [], "failing": []},
        })
        result = _parse_reflection_response(resp)
        assert len(result["strategy_adjustments"]) <= 5

    def test_rejects_invalid_status_updates(self):
        resp = json.dumps({
            "reflection": "ok",
            "strategy_adjustments": [],
            "status_updates": [
                {"sub_goal_id": "x", "new_status": "invalid", "evidence": "bad"},
                {"sub_goal_id": "y", "new_status": "done", "evidence": "good"},
            ],
            "patterns": {"working": [], "failing": []},
        })
        result = _parse_reflection_response(resp)
        assert len(result["status_updates"]) == 1
        assert result["status_updates"][0]["sub_goal_id"] == "y"

    def test_non_dict_returns_empty(self):
        assert _parse_reflection_response(json.dumps([1, 2, 3])) == {}

    def test_missing_fields_get_defaults(self):
        resp = json.dumps({"reflection": "minimal"})
        result = _parse_reflection_response(resp)
        assert result["reflection"] == "minimal"
        assert result["strategy_adjustments"] == []
        assert result["status_updates"] == []
        assert result["patterns"] == {"working": [], "failing": []}


# ── run_goal_reflection ──────────────────────────────────────


class FakeMessage:
    """Fake Anthropic message response."""
    def __init__(self, text: str):
        self.content = [MagicMock(text=text)]


class TestRunGoalReflection:
    def _make_store(self, tmp_path: Path) -> GoalStore:
        goals_file = tmp_path / "goals.yaml"
        state_file = tmp_path / "goal_state.json"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))
        store = GoalStore(goals_file, state_file)
        store.load()
        return store

    @patch("secretary.goal_reflection._call_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_stores_reflection_in_state(self, mock_client, mock_call, tmp_path):
        reflection_json = json.dumps({
            "reflection": "Tasks are progressing well on prefix-survival.",
            "strategy_adjustments": ["Focus more on router training"],
            "status_updates": [],
            "patterns": {"working": ["cost monitoring"], "failing": []},
        })
        mock_call.return_value = FakeMessage(reflection_json)

        store = self._make_store(tmp_path)
        log = FakeRunLog([_make_entry(task="Train router", success=True)])
        config = _make_config()

        result = asyncio.run(run_goal_reflection(store, log, config))

        assert result["reflection"] == "Tasks are progressing well on prefix-survival."
        # Check it was stored in state
        assert len(store._state.get("reflections", [])) == 1
        stored = store._state["reflections"][0]
        assert "progressing well" in stored["reflection"]

    @patch("secretary.goal_reflection._call_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_caps_reflections_at_max(self, mock_client, mock_call, tmp_path):
        store = self._make_store(tmp_path)
        # Pre-fill with MAX_REFLECTIONS reflections
        store._state["reflections"] = [
            {"reflection": f"old-{i}", "strategy_adjustments": [], "patterns": {}}
            for i in range(MAX_REFLECTIONS)
        ]

        reflection_json = json.dumps({
            "reflection": "new reflection",
            "strategy_adjustments": [],
            "status_updates": [],
            "patterns": {"working": [], "failing": []},
        })
        mock_call.return_value = FakeMessage(reflection_json)

        log = FakeRunLog([_make_entry()])
        config = _make_config()
        asyncio.run(run_goal_reflection(store, log, config))

        assert len(store._state["reflections"]) == MAX_REFLECTIONS
        assert store._state["reflections"][-1]["reflection"] == "new reflection"

    @patch("secretary.goal_reflection._call_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_applies_status_updates(self, mock_client, mock_call, tmp_path):
        reflection_json = json.dumps({
            "reflection": "Router training complete.",
            "strategy_adjustments": [],
            "status_updates": [
                {"sub_goal_id": "learned-router", "new_status": "done", "evidence": "Router deployed"},
            ],
            "patterns": {"working": [], "failing": []},
        })
        mock_call.return_value = FakeMessage(reflection_json)

        store = self._make_store(tmp_path)
        log = FakeRunLog([_make_entry()])
        config = _make_config()

        asyncio.run(run_goal_reflection(store, log, config))

        assert store.get_effective_status("learned-router", "not-started") == "done"

    @patch("secretary.goal_reflection._call_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_skips_when_no_outcomes_and_already_reflected(self, mock_client, mock_call, tmp_path):
        store = self._make_store(tmp_path)
        store._state["reflections"] = [{"reflection": "previous"}]

        log = FakeRunLog([])  # No goal tasks
        config = _make_config()

        result = asyncio.run(run_goal_reflection(store, log, config))
        assert result == {}
        mock_call.assert_not_called()

    @patch("secretary.goal_reflection._call_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_bootstrap_reflection_on_first_run(self, mock_client, mock_call, tmp_path):
        """First run with no outcomes and no prior reflections should still reflect."""
        reflection_json = json.dumps({
            "reflection": "No tasks yet. Focus on highest-priority goal first.",
            "strategy_adjustments": ["Start with prefix-survival"],
            "status_updates": [],
            "patterns": {"working": [], "failing": []},
        })
        mock_call.return_value = FakeMessage(reflection_json)

        store = self._make_store(tmp_path)
        log = FakeRunLog([])  # No goal tasks yet
        config = _make_config()

        result = asyncio.run(run_goal_reflection(store, log, config))
        assert "No tasks yet" in result["reflection"]

    @patch("secretary.goal_reflection._call_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_records_success_rate(self, mock_client, mock_call, tmp_path):
        reflection_json = json.dumps({
            "reflection": "Mixed results.",
            "strategy_adjustments": [],
            "status_updates": [],
            "patterns": {"working": [], "failing": []},
        })
        mock_call.return_value = FakeMessage(reflection_json)

        store = self._make_store(tmp_path)
        outcomes = [
            _make_entry(task="t1", success=True),
            _make_entry(task="t2", success=False, error="timeout"),
            _make_entry(task="t3", success=True),
        ]
        log = FakeRunLog(outcomes)
        config = _make_config()

        asyncio.run(run_goal_reflection(store, log, config))

        stored = store._state["reflections"][0]
        assert stored["task_count"] == 3
        assert abs(stored["success_rate"] - 2 / 3) < 0.01

    @patch("secretary.goal_reflection._call_reflection")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_api_failure_returns_empty(self, mock_client, mock_call, tmp_path):
        mock_call.side_effect = Exception("API error")

        store = self._make_store(tmp_path)
        log = FakeRunLog([_make_entry()])
        config = _make_config()

        result = asyncio.run(run_goal_reflection(store, log, config))
        assert result == {}


# ── RunLogEntry source field ─────────────────────────────────


class TestRunLogEntrySource:
    def test_default_source_is_campaign(self):
        entry = RunLogEntry(
            timestamp="2026-07-17T10:00:00",
            cycle=1, task="test", tier="low", model="haiku",
            success=True, output_preview="",
        )
        assert entry.source == "campaign"
        assert entry.goal_id == ""

    def test_source_field_round_trip(self):
        """Source and goal_id survive JSON serialization/deserialization."""
        import json
        from dataclasses import asdict
        entry = _make_entry(source="goals", goal_id="prefix-survival")
        d = asdict(entry)
        json_str = json.dumps(d)
        loaded = json.loads(json_str)
        restored = RunLogEntry(**loaded)
        assert restored.source == "goals"
        assert restored.goal_id == "prefix-survival"

    def test_backward_compat_missing_source(self):
        """Old log entries without source/goal_id still deserialize."""
        old_dict = {
            "timestamp": "2026-01-01T00:00:00",
            "cycle": 0, "task": "old", "tier": "low", "model": "haiku",
            "success": True, "output_preview": "",
        }
        entry = RunLogEntry(**old_dict)
        assert entry.source == "campaign"
        assert entry.goal_id == ""


# ── Goal prompt with reflections ─────────────────────────────


class TestGoalPromptWithReflections:
    def test_reflections_appear_in_prompt(self):
        from secretary.goals import _build_goal_prompt

        reflections = [{
            "reflection": "Budget alerts keep timing out. Use simpler tier.",
            "strategy_adjustments": ["Downgrade alert tasks to low tier"],
            "patterns": {
                "working": ["Email tasks succeed"],
                "failing": ["API tasks time out"],
            },
        }]
        prompt = _build_goal_prompt(
            goals=SAMPLE_GOALS,
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            reflections=reflections,
        )
        assert "Budget alerts keep timing out" in prompt
        assert "Downgrade alert tasks" in prompt
        assert "Email tasks succeed" in prompt
        assert "API tasks time out" in prompt
        assert "Reflections from Previous Task Outcomes" in prompt

    def test_no_reflections_section_when_empty(self):
        from secretary.goals import _build_goal_prompt

        prompt = _build_goal_prompt(
            goals=SAMPLE_GOALS,
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            reflections=[],
        )
        assert "Reflections from Previous Task Outcomes" not in prompt

    def test_no_reflections_section_when_none(self):
        from secretary.goals import _build_goal_prompt

        prompt = _build_goal_prompt(
            goals=SAMPLE_GOALS,
            sub_goal_overrides={},
            recent_log=[],
            memory_summary="",
            progress_notes=[],
            reflections=None,
        )
        assert "Reflections from Previous Task Outcomes" not in prompt
