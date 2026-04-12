"""Tests for Layer 19: goal_verification.py — Completion Verification."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.goal_verification import (
    FAIL,
    INCONCLUSIVE,
    MAX_VERIFICATION_LOG,
    PASS,
    _build_verify_prompt,
    _parse_verify_response,
    check_goal_completion,
    detect_completed_goals,
    mark_goals_completed,
    record_verification,
    verify_step_completion,
)


@pytest.fixture()
def state() -> dict:
    """Empty goal state."""
    return {}


# ── _build_verify_prompt ────────────────────────────────────────

def test_build_verify_prompt_includes_all_sections() -> None:
    prompt = _build_verify_prompt(
        action="Read data/run_log.jsonl",
        verification="File exists and has > 10 entries",
        agent_output="Found 25 entries in run_log.jsonl",
    )
    assert "## Step Action" in prompt
    assert "Read data/run_log.jsonl" in prompt
    assert "## Verification Criteria" in prompt
    assert "File exists and has > 10 entries" in prompt
    assert "## Agent Output" in prompt
    assert "Found 25 entries" in prompt
    assert "## Your Verdict" in prompt


def test_build_verify_prompt_no_output() -> None:
    prompt = _build_verify_prompt("act", "verify", "")
    assert "(no output)" in prompt


def test_build_verify_prompt_truncates_long_output() -> None:
    long_output = "x" * 5000
    prompt = _build_verify_prompt("act", "verify", long_output)
    # Output should be truncated to 4000 chars
    assert len(prompt) < 5000


def test_build_verify_prompt_includes_assertion_results() -> None:
    """When assertion text is provided, it appears in the prompt."""
    assertion_text = (
        "## Environment Assertions (ground truth)\n"
        "Result: 1/2 passed\n"
        "  [PASS] file_exists: out.json — file exists\n"
        "  [FAIL] file_contains: out.json — pattern not found"
    )
    prompt = _build_verify_prompt(
        action="Write output",
        verification="out.json exists with results",
        agent_output="Wrote output successfully",
        assertion_results_text=assertion_text,
    )
    assert "Environment Assertions (ground truth)" in prompt
    assert "[FAIL] file_contains" in prompt
    # Assertions should appear between Agent Output and Your Verdict
    agent_idx = prompt.index("## Agent Output")
    assertion_idx = prompt.index("Environment Assertions")
    verdict_idx = prompt.index("## Your Verdict")
    assert agent_idx < assertion_idx < verdict_idx


def test_build_verify_prompt_no_assertions_omits_section() -> None:
    """When no assertion text, the extra section is not added."""
    prompt = _build_verify_prompt("act", "verify", "output", assertion_results_text="")
    assert "Environment Assertions" not in prompt


# ── _parse_verify_response ──────────────────────────────────────

def test_parse_pass_response() -> None:
    text = json.dumps({"verdict": "pass", "reasoning": "Output clearly shows file exists."})
    result = _parse_verify_response(text)
    assert result["verdict"] == PASS
    assert "file exists" in result["reasoning"]


def test_parse_fail_response() -> None:
    text = json.dumps({"verdict": "fail", "reasoning": "No evidence of file creation."})
    result = _parse_verify_response(text)
    assert result["verdict"] == FAIL


def test_parse_inconclusive_response() -> None:
    text = json.dumps({"verdict": "inconclusive", "reasoning": "Ambiguous output."})
    result = _parse_verify_response(text)
    assert result["verdict"] == INCONCLUSIVE


def test_parse_markdown_fenced_json() -> None:
    text = '```json\n{"verdict": "pass", "reasoning": "OK"}\n```'
    result = _parse_verify_response(text)
    assert result["verdict"] == PASS


def test_parse_empty_response() -> None:
    result = _parse_verify_response("")
    assert result["verdict"] == PASS  # fail-open


def test_parse_invalid_json() -> None:
    result = _parse_verify_response("not json at all")
    assert result["verdict"] == PASS  # fail-open


def test_parse_unknown_verdict_falls_back_to_pass() -> None:
    text = json.dumps({"verdict": "maybe", "reasoning": "Unsure."})
    result = _parse_verify_response(text)
    assert result["verdict"] == PASS  # fail-open


def test_parse_truncates_long_reasoning() -> None:
    text = json.dumps({"verdict": "fail", "reasoning": "x" * 500})
    result = _parse_verify_response(text)
    assert len(result["reasoning"]) <= 300


# ── verify_step_completion ──────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_no_criteria_auto_pass() -> None:
    """Steps without verification criteria auto-pass."""
    config = MagicMock()
    result = await verify_step_completion("action", "", "output", config)
    assert result["verdict"] == PASS
    assert "No verification" in result["reasoning"]


@pytest.mark.asyncio
async def test_verify_whitespace_criteria_auto_pass() -> None:
    config = MagicMock()
    result = await verify_step_completion("action", "   ", "output", config)
    assert result["verdict"] == PASS


@pytest.mark.asyncio
async def test_verify_calls_haiku_and_returns_verdict() -> None:
    """Verify that the function calls the LLM and parses the response."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text='{"verdict": "pass", "reasoning": "Looks good."}')]

    config = MagicMock()

    with patch("secretary.goal_verification._call_verify", return_value=mock_msg):
        with patch("secretary.direct_agent._build_client"):
            with patch("secretary.direct_agent.AGENT_PREFIX", []):
                result = await verify_step_completion(
                    "Read file", "File has data", "Found 10 rows", config,
                )
    assert result["verdict"] == PASS
    assert "Looks good" in result["reasoning"]


