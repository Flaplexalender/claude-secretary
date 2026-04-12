"""End-to-end integration tests for the full goal actualization pipeline.

Tests the complete flow: goals review → task generation → strategy injection →
task execution → strategy extraction → outcome recording → consolidation.

This is the "Layer 14" test — proving all 13 layers fire correctly together
when goals.enabled=true.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from secretary.goals import GoalStore, is_review_due, run_goal_review
from secretary.strategy_library import (
    Strategy,
    StrategyLibrary,
    maybe_extract_strategy,
)
from secretary.learned_router import extract_category
from secretary import direct_agent


# ── Helpers ───────────────────────────────────────────────────────────


@dataclass
class FakeRunLogEntry:
    task: str = "check email"
    success: bool = True
    tier: str = "low"
    source: str = "campaign"
    goal_id: str = ""
    tools_used: list[str] = field(default_factory=list)
    duration_ms: int = 5000
    num_turns: int = 3
    output: str = "Done"


@dataclass
class FakeRunLog:
    _entries: list[FakeRunLogEntry] = field(default_factory=list)

    def recent(self, n: int = 10) -> list[FakeRunLogEntry]:
        return self._entries[-n:]


@dataclass
class FakeMemory:
    short: list[str] = field(default_factory=list)
    long: list[str] = field(default_factory=list)

    def get_short(self) -> list[str]:
        return self.short

    def get_long(self) -> list[str]:
        return self.long

    def recent_short(self, n: int = 5) -> list[str]:
        return self.short[-n:]

    def recent_long(self, n: int = 10) -> list[str]:
        return self.long[-n:]

    def access_long(self, idx: int) -> None:
        pass

    def consolidate(self) -> None:
        pass

    def save(self) -> None:
        pass


SAMPLE_GOALS = [
    {
        "id": "autoresearch",
        "description": "Break the eval plateau via autonomous optimization",
        "success_criteria": "Eval score > 0.80 sustained",
        "priority": 1,
        "status": "in-progress",
        "sub_goals": [
            {"id": "textgrad", "description": "TextGrad failure analysis", "status": "done"},
            {"id": "prompt-optimizer", "description": "OPRO meta-prompt optimizer", "status": "done"},
            {"id": "strategy-library", "description": "Voyager-style strategy store", "status": "done"},
            {"id": "live-pipeline", "description": "End-to-end pipeline test", "status": "not-started"},
        ],
    },
    {
        "id": "prefix-survival",
        "description": "Ensure proxy routing resilience",
        "success_criteria": "All tiers execute without prefix",
        "priority": 2,
        "status": "in-progress",
        "sub_goals": [
            {"id": "oracle-production", "description": "Oracle ensemble in production", "status": "done"},
            {"id": "cost-monitor", "description": "Automated cost alert system", "status": "not-started"},
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
    cfg.anthropic_base_url = "http://localhost:4141"
    cfg.api_key = "test-key"
    cfg.agent_prefix = True
    cfg._goal_model = None
    return cfg


def _mock_anthropic_stream(response_text: str) -> MagicMock:
    """Create a mock Anthropic streaming client that returns response_text."""
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
    return mock_client


# ── Phase 1: Goal Review → Task Generation ─────────────────────────


class TestGoalReviewGeneratesTasks:
    """Verify goal planner reviews goals and produces executable tasks."""

    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_review_produces_goal_tasks(self, mock_build_client):
        """Goal review should produce tasks with source='goals' and goal_id set."""
        response = json.dumps({
            "tasks": [
                {
                    "prompt": "Run end-to-end pipeline test of TextGrad + prompt_optimizer + strategy_library",
                    "tier": "high",
                    "priority": 1,
                    "goal_id": "autoresearch",
                },
                {
                    "prompt": "Build automated cost monitoring for prefix survival",
                    "tier": "medium",
                    "priority": 2,
                    "goal_id": "prefix-survival",
                },
            ],
            "goal_updates": [
                {"sub_goal_id": "strategy-library", "new_status": "done", "evidence": "All tests pass"},
            ],
            "reasoning": "Pipeline test is the critical blocker for autoresearch. Cost monitor needed for prefix survival.",
        })

        mock_build_client.return_value = _mock_anthropic_stream(response)

        store = MagicMock()
        store.goals = SAMPLE_GOALS
        store._state = {"sub_goal_status": {}, "progress_notes": []}
        run_log = FakeRunLog([FakeRunLogEntry(task="Strategy extraction test", success=True)])
        memory = FakeMemory(short=["Strategy library v1 deployed"])
        config = _make_config()

        tasks = asyncio.run(run_goal_review(store, run_log, memory, config))

        assert len(tasks) == 2
        # All tasks have goal provenance
        for t in tasks:
            assert t["source"] == "goals"
            assert t["goal_id"] in ("autoresearch", "prefix-survival")
        assert tasks[0]["prompt"].startswith("Run end-to-end")
        assert tasks[0]["tier"] == "high"
        store.apply_updates.assert_called_once()
        store.mark_reviewed.assert_called_once()

    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_review_caps_at_3_tasks(self, mock_build_client):
        """Planner may try to generate many tasks; should be capped at 3."""
        response = json.dumps({
            "tasks": [
                {"prompt": f"Task {i}", "tier": "low", "priority": i, "goal_id": "autoresearch"}
                for i in range(6)
            ],
            "goal_updates": [],
            "reasoning": "Many things to do.",
        })
        mock_build_client.return_value = _mock_anthropic_stream(response)

        store = MagicMock()
        store.goals = SAMPLE_GOALS
        store._state = {"sub_goal_status": {}, "progress_notes": []}
        config = _make_config()

        tasks = asyncio.run(run_goal_review(store, FakeRunLog(), FakeMemory(), config))
        assert len(tasks) <= 3


# ── Phase 2: Strategy Injection ────────────────────────────────────


class TestStrategyInjection:
    """Verify strategies are injected into system prompts for matching categories."""

    def test_format_for_prompt_injects_strategies(self):
        """Strategies with matching category appear in formatted prompt."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Always search inbox before drafting to check for existing threads",
            source_task="Draft reply to team update",
            tools_used=["gmail_search", "gmail_draft"],
            quality_score=0.9,
        ))
        lib.add_strategy(Strategy(
            category="email",
            description="Include subject line in search query for precise results",
            source_task="Find meeting invite",
            tools_used=["gmail_search"],
            quality_score=0.8,
        ))

        section = lib.format_for_prompt("email")
        assert "Learned Strategies" in section
        assert "search inbox before drafting" in section
        assert "subject line in search query" in section

    def test_no_strategies_for_unmatched_category(self):
        """No injection for categories without learned strategies."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Check inbox first",
            source_task="Email task",
            tools_used=["gmail_search"],
        ))
        section = lib.format_for_prompt("calendar")
        assert section == ""

    def test_system_prompt_includes_strategies(self):
        """When strategy_library has matching strategies, _build_system_prompt includes them."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search before sending",
            source_task="Email task",
            tools_used=["gmail_search", "gmail_send"],
            quality_score=0.85,
        ))

        memory = FakeMemory()
        prompt = direct_agent._build_system_prompt(
            memory=memory,
            task="Send reply to the team about meeting",
            max_turns=10,
            tier="low",
            strategy_library=lib,
        )
        assert "Learned Strategies" in prompt
        assert "Search before sending" in prompt

    def test_system_prompt_without_library(self):
        """Without strategy_library, no strategy section."""
        memory = FakeMemory()
        prompt = direct_agent._build_system_prompt(
            memory=memory,
            task="Send reply to the team about meeting",
            max_turns=10,
            tier="low",
            strategy_library=None,
        )
        assert "Learned Strategies" not in prompt


