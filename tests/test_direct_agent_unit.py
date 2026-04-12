"""Unit tests for direct_agent module — pure functions, no API calls.

Tests helper functions, constants, and schema building without touching
the Anthropic API. The main `run()` function is tested in test_chat_direct.py.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from secretary.direct_agent import (
    AGENT_PREFIX,
    RunResult,
    _build_system_prompt,
    _build_tool_schemas,
    _to_openai_messages,
    _to_openai_tools,
    _parse_openai_response,
    _OAIBlock,
    _OAIResponse,
    _OAIUsage,
    _TIER_MAX_TOKENS,
    _STREAMING_TIERS,
)
from secretary.memory import MemoryStore
from secretary.router import RoutingDecision


# ══════════════════════════════════════════════════════════════
#  AGENT_PREFIX
# ══════════════════════════════════════════════════════════════


@pytest.mark.skipif(not AGENT_PREFIX, reason="prefix data not installed")
def test_agent_prefix_structure():
    """AGENT_PREFIX must start with user, contain multi-turn tool demonstration."""
    assert len(AGENT_PREFIX) >= 4  # multi-turn demo
    assert AGENT_PREFIX[0]["role"] == "user"
    assert AGENT_PREFIX[1]["role"] == "assistant"
    # Should have tool_use blocks as few-shot demonstration
    assert isinstance(AGENT_PREFIX[1]["content"], list)
    assert any(b["type"] == "tool_use" for b in AGENT_PREFIX[1]["content"])


@pytest.mark.skipif(not AGENT_PREFIX, reason="prefix data not installed")
def test_agent_prefix_assistant_has_text_block():
    """Assistant messages use content block format (not plain string)."""
    for msg in AGENT_PREFIX:
        if msg["role"] == "assistant":
            assert isinstance(msg["content"], list)


# ══════════════════════════════════════════════════════════════
#  _build_system_prompt
# ══════════════════════════════════════════════════════════════


def test_system_prompt_contains_efficiency_rules(tmp_path):
    """System prompt should include efficiency rules."""
    mem = MemoryStore(path=tmp_path / "memory.json")
    prompt = _build_system_prompt(mem)
    assert "RULES" in prompt
    assert "7+" in prompt or "parallel" in prompt


def test_system_prompt_includes_long_term_memory(tmp_path):
    """Long-term memory entries appear in the system prompt."""
    mem = MemoryStore(path=tmp_path / "memory.json")
    mem.add_long("Important pattern: always use batch operations")
    prompt = _build_system_prompt(mem)
    assert "Important pattern" in prompt
    assert "## Memory" in prompt


def test_system_prompt_includes_short_term_memory(tmp_path):
    """Short-term memory entries appear in the system prompt."""
    mem = MemoryStore(path=tmp_path / "memory.json")
    mem.add_short("Task: Check morning emails")
    prompt = _build_system_prompt(mem)
    assert "Check morning emails" in prompt
    assert "## Recent" in prompt


def test_system_prompt_no_memory_sections_when_empty(tmp_path):
    """When memory is empty, no memory sections appear."""
    mem = MemoryStore(path=tmp_path / "memory.json")
    prompt = _build_system_prompt(mem)
    assert "## Memory" not in prompt
    assert "## Recent" not in prompt


def test_system_prompt_limits_long_memory_to_10(tmp_path):
    """Only the last 10 long-term entries are included.

    Uses entries distinct enough to avoid fuzzy deduplication (threshold=0.85).
    """
    mem = MemoryStore(path=tmp_path / "memory.json")
    # Use very different text per entry to avoid dedup
    topics = [
        "Use batch API calls for file operations",
        "Always run pytest before committing code",
        "OAuth tokens expire after 3600 seconds",
        "Calendar events need ISO 8601 timestamps",
        "Email regex must handle plus-addressing",
        "Watcher interval defaults to 15 minutes",
        "RunLog rotation happens at 10MB threshold",
        "Memory consolidation merges similar tasks",
        "Router scores keywords for tier selection",
        "Campaign YAML supports schedule expressions",
        "Direct agent uses streaming for Opus tier",
        "Tool results get truncated after 4 turns",
        "Heartbeat JSON tracks watcher uptime stats",
        "Cost forecast uses linear daily projection",
        "Export command supports both CSV and JSON",
    ]
    for topic in topics:
        mem.add_long(topic)

    # Verify all 15 were stored (no dedup since they're all different)
    assert len(mem.get_long()) == 15

    prompt = _build_system_prompt(mem)
    # Should include entries 5-14 (last 10)
    assert topics[5] in prompt
    assert topics[14] in prompt
    # Earlier entries should be excluded
    assert topics[4] not in prompt


def test_system_prompt_limits_short_memory_to_5(tmp_path):
    """Only the last 5 short-term entries are included."""
    mem = MemoryStore(path=tmp_path / "memory.json")
    for i in range(10):
        mem.add_short(f"Task: operation {i}")
    prompt = _build_system_prompt(mem)
    # Should include entries 5-9 (last 5)
    assert "operation 5" in prompt
    assert "operation 9" in prompt
    assert "operation 4" not in prompt


# ══════════════════════════════════════════════════════════════
#  _build_tool_schemas
# ══════════════════════════════════════════════════════════════


def test_build_tool_schemas_empty():
    """Empty tool registry → empty schemas list."""
    assert _build_tool_schemas({}) == []


def test_build_tool_schemas_extracts_fields():
    """Tool schemas contain name, description, input_schema — no func."""
    tools = {
        "test_tool": {
            "name": "test_tool",
            "description": "A test tool",
            "input_schema": {"type": "object", "properties": {}},
            "func": lambda x: x,  # should NOT appear in output
        }
    }
    schemas = _build_tool_schemas(tools)
    assert len(schemas) == 1
    assert schemas[0]["name"] == "test_tool"
    assert schemas[0]["description"] == "A test tool"
    assert "func" not in schemas[0]


def test_build_tool_schemas_multiple_tools():
    """Multiple tools produce multiple schemas in order."""
    tools = {
        "tool_a": {
            "name": "tool_a",
            "description": "Tool A",
            "input_schema": {"type": "object"},
            "func": lambda x: x,
        },
        "tool_b": {
            "name": "tool_b",
            "description": "Tool B",
            "input_schema": {"type": "object"},
            "func": lambda x: x,
        },
    }
    schemas = _build_tool_schemas(tools)
    assert len(schemas) == 2
    names = {s["name"] for s in schemas}
    assert names == {"tool_a", "tool_b"}


# ══════════════════════════════════════════════════════════════
#  Constants
# ══════════════════════════════════════════════════════════════


def test_tier_max_tokens_all_tiers_present():
    """All three tiers have max_tokens configured."""
    assert "high" in _TIER_MAX_TOKENS
    assert "medium" in _TIER_MAX_TOKENS
    assert "low" in _TIER_MAX_TOKENS


def test_tier_max_tokens_high_is_largest():
    """High tier should have the most tokens."""
    assert _TIER_MAX_TOKENS["high"] >= _TIER_MAX_TOKENS["medium"]
    assert _TIER_MAX_TOKENS["medium"] >= _TIER_MAX_TOKENS["low"]


def test_streaming_tiers_only_high():
    """High and deep tiers use streaming (Opus)."""
    assert _STREAMING_TIERS == {"high", "deep"}


# ══════════════════════════════════════════════════════════════
#  RunResult
# ══════════════════════════════════════════════════════════════


def test_run_result_defaults():
    """RunResult has sensible defaults."""
    routing = RoutingDecision(
        tier="low", model="claude-haiku-4.5", max_turns=10,
        max_budget_usd=0.0, reason="test",
    )
    r = RunResult(task="test task", routing=routing)
    assert r.task == "test task"
    assert r.text == ""
    assert r.error is None
    assert r.cost_usd == 0.0
    assert r.num_turns == 0
    assert r.messages == []
    assert r.tools_used == []
    assert r.input_tokens == 0
    assert r.output_tokens == 0


def test_run_result_mutable_lists_independent():
    """Each RunResult should have independent list instances."""
    routing = RoutingDecision(
        tier="low", model="claude-haiku-4.5", max_turns=10,
        max_budget_usd=0.0, reason="test",
    )
    r1 = RunResult(task="a", routing=routing)
    r2 = RunResult(task="b", routing=routing)
    r1.tools_used.append("gmail_search")
    assert r2.tools_used == []  # should not be shared


# ══════════════════════════════════════════════════════════════
#  OpenAI translation: _to_openai_messages
# ══════════════════════════════════════════════════════════════


def test_to_openai_messages_basic():
    """System prompt becomes first message; user message preserved."""
    messages = [{"role": "user", "content": "Hello"}]
    result = _to_openai_messages("You are helpful.", messages)
    assert result[0] == {"role": "system", "content": "You are helpful."}
    assert result[1] == {"role": "user", "content": "Hello"}


def test_to_openai_messages_agent_prefix():
    """Agent prefix translates correctly to OpenAI format."""
    messages = [
        {"role": "user", "content": "."},
        {"role": "assistant", "content": [{"type": "text", "text": "."}]},
        {"role": "user", "content": "Do task"},
    ]
    result = _to_openai_messages("sys", messages)
    assert len(result) == 4  # system + 3 messages
    assert result[1] == {"role": "user", "content": "."}
    assert result[2] == {"role": "assistant", "content": "."}  # no tool_calls when empty
    assert result[3] == {"role": "user", "content": "Do task"}


def test_to_openai_messages_tool_use():
    """Assistant tool_use blocks become tool_calls array."""
    messages = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "I'll read the file."},
            {"type": "tool_use", "id": "call_123", "name": "file_read",
             "input": {"path": "/tmp/test.txt"}},
        ]},
    ]
    result = _to_openai_messages("sys", messages)
    msg = result[1]
    assert msg["role"] == "assistant"
    assert msg["content"] == "I'll read the file."
    assert len(msg["tool_calls"]) == 1
    tc = msg["tool_calls"][0]
    assert tc["id"] == "call_123"
    assert tc["function"]["name"] == "file_read"
    assert json.loads(tc["function"]["arguments"]) == {"path": "/tmp/test.txt"}


def test_to_openai_messages_tool_result():
    """Anthropic tool_result blocks become OpenAI tool messages."""
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_123", "content": "file contents here"},
        ]},
    ]
    result = _to_openai_messages("sys", messages)
    assert result[1] == {
        "role": "tool",
        "tool_call_id": "call_123",
        "content": "file contents here",
    }


def test_to_openai_messages_multiple_tool_results():
    """Multiple tool_results in one Anthropic message expand to separate OAI messages."""
    messages = [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "result A"},
            {"type": "tool_result", "tool_use_id": "call_2", "content": "result B"},
        ]},
    ]
    result = _to_openai_messages("sys", messages)
    # system + 2 tool messages
    assert len(result) == 3
    assert result[1]["role"] == "tool"
    assert result[1]["tool_call_id"] == "call_1"
    assert result[2]["role"] == "tool"
    assert result[2]["tool_call_id"] == "call_2"


# ══════════════════════════════════════════════════════════════
#  OpenAI translation: _to_openai_tools
# ══════════════════════════════════════════════════════════════


def test_to_openai_tools_empty():
    assert _to_openai_tools([]) == []


def test_to_openai_tools_translates():
    """Anthropic tool schema → OpenAI function-calling format."""
    schemas = [
        {"name": "file_read", "description": "Read a file", "input_schema": {
            "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
        }},
    ]
    result = _to_openai_tools(schemas)
    assert len(result) == 1
    assert result[0]["type"] == "function"
    f = result[0]["function"]
    assert f["name"] == "file_read"
    assert f["description"] == "Read a file"
    assert f["parameters"]["required"] == ["path"]


# ══════════════════════════════════════════════════════════════
#  _parse_openai_response
# ══════════════════════════════════════════════════════════════


def test_parse_openai_response_text_only():
    """Text-only response parses correctly."""
    raw = {
        "choices": [{"message": {"content": "Hello!", "role": "assistant"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105},
    }
    resp = _parse_openai_response(raw)
    assert len(resp.content) == 1
    assert resp.content[0].type == "text"
    assert resp.content[0].text == "Hello!"
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 100
    assert resp.usage.output_tokens == 5


def test_parse_openai_response_tool_calls():
    """Tool call response parses to tool_use blocks."""
    raw = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "file_read", "arguments": '{"path": "/tmp/x"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30},
    }
    resp = _parse_openai_response(raw)
    assert len(resp.content) == 1  # no text (content was None)
    b = resp.content[0]
    assert b.type == "tool_use"
    assert b.id == "call_abc"
    assert b.name == "file_read"
    assert b.input == {"path": "/tmp/x"}
    assert resp.stop_reason == "tool_use"


def test_parse_openai_response_text_and_tools():
    """Response with both text and tool calls parses both."""
    raw = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "I'll read the file.",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "file_read", "arguments": '{"path": "/a"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }
    resp = _parse_openai_response(raw)
    assert len(resp.content) == 2
    assert resp.content[0].type == "text"
    assert resp.content[0].text == "I'll read the file."
    assert resp.content[1].type == "tool_use"
    assert resp.content[1].name == "file_read"


def test_parse_openai_response_bad_json_arguments():
    """Malformed tool arguments don't crash — fallback to empty dict."""
    raw = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "test", "arguments": "not valid json!!!"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    }
    resp = _parse_openai_response(raw)
    assert resp.content[0].input == {}  # graceful fallback
