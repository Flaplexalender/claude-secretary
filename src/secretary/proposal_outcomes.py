"""Empirical outcome tracking for self-improve proposals.

Closes the SOTA feedback loop (ACE / STOP / Reflexion pattern): after a
proposal's code change is promoted, measure whether the target metrics
actually improved over the next N tasks. Feed outcomes back into the
next analysis cycle so the LLM considers real impact when proposing
future changes — not just whether tests passed.

Data lives in ``data/proposal_outcomes.jsonl`` (append-only log):
    {
      "proposal_id": "a9562c6",          # commit short hash
      "commit_hash": "a9562c6...",
      "task": "Improve router caching",
      "description": "Add LRU cache to estimate_complexity",
      "promoted_at": 1776814072.12,
      "baseline": {
        "cost_per_success_usd": 0.087,
        "success_rate": 0.88,
        "avg_turns": 8.2,
        "avg_duration_s": 24.1,
        "task_count": 15
      },
      "outcome": null | {                 # filled once ≥N tasks land after
        "measured_at": 1776820000.0,
        "after_task_count": 18,
        "after": { ...same shape as baseline... },
        "delta_pct": {
          "cost_per_success": -12.4,     # negative = improvement
          "success_rate": +2.1,
          "avg_turns": -5.0
        },
        "verdict": "improvement" | "regression" | "neutral"
      }
    }

Designed to be cheap: pure file I/O, no LLM calls. The *injection* into
analysis prompts happens in goal_self_improve.py via
``format_recent_outcomes_for_prompt``.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

OUTCOMES_FILE = "proposal_outcomes.jsonl"
RUN_LOG_FILE = "run_log.jsonl"

# Minimum tasks after promotion before outcome can be measured.
MIN_TASKS_AFTER = 15
# Regression threshold: cost-per-success must rise > this pct to flag.
REGRESSION_PCT = 10.0
# Improvement threshold: cost-per-success must drop > this pct to flag.
IMPROVEMENT_PCT = 5.0


@dataclass
class MetricSnapshot:
    cost_per_success_usd: float
    success_rate: float
    avg_turns: float
    avg_duration_s: float
    task_count: int

    def to_dict(self) -> dict[str, float | int]:
        return {
            "cost_per_success_usd": round(self.cost_per_success_usd, 4),
            "success_rate": round(self.success_rate, 3),
            "avg_turns": round(self.avg_turns, 2),
            "avg_duration_s": round(self.avg_duration_s, 2),
            "task_count": self.task_count,
        }


def _snapshot_from_entries(entries: list[dict[str, Any]]) -> MetricSnapshot | None:
    """Compute metric snapshot from a slice of run_log entries."""
    if not entries:
        return None
    total_cost = sum(float(e.get("cost_usd") or 0.0) for e in entries)
    successes = sum(1 for e in entries if e.get("success"))
    total_turns = sum(int(e.get("num_turns") or 0) for e in entries)
    total_duration = sum(float(e.get("duration_s") or 0.0) for e in entries)
    n = len(entries)
    # Cost-per-success denominator: successes or 1 to avoid div-by-zero.
    cps = total_cost / successes if successes > 0 else total_cost
    return MetricSnapshot(
        cost_per_success_usd=cps,
        success_rate=successes / n if n else 0.0,
        avg_turns=total_turns / n if n else 0.0,
        avg_duration_s=total_duration / n if n else 0.0,
        task_count=n,
    )


def _load_run_log(data_root: Path, since_ts: float | None = None) -> list[dict[str, Any]]:
    """Load run_log entries, optionally filtered to those after a timestamp."""
    path = data_root / RUN_LOG_FILE
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since_ts is not None:
            ts = e.get("timestamp", "")
            try:
                # ISO8601 with +00:00 suffix
                from datetime import datetime
                dt = datetime.fromisoformat(ts)
                if dt.timestamp() <= since_ts:
                    continue
            except (ValueError, TypeError):
                continue
        entries.append(e)
    return entries


def record_baseline(
    data_root: Path,
    proposal_id: str,
    commit_hash: str,
    task: str,
    description: str,
    baseline_window: int = 15,
) -> bool:
    """Snapshot recent metrics and append a pending outcome record.

    Called from self_improve.py right after a successful promotion. The
    baseline is the last ``baseline_window`` run_log entries (before
    this promotion; the promoted change hasn't had time to affect runs
    yet so last-N is still the correct "before" estimate).
    """
    run_entries = _load_run_log(data_root)
    if not run_entries:
        log.debug("proposal_outcomes.record_baseline: run_log empty, skipping")
        return False
    baseline = _snapshot_from_entries(run_entries[-baseline_window:])
    if baseline is None:
        return False
    record = {
        "proposal_id": proposal_id,
        "commit_hash": commit_hash,
        "task": task[:200],
        "description": description[:300],
        "promoted_at": time.time(),
        "baseline": baseline.to_dict(),
        "outcome": None,
    }
    out_path = data_root / OUTCOMES_FILE
    try:
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.info(
            "proposal_outcomes: baseline recorded for %s (cps=$%.4f, success=%.0f%%)",
            proposal_id,
            baseline.cost_per_success_usd,
            baseline.success_rate * 100,
        )
        return True
    except OSError as e:
        log.warning("proposal_outcomes: failed to write baseline: %s", e)
        return False


def _read_outcomes(data_root: Path) -> list[dict[str, Any]]:
    path = data_root / OUTCOMES_FILE
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_outcomes(data_root: Path, records: list[dict[str, Any]]) -> None:
    path = data_root / OUTCOMES_FILE
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _verdict(delta_pct: dict[str, float]) -> str:
    cps = delta_pct.get("cost_per_success", 0.0)
    if cps <= -IMPROVEMENT_PCT:
        return "improvement"
    if cps >= REGRESSION_PCT:
        return "regression"
    return "neutral"


def measure_pending_outcomes(
    data_root: Path,
    min_tasks_after: int = MIN_TASKS_AFTER,
) -> int:
    """Fill in outcomes for any pending records that now have enough data.

    Call this once per watcher cycle. Returns count of outcomes measured.
    """
    records = _read_outcomes(data_root)
    if not records:
        return 0
    changed = 0
    for rec in records:
        if rec.get("outcome") is not None:
            continue
        promoted_at = float(rec.get("promoted_at", 0))
        if promoted_at <= 0:
            continue
        after_entries = _load_run_log(data_root, since_ts=promoted_at)
        if len(after_entries) < min_tasks_after:
            continue
        # Use first min_tasks_after entries after promotion for a fair window.
        after_slice = after_entries[:min_tasks_after]
        after = _snapshot_from_entries(after_slice)
        if after is None:
            continue
        baseline = rec.get("baseline", {})
        # Compute percentage deltas (negative = improvement for cost/turns).
        def _pct(before: float, now: float) -> float:
            if before == 0:
                return 0.0
            return round((now - before) / before * 100.0, 2)

        delta_pct = {
            "cost_per_success": _pct(
                float(baseline.get("cost_per_success_usd", 0)),
                after.cost_per_success_usd,
            ),
            "success_rate": _pct(
                float(baseline.get("success_rate", 0)),
                after.success_rate,
            ),
            "avg_turns": _pct(
                float(baseline.get("avg_turns", 0)),
                after.avg_turns,
            ),
        }
        rec["outcome"] = {
            "measured_at": time.time(),
            "after_task_count": len(after_slice),
            "after": after.to_dict(),
            "delta_pct": delta_pct,
            "verdict": _verdict(delta_pct),
        }
        log.info(
            "proposal_outcomes: %s verdict=%s (cps %+.1f%%, success %+.1f%%)",
            rec.get("proposal_id", "?"),
            rec["outcome"]["verdict"],
            delta_pct["cost_per_success"],
            delta_pct["success_rate"],
        )
        changed += 1
    if changed:
        _write_outcomes(data_root, records)
    return changed


def format_recent_outcomes_for_prompt(
    data_root: Path,
    max_n: int = 5,
) -> str:
    """Format recent measured outcomes for injection into analysis prompt.

    Returns a markdown block the self-improve analyzer can include so the
    LLM sees which past proposals actually moved the needle — and avoids
    re-proposing patterns that empirically regressed.

    Empty string if no measured outcomes exist yet.
    """
    records = _read_outcomes(data_root)
    measured = [r for r in records if r.get("outcome")]
    if not measured:
        return ""
    measured.sort(key=lambda r: r["outcome"]["measured_at"], reverse=True)
    lines: list[str] = [
        "## Measured Proposal Outcomes (empirical impact on metrics)",
        "Ranked most recent first. Use this to avoid proposing changes",
        "that have historically caused regressions; double-down on",
        "patterns that produced measurable improvements.",
        "",
    ]
    for r in measured[:max_n]:
        o = r["outcome"]
        verdict = o["verdict"].upper()
        dp = o["delta_pct"]
        lines.append(
            f"- **[{verdict}]** {r.get('description', '?')[:120]}"
        )
        lines.append(
            f"  - cost/success: {dp['cost_per_success']:+.1f}%, "
            f"success rate: {dp['success_rate']:+.1f}%, "
            f"turns: {dp['avg_turns']:+.1f}%"
        )
    lines.append("")
    return "\n".join(lines)
