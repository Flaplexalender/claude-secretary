"""Tests for watcher module helper functions — pure functions, no API calls.

Tests _check_schedule, _is_quota_error, and Watcher._format_duration.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from secretary.watcher import _check_schedule, _is_quota_error, Watcher


# ══════════════════════════════════════════════════════════════
#  _is_quota_error
# ══════════════════════════════════════════════════════════════

def test_quota_error_429():
    assert _is_quota_error("HTTP 429 Too Many Requests") is True


def test_quota_error_rate_limit():
    assert _is_quota_error("rate limit exceeded") is True


def test_quota_error_rate_limit_underscore():
    assert _is_quota_error("rate_limit_error: too fast") is True


def test_quota_error_quota():
    assert _is_quota_error("insufficient_quota for this request") is True


def test_quota_error_billing():
    assert _is_quota_error("billing issue: no active subscription") is True


def test_quota_error_premium():
    assert _is_quota_error("premium request limit reached") is True


def test_quota_error_model_capacity():
    assert _is_quota_error("model capacity exceeded") is True


def test_quota_error_case_insensitive():
    assert _is_quota_error("RATE LIMIT exceeded") is True


def test_quota_error_not_quota():
    assert _is_quota_error("Internal server error") is False


def test_quota_error_api_error():
    assert _is_quota_error("API error: timeout after 300s") is False


def test_quota_error_empty():
    assert _is_quota_error("") is False


def test_quota_error_generic():
    assert _is_quota_error("Connection reset by peer") is False


# ══════════════════════════════════════════════════════════════
#  _check_schedule
# ══════════════════════════════════════════════════════════════

class TestCheckScheduleHours:
    """Test hour-range scheduling."""

    def test_in_range_normal(self):
        now = datetime(2025, 1, 15, 10, 30)  # 10:30 AM
        assert _check_schedule("hours:8-17", now) is True

    def test_out_of_range_normal(self):
        now = datetime(2025, 1, 15, 18, 0)  # 6 PM
        assert _check_schedule("hours:8-17", now) is False

    def test_at_start_boundary(self):
        now = datetime(2025, 1, 15, 8, 0)  # exactly 8:00
        assert _check_schedule("hours:8-17", now) is True

    def test_at_end_boundary(self):
        now = datetime(2025, 1, 15, 17, 0)  # exactly 17:00 — end is exclusive
        assert _check_schedule("hours:8-17", now) is False

    def test_overnight_in_range_late(self):
        now = datetime(2025, 1, 15, 23, 0)  # 11 PM
        assert _check_schedule("hours:22-6", now) is True

    def test_overnight_in_range_early(self):
        now = datetime(2025, 1, 16, 3, 0)  # 3 AM
        assert _check_schedule("hours:22-6", now) is True

    def test_overnight_out_of_range(self):
        now = datetime(2025, 1, 15, 12, 0)  # noon
        assert _check_schedule("hours:22-6", now) is False

    def test_multiple_ranges(self):
        now = datetime(2025, 1, 15, 18, 0)  # 6 PM
        assert _check_schedule("hours:6-9,17-20", now) is True

    def test_multiple_ranges_out_of_both(self):
        now = datetime(2025, 1, 15, 12, 0)  # noon
        assert _check_schedule("hours:6-9,17-20", now) is False

    def test_multiple_ranges_in_first(self):
        now = datetime(2025, 1, 15, 7, 30)  # 7:30 AM
        assert _check_schedule("hours:6-9,17-20", now) is True

    def test_midnight_boundary(self):
        now = datetime(2025, 1, 15, 0, 0)  # midnight
        assert _check_schedule("hours:22-6", now) is True

    def test_midnight_not_in_normal_range(self):
        now = datetime(2025, 1, 15, 0, 0)  # midnight
        assert _check_schedule("hours:8-17", now) is False


class TestCheckScheduleWeekdays:
    """Test weekday/weekend scheduling."""

    def test_weekday_on_monday(self):
        # Jan 13, 2025 = Monday
        now = datetime(2025, 1, 13, 10, 0)
        assert _check_schedule("weekdays", now) is True

    def test_weekday_on_friday(self):
        # Jan 17, 2025 = Friday
        now = datetime(2025, 1, 17, 10, 0)
        assert _check_schedule("weekdays", now) is True

    def test_weekday_on_saturday(self):
        # Jan 18, 2025 = Saturday
        now = datetime(2025, 1, 18, 10, 0)
        assert _check_schedule("weekdays", now) is False

    def test_weekday_on_sunday(self):
        # Jan 19, 2025 = Sunday
        now = datetime(2025, 1, 19, 10, 0)
        assert _check_schedule("weekdays", now) is False

    def test_weekend_on_saturday(self):
        now = datetime(2025, 1, 18, 10, 0)
        assert _check_schedule("weekends", now) is True

    def test_weekend_on_sunday(self):
        now = datetime(2025, 1, 19, 10, 0)
        assert _check_schedule("weekends", now) is True

    def test_weekend_on_tuesday(self):
        now = datetime(2025, 1, 14, 10, 0)
        assert _check_schedule("weekends", now) is False


class TestCheckScheduleCombined:
    """Test combined rules with semicolons."""

    def test_weekday_within_hours(self):
        # Monday at 10 AM — both rules match
        now = datetime(2025, 1, 13, 10, 0)
        assert _check_schedule("hours:8-17;weekdays", now) is True

    def test_weekday_outside_hours(self):
        # Monday at 8 PM — hours fail
        now = datetime(2025, 1, 13, 20, 0)
        assert _check_schedule("hours:8-17;weekdays", now) is False

    def test_weekend_within_hours(self):
        # Saturday at 10 AM — weekdays fail
        now = datetime(2025, 1, 18, 10, 0)
        assert _check_schedule("hours:8-17;weekdays", now) is False

    def test_empty_schedule(self):
        # Empty schedule always matches (no rules = all match)
        now = datetime(2025, 1, 15, 10, 0)
        assert _check_schedule("", now) is True


class TestCheckScheduleEdgeCases:
    """Edge cases and malformed input."""

    def test_unknown_rule_passes(self):
        """Unknown rules are logged but don't fail the match."""
        now = datetime(2025, 1, 15, 10, 0)
        assert _check_schedule("unknown_rule", now) is True

    def test_spaces_in_rules(self):
        now = datetime(2025, 1, 13, 10, 0)
        assert _check_schedule(" hours:8-17 ; weekdays ", now) is True

    def test_malformed_hours_range(self):
        """Malformed range is skipped (logged as warning), rule doesn't match."""
        now = datetime(2025, 1, 15, 10, 0)
        # "abc-def" can't be parsed → skipped → in_range stays False → rule fails
        assert _check_schedule("hours:abc-def", now) is False

    def test_single_hour_range(self):
        """A range like '10-11' should match hour 10 only."""
        now = datetime(2025, 1, 15, 10, 30)
        assert _check_schedule("hours:10-11", now) is True
        now = datetime(2025, 1, 15, 11, 0)
        assert _check_schedule("hours:10-11", now) is False


# ══════════════════════════════════════════════════════════════
#  Watcher._format_duration
# ══════════════════════════════════════════════════════════════

def test_format_duration_seconds():
    assert Watcher._format_duration(45.0) == "45s"


def test_format_duration_minutes_seconds():
    assert Watcher._format_duration(125.0) == "2m 5s"


def test_format_duration_hours_minutes():
    assert Watcher._format_duration(3725.0) == "1h 2m"


def test_format_duration_zero():
    assert Watcher._format_duration(0.0) == "0s"


def test_format_duration_exact_minute():
    assert Watcher._format_duration(60.0) == "1m 0s"


def test_format_duration_exact_hour():
    assert Watcher._format_duration(3600.0) == "1h 0m"


def test_format_duration_large():
    assert Watcher._format_duration(86400.0) == "24h 0m"  # 24 hours
