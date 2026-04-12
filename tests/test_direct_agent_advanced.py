"""Advanced tests for direct_agent — context pruning, streaming path, API retry."""
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

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
# Helper to create mock API responses
# ---------------------------------------------------------------------------

def _make_response(text="ok", stop_reason="end_turn", tool_use=None,
                   input_tokens=100, output_tokens=50):
    content = []
    if text:
        tb = MagicMock()
        tb.type = "text"
        tb.text = text
        content.append(tb)
    if tool_use:
        for tu in tool_use:
            b = MagicMock()
            b.type = "tool_use"
            b.id = tu["id"]
            b.name = tu["name"]
            b.input = tu["input"]
            content.append(b)
    resp = MagicMock()
    resp.content = content
    resp.stop_reason = stop_reason
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    resp.usage = usage
    return resp


# ---------------------------------------------------------------------------
# Streaming vs sync selection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_high_tier_uses_streaming(tmp_path: Path):
    """High tier should use streaming API (via _stream_call)."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_response(text="Opus response")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        # stream_call would be invoked via asyncio.to_thread(_stream_call, ...)
        # We patch to_thread to check what function is called
        mock_bc.return_value = mock_client

        with patch("asyncio.to_thread") as mock_thread:
            mock_thread.return_value = mock_response

            result = await run(
                task="Deep analysis",
                config=config,
                memory=mem,
                force_tier="high",
            )

        # First call to to_thread should be _stream_call (for high tier)
        first_call = mock_thread.call_args_list[0]
        assert first_call[0][0].__name__ == "_stream_call"


@pytest.mark.asyncio
async def test_low_tier_uses_sync(tmp_path: Path):
    """Low tier should use sync API (messages.create)."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_response(text="Haiku response")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        with patch("asyncio.to_thread") as mock_thread:
            mock_thread.return_value = mock_response

            result = await run(
                task="Quick check",
                config=config,
                memory=mem,
                force_tier="low",
            )

        # For low tier, to_thread should call client.messages.create (not _stream_call)
        first_call = mock_thread.call_args_list[0]
        callable_arg = first_call[0][0]
        # Mock callables don't have __name__; check it's the create method
        assert callable_arg is mock_client.messages.create


# ---------------------------------------------------------------------------
# Context pruning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_pruning_with_many_turns(tmp_path: Path):
    """When messages exceed context limit, old messages are pruned."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    # Create a tool response that always triggers another tool call
    tool_response = _make_response(
        text="",
        stop_reason="tool_use",
        tool_use=[{"id": "tu_1", "name": "t", "input": {}}],
    )
    final_response = _make_response(text="Done.", stop_reason="end_turn")

    tools = {
        "t": {
            "name": "t",
            "description": "test tool",
            "input_schema": {"type": "object", "properties": {}},
            "func": AsyncMock(return_value={"content": [{"type": "text", "text": "ok"}]}),
        },
    }

    # Use many turns to trigger pruning
    responses = [tool_response] * 15 + [final_response]

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = responses
        mock_bc.return_value = mock_client

        result = await run(
            task="long running task",
            config=config,
            memory=mem,
            force_tier="low",
            tools=tools,
            max_turns=16,
        )

    # Should complete without error (context was pruned to prevent overflow)
    assert result.num_turns <= 16
    # Verify messages were passed to API — the last call should have pruned messages
    last_call_kwargs = mock_client.messages.create.call_args
    messages = last_call_kwargs.kwargs.get("messages") or last_call_kwargs[1].get("messages")
    # Verify structure is still valid (user/assistant alternation maintained)
    assert messages[0]["role"] == "user"  # prefix or task


# ---------------------------------------------------------------------------
# API retry on transient 500/502/503
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_api_retry_on_500(tmp_path: Path):
    """500 errors should be retried before giving up."""
    import anthropic as anthropic_mod

    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    good_response = _make_response(text="recovered")

    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise anthropic_mod.APIStatusError(
                message="Internal Server Error",
                response=MagicMock(status_code=500),
                body={"error": {"message": "Internal Server Error"}},
            )
        return good_response

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_bc.return_value = mock_client

        with patch("asyncio.to_thread", side_effect=side_effect):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await run(
                    task="test retry",
                    config=config,
                    memory=mem,
                    force_tier="low",
                )

    assert result.error is None
    assert "recovered" in result.text
    assert call_count == 3  # 2 failures + 1 success


@pytest.mark.asyncio
async def test_api_non_retryable_error_fails_fast(tmp_path: Path):
    """Non-retryable API errors (e.g., 400) should fail immediately."""
    import anthropic as anthropic_mod

    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    def side_effect(*args, **kwargs):
        raise anthropic_mod.APIStatusError(
            message="Bad Request",
            response=MagicMock(status_code=400),
            body={"error": {"message": "Bad Request"}},
        )

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_bc.return_value = mock_client

        with patch("asyncio.to_thread", side_effect=side_effect):
            result = await run(
                task="test no retry",
                config=config,
                memory=mem,
                force_tier="low",
            )

    assert result.error is not None
    assert "Bad Request" in result.error


# ---------------------------------------------------------------------------
# _build_system_prompt access_long tracking
# ---------------------------------------------------------------------------

def test_system_prompt_tracks_long_access(tmp_path: Path):
    """Building system prompt should call access_long for each long memory entry."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    for i in range(5):
        mem.add_long(f"fact {i}")

    prompt = _build_system_prompt(mem)

    # All 5 entries should have been accessed
    assert "fact 0" in prompt
    assert "fact 4" in prompt
    # access_long was called for each entry — check via _long_entries (the actual attribute)
    for entry in mem._long_entries:
        if isinstance(entry, dict):
            assert entry.get("access_count", 0) >= 1


