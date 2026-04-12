"""Layer 29: Self-Generated Test Harness — Secretary builds its own evaluation.

The secretary generates pytest test code from goal descriptions and success
criteria, runs the tests in a sandboxed subprocess, and returns structured
results for injection into the verification judge.

Research basis:
- Eureka (Ma 2023, ICLR 2024): LLM writes evaluation/reward code via
  evolutionary optimization.  Zero-shot code generation for eval criteria.
- CodeAct (Wang 2024, ICML 2024): Executable Python code as agent actions
  beats JSON/text.  Agents can "autonomously self-debug."
- MAST (Cemri 2025): Task verification is a top failure category —
  structured, executable tests improve reliability.
- Kwa (NeurIPS 2025): "Greater reliability and ability to adapt to mistakes"
  is the primary driver of long-horizon capability.

Architecture:
    goal description + success criteria
        → Haiku generates pytest test function (read-only, max 50 lines)
        → subprocess runs pytest in project dir with timeout
        → structured result: passed/failed + captured output
        → formatted text injected into verification judge alongside assertions

Security:
    - Generated code runs in a subprocess with timeout (default 30s)
    - PYTHONPATH set to project src/ so imports work
    - Tests SHOULD be read-only; mutations are the user's risk to accept
    - Output truncated to prevent context blowup
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import SecretaryConfig

log = logging.getLogger("secretary.goal_harness")

HARNESS_MODEL = "claude-haiku-4.5"
HARNESS_MAX_TOKENS = 1024
HARNESS_TEST_TIMEOUT = 30  # seconds
MAX_TEST_LINES = 50
MAX_OUTPUT_CHARS = 1500

# Validation loop parameters
CONSECUTIVE_PASSES_REQUIRED = 3  # Harness-generation is complete after N consecutive valid harnesses
MAX_GENERATION_ATTEMPTS = 10     # Give up after this many total attempts
VALIDATION_LOG_FILE = "data/harness_validation_log.jsonl"  # Structured failure log

_GENERATE_SYSTEM = """\
You are a test engineer for an AI secretary system.  Given a goal description \
and success criteria, generate a SHORT pytest test function that checks whether \
the criteria are met by inspecting the real filesystem and project state.

Rules:
1. Output ONLY valid Python code — no markdown fences, no explanation.
2. The test function must be named test_goal_<something>.
3. Use only stdlib + pytest + json + os + pathlib.  No pip installs.
4. Tests MUST be read-only: no file writes, no network, no subprocess calls.
5. Maximum 50 lines total (imports + function).
6. Use plain assert statements with descriptive messages.
7. If the success criteria mention a file, check it exists and has expected content.
8. If the criteria mention a numeric threshold, parse and compare.
9. If the criteria are too vague to test concretely, write a test that \
checks the most testable aspect and skips the rest with pytest.skip().
10. The working directory will be the project root.  Use relative paths.
11. CRITICAL: Your test MUST FAIL when run in an empty directory with no \
project files.  Every test must check real filesystem artifacts \
(files, directories, data) that only exist in a working project.  \
Tests that pass everywhere are useless and will be rejected.

REJECTED patterns (trivially true — pass in empty directories):
- assert True
- assert 1 == 1
- assert isinstance("x", str)
- data = {"score": 0.9}; assert data["score"] >= 0.8  (synthetic data)

REQUIRED pattern — always assert on real project state first:
- assert os.path.isfile("some/real/file"), "artifact missing"
- assert os.path.isdir("src/secretary"), "project structure missing"

Example output:
import os
import json

def test_goal_router_accuracy():
    assert os.path.isfile("data/router_analysis.json"), "Router analysis file missing"
    with open("data/router_analysis.json") as f:
        data = json.load(f)
    assert "accuracy" in data, "No accuracy field"
    assert data["accuracy"] >= 0.8, f"Accuracy {data['accuracy']} < 0.8"