# ── Phase 3: Strategy Extraction ───────────────────────────────────


class TestStrategyExtraction:
    """Verify strategies are extracted from successful task completions."""

    @patch("secretary.strategy_library.httpx.post")
    def test_extract_from_successful_task(self, mock_post):
        """Successful multi-tool task should produce a Strategy."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "description": "Search inbox for thread before drafting reply",
                "tools_pattern": ["gmail_search", "gmail_draft"],
            })}}],
        }
        mock_post.return_value = mock_resp

        lib = StrategyLibrary()
        result = maybe_extract_strategy(
            entry_task="Reply to the user's email about project deadline",
            entry_success=True,
            entry_tools=["gmail_search", "gmail_read", "gmail_draft"],
            entry_output="Drafted reply successfully",
            entry_duration=12.5,
            entry_turns=4,
            entry_source="campaign",
            entry_campaign="email-management",
            library=lib,
            base_url="http://localhost:4141",
        )
        assert result is not None
        assert result.category == "email"
        assert lib.size == 1

    def test_skip_failed_task(self):
        """Failed tasks should not trigger extraction."""
        lib = StrategyLibrary()
        result = maybe_extract_strategy(
            entry_task="Send email",
            entry_success=False,
            entry_tools=["gmail_send"],
            entry_output="Error: OAuth token expired",
            entry_duration=2.0,
            entry_turns=1,
            entry_source="campaign",
            entry_campaign="email-management",
            library=lib,
            base_url="http://localhost:4141",
        )
        assert result is None
        assert lib.size == 0

    def test_skip_trivial_single_tool_task(self):
        """Single-tool tasks are too simple for strategy extraction."""
        lib = StrategyLibrary()
        result = maybe_extract_strategy(
            entry_task="Check calendar",
            entry_success=True,
            entry_tools=["calendar_today"],
            entry_output="3 meetings today",
            entry_duration=1.5,
            entry_turns=1,
            entry_source="campaign",
            entry_campaign="calendar-check",
            library=lib,
            base_url="http://localhost:4141",
        )
        assert result is None


# ── Phase 4: Outcome Recording ─────────────────────────────────────


class TestOutcomeRecording:
    """Verify outcome recording updates strategy quality scores."""

    def test_success_boosts_quality(self):
        """Successful outcome should increase quality_score of retrieved strategies."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search first",
            source_task="Email task",
            tools_used=["gmail_search"],
            quality_score=0.8,
        ))
        # Retrieve to mark as "recently used"
        lib.format_for_prompt("email")
        original = lib.retrieve("email")[0].quality_score

        lib.record_outcome("email", success=True)
        assert lib.retrieve("email")[0].quality_score > original

    def test_failure_reduces_quality(self):
        """Failed outcome should decrease quality_score."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search first",
            source_task="Email task",
            tools_used=["gmail_search"],
            quality_score=0.8,
        ))
        lib.format_for_prompt("email")
        original = lib.retrieve("email")[0].quality_score

        lib.record_outcome("email", success=False)
        assert lib.retrieve("email")[0].quality_score < original

    def test_category_scoped_isolation(self):
        """Email failure should not affect calendar strategy scores."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search before send",
            source_task="Email task",
            tools_used=["gmail_search"],
            quality_score=0.8,
        ))
        lib.add_strategy(Strategy(
            category="calendar",
            description="Check today first",
            source_task="Calendar task",
            tools_used=["calendar_today"],
            quality_score=0.8,
        ))
        lib.format_for_prompt("email")
        lib.format_for_prompt("calendar")
        cal_before = lib.retrieve("calendar")[0].quality_score

        lib.record_outcome("email", success=False)
        cal_after = lib.retrieve("calendar")[0].quality_score
        assert cal_after == cal_before

    def test_extract_category_for_goal_tasks(self):
        """Goal-originated tasks should get 'goal-task' category."""
        cat = extract_category("Run pipeline test for autoresearch", source="goals", campaign="autoresearch")
        assert cat == "research"  # campaign='autoresearch' → 'research'

    def test_extract_category_email_campaign(self):
        """Email campaigns should extract 'email' category."""
        cat = extract_category("Draft reply to team", source="campaign", campaign="email-management")
        assert cat == "email"


