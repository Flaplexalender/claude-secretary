"""Regression test for oracle's reasoning_effort payload construction.

Bug (observed 2026-04-20 in data/run_log.jsonl):
  ``API error: 400 {"error":{"message":"reasoning_effort \"high\" is not
  supported by model claude-opus-4.7; supported values: [medium]"}}``

Root cause: ``oracle._query_checkpoint`` hardcoded
``reasoning_effort=config.reasoning_effort or "high"`` and passed it to
Opus 4.7, which only accepts ``"medium"``.

Fix: clamp via ``_clamp_reasoning_effort`` at the call sites in
``oracle_run`` AND omit the key from the payload when the clamp returns
``None`` (model rejects reasoning_effort entirely — Haiku 4.5).
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from secretary import oracle


def _fake_openai_response():
    """Minimal fake response matching _parse_openai_response's expected shape."""
    resp = MagicMock()
    resp.content = []
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock(input_tokens=0, output_tokens=0)
    return resp


@pytest.mark.asyncio
async def test_query_checkpoint_includes_reasoning_when_set():
    """When reasoning_effort is a truthy string, payload must include it."""
    captured: dict = {}

    def fake_stream(base_url, payload):
        captured.update(payload)
        return None  # parsed separately

    with patch.object(oracle, "_openai_stream_call", side_effect=fake_stream), \
         patch.object(oracle, "_parse_openai_response", return_value=_fake_openai_response()):
        await oracle._query_checkpoint(
            base_url="http://localhost:4141",
            model="claude-opus-4.7",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            oai_tools=[],
            task="t",
            turn_history_summary="",
            reasoning_effort="medium",
        )

    assert captured.get("reasoning_effort") == "medium"
    assert captured["model"] == "claude-opus-4.7"


@pytest.mark.asyncio
async def test_query_checkpoint_omits_reasoning_when_none():
    """When reasoning_effort is None (Haiku-style cap), key must be absent.

    Passing ``reasoning_effort: None`` through the proxy would otherwise
    trigger HTTP 400 on models that reject the parameter entirely.
    """
    captured: dict = {}

    def fake_stream(base_url, payload):
        captured.update(payload)
        return None

    with patch.object(oracle, "_openai_stream_call", side_effect=fake_stream), \
         patch.object(oracle, "_parse_openai_response", return_value=_fake_openai_response()):
        await oracle._query_checkpoint(
            base_url="http://localhost:4141",
            model="claude-haiku-4.5",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            oai_tools=[],
            task="t",
            turn_history_summary="",
            reasoning_effort=None,
        )

    assert "reasoning_effort" not in captured


@pytest.mark.asyncio
async def test_query_checkpoint_omits_reasoning_when_empty_string():
    """Empty-string reasoning_effort also omits the key (no proxy 400)."""
    captured: dict = {}

    def fake_stream(base_url, payload):
        captured.update(payload)
        return None

    with patch.object(oracle, "_openai_stream_call", side_effect=fake_stream), \
         patch.object(oracle, "_parse_openai_response", return_value=_fake_openai_response()):
        await oracle._query_checkpoint(
            base_url="http://localhost:4141",
            model="claude-haiku-4.5",
            system_prompt="sys",
            messages=[{"role": "user", "content": "hi"}],
            oai_tools=[],
            task="t",
            turn_history_summary="",
            reasoning_effort="",
        )

    assert "reasoning_effort" not in captured


def test_clamp_is_imported_at_module_level():
    """_clamp_reasoning_effort must be importable from oracle so oracle_run
    can clamp before calling _query_checkpoint. If this import breaks,
    the clamp-at-call-site fix is dead code."""
    from secretary.oracle import _clamp_reasoning_effort as clamp
    # Haiku rejects all — returns None.
    assert clamp("claude-haiku-4.5", "high") is None
    # Opus 4.7 caps at medium — returns "medium" even when "high" requested.
    assert clamp("claude-opus-4.7", "high") == "medium"
