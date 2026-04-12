"""History command — display and analyze past run records.

Reads from run_log.jsonl and presents formatted summaries, stats,
and filterable run history for the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .run_log import RunLog, RunLogEntry


@dataclass
class HistoryStats:
    """Aggregated statistics from run history."""
    total_runs: int
    successful: int
    failed: int
    success_rate: str  # e.g. "85%"
    avg_duration_s: float
    total_premium: float
    total_cost_usd: float
    total_cost_cad: float
    by_tier: dict[str, TierStats]


@dataclass
class TierStats:
    """Per-tier breakdown."""
    count: int
    successful: int
    failed: int
    success_rate: str
    avg_duration_s: float
    total_premium: float = 0.0


@dataclass
class HistoryResult:
    """Full history query result."""
    entries: list[RunLogEntry]
    stats: HistoryStats
    filters_applied: dict[str, Any]


def _compute_tier_stats(entries: list[RunLogEntry]) -> dict[str, TierStats]:
    """Group entries by tier and compute stats."""
    buckets: dict[str, list[RunLogEntry]] = {}
    for e in entries:
        buckets.setdefault(e.tier, []).append(e)

    result = {}
    for tier, group in sorted(buckets.items()):
        count = len(group)
        ok = sum(1 for e in group if e.success)
        durations = [e.duration_s for e in group if e.duration_s > 0]
        avg_dur = sum(durations) / len(durations) if durations else 0.0
        tier_premium = sum(e.premium_cost for e in group)
        result[tier] = TierStats(
            count=count,
            successful=ok,
            failed=count - ok,
            success_rate=f"{ok / count:.0%}" if count else "0%",
            avg_duration_s=round(avg_dur, 1),
            total_premium=round(tier_premium, 2),
        )
    return result


def compute_stats(entries: list[RunLogEntry]) -> HistoryStats:
    """Compute aggregate statistics from a list of entries."""
    from .currency import usd_to_cad

    total = len(entries)
    if total == 0:
        return HistoryStats(
            total_runs=0, successful=0, failed=0,
            success_rate="0%", avg_duration_s=0.0, total_premium=0.0,
            total_cost_usd=0.0, total_cost_cad=0.0, by_tier={},
        )

    ok = sum(1 for e in entries if e.success)
    durations = [e.duration_s for e in entries if e.duration_s > 0]
    avg_dur = sum(durations) / len(durations) if durations else 0.0
    total_premium = sum(e.premium_cost for e in entries)
    total_cost_usd = sum(e.cost_usd for e in entries)
    total_cost_cad = usd_to_cad(total_cost_usd)

    return HistoryStats(
        total_runs=total,
        successful=ok,
        failed=total - ok,
        success_rate=f"{ok / total:.0%}",
        avg_duration_s=round(avg_dur, 1),
        total_premium=round(total_premium, 2),
        total_cost_usd=round(total_cost_usd, 4),
        total_cost_cad=round(total_cost_cad, 4),
        by_tier=_compute_tier_stats(entries),
    )


def query_history(
    log: RunLog,
    *,
    tier: str | None = None,
    last: int = 10,
    failed_only: bool = False,
    search: str | None = None,
) -> HistoryResult:
    """Query run history with optional filters.

    Args:
        log: RunLog instance to read from.
        tier: Filter to only this tier (e.g. "low", "medium", "high").
        last: Maximum number of recent entries to return.
        failed_only: If True, show only failed runs.
        search: Case-insensitive substring match against task description.
    """
    # Read all entries for stats, then apply filters for display
    all_entries = log.recent(10000)

    # Filter by tier if requested
    filtered = all_entries
    if tier:
        filtered = [e for e in filtered if e.tier == tier]

    # Filter by status
    if failed_only:
        filtered = [e for e in filtered if not e.success]

    # Filter by search term
    if search:
        term = search.lower()
        filtered = [e for e in filtered if term in e.task.lower()]

    # Compute stats on the filtered set
    stats = compute_stats(filtered)

    # Limit to last N for display
    display_entries = filtered[-last:] if last > 0 else filtered

    filters_applied: dict[str, Any] = {}
    if tier:
        filters_applied["tier"] = tier
    if last != 10:
        filters_applied["last"] = last
    if failed_only:
        filters_applied["failed_only"] = True
    if search:
        filters_applied["search"] = search

    return HistoryResult(
        entries=display_entries,
        stats=stats,
        filters_applied=filters_applied,
    )


def format_history(result: HistoryResult) -> str:
    """Format history result as human-readable text."""
    lines: list[str] = []
    stats = result.stats

    # Header
    lines.append("═══ Run History ═══")
    lines.append("")

    if stats.total_runs == 0:
        lines.append("No runs recorded yet.")
        return "\n".join(lines)

    # Summary
    premium_str = f"  |  Premium: {stats.total_premium}x" if stats.total_premium > 0 else ""
    cost_str = ""
    if stats.total_cost_usd > 0:
        cost_str = f"  |  Cost: ${stats.total_cost_cad:.4f} CAD (${stats.total_cost_usd:.4f} USD)"
    lines.append(f"Total: {stats.total_runs} runs  |  "
                 f"✓ {stats.successful}  ✗ {stats.failed}  |  "
                 f"Success rate: {stats.success_rate}  |  "
                 f"Avg duration: {stats.avg_duration_s}s{premium_str}{cost_str}")
    lines.append("")

    # Per-tier breakdown
    if stats.by_tier:
        lines.append("── By Tier ──")
        for tier_name, ts in stats.by_tier.items():
            lines.append(
                f"  {tier_name:8s}  {ts.count:3d} runs  "
                f"✓ {ts.successful:2d}  ✗ {ts.failed:2d}  "
                f"({ts.success_rate})  avg {ts.avg_duration_s}s"
            )
        lines.append("")

    # Filters
    if result.filters_applied:
        parts = [f"{k}={v}" for k, v in result.filters_applied.items()]
        lines.append(f"Filters: {', '.join(parts)}")
        lines.append("")

    # Recent runs table
    entries = result.entries
    if entries:
        lines.append("── Recent Runs ──")
        lines.append(f"  {'Timestamp':<22s}  {'Tier':<8s}  {'Status':<6s}  "
                     f"{'Duration':>8s}  {'Cost':>5s}  Task")
        lines.append(f"  {'─' * 22}  {'─' * 8}  {'─' * 6}  {'─' * 8}  {'─' * 5}  {'─' * 30}")
        for e in entries:
            # Parse and format timestamp for readability
            ts = _format_timestamp(e.timestamp)
            status = "✓ OK" if e.success else "✗ FAIL"
            dur = f"{e.duration_s:>7.1f}s" if e.duration_s > 0 else "     n/a"
            cost = f"{e.premium_cost:.2f}x" if e.premium_cost > 0 else "    -"
            task = e.task[:50] + "…" if len(e.task) > 50 else e.task
            lines.append(f"  {ts:<22s}  {e.tier:<8s}  {status:<6s}  {dur}  {cost:>5s}  {task}")

    return "\n".join(lines)


def format_history_json(result: HistoryResult) -> str:
    """Format history result as JSON."""
    import json
    from dataclasses import asdict

    data = {
        "stats": {
            "total_runs": result.stats.total_runs,
            "successful": result.stats.successful,
            "failed": result.stats.failed,
            "success_rate": result.stats.success_rate,
            "avg_duration_s": result.stats.avg_duration_s,
            "by_tier": {
                k: asdict(v) for k, v in result.stats.by_tier.items()
            },
        },
        "filters": result.filters_applied,
        "entries": [
            {
                "timestamp": e.timestamp,
                "task": e.task,
                "tier": e.tier,
                "model": e.model,
                "success": e.success,
                "duration_s": e.duration_s,
                "error": e.error,
            }
            for e in result.entries
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)


def _format_timestamp(ts: str) -> str:
    """Parse ISO timestamp and return a short readable format."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError, TypeError):
        return str(ts)[:22] if ts else ""
