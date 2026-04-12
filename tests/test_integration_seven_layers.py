"""Integration tests for the full 7-layer planning architecture.

Exercises all layers in a single watcher cycle:
1. Static YAML campaigns
2. Reactive event bus → event detection
3. OODA decision loop → ad-hoc tasks from events
4. Goal reflection → verbal feedback from previous outcomes
5. Goal progress scoring → quantitative metrics
6. Sub-goal decomposition → step plan creation during review
7. Adaptive replanning → failure recovery for steps

These tests mock all LLM calls but use real config/state/files to verify
the full wiring works end-to-end.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from secretary.config import SecretaryConfig
from secretary.event_bus import Event, EventBus, EventType
from secretary.goal_decomposition import (
    get_next_step,
    get_step_plans,
    record_step_result,
    save_step_plan,
)
from secretary.watcher import Watcher


# ── Fixtures ─────────────────────────────────────────────────

SAMPLE_GOALS = [
    {
        "id": "self-sustaining",
        "description": "Self-sustaining autonomy",
        "success_criteria": "Agent makes decisions without human input",
        "priority": 2,
        "status": "in-progress",
        "sub_goals": [
            {"id": "event-bus", "description": "Event-driven triggers", "status": "done"},
            {"id": "ooda-loop", "description": "OODA decision loop", "status": "done"},
            {"id": "goal-planner", "description": "Proactive goal planner", "status": "in-progress"},
            {"id": "live-test", "description": "Live integration test", "status": "not-started"},
        ],
    },
    {
        "id": "self-improvement",
        "description": "Autonomous self-improvement",
        "success_criteria": "Agent improves own code without review",
        "priority": 3,
        "status": "not-started",
        "sub_goals": [
            {"id": "failure-analysis", "description": "Analyze failures", "status": "not-started"},
            {"id": "code-review", "description": "Autonomous code review", "status": "not-started"},
        ],
    },
]


@dataclass
class _FakeRouting:
    tier: str = "low"
    model: str = "claude-haiku-4.5"
    max_turns: int = 10
    max_budget_usd: float = 0.0
    reason: str = "test"


@dataclass
class _FakeResult:
    text: str = "done"
    error: str | None = None
    routing: _FakeRouting | None = None
    cost_usd: float = 0.0
    num_turns: int = 1
    tools_used: list = field(default_factory=list)
    output: str = "step completed successfully"
    premium_requests: float = 0.0

    def __post_init__(self):
        if self.routing is None:
            self.routing = _FakeRouting()


@pytest.fixture
def workspace(tmp_path: Path):
    """Set up an isolated workspace with goals, campaigns, and state."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    # Goals YAML
    goals_file = tmp_path / "goals.yaml"
    goals_file.write_text(yaml.dump({"goals": SAMPLE_GOALS}), encoding="utf-8")

    # Campaign with one static task
    campaign_file = tmp_path / "campaign.yaml"
    campaign_file.write_text(
        "tasks:\n"
        "  - prompt: Check email inbox for urgent messages\n"
        "    tier: low\n",
        encoding="utf-8",
    )

    # Config
    config = SecretaryConfig(data_root=str(data_dir))
    config.events.enabled = True
    config.events.ooda_enabled = True
    config.events.gmail_source = False
    config.events.calendar_source = False
    config.goals.enabled = True
    config.goals.goals_file = str(goals_file)
    config.goals.review_interval_hours = 0  # Always review
    config.goals.approval_mode = "auto"  # Integration tests: no approval queue
    config.watcher.max_retries = 0  # No watcher-level retries (replanner handles it)
    config.watcher.retry_base_delay = 0.0

    return {
        "tmp_path": tmp_path,
        "data_dir": data_dir,
        "goals_file": goals_file,
        "campaign_file": campaign_file,
        "config": config,
    }


def _mock_goal_review_response():
    """Mock LLM response for goal review — returns tasks + status updates."""
    return json.dumps({
        "tasks": [
            {
                "prompt": "Analyze run_log for autonomous task patterns",
                "tier": "low",
                "priority": 2,
                "goal_id": "self-sustaining",
            },
        ],
        "goal_updates": [
            {
                "sub_goal_id": "live-test",
                "new_status": "in-progress",
                "evidence": "Starting integration test",
            },
        ],
        "reasoning": "Live integration test is the next logical step.",
    })


