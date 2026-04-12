"""Tests for learned router — Bayesian bandit adaptive routing."""
import json
import random
from pathlib import Path
from unittest.mock import patch

import pytest

from secretary.learned_router import (
    TierStats,
    RoutingStats,
    LearnedRoutingDecision,
    extract_category,
    build_stats_from_log,
    learned_route,
    save_stats,
    load_stats,
    TIER_ORDER,
    _MIN_OBSERVATIONS,
    _QUALITY_THRESHOLD,
)
from secretary.run_log import RunLog, RunLogEntry
from secretary.config import SecretaryConfig
from secretary.router import select_model


# ---------- TierStats ----------

class TestTierStats:
    def test_default_values(self):
        ts = TierStats()
        assert ts.successes == 0
        assert ts.failures == 0
        assert ts.total == 0

    def test_total(self):
        ts = TierStats(successes=5, failures=3)
        assert ts.total == 8

    def test_success_rate_empty(self):
        ts = TierStats()
        # Beta(1,1) mean = 0.5 — uninformative prior
        assert ts.success_rate == pytest.approx(0.5)

    def test_success_rate_all_success(self):
        ts = TierStats(successes=10, failures=0)
        # Beta(11,1) mean = 11/12
        assert ts.success_rate == pytest.approx(11 / 12)

    def test_success_rate_all_failure(self):
        ts = TierStats(successes=0, failures=10)
        # Beta(1,11) mean = 1/12
        assert ts.success_rate == pytest.approx(1 / 12)

    def test_success_rate_mixed(self):
        ts = TierStats(successes=7, failures=3)
        # Beta(8,4) mean = 8/12
        assert ts.success_rate == pytest.approx(8 / 12)

    def test_avg_turns(self):
        ts = TierStats(successes=3, failures=1, total_turns=20)
        assert ts.avg_turns == pytest.approx(5.0)

    def test_avg_turns_zero(self):
        ts = TierStats()
        assert ts.avg_turns == pytest.approx(0.0)

    def test_thompson_sample_range(self):
        ts = TierStats(successes=5, failures=5)
        samples = [ts.thompson_sample() for _ in range(100)]
        assert all(0 <= s <= 1 for s in samples)

    def test_has_enough_data_false(self):
        ts = TierStats(successes=1, failures=0)
        assert not ts.has_enough_data()

    def test_has_enough_data_true(self):
        ts = TierStats(successes=2, failures=1)
        assert ts.has_enough_data()


# ---------- extract_category ----------

class TestExtractCategory:
    def test_email_campaign(self):
        assert extract_category("anything", campaign="gmail-monitor") == "email"

    def test_calendar_campaign(self):
        assert extract_category("anything", campaign="calendar-sync") == "calendar"

    def test_improve_campaign(self):
        assert extract_category("anything", campaign="self-improve-v2") == "code"

    def test_health_campaign(self):
        assert extract_category("anything", campaign="health-check") == "health"

    def test_research_campaign(self):
        assert extract_category("anything", campaign="autoresearch") == "research"

    def test_ooda_source(self):
        assert extract_category("handle new event", source="ooda") == "reactive"

    def test_goals_source(self):
        assert extract_category("advance goal", source="goals") == "goal-task"

    def test_email_keywords(self):
        assert extract_category("Check inbox for new emails and draft replies") == "email"

    def test_code_keywords(self):
        assert extract_category("Implement the retry mechanism and fix the parser bug") == "code"

    def test_calendar_keywords(self):
        assert extract_category("Schedule a meeting for next Tuesday") == "calendar"

    def test_file_keywords(self):
        assert extract_category("Read the config file and search for patterns") == "file-ops"

    def test_health_keywords(self):
        assert extract_category("Check system health and verify status") == "health"

    def test_memory_keywords(self):
        assert extract_category("Consolidate memory notes from last session") == "memory"

    def test_general_fallback(self):
        assert extract_category("do the thing now") == "general"

    def test_campaign_overrides_keywords(self):
        # Campaign name takes priority over task text keywords
        assert extract_category("implement code fix", campaign="gmail-inbox") == "email"


