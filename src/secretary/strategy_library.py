"""Strategy Library — Voyager-inspired learned knowledge store (Layer 13).

After each successful task, extract a reusable strategy description and store it
indexed by category.  At prompt-build time, retrieve matching strategies and inject
them as task-specific context.  This replaces global prompt changes (which cause
cross-task interference) with **category-scoped learned knowledge**.

Research basis:
- Voyager (NVIDIA, 2023): ever-growing skill library + retrieval for lifelong learning
- OPRO (Google DeepMind, ICLR 2024): trajectory memory drives informed optimization
- Secretary Exp #10 lesson: generic prompt hint changes have cross-task interference

Design: category-based retrieval (no embeddings needed — reuses extract_category),
LLM-generated strategy descriptions from successful RunLogEntry data, decay + dedup
for quality control.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

from .learned_router import extract_category

logger = logging.getLogger(__name__)

# ── Limits ────────────────────────────────────────────────────

MAX_STRATEGIES_PER_CATEGORY = 5   # top-K injected per prompt
MAX_TOTAL_STRATEGIES = 80         # global cap — prune lowest-quality first
MIN_QUALITY_SCORE = 0.3           # below this → prune on next consolidation
DECAY_RATE = 0.95                 # quality *= DECAY_RATE each consolidation cycle


# ── Data model ────────────────────────────────────────────────

@dataclass
class Strategy:
    """A reusable strategy extracted from a successful task."""

    category: str           # e.g. "email", "research", "file", "implement"
    description: str        # 1-2 sentence summary of the strategy
    source_task: str        # abbreviated task text that produced this strategy
    tools_used: list[str] = field(default_factory=list)
    quality_score: float = 1.0   # decays over time, boosted on reuse
    use_count: int = 0           # how many times injected into a prompt
    success_count: int = 0       # how many times the task succeeded when injected
    created_at: float = 0.0      # unix timestamp
    last_used_at: float = 0.0    # unix timestamp

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            self.created_at = time.time()


# ── Core library ──────────────────────────────────────────────

class StrategyLibrary:
    """Category-indexed store of learned strategies with quality tracking."""

    def __init__(self, path: Path | None = None) -> None:
        self._strategies: list[Strategy] = []
        self._path = path
        if path and path.exists():
            self._load(path)

    # ── Retrieval ─────────────────────────────────────────────

    def retrieve(self, category: str, max_results: int = 3) -> list[Strategy]:
        """Get top strategies for a category, sorted by quality score."""
        matching = [s for s in self._strategies if s.category == category]
        matching.sort(key=lambda s: s.quality_score, reverse=True)
        return matching[:max_results]

    def format_for_prompt(self, category: str, max_results: int = 3) -> str:
        """Format retrieved strategies as a prompt section.

        Returns empty string if no strategies match.
        """
        strategies = self.retrieve(category, max_results)
        if not strategies:
            return ""

        lines = ["## Learned Strategies"]
        for s in strategies:
            success_rate = (
                f"{s.success_count}/{s.use_count}" if s.use_count > 0
                else "new"
            )
            lines.append(f"- [{success_rate}] {s.description}")
            s.use_count += 1
            s.last_used_at = time.time()

        if self._path:
            self._save(self._path)

        return "\n".join(lines)

    # ── Addition ──────────────────────────────────────────────

    def add_strategy(self, strategy: Strategy) -> bool:
        """Add a strategy if it's not a duplicate.

        Returns True if added, False if duplicate detected.
        """
        # Dedup: skip if same category + very similar description
        for existing in self._strategies:
            if (existing.category == strategy.category
                    and _similar(existing.description, strategy.description)):
                # Boost the existing one instead of adding duplicate
                existing.quality_score = min(
                    existing.quality_score + 0.1, 2.0,
                )
                logger.debug(
                    "Duplicate strategy for %s — boosted existing",
                    strategy.category,
                )
                if self._path:
                    self._save(self._path)
                return False

        self._strategies.append(strategy)

        # Enforce global cap
        if len(self._strategies) > MAX_TOTAL_STRATEGIES:
            self._prune()

        if self._path:
            self._save(self._path)

        logger.info(
            "Added strategy for %s: %s",
            strategy.category,
            strategy.description[:80],
        )
        return True

    def record_outcome(self, category: str, success: bool) -> None:
        """Record whether the most recently injected strategies led to success."""
        recent = [
            s for s in self._strategies
            if s.category == category and s.use_count > 0
        ]
        recent.sort(key=lambda s: s.last_used_at, reverse=True)

        for s in recent[:MAX_STRATEGIES_PER_CATEGORY]:
            if success:
                s.success_count += 1
                s.quality_score = min(s.quality_score + 0.05, 2.0)
            else:
                s.quality_score = max(s.quality_score - 0.1, 0.0)

        if self._path:
            self._save(self._path)

    # ── Maintenance ───────────────────────────────────────────

    def consolidate(self) -> int:
        """Decay all quality scores and prune low-quality strategies.

        Returns count of pruned strategies.
        """
        for s in self._strategies:
            s.quality_score *= DECAY_RATE

        before = len(self._strategies)
        self._prune()
        pruned = before - len(self._strategies)

        if self._path:
            self._save(self._path)

        if pruned:
            logger.info("Consolidated: pruned %d strategies", pruned)
        return pruned

    def _prune(self) -> None:
        """Remove low-quality strategies and enforce caps."""
        # Remove below min quality
        self._strategies = [
            s for s in self._strategies
            if s.quality_score >= MIN_QUALITY_SCORE
        ]

        # Enforce per-category cap
        by_cat: dict[str, list[Strategy]] = {}
        for s in self._strategies:
            by_cat.setdefault(s.category, []).append(s)

        kept: list[Strategy] = []
        for cat, strats in by_cat.items():
            strats.sort(key=lambda s: s.quality_score, reverse=True)
            kept.extend(strats[:MAX_STRATEGIES_PER_CATEGORY])

        # Enforce global cap
        kept.sort(key=lambda s: s.quality_score, reverse=True)
        self._strategies = kept[:MAX_TOTAL_STRATEGIES]

    # ── Stats ─────────────────────────────────────────────────

    @property
    def size(self) -> int:
        return len(self._strategies)

    def categories(self) -> dict[str, int]:
        """Returns {category: count} mapping."""
        counts: dict[str, int] = {}
        for s in self._strategies:
            counts[s.category] = counts.get(s.category, 0) + 1
        return counts

    def all_strategies(self) -> list[Strategy]:
        """Return a copy of all strategies."""
        return list(self._strategies)

    # ── Persistence ───────────────────────────────────────────

    def _save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(s) for s in self._strategies]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._strategies = [Strategy(**entry) for entry in raw]
            logger.debug("Loaded %d strategies from %s", len(self._strategies), path)
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Failed to load strategies from %s: %s", path, exc)
            self._strategies = []


# ── Strategy extraction via LLM ───────────────────────────────

_EXTRACT_PROMPT = """\
You are analyzing a successful AI agent task to extract a reusable strategy.

