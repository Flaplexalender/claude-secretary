"""Advanced watcher tests — dedup pruning, heartbeat states, edge cases."""
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.config import SecretaryConfig, OptimizationConfig
from secretary.watcher import Watcher, _check_schedule, _is_quota_error


# ---------------------------------------------------------------------------
# _save_dedup_history pruning
# ---------------------------------------------------------------------------

@dataclass
class _FakeRouting:
    tier: str = "low"
    model: str = "claude-haiku-4.5"
    max_turns: int = 10
    max_budget_usd: float = 0.0
    reason: str = "test"


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
            self.routing = _FakeRouting()


def test_dedup_pruning_removes_stale_entries(tmp_path: Path):
    """_save_dedup_history prunes entries older than 2 cycles."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=tmp_path / "c.yaml", dry_run=True)

    # Simulate history from cycles 1-5
    w._success_history = {
        "hash_cycle1": 1,
        "hash_cycle2": 2,
        "hash_cycle3": 3,
        "hash_cycle4": 4,
        "hash_cycle5": 5,
    }
    w._runs_completed = 5

    w._save_dedup_history()

    # Cutoff = 5 - 2 = 3, so cycles < 3 are pruned
    data = json.loads(w._dedup_path.read_text(encoding="utf-8"))
    assert "hash_cycle1" not in data
    assert "hash_cycle2" not in data
    assert "hash_cycle3" in data
    assert "hash_cycle4" in data
    assert "hash_cycle5" in data


def test_dedup_history_atomic_write(tmp_path: Path):
    """_save_dedup_history uses atomic write (no corruption on crash)."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=tmp_path / "c.yaml", dry_run=True)
    w._success_history = {"test_hash": 1}
    w._runs_completed = 1

    w._save_dedup_history()

    # File should be valid JSON
    data = json.loads(w._dedup_path.read_text(encoding="utf-8"))
    assert data == {"test_hash": 1}

    # No temp files should remain
    tmp_files = list(w._dedup_path.parent.glob(".dedup_*.tmp"))
    assert len(tmp_files) == 0


def test_dedup_load_corrupted_file(tmp_path: Path):
    """Corrupted dedup file should be handled gracefully."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "dedup_history.json").write_text("{broken json", encoding="utf-8")

    config = SecretaryConfig(data_root=str(data_dir))
    w = Watcher(config=config, campaign_file=tmp_path / "c.yaml", dry_run=True)
    # Should load as empty, not crash
    assert w._success_history == {}


# ---------------------------------------------------------------------------
# Heartbeat states
# ---------------------------------------------------------------------------

def test_heartbeat_running_state(tmp_path: Path):
    """_write_heartbeat writes 'running' status with correct fields."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=tmp_path / "c.yaml", dry_run=True)
    w._start_time = 1000.0
    w._runs_completed = 3
    w._total_passed = 10
    w._total_task_failures = 2
    w._total_premium_spent = 5.5
    w._total_cost_usd = 0.15

    with patch("time.monotonic", return_value=1060.0):
        w._write_heartbeat(4, 1)

    hb_path = tmp_path / "data" / "heartbeat.json"
    data = json.loads(hb_path.read_text(encoding="utf-8"))
    assert data["status"] == "running"
    assert data["cycle"] == 3
    assert data["last_cycle_passed"] == 4
    assert data["last_cycle_failed"] == 1
    assert data["total_passed"] == 10
    assert data["total_failed"] == 2
    assert data["total_premium"] == 5.5
    assert data["uptime_seconds"] == 60
    assert data["dry_run"] is True


