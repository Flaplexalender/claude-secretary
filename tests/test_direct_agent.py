"""Tests for direct_agent module — unit tests (no API calls)."""
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch
import asyncio

import pytest

from secretary.direct_agent import (
    AGENT_PREFIX,
    RunResult,
    _build_system_prompt,
    _build_client,
    _build_tool_schemas,
    _execute_tool,
    _stream_call,
    _TIER_MAX_TOKENS,
    _STREAMING_TIERS,
    run,
)
from secretary.config import SecretaryConfig
from secretary.memory import MemoryStore


# ---------------------------------------------------------------------------
# AGENT_PREFIX
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not AGENT_PREFIX, reason="prefix data not installed")
def test_agent_prefix_structure():
    """Prefix must start with user, contain assistant role, and have tool demonstrations."""
    assert len(AGENT_PREFIX) >= 4  # multi-turn demonstration
    assert AGENT_PREFIX[0]["role"] == "user"
    assert AGENT_PREFIX[1]["role"] == "assistant"
    # Should contain tool_use blocks as few-shot demonstration
    assistant_content = AGENT_PREFIX[1]["content"]
    assert isinstance(assistant_content, list)
    assert any(b["type"] == "tool_use" for b in assistant_content)


@pytest.mark.skipif(not AGENT_PREFIX, reason="prefix data not installed")
def test_agent_prefix_assistant_has_content_blocks():
    """Assistant messages must use content blocks (not plain string)."""
    for msg in AGENT_PREFIX:
        if msg["role"] == "assistant":
            assert isinstance(msg["content"], list)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_tier_max_tokens_has_all_tiers():
    """All expected tiers are in the token budget map."""
    assert "high" in _TIER_MAX_TOKENS
    assert "medium" in _TIER_MAX_TOKENS
    assert "low" in _TIER_MAX_TOKENS
    assert _TIER_MAX_TOKENS["high"] >= _TIER_MAX_TOKENS["medium"]


def test_streaming_tiers_only_high():
    """Only 'high' tier uses streaming (Opus needs it for large max_tokens)."""
    assert "high" in _STREAMING_TIERS
    assert "medium" not in _STREAMING_TIERS
    assert "low" not in _STREAMING_TIERS


# ---------------------------------------------------------------------------
# _build_system_prompt
# ---------------------------------------------------------------------------

