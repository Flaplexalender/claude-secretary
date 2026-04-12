"""Tests for Layer 29: goal_harness.py — Self-Generated Test Harness."""

from __future__ import annotations

import json
import os
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.goal_harness import (
    HARNESS_TEST_TIMEOUT,
    MAX_OUTPUT_CHARS,
    MAX_TEST_LINES,
    HarnessResult,
    _build_generate_prompt,
    _clean_generated_code,
    extract_context_hints,
    format_harness_result,
    generate_goal_test,
    run_harness_test,
)


# ── _build_generate_prompt ──────────────────────────────────────


def test_generate_prompt_includes_goal_and_criteria() -> None:
    prompt = _build_generate_prompt(
        goal_description="Improve test coverage",
        success_criteria="Coverage exceeds 80%",
    )
    assert "## Goal" in prompt
    assert "Improve test coverage" in prompt
    assert "## Success Criteria" in prompt
    assert "Coverage exceeds 80%" in prompt
    assert "## Task" in prompt


def test_generate_prompt_includes_context_hints() -> None:
    prompt = _build_generate_prompt(
        "desc", "criteria",
        context_hints=["expected file: data/result.json", "sub-goal 'x': done"],
    )
    assert "## Context Hints" in prompt
    assert "data/result.json" in prompt
    assert "sub-goal 'x'" in prompt


def test_generate_prompt_no_hints_omits_section() -> None:
    prompt = _build_generate_prompt("desc", "criteria")
    assert "Context Hints" not in prompt


def test_generate_prompt_caps_hints_at_10() -> None:
    hints = [f"hint {i}" for i in range(20)]
    prompt = _build_generate_prompt("desc", "criteria", context_hints=hints)
    # Should include at most 10
    assert "hint 9" in prompt
    assert "hint 10" not in prompt


def test_generate_prompt_includes_previous_failure() -> None:
    prompt = _build_generate_prompt(
        "desc", "criteria",
        previous_failure="test passes on known-bad env (trivially true)",
    )
    assert "## Previous Attempt Failed" in prompt
    assert "trivially true" in prompt
    assert "Fix the issue" in prompt


def test_generate_prompt_no_failure_omits_section() -> None:
    prompt = _build_generate_prompt("desc", "criteria", previous_failure=None)
    assert "Previous Attempt" not in prompt


# ── _clean_generated_code ───────────────────────────────────────


def test_clean_strips_markdown_fences() -> None:
    raw = '```python\ndef test_goal_check():\n    assert True\n```'
    result = _clean_generated_code(raw)
    assert "```" not in result
    assert "def test_goal_check" in result


def test_clean_preserves_plain_code() -> None:
    code = "import os\n\ndef test_goal_exists():\n    assert os.path.isdir('src')"
    result = _clean_generated_code(code)
    assert result == code


def test_clean_rejects_no_test_function() -> None:
    with pytest.raises(ValueError, match="does not contain a test function"):
        _clean_generated_code("print('hello world')")


def test_clean_rejects_too_many_lines() -> None:
    lines = ["import os"] + [f"    x{i} = {i}" for i in range(MAX_TEST_LINES + 5)]
    lines.insert(1, "def test_goal_big():")
    code = "\n".join(lines)
    with pytest.raises(ValueError, match="exceeds"):
        _clean_generated_code(code)


def test_clean_accepts_exactly_max_lines() -> None:
    # MAX_TEST_LINES lines should be OK
    code_lines = ["import os", "def test_goal_ok():"]
    code_lines += [f"    x = {i}" for i in range(MAX_TEST_LINES - 2)]
    code = "\n".join(code_lines)
    result = _clean_generated_code(code)
    assert "def test_goal_ok" in result


# ── HarnessResult ──────────────────────────────────────────────


def test_harness_result_defaults() -> None:
    r = HarnessResult(test_code="x", passed=True, output="ok")
    assert r.error is None
    assert r.duration == 0.0


# ── format_harness_result ──────────────────────────────────────


def test_format_harness_result_pass() -> None:
    r = HarnessResult(test_code="x", passed=True, output="1 passed", duration=0.5)
    text = format_harness_result(r)
    assert "[PASS]" in text
    assert "0.5s" in text
    assert "Self-Generated Test Harness" in text
    assert "WARNING" not in text


