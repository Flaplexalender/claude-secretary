"""Haiku-as-planner — cheap model plans, expensive model executes.

For complex tasks (routed to Opus/deep tier), use Haiku to decompose the
task into focused sub-steps before handing to Opus. Benefits:

- Haiku at 0.33x premium does the exploratory "what should I do?" thinking
- Opus at 3x premium gets a clear plan → fewer wasted turns exploring
- Net savings: 1 Haiku call (~$0.01) replaces 2-3 Opus exploration turns (~$0.36)

The planner produces a structured plan that's injected into the Opus prompt.
Opus doesn't need to figure out the approach — it just executes.

When agent_prefix is active, the planner still saves turns
(Opus with a plan finishes in 2-3 turns vs 5-7 without).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import anthropic
import httpx

from .config import SecretaryConfig, _interpolate_env

log = logging.getLogger(__name__)

# Tasks shorter than this (words) are too simple for planning overhead.
_MIN_WORDS_FOR_PLANNING = 8

# The planner system prompt — kept minimal to reduce Haiku input tokens.
_PLANNER_SYSTEM = """You are a task planner. Given a task, output a JSON plan.

Rules:
1. Break the task into 2-5 concrete steps. Each step should be one clear action.
2. Identify which files/tools are relevant for each step.
3. Flag any dependencies between steps.
4. Keep it brief — the executor is a senior engineer who doesn't need hand-holding.

Output format (JSON only, no markdown):
{
  "steps": [
    {"id": 1, "action": "what to do", "tools": ["tool1"], "depends_on": []},
    {"id": 2, "action": "what to do next", "tools": ["tool2"], "depends_on": [1]}
  ],
  "parallel_groups": [[1, 2], [3]],
  "key_files": ["path/to/relevant.py"],
  "estimated_complexity": "medium"
}

parallel_groups: steps that can run in parallel (same group = parallel).
estimated_complexity: "low", "medium", or "high" — your assessment.
If the task is actually simple (1-2 steps), say so honestly."""

# Max tokens for planner response — plans should be compact.
_PLANNER_MAX_TOKENS = 1024


@dataclass
class PlanStep:
    """A single step in the task plan."""
    id: int
    action: str
    tools: list[str] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)


@dataclass
class TaskPlan:
    """Structured plan produced by the Haiku planner."""
    steps: list[PlanStep] = field(default_factory=list)
    parallel_groups: list[list[int]] = field(default_factory=list)
    key_files: list[str] = field(default_factory=list)
    estimated_complexity: str = "medium"
    raw_json: str = ""
    planner_model: str = ""
    planner_input_tokens: int = 0
    planner_output_tokens: int = 0

    @property
    def is_simple(self) -> bool:
        """True if the planner thinks the task is actually simple."""
        return self.estimated_complexity == "low" or len(self.steps) <= 1

    def format_for_prompt(self) -> str:
        """Format the plan as a compact instruction block for the executor."""
        if not self.steps:
            return ""
        lines = ["## Pre-planned approach (from planner — follow this plan):"]
        for step in self.steps:
            deps = f" (after step {', '.join(str(d) for d in step.depends_on)})" if step.depends_on else ""
            tools = f" [{', '.join(step.tools)}]" if step.tools else ""
            lines.append(f"{step.id}. {step.action}{tools}{deps}")
        if self.key_files:
            lines.append(f"\nKey files: {', '.join(self.key_files)}")
        if self.parallel_groups:
            groups = [f"({', '.join(str(s) for s in g)})" for g in self.parallel_groups if len(g) > 1]
            if groups:
                lines.append(f"Parallel groups: {', '.join(groups)} — batch these tool calls together.")
        return "\n".join(lines)


def _should_plan(task: str, tier: str, config: SecretaryConfig) -> bool:
    """Decide whether this task benefits from planning.

    Planning adds ~1 cheap API call overhead, so we only plan for:
    - High-complexity tasks (routed to high/deep tier)
    - Tasks with enough substance to decompose (>= 8 words)
    - When the optimization is enabled in config
    """
    if not config.optimizations.haiku_planner:
        return False
    if tier not in ("high", "deep"):
        return False
    if len(task.split()) < _MIN_WORDS_FOR_PLANNING:
        return False
    return True


def _parse_plan(raw: str) -> TaskPlan:
    """Parse the planner's JSON response into a TaskPlan.

    Tolerant of markdown code blocks and minor formatting issues.
    """
    # Strip markdown code fences if present
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Planner returned invalid JSON, skipping plan")
        return TaskPlan(raw_json=raw)

    steps = []
    for s in data.get("steps", []):
        steps.append(PlanStep(
            id=s.get("id", 0),
            action=s.get("action", ""),
            tools=s.get("tools", []),
            depends_on=s.get("depends_on", []),
        ))

    return TaskPlan(
        steps=steps,
        parallel_groups=data.get("parallel_groups", []),
        key_files=data.get("key_files", []),
        estimated_complexity=data.get("estimated_complexity", "medium"),
        raw_json=raw,
    )


async def plan_task(
    task: str,
    config: SecretaryConfig,
    tier: str = "high",
) -> TaskPlan | None:
    """Call Haiku to create a plan for the task.

    Returns None if planning is not appropriate or fails.
    Uses the cheapest available Claude model (Haiku) for planning.
    """
    if not _should_plan(task, tier, config):
        return None

    planner_model = config.routing.tiers.get("low", None)
    if not planner_model:
        log.warning("No low tier configured — skipping planner")
        return None

    model_name = planner_model.model
    base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")

    log.info("Planning task with %s before %s execution", model_name, tier)

    try:
        client = anthropic.Anthropic(
            base_url=base_url,
            api_key="copilot-proxy",
        )

        # Use message prefix if available
        messages: list[dict] = []
        if config.agent_prefix:
            messages.extend([
                {"role": "user", "content": "Plan the following task."},
                {"role": "assistant", "content": "I'll analyze and create a structured plan."},
            ])
        messages.append({"role": "user", "content": f"Plan this task:\n\n{task}"})

        response = await asyncio.to_thread(
            client.messages.create,
            model=model_name,
            max_tokens=_PLANNER_MAX_TOKENS,
            system=_PLANNER_SYSTEM,
            messages=messages,
        )

        # Extract text from response
        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text

        plan = _parse_plan(raw_text)
        plan.planner_model = model_name
        if hasattr(response, "usage"):
            plan.planner_input_tokens = response.usage.input_tokens
            plan.planner_output_tokens = response.usage.output_tokens

        if plan.steps:
            log.info(
                "Plan: %d steps, complexity=%s, key_files=%s",
                len(plan.steps), plan.estimated_complexity, plan.key_files,
            )
        else:
            log.warning("Planner returned no steps — will execute without plan")
            return None

        return plan

    except (anthropic.APIError, httpx.HTTPError) as e:
        log.warning("Planner call failed (%s) — executing without plan", e)
        return None
    except Exception as e:
        log.warning("Unexpected planner error (%s) — executing without plan", e)
        return None