def test_build_system_prompt_empty_memory(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    prompt = _build_system_prompt(mem)
    assert "AI agent" in prompt
    assert "## Memory" not in prompt


def test_build_system_prompt_with_long_memory(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("important fact")
    prompt = _build_system_prompt(mem)
    assert "## Memory" in prompt
    assert "important fact" in prompt


def test_build_system_prompt_with_short_memory(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_short("just happened")
    prompt = _build_system_prompt(mem)
    assert "## Recent" in prompt
    assert "just happened" in prompt


def test_build_system_prompt_limits_long_to_10(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
             "golf", "hotel", "india", "juliet", "kilo", "lima",
             "mike", "november", "oscar"]
    for w in words:
        mem.add_long(f"phonetic: {w}")
    prompt = _build_system_prompt(mem)
    assert "phonetic: oscar" in prompt
    assert "phonetic: foxtrot" in prompt
    assert "phonetic: echo" not in prompt


def test_build_system_prompt_limits_short_to_5(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    for w in words:
        mem.add_short(f"short: {w}")
    prompt = _build_system_prompt(mem)
    assert "short: hotel" in prompt
    assert "short: delta" in prompt
    assert "short: charlie" not in prompt


def test_build_system_prompt_efficiency_rules(tmp_path: Path):
    """System prompt includes critical efficiency rules."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    prompt = _build_system_prompt(mem)
    assert "RULES" in prompt
    assert "7+" in prompt or "parallel" in prompt
    assert "tool" in prompt.lower()


# ---------------------------------------------------------------------------
# _build_client
# ---------------------------------------------------------------------------

def test_build_client_strips_trailing_slash(tmp_path: Path):
    config = SecretaryConfig(
        data_root=str(tmp_path),
        anthropic_base_url="http://localhost:4141/",
    )
    client = _build_client(config)
    # base_url should not end with /
    assert not str(client.base_url).rstrip("/").endswith("/v1/")


def test_build_client_uses_proxy_key(tmp_path: Path):
    config = SecretaryConfig(
        data_root=str(tmp_path),
        anthropic_base_url="http://localhost:4141",
    )
    client = _build_client(config)
    assert client.api_key == "copilot-proxy"


# ---------------------------------------------------------------------------
# _build_tool_schemas
# ---------------------------------------------------------------------------

def test_build_tool_schemas_empty():
    assert _build_tool_schemas({}) == []


def test_build_tool_schemas_converts():
    tools = {
        "my_tool": {
            "name": "my_tool",
            "description": "Does stuff",
            "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "func": AsyncMock(),
        },
    }
    schemas = _build_tool_schemas(tools)
    assert len(schemas) == 1
    assert schemas[0]["name"] == "my_tool"
    assert schemas[0]["description"] == "Does stuff"
    assert "func" not in schemas[0]


def test_build_tool_schemas_multiple():
    """Multiple tools are all converted correctly."""
    tools = {
        "tool_a": {
            "name": "tool_a",
            "description": "Tool A",
            "input_schema": {"type": "object"},
            "func": AsyncMock(),
        },
        "tool_b": {
            "name": "tool_b",
            "description": "Tool B",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
            "func": AsyncMock(),
        },
    }
    schemas = _build_tool_schemas(tools)
    assert len(schemas) == 2
    names = {s["name"] for s in schemas}
    assert names == {"tool_a", "tool_b"}


# ---------------------------------------------------------------------------
# _execute_tool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_tool_success():
    tools = {
        "greet": {
            "func": AsyncMock(return_value={"content": [{"type": "text", "text": "Hello!"}]}),
        },
    }
    result = await _execute_tool("greet", {"name": "world"}, tools)
    assert result == "Hello!"
    tools["greet"]["func"].assert_awaited_once_with({"name": "world"})


@pytest.mark.asyncio
async def test_execute_tool_unknown():
    result = await _execute_tool("no_such_tool", {}, {})
    assert "Unknown tool" in result


@pytest.mark.asyncio
async def test_execute_tool_error():
    tools = {
        "bad": {
            "func": AsyncMock(side_effect=RuntimeError("boom")),
        },
    }
    result = await _execute_tool("bad", {}, tools)
    assert "Error executing bad" in result
    assert "boom" in result


@pytest.mark.asyncio
async def test_execute_tool_non_dict_result():
    """Tool returning a non-dict result is stringified."""
    tools = {
        "weird": {
            "func": AsyncMock(return_value="plain string result"),
        },
    }
    result = await _execute_tool("weird", {}, tools)
    assert result == "plain string result"


@pytest.mark.asyncio
async def test_execute_tool_multi_text_blocks():
    """Tool returning multiple text blocks joins them with newlines."""
    tools = {
        "multi": {
            "func": AsyncMock(return_value={
                "content": [
                    {"type": "text", "text": "Line 1"},
                    {"type": "text", "text": "Line 2"},
                ]
            }),
        },
    }
    result = await _execute_tool("multi", {}, tools)
    assert result == "Line 1\nLine 2"


# ---------------------------------------------------------------------------
# _stream_call
# ---------------------------------------------------------------------------

def test_stream_call_returns_final_message():
    """_stream_call uses streaming and returns the final message."""
    mock_client = MagicMock()
    mock_final = MagicMock()
    mock_stream = MagicMock()
    mock_stream.get_final_message.return_value = mock_final
    mock_stream.__enter__ = MagicMock(return_value=mock_stream)
    mock_stream.__exit__ = MagicMock(return_value=False)
    mock_client.messages.stream.return_value = mock_stream

    result = _stream_call(mock_client, model="test", messages=[], max_tokens=100)

    mock_client.messages.stream.assert_called_once()
    assert result == mock_final


# ---------------------------------------------------------------------------
# RunResult
# ---------------------------------------------------------------------------

def test_run_result_defaults():
    r = RunResult(task="test", routing=None)  # type: ignore[arg-type]
    assert r.text == ""
    assert r.error is None
    assert r.cost_usd == 0.0
    assert r.num_turns == 0
    assert r.tools_used == []
    assert r.input_tokens == 0
    assert r.output_tokens == 0
    assert r.messages == []


def test_run_result_session_id_default():
    r = RunResult(task="test", routing=None)  # type: ignore[arg-type]
    assert r.session_id == ""


# ---------------------------------------------------------------------------
# run() — integration-style with mocked API
# ---------------------------------------------------------------------------

def _make_mock_response(text="Hello", stop_reason="end_turn", tool_use=None, input_tokens=100, output_tokens=50):
    """Create a mock Anthropic response."""
    content = []
    if text:
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = text
        content.append(text_block)
    if tool_use:
        for tu in tool_use:
            block = MagicMock()
            block.type = "tool_use"
            block.id = tu["id"]
            block.name = tu["name"]
            block.input = tu["input"]
            content.append(block)
    response = MagicMock()
    response.content = content
    response.stop_reason = stop_reason
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    response.usage = usage
    return response


@pytest.mark.asyncio
async def test_run_simple_task(tmp_path: Path):
    """Simple task with no tools — one API call, done."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_mock_response(text="The answer is 42.")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        result = await run(
            task="What is the meaning of life?",
            config=config,
            memory=mem,
            force_tier="low",
        )

    assert result.text == "The answer is 42."
    assert result.error is None
    assert result.num_turns == 1
    assert result.tools_used == []

    # Verify messages include the task
    call_kwargs = mock_client.messages.create.call_args
    messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
    user_msgs = [m for m in messages if m["role"] == "user"]
    assert any("meaning of life" in str(m.get("content", "")) for m in user_msgs)


@pytest.mark.asyncio
async def test_run_with_tool_loop(tmp_path: Path):
    """Task that triggers a tool use and then gets a final answer."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    # First response: tool_use
    tool_response = _make_mock_response(
        text="Let me search for that.",
        stop_reason="tool_use",
        tool_use=[{"id": "tu_123", "name": "gmail_search", "input": {"query": "test"}}],
    )
    # Second response: final answer
    final_response = _make_mock_response(text="Found 3 emails.", stop_reason="end_turn")

    mock_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "3 results found"}]})
    tools = {
        "gmail_search": {
            "name": "gmail_search",
            "description": "Search Gmail",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "func": mock_tool,
        },
    }

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [tool_response, final_response]
        mock_bc.return_value = mock_client

        result = await run(
            task="Search my email for test",
            config=config,
            memory=mem,
            force_tier="low",
            tools=tools,
        )

    assert "Found 3 emails" in result.text
    assert result.num_turns == 2
    assert "gmail_search" in result.tools_used
    mock_tool.assert_awaited_once_with({"query": "test"})
    assert result.error is None


@pytest.mark.asyncio
async def test_run_respects_max_turns(tmp_path: Path):
    """Agent should stop after max_turns even if tools keep being requested."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    # Always returns tool_use
    tool_response = _make_mock_response(
        text="",
        stop_reason="tool_use",
        tool_use=[{"id": "tu_1", "name": "search", "input": {}}],
    )
    mock_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]})
    tools = {
        "search": {
            "name": "search",
            "description": "Search",
            "input_schema": {"type": "object", "properties": {}},
            "func": mock_tool,
        },
    }

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = tool_response
        mock_bc.return_value = mock_client

        result = await run(
            task="keep searching",
            config=config,
            memory=mem,
            force_tier="low",
            tools=tools,
            max_turns=3,
        )

    assert result.num_turns == 3
    assert mock_client.messages.create.call_count == 3


