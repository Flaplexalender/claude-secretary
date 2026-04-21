"""Tests for run_log.py — audit(), analyze(), forecast(), seek, rotation."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from secretary.run_log import RunLog, RunLogEntry
from secretary.currency import set_rate, get_rate


def _ts(hours_ago: float = 0, day_offset: int = 0) -> str:
    """Generate ISO timestamp N hours ago and/or N days offset."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago, days=day_offset)
    return dt.isoformat()


def _make_entry(
    task: str = "test task",
    tier: str = "low",
    model: str = "claude-haiku-4.5",
    success: bool = True,
    error: str | None = None,
    premium_cost: float = 0.33,
    cost_usd: float = 0.001,
    num_turns: int = 1,
    tools_used: list | None = None,
    cycle: int = 1,
    hours_ago: float = 0,
    day_offset: int = 0,
    duration_s: float = 2.0,
    output_preview: str | None = None,
) -> RunLogEntry:
    return RunLogEntry(
        timestamp=_ts(hours_ago, day_offset),
        cycle=cycle,
        task=task,
        tier=tier,
        model=model,
        success=success,
        output_preview=output_preview or ("ok" if success else "fail"),
        error=error,
        duration_s=duration_s,
        premium_cost=premium_cost,
        cost_usd=cost_usd,
        num_turns=num_turns,
        tools_used=tools_used or [],
    )


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "run_log.jsonl"


# ══════════════════════════════════════════════════════════════
# audit()
# ══════════════════════════════════════════════════════════════


class TestAudit:
    def test_audit_empty(self, log_path: Path):
        log = RunLog(log_path)
        result = log.audit()
        assert result["downgrades"] == []
        assert result["top_tasks"] == []
        assert result["worst_cycle"] is None

    def test_audit_finds_downgrades(self, log_path: Path):
        """High tier task with short output and no tools → downgrade candidate."""
        log = RunLog(log_path)
        entry = _make_entry(
            task="What is 2+2?",
            tier="high",
            model="claude-opus-4.7",
            premium_cost=3.0,
            num_turns=1,
            output_preview="4",  # very short
        )
        entry.tools_used = []
        log.append(entry)

        result = log.audit()
        assert len(result["downgrades"]) == 1
        assert "low" in result["downgrades"][0]["action"].lower()

    def test_audit_no_false_downgrade_for_complex_task(self, log_path: Path):
        """High tier task with long output + tools should NOT be flagged."""
        log = RunLog(log_path)
        entry = _make_entry(
            task="Deep architecture review",
            tier="high",
            model="claude-opus-4.7",
            premium_cost=3.0,
            num_turns=5,
            output_preview="x" * 200,
            tools_used=["gmail_search"],
        )
        log.append(entry)

        result = log.audit()
        assert len(result["downgrades"]) == 0

    def test_audit_no_downgrade_for_low_tier(self, log_path: Path):
        """Low tier tasks are never flagged for downgrade."""
        log = RunLog(log_path)
        log.append(_make_entry(tier="low", output_preview="4", num_turns=1))

        result = log.audit()
        assert len(result["downgrades"]) == 0

    def test_audit_no_downgrade_for_failed_tasks(self, log_path: Path):
        """Failed tasks should not be flagged for downgrade."""
        log = RunLog(log_path)
        entry = _make_entry(
            tier="high",
            success=False,
            error="fail",
            output_preview="error",
            num_turns=1,
        )
        entry.tools_used = []
        log.append(entry)

        result = log.audit()
        assert len(result["downgrades"]) == 0

    def test_audit_top_tasks(self, log_path: Path):
        """Top 3 costliest tasks are identified."""
        log = RunLog(log_path)
        for task, premium in [
            ("expensive task A", 3.0),
            ("expensive task B", 2.0),
            ("cheap task C", 0.33),
            ("expensive task D", 5.0),
        ]:
            log.append(_make_entry(task=task, premium_cost=premium))

        result = log.audit()
        assert len(result["top_tasks"]) == 3
        # Most expensive first
        assert result["top_tasks"][0]["total_premium"] == 5.0
        assert result["top_tasks"][1]["total_premium"] == 3.0
        assert result["top_tasks"][2]["total_premium"] == 2.0

    def test_audit_worst_cycle(self, log_path: Path):
        """Identifies the cycle with worst pass rate."""
        log = RunLog(log_path)
        # Cycle 1: 2/2 pass
        log.append(_make_entry(cycle=1, success=True))
        log.append(_make_entry(cycle=1, success=True))
        # Cycle 2: 0/2 pass (worst)
        log.append(_make_entry(cycle=2, success=False, error="fail"))
        log.append(_make_entry(cycle=2, success=False, error="fail"))
        # Cycle 3: 1/2 pass
        log.append(_make_entry(cycle=3, success=True))
        log.append(_make_entry(cycle=3, success=False, error="fail"))

        result = log.audit()
        assert result["worst_cycle"] is not None
        assert result["worst_cycle"]["cycle"] == 2
        assert result["worst_cycle"]["pass_rate"] == "0%"

    def test_audit_no_worst_cycle_without_watcher_entries(self, log_path: Path):
        """One-shot tasks (cycle=0) don't create worst_cycle."""
        log = RunLog(log_path)
        log.append(_make_entry(cycle=0, success=False, error="fail"))

        result = log.audit()
        assert result["worst_cycle"] is None

    def test_audit_multiline_output_not_downgraded(self, log_path: Path):
        """High tier task with multiline output (even short) is not flagged."""
        log = RunLog(log_path)
        entry = _make_entry(
            tier="high",
            output_preview="Line 1\nLine 2",
            num_turns=1,
        )
        entry.tools_used = []
        log.append(entry)

        result = log.audit()
        assert len(result["downgrades"]) == 0


