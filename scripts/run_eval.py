#!/usr/bin/env python3
"""Eval harness — runs eval_tasks.json against the live agent and scores results.

Each task has a known expected output. Scoring:
  - contains: expected substring present in agent's text output (case-insensitive)
  - exact_match: agent text == expected (normalized)
  - llm_judge: real LLM call (Haiku) judges response as binary PASS/FAIL

Aggregate metrics:
  EVAL_SCORE = 0.40 * mean_score + 0.20 * success_rate + 0.20 * first_turn_rate + 0.20 * efficiency
  efficiency = max(0, 1 - (avg_turns - 1) / 9)   # 1 turn = 1.0, 10+ turns = 0.0

Usage:
  python scripts/run_eval.py
  python scripts/run_eval.py --tier free         # force all tasks to free tier
  python scripts/run_eval.py --tier oracle        # force oracle tier
  python scripts/run_eval.py --max-tasks 5        # only run first N tasks
  python scripts/run_eval.py --max-turns 3        # cap turns per task
  python scripts/run_eval.py --category computation  # only run one category

Output (stdout, machine-parseable last line):
  EVAL_SCORE: 0.72  MEAN_SCORE: 0.95  SUCCESS_RATE: 0.80  FIRST_TURN: 0.40  AVG_TURNS: 2.5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import httpx

# Add src/ to path so we can import secretary without installing it
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from secretary.config import SecretaryConfig, _interpolate_env
from secretary.direct_tools import build_tool_registry
from secretary.direct_agent import run as agent_run
from secretary.oracle import oracle_run

log = logging.getLogger(__name__)


# ── Scoring helpers ───────────────────────────────────────────

def _score_contains(text: str, expected: str) -> float:
    """1.0 if expected appears as a substring (case-insensitive), else 0.0."""
    return 1.0 if expected.casefold() in text.casefold() else 0.0


def _score_exact(text: str, expected: str) -> float:
    """1.0 if normalized text exactly equals expected, else 0.0."""
    return 1.0 if text.strip().casefold() == expected.strip().casefold() else 0.0


# ── LLM Judge ─────────────────────────────────────────────────

_LLM_JUDGE_PROMPT = """You are an eval judge. Decide if the RESPONSE adequately answers the TASK.

TASK: {task}
PASS CRITERIA: {criteria}
RESPONSE: {response}

Judge as PASS if the response satisfies ALL of the required criteria listed above.
Judge as FAIL if it misses any required element.