def test_heartbeat_stopped_state(tmp_path: Path):
    """_write_stopped_heartbeat writes 'stopped' status."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=tmp_path / "c.yaml")
    w._start_time = 1000.0
    w._runs_completed = 5
    w._total_passed = 15
    w._total_task_failures = 3
    w._total_premium_spent = 8.0

    with patch("time.monotonic", return_value=2000.0):
        w._write_stopped_heartbeat()

    hb_path = tmp_path / "data" / "heartbeat.json"
    data = json.loads(hb_path.read_text(encoding="utf-8"))
    assert data["status"] == "stopped"
    assert data["cycle"] == 5
    assert data["uptime_seconds"] == 1000


# ---------------------------------------------------------------------------
# _check_schedule — more edge cases
# ---------------------------------------------------------------------------

def test_schedule_hour_exact_boundaries_overnight():
    """Test exact boundary conditions for overnight range."""
    # hours:22-6 should include 22 but exclude 6
    assert _check_schedule("hours:22-6", datetime(2026, 1, 1, 22, 0)) is True
    assert _check_schedule("hours:22-6", datetime(2026, 1, 1, 21, 59)) is False
    assert _check_schedule("hours:22-6", datetime(2026, 1, 1, 5, 59)) is True
    assert _check_schedule("hours:22-6", datetime(2026, 1, 1, 6, 0)) is False


def test_schedule_single_hour_range():
    """hours:10-11 should only match hour 10."""
    assert _check_schedule("hours:10-11", datetime(2026, 1, 1, 10, 0)) is True
    assert _check_schedule("hours:10-11", datetime(2026, 1, 1, 10, 59)) is True
    assert _check_schedule("hours:10-11", datetime(2026, 1, 1, 11, 0)) is False
    assert _check_schedule("hours:10-11", datetime(2026, 1, 1, 9, 0)) is False


def test_schedule_midnight_range():
    """hours:0-1 should only match hour 0."""
    assert _check_schedule("hours:0-1", datetime(2026, 1, 1, 0, 30)) is True
    assert _check_schedule("hours:0-1", datetime(2026, 1, 1, 1, 0)) is False


def test_schedule_same_hour_range():
    """hours:10-10 (same start/end) — normal range → no hour matches."""
    # When start == end, start <= hour < end is never true
    assert _check_schedule("hours:10-10", datetime(2026, 1, 1, 10, 0)) is False


def test_schedule_combined_weekends_and_hours():
    """Combined weekend + hours schedule."""
    sat_morning = datetime(2026, 3, 14, 9, 0)  # Saturday
    sat_evening = datetime(2026, 3, 14, 20, 0)  # Saturday
    mon_morning = datetime(2026, 3, 9, 9, 0)  # Monday

    assert _check_schedule("weekends;hours:8-18", sat_morning) is True
    assert _check_schedule("weekends;hours:8-18", sat_evening) is False
    assert _check_schedule("weekends;hours:8-18", mon_morning) is False


# ---------------------------------------------------------------------------
# _is_quota_error edge cases
# ---------------------------------------------------------------------------

def test_quota_error_case_insensitive():
    """Quota detection should be case-insensitive."""
    assert _is_quota_error("QUOTA EXHAUSTED")
    assert _is_quota_error("Rate Limit Exceeded")
    assert _is_quota_error("INSUFFICIENT_QUOTA")


def test_quota_error_empty_string():
    """Empty error string should not be a quota error."""
    assert not _is_quota_error("")


def test_quota_error_model_capacity():
    """Model capacity errors should be detected as quota."""
    assert _is_quota_error("Model capacity exceeded, please wait")


# ---------------------------------------------------------------------------
# Watcher construction edge cases
# ---------------------------------------------------------------------------

def test_watcher_multi_campaign_parsing(tmp_path: Path):
    """Comma-separated campaign files are parsed correctly."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file="a.yaml,b.yaml, c.yaml")
    assert len(w.campaign_files) == 3
    assert w.campaign_files[0] == Path("a.yaml")
    assert w.campaign_files[2] == Path("c.yaml")
    assert w.campaign_file == Path("a.yaml")  # primary


def test_watcher_default_consolidate_every(tmp_path: Path):
    """Default consolidation period is every 3 cycles."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    assert w._consolidate_every == 3


def test_watcher_initial_state(tmp_path: Path):
    """Watcher starts with all counters at zero."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    assert w._runs_completed == 0
    assert w._runs_failed == 0
    assert w._total_passed == 0
    assert w._total_task_failures == 0
    assert w._total_premium_spent == 0.0
    assert w._total_cost_usd == 0.0
    assert w._stop is False
    assert w._quota_exhausted is False


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------

def test_format_duration_zero():
    assert Watcher._format_duration(0) == "0s"


def test_format_duration_large():
    """Multiple hours format correctly."""
    assert Watcher._format_duration(7200) == "2h 0m"
    assert Watcher._format_duration(7265) == "2h 1m"


# ---------------------------------------------------------------------------
# Campaign loading edge cases
# ---------------------------------------------------------------------------

def test_campaign_empty_tasks_key(tmp_path: Path):
    """Campaign with 'tasks: []' should produce 0 tasks."""
    f = tmp_path / "empty_tasks.yaml"
    f.write_text("tasks: []\n", encoding="utf-8")
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)
    tasks = w._load_campaign()
    assert tasks == []