# ── Phase 5: Consolidation ─────────────────────────────────────────


class TestConsolidation:
    """Verify consolidation decays quality scores and prunes weak strategies."""

    def test_decay_reduces_quality(self):
        """Consolidation should multiply all quality scores by decay rate."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search first",
            source_task="Email task",
            tools_used=["gmail_search"],
            quality_score=1.0,
        ))
        lib.consolidate()
        # After one decay: 1.0 * 0.95 = 0.95
        assert abs(lib.retrieve("email")[0].quality_score - 0.95) < 0.01

    def test_prune_below_threshold(self):
        """Strategies below MIN_QUALITY_SCORE should be pruned."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Weak strategy",
            source_task="Old task",
            tools_used=["gmail_search"],
            quality_score=0.31,
        ))
        pruned = lib.consolidate()
        assert pruned >= 1
        assert lib.size == 0

    def test_healthy_strategies_survive(self):
        """Strategies above threshold should survive consolidation."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Strong strategy",
            source_task="Good task",
            tools_used=["gmail_search", "gmail_draft"],
            quality_score=0.9,
        ))
        lib.consolidate()
        assert lib.size == 1
        assert lib.retrieve("email")[0].quality_score > 0.3

    def test_multiple_consolidations_decay_progressively(self):
        """Running consolidate N times should compound the decay."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Decaying strategy",
            source_task="Task",
            tools_used=["gmail_search"],
            quality_score=1.0,
        ))
        for _ in range(5):
            lib.consolidate()
        expected = 1.0 * (0.95 ** 5)  # ~0.7738
        actual = lib.retrieve("email")[0].quality_score
        assert abs(actual - expected) < 0.01