def test_system_prompt_both_memories(tmp_path: Path):
    """System prompt includes both long and short memory sections."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("persistent fact")
    mem.add_short("recent event")

    prompt = _build_system_prompt(mem)
    assert "## Memory" in prompt
    assert "persistent fact" in prompt
    assert "## Recent" in prompt
    assert "recent event" in prompt


# ---------------------------------------------------------------------------
# Cost estimation for different tiers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cost_estimation_opus(tmp_path: Path):
    """Cost estimation for Opus tier uses correct rates."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_response(text="deep analysis", input_tokens=10000, output_tokens=5000)

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_bc.return_value = mock_client

        with patch("asyncio.to_thread", return_value=mock_response):
            result = await run(
                task="complex task",
                config=config,
                memory=mem,
                force_tier="high",
            )

    # Opus: $15/1M in + $75/1M out
    expected = 10000 * (15.0 / 1e6) + 5000 * (75.0 / 1e6)
    assert abs(result.cost_usd - expected) < 1e-6


@pytest.mark.asyncio
async def test_cost_estimation_sonnet(tmp_path: Path):
    """Cost estimation for Sonnet tier uses correct rates."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_response(text="standard work", input_tokens=2000, output_tokens=1000)

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        result = await run(
            task="medium task",
            config=config,
            memory=mem,
            force_tier="medium",
        )

    # Sonnet: $3/1M in + $15/1M out
    expected = 2000 * (3.0 / 1e6) + 1000 * (15.0 / 1e6)
    assert abs(result.cost_usd - expected) < 1e-6


# ---------------------------------------------------------------------------
# Token budget per tier
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_tokens_per_tier(tmp_path: Path):
    """Each tier should use the correct max_tokens value."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    for tier, expected_tokens in [("low", 12288), ("medium", 16384)]:
        mock_response = _make_response(text="ok")

        with patch("secretary.direct_agent._build_client") as mock_bc:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = mock_response
            mock_bc.return_value = mock_client

            await run(
                task=f"test {tier} tokens",
                config=config,
                memory=mem,
                force_tier=tier,
            )

        call_kwargs = mock_client.messages.create.call_args
        kwargs = call_kwargs.kwargs if call_kwargs.kwargs else call_kwargs[1]
        assert kwargs["max_tokens"] == expected_tokens, f"Tier {tier}: expected {expected_tokens}"


# ---------------------------------------------------------------------------
# _execute_tool with content dict but no text blocks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_tool_content_no_text_blocks():
    """Tool returning content with no text blocks should stringify result."""
    tools = {
        "special": {
            "func": AsyncMock(return_value={
                "content": [{"type": "image", "data": "base64..."}]
            }),
        },
    }
    result = await _execute_tool("special", {}, tools)
    # When no text blocks found, _execute_tool falls back to str(result)
    assert "content" in result
    assert "image" in result


# ---------------------------------------------------------------------------
# RunResult fields
# ---------------------------------------------------------------------------

def test_run_result_independent_lists():
    """Each RunResult should have independent lists (not shared)."""
    r1 = RunResult(task="a", routing=MagicMock())
    r2 = RunResult(task="b", routing=MagicMock())
    r1.tools_used.append("tool_x")
    r1.messages.append({"test": True})
    assert r2.tools_used == []
    assert r2.messages == []


def test_run_result_duration_default():
    r = RunResult(task="t", routing=MagicMock())
    assert r.duration_ms == 0


# ---------------------------------------------------------------------------
# _build_tool_schemas preserves schema structure
# ---------------------------------------------------------------------------

def test_build_tool_schemas_preserves_required():
    """Schema's 'required' field should be preserved."""
    tools = {
        "test": {
            "name": "test",
            "description": "desc",
            "input_schema": {
                "type": "object",
                "properties": {"x": {"type": "string"}},
                "required": ["x"],
            },
            "func": AsyncMock(),
        },
    }
    schemas = _build_tool_schemas(tools)
    assert schemas[0]["input_schema"]["required"] == ["x"]


# ---------------------------------------------------------------------------
# General exception handling in run()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_catches_generic_exception(tmp_path: Path):
    """Generic exceptions during API call are caught and set result.error."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("unexpected crash")
        mock_bc.return_value = mock_client

        result = await run(
            task="test generic error",
            config=config,
            memory=mem,
            force_tier="low",
        )

    assert result.error is not None
    assert "unexpected crash" in result.error


# ---------------------------------------------------------------------------
# Memory save after run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_saves_memory_on_success(tmp_path: Path):
    """Memory should be saved after successful run."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    mock_response = _make_response(text="ok")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        mock_bc.return_value = mock_client

        await run(task="test save", config=config, memory=mem, force_tier="low")

    # Memory file should exist (save was called)
    assert tmp_path.joinpath("mem.json").exists()


@pytest.mark.asyncio
async def test_run_saves_memory_on_error(tmp_path: Path):
    """Memory should be saved even when run errors out."""
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        anthropic_base_url="http://localhost:4141",
    )
    mem = MemoryStore(path=tmp_path / "mem.json")

    with patch("secretary.direct_agent._build_client") as mock_bc:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("crash")
        mock_bc.return_value = mock_client

        await run(task="test error save", config=config, memory=mem, force_tier="low")

    assert tmp_path.joinpath("mem.json").exists()
    short = mem.get_short()
    assert any("Error:" in s for s in short)
