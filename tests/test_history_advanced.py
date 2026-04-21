"""Tests for history.py — format_history, format_history_json, query_history."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from secretary.history import (
    compute_stats,
    query_history,
    format_history,
    format_history_json,
    HistoryStats,
    TierStats,
    HistoryResult,
    _format_timestamp,
)
from secretary.run_log import RunLog, RunLogEntry


def _ts(hours_ago: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.isoformat()


def _entry(
    task: str = "test task",
    tier: str = "low",
    success: bool = True,
    error: str | None = None,
    premium_cost: float = 0.33,
    cost_usd: float = 0.001,
    duration_s: float = 2.0,
    hours_ago: float = 0,
) -> RunLogEntry:
    return RunLogEntry(
        timestamp=_ts(hours_ago),
        cycle=0,
        task=task,
        tier=tier,
        model="claude-haiku-4.5",
        success=success,
        output_preview="ok" if success else "fail",
        error=error,
        duration_s=duration_s,
        premium_cost=premium_cost,
        cost_usd=cost_usd,
    )


# ══════════════════════════════════════════════════════════════
# compute_stats
# ══════════════════════════════════════════════════════════════


class TestComputeStats:
    def test_empty(self):
        stats = compute_stats([])
        assert stats.total_runs == 0
        assert stats.success_rate == "0%"
        assert stats.total_premium == 0.0
        assert stats.by_tier == {}

    def test_basic(self):
        entries = [
            _entry(success=True, premium_cost=0.33, cost_usd=0.001),
            _entry(success=True, premium_cost=0.33, cost_usd=0.001),
            _entry(success=False, error="x", premium_cost=1.0, cost_usd=0.01),
        ]
        stats = compute_stats(entries)
        assert stats.total_runs == 3
        assert stats.successful == 2
        assert stats.failed == 1
        assert stats.success_rate == "67%"
        assert stats.total_premium == 1.66

    def test_by_tier(self):
        entries = [
            _entry(tier="low", premium_cost=0.33),
            _entry(tier="low", premium_cost=0.33),
            _entry(tier="high", premium_cost=3.0),
        ]
        stats = compute_stats(entries)
        assert "low" in stats.by_tier
        assert "high" in stats.by_tier
        assert stats.by_tier["low"].count == 2
        assert stats.by_tier["high"].count == 1
        assert stats.by_tier["high"].total_premium == 3.0

    def test_avg_duration(self):
        entries = [
            _entry(duration_s=2.0),
            _entry(duration_s=4.0),
        ]
        stats = compute_stats(entries)
        assert stats.avg_duration_s == 3.0

    def test_all_successful(self):
        entries = [_entry(success=True)] * 5
        stats = compute_stats(entries)
        assert stats.success_rate == "100%"
        assert stats.failed == 0

    def test_all_failed(self):
        entries = [_entry(success=False, error="x")] * 3
        stats = compute_stats(entries)
        assert stats.success_rate == "0%"
        assert stats.successful == 0


# ══════════════════════════════════════════════════════════════
# query_history
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def populated_log(tmp_path: Path) -> RunLog:
    log = RunLog(tmp_path / "run_log.jsonl")
    log.append(RunLogEntry(
        timestamp=_ts(5), cycle=0, task="Check email",
        tier="low", model="claude-haiku-4.5", success=True,
        output_preview="ok", duration_s=1.0, premium_cost=0.33, cost_usd=0.001,
    ))
    log.append(RunLogEntry(
        timestamp=_ts(4), cycle=0, task="Write report",
        tier="medium", model="claude-sonnet-4.6", success=True,
        output_preview="ok", duration_s=5.0, premium_cost=1.0, cost_usd=0.01,
    ))
    log.append(RunLogEntry(
        timestamp=_ts(3), cycle=0, task="Deploy code",
        tier="high", model="claude-opus-4.7", success=False,
        output_preview="fail", error="timeout", duration_s=10.0,
        premium_cost=3.0, cost_usd=0.05,
    ))
    log.append(RunLogEntry(
        timestamp=_ts(2), cycle=0, task="Check calendar",
        tier="low", model="claude-haiku-4.5", success=True,
        output_preview="ok", duration_s=1.5, premium_cost=0.33, cost_usd=0.001,
    ))
    log.append(RunLogEntry(
        timestamp=_ts(1), cycle=0, task="Review PR",
        tier="medium", model="claude-sonnet-4.6", success=True,
        output_preview="ok", duration_s=8.0, premium_cost=1.0, cost_usd=0.02,
    ))
    return log


class TestQueryHistory:
    def test_no_filters(self, populated_log: RunLog):
        result = query_history(populated_log, last=10)
        assert len(result.entries) == 5
        assert result.stats.total_runs == 5

    def test_filter_tier(self, populated_log: RunLog):
        result = query_history(populated_log, tier="low", last=10)
        assert len(result.entries) == 2
        assert all(e.tier == "low" for e in result.entries)

    def test_filter_failed(self, populated_log: RunLog):
        result = query_history(populated_log, failed_only=True, last=10)
        assert len(result.entries) == 1
        assert not result.entries[0].success

    def test_filter_search(self, populated_log: RunLog):
        result = query_history(populated_log, search="email", last=10)
        assert len(result.entries) == 1
        assert "email" in result.entries[0].task.lower()

    def test_last_limits(self, populated_log: RunLog):
        result = query_history(populated_log, last=2)
        assert len(result.entries) == 2

    def test_combined_filters(self, populated_log: RunLog):
        """Tier + failed_only combined."""
        result = query_history(populated_log, tier="high", failed_only=True, last=10)
        assert len(result.entries) == 1
        assert result.entries[0].tier == "high"
        assert not result.entries[0].success

    def test_no_match(self, populated_log: RunLog):
        result = query_history(populated_log, search="nonexistent", last=10)
        assert len(result.entries) == 0

    def test_filters_applied_tracking(self, populated_log: RunLog):
        result = query_history(populated_log, tier="low", failed_only=True, last=5)
        assert result.filters_applied["tier"] == "low"
        assert result.filters_applied["failed_only"] is True

    def test_stats_computed_on_filtered_set(self, populated_log: RunLog):
        """Stats are computed on filtered entries, not all entries."""
        result = query_history(populated_log, tier="low", last=10)
        assert result.stats.total_runs == 2
        assert result.stats.successful == 2
        assert result.stats.failed == 0


# ══════════════════════════════════════════════════════════════
# format_history
# ══════════════════════════════════════════════════════════════


class TestFormatHistory:
    def test_empty(self):
        result = HistoryResult(
            entries=[],
            stats=compute_stats([]),
            filters_applied={},
        )
        text = format_history(result)
        assert "No runs recorded" in text

    def test_with_entries(self, populated_log: RunLog):
        result = query_history(populated_log, last=10)
        text = format_history(result)
        assert "Run History" in text
        assert "5 runs" in text
        assert "✓" in text
        assert "✗" in text
        assert "By Tier" in text
        assert "Recent Runs" in text

    def test_shows_tasks(self, populated_log: RunLog):
        result = query_history(populated_log, last=10)
        text = format_history(result)
        assert "Check email" in text
        assert "Write report" in text
        assert "Deploy code" in text

    def test_shows_filters(self, populated_log: RunLog):
        result = query_history(populated_log, tier="low", last=10)
        text = format_history(result)
        assert "Filters:" in text
        assert "tier=low" in text

    def test_truncates_long_tasks(self):
        """Tasks longer than 50 chars are truncated with ellipsis."""
        long_task = "x" * 60
        entry = _entry(task=long_task)
        result = HistoryResult(
            entries=[entry],
            stats=compute_stats([entry]),
            filters_applied={},
        )
        text = format_history(result)
        assert "…" in text


# ══════════════════════════════════════════════════════════════
# format_history_json
# ══════════════════════════════════════════════════════════════


class TestFormatHistoryJson:
    def test_valid_json(self, populated_log: RunLog):
        result = query_history(populated_log, last=10)
        json_str = format_history_json(result)
        data = json.loads(json_str)
        assert "stats" in data
        assert "entries" in data
        assert "filters" in data
        assert data["stats"]["total_runs"] == 5
        assert len(data["entries"]) == 5

    def test_entries_have_fields(self, populated_log: RunLog):
        result = query_history(populated_log, last=10)
        json_str = format_history_json(result)
        data = json.loads(json_str)
        entry = data["entries"][0]
        assert "timestamp" in entry
        assert "task" in entry
        assert "tier" in entry
        assert "model" in entry
        assert "success" in entry
        assert "duration_s" in entry

    def test_with_filters(self, populated_log: RunLog):
        result = query_history(populated_log, tier="low", last=10)
        json_str = format_history_json(result)
        data = json.loads(json_str)
        assert data["filters"]["tier"] == "low"

    def test_by_tier_in_stats(self, populated_log: RunLog):
        result = query_history(populated_log, last=10)
        json_str = format_history_json(result)
        data = json.loads(json_str)
        assert "by_tier" in data["stats"]
        assert "low" in data["stats"]["by_tier"]


# ══════════════════════════════════════════════════════════════
# _format_timestamp
# ══════════════════════════════════════════════════════════════


class TestFormatTimestamp:
    def test_iso_with_offset(self):
        result = _format_timestamp("2026-03-12T10:30:45+00:00")
        assert "2026-03-12" in result
        assert "10:30:45" in result

    def test_iso_with_z(self):
        result = _format_timestamp("2026-03-12T10:30:45Z")
        assert "2026-03-12" in result

    def test_invalid(self):
        result = _format_timestamp("not-a-timestamp")
        assert isinstance(result, str)
        assert len(result) <= 22

    def test_none(self):
        result = _format_timestamp(None)  # type: ignore[arg-type]
        assert result == ""

    def test_empty(self):
        result = _format_timestamp("")
        assert result == ""

    def test_output_format(self):
        result = _format_timestamp("2026-03-12T10:30:45+00:00")
        assert result == "2026-03-12 10:30:45"
