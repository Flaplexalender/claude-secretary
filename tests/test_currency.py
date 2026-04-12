"""Tests for the currency conversion module."""
import pytest

from secretary.currency import (
    usd_to_cad,
    format_cad,
    format_cost,
    get_rate,
    set_rate,
    DEFAULT_USD_TO_CAD,
)


def test_default_rate():
    assert get_rate() == DEFAULT_USD_TO_CAD


def test_set_rate():
    original = get_rate()
    try:
        set_rate(1.50)
        assert get_rate() == 1.50
        assert usd_to_cad(1.0) == 1.50
    finally:
        set_rate(original)


def test_usd_to_cad():
    set_rate(1.44)
    assert usd_to_cad(0) == 0
    assert usd_to_cad(1.0) == 1.44
    assert abs(usd_to_cad(10.0) - 14.4) < 0.001


def test_format_cad():
    set_rate(1.44)
    assert format_cad(1.0) == "$1.44 CAD"
    assert format_cad(0) == "$0.00 CAD"
    assert format_cad(10.0, decimals=0) == "$14 CAD"


def test_format_cost():
    set_rate(1.44)
    assert format_cost(0) == "$0"
    result = format_cost(1.0)
    assert "USD" in result
    assert "CAD" in result
    assert "$1.0000 USD" in result
    assert "$1.4400 CAD" in result


def test_format_cost_precision():
    set_rate(1.44)
    result = format_cost(0.0012)
    assert "$0.0012 USD" in result
    assert "CAD" in result


# ── set_rate validation ──────────────────────────────────────


def test_set_rate_rejects_zero():
    with pytest.raises(ValueError, match="Invalid exchange rate"):
        set_rate(0)


def test_set_rate_rejects_negative():
    with pytest.raises(ValueError, match="Invalid exchange rate"):
        set_rate(-1.5)


def test_set_rate_rejects_nan():
    with pytest.raises(ValueError, match="Invalid exchange rate"):
        set_rate(float("nan"))


def test_set_rate_rejects_infinity():
    with pytest.raises(ValueError, match="Invalid exchange rate"):
        set_rate(float("inf"))


# ── Cycle 7: Additional coverage ──────────────────────────────


def test_set_rate_rejects_negative_infinity():
    with pytest.raises(ValueError, match="Invalid exchange rate"):
        set_rate(float("-inf"))


def test_set_rate_rejects_string():
    with pytest.raises(ValueError, match="Invalid exchange rate"):
        set_rate("1.5")  # type: ignore[arg-type]


def test_set_rate_rejects_none():
    with pytest.raises(ValueError, match="Invalid exchange rate"):
        set_rate(None)  # type: ignore[arg-type]


def test_set_rate_accepts_int():
    original = get_rate()
    try:
        set_rate(2)
        assert get_rate() == 2
        assert usd_to_cad(1.0) == 2.0
    finally:
        set_rate(original)


def test_set_rate_accepts_very_small_positive():
    original = get_rate()
    try:
        set_rate(0.001)
        assert get_rate() == 0.001
    finally:
        set_rate(original)


def test_format_cad_negative_usd():
    """Negative USD amounts should format correctly (e.g., refunds)."""
    set_rate(1.44)
    result = format_cad(-1.0)
    assert result == "$-1.44 CAD"


def test_format_cost_custom_decimals():
    """format_cost respects custom decimal parameter."""
    set_rate(1.44)
    result = format_cost(1.0, decimals=2)
    assert "$1.00 USD" in result
    assert "$1.44 CAD" in result


def test_format_cad_large_amount():
    set_rate(1.44)
    result = format_cad(1000000.0)
    assert result == "$1440000.00 CAD"


def test_usd_to_cad_preserves_precision():
    """Small amounts shouldn't lose precision."""
    set_rate(1.44)
    result = usd_to_cad(0.0001)
    assert abs(result - 0.000144) < 1e-10
