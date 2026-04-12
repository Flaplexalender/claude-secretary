"""Goal Planner — proactive long-horizon goal pursuit.

Complement to the OODA loop (reactive: events → tasks).
The Goal Planner is proactive: goals → progress assessment → tasks.

Architecture:
    goals.yaml  →  GoalStore (read)  →  Haiku review  →  proactive tasks
                                              ^            injected into watcher
                                        context from:
                                        - goal hierarchy + status
                                        - recent run_log activity
                                        - memory highlights
                                        - goal_state.json (progress tracking)

The planner runs on a configurable interval (default: every 8 hours).
It uses Haiku (0.33x) for cost efficiency — same as OODA.

Separation of concerns:
    goals.yaml       = goal DEFINITIONS (human-authored, version-controlled)
    goal_state.json  = goal STATE (machine-updated, tracks progress + last review)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import yaml

from .config import SecretaryConfig
from .goal_decomposition import (
    decompose_sub_goal,
    find_decomposable_sub_goals,
    format_step_plans_section,
    get_next_step,
    get_step_plans,
    record_step_result,
    save_step_plan,
    step_to_task,
)
from .goal_dependencies import (
    filter_blocked_sub_goals,
    format_dependency_section,
)
from .goal_progress import compute_progress, format_progress_section, record_snapshot
from .memory import MemoryStore
from .run_log import RunLog

log = logging.getLogger("secretary.goals")

PLANNER_MODEL = "claude-haiku-4.5"
PLANNER_MAX_TOKENS = 1536

_GOAL_PLANNER_SYSTEM = """\
You are the strategic planner for an AI secretary.  Your job is to review \
long-horizon goals, assess progress, and generate proactive tasks that advance \
the most important or neglected goals.