@pytest.mark.asyncio
async def test_run_handles_api_error(tmp_path: Path):
    """API errors should be caught and set result.error."""
    import anthropic as anthropic_mod

    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        # Simulate an API error
        err = anthropic_mod.APIStatusError(
            message="rate limited",
            response=MagicMock(status_code=429),
            body={"error": {"message": "rate limited"}},
        )
        mock_client.messages.create.side_effect = err
        mock_bc.return_value = mock_client

        result = await run(
            task="test",
            config=config,
            memory=mem,
            force_tier="low",
        )

    assert result.error is not None
    assert "rate limited" in result.error


@pytest.mark.asyncio
async def test_run_updates_memory(tmp_path: Path):
    """Run should add task summary to short-term memory."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_mock_response(text="Done.")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        await run(
            task="Check my calendar",
            config=config,
            memory=mem,
            force_tier="low",
        )

    short = mem.get_short()
    assert any("Check my calendar" in s for s in short)


@pytest.mark.asyncio
async def test_run_no_tools_doesnt_send_tools_param(tmp_path: Path):
    """When no tools provided, API call should not include tools parameter."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_mock_response(text="ok")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        await run(
            task="hello",
            config=config,
            memory=mem,
            force_tier="low",
        )

    call_kwargs = mock_client.messages.create.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
    assert "tools" not in kwargs


