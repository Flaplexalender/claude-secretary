"""Tests for history module — query, stats, formatting.

Direct tests for the history API. The CLI wrappers are tested in test_cli.py;
these tests verify the core query and formatting functions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from secretary.currency import set_rate, get_rate
from secretary.history import (
    HistoryResult,
    HistoryStats,
    TierStats,
    _compute_tier_stats,
    _format_timestamp,
    compute_stats,
    format_history,
    format_history_json,
    query_history,
)
from secretary.run_log import RunLog, RunLogEntry


# ── Fixtures ──────────────────────────────────────────────────


def _entry(
    task: str = "test task",
    tier: str = "low",
    model: str = "claude-haiku-4.5",
    success: bool = True,
    duration_s: float = 1.5,
    premium_cost: float = 0.33,
    cost_usd: float = 0.001,
    cycle: int = 0,
    error: str | None = None,
) -> RunLogEntry:
    return RunLogEntry(
        timestamp=RunLog.now(),
        cycle=cycle,
        task=task,
        tier=tier,
        model=model,
        success=success,
        output_preview="ok",
        error=error,
        duration_s=duration_s,
        premium_cost=premium_cost,
        cost_usd=cost_usd,
    )


def _populated_log(tmp_path: Path) -> RunLog:
    """Create a RunLog with diverse entries for testing."""
    log = RunLog(tmp_path / "run_log.jsonl")
    entries = [
        _entry(task="Check morning emails", tier="low", success=True, duration_s=2.0, premium_cost=0.33),
        _entry(task="Refactor agent module", tier="high", model="claude-opus-4.7", success=True, duration_s=30.0, premium_cost=3.0, cost_usd=0.05),
        _entry(task="Fix typo in readme", tier="low", success=True, duration_s=0.5, premium_cost=0.33),
        _entry(task="Analyze campaign data", tier="medium", model="claude-sonnet-4.6", success=False, duration_s=5.0, premium_cost=1.0, error="timeout"),
        _entry(task="Check email for updates", tier="low", success=True, duration_s=1.8, premium_cost=0.33),
        _entry(task="Write integration tests", tier="medium", model="claude-sonnet-4.6", success=True, duration_s=15.0, premium_cost=1.0, cost_usd=0.02),
        _entry(task="Debug watcher crash", tier="high", model="claude-opus-4.7", success=False, duration_s=45.0, premium_cost=3.0, cost_usd=0.10, error="API rate limit"),
    ]
    for e in entries:
        log.append(e)
    return log


# ══════════════════════════════════════════════════════════════
#  _format_timestamp
# ══════════════════════════════════════════════════════════════


def test_format_timestamp_iso():
    result = _format_timestamp("2025-01-15T10:30:45+00:00")
    assert result == "2025-01-15 10:30:45"


def test_format_timestamp_with_z():
    result = _format_timestamp("2025-01-15T10:30:45Z")
    assert result == "2025-01-15 10:30:45"


def test_format_timestamp_invalid():
    result = _format_timestamp("not-a-timestamp")
    assert result == "not-a-timestamp"


def test_format_timestamp_none():
    result = _format_timestamp(None)
    assert result == ""


def test_format_timestamp_empty_string():
    result = _format_timestamp("")
    assert result == ""


# ══════════════════════════════════════════════════════════════
#  _compute_tier_stats
# ══════════════════════════════════════════════════════════════


def test_compute_tier_stats_empty():
    result = _compute_tier_stats([])
    assert result == {}


def test_compute_tier_stats_single_tier():
    entries = [_entry(tier="low", success=True, duration_s=2.0)]
    result = _compute_tier_stats(entries)
    assert "low" in result
    assert result["low"].count == 1
    assert result["low"].successful == 1
    assert result["low"].failed == 0
    assert result["low"].avg_duration_s == 2.0


def test_compute_tier_stats_multiple_tiers():
    entries = [
        _entry(tier="low", success=True, duration_s=1.0, premium_cost=0.33),
        _entry(tier="low", success=False, duration_s=2.0, premium_cost=0.33),
        _entry(tier="high", success=True, duration_s=30.0, premium_cost=3.0),
    ]
    result = _compute_tier_stats(entries)
    assert result["low"].count == 2
    assert result["low"].successful == 1
    assert result["low"].failed == 1
    assert result["low"].success_rate == "50%"
    assert result["low"].total_premium == 0.66
    assert result["high"].count == 1
    assert result["high"].success_rate == "100%"


def test_compute_tier_stats_zero_duration():
    """Entries with duration_s=0 should be excluded from avg calculation."""
    entries = [
        _entry(tier="low", duration_s=0.0),
        _entry(tier="low", duration_s=4.0),
    ]
    result = _compute_tier_stats(entries)
    assert result["low"].avg_duration_s == 4.0  # only non-zero counted


# ══════════════════════════════════════════════════════════════
#  compute_stats
# ══════════════════════════════════════════════════════════════


def test_compute_stats_empty():
    stats = compute_stats([])
    assert stats.total_runs == 0
    assert stats.success_rate == "0%"
    assert stats.avg_duration_s == 0.0
    assert stats.by_tier == {}


def test_compute_stats_all_success():
    entries = [_entry(success=True) for _ in range(5)]
    stats = compute_stats(entries)
    assert stats.total_runs == 5
    assert stats.successful == 5
    assert stats.failed == 0
    assert stats.success_rate == "100%"


def test_compute_stats_mixed():
    original_rate = get_rate()
    try:
        set_rate(1.44)
        entries = [
            _entry(success=True, cost_usd=0.01),
            _entry(success=False, cost_usd=0.02),
            _entry(success=True, cost_usd=0.03),
        ]
        stats = compute_stats(entries)
        assert stats.total_runs == 3
        assert stats.successful == 2
        assert stats.failed == 1
        assert stats.success_rate == "67%"
        assert abs(stats.total_cost_usd - 0.06) < 0.001
        assert abs(stats.total_cost_cad - 0.06 * 1.44) < 0.001
    finally:
        set_rate(original_rate)


def test_compute_stats_avg_duration():
    entries = [
        _entry(duration_s=2.0),
        _entry(duration_s=4.0),
        _entry(duration_s=6.0),
    ]
    stats = compute_stats(entries)
    assert stats.avg_duration_s == 4.0


# ══════════════════════════════════════════════════════════════
#  query_history
# ══════════════════════════════════════════════════════════════


def test_query_history_no_filters(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log)
    assert result.stats.total_runs == 7
    assert len(result.entries) == 7  # last=10 > 7 entries


def test_query_history_filter_tier(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, tier="low")
    assert result.stats.total_runs == 3
    assert all(e.tier == "low" for e in result.entries)
    assert result.filters_applied["tier"] == "low"


def test_query_history_filter_failed(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, failed_only=True)
    assert result.stats.total_runs == 2
    assert all(not e.success for e in result.entries)
    assert result.filters_applied.get("failed_only") is True


def test_query_history_filter_search(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, search="email")
    assert result.stats.total_runs == 2
    assert all("email" in e.task.lower() for e in result.entries)


def test_query_history_last_limit(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, last=3)
    assert len(result.entries) == 3
    # Stats should still reflect all entries (not just last 3)
    assert result.stats.total_runs == 7


def test_query_history_combined_filters(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, tier="high", failed_only=True)
    assert result.stats.total_runs == 1
    assert result.entries[0].task == "Debug watcher crash"


def test_query_history_no_matches(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, search="nonexistent_xyz")
    assert result.stats.total_runs == 0
    assert len(result.entries) == 0


def test_query_history_empty_log(tmp_path: Path):
    log = RunLog(tmp_path / "run_log.jsonl")
    result = query_history(log)
    assert result.stats.total_runs == 0
    assert result.entries == []


# ══════════════════════════════════════════════════════════════
#  format_history
# ══════════════════════════════════════════════════════════════


def test_format_history_empty():
    stats = HistoryStats(
        total_runs=0, successful=0, failed=0, success_rate="0%",
        avg_duration_s=0.0, total_premium=0.0,
        total_cost_usd=0.0, total_cost_cad=0.0, by_tier={},
    )
    result = HistoryResult(entries=[], stats=stats, filters_applied={})
    output = format_history(result)
    assert "No runs recorded yet" in output


def test_format_history_with_entries(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log)
    output = format_history(result)
    assert "Run History" in output
    assert "Total: 7 runs" in output
    assert "By Tier" in output
    assert "Recent Runs" in output
    assert "Check morning emails" in output


def test_format_history_with_filters(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, tier="low", failed_only=False)
    output = format_history(result)
    assert "Filters:" in output
    assert "tier=low" in output


def test_format_history_truncates_long_tasks():
    """Tasks longer than 50 chars should be truncated with ellipsis."""
    entries = [_entry(task="A" * 60)]
    stats = compute_stats(entries)
    result = HistoryResult(entries=entries, stats=stats, filters_applied={})
    output = format_history(result)
    assert "A" * 50 + "…" in output


# ══════════════════════════════════════════════════════════════
#  format_history_json
# ══════════════════════════════════════════════════════════════


def test_format_history_json_valid(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log)
    output = format_history_json(result)
    data = json.loads(output)
    assert "stats" in data
    assert "entries" in data
    assert "filters" in data
    assert data["stats"]["total_runs"] == 7


def test_format_history_json_entries_fields(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, last=1)
    output = format_history_json(result)
    data = json.loads(output)
    entry = data["entries"][0]
    assert "timestamp" in entry
    assert "task" in entry
    assert "tier" in entry
    assert "model" in entry
    assert "success" in entry
    assert "duration_s" in entry
    assert "error" in entry


def test_format_history_json_with_filters(tmp_path: Path):
    log = _populated_log(tmp_path)
    result = query_history(log, tier="low", search="email")
    output = format_history_json(result)
    data = json.loads(output)
    assert data["filters"]["tier"] == "low"
    assert data["filters"]["search"] == "email"


def test_format_history_json_empty():
    stats = compute_stats([])
    result = HistoryResult(entries=[], stats=stats, filters_applied={})
    output = format_history_json(result)
    data = json.loads(output)
    assert data["stats"]["total_runs"] == 0
    assert data["entries"] == []
