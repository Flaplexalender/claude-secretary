"""Tests for agent.py RunResult — ensure API compatibility with direct_agent.py.

Bug fix verification: agent.py's RunResult must have input_tokens and
output_tokens fields so _cmd_run in __main__.py works for both --sdk
(agent.py) and default (direct_agent.py) code paths.
"""
from __future__ import annotations

from secretary.agent import RunResult as SdkRunResult
from secretary.direct_agent import RunResult as DirectRunResult
from secretary.router import RoutingDecision


class TestRunResultApiCompat:
    """Both RunResult classes must have the same public fields used by __main__.py."""

    def _make_routing(self) -> RoutingDecision:
        return RoutingDecision(
            tier="medium",
            model="claude-3-5-sonnet-20241022",
            max_turns=25,
            max_budget_usd=0.0,
            reason="test",
        )

    def test_sdk_result_has_input_tokens(self):
        """Bug fix: agent.py's RunResult must have input_tokens."""
        result = SdkRunResult(task="test", routing=self._make_routing())
        assert hasattr(result, "input_tokens")
        assert result.input_tokens == 0

    def test_sdk_result_has_output_tokens(self):
        """Bug fix: agent.py's RunResult must have output_tokens."""
        result = SdkRunResult(task="test", routing=self._make_routing())
        assert hasattr(result, "output_tokens")
        assert result.output_tokens == 0

    def test_direct_result_has_input_tokens(self):
        result = DirectRunResult(task="test", routing=self._make_routing())
        assert hasattr(result, "input_tokens")
        assert result.input_tokens == 0

    def test_direct_result_has_output_tokens(self):
        result = DirectRunResult(task="test", routing=self._make_routing())
        assert hasattr(result, "output_tokens")
        assert result.output_tokens == 0

    def test_common_fields_match(self):
        """All fields used by _cmd_run must exist on both RunResult classes."""
        # Fields accessed in __main__.py _cmd_run:
        required_fields = [
            "task", "routing", "text", "error", "cost_usd",
            "num_turns", "tools_used", "input_tokens", "output_tokens",
        ]
        sdk = SdkRunResult(task="t", routing=self._make_routing())
        direct = DirectRunResult(task="t", routing=self._make_routing())
        for field in required_fields:
            assert hasattr(sdk, field), f"SdkRunResult missing field: {field}"
            assert hasattr(direct, field), f"DirectRunResult missing field: {field}"

    def test_sdk_result_default_values(self):
        """RunResult should have sensible defaults for all fields."""
        result = SdkRunResult(task="test", routing=self._make_routing())
        assert result.text == ""
        assert result.error is None
        assert result.cost_usd == 0.0
        assert result.num_turns == 0
        assert result.duration_ms == 0
        assert result.session_id == ""
        assert result.tools_used == []
        assert result.input_tokens == 0
        assert result.output_tokens == 0
        assert result.messages == []


class TestInExecutorDeprecation:
    """Verify _in_executor uses get_running_loop() not deprecated get_event_loop()."""

    def test_in_executor_uses_running_loop(self):
        """_in_executor should call asyncio.get_running_loop(), not get_event_loop()."""
        import asyncio
        import inspect
        from secretary._tool_helpers import _in_executor

        source = inspect.getsource(_in_executor)
        assert "get_running_loop" in source, (
            "_in_executor should use asyncio.get_running_loop() "
            "instead of the deprecated asyncio.get_event_loop()"
        )
        # Make sure the old deprecated call isn't there
        assert "get_event_loop" not in source

    def test_in_executor_works_in_running_loop(self):
        """_in_executor should work when called from a running event loop."""
        import asyncio
        from secretary._tool_helpers import _in_executor

        async def _test():
            result = await _in_executor(lambda: 42)
            return result

        assert asyncio.run(_test()) == 42
