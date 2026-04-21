"""Tests for watcher — budget caps, dry-run mode, CLI wiring. No API calls."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from secretary.config import SecretaryConfig
from secretary.router import get_premium_cost, TIER_MULTIPLIERS
from secretary.run_log import RunLogEntry
from secretary.watcher import Watcher


# ── get_premium_cost ──────────────────────────────────────────


def test_get_premium_cost_haiku():
    assert get_premium_cost("claude-haiku-4.5") == 0.33


def test_get_premium_cost_sonnet():
    assert get_premium_cost("claude-sonnet-4.6") == 1.0


def test_get_premium_cost_opus():
    assert get_premium_cost("claude-opus-4.7") == 3.0


def test_get_premium_cost_unknown_defaults_to_1():
    assert get_premium_cost("some-future-model") == 1.0


# ── RunLogEntry premium_cost field ────────────────────────────


def test_run_log_entry_default_premium_cost():
    entry = RunLogEntry(
        timestamp="2026-03-11T00:00:00Z",
        cycle=1, task="t", tier="low", model="m",
        success=True, output_preview="",
    )
    assert entry.premium_cost == 0.0


def test_run_log_entry_explicit_premium_cost():
    entry = RunLogEntry(
        timestamp="2026-03-11T00:00:00Z",
        cycle=1, task="t", tier="low", model="claude-haiku-4.5",
        success=True, output_preview="", premium_cost=0.33,
    )
    assert entry.premium_cost == 0.33


# ── Watcher construction ─────────────────────────────────────


def test_watcher_dry_run_flag(tmp_path: Path):
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, dry_run=True)
    assert w.dry_run is True


def test_watcher_max_premium_from_config(tmp_path: Path):
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_premium_per_cycle = 5.0
    w = Watcher(config=config)
    assert w.max_premium_per_cycle == 5.0


# ── Watcher._run_cycle dry-run mode ──────────────────────────


@dataclass
class _FakeRouting:
    tier: str = "low"
    model: str = "claude-haiku-4.5"
    max_turns: int = 10
    max_budget_usd: float = 0.0
    reason: str = "test"


@pytest.fixture
def campaign_file(tmp_path: Path) -> Path:
    f = tmp_path / "campaign.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Check email\n"
        "    tier: low\n"
        "  - prompt: Review calendar\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    return f


@pytest.fixture
def watcher_dry(tmp_path: Path, campaign_file: Path) -> Watcher:
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_premium_per_cycle = 3.0
    return Watcher(config=config, campaign_file=campaign_file, dry_run=True)


def test_dry_run_does_not_call_agent(watcher_dry: Watcher):
    """Dry-run mode should never touch the agent."""
    with patch("secretary.watcher.direct_agent") as mock_agent:
        passed, failed, *_ = asyncio.run(watcher_dry._run_cycle(MagicMock()))
    mock_agent.run.assert_not_called()
    assert passed == 2
    assert failed == 0


# ── Premium budget cap ─────────────────────────────────────────


@pytest.fixture
def expensive_campaign(tmp_path: Path) -> Path:
    """Campaign with 2 opus tasks (3x each = 6x total)."""
    f = tmp_path / "expensive.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Deep architecture review\n"
        "    tier: high\n"
        "  - prompt: Security audit everything\n"
        "    tier: high\n",
        encoding="utf-8",
    )
    return f


@dataclass
class _FakeResult:
    text: str = "done"
    error: str | None = None
    routing: _FakeRouting = None
    cost_usd: float = 0.0
    num_turns: int = 0
    tools_used: list = field(default_factory=list)

    def __post_init__(self):
        if self.routing is None:
            self.routing = _FakeRouting(tier="high", model="claude-opus-4.7")


def test_budget_cap_skips_excess(tmp_path: Path, expensive_campaign: Path):
    """With cap=3.0, first opus task (3.0) runs, second (3.0) is skipped."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_premium_per_cycle = 3.0
    w = Watcher(config=config, campaign_file=expensive_campaign)

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = AsyncMock(return_value=_FakeResult())
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    # Only one task should have been executed — second skipped by budget
    assert mock_agent.run.call_count == 1
    assert passed == 1
    assert failed == 0


