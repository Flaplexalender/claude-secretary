"""Tests for src.secretary.proposal_outcomes.

Covers the empirical-outcome feedback loop added in commit 4925e54:
    - record_baseline: snapshot last-N run_log entries on promotion
    - measure_pending_outcomes: fill outcome once N tasks landed after
    - _verdict: classify improvement/regression/neutral
    - format_recent_outcomes_for_prompt: markdown injection for LLM analysis

All tests use isolated tmp_path data roots and synthetic run_log entries —
no network, no LLM calls.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from secretary import proposal_outcomes as po


def _write_run_log(
    data_root: Path,
    entries: list[dict],
) -> None:
    path = data_root / po.RUN_LOG_FILE
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _entry(
    *,
    ts: datetime,
    success: bool = True,
    cost_usd: float = 0.05,
    num_turns: int = 5,
    duration_s: float = 10.0,
) -> dict:
    return {
        "timestamp": ts.isoformat(),
        "success": success,
        "cost_usd": cost_usd,
        "num_turns": num_turns,
        "duration_s": duration_s,
    }


# ── _snapshot_from_entries ────────────────────────────────────────────────


def test_snapshot_empty_returns_none() -> None:
    assert po._snapshot_from_entries([]) is None


def test_snapshot_computes_averages_and_cost_per_success() -> None:
    now = datetime.now(timezone.utc)
    entries = [
        _entry(ts=now, success=True, cost_usd=0.10, num_turns=4, duration_s=8.0),
        _entry(ts=now, success=True, cost_usd=0.20, num_turns=6, duration_s=12.0),
        _entry(ts=now, success=False, cost_usd=0.30, num_turns=10, duration_s=20.0),
    ]
    snap = po._snapshot_from_entries(entries)
    assert snap is not None
    # total cost $0.60, 2 successes → cps = $0.30
    assert snap.cost_per_success_usd == pytest.approx(0.30)
    # 2/3 successes
    assert snap.success_rate == pytest.approx(2 / 3)
    # average turns = 20/3
    assert snap.avg_turns == pytest.approx(20 / 3)
    assert snap.avg_duration_s == pytest.approx(40 / 3)
    assert snap.task_count == 3


def test_snapshot_all_failures_falls_back_to_total_cost() -> None:
    """Avoid div-by-zero when no successes: cps = total_cost."""
    now = datetime.now(timezone.utc)
    entries = [_entry(ts=now, success=False, cost_usd=0.05) for _ in range(4)]
    snap = po._snapshot_from_entries(entries)
    assert snap is not None
    assert snap.cost_per_success_usd == pytest.approx(0.20)
    assert snap.success_rate == 0.0


def test_snapshot_tolerates_missing_numeric_fields() -> None:
    """Entries missing cost_usd / num_turns / duration_s default to 0."""
    entries = [{"timestamp": "x", "success": True}]
    snap = po._snapshot_from_entries(entries)
    assert snap is not None
    # successes=1, total_cost=0 → cps=0
    assert snap.cost_per_success_usd == 0.0
    assert snap.avg_turns == 0.0
    assert snap.success_rate == 1.0


# ── _load_run_log ─────────────────────────────────────────────────────────


def test_load_run_log_returns_empty_when_file_missing(tmp_path: Path) -> None:
    assert po._load_run_log(tmp_path) == []


def test_load_run_log_skips_malformed_lines(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    good = _entry(ts=now)
    (tmp_path / po.RUN_LOG_FILE).write_text(
        json.dumps(good) + "\nnot-json\n\n" + json.dumps(good) + "\n",
        encoding="utf-8",
    )
    entries = po._load_run_log(tmp_path)
    assert len(entries) == 2


def test_load_run_log_filters_by_since_ts(tmp_path: Path) -> None:
    t0 = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 4, 20, 11, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 20, 12, 0, 0, tzinfo=timezone.utc)
    _write_run_log(tmp_path, [_entry(ts=t0), _entry(ts=t1), _entry(ts=t2)])
    # since just after t1 → only t2 should pass
    after = po._load_run_log(tmp_path, since_ts=t1.timestamp() + 1)
    assert len(after) == 1
    assert after[0]["timestamp"] == t2.isoformat()


# ── record_baseline ───────────────────────────────────────────────────────


def test_record_baseline_skips_when_run_log_empty(tmp_path: Path) -> None:
    ok = po.record_baseline(
        tmp_path, proposal_id="abc", commit_hash="abc1234",
        task="t", description="d",
    )
    assert ok is False
    assert not (tmp_path / po.OUTCOMES_FILE).exists()


def test_record_baseline_appends_pending_record(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_run_log(tmp_path, [_entry(ts=now, cost_usd=0.1) for _ in range(20)])

    ok = po.record_baseline(
        tmp_path, proposal_id="abc1234", commit_hash="abc1234deadbeef",
        task="improve router", description="add LRU cache",
        baseline_window=15,
    )
    assert ok is True
    recs = po._read_outcomes(tmp_path)
    assert len(recs) == 1
    r = recs[0]
    assert r["proposal_id"] == "abc1234"
    assert r["commit_hash"] == "abc1234deadbeef"
    assert r["outcome"] is None
    assert r["baseline"]["task_count"] == 15
    assert r["promoted_at"] > 0


def test_record_baseline_truncates_task_and_description(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_run_log(tmp_path, [_entry(ts=now)])
    long_task = "x" * 500
    long_desc = "y" * 500
    po.record_baseline(
        tmp_path, proposal_id="p", commit_hash="h",
        task=long_task, description=long_desc,
    )
    r = po._read_outcomes(tmp_path)[0]
    assert len(r["task"]) == 200
    assert len(r["description"]) == 300


def test_record_baseline_appends_not_overwrites(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_run_log(tmp_path, [_entry(ts=now)])
    po.record_baseline(tmp_path, proposal_id="p1", commit_hash="h1", task="t", description="d")
    po.record_baseline(tmp_path, proposal_id="p2", commit_hash="h2", task="t", description="d")
    recs = po._read_outcomes(tmp_path)
    assert [r["proposal_id"] for r in recs] == ["p1", "p2"]


# ── _verdict ──────────────────────────────────────────────────────────────


def test_verdict_improvement_when_cps_drops() -> None:
    assert po._verdict({"cost_per_success": -10.0}) == "improvement"
    assert po._verdict({"cost_per_success": -po.IMPROVEMENT_PCT}) == "improvement"


def test_verdict_regression_when_cps_rises() -> None:
    assert po._verdict({"cost_per_success": 20.0}) == "regression"
    assert po._verdict({"cost_per_success": po.REGRESSION_PCT}) == "regression"


def test_verdict_neutral_when_change_small() -> None:
    assert po._verdict({"cost_per_success": 0.0}) == "neutral"
    assert po._verdict({"cost_per_success": 3.0}) == "neutral"
    assert po._verdict({"cost_per_success": -3.0}) == "neutral"


def test_verdict_missing_key_is_neutral() -> None:
    assert po._verdict({}) == "neutral"


# ── measure_pending_outcomes ──────────────────────────────────────────────


def test_measure_pending_outcomes_empty_file(tmp_path: Path) -> None:
    assert po.measure_pending_outcomes(tmp_path) == 0


def test_measure_pending_outcomes_waits_for_enough_post_tasks(tmp_path: Path) -> None:
    """Pending outcome is NOT measured until ≥min_tasks_after post-promotion."""
    # 20 entries all BEFORE promotion
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    _write_run_log(tmp_path, [_entry(ts=t_before) for _ in range(20)])
    po.record_baseline(tmp_path, proposal_id="p", commit_hash="h", task="t", description="d")

    # No post-promotion entries yet — should do nothing.
    assert po.measure_pending_outcomes(tmp_path, min_tasks_after=15) == 0
    rec = po._read_outcomes(tmp_path)[0]
    assert rec["outcome"] is None


def test_measure_pending_outcomes_records_improvement(tmp_path: Path) -> None:
    """Baseline expensive+failing → after cheap+succeeding → verdict=improvement."""
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    # Baseline: 15 expensive half-failing entries (cps high)
    baseline_entries = [
        _entry(ts=t_before, success=(i % 2 == 0), cost_usd=0.20)
        for i in range(15)
    ]
    _write_run_log(tmp_path, baseline_entries)
    po.record_baseline(
        tmp_path, proposal_id="p1", commit_hash="h1",
        task="t", description="d", baseline_window=15,
    )
    rec = po._read_outcomes(tmp_path)[0]
    promoted_at = rec["promoted_at"]

    # Post-promotion: 15 entries all succeed, cheaper
    t_after = datetime.fromtimestamp(promoted_at + 60, tz=timezone.utc)
    after_entries = [
        _entry(ts=t_after, success=True, cost_usd=0.05) for _ in range(15)
    ]
    _write_run_log(tmp_path, baseline_entries + after_entries)

    changed = po.measure_pending_outcomes(tmp_path, min_tasks_after=15)
    assert changed == 1
    rec2 = po._read_outcomes(tmp_path)[0]
    assert rec2["outcome"] is not None
    assert rec2["outcome"]["verdict"] == "improvement"
    # cost-per-success went from $0.40 (0.20*15/8 successes) down → negative pct
    assert rec2["outcome"]["delta_pct"]["cost_per_success"] < -po.IMPROVEMENT_PCT


def test_measure_pending_outcomes_records_regression(tmp_path: Path) -> None:
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    # Baseline: cheap, all succeed
    baseline = [_entry(ts=t_before, success=True, cost_usd=0.05) for _ in range(15)]
    _write_run_log(tmp_path, baseline)
    po.record_baseline(
        tmp_path, proposal_id="p", commit_hash="h",
        task="t", description="d",
    )
    rec = po._read_outcomes(tmp_path)[0]
    # After: expensive, half fail — cps way up
    t_after = datetime.fromtimestamp(rec["promoted_at"] + 60, tz=timezone.utc)
    after = [
        _entry(ts=t_after, success=(i % 2 == 0), cost_usd=0.50)
        for i in range(15)
    ]
    _write_run_log(tmp_path, baseline + after)

    assert po.measure_pending_outcomes(tmp_path, min_tasks_after=15) == 1
    rec2 = po._read_outcomes(tmp_path)[0]
    assert rec2["outcome"]["verdict"] == "regression"


def test_measure_pending_outcomes_is_idempotent(tmp_path: Path) -> None:
    """Re-running should NOT re-measure already-measured records."""
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    baseline = [_entry(ts=t_before) for _ in range(15)]
    _write_run_log(tmp_path, baseline)
    po.record_baseline(tmp_path, proposal_id="p", commit_hash="h", task="t", description="d")
    rec = po._read_outcomes(tmp_path)[0]
    t_after = datetime.fromtimestamp(rec["promoted_at"] + 60, tz=timezone.utc)
    _write_run_log(tmp_path, baseline + [_entry(ts=t_after) for _ in range(15)])

    assert po.measure_pending_outcomes(tmp_path) == 1
    # Second call — outcome already filled, no change
    assert po.measure_pending_outcomes(tmp_path) == 0


# ── format_recent_outcomes_for_prompt ─────────────────────────────────────


def test_format_returns_empty_when_nothing_measured(tmp_path: Path) -> None:
    assert po.format_recent_outcomes_for_prompt(tmp_path) == ""


def test_format_returns_empty_when_only_pending(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_run_log(tmp_path, [_entry(ts=now)])
    po.record_baseline(tmp_path, proposal_id="p", commit_hash="h", task="t", description="d")
    # outcome is None → no measured outcomes
    assert po.format_recent_outcomes_for_prompt(tmp_path) == ""


def test_format_shows_measured_outcomes_with_verdict(tmp_path: Path) -> None:
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    baseline = [_entry(ts=t_before, success=True, cost_usd=0.05) for _ in range(15)]
    _write_run_log(tmp_path, baseline)
    po.record_baseline(
        tmp_path, proposal_id="p", commit_hash="h",
        task="t", description="cache router estimates",
    )
    rec = po._read_outcomes(tmp_path)[0]
    t_after = datetime.fromtimestamp(rec["promoted_at"] + 60, tz=timezone.utc)
    # big regression
    after = [_entry(ts=t_after, success=False, cost_usd=0.50) for _ in range(15)]
    _write_run_log(tmp_path, baseline + after)
    po.measure_pending_outcomes(tmp_path)

    md = po.format_recent_outcomes_for_prompt(tmp_path)
    assert "REGRESSION" in md
    assert "cache router estimates" in md
    assert "cost/success:" in md
    assert "success rate:" in md


def test_format_limits_to_max_n(tmp_path: Path) -> None:
    """Only the N most-recently-measured records appear."""
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    baseline = [_entry(ts=t_before) for _ in range(15)]

    for i in range(4):
        _write_run_log(tmp_path, baseline)
        po.record_baseline(
            tmp_path, proposal_id=f"p{i}", commit_hash=f"h{i}",
            task="t", description=f"change-{i}",
        )
        rec = po._read_outcomes(tmp_path)[-1]
        t_after = datetime.fromtimestamp(rec["promoted_at"] + 60 + i, tz=timezone.utc)
        _write_run_log(tmp_path, baseline + [_entry(ts=t_after) for _ in range(15)])
        # Tiny sleep so measured_at timestamps differ
        time.sleep(0.01)
        po.measure_pending_outcomes(tmp_path)

    md = po.format_recent_outcomes_for_prompt(tmp_path, max_n=2)
    # Should contain only the 2 most recent descriptions
    assert md.count("**[") == 2


# ── _is_environmental_failure / _filter_environmental ────────────────────


def test_env_failure_detects_baseline_broken() -> None:
    """Entries with 'Pre-test baseline FAILED' prefix are environmental."""
    e = {
        "success": False,
        "error": "Pre-test baseline FAILED — tests broken before agent ran. Affected: ...",
    }
    assert po._is_environmental_failure(e) is True


def test_env_failure_detects_proxy_connection_refused() -> None:
    """Proxy-down entries are environmental, not agent-caused."""
    e = {
        "success": False,
        "error": "ConnectionRefusedError: [WinError 10061] No connection could be made",
    }
    assert po._is_environmental_failure(e) is True


def test_env_failure_false_for_success() -> None:
    """Successful entries are never environmental failures."""
    assert po._is_environmental_failure({"success": True, "error": "Pre-test baseline FAILED"}) is False


def test_env_failure_false_for_agent_failure() -> None:
    """Normal agent failures (non-baseline, non-proxy) are not environmental."""
    e = {"success": False, "error": "Sandbox tests failed after code changes"}
    assert po._is_environmental_failure(e) is False


def test_env_failure_false_for_missing_error_field() -> None:
    """Missing/None/non-str error field doesn't crash the check."""
    assert po._is_environmental_failure({"success": False}) is False
    assert po._is_environmental_failure({"success": False, "error": None}) is False
    assert po._is_environmental_failure({"success": False, "error": 123}) is False


