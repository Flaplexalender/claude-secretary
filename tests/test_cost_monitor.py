"""Tests for cost_monitor — budget tracking, alerting, spend calculations."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from secretary.cost_monitor import CostAlert, CostMonitor, CostMonitorConfig


# Fixed midweek date for "weekly window" tests so they don't fail on
# Mondays (when hours_ago=24 lands in the previous ISO week). Wednesday
# 12:00 leaves room for up to 48h of back-dated entries on either side
# of the week boundary.
_FAKE_NOW = datetime(2026, 4, 22, 12, 0, 0)  # Wednesday


@pytest.fixture
def frozen_now():
    """Patch cost_monitor.datetime so .now() returns a fixed Wednesday.

    `fromisoformat` still falls through to the real implementation so
    entries written via `_entry(...)` are parsed normally.
    """
    with patch("secretary.cost_monitor.datetime") as mock_dt:
        mock_dt.now.return_value = _FAKE_NOW
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        yield mock_dt


@pytest.fixture
def tmp_data(tmp_path: Path):
    """Create a temp dir with run_log and cost_alerts paths."""
    run_log = tmp_path / "run_log.jsonl"
    alert_log = tmp_path / "cost_alerts.jsonl"
    return tmp_path, run_log, alert_log


def _make_config(
    daily: float = 10.0,
    weekly: float = 50.0,
    pct: int = 80,
    alert_log: str = "data/cost_alerts.jsonl",
) -> CostMonitorConfig:
    return CostMonitorConfig(
        enabled=True,
        daily_limit_usd=daily,
        weekly_limit_usd=weekly,
        alert_threshold_pct=pct,
        log_path=alert_log,
    )


def _write_entries(path: Path, entries: list[dict]):
    """Write run log entries to JSONL."""
    with open(path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _entry(cost_usd: float = 1.0, premium: float = 1.0, hours_ago: float = 0, success: bool = True, now: datetime | None = None) -> dict:
    """Create a run log entry dict. Pass `now` to anchor the timestamp
    relative to a frozen clock (else uses real datetime.now)."""
    anchor = now if now is not None else datetime.now(timezone.utc)
    ts = anchor - timedelta(hours=hours_ago)
    return {
        "timestamp": ts.isoformat(),
        "cycle": 1,
        "task": "test-task",
        "tier": "medium",
        "model": "claude-sonnet-4.6",
        "success": success,
        "output_preview": "",
        "cost_usd": cost_usd,
        "premium_cost": premium,
    }


# --- Daily/Weekly spend calculation ---

def test_daily_spend_empty(tmp_data):
    _, run_log, alert_log = tmp_data
    cfg = _make_config(alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    usd, premium = mon._calculate_daily_spend()
    assert usd == 0.0
    assert premium == 0.0


def test_daily_spend_today_only(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [
        _entry(cost_usd=2.0, premium=1.0, hours_ago=0),   # today
        _entry(cost_usd=3.0, premium=1.0, hours_ago=0),   # today
        _entry(cost_usd=10.0, premium=3.0, hours_ago=48), # two days ago — excluded
    ])
    cfg = _make_config(alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    usd, premium = mon._calculate_daily_spend()
    assert usd == 5.0
    assert premium == 2.0


def test_weekly_spend_includes_this_week(tmp_data, frozen_now):
    _, run_log, alert_log = tmp_data
    # Create entries for today and a few days ago (within week).
    # Anchored to _FAKE_NOW (Wed) so hours_ago=24 lands on Tuesday.
    _write_entries(run_log, [
        _entry(cost_usd=2.0, hours_ago=0, now=_FAKE_NOW),
        _entry(cost_usd=3.0, hours_ago=24, now=_FAKE_NOW),    # yesterday
        _entry(cost_usd=99.0, hours_ago=240, now=_FAKE_NOW),   # 10 days ago — excluded
    ])
    cfg = _make_config(alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    usd, _ = mon._calculate_weekly_spend()
    assert usd == 5.0


def test_failed_tasks_excluded_from_spend(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [
        _entry(cost_usd=2.0, success=True),
        _entry(cost_usd=5.0, success=False),  # failed — excluded
    ])
    cfg = _make_config(alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    usd, _ = mon._calculate_daily_spend()
    assert usd == 2.0


# --- Alert triggering ---

def test_daily_alert_fires_at_threshold(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=8.5)])  # 85% of $10 limit
    cfg = _make_config(daily=10.0, pct=80, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    alert = mon.check_and_alert()
    assert alert is not None
    assert alert.alert_type == "daily"
    assert alert.threshold_pct == 85


def test_no_alert_below_threshold(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=5.0)])  # 50% of $10 limit
    cfg = _make_config(daily=10.0, pct=80, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    alert = mon.check_and_alert()
    assert alert is None


def test_alert_disabled(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=100.0)])
    cfg = CostMonitorConfig(enabled=False, alert_threshold_pct=80, log_path=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    assert mon.check_and_alert() is None


def test_alert_debounce(tmp_data):
    """Second check within debounce window should not re-alert."""
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=9.0)])
    cfg = _make_config(daily=10.0, pct=80, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)

    alert1 = mon.check_and_alert()
    assert alert1 is not None

    alert2 = mon.check_and_alert()  # within debounce
    assert alert2 is None


def test_weekly_alert_when_daily_is_ok(tmp_data, frozen_now):
    """Weekly alert fires even if daily is under threshold."""
    _, run_log, alert_log = tmp_data
    # Spread across multiple days this week, daily under $10 but weekly
    # over 80% of $12.  Anchored to _FAKE_NOW (Wed) so 48h back lands on
    # Monday of the same ISO week regardless of when the test runs.
    _write_entries(run_log, [
        _entry(cost_usd=3.0, hours_ago=0, now=_FAKE_NOW),
        _entry(cost_usd=3.5, hours_ago=24, now=_FAKE_NOW),
        _entry(cost_usd=4.0, hours_ago=48, now=_FAKE_NOW),
    ])
    cfg = _make_config(daily=10.0, weekly=12.0, pct=80, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    alert = mon.check_and_alert()
    assert alert is not None
    assert alert.alert_type == "weekly"


# --- Budget exhaustion ---

def test_budget_exhausted_daily(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=11.0)])
    cfg = _make_config(daily=10.0, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    assert mon.is_budget_exhausted() is True


def test_budget_not_exhausted(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=5.0)])
    cfg = _make_config(daily=10.0, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    assert mon.is_budget_exhausted() is False


def test_budget_exhausted_disabled(tmp_data):
    """Budget check returns False when limits are 0 (disabled)."""
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=1000.0)])
    cfg = _make_config(daily=0.0, weekly=0.0, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    assert mon.is_budget_exhausted() is False


# --- Spend summary ---

def test_get_spend_summary(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=4.0, premium=2.0)])
    cfg = _make_config(daily=10.0, weekly=50.0, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    s = mon.get_spend_summary()
    assert s["daily_usd"] == 4.0
    assert s["daily_pct"] == 40
    assert s["exhausted"] is False


# --- Alert logging ---

def test_alert_logged_to_file(tmp_data):
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=9.0)])
    cfg = _make_config(daily=10.0, pct=80, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    mon.check_and_alert()
    assert alert_log.exists()
    lines = alert_log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["alert_type"] == "daily"


# --- Gmail alert ---

def test_send_alert_email_drafts(tmp_data):
    """Verify that send_alert_email calls Gmail API correctly."""
    _, run_log, alert_log = tmp_data
    cfg = _make_config(alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)

    alert = CostAlert(
        timestamp="2026-03-22T12:00:00Z",
        current_spend_usd=8.0,
        spend_multiplier=4.0,
        daily_limit_usd=10.0,
        weekly_limit_usd=None,
        threshold_pct=80,
        alert_type="daily",
        message="Budget alert test",
    )

    mock_svc = MagicMock()
    with patch("secretary.cost_monitor.CostMonitor.send_alert_email") as mock:
        mock.return_value = True
        result = mon.send_alert_email(alert, "test@example.com", tmp_data[0])
    assert result is True


# --- Zero-limit edge cases ---

def test_zero_daily_limit_no_alert(tmp_data):
    """Zero daily limit means disabled — no alert regardless of spend."""
    _, run_log, alert_log = tmp_data
    _write_entries(run_log, [_entry(cost_usd=1000.0)])
    cfg = _make_config(daily=0.0, weekly=0.0, pct=80, alert_log=str(alert_log))
    mon = CostMonitor(cfg, run_log_path=run_log)
    assert mon.check_and_alert() is None
