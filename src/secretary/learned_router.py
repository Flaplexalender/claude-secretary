"""Learned Router — adaptive cost-aware model routing from run_log data.

Uses Bayesian bandit approach (Thompson Sampling) to learn which tier
handles which task categories best, then routes to the cheapest tier
that meets a quality threshold.

Degrades gracefully to static scoring when data is insufficient.

Research basis:
- RouteLLM (LMSYS, 2024): preference data → learned routing, 2× cost reduction
- Hybrid LLM (ICLR 2024): predicted difficulty + quality threshold → dynamic routing
- Thompson Sampling: Bayesian exploration-exploitation for multi-armed bandits
"""
from __future__ import annotations

import json
import logging
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .run_log import RunLog, RunLogEntry

log = logging.getLogger(__name__)

# Minimum observations per (category, tier) before we trust the learned signal
_MIN_OBSERVATIONS = 3

# Quality threshold: route to cheapest tier with success rate >= this
_QUALITY_THRESHOLD = 0.75

# Exploration probability: sample a random tier to gather data
_EXPLORE_PROB = 0.10

# Premium cost multipliers by tier name (matches router.py TIER_MULTIPLIERS concept)
_TIER_COSTS: dict[str, float] = {
    "free": 0.0,
    "low": 0.33,
    "medium": 1.0,
    "high": 3.0,
    "deep": 3.0,
    "oracle": 0.0,
}

# Ordered tiers from cheapest to most expensive (for selecting cheapest viable)
TIER_ORDER = ["free", "low", "medium", "high"]


@dataclass
class TierStats:
    """Success/failure counts for a (category, tier) pair.

    Models a Beta(successes+1, failures+1) distribution.
    """
    successes: int = 0
    failures: int = 0
    total_turns: int = 0
    total_cost_usd: float = 0.0

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        """Point estimate: Beta distribution mean."""
        return (self.successes + 1) / (self.successes + self.failures + 2)

    @property
    def avg_turns(self) -> float:
        return self.total_turns / max(self.total, 1)

    def thompson_sample(self) -> float:
        """Draw from Beta(successes+1, failures+1) for exploration."""
        return random.betavariate(self.successes + 1, self.failures + 1)

    def has_enough_data(self) -> bool:
        return self.total >= _MIN_OBSERVATIONS


@dataclass
class RoutingStats:
    """Per-category, per-tier performance statistics."""
    # category -> tier -> TierStats
    stats: dict[str, dict[str, TierStats]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(TierStats)))
    total_entries_processed: int = 0


@dataclass
class LearnedRoutingDecision:
    """Result from the learned router."""
    recommended_tier: str | None  # None = insufficient data, use static
    confidence: str  # "learned", "explored", "insufficient"
    reason: str
    category: str
    stats_summary: dict[str, Any] = field(default_factory=dict)


# --- Category extraction ---

# Campaign/source patterns that help categorize tasks
_CATEGORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b(email|gmail|inbox|draft|send|reply)\b", re.I)),
    ("calendar", re.compile(r"\b(calendar|schedule|meeting|event|appointment)\b", re.I)),
    ("code", re.compile(r"\b(refactor|implement|fix|bug|test|code|function|class|module)\b", re.I)),
    ("research", re.compile(r"\b(research|analyze|summarize|investigate|review|read)\b", re.I)),
    ("file-ops", re.compile(r"\b(file|read|write|edit|grep|search|directory)\b", re.I)),
    ("health", re.compile(r"\b(health|status|heartbeat|check|verify|monitor)\b", re.I)),
    ("memory", re.compile(r"\b(memory|remember|consolidat|note|scratchpad)\b", re.I)),
]


def extract_category(task: str, source: str = "", campaign: str = "") -> str:
    """Extract a task category from task text, source, and campaign name.

    Categories are coarse-grained to ensure enough data per bucket.
    """
    # Campaign name is the strongest signal
    campaign_lower = campaign.lower()
    if "email" in campaign_lower or "gmail" in campaign_lower:
        return "email"
    if "calendar" in campaign_lower:
        return "calendar"
    if "improve" in campaign_lower or "self-improve" in campaign_lower:
        return "code"
    if "health" in campaign_lower or "heartbeat" in campaign_lower:
        return "health"
    if "research" in campaign_lower or "autoresearch" in campaign_lower:
        return "research"

    # Source-based categorization
    if source == "ooda":
        return "reactive"
    if source == "goals":
        return "goal-task"

    # Keyword matching on task text
    scores: dict[str, int] = defaultdict(int)
    for cat, pattern in _CATEGORY_PATTERNS:
        hits = len(pattern.findall(task))
        if hits:
            scores[cat] += hits

    if scores:
        return max(scores, key=scores.get)

    return "general"


