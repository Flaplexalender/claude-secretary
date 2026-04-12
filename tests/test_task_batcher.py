"""Tests for task batching — grouping compatible campaign tasks."""
from __future__ import annotations

import pytest

from secretary.task_batcher import group_into_batches, TaskBatch


# ── Helpers ──────────────────────────────────────────────────

def _task(prompt: str, tier: str = "low", batch_compatible: bool = False, **kw) -> dict:
    """Build a minimal task dict."""
    t = {"prompt": prompt, "tier": tier, "batch_compatible": batch_compatible}
    t.update(kw)
    return t


# ── Test: basic batch grouping ──────────────────────────────

class TestBatchGrouping:
    """Core grouping logic."""

    def test_consecutive_same_tier_batched(self):
        """Three consecutive batch_compatible tasks of same tier → 1 batch."""
        tasks = [
            _task("Check email", "low", batch_compatible=True, id="email"),
            _task("Check calendar", "low", batch_compatible=True, id="cal"),
            _task("Update notes", "low", batch_compatible=True, id="notes"),
        ]
        batches = group_into_batches(tasks)
        assert len(batches) == 1
        assert batches[0].is_batch is True
        assert batches[0].task_count == 3
        assert batches[0].tier == "low"
        # Merged prompt contains all sub-prompts
        assert "Check email" in batches[0].merged_prompt
        assert "Check calendar" in batches[0].merged_prompt
        assert "Update notes" in batches[0].merged_prompt
        assert "---" in batches[0].merged_prompt

    def test_different_tiers_not_batched(self):
        """batch_compatible tasks with different tiers → separate batches."""
        tasks = [
            _task("Check email", "low", batch_compatible=True),
            _task("Deep analysis", "high", batch_compatible=True),
            _task("Quick check", "low", batch_compatible=True),
        ]
        batches = group_into_batches(tasks)
        assert len(batches) == 3
        assert all(not b.is_batch for b in batches)

    def test_non_batchable_breaks_group(self):
        """A non-batchable task between batchable ones splits into 3 groups."""
        tasks = [
            _task("A", "low", batch_compatible=True),
            _task("B", "low", batch_compatible=True),
            _task("BLOCKER", "low", batch_compatible=False),
            _task("C", "low", batch_compatible=True),
            _task("D", "low", batch_compatible=True),
        ]
        batches = group_into_batches(tasks)
        assert len(batches) == 3
        # First batch: A+B
        assert batches[0].is_batch is True
        assert batches[0].task_count == 2
        # Solo blocker
        assert batches[1].is_batch is False
        assert batches[1].task_count == 1
        assert "BLOCKER" in batches[1].merged_prompt
        # Third batch: C+D
        assert batches[2].is_batch is True
        assert batches[2].task_count == 2

    def test_max_batch_size_enforced(self):
        """Batches don't exceed max_batch_size."""
        tasks = [
            _task(f"Task {i}", "low", batch_compatible=True)
            for i in range(7)
        ]
        batches = group_into_batches(tasks, max_batch_size=3)
        # 7 tasks, max 3 per batch → 3 + 3 + 1
        assert len(batches) == 3
        assert batches[0].task_count == 3
        assert batches[1].task_count == 3
        assert batches[2].task_count == 1
        # First two are real batches, last is solo (1 task)
        assert batches[0].is_batch is True
        assert batches[1].is_batch is True
        assert batches[2].is_batch is False

    def test_single_batchable_task_is_solo(self):
        """One batch_compatible task alone is still solo (no merge needed)."""
        tasks = [_task("Solo task", "medium", batch_compatible=True)]
        batches = group_into_batches(tasks)
        assert len(batches) == 1
        assert batches[0].is_batch is False
        assert batches[0].merged_prompt == "Solo task"

    def test_empty_task_list(self):
        """Empty input → empty output."""
        assert group_into_batches([]) == []

    def test_all_non_batchable(self):
        """All non-batchable tasks → all solo batches."""
        tasks = [
            _task("A", "low"),
            _task("B", "medium"),
            _task("C", "high"),
        ]
        batches = group_into_batches(tasks)
        assert len(batches) == 3
        assert all(not b.is_batch for b in batches)
        assert all(b.task_count == 1 for b in batches)


# ── Test: disabled mode (backwards compatible) ──────────────

