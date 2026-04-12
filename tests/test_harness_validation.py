"""Tests for harness validation loop (syntax_check, validate_harness, harness_validation_loop)."""

from __future__ import annotations

import ast
import json
import os
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.goal_harness import (
    CONSECUTIVE_PASSES_REQUIRED,
    MAX_GENERATION_ATTEMPTS,
    HarnessResult,
    ValidationResult,
    harness_validation_loop,
    syntax_check,
    validate_harness,
    _log_validation_failure,
)


# ── syntax_check tests ──────────────────────────────────────────

class TestSyntaxCheck:
    def test_valid_code(self):
        ok, err = syntax_check("def test_foo():\n    assert True\n")
        assert ok is True
        assert err is None

    def test_empty_code(self):
        ok, err = syntax_check("")
        assert ok is True
        assert err is None

    def test_invalid_syntax(self):
        ok, err = syntax_check("def test_foo(\n")
        assert ok is False
        assert err is not None
        assert "line" in err.lower() or "syntax" in err.lower()

    def test_indentation_error(self):
        ok, err = syntax_check("def test_foo():\nassert True\n")
        assert ok is False
        assert err is not None

    def test_complex_valid_code(self):
        code = textwrap.dedent("""\
            import os
            import pytest

            def test_file_exists():
                assert os.path.isfile("README.md")

            @pytest.mark.parametrize("x", [1, 2, 3])
            def test_values(x):
                assert x > 0
        """)
        ok, err = syntax_check(code)
        assert ok is True
        assert err is None


# ── ValidationResult tests ───────────────────────────────────────

class TestValidationResult:
    def test_all_pass(self):
        r = ValidationResult(
            test_code="...",
            syntax_ok=True,
            known_good=HarnessResult(test_code="", passed=True, output="ok", error=None, duration=0.1),
            known_bad=HarnessResult(test_code="", passed=False, output="FAILED", error="assert", duration=0.1),
        )
        assert r.passed is True

    def test_syntax_fail(self):
        r = ValidationResult(test_code="...", syntax_ok=False, syntax_error="bad")
        assert r.passed is False
        assert "syntax" in r.failure_reason

    def test_known_good_fails(self):
        r = ValidationResult(
            test_code="...",
            syntax_ok=True,
            known_good=HarnessResult(test_code="", passed=False, output="FAIL", error="oops", duration=0.1),
            known_bad=HarnessResult(test_code="", passed=False, output="FAIL", error="ok", duration=0.1),
        )
        assert r.passed is False
        assert "known-good" in r.failure_reason

    def test_known_bad_passes_trivial(self):
        """If test passes on empty dir, it's trivially true → should fail validation."""
        r = ValidationResult(
            test_code="...",
            syntax_ok=True,
            known_good=HarnessResult(test_code="", passed=True, output="ok", error=None, duration=0.1),
            known_bad=HarnessResult(test_code="", passed=True, output="ok", error=None, duration=0.1),
        )
        assert r.passed is False
        assert "trivially true" in r.failure_reason

    def test_known_good_none(self):
        r = ValidationResult(test_code="...", syntax_ok=True, known_good=None)
        assert r.passed is False


# ── validate_harness tests ───────────────────────────────────────