def build_stats_from_log(run_log: RunLog, max_entries: int = 500) -> RoutingStats:
    """Build routing statistics from run_log entries.

    Processes the most recent entries to build per-(category, tier)
    success/failure counts.
    """
    entries = run_log.recent(max_entries)
    stats = RoutingStats()

    for entry in entries:
        # Extract campaign name from task (first line or known pattern)
        campaign = ""  # run_log doesn't store campaign name directly
        category = extract_category(entry.task, entry.source, campaign)
        tier = entry.tier

        if tier not in TIER_ORDER:
            continue  # skip oracle/deep — different routing strategy

        tier_stats = stats.stats[category][tier]
        if entry.success:
            tier_stats.successes += 1
        else:
            tier_stats.failures += 1
        tier_stats.total_turns += entry.num_turns
        tier_stats.total_cost_usd += entry.cost_usd

    stats.total_entries_processed = len(entries)
    return stats


def learned_route(
    stats: RoutingStats,
    task: str,
    available_tiers: list[str],
    source: str = "",
    campaign: str = "",
    explore_prob: float = _EXPLORE_PROB,
    quality_threshold: float = _QUALITY_THRESHOLD,
) -> LearnedRoutingDecision:
    """Use learned statistics to recommend a tier.

    Returns recommended_tier=None when data is insufficient (caller
    should fall through to static scoring).
    """
    category = extract_category(task, source, campaign)
    cat_stats = stats.stats.get(category, {})

    # Filter to available tiers in cost order
    ordered_tiers = [t for t in TIER_ORDER if t in available_tiers]
    if not ordered_tiers:
        return LearnedRoutingDecision(
            recommended_tier=None,
            confidence="insufficient",
            reason="no available tiers in TIER_ORDER",
            category=category,
        )

    # Check if we have enough data for any tier in this category
    tiers_with_data = {
        t: cat_stats[t] for t in ordered_tiers
        if t in cat_stats and cat_stats[t].has_enough_data()
    }

    if not tiers_with_data:
        return LearnedRoutingDecision(
            recommended_tier=None,
            confidence="insufficient",
            reason=f"category '{category}': <{_MIN_OBSERVATIONS} observations per tier",
            category=category,
        )

    # Exploration: with probability ε, pick a random tier
    if random.random() < explore_prob:
        explored = random.choice(ordered_tiers)
        return LearnedRoutingDecision(
            recommended_tier=explored,
            confidence="explored",
            reason=f"exploration (ε={explore_prob}): random tier '{explored}' for category '{category}'",
            category=category,
            stats_summary=_stats_summary(tiers_with_data),
        )

    # Exploitation: find cheapest tier meeting quality threshold
    # Use Thompson Sampling for each tier, then pick cheapest that exceeds threshold
    for tier in ordered_tiers:
        if tier not in tiers_with_data:
            continue
        ts = tiers_with_data[tier]
        sampled_quality = ts.thompson_sample()
        if sampled_quality >= quality_threshold:
            return LearnedRoutingDecision(
                recommended_tier=tier,
                confidence="learned",
                reason=(
                    f"category '{category}': tier '{tier}' "
                    f"(success={ts.success_rate:.2f}, n={ts.total}, "
                    f"sampled={sampled_quality:.2f} >= {quality_threshold})"
                ),
                category=category,
                stats_summary=_stats_summary(tiers_with_data),
            )

    # No tier met the threshold — recommend the one with highest success rate
    best_tier = max(tiers_with_data, key=lambda t: tiers_with_data[t].success_rate)
    best_stats = tiers_with_data[best_tier]
    return LearnedRoutingDecision(
        recommended_tier=best_tier,
        confidence="learned",
        reason=(
            f"category '{category}': no tier met threshold {quality_threshold}, "
            f"best='{best_tier}' (success={best_stats.success_rate:.2f}, n={best_stats.total})"
        ),
        category=category,
        stats_summary=_stats_summary(tiers_with_data),
    )


def _stats_summary(tiers: dict[str, TierStats]) -> dict[str, Any]:
    """Create a compact summary for logging."""
    return {
        tier: {
            "success_rate": round(s.success_rate, 3),
            "n": s.total,
            "avg_turns": round(s.avg_turns, 1),
        }
        for tier, s in tiers.items()
    }


# --- Persistence ---

def save_stats(stats: RoutingStats, path: Path) -> None:
    """Save routing stats to JSON for persistence across restarts."""
    data = {
        "total_entries_processed": stats.total_entries_processed,
        "stats": {},
    }
    for category, tiers in stats.stats.items():
        data["stats"][category] = {}
        for tier, ts in tiers.items():
            data["stats"][category][tier] = asdict(ts)

    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def load_stats(path: Path) -> RoutingStats | None:
    """Load routing stats from JSON. Returns None if file doesn't exist."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        stats = RoutingStats(total_entries_processed=data.get("total_entries_processed", 0))
        for category, tiers in data.get("stats", {}).items():
            for tier, ts_data in tiers.items():
                stats.stats[category][tier] = TierStats(**ts_data)
        return stats
    except (json.JSONDecodeError, TypeError, KeyError) as e:
        log.warning("Failed to load routing stats from %s: %s", path, e)
        return None


# Need os for fsync
import os
