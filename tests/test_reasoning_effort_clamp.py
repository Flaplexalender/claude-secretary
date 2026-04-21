"""Tests for ``_clamp_reasoning_effort`` — the per-model reasoning-effort
capability clamp that guards against proxy HTTP 400 ``invalid_reasoning_effort``
errors when a user's configured effort exceeds a given model's cap.

Discovered empirically against copilot-api 2026-04-20:
- Haiku 4.5 rejects ``reasoning_effort`` entirely.
- Opus 4.7 supports only ``"medium"`` (high + low both rejected).
- Other Claude models (Sonnet 4/4.5/4.6, Opus 4.5/4.6) accept the full range.
"""
from __future__ import annotations

import pytest

from secretary.direct_agent import _clamp_reasoning_effort


class TestClampReasoningEffort:
    @pytest.mark.parametrize(
        "requested,expected",
        [
            ("high", None),
            ("medium", None),
            ("low", None),
            ("", None),
            (None, None),
        ],
    )
    def test_haiku_never_supports_reasoning(self, requested, expected):
        """Haiku 4.5 must always return None — proxy 400s on any effort."""
        assert _clamp_reasoning_effort("claude-haiku-4.5", requested) == expected

    @pytest.mark.parametrize(
        "requested,expected",
        [
            ("high", "medium"),   # clamp down from high
            ("medium", "medium"),  # already at cap
            ("low", "low"),        # below cap — unchanged
        ],
    )
    def test_opus_47_caps_at_medium(self, requested, expected):
        """Opus 4.7 caps at medium — high must clamp, low must pass through."""
        assert _clamp_reasoning_effort("claude-opus-4.7", requested) == expected

    def test_opus_47_none_request_returns_none(self):
        assert _clamp_reasoning_effort("claude-opus-4.7", None) is None
        assert _clamp_reasoning_effort("claude-opus-4.7", "") is None

    @pytest.mark.parametrize(
        "model",
        [
            "claude-sonnet-4.6",
            "claude-sonnet-4.5",
            "claude-sonnet-4",
            "claude-opus-4.5",
            "claude-opus-4.6",
        ],
    )
    @pytest.mark.parametrize("effort", ["low", "medium", "high"])
    def test_uncapped_models_pass_through(self, model, effort):
        """Models without an explicit cap must return requested effort unchanged."""
        assert _clamp_reasoning_effort(model, effort) == effort

    def test_unknown_model_defaults_to_full_range(self):
        """Future Claude models we haven't seen default to full range for
        backwards compatibility — the worst case is a single failing request
        that surfaces the cap, not a silent degradation."""
        assert _clamp_reasoning_effort("claude-future-99", "high") == "high"

    def test_unknown_effort_level_falls_back_to_cap(self):
        """If someone passes an unknown effort string, clamp to the model cap
        rather than silently passing through a value the proxy will reject."""
        assert _clamp_reasoning_effort("claude-opus-4.7", "extreme") == "medium"
        assert _clamp_reasoning_effort("claude-haiku-4.5", "extreme") is None