# ══════════════════════════════════════════════════════════════
# analyze()
# ══════════════════════════════════════════════════════════════


class TestAnalyze:
    def test_analyze_empty(self, log_path: Path):
        log = RunLog(log_path)
        result = log.analyze()
        assert result["task_reliability"] == []
        assert result["failure_patterns"] == []
        assert result["suggestions"] == []

    def test_analyze_task_reliability(self, log_path: Path):
        """Per-task reliability scoring, sorted by pass rate ascending."""
        log = RunLog(log_path)
        # Task A: 3/4 pass
        for i in range(4):
            log.append(_make_entry(
                task="Task A",
                success=i != 2,
                error="fail" if i == 2 else None,
                duration_s=2.0,
            ))
        # Task B: 1/3 pass (worse)
        for i in range(3):
            log.append(_make_entry(
                task="Task B",
                success=i == 0,
                error="fail" if i != 0 else None,
                duration_s=5.0,
            ))

        result = log.analyze()
        reliability = result["task_reliability"]
        assert len(reliability) == 2
        # Sorted by pass rate ascending — Task B (0.33) first
        assert reliability[0]["task"] == "Task B"
        assert reliability[0]["pass_rate"] < 0.5
        assert reliability[1]["task"] == "Task A"
        assert reliability[1]["pass_rate"] == 0.75

    def test_analyze_failure_patterns(self, log_path: Path):
        """Repeated error patterns are detected and sorted by count."""
        log = RunLog(log_path)
        for _ in range(5):
            log.append(_make_entry(
                success=False,
                error="Connection timed out after 30s",
            ))
        for _ in range(2):
            log.append(_make_entry(
                success=False,
                error="Rate limit exceeded",
            ))

        result = log.analyze()
        patterns = result["failure_patterns"]
        assert len(patterns) == 2
        assert patterns[0]["count"] == 5  # most frequent first
        assert "Connection" in patterns[0]["pattern"]

    def test_analyze_hour_performance(self, log_path: Path):
        """Pass rate is tracked by hour of day."""
        log = RunLog(log_path)
        log.append(_make_entry(success=True, hours_ago=0))
        log.append(_make_entry(success=False, error="fail", hours_ago=0))

        result = log.analyze()
        perf = result["hour_performance"]
        assert len(perf) >= 1
        # Find the hour with 2 entries
        for h, stats in perf.items():
            if stats["total"] == 2:
                assert stats["pass_rate"] == 0.5

    def test_analyze_cycle_trend(self, log_path: Path):
        """Cycle trend tracks passed/failed/premium per cycle."""
        log = RunLog(log_path)
        log.append(_make_entry(cycle=1, success=True, premium_cost=0.33))
        log.append(_make_entry(cycle=1, success=True, premium_cost=0.33))
        log.append(_make_entry(cycle=2, success=True, premium_cost=1.0))
        log.append(_make_entry(cycle=2, success=False, error="x", premium_cost=1.0))

        result = log.analyze()
        trend = result["cycle_trend"]
        assert len(trend) == 2
        assert trend[0]["cycle"] == 1
        assert trend[0]["passed"] == 2
        assert trend[0]["failed"] == 0
        assert trend[1]["cycle"] == 2
        assert trend[1]["passed"] == 1
        assert trend[1]["failed"] == 1

    def test_analyze_suggestions_unreliable_task(self, log_path: Path):
        """Suggestion for tasks with < 50% pass rate over 3+ runs."""
        log = RunLog(log_path)
        for i in range(5):
            log.append(_make_entry(
                task="Unreliable task",
                success=i == 0,
                error="fail" if i != 0 else None,
            ))

        result = log.analyze()
        assert any("Unreliable task" in s for s in result["suggestions"])

    def test_analyze_suggestions_declining_performance(self, log_path: Path):
        """Suggestion when pass rate declines over last 3 cycles."""
        log = RunLog(log_path)
        # Cycle 1: 3/3 pass (100%)
        for _ in range(3):
            log.append(_make_entry(cycle=1, success=True))
        # Cycle 2: 2/3 pass (67%)
        log.append(_make_entry(cycle=2, success=True))
        log.append(_make_entry(cycle=2, success=True))
        log.append(_make_entry(cycle=2, success=False, error="x"))
        # Cycle 3: 1/3 pass (33%)
        log.append(_make_entry(cycle=3, success=True))
        log.append(_make_entry(cycle=3, success=False, error="x"))
        log.append(_make_entry(cycle=3, success=False, error="x"))

        result = log.analyze()
        assert any("declining" in s.lower() for s in result["suggestions"])

    def test_analyze_suggestions_repeated_errors(self, log_path: Path):
        """Suggestion for error patterns occurring 3+ times."""
        log = RunLog(log_path)
        for _ in range(4):
            log.append(_make_entry(
                success=False,
                error="TokenExpiredError: refresh failed",
            ))

        result = log.analyze()
        assert any("TokenExpiredError" in s for s in result["suggestions"])

    def test_analyze_cycle_trend_excludes_oneshot(self, log_path: Path):
        """One-shot tasks (cycle=0) are excluded from cycle trend."""
        log = RunLog(log_path)
        log.append(_make_entry(cycle=0, success=True))
        log.append(_make_entry(cycle=1, success=True))

        result = log.analyze()
        assert len(result["cycle_trend"]) == 1
        assert result["cycle_trend"][0]["cycle"] == 1

    def test_analyze_multi_tier_suggestion(self, log_path: Path):
        """Tasks using multiple tiers get a suggestion."""
        log = RunLog(log_path)
        for tier in ["low", "medium", "high"]:
            log.append(_make_entry(task="Escalating task", tier=tier))

        result = log.analyze()
        assert any("Escalating task" in s for s in result["suggestions"])


