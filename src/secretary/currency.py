"""Currency conversion for cost tracking.

All costs are tracked in USD internally (from the SDK).
This module converts to the user's display currency (CAD by default).
"""
from __future__ import annotations

# Default USD → CAD exchange rate (updated manually or via config)
DEFAULT_USD_TO_CAD = 1.44

_rate: float = DEFAULT_USD_TO_CAD


def set_rate(rate: float) -> None:
    """Override the USD → CAD exchange rate."""
    import math
    if not isinstance(rate, (int, float)) or rate <= 0 or math.isnan(rate) or math.isinf(rate):
        raise ValueError(f"Invalid exchange rate: {rate!r} (must be a positive finite number)")
    global _rate
    _rate = rate


def get_rate() -> float:
    """Return the current USD → CAD exchange rate."""
    return _rate


def usd_to_cad(usd: float) -> float:
    """Convert USD to CAD."""
    return usd * _rate


def format_cad(usd: float, decimals: int = 2) -> str:
    """Format a USD amount as a CAD string like '$1.23 CAD'."""
    cad = usd_to_cad(usd)
    return f"${cad:.{decimals}f} CAD"


def format_cost(usd: float, decimals: int = 4) -> str:
    """Format cost showing both USD and CAD: '$0.0012 USD ($0.0017 CAD)'."""
    if usd == 0:
        return "$0"
    cad = usd_to_cad(usd)
    return f"${usd:.{decimals}f} USD (${cad:.{decimals}f} CAD)"
