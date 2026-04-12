"""Tests for Haiku-as-planner — cheap model plans, expensive model executes."""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from secretary.planner import (
    PlanStep,
    TaskPlan,
    _MIN_WORDS_FOR_PLANNING,
    _PLANNER_SYSTEM,
    _parse_plan,
    _should_plan,
    plan_task,
)
from secretary.config import SecretaryConfig, OptimizationConfig


# ── Fixtures ──────────────────────────────────────────────

def _make_config(haiku_planner: bool = True, agent_prefix: bool = True) -> SecretaryConfig:
    """Create a config with planner settings."""
    return SecretaryConfig(
        optimizations=OptimizationConfig(haiku_planner=haiku_planner),
        agent_prefix=agent_prefix,
    )


def _make_plan_json(
    steps: int = 3,
    complexity: str = "medium",
    key_files: list[str] | None = None,
) -> str:
    """Build a valid planner JSON response."""
    plan_steps = []
    for i in range(1, steps + 1):
        plan_steps.append({
            "id": i,
            "action": f"Step {i}: do something useful",
            "tools": ["grep_search", "file_edit"],
            "depends_on": [i - 1] if i > 1 else [],
        })
    return json.dumps({
        "steps": plan_steps,
        "parallel_groups": [[1, 2], [3]] if steps >= 3 else [[1]],
        "key_files": key_files or ["src/secretary/agent.py"],
        "estimated_complexity": complexity,
    })


# ── PlanStep / TaskPlan unit tests ────────────────────────

class TestPlanStep:
    def test_basic_creation(self):
        step = PlanStep(id=1, action="Read the file", tools=["file_read"])
        assert step.id == 1
        assert step.action == "Read the file"
        assert step.tools == ["file_read"]
        assert step.depends_on == []

    def test_with_dependencies(self):
        step = PlanStep(id=3, action="Test", tools=["run_command"], depends_on=[1, 2])
        assert step.depends_on == [1, 2]


class TestTaskPlan:
    def test_empty_plan_is_simple(self):
        plan = TaskPlan()
        assert plan.is_simple is True

    def test_single_step_is_simple(self):
        plan = TaskPlan(steps=[PlanStep(id=1, action="Do it")])
        assert plan.is_simple is True

    def test_low_complexity_is_simple(self):
        plan = TaskPlan(
            steps=[PlanStep(id=1, action="A"), PlanStep(id=2, action="B")],
            estimated_complexity="low",
        )
        assert plan.is_simple is True

    def test_medium_multi_step_not_simple(self):
        plan = TaskPlan(
            steps=[PlanStep(id=1, action="A"), PlanStep(id=2, action="B")],
            estimated_complexity="medium",
        )
        assert plan.is_simple is False

    def test_format_for_prompt_empty(self):
        plan = TaskPlan()
        assert plan.format_for_prompt() == ""

    def test_format_for_prompt_basic(self):
        plan = TaskPlan(
            steps=[
                PlanStep(id=1, action="Read config", tools=["file_read"]),
                PlanStep(id=2, action="Edit router", tools=["file_edit"], depends_on=[1]),
            ],
            key_files=["src/secretary/router.py"],
            parallel_groups=[[1, 2]],
        )
        text = plan.format_for_prompt()
        assert "Pre-planned approach" in text
        assert "1. Read config [file_read]" in text
        assert "2. Edit router [file_edit] (after step 1)" in text
        assert "Key files: src/secretary/router.py" in text
        assert "Parallel groups: (1, 2)" in text

    def test_format_no_parallel_groups(self):
        plan = TaskPlan(
            steps=[PlanStep(id=1, action="Just one thing")],
            parallel_groups=[],
        )
        text = plan.format_for_prompt()
        assert "Parallel groups" not in text

    def test_format_single_item_groups_hidden(self):
        """Single-item parallel groups are not shown (not useful)."""
        plan = TaskPlan(
            steps=[PlanStep(id=1, action="A"), PlanStep(id=2, action="B")],
            parallel_groups=[[1], [2]],
        )
        text = plan.format_for_prompt()
        assert "Parallel groups" not in text


# ── _should_plan tests ────────────────────────────────────

