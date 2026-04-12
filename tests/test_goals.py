"""Tests for goals — proactive goal planner, store, and parser."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from secretary.goals import (
    GoalStore,
    _build_goal_prompt,
    _parse_goal_response,
    is_review_due,
    run_goal_review,
)


# ── Helpers ───────────────────────────────────────────────────


@dataclass
class FakeRunLogEntry:
    task: str = "check email"
    success: bool = True
    tier: str = "low"
    source: str = "campaign"
    goal_id: str = ""


@dataclass
class FakeRunLog:
    _entries: list[FakeRunLogEntry] = field(default_factory=list)

    def recent(self, n: int = 5) -> list[FakeRunLogEntry]:
        return self._entries[:n]


@dataclass
class FakeMemory:
    short: list[str] = field(default_factory=list)


SAMPLE_GOALS = [
    {
        "id": "prefix-survival",
        "description": "Ensure proxy routing resilience",
        "success_criteria": "All tiers execute without prefix",
        "priority": 1,
        "status": "in-progress",
        "sub_goals": [
            {"id": "oracle-production", "description": "Oracle ensemble in production", "status": "done"},
            {"id": "learned-router", "description": "Train cost-aware router", "status": "not-started"},
        ],
    },
    {
        "id": "self-improvement",
        "description": "Autonomous code review and testing",
        "success_criteria": "Secretary finds and fixes bugs autonomously",
        "priority": 3,
        "status": "not-started",
        "sub_goals": [
            {"id": "failure-analysis", "description": "Feed failed traces to LLM", "status": "not-started"},
        ],
    },
]


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.goals.enabled = True
    cfg.goals.goals_file = "goals.yaml"
    cfg.goals.review_interval_hours = 8
    cfg.goals.review_model = "claude-haiku-4.5"
    cfg.goals.max_tasks_per_review = 3
    cfg.data_root = "data"
    cfg.proxy_url = "http://localhost:4141"
    cfg.api_key = "test-key"
    cfg.agent_prefix = True
    cfg._goal_model = None
    return cfg


# ── GoalStore ─────────────────────────────────────────────────


class TestGoalStore:
    def test_load_goals_from_yaml(self, tmp_path: Path):
        goals_file = tmp_path / "goals.yaml"
        state_file = tmp_path / "goal_state.json"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))

        store = GoalStore(goals_file, state_file)
        store.load()

        assert len(store.goals) == 2
        assert store.goals[0]["id"] == "prefix-survival"
        assert store.last_reviewed is None

    def test_load_existing_state(self, tmp_path: Path):
        goals_file = tmp_path / "goals.yaml"
        state_file = tmp_path / "goal_state.json"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))
        state_file.write_text(json.dumps({
            "last_reviewed": "2026-03-17T10:00:00+00:00",
            "sub_goal_status": {"learned-router": {"status": "in-progress", "evidence": "Started training"}},
            "progress_notes": [],
        }))

        store = GoalStore(goals_file, state_file)
        store.load()

        assert store.last_reviewed == "2026-03-17T10:00:00+00:00"
        assert store.get_effective_status("learned-router", "not-started") == "in-progress"

    def test_load_missing_goals_file(self, tmp_path: Path):
        store = GoalStore(tmp_path / "missing.yaml", tmp_path / "state.json")
        store.load()
        assert store.goals == []

    def test_mark_reviewed(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        assert store.last_reviewed is None
        store.mark_reviewed()
        assert store.last_reviewed is not None

    def test_apply_updates(self, tmp_path: Path):
        goals_file = tmp_path / "g.yaml"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))
        store = GoalStore(goals_file, tmp_path / "s.json")
        store.load()

        store.apply_updates([
            {"sub_goal_id": "oracle-production", "new_status": "done", "evidence": "Tests pass"},
            {"sub_goal_id": "learned-router", "new_status": "in-progress", "evidence": "Started"},
        ])

        assert store.get_effective_status("oracle-production", "not-started") == "done"
        assert store.get_effective_status("learned-router", "not-started") == "in-progress"

    def test_apply_updates_rejects_invalid_status(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()

        store.apply_updates([
            {"sub_goal_id": "test", "new_status": "invalid_status", "evidence": "bad"},
        ])

        assert store.get_effective_status("test", "not-started") == "not-started"

    def test_apply_updates_rejects_invented_sub_ids(self, tmp_path: Path):
        """apply_updates should reject sub_ids not in goals.yaml."""
        goals_file = tmp_path / "goals.yaml"
        state_file = tmp_path / "goal_state.json"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))

        store = GoalStore(goals_file, state_file)
        store.load()

        # Try to update a valid and an invented sub_id
        store.apply_updates([
            {"sub_goal_id": "oracle-production", "new_status": "done", "evidence": "Real"},
            {"sub_goal_id": "self-improvement:goal-planner", "new_status": "in-progress", "evidence": "Invented"},
            {"sub_goal_id": "totally-fake", "new_status": "done", "evidence": "Also invented"},
        ])

        # Valid one accepted
        assert store.get_effective_status("oracle-production", "not-started") == "done"
        # Invented ones rejected
        assert "self-improvement:goal-planner" not in store._state["sub_goal_status"]
        assert "totally-fake" not in store._state["sub_goal_status"]

    def test_valid_sub_goal_ids(self, tmp_path: Path):
        """_valid_sub_goal_ids returns all sub-goal IDs from YAML."""
        goals_file = tmp_path / "goals.yaml"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))

        store = GoalStore(goals_file, tmp_path / "s.json")
        store.load()

        ids = store._valid_sub_goal_ids()
        assert ids == {"prefix-survival", "self-improvement", "oracle-production", "learned-router", "failure-analysis"}

    def test_prune_orphan_statuses(self, tmp_path: Path):
        """prune_orphan_statuses removes entries not in goals.yaml."""
        goals_file = tmp_path / "goals.yaml"
        state_file = tmp_path / "goal_state.json"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))
        state_file.write_text(json.dumps({
            "sub_goal_status": {
                "oracle-production": {"status": "done", "evidence": "Valid"},
                "self-improvement:goal-planner": {"status": "in-progress", "evidence": "Orphan"},
                "oracle-default: file read task": {"status": "blocked", "evidence": "Orphan"},
            },
            "step_plans": {
                "oracle-production": [{"step": "Deploy"}],
                "self-improvement:goal-planner": [{"step": "Invented"}],
            },
        }))

        store = GoalStore(goals_file, state_file)
        store.load()

        pruned = store.prune_orphan_statuses()
        assert pruned == 2
        assert "oracle-production" in store._state["sub_goal_status"]
        assert "self-improvement:goal-planner" not in store._state["sub_goal_status"]
        assert "oracle-default: file read task" not in store._state["sub_goal_status"]
        # Step plan for orphan also removed
        assert "self-improvement:goal-planner" not in store._state.get("step_plans", {})
        # Valid step plan preserved
        assert "oracle-production" in store._state.get("step_plans", {})

    def test_prune_orphan_statuses_no_orphans(self, tmp_path: Path):
        """prune_orphan_statuses returns 0 when all entries are valid."""
        goals_file = tmp_path / "goals.yaml"
        state_file = tmp_path / "goal_state.json"
        goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}))
        state_file.write_text(json.dumps({
            "sub_goal_status": {
                "oracle-production": {"status": "done", "evidence": "Valid"},
            },
        }))

        store = GoalStore(goals_file, state_file)
        store.load()

        pruned = store.prune_orphan_statuses()
        assert pruned == 0
        assert "oracle-production" in store._state["sub_goal_status"]

    def test_save_state_atomic(self, tmp_path: Path):
        state_file = tmp_path / "state.json"
        store = GoalStore(tmp_path / "g.yaml", state_file)
        store.load()
        store.mark_reviewed()
        store.add_progress_note("Made progress on X")
        store.save_state()

        # Verify file was written
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["last_reviewed"] is not None
        assert len(data["progress_notes"]) == 1

    def test_progress_notes_capped_at_20(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        for i in range(25):
            store.add_progress_note(f"Note {i}")
        assert len(store._state["progress_notes"]) == 20

    def test_get_effective_status_no_override(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        assert store.get_effective_status("unknown-id", "not-started") == "not-started"


# ── is_review_due ─────────────────────────────────────────────


class TestIsReviewDue:
    def test_never_reviewed(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        assert is_review_due(store, 8) is True

    def test_recently_reviewed(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        store.mark_reviewed()
        assert is_review_due(store, 8) is False

    def test_review_overdue(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        past = datetime.now(timezone.utc) - timedelta(hours=10)
        store._state["last_reviewed"] = past.isoformat()
        assert is_review_due(store, 8) is True

    def test_exactly_at_boundary(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        boundary = datetime.now(timezone.utc) - timedelta(hours=8, minutes=1)
        store._state["last_reviewed"] = boundary.isoformat()
        assert is_review_due(store, 8) is True

    def test_invalid_timestamp(self, tmp_path: Path):
        store = GoalStore(tmp_path / "g.yaml", tmp_path / "s.json")
        store.load()
        store._state["last_reviewed"] = "not-a-date"
        assert is_review_due(store, 8) is True


# ── _build_goal_prompt ────────────────────────────────────────


class TestBuildGoalPrompt:
    def test_includes_goal_hierarchy(self):
        prompt = _build_goal_prompt(SAMPLE_GOALS, {}, [], "", [])
        assert "prefix-survival" in prompt
        assert "Ensure proxy routing" in prompt
        assert "oracle-production" in prompt
        assert "done" in prompt

    def test_includes_sub_goal_status_override(self):
        overrides = {"learned-router": {"status": "in-progress"}}
        prompt = _build_goal_prompt(SAMPLE_GOALS, overrides, [], "", [])
        assert "in-progress" in prompt

    def test_includes_recent_activity(self):
        log_entries = [{"task": "triage inbox", "success": True}]
        prompt = _build_goal_prompt(SAMPLE_GOALS, {}, log_entries, "", [])
        assert "PASS" in prompt
        assert "triage inbox" in prompt

    def test_includes_failed_activity(self):
        log_entries = [{"task": "deploy code", "success": False}]
        prompt = _build_goal_prompt(SAMPLE_GOALS, {}, log_entries, "", [])
        assert "FAIL" in prompt

    def test_includes_memory(self):
        prompt = _build_goal_prompt(SAMPLE_GOALS, {}, [], "Oracle saves 91% cost", [])
        assert "Oracle saves 91% cost" in prompt

    def test_no_memory_no_section(self):
        prompt = _build_goal_prompt(SAMPLE_GOALS, {}, [], "", [])
        assert "Key Memory" not in prompt

    def test_includes_progress_notes(self):
        notes = [{"note": "Finished oracle benchmarks", "ts": "2026-03-17T10:00:00Z"}]
        prompt = _build_goal_prompt(SAMPLE_GOALS, {}, [], "", notes)
        assert "Finished oracle benchmarks" in prompt
        assert "Previous Review Notes" in prompt

    def test_empty_goals(self):
        prompt = _build_goal_prompt([], {}, [], "", [])
        assert "Long-Horizon Goals" in prompt

    def test_assessment_section_present(self):
        prompt = _build_goal_prompt(SAMPLE_GOALS, {}, [], "", [])
        assert "Your Assessment" in prompt
        assert "stalled" in prompt


# ── _parse_goal_response ──────────────────────────────────────


class TestParseGoalResponse:
    def test_valid_response(self):
        text = json.dumps({
            "tasks": [
                {"prompt": "Train router on run_log data", "tier": "high", "priority": 2, "goal_id": "prefix-survival"},
            ],
            "goal_updates": [
                {"sub_goal_id": "learned-router", "new_status": "in-progress", "evidence": "Started"},
            ],
            "reasoning": "Prefix survival is critical and router is not started yet.",
        })
        result = _parse_goal_response(text)
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["prompt"] == "Train router on run_log data"
        assert result["tasks"][0]["source"] == "goals"
        assert result["tasks"][0]["id"] == "goal-1"
        assert result["tasks"][0]["goal_id"] == "prefix-survival"
        assert len(result["goal_updates"]) == 1
        assert result["goal_updates"][0]["new_status"] == "in-progress"
        assert "critical" in result["reasoning"]

    def test_empty_tasks(self):
        text = json.dumps({"tasks": [], "goal_updates": [], "reasoning": "All on track"})
        result = _parse_goal_response(text)
        assert result["tasks"] == []
        assert result["reasoning"] == "All on track"

    def test_tasks_capped_at_3(self):
        tasks = [
            {"prompt": f"Task {i}", "tier": "low", "priority": i, "goal_id": "test"}
            for i in range(6)
        ]
        text = json.dumps({"tasks": tasks, "goal_updates": [], "reasoning": ""})
        result = _parse_goal_response(text)
        assert len(result["tasks"]) == 3

    def test_invalid_tier_defaults_to_medium(self):
        text = json.dumps({
            "tasks": [{"prompt": "Do thing", "tier": "ultra", "priority": 1, "goal_id": "x"}],
            "goal_updates": [],
            "reasoning": "",
        })
        result = _parse_goal_response(text)
        assert result["tasks"][0]["tier"] == "medium"

    def test_invalid_priority_defaults(self):
        text = json.dumps({
            "tasks": [{"prompt": "Do thing", "tier": "low", "priority": -5, "goal_id": "x"}],
            "goal_updates": [],
            "reasoning": "",
        })
        result = _parse_goal_response(text)
        assert result["tasks"][0]["priority"] == 3

    def test_missing_prompt_skips_task(self):
        text = json.dumps({
            "tasks": [{"tier": "low", "priority": 1, "goal_id": "x"}],
            "goal_updates": [],
            "reasoning": "",
        })
        result = _parse_goal_response(text)
        assert result["tasks"] == []

    def test_invalid_json_returns_empty(self):
        result = _parse_goal_response("not valid json {{{")
        assert result["tasks"] == []
        assert result["goal_updates"] == []

    def test_empty_string(self):
        result = _parse_goal_response("")
        assert result["tasks"] == []

    def test_markdown_fences_stripped(self):
        inner = json.dumps({
            "tasks": [{"prompt": "Task A", "tier": "low", "priority": 1, "goal_id": "g1"}],
            "goal_updates": [],
            "reasoning": "test",
        })
        text = f"```json\n{inner}\n```"
        result = _parse_goal_response(text)
        assert len(result["tasks"]) == 1

    def test_non_dict_response(self):
        result = _parse_goal_response("[1, 2, 3]")
        assert result["tasks"] == []

    def test_invalid_update_status_skipped(self):
        text = json.dumps({
            "tasks": [],
            "goal_updates": [
                {"sub_goal_id": "x", "new_status": "exploded", "evidence": "bad"},
            ],
            "reasoning": "",
        })
        result = _parse_goal_response(text)
        assert result["goal_updates"] == []

    def test_updates_capped_at_10(self):
        updates = [
            {"sub_goal_id": f"sg-{i}", "new_status": "done", "evidence": f"ev{i}"}
            for i in range(15)
        ]
        text = json.dumps({"tasks": [], "goal_updates": updates, "reasoning": ""})
        result = _parse_goal_response(text)
        assert len(result["goal_updates"]) == 10

    def test_evidence_truncated(self):
        text = json.dumps({
            "tasks": [],
            "goal_updates": [
                {"sub_goal_id": "x", "new_status": "done", "evidence": "A" * 500},
            ],
            "reasoning": "",
        })
        result = _parse_goal_response(text)
        assert len(result["goal_updates"][0]["evidence"]) == 200


# ── run_goal_review ───────────────────────────────────────────


class TestRunGoalReview:
    def test_no_goals_returns_empty(self):
        store = MagicMock()
        store.goals = []
        run_log = FakeRunLog()
        memory = FakeMemory()
        config = _make_config()

        result = asyncio.run(run_goal_review(store, run_log, memory, config))
        assert result == []

    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_successful_review(self, mock_build_client):
        # Mock the Anthropic response
        response_text = json.dumps({
            "tasks": [
                {"prompt": "Train the cost router", "tier": "high", "priority": 2, "goal_id": "prefix-survival"},
            ],
            "goal_updates": [
                {"sub_goal_id": "goal-planner", "new_status": "done", "evidence": "Implemented in goals.py"},
            ],
            "reasoning": "Prefix survival is top priority, router not started.",
        })

        mock_block = MagicMock()
        mock_block.text = response_text
        mock_message = MagicMock()
        mock_message.content = [mock_block]

        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.get_final_message.return_value = mock_message

        mock_client = MagicMock()
        mock_client.messages.stream.return_value = mock_stream
        mock_build_client.return_value = mock_client

        store = MagicMock()
        store.goals = SAMPLE_GOALS
        store._state = {"sub_goal_status": {}, "progress_notes": []}
        run_log = FakeRunLog(
            _entries=[FakeRunLogEntry(task="Oracle benchmark", success=True)]
        )
        memory = FakeMemory(short=["Oracle saves 91%"])
        config = _make_config()

        tasks = asyncio.run(run_goal_review(store, run_log, memory, config))

        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "Train the cost router"
        assert tasks[0]["source"] == "goals"
        assert tasks[0]["goal_id"] == "prefix-survival"
        store.apply_updates.assert_called_once()
        store.mark_reviewed.assert_called_once()
        store.save_state.assert_called_once()

    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_planner_failure_returns_empty(self, mock_build_client):
        # Fail on the stream call, not client construction
        mock_client = MagicMock()
        mock_client.messages.stream.side_effect = Exception("API down")
        mock_build_client.return_value = mock_client

        store = MagicMock()
        store.goals = SAMPLE_GOALS
        store._state = {"sub_goal_status": {}, "progress_notes": []}
        run_log = FakeRunLog()
        memory = FakeMemory()
        config = _make_config()

        tasks = asyncio.run(run_goal_review(store, run_log, memory, config))
        assert tasks == []

    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_empty_tasks_response(self, mock_build_client):
        response_text = json.dumps({
            "tasks": [],
            "goal_updates": [],
            "reasoning": "All goals on track.",
        })

        mock_block = MagicMock()
        mock_block.text = response_text
        mock_message = MagicMock()
        mock_message.content = [mock_block]

        mock_stream = MagicMock()
        mock_stream.__enter__ = MagicMock(return_value=mock_stream)
        mock_stream.__exit__ = MagicMock(return_value=False)
        mock_stream.get_final_message.return_value = mock_message

        mock_client = MagicMock()
        mock_client.messages.stream.return_value = mock_stream
        mock_build_client.return_value = mock_client

        store = MagicMock()
        store.goals = SAMPLE_GOALS
        store._state = {"sub_goal_status": {}, "progress_notes": []}
        run_log = FakeRunLog()
        memory = FakeMemory()
        config = _make_config()

        tasks = asyncio.run(run_goal_review(store, run_log, memory, config))
        assert tasks == []
        store.mark_reviewed.assert_called_once()
        store.save_state.assert_called_once()
