"""Prompt Optimizer — closed-loop prompt optimization via OPRO + TextGrad.

Combines three research approaches:
  - OPRO (Yang et al. ICLR 2024): solution trajectory memory — show the optimizer
    ALL previous attempts + scores so it learns from failures
  - ProTeGi/APO (Pryzant et al. EMNLP 2023): natural language "gradients" that
    criticize prompts → targeted mutations instead of blind exploration
  - TextGrad-lite (local, S29): eval failure analysis → specific root causes

Replaces UCB1's blind dimension selection in autoresearch with INFORMED proposals:
  1. Reads trajectory (autoresearch_results.tsv)
  2. Loads TextGrad gradients from latest eval failures
  3. Reads current optimization targets (actual source code)
  4. Builds OPRO-style meta-prompt with full context
  5. Generates a targeted proposal via Haiku
  6. Outputs structured PromptProposal for autoresearch eval pipeline

Integration:
  - campaigns/textgrad_autoresearch.yaml: uses this instead of UCB1
  - Can also be called standalone: python -m secretary.prompt_optimizer
"""
from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx

from .strategy_library import Strategy

from .textgrad_lite import (
    TextualGradient,
    run_textgrad_analysis,
    load_gradients,
    save_gradients,
)

log = logging.getLogger("secretary.prompt_optimizer")

# ── Data classes ──────────────────────────────────────────────


@dataclass
class Experiment:
    """One row from autoresearch_results.tsv."""

    timestamp: str
    commit: str
    eval_score: float
    median: float
    status: str  # baseline, keep, discard
    dimension: str
    description: str
    run_scores: str


@dataclass
class PromptProposal:
    """A targeted prompt change proposal informed by trajectory + gradients.

    More structured than TextualGradient — includes cross-task risk analysis
    and exact code change instructions, informed by what worked/failed before.
    """

    target_file: str
    target_variable: str
    change_description: str  # what to change (natural language)
    exact_change: str  # specific text to add/modify
    rationale: str  # why (from trajectory + gradients)
    expected_improvements: list[str]  # task IDs that should improve
    risk_tasks: list[str]  # task IDs that might regress
    confidence: float
    source_gradient: str  # task_id of the TextGrad gradient that inspired this


# ── Trajectory loading ────────────────────────────────────────


def load_trajectory(tsv_path: Path) -> list[Experiment]:
    """Parse autoresearch_results.tsv into Experiment objects."""
    if not tsv_path.exists():
        log.warning("Trajectory file not found: %s", tsv_path)
        return []

    experiments: list[Experiment] = []
    text = tsv_path.read_text(encoding="utf-8")
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    for row in reader:
        try:
            experiments.append(Experiment(
                timestamp=row.get("timestamp", ""),
                commit=row.get("commit", ""),
                eval_score=float(row.get("eval_score", 0)),
                median=float(row.get("median", 0)),
                status=row.get("status", ""),
                dimension=row.get("dimension", ""),
                description=row.get("description", ""),
                run_scores=row.get("run_scores", ""),
            ))
        except (ValueError, KeyError) as exc:
            log.debug("Skipping malformed TSV row: %s", exc)

    return experiments


def get_current_baseline(experiments: list[Experiment]) -> float:
    """Find the current baseline score (most recent keep or baseline row)."""
    for exp in reversed(experiments):
        if exp.status in ("keep", "baseline"):
            return exp.median
    return 0.0


def analyze_trajectory(experiments: list[Experiment]) -> dict[str, Any]:
    """Extract patterns from experiment history for the meta-prompt.

    Returns a structured summary: what worked, what failed, interference patterns.
    """
    kept = [e for e in experiments if e.status == "keep" and e.dimension != "baseline"]
    discarded = [e for e in experiments if e.status == "discard"]

    # Group by dimension
    dim_stats: dict[str, dict[str, Any]] = {}
    for exp in experiments:
        if exp.dimension == "baseline":
            continue
        if exp.dimension not in dim_stats:
            dim_stats[exp.dimension] = {"kept": [], "discarded": [], "scores": []}
        dim_stats[exp.dimension]["scores"].append(exp.eval_score)
        if exp.status == "keep":
            dim_stats[exp.dimension]["kept"].append(exp.description)
        else:
            dim_stats[exp.dimension]["discarded"].append(exp.description)

    return {
        "total_experiments": len(experiments) - len([e for e in experiments if e.dimension == "baseline"]),
        "kept_count": len(kept),
        "discarded_count": len(discarded),
        "success_rate": len(kept) / max(len(kept) + len(discarded), 1),
        "dimension_stats": dim_stats,
        "baseline": get_current_baseline(experiments),
    }


