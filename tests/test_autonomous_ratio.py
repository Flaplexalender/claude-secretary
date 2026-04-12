"""Tests for autonomous_ratio module."""
import json
import pytest
from pathlib import Path

from secretary.autonomous_ratio import (
    AUTONOMOUS_PREFIXES,
    AUTONOMOUS_SOURCES,
    _is_autonomous,
    autonomous_task_ratio,
    format_ratio_summary,
    format_detailed_summary,
)


def _write_entries(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a list of dicts as JSONL to a temp run_log file."""
    log_path = tmp_path / "run_log.jsonl"
    with open(log_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log_path


class TestIsAutonomous:
    def test_ooda_prefix(self):
        assert _is_autonomous({"task": "[ooda] Check email patterns"}) is True

    def test_goal_prefix(self):
        assert _is_autonomous({"task": "[goal] Advance test coverage"}) is True

    def test_event_prefix(self):
        assert _is_autonomous({"task": "[event] New email from boss"}) is True

    def test_reactive_prefix(self):
        assert _is_autonomous({"task": "[reactive] Budget alert triggered"}) is True

    def test_self_improve_prefix(self):
        assert _is_autonomous({"task": "[self-improve] Fix error handling"}) is True

    def test_static_campaign_task(self):
        assert _is_autonomous({"task": "Check my Gmail for unread messages"}) is False

    def test_source_ooda(self):
        assert _is_autonomous({"task": "Some task", "source": "ooda"}) is True

    def test_source_goals(self):
        assert _is_autonomous({"task": "Some task", "source": "goals"}) is True

    def test_source_campaign(self):
        assert _is_autonomous({"task": "Some task", "source": "campaign"}) is False

    def test_empty_task(self):
        assert _is_autonomous({"task": ""}) is False

    def test_case_insensitive_prefix(self):
        # Prefixes are checked case-insensitively (lowered)
        assert _is_autonomous({"task": "[OODA] uppercase"}) is True


class TestAutonomousTaskRatio:
    def test_empty_log(self, tmp_path):
        log_path = _write_entries(tmp_path, [])
        stats = autonomous_task_ratio(log_path)
        assert stats["total"] == 0
        assert stats["autonomous"] == 0
        assert stats["ratio"] == 0.0

    def test_nonexistent_file(self, tmp_path):
        stats = autonomous_task_ratio(tmp_path / "missing.jsonl")
        assert stats["total"] == 0
        assert stats["ratio"] == 0.0

    def test_all_static(self, tmp_path):
        entries = [
            {"task": "Check Gmail", "source": "campaign"},
            {"task": "Summarize calendar", "source": "campaign"},
        ]
        log_path = _write_entries(tmp_path, entries)
        stats = autonomous_task_ratio(log_path)
        assert stats["total"] == 2
        assert stats["autonomous"] == 0
        assert stats["static"] == 2
        assert stats["ratio"] == 0.0

    def test_all_autonomous(self, tmp_path):
        entries = [
            {"task": "[ooda] Detected spam pattern", "source": "ooda"},
            {"task": "[goal] Advance self-improvement", "source": "goals"},
        ]
        log_path = _write_entries(tmp_path, entries)
        stats = autonomous_task_ratio(log_path)
        assert stats["total"] == 2
        assert stats["autonomous"] == 2
        assert stats["static"] == 0
        assert stats["ratio"] == 1.0

    def test_mixed_tasks(self, tmp_path):
        entries = [
            {"task": "Check Gmail", "source": "campaign"},
            {"task": "[ooda] Pattern detected", "source": "ooda"},
            {"task": "Review drafts", "source": "campaign"},
            {"task": "[goal] Test coverage", "source": "goals"},
        ]
        log_path = _write_entries(tmp_path, entries)
        stats = autonomous_task_ratio(log_path)
        assert stats["total"] == 4
        assert stats["autonomous"] == 2
        assert stats["static"] == 2
        assert stats["ratio"] == 0.5

    def test_by_source_tracking(self, tmp_path):
        entries = [
            {"task": "Task A", "source": "campaign"},
            {"task": "Task B", "source": "campaign"},
            {"task": "[ooda] Task C", "source": "ooda"},
        ]
        log_path = _write_entries(tmp_path, entries)
        stats = autonomous_task_ratio(log_path)
        assert stats["by_source"]["campaign"] == 2
        assert stats["by_source"]["ooda"] == 1

    def test_corrupted_lines_skipped(self, tmp_path):
        log_path = tmp_path / "run_log.jsonl"
        with open(log_path, "w") as f:
            f.write('{"task": "valid", "source": "campaign"}\n')
            f.write("not valid json\n")
            f.write('{"task": "[ooda] also valid", "source": "ooda"}\n')
        stats = autonomous_task_ratio(log_path)
        assert stats["total"] == 2
        assert stats["autonomous"] == 1


class TestFormatRatioSummary:
    def test_no_tasks(self):
        stats = {"total": 0, "autonomous": 0, "ratio": 0.0}
        assert "no tasks recorded" in format_ratio_summary(stats)

    def test_with_tasks(self):
        stats = {"total": 100, "autonomous": 45, "ratio": 0.45}
        result = format_ratio_summary(stats)
        assert "45/100" in result
        assert "45.0%" in result

    def test_100_percent(self):
        stats = {"total": 10, "autonomous": 10, "ratio": 1.0}
        result = format_ratio_summary(stats)
        assert "100.0%" in result


class TestFormatDetailedSummary:
    def test_includes_source_breakdown(self):
        stats = {
            "total": 10,
            "autonomous": 3,
            "static": 7,
            "ratio": 0.3,
            "by_source": {"campaign": 7, "ooda": 2, "goals": 1},
        }
        result = format_detailed_summary(stats)
        assert "campaign" in result
        assert "ooda" in result
        assert "NOT MET" in result

    def test_goal_met(self):
        stats = {
            "total": 10,
            "autonomous": 6,
            "static": 4,
            "ratio": 0.6,
            "by_source": {"ooda": 6, "campaign": 4},
        }
        result = format_detailed_summary(stats)
        assert "MET" in result
        assert "NOT MET" not in result