def test_format_harness_result_fail() -> None:
    r = HarnessResult(
        test_code="x", passed=False, output="FAILED", error="exit code 1", duration=1.2,
    )
    text = format_harness_result(r)
    assert "[FAIL]" in text
    assert "exit code 1" in text
    assert "WARNING" in text


def test_format_harness_result_none() -> None:
    assert format_harness_result(None) == ""


def test_format_harness_result_timeout() -> None:
    r = HarnessResult(
        test_code="x", passed=False, output="timed out", error="timeout", duration=30.0,
    )
    text = format_harness_result(r)
    assert "timeout" in text
    assert "[FAIL]" in text


# ── run_harness_test ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_harness_passing_test(tmp_path) -> None:
    """A passing test generates a PASS result."""
    code = textwrap.dedent("""\
        def test_goal_trivial():
            assert 1 + 1 == 2
    """)
    result = await run_harness_test(code, str(tmp_path))
    assert result.passed is True
    assert result.error is None
    assert result.duration > 0
    assert "passed" in result.output.lower()


@pytest.mark.asyncio
async def test_run_harness_failing_test(tmp_path) -> None:
    """A failing test generates a FAIL result."""
    code = textwrap.dedent("""\
        def test_goal_fail():
            assert False, "This should fail"
    """)
    result = await run_harness_test(code, str(tmp_path))
    assert result.passed is False
    assert result.error is not None
    assert "FAILED" in result.output or "failed" in result.output.lower()


@pytest.mark.asyncio
async def test_run_harness_timeout(tmp_path) -> None:
    """A slow test gets killed after timeout."""
    code = textwrap.dedent("""\
        import time
        def test_goal_slow():
            time.sleep(60)
    """)
    result = await run_harness_test(code, str(tmp_path), timeout=2)
    assert result.passed is False
    assert result.error == "timeout"


@pytest.mark.asyncio
async def test_run_harness_syntax_error(tmp_path) -> None:
    """Code with syntax error fails gracefully."""
    code = "def test_goal_broken(\n    this is not valid python"
    result = await run_harness_test(code, str(tmp_path))
    assert result.passed is False


@pytest.mark.asyncio
async def test_run_harness_file_check(tmp_path) -> None:
    """Test that checks for a file works when file exists."""
    # Create the file the test will look for
    (tmp_path / "data.json").write_text('{"score": 0.95}')
    code = textwrap.dedent("""\
        import os
        import json

        def test_goal_file_check():
            assert os.path.isfile("data.json"), "data.json missing"
            with open("data.json") as f:
                data = json.load(f)
            assert data["score"] >= 0.8, f"Score {data['score']} below threshold"
    """)
    result = await run_harness_test(code, str(tmp_path))
    assert result.passed is True


@pytest.mark.asyncio
async def test_run_harness_file_missing(tmp_path) -> None:
    """Test that checks for a file correctly fails when file is absent."""
    code = textwrap.dedent("""\
        import os

        def test_goal_file_check():
            assert os.path.isfile("nonexistent.json"), "File missing"
    """)
    result = await run_harness_test(code, str(tmp_path))
    assert result.passed is False


@pytest.mark.asyncio
async def test_run_harness_cleans_up_temp_file(tmp_path) -> None:
    """Temp test file is removed after execution."""
    code = "def test_goal_clean():\n    assert True"
    await run_harness_test(code, str(tmp_path))
    # No harness test files should remain (pytest_cache/__pycache__ are OK)
    remaining = [f for f in os.listdir(tmp_path) if f.endswith("_harness_test.py")]
    assert remaining == []


@pytest.mark.asyncio
async def test_run_harness_output_truncated(tmp_path) -> None:
    """Long output is truncated to MAX_OUTPUT_CHARS."""
    code = textwrap.dedent("""\
        def test_goal_verbose():
            # Generate a lot of output via print
            for i in range(500):
                print(f"line {i}: " + "x" * 100)
            assert True
    """)
    result = await run_harness_test(code, str(tmp_path))
    # Output should be capped
    assert len(result.output) <= MAX_OUTPUT_CHARS + 100  # small margin for encoding