def test_record_baseline_excludes_environmental_failures(tmp_path: Path) -> None:
    """Baseline snapshot must ignore environmental failures.

    Regression for Apr 22 window: 18 'Pre-test baseline FAILED' entries
    in a day would have crushed success_rate from 1.0 to ~0.0 and
    inflated cps, poisoning every outcome measured during that window.
    """
    now = datetime.now(timezone.utc)
    real_runs = [
        _entry(ts=now, success=True, cost_usd=0.10, num_turns=5, duration_s=10.0)
        for _ in range(15)
    ]
    env_failures = [
        {
            "timestamp": now.isoformat(),
            "success": False,
            "cost_usd": 0.0,
            "num_turns": 0,
            "duration_s": 0.0,
            "error": "Pre-test baseline FAILED — tests broken before agent ran",
        }
        for _ in range(10)
    ]
    # Env failures are the most recent; a naive last-15 window would be
    # dominated by them. After filtering, the window should be the 15
    # real successful runs.
    _write_run_log(tmp_path, real_runs + env_failures)

    ok = po.record_baseline(
        tmp_path, proposal_id="p", commit_hash="h",
        task="t", description="d",
    )
    assert ok is True
    rec = po._read_outcomes(tmp_path)[0]
    assert rec["baseline"]["task_count"] == 15
    assert rec["baseline"]["success_rate"] == 1.0
    # cps = total_cost / successes = $1.50 / 15 = $0.10
    assert rec["baseline"]["cost_per_success_usd"] == pytest.approx(0.10)