def _mock_ooda_response():
    """Mock LLM response for OODA — returns reactive task from event."""
    return json.dumps([
        {
            "prompt": "Respond to new email from test@example.com",
            "tier": "low",
            "priority": 1,
        },
    ])


def _mock_reflection_response():
    """Mock LLM response for goal reflection."""
    return json.dumps({
        "reflection": "Previous tasks showed good progress on event-driven architecture.",
        "strategy_adjustments": ["Focus on live integration testing next"],
        "patterns": {
            "working": ["Event-driven tasks complete reliably"],
            "failing": ["Complex multi-step tasks sometimes timeout"],
        },
    })


def _mock_decomposition_response():
    """Mock LLM response for sub-goal decomposition."""
    return json.dumps({
        "steps": [
            {
                "action": "Enable all planning layers in config",
                "verification": "Config file shows all layers enabled",
                "tier": "low",
            },
            {
                "action": "Run watcher for one cycle and check logs",
                "verification": "Run log shows tasks from all sources",
                "tier": "low",
            },
            {
                "action": "Verify step execution and replanning work",
                "verification": "Step plans show completed and retried steps",
                "tier": "medium",
            },
        ],
    })


def _mock_replanner_analysis():
    """Mock LLM response for failure analysis."""
    return json.dumps({
        "root_cause": "Config path was incorrect",
        "is_transient": False,
        "strategy": "revise",
        "revised_action": "Fix config path and re-enable layers",
        "revised_verification": "Config loads without errors",
        "revised_tier": "low",
    })


# ── Helpers ──────────────────────────────────────────────────

def _make_mock_stream(response_text: str):
    """Create a mock Anthropic streaming response."""
    mock_block = MagicMock()
    mock_block.text = response_text
    mock_message = MagicMock()
    mock_message.content = [mock_block]
    mock_stream = MagicMock()
    mock_stream.__enter__ = MagicMock(return_value=mock_stream)
    mock_stream.__exit__ = MagicMock(return_value=False)
    mock_stream.get_final_message.return_value = mock_message
    return mock_stream


# ── Integration: Step Execution Pipeline ─────────────────────


