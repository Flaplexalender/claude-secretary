"""TextGrad-lite — textual gradient analysis for eval failure optimization.

Inspired by TextGrad (Yuksekgonul et al. 2024, published Nature 2025):
backpropagate textual feedback from LLM judges through failed eval traces
to generate targeted prompt/hint modifications.

Unlike full TextGrad (which requires a computation graph and automatic
differentiation), this "lite" version operates on eval result JSON files:

    eval results (with traces) → Haiku gradient analysis → TextualGradient
    → proposed prompt changes → autoresearch eval → keep/discard

Integration points:
    - goal_self_improve.py: use as alternative proposal source (eval-driven)
    - autoresearch.yaml: use instead of UCB1 when on plateau
    - scripts/run_textgrad.py: standalone analysis

The optimization targets (prompt variables) are:
    1. direct_agent._TASK_HINTS — per-category hints
    2. direct_agent._build_system_prompt() — universal rules
    3. oracle._ORACLE_TASK_HINTS — per-category oracle hints
    4. oracle._build_oracle_system_prompt() — oracle worker rules
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("secretary.textgrad_lite")

# ── Data classes ──────────────────────────────────────────────

@dataclass
class FailedTrace:
    """A failed eval task with full context for gradient analysis."""
    task_id: str
    task_prompt: str
    judge_criteria: str       # what the judge expected
    response_text: str        # what the agent produced
    judge_reason: str         # why the judge ruled FAIL
    turns: int
    category: str
    score_type: str           # "llm_judge", "contains", "exact_match"


@dataclass
class TextualGradient:
    """LLM-generated critique + suggested fix for a failed eval task.

    Analogous to a gradient in neural network training: points in the
    direction of improvement for a specific prompt variable.
    """
    task_id: str
    root_cause: str           # why the agent failed (1-2 sentences)
    critique: str             # what's missing/wrong in the response
    target: str               # which prompt variable to modify
    suggested_change: str     # specific text to add/modify
    confidence: float         # 0.0-1.0 (Haiku's self-assessed confidence)
    category: str             # task category or "general"


# ── Prompt for gradient generation ────────────────────────────

_GRADIENT_PROMPT = """You are analyzing why an AI agent failed an evaluation task.
Your goal: generate a "textual gradient" — a specific, actionable critique that
points toward the exact prompt change needed to fix the failure.

## Failed Task
TASK: {task_prompt}
PASS CRITERIA: {judge_criteria}
AGENT RESPONSE (truncated): {response_text}
VERDICT: FAIL
JUDGE REASON: {judge_reason}
TURNS USED: {turns}

## Optimization Targets (prompt variables you can suggest changes to)
1. "direct_agent._TASK_HINTS" — Dict mapping categories (email/calendar/file/implement/research) to brief hints appended to the system prompt. Currently ~1 sentence each.
2. "direct_agent.system_rules" — Universal rules in the system prompt: "(1) 6+ tool calls per response. (2) No text until final turn. (3) grep_search to find, file_edit to change, run_command to test."
3. "oracle._ORACLE_TASK_HINTS" — Dict mapping categories to more prescriptive approach hints for oracle ensemble workers.
4. "oracle.worker_rules" — Oracle worker rules: "(1) Call tools. (2) Be direct. (3) Parallel tool calls. (4) For explanation tasks, address every part — completeness matters."
5. "oracle.checkpoint_rules" — Checkpoint (Opus reviewer) rules.

## Rules for your analysis
- Focus on ROOT CAUSE: why did the agent's response fail the criteria?
- Be SPECIFIC: "add rule to mention X" not "improve the prompt"
- Target the CHEAPEST fix: prefer adding 1 hint over rewriting system rules
- If the failure is due to missing knowledge (not prompt issues), say so
- Consider cross-task interference: will this change hurt other tasks?

