"""Tests for goal_self_improve.py — Autonomous Self-Improvement Engine."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.goal_self_improve import (
    ANALYSIS_COOLDOWN_HOURS,
    ANALYSIS_MODEL,
    MAX_FAILURE_ENTRIES,
    MAX_PENDING_PROPOSALS,
    MAX_PROPOSALS_PER_ANALYSIS,
    SELF_IMPROVE_GOAL_ID,
    ImprovementProposal,
    _build_analysis_prompt,
    _count_pending,
    _get_improve_state,
    _parse_json_response,
    _run_failure_analysis,
    discard_stale_proposals,
    get_pending_proposal,
    is_analysis_due,
    proposal_to_task,
    record_proposal_result,
    run_self_improve_analysis,
)
from secretary.run_log import RunLogEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_entry(
    success: bool = False,
    task: str = "Test task",
    error: str | None = "Some error",
    tier: str = "medium",
    model: str = "claude-sonnet-4.6",
    source: str = "campaign",
    goal_id: str = "",
    output_preview: str = "Preview output",
    tools_used: list[str] | None = None,
    duration_s: float = 5.0,
    num_turns: int = 3,
) -> RunLogEntry:
    return RunLogEntry(
        timestamp=datetime.now(timezone.utc).isoformat(),
        cycle=1,
        task=task,
        tier=tier,
        model=model,
        success=success,
        output_preview=output_preview,
        error=error if not success else None,
        duration_s=duration_s,
        tools_used=tools_used or [],
        source=source,
        goal_id=goal_id,
        num_turns=num_turns,
    )


def _make_state(proposals: list[dict] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Build a minimal goal_state dict with self_improve_state."""
    si = {
        "proposals": proposals or [],
        "last_analysis": kwargs.get("last_analysis"),
        "total_proposed": kwargs.get("total_proposed", 0),
        "total_executed": kwargs.get("total_executed", 0),
        "total_promoted": kwargs.get("total_promoted", 0),
        "total_discarded": kwargs.get("total_discarded", 0),
    }
    return {"self_improve_state": si}


def _make_proposal(
    proposal_id: str = "prop-test1",
    category: str = "failure-fix",
    description: str = "Fix test failure",
    status: str = "pending",
    priority: float = 0.8,
    task_prompt: str = "Fix the bug in module.py",
    created: str | None = None,
) -> dict[str, Any]:
    return {
        "proposal_id": proposal_id,
        "category": category,
        "description": description,
        "target_files": ["src/secretary/module.py"],
        "task_prompt": task_prompt,
        "priority": priority,
        "evidence": "Seen in 5 failures",
        "status": status,
        "result": None,
        "created": created or datetime.now(timezone.utc).isoformat(),
        "executed": None,
    }


def _make_config() -> MagicMock:
    config = MagicMock()
    config.anthropic_base_url = "http://localhost:4141"
    return config


# ---------------------------------------------------------------------------
# Tests: _get_improve_state
# ---------------------------------------------------------------------------

class TestGetImproveState:
    def test_creates_default(self):
        state: dict[str, Any] = {}
        imp = _get_improve_state(state)
        assert "self_improve_state" in state
        assert imp["proposals"] == []
        assert imp["last_analysis"] is None
        assert imp["total_proposed"] == 0

    def test_returns_existing(self):
        state = _make_state(total_proposed=5)
        imp = _get_improve_state(state)
        assert imp["total_proposed"] == 5

    def test_preserves_proposals(self):
        p = _make_proposal()
        state = _make_state(proposals=[p])
        imp = _get_improve_state(state)
        assert len(imp["proposals"]) == 1
        assert imp["proposals"][0]["proposal_id"] == "prop-test1"


# ---------------------------------------------------------------------------
# Tests: is_analysis_due
# ---------------------------------------------------------------------------