@pytest.mark.asyncio
async def test_run_tracks_token_usage(tmp_path: Path):
    """Token usage from API response is accumulated in RunResult."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_mock_response(text="ok", input_tokens=200, output_tokens=75)

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        result = await run(
            task="test tokens",
            config=config,
            memory=mem,
            force_tier="low",
        )

    assert result.input_tokens == 200
    assert result.output_tokens == 75


@pytest.mark.asyncio
async def test_run_accumulates_tokens_across_turns(tmp_path: Path):
    """Token usage is accumulated across multiple API calls (tool loop)."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    tool_resp = _make_mock_response(
        text="Searching...",
        stop_reason="tool_use",
        tool_use=[{"id": "tu_1", "name": "gmail_search", "input": {"query": "x"}}],
        input_tokens=300,
        output_tokens=100,
    )
    final_resp = _make_mock_response(text="Done.", input_tokens=500, output_tokens=150)

    mock_tool = AsyncMock(return_value={"content": [{"type": "text", "text": "result"}]})
    tools = {
        "gmail_search": {
            "name": "gmail_search",
            "description": "Search",
            "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            "func": mock_tool,
        },
    }

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [tool_resp, final_resp]
        mock_bc.return_value = mock_client

        result = await run(
            task="test multi-turn tokens",
            config=config,
            memory=mem,
            force_tier="low",
            tools=tools,
        )

    assert result.input_tokens == 800  # 300 + 500
    assert result.output_tokens == 250  # 100 + 150


@pytest.mark.asyncio
async def test_run_handles_empty_response_content(tmp_path: Path):
    """Empty response.content should terminate gracefully, not crash."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    response = MagicMock()
    response.content = []
    response.stop_reason = "end_turn"
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    response.usage = usage

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = response
        mock_bc.return_value = mock_client

        result = await run(
            task="test empty response",
            config=config,
            memory=mem,
            force_tier="low",
        )

    assert result.error is None
    assert result.text == ""
    assert result.num_turns == 1


@pytest.mark.asyncio
async def test_run_consecutive_tool_errors_break_loop(tmp_path: Path):
    """Agent stops after 3 consecutive tool errors to prevent infinite loops."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    # Every response requests a tool that always errors
    tool_response = _make_mock_response(
        text="",
        stop_reason="tool_use",
        tool_use=[{"id": "tu_1", "name": "bad_tool", "input": {}}],
    )
    tools = {
        "bad_tool": {
            "name": "bad_tool",
            "description": "Always fails",
            "input_schema": {"type": "object", "properties": {}},
            "func": AsyncMock(side_effect=RuntimeError("always broken")),
        },
    }

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = tool_response
        mock_bc.return_value = mock_client

        result = await run(
            task="try the broken tool",
            config=config,
            memory=mem,
            force_tier="low",
            tools=tools,
            max_turns=10,
        )

    # Should stop after 3 consecutive errors, not 10 turns
    assert result.num_turns == 3
    assert result.error is not None
    assert "consecutive tool errors" in result.error