def test_measure_pending_outcomes_excludes_environmental_in_after_window(
    tmp_path: Path,
) -> None:
    """After-window snapshot must also filter environmental failures."""
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    baseline = [_entry(ts=t_before, success=True, cost_usd=0.10) for _ in range(15)]
    _write_run_log(tmp_path, baseline)
    po.record_baseline(
        tmp_path, proposal_id="p", commit_hash="h",
        task="t", description="d",
    )
    rec = po._read_outcomes(tmp_path)[0]
    # Build an after-window with 15 real successes + 20 env-failures
    # interleaved. Without the filter, min_tasks_after=15 would include
    # the env failures and drag metrics down.
    t_after = datetime.fromtimestamp(rec["promoted_at"] + 60, tz=timezone.utc)
    after_real = [
        _entry(ts=t_after, success=True, cost_usd=0.10)
        for _ in range(15)
    ]
    after_env = [
        {
            "timestamp": t_after.isoformat(),
            "success": False,
            "cost_usd": 0.0,
            "num_turns": 0,
            "duration_s": 0.0,
            "error": "Pre-test baseline FAILED",
        }
        for _ in range(20)
    ]
    # Put env failures FIRST in the after stream so without the filter
    # they'd dominate the first-15 slice.
    _write_run_log(tmp_path, baseline + after_env + after_real)

    n = po.measure_pending_outcomes(tmp_path)
    assert n == 1
    rec = po._read_outcomes(tmp_path)[0]
    outcome = rec["outcome"]
    assert outcome is not None
    # after_task_count should be 15 (min_tasks_after) of REAL runs.
    assert outcome["after_task_count"] == 15
    assert outcome["after"]["success_rate"] == 1.0
    # Baseline cps=$0.10, after cps=$0.10 → neutral, ~0% delta
    assert outcome["verdict"] == "neutral"