class TestBatchingDisabled:
    """When task_batching=False, every task is solo."""

    def test_disabled_no_batching(self):
        """Even batch_compatible tasks aren't merged when disabled."""
        tasks = [
            _task("A", "low", batch_compatible=True),
            _task("B", "low", batch_compatible=True),
            _task("C", "low", batch_compatible=True),
        ]
        batches = group_into_batches(tasks, enabled=False)
        assert len(batches) == 3
        assert all(not b.is_batch for b in batches)


# ── Test: merged prompt format ──────────────────────────────

class TestMergedPrompt:
    """Verify the format of merged prompts."""

    def test_prompt_has_header_and_separators(self):
        """Merged prompt includes task count header and --- separators."""
        tasks = [
            _task("Check inbox", "low", batch_compatible=True, id="inbox"),
            _task("Scan calendar", "low", batch_compatible=True, id="cal"),
        ]
        batches = group_into_batches(tasks)
        prompt = batches[0].merged_prompt
        assert "2 tasks" in prompt
        assert "## Task 1 [inbox]" in prompt
        assert "## Task 2 [cal]" in prompt
        assert "Check inbox" in prompt
        assert "Scan calendar" in prompt
        assert "---" in prompt

    def test_task_ids_in_batch(self):
        """TaskBatch.task_ids returns correct IDs."""
        tasks = [
            _task("A", "low", batch_compatible=True, id="alpha"),
            _task("B", "low", batch_compatible=True, id="beta"),
        ]
        batches = group_into_batches(tasks)
        assert batches[0].task_ids == ["alpha", "beta"]


# ── Test: default_tier fallback ─────────────────────────────

class TestDefaultTier:
    """Tasks without explicit tier use default_tier."""

    def test_no_tier_uses_default(self):
        """Tasks without tier key get default_tier."""
        tasks = [
            {"prompt": "A", "batch_compatible": True},
            {"prompt": "B", "batch_compatible": True},
        ]
        batches = group_into_batches(tasks, default_tier="medium")
        assert len(batches) == 1
        assert batches[0].tier == "medium"

    def test_mixed_explicit_and_default_tier(self):
        """Explicit tier 'low' + no tier (default 'low') → batched together."""
        tasks = [
            {"prompt": "A", "tier": "low", "batch_compatible": True},
            {"prompt": "B", "batch_compatible": True},  # no tier → default
        ]
        batches = group_into_batches(tasks, default_tier="low")
        assert len(batches) == 1
        assert batches[0].task_count == 2

    def test_mixed_explicit_and_different_default(self):
        """Explicit 'low' + default 'medium' → NOT batched."""
        tasks = [
            {"prompt": "A", "tier": "low", "batch_compatible": True},
            {"prompt": "B", "batch_compatible": True},  # no tier → default medium
        ]
        batches = group_into_batches(tasks, default_tier="medium")
        assert len(batches) == 2


# ── Test: config integration ────────────────────────────────

class TestConfigDefaults:
    """Verify OptimizationConfig has the new fields."""

    def test_optimization_config_defaults(self):
        from secretary.config import OptimizationConfig
        cfg = OptimizationConfig()
        assert cfg.task_batching is True
        assert cfg.max_batch_size == 3

    def test_optimization_config_custom(self):
        from secretary.config import OptimizationConfig
        cfg = OptimizationConfig(task_batching=False, max_batch_size=5)
        assert cfg.task_batching is False
        assert cfg.max_batch_size == 5


# ── Test: campaign validation accepts batch_compatible ──────

class TestCampaignValidation:
    """batch_compatible is a valid campaign key."""

    def test_batch_compatible_no_warning(self, tmp_path):
        """batch_compatible key doesn't trigger 'unknown keys' warning."""
        from secretary.campaign import validate_campaign
        campaign = tmp_path / "campaign.yaml"
        campaign.write_text(
            "tasks:\n"
            "  - prompt: Check email\n"
            "    tier: low\n"
            "    batch_compatible: true\n"
            "    id: email\n"
        )
        result = validate_campaign(campaign)
        assert result.valid
        assert not result.warnings

    def test_batch_compatible_invalid_type(self, tmp_path):
        """batch_compatible with non-bool value → error."""
        from secretary.campaign import validate_campaign
        campaign = tmp_path / "campaign.yaml"
        campaign.write_text(
            "tasks:\n"
            "  - prompt: Check email\n"
            "    tier: low\n"
            "    batch_compatible: maybe\n"
        )
        result = validate_campaign(campaign)
        assert not result.valid
        assert any("batch_compatible" in e for e in result.errors)