class TestShouldPlan:
    def test_enabled_high_tier_long_task(self):
        config = _make_config(haiku_planner=True)
        task = "Refactor the authentication system to support multi-tenancy across all modules"
        assert _should_plan(task, "high", config) is True

    def test_disabled_in_config(self):
        config = _make_config(haiku_planner=False)
        task = "Refactor the authentication system to support multi-tenancy across all modules"
        assert _should_plan(task, "high", config) is False

    def test_low_tier_skipped(self):
        config = _make_config(haiku_planner=True)
        task = "What is the current time in PST timezone right now?"
        assert _should_plan(task, "low", config) is False

    def test_medium_tier_skipped(self):
        config = _make_config(haiku_planner=True)
        task = "Check my unread emails from today and summarize them"
        assert _should_plan(task, "medium", config) is False

    def test_deep_tier_planned(self):
        config = _make_config(haiku_planner=True)
        task = "Investigate why the watcher daemon fails after running for 24 hours continuously"
        assert _should_plan(task, "deep", config) is True

    def test_short_task_skipped(self):
        config = _make_config(haiku_planner=True)
        task = "Fix the bug"  # only 3 words
        assert _should_plan(task, "high", config) is False

    def test_exactly_min_words(self):
        config = _make_config(haiku_planner=True)
        task = " ".join(["word"] * _MIN_WORDS_FOR_PLANNING)
        assert _should_plan(task, "high", config) is True

    def test_below_min_words(self):
        config = _make_config(haiku_planner=True)
        task = " ".join(["word"] * (_MIN_WORDS_FOR_PLANNING - 1))
        assert _should_plan(task, "high", config) is False


# ── _parse_plan tests ─────────────────────────────────────

class TestParsePlan:
    def test_valid_json(self):
        raw = _make_plan_json(steps=3, complexity="high")
        plan = _parse_plan(raw)
        assert len(plan.steps) == 3
        assert plan.estimated_complexity == "high"
        assert plan.key_files == ["src/secretary/agent.py"]
        assert plan.steps[0].depends_on == []
        assert plan.steps[1].depends_on == [1]

    def test_markdown_code_fence(self):
        raw = f"```json\n{_make_plan_json(steps=2)}\n```"
        plan = _parse_plan(raw)
        assert len(plan.steps) == 2

    def test_bare_code_fence(self):
        raw = f"```\n{_make_plan_json(steps=2)}\n```"
        plan = _parse_plan(raw)
        assert len(plan.steps) == 2

    def test_invalid_json_returns_empty(self):
        plan = _parse_plan("this is not json at all")
        assert len(plan.steps) == 0
        assert plan.raw_json == "this is not json at all"

    def test_empty_steps(self):
        plan = _parse_plan(json.dumps({"steps": [], "estimated_complexity": "low"}))
        assert len(plan.steps) == 0
        assert plan.estimated_complexity == "low"

    def test_missing_fields_defaults(self):
        raw = json.dumps({
            "steps": [{"id": 1, "action": "Do it"}],
        })
        plan = _parse_plan(raw)
        assert len(plan.steps) == 1
        assert plan.steps[0].tools == []
        assert plan.steps[0].depends_on == []
        assert plan.estimated_complexity == "medium"  # default

    def test_parallel_groups_preserved(self):
        raw = _make_plan_json(steps=4, complexity="medium")
        plan = _parse_plan(raw)
        assert len(plan.parallel_groups) > 0

    def test_whitespace_tolerance(self):
        raw = f"\n\n  {_make_plan_json(steps=2)}  \n\n"
        plan = _parse_plan(raw)
        assert len(plan.steps) == 2


# ── plan_task integration tests ───────────────────────────