# ── generate_goal_test ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_goal_test_calls_llm() -> None:
    """generate_goal_test calls the LLM and returns cleaned code."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(
        text='import os\n\ndef test_goal_check():\n    assert os.path.isdir("src")',
    )]
    config = MagicMock()

    with patch("secretary.goal_harness._call_generate", return_value=mock_msg):
        with patch("secretary.direct_agent._build_client"):
            with patch("secretary.direct_agent.AGENT_PREFIX", []):
                code = await generate_goal_test(
                    "Check project structure",
                    "src/ directory exists",
                    config,
                )
    assert "def test_goal_check" in code
    assert "os.path.isdir" in code


@pytest.mark.asyncio
async def test_generate_goal_test_strips_fences() -> None:
    """Markdown fences in LLM output are stripped."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(
        text='```python\ndef test_goal_x():\n    assert True\n```',
    )]
    config = MagicMock()

    with patch("secretary.goal_harness._call_generate", return_value=mock_msg):
        with patch("secretary.direct_agent._build_client"):
            with patch("secretary.direct_agent.AGENT_PREFIX", []):
                code = await generate_goal_test("g", "c", config)
    assert "```" not in code
    assert "def test_goal_x" in code


@pytest.mark.asyncio
async def test_generate_goal_test_invalid_code_raises() -> None:
    """If LLM generates code without a test function, ValueError is raised."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="print('hello')")]
    config = MagicMock()

    with patch("secretary.goal_harness._call_generate", return_value=mock_msg):
        with patch("secretary.direct_agent._build_client"):
            with patch("secretary.direct_agent.AGENT_PREFIX", []):
                with pytest.raises(ValueError, match="does not contain"):
                    await generate_goal_test("g", "c", config)


# ── extract_context_hints ──────────────────────────────────────


def test_extract_context_hints_from_sub_goals() -> None:
    goal = {
        "id": "g1",
        "sub_goals": [
            {"id": "sg1", "evidence": "Router trained, commit abc123"},
            {"id": "sg2", "evidence": ""},
        ],
    }
    hints = extract_context_hints(goal)
    assert any("Router trained" in h for h in hints)
    assert len(hints) >= 1


def test_extract_context_hints_from_assertions() -> None:
    goal = {
        "id": "g1",
        "sub_goals": [
            {
                "id": "sg1",
                "expected_effects": [{"path": "data/result.json"}],
                "preconditions": [{"path": "config.yaml"}],
            },
        ],
    }
    hints = extract_context_hints(goal)
    assert any("data/result.json" in h for h in hints)
    assert any("config.yaml" in h for h in hints)


def test_extract_context_hints_empty_goal() -> None:
    hints = extract_context_hints({"id": "g1"})
    assert hints == []


def test_extract_context_hints_caps_at_10() -> None:
    goal = {
        "id": "g1",
        "sub_goals": [
            {"id": f"sg{i}", "evidence": f"evidence for {i}"}
            for i in range(20)
        ],
    }
    hints = extract_context_hints(goal)
    assert len(hints) <= 10


# ── Integration: generate + run ─────────────────────────────────


@pytest.mark.asyncio
async def test_end_to_end_generate_and_run(tmp_path) -> None:
    """Simulate full flow: generate test code, run it, format result."""
    # Create a file the test will check
    (tmp_path / "output.txt").write_text("success: 42 items processed")

    # Simulate LLM generating test code
    test_code = textwrap.dedent("""\
        import os

        def test_goal_output():
            assert os.path.isfile("output.txt"), "output.txt missing"
            with open("output.txt") as f:
                content = f.read()
            assert "success" in content, "No success indicator"
            assert "42" in content, "Expected 42 items"
    """)

    result = await run_harness_test(test_code, str(tmp_path))
    assert result.passed is True

    formatted = format_harness_result(result)
    assert "PASS" in formatted
    assert "Self-Generated Test Harness" in formatted


@pytest.mark.asyncio
async def test_end_to_end_fail_flow(tmp_path) -> None:
    """Full flow with a failing test produces useful diagnostics."""
    test_code = textwrap.dedent("""\
        import os

        def test_goal_missing():
            assert os.path.isfile("required_output.json"), "Output file not created"
    """)

    result = await run_harness_test(test_code, str(tmp_path))
    assert result.passed is False

    formatted = format_harness_result(result)
    assert "FAIL" in formatted
    assert "WARNING" in formatted