# ---------- build_stats_from_log ----------

class TestBuildStatsFromLog:
    def _make_entries(self, specs: list[tuple[str, str, bool, int]]) -> list[RunLogEntry]:
        """Create RunLogEntry list from (task, tier, success, turns) tuples."""
        entries = []
        for i, (task, tier, success, turns) in enumerate(specs):
            entries.append(RunLogEntry(
                timestamp=f"2026-03-18T00:00:{i:02d}Z",
                cycle=1,
                task=task,
                tier=tier,
                model=f"model-{tier}",
                success=success,
                output_preview="ok",
                num_turns=turns,
                cost_usd=0.01,
            ))
        return entries

    def test_empty_log(self, tmp_path):
        rl = RunLog(tmp_path / "test.jsonl")
        stats = build_stats_from_log(rl)
        assert stats.total_entries_processed == 0
        assert len(stats.stats) == 0

    def test_basic_stats(self, tmp_path):
        rl = RunLog(tmp_path / "test.jsonl")
        entries = self._make_entries([
            ("Check inbox for emails", "low", True, 2),
            ("Check inbox for emails", "low", True, 3),
            ("Check inbox for emails", "low", False, 5),
        ])
        for e in entries:
            rl.append(e)

        stats = build_stats_from_log(rl)
        assert stats.total_entries_processed == 3
        # "email" category, "low" tier
        email_low = stats.stats["email"]["low"]
        assert email_low.successes == 2
        assert email_low.failures == 1
        assert email_low.total_turns == 10

    def test_skips_oracle_deep_tiers(self, tmp_path):
        rl = RunLog(tmp_path / "test.jsonl")
        entries = self._make_entries([
            ("research topic", "oracle", True, 5),
            ("deep analysis", "deep", True, 100),
        ])
        for e in entries:
            rl.append(e)

        stats = build_stats_from_log(rl)
        assert stats.total_entries_processed == 2
        # Neither oracle nor deep should be in stats
        for category in stats.stats:
            assert "oracle" not in stats.stats[category]
            assert "deep" not in stats.stats[category]

    def test_multiple_categories(self, tmp_path):
        rl = RunLog(tmp_path / "test.jsonl")
        entries = self._make_entries([
            ("Check email inbox", "low", True, 2),
            ("Implement retry logic", "medium", True, 5),
            ("Implement retry logic", "medium", False, 8),
        ])
        for e in entries:
            rl.append(e)

        stats = build_stats_from_log(rl)
        assert "email" in stats.stats
        assert "code" in stats.stats


# ---------- learned_route ----------

