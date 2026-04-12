"""Tests for pipeline_health.py — the pipeline health logging system."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from secretary.pipeline_health import HealthEvent, HealthLog


@pytest.fixture
def health_log(tmp_path: Path) -> HealthLog:
    return HealthLog(path=tmp_path / "health.jsonl")


class TestHealthLog:
    def test_record_creates_file(self, health_log: HealthLog) -> None:
        health_log.record("analysis_failure", "error", "LLM call failed", source="test")
        assert health_log.path.exists()

    def test_record_writes_valid_jsonl(self, health_log: HealthLog) -> None:
        health_log.record("pipeline_error", "warning", "Git commit failed", source="test")
        line = health_log.path.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["category"] == "pipeline_error"
        assert data["severity"] == "warning"
        assert data["message"] == "Git commit failed"
        assert data["source"] == "test"

    def test_recent_returns_events(self, health_log: HealthLog) -> None:
        health_log.record("analysis_failure", "error", "msg1")
        health_log.record("scope_violation", "warning", "msg2")
        events = health_log.recent(10)
        assert len(events) == 2
        assert events[0].message == "msg1"
        assert events[1].message == "msg2"

    def test_recent_limits_count(self, health_log: HealthLog) -> None:
        for i in range(10):
            health_log.record("test", "info", f"msg{i}")
        events = health_log.recent(3)
        assert len(events) == 3
        # Should be the LAST 3
        assert events[0].message == "msg7"

    def test_recent_errors_filters_severity(self, health_log: HealthLog) -> None:
        health_log.record("test", "info", "info msg")
        health_log.record("test", "warning", "warn msg")
        health_log.record("test", "error", "error msg")
        errors = health_log.recent_errors(10, hours=1.0)
        assert len(errors) == 2
        assert errors[0].severity == "warning"
        assert errors[1].severity == "error"

    def test_message_truncation(self, health_log: HealthLog) -> None:
        long_msg = "x" * 500
        health_log.record("test", "info", long_msg)
        events = health_log.recent(1)
        assert len(events[0].message) == 300

    def test_details_truncation(self, health_log: HealthLog) -> None:
        long_details = "d" * 1000
        health_log.record("test", "info", "msg", details=long_details)
        events = health_log.recent(1)
        assert len(events[0].details) == 500

    def test_rotation_at_max_size(self, health_log: HealthLog) -> None:
        # Write enough to exceed 2MB
        health_log._MAX_BYTES = 1000  # Small for test
        for i in range(50):
            health_log.record("test", "info", "x" * 200)
        # Archive should exist
        archive = health_log.path.with_suffix(".jsonl.1")
        assert archive.exists()
        # Current file should be small
        assert health_log.path.stat().st_size < 1000

    def test_empty_file_returns_empty(self, health_log: HealthLog) -> None:
        assert health_log.recent() == []
        assert health_log.recent_errors() == []

    def test_record_with_all_fields(self, health_log: HealthLog) -> None:
        health_log.record(
            "quota_exhaustion", "error", "Out of quota",
            source="watcher", details="Used 100/100", cycle=42,
        )
        events = health_log.recent(1)
        assert events[0].category == "quota_exhaustion"
        assert events[0].cycle == 42
        assert events[0].details == "Used 100/100"
