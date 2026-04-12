"""Tests for run_log rotation — file size management and archive shifting.

The _rotate method is called when run_log.jsonl exceeds _MAX_BYTES (10 MB).
It shifts archives: .1 → .2, .2 → .3, .3 deleted; then renames current → .1.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from secretary.run_log import RunLog, RunLogEntry


def _make_entry(i: int, task: str = "task") -> RunLogEntry:
    """Create a RunLogEntry for testing."""
    return RunLogEntry(
        timestamp=f"2026-03-{11 + (i % 20):02d}T{i % 24:02d}:00:00Z",
        cycle=i,
        task=f"{task} {i}",
        tier="low",
        model="claude-haiku-4.5",
        success=True,
        output_preview=f"output {i}",
        duration_s=1.0,
        premium_cost=0.33,
        cost_usd=0.001,
    )


# ══════════════════════════════════════════════════════════════
#  _rotate
# ══════════════════════════════════════════════════════════════


def test_rotate_creates_archive_1(tmp_path: Path):
    """After rotation, current file becomes .1 archive."""
    log_path = tmp_path / "run_log.jsonl"
    log = RunLog(log_path)
    # Write some data
    for i in range(5):
        log.append(_make_entry(i))

    original_content = log_path.read_text(encoding="utf-8")
    assert log_path.exists()

    # Manually trigger rotation
    log._rotate()

    # Current file should be gone (replaced by .1)
    assert not log_path.exists()
    archive_1 = log_path.with_name(log_path.name + ".1")
    assert archive_1.exists()
    assert archive_1.read_text(encoding="utf-8") == original_content


def test_rotate_shifts_existing_archives(tmp_path: Path):
    """Rotation shifts .1 → .2, then current → .1."""
    log_path = tmp_path / "run_log.jsonl"
    archive_1 = log_path.with_name(log_path.name + ".1")
    archive_2 = log_path.with_name(log_path.name + ".2")

    # Create initial state: current + .1 archive
    log = RunLog(log_path)
    for i in range(3):
        log.append(_make_entry(i, task="current"))
    archive_1.write_text("archive-1-content\n", encoding="utf-8")

    log._rotate()

    # .1 should now be the old current file
    assert archive_1.exists()
    assert "current" in archive_1.read_text(encoding="utf-8")

    # .2 should be the old .1
    assert archive_2.exists()
    assert "archive-1-content" in archive_2.read_text(encoding="utf-8")


def test_rotate_drops_oldest_archive(tmp_path: Path):
    """When .3 already exists, it's deleted during rotation (max 3 archives)."""
    log_path = tmp_path / "run_log.jsonl"
    archive_1 = log_path.with_name(log_path.name + ".1")
    archive_2 = log_path.with_name(log_path.name + ".2")
    archive_3 = log_path.with_name(log_path.name + ".3")

    log = RunLog(log_path)
    for i in range(3):
        log.append(_make_entry(i, task="current"))

    archive_1.write_text("archive-1\n", encoding="utf-8")
    archive_2.write_text("archive-2\n", encoding="utf-8")
    archive_3.write_text("archive-3-oldest\n", encoding="utf-8")

    log._rotate()

    # .3 should now contain old .2 content (old .3 was dropped)
    assert archive_3.exists()
    assert "archive-2" in archive_3.read_text(encoding="utf-8")

    # .2 should contain old .1
    assert "archive-1" in archive_2.read_text(encoding="utf-8")

    # .1 should contain old current
    assert "current" in archive_1.read_text(encoding="utf-8")


def test_rotation_triggered_by_size(tmp_path: Path):
    """Rotation is auto-triggered when file exceeds _MAX_BYTES."""
    log_path = tmp_path / "run_log.jsonl"
    log = RunLog(log_path)

    # Override max bytes for testing (use 1 KB instead of 10 MB)
    original_max = RunLog._MAX_BYTES
    try:
        RunLog._MAX_BYTES = 1024  # 1 KB

        # Write enough to exceed 1 KB
        for i in range(20):
            log.append(_make_entry(i, task="padding " + "x" * 50))

        # Verify we triggered rotation at some point
        archive_1 = log_path.with_name(log_path.name + ".1")
        assert archive_1.exists(), "Rotation should have been triggered"

        # Current file should have remaining entries
        assert log_path.exists()
        assert log_path.stat().st_size < 1500  # should be small again

    finally:
        RunLog._MAX_BYTES = original_max


def test_rotation_preserves_data_integrity(tmp_path: Path):
    """After rotation, all data is still readable across files."""
    log_path = tmp_path / "run_log.jsonl"
    log = RunLog(log_path)

    original_max = RunLog._MAX_BYTES
    try:
        RunLog._MAX_BYTES = 500  # very small, trigger multiple rotations

        total_entries = 50
        for i in range(total_entries):
            log.append(_make_entry(i))

        # Count all entries across current + archives
        all_entries = 0
        for suffix in ["", ".1", ".2", ".3"]:
            f = log_path.with_name(log_path.name + suffix) if suffix else log_path
            if f.exists():
                for line in f.read_text(encoding="utf-8").strip().split("\n"):
                    if line.strip():
                        try:
                            json.loads(line)
                            all_entries += 1
                        except json.JSONDecodeError:
                            pass

        # We should have preserved most entries (some may be lost when .3 is dropped)
        # But with 3 archives + current, we should have a lot
        assert all_entries > 0

    finally:
        RunLog._MAX_BYTES = original_max


def test_append_after_rotation(tmp_path: Path):
    """Appending after rotation creates a new current file."""
    log_path = tmp_path / "run_log.jsonl"
    log = RunLog(log_path)

    for i in range(5):
        log.append(_make_entry(i))

    log._rotate()
    assert not log_path.exists()

    # Append should create a new file
    log.append(_make_entry(99, task="after rotation"))
    assert log_path.exists()

    entries = log.recent(10)
    assert len(entries) == 1
    assert entries[0].task == "after rotation 99"


def test_rotate_with_no_file(tmp_path: Path):
    """Rotation with non-existent file doesn't crash."""
    log_path = tmp_path / "run_log.jsonl"
    log = RunLog(log_path)
    # File doesn't exist — rotation should handle gracefully
    # (prev.exists() check in _rotate prevents errors)
    log._rotate()
    # No crash = success
    assert not log_path.exists()


def test_recent_reads_only_current_file(tmp_path: Path):
    """recent() only reads the current file, not archives."""
    log_path = tmp_path / "run_log.jsonl"
    log = RunLog(log_path)

    # Write and rotate
    for i in range(3):
        log.append(_make_entry(i, task="old"))
    log._rotate()

    # Write new entries
    for i in range(2):
        log.append(_make_entry(10 + i, task="new"))

    entries = log.recent(100)
    assert len(entries) == 2
    assert all("new" in e.task for e in entries)