class TestLearnedRoute:
    def _stats_with_data(self, category: str, tier_data: dict[str, tuple[int, int]]) -> RoutingStats:
        """Create RoutingStats with pre-set success/failure counts.

        tier_data: {tier: (successes, failures)}
        """
        stats = RoutingStats()
        for tier, (succ, fail) in tier_data.items():
            stats.stats[category][tier] = TierStats(successes=succ, failures=fail)
        return stats

    def test_insufficient_data(self):
        stats = RoutingStats()
        result = learned_route(stats, "check email", ["low", "medium", "high"])
        assert result.recommended_tier is None
        assert result.confidence == "insufficient"

    def test_insufficient_data_low_counts(self):
        stats = self._stats_with_data("email", {"low": (1, 0)})  # only 1 observation
        result = learned_route(stats, "Check email inbox", ["low", "medium", "high"])
        assert result.recommended_tier is None
        assert result.confidence == "insufficient"

    def test_routes_to_cheapest_good_tier(self):
        # Low tier has great success rate — should route there
        stats = self._stats_with_data("email", {
            "low": (9, 1),   # 90%+ success
            "medium": (8, 2),  # 80% success
        })
        # Force no exploration (deterministic)
        result = learned_route(
            stats, "Check email inbox", ["low", "medium", "high"],
            explore_prob=0.0, quality_threshold=0.5,
        )
        assert result.recommended_tier == "low"
        assert result.confidence == "learned"

    def test_upgrades_when_cheap_fails(self):
        # Low tier always fails — should upgrade to medium
        stats = self._stats_with_data("code", {
            "low": (0, 10),    # 0% success
            "medium": (9, 1),  # 90% success
        })
        result = learned_route(
            stats, "Implement retry logic", ["low", "medium", "high"],
            explore_prob=0.0, quality_threshold=0.7,
        )
        assert result.recommended_tier == "medium"

    def test_exploration_mode(self):
        stats = self._stats_with_data("email", {
            "low": (9, 1),
            "medium": (9, 1),
        })
        # Force exploration
        result = learned_route(
            stats, "Check email inbox", ["low", "medium", "high"],
            explore_prob=1.0,
        )
        assert result.recommended_tier in ("low", "medium", "high")
        assert result.confidence == "explored"

    def test_no_available_tiers(self):
        stats = RoutingStats()
        result = learned_route(stats, "anything", [])
        assert result.recommended_tier is None
        assert result.confidence == "insufficient"

    def test_best_fallback_when_no_threshold_met(self):
        # Both tiers have mediocre success — but forced threshold very high
        stats = self._stats_with_data("code", {
            "low": (3, 4),    # ~43% success
            "medium": (4, 3), # ~57% success
        })
        result = learned_route(
            stats, "Implement feature", ["low", "medium", "high"],
            explore_prob=0.0, quality_threshold=0.99,  # impossibly high
        )
        # Should fall back to best available — medium has higher success
        assert result.recommended_tier == "medium"
        assert result.confidence == "learned"

    def test_respects_tier_order(self):
        # Both tiers are good — should pick cheapest (low before medium)
        # Seed RNG so Thompson sampling is deterministic
        random.seed(42)
        stats = self._stats_with_data("email", {
            "low": (10, 0),    # ~perfect
            "medium": (10, 0), # ~perfect
        })
        result = learned_route(
            stats, "Check email inbox", ["low", "medium", "high"],
            explore_prob=0.0, quality_threshold=0.5,
        )
        assert result.recommended_tier == "low"

    def test_category_extraction_in_route(self):
        stats = self._stats_with_data("email", {"low": (10, 0)})
        result = learned_route(
            stats, "Check email inbox", ["low", "medium"],
            explore_prob=0.0, quality_threshold=0.5,
        )
        assert result.category == "email"

    def test_stats_summary_included(self):
        stats = self._stats_with_data("email", {"low": (5, 1)})
        result = learned_route(
            stats, "Check email inbox", ["low", "medium"],
            explore_prob=0.0, quality_threshold=0.5,
        )
        assert "low" in result.stats_summary
        assert "success_rate" in result.stats_summary["low"]
        assert "n" in result.stats_summary["low"]


# ---------- Persistence ----------

class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        stats = RoutingStats(total_entries_processed=42)
        stats.stats["email"]["low"] = TierStats(successes=5, failures=2, total_turns=21, total_cost_usd=0.07)
        stats.stats["code"]["medium"] = TierStats(successes=8, failures=1, total_turns=45, total_cost_usd=0.30)

        path = tmp_path / "routing_stats.json"
        save_stats(stats, path)

        loaded = load_stats(path)
        assert loaded is not None
        assert loaded.total_entries_processed == 42
        assert loaded.stats["email"]["low"].successes == 5
        assert loaded.stats["email"]["low"].failures == 2
        assert loaded.stats["code"]["medium"].total_turns == 45

    def test_load_missing_file(self, tmp_path):
        result = load_stats(tmp_path / "nonexistent.json")
        assert result is None

    def test_load_corrupted_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        result = load_stats(path)
        assert result is None

    def test_save_atomic(self, tmp_path):
        """Save should not leave partial files on crash."""
        stats = RoutingStats(total_entries_processed=1)
        stats.stats["x"]["low"] = TierStats(successes=1)
        path = tmp_path / "stats.json"
        save_stats(stats, path)
        # File should be valid JSON
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["total_entries_processed"] == 1


# ---------- Integration with router.select_model ----------