# ── Read current optimization targets ────────────────────────

# Map target name → (file path, regex pattern to extract current value)
_TARGET_PATTERNS: dict[str, tuple[str, str]] = {
    "direct_agent._TASK_HINTS": (
        "src/secretary/direct_agent.py",
        r"(_TASK_HINTS:\s*dict\[str,\s*str\]\s*=\s*\{[^}]+\})",
    ),
    "oracle._ORACLE_TASK_HINTS": (
        "src/secretary/oracle.py",
        r"(_ORACLE_TASK_HINTS:\s*dict\[str,\s*str\]\s*=\s*\{[^}]+\})",
    ),
    "direct_agent.system_rules": (
        "src/secretary/direct_agent.py",
        r'(RULES:.*?"[,)])',
    ),
    "oracle.worker_rules": (
        "src/secretary/oracle.py",
        r'("RULES:.*?"[,)])',
    ),
    "oracle.checkpoint_rules": (
        "src/secretary/oracle.py",
        r'("ROLE: Senior reviewer.*?"[,)])',
    ),
}


def read_current_targets(project_root: Path) -> dict[str, str]:
    """Read the current value of each optimization target from source files.

    Returns target_name → current code snippet.
    """
    targets: dict[str, str] = {}
    for name, (rel_path, pattern) in _TARGET_PATTERNS.items():
        full_path = project_root / rel_path
        if not full_path.exists():
            log.warning("Target file not found: %s", full_path)
            continue
        source = full_path.read_text(encoding="utf-8")
        match = re.search(pattern, source, re.DOTALL)
        if match:
            # Cap at 500 chars to keep meta-prompt reasonable
            targets[name] = match.group(1)[:500]
        else:
            log.debug("Could not extract %s from %s", name, rel_path)
    return targets


# ── Meta-prompt construction (OPRO-inspired) ──────────────────

_META_PROMPT = """You are optimizing prompt variables for an AI agent system.
Current eval score (baseline): {baseline:.3f}. Goal: improve it.

## Previous Experiments (trajectory)
{trajectory_section}

## Trajectory Patterns
- Total experiments: {total_experiments}. Kept: {kept_count}. Discarded: {discarded_count}.
- Overall success rate: {success_rate:.0%}
{dimension_summary}

## Current Failure Analysis (TextGrad)
{gradient_section}

## Current Optimization Targets
{targets_section}

## Learned Strategy Library
{strategies_section}

## Rules for Your Proposal
1. Propose ONE targeted change to improve the eval score
2. Choose a change that fixes a SPECIFIC failing task (prefer highest-confidence gradient)
3. Consider cross-task interference: previous experiments show some fixes break other tasks
4. Be EXACT: specify the precise text to add/modify in the target variable
5. DO NOT repeat changes that were already tried and discarded
6. Target the variable most likely to fix the identified failure without side effects
7. Prefer ADDING a specific hint for a category over MODIFYING universal rules
8. Keep changes small and focused — one line/hint per proposal

Respond with ONLY a JSON object:
{{
    "target_file": "src/secretary/direct_agent.py or src/secretary/oracle.py",
    "target_variable": "one of: _TASK_HINTS | _ORACLE_TASK_HINTS | system_rules | worker_rules | checkpoint_rules",
    "change_description": "What to change (1-2 sentences)",
    "exact_change": "The exact text to add or modify (write the actual hint/rule text)",
    "rationale": "Why this should work based on trajectory + failure analysis",
    "expected_improvements": ["task_id", ...],
    "risk_tasks": ["task_id that might regress"],
    "confidence": 0.0-1.0,
    "source_gradient": "task_id of the TextGrad gradient that inspired this"
}}"""


def _format_trajectory(experiments: list[Experiment]) -> str:
    """Format experiment history for the meta-prompt."""
    if not experiments:
        return "No previous experiments. This is the first optimization attempt."

    lines = []
    for i, exp in enumerate(experiments):
        if exp.dimension == "baseline":
            lines.append(f"- BASELINE ({exp.timestamp[:10]}): {exp.median:.3f}")
        else:
            status_icon = "✓ KEPT" if exp.status == "keep" else "✗ DISCARDED"
            lines.append(
                f"- Exp #{i}: [{status_icon}] dimension={exp.dimension} "
                f"score={exp.eval_score:.3f} — {exp.description}"
            )
    return "\n".join(lines)