def test_campaign_no_tasks_key(tmp_path: Path):
    """Campaign YAML without 'tasks' key should produce 0 tasks (with warning)."""
    f = tmp_path / "no_tasks.yaml"
    f.write_text("name: my campaign\n", encoding="utf-8")
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)
    tasks = w._load_campaign()
    assert tasks == []


def test_campaign_task_without_prompt_skipped(tmp_path: Path):
    """Task with no 'prompt' or 'task' key should be skipped."""
    f = tmp_path / "no_prompt.yaml"
    f.write_text(
        "tasks:\n"
        "  - tier: low\n"
        "    id: orphan\n"
        "  - prompt: Valid task\n"
        "    tier: low\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))
    # Only the valid task should run
    assert passed == 1


# ---------------------------------------------------------------------------
# Dependency with list form
# ---------------------------------------------------------------------------

def test_dependency_list_form(tmp_path: Path):
    """depends_on as a list should use the first element."""
    f = tmp_path / "deps_list.yaml"
    f.write_text(
        "tasks:\n"
        "  - id: parent\n"
        "    prompt: Parent task\n"
        "    tier: low\n"
        "  - id: child\n"
        "    prompt: Child task\n"
        "    tier: low\n"
        "    depends_on:\n"
        "      - parent\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))
    assert passed == 2


def test_dependency_empty_list(tmp_path: Path):
    """depends_on as empty list should not block the task."""
    f = tmp_path / "deps_empty.yaml"
    f.write_text(
        "tasks:\n"
        "  - prompt: Independent\n"
        "    tier: low\n"
        "    depends_on: []\n",
        encoding="utf-8",
    )
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config, campaign_file=f, dry_run=True)

    passed, failed, *_ = asyncio.run(w._run_cycle(MagicMock()))
    assert passed == 1


# ---------------------------------------------------------------------------
# _request_stop
# ---------------------------------------------------------------------------

def test_request_stop_sets_flag(tmp_path: Path):
    """_request_stop should set _stop = True."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    assert w._stop is False
    w._request_stop()
    assert w._stop is True


# ---------------------------------------------------------------------------
# File cache
# ---------------------------------------------------------------------------

def test_cached_file_read_returns_content(tmp_path: Path):
    """_cached_file_read should return file content."""
    f = tmp_path / "test.txt"
    f.write_text("hello world", encoding="utf-8")
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    assert w._cached_file_read(f) == "hello world"
    assert w._file_cache_misses == 1


def test_cached_file_read_hits_cache(tmp_path: Path):
    """Second read of same file should be a cache hit."""
    f = tmp_path / "test.txt"
    f.write_text("cached", encoding="utf-8")
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    w._cached_file_read(f)
    w._cached_file_read(f)
    assert w._file_cache_hits == 1
    assert w._file_cache_misses == 1


def test_cached_file_read_invalidates_on_mtime(tmp_path: Path):
    """Cache should invalidate when file mtime changes."""
    import time as _time
    f = tmp_path / "test.txt"
    f.write_text("v1", encoding="utf-8")
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    assert w._cached_file_read(f) == "v1"
    _time.sleep(0.05)  # ensure mtime changes
    f.write_text("v2", encoding="utf-8")
    assert w._cached_file_read(f) == "v2"
    assert w._file_cache_misses == 2


def test_cached_file_read_missing_file(tmp_path: Path):
    """_cached_file_read should return None for missing files."""
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    assert w._cached_file_read(tmp_path / "nope.txt") is None


def test_cached_file_read_max_chars(tmp_path: Path):
    """_cached_file_read should truncate to max_chars."""
    f = tmp_path / "long.txt"
    f.write_text("A" * 1000, encoding="utf-8")
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=config)
    result = w._cached_file_read(f, max_chars=100)
    assert len(result) == 100


def test_cached_file_read_disabled(tmp_path: Path):
    """When file_cache=False, should read directly without caching."""
    f = tmp_path / "test.txt"
    f.write_text("direct", encoding="utf-8")
    config = SecretaryConfig(
        data_root=str(tmp_path / "data"),
        optimizations=OptimizationConfig(file_cache=False),
    )
    w = Watcher(config=config)
    assert w._cached_file_read(f) == "direct"
    assert w._file_cache_hits == 0
    assert w._file_cache_misses == 0