class TestSelectModelIntegration:
    def test_learned_router_inactive_in_free_mode(self):
        """When agent_prefix=true and always_opus, learned router is bypassed."""
        config = SecretaryConfig()
        config.agent_prefix = True
        config.optimizations.always_opus = True

        stats = RoutingStats()
        stats.stats["email"]["low"] = TierStats(successes=10, failures=0)

        decision = select_model(config, "Check email inbox", learned_stats=stats)
        assert decision.tier == "high"  # always_opus

    def test_learned_router_used_in_paid_mode(self):
        """In paid mode, learned router overrides static scoring."""
        config = SecretaryConfig()
        config.agent_prefix = False

        from secretary.learned_router import RoutingStats as RS
        stats = RS()
        # email category: low tier is excellent
        stats.stats["email"]["low"] = TierStats(successes=10, failures=0)

        # Run multiple times to handle Thompson sampling stochasticity
        tiers_seen = set()
        for _ in range(20):
            decision = select_model(
                config, "Check email inbox for new messages",
                learned_stats=stats,
            )
            tiers_seen.add(decision.tier)

        # With 10/0 success on low, Thompson sampling should almost always pick low
        # (may occasionally explore or use free-model routing)
        assert "low" in tiers_seen or "free" in tiers_seen

    def test_forced_tier_overrides_learned_router(self):
        """Forced tier always wins over learned router."""
        config = SecretaryConfig()
        config.agent_prefix = False

        stats = RoutingStats()
        stats.stats["email"]["low"] = TierStats(successes=10, failures=0)

        decision = select_model(
            config, "Check email inbox",
            force_tier="high",
            learned_stats=stats,
        )
        assert decision.tier == "high"

    def test_no_learned_stats_falls_through(self):
        """When no stats provided, static scoring is used normally."""
        config = SecretaryConfig()
        config.agent_prefix = False

        decision = select_model(config, "Check email inbox")
        # Static scoring should work without learned_stats
        assert decision.tier in ("free", "low", "medium", "high")

    def test_backward_compatible_no_kwargs(self):
        """select_model still works with old 3-arg signature."""
        config = SecretaryConfig()
        decision = select_model(config, "Fix a typo")
        assert decision.tier in ("free", "low", "medium", "high")


# ---------- Thompson Sampling statistical properties ----------

class TestThompsonSampling:
    def test_high_success_samples_high(self):
        """A tier with 95% success should sample > 0.5 most of the time."""
        ts = TierStats(successes=19, failures=1)
        random.seed(42)
        samples = [ts.thompson_sample() for _ in range(100)]
        high_samples = sum(1 for s in samples if s > 0.5)
        assert high_samples > 80  # should be almost all

    def test_low_success_samples_low(self):
        """A tier with 5% success should sample < 0.5 most of the time."""
        ts = TierStats(successes=1, failures=19)
        random.seed(42)
        samples = [ts.thompson_sample() for _ in range(100)]
        low_samples = sum(1 for s in samples if s < 0.5)
        assert low_samples > 80

    def test_uninformative_prior_spreads(self):
        """Empty TierStats should have high variance (exploration)."""
        ts = TierStats()
        random.seed(42)
        samples = [ts.thompson_sample() for _ in range(100)]
        # Should cover a wide range
        assert min(samples) < 0.3
        assert max(samples) > 0.7


# ---------- Edge cases ----------

class TestEdgeCases:
    def test_category_with_empty_task(self):
        cat = extract_category("")
        assert cat == "general"

    def test_learned_route_single_tier(self):
        stats = RoutingStats()
        stats.stats["email"]["low"] = TierStats(successes=10, failures=0)
        result = learned_route(
            stats, "Check email", ["low"],
            explore_prob=0.0, quality_threshold=0.5,
        )
        assert result.recommended_tier == "low"

    def test_stats_defaultdict_works(self):
        """RoutingStats.stats should auto-create nested dicts."""
        stats = RoutingStats()
        stats.stats["new_cat"]["new_tier"].successes = 5
        assert stats.stats["new_cat"]["new_tier"].successes == 5