class TestIsAnalysisDue:
    def test_first_time(self):
        state: dict[str, Any] = {}
        assert is_analysis_due(state) is True

    def test_none_last_analysis(self):
        state = _make_state(last_analysis=None)
        assert is_analysis_due(state) is True

    def test_within_cooldown(self):
        recent = datetime.now(timezone.utc).isoformat()
        state = _make_state(last_analysis=recent)
        assert is_analysis_due(state) is False

    def test_after_cooldown(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        state = _make_state(last_analysis=old)
        assert is_analysis_due(state) is True

    def test_custom_cooldown(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state = _make_state(last_analysis=one_hour_ago)
        # 2-hour cooldown: not due yet
        assert is_analysis_due(state, cooldown_hours=2.0) is False
        # 0.5-hour cooldown: overdue
        assert is_analysis_due(state, cooldown_hours=0.5) is True

    def test_invalid_timestamp(self):
        state = _make_state(last_analysis="not-a-timestamp")
        assert is_analysis_due(state) is True


# ---------------------------------------------------------------------------
# Tests: _count_pending
# ---------------------------------------------------------------------------

class TestCountPending:
    def test_empty(self):
        state = _make_state()
        assert _count_pending(state) == 0

    def test_counts_pending_only(self):
        proposals = [
            _make_proposal(proposal_id="p1", status="pending"),
            _make_proposal(proposal_id="p2", status="executing"),
            _make_proposal(proposal_id="p3", status="pending"),
            _make_proposal(proposal_id="p4", status="completed"),
        ]
        state = _make_state(proposals=proposals)
        assert _count_pending(state) == 2


# ---------------------------------------------------------------------------
# Tests: get_pending_proposal
# ---------------------------------------------------------------------------

class TestGetPendingProposal:
    def test_no_proposals(self):
        state = _make_state()
        assert get_pending_proposal(state) is None

    def test_no_pending(self):
        proposals = [_make_proposal(status="completed")]
        state = _make_state(proposals=proposals)
        assert get_pending_proposal(state) is None

    def test_returns_highest_priority(self):
        proposals = [
            _make_proposal(proposal_id="low", priority=0.3, status="pending"),
            _make_proposal(proposal_id="high", priority=0.9, status="pending"),
            _make_proposal(proposal_id="mid", priority=0.5, status="pending"),
        ]
        state = _make_state(proposals=proposals)
        result = get_pending_proposal(state)
        assert result is not None
        assert result["proposal_id"] == "high"

    def test_skips_non_pending(self):
        proposals = [
            _make_proposal(proposal_id="done", priority=0.9, status="completed"),
            _make_proposal(proposal_id="low", priority=0.3, status="pending"),
        ]
        state = _make_state(proposals=proposals)
        result = get_pending_proposal(state)
        assert result is not None
        assert result["proposal_id"] == "low"


# ---------------------------------------------------------------------------
# Tests: record_proposal_result
# ---------------------------------------------------------------------------

class TestRecordProposalResult:
    def test_records_success(self):
        p = _make_proposal(proposal_id="p1", status="executing")
        state = _make_state(proposals=[p])
        record_proposal_result(
            state, "p1", success=True, promoted=True,
            changed_files=["src/a.py", "src/b.py"],
        )
        assert p["status"] == "completed"
        assert p["result"]["promoted"] is True
        assert p["result"]["changed_files"] == ["src/a.py", "src/b.py"]
        assert p["executed"] is not None
        assert state["self_improve_state"]["total_executed"] == 1
        assert state["self_improve_state"]["total_promoted"] == 1

    def test_records_failure(self):
        p = _make_proposal(proposal_id="p1", status="executing")
        state = _make_state(proposals=[p])
        record_proposal_result(
            state, "p1", success=False, promoted=False,
            error="Tests failed",
        )
        assert p["status"] == "failed"
        assert p["result"]["error"] == "Tests failed"
        assert state["self_improve_state"]["total_executed"] == 1
        assert state["self_improve_state"]["total_promoted"] == 0

    def test_no_match_is_noop(self):
        p = _make_proposal(proposal_id="p1", status="executing")
        state = _make_state(proposals=[p])
        record_proposal_result(state, "nonexistent", success=True, promoted=True)
        assert p["status"] == "executing"  # unchanged


# ---------------------------------------------------------------------------
# Tests: discard_stale_proposals
# ---------------------------------------------------------------------------

class TestDiscardStaleProposals:
    def test_discards_old(self):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        proposals = [
            _make_proposal(proposal_id="old", status="pending", created=old_time),
            _make_proposal(proposal_id="new", status="pending"),
        ]
        state = _make_state(proposals=proposals)
        count = discard_stale_proposals(state)
        assert count == 1
        assert proposals[0]["status"] == "discarded"
        assert proposals[1]["status"] == "pending"

    def test_skips_non_pending(self):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        proposals = [
            _make_proposal(proposal_id="done", status="completed", created=old_time),
        ]
        state = _make_state(proposals=proposals)
        count = discard_stale_proposals(state)
        assert count == 0

    def test_no_proposals(self):
        state = _make_state()
        count = discard_stale_proposals(state)
        assert count == 0

    def test_custom_max_age(self):
        two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        proposals = [
            _make_proposal(proposal_id="p1", status="pending", created=two_hours_ago),
        ]
        state = _make_state(proposals=proposals)
        # 1-hour max age: should discard
        assert discard_stale_proposals(state, max_age_hours=1.0) == 1
        assert proposals[0]["status"] == "discarded"


# ---------------------------------------------------------------------------
# Tests: _build_analysis_prompt
# ---------------------------------------------------------------------------

class TestBuildAnalysisPrompt:
    def test_includes_failure_data(self):
        entries = [_make_entry(task="Email triage failed", error="SMTP timeout")]
        prompt = _build_analysis_prompt(entries, [])
        assert "Email triage failed" in prompt
        assert "SMTP timeout" in prompt

    def test_includes_previous_proposals(self):
        entries = [_make_entry()]
        prev = [_make_proposal(description="Previous fix")]
        prompt = _build_analysis_prompt(entries, prev)
        assert "Previous fix" in prompt
        assert "Previous Proposals" in prompt

    def test_empty_previous_proposals(self):
        entries = [_make_entry()]
        prompt = _build_analysis_prompt(entries, [])
        assert "Previous Proposals" not in prompt

    def test_limits_entries(self):
        entries = [_make_entry(task=f"Task {i}") for i in range(50)]
        prompt = _build_analysis_prompt(entries, [])
        # Should only include MAX_FAILURE_ENTRIES
        assert f"Task {MAX_FAILURE_ENTRIES - 1}" in prompt
        # Task 30+ should not be included (0-indexed)
        assert f"Failure #{MAX_FAILURE_ENTRIES + 1}" not in prompt

    def test_includes_metadata(self):
        entries = [_make_entry(
            tier="high", model="claude-opus-4.7",
            source="goals", goal_id="self-improvement",
            tools_used=["file_read", "run_command"],
        )]
        prompt = _build_analysis_prompt(entries, [])
        assert "high" in prompt
        assert "claude-opus-4.7" in prompt
        assert "goals" in prompt
        assert "self-improvement" in prompt
        assert "file_read" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_json_response
# ---------------------------------------------------------------------------

class TestParseJsonResponse:
    def test_plain_json(self):
        text = '{"proposals": [], "analysis_summary": "No issues"}'
        result = _parse_json_response(text)
        assert result["proposals"] == []

    def test_markdown_fenced(self):
        text = '```json\n{"proposals": [{"x": 1}]}\n```'
        result = _parse_json_response(text)
        assert len(result["proposals"]) == 1

    def test_plain_fence(self):
        text = '```\n{"proposals": []}\n```'
        result = _parse_json_response(text)
        assert result["proposals"] == []

    def test_invalid_json(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_json_response("not json at all")


# ---------------------------------------------------------------------------
# Tests: _run_failure_analysis
# ---------------------------------------------------------------------------

class TestRunFailureAnalysis:
    @pytest.mark.asyncio
    async def test_empty_failures(self):
        config = _make_config()
        result = await _run_failure_analysis([], [], config)
        assert result == []

    @pytest.mark.asyncio
    async def test_generates_proposals(self):
        config = _make_config()
        failures = [_make_entry(task="Email send failed", error="Auth error")]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps({
            "proposals": [{
                "category": "failure-fix",
                "description": "Fix email auth handling",
                "target_files": ["src/secretary/tools_gmail.py"],
                "task_prompt": "Update the Gmail auth flow...",
                "priority": 0.8,
                "evidence": "1 failure with Auth error"
            }],
            "analysis_summary": "Auth issues detected"
        }))]

        with patch("secretary.goal_self_improve.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await _run_failure_analysis(failures, [], config)

        assert len(result) == 1
        assert result[0]["category"] == "failure-fix"
        assert result[0]["description"] == "Fix email auth handling"
        assert result[0]["status"] == "pending"
        assert "proposal_id" in result[0]

    @pytest.mark.asyncio
    async def test_limits_proposals(self):
        config = _make_config()
        failures = [_make_entry()]

        # Return more proposals than the limit
        proposals = [
            {"category": "failure-fix", "description": f"Fix {i}",
             "target_files": [f"f{i}.py"], "task_prompt": f"Fix {i}...",
             "priority": 0.5, "evidence": "test"}
            for i in range(10)
        ]
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps({
            "proposals": proposals, "analysis_summary": "Many issues"
        }))]

        with patch("secretary.goal_self_improve.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await _run_failure_analysis(failures, [], config)

        assert len(result) <= MAX_PROPOSALS_PER_ANALYSIS

    @pytest.mark.asyncio
    async def test_handles_llm_error(self):
        config = _make_config()
        failures = [_make_entry()]

        with patch("secretary.goal_self_improve.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))
            mock_client_cls.return_value = mock_client

            result = await _run_failure_analysis(failures, [], config)

        assert result == []

    @pytest.mark.asyncio
    async def test_handles_invalid_json(self):
        config = _make_config()
        failures = [_make_entry()]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Not valid JSON")]

        with patch("secretary.goal_self_improve.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await _run_failure_analysis(failures, [], config)

        assert result == []

    @pytest.mark.asyncio
    async def test_clamps_priority(self):
        config = _make_config()
        failures = [_make_entry()]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps({
            "proposals": [{
                "category": "failure-fix",
                "description": "Fix",
                "target_files": ["f.py"],
                "task_prompt": "Fix it",
                "priority": 1.5,  # Over 1.0
                "evidence": "test"
            }],
            "analysis_summary": ""
        }))]

        with patch("secretary.goal_self_improve.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await _run_failure_analysis(failures, [], config)

        assert result[0]["priority"] == 1.0  # clamped


# ---------------------------------------------------------------------------
# Tests: proposal_to_task
# ---------------------------------------------------------------------------

class TestProposalToTask:
    def test_converts_correctly(self):
        p = _make_proposal(
            proposal_id="prop-abc",
            task_prompt="Fix the bug in module.py",
        )
        task = proposal_to_task(p)
        assert task["_self_improve"] is True
        assert task["_proposal_id"] == "prop-abc"
        assert task["tier"] == "high"
        assert task["source"] == "goals"
        assert task["goal_id"] == SELF_IMPROVE_GOAL_ID
        assert "Fix the bug" in task["prompt"]

    def test_task_has_id(self):
        p = _make_proposal(proposal_id="prop-xyz")
        task = proposal_to_task(p)
        assert task["id"] == "self-improve-prop-xyz"


# ---------------------------------------------------------------------------
# Tests: run_self_improve_analysis
# ---------------------------------------------------------------------------

class TestRunSelfImproveAnalysis:
    @pytest.mark.asyncio
    async def test_skips_when_not_due(self):
        recent = datetime.now(timezone.utc).isoformat()
        state = _make_state(last_analysis=recent)
        run_log = MagicMock()
        config = _make_config()

        tasks = await run_self_improve_analysis(state, run_log, config)
        assert tasks == []

    @pytest.mark.asyncio
    async def test_executes_pending_when_not_due(self):
        """Even when analysis isn't due, pending proposals should execute."""
        recent = datetime.now(timezone.utc).isoformat()
        p = _make_proposal(proposal_id="p1", status="pending")
        state = _make_state(proposals=[p], last_analysis=recent)
        run_log = MagicMock()
        config = _make_config()

        tasks = await run_self_improve_analysis(state, run_log, config)
        assert len(tasks) == 1
        assert tasks[0]["_self_improve"] is True
        assert tasks[0]["_proposal_id"] == "p1"
        assert p["status"] == "executing"

    @pytest.mark.asyncio
    async def test_skips_when_too_many_pending(self):
        proposals = [
            _make_proposal(proposal_id=f"p{i}", status="pending")
            for i in range(MAX_PENDING_PROPOSALS)
        ]
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        state = _make_state(proposals=proposals, last_analysis=old)
        run_log = MagicMock()
        config = _make_config()

        tasks = await run_self_improve_analysis(state, run_log, config)
        # Should return 1 task (executing the top proposal) but NOT run analysis
        assert len(tasks) == 1

    @pytest.mark.asyncio
    async def test_no_failures_sets_timestamp(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        state = _make_state(last_analysis=old)
        run_log = MagicMock()
        run_log.recent.return_value = [
            _make_entry(success=True, error=None),  # all successes
        ]
        config = _make_config()

        tasks = await run_self_improve_analysis(state, run_log, config)
        assert tasks == []
        # Should still update last_analysis
        imp = state["self_improve_state"]
        assert imp["last_analysis"] is not None

    @pytest.mark.asyncio
    async def test_generates_and_executes(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        state = _make_state(last_analysis=old)
        run_log = MagicMock()
        run_log.recent.return_value = [
            _make_entry(success=False, task="Broken task", error="Error X"),
        ]
        config = _make_config()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps({
            "proposals": [{
                "category": "failure-fix",
                "description": "Fix Error X",
                "target_files": ["src/secretary/broken.py"],
                "task_prompt": "Fix the error...",
                "priority": 0.9,
                "evidence": "1 failure"
            }],
            "analysis_summary": "Found fixable error"
        }))]

        with patch("secretary.goal_self_improve.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            tasks = await run_self_improve_analysis(state, run_log, config)

        assert len(tasks) == 1
        assert tasks[0]["_self_improve"] is True
        imp = state["self_improve_state"]
        assert imp["total_proposed"] == 1
        assert imp["last_analysis"] is not None

    @pytest.mark.asyncio
    async def test_discards_stale_before_analysis(self):
        old_analysis = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        old_created = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        proposals = [
            _make_proposal(proposal_id="stale", status="pending", created=old_created),
        ]
        state = _make_state(proposals=proposals, last_analysis=old_analysis)
        run_log = MagicMock()
        run_log.recent.return_value = []  # no failures
        config = _make_config()

        await run_self_improve_analysis(state, run_log, config)

        assert proposals[0]["status"] == "discarded"


# ---------------------------------------------------------------------------
# Tests: ImprovementProposal dataclass
# ---------------------------------------------------------------------------

class TestImprovementProposal:
    def test_fields(self):
        p = ImprovementProposal(
            proposal_id="test",
            category="failure-fix",
            description="Fix something",
            target_files=["a.py"],
            task_prompt="Do the fix",
            priority=0.7,
            evidence="3 failures",
        )
        assert p.status == "pending"
        assert p.result is None
        assert p.executed is None


# ---------------------------------------------------------------------------
# Tests: End-to-end proposal lifecycle
# ---------------------------------------------------------------------------

class TestProposalLifecycle:
    @pytest.mark.asyncio
    async def test_full_lifecycle(self):
        """Walk through: analysis → proposal → execute → record result."""
        state: dict[str, Any] = {}
        config = _make_config()

        # 1. Analysis generates a proposal
        run_log = MagicMock()
        run_log.recent.return_value = [
            _make_entry(success=False, task="Task A", error="ModuleNotFoundError"),
            _make_entry(success=False, task="Task B", error="ModuleNotFoundError"),
        ]

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps({
            "proposals": [{
                "category": "failure-fix",
                "description": "Add missing import",
                "target_files": ["src/secretary/agent.py"],
                "task_prompt": "Add the missing import statement",
                "priority": 0.9,
                "evidence": "2 failures with ModuleNotFoundError"
            }],
            "analysis_summary": "Missing import detected"
        }))]

        with patch("secretary.goal_self_improve.anthropic.AsyncAnthropic") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            tasks = await run_self_improve_analysis(state, run_log, config)

        assert len(tasks) == 1
        task = tasks[0]
        proposal_id = task["_proposal_id"]

        # 2. Proposal is now "executing"
        imp = _get_improve_state(state)
        executing = [p for p in imp["proposals"] if p["status"] == "executing"]
        assert len(executing) == 1

        # 3. Record successful result
        record_proposal_result(
            state, proposal_id,
            success=True, promoted=True,
            changed_files=["src/secretary/agent.py"],
        )

        completed = [p for p in imp["proposals"] if p["status"] == "completed"]
        assert len(completed) == 1
        assert imp["total_promoted"] == 1

        # 4. Next analysis: proposal is no longer pending
        assert get_pending_proposal(state) is None


# ---------------------------------------------------------------------------
# Tests: _deduplicate_proposals
# ---------------------------------------------------------------------------

class TestDeduplicateProposals:
    def test_drops_similar_proposals(self):
        from secretary.goal_self_improve import _deduplicate_proposals
        existing = [{"description": "Fix test failures not propagating feedback", "status": "pending"}]
        new = [{"description": "Fix test failures not propagating feedback to loop", "status": "pending"}]
        result = _deduplicate_proposals(new, existing)
        assert len(result) == 0

    def test_keeps_distinct_proposals(self):
        from secretary.goal_self_improve import _deduplicate_proposals
        existing = [{"description": "Fix test failures not propagating feedback", "status": "pending"}]
        new = [{"description": "Add retry logic for Gmail auth token refresh", "status": "pending"}]
        result = _deduplicate_proposals(new, existing)
        assert len(result) == 1

    def test_ignores_discarded_proposals(self):
        from secretary.goal_self_improve import _deduplicate_proposals
        existing = [{"description": "Fix test failures not propagating feedback", "status": "discarded"}]
        new = [{"description": "Fix test failures not propagating feedback to loop", "status": "pending"}]
        result = _deduplicate_proposals(new, existing)
        assert len(result) == 1

    def test_dedup_within_batch(self):
        from secretary.goal_self_improve import _deduplicate_proposals
        new = [
            {"description": "Add scope constraints to self-improve prompts", "status": "pending"},
            {"description": "Add scope constraints to self-improvement prompts", "status": "pending"},
        ]
        result = _deduplicate_proposals(new, [])
        assert len(result) == 1

    def test_dedup_by_target_function(self):
        """Proposals targeting the same file+function are dropped even if descriptions differ."""
        from secretary.goal_self_improve import _deduplicate_proposals
        existing = [{
            "description": "JSON parsing crashes analysis",
            "task_prompt": "In src/secretary/goal_self_improve.py, function _run_failure_analysis: fix crash",
            "status": "completed",
        }]
        new = [{
            "description": "LLM API errors cause analysis failure",
            "task_prompt": "In src/secretary/goal_self_improve.py, function _run_failure_analysis: add retries",
            "status": "pending",
        }]
        result = _deduplicate_proposals(new, existing)
        assert len(result) == 0

    def test_dedup_different_function_passes(self):
        """Proposals targeting different functions in the same file are kept."""
        from secretary.goal_self_improve import _deduplicate_proposals
        existing = [{
            "description": "Fix crash in analysis",
            "task_prompt": "In src/secretary/goal_self_improve.py, function _run_failure_analysis: fix crash",
            "status": "completed",
        }]
        new = [{
            "description": "Fix stagnation detection",
            "task_prompt": "In src/secretary/goal_self_improve.py, function _detect_stagnation: improve logic",
            "status": "pending",
        }]
        result = _deduplicate_proposals(new, existing)
        assert len(result) == 1

    def test_extract_target(self):
        from secretary.goal_self_improve import _extract_target
        assert _extract_target("In src/secretary/foo.py, function bar: do stuff") == ("src/secretary/foo.py", "bar")
        assert _extract_target("No function target here") is None