@pytest.mark.asyncio
async def test_verify_fail_verdict() -> None:
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text='{"verdict": "fail", "reasoning": "No evidence."}')]

    config = MagicMock()

    with patch("secretary.goal_verification._call_verify", return_value=mock_msg):
        with patch("secretary.direct_agent._build_client"):
            with patch("secretary.direct_agent.AGENT_PREFIX", []):
                result = await verify_step_completion(
                    "action", "criteria", "output", config,
                )
    assert result["verdict"] == FAIL


@pytest.mark.asyncio
async def test_verify_llm_error_fails_open() -> None:
    """If the LLM call fails, fail-open to PASS."""
    config = MagicMock()

    with patch("secretary.direct_agent._build_client"):
        with patch("secretary.direct_agent.AGENT_PREFIX", []):
            with patch("secretary.goal_verification.asyncio") as mock_aio:
                mock_aio.to_thread = AsyncMock(side_effect=Exception("API down"))
                result = await verify_step_completion(
                    "action", "criteria", "output", config,
                )
    assert result["verdict"] == PASS
    assert "failed" in result["reasoning"].lower()
    assert result["verdict"] == PASS
    assert "failed" in result["reasoning"].lower()


@pytest.mark.asyncio
async def test_verify_passes_assertion_text_to_prompt() -> None:
    """assertion_results_text is forwarded into the LLM prompt."""
    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text='{"verdict": "fail", "reasoning": "Assertions failed."}')]

    config = MagicMock()
    assertion_text = "## Environment Assertions (ground truth)\nResult: 0/1 passed"

    with patch("secretary.goal_verification._call_verify", return_value=mock_msg) as mock_call:
        with patch("secretary.direct_agent._build_client"):
            with patch("secretary.direct_agent.AGENT_PREFIX", []):
                result = await verify_step_completion(
                    "Write file", "file created", "Done",
                    config, assertion_results_text=assertion_text,
                )
    assert result["verdict"] == FAIL
    # Verify the assertion text was included in the prompt sent to the LLM
    call_args = mock_call.call_args
    messages = call_args[1]["messages"] if "messages" in (call_args[1] or {}) else call_args[0][1]
    user_prompt = messages[-1]["content"]
    assert "Environment Assertions" in user_prompt


# ── record_verification ─────────────────────────────────────────

def test_record_verification_creates_log(state: dict) -> None:
    record_verification(state, "s1.1", "sg1", PASS, "Verified OK.")
    assert len(state["verification_log"]) == 1
    entry = state["verification_log"][0]
    assert entry["step_id"] == "s1.1"
    assert entry["sub_goal_id"] == "sg1"
    assert entry["verdict"] == PASS
    assert entry["reasoning"] == "Verified OK."
    assert "ts" in entry


def test_record_verification_appends(state: dict) -> None:
    record_verification(state, "s1.1", "sg1", PASS, "OK")
    record_verification(state, "s1.2", "sg1", FAIL, "Nope")
    assert len(state["verification_log"]) == 2


def test_record_verification_truncates_reasoning(state: dict) -> None:
    record_verification(state, "s1.1", "sg1", PASS, "x" * 500)
    assert len(state["verification_log"][0]["reasoning"]) <= 300


def test_record_verification_caps_log_size(state: dict) -> None:
    for i in range(MAX_VERIFICATION_LOG + 20):
        record_verification(state, f"s{i}", "sg1", PASS, f"entry {i}")
    assert len(state["verification_log"]) == MAX_VERIFICATION_LOG


def test_record_verification_with_goal_id(state: dict) -> None:
    """Layer 30: Non-step goal tasks record goal_id for direct trust matching."""
    record_verification(state, "task-abc123", "", PASS, "Task passed", goal_id="prefix-survival")
    entry = state["verification_log"][0]
    assert entry["goal_id"] == "prefix-survival"
    assert entry["sub_goal_id"] == ""
    assert entry["step_id"] == "task-abc123"


def test_record_verification_without_goal_id_has_no_field(state: dict) -> None:
    """Existing callers without goal_id don't add the field."""
    record_verification(state, "s1.1", "sg1", PASS, "OK")
    entry = state["verification_log"][0]
    assert "goal_id" not in entry


# ── check_goal_completion ────────────────────────────────────────

def test_check_goal_all_done() -> None:
    goal = {
        "id": "g1",
        "sub_goals": [
            {"id": "sg1", "status": "done"},
            {"id": "sg2", "status": "done"},
        ],
    }
    assert check_goal_completion(goal, {}) is True