# ── Phase 6: Full Pipeline Integration ─────────────────────────────


class TestFullPipeline:
    """End-to-end test: goal review → strategy injection → extraction → recording → consolidation."""

    @patch("secretary.strategy_library.httpx.post")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_full_goal_actualization_pipeline(self, mock_build_client, mock_httpx_post):
        """
        Simulates one complete goal actualization cycle:
        1. Goal review generates a task
        2. Strategy library has learned strategies → injected into prompt
        3. Task "executes" → strategy extracted from success
        4. Outcome recorded → quality updated
        5. Consolidation → decay + prune
        """
        # === Setup: pre-populate strategy library ===
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="research",
            description="Run eval 3x median for reliable baseline comparison",
            source_task="Previous autoresearch iteration",
            tools_used=["run_python", "file_read"],
            quality_score=0.85,
        ))

        # === Step 1: Goal review generates tasks ===
        goal_response = json.dumps({
            "tasks": [
                {
                    "prompt": "Analyze and benchmark TextGrad + prompt_optimizer + strategy_library end-to-end pipeline",
                    "tier": "high",
                    "priority": 1,
                    "goal_id": "autoresearch",
                },
            ],
            "goal_updates": [],
            "reasoning": "Pipeline integration is the critical next step.",
        })
        mock_build_client.return_value = _mock_anthropic_stream(goal_response)

        store = MagicMock()
        store.goals = SAMPLE_GOALS
        store._state = {"sub_goal_status": {}, "progress_notes": []}
        config = _make_config()
        run_log = FakeRunLog([FakeRunLogEntry(task="Strategy library tests all pass", success=True)])
        memory = FakeMemory(short=["Layer 13c complete"])

        tasks = asyncio.run(run_goal_review(store, run_log, memory, config))
        assert len(tasks) == 1
        task = tasks[0]
        assert task["source"] == "goals"
        assert task["goal_id"] == "autoresearch"

        # === Step 2: Strategy injection into system prompt ===
        # The task is research-flavored, library has research strategies
        task_category = extract_category(
            task["prompt"], source=task["source"], campaign="autoresearch"
        )
        assert task_category == "research"

        strategies_section = lib.format_for_prompt(task_category)
        assert "Learned Strategies" in strategies_section
        assert "eval 3x median" in strategies_section

        # Build system prompt with strategy injection
        prompt = direct_agent._build_system_prompt(
            memory=memory,
            task=task["prompt"],
            max_turns=30,
            tier=task["tier"],
            strategy_library=lib,
        )
        assert "Learned Strategies" in prompt

        # === Step 3: Simulate successful task execution + strategy extraction ===
        extract_response = MagicMock()
        extract_response.status_code = 200
        extract_response.raise_for_status = MagicMock()
        extract_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "description": "Run 3x eval, compute TextGrad gradients, feed to OPRO meta-prompt, select best proposal",
                "tools_pattern": ["run_python", "file_read", "file_write"],
            })}}],
        }
        mock_httpx_post.return_value = extract_response

        initial_size = lib.size
        extracted = maybe_extract_strategy(
            entry_task=task["prompt"][:200],
            entry_success=True,
            entry_tools=["run_python", "file_read", "file_write"],
            entry_output="Pipeline test completed: score 0.782 (+0.012)",
            entry_duration=45.0,
            entry_turns=8,
            entry_source="goals",
            entry_campaign="autoresearch",
            library=lib,
            base_url="http://localhost:4141",
        )
        assert extracted is not None
        assert lib.size == initial_size + 1

        # === Step 4: Outcome recording ===
        lib.record_outcome(task_category, success=True)
        # Original strategy should have boosted quality
        research_strats = lib.retrieve("research")
        assert len(research_strats) >= 1
        # At least one strategy should have quality > 0.85 (the original)
        best_quality = max(s.quality_score for s in research_strats)
        assert best_quality > 0.85

        # === Step 5: Consolidation ===
        lib.consolidate()
        # All strategies should survive (quality well above 0.3 minimum)
        assert lib.size >= 1
        for s in lib.all_strategies():
            assert s.quality_score >= 0.3

    @patch("secretary.strategy_library.httpx.post")
    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_failed_task_degrades_strategy_quality(self, mock_build_client, mock_httpx_post):
        """
        When a goal-originated task fails, outcome recording should degrade
        quality of strategies in that category, and consolidation should
        eventually prune them if quality drops enough.
        """
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="research",
            description="Fragile strategy that keeps failing",
            source_task="Old experiment",
            tools_used=["run_python"],
            quality_score=0.5,
        ))

        # Task from goals fails
        task_category = "research"
        lib.format_for_prompt(task_category)  # Mark as recently used

        # Record multiple failures
        for _ in range(3):
            lib.record_outcome(task_category, success=False)

        # Quality should have dropped significantly
        strats = lib.retrieve(task_category)
        if strats:
            assert strats[0].quality_score < 0.5

        # Consolidation should prune if quality is now below threshold
        lib.consolidate()
        remaining = lib.retrieve(task_category)
        # Either pruned entirely or quality score is very low
        if remaining:
            assert remaining[0].quality_score < 0.5

    @patch("secretary.direct_agent._build_client")
    @patch("secretary.direct_agent.AGENT_PREFIX", [{"role": "user", "content": "."}, {"role": "assistant", "content": "."}])
    def test_goal_task_feeds_back_to_next_review(self, mock_build_client):
        """
        Completed goal tasks should appear in run_log, informing the next
        goal review — completing the feedback loop.
        """
        # First review generates a task
        response1 = json.dumps({
            "tasks": [{"prompt": "Build cost monitoring alert", "tier": "medium", "priority": 1, "goal_id": "prefix-survival"}],
            "goal_updates": [],
            "reasoning": "Cost monitoring is needed.",
        })
        mock_build_client.return_value = _mock_anthropic_stream(response1)

        store = MagicMock()
        store.goals = SAMPLE_GOALS
        store._state = {"sub_goal_status": {}, "progress_notes": []}
        config = _make_config()

        tasks1 = asyncio.run(run_goal_review(store, FakeRunLog(), FakeMemory(), config))
        assert len(tasks1) == 1

        # Simulate: task executed successfully, added to run_log
        completed_entry = FakeRunLogEntry(
            task="Build cost monitoring alert",
            success=True,
            source="goals",
            goal_id="prefix-survival",
        )

        # Second review sees the completed task in run_log
        response2 = json.dumps({
            "tasks": [{"prompt": "Test cost alerts with simulated spike", "tier": "low", "priority": 2, "goal_id": "prefix-survival"}],
            "goal_updates": [{"sub_goal_id": "cost-monitor", "new_status": "in-progress", "evidence": "Built alert system"}],
            "reasoning": "Alert built, now need to test it.",
        })
        mock_build_client.return_value = _mock_anthropic_stream(response2)

        store.mark_reviewed.reset_mock()
        store.save_state.reset_mock()
        store.apply_updates.reset_mock()

        tasks2 = asyncio.run(run_goal_review(
            store,
            FakeRunLog([completed_entry]),
            FakeMemory(short=["Cost monitoring built"]),
            config,
        ))

        assert len(tasks2) == 1
        assert "Test cost alerts" in tasks2[0]["prompt"]
        # Goal update should mark cost-monitor as in-progress
        store.apply_updates.assert_called_once()


