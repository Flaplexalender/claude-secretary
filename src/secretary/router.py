"""Model routing — pick the right model for the task complexity.

Scoring heuristics adapted from Captain v2's auto_select.py.
Maps complexity → tier → model config.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import SecretaryConfig, ModelTier

# Premium request multipliers per model (Copilot billing — updated 2026-07)
# Source: docs.github.com/en/copilot/concepts/billing/copilot-requests
TIER_MULTIPLIERS: dict[str, float] = {
    "gpt-4.1": 0.0,             # FREE — included in all plans
    "gpt-4o": 0.0,              # FREE — included in all plans
    "gpt-5-mini": 0.0,          # FREE — included in all plans
    "claude-haiku-4.5": 0.33,
    "claude-sonnet-4": 1.0,
    "claude-sonnet-4.5": 1.0,
    "claude-sonnet-4.6": 1.0,
    "claude-opus-4.5": 3.0,
    "claude-opus-4.6": 3.0,
    "claude-opus-4.7": 3.0,
    "oracle-ensemble": 0.0,  # Mixed — tracked per-turn in oracle.py
}

# Deep tier always uses Opus (same multiplier)
# Oracle tier uses free workers + Opus checkpoints (tracked internally)


def get_premium_cost(model: str) -> float:
    """Return the premium request multiplier for a model."""
    return TIER_MULTIPLIERS.get(model, 1.0)


# --- keyword patterns ---

_HIGH_PATTERNS = re.compile(
    r"\b(refactor|architect|migrat|redesign|multi.?file|cross.?cutting|"
    r"security.?audit|complex.?debug|overhaul|rewrite)\b", re.I
)
_MEDIUM_PATTERNS = re.compile(
    r"\b(implement|fix.?bug|write.?code|add.?feature|debug|email|calendar|"
    r"research|summarize|analyze|review)\b", re.I
)
_LOW_PATTERNS = re.compile(
    r"\b(fix.?typo|rename|format|simple|trivial|quick|"
    r"what.?is|how.?do|explain|list|show)\b", re.I
)
_SCOPE_REDUCERS = re.compile(
    r"\b(just.?this.?file|only.?one|single.?change|small.?fix)\b", re.I
)
_QUESTION_PATTERN = re.compile(
    r"^\s*(what|how|where|when|who|why|which|is|are|do|does|can|could|should)\b", re.I
)
_FILE_PATH_PATTERN = re.compile(
    r"(?:[a-zA-Z]:[\\/]|\b(?:src|tests|lib|app|config)[\\/])[\w/\\.\-]+"
)
_CODE_BLOCK_PATTERN = re.compile(r"```")
_NUMBERED_STEP_PATTERN = re.compile(r"^\s*\d+[\.\)]\s", re.M)


@dataclass
class RoutingDecision:
    tier: str
    model: str
    max_turns: int
    max_budget_usd: float
    reason: str
    confidence: str = "medium"
    premium_multiplier: float = 1.0  # Copilot premium requests per API call


def estimate_complexity(task: str) -> tuple[str, int, str, str]:
    """Score a task and return (complexity_level, score, reason, confidence).

    confidence is 'high' when the score strongly indicates a tier (>= 4 or <= -1),
    otherwise 'medium'.
    """
    score = 0
    reasons: list[str] = []

    # keyword scoring
    high_hits = _HIGH_PATTERNS.findall(task)
    if high_hits:
        score += 2 * len(high_hits)
        reasons.append(f"complex keywords: {', '.join(high_hits[:3])}")

    med_hits = _MEDIUM_PATTERNS.findall(task)
    if med_hits:
        score += len(med_hits)
        reasons.append(f"standard keywords: {', '.join(med_hits[:3])}")

    low_hits = _LOW_PATTERNS.findall(task)
    if low_hits:
        score -= len(low_hits)
        reasons.append(f"simple keywords: {', '.join(low_hits[:3])}")

    if _SCOPE_REDUCERS.search(task):
        score -= 1
        reasons.append("scope reducer")

    # question framing — questions are simpler; stronger penalty when combined
    # with low-complexity keywords (e.g. "what is refactoring" → clearly low)
    if _QUESTION_PATTERN.search(task):
        if low_hits:
            score -= 2
            reasons.append("question framing + simple keywords")
        else:
            score -= 1
            reasons.append("question framing")

    # file paths — indicate code-level work
    file_paths = _FILE_PATH_PATTERN.findall(task)
    if file_paths:
        score += 2
        reasons.append(f"file paths: {', '.join(file_paths[:2])}")

    # code blocks — indicate complex technical task
    code_blocks = len(_CODE_BLOCK_PATTERN.findall(task))
    if code_blocks >= 2:  # opening + closing = 1 block
        score += 2
        reasons.append(f"code blocks ({code_blocks // 2})")

    # length heuristic
    words = len(task.split())
    if words > 80:
        score += 2
        reasons.append(f"long prompt ({words} words)")
    elif words > 40:
        score += 1
    elif words <= 5:
        score -= 1
        reasons.append("very short prompt")

    # multi-step detection
    numbered = len(_NUMBERED_STEP_PATTERN.findall(task))
    if numbered >= 4:
        score += 3
        reasons.append(f"{numbered} numbered steps")
    elif numbered >= 2:
        score += 1

    # classify
    if score >= 4:
        level = "high"
    elif score <= 0:
        level = "low"
    else:
        level = "medium"

    # confidence: high when score is decisive, medium otherwise
    confidence = "high" if score >= 4 or score <= -1 else "medium"

    return level, score, "; ".join(reasons) if reasons else "default", confidence


def select_model(
    config: SecretaryConfig,
    task: str,
    force_tier: str | None = None,
    learned_stats: object | None = None,
    source: str = "",
    campaign: str = "",
) -> RoutingDecision:
    """Pick the best model tier for this task.

    When agent_prefix is true and always_opus optimization is on,
    ALL tasks are routed to Opus regardless of complexity scoring.

    When learned_stats is provided (a RoutingStats object from learned_router),
    and agent_prefix is false, the learned router can override
    the static scoring if it has enough historical data.
    """
    if force_tier and force_tier in config.routing.tiers:
        tier_name = force_tier
        reason = f"forced tier: {force_tier}"
        confidence = "high"  # forced tier = high confidence
    elif config.agent_prefix and config.optimizations.always_opus and "high" in config.routing.tiers:
        tier_name = "high"
        _original_tier, _score, _orig_reason, _ = estimate_complexity(task)
        reason = f"always_opus — original: {_original_tier} ({_orig_reason})"
        confidence = "high"
    else:
        tier_name, _score, reason, confidence = estimate_complexity(task)

        # Learned router override (paid mode only, when stats available)
        if learned_stats is not None and not config.agent_prefix:
            from .learned_router import learned_route, RoutingStats
            if isinstance(learned_stats, RoutingStats):
                available = [t for t in config.routing.tiers if t in ("free", "low", "medium", "high")]
                decision = learned_route(learned_stats, task, available, source, campaign)
                if decision.recommended_tier and decision.confidence in ("learned", "explored"):
                    static_tier = tier_name
                    tier_name = decision.recommended_tier
                    reason = f"learned({decision.confidence}): {decision.reason} — static was '{static_tier}'"
                    confidence = decision.confidence

        # Paid-mode optimization: use free models (0× multiplier) for trivial tasks
        if (not config.agent_prefix
                and config.optimizations.use_free_models
                and tier_name == "low"
                and "free" in config.routing.tiers):
            tier_name = "free"
            reason = f"free-model routing (0× premium) — {reason}"
        # fall back to default if tier not configured
        if tier_name not in config.routing.tiers:
            tier_name = config.routing.default_tier

    tier: ModelTier = config.routing.tiers[tier_name]
    return RoutingDecision(
        tier=tier_name,
        model=tier.model,
        max_turns=tier.max_turns,
        max_budget_usd=tier.max_budget_usd,
        reason=reason,
        confidence=confidence,
        premium_multiplier=get_premium_cost(tier.model),
    )