def test_check_goal_not_all_done() -> None:
    goal = {
        "id": "g1",
        "sub_goals": [
            {"id": "sg1", "status": "done"},
            {"id": "sg2", "status": "in-progress"},
        ],
    }
    assert check_goal_completion(goal, {}) is False


def test_check_goal_override_makes_done() -> None:
    goal = {
        "id": "g1",
        "sub_goals": [
            {"id": "sg1", "status": "done"},
            {"id": "sg2", "status": "in-progress"},
        ],
    }
    overrides = {"sg2": {"status": "done"}}
    assert check_goal_completion(goal, overrides) is True


def test_check_goal_override_makes_not_done() -> None:
    goal = {
        "id": "g1",
        "sub_goals": [
            {"id": "sg1", "status": "done"},
            {"id": "sg2", "status": "done"},
        ],
    }
    overrides = {"sg1": {"status": "blocked"}}
    assert check_goal_completion(goal, overrides) is False


def test_check_goal_no_sub_goals() -> None:
    goal = {"id": "g1", "sub_goals": []}
    assert check_goal_completion(goal, {}) is False


def test_check_goal_missing_sub_goals_key() -> None:
    goal = {"id": "g1"}
    assert check_goal_completion(goal, {}) is False


# ── detect_completed_goals ──────────────────────────────────────

def test_detect_completed_goals_finds_complete() -> None:
    goals = [
        {
            "id": "g1",
            "status": "in-progress",
            "sub_goals": [
                {"id": "sg1", "status": "done"},
                {"id": "sg2", "status": "done"},
            ],
        },
    ]
    state = {"sub_goal_status": {}}
    result = detect_completed_goals(goals, state)
    assert "g1" in result


def test_detect_completed_goals_skips_already_done() -> None:
    goals = [
        {
            "id": "g1",
            "status": "done",
            "sub_goals": [
                {"id": "sg1", "status": "done"},
            ],
        },
    ]
    state = {"sub_goal_status": {}}
    result = detect_completed_goals(goals, state)
    assert result == []


def test_detect_completed_goals_uses_overrides() -> None:
    goals = [
        {
            "id": "g1",
            "status": "in-progress",
            "sub_goals": [
                {"id": "sg1", "status": "done"},
                {"id": "sg2", "status": "not-started"},
            ],
        },
    ]
    state = {"sub_goal_status": {"sg2": {"status": "done"}}}
    result = detect_completed_goals(goals, state)
    assert "g1" in result


def test_detect_completed_goals_incomplete() -> None:
    goals = [
        {
            "id": "g1",
            "status": "in-progress",
            "sub_goals": [
                {"id": "sg1", "status": "done"},
                {"id": "sg2", "status": "in-progress"},
            ],
        },
    ]
    state = {"sub_goal_status": {}}
    result = detect_completed_goals(goals, state)
    assert result == []


# ── mark_goals_completed ────────────────────────────────────────

def test_mark_goals_completed_records(state: dict) -> None:
    goals = [
        {
            "id": "g1",
            "sub_goals": [{"id": "sg1"}, {"id": "sg2"}],
        },
    ]
    mark_goals_completed(goals, state, ["g1"])
    assert "completed_goals" in state
    assert state["completed_goals"]["g1"]["completed"] is True
    assert "2 sub-goals" in state["completed_goals"]["g1"]["evidence"]
    assert "ts" in state["completed_goals"]["g1"]


def test_mark_goals_completed_idempotent(state: dict) -> None:
    goals = [{"id": "g1", "sub_goals": [{"id": "sg1"}]}]
    mark_goals_completed(goals, state, ["g1"])
    ts1 = state["completed_goals"]["g1"]["ts"]
    mark_goals_completed(goals, state, ["g1"])
    # Should not update timestamp on re-mark
    assert state["completed_goals"]["g1"]["ts"] == ts1


def test_mark_goals_completed_multiple(state: dict) -> None:
    goals = [
        {"id": "g1", "sub_goals": [{"id": "sg1"}]},
        {"id": "g2", "sub_goals": [{"id": "sg2"}, {"id": "sg3"}]},
    ]
    mark_goals_completed(goals, state, ["g1", "g2"])
    assert "g1" in state["completed_goals"]
    assert "g2" in state["completed_goals"]


# ── step_to_task includes verification fields ───────────────────

def test_step_to_task_includes_verification_fields() -> None:
    from secretary.goal_decomposition import step_to_task

    step = {
        "step_id": "sg1.1",
        "action": "Read the file",
        "verification": "File has 10 rows",
        "tier": "low",
    }
    task = step_to_task(step, "sg1", "g1")
    assert task["_action"] == "Read the file"
    assert task["_verification"] == "File has 10 rows"
    assert task["_step_id"] == "sg1.1"
    assert task["_sub_goal_id"] == "sg1"


def test_step_to_task_empty_verification() -> None:
    from secretary.goal_decomposition import step_to_task

    step = {
        "step_id": "sg1.1",
        "action": "Do something",
        "verification": "",
        "tier": "low",
    }
    task = step_to_task(step, "sg1", "g1")
    assert task["_verification"] == ""
    assert task["_action"] == "Do something"