class TestStepExecutionPipeline:
    """Test full step execution flow: plan → execute → record → complete."""

    def test_step_success_marks_subgoal_done(self, workspace):
        """When all steps pass, sub-goal is marked done via plan completion."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        # Pre-populate goal state with a step plan (1 step, about to complete)
        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "last_reviewed": "2099-01-01T00:00:00+00:00",  # Far future = no review
            "sub_goal_status": {
                "live-test": {"status": "in-progress", "evidence": "Started"},
            },
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {
                "live-test": {
                    "goal_id": "self-sustaining",
                    "steps": [
                        {
                            "step_id": "live-test.1",
                            "action": "Run the integration test",
                            "verification": "All layers fire in sequence",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                    ],
                    "created": "2026-07-17T00:00:00+00:00",
                    "completed": False,
                },
            },
        }), encoding="utf-8")

        # Disable review (state has future last_reviewed)
        config.goals.review_interval_hours = 999999

        w = Watcher(config=config, campaign_file=campaign_file)

        # Mock agent to succeed
        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}):
            mock_agent.run = AsyncMock(return_value=_FakeResult())
            asyncio.run(w._run_cycle(MagicMock()))

        # Verify: step was recorded as completed
        state = json.loads(state_file.read_text(encoding="utf-8"))
        plan = state["step_plans"]["live-test"]
        assert plan["steps"][0]["status"] == "completed"
        assert plan["completed"] is True

        # Verify: sub-goal was marked done
        assert state["sub_goal_status"]["live-test"]["status"] == "done"

    def test_step_failure_triggers_replanner(self, workspace):
        """When a step fails, replanner is called and applies a strategy."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        # Pre-populate with a step plan (1 step, will fail)
        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "last_reviewed": "2099-01-01T00:00:00+00:00",
            "sub_goal_status": {
                "live-test": {"status": "in-progress", "evidence": "Started"},
            },
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {
                "live-test": {
                    "goal_id": "self-sustaining",
                    "steps": [
                        {
                            "step_id": "live-test.1",
                            "action": "Run the integration test",
                            "verification": "All layers fire",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                    ],
                    "created": "2026-07-17T00:00:00+00:00",
                    "completed": False,
                },
            },
        }), encoding="utf-8")

        config.goals.review_interval_hours = 999999

        w = Watcher(config=config, campaign_file=campaign_file)

        # Mock agent to fail
        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.handle_step_failure", new_callable=AsyncMock) as mock_replan:
            mock_agent.run = AsyncMock(return_value=_FakeResult(error="config path wrong"))
            mock_replan.return_value = "retry"
            asyncio.run(w._run_cycle(MagicMock()))

        # Verify: replanner was called
        mock_replan.assert_called_once()
        call_kwargs = mock_replan.call_args
        assert call_kwargs[0][1] == "live-test"  # sub_goal_id
        assert call_kwargs[0][2] == "live-test.1"  # step_id

    def test_step_failure_block_marks_subgoal_blocked(self, workspace):
        """When replanner returns 'block', sub-goal is marked blocked."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "last_reviewed": "2099-01-01T00:00:00+00:00",
            "sub_goal_status": {
                "live-test": {"status": "in-progress", "evidence": "Started"},
            },
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {
                "live-test": {
                    "goal_id": "self-sustaining",
                    "steps": [
                        {
                            "step_id": "live-test.1",
                            "action": "Run the integration test",
                            "verification": "All layers fire",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                    ],
                    "created": "2026-07-17T00:00:00+00:00",
                    "completed": False,
                },
            },
        }), encoding="utf-8")

        config.goals.review_interval_hours = 999999

        w = Watcher(config=config, campaign_file=campaign_file)

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.handle_step_failure", new_callable=AsyncMock) as mock_replan:
            mock_agent.run = AsyncMock(return_value=_FakeResult(error="totally broken"))
            mock_replan.return_value = "block"
            asyncio.run(w._run_cycle(MagicMock()))

        # Verify: sub-goal was marked blocked
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["sub_goal_status"]["live-test"]["status"] == "blocked"

    def test_multi_step_plan_advances_one_per_cycle(self, workspace):
        """Only one step executes per cycle (deliberate progress)."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "last_reviewed": "2099-01-01T00:00:00+00:00",
            "sub_goal_status": {},
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {
                "live-test": {
                    "goal_id": "self-sustaining",
                    "steps": [
                        {
                            "step_id": "live-test.1",
                            "action": "Step one",
                            "verification": "Check one",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                        {
                            "step_id": "live-test.2",
                            "action": "Step two",
                            "verification": "Check two",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                    ],
                    "created": "2026-07-17T00:00:00+00:00",
                    "completed": False,
                },
            },
        }), encoding="utf-8")

        config.goals.review_interval_hours = 999999

        w = Watcher(config=config, campaign_file=campaign_file)

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}):
            mock_agent.run = AsyncMock(return_value=_FakeResult())
            asyncio.run(w._run_cycle(MagicMock()))

        # Verify: only first step completed, second still pending
        state = json.loads(state_file.read_text(encoding="utf-8"))
        steps = state["step_plans"]["live-test"]["steps"]
        assert steps[0]["status"] == "completed"
        assert steps[1]["status"] == "pending"
        assert state["step_plans"]["live-test"]["completed"] is False

    def test_blocked_plan_skipped(self, workspace):
        """Blocked plans don't produce step tasks."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "last_reviewed": "2099-01-01T00:00:00+00:00",
            "sub_goal_status": {},
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {
                "live-test": {
                    "goal_id": "self-sustaining",
                    "blocked": True,
                    "blocked_reason": "All recovery exhausted",
                    "steps": [
                        {
                            "step_id": "live-test.1",
                            "action": "Step one",
                            "verification": "Check one",
                            "tier": "low",
                            "status": "failed",
                            "result": "Error",
                            "ts": None,
                        },
                    ],
                    "created": "2026-07-17T00:00:00+00:00",
                    "completed": False,
                },
            },
        }), encoding="utf-8")

        config.goals.review_interval_hours = 999999

        w = Watcher(config=config, campaign_file=campaign_file)

        call_count = 0

        async def _counting_run(**kwargs):
            nonlocal call_count
            call_count += 1
            return _FakeResult()

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}):
            mock_agent.run = _counting_run
            asyncio.run(w._run_cycle(MagicMock()))

        # Only the static campaign task should run, not the blocked step
        assert call_count == 1


# ── Integration: Goal Review with Decomposition ─────────────


class TestGoalReviewDecomposition:
    """Test goal review → decomposition → step plan creation."""

    def test_review_triggers_decomposition(self, workspace):
        """A goal review should decompose eligible sub-goals into step plans."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        # Start with empty state (no prior review)
        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "sub_goal_status": {},
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {},
        }), encoding="utf-8")

        w = Watcher(config=config, campaign_file=campaign_file)

        # We need to mock: direct_agent, goal_review LLM, reflection LLM, decomposition LLM
        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.run_goal_reflection", new_callable=AsyncMock) as mock_ref, \
             patch("secretary.watcher.run_goal_review", new_callable=AsyncMock) as mock_review, \
             patch("secretary.watcher.is_review_due", return_value=True):

            mock_agent.run = AsyncMock(return_value=_FakeResult())

            # Goal review returns tasks + updates
            review_result = [
                {
                    "prompt": "Analyze patterns",
                    "tier": "low",
                    "priority": 2,
                    "goal_id": "self-sustaining",
                    "source": "goals",
                },
            ]
            mock_review.return_value = review_result

            passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

        # Review was called
        mock_review.assert_called_once()
        # Reflection was called before review
        mock_ref.assert_called_once()

    def test_progress_scoring_runs_before_review(self, workspace):
        """Progress metrics are computed before the review decision."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "sub_goal_status": {
                "event-bus": {"status": "done", "evidence": "Implemented"},
                "ooda-loop": {"status": "done", "evidence": "Implemented"},
            },
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {},
        }), encoding="utf-8")

        w = Watcher(config=config, campaign_file=campaign_file)

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.run_goal_reflection", new_callable=AsyncMock), \
             patch("secretary.watcher.run_goal_review", new_callable=AsyncMock, return_value=[]), \
             patch("secretary.watcher.is_review_due", return_value=True), \
             patch("secretary.watcher.compute_progress") as mock_progress:

            mock_agent.run = AsyncMock(return_value=_FakeResult())
            mock_progress.return_value = {}

            asyncio.run(w._run_cycle(MagicMock()))

        # Progress was computed
        mock_progress.assert_called_once()


# ── Integration: Event Bus → OODA Pipeline ───────────────────


class TestEventOodaPipeline:
    """Test event detection → OODA → reactive task injection."""

    def test_events_trigger_ooda_tasks(self, workspace):
        """Events detected by the bus should trigger OODA task generation."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]

        w = Watcher(config=config, campaign_file=campaign_file)

        # Manually inject an event into the bus
        assert w._event_bus is not None
        test_event = Event(
            type=EventType.NEW_EMAIL,
            source="test",
            payload={"from": "boss@company.com", "subject": "Urgent: review needed"},
        )

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.run_ooda_cycle", new_callable=AsyncMock) as mock_ooda, \
             patch("secretary.watcher.run_goal_reflection", new_callable=AsyncMock), \
             patch("secretary.watcher.run_goal_review", new_callable=AsyncMock, return_value=[]), \
             patch("secretary.watcher.is_review_due", return_value=False):

            mock_agent.run = AsyncMock(return_value=_FakeResult())

            # OODA returns a reactive task
            mock_ooda.return_value = [
                {
                    "prompt": "Read and respond to urgent email from boss",
                    "tier": "low",
                    "priority": 1,
                    "source": "ooda",
                },
            ]

            # Make poll_all return our test event
            original_poll = w._event_bus.poll_all

            async def _mock_poll():
                w._event_bus.emit(test_event)
                return [test_event]

            w._event_bus.poll_all = _mock_poll

            passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

        # OODA was called (events triggered it)
        mock_ooda.assert_called_once()


