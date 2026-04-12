"""Tests for metrics.py — multi-instance metrics collection and benchmarking."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from secretary.metrics import (
    BenchmarkResult,
    InstanceMetrics,
    MetricsCollector,
    TaskMetric,
    _aggregate_tasks,
    top_k_tasks,
)


@pytest.fixture
def metrics_dir(tmp_path):
    return tmp_path / "metrics"


@pytest.fixture
def mc(metrics_dir):
    return MetricsCollector(metrics_dir)


def _make_metric(
    instance_id: str = "worker-a",
    success: bool = True,
    num_turns: int = 5,
    input_tokens: int = 10000,
    output_tokens: int = 3000,
    duration_s: float = 30.0,
    cost_usd: float = 0.01,
    tool_calls_total: int = 8,
    reasoning_effort: str = "high",
    **kwargs,
) -> TaskMetric:
    return TaskMetric(
        instance_id=instance_id,
        success=success,
        num_turns=num_turns,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_s=duration_s,
        cost_usd=cost_usd,
        tool_calls_total=tool_calls_total,
        reasoning_effort=reasoning_effort,
        **kwargs,
    )


class TestMetricsCollector:
    def test_record_and_load(self, mc):
        m = _make_metric()
        mc.record(m)
        loaded = mc.load_all()
        assert len(loaded) == 1
        assert loaded[0].instance_id == "worker-a"
        assert loaded[0].success is True

    def test_derived_fields_computed(self, mc):
        m = _make_metric(num_turns=5, input_tokens=10000, output_tokens=5000, tool_calls_total=10, duration_s=50.0)
        mc.record(m)
        loaded = mc.load_all()
        assert loaded[0].tokens_per_turn == 3000.0  # 15000/5
        assert loaded[0].tools_per_turn == 2.0  # 10/5
        assert loaded[0].seconds_per_turn == 10.0  # 50/5
        assert loaded[0].total_tokens == 15000

    def test_timestamp_auto_populated(self, mc):
        m = _make_metric()
        assert m.timestamp == ""
        mc.record(m)
        loaded = mc.load_all()
        assert loaded[0].timestamp != ""

    def test_load_empty(self, mc):
        assert mc.load_all() == []

    def test_record_multiple(self, mc):
        for i in range(5):
            mc.record(_make_metric(instance_id=f"worker-{i}"))
        assert len(mc.load_all()) == 5

    def test_load_with_since_filter(self, mc):
        m1 = _make_metric(instance_id="old")
        m1.timestamp = "2020-01-01T00:00:00"
        mc.record(m1)
        m2 = _make_metric(instance_id="new")
        m2.timestamp = "2030-01-01T00:00:00"
        mc.record(m2)
        loaded = mc.load_all(since="2025-01-01T00:00:00")
        assert len(loaded) == 1
        assert loaded[0].instance_id == "new"


class TestAggregation:
    def test_aggregate_empty(self):
        agg = _aggregate_tasks("test", [])
        assert agg.tasks_total == 0
        assert agg.success_rate == 0.0

    def test_aggregate_single_success(self):
        metrics = [_make_metric(success=True, num_turns=3, cost_usd=0.05, duration_s=20.0)]
        agg = _aggregate_tasks("inst", metrics)
        assert agg.tasks_total == 1
        assert agg.tasks_passed == 1
        assert agg.success_rate == 1.0
        assert agg.avg_turns_per_task == 3.0
        assert agg.cost_per_successful_task == 0.05
        assert agg.throughput_tasks_per_hour > 0

    def test_aggregate_mixed(self):
        metrics = [
            _make_metric(success=True, num_turns=3, cost_usd=0.05, duration_s=20.0),
            _make_metric(success=False, num_turns=5, cost_usd=0.08, duration_s=40.0),
        ]
        agg = _aggregate_tasks("inst", metrics)
        assert agg.tasks_total == 2
        assert agg.tasks_passed == 1
        assert agg.tasks_failed == 1
        assert agg.success_rate == 0.5
        assert agg.total_cost_usd == pytest.approx(0.13)

    def test_aggregate_by_instance(self, mc):
        mc.record(_make_metric(instance_id="a", success=True))
        mc.record(_make_metric(instance_id="a", success=True))
        mc.record(_make_metric(instance_id="b", success=False))
        by_inst = mc.aggregate_by_instance()
        assert "a" in by_inst
        assert "b" in by_inst
        assert by_inst["a"].tasks_passed == 2
        assert by_inst["b"].tasks_failed == 1

    def test_aggregate_by_config(self, mc):
        mc.record(_make_metric(reasoning_effort="high", success=True))
        mc.record(_make_metric(reasoning_effort="", success=True))
        mc.record(_make_metric(reasoning_effort="high", success=False))
        by_re = mc.aggregate_by_config("reasoning_effort")
        assert "high" in by_re
        assert "" in by_re
        assert by_re["high"].tasks_total == 2
        assert by_re[""].tasks_total == 1


class TestComparison:
    def test_compare_clear_winner(self, mc):
        group_a = [_make_metric(success=True, num_turns=10, cost_usd=0.1, duration_s=100.0)]
        group_b = [
            _make_metric(success=True, num_turns=3, cost_usd=0.02, duration_s=20.0),
            _make_metric(success=True, num_turns=4, cost_usd=0.03, duration_s=25.0),
        ]
        result = mc.compare(group_a, group_b, name="efficiency test")
        # B should win — lower cost, more throughput, fewer turns
        assert result.winner == "b"
        assert result.improvement_pct > 0
        assert "efficiency test" in result.name

    def test_compare_persisted(self, mc):
        group_a = [_make_metric(success=True)]
        group_b = [_make_metric(success=True)]
        mc.compare(group_a, group_b, name="test")
        benchmarks = mc.get_benchmarks()
        assert len(benchmarks) == 1
        assert benchmarks[0].name == "test"

    def test_compare_tie(self, mc):
        m = _make_metric(success=True)
        result = mc.compare([m], [m], name="same")
        assert result.winner == "tie"


class TestFormatting:
    def test_format_instance_report_empty(self, mc):
        report = mc.format_instance_report()
        assert "No metrics" in report

    def test_format_instance_report_with_data(self, mc):
        mc.record(_make_metric(instance_id="worker-a", success=True))
        mc.record(_make_metric(instance_id="worker-a", success=False))
        report = mc.format_instance_report()
        assert "worker-a" in report
        assert "1/2 passed" in report

    def test_get_benchmarks_empty(self, mc):
        assert mc.get_benchmarks() == []


class TestTopKTasks:
    """Tests for the top_k_tasks function."""

    def test_top_k_returns_sorted_by_tools_per_turn(self, tmp_path):
        """top_k_tasks should return entries sorted descending by tools_per_turn."""
        log_file = tmp_path / "run_log.jsonl"
        entries = [
            # 2 tools / 2 turns = 1.0 tools_per_turn
            {"timestamp": "2026-01-01T00:00:00", "cycle": 0, "task": "low-density task",
             "tier": "low", "model": "haiku", "success": True, "output_preview": "",
             "num_turns": 2, "tools_used": ["file_read", "file_write"], "cost_usd": 0.01},
            # 9 tools / 3 turns = 3.0 tools_per_turn
            {"timestamp": "2026-01-01T01:00:00", "cycle": 0, "task": "high-density task",
             "tier": "high", "model": "opus", "success": True, "output_preview": "",
             "num_turns": 3, "tools_used": ["a", "b", "c", "d", "e", "f", "g", "h", "i"],
             "cost_usd": 0.05},
            # 4 tools / 2 turns = 2.0 tools_per_turn
            {"timestamp": "2026-01-01T02:00:00", "cycle": 0, "task": "medium-density task",
             "tier": "medium", "model": "sonnet", "success": False, "output_preview": "",
             "num_turns": 2, "tools_used": ["a", "b", "c", "d"], "cost_usd": 0.03},
            # 0 turns — should be excluded
            {"timestamp": "2026-01-01T03:00:00", "cycle": 0, "task": "zero-turn task",
             "tier": "low", "model": "haiku", "success": False, "output_preview": "",
             "num_turns": 0, "tools_used": [], "cost_usd": 0.0},
        ]
        import json
        log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        result = top_k_tasks(k=3, log_path=log_file)
        assert len(result) == 3
        # Sorted descending: 3.0, 2.0, 1.0
        assert result[0]["tools_per_turn"] == 3.0
        assert result[0]["task"] == "high-density task"
        assert result[1]["tools_per_turn"] == 2.0
        assert result[2]["tools_per_turn"] == 1.0
        # Zero-turn entry excluded
        assert all(r["task"] != "zero-turn task" for r in result)

    def test_top_k_respects_k_and_missing_file(self, tmp_path):
        """top_k_tasks should return at most k results and [] for missing files."""
        # Missing file returns empty
        missing = tmp_path / "nonexistent.jsonl"
        assert top_k_tasks(k=5, log_path=missing) == []

        # k < total entries
        log_file = tmp_path / "run_log.jsonl"
        import json
        entries = [
            {"timestamp": f"2026-01-01T0{i}:00:00", "cycle": 0, "task": f"task-{i}",
             "tier": "medium", "model": "sonnet", "success": True, "output_preview": "",
             "num_turns": 2, "tools_used": ["t"] * (i + 1), "cost_usd": 0.01}
            for i in range(5)
        ]
        log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = top_k_tasks(k=2, log_path=log_file)
        assert len(result) == 2
        # Top 2 should be task-4 (2.5 tpt) and task-3 (2.0 tpt)
        assert result[0]["tools_per_turn"] == 2.5
        assert result[1]["tools_per_turn"] == 2.0