class TestValidateHarness:
    @pytest.mark.asyncio
    async def test_syntax_error_skips_execution(self):
        result = await validate_harness("def broken(", "/tmp")
        assert result.syntax_ok is False
        assert result.known_good is None
        assert result.known_bad is None
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_valid_passes_both_phases(self):
        good = HarnessResult(test_code="", passed=True, output="1 passed", error=None, duration=0.1)
        bad = HarnessResult(test_code="", passed=False, output="1 failed", error="assert", duration=0.1)
        with patch("secretary.goal_harness.run_harness_test", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [good, bad]
            result = await validate_harness("def test_x():\n    assert True\n", "/tmp/good")
        assert result.syntax_ok is True
        assert result.passed is True
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_trivially_true_test_fails(self):
        """Both known-good and known-bad pass → trivially true → validation fails."""
        always_pass = HarnessResult(test_code="", passed=True, output="1 passed", error=None, duration=0.1)
        with patch("secretary.goal_harness.run_harness_test", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = always_pass
            result = await validate_harness("def test_x():\n    assert True\n", "/tmp/good")
        assert result.passed is False


# ── _log_validation_failure tests ────────────────────────────────

class TestLogValidationFailure:
    def test_writes_jsonl(self, tmp_path):
        log_file = str(tmp_path / "log.jsonl")
        r = ValidationResult(test_code="code", syntax_ok=False, syntax_error="bad")
        _log_validation_failure("goal-1", 1, r, log_path=log_file)
        _log_validation_failure("goal-1", 2, r, log_path=log_file)
        lines = Path(log_file).read_text().strip().split("\n")
        assert len(lines) == 2
        entry = json.loads(lines[0])
        assert entry["goal_id"] == "goal-1"
        assert entry["attempt"] == 1
        assert entry["syntax_ok"] is False


# ── harness_validation_loop tests ────────────────────────────────

class TestHarnessValidationLoop:
    @pytest.mark.asyncio
    async def test_passes_after_3_consecutive(self):
        """Loop should return test code after 3 consecutive validation passes."""
        good_code = "def test_x():\n    assert True\n"
        good_validation = ValidationResult(
            test_code=good_code,
            syntax_ok=True,
            known_good=HarnessResult(test_code="", passed=True, output="ok", error=None, duration=0.1),
            known_bad=HarnessResult(test_code="", passed=False, output="FAIL", error="x", duration=0.1),
        )
        config = MagicMock()
        with (
            patch("secretary.goal_harness.generate_goal_test", new_callable=AsyncMock, return_value=good_code),
            patch("secretary.goal_harness.validate_harness", new_callable=AsyncMock, return_value=good_validation),
        ):
            best, results = await harness_validation_loop(
                goal_id="g1", goal_description="test", success_criteria="pass",
                config=config, known_good_dir="/tmp",
            )
        assert best == good_code
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_resets_on_failure(self):
        """Consecutive counter resets to 0 after a failed validation."""
        good_code = "def test_x():\n    assert True\n"
        good_vr = ValidationResult(
            test_code=good_code, syntax_ok=True,
            known_good=HarnessResult(test_code="", passed=True, output="ok", error=None, duration=0.1),
            known_bad=HarnessResult(test_code="", passed=False, output="FAIL", error="x", duration=0.1),
        )
        bad_vr = ValidationResult(test_code="bad", syntax_ok=False, syntax_error="err")
        config = MagicMock()
        # Pattern: pass, pass, FAIL, pass, pass, pass → completes at attempt 6
        with (
            patch("secretary.goal_harness.generate_goal_test", new_callable=AsyncMock, return_value=good_code),
            patch("secretary.goal_harness.validate_harness", new_callable=AsyncMock,
                  side_effect=[good_vr, good_vr, bad_vr, good_vr, good_vr, good_vr]),
            patch("secretary.goal_harness._log_validation_failure"),
        ):
            best, results = await harness_validation_loop(
                goal_id="g2", goal_description="test", success_criteria="pass",
                config=config, known_good_dir="/tmp",
            )
        assert best == good_code
        assert len(results) == 6  # 2 pass + 1 fail + 3 pass

    @pytest.mark.asyncio
    async def test_exhausts_max_attempts(self):
        """Returns None when all attempts fail."""
        bad_vr = ValidationResult(test_code="x", syntax_ok=False, syntax_error="err")
        config = MagicMock()
        with (
            patch("secretary.goal_harness.generate_goal_test", new_callable=AsyncMock, return_value="x"),
            patch("secretary.goal_harness.validate_harness", new_callable=AsyncMock, return_value=bad_vr),
            patch("secretary.goal_harness._log_validation_failure"),
        ):
            best, results = await harness_validation_loop(
                goal_id="g3", goal_description="test", success_criteria="pass",
                config=config, known_good_dir="/tmp", max_attempts=5,
            )
        assert best is None
        assert len(results) == 5

    @pytest.mark.asyncio
    async def test_generation_error_resets_consecutive(self):
        """If generate_goal_test raises, consecutive resets and loop continues."""
        good_code = "def test_x():\n    assert True\n"
        good_vr = ValidationResult(
            test_code=good_code, syntax_ok=True,
            known_good=HarnessResult(test_code="", passed=True, output="ok", error=None, duration=0.1),
            known_bad=HarnessResult(test_code="", passed=False, output="FAIL", error="x", duration=0.1),
        )
        config = MagicMock()
        call_count = 0

        async def gen_side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ValueError("LLM failed")
            return good_code

        with (
            patch("secretary.goal_harness.generate_goal_test", new_callable=AsyncMock, side_effect=gen_side_effect),
            patch("secretary.goal_harness.validate_harness", new_callable=AsyncMock, return_value=good_vr),
            patch("secretary.goal_harness._log_validation_failure"),
        ):
            best, results = await harness_validation_loop(
                goal_id="g4", goal_description="test", success_criteria="pass",
                config=config, known_good_dir="/tmp", max_attempts=8,
            )
        assert best == good_code
        # attempt 1: pass (consec=1), attempt 2: gen error (consec=0),
        # attempts 3,4,5: pass (consec=3) → done at attempt 5
        assert len(results) == 5
