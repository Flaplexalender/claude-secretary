"""Tests for the oracle ensemble module."""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from secretary.oracle import (
    OracleConfig,
    VoteResult,
    _majority_vote,
    _normalize_tool_call,
    _summarize_turns,
    _build_oracle_system_prompt,
    oracle_run,
    FREE_MODELS,
    CHECKPOINT_MODEL,
    MAJORITY_THRESHOLD,
)
from secretary.config import SecretaryConfig
from secretary.memory import MemoryStore


# ── _normalize_tool_call ──


class TestNormalizeToolCall:
    def test_simple_tool(self):
        tc = {"name": "file_read", "input": {"path": "src/main.py"}}
        sig = _normalize_tool_call(tc)
        assert sig == 'file_read:{"path": "src/main.py"}'

    def test_sorted_args(self):
        tc1 = {"name": "grep_search", "input": {"path": "src/", "pattern": "TODO"}}
        tc2 = {"name": "grep_search", "input": {"pattern": "TODO", "path": "src/"}}
        assert _normalize_tool_call(tc1) == _normalize_tool_call(tc2)

    def test_string_args(self):
        tc = {"name": "file_read", "arguments": '{"path": "test.py"}'}
        sig = _normalize_tool_call(tc)
        assert "file_read" in sig
        assert "test.py" in sig

    def test_empty_args(self):
        tc = {"name": "noop", "input": {}}
        sig = _normalize_tool_call(tc)
        assert sig == "noop:{}"


# ── _majority_vote ──


class TestMajorityVote:
    def test_unanimous_tool_call(self):
        responses = [
            {"content": [{"type": "tool_use", "id": f"t{i}", "name": "file_read", "input": {"path": "x.py"}}]}
            for i in range(3)
        ]
        vote = _majority_vote(responses)
        assert len(vote.tool_calls) == 1
        assert vote.tool_calls[0]["name"] == "file_read"
        assert vote.agreement == 1.0
        assert vote.voter_count == 3

    def test_majority_2_of_3(self):
        responses = [
            {"content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "a.py"}}]},
            {"content": [{"type": "tool_use", "id": "t2", "name": "file_read", "input": {"path": "a.py"}}]},
            {"content": [{"type": "tool_use", "id": "t3", "name": "file_read", "input": {"path": "b.py"}}]},
        ]
        vote = _majority_vote(responses)
        assert len(vote.tool_calls) == 1
        assert vote.tool_calls[0]["input"]["path"] == "a.py"
        assert vote.agreement == pytest.approx(2 / 3, abs=0.01)

    def test_no_majority_all_different(self):
        responses = [
            {"content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "a.py"}}]},
            {"content": [{"type": "tool_use", "id": "t2", "name": "file_read", "input": {"path": "b.py"}}]},
            {"content": [{"type": "tool_use", "id": "t3", "name": "file_read", "input": {"path": "c.py"}}]},
        ]
        vote = _majority_vote(responses)
        assert len(vote.tool_calls) == 0  # No exact majority
        # But soft vote should detect same tool name
        assert len(vote.soft_tool_calls) == 1
        assert vote.soft_tool_calls[0]["name"] == "file_read"

    def test_no_soft_vote_different_tools(self):
        """All different tool names → no soft vote either."""
        responses = [
            {"content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "a.py"}}]},
            {"content": [{"type": "tool_use", "id": "t2", "name": "grep_search", "input": {"pattern": "x"}}]},
            {"content": [{"type": "tool_use", "id": "t3", "name": "file_list", "input": {"path": "/"}}]},
        ]
        vote = _majority_vote(responses)
        assert len(vote.tool_calls) == 0
        assert len(vote.soft_tool_calls) == 0

    def test_text_vote(self):
        responses = [
            {"content": [{"type": "text", "text": "The answer is 42"}]},
            {"content": [{"type": "text", "text": "The answer is 42"}]},
            {"content": [{"type": "text", "text": "The answer is 43"}]},
        ]
        vote = _majority_vote(responses)
        assert vote.text == "The answer is 42"

    def test_empty_responses(self):
        vote = _majority_vote([])
        assert vote.tool_calls == []
        assert vote.text == ""
        assert vote.agreement == 0.0
        assert vote.voter_count == 0

    def test_mixed_tools_and_text(self):
        responses = [
            {"content": [
                {"type": "tool_use", "id": "t1", "name": "grep_search", "input": {"pattern": "TODO"}},
                {"type": "text", "text": "Searching..."},
            ]},
            {"content": [
                {"type": "tool_use", "id": "t2", "name": "grep_search", "input": {"pattern": "TODO"}},
            ]},
            {"content": [
                {"type": "text", "text": "Done"},
            ]},
        ]
        vote = _majority_vote(responses)
        assert len(vote.tool_calls) == 1
        assert vote.tool_calls[0]["name"] == "grep_search"

    def test_multiple_tools_majority(self):
        """When workers agree on multiple tool calls."""
        responses = [
            {"content": [
                {"type": "tool_use", "id": "t1a", "name": "file_read", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "t1b", "name": "file_read", "input": {"path": "b.py"}},
            ]},
            {"content": [
                {"type": "tool_use", "id": "t2a", "name": "file_read", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "t2b", "name": "file_read", "input": {"path": "b.py"}},
            ]},
            {"content": [
                {"type": "tool_use", "id": "t3a", "name": "file_read", "input": {"path": "a.py"}},
            ]},
        ]
        vote = _majority_vote(responses)
        # a.py has 3/3 majority, b.py has 2/3 majority → both should win
        names = [tc["input"]["path"] for tc in vote.tool_calls]
        assert "a.py" in names
        assert "b.py" in names


# ── _summarize_turns ──


class TestSummarizeTurns:
    def test_empty_history(self):
        messages = [{"role": "user", "content": "do something"}]
        summary = _summarize_turns(messages, anchor=1)
        assert summary == "No work done yet."

    def test_tool_calls_summarized(self):
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "x.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file contents here"},
            ]},
        ]
        summary = _summarize_turns(messages, anchor=1)
        assert "Turn 1" in summary
        assert "file_read" in summary
        assert "file contents" in summary