Task: {task}
Category: {category}
Tools used: {tools}
Output preview: {output}
Duration: {duration}s, Turns: {turns}

Extract a 1-2 sentence strategy description that captures:
1. What approach worked (tool sequence, parallel patterns, key decisions)
2. Why it was effective (speed, reliability, completeness)

Respond with ONLY a JSON object:
{{"description": "...", "tools_pattern": ["tool1", "tool2"]}}
"""


def extract_strategy_from_entry(
    task: str,
    category: str,
    tools_used: list[str],
    output_preview: str,
    duration_s: float,
    num_turns: int,
    base_url: str,
    model: str = "claude-haiku-4.5",
) -> Strategy | None:
    """Use Haiku to extract a reusable strategy from a successful task.

    Returns Strategy object or None on failure.
    """
    prompt = _EXTRACT_PROMPT.format(
        task=task[:300],
        category=category,
        tools=", ".join(tools_used[:10]),
        output=output_preview[:300],
        duration=duration_s,
        turns=num_turns,
    )

    try:
        resp = httpx.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 200,
                "temperature": 0.1,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # Handle code fences
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]

        data = json.loads(content)
        return Strategy(
            category=category,
            description=data["description"],
            source_task=task[:200],
            tools_used=data.get("tools_pattern", tools_used[:5]),
        )
    except Exception as exc:
        logger.warning("Strategy extraction failed: %s", exc)
        return None


def maybe_extract_strategy(
    entry_task: str,
    entry_success: bool,
    entry_tools: list[str],
    entry_output: str,
    entry_duration: float,
    entry_turns: int,
    entry_source: str,
    entry_campaign: str,
    library: StrategyLibrary,
    base_url: str,
    model: str = "claude-haiku-4.5",
) -> Strategy | None:
    """Conditionally extract a strategy from a completed task.

    Only extracts from successful tasks that used tools (no trivial tasks).
    Returns the Strategy if extracted and added, None otherwise.
    """
    if not entry_success:
        return None
    if len(entry_tools) < 2:
        return None  # trivial tasks don't produce useful strategies

    category = extract_category(
        entry_task, source=entry_source, campaign=entry_campaign,
    )

    # Skip if category is already well-represented
    existing = library.retrieve(category, MAX_STRATEGIES_PER_CATEGORY)
    if len(existing) >= MAX_STRATEGIES_PER_CATEGORY:
        # Only extract if quality is low enough to potentially replace
        min_quality = min(s.quality_score for s in existing)
        if min_quality > 0.8:
            return None

    strategy = extract_strategy_from_entry(
        task=entry_task,
        category=category,
        tools_used=entry_tools,
        output_preview=entry_output,
        duration_s=entry_duration,
        num_turns=entry_turns,
        base_url=base_url,
        model=model,
    )

    if strategy:
        added = library.add_strategy(strategy)
        if added:
            return strategy

    return None


# ── Helpers ───────────────────────────────────────────────────

def _similar(a: str, b: str) -> bool:
    """Quick similarity check — word overlap > 70%."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return False
    overlap = len(words_a & words_b)
    smaller = min(len(words_a), len(words_b))
    return overlap / smaller > 0.7


def load_library(path: Path) -> StrategyLibrary:
    """Load or create a strategy library."""
    return StrategyLibrary(path)
