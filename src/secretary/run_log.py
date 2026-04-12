"""Run log — JSONL persistence for task outcomes.

Every task execution (one-shot or watcher cycle) gets logged here.
The agent can review its own history for self-improvement.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class RunLogEntry:
    """A single task execution record."""
    timestamp: str
    cycle: int          # 0 for one-shot, >0 for watcher cycles
    task: str
    tier: str
    model: str
    success: bool
    output_preview: str  # first 500 chars of output
    error: str | None = None
    duration_s: float = 0.0
    premium_cost: float = 0.0  # premium request multiplier (e.g. 0.33, 1.0, 3.0)
    cost_usd: float = 0.0     # SDK-reported actual cost (if available)
    num_turns: int = 0         # SDK-reported turn count
    tools_used: list[str] = field(default_factory=list)  # MCP tools invoked
    source: str = "campaign"   # origin: "campaign", "ooda", or "goals"
    goal_id: str = ""          # goal that spawned this task (if source=="goals")


class RunLog:
    """Append-only JSONL log of all task executions."""

    _MAX_BYTES = 10 * 1024 * 1024  # rotate at 10 MB
    _MAX_ARCHIVES = 3               # keep run_log.jsonl.1 … run_log.jsonl.3

    def __init__(self, path: Path | str = "data/run_log.jsonl"):
        """Initialize RunLog with path to the JSONL file."""
        self.path = Path(path)

    def _rotate(self) -> None:
        """Rename run_log.jsonl → run_log.jsonl.N (shift existing archives up)."""
        for i in range(self._MAX_ARCHIVES, 0, -1):
            archive = self.path.with_name(self.path.name + f".{i}")
            prev = self.path.with_name(self.path.name + f".{i - 1}") if i > 1 else self.path
            if prev.exists():
                archive.unlink(missing_ok=True)
                prev.replace(archive)
        log.info("Rotated run log: %s → %s.1", self.path, self.path.name)

    def append(self, entry: RunLogEntry) -> None:
        """Append a run log entry to the JSONL file, rotating if size exceeds limit."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size >= self._MAX_BYTES:
            self._rotate()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def recent(self, n: int = 20) -> list[RunLogEntry]:
        """Read the last N entries efficiently.

        Uses seek-from-end for large files (O(chunk) instead of O(file_size)).
        Falls back to bounded deque for small files or encoding edge cases.
        """
        if not self.path.exists() or n <= 0:
            return []
        try:
            return self._recent_seek(n)
        except Exception:
            # Fallback: deque-based approach (always correct)
            return self._recent_deque(n)

    def _recent_seek(self, n: int) -> list[RunLogEntry]:
        """O(1) tail read: seek to end of file, read backwards in chunks."""
        file_size = self.path.stat().st_size
        if file_size == 0:
            return []

        # For small files (< 64KB), just read the whole thing
        if file_size < 65536:
            return self._recent_deque(n)

        # Estimate: each JSONL line is ~300-800 bytes. Start with generous chunk.
        # Read progressively larger chunks until we have enough lines.
        chunk_size = max(1024 * n, 65536)  # at least 1KB per line expected
        lines: list[str] = []

        with open(self.path, "rb") as f:
            pos = file_size
            remainder = b""
            while pos > 0 and len(lines) < n:
                read_size = min(chunk_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size) + remainder
                remainder = b""

                # Split into lines
                parts = chunk.split(b"\n")

                # If we didn't start at the beginning of the file, the first
                # part may be a partial line — save it for the next iteration
                if pos > 0:
                    remainder = parts[0]
                    parts = parts[1:]

                # Add non-empty lines (in reverse order since we read backwards)
                for part in reversed(parts):
                    stripped = part.strip()
                    if stripped:
                        lines.append(stripped.decode("utf-8"))
                        if len(lines) >= n:
                            break

                # Double chunk size for next iteration if needed
                chunk_size *= 2

            # If we reached the beginning and have a remainder, it's the first line
            if remainder:
                stripped = remainder.strip()
                if stripped and len(lines) < n:
                    lines.append(stripped.decode("utf-8"))

        # lines is in reverse order (newest first) — reverse to get oldest first
        lines = list(reversed(lines[-n:]))

        entries = []
        for line in lines:
            try:
                d = json.loads(line)
                entries.append(RunLogEntry(**d))
            except (json.JSONDecodeError, TypeError) as e:
                log.warning("Skipping corrupted log entry: %s", e)
        return entries

    def _recent_deque(self, n: int) -> list[RunLogEntry]:
        """Fallback: read last N lines using bounded deque (reads entire file)."""
        import collections
        entries = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                tail = collections.deque(f, maxlen=n)
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    entries.append(RunLogEntry(**d))
                except (json.JSONDecodeError, TypeError) as e:
                    log.warning("Skipping corrupted log entry: %s", e)
        except OSError as e:
            log.error("Failed to read run log: %s", e)
        return entries

    def summary(self) -> dict:
        """Quick stats: total runs, pass rate, by-tier breakdown, premium spend."""
        from .currency import usd_to_cad

        entries = self.recent(1000)
        if not entries:
            return {"total": 0}
        total = len(entries)
        passed = sum(1 for e in entries if e.success)
        total_premium = sum(e.premium_cost for e in entries)
        total_cost_usd = sum(e.cost_usd for e in entries)
        total_cost_cad = usd_to_cad(total_cost_usd)
        total_turns = sum(e.num_turns for e in entries)
        by_tier: dict[str, dict] = {}
        by_source: dict[str, dict] = {}
        for e in entries:
            if e.tier not in by_tier:
                by_tier[e.tier] = {"total": 0, "passed": 0}
            by_tier[e.tier]["total"] += 1
            if e.success:
                by_tier[e.tier]["passed"] += 1
            src = e.source or "campaign"
            if src not in by_source:
                by_source[src] = {"total": 0, "passed": 0}
            by_source[src]["total"] += 1
            if e.success:
                by_source[src]["passed"] += 1
        autonomous = sum(
            v["total"] for k, v in by_source.items() if k in ("goals", "ooda")
        )
        return {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": f"{passed / total:.0%}",
            "total_premium": round(total_premium, 2),
            "total_cost_usd": round(total_cost_usd, 4),
            "total_cost_cad": round(total_cost_cad, 4),
            "total_turns": total_turns,
            "by_tier": by_tier,
            "by_source": by_source,
            "autonomous_ratio": round(autonomous / total, 3) if total else 0.0,
        }

    @staticmethod
    def now() -> str:
        """Return the current UTC time as an ISO 8601 string."""
        return datetime.now(timezone.utc).isoformat()

    def audit(self) -> dict:
        """Analyze run history for cost optimization opportunities.

        Returns dict with keys:
            downgrades: tasks that ran at high tier but look like they could be lower
            top_tasks: top 3 most expensive task prompts by cumulative premium
            worst_cycle: the watcher cycle with the worst pass rate
        """
        entries = self.recent(1000)
        if not entries:
            return {"downgrades": [], "top_tasks": [], "worst_cycle": None}

        # (A) Potential downgrades: high/medium tier + short output + no tools + single-turn
        downgrades: list[dict] = []
        for e in entries:
            if e.tier in ("high", "medium") and e.success:
                output_len = len(e.output_preview)
                has_tools = bool(e.tools_used)
                is_short = output_len <= 150 and "\n" not in e.output_preview.strip()
                if is_short and not has_tools and e.num_turns <= 1:
                    downgrades.append({
                        "task": e.task[:80],
                        "tier": e.tier,
                        "premium": e.premium_cost,
                        "output_len": output_len,
                        "action": f"Consider routing to low (output was {output_len} chars, no tools)",
                    })

        # (B) Costliest task prompts by cumulative premium
        task_costs: dict[str, float] = {}
        task_counts: dict[str, int] = {}
        for e in entries:
            key = e.task[:80]
            task_costs[key] = task_costs.get(key, 0) + e.premium_cost
            task_counts[key] = task_counts.get(key, 0) + 1
        top_tasks = sorted(task_costs.items(), key=lambda x: x[1], reverse=True)[:3]
        top_tasks_out = [
            {"task": t, "total_premium": round(c, 2), "runs": task_counts[t],
             "action": "Review if tier can be lowered" if c > 2.0 else "OK"}
            for t, c in top_tasks
        ]

        # (C) Worst watcher cycle (cycle > 0)
        cycle_stats: dict[int, dict] = {}
        for e in entries:
            if e.cycle > 0:
                if e.cycle not in cycle_stats:
                    cycle_stats[e.cycle] = {"total": 0, "passed": 0, "premium": 0.0}
                cycle_stats[e.cycle]["total"] += 1
                if e.success:
                    cycle_stats[e.cycle]["passed"] += 1
                cycle_stats[e.cycle]["premium"] += e.premium_cost

        worst_cycle = None
        if cycle_stats:
            worst_id = min(
                cycle_stats,
                key=lambda c: cycle_stats[c]["passed"] / max(cycle_stats[c]["total"], 1),
            )
            s = cycle_stats[worst_id]
            rate = s["passed"] / max(s["total"], 1)
            worst_cycle = {
                "cycle": worst_id,
                "total": s["total"],
                "passed": s["passed"],
                "pass_rate": f"{rate:.0%}",
                "premium_spent": round(s["premium"], 2),
                "action": "Investigate failures" if rate < 0.5 else "Acceptable",
            }

        return {
            "downgrades": downgrades,
            "top_tasks": top_tasks_out,
            "worst_cycle": worst_cycle,
        }

    def analyze(self) -> dict:
        """Deep campaign analysis — patterns, reliability, and suggestions.

        Returns dict with keys:
            task_reliability: per-task pass rate + avg duration + retry count
            failure_patterns: repeated failure strings with counts
            hour_performance: pass rate by hour of day (UTC)
            cycle_trend: list of {cycle, passed, failed, premium} for trend analysis
            suggestions: auto-generated improvement recommendations
        """
        entries = self.recent(1000)
        if not entries:
            return {
                "task_reliability": [],
                "failure_patterns": [],
                "hour_performance": {},
                "cycle_trend": [],
                "suggestions": [],
            }

        # ── Per-task reliability scoring ──
        task_stats: dict[str, dict] = {}
        for e in entries:
            key = e.task[:80]
            if key not in task_stats:
                task_stats[key] = {
                    "task": key,
                    "total": 0,
                    "passed": 0,
                    "durations": [],
                    "tiers_used": set(),
                    "errors": [],
                    "total_cost_usd": 0.0,
                }
            s = task_stats[key]
            s["total"] += 1
            if e.success:
                s["passed"] += 1
            else:
                s["errors"].append(e.error or "unknown")
            if e.duration_s > 0:
                s["durations"].append(e.duration_s)
            s["tiers_used"].add(e.tier)
            s["total_cost_usd"] += e.cost_usd

        task_reliability = []
        for s in task_stats.values():
            rate = s["passed"] / max(s["total"], 1)
            avg_dur = sum(s["durations"]) / max(len(s["durations"]), 1)
            task_reliability.append({
                "task": s["task"],
                "total_runs": s["total"],
                "pass_rate": round(rate, 2),
                "avg_duration_s": round(avg_dur, 1),
                "tiers_used": sorted(s["tiers_used"]),
                "total_cost_usd": round(s["total_cost_usd"], 4),
                "retry_count": s["total"] - s["passed"],
            })
        task_reliability.sort(key=lambda t: t["pass_rate"])

        # ── Failure pattern detection ──
        error_counts: dict[str, int] = {}
        for e in entries:
            if not e.success and e.error:
                # Normalize: take first 80 chars as pattern key
                pattern = e.error[:80]
                error_counts[pattern] = error_counts.get(pattern, 0) + 1
        failure_patterns = [
            {"pattern": p, "count": c}
            for p, c in sorted(error_counts.items(), key=lambda x: x[1], reverse=True)
        ]

        # ── Hour-of-day performance (UTC) ──
        hour_stats: dict[int, dict] = {}
        for e in entries:
            try:
                ts = datetime.fromisoformat(e.timestamp)
                h = ts.hour
            except (ValueError, AttributeError):
                continue
            if h not in hour_stats:
                hour_stats[h] = {"total": 0, "passed": 0}
            hour_stats[h]["total"] += 1
            if e.success:
                hour_stats[h]["passed"] += 1
        hour_performance = {
            h: {
                "total": s["total"],
                "passed": s["passed"],
                "pass_rate": round(s["passed"] / max(s["total"], 1), 2),
            }
            for h, s in sorted(hour_stats.items())
        }

        # ── Cycle trend ──
        cycle_data: dict[int, dict] = {}
        for e in entries:
            if e.cycle > 0:
                if e.cycle not in cycle_data:
                    cycle_data[e.cycle] = {"passed": 0, "failed": 0, "premium": 0.0}
                if e.success:
                    cycle_data[e.cycle]["passed"] += 1
                else:
                    cycle_data[e.cycle]["failed"] += 1
                cycle_data[e.cycle]["premium"] += e.premium_cost
        cycle_trend = [
            {"cycle": c, **d} for c, d in sorted(cycle_data.items())
        ]

        # ── Auto-generated suggestions ──
        suggestions = []

        # Flag unreliable tasks (< 50% pass rate with 3+ runs)
        for t in task_reliability:
            if t["pass_rate"] < 0.5 and t["total_runs"] >= 3:
                suggestions.append(
                    f"Task '{t['task'][:50]}' has {t['pass_rate']:.0%} pass rate over "
                    f"{t['total_runs']} runs — consider rewriting the prompt or adding "
                    f"a higher tier override."
                )

        # Flag tasks that always need escalation
        for s in task_stats.values():
            if len(s["tiers_used"]) > 1 and s["total"] >= 3:
                suggestions.append(
                    f"Task '{s['task'][:50]}' used tiers {sorted(s['tiers_used'])} — "
                    f"consider setting tier explicitly to avoid retry escalation cost."
                )

        # Flag repeated errors
        for fp in failure_patterns[:3]:
            if fp["count"] >= 3:
                suggestions.append(
                    f"Error pattern '{fp['pattern'][:50]}' occurred {fp['count']} times — "
                    f"investigate root cause."
                )

        # Flag declining performance (last 3 cycles getting worse)
        if len(cycle_trend) >= 3:
            last3 = cycle_trend[-3:]
            rates = [
                d["passed"] / max(d["passed"] + d["failed"], 1)
                for d in last3
            ]
            if all(rates[i] > rates[i + 1] for i in range(len(rates) - 1)):
                suggestions.append(
                    "Pass rate declining over last 3 cycles — review recent failures."
                )

        return {
            "task_reliability": task_reliability,
            "failure_patterns": failure_patterns,
            "hour_performance": hour_performance,
            "cycle_trend": cycle_trend,
            "suggestions": suggestions,
        }

    def forecast(self, days: int = 30) -> dict:
        """Predict cost over the next N days based on recent history.

        Returns dict with keys:
            daily_rate_usd: average daily USD cost from observed data
            daily_rate_premium: average daily premium spend
            projected_usd: projected total cost over `days`
            projected_premium: projected total premium over `days`
            data_days: number of days of history used for the estimate
            confidence: 'high' if 7+ days of data, 'medium' if 3+, 'low' otherwise
        """
        from .currency import usd_to_cad

        entries = self.recent(1000)
        if not entries:
            return {
                "daily_rate_usd": 0.0, "daily_rate_premium": 0.0,
                "projected_usd": 0.0, "projected_premium": 0.0,
                "projected_cad": 0.0,
                "data_days": 0, "confidence": "none",
            }

        # Find date range of observed entries
        timestamps = []
        for e in entries:
            try:
                timestamps.append(datetime.fromisoformat(e.timestamp))
            except (ValueError, AttributeError):
                continue

        if len(timestamps) < 2:
            total_usd = sum(e.cost_usd for e in entries)
            total_premium = sum(e.premium_cost for e in entries)
            return {
                "daily_rate_usd": round(total_usd, 4),
                "daily_rate_premium": round(total_premium, 2),
                "projected_usd": round(total_usd * days, 4),
                "projected_premium": round(total_premium * days, 2),
                "projected_cad": round(usd_to_cad(total_usd * days), 4),
                "data_days": 1, "confidence": "low",
            }

        earliest = min(timestamps)
        latest = max(timestamps)
        span = (latest - earliest).total_seconds() / 86400  # days
        data_days = max(span, 1.0)

        total_usd = sum(e.cost_usd for e in entries)
        total_premium = sum(e.premium_cost for e in entries)

        daily_usd = total_usd / data_days
        daily_premium = total_premium / data_days

        confidence = "high" if data_days >= 7 else "medium" if data_days >= 3 else "low"

        return {
            "daily_rate_usd": round(daily_usd, 4),
            "daily_rate_premium": round(daily_premium, 2),
            "projected_usd": round(daily_usd * days, 4),
            "projected_premium": round(daily_premium * days, 2),
            "projected_cad": round(usd_to_cad(daily_usd * days), 4),
            "data_days": round(data_days, 1),
            "confidence": confidence,
        }