def _format_dimension_summary(dim_stats: dict[str, Any]) -> str:
    """Format per-dimension success/failure patterns."""
    lines = []
    for dim, stats in sorted(dim_stats.items()):
        n_kept = len(stats["kept"])
        n_disc = len(stats["discarded"])
        avg = sum(stats["scores"]) / max(len(stats["scores"]), 1)
        lines.append(f"- {dim}: {n_kept} kept, {n_disc} discarded, avg score={avg:.3f}")
        if stats["kept"]:
            lines.append(f"  Worked: {'; '.join(stats['kept'][:2])}")
        if stats["discarded"]:
            lines.append(f"  Failed: {'; '.join(stats['discarded'][:2])}")
    return "\n".join(lines) if lines else "- No dimension data yet."


def _format_gradients(gradients: list[TextualGradient]) -> str:
    """Format TextGrad gradients for the meta-prompt."""
    if not gradients:
        return "No TextGrad analysis available. Propose based on trajectory patterns."

    lines = []
    for i, g in enumerate(gradients, 1):
        lines.append(
            f"Failure {i}: task={g.task_id} (confidence={g.confidence:.2f})\n"
            f"  Root cause: {g.root_cause}\n"
            f"  Suggested target: {g.target}\n"
            f"  Suggested change: {g.suggested_change}"
        )
    return "\n".join(lines)


def _format_targets(targets: dict[str, str]) -> str:
    """Format current optimization target values."""
    if not targets:
        return "Could not read current target values."

    lines = []
    for name, value in targets.items():
        # Truncate long values
        display = value[:300] + "..." if len(value) > 300 else value
        lines.append(f"### {name}\n```python\n{display}\n```")
    return "\n".join(lines)


def _format_strategies(strategies: list[Strategy]) -> str:
    """Format strategy library entries for the meta-prompt."""
    if not strategies:
        return "No learned strategies yet. This is the first cycle with strategy context."

    by_cat: dict[str, list[Strategy]] = {}
    for s in strategies:
        by_cat.setdefault(s.category, []).append(s)

    lines = []
    for cat, strats in sorted(by_cat.items()):
        strats.sort(key=lambda s: s.quality_score, reverse=True)
        lines.append(f"### {cat}")
        for s in strats[:3]:
            rate = f"{s.success_count}/{s.use_count}" if s.use_count else "new"
            lines.append(f"- [{rate}] (q={s.quality_score:.2f}) {s.description}")
    return "\n".join(lines)


def build_meta_prompt(
    experiments: list[Experiment],
    gradients: list[TextualGradient],
    targets: dict[str, str],
    strategies: list[Strategy] | None = None,
) -> str:
    """Build OPRO-style meta-prompt combining trajectory + gradients + current state."""
    analysis = analyze_trajectory(experiments)

    return _META_PROMPT.format(
        baseline=analysis["baseline"],
        trajectory_section=_format_trajectory(experiments),
        total_experiments=analysis["total_experiments"],
        kept_count=analysis["kept_count"],
        discarded_count=analysis["discarded_count"],
        success_rate=analysis["success_rate"],
        dimension_summary=_format_dimension_summary(analysis["dimension_stats"]),
        gradient_section=_format_gradients(gradients),
        targets_section=_format_targets(targets),
        strategies_section=_format_strategies(strategies or []),
    )


# ── Proposal generation ───────────────────────────────────────