# ══════════════════════════════════════════════════════════════
# forecast()
# ══════════════════════════════════════════════════════════════


class TestForecast:
    def test_forecast_empty(self, log_path: Path):
        log = RunLog(log_path)
        result = log.forecast(30)
        assert result["daily_rate_usd"] == 0.0
        assert result["confidence"] == "none"

    def test_forecast_single_entry(self, log_path: Path):
        """Single entry → low confidence."""
        log = RunLog(log_path)
        log.append(_make_entry(cost_usd=0.50, premium_cost=1.0))

        result = log.forecast(30)
        assert result["confidence"] == "low"
        assert result["daily_rate_usd"] == 0.50
        assert result["projected_usd"] == 15.0  # 0.50 * 30

    def test_forecast_multi_day_high_confidence(self, log_path: Path):
        """7+ days of data → high confidence."""
        log = RunLog(log_path)
        for day in range(9):
            log.append(_make_entry(
                cost_usd=1.0,
                premium_cost=2.0,
                day_offset=day,
            ))

        result = log.forecast(30)
        assert result["confidence"] == "high"
        assert result["data_days"] >= 7.0
        # 9 USD over ~9 days ≈ 1 USD/day → ~30 USD over 30 days
        assert 25.0 <= result["projected_usd"] <= 40.0

    def test_forecast_medium_confidence(self, log_path: Path):
        """3-6 days of data → medium confidence."""
        log = RunLog(log_path)
        for day in range(5):
            log.append(_make_entry(cost_usd=0.5, day_offset=day))

        result = log.forecast(30)
        assert result["confidence"] == "medium"

    def test_forecast_includes_cad(self, log_path: Path):
        """Forecast includes CAD conversion."""
        original_rate = get_rate()
        try:
            set_rate(1.44)
            log = RunLog(log_path)
            log.append(_make_entry(cost_usd=1.0, day_offset=0))
            log.append(_make_entry(cost_usd=1.0, day_offset=1))

            result = log.forecast(30)
            assert result["projected_cad"] > result["projected_usd"]
        finally:
            set_rate(original_rate)

    def test_forecast_premium_projection(self, log_path: Path):
        """Premium spend is also projected."""
        log = RunLog(log_path)
        log.append(_make_entry(premium_cost=2.0, cost_usd=0.01, day_offset=0))
        log.append(_make_entry(premium_cost=2.0, cost_usd=0.01, day_offset=1))

        result = log.forecast(30)
        assert result["projected_premium"] > 0
        assert result["daily_rate_premium"] > 0

    def test_forecast_custom_days(self, log_path: Path):
        """Forecast over different time horizons."""
        log = RunLog(log_path)
        log.append(_make_entry(cost_usd=1.0, day_offset=0))
        log.append(_make_entry(cost_usd=1.0, day_offset=1))

        r7 = log.forecast(7)
        r30 = log.forecast(30)
        r90 = log.forecast(90)

        assert r7["projected_usd"] < r30["projected_usd"] < r90["projected_usd"]