Respond with ONLY a JSON object: {{"verdict": "PASS" or "FAIL", "reason": "<one sentence>"}}"""


def _call_llm_judge(
    base_url: str,
    model: str,
    task_prompt: str,
    criteria: str,
    response_text: str,
) -> tuple[float, str]:
    """Call a cheap model (Haiku) to judge instruction task quality.

    Returns (score, reason). Score is 1.0 (PASS) or 0.0 (FAIL).
    Falls back to contains on API failure (returns (-1.0, "")).
    """
    prompt = _LLM_JUDGE_PROMPT.format(
        task=task_prompt,
        criteria=criteria,
        response=response_text[:2000],  # Cap to avoid huge payloads
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 150,
        "temperature": 0.0,
    }
    url = f"{base_url}/v1/chat/completions"
    try:
        resp = httpx.post(url, json=payload, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        # Parse JSON from response (handle markdown code fences)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text)
        verdict = str(parsed["verdict"]).upper().strip()
        reason = str(parsed.get("reason", ""))
        score = 1.0 if verdict == "PASS" else 0.0
        log.info("LLM judge: %s — %s", verdict, reason)
        return score, reason
    except Exception as exc:
        log.warning("LLM judge failed (%s), falling back to contains", exc)
        return -1.0, ""  # Sentinel: caller falls back to contains


def _score_task(
    result_text: str,
    task: dict[str, Any],
    base_url: str = "",
    judge_model: str = "",
) -> tuple[float, str]:
    """Score a single task result against its expected output.

    Returns (score, judge_reason). judge_reason is non-empty only for llm_judge tasks.
    """
    score_type = task.get("score_type", "contains")
    expected = task.get("expected", "")
    if score_type == "exact_match":
        return _score_exact(result_text, expected), ""
    elif score_type == "contains":
        return _score_contains(result_text, expected), ""
    elif score_type == "llm_judge":
        if base_url and judge_model:
            criteria = task.get("judge_criteria", expected)
            score, reason = _call_llm_judge(
                base_url, judge_model, task["prompt"], criteria, result_text,
            )
            if score >= 0.0:
                return score, reason
        # Fallback to contains
        return _score_contains(result_text, expected), ""
    return 0.0, ""


def _extract_all_text(result: Any) -> str:
    """Combine agent text output with tool outputs for scoring.

    The agent's final result.text is the primary signal. For tasks where
    the answer lives in tool call outputs (not in the final text summary),
    we also scan the message history for tool results.
    """
    parts = [result.text or ""]

    # Scan messages for tool results (covers cases where agent doesn't summarize)
    for msg in result.messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_text = block.get("content", "")
                if isinstance(tool_text, str):
                    parts.append(tool_text)

    return "\n".join(p for p in parts if p)


# ── Main eval loop ────────────────────────────────────────────

async def run_eval(
    tasks: list[dict[str, Any]],
    config: SecretaryConfig,
    tools: dict[str, Any],
    force_tier: str | None,
    max_turns: int | None,
    judge_model: str = "",
) -> list[dict[str, Any]]:
    """Run all eval tasks sequentially and return per-task result dicts."""
    results: list[dict[str, Any]] = []

    for i, task in enumerate(tasks):
        task_id = task["id"]
        prompt = task["prompt"]
        tier = force_tier or task.get("tier")
        turns = max_turns or task.get("max_turns")

        print(f"  [{i + 1:02d}/{len(tasks)}] {task_id:<12} ...", end="", flush=True)
        t0 = time.monotonic()

        try:
            if tier == "oracle":
                result = await oracle_run(
                    task=prompt,
                    config=config,
                    tools=tools,
                    max_turns=turns,
                )
            else:
                result = await agent_run(
                    task=prompt,
                    config=config,
                    force_tier=tier,
                    tools=tools,
                    max_turns=turns,
                )
        except Exception as exc:
            elapsed = time.monotonic() - t0
            print(f" ERROR ({elapsed:.1f}s): {exc}")
            results.append({
                "id": task_id,
                "category": task.get("category", ""),
                "score": 0.0,
                "success": False,
                "first_turn": False,
                "turns": 0,
                "elapsed_s": round(elapsed, 1),
                "error": str(exc),
            })
            continue

        elapsed = time.monotonic() - t0

        # Score using all available text (result.text + tool outputs)
        all_text = _extract_all_text(result)
        base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")
        score, judge_reason = _score_task(all_text, task, base_url=base_url, judge_model=judge_model)

        success = score >= 0.5
        first_turn = success and (result.num_turns == 1)

        status = "PASS" if success else "FAIL"
        print(
            f" {status} "
            f"(score={score:.2f}, turns={result.num_turns}, {elapsed:.1f}s)"
        )

        results.append({
            "id": task_id,
            "category": task.get("category", ""),
            "score": score,
            "success": success,
            "first_turn": first_turn,
            "turns": result.num_turns,
            "elapsed_s": round(elapsed, 1),
            "error": result.error,
            "response_text": all_text[:3000],  # Cap for JSON size
            "judge_reason": judge_reason,
        })

    return results


# ── Metrics ───────────────────────────────────────────────────

def _compute_metrics(results: list[dict[str, Any]]) -> dict[str, float]:
    """Compute aggregate eval metrics from per-task results.

    EVAL_SCORE = 0.40 * mean_score + 0.20 * success_rate + 0.20 * first_turn_rate + 0.20 * efficiency

    mean_score uses raw scores (0.0-1.0) instead of binary pass/fail,
    giving the autoresearch loop a gradient on instruction quality.
    """
    n = len(results)
    if n == 0:
        return {
            "eval_score": 0.0,
            "mean_score": 0.0,
            "success_rate": 0.0,
            "first_turn_rate": 0.0,
            "avg_turns": 0.0,
        }

    scores = [r["score"] for r in results]
    mean_score = sum(scores) / n
    successes = sum(1 for r in results if r["success"])
    first_turns = sum(1 for r in results if r["first_turn"])
    turn_counts = [r["turns"] for r in results if r["turns"] > 0]

    success_rate = successes / n
    first_turn_rate = first_turns / n
    avg_turns = sum(turn_counts) / len(turn_counts) if turn_counts else 0.0

    # EVAL_SCORE = 0.40 * mean_score + 0.20 * success_rate + 0.20 * first_turn_rate + 0.20 * efficiency
    # efficiency: 1 turn = 1.0, 10 turns = 0.0 (linear decay)
    efficiency = max(0.0, 1.0 - (avg_turns - 1.0) / 9.0) if avg_turns >= 1 else 1.0
    eval_score = 0.40 * mean_score + 0.20 * success_rate + 0.20 * first_turn_rate + 0.20 * efficiency

    return {
        "eval_score": round(eval_score, 3),
        "mean_score": round(mean_score, 3),
        "success_rate": round(success_rate, 3),
        "first_turn_rate": round(first_turn_rate, 3),
        "avg_turns": round(avg_turns, 2),
    }


def _print_category_breakdown(results: list[dict[str, Any]]) -> None:
    """Print per-category pass rates."""
    by_cat: dict[str, list[dict]] = {}
    for r in results:
        cat = r.get("category", "unknown")
        by_cat.setdefault(cat, []).append(r)
    print("\nBy category:")
    for cat, rs in sorted(by_cat.items()):
        n = len(rs)
        passed = sum(1 for r in rs if r["success"])
        print(f"  {cat:<15} {passed}/{n} passed")


# ── Entry point ───────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run eval benchmark against live Secretary agent"
    )
    parser.add_argument(
        "--tier",
        default=None,
        help="Force all tasks to this tier (free/low/medium/high/oracle). "
             "Default: auto-route per task.",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Only run the first N tasks (useful for quick smoke tests).",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Override max_turns per task (overrides per-task max_turns too).",
    )
    parser.add_argument(
        "--category",
        default=None,
        help="Only run tasks with this category (computation/file/multi_step/instruction).",
    )
    parser.add_argument(
        "--eval-file",
        default=None,
        help="Path to eval_tasks.json. Default: <project_root>/eval_tasks.json.",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="Write per-task results to this JSON file.",
    )
    parser.add_argument(
        "--judge-model",
        default="claude-haiku-4.5",
        help="Model for llm_judge scoring (default: claude-haiku-4.5). "
             "Set to 'none' to disable and fall back to contains.",
    )
    args = parser.parse_args()

    # Locate eval_tasks.json
    if args.eval_file:
        eval_path = Path(args.eval_file)
    else:
        eval_path = _PROJECT_ROOT / "eval_tasks.json"

    if not eval_path.exists():
        print(f"ERROR: eval_tasks.json not found at {eval_path}", file=sys.stderr)
        print(
            "Create it first at the project root. "
            "See eval_tasks.json for the schema.",
            file=sys.stderr,
        )
        return 1

    data = json.loads(eval_path.read_text(encoding="utf-8"))
    tasks: list[dict] = data.get("tasks", data) if isinstance(data, dict) else data

    # Filter by category
    if args.category:
        tasks = [t for t in tasks if t.get("category") == args.category]
        if not tasks:
            print(f"ERROR: no tasks match category '{args.category}'", file=sys.stderr)
            return 1

    # Limit task count
    if args.max_tasks:
        tasks = tasks[: args.max_tasks]

    tier_display = args.tier or "auto"
    turns_display = str(args.max_turns) if args.max_turns else "auto"
    print(
        f"Running {len(tasks)} eval tasks "
        f"(tier={tier_display}, max_turns={turns_display})..."
    )

    # Load config and build tool registry (file-only — no Gmail/Calendar needed)
    config = SecretaryConfig.load()
    tools = build_tool_registry(
        data_root=config.data_path,
        workspace_root=None,
        unrestricted_files=True,
    )

    judge_model = "" if args.judge_model == "none" else args.judge_model

    results = asyncio.run(
        run_eval(tasks, config, tools, args.tier, args.max_turns, judge_model=judge_model)
    )

    metrics = _compute_metrics(results)

    _print_category_breakdown(results)

    # Final parseable summary line
    print(
        f"\nEVAL_SCORE: {metrics['eval_score']:.2f}  "
        f"MEAN_SCORE: {metrics['mean_score']:.2f}  "
        f"SUCCESS_RATE: {metrics['success_rate']:.2f}  "
        f"FIRST_TURN: {metrics['first_turn_rate']:.2f}  "
        f"AVG_TURNS: {metrics['avg_turns']:.1f}"
    )

    # Optionally write JSON results
    if args.output_json:
        out = {"metrics": metrics, "tasks": results}
        Path(args.output_json).write_text(
            json.dumps(out, indent=2), encoding="utf-8"
        )
        print(f"Results written to {args.output_json}")

    # Exit 0 if success_rate >= 0.5, else 1 (useful for CI)
    return 0 if metrics["success_rate"] >= 0.5 else 1


if __name__ == "__main__":
    sys.exit(main())