def generate_proposal(
    meta_prompt: str,
    base_url: str,
    model: str = "claude-haiku-4.5",
) -> PromptProposal | None:
    """Call Haiku with the OPRO meta-prompt to generate a targeted proposal."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": meta_prompt}],
        "max_tokens": 800,
        "temperature": 0.2,  # Slightly creative but mostly deterministic
    }
    url = f"{base_url.rstrip('/')}/v1/chat/completions"

    try:
        resp = httpx.post(url, json=payload, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()

        # Parse JSON (handle code fences)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text)

        proposal = PromptProposal(
            target_file=str(parsed.get("target_file", "")),
            target_variable=str(parsed.get("target_variable", "")),
            change_description=str(parsed.get("change_description", "")),
            exact_change=str(parsed.get("exact_change", "")),
            rationale=str(parsed.get("rationale", "")),
            expected_improvements=list(parsed.get("expected_improvements", [])),
            risk_tasks=list(parsed.get("risk_tasks", [])),
            confidence=float(parsed.get("confidence", 0.5)),
            source_gradient=str(parsed.get("source_gradient", "")),
        )
        log.info(
            "Generated proposal: %s → %s (confidence=%.2f)",
            proposal.target_variable,
            proposal.change_description[:80],
            proposal.confidence,
        )
        return proposal

    except Exception as exc:
        log.warning("Proposal generation failed: %s", exc)
        return None


# ── Campaign integration ──────────────────────────────────────


def format_as_agent_instructions(proposal: PromptProposal) -> str:
    """Format a PromptProposal as instructions for the autoresearch agent.

    The agent receives this as a "guided" task instead of UCB1 blind exploration.
    """
    return (
        f"## TextGrad-Guided Change (confidence: {proposal.confidence:.2f})\n\n"
        f"**Target file:** {proposal.target_file}\n"
        f"**Target variable:** {proposal.target_variable}\n\n"
        f"### What to change\n{proposal.change_description}\n\n"
        f"### Exact change\n{proposal.exact_change}\n\n"
        f"### Rationale\n{proposal.rationale}\n\n"
        f"### Expected improvements\n"
        f"Tasks that should improve: {', '.join(proposal.expected_improvements)}\n"
        f"Tasks at risk of regression: {', '.join(proposal.risk_tasks)}\n\n"
        f"**Apply this change, then run eval 3x to validate.**"
    )


# ── Full pipeline ─────────────────────────────────────────────


def run_optimization_cycle(
    base_url: str,
    project_root: Path,
    eval_results_path: Path | None = None,
    eval_tasks_path: Path | None = None,
    gradients_cache_path: Path | None = None,
    strategies: list[Strategy] | None = None,
    model: str = "claude-haiku-4.5",
) -> PromptProposal | None:
    """Full closed-loop optimization cycle.

    1. Load trajectory from autoresearch_results.tsv
    2. Load or generate TextGrad gradients
    3. Read current optimization targets
    4. Build OPRO meta-prompt (with optional strategy library context)
    5. Generate targeted proposal

    Returns a PromptProposal ready for the autoresearch eval pipeline,
    or None if no proposal could be generated.
    """
    # 1. Load trajectory
    tsv_path = project_root / "data" / "autoresearch_results.tsv"
    experiments = load_trajectory(tsv_path)
    baseline = get_current_baseline(experiments)
    log.info("Loaded %d experiments, baseline=%.3f", len(experiments), baseline)

    # 2. Load or generate gradients
    gradients: list[TextualGradient] = []
    if gradients_cache_path and gradients_cache_path.exists():
        gradients = load_gradients(gradients_cache_path)
        log.info("Loaded %d cached gradients", len(gradients))

    if not gradients:
        # Try to generate from eval results
        if eval_results_path is None:
            candidates = sorted(project_root.glob("data/autoresearch_eval_*.json"))
            if candidates:
                eval_results_path = candidates[-1]

        if eval_tasks_path is None:
            eval_tasks_path = project_root / "eval_tasks.json"

        if eval_results_path and eval_results_path.exists() and eval_tasks_path.exists():
            gradients = run_textgrad_analysis(
                base_url=base_url,
                eval_results_path=eval_results_path,
                eval_tasks_path=eval_tasks_path,
                model=model,
                max_gradients=5,
            )
            # Cache for reuse
            if gradients and gradients_cache_path:
                save_gradients(gradients, gradients_cache_path)

    # 3. Read current targets
    targets = read_current_targets(project_root)
    log.info("Read %d optimization targets", len(targets))

    # 4. Build meta-prompt
    meta_prompt = build_meta_prompt(experiments, gradients, targets, strategies)

    # 5. Generate proposal
    proposal = generate_proposal(meta_prompt, base_url, model)

    if proposal:
        log.info(
            "Optimization cycle complete: %s → %s (confidence=%.2f, gradient=%s)",
            proposal.target_variable,
            proposal.change_description[:60],
            proposal.confidence,
            proposal.source_gradient,
        )
    else:
        log.warning("Optimization cycle produced no proposal")

    return proposal


def save_proposal(proposal: PromptProposal, path: Path) -> None:
    """Save a proposal to JSON."""
    path.write_text(json.dumps(asdict(proposal), indent=2), encoding="utf-8")


def load_proposal(path: Path) -> PromptProposal | None:
    """Load a proposal from JSON."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PromptProposal(**data)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("Failed to load proposal: %s", exc)
        return None