class TestPlanTask:
    @pytest.mark.asyncio
    async def test_skips_when_disabled(self):
        config = _make_config(haiku_planner=False)
        result = await plan_task("Refactor the entire authentication module for the project", config, tier="high")
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_low_tier(self):
        config = _make_config(haiku_planner=True)
        result = await plan_task("What time is it right now in my timezone?", config, tier="low")
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_short_task(self):
        config = _make_config(haiku_planner=True)
        result = await plan_task("Fix bug", config, tier="high")
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_plan(self):
        """Mock the Anthropic API and verify plan is returned."""
        config = _make_config(haiku_planner=True, agent_prefix=True)
        plan_json = _make_plan_json(steps=3, complexity="high")

        # Mock the Anthropic client
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=plan_json)]
        mock_response.usage = MagicMock(input_tokens=200, output_tokens=150)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("secretary.planner.anthropic.Anthropic", return_value=mock_client):
            result = await plan_task(
                "Refactor the authentication system to support multi-tenancy across modules",
                config,
                tier="high",
            )

        assert result is not None
        assert len(result.steps) == 3
        assert result.estimated_complexity == "high"
        assert result.planner_model == "claude-haiku-4.5"
        assert result.planner_input_tokens == 200
        assert result.planner_output_tokens == 150

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        """API failures should not crash — just skip planning."""
        import anthropic as anthropic_mod
        config = _make_config(haiku_planner=True)

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = anthropic_mod.APIError(
            message="Service unavailable",
            request=MagicMock(),
            body=None,
        )

        with patch("secretary.planner.anthropic.Anthropic", return_value=mock_client):
            result = await plan_task(
                "Refactor the complete authentication system for the entire project",
                config,
                tier="high",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        """If planner returns gibberish, skip planning gracefully."""
        config = _make_config(haiku_planner=True)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="I'm not sure what to do here.")]
        mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("secretary.planner.anthropic.Anthropic", return_value=mock_client):
            result = await plan_task(
                "Investigate the complex performance issue in the watcher daemon module",
                config,
                tier="high",
            )

        assert result is None  # no steps → returns None

    @pytest.mark.asyncio
    async def test_agent_prefix_included_in_messages(self):
        """When agent_prefix=True, planner should include prefix messages."""
        config = _make_config(haiku_planner=True, agent_prefix=True)
        plan_json = _make_plan_json(steps=2)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=plan_json)]
        mock_response.usage = MagicMock(input_tokens=100, output_tokens=80)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("secretary.planner.anthropic.Anthropic", return_value=mock_client):
            await plan_task(
                "Redesign the model routing system to support dynamic tier switching",
                config,
                tier="high",
            )

        # Verify the messages sent to API include prefix
        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        assert len(messages) == 3  # prefix user + prefix assistant + actual task
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_no_prefix_when_disabled(self):
        """When agent_prefix=False, planner should not include prefix messages."""
        config = _make_config(haiku_planner=True, agent_prefix=False)
        plan_json = _make_plan_json(steps=2)

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=plan_json)]
        mock_response.usage = MagicMock(input_tokens=100, output_tokens=80)

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("secretary.planner.anthropic.Anthropic", return_value=mock_client):
            await plan_task(
                "Redesign the model routing system to support dynamic tier switching",
                config,
                tier="high",
            )

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        assert len(messages) == 1  # just the task, no prefix

    @pytest.mark.asyncio
    async def test_no_low_tier_returns_none(self):
        """If low tier is not configured, planner should skip."""
        config = _make_config(haiku_planner=True)
        # Remove the low tier
        del config.routing.tiers["low"]

        result = await plan_task(
            "Refactor the entire authentication module for this project",
            config,
            tier="high",
        )
        assert result is None


# ── Integration: planner + direct_agent config ────────────

class TestPlannerConfig:
    def test_default_config_has_planner_enabled(self):
        config = SecretaryConfig()
        assert config.optimizations.haiku_planner is True

    def test_planner_can_be_disabled(self):
        config = SecretaryConfig(
            optimizations=OptimizationConfig(haiku_planner=False),
        )
        assert config.optimizations.haiku_planner is False

    def test_planner_system_prompt_is_compact(self):
        """System prompt should be under 500 tokens (~2000 chars)."""
        assert len(_PLANNER_SYSTEM) < 2000

    def test_plan_format_fits_in_prompt(self):
        """A 5-step plan should format to under 500 chars."""
        plan = TaskPlan(
            steps=[PlanStep(id=i, action=f"Step {i} action") for i in range(1, 6)],
            key_files=["a.py", "b.py"],
            parallel_groups=[[1, 2], [3, 4, 5]],
        )
        text = plan.format_for_prompt()
        assert len(text) < 500