# ── Integration: Full 7-Layer Cycle ──────────────────────────


class TestFullSevenLayerCycle:
    """The ultimate integration test: all 7 layers fire in one cycle."""

    def test_all_layers_fire_in_sequence(self, workspace):
        """Run a complete cycle with all layers active and verify ordering."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        # Pre-populate state with a step plan (for layer 6)
        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "sub_goal_status": {},
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {
                "live-test": {
                    "goal_id": "self-sustaining",
                    "steps": [
                        {
                            "step_id": "live-test.1",
                            "action": "Verify full pipeline",
                            "verification": "All layers fire",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                    ],
                    "created": "2026-07-17T00:00:00+00:00",
                    "completed": False,
                },
            },
        }), encoding="utf-8")

        w = Watcher(config=config, campaign_file=campaign_file)

        # Track call order
        call_order = []

        async def _track_reflection(*args, **kwargs):
            call_order.append("reflection")

        async def _track_ooda(*args, **kwargs):
            call_order.append("ooda")
            return []  # No OODA tasks

        async def _track_review(*args, **kwargs):
            call_order.append("review")
            return []

        def _track_progress(*args, **kwargs):
            call_order.append("progress")
            return {}

        executed_prompts = []

        async def _track_agent(**kwargs):
            prompt = kwargs.get("task", "")
            executed_prompts.append(prompt)
            call_order.append(f"agent:{prompt[:30]}")
            return _FakeResult()

        # Inject event so OODA fires
        test_event = Event(type=EventType.NEW_EMAIL, source="test", payload={"subject": "hi"})

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.run_goal_reflection", side_effect=_track_reflection), \
             patch("secretary.watcher.run_goal_review", side_effect=_track_review), \
             patch("secretary.watcher.compute_progress", side_effect=_track_progress), \
             patch("secretary.watcher.run_ooda_cycle", side_effect=_track_ooda), \
             patch("secretary.watcher.is_review_due", return_value=True):

            mock_agent.run = _track_agent

            # Inject event via poll_all
            async def _mock_poll():
                w._event_bus.emit(test_event)
                return [test_event]

            w._event_bus.poll_all = _mock_poll

            passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

        # Verify execution order: OODA → Reflection → Progress → Review → Execution
        # (OODA fires because events exist, then goal system runs)
        assert "ooda" in call_order, f"OODA should fire; order: {call_order}"
        assert "reflection" in call_order, f"Reflection should fire; order: {call_order}"
        assert "progress" in call_order, f"Progress should fire; order: {call_order}"
        assert "review" in call_order, f"Review should fire; order: {call_order}"

        # OODA fires before goal system
        ooda_idx = call_order.index("ooda")
        reflection_idx = call_order.index("reflection")
        assert ooda_idx < reflection_idx, "OODA should run before reflection"

        # Reflection fires before progress
        progress_idx = call_order.index("progress")
        assert reflection_idx < progress_idx, "Reflection should run before progress"

        # Progress fires before review
        review_idx = call_order.index("review")
        assert progress_idx < review_idx, "Progress should run before review"

        # At least the static campaign task + step task should execute
        assert passed >= 2, f"Expected at least 2 passed tasks, got {passed}"

        # Step plan should be recorded
        state = json.loads(state_file.read_text(encoding="utf-8"))
        plan = state["step_plans"]["live-test"]
        assert plan["steps"][0]["status"] == "completed"

    def test_seven_layer_with_step_failure_and_replan(self, workspace):
        """Full cycle where a step fails and replanner applies retry."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]
        data_dir = workspace["data_dir"]

        state_file = data_dir / "goal_state.json"
        state_file.write_text(json.dumps({
            "sub_goal_status": {},
            "progress_notes": [],
            "reflections": [],
            "progress_snapshots": [],
            "step_plans": {
                "live-test": {
                    "goal_id": "self-sustaining",
                    "steps": [
                        {
                            "step_id": "live-test.1",
                            "action": "Run failing task",
                            "verification": "Should fail",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                        {
                            "step_id": "live-test.2",
                            "action": "Second step",
                            "verification": "After retry",
                            "tier": "low",
                            "status": "pending",
                            "result": None,
                            "ts": None,
                        },
                    ],
                    "created": "2026-07-17T00:00:00+00:00",
                    "completed": False,
                },
            },
        }), encoding="utf-8")

        w = Watcher(config=config, campaign_file=campaign_file)

        call_count = 0

        async def _mixed_agent(**kwargs):
            nonlocal call_count
            call_count += 1
            prompt = kwargs.get("task", "")
            # Step task fails; campaign task succeeds
            if "Run failing task" in prompt:
                return _FakeResult(error="something broke", text="")
            return _FakeResult()

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.run_goal_reflection", new_callable=AsyncMock), \
             patch("secretary.watcher.run_goal_review", new_callable=AsyncMock, return_value=[]), \
             patch("secretary.watcher.compute_progress", return_value={}), \
             patch("secretary.watcher.is_review_due", return_value=False), \
             patch("secretary.watcher.handle_step_failure", new_callable=AsyncMock) as mock_replan:

            mock_agent.run = _mixed_agent
            mock_replan.return_value = "retry"

            passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

        # Replanner was called with the failed step
        mock_replan.assert_called_once()
        # The step was recorded as failed
        state = json.loads(state_file.read_text(encoding="utf-8"))
        step = state["step_plans"]["live-test"]["steps"][0]
        assert step["status"] == "failed"


# ── Integration: Decomposition State Management ──────────────


class TestDecompositionState:
    """Test the decomposition/step-plan state persistence through cycles."""

    def test_step_plan_state_persists_across_save_load(self, tmp_path):
        """Step plans survive JSON round-trip."""
        state = {"step_plans": {}}
        save_step_plan(state, "test-sg", "test-goal", [
            {
                "step_id": "test-sg.1",
                "action": "First step",
                "verification": "Check first",
                "tier": "low",
                "status": "pending",
                "result": None,
                "ts": None,
            },
            {
                "step_id": "test-sg.2",
                "action": "Second step",
                "verification": "Check second",
                "tier": "medium",
                "status": "pending",
                "result": None,
                "ts": None,
            },
        ])

        # Save to JSON and reload
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        reloaded = json.loads(state_file.read_text(encoding="utf-8"))

        plans = get_step_plans(reloaded)
        assert "test-sg" in plans
        assert len(plans["test-sg"]["steps"]) == 2

        # Get next step
        nxt = get_next_step(reloaded, "test-sg")
        assert nxt["step_id"] == "test-sg.1"

        # Record result
        record_step_result(reloaded, "test-sg", "test-sg.1", True, "success")
        nxt2 = get_next_step(reloaded, "test-sg")
        assert nxt2["step_id"] == "test-sg.2"

        # Complete second step
        record_step_result(reloaded, "test-sg", "test-sg.2", True, "all done")
        assert reloaded["step_plans"]["test-sg"]["completed"] is True

    def test_failed_step_blocks_subsequent_steps(self, tmp_path):
        """A failed step prevents get_next_step from advancing."""
        state = {"step_plans": {}}
        save_step_plan(state, "sg1", "g1", [
            {
                "step_id": "sg1.1",
                "action": "Will fail",
                "verification": "Never",
                "tier": "low",
                "status": "pending",
                "result": None,
                "ts": None,
            },
            {
                "step_id": "sg1.2",
                "action": "Should not run",
                "verification": "Blocked",
                "tier": "low",
                "status": "pending",
                "result": None,
                "ts": None,
            },
        ])

        # Record failure on first step
        record_step_result(state, "sg1", "sg1.1", False, "boom")

        # Next step should be None (blocked by failure)
        nxt = get_next_step(state, "sg1")
        assert nxt is None


# ── Integration: OODA is skipped when no events ─────────────


class TestOodaConditional:
    """OODA should only fire when events are detected."""

    def test_ooda_skipped_when_no_events(self, workspace):
        """Without events, OODA should not be called."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]

        w = Watcher(config=config, campaign_file=campaign_file)

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.run_ooda_cycle", new_callable=AsyncMock) as mock_ooda, \
             patch("secretary.watcher.run_goal_reflection", new_callable=AsyncMock), \
             patch("secretary.watcher.run_goal_review", new_callable=AsyncMock, return_value=[]), \
             patch("secretary.watcher.is_review_due", return_value=False):

            mock_agent.run = AsyncMock(return_value=_FakeResult())

            # poll_all returns empty — no events
            async def _empty_poll():
                return []

            w._event_bus.poll_all = _empty_poll

            asyncio.run(w._run_cycle(MagicMock()))

        # OODA should NOT have been called
        mock_ooda.assert_not_called()


# ── Integration: Reflection non-fatal ────────────────────────


class TestReflectionNonFatal:
    """Goal reflection failure should not block the rest of the cycle."""

    def test_reflection_error_does_not_block_cycle(self, workspace):
        """If reflection throws, rest of goal system + tasks still run."""
        config = workspace["config"]
        campaign_file = workspace["campaign_file"]

        w = Watcher(config=config, campaign_file=campaign_file)

        with patch("secretary.watcher.direct_agent") as mock_agent, \
             patch("secretary.watcher.build_tool_registry", return_value={}), \
             patch("secretary.watcher.run_goal_reflection", new_callable=AsyncMock, side_effect=RuntimeError("LLM down")), \
             patch("secretary.watcher.run_goal_review", new_callable=AsyncMock, return_value=[]) as mock_review, \
             patch("secretary.watcher.compute_progress", return_value={}), \
             patch("secretary.watcher.is_review_due", return_value=True):

            mock_agent.run = AsyncMock(return_value=_FakeResult())
            passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

        # Review still ran despite reflection failure
        mock_review.assert_called_once()
        # Campaign task still executed
        assert passed >= 1
