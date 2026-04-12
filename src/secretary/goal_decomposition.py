"""Sub-Goal Decomposition Engine — bridge between strategic goals and executable steps.

The missing link between "having goals" and "actualising them":
    goals.yaml → goal planner → **decomposition** → step-by-step execution

Research basis:
- Weng (2023): "LLMs struggle to adjust plans when faced with unexpected errors"
  The agent needs ordered, verifiable steps — not one-shot mega-tasks.
- Kambhampati (ICML 2024): Quantitative verification needed at EACH step,
  not just at the goal level.
- Anthropic "Building Effective Agents": "Ground truth from environment at
  each step to assess progress."
- HuggingGPT (Shen 2023): four-stage pipeline with dependency tracking.

Architecture:
    sub-goal (not-started)  →  Haiku decomposition  →  step_plans stored in
    goal_state.json  →  each watcher cycle picks next pending step  →
    step becomes a campaign task  →  result recorded  →  on completion
    sub-goal marked done.

    goal_state.json["step_plans"] = {
        "<sub_goal_id>": {
            "goal_id": str,
            "steps": [
                {
                    "step_id": "<sub_goal_id>.1",
                    "action": "Analyse run_log data ...",
                    "verification": "data/router_analysis.json exists ...",
                    "tier": "low|medium|high",
                    "status": "pending|completed|failed|skipped",
                    "result": str | None,
                    "ts": str | None,
                }
            ],
            "created": str,    # ISO timestamp
            "completed": bool,
        }
    }
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import anthropic

from .config import SecretaryConfig
from .goal_expectations import parse_assertions

log = logging.getLogger("secretary.goal_decomposition")

# ---------------------------------------------------------------------------
# Investigation-skip: similarity threshold for Rule 7 bypass
# ---------------------------------------------------------------------------

#: If the current task description is >= this similar to the last-cycle task,
#: skip the mandatory investigation step to break the infinite re-diagnosis loop.
_INVESTIGATION_SIMILARITY_THRESHOLD: float = 0.90


def prior_investigation_similarity(current_task: str, last_cycle_task: str) -> float:
    """Return a [0, 1] similarity score between *current_task* and *last_cycle_task*.

    Uses :func:`difflib.SequenceMatcher` on the lowercased, whitespace-normalised
    strings so that trivial formatting differences do not lower the score.

    A score >= :data:`_INVESTIGATION_SIMILARITY_THRESHOLD` (0.90) means the agent
    has already diagnosed this task in the previous cycle and should skip straight
    to mutation sub-tasks instead of re-running analysis.

    Examples::

        >>> prior_investigation_similarity("fix Rule 7 loop", "fix Rule 7 loop")
        1.0
        >>> prior_investigation_similarity("analyse billing.py", "check billing.py")
        # typically 0.80–0.92 depending on exact wording
    """
    if not current_task or not last_cycle_task:
        return 0.0
    a = " ".join(current_task.lower().split())
    b = " ".join(last_cycle_task.lower().split())
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Mutation-step enforcement
# ---------------------------------------------------------------------------
_MUTATION_KEYWORDS: frozenset = frozenset({
    "file_write", "file_edit", "run_command", "run_python",
    "write", "edit", "create", "implement", "add", "fix", "patch",
    "modify", "refactor", "update", "delete", "remove", "migrate",
    "generate", "build", "install", "configure", "deploy",
})


def _has_mutation_step(steps):
    """Return True if at least one step contains a mutation keyword."""
    for step in steps:
        text = (step.get("action", "") + " " + step.get("verification", "")).lower()
        if any(kw in text for kw in _MUTATION_KEYWORDS):
            return True
    return False

DECOMP_MODEL = "claude-haiku-4.5"
DECOMP_MAX_TOKENS = 2048
MAX_STEPS_PER_PLAN = 7

_DECOMP_SYSTEM = """\
You are a task decomposition engine for an AI secretary.  Given a sub-goal \
(part of a larger strategic goal), break it into 3-7 concrete, ordered steps \
that an LLM agent with file and tool access can execute one at a time.

Rules:
1. Each step must be a SINGLE, focused action — not a compound task.
2. Steps are executed in order.  A step may depend on the output of prior steps.
3. Each step needs a verification criterion — how to confirm it succeeded.
4. Assign a tier (low/medium/high) based on complexity:
   - low: simple analysis, reading files, formatting data
   - medium: implementing code changes, writing tests, calling APIs
   - high: complex multi-file refactoring, architecture design, debugging
