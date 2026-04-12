"""Tests for self_improve._log_to_run_log — extracted logging helper.

Verifies that:
- Successful results are logged correctly
- Failed results include error info
- Logging failures don't crash (but DO log at ERROR level)
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from secretary.config import SecretaryConfig
from secretary.self_improve import _log_to_run_log, ImprovementResult


@pytest.fixture
def config(tmp_path: Path) -> SecretaryConfig:
    """Config with tmp_path data directory."""
    return SecretaryConfig(data_root=str(tmp_path))


@pytest.fixture
def success_result() -> ImprovementResult:
    """A successful improvement result."""
    return ImprovementResult(
        task="add tests",
        sandbox_dir="/tmp/sandbox",
        tests_passed=True,
        promoted=True,
        changed_files=["MOD: src/foo.py", "NEW: tests/test_foo.py"],
        cost_usd=0.05,
        num_turns=3,
    )


@pytest.fixture
def failed_result() -> ImprovementResult:
    """A failed improvement result."""
    return ImprovementResult(
        task="break things",
        sandbox_dir="/tmp/sandbox",
        tests_passed=False,
        error="Tests failed",
        changed_files=["MOD: src/bar.py"],
        cost_usd=0.02,
        num_turns=1,
    )


class TestLogToRunLog:
    def test_logs_successful_result(self, config: SecretaryConfig, success_result: ImprovementResult, tmp_path: Path):
        """Successful improvement is logged with success=True."""
        _log_to_run_log(config, "add tests", success_result)

        from secretary.run_log import RunLog
        log = RunLog(tmp_path / "run_log.jsonl")
        entries = log.recent(10)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.success is True
        assert "[self-improve]" in entry.task
        assert "add tests" in entry.task
        assert entry.tier == "high"
        assert "PASS" in entry.output_preview
        assert "promoted=True" in entry.output_preview

    def test_logs_failed_result(self, config: SecretaryConfig, failed_result: ImprovementResult, tmp_path: Path):
        """Failed improvement is logged with success=False and error."""
        _log_to_run_log(config, "break things", failed_result)

        from secretary.run_log import RunLog
        log = RunLog(tmp_path / "run_log.jsonl")
        entries = log.recent(10)
        assert len(entries) == 1
        entry = entries[0]
        assert entry.success is False
        assert entry.error == "Tests failed"
        assert "FAIL" in entry.output_preview

    def test_logs_cost_and_turns(self, config: SecretaryConfig, success_result: ImprovementResult, tmp_path: Path):
        """Cost and turn count are recorded."""
        _log_to_run_log(config, "add tests", success_result)

        from secretary.run_log import RunLog
        log = RunLog(tmp_path / "run_log.jsonl")
        entries = log.recent(10)
        assert entries[0].cost_usd == 0.05
        assert entries[0].num_turns == 3

    def test_logs_changed_files_in_preview(self, config: SecretaryConfig, success_result: ImprovementResult, tmp_path: Path):
        """Changed files appear in output_preview."""
        _log_to_run_log(config, "add tests", success_result)

        from secretary.run_log import RunLog
        log = RunLog(tmp_path / "run_log.jsonl")
        entries = log.recent(10)
        assert "changes=2" in entries[0].output_preview

    def test_logging_failure_does_not_crash(self, config: SecretaryConfig, success_result: ImprovementResult, caplog):
        """If RunLog construction fails, it logs ERROR but doesn't raise."""
        # Patch at the source module (lazy import `from .run_log import RunLog`
        # rebinds the name, so we patch the actual class in run_log module)
        with patch("secretary.run_log.RunLog", side_effect=RuntimeError("disk full")):
            with caplog.at_level(logging.ERROR, logger="secretary.self_improve"):
                _log_to_run_log(config, "add tests", success_result)

        # Verify it logged the error at ERROR level (not WARNING)
        assert any("Failed to log self-improvement result" in r.message for r in caplog.records)
        assert any(r.levelno == logging.ERROR for r in caplog.records
                   if "Failed to log" in r.message)

    def test_truncates_long_task(self, config: SecretaryConfig, tmp_path: Path):
        """Task description longer than 180 chars is truncated in the log."""
        long_task = "x" * 300
        result = ImprovementResult(
            task=long_task,
            sandbox_dir="/tmp/sb",
            tests_passed=True,
        )
        _log_to_run_log(config, long_task, result)

        from secretary.run_log import RunLog
        log = RunLog(tmp_path / "run_log.jsonl")
        entries = log.recent(10)
        # "[self-improve] " is 16 chars, task truncated to 180
        assert len(entries[0].task) <= 16 + 180
