"""Tests for overnight schedule range support in _check_schedule.

These test the wrap-around logic added in cycle 2:
    "hours:22-6" should match 22, 23, 0, 1, 2, 3, 4, 5.
"""
from datetime import datetime
from secretary.watcher import _check_schedule


def test_overnight_range_late_night():
    """hours:22-6 matches at 23:00 (late night)."""
    at_23 = datetime(2026, 3, 12, 23, 0)
    assert _check_schedule("hours:22-6", at_23) is True


def test_overnight_range_early_morning():
    """hours:22-6 matches at 3:00 AM (early morning)."""
    at_3 = datetime(2026, 3, 12, 3, 0)
    assert _check_schedule("hours:22-6", at_3) is True


def test_overnight_range_start_boundary():
    """hours:22-6 matches at exactly 22:00 (inclusive start)."""
    at_22 = datetime(2026, 3, 12, 22, 0)
    assert _check_schedule("hours:22-6", at_22) is True


def test_overnight_range_end_boundary():
    """hours:22-6 does NOT match at 6:00 (exclusive end)."""
    at_6 = datetime(2026, 3, 12, 6, 0)
    assert _check_schedule("hours:22-6", at_6) is False


def test_overnight_range_midday_excluded():
    """hours:22-6 does NOT match at 12:00 (midday)."""
    at_12 = datetime(2026, 3, 12, 12, 0)
    assert _check_schedule("hours:22-6", at_12) is False


def test_overnight_range_afternoon_excluded():
    """hours:22-6 does NOT match at 15:00."""
    at_15 = datetime(2026, 3, 12, 15, 0)
    assert _check_schedule("hours:22-6", at_15) is False


def test_overnight_range_midnight():
    """hours:22-6 matches at midnight (0:00)."""
    at_0 = datetime(2026, 3, 12, 0, 0)
    assert _check_schedule("hours:22-6", at_0) is True


def test_overnight_mixed_with_normal_range():
    """Comma-separated with one overnight and one normal range."""
    # hours:22-6,9-12 — should match 22-6 OR 9-12
    at_23 = datetime(2026, 3, 12, 23, 0)
    at_10 = datetime(2026, 3, 12, 10, 0)
    at_15 = datetime(2026, 3, 12, 15, 0)
    assert _check_schedule("hours:22-6,9-12", at_23) is True
    assert _check_schedule("hours:22-6,9-12", at_10) is True
    assert _check_schedule("hours:22-6,9-12", at_15) is False


def test_overnight_combined_with_weekdays():
    """Overnight schedule combined with weekdays rule."""
    # Monday at 23:00 — should pass both rules
    mon_night = datetime(2026, 3, 9, 23, 0)   # Monday
    sat_night = datetime(2026, 3, 14, 23, 0)  # Saturday
    assert _check_schedule("hours:22-6;weekdays", mon_night) is True
    assert _check_schedule("hours:22-6;weekdays", sat_night) is False


def test_normal_range_still_works():
    """Verify normal (non-overnight) ranges are unaffected by the fix."""
    at_10 = datetime(2026, 3, 12, 10, 0)
    at_20 = datetime(2026, 3, 12, 20, 0)
    assert _check_schedule("hours:8-17", at_10) is True
    assert _check_schedule("hours:8-17", at_20) is False


def test_same_hour_range():
    """hours:10-10 — start == end, should match nothing (zero-width range)."""
    at_10 = datetime(2026, 3, 12, 10, 0)
    at_11 = datetime(2026, 3, 12, 11, 0)
    assert _check_schedule("hours:10-10", at_10) is False
    assert _check_schedule("hours:10-10", at_11) is False