5. Keep steps concrete: "Read data/run_log.jsonl and compute per-tier cost averages" \
not "Analyse the data".
6. The agent has access to: file_read, file_write, file_edit, grep_search, \
run_command (shell), run_python (Python scripts), Gmail/Calendar tools.
7. If this sub-goal has NOT been investigated in the immediately preceding cycle, \
make the first step an investigation/analysis step (understand before acting).  \
If prior investigation already covers this task (similarity >= 0.90), skip the \
investigation step and begin directly with mutation/implementation sub-tasks.
8. PLATFORM: This runs on Windows. Use PowerShell or Python, NOT bash/grep/sed/awk. \
Use Windows paths (backslash or forward slash), not /tmp or Unix paths. \
For searching files, use run_python with pathlib or grep_search tool, not shell grep.

Respond with ONLY JSON (no markdown fences):
{
  "steps": [
    {
      "action": "...",
      "verification": "...",
      "tier": "low|medium|high",
      "preconditions": [
        {"type": "file_exists", "path": "relative/path"},
        {"type": "json_field", "path": "data/state.json", "field": "status", "value": "ready"}
      ],
      "expected_effects": [
        {"type": "file_exists", "path": "output/result.json"},
        {"type": "file_contains", "path": "config.yaml", "pattern": "enabled: true"}
      ]
    }
  ],
  "rationale": "Brief explanation of the decomposition approach."
}

Assertion types for preconditions and expected_effects:
- file_exists: {"type": "file_exists", "path": "..."}
- file_contains: {"type": "file_contains", "path": "...", "pattern": "substring"}
- json_field: {"type": "json_field", "path": "...", "field": "dotted.path", "value": expected}