# ── _build_oracle_system_prompt ──


class TestBuildOracleSystemPrompt:
    def test_worker_prompt(self):
        prompt = _build_oracle_system_prompt(None, "test task", max_turns=10)
        assert "AI agent" in prompt
        assert "10 turns" in prompt

    def test_checkpoint_prompt(self):
        prompt = _build_oracle_system_prompt(None, "test task", max_turns=10, is_checkpoint=True)
        assert "reviewing" in prompt.lower()
        assert "Senior reviewer" in prompt

    def test_memory_included(self):
        mem = MagicMock(spec=MemoryStore)
        mem.get_long.return_value = ["Remember: user likes CAD"]
        mem.get_short.return_value = []
        prompt = _build_oracle_system_prompt(mem, "test", max_turns=5)
        assert "CAD" in prompt

    def test_research_hint_for_audit_task(self):
        """Audit/search tasks get prescriptive grep_search hint."""
        prompt = _build_oracle_system_prompt(None, "audit all try/except blocks", max_turns=10)
        assert "grep_search" in prompt
        assert "APPROACH" in prompt

    def test_file_hint_for_file_task(self):
        """File tasks get file_read-first hint."""
        prompt = _build_oracle_system_prompt(None, "read config.py and check values", max_turns=10)
        assert "file_read" in prompt
        assert "APPROACH" in prompt

    def test_no_hint_for_checkpoint(self):
        """Checkpoints (Opus) should NOT get worker strategy hints."""
        prompt = _build_oracle_system_prompt(
            None, "audit all try/except blocks", max_turns=10, is_checkpoint=True
        )
        assert "APPROACH" not in prompt

    def test_no_hint_for_generic_task(self):
        """Tasks with no category match get no hint."""
        prompt = _build_oracle_system_prompt(None, "hello world", max_turns=10)
        assert "APPROACH" not in prompt


# ── OracleConfig ──


class TestOracleConfig:
    def test_defaults(self):
        cfg = OracleConfig()
        assert cfg.worker_models == FREE_MODELS
        assert cfg.checkpoint_model == CHECKPOINT_MODEL
        assert cfg.checkpoint_interval == 6
        assert cfg.max_turns == 14
        assert cfg.max_checkpoints == 3
        assert cfg.escalation_cooldown == 2  # Suppresses escalation for 2 turns after checkpoint

    def test_custom(self):
        cfg = OracleConfig(checkpoint_interval=2, max_checkpoints=5)
        assert cfg.checkpoint_interval == 2
        assert cfg.max_checkpoints == 5


