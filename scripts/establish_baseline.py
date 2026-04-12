#!/usr/bin/env python3
"""Establish baseline EVAL_SCORE and bootstrap the autoresearch TSV log.

Runs the eval harness N times (default 3), computes the median EVAL_SCORE,
and writes the initial row to data/autoresearch_results.tsv.

TSV schema:
  timestamp  commit  eval_score  median  status  dimension  description  run_scores

Dimensions for Secretary:
  model_routing, checkpoint_timing, escalation, worker_config, prompt_hints, voting

Usage:
  python scripts/establish_baseline.py
  python scripts/establish_baseline.py --runs 5
  python scripts/establish_baseline.py --tier oracle --category instruction
  python scripts/establish_baseline.py --output data/autoresearch_results.tsv
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from secretary.config import SecretaryConfig
from secretary.direct_tools import build_tool_registry

# Import eval harness internals
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from run_eval import run_eval, _compute_metrics  # noqa: E402

TSV_HEADER = "timestamp\tcommit\teval_score\tmedian\tstatus\tdimension\tdescription\trun_scores"


def _get_git_commit() -> str:
    """Get short git commit hash, or 'unknown' if not in a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(_PROJECT_ROOT),
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def _load_tasks(
    eval_file: Path | None,
    category: str | None,
    max_tasks: int | None,
) -> list[dict]:
    """Load and filter eval tasks."""
    eval_path = eval_file or (_PROJECT_ROOT / "eval_tasks.json")
    if not eval_path.exists():
        print(f"ERROR: eval_tasks.json not found at {eval_path}", file=sys.stderr)
        sys.exit(1)

    data = json.loads(eval_path.read_text(encoding="utf-8"))
    tasks = data.get("tasks", data) if isinstance(data, dict) else data

    if category:
        tasks = [t for t in tasks if t.get("category") == category]
    if max_tasks:
        tasks = tasks[:max_tasks]
    return tasks


async def _run_single_eval(
    tasks: list[dict],
    config: SecretaryConfig,
    tools: dict,
    tier: str | None,
    max_turns: int | None,
    judge_model: str,
    run_num: int,
    total_runs: int,
) -> float:
    """Run one full eval pass and return EVAL_SCORE."""
    print(f"\n{'='*60}")
    print(f"  Baseline run {run_num}/{total_runs}")
    print(f"{'='*60}")

    results = await run_eval(
        tasks, config, tools, tier, max_turns, judge_model=judge_model,
    )
    metrics = _compute_metrics(results)
    score = metrics["eval_score"]
    print(f"  Run {run_num} EVAL_SCORE: {score:.3f}")
    return score


def _write_tsv_row(
    tsv_path: Path,
    commit: str,
    scores: list[float],
    median_score: float,
) -> None:
    """Append the baseline row to the TSV file (create with header if needed)."""
    is_new = not tsv_path.exists() or tsv_path.stat().st_size == 0

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_scores_str = ",".join(f"{s:.3f}" for s in scores)

    row = (
        f"{timestamp}\t{commit}\t{median_score:.3f}\t{median_score:.3f}\t"
        f"baseline\tbaseline\tInitial baseline ({len(scores)} runs)\t{run_scores_str}"
    )

    with open(tsv_path, "a", encoding="utf-8") as f:
        if is_new:
            f.write(TSV_HEADER + "\n")
        f.write(row + "\n")

    print(f"\nBaseline written to {tsv_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Establish baseline EVAL_SCORE for autoresearch loop"
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Number of eval runs (default: 3). Median is used.",
    )
    parser.add_argument(
        "--tier", default=None,
        help="Force all tasks to this tier.",
    )
    parser.add_argument(
        "--max-tasks", type=int, default=None,
        help="Only run first N tasks.",
    )
    parser.add_argument(
        "--max-turns", type=int, default=None,
        help="Override max_turns per task.",
    )
    parser.add_argument(
        "--category", default=None,
        help="Only run tasks with this category.",
    )
    parser.add_argument(
        "--eval-file", default=None,
        help="Path to eval_tasks.json.",
    )
    parser.add_argument(
        "--output", default=None,
        help="TSV output path (default: data/autoresearch_results.tsv).",
    )
    parser.add_argument(
        "--judge-model", default="claude-haiku-4.5",
        help="Model for llm_judge scoring (default: claude-haiku-4.5).",
    )
    args = parser.parse_args()

    # Load config and tools
    config = SecretaryConfig.load()
    tools = build_tool_registry(
        data_root=config.data_path,
        workspace_root=None,
        unrestricted_files=True,
    )

    tasks = _load_tasks(
        Path(args.eval_file) if args.eval_file else None,
        args.category,
        args.max_tasks,
    )

    commit = _get_git_commit()
    judge_model = "" if args.judge_model == "none" else args.judge_model

    print(f"Establishing baseline: {args.runs} runs × {len(tasks)} tasks")
    print(f"Commit: {commit}")
    print(f"Judge model: {judge_model or 'disabled (contains fallback)'}")

    # Run N eval passes
    scores: list[float] = []
    t0 = time.monotonic()
    for i in range(args.runs):
        score = asyncio.run(
            _run_single_eval(
                tasks, config, tools, args.tier, args.max_turns,
                judge_model, i + 1, args.runs,
            )
        )
        scores.append(score)

    elapsed = time.monotonic() - t0
    median_score = median(scores)

    print(f"\n{'='*60}")
    print(f"  BASELINE RESULTS ({args.runs} runs, {elapsed:.0f}s total)")
    print(f"{'='*60}")
    print(f"  Scores:  {', '.join(f'{s:.3f}' for s in scores)}")
    print(f"  Median:  {median_score:.3f}")
    print(f"  Spread:  {max(scores) - min(scores):.3f}")

    # Write TSV
    tsv_path = Path(args.output) if args.output else (_PROJECT_ROOT / "data" / "autoresearch_results.tsv")
    tsv_path.parent.mkdir(parents=True, exist_ok=True)
    _write_tsv_row(tsv_path, commit, scores, median_score)

    # Success if median >= 0.5
    return 0 if median_score >= 0.5 else 1


if __name__ == "__main__":
    sys.exit(main())