Preconditions are checked BEFORE the step runs (skip if unmet).
Expected effects are checked AFTER the step runs against the real filesystem.
Only include assertions where a deterministic check is meaningful. \
Omit preconditions/expected_effects if no useful check exists for a step.\
"""


def _build_decomp_prompt(
    sub_goal: dict[str, Any],
    parent_goal: dict[str, Any],
    context: str,
) -> str:
    """Build the user prompt for sub-goal decomposition."""
    parts: list[str] = []

    parts.append("## Sub-Goal to Decompose")
    parts.append(f"ID: {sub_goal.get('id', '?')}")
    parts.append(f"Description: {sub_goal.get('description', '')}")

    parts.append("\n## Parent Goal")
    parts.append(f"ID: {parent_goal.get('id', '?')}")
    parts.append(f"Description: {parent_goal.get('description', '')}")
    parts.append(f"Success criteria: {parent_goal.get('success_criteria', '')}")

    # Sibling sub-goals for context (what's done, what's pending)
    siblings = parent_goal.get("sub_goals", [])
    if siblings:
        parts.append("\n## Sibling Sub-Goals (for context)")
        for sg in siblings:
            sg_id = sg.get("id", "?")
            sg_desc = sg.get("description", "")
            sg_status = sg.get("status", "not-started")
            marker = "DONE" if sg_status == "done" else sg_status.upper()
            parts.append(f"  - [{sg_id}] {sg_desc} — {marker}")

    if context:
        parts.append(f"\n## Additional Context\n{context}")

    parts.append("\n## Instructions")
    parts.append(
        "Break this sub-goal into 3-7 concrete, ordered steps. "
        "Each step should be independently executable and verifiable. "
        "Start with investigation, end with verification/testing. "
        "IMPORTANT: This runs on Windows — use PowerShell/Python, not bash/grep/sed."
    )

    return "\n".join(parts)


def _parse_decomp_response(text: str) -> dict[str, Any]:
    """Parse decomposition JSON response.

    Returns empty steps list on parse failure.
    """
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    if not text:
        return {"steps": [], "rationale": ""}

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try extracting JSON object with string-aware brace matching
        start = text.find("{")
        if start >= 0:
            depth = 0
            in_string = False
            escape = False
            end = start
            for i in range(start, len(text)):
                c = text[i]
                if escape:
                    escape = False
                    continue
                if c == "\\":
                    escape = True
                    continue
                if c == '"' and not escape:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if depth == 0 and end > start:
                try:
                    data = json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(data, dict) and "steps" in data:
                        # Successfully extracted — fall through
                        pass
                    else:
                        data = None
            else:
                data = None
        else:
            data = None

        if data is None:
            # Truncated JSON recovery: close open brackets/braces/strings
            if start is not None and start >= 0:
                fragment = text[start:]
                # Close any open string
                quote_count = fragment.count('"') - fragment.count('\\"')
                if quote_count % 2 == 1:
                    fragment += '"'
                # Close open brackets and braces
                open_braces = fragment.count("{") - fragment.count("}")
                open_brackets = fragment.count("[") - fragment.count("]")
                fragment += "]" * max(open_brackets, 0)
                fragment += "}" * max(open_braces, 0)
                try:
                    data = json.loads(fragment)
                    if isinstance(data, dict) and "steps" in data:
                        log.info("Decomposition: recovered truncated JSON (%d steps)", len(data.get("steps", [])))
                    else:
                        data = None
                except json.JSONDecodeError:
                    data = None

        if data is None:
            log.warning("Decomposition: failed to parse response: %s", text[:200])
            return {"steps": [], "rationale": ""}

    if not isinstance(data, dict):
        return {"steps": [], "rationale": ""}

    raw_steps = data.get("steps", [])
    if not isinstance(raw_steps, list):
        return {"steps": [], "rationale": ""}

    valid_tiers = {"low", "medium", "high"}
    steps: list[dict[str, Any]] = []
    for item in raw_steps[:MAX_STEPS_PER_PLAN]:
        if not isinstance(item, dict):
            continue
        action = item.get("action", "")
        if not action or not isinstance(action, str):
            continue
        verification = item.get("verification", "")
        tier = item.get("tier", "medium")
        if tier not in valid_tiers:
            tier = "medium"
        step: dict[str, Any] = {
            "action": action,
            "verification": str(verification),
            "tier": tier,
        }
        # Layer 27: parse structured assertions (silently drops invalid entries)
        raw_pre = item.get("preconditions")
        if isinstance(raw_pre, list) and raw_pre:
            parsed = parse_assertions(raw_pre)
            if parsed:
                step["preconditions"] = parsed
        raw_eff = item.get("expected_effects")
        if isinstance(raw_eff, list) and raw_eff:
            parsed = parse_assertions(raw_eff)
            if parsed:
                step["expected_effects"] = parsed
        steps.append(step)

    rationale = str(data.get("rationale", ""))[:300]
    return {"steps": steps, "rationale": rationale}


async def decompose_sub_goal(
    sub_goal: dict[str, Any],
    parent_goal: dict[str, Any],
    config: SecretaryConfig,
    context: str = "",
    last_cycle_task: str = "",
) -> list[dict[str, Any]]:
    """Decompose a sub-goal into ordered, executable steps.

    Returns a list of step dicts with: step_id, action, verification,
    tier, status='pending'.

    Uses Haiku (0.33x) for cost efficiency.

    Args:
        sub_goal: The sub-goal dict to decompose.
        parent_goal: The parent goal dict for context.
        config: Secretary configuration.
        context: Optional extra context string for the prompt.
        last_cycle_task: Description of the task attempted in the immediately
            preceding cycle.  When the similarity between this and the current
            sub-goal description is >= ``_INVESTIGATION_SIMILARITY_THRESHOLD``
            (0.90), the mandatory investigation-first step (Rule 7) is skipped
            and the LLM is instructed to generate mutation/implementation steps
            directly, breaking the infinite re-diagnosis loop.
    """
    from .direct_agent import _build_client, AGENT_PREFIX

    # ── Rule 7 guard: skip investigation if prior cycle already covered this ──
    current_description = sub_goal.get("description", sub_goal.get("title", ""))
    skip_investigation = (
        bool(last_cycle_task)
        and prior_investigation_similarity(current_description, last_cycle_task)
        >= _INVESTIGATION_SIMILARITY_THRESHOLD
    )
    if skip_investigation:
        log.info(
            "Rule 7 skip: similarity=%.2f >= %.2f for sub-goal %s — "
            "proceeding directly to mutation sub-tasks",
            prior_investigation_similarity(current_description, last_cycle_task),
            _INVESTIGATION_SIMILARITY_THRESHOLD,
            sub_goal.get("id", "unknown"),
        )
        # Append an explicit instruction so the LLM skips the analysis step
        context = (
            context
            + "\n\n[SYSTEM NOTE] Prior cycle already investigated this task. "
            "Skip Rule 7 investigation step. Begin immediately with concrete "
            "mutation/implementation sub-tasks (file edits, code changes, etc.)."
        ).strip()

    prompt = _build_decomp_prompt(sub_goal, parent_goal, context)
    client = _build_client(config)

    messages: list[dict[str, Any]] = list(AGENT_PREFIX) + [
        {"role": "user", "content": prompt},
    ]

    model = DECOMP_MODEL

    start = time.monotonic()
    try:
        response = await asyncio.to_thread(
            _call_decomp, client, messages, model,
        )
    except Exception as e:
        log.warning("Sub-goal decomposition failed for %s: %s", sub_goal.get("id"), e)
        return []
    elapsed = time.monotonic() - start

    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
            break

    result = _parse_decomp_response(text)
    raw_steps = result.get("steps", [])
    rationale = result.get("rationale", "")

    sub_goal_id = sub_goal.get("id", "unknown")
    steps = []
    for i, raw in enumerate(raw_steps, 1):
        step_entry: dict[str, Any] = {
            "step_id": f"{sub_goal_id}.{i}",
            "action": raw["action"],
            "verification": raw["verification"],
            "tier": raw["tier"],
            "status": "pending",
            "result": None,
            "ts": None,
        }
        # Layer 27: preserve structured assertions
        if "preconditions" in raw:
            step_entry["preconditions"] = raw["preconditions"]
        if "expected_effects" in raw:
            step_entry["expected_effects"] = raw["expected_effects"]
        steps.append(step_entry)

    log.info(
        "Decomposed sub-goal %s → %d steps (%.1fs): %s",
        sub_goal_id, len(steps), elapsed, rationale[:100],
    )
    return steps


def _call_decomp(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
    model: str,
) -> anthropic.types.Message:
    """Synchronous Haiku call — runs in thread via asyncio.to_thread."""
    with client.messages.stream(
        model=model,
        max_tokens=DECOMP_MAX_TOKENS,
        system=_DECOMP_SYSTEM,
        messages=messages,
    ) as stream:
        return stream.get_final_message()


# ---------------------------------------------------------------------------
# Step plan management — works with goal_state.json
# ---------------------------------------------------------------------------


def get_step_plans(state: dict[str, Any]) -> dict[str, Any]:
    """Get all step plans from goal state."""
    return state.get("step_plans", {})


def save_step_plan(
    state: dict[str, Any],
    sub_goal_id: str,
    goal_id: str,
    steps: list[dict[str, Any]],
) -> None:
    """Store a new step plan in goal state."""
    plans = state.setdefault("step_plans", {})
    plans[sub_goal_id] = {
        "goal_id": goal_id,
        "steps": steps,
        "created": datetime.now(timezone.utc).isoformat(),
        "completed": False,
    }


def get_next_step(state: dict[str, Any], sub_goal_id: str) -> dict[str, Any] | None:
    """Get the next pending step whose dependencies are met.

    Steps are ordered — a step is ready when all prior steps are completed.
    Returns None if no step is ready or if plan doesn't exist.
    """
    plans = state.get("step_plans", {})
    plan = plans.get(sub_goal_id)
    if not plan or plan.get("completed") or plan.get("blocked"):
        return None

    steps = plan.get("steps", [])
    for i, step in enumerate(steps):
        if step.get("status") == "pending":
            # Check that all prior steps are completed
            prior_ok = all(
                s.get("status") == "completed" for s in steps[:i]
            )
            if prior_ok:
                return step
            # If a prior step is failed, this step is blocked
            return None
    return None


def record_step_result(
    state: dict[str, Any],
    sub_goal_id: str,
    step_id: str,
    success: bool,
    evidence: str = "",
) -> None:
    """Record the outcome of a step execution.

    Updates step status and checks whether the whole plan is complete.
    """
    plans = state.get("step_plans", {})
    plan = plans.get(sub_goal_id)
    if not plan:
        return

    for step in plan.get("steps", []):
        if step.get("step_id") == step_id:
            step["status"] = "completed" if success else "failed"
            step["result"] = evidence[:500] if evidence else None
            step["ts"] = datetime.now(timezone.utc).isoformat()
            break

    # Check if all steps are completed → mark plan completed
    steps = plan.get("steps", [])
    if steps and all(s.get("status") == "completed" for s in steps):
        plan["completed"] = True

        # Mutation-step enforcement: if plan is read-only, inject a concrete write step
        if steps and not _has_mutation_step(steps):
            import logging as _logging
            _logging.getLogger("secretary.goal_decomposition").warning(
                "Sub-goal %s got a read-only plan (%d steps) -- injecting mutation step.",
                sub_goal_id, len(steps),
            )
            steps.append({
                "step_id": f"{sub_goal_id}.{len(steps) + 1}",
                "action": (
                    f"Implement findings: use file_edit or file_write to apply at least one "
                    f"concrete change advancing sub-goal '{sub_goal_id}'."
                ),
                "verification": (
                    "Confirm file was modified and run 'python -m pytest tests/ -x -q' passes."
                ),
                "tier": steps[-1].get("tier", "medium"),
                "status": "pending",
                "result": None,
                "ts": None,
            })
            plan["steps"] = steps

        log.info("Step plan for %s completed — all %d steps done", sub_goal_id, len(steps))


def step_to_task(
    step: dict[str, Any],
    sub_goal_id: str,
    goal_id: str,
    *,
    tier_override: str | None = None,
) -> dict[str, Any]:
    """Convert a step plan entry into a task dict for the watcher.

    The task prompt includes both the action and the verification criteria
    so the executing agent knows what "success" looks like.
    """
    action = step.get("action", "")
    verification = step.get("verification", "")
    step_id = step.get("step_id", "")

    prompt = action
    if verification:
        prompt += (
            f"\n\nVerification: after completing this step, confirm: {verification}"
        )

    # Apply tier override from capability-failure auto-escalation
    tier = step.get("tier", "medium")
    _tier_order = {"low": 0, "medium": 1, "high": 2}
    if tier_override and _tier_order.get(tier_override, 0) > _tier_order.get(tier, 0):
        tier = tier_override

    return {
        "prompt": prompt,
        "tier": tier,
        "priority": 2,  # Steps are high-priority (goal-driven, ordered)
        "goal_id": goal_id,
        "source": "goals",
        "id": f"step-{step_id}",
        "_step_id": step_id,
        "_sub_goal_id": sub_goal_id,
        "_action": action,
        "_verification": verification,
        "_preconditions": step.get("preconditions", []),
        "_expected_effects": step.get("expected_effects", []),
    }


def find_decomposable_sub_goals(
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
    step_plans: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Find sub-goals that need decomposition.

    A sub-goal needs decomposition if:
    1. Its effective status is 'not-started' or 'in-progress' (not 'done' or 'blocked')
    2. It doesn't already have a step plan
    3. Its parent goal is not done

    Returns list of (sub_goal_dict, parent_goal_dict) tuples.
    """
    results = []
    for goal in goals:
        goal_status = goal.get("status", "not-started")
        if goal_status == "done":
            continue

        goal_id = goal.get("id", "")
        for sg in goal.get("sub_goals", []):
            sg_id = sg.get("id", "")
            # Get effective status (state override takes precedence)
            override = sub_goal_overrides.get(sg_id)
            effective_status = override["status"] if override else sg.get("status", "not-started")

            if effective_status in ("done", "blocked"):
                continue
            if sg_id in step_plans:
                continue  # Already has a plan

            results.append((sg, goal))

    return results