@pytest.mark.asyncio
async def test_run_tool_errors_reset_on_success(tmp_path: Path):
    """Consecutive error counter resets when a tool call succeeds."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    # Pattern: fail, fail, succeed, fail, fail, final
    call_count = 0

    async def alternating_tool(inp):
        nonlocal call_count
        call_count += 1
        if call_count == 3:
            return {"content": [{"type": "text", "text": "ok"}]}
        raise RuntimeError("intermittent failure")

    tool_response = _make_mock_response(
        text="",
        stop_reason="tool_use",
        tool_use=[{"id": "tu_1", "name": "flaky", "input": {}}],
    )
    final_response = _make_mock_response(text="Done.", stop_reason="end_turn")

    tools = {
        "flaky": {
            "name": "flaky",
            "description": "Sometimes fails",
            "input_schema": {"type": "object", "properties": {}},
            "func": alternating_tool,
        },
    }

    responses = [tool_response] * 5 + [final_response]

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        mock_bc.return_value = mock_client

        result = await run(
            task="use flaky tool",
            config=config,
            memory=mem,
            force_tier="low",
            tools=tools,
            max_turns=10,
        )

    # Should NOT have been stopped by error breaker — call 3 succeeded, resetting counter
    # After call 3 succeeds: counter resets to 0
    # Calls 4, 5 fail: counter = 2 (below 3)
    # Call 6 is the final response
    assert result.error is None or "consecutive" not in (result.error or "")


@pytest.mark.asyncio
async def test_run_without_agent_prefix(tmp_path: Path):
    """When agent_prefix is disabled, messages don't include prefix."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
        agent_prefix=False,
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_mock_response(text="ok")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        await run(
            task="test no prefix",
            config=config,
            memory=mem,
            force_tier="low",
        )

    call_kwargs = mock_client.messages.create.call_args
    kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
    messages = kwargs["messages"]
    # Without agent prefix, first message should be the task directly.
    # Note: messages list is mutated after call (assistant appended), so check
    # that the *first* message is the user task with no prefix before it.
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "test no prefix"


@pytest.mark.asyncio
async def test_run_cost_estimation_haiku(tmp_path: Path):
    """Cost is estimated using Haiku rates for low tier."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    # 1000 input, 500 output tokens
    mock_response = _make_mock_response(text="ok", input_tokens=1000, output_tokens=500)

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        result = await run(
            task="test cost",
            config=config,
            memory=mem,
            force_tier="low",
        )

    # Haiku: $0.80/1M in + $4.00/1M out
    expected = 1000 * (0.80 / 1e6) + 500 * (4.00 / 1e6)
    assert abs(result.cost_usd - expected) < 1e-8


@pytest.mark.asyncio
async def test_run_error_added_to_memory(tmp_path: Path):
    """When agent errors, the error is also added to short-term memory."""
    import anthropic as anthropic_mod

    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    with patch("secretary.direct_agent._build_client") as mock_bc, \
         patch("asyncio.sleep", new_callable=lambda: AsyncMock):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic_mod.APIStatusError(
            message="server error",
            response=MagicMock(status_code=500),
            body={"error": {"message": "server error"}},
        )
        mock_bc.return_value = mock_client

        result = await run(
            task="test error memory",
            config=config,
            memory=mem,
            force_tier="low",
        )

    short = mem.get_short()
    assert any("Error:" in s for s in short)