This is NOT reactive planning (that's handled by OODA).  You focus on \
PROACTIVE work — things that need doing even when nothing triggered them.

Rules:
1. Generate tasks that ADVANCE goals, not just react to events.
2. Prioritize stalled or neglected goals (long time since progress).
3. Consider dependencies between sub-goals (don't work on X if Y isn't done).
4. Each task: prompt (specific, actionable), tier (low/medium/high), priority (1-5), goal_id.
5. Don't generate tasks for goals or sub-goals already marked "done".
6. Maximum 3 tasks per review — proactive work should be focused, not scattered.
7. If all goals are on track and nothing is stalled, return empty tasks list.
8. Also return goal_updates: sub-goal status changes you can infer from recent activity.
9. Pay special attention to the **Goal Progress Metrics** section — it shows \
numerical completion, task success rates, and stall warnings.  Prioritize STALLED goals.
10. If a sub-goal has an **Active Step Plan**, do NOT generate tasks for it — \
steps are being executed automatically.  Focus tasks on sub-goals WITHOUT plans.
11. ANTI-STAGNATION: If reflections mention "read-only", "investigation only", \
"analysis without execution", or similar — you MUST generate IMPLEMENTATION tasks \
that use file_write/file_edit/run_command to make real code changes.  Investigation \
without implementation is NOT progress.  Every task prompt must specify what files \
to create or modify.
12. Task prompts MUST be specific enough that an agent can execute them with \
file tools.  Bad: "Investigate X".  Good: "Edit src/secretary/X.py to add Y \
method that does Z, then write a test in tests/test_X.py".
13. This is a WINDOWS system (PowerShell).  Never use bash/Linux commands. \
Use relative paths from the project root for src/secretary/ source, \
tests/ for tests, campaigns/ for YAML tasks.

Respond with ONLY JSON (no markdown fences):
{
  "tasks": [
    {"prompt": "...", "tier": "...", "priority": N, "goal_id": "..."}
  ],
  "goal_updates": [
    {"sub_goal_id": "...", "new_status": "done|in-progress|blocked", "evidence": "..."}
  ],
  "proposed_goals": [
    {"id": "kebab-case-id", "description": "...", "success_criteria": "measurable outcome", "priority": 3}
  ],
  "reasoning": "Brief explanation of what you assessed and why these tasks matter."
}\
"""


class GoalStore:
    """Loads goal definitions from YAML, manages state from JSON."""

    def __init__(self, goals_file: Path, state_file: Path):
        self.goals_file = goals_file
        self.state_file = state_file
        self.goals: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {
            "last_reviewed": None,
            "sub_goal_status": {},
            "progress_notes": [],
        }

    def load(self) -> None:
        """Load goals from YAML and state from JSON."""
        if self.goals_file.exists():
            with open(self.goals_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self.goals = data.get("goals", [])
        else:
            log.warning("Goals file not found: %s", self.goals_file)
            self.goals = []

        if self.state_file.exists():
            with open(self.state_file, encoding="utf-8") as f:
                self._state = json.load(f)

    def save_state(self) -> None:
        """Atomic write of state file."""
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, indent=2)
            f.flush()
        tmp.replace(self.state_file)

    @property
    def last_reviewed(self) -> str | None:
        return self._state.get("last_reviewed")

    def mark_reviewed(self) -> None:
        self._state["last_reviewed"] = datetime.now(timezone.utc).isoformat()

    def _valid_sub_goal_ids(self) -> set[str]:
        """Return the set of goal and sub-goal IDs defined in goals.yaml."""
        ids: set[str] = set()
        for g in self.goals:
            ids.add(g["id"])
            for sg in g.get("sub_goals", []):
                ids.add(sg["id"])
        return ids

    def apply_updates(self, updates: list[dict[str, Any]]) -> None:
        """Apply sub-goal status updates from the planner.

        Only accepts sub_goal_ids that exist in goals.yaml to prevent
        the planner from creating orphan entries with invented keys.
        """
        valid = self._valid_sub_goal_ids()
        for update in updates:
            sub_id = update.get("sub_goal_id", "")
            new_status = update.get("new_status", "")
            evidence = update.get("evidence", "")
            if not sub_id or new_status not in ("done", "in-progress", "blocked", "not-started"):
                continue
            if sub_id not in valid:
                log.warning("Goal update rejected — '%s' not in goals.yaml", sub_id)
                continue
            self._state["sub_goal_status"][sub_id] = {
                "status": new_status,
                "evidence": evidence,
                "updated": datetime.now(timezone.utc).isoformat(),
            }
            log.info("Goal update: %s → %s (%s)", sub_id, new_status, evidence[:80])

    def get_effective_status(self, sub_goal_id: str, yaml_status: str) -> str:
        """Get status with state overrides applied."""
        override = self._state.get("sub_goal_status", {}).get(sub_goal_id)
        if override:
            return override["status"]
        return yaml_status

    def add_progress_note(self, note: str) -> None:
        notes = self._state.setdefault("progress_notes", [])
        notes.append({
            "note": note[:300],
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        # Keep last 20 notes
        if len(notes) > 20:
            self._state["progress_notes"] = notes[-20:]

    def prune_stale_goals(self, max_blocked_days: float = 7.0) -> int:
        """Archive sub-goals that have been blocked for too long.

        Returns count of pruned goals.
        """
        sub_status = self._state.get("sub_goal_status", {})
        now = datetime.now(timezone.utc)
        pruned = 0
        to_remove: list[str] = []
        for sub_id, info in sub_status.items():
            if info.get("status") != "blocked":
                continue
            updated = info.get("updated")
            if not updated:
                continue
            try:
                updated_dt = datetime.fromisoformat(updated)
                age_days = (now - updated_dt).total_seconds() / 86400
                if age_days > max_blocked_days:
                    to_remove.append(sub_id)
            except (ValueError, TypeError):
                continue
        # Also prune stale step plans for removed goals
        step_plans = self._state.get("step_plans", {})
        for sub_id in to_remove:
            del sub_status[sub_id]
            step_plans.pop(sub_id, None)
            pruned += 1
        if pruned:
            log.info("Pruned %d stale blocked sub-goals (>%.0f days)", pruned, max_blocked_days)
        return pruned

    def prune_orphan_statuses(self) -> int:
        """Remove sub_goal_status entries that don't match any ID in goals.yaml.

        The LLM planner sometimes invents compound keys like
        ``self-improvement:goal-planner`` that aren't real sub-goal IDs.
        These orphans pollute the planner prompt and waste context.
        """
        valid = self._valid_sub_goal_ids()
        sub_status = self._state.get("sub_goal_status", {})
        orphans = [k for k in sub_status if k not in valid]
        step_plans = self._state.get("step_plans", {})
        for key in orphans:
            del sub_status[key]
            step_plans.pop(key, None)
        if orphans:
            log.info("Pruned %d orphan sub_goal_status entries: %s", len(orphans), orphans[:5])
        return len(orphans)


def is_review_due(goal_store: GoalStore, interval_hours: int) -> bool:
    """Check if enough time has passed since the last goal review."""
    last = goal_store.last_reviewed
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return True
    now = datetime.now(timezone.utc)
    elapsed_hours = (now - last_dt).total_seconds() / 3600
    return elapsed_hours >= interval_hours


def _build_goal_prompt(
    goals: list[dict[str, Any]],
    sub_goal_overrides: dict[str, Any],
    recent_log: list[dict[str, Any]],
    memory_summary: str,
    progress_notes: list[dict[str, Any]],
    reflections: list[dict[str, Any]] | None = None,
    progress_section: str = "",
    step_plans_section: str = "",
    meta_reflection_section: str = "",
    dependency_section: str = "",
) -> str:
    """Build the user prompt for the goal planner."""
    parts: list[str] = []

    # Goals hierarchy
    parts.append("## Long-Horizon Goals")
    for goal in goals:
        gid = goal.get("id", "?")
        desc = goal.get("description", "")
        status = goal.get("status", "not-started")
        priority = goal.get("priority", 5)
        criteria = goal.get("success_criteria", "")
        parts.append(f"\n### [{gid}] {desc}")
        parts.append(f"Status: {status} | Priority: {priority}")
        if criteria:
            parts.append(f"Success criteria: {criteria}")

        sub_goals = goal.get("sub_goals", [])
        if sub_goals:
            parts.append("Sub-goals:")
            for sg in sub_goals:
                sg_id = sg.get("id", "?")
                sg_desc = sg.get("description", "")
                sg_status = sg.get("status", "not-started")
                # Apply state overrides
                override = sub_goal_overrides.get(sg_id)
                if override:
                    sg_status = override.get("status", sg_status)
                parts.append(f"  - [{sg_id}] {sg_desc} — {sg_status}")

    # Recent activity
    parts.append("\n## Recent Activity (last 24h)")
    if recent_log:
        for entry in recent_log[:10]:
            status = "PASS" if entry.get("success") else "FAIL"
            parts.append(f"- [{status}] {entry.get('task', '')[:120]}")
    else:
        parts.append("- No recent activity.")

    # Failure pattern summary for autonomous goal proposal
    if recent_log:
        failures = [e for e in recent_log if not e.get("success")]
        if failures:
            parts.append(f"\n## Failure Patterns ({len(failures)} failures in recent log)")
            # Categorize by error type
            error_cats: dict[str, int] = {}
            for f in failures:
                err = f.get("error", f.get("task", ""))[:80]
                # Extract first meaningful category word
                cat = "unknown"
                for keyword in ("timeout", "budget", "auth", "tool", "parse", "import",
                                "network", "permission", "file", "api", "validation"):
                    if keyword in err.lower():
                        cat = keyword
                        break
                error_cats[cat] = error_cats.get(cat, 0) + 1
            for cat, count in sorted(error_cats.items(), key=lambda x: -x[1]):
                parts.append(f"- {cat}: {count} occurrence{'s' if count > 1 else ''}")
            parts.append(
                "Consider proposing goals to address recurring failure patterns."
            )

    # Progress notes from previous reviews
    if progress_notes:
        parts.append("\n## Previous Review Notes")
        for note in progress_notes[-5:]:
            parts.append(f"- {note.get('note', '')}")

    # Memory
    if memory_summary:
        parts.append(f"\n## Key Memory\n{memory_summary[:500]}")

    # Progress metrics (quantitative loss signal)
    if progress_section:
        parts.append("\n" + progress_section)

    # Active step plans (sub-goals under decomposition)
    if step_plans_section:
        parts.append("\n" + step_plans_section)

    # Reflections from the feedback loop (Reflexion-style verbal RL)
    if reflections:
        parts.append("\n## Reflections from Previous Task Outcomes")
        parts.append(
            "(These are insights from analyzing how your last batch of tasks performed. "
            "Use them to generate BETTER tasks this time.)"
        )
        for ref in reflections[-3:]:
            parts.append(f"\n**Reflection**: {ref.get('reflection', '')[:300]}")
            adjustments = ref.get("strategy_adjustments", [])
            if adjustments:
                parts.append("Strategy adjustments:")
                for adj in adjustments[:3]:
                    parts.append(f"  - {adj[:200]}")
            patterns = ref.get("patterns", {})
            working = patterns.get("working", [])
            failing = patterns.get("failing", [])
            if working:
                parts.append("What's working: " + "; ".join(w[:100] for w in working[:3]))
            if failing:
                parts.append("What's failing: " + "; ".join(f[:100] for f in failing[:3]))

    # Cross-goal meta-reflection: patterns across all goals
    if meta_reflection_section:
        parts.append("\n" + meta_reflection_section)

    # Sub-goal dependency status (Layer 25)
    if dependency_section:
        parts.append("\n" + dependency_section)

    # Stagnation detection: inject strong signal when goals are stuck in investigation
    stagnation_keywords = [
        "read-only", "investigation", "analysis without",
        "no code changes", "no implementation", "zero executable",
        "0 executable", "zero actionable", "0 actionable",
    ]
    stagnant_goals: list[str] = []
    for sg_id, sg_data in sub_goal_overrides.items():
        if not isinstance(sg_data, dict):
            continue
        evidence = sg_data.get("evidence", "").lower()
        status = sg_data.get("status", "")
        if status in ("in-progress", "blocked") and any(kw in evidence for kw in stagnation_keywords):
            stagnant_goals.append(sg_id)
    if stagnant_goals:
        parts.append("\n## ⚠ STAGNATION ALERT")
        parts.append(
            f"The following sub-goals are stuck in investigation-only mode with "
            f"zero code changes: {', '.join(stagnant_goals)}. "
            f"You MUST generate IMPLEMENTATION tasks (using file_edit/file_write) "
            f"for at least one of these. DO NOT generate more read/analyze tasks."
        )

    parts.append("\n## Your Assessment")
    parts.append(
        "Review the goals above and decide what proactive tasks to generate. "
        "Focus on goals that are stalled, neglected, or have unmet dependencies. "
        "Also identify any sub-goal status changes based on recent activity."
    )

    return "\n".join(parts)


async def run_goal_review(
    goal_store: GoalStore,
    run_log: RunLog,
    memory: MemoryStore,
    config: SecretaryConfig,
) -> list[dict[str, Any]]:
    """Run a strategic goal review.  Returns proactive task dicts.

    Each dict has: ``prompt``, ``tier``, ``priority``, ``goal_id``,
    and ``source: "goals"``.
    """
    if not goal_store.goals:
        log.debug("Goal review: no goals defined, skipping")
        return []

    # Gather context
    recent = run_log.recent(10)
    recent_dicts = [
        {"task": e.task, "success": e.success, "tier": e.tier}
        for e in recent
    ]
    mem_summary = ""
    if memory.short:
        mem_summary = "\n".join(
            f"- {m}" for m in list(memory.short)[-5:]
        )
    progress_notes = goal_store._state.get("progress_notes", [])
    reflections = goal_store._state.get("reflections", [])

    # Compute quantitative progress metrics
    snapshots = goal_store._state.get("progress_snapshots", [])
    progress = compute_progress(
        goal_store.goals,
        goal_store._state.get("sub_goal_status", {}),
        run_log,
        snapshots,
    )
    progress_section = format_progress_section(progress)

    # Step plans section — shows decomposed sub-goals so planner doesn't duplicate
    step_plans_section = format_step_plans_section(goal_store._state, goal_store.goals)

    # Cross-goal meta-reflection section
    from .goal_meta_reflection import format_meta_for_prompt

    meta_reflection_section = format_meta_for_prompt(goal_store._state)

    # Sub-goal dependency section (Layer 25)
    _overrides_for_deps = goal_store._state.get("sub_goal_status", {})
    dependency_section = format_dependency_section(goal_store.goals, _overrides_for_deps)

    prompt = _build_goal_prompt(
        goal_store.goals,
        goal_store._state.get("sub_goal_status", {}),
        recent_dicts,
        mem_summary,
        progress_notes,
        reflections=reflections,
        progress_section=progress_section,
        step_plans_section=step_plans_section,
        meta_reflection_section=meta_reflection_section,
        dependency_section=dependency_section,
    )

    # Build the Anthropic client
    from .direct_agent import _build_client, AGENT_PREFIX

    client = _build_client(config)

    messages: list[dict[str, Any]] = list(AGENT_PREFIX) + [
        {"role": "user", "content": prompt},
    ]

    model = getattr(config, "_goal_model", None) or PLANNER_MODEL

    start = time.monotonic()
    try:
        response = await asyncio.to_thread(
            _call_goal_planner, client, messages, model,
        )
    except Exception as e:
        log.warning("Goal planner call failed: %s", e)
        return []
    elapsed = time.monotonic() - start

    # Extract text from response
    text = ""
    for block in response.content:
        if hasattr(block, "text"):
            text = block.text
            break

    result = _parse_goal_response(text)
    tasks = result.get("tasks", [])
    updates = result.get("goal_updates", [])
    reasoning = result.get("reasoning", "")

    # Apply status updates
    if updates:
        goal_store.apply_updates(updates)

    # Process proposed goals (autonomous goal creation)
    proposed = result.get("proposed_goals", [])
    if proposed:
        from .goal_authoring import propose_goal, MAX_GOALS_PER_CYCLE
        for pg in proposed[:MAX_GOALS_PER_CYCLE]:
            ok, msg = propose_goal(
                goals_file=goal_store._goals_file,
                goal_id=pg.get("id", ""),
                description=pg.get("description", ""),
                success_criteria=pg.get("success_criteria", ""),
                priority=pg.get("priority", 4),
                sub_goals=pg.get("sub_goals"),
            )
            if ok:
                log.info("Goal authoring: %s", msg)
                # Reload goals to include the new one
                goal_store.load()
            else:
                log.debug("Goal authoring skipped: %s", msg)

    # Record review
    goal_store.mark_reviewed()
    if reasoning:
        goal_store.add_progress_note(reasoning[:300])
    # Record progress snapshot for trend tracking
    record_snapshot(goal_store._state, progress)

    # Decompose sub-goals that need step plans.
    # This runs AFTER the review so status updates are applied first.
    try:
        _overrides = goal_store._state.get("sub_goal_status", {})
        decomposable = find_decomposable_sub_goals(
            goal_store.goals,
            _overrides,
            get_step_plans(goal_store._state),
        )
        # Layer 25: filter out sub-goals whose depends_on prerequisites aren't met
        decomposable = filter_blocked_sub_goals(
            decomposable, goal_store.goals, _overrides,
        )
        # Derive last_cycle_task from the most recent run-log entry so that
        # decompose_sub_goal can skip the Rule 7 investigation step when the
        # sub-goal description closely matches what was just analysed.
        _last_cycle_task: str = ""
        if recent:
            _last_entry = recent[-1] if isinstance(recent, list) else None
            if _last_entry is not None:
                _last_cycle_task = getattr(_last_entry, "task", "") or ""
        # Decompose up to 2 sub-goals per review (limit API calls)
        for sg, parent in decomposable[:2]:
            steps = await decompose_sub_goal(sg, parent, config, last_cycle_task=_last_cycle_task)
            if steps:
                save_step_plan(
                    goal_store._state,
                    sg.get("id", ""),
                    parent.get("id", ""),
                    steps,
                )
    except Exception as e:
        log.warning("Sub-goal decomposition failed (non-fatal): %s", e)

    goal_store.save_state()

    log.info(
        "Goal review: %d goal(s) assessed → %d task(s), %d update(s) (%.1fs)",
        len(goal_store.goals), len(tasks), len(updates), elapsed,
    )
    return tasks


def _call_goal_planner(
    client: anthropic.Anthropic,
    messages: list[dict[str, Any]],
    model: str,
) -> anthropic.types.Message:
    """Synchronous Haiku call — runs in thread via asyncio.to_thread."""
    with client.messages.stream(
        model=model,
        max_tokens=PLANNER_MAX_TOKENS,
        system=_GOAL_PLANNER_SYSTEM,
        messages=messages,
    ) as stream:
        return stream.get_final_message()


def _parse_goal_response(text: str) -> dict[str, Any]:
    """Parse planner JSON response.

    Expected format::

        {
          "tasks": [{"prompt": ..., "tier": ..., "priority": N, "goal_id": ...}],
          "goal_updates": [{"sub_goal_id": ..., "new_status": ..., "evidence": ...}],
          "reasoning": "..."
        }

    Returns empty structures on parse failure.
    """
    text = text.strip()
    # Strip markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    if not text:
        return {"tasks": [], "goal_updates": [], "reasoning": ""}

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Goal planner: failed to parse response: %s", text[:200])
        return {"tasks": [], "goal_updates": [], "reasoning": ""}

    if not isinstance(data, dict):
        log.warning("Goal planner: expected dict, got %s", type(data).__name__)
        return {"tasks": [], "goal_updates": [], "reasoning": ""}

    # Validate tasks
    raw_tasks = data.get("tasks", [])
    if not isinstance(raw_tasks, list):
        raw_tasks = []

    valid_tiers = {"low", "medium", "high"}
    tasks: list[dict[str, Any]] = []
    for item in raw_tasks[:3]:  # cap at 3 proactive tasks
        if not isinstance(item, dict):
            continue
        prompt = item.get("prompt", "")
        if not prompt or not isinstance(prompt, str):
            continue
        tier = item.get("tier", "medium")
        if tier not in valid_tiers:
            tier = "medium"
        priority = item.get("priority", 3)
        if not isinstance(priority, (int, float)) or priority < 1:
            priority = 3
        goal_id = item.get("goal_id", "")
        tasks.append({
            "prompt": prompt,
            "tier": tier,
            "priority": int(priority),
            "goal_id": goal_id,
            "source": "goals",
            "id": f"goal-{len(tasks) + 1}",
        })

    # Validate updates
    raw_updates = data.get("goal_updates", [])
    if not isinstance(raw_updates, list):
        raw_updates = []

    valid_statuses = {"done", "in-progress", "blocked", "not-started"}
    updates: list[dict[str, Any]] = []
    for item in raw_updates[:10]:
        if not isinstance(item, dict):
            continue
        sub_id = item.get("sub_goal_id", "")
        new_status = item.get("new_status", "")
        if sub_id and new_status in valid_statuses:
            updates.append({
                "sub_goal_id": sub_id,
                "new_status": new_status,
                "evidence": str(item.get("evidence", ""))[:200],
            })

    reasoning = str(data.get("reasoning", ""))[:500]

    return {"tasks": tasks, "goal_updates": updates, "reasoning": reasoning}