def format_step_plans_section(
    state: dict[str, Any],
    goals: list[dict[str, Any]],
) -> str:
    """Render active step plans for the planner prompt.

    Shows the planner which sub-goals have been decomposed and what
    step is currently being worked on — so it doesn't generate duplicate tasks.
    """
    plans = state.get("step_plans", {})
    if not plans:
        return ""

    parts: list[str] = ["## Active Step Plans"]
    parts.append(
        "(Sub-goals decomposed into ordered steps. "
        "Do NOT generate tasks for sub-goals that already have active step plans.)"
    )

    for sg_id, plan in sorted(plans.items()):
        if plan.get("completed"):
            continue  # Skip completed plans
        steps = plan.get("steps", [])
        if not steps:
            continue

        done_count = sum(1 for s in steps if s.get("status") == "completed")
        total = len(steps)

        if plan.get("blocked"):
            parts.append(f"\n**{sg_id}** BLOCKED ({done_count}/{total} steps done)")
            reason = plan.get("block_reason", "")[:100]
            if reason:
                parts.append(f"  Reason: {reason}")
            continue

        current = get_next_step(state, sg_id)
        current_desc = current["action"][:80] if current else "blocked"

        parts.append(f"\n**{sg_id}** ({done_count}/{total} steps done)")
        parts.append(f"  Current: {current_desc}")

        # Show step statuses compactly
        for step in steps:
            status_icon = {
                "completed": "done",
                "failed": "FAILED",
                "pending": "...",
                "skipped": "skip",
            }.get(step.get("status", "pending"), "?")
            parts.append(f"  {step.get('step_id', '?')}: [{status_icon}] {step.get('action', '')[:60]}")

    return "\n".join(parts) if len(parts) > 2 else ""