"""


@dataclass
class HarnessResult:
    """Result of running a generated harness test."""

    test_code: str
    passed: bool
    output: str  # captured stdout/stderr, truncated
    error: str | None = None
    duration: float = 0.0


def _build_generate_prompt(
    goal_description: str,
    success_criteria: str,
    context_hints: list[str] | None = None,
    previous_failure: str | None = None,
) -> str:
    """Build the prompt for test generation."""
    parts = [
        f"## Goal\n{goal_description}",
        f"\n## Success Criteria\n{success_criteria}",
    ]
    if context_hints:
        parts.append("\n## Context Hints (files/paths that may be relevant)")
        for h in context_hints[:10]:
            parts.append(f"- {h}")
    if previous_failure:
        parts.append(
            f"\n## Previous Attempt Failed\n{previous_failure}\n"
            "Fix the issue above in your new attempt."
        )
    parts.append(
        "\n## Task\nGenerate a pytest test function that verifies the "
        "success criteria above.  Output ONLY Python code."
    )
    return "\n".join(parts)


async def generate_goal_test(
    goal_description: str,
    success_criteria: str,
    config: SecretaryConfig,
    context_hints: list[str] | None = None,
    previous_failure: str | None = None,
) -> str:
    """Use Haiku to generate a pytest test function for a goal.

    Returns the generated Python test code as a string.
    Raises on LLM failure (caller should handle gracefully).
    """
    from .direct_agent import _build_client, AGENT_PREFIX

    client = _build_client(config)
    prompt = _build_generate_prompt(
        goal_description, success_criteria, context_hints, previous_failure,
    )

    messages: list[dict[str, Any]] = list(AGENT_PREFIX) + [
        {"role": "user", "content": prompt},
    ]

    response = await asyncio.to_thread(
        _call_generate, client, messages,
    )

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
            break

    return _clean_generated_code(text)


def _call_generate(client: Any, messages: list[dict[str, Any]]) -> Any:
    """Synchronous Haiku call for test generation."""
    import anthropic

    with client.messages.stream(
        model=HARNESS_MODEL,
        max_tokens=HARNESS_MAX_TOKENS,
        system=_GENERATE_SYSTEM,
        messages=messages,
    ) as stream:
        return stream.get_final_message()


def _clean_generated_code(raw: str) -> str:
    """Strip markdown fences and validate basic structure."""
    text = raw.strip()
    # Remove markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Basic validation
    if "def test_" not in text:
        raise ValueError("Generated code does not contain a test function")
    if text.count("\n") > MAX_TEST_LINES:
        raise ValueError(
            f"Generated code exceeds {MAX_TEST_LINES} lines "
            f"({text.count(chr(10))} lines)"
        )

    return text


async def run_harness_test(
    test_code: str,
    base_dir: str,
    timeout: int = HARNESS_TEST_TIMEOUT,
) -> HarnessResult:
    """Run generated test code in a subprocess with timeout.

    Creates a temporary file, runs pytest on it, captures output.
    """
    start = time.monotonic()

    # Write test to a temp file in the project directory
    fd, test_path = tempfile.mkstemp(
        suffix="_harness_test.py", prefix="test_goal_", dir=base_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(test_code)

        # Build environment: project src/ on PYTHONPATH
        env = os.environ.copy()
        src_dir = os.path.join(base_dir, "src")
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing}" if existing else src_dir

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pytest", test_path, "-v", "--tb=short", "--no-header",
            cwd=base_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            elapsed = time.monotonic() - start
            return HarnessResult(
                test_code=test_code,
                passed=False,
                output=f"Test timed out after {timeout}s",
                error="timeout",
                duration=elapsed,
            )

        elapsed = time.monotonic() - start
        output_text = stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
        passed = proc.returncode == 0

        return HarnessResult(
            test_code=test_code,
            passed=passed,
            output=output_text,
            error=None if passed else f"exit code {proc.returncode}",
            duration=elapsed,
        )

    finally:
        # Clean up temp test file
        try:
            os.unlink(test_path)
        except OSError:
            pass


def format_harness_result(result: HarnessResult) -> str:
    """Format harness result as text for injection into verification context.

    Returns empty string if result is None.
    """
    if result is None:
        return ""

    icon = "PASS" if result.passed else "FAIL"
    lines = [
        "## Self-Generated Test Harness (ground truth)",
        f"Result: [{icon}] in {result.duration:.1f}s",
    ]

    if result.error:
        lines.append(f"Error: {result.error}")

    if result.output:
        # Show truncated test output
        lines.append(f"Output:\n{result.output}")

    if not result.passed:
        lines.append(
            "\nWARNING: Self-generated harness test failed. "
            "The goal's success criteria may not be met."
        )

    return "\n".join(lines)


# ── Validation Loop ──────────────────────────────────────────────
# After LLM generates pytest code:
#   (1) Syntax-check via ast.parse / compile
#   (2) Run against known-good outcome (real project dir) → must PASS
#   (3) Run against known-bad outcome (empty temp dir) → must FAIL
# Log all failures. Mark complete only after 3 consecutive passes.


@dataclass
class ValidationResult:
    """Result of the 3-phase harness validation."""

    test_code: str
    syntax_ok: bool
    syntax_error: str | None = None
    known_good: HarnessResult | None = None
    known_bad: HarnessResult | None = None

    @property
    def passed(self) -> bool:
        """All three checks passed: syntax OK, passes on good, fails on bad."""
        if not self.syntax_ok:
            return False
        if self.known_good is None or not self.known_good.passed:
            return False
        if self.known_bad is None or self.known_bad.passed:
            # known_bad should FAIL — if it passes, the test is trivially true
            return False
        return True

    @property
    def failure_reason(self) -> str:
        """Human-readable reason if validation failed."""
        if not self.syntax_ok:
            return f"syntax error: {self.syntax_error}"
        if self.known_good is None:
            return "known-good test not run"
        if not self.known_good.passed:
            return f"test fails on known-good env: {self.known_good.error or self.known_good.output[:200]}"
        if self.known_bad is None:
            return "known-bad test not run"
        if self.known_bad.passed:
            return "test passes on known-bad env (trivially true — not a real test)"
        return "unknown"


def syntax_check(test_code: str) -> tuple[bool, str | None]:
    """Phase 1: Validate generated test code parses as valid Python.

    Uses ast.parse() which catches syntax errors without executing the code.
    Returns (ok, error_message).
    """
    try:
        ast.parse(test_code, filename="<harness>", mode="exec")
        return True, None
    except SyntaxError as e:
        msg = f"line {e.lineno}: {e.msg}" if e.lineno else str(e.msg)
        return False, msg


async def validate_harness(
    test_code: str,
    known_good_dir: str,
    timeout: int = HARNESS_TEST_TIMEOUT,
) -> ValidationResult:
    """Run the 3-phase validation on a generated harness test.

    Phase 1: Syntax-check (ast.parse)
    Phase 2: Run against known-good dir (real project) → expects PASS
    Phase 3: Run against known-bad dir (empty temp dir) → expects FAIL

    If phase 1 fails, phases 2-3 are skipped.
    If phase 2 fails, phase 3 is still run for diagnostic completeness.
    """
    # Phase 1: Syntax check
    syntax_ok, syntax_err = syntax_check(test_code)
    if not syntax_ok:
        return ValidationResult(
            test_code=test_code,
            syntax_ok=False,
            syntax_error=syntax_err,
        )

    # Phase 2: Run against known-good environment (real project dir)
    known_good_result = await run_harness_test(test_code, known_good_dir, timeout=timeout)

    # Phase 3: Run against known-bad environment (empty temp dir)
    # An empty dir has none of the goal's artifacts → test SHOULD fail.
    # If it passes anyway, the test is trivially true and useless.
    known_bad_result: HarnessResult | None = None
    with tempfile.TemporaryDirectory(prefix="harness_bad_") as bad_dir:
        known_bad_result = await run_harness_test(test_code, bad_dir, timeout=timeout)

    return ValidationResult(
        test_code=test_code,
        syntax_ok=True,
        known_good=known_good_result,
        known_bad=known_bad_result,
    )


def _log_validation_failure(
    goal_id: str,
    attempt: int,
    result: ValidationResult,
    log_path: str = VALIDATION_LOG_FILE,
) -> None:
    """Append a structured failure record to the validation log (JSONL)."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "goal_id": goal_id,
        "attempt": attempt,
        "syntax_ok": result.syntax_ok,
        "syntax_error": result.syntax_error,
        "known_good_passed": result.known_good.passed if result.known_good else None,
        "known_bad_passed": result.known_bad.passed if result.known_bad else None,
        "failure_reason": result.failure_reason,
        "test_code_preview": result.test_code[:300],
    }
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        log.debug("Could not write validation log: %s", e)