# ══════════════════════════════════════════════════════════════
# Seek-based reading
# ══════════════════════════════════════════════════════════════


class TestSeekReading:
    def test_recent_seek_matches_deque(self, log_path: Path):
        """_recent_seek and _recent_deque produce identical results."""
        log = RunLog(log_path)
        for i in range(50):
            log.append(_make_entry(task=f"Task {i}"))

        seek_result = log._recent_seek(10)
        deque_result = log._recent_deque(10)

        assert len(seek_result) == len(deque_result)
        for s, d in zip(seek_result, deque_result):
            assert s.task == d.task

    def test_recent_zero_returns_empty(self, log_path: Path):
        log = RunLog(log_path)
        log.append(_make_entry())
        assert log.recent(0) == []

    def test_recent_more_than_available(self, log_path: Path):
        log = RunLog(log_path)
        for i in range(3):
            log.append(_make_entry(task=f"Task {i}"))
        result = log.recent(100)
        assert len(result) == 3

    def test_recent_preserves_order(self, log_path: Path):
        """Entries should be in chronological order (oldest first)."""
        log = RunLog(log_path)
        for i in range(10):
            log.append(_make_entry(task=f"Task {i}"))

        result = log.recent(10)
        assert result[0].task == "Task 0"
        assert result[-1].task == "Task 9"

    def test_recent_nonexistent_file(self, log_path: Path):
        """Reading from nonexistent file returns empty list."""
        log = RunLog(log_path)
        assert log.recent(10) == []


# ══════════════════════════════════════════════════════════════
# summary()
# ══════════════════════════════════════════════════════════════


class TestSummary:
    def test_summary_empty(self, log_path: Path):
        log = RunLog(log_path)
        result = log.summary()
        assert result["total"] == 0

    def test_summary_with_entries(self, log_path: Path):
        log = RunLog(log_path)
        log.append(_make_entry(tier="low", success=True, premium_cost=0.33, cost_usd=0.001))
        log.append(_make_entry(tier="medium", success=False, error="x", premium_cost=1.0, cost_usd=0.01))

        result = log.summary()
        assert result["total"] == 2
        assert result["passed"] == 1
        assert result["failed"] == 1
        assert result["pass_rate"] == "50%"
        assert result["total_premium"] == 1.33
        assert "low" in result["by_tier"]
        assert "medium" in result["by_tier"]

    def test_summary_cad_conversion(self, log_path: Path):
        original_rate = get_rate()
        try:
            set_rate(1.44)
            log = RunLog(log_path)
            log.append(_make_entry(cost_usd=1.0))

            result = log.summary()
            assert abs(result["total_cost_cad"] - 1.44) < 0.01
        finally:
            set_rate(original_rate)


# ══════════════════════════════════════════════════════════════
# Log rotation
# ══════════════════════════════════════════════════════════════


class TestRotation:
    def test_rotation_creates_archive(self, log_path: Path):
        """When log exceeds max size, it's rotated to .1 archive."""
        log = RunLog(log_path)
        log._MAX_BYTES = 100  # Tiny limit for testing

        for i in range(20):
            log.append(_make_entry(task=f"Task {i}" * 10))

        archive = log_path.with_name(log_path.name + ".1")
        assert archive.exists()

    def test_rotation_preserves_recent_data(self, log_path: Path):
        """After rotation, recent entries are still readable."""
        log = RunLog(log_path)
        log._MAX_BYTES = 500

        for i in range(30):
            log.append(_make_entry(task=f"Task {i}"))

        # Should still be able to read the most recent entries
        result = log.recent(5)
        assert len(result) >= 1


# ══════════════════════════════════════════════════════════════
# RunLogEntry
# ══════════════════════════════════════════════════════════════


class TestRunLogEntry:
    def test_default_tools_used(self):
        entry = RunLogEntry(
            timestamp="2026-01-01T00:00:00Z",
            cycle=0, task="test", tier="low", model="m",
            success=True, output_preview="ok",
        )
        assert entry.tools_used == []

    def test_default_premium_cost(self):
        entry = RunLogEntry(
            timestamp="2026-01-01T00:00:00Z",
            cycle=0, task="test", tier="low", model="m",
            success=True, output_preview="ok",
        )
        assert entry.premium_cost == 0.0

    def test_default_num_turns(self):
        entry = RunLogEntry(
            timestamp="2026-01-01T00:00:00Z",
            cycle=0, task="test", tier="low", model="m",
            success=True, output_preview="ok",
        )
        assert entry.num_turns == 0

    def test_now_is_utc_iso(self):
        ts = RunLog.now()
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None