def test_budget_unlimited_runs_all(tmp_path: Path, expensive_campaign: Path):
    """With cap=0.0 (unlimited), all tasks run regardless of cost."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_premium_per_cycle = 0.0  # unlimited
    w = Watcher(config=config, campaign_file=expensive_campaign)

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = AsyncMock(return_value=_FakeResult())
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    assert mock_agent.run.call_count == 2
    assert passed == 2


def test_budget_tracks_cumulative(tmp_path: Path):
    """Mixed-tier campaign: 2 low (0.33 each) + 1 high (3.0) with cap=1.0.
    The two low tasks fit (0.66 total) but the high task exceeds budget."""
    f = tmp_path / "mixed.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Quick check\n"
        "    tier: low\n"
        "  - prompt: Another check\n"
        "    tier: low\n"
        "  - prompt: Deep review\n"
        "    tier: high\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_premium_per_cycle = 1.0
    w = Watcher(config=config, campaign_file=f)

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = AsyncMock(return_value=_FakeResult(
            routing=_FakeRouting(tier="low", model="claude-haiku-4.5"),
        ))
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    # 2 low tasks run (0.66 total), high task skipped (would push to 3.66)
    assert mock_agent.run.call_count == 2
    assert passed == 2


# ── Error retry with backoff ──────────────────────────────────


def test_retry_on_failure(tmp_path: Path, campaign_file: Path):
    """Failed tasks are retried up to max_retries times."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 2
    config.watcher.retry_base_delay = 0.0  # no actual delay in tests
    w = Watcher(config=config, campaign_file=campaign_file)

    # First task: fail twice then succeed. Second task: succeed immediately.
    call_count = 0

    async def _mock_run(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return _FakeResult(
                error="transient failure",
                routing=_FakeRouting(tier="low", model="claude-haiku-4.5"),
            )
        return _FakeResult(
            routing=_FakeRouting(tier="low", model="claude-haiku-4.5"),
        )

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = _mock_run
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    # First task: 2 failures + 1 success = 3 calls, then second task = 1 call
    assert call_count == 4
    assert passed == 2
    assert failed == 0


def test_retry_exhausted(tmp_path: Path):
    """Task that fails all retries is counted as failed."""
    f = tmp_path / "single.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Always fails\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 1
    config.watcher.retry_base_delay = 0.0
    w = Watcher(config=config, campaign_file=f)

    async def _always_fail(**kwargs):
        return _FakeResult(
            error="permanent failure",
            routing=_FakeRouting(tier="low", model="claude-haiku-4.5"),
        )

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = _always_fail
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    assert passed == 0
    assert failed == 1


def test_no_retry_when_zero(tmp_path: Path):
    """With max_retries=0, failures are not retried."""
    f = tmp_path / "single.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Fails once\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 0
    w = Watcher(config=config, campaign_file=f)

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = AsyncMock(return_value=_FakeResult(
            error="fail",
            routing=_FakeRouting(tier="low", model="claude-haiku-4.5"),
        ))
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    assert mock_agent.run.call_count == 1
    assert failed == 1


def test_retry_exception_then_success(tmp_path: Path):
    """Exception on first try, success on retry."""
    f = tmp_path / "single.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Unstable task\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 1
    config.watcher.retry_base_delay = 0.0
    w = Watcher(config=config, campaign_file=f)

    call_count = 0

    async def _exception_then_ok(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise ConnectionError("network blip")
        return _FakeResult(
            routing=_FakeRouting(tier="low", model="claude-haiku-4.5"),
        )

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = _exception_then_ok
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    assert call_count == 2
    assert passed == 1
    assert failed == 0


# ── Task dedup ────────────────────────────────────────────────


def test_duplicate_tasks_skipped(tmp_path: Path):
    """Identical prompts in the same cycle are deduped."""
    f = tmp_path / "dupes.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Check email\n"
        "    tier: low\n"
        "  - prompt: Check email\n"
        "    tier: low\n"
        "  - prompt: Different task\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    # 2 unique tasks (duplicate skipped)
    assert passed == 2
    assert failed == 0
    assert failed == 0


# ── Cycle premium tracking ────────────────────────────────────


def test_cycle_returns_premium_spent(tmp_path: Path, campaign_file: Path):
    """_run_cycle returns cumulative premium spend for the cycle."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=campaign_file)

    with patch("secretary.watcher.direct_agent") as mock_agent:
        mock_agent.run = AsyncMock(return_value=_FakeResult(
            routing=_FakeRouting(tier="low", model="claude-haiku-4.5"),
        ))
        passed, failed, premium, _ = asyncio.run(w._run_cycle(MagicMock()))

    # 2 tasks at 0.33x each = 0.66
    assert passed == 2
    assert abs(premium - 0.66) < 0.01


def test_dry_run_returns_no_cost(tmp_path: Path, campaign_file: Path):
    """Dry-run mode returns zero cost (no API calls)."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    _, _, premium, _ = asyncio.run(w._run_cycle(MagicMock()))
    assert premium == 0.0


# ── Watcher summary tracking ─────────────────────────────────


def test_watcher_tracks_totals(tmp_path: Path, campaign_file: Path):
    """Watcher accumulates per-cycle stats into session totals."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 2
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    asyncio.run(w.run())

    # 2 cycles × 2 tasks each = 4 total passed
    assert w._runs_completed == 2
    assert w._total_passed == 4
    assert w._total_task_failures == 0
    assert w._total_premium_spent == 0.0  # dry-run


def test_consolidation_batching(tmp_path: Path, campaign_file: Path):
    """Memory consolidation only runs every N cycles, not every cycle."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 4
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    # Default _consolidate_every = 3, so with 4 cycles:
    # Cycle 1: no consolidation (1 % 3 != 0)
    # Cycle 2: no consolidation (2 % 3 != 0)
    # Cycle 3: consolidation (3 % 3 == 0)
    # Cycle 4: no consolidation (4 % 3 != 0)
    with patch("secretary.memory.MemoryStore.consolidate") as mock_consolidate:
        asyncio.run(w.run())

    # Should be called once (at cycle 3)
    assert mock_consolidate.call_count == 1


def test_format_duration():
    """Static duration formatter."""
    assert Watcher._format_duration(5) == "5s"
    assert Watcher._format_duration(65) == "1m 5s"
    assert Watcher._format_duration(3665) == "1h 1m"


# ── Cross-cycle dedup ────────────────────────────────────────


def test_cross_cycle_dedup_skips_recent_success(tmp_path: Path, campaign_file: Path):
    """Tasks that succeeded in the previous cycle are skipped."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 2
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    asyncio.run(w.run())

    # Cycle 1: 2 tasks run (both pass) → tracked in _success_history
    # Cycle 2: both tasks skipped (succeeded last cycle) → counted as passed
    assert w._runs_completed == 2
    assert w._total_passed == 4  # 2 from cycle 1 + 2 skipped-as-passed from cycle 2


def test_cross_cycle_dedup_disabled_per_task(tmp_path: Path):
    """Tasks with skip_if_recent=false always run."""
    f = tmp_path / "no_skip.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Always run me\n"
        "    tier: low\n"
        "    skip_if_recent: false\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 2
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    asyncio.run(w.run())

    # Both cycles should run the task (not skipped despite recent success)
    assert w._runs_completed == 2
    assert w._total_passed == 2


# ── Schedule expressions ─────────────────────────────────────


from secretary.watcher import _check_schedule
from datetime import datetime


def test_schedule_hours_in_range():
    """Task runs when current hour is within range."""
    now = datetime(2026, 3, 12, 9, 30)  # 9:30 AM
    assert _check_schedule("hours:8-17", now) is True


def test_schedule_hours_out_of_range():
    """Task skipped when current hour is outside range."""
    now = datetime(2026, 3, 12, 20, 0)  # 8 PM
    assert _check_schedule("hours:8-17", now) is False


def test_schedule_hours_multiple_ranges():
    """Multiple hour ranges with comma."""
    morning = datetime(2026, 3, 12, 8, 0)
    afternoon = datetime(2026, 3, 12, 14, 0)
    evening = datetime(2026, 3, 12, 18, 0)
    assert _check_schedule("hours:6-10,17-20", morning) is True
    assert _check_schedule("hours:6-10,17-20", afternoon) is False
    assert _check_schedule("hours:6-10,17-20", evening) is True


def test_schedule_weekdays():
    """weekdays rule passes Mon-Fri."""
    monday = datetime(2026, 3, 9, 12, 0)     # Monday
    saturday = datetime(2026, 3, 14, 12, 0)  # Saturday
    assert _check_schedule("weekdays", monday) is True
    assert _check_schedule("weekdays", saturday) is False


def test_schedule_weekends():
    """weekends rule passes Sat-Sun."""
    friday = datetime(2026, 3, 13, 12, 0)    # Friday
    sunday = datetime(2026, 3, 15, 12, 0)    # Sunday
    assert _check_schedule("weekends", friday) is False
    assert _check_schedule("weekends", sunday) is True


def test_schedule_combined_rules():
    """Multiple rules with semicolons — all must match."""
    # Weekday morning at 9 AM
    ok = datetime(2026, 3, 9, 9, 0)       # Monday 9 AM
    wrong_time = datetime(2026, 3, 9, 20, 0)  # Monday 8 PM
    wrong_day = datetime(2026, 3, 14, 9, 0)   # Saturday 9 AM

    assert _check_schedule("hours:8-17;weekdays", ok) is True
    assert _check_schedule("hours:8-17;weekdays", wrong_time) is False
    assert _check_schedule("hours:8-17;weekdays", wrong_day) is False


def test_schedule_empty_always_runs():
    """Empty schedule string → always runs."""
    assert _check_schedule("") is True


def test_schedule_malformed_hours_graceful():
    """Malformed hours range (non-numeric) should not crash."""
    now = datetime(2026, 3, 12, 10, 0)
    # Malformed range treated as no matching range → returns False
    assert _check_schedule("hours:abc-def", now) is False


def test_schedule_unknown_rule_warns():
    """Unknown rule should be ignored (logged), not block the schedule."""
    now = datetime(2026, 3, 9, 10, 0)  # Monday 10 AM
    # Unknown rule is ignored, so it still passes
    assert _check_schedule("foobar", now) is True


def test_schedule_boundary_hour_exact():
    """Hour boundary: start_h <= hour < end_h (exclusive end)."""
    at_8 = datetime(2026, 3, 12, 8, 0)
    at_17 = datetime(2026, 3, 12, 17, 0)
    assert _check_schedule("hours:8-17", at_8) is True   # inclusive start
    assert _check_schedule("hours:8-17", at_17) is False   # exclusive end


def test_schedule_overnight_wrap_around():
    """Overnight range hours:22-6 should match 22-23 and 0-5."""
    assert _check_schedule("hours:22-6", datetime(2026, 3, 12, 23, 0)) is True
    assert _check_schedule("hours:22-6", datetime(2026, 3, 12, 22, 0)) is True
    assert _check_schedule("hours:22-6", datetime(2026, 3, 12, 3, 0)) is True
    assert _check_schedule("hours:22-6", datetime(2026, 3, 12, 5, 59)) is True
    assert _check_schedule("hours:22-6", datetime(2026, 3, 12, 6, 0)) is False
    assert _check_schedule("hours:22-6", datetime(2026, 3, 12, 12, 0)) is False
    assert _check_schedule("hours:22-6", datetime(2026, 3, 12, 21, 0)) is False


def test_schedule_skips_task_in_campaign(tmp_path: Path):
    """Watcher respects schedule field in campaign YAML."""
    # Create a campaign where all tasks have a schedule that won't match
    f = tmp_path / "sched.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Morning task\n"
        "    tier: low\n"
        "    schedule: 'hours:14-15'\n"  # 2-3 PM — unlikely to match during test runs
        "  - prompt: Always task\n"
        "    tier: low\n",  # No schedule — always runs
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    asyncio.run(w.run())

    # Only "Always task" should run; "Morning task" skipped by schedule
    assert w._total_passed == 1


# ── Persistent dedup ─────────────────────────────────────────


def test_dedup_history_persists_to_disk(tmp_path: Path, campaign_file: Path):
    """Dedup history is saved to disk after each cycle."""
    import json
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    asyncio.run(w.run())

    dedup_path = tmp_path / "data" / "dedup_history.json"
    assert dedup_path.exists()
    data = json.loads(dedup_path.read_text(encoding="utf-8"))
    assert len(data) == 2  # 2 tasks in default campaign_file fixture


def test_dedup_history_loads_on_restart(tmp_path: Path, campaign_file: Path):
    """Dedup history from previous run is loaded on restart."""
    import json
    data_dir = tmp_path / "data"

    # Run 1: populate dedup history
    config = SecretaryConfig(data_root=str(data_dir))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w1 = Watcher(config=config, campaign_file=campaign_file, dry_run=True)
    asyncio.run(w1.run())

    # Run 2: new Watcher instance loads history
    w2 = Watcher(config=config, campaign_file=campaign_file, dry_run=True)
    assert len(w2._success_history) == 2


# ── Multi-campaign ───────────────────────────────────────────


def test_multi_campaign_loads_all_tasks(tmp_path: Path):
    """Comma-separated campaign files load tasks from all files."""
    f1 = tmp_path / "camp1.yaml"
    f2 = tmp_path / "camp2.yaml"
    f1.write_text("tasks:\n  - prompt: Task A\n    tier: low\n", encoding="utf-8")
    f2.write_text("tasks:\n  - prompt: Task B\n    tier: low\n", encoding="utf-8")

    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    campaign_str = f"{f1},{f2}"
    w = Watcher(config=config, campaign_file=campaign_str, dry_run=True)

    asyncio.run(w.run())

    assert w._total_passed == 2  # Both tasks from both files


# ── Heartbeat ────────────────────────────────────────────────


def test_heartbeat_written_after_cycle(tmp_path: Path, campaign_file: Path):
    """Watcher writes heartbeat.json after each cycle."""
    import json
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    asyncio.run(w.run())

    hb_path = tmp_path / "data" / "heartbeat.json"
    assert hb_path.exists()
    data = json.loads(hb_path.read_text(encoding="utf-8"))
    # After run() completes, final heartbeat is "stopped"
    assert data["status"] == "stopped"
    assert data["cycle"] == 1
    assert data["total_passed"] == 2


def test_heartbeat_cmd_no_file(tmp_path: Path, capsys):
    """secretary heartbeat with no heartbeat file."""
    from secretary.__main__ import _cmd_heartbeat
    config = SecretaryConfig(data_root=str(tmp_path))
    _cmd_heartbeat(config)

    out = capsys.readouterr().out
    assert "No heartbeat found" in out


def test_heartbeat_cmd_with_file(tmp_path: Path, capsys):
    """secretary heartbeat reads and displays heartbeat."""
    import json
    from secretary.__main__ import _cmd_heartbeat

    hb = {
        "status": "running",
        "timestamp": "2026-03-12T10:00:00+00:00",
        "cycle": 5,
        "total_passed": 10,
        "total_failed": 1,
        "total_premium": 3.5,
        "uptime_seconds": 3665,
        "dry_run": False,
        "campaigns": ["email-triage.yaml"],
    }
    (tmp_path / "heartbeat.json").write_text(json.dumps(hb), encoding="utf-8")

    config = SecretaryConfig(data_root=str(tmp_path))
    _cmd_heartbeat(config)

    out = capsys.readouterr().out
    assert "running" in out
    assert "Cycles: 5" in out
    assert "10 passed" in out
    assert "email-triage.yaml" in out


# ── Task timeout ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_timeout_triggers(tmp_path: Path):
    """Task that exceeds timeout is killed and logged as failure."""
    f = tmp_path / "slow.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Slow task\n"
        "    tier: low\n"
        "    timeout: 1\n",  # 1 second timeout
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    config.watcher.max_retries = 0  # no retries, faster test
    w = Watcher(config=config, campaign_file=f, dry_run=False)

    async def slow_agent(*a, **kw):
        await asyncio.sleep(10)  # must exceed timeout (1s) to trigger cancellation

    with patch("secretary.watcher.direct_agent.run", side_effect=slow_agent):
        await w.run()

    assert w._total_task_failures == 1
    assert w._total_passed == 0

    # Check run log records the timeout
    entries = w.run_log.recent(5)
    assert len(entries) == 1
    assert "timed out" in entries[0].error


# ── Task priority ────────────────────────────────────────────


def test_task_priority_ordering(tmp_path: Path):
    """Tasks are executed in priority order (lower number first)."""
    f = tmp_path / "priority.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Low priority\n"
        "    tier: low\n"
        "    priority: 20\n"
        "  - prompt: High priority\n"
        "    tier: low\n"
        "    priority: 1\n"
        "  - prompt: Default priority\n"
        "    tier: low\n",  # default priority = 10
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    # Verify load order by checking _load_campaign directly
    tasks = w._load_campaign()
    assert tasks[0]["prompt"].strip() == "High priority"
    assert tasks[1]["prompt"].strip() == "Default priority"
    assert tasks[2]["prompt"].strip() == "Low priority"


# ── Tier escalation on retry ─────────────────────────────────


def test_tier_escalation_on_retry(tmp_path: Path):
    """When a task fails, retry escalates the tier (low → medium → high)."""
    f = tmp_path / "escalate.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Tricky task\n"
        "    tier: null\n",  # auto-route (will route low for short prompt)
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 2
    config.watcher.retry_base_delay = 0.01  # fast for testing
    w = Watcher(config=config, campaign_file=f, dry_run=False)

    call_tiers = []

    async def track_tier(*a, **kw):
        tier = kw.get("force_tier")
        call_tiers.append(tier)
        return _FakeResult(error="fail")

    with patch("secretary.watcher.direct_agent.run", side_effect=track_tier):
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    assert failed == 1
    # First call: None (auto-route), retries escalate
    assert call_tiers[0] is None
    # Subsequent retries should use escalated tiers
    assert len(call_tiers) == 3  # 1 initial + 2 retries


def test_tier_escalation_disabled_when_tier_forced(tmp_path: Path):
    """When tier is explicitly set, retries don't escalate."""
    pass  # Stub — escalation with forced tier not yet implemented


# ── KeyboardInterrupt saves state ────────────────────────────


def test_keyboard_interrupt_saves_dedup_history(tmp_path: Path, campaign_file: Path):
    """Dedup history must be saved even on Ctrl+C during a cycle."""
    import json

    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 5  # Would run 5 times if not interrupted
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    # Patch _run_cycle to succeed once, then raise KeyboardInterrupt
    call_count = 0
    original_run_cycle = w._run_cycle

    async def interrupt_on_second_call(memory):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return await original_run_cycle(memory)
        raise KeyboardInterrupt()

    with patch.object(w, "_run_cycle", side_effect=interrupt_on_second_call):
        asyncio.run(w.run())

    # Verify dedup history was saved despite KeyboardInterrupt
    dedup_path = tmp_path / "data" / "dedup_history.json"
    assert dedup_path.exists(), "Dedup history should be saved on KeyboardInterrupt"
    data = json.loads(dedup_path.read_text(encoding="utf-8"))
    assert len(data) >= 1  # At least the first cycle's tasks
    f = tmp_path / "forced.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Forced low task\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 1
    config.watcher.retry_base_delay = 0.01
    w = Watcher(config=config, campaign_file=f, dry_run=False)

    call_tiers = []

    async def track_tier(*a, **kw):
        call_tiers.append(kw.get("force_tier"))
        return _FakeResult(error="fail")

    with patch("secretary.watcher.direct_agent.run", side_effect=track_tier):
        asyncio.run(w._run_cycle(MagicMock()))

    # Both calls should use "low" (not escalated)
    assert call_tiers == ["low", "low"]


# ── Predictive prefetch ─────────────────────────────────────


def test_predictive_prefetch_email(tmp_path: Path):
    """Email keywords in prompt trigger gmail_search prefetch."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    mock_result = {"content": [{"text": "3 unread emails found: msg1, msg2, msg3"}]}
    mock_func = AsyncMock(return_value=mock_result)
    tools = {"gmail_search": {"func": mock_func}}

    result = asyncio.run(w._predictive_prefetch("Check my email inbox", tools))
    mock_func.assert_awaited_once()
    assert "PRE-FETCHED" in result
    assert "gmail_search" in result


def test_predictive_prefetch_calendar(tmp_path: Path):
    """Calendar keywords in prompt trigger calendar_today prefetch."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    mock_result = {"content": [{"text": "2 meetings today: standup at 9am, review at 2pm"}]}
    mock_func = AsyncMock(return_value=mock_result)
    tools = {"calendar_today": {"func": mock_func}}

    result = asyncio.run(w._predictive_prefetch("What meetings do I have today?", tools))
    mock_func.assert_awaited_once()
    assert "PRE-FETCHED" in result
    assert "calendar_today" in result


def test_predictive_prefetch_disabled(tmp_path: Path):
    """When predictive_prefetch=False, returns empty string."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.optimizations.predictive_prefetch = False
    w = Watcher(config=config)

    tools = {"gmail_search": {"func": AsyncMock()}}
    result = asyncio.run(w._predictive_prefetch("Check my email", tools))
    assert result == ""


def test_predictive_prefetch_no_match(tmp_path: Path):
    """Non-matching keywords return empty string."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    tools = {"gmail_search": {"func": AsyncMock()}}
    result = asyncio.run(w._predictive_prefetch("Tell me a joke", tools))
    assert result == ""


def test_predictive_prefetch_both(tmp_path: Path):
    """Task mentioning email AND calendar triggers both prefetches."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    email_result = {"content": [{"text": "2 unread emails about the meeting"}]}
    cal_result = {"content": [{"text": "Team meeting at 10am, lunch at noon"}]}
    tools = {
        "gmail_search": {"func": AsyncMock(return_value=email_result)},
        "calendar_today": {"func": AsyncMock(return_value=cal_result)},
    }

    result = asyncio.run(w._predictive_prefetch("Check email and calendar events", tools))
    assert "gmail_search" in result
    assert "calendar_today" in result


def test_predictive_prefetch_error_graceful(tmp_path: Path):
    """Prefetch errors are caught — returns empty string."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    tools = {"gmail_search": {"func": AsyncMock(side_effect=RuntimeError("token expired"))}}
    result = asyncio.run(w._predictive_prefetch("Check my email", tools))
    assert result == ""


# ── Tool memoization ─────────────────────────────────────────


def test_memo_set_and_get(tmp_path: Path):
    """Basic set then get returns cached result."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    key = w._memo_key("gmail_search", {"query": "is:unread"})
    w._memo_set(key, "3 emails found")
    assert w._memo_get(key) == "3 emails found"
    assert w._tool_memo_hits == 1


def test_memo_ttl_expiry(tmp_path: Path):
    """Expired entries return None."""
    import time as time_mod
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.optimizations.tool_memo_ttl_seconds = 1  # 1 second TTL
    w = Watcher(config=config)

    key = w._memo_key("calendar_today", {})
    # Manually insert with old timestamp
    w._tool_memo[key] = (time_mod.time() - 10, "old data")
    assert w._memo_get(key) is None  # expired


def test_memo_disabled(tmp_path: Path):
    """When tool_memoization=False, set/get are no-ops."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.optimizations.tool_memoization = False
    w = Watcher(config=config)

    key = w._memo_key("gmail_search", {"query": "test"})
    w._memo_set(key, "cached")
    assert w._memo_get(key) is None  # not stored


def test_memo_key_stable(tmp_path: Path):
    """Same tool+args produce same key regardless of dict order."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    key1 = w._memo_key("gmail_search", {"query": "is:unread", "max_results": 10})
    key2 = w._memo_key("gmail_search", {"max_results": 10, "query": "is:unread"})
    assert key1 == key2


def test_memo_cleared_per_cycle(tmp_path: Path, campaign_file: Path):
    """Memo cache is cleared at cycle start."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=campaign_file, dry_run=True)

    # Pre-populate cache
    w._tool_memo["test_key"] = (0, "stale")
    w._tool_memo_hits = 5
    w._tool_memo_misses = 3

    asyncio.run(w._run_cycle(MagicMock()))
    assert len(w._tool_memo) == 0  # cleared


def test_memo_different_args_different_keys(tmp_path: Path):
    """Different args produce different cache keys."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)

    key1 = w._memo_key("gmail_search", {"query": "is:unread"})
    key2 = w._memo_key("gmail_search", {"query": "from:boss"})
    assert key1 != key2


# ── Campaign error recovery ──────────────────────────────────


def test_campaign_missing_file_skipped(tmp_path: Path):
    """Missing campaign files are logged and skipped, not fatal."""
    good = tmp_path / "good.yaml"
    good.write_text("tasks:\n  - prompt: Task A\n    tier: low\n", encoding="utf-8")
    bad = tmp_path / "nonexistent.yaml"

    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=f"{good},{bad}", dry_run=True)

    asyncio.run(w.run())
    assert w._total_passed == 1  # Only the good campaign's task ran


def test_campaign_malformed_yaml_skipped(tmp_path: Path):
    """Malformed YAML in a campaign is skipped, not fatal."""
    good = tmp_path / "good.yaml"
    good.write_text("tasks:\n  - prompt: Task A\n    tier: low\n", encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text(": : : not valid yaml [[[", encoding="utf-8")

    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=f"{good},{bad}", dry_run=True)

    asyncio.run(w.run())
    assert w._total_passed == 1


def test_campaign_not_a_dict_skipped(tmp_path: Path):
    """Campaign file that parses as a list (not dict) is skipped."""
    good = tmp_path / "good.yaml"
    good.write_text("tasks:\n  - prompt: Task A\n    tier: low\n", encoding="utf-8")
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")

    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_runs = 1
    config.watcher.interval_minutes = 0
    w = Watcher(config=config, campaign_file=f"{good},{bad}", dry_run=True)

    asyncio.run(w.run())
    assert w._total_passed == 1


def test_escalate_on_retry_opt_out(tmp_path: Path):
    """escalate_on_retry: false disables tier escalation."""
    f = tmp_path / "no_escalate.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Stay low\n"
        "    escalate_on_retry: false\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 1
    config.watcher.retry_base_delay = 0.01
    w = Watcher(config=config, campaign_file=f, dry_run=False)

    call_tiers = []

    async def track_tier(*a, **kw):
        call_tiers.append(kw.get("force_tier"))
        return _FakeResult(error="fail")

    with patch("secretary.watcher.direct_agent.run", side_effect=track_tier):
        asyncio.run(w._run_cycle(MagicMock()))

    # Both calls should use None (auto-route, not escalated)
    assert all(t is None for t in call_tiers)


# ── Task dependencies ────────────────────────────────────────


def test_dependency_runs_when_parent_passes(tmp_path: Path):
    """Task with depends_on runs when the parent task succeeds."""
    f = tmp_path / "deps.yaml"
    f.write_text(
        "tasks:\n"
        "  - id: fetch\n"
        "    prompt: Fetch data\n"
        "    tier: low\n"
        "  - id: process\n"
        "    prompt: Process data\n"
        "    tier: low\n"
        "    depends_on: fetch\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    assert passed == 2
    assert failed == 0


def test_dependency_skips_when_parent_fails(tmp_path: Path):
    """Task with depends_on is skipped when the parent task fails."""
    f = tmp_path / "deps_fail.yaml"
    f.write_text(
        "tasks:\n"
        "  - id: fetch\n"
        "    prompt: Fetch data\n"
        "    tier: low\n"
        "  - id: process\n"
        "    prompt: Process data\n"
        "    tier: low\n"
        "    depends_on: fetch\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 0
    w = Watcher(config=config, campaign_file=f, dry_run=False)

    with patch("secretary.watcher.direct_agent.run", AsyncMock(return_value=_FakeResult(error="network error"))):
        passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    # fetch fails → process skipped (not counted as fail, just skipped)
    assert failed == 1
    assert passed == 0


def test_dependency_missing_id_skips(tmp_path: Path):
    """Task depending on non-existent ID is skipped."""
    f = tmp_path / "deps_missing.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Orphan task\n"
        "    tier: low\n"
        "    depends_on: nonexistent\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))

    assert passed == 0
    assert failed == 0  # skipped, not failed


# ── Failure notification ──────────────────────────────────────


def test_notify_failures_drafts_email(tmp_path: Path):
    """Failure notification creates a Gmail draft when notify_email is set."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.notify_email = "test@example.com"
    w = Watcher(config=config, campaign_file=tmp_path / "c.yaml", dry_run=True)
    w._runs_completed = 3
    w._total_premium_spent = 2.5

    mock_svc = MagicMock()
    with patch("secretary.mcp_tools.google_auth.build_gmail_service", return_value=mock_svc) as mock_build:
        w._notify_failures(2, 3)

    mock_svc.users().drafts().create.assert_called_once()
    call_body = mock_svc.users().drafts().create.call_args
    assert call_body is not None


def test_notify_failures_silent_on_error(tmp_path: Path):
    """Notification failure is logged but doesn't crash the watcher."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.notify_email = "test@example.com"
    w = Watcher(config=config, campaign_file=tmp_path / "c.yaml", dry_run=True)

    with patch("secretary.mcp_tools.google_auth.build_gmail_service", side_effect=Exception("no auth")):
        # Should not raise
        w._notify_failures(1, 0)


# ── tools_used tracking ──────────────────────────────────────


def test_run_log_entry_tools_used():
    """RunLogEntry supports tools_used field."""
    entry = RunLogEntry(
        timestamp="2026-01-01T00:00:00Z",
        cycle=1,
        task="test",
        tier="low",
        model="claude-haiku-4.5",
        success=True,
        output_preview="ok",
        tools_used=["mcp__gmail__gmail_search", "mcp__calendar__calendar_today"],
    )
    assert len(entry.tools_used) == 2
    assert "mcp__gmail__gmail_search" in entry.tools_used


# ── Quota exhaustion detection ────────────────────────────────


def test_is_quota_error_detects_rate_limit():
    from secretary.watcher import _is_quota_error
    assert _is_quota_error("429 Too Many Requests")
    assert _is_quota_error("Rate limit exceeded for this model")
    assert _is_quota_error("Insufficient quota remaining")
    assert _is_quota_error("Premium request quota exhausted")
    assert _is_quota_error("billing issue detected")


def test_is_quota_error_negative():
    from secretary.watcher import _is_quota_error
    assert not _is_quota_error("Connection reset by peer")
    assert not _is_quota_error("Timeout waiting for response")
    assert not _is_quota_error("Invalid JSON in response")
    assert not _is_quota_error("disk capacity exceeded")
    assert not _is_quota_error("database limit exceeded")


@pytest.mark.asyncio
async def test_quota_error_stops_cycle(tmp_path: Path):
    """When a quota error is detected, the cycle stops and _quota_exhausted is set."""
    f = tmp_path / "campaign.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Task one\n"
        "    tier: low\n"
        "  - prompt: Task two\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    config.watcher.max_retries = 0
    w = Watcher(config=config, campaign_file=f, dry_run=False)

    async def quota_fail(*a, **kw):
        raise RuntimeError("429 Too Many Requests: rate limit exceeded")

    with patch("secretary.watcher.direct_agent.run", side_effect=quota_fail):
        passed, failed, *_ = await w._run_cycle(MagicMock())

    assert w._quota_exhausted is True
    assert failed == 1
    # Second task should have been skipped
    assert passed == 0


# ── Currency in run log summary ───────────────────────────────


def test_run_log_summary_includes_cad(tmp_path: Path):
    from secretary.run_log import RunLog, RunLogEntry
    from secretary.currency import set_rate, get_rate

    original_rate = get_rate()
    try:
        set_rate(1.44)
        log = RunLog(tmp_path / "test.jsonl")
        log.append(RunLogEntry(
            timestamp="2026-01-01T00:00:00Z",
            cycle=1,
            task="test",
            tier="medium",
            model="claude-sonnet-4.6",
            success=True,
            output_preview="ok",
            cost_usd=1.0,
        ))
        summary = log.summary()
        assert "total_cost_cad" in summary
        assert abs(summary["total_cost_cad"] - 1.44) < 0.01
    finally:
        set_rate(original_rate)