# ── End-to-end wiring: ensure three call sites agree on data_root ─────────


def test_end_to_end_wiring_single_data_root(tmp_path: Path) -> None:
    """Regression test for the three-call-site divergence bug.

    record_baseline (called from self_improve.py after promotion),
    measure_pending_outcomes (called from watcher._run_cycle), and
    format_recent_outcomes_for_prompt (called from
    goal_self_improve._run_failure_analysis) MUST all use the same
    data_root. Previously self_improve.py hardcoded `project / "data"`
    while the other two used `config.data_path`, so under --instance
    or non-project-root CWD, outcomes got written to one file and
    read from another → ACE feedback loop silently dropped all
    signal.

    This test exercises the full pipeline against a single data_root
    and verifies the prompt-injection text reflects the verdict from
    the measured outcome — proving all three share state.
    """
    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    baseline = [_entry(ts=t_before, success=True, cost_usd=0.10) for _ in range(15)]
    _write_run_log(tmp_path, baseline)

    # Call site 1: record_baseline (self_improve.py after git commit)
    ok = po.record_baseline(
        tmp_path, proposal_id="abc1234",
        commit_hash="abc1234deadbeef",
        task="Improve caching", description="Add LRU cache",
    )
    assert ok is True

    # Simulate 15 tasks landing after the promotion with cheaper cost —
    # should produce an "improvement" verdict.
    rec = po._read_outcomes(tmp_path)[0]
    t_after = datetime.fromtimestamp(rec["promoted_at"] + 60, tz=timezone.utc)
    after_runs = [_entry(ts=t_after, success=True, cost_usd=0.05) for _ in range(15)]
    _write_run_log(tmp_path, baseline + after_runs)

    # Call site 2: measure_pending_outcomes (watcher._run_cycle)
    measured = po.measure_pending_outcomes(tmp_path)
    assert measured == 1

    # Call site 3: format_recent_outcomes_for_prompt (goal_self_improve)
    md = po.format_recent_outcomes_for_prompt(tmp_path)
    assert md != ""
    assert "IMPROVEMENT" in md
    assert "Add LRU cache" in md
    # The prompt must report the actual measured delta, not an empty record.
    assert "cost/success:" in md
    # With cost halving, cps should drop ~50%. Just verify we see a
    # negative-cost-delta line (improvement sign convention: negative).
    assert "-" in md  # some negative delta rendered


def test_end_to_end_wiring_fails_on_divergent_data_roots(tmp_path: Path) -> None:
    """If one call site writes to path A and another reads from path B,
    the ACE feedback loop produces NO prompt injection. Guards against
    any future re-introduction of the same class of bug."""
    path_a = tmp_path / "a"
    path_b = tmp_path / "b"
    path_a.mkdir()
    path_b.mkdir()

    t_before = datetime(2026, 4, 20, 10, 0, 0, tzinfo=timezone.utc)
    baseline = [_entry(ts=t_before, success=True, cost_usd=0.10) for _ in range(15)]
    _write_run_log(path_a, baseline)

    # record_baseline → path_a
    po.record_baseline(
        path_a, proposal_id="x", commit_hash="y",
        task="t", description="diverged-write",
    )
    # measure_pending_outcomes → path_b (wrong)
    assert po.measure_pending_outcomes(path_b) == 0
    # format_recent_outcomes_for_prompt → path_b (wrong)
    md = po.format_recent_outcomes_for_prompt(path_b)
    assert md == ""
    # The signal exists in path_a but is invisible to path_b — exactly
    # the class of silent failure the single-source-of-truth fix prevents.