async def harness_validation_loop(
    goal_id: str,
    goal_description: str,
    success_criteria: str,
    config: SecretaryConfig,
    known_good_dir: str,
    context_hints: list[str] | None = None,
    max_attempts: int = MAX_GENERATION_ATTEMPTS,
    required_consecutive: int = CONSECUTIVE_PASSES_REQUIRED,
) -> tuple[str | None, list[ValidationResult]]:
    """Generate harnesses in a loop until N consecutive ones pass validation.

    This is the main entry point for validated harness generation.

    Returns:
        (best_test_code, all_results)
        - best_test_code: The last passing test code, or None if loop exhausted.
        - all_results: All ValidationResult objects for diagnostics.

    The loop:
        1. Call generate_goal_test() to get LLM-generated pytest code
        2. Run validate_harness() (syntax → known-good → known-bad)
        3. If passes, increment consecutive counter; if fails, reset to 0 + log
        4. Complete when consecutive counter reaches required_consecutive
        5. Give up after max_attempts total generations
    """
    consecutive = 0
    all_results: list[ValidationResult] = []
    last_good_code: str | None = None
    last_failure_reason: str | None = None

    for attempt in range(1, max_attempts + 1):
        log.info(
            "Harness validation loop: goal=%s attempt=%d/%d consecutive=%d/%d",
            goal_id, attempt, max_attempts, consecutive, required_consecutive,
        )

        # Step 1: Generate test code via LLM (with failure feedback on retries)
        try:
            test_code = await generate_goal_test(
                goal_description,
                success_criteria,
                config,
                context_hints=context_hints,
                previous_failure=last_failure_reason,
            )
        except (ValueError, Exception) as e:
            # Generation itself failed (bad LLM output, API error)
            log.warning("Harness generation failed for goal %s attempt %d: %s", goal_id, attempt, e)
            # Create a synthetic failed result for logging
            fail_result = ValidationResult(
                test_code=str(e),
                syntax_ok=False,
                syntax_error=f"generation error: {e}",
            )
            all_results.append(fail_result)
            _log_validation_failure(goal_id, attempt, fail_result)
            consecutive = 0
            last_failure_reason = fail_result.failure_reason
            continue

        # Step 2: Validate (syntax → known-good → known-bad)
        result = await validate_harness(test_code, known_good_dir)
        all_results.append(result)

        if result.passed:
            consecutive += 1
            last_good_code = test_code
            last_failure_reason = None  # reset on success
            log.info(
                "Harness validation PASSED: goal=%s attempt=%d consecutive=%d/%d",
                goal_id, attempt, consecutive, required_consecutive,
            )
            if consecutive >= required_consecutive:
                log.info(
                    "Harness generation COMPLETE: goal=%s — %d consecutive passes",
                    goal_id, required_consecutive,
                )
                return last_good_code, all_results
        else:
            log.warning(
                "Harness validation FAILED: goal=%s attempt=%d reason=%s",
                goal_id, attempt, result.failure_reason,
            )
            _log_validation_failure(goal_id, attempt, result)
            consecutive = 0
            last_failure_reason = result.failure_reason

    # Exhausted attempts
    log.warning(
        "Harness validation loop EXHAUSTED: goal=%s — %d attempts, best consecutive=%d/%d",
        goal_id, max_attempts, consecutive, required_consecutive,
    )
    return last_good_code, all_results


def extract_context_hints(goal: dict[str, Any]) -> list[str]:
    """Extract filesystem hints from a goal's sub_goals and assertions.

    Scans for file paths mentioned in sub-goal evidence, expected_effects,
    and preconditions to help the test generator know what to check.
    """
    hints: list[str] = []

    for sg in goal.get("sub_goals", []):
        evidence = sg.get("evidence", "")
        if evidence:
            hints.append(f"sub-goal '{sg.get('id', '?')}': {evidence[:100]}")

    # Scan step plans for file paths from assertions
    for sg in goal.get("sub_goals", []):
        for eff in sg.get("expected_effects", []):
            path = eff.get("path", "")
            if path:
                hints.append(f"expected file: {path}")
        for pre in sg.get("preconditions", []):
            path = pre.get("path", "")
            if path:
                hints.append(f"precondition file: {path}")

    return hints[:10]  # Cap at 10 hints
