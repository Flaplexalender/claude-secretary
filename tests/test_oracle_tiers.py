"""Tests: oracle-all-tiers — low/medium/high route through oracle ensemble."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from oracle_integration.billing import (
    tier_to_oracle_config,
    should_route_to_oracle,
    route_task,
)
from secretary.oracle import OracleConfig


# ---------------------------------------------------------------------------
# tier_to_oracle_config() unit tests
# ---------------------------------------------------------------------------

class TestTierToOracleConfig:
    def test_low_returns_oracle_config(self):
        cfg = tier_to_oracle_config("low")
        assert isinstance(cfg, OracleConfig)

    def test_medium_returns_oracle_config(self):
        cfg = tier_to_oracle_config("medium")
        assert isinstance(cfg, OracleConfig)

    def test_high_returns_oracle_config(self):
        cfg = tier_to_oracle_config("high")
        assert isinstance(cfg, OracleConfig)

    def test_low_has_fewest_turns(self):
        low = tier_to_oracle_config("low")
        med = tier_to_oracle_config("medium")
        high = tier_to_oracle_config("high")
        assert low.max_turns <= med.max_turns <= high.max_turns

    def test_high_has_most_checkpoints(self):
        low = tier_to_oracle_config("low")
        high = tier_to_oracle_config("high")
        assert high.max_checkpoints >= low.max_checkpoints

    def test_high_has_shortest_checkpoint_interval(self):
        low = tier_to_oracle_config("low")
        high = tier_to_oracle_config("high")
        assert high.checkpoint_interval <= low.checkpoint_interval

    def test_low_no_escalation(self):
        cfg = tier_to_oracle_config("low")
        assert cfg.escalate_on_disagreement is False

    def test_medium_escalation(self):
        cfg = tier_to_oracle_config("medium")
        assert cfg.escalate_on_disagreement is True

    def test_high_escalation(self):
        cfg = tier_to_oracle_config("high")
        assert cfg.escalate_on_disagreement is True

    def test_deep_alias_same_as_high(self):
        deep = tier_to_oracle_config("deep")
        high = tier_to_oracle_config("high")
        assert deep.max_turns == high.max_turns
        assert deep.checkpoint_interval == high.checkpoint_interval

    def test_unknown_tier_falls_back_to_medium(self):
        unknown = tier_to_oracle_config("banana")
        medium = tier_to_oracle_config("medium")
        assert unknown.max_turns == medium.max_turns

    def test_case_insensitive(self):
        cfg = tier_to_oracle_config("HIGH")
        assert isinstance(cfg, OracleConfig)
        assert cfg.checkpoint_interval == tier_to_oracle_config("high").checkpoint_interval

    def test_all_tiers_have_worker_models(self):
        for tier in ("low", "medium", "high"):
            cfg = tier_to_oracle_config(tier)
            assert len(cfg.worker_models) >= 1


# ---------------------------------------------------------------------------
# should_route_to_oracle()
# ---------------------------------------------------------------------------

class TestShouldRouteToOracle:
    @pytest.mark.parametrize("tier", ["low", "medium", "high", "deep"])
    def test_oracle_tiers_return_true(self, tier):
        assert should_route_to_oracle(tier) is True

    @pytest.mark.parametrize("tier", ["free", "oracle", "unknown", ""])
    def test_non_oracle_tiers_return_false(self, tier):
        assert should_route_to_oracle(tier) is False

    def test_case_insensitive(self):
        assert should_route_to_oracle("LOW") is True
        assert should_route_to_oracle("MEDIUM") is True
        assert should_route_to_oracle("HIGH") is True


# ---------------------------------------------------------------------------
# route_task() — integration: all 3 tiers complete ≥1 task via oracle_run
# ---------------------------------------------------------------------------

def _make_mock_result(tier: str):
    result = MagicMock()
    result.output = f"Task completed via oracle ({tier})"
    result.turns = 1
    result.tier = tier
    return result


class TestRouteTask:
    """Verify low/medium/high all execute through oracle_run (mocked)."""

    @pytest.mark.parametrize("tier", ["low", "medium", "high"])
    def test_tier_routes_to_oracle_run(self, tier):
        mock_result = _make_mock_result(tier)

        with patch(
            "oracle_integration.billing.oracle_run",
            new=AsyncMock(return_value=mock_result),
        ) as mock_oracle:
            result = asyncio.run(
                route_task(task=f"Do a {tier} task", tier=tier)
            )

        # oracle_run must have been called exactly once
        mock_oracle.assert_called_once()
        call_kwargs = mock_oracle.call_args
        # oracle_config must be an OracleConfig matching the tier
        passed_config = call_kwargs.kwargs.get(
            "oracle_config", call_kwargs.args[3] if len(call_kwargs.args) > 3 else None
        )
        assert isinstance(passed_config, OracleConfig)
        assert result.output == f"Task completed via oracle ({tier})"

    def test_all_three_tiers_complete_at_least_one_task(self):
        """Success criterion: low, medium, high each complete ≥1 task via oracle."""
        completed = {}

        for tier in ("low", "medium", "high"):
            mock_result = _make_mock_result(tier)
            with patch(
                "oracle_integration.billing.oracle_run",
                new=AsyncMock(return_value=mock_result),
            ):
                result = asyncio.run(
                    route_task(task=f"Complete a {tier} task", tier=tier)
                )
            completed[tier] = result

        # All three tiers produced a result
        assert len(completed) == 3
        for tier, result in completed.items():
            assert result is not None, f"{tier} tier did not complete a task"
            assert tier in result.output

    def test_max_turns_override_respected(self):
        mock_result = _make_mock_result("medium")
        with patch(
            "oracle_integration.billing.oracle_run",
            new=AsyncMock(return_value=mock_result),
        ) as mock_oracle:
            asyncio.run(
                route_task(task="Quick task", tier="medium", max_turns=3)
            )

        call_kwargs = mock_oracle.call_args
        # max_turns kwarg should be 3
        passed_max = call_kwargs.kwargs.get("max_turns")
        assert passed_max == 3

    @pytest.mark.parametrize("tier", ["low", "medium", "high"])
    def test_oracle_config_passed_to_oracle_run(self, tier):
        """oracle_config built by tier_to_oracle_config is passed to oracle_run."""
        expected_cfg = tier_to_oracle_config(tier)
        mock_result = _make_mock_result(tier)

        with patch(
            "oracle_integration.billing.oracle_run",
            new=AsyncMock(return_value=mock_result),
        ) as mock_oracle:
            asyncio.run(route_task(task="test", tier=tier))

        call_kwargs = mock_oracle.call_args
        passed_config = call_kwargs.kwargs.get("oracle_config")
        assert isinstance(passed_config, OracleConfig)
        assert passed_config.checkpoint_interval == expected_cfg.checkpoint_interval
        assert passed_config.max_turns == expected_cfg.max_turns
