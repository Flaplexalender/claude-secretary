"""Integration layer: hook TextGrad evolution into goal_self_improve.py.

This module provides the bridge between:
  1. Failed task traces (from data/run_log.jsonl)
  2. Prompt evolution (textgrad_evolution.py)
  3. Autoresearch experiments (campaigns/)
  4. Logging + reporting (data/textgrad_evolved_prompts.jsonl)

Main entry point: run_textgrad_analysis_cycle() — called from goal_self_improve.py
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any
from datetime import datetime, timezone, timedelta

from .textgrad_evolution import (
    generate_evolved_prompts,
    save_evolution_round,
    format_evolution_report,
    variant_to_experiment_config,
    PromptVariant,
    PromptEvolutionRound,
)

log = logging.getLogger("secretary.textgrad_integration")


def _load_recent_failures(
    run_log_path: Path,
    hours_ago: int = 24,
    min_failures: int = 3,
) -> list[dict[str, Any]]:
    """Load recent failed traces from run_log.jsonl.

    Filters for:
    - Recent traces (last N hours)
    - Failed runs (judge_score < 1.0 or judge_reason present)
    - Removes duplicates by task_id
    """
    if not run_log_path.exists():
        log.warning("run_log.jsonl not found at %s", run_log_path)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    failures: dict[str, dict] = {}  # task_id -> most recent failure

    try:
        with open(run_log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Filter by timestamp
                ts_str = entry.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue

                if ts < cutoff:
                    continue

                # Filter by failure
                judge_score = entry.get("judge_score", 1.0)
                judge_reason = entry.get("judge_reason", "")
                if judge_score >= 1.0 and not judge_reason:
                    continue  # Success, skip

                task_id = entry.get("task_id", "unknown")
                if task_id not in failures or ts > datetime.fromisoformat(
                    failures[task_id].get("timestamp", "").replace("Z", "+00:00")
                ):
                    failures[task_id] = entry

    except Exception as exc:
        log.warning("Error loading run_log.jsonl: %s", exc)
        return []

    result = list(failures.values())
    if len(result) < min_failures:
        log.info("Only %d failures in last %d hours (min %d)", len(result), hours_ago, min_failures)
        return result

    log.info("Loaded %d unique failures from last %d hours", len(result), hours_ago)
    return result


def _categorize_failures(
    failures: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group failures by task category (email, calendar, file, implement, research)."""
    categorized: dict[str, list] = {
        "email": [],
        "calendar": [],
        "file": [],
        "implement": [],
        "research": [],
    }

    for failure in failures:
        category = failure.get("category", "file")
        if category in categorized:
            categorized[category].append(failure)
        else:
            categorized["file"].append(failure)

    return categorized


def _select_traces_for_evolution(
    failures: list[dict[str, Any]],
    max_traces: int = 5,
) -> list[dict[str, Any]]:
    """Select the most informative failures for prompt evolution.

    Heuristic: prioritize failures with detailed judge_reason (more info)
    and recent timestamps (more relevant to current state).
    """
    def score_informativeness(failure: dict) -> float:
        score = 0.0
        # Length of judge_reason (more detail = higher score)
        reason = failure.get("judge_reason", "")
        score += min(len(reason) / 200.0, 1.0) * 0.6
        # Recency
        ts_str = failure.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            score += max(1.0 - (age_hours / 24.0), 0.0) * 0.4
        except (ValueError, AttributeError):
            pass
        return score

    sorted_failures = sorted(failures, key=score_informativeness, reverse=True)
    return sorted_failures[:max_traces]


