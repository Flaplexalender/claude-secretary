"""Tests for ooda — OODA decision loop planner and parser."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.ooda import (
    _build_ooda_prompt,
    _parse_planner_response,
    run_ooda_cycle,
)
from secretary.event_bus import Event, EventBus, EventType


# ── Helpers ───────────────────────────────────────────────────


@dataclass
class FakeRunLogEntry:
    task: str = "check email"
    success: bool = True
    tier: str = "low"


@dataclass
class FakeRunLog:
    _entries: list[FakeRunLogEntry] = field(default_factory=list)

    def recent(self, n: int = 5) -> list[FakeRunLogEntry]:
        return self._entries[:n]


@dataclass
class FakeMemory:
    short: list[str] = field(default_factory=list)


def _make_config(ooda_enabled: bool = True) -> MagicMock:
    cfg = MagicMock()
    cfg.events.ooda_enabled = ooda_enabled
    cfg.events.ooda_model = "claude-haiku-4.5"
    cfg.proxy_url = "http://localhost:4141"
    cfg.api_key = "test-key"
    cfg.agent_prefix = True
    return cfg


# ── _build_ooda_prompt ────────────────────────────────────────


class TestBuildOodaPrompt:
    def test_with_events(self):
        events = [
            Event(type=EventType.NEW_EMAIL, payload={"from": "boss@co.com", "subject": "Urgent"}),
        ]
        prompt = _build_ooda_prompt(events, [], "")
        assert "Events This Cycle" in prompt
        assert "new_email" in prompt
        assert "boss@co.com" in prompt

    def test_no_events(self):
        prompt = _build_ooda_prompt([], [], "")
        assert "No new events" in prompt

    def test_with_recent_log(self):
        log_entries = [{"task": "triage inbox", "success": True}]
        prompt = _build_ooda_prompt([], log_entries, "")
        assert "PASS" in prompt
        assert "triage inbox" in prompt

    def test_with_failed_log(self):
        log_entries = [{"task": "send email", "success": False}]
        prompt = _build_ooda_prompt([], log_entries, "")
        assert "FAIL" in prompt

    def test_with_memory_summary(self):
        prompt = _build_ooda_prompt([], [], "Boss prefers morning emails")
        assert "Boss prefers morning emails" in prompt
        assert "Key Memory" in prompt

    def test_empty_memory_no_section(self):
        prompt = _build_ooda_prompt([], [], "")
        assert "Key Memory" not in prompt

    def test_events_capped_at_10(self):
        events = [
            Event(type=EventType.NEW_EMAIL, payload={"from": f"user{i}@x.com", "subject": f"msg{i}"})
            for i in range(15)
        ]
        prompt = _build_ooda_prompt(events, [], "")
        # Only first 10 should appear
        assert "user9@x.com" in prompt
        assert "user10@x.com" not in prompt

    def test_raw_text_preview(self):
        events = [
            Event(type=EventType.NEW_EMAIL, payload={"from": "a@b.com", "subject": "hi", "raw_text": "Hello world body"}),
        ]
        prompt = _build_ooda_prompt(events, [], "")
        assert "Hello world body" in prompt
        assert "Preview:" in prompt

    def test_decision_section_present(self):
        prompt = _build_ooda_prompt([], [], "")
        assert "Your Decision" in prompt
        assert "JSON array" in prompt


# ── _parse_planner_response ───────────────────────────────────


class TestParsePlannerResponse:
    def test_valid_json_array(self):
        text = json.dumps([
            {"prompt": "Read boss email", "tier": "medium", "priority": 1},
            {"prompt": "Check calendar", "tier": "low", "priority": 3},
        ])
        tasks = _parse_planner_response(text)
        assert len(tasks) == 2
        assert tasks[0]["prompt"] == "Read boss email"
        assert tasks[0]["tier"] == "medium"
        assert tasks[0]["priority"] == 1
        assert tasks[0]["source"] == "ooda"
        assert tasks[0]["id"] == "ooda-1"
        assert tasks[1]["id"] == "ooda-2"

    def test_empty_array(self):
        assert _parse_planner_response("[]") == []

    def test_empty_string(self):
        assert _parse_planner_response("") == []

    def test_markdown_fences(self):
        text = "```json\n[{\"prompt\": \"test\", \"tier\": \"low\", \"priority\": 2}]\n```"
        tasks = _parse_planner_response(text)
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "test"

    def test_invalid_json(self):
        assert _parse_planner_response("not valid json at all") == []

    def test_non_list_response(self):
        assert _parse_planner_response('{"prompt": "x"}') == []

    def test_invalid_tier_defaults_to_medium(self):
        text = json.dumps([{"prompt": "task", "tier": "ultra", "priority": 1}])
        tasks = _parse_planner_response(text)
        assert tasks[0]["tier"] == "medium"

    def test_missing_tier_defaults_to_medium(self):
        text = json.dumps([{"prompt": "task", "priority": 1}])
        tasks = _parse_planner_response(text)
        assert tasks[0]["tier"] == "medium"

    def test_invalid_priority_defaults_to_3(self):
        text = json.dumps([{"prompt": "task", "tier": "low", "priority": "high"}])
        tasks = _parse_planner_response(text)
        assert tasks[0]["priority"] == 3

    def test_negative_priority_defaults_to_3(self):
        text = json.dumps([{"prompt": "task", "tier": "low", "priority": -1}])
        tasks = _parse_planner_response(text)
        assert tasks[0]["priority"] == 3

    def test_capped_at_5_tasks(self):
        items = [{"prompt": f"task {i}", "tier": "low", "priority": i} for i in range(10)]
        text = json.dumps(items)
        tasks = _parse_planner_response(text)
        assert len(tasks) == 5

    def test_skips_entries_without_prompt(self):
        text = json.dumps([{"tier": "low"}, {"prompt": "valid", "tier": "low", "priority": 1}])
        tasks = _parse_planner_response(text)
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "valid"

    def test_skips_non_dict_entries(self):
        text = json.dumps(["string entry", {"prompt": "valid", "tier": "low", "priority": 1}])
        tasks = _parse_planner_response(text)
        assert len(tasks) == 1

    def test_float_priority_cast_to_int(self):
        text = json.dumps([{"prompt": "x", "tier": "low", "priority": 2.5}])
        tasks = _parse_planner_response(text)
        assert tasks[0]["priority"] == 2
        assert isinstance(tasks[0]["priority"], int)

    def test_whitespace_tolerance(self):
        text = "  \n  []  \n  "
        assert _parse_planner_response(text) == []


# ── run_ooda_cycle ────────────────────────────────────────────


class TestRunOodaCycle:
    def test_no_events_returns_empty(self):
        bus = EventBus()
        log = FakeRunLog()
        mem = FakeMemory()
        cfg = _make_config()

        result = asyncio.run(run_ooda_cycle(bus, log, mem, cfg))
        assert result == []

    @patch("secretary.ooda._call_planner")
    @patch("secretary.direct_agent._build_client")
    def test_with_events_calls_planner(self, mock_client, mock_planner):
        bus = EventBus()
        bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "a@b.com", "subject": "hi"}))

        # Mock planner returns a valid response
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text=json.dumps([{"prompt": "Read email from a@b.com", "tier": "low", "priority": 1}]))
        ]
        mock_planner.return_value = mock_response

        log = FakeRunLog()
        mem = FakeMemory()
        cfg = _make_config()

        result = asyncio.run(run_ooda_cycle(bus, log, mem, cfg))
        assert len(result) == 1
        assert result[0]["prompt"] == "Read email from a@b.com"
        assert result[0]["source"] == "ooda"
        mock_planner.assert_called_once()

    @patch("secretary.ooda._call_planner")
    @patch("secretary.direct_agent._build_client")
    def test_planner_exception_returns_empty(self, mock_client, mock_planner):
        bus = EventBus()
        bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "x@y.com", "subject": "test"}))
        mock_planner.side_effect = Exception("API timeout")

        result = asyncio.run(
            run_ooda_cycle(bus, FakeRunLog(), FakeMemory(), _make_config())
        )
        assert result == []

    @patch("secretary.ooda._call_planner")
    @patch("secretary.direct_agent._build_client")
    def test_planner_empty_response_returns_empty(self, mock_client, mock_planner):
        bus = EventBus()
        bus.emit(Event(type=EventType.CALENDAR_SOON, payload={"title": "Standup", "minutes_until": 10}))

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]
        mock_planner.return_value = mock_response

        result = asyncio.run(
            run_ooda_cycle(bus, FakeRunLog(), FakeMemory(), _make_config())
        )
        assert result == []

    @patch("secretary.ooda._call_planner")
    @patch("secretary.direct_agent._build_client")
    def test_memory_context_included(self, mock_client, mock_planner):
        bus = EventBus()
        bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "a@b.com", "subject": "hi"}))

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]
        mock_planner.return_value = mock_response

        mem = FakeMemory(short=["Boss prefers morning replies"])
        result = asyncio.run(
            run_ooda_cycle(bus, FakeRunLog(), mem, _make_config())
        )
        # Verify the planner was called (memory was gathered)
        mock_planner.assert_called_once()

    @patch("secretary.ooda._call_planner")
    @patch("secretary.direct_agent._build_client")
    def test_recent_log_included(self, mock_client, mock_planner):
        bus = EventBus()
        bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "a@b.com", "subject": "hi"}))

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]
        mock_planner.return_value = mock_response

        log = FakeRunLog([FakeRunLogEntry(task="sent reply", success=True)])
        result = asyncio.run(
            run_ooda_cycle(bus, log, FakeMemory(), _make_config())
        )
        mock_planner.assert_called_once()


# ── Config defaults ───────────────────────────────────────────


class TestOodaConfig:
    def test_ooda_disabled_by_default(self):
        from secretary.config import EventConfig
        cfg = EventConfig()
        assert cfg.ooda_enabled is False

    def test_ooda_model_default(self):
        from secretary.config import EventConfig
        cfg = EventConfig()
        assert cfg.ooda_model == "claude-haiku-4.5"