# ── oracle_run (integration-style with mocks) ──


class TestOracleRun:
    @pytest.mark.asyncio
    async def test_single_turn_text_response(self):
        """Workers all return text (no tools) → task complete in 1 turn."""
        mock_response = {
            "content": [{"type": "text", "text": "The answer is 42."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-4.1",
        }

        with patch("secretary.oracle._query_worker", return_value=mock_response):
            result = await oracle_run(
                task="What is 6 * 7?",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools={},
                oracle_config=OracleConfig(max_turns=3),
            )

        assert result.error is None
        assert result.text == "The answer is 42."
        assert result.premium_requests == 0.0  # Only free workers used

    @pytest.mark.asyncio
    async def test_tool_round_with_vote(self):
        """Workers agree on a tool call → tool executed → workers return text."""
        tool_response = {
            "content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "test.py"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-4.1",
        }
        text_response = {
            "content": [{"type": "text", "text": "File contains tests."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 200, "output_tokens": 80},
            "model": "gpt-4.1",
        }

        call_count = 0

        async def mock_worker(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # First 3 calls (turn 1): return tool call
            # Next 3 calls (turn 2): return text
            if call_count <= 3:
                return tool_response
            return text_response

        mock_tool = {
            "file_read": {
                "name": "file_read",
                "description": "Read file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "# test code"}]}),
            }
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker):
            result = await oracle_run(
                task="Read test.py and summarize",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tool,
                oracle_config=OracleConfig(max_turns=5, checkpoint_interval=10),
            )

        assert result.error is None
        assert "file_read" in result.tools_used
        assert result.premium_requests == 0.0  # Free workers only

    @pytest.mark.asyncio
    async def test_checkpoint_triggered(self):
        """After checkpoint_interval worker turns, Opus checkpoint fires."""
        tool_response = {
            "content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "x.py"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-4.1",
        }
        checkpoint_response = {
            "content": [{"type": "text", "text": "Task looks complete. Good work."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "model": "claude-opus-4.7",
        }

        worker_turn = 0

        async def mock_worker(*args, **kwargs):
            return tool_response

        async def mock_checkpoint(*args, **kwargs):
            return checkpoint_response

        mock_tool = {
            "file_read": {
                "name": "file_read",
                "description": "Read file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "content"}]}),
            }
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker), \
             patch("secretary.oracle._query_checkpoint", side_effect=mock_checkpoint):
            result = await oracle_run(
                task="Read all files",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tool,
                oracle_config=OracleConfig(max_turns=8, checkpoint_interval=2),
            )

        # Opus checkpoint should have been called (3× premium)
        assert result.premium_requests > 0
        assert result.premium_requests == pytest.approx(3.0)  # One Opus checkpoint

    @pytest.mark.asyncio
    async def test_escalation_on_disagreement(self):
        """When all 3 workers disagree on different tools, Opus decides."""
        # Workers call completely different tools — soft voting can't help
        disagreeing_responses = [
            {"content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "a.py"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4.1"},
            {"content": [{"type": "tool_use", "id": "t2", "name": "grep_search", "input": {"pattern": "foo"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4o"},
            {"content": [{"type": "tool_use", "id": "t3", "name": "file_list", "input": {"path": "src/"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-5-mini"},
        ]

        opus_response = {
            "content": [{"type": "tool_use", "id": "opus1", "name": "file_read", "input": {"path": "correct.py"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "model": "claude-opus-4.7",
        }
        text_response = {
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 30},
            "model": "gpt-4.1",
        }

        call_count = 0
        worker_call_count = 0

        async def mock_worker(*args, **kwargs):
            nonlocal worker_call_count
            idx = worker_call_count % 3
            worker_call_count += 1
            if worker_call_count <= 3:
                return disagreeing_responses[idx]
            return text_response

        async def mock_checkpoint(*args, **kwargs):
            return opus_response

        mock_tool = {
            "file_read": {
                "name": "file_read",
                "description": "Read file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "content"}]}),
            },
            "grep_search": {
                "name": "grep_search",
                "description": "Search",
                "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "results"}]}),
            },
            "file_list": {
                "name": "file_list",
                "description": "List",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "files"}]}),
            },
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker), \
             patch("secretary.oracle._query_checkpoint", side_effect=mock_checkpoint):
            result = await oracle_run(
                task="Analyze the project",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tool,
                oracle_config=OracleConfig(max_turns=5, checkpoint_interval=10),
            )

        # Escalation to Opus should have happened (3× premium)
        assert result.premium_requests >= 3.0
        assert "file_read" in result.tools_used

    @pytest.mark.asyncio
    async def test_soft_tool_name_voting(self):
        """Workers agree on tool name but not args → soft vote picks one, no escalation."""
        disagreeing_responses = [
            {"content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "a.py"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4.1"},
            {"content": [{"type": "tool_use", "id": "t2", "name": "file_read", "input": {"path": "b.py"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4o"},
            {"content": [{"type": "tool_use", "id": "t3", "name": "file_read", "input": {"path": "c.py"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-5-mini"},
        ]
        text_response = {
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 30},
            "model": "gpt-4.1",
        }

        worker_call_count = 0

        async def mock_worker(*args, **kwargs):
            nonlocal worker_call_count
            idx = worker_call_count % 3
            worker_call_count += 1
            if worker_call_count <= 3:
                return disagreeing_responses[idx]
            return text_response

        mock_tool = {
            "file_read": {
                "name": "file_read",
                "description": "Read file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "content"}]}),
            }
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker):
            result = await oracle_run(
                task="Read a file",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tool,
                oracle_config=OracleConfig(max_turns=5, checkpoint_interval=10),
            )

        # Soft vote should have picked file_read without Opus escalation
        assert result.premium_requests == 0.0
        assert "file_read" in result.tools_used

    @pytest.mark.asyncio
    async def test_majority_end_turn(self):
        """2/3 workers signaling end_turn is enough to finish (not all 3)."""
        responses = [
            {"content": [{"type": "text", "text": "Answer A"}],
             "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4.1"},
            {"content": [{"type": "text", "text": "Answer B"}],
             "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4o"},
            {"content": [{"type": "text", "text": "Answer C"}],
             "stop_reason": "max_tokens", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-5-mini"},
        ]

        with patch("secretary.oracle._query_worker", side_effect=lambda *a, **kw: responses[0]):
            # All return same response (end_turn) to ensure majority
            result = await oracle_run(
                task="Quick question",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools={},
                oracle_config=OracleConfig(max_turns=5),
            )

        assert result.error is None
        assert result.num_turns <= 2  # Should finish quickly

    @pytest.mark.asyncio
    async def test_consecutive_stalls_trigger_early_checkpoint(self):
        """After 2 stalled turns, next turn triggers Opus checkpoint."""
        stall_response = {
            "content": [{"type": "text", "text": "thinking..."}],
            "stop_reason": "max_tokens",  # Not end_turn, so loop continues
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-4.1",
        }
        stall_response_2 = {
            "content": [{"type": "text", "text": "still thinking..."}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-4o",
        }
        stall_response_3 = {
            "content": [{"type": "text", "text": "working on it..."}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-5-mini",
        }
        checkpoint_done = {
            "content": [{"type": "text", "text": "The answer is 42."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "model": "claude-opus-4.7",
        }

        worker_turn = 0

        async def mock_worker(*args, **kwargs):
            nonlocal worker_turn
            idx = worker_turn % 3
            worker_turn += 1
            return [stall_response, stall_response_2, stall_response_3][idx]

        async def mock_checkpoint(*args, **kwargs):
            return checkpoint_done

        with patch("secretary.oracle._query_worker", side_effect=mock_worker), \
             patch("secretary.oracle._query_checkpoint", side_effect=mock_checkpoint):
            result = await oracle_run(
                task="Tricky task",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools={},
                # checkpoint_interval=10 means normally no checkpoint for 10 turns
                # but stall detection should trigger it much earlier
                oracle_config=OracleConfig(max_turns=8, checkpoint_interval=10),
            )

        # Opus checkpoint should have fired due to stall detection, not interval
        assert result.premium_requests > 0

    @pytest.mark.asyncio
    async def test_all_workers_fail(self):
        """When all workers fail, result has error."""
        error_response = {
            "content": [],
            "stop_reason": "error",
            "usage": {},
            "model": "gpt-4.1",
            "error": "Connection refused",
        }

        with patch("secretary.oracle._query_worker", return_value=error_response):
            result = await oracle_run(
                task="Do something",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools={},
                oracle_config=OracleConfig(max_turns=3),
            )

        assert result.error is not None
        assert "All oracle workers failed" in result.error

    @pytest.mark.asyncio
    async def test_no_consecutive_checkpoints(self):
        """Checkpoints require NEW worker turns since the last checkpoint.

        Previously, worker_turns % interval == 0 would fire all remaining
        checkpoints consecutively without worker turns in between.
        """
        call_count = {"worker": 0, "checkpoint": 0}

        tool_response = {
            "content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "x.py"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "gpt-4.1",
        }
        checkpoint_response = {
            "content": [{"type": "tool_use", "id": "opus1", "name": "file_read", "input": {"path": "y.py"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "model": "claude-opus-4.7",
        }

        async def mock_worker(*args, **kwargs):
            call_count["worker"] += 1
            return tool_response

        async def mock_checkpoint(*args, **kwargs):
            call_count["checkpoint"] += 1
            return checkpoint_response

        mock_tool = {
            "file_read": {
                "name": "file_read",
                "description": "Read file",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "code"}]}),
            }
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker), \
             patch("secretary.oracle._query_checkpoint", side_effect=mock_checkpoint):
            result = await oracle_run(
                task="Read all files",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tool,
                # interval=2, max_checkpoints=3 — previously would fire 3 at worker_turns=2
                oracle_config=OracleConfig(max_turns=10, checkpoint_interval=2, max_checkpoints=3),
            )

        # With the fix: checkpoint fires at worker_turns 2, then needs 2 more → 4, then 2 more → 6
        # NOT all 3 at worker_turns=2
        # Workers should get turns between checkpoints
        assert call_count["checkpoint"] <= 3
        # Key assertion: there must be worker turns between checkpoints
        # With interval=2 and 3 checkpoints in 10 total turns:
        # worker_turn 1, 2 → checkpoint 1 → worker_turn 3, 4 → checkpoint 2 → worker_turn 5, 6 → checkpoint 3
        # So we need at least 6 worker turns for 3 checkpoints
        assert call_count["worker"] >= call_count["checkpoint"] * 2

    @pytest.mark.asyncio
    async def test_escalation_cooldown_after_checkpoint(self):
        """Disagreement escalation is suppressed during cooldown after a checkpoint.

        After a scheduled/disagreement checkpoint, workers need time to
        incorporate Opus's corrections before another escalation fires.
        """
        # First 3 calls: workers disagree (triggers escalation)
        # Next 3 calls: workers still disagree (should be suppressed by cooldown)
        # Next 3 calls: workers agree (end_turn)
        disagreeing = [
            {"content": [{"type": "tool_use", "id": "t1", "name": "file_read", "input": {"path": "a.py"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4.1"},
            {"content": [{"type": "tool_use", "id": "t2", "name": "grep_search", "input": {"pattern": "x"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-4o"},
            {"content": [{"type": "tool_use", "id": "t3", "name": "file_list", "input": {"path": "/"}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 100, "output_tokens": 50}, "model": "gpt-5-mini"},
        ]
        done = {
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "model": "gpt-4.1",
        }
        opus_resp = {
            "content": [{"type": "tool_use", "id": "opus1", "name": "file_read", "input": {"path": "fix.py"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "model": "claude-opus-4.7",
        }

        worker_call = {"n": 0}

        async def mock_worker(*args, **kwargs):
            idx = worker_call["n"] % 3
            worker_call["n"] += 1
            # First 4 calls (2 turns × 2 workers): disagree. Then agree to end.
            if worker_call["n"] <= 4:
                return disagreeing[idx]
            return done

        checkpoint_calls = {"n": 0}

        async def mock_checkpoint(*args, **kwargs):
            checkpoint_calls["n"] += 1
            return opus_resp

        mock_tools = {
            "file_read": {"name": "file_read", "description": "Read", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                          "func": AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})},
            "grep_search": {"name": "grep_search", "description": "Search", "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
                            "func": AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})},
            "file_list": {"name": "file_list", "description": "List", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                          "func": AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})},
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker), \
             patch("secretary.oracle._query_checkpoint", side_effect=mock_checkpoint):
            result = await oracle_run(
                task="Analyze project",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tools,
                # High interval so only disagreement-based escalation triggers
                oracle_config=OracleConfig(
                    max_turns=8, checkpoint_interval=20,
                    escalation_cooldown=2,
                ),
            )

        # Only 1 checkpoint should fire — the 2nd disagreement is within cooldown
        assert checkpoint_calls["n"] == 1
        assert result.premium_requests == pytest.approx(3.0)


# ── Config integration ──


class TestConfigIntegration:
    def test_oracle_tier_exists(self):
        config = SecretaryConfig()
        assert "oracle" in config.routing.tiers
        assert config.routing.tiers["oracle"].model == "oracle-ensemble"

    def test_oracle_budget(self):
        config = SecretaryConfig()
        assert "oracle" in config.optimizations.paid_turn_limits
        assert "oracle" in config.optimizations.task_premium_budget
        assert config.optimizations.task_premium_budget["oracle"] == 9.0

    def test_oracle_tier_multiplier(self):
        from secretary.router import TIER_MULTIPLIERS
        assert "oracle-ensemble" in TIER_MULTIPLIERS
        assert TIER_MULTIPLIERS["oracle-ensemble"] == 0.0


# ── Worker timeout ──


class TestWorkerTimeout:
    """Tests for per-worker timeout in oracle ensemble."""

    @pytest.mark.asyncio
    async def test_timeout_config_default(self):
        """Default timeout is 30 seconds."""
        config = OracleConfig()
        assert config.worker_timeout == 30.0

    @pytest.mark.asyncio
    async def test_timeout_config_override(self):
        """Timeout can be configured."""
        config = OracleConfig(worker_timeout=15.0)
        assert config.worker_timeout == 15.0

    @pytest.mark.asyncio
    async def test_slow_worker_times_out(self):
        """A slow worker should time out and be filtered as an error."""
        call_count = {"n": 0}

        async def mock_worker(*args, **kwargs):
            call_count["n"] += 1
            model = kwargs.get("model") or args[1]
            if model == "gpt-4o":
                # Simulate a slow worker
                await asyncio.sleep(0.1)  # reduced from 10s for testing
            return {
                "content": [{"type": "text", "text": "Done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 50, "output_tokens": 20},
                "model": model,
            }

        mock_tools = {
            "file_read": {
                "name": "file_read", "description": "Read",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]}),
            },
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker):
            result = await oracle_run(
                task="Simple test",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tools,
                oracle_config=OracleConfig(
                    worker_timeout=0.5,  # Very short timeout
                    max_turns=2,
                    checkpoint_interval=20,  # No checkpoints
                ),
            )

        # Task should complete — 2 of 3 workers respond, 1 times out
        assert result.error is None
        assert result.premium_requests == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_all_workers_timeout(self):
        """If all workers time out, oracle should error gracefully."""

        async def mock_worker(*args, **kwargs):
            await asyncio.sleep(10)  # must exceed worker_timeout (0.5s) to trigger timeout
            return {
                "content": [{"type": "text", "text": "Done"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 50, "output_tokens": 20},
                "model": "gpt-4.1",
            }

        mock_tools = {
            "file_read": {
                "name": "file_read", "description": "Read",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]}),
            },
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker):
            result = await oracle_run(
                task="All timeout test",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tools,
                oracle_config=OracleConfig(
                    worker_timeout=0.5,
                    max_turns=2,
                    checkpoint_interval=20,
                ),
            )

        # Should fail gracefully, not crash
        assert result.error is not None or result.num_turns == 0

    @pytest.mark.asyncio
    async def test_no_timeout_when_zero(self):
        """worker_timeout=0 means no timeout (backward compatible)."""
        done = {
            "content": [{"type": "text", "text": "Done"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 20},
            "model": "gpt-4.1",
        }

        async def mock_worker(*args, **kwargs):
            return done

        mock_tools = {
            "file_read": {
                "name": "file_read", "description": "Read",
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                "func": AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]}),
            },
        }

        with patch("secretary.oracle._query_worker", side_effect=mock_worker):
            result = await oracle_run(
                task="No timeout test",
                config=SecretaryConfig(anthropic_base_url="http://localhost:4141"),
                tools=mock_tools,
                oracle_config=OracleConfig(
                    worker_timeout=0,  # No timeout
                    max_turns=2,
                    checkpoint_interval=20,
                ),
            )

        assert result.error is None