def run_textgrad_analysis_cycle(
    original_prompt: str,
    data_root: Path,
    base_url: str,
    category: str = "general",
    hours_ago: int = 24,
    min_failures: int = 3,
) -> PromptEvolutionRound | None:
    """Run a complete TextGrad analysis cycle.

    Steps:
    1. Load recent failures from run_log.jsonl (last N hours)
    2. Filter for most informative traces
    3. Generate evolved prompts using TextGrad (Haiku)
    4. Save variants to data/textgrad_evolved_prompts.jsonl
    5. Return PromptEvolutionRound with results

    Args:
        original_prompt: Current system prompt to evolve
        data_root: Path to data/ directory
        base_url: LLM API base URL
        category: Task category for context
        hours_ago: Look back this many hours for failures
        min_failures: Minimum failures required to proceed

    Returns:
        PromptEvolutionRound if successful, None otherwise
    """
    run_log_path = data_root / "run_log.jsonl"
    output_path = data_root / "textgrad_evolved_prompts.jsonl"

    # 1. Load recent failures
    all_failures = _load_recent_failures(
        run_log_path,
        hours_ago=hours_ago,
        min_failures=min_failures,
    )

    if not all_failures:
        log.warning("Insufficient failures for evolution (need >=%d)", min_failures)
        return None

    # 2. Select most informative traces
    selected = _select_traces_for_evolution(all_failures, max_traces=5)
    log.info("Selected %d/%d failures for evolution analysis", len(selected), len(all_failures))

    # 3. Generate evolved prompts
    round_obj = generate_evolved_prompts(
        original_prompt=original_prompt,
        traces=selected,
        base_url=base_url,
        category=category,
        model="claude-haiku-4.5",
    )

    if not round_obj:
        log.error("Failed to generate evolved prompts")
        return None

    # 4. Save to persistent log
    save_evolution_round(round_obj, output_path)

    # 5. Log report
    report = format_evolution_report(round_obj)
    log.info(report)

    return round_obj


def create_autoresearch_experiments(
    evolution_round: PromptEvolutionRound,
) -> dict[str, Any]:
    """Convert evolved variants into autoresearch experiment configs.

    Output format: ready to inject into campaigns/textgrad_autoresearch.yaml
    """
    experiments = {
        "evolution_round": evolution_round.round_id,
        "timestamp": evolution_round.timestamp,
        "num_variants": len(evolution_round.variants),
        "guided_experiments": [
            variant_to_experiment_config(v) for v in evolution_round.variants
        ],
    }
    return experiments


def summarize_evolution_history(
    output_path: Path,
    num_rounds: int = 5,
) -> str:
    """Summarize recent evolution rounds and improvements."""
    if not output_path.exists():
        return "(no evolution history)"

    rounds: list[PromptEvolutionRound] = []
    try:
        with open(output_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    variants = [
                        PromptVariant(**v) for v in data.get("variants", [])
                    ]
                    round_obj = PromptEvolutionRound(
                        round_id=data["round_id"],
                        timestamp=data["timestamp"],
                        original_prompt=data["original_prompt"],
                        variants=variants,
                        num_traces_analyzed=data.get("num_traces_analyzed", 0),
                        meta_analysis=data.get("meta_analysis", ""),
                    )
                    rounds.append(round_obj)
    except Exception as exc:
        log.warning("Failed to load history: %s", exc)
        return f"(load error: {exc})"

    if not rounds:
        return "(no evolution rounds yet)"

    # Most recent N rounds
    recent = sorted(rounds, key=lambda r: r.timestamp, reverse=True)[:num_rounds]

    lines = []
    lines.append("\n" + "=" * 80)
    lines.append("TEXTGRAD EVOLUTION HISTORY")
    lines.append("=" * 80)
    lines.append(f"Total rounds: {len(rounds)}")
    lines.append(f"Showing last {len(recent)} rounds:\n")

    for round_obj in recent:
        lines.append(f"[{round_obj.timestamp}] {round_obj.round_id}")
        lines.append(f"  Analyzed: {round_obj.num_traces_analyzed} failure traces")
        lines.append(f"  Generated: {len(round_obj.variants)} variants")
        avg_conf = sum(v.confidence for v in round_obj.variants) / max(len(round_obj.variants), 1)
        lines.append(f"  Avg confidence: {avg_conf:.2f}")
        if round_obj.meta_analysis:
            lines.append(f"  Analysis: {round_obj.meta_analysis[:100]}")
        lines.append("")

    lines.append("=" * 80 + "\n")
    return "\n".join(lines)


# Re-export for convenience
from .textgrad_evolution import PromptVariant

__all__ = [
    "run_textgrad_analysis_cycle",
    "create_autoresearch_experiments",
    "summarize_evolution_history",
    "PromptVariant",
    "PromptEvolutionRound",
]
