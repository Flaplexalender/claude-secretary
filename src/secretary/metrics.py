"""Metrics collection and benchmarking for multi-instance Secretary.

Tracks per-instance and per-task efficiency metrics, enables A/B comparisons
between different configurations (single vs multi-instance, different tiers,
different reasoning_effort levels, system prompt variants).

Metrics are append-only JSONL for durability, with in-memory aggregation
for reporting.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("secretary.metrics")


@dataclass
class TaskMetric:
    """Metrics for a single task execution."""
    timestamp: str = ""
    instance_id: str = ""
    task_hash: str = ""
    prompt_preview: str = ""
    tier: str = ""
    model: str = ""
    success: bool = False
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    duration_s: float = 0.0
    cost_usd: float = 0.0
    reasoning_effort: str = ""
    # Quality signals
    tools_used: list[str] = field(default_factory=list)
    tool_calls_total: int = 0
    consecutive_errors: int = 0
    quality_score: float = 0.0   # heuristic quality 0.0-1.0
    # Derived efficiency
    tokens_per_turn: float = 0.0
    tools_per_turn: float = 0.0
    seconds_per_turn: float = 0.0


@dataclass
class InstanceMetrics:
    """Aggregated metrics for one instance over a period."""
    instance_id: str
    role: str = ""
    tasks_total: int = 0
    tasks_passed: int = 0
    tasks_failed: int = 0
    total_turns: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_s: float = 0.0
    total_cost_usd: float = 0.0
    total_tool_calls: int = 0
    # Derived
    success_rate: float = 0.0
    avg_turns_per_task: float = 0.0
    avg_tokens_per_task: float = 0.0
    avg_duration_per_task: float = 0.0
    avg_tools_per_turn: float = 0.0
    throughput_tasks_per_hour: float = 0.0
    cost_per_successful_task: float = 0.0
    avg_quality_score: float = 0.0


@dataclass
class BenchmarkResult:
    """Result of an A/B benchmark comparison."""
    name: str
    config_a: dict[str, Any] = field(default_factory=dict)
    config_b: dict[str, Any] = field(default_factory=dict)
    metrics_a: dict[str, Any] = field(default_factory=dict)
    metrics_b: dict[str, Any] = field(default_factory=dict)
    # Comparison
    winner: str = ""               # "a", "b", or "tie"
    improvement_pct: float = 0.0   # positive = B is better
    summary: str = ""
    timestamp: str = ""


class MetricsCollector:
    """Append-only metrics logger + in-memory aggregation.

    Each metric is a TaskMetric written to a JSONL file.
    Aggregation happens on-demand from the JSONL data.
    """

    def __init__(self, metrics_dir: Path):
        """Initialize metrics collector, creating the metrics directory if needed."""
        self.metrics_dir = metrics_dir
        self._log_path = metrics_dir / "task_metrics.jsonl"
        self._benchmarks_path = metrics_dir / "benchmarks.jsonl"
        metrics_dir.mkdir(parents=True, exist_ok=True)

    def record(self, metric: TaskMetric) -> None:
        """Append a task metric to the JSONL log."""
        if not metric.timestamp:
            metric.timestamp = datetime.now(timezone.utc).isoformat()
        # Compute derived fields
        if metric.num_turns > 0:
            metric.tokens_per_turn = (metric.input_tokens + metric.output_tokens) / metric.num_turns
            metric.tools_per_turn = metric.tool_calls_total / metric.num_turns
            metric.seconds_per_turn = metric.duration_s / metric.num_turns
        metric.total_tokens = metric.input_tokens + metric.output_tokens
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(metric)) + "\n")

    def load_all(self, since: str | None = None) -> list[TaskMetric]:
        """Load all metrics, optionally filtered by timestamp.

        Reads line-by-line to avoid loading the entire JSONL file into memory
        at once — important for long-running daemons with large log files.
        """
        if not self._log_path.exists():
            return []
        metrics = []
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    if since and data.get("timestamp", "") < since:
                        continue
                    metrics.append(TaskMetric(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
        return metrics

    def aggregate_by_instance(self, since: str | None = None) -> dict[str, InstanceMetrics]:
        """Aggregate metrics per instance."""
        all_metrics = self.load_all(since)
        by_instance: dict[str, list[TaskMetric]] = {}
        for m in all_metrics:
            by_instance.setdefault(m.instance_id, []).append(m)

        result = {}
        for inst_id, task_metrics in by_instance.items():
            agg = _aggregate_tasks(inst_id, task_metrics)
            result[inst_id] = agg
        return result

    def aggregate_by_config(
        self, group_key: str = "reasoning_effort", since: str | None = None
    ) -> dict[str, InstanceMetrics]:
        """Aggregate metrics by a config dimension (for A/B comparison)."""
        all_metrics = self.load_all(since)
        groups: dict[str, list[TaskMetric]] = {}
        for m in all_metrics:
            key = getattr(m, group_key, "unknown")
            groups.setdefault(str(key), []).append(m)

        result = {}
        for group, task_metrics in groups.items():
            agg = _aggregate_tasks(group, task_metrics)
            result[group] = agg
        return result

    def compare(
        self,
        group_a: list[TaskMetric],
        group_b: list[TaskMetric],
        name: str = "comparison",
        config_a: dict[str, Any] | None = None,
        config_b: dict[str, Any] | None = None,
    ) -> BenchmarkResult:
        """Compare two groups of task metrics and determine a winner."""
        agg_a = _aggregate_tasks("a", group_a)
        agg_b = _aggregate_tasks("b", group_b)

        # Scoring: higher success rate, lower cost per success, lower avg turns
        score_a = 0.0
        score_b = 0.0

        # Success rate (weight 3)
        if agg_a.success_rate > agg_b.success_rate:
            score_a += 3
        elif agg_b.success_rate > agg_a.success_rate:
            score_b += 3

        # Cost per successful task (weight 2, lower is better)
        if agg_a.cost_per_successful_task > 0 and agg_b.cost_per_successful_task > 0:
            if agg_a.cost_per_successful_task < agg_b.cost_per_successful_task:
                score_a += 2
            elif agg_b.cost_per_successful_task < agg_a.cost_per_successful_task:
                score_b += 2

        # Throughput (weight 2)
        if agg_a.throughput_tasks_per_hour > agg_b.throughput_tasks_per_hour:
            score_a += 2
        elif agg_b.throughput_tasks_per_hour > agg_a.throughput_tasks_per_hour:
            score_b += 2

        # Avg turns per task (weight 1, lower is better)
        if agg_a.avg_turns_per_task > 0 and agg_b.avg_turns_per_task > 0:
            if agg_a.avg_turns_per_task < agg_b.avg_turns_per_task:
                score_a += 1
            elif agg_b.avg_turns_per_task < agg_a.avg_turns_per_task:
                score_b += 1

        # Quality score (weight 2, higher is better)
        if agg_a.avg_quality_score > agg_b.avg_quality_score + 0.05:
            score_a += 2
        elif agg_b.avg_quality_score > agg_a.avg_quality_score + 0.05:
            score_b += 2

        if score_a > score_b:
            winner = "a"
        elif score_b > score_a:
            winner = "b"
        else:
            winner = "tie"

        # Calculate improvement percentage on throughput
        improvement = 0.0
        if agg_a.throughput_tasks_per_hour > 0:
            improvement = (
                (agg_b.throughput_tasks_per_hour - agg_a.throughput_tasks_per_hour)
                / agg_a.throughput_tasks_per_hour * 100
            )

        result = BenchmarkResult(
            name=name,
            config_a=config_a or {},
            config_b=config_b or {},
            metrics_a=asdict(agg_a),
            metrics_b=asdict(agg_b),
            winner=winner,
            improvement_pct=round(improvement, 1),
            summary=_build_summary(agg_a, agg_b, winner, improvement),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # Persist
        with open(self._benchmarks_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(result)) + "\n")

        return result

    def get_benchmarks(self) -> list[BenchmarkResult]:
        """Load all benchmark results.

        Reads line-by-line to avoid loading the entire file into memory.
        """
        if not self._benchmarks_path.exists():
            return []
        results = []
        with open(self._benchmarks_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    results.append(BenchmarkResult(**data))
                except (json.JSONDecodeError, TypeError):
                    continue
        return results

    def format_instance_report(self, since: str | None = None) -> str:
        """Format a human-readable report of per-instance metrics."""
        by_inst = self.aggregate_by_instance(since)
        if not by_inst:
            return "No metrics recorded yet."

        lines = ["## Instance Metrics", ""]
        for inst_id, m in sorted(by_inst.items()):
            lines.append(f"### {inst_id} ({m.role or 'generalist'})")
            lines.append(f"  Tasks: {m.tasks_passed}/{m.tasks_total} passed ({m.success_rate:.0%})")
            lines.append(f"  Turns: {m.total_turns} total, {m.avg_turns_per_task:.1f}/task")
            lines.append(f"  Tokens: {m.total_input_tokens + m.total_output_tokens:,} total, {m.avg_tokens_per_task:,.0f}/task")
            lines.append(f"  Cost: ${m.total_cost_usd:.4f} total, ${m.cost_per_successful_task:.4f}/success")
            lines.append(f"  Throughput: {m.throughput_tasks_per_hour:.1f} tasks/hour")
            lines.append(f"  Duration: {m.total_duration_s:.0f}s total, {m.avg_duration_per_task:.1f}s/task")
            lines.append(f"  Tool calls: {m.total_tool_calls} total, {m.avg_tools_per_turn:.1f}/turn")
            lines.append(f"  Quality: {m.avg_quality_score:.3f} avg score")
            lines.append("")
        return "\n".join(lines)


def _aggregate_tasks(instance_id: str, metrics: list[TaskMetric]) -> InstanceMetrics:
    """Aggregate a list of task metrics into instance-level summary."""
    if not metrics:
        return InstanceMetrics(instance_id=instance_id)

    passed = [m for m in metrics if m.success]
    failed = [m for m in metrics if not m.success]

    total_turns = sum(m.num_turns for m in metrics)
    total_in = sum(m.input_tokens for m in metrics)
    total_out = sum(m.output_tokens for m in metrics)
    total_dur = sum(m.duration_s for m in metrics)
    total_cost = sum(m.cost_usd for m in metrics)
    total_tools = sum(m.tool_calls_total for m in metrics)

    n = len(metrics)
    agg = InstanceMetrics(
        instance_id=instance_id,
        tasks_total=n,
        tasks_passed=len(passed),
        tasks_failed=len(failed),
        total_turns=total_turns,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        total_duration_s=total_dur,
        total_cost_usd=total_cost,
        total_tool_calls=total_tools,
        success_rate=len(passed) / n if n > 0 else 0.0,
        avg_turns_per_task=total_turns / n if n > 0 else 0.0,
        avg_tokens_per_task=(total_in + total_out) / n if n > 0 else 0.0,
        avg_duration_per_task=total_dur / n if n > 0 else 0.0,
        avg_tools_per_turn=total_tools / total_turns if total_turns > 0 else 0.0,
        cost_per_successful_task=total_cost / len(passed) if passed else 0.0,
        avg_quality_score=sum(m.quality_score for m in metrics) / n if n > 0 else 0.0,
    )

    # Throughput: tasks per hour based on total wall time
    if total_dur > 0:
        agg.throughput_tasks_per_hour = n / (total_dur / 3600)

    return agg


def top_k_tasks(k: int = 5, log_path: Path | str = "data/run_log.jsonl") -> list[dict]:
    """Return the top-K tasks ranked by tools_per_turn from run_log.jsonl.

    Each returned dict contains:
        task (str):           first 120 chars of the task prompt
        tools_per_turn (float): len(tools_used) / num_turns
        num_turns (int):      total agent turns
        tool_count (int):     total tool invocations
        success (bool):       whether the task succeeded
        cost_usd (float):     SDK-reported cost
        model (str):          model used
        timestamp (str):      ISO timestamp
    """
    path = Path(log_path)
    if not path.exists():
        return []

    scored: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            num_turns = entry.get("num_turns", 0)
            tools_used = entry.get("tools_used", [])
            if num_turns <= 0:
                continue
            tpt = len(tools_used) / num_turns
            scored.append({
                "task": (entry.get("task", "") or "")[:120],
                "tools_per_turn": round(tpt, 3),
                "num_turns": num_turns,
                "tool_count": len(tools_used),
                "success": entry.get("success", False),
                "cost_usd": entry.get("cost_usd", 0.0),
                "model": entry.get("model", ""),
                "timestamp": entry.get("timestamp", ""),
            })

    scored.sort(key=lambda d: d["tools_per_turn"], reverse=True)
    return scored[:k]


def _build_summary(a: InstanceMetrics, b: InstanceMetrics, winner: str, improvement: float) -> str:
    """Build a human-readable comparison summary."""
    parts = []
    if winner == "tie":
        parts.append("Result: TIE — no clear winner.")
    else:
        label = "Config A" if winner == "a" else "Config B"
        parts.append(f"Result: {label} wins.")

    parts.append(f"Success rate: A={a.success_rate:.0%} vs B={b.success_rate:.0%}")
    parts.append(f"Avg turns/task: A={a.avg_turns_per_task:.1f} vs B={b.avg_turns_per_task:.1f}")
    parts.append(f"Throughput: A={a.throughput_tasks_per_hour:.1f}/hr vs B={b.throughput_tasks_per_hour:.1f}/hr")

    if a.cost_per_successful_task > 0 and b.cost_per_successful_task > 0:
        parts.append(f"Cost/success: A=${a.cost_per_successful_task:.4f} vs B=${b.cost_per_successful_task:.4f}")

    if abs(improvement) > 1:
        parts.append(f"Throughput change: {improvement:+.1f}%")

    return " | ".join(parts)