Respond with ONLY a JSON object:
{{
    "root_cause": "Why the agent failed (1-2 sentences)",
    "critique": "What's specifically missing or wrong in the response (1-2 sentences)",
    "target": "one of: direct_agent._TASK_HINTS | direct_agent.system_rules | oracle._ORACLE_TASK_HINTS | oracle.worker_rules | oracle.checkpoint_rules",
    "suggested_change": "The specific text to ADD or MODIFY in the target. Be exact — write the actual hint/rule text.",
    "confidence": 0.0-1.0,
    "category": "email | calendar | file | implement | research | general"
}}"""


# ── Core functions ────────────────────────────────────────────

def collect_failed_traces(
    eval_results_path: Path,
    eval_tasks_path: Path,
) -> list[FailedTrace]:
    """Read eval results JSON and pair with task definitions to build traces.

    Requires eval results generated with response_text and judge_reason fields
    (run_eval.py with enhanced output).
    """
    if not eval_results_path.exists():
        log.warning("Eval results not found: %s", eval_results_path)
        return []

    results_data = json.loads(eval_results_path.read_text(encoding="utf-8"))
    task_results = results_data.get("tasks", [])

    # Load task definitions for judge_criteria and prompts
    tasks_data = json.loads(eval_tasks_path.read_text(encoding="utf-8"))
    tasks_list = tasks_data.get("tasks", tasks_data) if isinstance(tasks_data, dict) else tasks_data
    task_defs = {t["id"]: t for t in tasks_list}

    traces: list[FailedTrace] = []
    for result in task_results:
        if result.get("success"):
            continue  # Only analyze failures

        task_id = result.get("id", "")
        task_def = task_defs.get(task_id, {})

        response_text = result.get("response_text", "")
        judge_reason = result.get("judge_reason", "")

        # Skip if no response text (can't analyze empty traces)
        if not response_text:
            log.debug("Skipping %s — no response_text in eval results", task_id)
            continue

        traces.append(FailedTrace(
            task_id=task_id,
            task_prompt=task_def.get("prompt", ""),
            judge_criteria=task_def.get("judge_criteria", task_def.get("expected", "")),
            response_text=response_text[:2000],  # Cap for API payload
            judge_reason=judge_reason,
            turns=result.get("turns", 0),
            category=result.get("category", ""),
            score_type=task_def.get("score_type", "contains"),
        ))

    log.info("Collected %d failed traces from %s", len(traces), eval_results_path)
    return traces


def generate_gradient(
    trace: FailedTrace,
    base_url: str,
    model: str = "claude-haiku-4.5",
) -> TextualGradient | None:
    """Feed a failed trace to Haiku to generate a textual gradient.

    Returns None if the LLM call fails or produces unparseable output.
    """
    prompt = _GRADIENT_PROMPT.format(
        task_prompt=trace.task_prompt,
        judge_criteria=trace.judge_criteria,
        response_text=trace.response_text[:1500],
        judge_reason=trace.judge_reason,
        turns=trace.turns,
    )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.0,
    }
    url = f"{base_url.rstrip('/')}/v1/chat/completions"

    try:
        resp = httpx.post(url, json=payload, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()

        # Parse JSON (handle markdown code fences)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text)

        gradient = TextualGradient(
            task_id=trace.task_id,
            root_cause=str(parsed.get("root_cause", "")),
            critique=str(parsed.get("critique", "")),
            target=str(parsed.get("target", "")),
            suggested_change=str(parsed.get("suggested_change", "")),
            confidence=float(parsed.get("confidence", 0.5)),
            category=str(parsed.get("category", "general")),
        )
        log.info(
            "Gradient for %s: target=%s confidence=%.2f",
            trace.task_id, gradient.target, gradient.confidence,
        )
        return gradient

    except Exception as exc:
        log.warning("Gradient generation failed for %s: %s", trace.task_id, exc)
        return None


def generate_gradients(
    traces: list[FailedTrace],
    base_url: str,
    model: str = "claude-haiku-4.5",
    max_gradients: int = 5,
) -> list[TextualGradient]:
    """Generate textual gradients for multiple failed traces.

    Processes up to max_gradients traces (sorted by LLM-judged tasks first,
    since those have the richest failure signals).
    """
    # Prioritize llm_judge tasks (have judge_reason) over contains/exact
    sorted_traces = sorted(
        traces,
        key=lambda t: (t.score_type != "llm_judge", t.task_id),
    )
    sorted_traces = sorted_traces[:max_gradients]

    gradients: list[TextualGradient] = []
    for trace in sorted_traces:
        gradient = generate_gradient(trace, base_url, model)
        if gradient:
            gradients.append(gradient)

    # Sort by confidence descending
    gradients.sort(key=lambda g: g.confidence, reverse=True)
    log.info("Generated %d gradients from %d traces", len(gradients), len(sorted_traces))
    return gradients


# ── Gradient → Proposal conversion ────────────────────────────

def gradient_to_proposal(gradient: TextualGradient) -> dict[str, Any]:
    """Convert a textual gradient into a self-improvement proposal dict.

    Compatible with goal_self_improve.ImprovementProposal format for
    integration with the existing self-improvement pipeline.
    """
    target_map = {
        "direct_agent._TASK_HINTS": "src/secretary/direct_agent.py",
        "direct_agent.system_rules": "src/secretary/direct_agent.py",
        "oracle._ORACLE_TASK_HINTS": "src/secretary/oracle.py",
        "oracle.worker_rules": "src/secretary/oracle.py",
        "oracle.checkpoint_rules": "src/secretary/oracle.py",
    }
    target_file = target_map.get(gradient.target, "src/secretary/direct_agent.py")

    return {
        "category": "failure-fix",
        "description": f"TextGrad: {gradient.critique[:100]}",
        "target_files": [target_file],
        "task_prompt": (
            f"Apply this TextGrad-generated prompt improvement:\n\n"
            f"## Root Cause\n{gradient.root_cause}\n\n"
            f"## Critique\n{gradient.critique}\n\n"
            f"## Target Variable\n{gradient.target}\n\n"
            f"## Suggested Change\n{gradient.suggested_change}\n\n"
            f"## Instructions\n"
            f"1. Read the target file: {target_file}\n"
            f"2. Locate the target variable: {gradient.target.split('.')[-1]}\n"
            f"3. Apply the suggested change (add/modify the hint or rule text)\n"
            f"4. Run: python -m pytest tests/ -q --tb=no\n"
            f"5. If tests pass, the change is ready for eval validation"
        ),
        "priority": gradient.confidence,
        "evidence": (
            f"Task {gradient.task_id} consistently fails eval. "
            f"Root cause: {gradient.root_cause}"
        ),
        "source": "textgrad",
        "gradient": asdict(gradient),
    }


def format_gradients_for_autoresearch(gradients: list[TextualGradient]) -> str:
    """Format gradients as a readable summary for the autoresearch campaign.

    Can be injected into the autoresearch prompt as an alternative to
    UCB1 dimension selection when on a plateau.
    """
    if not gradients:
        return "No gradients generated — all eval tasks passed or no traces available."

    lines = ["## TextGrad Analysis — Targeted Improvements\n"]
    for i, g in enumerate(gradients, 1):
        lines.append(
            f"### Gradient {i}: {g.task_id} (confidence: {g.confidence:.2f})\n"
            f"- **Root cause**: {g.root_cause}\n"
            f"- **Critique**: {g.critique}\n"
            f"- **Target**: `{g.target}`\n"
            f"- **Suggested change**: {g.suggested_change}\n"
        )

    lines.append(
        "\n## Recommendation\n"
        "Apply Gradient 1 (highest confidence). Modify the target variable "
        "with the suggested change, then run eval 3x to validate."
    )
    return "\n".join(lines)


# ── Full analysis pipeline ────────────────────────────────────

def run_textgrad_analysis(
    base_url: str,
    eval_results_path: Path,
    eval_tasks_path: Path,
    model: str = "claude-haiku-4.5",
    max_gradients: int = 5,
) -> list[TextualGradient]:
    """Full pipeline: collect failed traces → generate gradients.

    Returns list of TextualGradient objects sorted by confidence.
    """
    traces = collect_failed_traces(eval_results_path, eval_tasks_path)
    if not traces:
        log.info("No failed traces found — all tasks passed")
        return []

    return generate_gradients(traces, base_url, model, max_gradients)


def save_gradients(gradients: list[TextualGradient], path: Path) -> None:
    """Save gradients to JSON for later use."""
    data = [asdict(g) for g in gradients]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.info("Saved %d gradients to %s", len(gradients), path)


def load_gradients(path: Path) -> list[TextualGradient]:
    """Load gradients from JSON."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [TextualGradient(**g) for g in data]
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("Failed to load gradients from %s: %s", path, exc)
        return []