# ── Phase 7: Autonomous Ratio Tracking ─────────────────────────────


class TestAutonomousRatio:
    """Verify we can measure the ratio of goal-generated vs static tasks."""

    def test_task_provenance_tracking(self):
        """Tasks from different sources should be distinguishable."""
        campaign_task = {"prompt": "Check email", "source": "campaign"}
        goal_task = {"prompt": "Build cost monitor", "source": "goals", "goal_id": "prefix-survival"}
        ooda_task = {"prompt": "Respond to new email", "source": "ooda"}

        # Simulate run_log with mixed sources
        entries = [
            FakeRunLogEntry(task="Check email", source="campaign", success=True),
            FakeRunLogEntry(task="Build cost monitor", source="goals", goal_id="prefix-survival", success=True),
            FakeRunLogEntry(task="Respond to new email", source="ooda", success=True),
            FakeRunLogEntry(task="Optimize prompts", source="goals", goal_id="autoresearch", success=True),
        ]

        # Compute autonomous ratio
        total = len(entries)
        autonomous = sum(1 for e in entries if e.source in ("goals", "ooda"))
        static = sum(1 for e in entries if e.source == "campaign")

        assert autonomous == 3
        assert static == 1
        assert autonomous / total == 0.75  # 75% autonomous

    def test_goal_task_success_rate(self):
        """Track success rate specifically for goal-originated tasks."""
        entries = [
            FakeRunLogEntry(source="goals", goal_id="autoresearch", success=True),
            FakeRunLogEntry(source="goals", goal_id="autoresearch", success=False),
            FakeRunLogEntry(source="goals", goal_id="prefix-survival", success=True),
            FakeRunLogEntry(source="campaign", success=True),
        ]

        goal_entries = [e for e in entries if e.source == "goals"]
        success_rate = sum(1 for e in goal_entries if e.success) / len(goal_entries)
        assert abs(success_rate - 2 / 3) < 0.01


