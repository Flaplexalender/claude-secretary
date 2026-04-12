"""Tests for run_log — all offline, no API calls."""
import json
from pathlib import Path
from secretary.run_log import RunLog, RunLogEntry


def test_append_and_recent(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    entry = RunLogEntry(
        timestamp="2026-03-11T00:00:00Z",
        cycle=1,
        task="test task",
        tier="low",
        model="claude-haiku-4.5",
        success=True,
        output_preview="output here",
        duration_s=1.5,
    )
    log.append(entry)
    entries = log.recent()
    assert len(entries) == 1
    assert entries[0].task == "test task"
    assert entries[0].success is True


def test_multiple_entries(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    for i in range(5):
        log.append(RunLogEntry(
            timestamp=f"2026-03-11T00:0{i}:00Z",
            cycle=i,
            task=f"task {i}",
            tier="low",
            model="claude-haiku-4.5",
            success=i % 2 == 0,
            output_preview="",
            duration_s=float(i),
        ))
    entries = log.recent(3)
    assert len(entries) == 3
    assert entries[0].task == "task 2"


def test_summary(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    for tier, ok in [("low", True), ("low", True), ("medium", False), ("low", True)]:
        log.append(RunLogEntry(
            timestamp="2026-03-11T00:00:00Z",
            cycle=1,
            task="t",
            tier=tier,
            model="m",
            success=ok,
            output_preview="",
        ))
    stats = log.summary()
    assert stats["total"] == 4
    assert stats["passed"] == 3
    assert stats["failed"] == 1
    assert stats["by_tier"]["low"]["passed"] == 3
    assert stats["by_tier"]["medium"]["passed"] == 0


def test_empty_summary(tmp_path: Path):
    log = RunLog(tmp_path / "nonexistent.jsonl")
    stats = log.summary()
    assert stats["total"] == 0


def test_empty_recent(tmp_path: Path):
    log = RunLog(tmp_path / "nonexistent.jsonl")
    assert log.recent() == []


def test_entry_with_error(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    log.append(RunLogEntry(
        timestamp="2026-03-11T00:00:00Z",
        cycle=1,
        task="failing task",
        tier="high",
        model="claude-opus-4.6",
        success=False,
        output_preview="",
        error="Connection timeout",
        duration_s=30.0,
    ))
    entries = log.recent()
    assert entries[0].error == "Connection timeout"
    assert entries[0].success is False


def test_recent_skips_corrupted_lines(tmp_path: Path):
    """Corrupted JSONL lines should be skipped, not crash."""
    path = tmp_path / "log.jsonl"
    good = '{"timestamp":"2026-03-11T00:00:00Z","cycle":1,"task":"ok","tier":"low","model":"m","success":true,"output_preview":""}'
    path.write_text(good + "\n{broken json\n" + good + "\n", encoding="utf-8")
    log = RunLog(path)
    entries = log.recent()
    assert len(entries) == 2
    assert all(e.task == "ok" for e in entries)


# --- Seek-based tail read (Issue D optimization) ---


def _make_entry(i: int) -> str:
    """Create a JSONL line for task i."""
    return json.dumps({
        "timestamp": f"2026-03-11T00:{i % 60:02d}:00Z",
        "cycle": i,
        "task": f"task {i}",
        "tier": "low",
        "model": "m",
        "success": True,
        "output_preview": f"output for task {i}",
    }, ensure_ascii=False)


def test_seek_tail_large_file(tmp_path: Path):
    """Seek-based read returns correct last N entries for large files (>64KB)."""
    path = tmp_path / "big.jsonl"
    num_entries = 500
    # Write enough entries to exceed 64KB threshold
    lines = [_make_entry(i) for i in range(num_entries)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size > 65536, "File must exceed 64KB for seek path"

    log = RunLog(path)
    entries = log.recent(10)
    assert len(entries) == 10
    # Should be the last 10 entries
    assert entries[0].task == f"task {num_entries - 10}"
    assert entries[-1].task == f"task {num_entries - 1}"


def test_seek_tail_returns_all_when_n_exceeds_total(tmp_path: Path):
    """Requesting more entries than exist returns all entries."""
    path = tmp_path / "big.jsonl"
    num_entries = 500
    lines = [_make_entry(i) for i in range(num_entries)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size > 65536

    log = RunLog(path)
    entries = log.recent(9999)
    assert len(entries) == num_entries
    assert entries[0].task == "task 0"
    assert entries[-1].task == f"task {num_entries - 1}"


def test_seek_tail_handles_corrupted_lines_in_large_file(tmp_path: Path):
    """Seek-based read skips corrupted lines gracefully."""
    path = tmp_path / "big.jsonl"
    lines = [_make_entry(i) for i in range(500)]
    # Insert corrupted line near end
    lines.insert(498, "{broken json line here")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size > 65536

    log = RunLog(path)
    entries = log.recent(5)
    # Should get valid entries, skipping the corrupted one
    assert len(entries) >= 4
    assert all(hasattr(e, "task") for e in entries)


def test_seek_and_deque_agree(tmp_path: Path):
    """Seek-based and deque-based reads return identical results."""
    path = tmp_path / "big.jsonl"
    num_entries = 600
    lines = [_make_entry(i) for i in range(num_entries)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size > 65536

    log = RunLog(path)
    seek_results = log._recent_seek(20)
    deque_results = log._recent_deque(20)
    assert len(seek_results) == len(deque_results) == 20
    for s, d in zip(seek_results, deque_results):
        assert s.task == d.task
        assert s.cycle == d.cycle


def test_recent_small_file_uses_deque_path(tmp_path: Path):
    """Small files (<64KB) use deque path via _recent_seek fallback."""
    log = RunLog(tmp_path / "small.jsonl")
    for i in range(5):
        log.append(RunLogEntry(
            timestamp=f"2026-03-11T00:0{i}:00Z",
            cycle=i, task=f"task {i}", tier="low", model="m",
            success=True, output_preview="",
        ))
    entries = log.recent(3)
    assert len(entries) == 3
    assert entries[-1].task == "task 4"


def test_seek_tail_unicode_content(tmp_path: Path):
    """Seek-based read handles unicode (non-ASCII) content correctly."""
    path = tmp_path / "big.jsonl"
    # Mix ASCII and Unicode entries to exceed 64KB
    lines = []
    for i in range(500):
        entry = {
            "timestamp": f"2026-03-11T00:{i % 60:02d}:00Z",
            "cycle": i,
            "task": f"tâsk {i} — émojis: 🚀✅" if i % 3 == 0 else f"task {i}",
            "tier": "low",
            "model": "m",
            "success": True,
            "output_preview": "",
        }
        lines.append(json.dumps(entry, ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert path.stat().st_size > 65536

    log = RunLog(path)
    entries = log.recent(5)
    assert len(entries) == 5
    # Last entry at i=499 → 499 % 3 == 1 → plain "task 499"
    assert entries[-1].task == "task 499"
    # Entry at i=498 → 498 % 3 == 0 → unicode
    assert "émojis" in entries[-2].task


# --- Audit tests ---


def test_audit_empty(tmp_path: Path):
    log = RunLog(tmp_path / "nonexistent.jsonl")
    report = log.audit()
    assert report == {"downgrades": [], "top_tasks": [], "worst_cycle": None}


def test_audit_identifies_downgrade(tmp_path: Path):
    """High-tier task with tiny output, no tools, 1 turn should flag as downgrade."""
    log = RunLog(tmp_path / "log.jsonl")
    log.append(RunLogEntry(
        timestamp="2026-03-11T00:00:00Z", cycle=0, task="What is 2+2?",
        tier="high", model="claude-opus-4.6", success=True,
        output_preview="4", duration_s=3.0, premium_cost=3.0,
        num_turns=1, tools_used=[],
    ))
    report = log.audit()
    assert len(report["downgrades"]) == 1
    assert "low" in report["downgrades"][0]["action"]


def test_audit_no_downgrade_when_tools_used(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    log.append(RunLogEntry(
        timestamp="2026-03-11T00:00:00Z", cycle=0, task="Send email",
        tier="high", model="claude-opus-4.6", success=True,
        output_preview="Done.", duration_s=5.0, premium_cost=3.0,
        num_turns=2, tools_used=["gmail_send"],
    ))
    report = log.audit()
    assert len(report["downgrades"]) == 0


def test_audit_top_tasks(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    for i, premium in enumerate([3.0, 1.0, 3.0, 0.33, 3.0]):
        log.append(RunLogEntry(
            timestamp=f"2026-03-11T00:0{i}:00Z", cycle=0,
            task="expensive task" if premium == 3.0 else f"task {i}",
            tier="high" if premium == 3.0 else "low",
            model="m", success=True, output_preview="x" * 200,
            premium_cost=premium,
        ))
    report = log.audit()
    assert len(report["top_tasks"]) == 3
    assert report["top_tasks"][0]["total_premium"] == 9.0  # 3x "expensive task"


def test_audit_worst_cycle(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    # Cycle 1: 2/2 pass
    for ok in [True, True]:
        log.append(RunLogEntry(
            timestamp="2026-03-11T00:00:00Z", cycle=1, task="t",
            tier="low", model="m", success=ok, output_preview="",
        ))
    # Cycle 2: 0/3 pass
    for ok in [False, False, False]:
        log.append(RunLogEntry(
            timestamp="2026-03-11T00:01:00Z", cycle=2, task="t",
            tier="low", model="m", success=ok, output_preview="",
        ))
    report = log.audit()
    assert report["worst_cycle"] is not None
    assert report["worst_cycle"]["cycle"] == 2
    assert report["worst_cycle"]["pass_rate"] == "0%"
    assert report["worst_cycle"]["action"] == "Investigate failures"


# --- Boundary conditions ---


def test_recent_zero_returns_empty(tmp_path: Path):
    """recent(0) should return empty list."""
    log = RunLog(tmp_path / "log.jsonl")
    log.append(RunLogEntry(
        timestamp="2026-03-11T00:00:00Z", cycle=1, task="test",
        tier="low", model="m", success=True, output_preview="ok",
    ))
    assert log.recent(0) == []


def test_recent_negative_returns_empty(tmp_path: Path):
    """recent(-1) should handle gracefully, not crash."""
    log = RunLog(tmp_path / "log.jsonl")
    log.append(RunLogEntry(
        timestamp="2026-03-11T00:00:00Z", cycle=1, task="test",
        tier="low", model="m", success=True, output_preview="ok",
    ))
    assert log.recent(-1) == []


def test_summary_all_failed(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    for i in range(3):
        log.append(RunLogEntry(
            timestamp=f"2026-03-11T00:0{i}:00Z", cycle=1,
            task=f"fail {i}", tier="low", model="m",
            success=False, output_preview="",
        ))
    stats = log.summary()
    assert stats["pass_rate"] == "0%"
    assert stats["failed"] == 3
    assert stats["passed"] == 0


# --- Analyze tests ---


def test_analyze_empty(tmp_path: Path):
    log = RunLog(tmp_path / "nonexistent.jsonl")
    report = log.analyze()
    assert report["task_reliability"] == []
    assert report["failure_patterns"] == []
    assert report["suggestions"] == []


def test_analyze_task_reliability(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    for ok in [True, True, False]:
        log.append(RunLogEntry(
            timestamp="2026-03-11T10:00:00Z", cycle=1, task="email triage",
            tier="low", model="m", success=ok, output_preview="",
            duration_s=5.0, cost_usd=0.001,
            error=None if ok else "timeout",
        ))
    report = log.analyze()
    tasks = report["task_reliability"]
    assert len(tasks) == 1
    assert tasks[0]["total_runs"] == 3
    assert tasks[0]["pass_rate"] == round(2 / 3, 2)
    assert tasks[0]["avg_duration_s"] == 5.0
    assert tasks[0]["retry_count"] == 1


def test_analyze_failure_patterns(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    for i in range(4):
        log.append(RunLogEntry(
            timestamp="2026-03-11T10:00:00Z", cycle=1, task=f"task {i}",
            tier="low", model="m", success=False, output_preview="",
            error="Connection timeout" if i < 3 else "API error",
        ))
    report = log.analyze()
    patterns = report["failure_patterns"]
    assert len(patterns) == 2
    assert patterns[0]["pattern"] == "Connection timeout"
    assert patterns[0]["count"] == 3


def test_analyze_hour_performance(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    # 2 runs at 10:xx UTC, 1 pass 1 fail
    log.append(RunLogEntry(
        timestamp="2026-03-11T10:00:00Z", cycle=1, task="t",
        tier="low", model="m", success=True, output_preview="",
    ))
    log.append(RunLogEntry(
        timestamp="2026-03-11T10:30:00Z", cycle=1, task="t2",
        tier="low", model="m", success=False, output_preview="",
    ))
    report = log.analyze()
    assert 10 in report["hour_performance"]
    assert report["hour_performance"][10]["total"] == 2
    assert report["hour_performance"][10]["pass_rate"] == 0.5


def test_analyze_cycle_trend(tmp_path: Path):
    log = RunLog(tmp_path / "log.jsonl")
    for cycle, ok in [(1, True), (1, True), (2, True), (2, False), (3, False)]:
        log.append(RunLogEntry(
            timestamp="2026-03-11T10:00:00Z", cycle=cycle, task="t",
            tier="low", model="m", success=ok, output_preview="",
            premium_cost=0.33,
        ))
    report = log.analyze()
    trend = report["cycle_trend"]
    assert len(trend) == 3
    assert trend[0]["cycle"] == 1
    assert trend[0]["passed"] == 2
    assert trend[2]["cycle"] == 3
    assert trend[2]["failed"] == 1


def test_analyze_suggests_unreliable_task(tmp_path: Path):
    """Tasks with <50% pass rate over 3+ runs should generate a suggestion."""
    log = RunLog(tmp_path / "log.jsonl")
    for ok in [False, False, False, True]:
        log.append(RunLogEntry(
            timestamp="2026-03-11T10:00:00Z", cycle=1, task="flaky task",
            tier="low", model="m", success=ok, output_preview="",
            error=None if ok else "err",
        ))
    report = log.analyze()
    assert any("flaky task" in s for s in report["suggestions"])


def test_analyze_suggests_tier_escalation(tmp_path: Path):
    """Tasks using multiple tiers over 3+ runs should suggest explicit tier."""
    log = RunLog(tmp_path / "log.jsonl")
    for tier in ["low", "medium", "high"]:
        log.append(RunLogEntry(
            timestamp="2026-03-11T10:00:00Z", cycle=1, task="escalating task",
            tier=tier, model="m", success=True, output_preview="",
        ))
    report = log.analyze()
    assert any("escalating task" in s for s in report["suggestions"])


def test_analyze_suggests_declining_trend(tmp_path: Path):
    """Declining pass rates over 3 cycles should generate a suggestion."""
    log = RunLog(tmp_path / "log.jsonl")
    # Cycle 1: 3/3 pass, Cycle 2: 2/3 pass, Cycle 3: 1/3 pass
    for cycle, passes in [(1, 3), (2, 2), (3, 1)]:
        for i in range(3):
            log.append(RunLogEntry(
                timestamp="2026-03-11T10:00:00Z", cycle=cycle, task=f"t{i}",
                tier="low", model="m", success=i < passes, output_preview="",
                error=None if i < passes else "err",
            ))
    report = log.analyze()
    assert any("declining" in s.lower() for s in report["suggestions"])


# --- Forecast tests ---


def test_forecast_empty(tmp_path: Path):
    """No history → zero forecast."""
    log = RunLog(tmp_path / "nonexistent.jsonl")
    fc = log.forecast(30)
    assert fc["confidence"] == "none"
    assert fc["projected_usd"] == 0.0
    assert fc["projected_premium"] == 0.0


def test_forecast_single_entry(tmp_path: Path):
    """Single entry → low confidence, extrapolated by days."""
    log = RunLog(tmp_path / "log.jsonl")
    log.append(RunLogEntry(
        timestamp="2026-03-11T00:00:00Z", cycle=0, task="test",
        tier="low", model="m", success=True, output_preview="",
        cost_usd=0.01, premium_cost=0.33,
    ))
    fc = log.forecast(30)
    assert fc["confidence"] == "low"
    assert fc["data_days"] == 1
    assert fc["daily_rate_usd"] == 0.01
    assert fc["projected_usd"] == 0.3  # 0.01 * 30


def test_forecast_multi_day(tmp_path: Path):
    """Multiple entries across days → proper daily rate calculation."""
    log = RunLog(tmp_path / "log.jsonl")
    # 8 entries over 8 days (span = 7 days), $0.01 each
    for d in range(8):
        log.append(RunLogEntry(
            timestamp=f"2026-03-{11+d:02d}T12:00:00Z", cycle=d+1,
            task="daily task", tier="low", model="m", success=True,
            output_preview="", cost_usd=0.01, premium_cost=0.33,
        ))
    fc = log.forecast(30)
    assert fc["confidence"] == "high"  # >= 7 days span
    assert fc["data_days"] >= 7.0
    assert fc["projected_usd"] > 0
    assert fc["projected_cad"] > 0  # CAD should be populated


def test_forecast_medium_confidence(tmp_path: Path):
    """3-6 days of data → medium confidence."""
    log = RunLog(tmp_path / "log.jsonl")
    for d in range(4):
        log.append(RunLogEntry(
            timestamp=f"2026-03-{11+d:02d}T12:00:00Z", cycle=d+1,
            task="task", tier="low", model="m", success=True,
            output_preview="", cost_usd=0.02, premium_cost=0.33,
        ))
    fc = log.forecast(30)
    assert fc["confidence"] == "medium"


def test_forecast_custom_days(tmp_path: Path):
    """Forecasting for different day periods should scale."""
    log = RunLog(tmp_path / "log.jsonl")
    for d in range(7):
        log.append(RunLogEntry(
            timestamp=f"2026-03-{11+d:02d}T12:00:00Z", cycle=d+1,
            task="task", tier="low", model="m", success=True,
            output_preview="", cost_usd=0.07, premium_cost=0.33,
        ))
    fc_7 = log.forecast(7)
    fc_30 = log.forecast(30)
    # 30-day projection should be ~4.3x the 7-day projection
    assert fc_30["projected_usd"] > fc_7["projected_usd"]