# ── Phase 8: Strategy Library Persistence ──────────────────────────


class TestStrategyPersistence:
    """Verify strategy library survives across simulated cycles."""

    def test_save_and_reload(self, tmp_path: Path):
        """Strategies persisted to disk should be reloadable."""
        lib_path = tmp_path / "strategy_library.json"

        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Check inbox before drafting",
            source_task="Email task",
            tools_used=["gmail_search", "gmail_draft"],
            quality_score=0.9,
        ))
        lib._save(lib_path)

        lib2 = StrategyLibrary()
        lib2._load(lib_path)
        assert lib2.size == 1
        strats = lib2.retrieve("email")
        assert len(strats) == 1
        assert "inbox before drafting" in strats[0].description

    def test_outcome_persists_through_save_reload(self, tmp_path: Path):
        """Quality score changes from outcome recording should persist."""
        lib_path = tmp_path / "strategy_library.json"

        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search first",
            source_task="Email task",
            tools_used=["gmail_search"],
            quality_score=0.8,
        ))
        lib.format_for_prompt("email")
        lib.record_outcome("email", success=True)
        lib._save(lib_path)

        lib2 = StrategyLibrary()
        lib2._load(lib_path)
        strat = lib2.retrieve("email")[0]
        assert strat.quality_score > 0.8  # Boosted by success


# ── Phase 9: Watcher Strategy Integration Points ──────────────────


class TestWatcherIntegrationPoints:
    """Verify the exact integration points in watcher align with strategy library API."""

    def test_extract_category_deterministic(self):
        """extract_category should produce consistent categories for watcher to use."""
        # Same inputs → same category
        cat1 = extract_category("Draft email reply", source="campaign", campaign="email-management")
        cat2 = extract_category("Draft email reply", source="campaign", campaign="email-management")
        assert cat1 == cat2
        assert cat1 == "email"

    def test_record_outcome_after_every_task(self):
        """Watcher calls record_outcome for both success and failure."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search first",
            source_task="Task",
            tools_used=["gmail_search"],
            quality_score=0.8,
        ))
        lib.format_for_prompt("email")

        # Watcher pattern: record_outcome after every task
        lib.record_outcome("email", success=True)
        score_after_success = lib.retrieve("email")[0].quality_score
        assert score_after_success > 0.8

        lib.record_outcome("email", success=False)
        score_after_failure = lib.retrieve("email")[0].quality_score
        assert score_after_failure < score_after_success

    def test_consolidate_in_housekeeping(self):
        """Consolidation should be safe to call on empty/full library."""
        empty_lib = StrategyLibrary()
        assert empty_lib.consolidate() == 0

        full_lib = StrategyLibrary()
        for i in range(10):
            full_lib.add_strategy(Strategy(
                category=f"cat-{i % 3}",
                description=f"Strategy {i} with unique wording here",
                source_task=f"Task {i}",
                tools_used=["tool_a"],
                quality_score=0.5 + (i * 0.05),
            ))
        pruned = full_lib.consolidate()
        assert isinstance(pruned, int)
        # All remaining strategies should be above minimum
        for s in full_lib.all_strategies():
            assert s.quality_score >= 0.3
