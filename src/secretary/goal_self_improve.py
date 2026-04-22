"""Autonomous Self-Improvement Engine — failure analysis and improvement proposals.

Bridges the goal planner (Goal 3: self-improvement) to the existing
self_improve.py sandbox pipeline.  Identifies improvement opportunities
by mining run_log failure data, generates proposals via Haiku LLM,
and converts them into tasks that the watcher routes through the
sandbox → test → promote pipeline.

Research basis:
- STOP (Zelikman 2023): Seed improver that improves programs according to
  a utility function → propose → evaluate → keep/discard.
- Self-Refine (Madaan 2023): Generate → self-feedback → refine.  Same LLM
  as generator + critic.  ~20% improvement across tasks.
- Voyager (Wang 2023): Skill library of successful patterns; lifelong
  accumulation.  Don't repeat failures.
- Reflexion (Shinn 2023): Verbal feedback in episodic memory → better
  decisions in subsequent trials.

Architecture:
    run_log (failed tasks)  →  Haiku failure analysis  →  ImprovementProposal
    →  task dict with _self_improve flag  →  watcher routes through
    self_improve.improve()  →  result recorded in goal_state.json

    goal_state.json["self_improve_state"] = {
        "proposals": [{
            "proposal_id": str,
            "category": "failure-fix" | "code-quality" | "test-gap",
            "description": str,
            "target_files": [str],
            "task_prompt": str,
            "priority": float,     # 0-1
            "evidence": str,
            "status": "pending" | "executing" | "completed" | "failed" | "discarded",
            "result": {...} | None,
            "created": str,
            "executed": str | None
        }],
        "last_analysis": str | None,
        "total_proposed": int,
        "total_executed": int,
        "total_promoted": int,
        "total_discarded": int
    }
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

import anthropic

from .config import SecretaryConfig
from .run_log import RunLog, RunLogEntry
from .tool_policy import validate_file_write_path
from .pipeline_health import HealthLog
from .textgrad_lite import (
    run_textgrad_analysis,
    gradient_to_proposal,
    TextualGradient,
)

log = logging.getLogger("secretary.goal_self_improve")

# ── Constants ────────────────────────────────────────────────────────────
ANALYSIS_MODEL = "claude-haiku-4.5"
ANALYSIS_MAX_TOKENS = 2048
MAX_PROPOSALS_PER_ANALYSIS = 3
# Defaults — overridden by config.self_improve.analysis_cooldown_hours if set
ANALYSIS_COOLDOWN_HOURS = 0.5   # 30min — match watcher interval; analysis is free Haiku
STAGNATION_COOLDOWN_HOURS = 1.0 # independent from failure analysis cooldown
MAX_FAILURE_ENTRIES = 30       # how many recent failures to feed to Haiku
MAX_PENDING_PROPOSALS = 5      # don't accumulate too many unexecuted proposals
CONSECUTIVE_FAIL_LIMIT = 3     # pause pipeline after this many consecutive failures
CONSECUTIVE_FAIL_COOLDOWN_HOURS = 2.0  # how long to pause after hitting the limit
SELF_IMPROVE_GOAL_ID = "self-improvement"

# Master-baseline-red circuit breaker.
# When master's test suite is broken (any source), self-improve sandbox tasks
# all fail with "Pre-test baseline FAILED — tests broken before agent ran"
# at self_improve.py L1612 AFTER spending sandbox setup + optional Haiku
# analysis. Observed Apr 22: 18 such failures in one day = wasted setup
# for the same root cause. This circuit breaker scans run_log directly and
# short-circuits BEFORE proposal generation when master is likely red.
#
# Complementary to _check_consecutive_failures (which only triggers after
# 3 CONFIRMED proposal failures — 3 wasted cycles minimum). This gate
# fires on any run_log evidence and catches the problem in cycle 1.
BASELINE_RED_LOOKBACK = 20         # scan this many recent run_log entries
BASELINE_RED_THRESHOLD = 2         # trip breaker at this many recent baseline failures
BASELINE_RED_COOLDOWN_MINUTES = 60 # most recent must be within this window
_BASELINE_RED_ERROR_PREFIX = "Pre-test baseline FAILED"


def _master_baseline_appears_red(
    run_log: "RunLog",
    now: datetime | None = None,
) -> bool:
    """Return True if recent run_log evidence suggests master's tests are broken.

    Heuristic: count entries in the last ``BASELINE_RED_LOOKBACK`` whose
    ``error`` starts with ``Pre-test baseline FAILED``. If >=
    ``BASELINE_RED_THRESHOLD`` such entries exist AND the most recent one
    is within ``BASELINE_RED_COOLDOWN_MINUTES``, the breaker trips.

    Pure run_log inspection — no test execution, no LLM calls, cheap.
    """
    try:
        recent = run_log.recent(BASELINE_RED_LOOKBACK)
    except Exception:  # defensive — never let this crash the cycle
        log.debug("baseline-red check: run_log.recent failed", exc_info=True)
        return False
    if not recent:
        return False
    baseline_failures = [
        e for e in recent
        if not e.success
        and isinstance(getattr(e, "error", None), str)
        and e.error.startswith(_BASELINE_RED_ERROR_PREFIX)
    ]
    if len(baseline_failures) < BASELINE_RED_THRESHOLD:
        return False
    # Most recent baseline failure must be fresh — older ones likely already
    # healed and we'd just be pausing on stale signal.
    _now = now or datetime.now(timezone.utc)
    most_recent = max(
        baseline_failures,
        key=lambda e: getattr(e, "timestamp", ""),
        default=None,
    )
    if most_recent is None:
        return False
    ts = getattr(most_recent, "timestamp", "")
    if not ts:
        return False
    try:
        last_dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    minutes_since = (_now - last_dt).total_seconds() / 60.0
    return minutes_since <= BASELINE_RED_COOLDOWN_MINUTES

# Scope preamble prepended to every self-improvement task prompt to prevent
# agents from writing files outside the allowed scope.
_SCOPE_PREAMBLE = """\
SCOPE CONSTRAINTS (mandatory — violating these will cause task failure):
- You may modify files under src/secretary/ (.py only) and tests/ (.py only).
- You may create campaign files in campaigns/ (.yaml/.yml only).
- NEVER create temporary files (_tmp_*, *.txt, scratch files, analysis dumps).
- NEVER write to data/, config.yaml, .env, or credential files.
- If you need to debug, use log.info() in the code.
- Do NOT weaken or delete existing test assertions to make your change pass.
- If existing tests fail, fix the implementation in src/secretary/, not the tests.

"""


# ── Proposal deduplication ───────────────────────────────────────────────

_DEDUP_SIMILARITY_THRESHOLD = 0.80
_TARGET_RE = re.compile(
    r"In\s+(\S+\.py),\s+function\s+(\w+)", re.IGNORECASE,
)


def _extract_target(prompt: str) -> tuple[str, str] | None:
    """Extract (file, function) from a task_prompt like 'In src/X.py, function Y:'."""
    m = _TARGET_RE.search(prompt)
    return (m.group(1), m.group(2)) if m else None


def _deduplicate_proposals(
    new_proposals: list[dict[str, Any]],
    existing_proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter out new proposals that are too similar to existing ones.

    Two dedup layers:
    1. Target-function: if any non-discarded proposal targets the same file+function, drop.
    2. Description similarity: SequenceMatcher >= 80% against existing descriptions.
    """
    kept: list[dict[str, Any]] = []
    active = [p for p in existing_proposals if p.get("status") not in ("discarded",)]
    existing_targets = {
        _extract_target(p.get("task_prompt", ""))
        for p in active
    }
    existing_targets.discard(None)
    existing_descs = [
        " ".join(p.get("description", "").lower().split())
        for p in active
    ]
    for new_p in new_proposals:
        # Layer 1: same file+function as any active proposal → drop
        new_target = _extract_target(new_p.get("task_prompt", ""))
        if new_target and new_target in existing_targets:
            log.info(
                "Dedup: dropping proposal (same target %s:%s): %s",
                new_target[0], new_target[1],
                new_p.get("description", "")[:60],
            )
            continue

        # Layer 2: description similarity
        new_desc = " ".join(new_p.get("description", "").lower().split())
        is_dup = False
        for existing_desc in existing_descs:
            if not new_desc or not existing_desc:
                continue
            sim = difflib.SequenceMatcher(None, new_desc, existing_desc).ratio()
            if sim >= _DEDUP_SIMILARITY_THRESHOLD:
                log.info(
                    "Dedup: dropping proposal (%.0f%% similar to existing): %s",
                    sim * 100, new_p.get("description", "")[:60],
                )
                is_dup = True
                break
        if not is_dup:
            kept.append(new_p)
            existing_descs.append(new_desc)
            if new_target:
                existing_targets.add(new_target)
    return kept


# ── Data structures ──────────────────────────────────────────────────────

@dataclass
class ImprovementProposal:
    """A proposed improvement to the secretary's own codebase."""
    proposal_id: str
    category: str           # "failure-fix" | "code-quality" | "test-gap"
    description: str        # what to improve
    target_files: list[str] # files to modify
    task_prompt: str        # detailed prompt for the improvement agent
    priority: float         # 0.0-1.0, higher = more impactful
    evidence: str           # what data led to this proposal
    status: str = "pending"
    result: dict[str, Any] | None = None
    created: str = ""
    executed: str | None = None


# ── LLM prompts ──────────────────────────────────────────────────────────

_ANALYSIS_SYSTEM = """\
You are a failure analysis engine for an autonomous AI secretary.  You review \
recent task failure logs and propose concrete code improvements to fix them.

The secretary is a Python project with:
- src/secretary/ — source code:
  direct_agent.py (agent loop), watcher.py (daemon/orchestrator), \
goals.py (goal planner), goal_decomposition.py, goal_replanner.py, \
goal_escalation.py, goal_guardrails.py, goal_self_improve.py, \
goal_meta_reflection.py, goal_reflection.py, goal_scheduler.py, \
goal_verification.py, goal_progress.py, goal_dependencies.py, \
goal_expectations.py, goal_approval.py, tool_policy.py, \
oracle.py, router.py, learned_router.py, strategy_library.py, \
textgrad_lite.py, prompt_optimizer.py, config.py, run_log.py, \
memory.py, event_bus.py, ooda.py, self_improve.py, tools.py
- tests/ — pytest test suite
- campaigns/ — YAML task definitions
- goals.yaml — long-horizon goals
- PLATFORM: Windows + PowerShell (never use bash/Linux commands)

Your job: analyse the failure patterns and propose specific, actionable fixes.

You may receive TWO kinds of input:
A) **Task failures** from run_log.jsonl — individual tasks that failed during execution.
B) **Pipeline health issues** — problems in the secretary's own infrastructure \
(analysis engine errors, reflection failures, trust scoring failures, quota \
exhaustion, cycle metadata anomalies).  These are HIGHER priority than task \
failures because they prevent the self-improvement loop itself from functioning.

Rules:
1. Focus on RECURRING patterns — a single flaky failure is not worth fixing.
2. Each proposal must target specific files from the list above.  Do NOT \
hallucinate file names.  Use the exact names listed.
3. Prioritize by impact: pipeline health issues > multi-task fixes > single-task fixes.
4. The improvement agent has file_read, file_write, file_edit, grep_search, \
run_command, and run_python tools.  Write task prompts that use these.
5. Keep task prompts under 500 words.  Be SPECIFIC: name the EXACT function \
to change and the EXACT modification.  Do NOT write vague prompts like \
"investigate", "analyze", "review", or "check".  Every task_prompt MUST \
contain a file_edit instruction.
6. TASK PROMPT FORMAT (mandatory):
   Line 1: "In src/secretary/MODULE.py, function FUNC_NAME:"
   Line 2-3: What to change and why (one sentence each)
   Line 4+: "Steps: 1. file_read src/secretary/MODULE.py  2. file_edit: ..."
   The agent will be given 8 turns max.  Turn 1 = read, Turn 2-3 = edit, \
Turn 4 = test.  Design your task_prompt for this budget.
7. Example of a GOOD task_prompt:
   "In src/secretary/direct_agent.py, function _run_tool_call: The error \
handler catches Exception but loses the traceback. Add traceback.format_exc() \
to the error message.  Steps: 1. file_read src/secretary/direct_agent.py \
2. file_edit the except block in _run_tool_call to include traceback output  \
3. run_command: python -m pytest tests/test_direct_agent.py -x -q"
8. Example of a BAD task_prompt (do NOT write these):
   "Investigate why direct_agent fails and analyze the error handling patterns."
9. Don't propose changes that would break existing tests — the sandbox will \
catch this, but it wastes time.
10. SCOPE: The improvement agent can modify files in src/secretary/ (.py) \
and tests/ (.py).  It can also create campaigns/ (.yaml). \
Do NOT propose changes to data/, config.yaml, .env, or credential files. \
Do NOT weaken or delete existing test assertions.
11. Do NOT propose fixes to self_improve.py or goal_self_improve.py. \
These files are manually maintained — self-referential fixes are unreliable.

Respond with ONLY JSON (no markdown fences):
{
  "proposals": [
    {
      "category": "failure-fix",
      "description": "Brief what and why",
      "target_files": ["src/secretary/module.py"],
      "task_prompt": "Detailed instructions for the improvement agent...",
      "priority": 0.8,
      "evidence": "Seen in N failures: error pattern X"
    }
  ],
  "analysis_summary": "1-2 sentence overview of the failure landscape"
}\
"""


def _build_analysis_prompt(
    failures: list[RunLogEntry],
    previous_proposals: list[dict[str, Any]],
    health_events: list | None = None,
) -> str:
    """Build the user prompt for failure analysis."""
    parts: list[str] = []

    parts.append("## Recent Task Failures")
    parts.append(f"({len(failures)} failed tasks from recent run log)\n")

    for i, entry in enumerate(failures[:MAX_FAILURE_ENTRIES], 1):
        parts.append(f"### Failure #{i}")
        parts.append(f"- **Task**: {entry.task[:200]}")
        parts.append(f"- **Tier**: {entry.tier} | **Model**: {entry.model}")
        parts.append(f"- **Source**: {entry.source}")
        if entry.goal_id:
            parts.append(f"- **Goal**: {entry.goal_id}")
        if entry.error:
            parts.append(f"- **Error**: {entry.error[:300]}")
        parts.append(f"- **Output**: {entry.output_preview[:300]}")
        # Include test failure details from verification if available
        extra = getattr(entry, "extra", None) or {}
        if isinstance(extra, dict):
            if extra.get("failed_tests"):
                parts.append(f"- **Failed tests**: {', '.join(extra['failed_tests'][:5])}")
            if extra.get("error_output"):
                parts.append(f"- **Test error output**: {extra['error_output'][:500]}")
            if extra.get("error_summary"):
                parts.append(f"- **Error summary**: {extra['error_summary'][:300]}")
        parts.append(f"- **Tools**: {', '.join(entry.tools_used) if entry.tools_used else 'none'}")
        parts.append(f"- **Duration**: {entry.duration_s:.1f}s | **Turns**: {entry.num_turns}")
        parts.append("")

    # Pipeline health events — non-task issues invisible to run_log
    if health_events:
        parts.append("## Pipeline Health Issues")
        parts.append(
            f"({len(health_events)} recent warning/error events from pipeline internals)\n"
        )
        for i, evt in enumerate(health_events[:15], 1):
            parts.append(f"### Health Issue #{i}")
            parts.append(f"- **Category**: {evt.category}")
            parts.append(f"- **Severity**: {evt.severity}")
            parts.append(f"- **Source**: {evt.source}")
            parts.append(f"- **Message**: {evt.message}")
            if evt.details:
                parts.append(f"- **Details**: {evt.details[:300]}")
            parts.append("")

    # Show previous proposals to avoid re-proposing the same thing
    if previous_proposals:
        parts.append("## Previous Proposals (avoid repeating these)")
        for p in previous_proposals[-10:]:
            status = p.get("status", "?")
            desc = p.get("description", "?")
            parts.append(f"- [{status}] {desc}")
            # Surface test failure details for failed proposals so the LLM
            # can learn *why* a mutation broke and avoid repeating the mistake.
            result = p.get("result") or {}
            if status == "failed" and isinstance(result, dict):
                if result.get("failed_tests"):
                    parts.append(
                        f"  Failed tests: {', '.join(result['failed_tests'][:5])}"
                    )
                if result.get("error_output"):
                    parts.append(
                        f"  Test error: {result['error_output'][:300]}"
                    )
                if result.get("test_output"):
                    # Show full test failure context (up to 1500 chars) so the
                    # LLM reflection can see exactly what broke and avoid repeats.
                    # Matches the structured "TEST FAILURE OUTPUT:" format from
                    # self_improve._run_tests for consistent failure visibility.
                    parts.append(
                        f"  Test failure output:\n{result['test_output'][:1500]}"
                    )
                if result.get("error_summary"):
                    parts.append(
                        f"  Error summary: {result['error_summary'][:200]}"
                    )
        parts.append("")

    # Self-improvement success rate stats (Reflexion-inspired meta-learning)
    if previous_proposals:
        completed = sum(1 for p in previous_proposals if p.get("status") == "completed")
        failed = sum(1 for p in previous_proposals if p.get("status") == "failed")
        total_attempted = completed + failed
        if total_attempted > 0:
            rate = completed / total_attempted * 100
            parts.append("## Self-Improvement Track Record")
            parts.append(
                f"- {completed}/{total_attempted} proposals succeeded ({rate:.0f}% success rate)"
            )
            if rate < 30:
                parts.append(
                    "- WARNING: Low success rate. Your proposals are failing. Adjust strategy:\n"
                    "  * Target ONE function in ONE file — no multi-file changes\n"
                    "  * Pick the simplest possible fix (add try/except, fix a string, add a check)\n"
                    "  * The executing agent has only 8 turns — design for that budget\n"
                    "  * Write the task_prompt as: file to read → exact edit to make → test to run\n"
                    "  * AVOID: refactors, new features, multi-step changes, investigation tasks"
                )
            elif rate > 70:
                parts.append(
                    "- Good success rate. Continue with similar scope and targeting."
                )
            parts.append("")

    parts.append("## Instructions")
    parts.append(
        f"Propose up to {MAX_PROPOSALS_PER_ANALYSIS} concrete improvements "
        "to fix the most impactful failure patterns.  If no failures warrant "
        "a code fix (e.g. all are transient network errors), return an empty "
        "proposals list."
    )

    return "\n".join(parts)


# ── State management ─────────────────────────────────────────────────────

def _get_improve_state(state: dict[str, Any]) -> dict[str, Any]:
    """Get or create the self_improve_state section in goal_state."""
    if "self_improve_state" not in state:
        state["self_improve_state"] = {
            "proposals": [],
            "last_analysis": None,
            "total_proposed": 0,
            "total_executed": 0,
            "total_promoted": 0,
            "total_discarded": 0,
        }
    return state["self_improve_state"]


def is_analysis_due(
    state: dict[str, Any],
    cooldown_hours: float = ANALYSIS_COOLDOWN_HOURS,
) -> bool:
    """Check if enough time has elapsed since last analysis."""
    imp = _get_improve_state(state)
    last = imp.get("last_analysis")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        now = datetime.now(timezone.utc)
        hours_elapsed = (now - last_dt).total_seconds() / 3600
        return hours_elapsed >= cooldown_hours
    except (ValueError, TypeError):
        return True


def _count_pending(state: dict[str, Any]) -> int:
    """Count proposals still in pending status."""
    imp = _get_improve_state(state)
    return sum(1 for p in imp["proposals"] if p.get("status") == "pending")


def get_pending_proposal(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the highest-priority pending proposal, or None."""
    imp = _get_improve_state(state)
    pending = [p for p in imp["proposals"] if p.get("status") == "pending"]
    if not pending:
        return None
    # Sort by priority descending
    pending.sort(key=lambda p: p.get("priority", 0), reverse=True)
    return pending[0]


def _check_consecutive_failures(state: dict[str, Any]) -> bool:
    """Return True if the last N executed proposals all failed (pipeline should pause).

    Looks at the most recent CONSECUTIVE_FAIL_LIMIT proposals that have a result
    (completed, failed, succeeded_not_promoted). If all are 'failed', returns True.
    Also checks if enough time has passed since the last failure to resume.
    """
    imp = _get_improve_state(state)
    # Get proposals with results, sorted by execution time (most recent first)
    executed = [
        p for p in imp["proposals"]
        if p.get("status") in ("completed", "failed", "succeeded_not_promoted")
        and p.get("executed")
    ]
    if len(executed) < CONSECUTIVE_FAIL_LIMIT:
        return False
    executed.sort(key=lambda p: p.get("executed", ""), reverse=True)
    recent = executed[:CONSECUTIVE_FAIL_LIMIT]
    if not all(p.get("status") == "failed" for p in recent):
        return False
    # All recent are failures — check if cooldown has elapsed
    last_fail_ts = recent[0].get("executed", "")
    if last_fail_ts:
        try:
            last_dt = datetime.fromisoformat(last_fail_ts)
            hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours >= CONSECUTIVE_FAIL_COOLDOWN_HOURS:
                log.info("Consecutive failure cooldown expired (%.1fh), resuming", hours)
                return False
        except (ValueError, TypeError):
            pass
    log.warning(
        "Self-improve paused: last %d proposals all failed (cooldown %dh)",
        CONSECUTIVE_FAIL_LIMIT, CONSECUTIVE_FAIL_COOLDOWN_HOURS,
    )
    return True


def record_proposal_result(
    state: dict[str, Any],
    proposal_id: str,
    success: bool,
    promoted: bool,
    changed_files: list[str] | None = None,
    error: str | None = None,
    test_output: str | None = None,
) -> None:
    """Record the result of executing a proposal."""
    imp = _get_improve_state(state)
    for p in imp["proposals"]:
        if p.get("proposal_id") == proposal_id:
            if success and promoted:
                p["status"] = "completed"
            elif not success:
                p["status"] = "failed"
            else:
                # Tests passed but not promoted (auto_promote=false or git failed)
                p["status"] = "succeeded_not_promoted"
            p["executed"] = datetime.now(timezone.utc).isoformat()
            p["result"] = {
                "success": success,
                "promoted": promoted,
                "changed_files": changed_files or [],
                "error": error,
            }
            # Store test output snippet for debugging — always capture
            # so both pass and fail details are available to the improvement loop
            if test_output:
                p["result"]["test_output"] = test_output[-2000:]
            imp["total_executed"] = imp.get("total_executed", 0) + 1
            if promoted:
                imp["total_promoted"] = imp.get("total_promoted", 0) + 1
            break


def defer_proposal_for_baseline_red(
    state: dict[str, Any],
    proposal_id: str,
) -> bool:
    """Revert an ``executing`` proposal back to ``pending`` without counting it
    as executed. Used by the watcher dispatch-level baseline-red circuit
    breaker to recycle proposals when master's test suite is broken, so they
    are retried once master heals instead of being prematurely marked failed.

    Does NOT mutate total_executed / total_promoted / result / executed fields
    (those only move on real sandbox execution).  Stamps a ``deferred_at``
    timestamp and increments ``defer_count`` for observability.

    Returns True if a matching proposal was found and reverted, else False.
    """
    imp = _get_improve_state(state)
    for p in imp["proposals"]:
        if p.get("proposal_id") != proposal_id:
            continue
        # Only defer proposals that were actually dispatched (status=executing).
        # Proposals in other states (pending/completed/failed/...) are either
        # already safe to pick up or already terminal — don't touch them.
        if p.get("status") != "executing":
            return False
        p["status"] = "pending"
        p["deferred_at"] = datetime.now(timezone.utc).isoformat()
        p["defer_count"] = int(p.get("defer_count", 0)) + 1
        return True
    return False


def discard_stale_proposals(
    state: dict[str, Any],
    max_age_hours: float = 72.0,
) -> int:
    """Discard pending proposals older than max_age_hours. Returns count discarded.

    Also cleans up proposals with non-standard statuses (e.g. 'expired' from
    earlier code versions) by marking them as 'discarded', and resets stale
    'executing' proposals (crashed before recording result) back to 'pending'.
    """
    imp = _get_improve_state(state)
    now = datetime.now(timezone.utc)
    _VALID_TERMINAL = {"completed", "failed", "discarded", "succeeded_not_promoted"}
    _VALID_ACTIVE = {"pending", "executing"}
    discarded = 0
    for p in imp["proposals"]:
        status = p.get("status", "")
        # Clean up non-standard statuses (e.g. "expired")
        if status not in _VALID_TERMINAL and status not in _VALID_ACTIVE:
            p["status"] = "discarded"
            imp["total_discarded"] = imp.get("total_discarded", 0) + 1
            discarded += 1
            continue
        # Reset stale "executing" proposals (crashed/interrupted before result)
        if status == "executing" and p.get("result") is None:
            try:
                created = datetime.fromisoformat(p["created"])
                age_hours = (now - created).total_seconds() / 3600
                if age_hours > 1.0:  # Stuck for >1h = definitely crashed
                    log.info(
                        "Resetting stale executing proposal %s → pending",
                        p.get("proposal_id", "?"),
                    )
                    p["status"] = "pending"
            except (ValueError, TypeError, KeyError):
                pass
            continue
        if status != "pending":
            continue
        # Discard pending proposals with empty task (malformed generation)
        if not p.get("task_prompt"):
            p["status"] = "discarded"
            imp["total_discarded"] = imp.get("total_discarded", 0) + 1
            discarded += 1
            log.info("Discarded empty-task proposal %s", p.get("proposal_id", "?"))
            continue
        try:
            created = datetime.fromisoformat(p["created"])
            age_hours = (now - created).total_seconds() / 3600
            if age_hours > max_age_hours:
                p["status"] = "discarded"
                imp["total_discarded"] = imp.get("total_discarded", 0) + 1
                discarded += 1
        except (ValueError, TypeError, KeyError):
            continue
    return discarded


def prune_old_proposals(
    state: dict[str, Any],
    keep_recent: int = 20,
) -> int:
    """Remove old terminal proposals (discarded/failed) to prevent state bloat.

    Keeps the most recent `keep_recent` (by creation time) plus ALL completed
    and active proposals. Returns count of proposals removed.
    """
    imp = _get_improve_state(state)
    _TERMINAL_PRUNABLE = {"discarded", "failed"}
    _KEEP = {"completed", "succeeded_not_promoted", "pending", "executing"}

    keep: list[dict] = []
    prunable: list[dict] = []
    for p in imp["proposals"]:
        if p.get("status") in _TERMINAL_PRUNABLE:
            prunable.append(p)
        else:
            keep.append(p)

    # Sort prunable by created desc, keep the most recent ones
    prunable.sort(key=lambda p: p.get("created", ""), reverse=True)
    keep.extend(prunable[:keep_recent])
    removed = prunable[keep_recent:]

    if removed:
        imp["proposals"] = keep
        log.info("Pruned %d old terminal proposals (kept %d)", len(removed), len(keep))

    return len(removed)


# ── LLM call ─────────────────────────────────────────────────────────────

def _parse_json_response(text: str) -> dict[str, Any]:
    """Parse JSON from LLM response, handling markdown fences and truncation."""
    cleaned = text.strip()
    # Strip markdown code fences if present
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```json or ```)
        lines = lines[1:]
        # Remove last ``` line
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip().startswith("```"):
                lines = lines[:i]
                break
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fallback: extract first JSON object via brace-depth matching
    # (handles truncated strings, trailing text, etc.)
    brace_start = cleaned.find("{")
    if brace_start >= 0:
        depth = 0
        for i, ch in enumerate(cleaned[brace_start:], brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(cleaned[brace_start:i + 1])
    raise json.JSONDecodeError("No valid JSON object found", cleaned, 0)


def _fallback_analysis_proposals() -> list[dict[str, Any]]:
    """Return an empty list when analysis LLM returns empty/invalid JSON.

    Returning [] is intentional: a non-parseable LLM response means we have no
    actionable proposals, not that we should inject a synthetic pipeline-error
    proposal that could pollute the improvement queue.
    """
    return []


async def _run_failure_analysis(
    failures: list[RunLogEntry],
    previous_proposals: list[dict[str, Any]],
    config: SecretaryConfig,
    health_events: list | None = None,
) -> list[dict[str, Any]]:
    """Call Haiku to analyze failures and generate proposals."""
    if not failures and not health_events:
        return []

    prompt = _build_analysis_prompt(failures, previous_proposals, health_events)

    # Inject empirical outcomes of past promoted proposals so the LLM
    # considers real metric impact, not just tests-passing success.
    try:
        from . import proposal_outcomes
        outcomes_block = proposal_outcomes.format_recent_outcomes_for_prompt(
            config.data_path, max_n=5
        )
        if outcomes_block:
            prompt = outcomes_block + "\n" + prompt
    except Exception as _po_err:
        log.debug("proposal_outcomes prompt injection skipped: %s", _po_err)

    from .config import _interpolate_env
    base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")
    client = anthropic.AsyncAnthropic(
        base_url=base_url,
        api_key="copilot-proxy",
    )

    import time
    from anthropic import APIError

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await client.messages.create(
                model=ANALYSIS_MODEL,
                max_tokens=ANALYSIS_MAX_TOKENS,
                system=_ANALYSIS_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            break  # Success
        except (ConnectionError, APIError) as e:
            if attempt < max_retries - 1:
                # Exponential backoff: 2s, 4s, 8s (max 30s)
                wait_time = min(2 ** (attempt + 1), 30)
                log.warning(
                    "Failure analysis LLM call failed (attempt %d/%d), retrying in %ds: %s",
                    attempt + 1, max_retries, wait_time, e,
                )
                time.sleep(wait_time)
            else:
                log.error("Failure analysis LLM call failed after %d attempts: %s", max_retries, e)
                HealthLog().record("analysis_failure", "error", f"Failure analysis LLM call failed after {max_retries} retries: {e}", source="goal_self_improve._run_failure_analysis")
                return []
        except Exception as e:
            log.error("Failure analysis LLM call failed: %s", e)
            HealthLog().record("analysis_failure", "error", f"Failure analysis LLM call failed: {e}", source="goal_self_improve._run_failure_analysis")
            return []

    text = response.content[0].text if response.content else ""

    # Check for empty or whitespace-only response before parsing
    if not text or text.strip() == "":
        log.warning("Empty response from analysis LLM")
        HealthLog().record("analysis_failure", "warning", "Analysis LLM returned empty response", source="goal_self_improve._run_failure_analysis")
        return _fallback_analysis_proposals()

    try:
        data = _parse_json_response(text)
    except json.JSONDecodeError as e:
        log.warning("Failed to parse analysis response: %s | raw: %.200s", e, text)
        HealthLog().record("analysis_failure", "warning", f"Failed to parse analysis JSON: {e}", source="goal_self_improve._run_failure_analysis", details=text[:300])
        # Return structured fallback with analysis proposal instead of crashing
        return _fallback_analysis_proposals()
    except IndexError as e:
        log.warning("Empty response content from analysis LLM: %s", e)
        HealthLog().record("analysis_failure", "warning", f"Empty response content: {e}", source="goal_self_improve._run_failure_analysis")
        return []
    except Exception as e:
        log.error("Unexpected error parsing analysis response: %s", e)
        HealthLog().record("analysis_failure", "error", f"Unexpected parsing error: {e}", source="goal_self_improve._run_failure_analysis")
        return []

    raw_proposals = data.get("proposals", [])
    summary = data.get("analysis_summary", "")
    if summary:
        log.info("Failure analysis: %s", summary)

    # Validate and normalize proposals
    proposals: list[dict[str, Any]] = []
    for rp in raw_proposals[:MAX_PROPOSALS_PER_ANALYSIS]:
        if not isinstance(rp, dict):
            continue
        if not rp.get("description") or not rp.get("task_prompt"):
            continue
        proposals.append({
            "proposal_id": f"prop-{uuid.uuid4().hex[:8]}",
            "category": rp.get("category", "failure-fix"),
            "description": str(rp["description"])[:500],
            "target_files": rp.get("target_files", [])[:10],
            "task_prompt": str(rp["task_prompt"])[:2000],
            "priority": max(0.0, min(1.0, float(rp.get("priority", 0.5)))),
            "evidence": str(rp.get("evidence", ""))[:500],
            "status": "pending",
            "result": None,
            "created": datetime.now(timezone.utc).isoformat(),
            "executed": None,
        })

    return proposals


# ── Stagnation detection ─────────────────────────────────────────────────

# Only count tools that modify files — run_command/run_python can be used for reading
_WRITE_TOOLS = {"file_write", "file_edit"}


def _detect_stagnation(
    state: dict[str, Any],
    recent_entries: list[RunLogEntry],
    config: SecretaryConfig,
) -> list[dict[str, Any]]:
    """Detect goals trapped in investigation-only execution.

    When goal tasks succeed but never use write tools (file_write, file_edit),
    the goal is stagnating — producing analysis without implementation.
    Generate proposals that force implementation.
    """
    sub_goal_status = state.get("sub_goal_status", {})
    stagnant_goals: list[tuple[str, str]] = []  # (sub_goal_id, evidence)

    # Find goals that are "in-progress" or "blocked" with stagnation evidence
    stagnation_keywords = [
        "read-only", "investigation", "analysis without",
        "no code changes", "no implementation", "zero executable",
        "0 executable", "zero actionable", "0 actionable",
        "stuck in", "no write",
    ]
    for sg_id, sg_data in sub_goal_status.items():
        if not isinstance(sg_data, dict):
            continue
        status = sg_data.get("status", "")
        evidence = sg_data.get("evidence", "")
        if status not in ("in-progress", "blocked"):
            continue
        # Check if evidence mentions investigation-only patterns
        evidence_lower = evidence.lower()
        if any(kw in evidence_lower for kw in stagnation_keywords):
            stagnant_goals.append((sg_id, evidence[:200]))

    if not stagnant_goals:
        return []

    # Also check recent goal task activity for write tool usage —
    # BUT only for the stagnant goals' parent goal_ids.  Unrelated goals
    # using file_write for reports should not clear stagnation signals.
    stagnant_goal_ids = set()
    for sg_id, _ in stagnant_goals:
        # Sub-goal IDs may be "parent:child" or "parent.child" or just "parent"
        for sep in (":", "."):
            if sep in sg_id:
                stagnant_goal_ids.add(sg_id.split(sep)[0])
                break
        else:
            stagnant_goal_ids.add(sg_id)

    goal_entries = [
        e for e in recent_entries
        if e.source == "goals" and e.goal_id in stagnant_goal_ids
    ]
    write_tool_usage = sum(1 for e in goal_entries if _WRITE_TOOLS & set(e.tools_used))
    total_goal_tasks = len(goal_entries)

    if total_goal_tasks > 0 and write_tool_usage > 0:
        # Stagnant goals' own tasks are using write tools — not stagnant
        return []

    log.info(
        "Stagnation detected: %d sub-goals stuck in investigation, "
        "%d/%d recent goal tasks used write tools",
        len(stagnant_goals), write_tool_usage, total_goal_tasks,
    )

    # Generate concrete implementation proposals for stagnant goals
    # Collect prior failure context to avoid repeating mistakes
    imp = _get_improve_state(state)
    prior_failures: dict[str, str] = {}  # sg_id → test output snippet
    for p in imp.get("proposals", []):
        if p.get("status") == "failed" and p.get("category") == "stagnation-fix":
            desc = p.get("description", "")
            _result = p.get("result")
            if not isinstance(_result, dict):
                _result = {}
            test_out = _result.get("test_output", "")
            changed = _result.get("changed_files", [])
            for sg_id_candidate, _ in stagnant_goals:
                if sg_id_candidate in desc:
                    prior_failures[sg_id_candidate] = (
                        f"Changed: {changed}\nTest output (tail): {test_out[-500:]}"
                    )

    proposals: list[dict[str, Any]] = []
    for sg_id, evidence in stagnant_goals[:MAX_PROPOSALS_PER_ANALYSIS]:
        target_files = _guess_target_files(sg_id)
        target_list = ", ".join(target_files)

        # Build prior failure context if available
        _prior_section = ""
        if sg_id in prior_failures:
            _prior_section = (
                f"\n\nPRIOR FAILED ATTEMPT (learn from this — do NOT repeat):\n"
                f"{prior_failures[sg_id]}\n"
                f"Make a DIFFERENT change this time. The previous approach did not work.\n"
            )

        proposal = {
            "proposal_id": f"stag-{uuid.uuid4().hex[:8]}",
            "category": "stagnation-fix",
            "description": (
                f"Sub-goal '{sg_id}' is stuck in investigation mode. "
                f"Force implementation by writing actual code changes."
            ),
            "target_files": target_files,
            "task_prompt": (
                f"The sub-goal '{sg_id}' has been stuck in investigation-only mode. "
                f"Previous evidence: {evidence}\n\n"
                f"TARGET FILES: {target_list}\n"
                f"Read these files with file_read, find a specific function to improve, "
                f"then use file_edit to modify it.\n\n"
                f"REQUIREMENTS:\n"
                f"- Use file_edit to modify at least one source file under src/secretary/\n"
                f"- Do NOT create temporary files or dump file contents to new files\n"
                f"- Pick ONE specific function and make a concrete improvement:\n"
                f"  * Add error handling, improve logging, fix a bug, add a missing feature\n"
                f"  * Add type hints, improve docstrings, refactor for clarity\n"
                f"  * Add input validation, improve edge case handling\n"
                f"- After your change, run tests: run_command {{command: 'python -m pytest tests/ -x -q'}}\n"
                f"- Make the SMALLEST useful change — one function, one fix"
                f"{_prior_section}"
            ),
            "priority": 0.85,
            "evidence": f"Stagnation: {evidence}",
            "status": "pending",
            "result": None,
            "created": datetime.now(timezone.utc).isoformat(),
            "executed": None,
        }
        proposals.append(proposal)

    return proposals


def _guess_target_files(sub_goal_id: str) -> list[str]:
    """Map sub-goal IDs to likely target files."""
    mappings: dict[str, list[str]] = {
        "goal-planner": ["src/secretary/goals.py", "src/secretary/goal_progress.py"],
        "failure-analysis": ["src/secretary/goal_self_improve.py"],
        "cost-monitoring": ["src/secretary/watcher.py", "src/secretary/router.py"],
        "harness-generation": ["src/secretary/goal_self_improve.py"],
        "oracle": ["src/secretary/oracle.py", "src/secretary/router.py"],
        "self-improvement": ["src/secretary/self_improve.py", "src/secretary/goal_self_improve.py"],
        "self-harness": ["src/secretary/goal_self_improve.py"],
        "prefix": ["src/secretary/watcher.py", "src/secretary/config.py"],
        "autonomy": ["src/secretary/watcher.py", "src/secretary/goal_scheduler.py"],
        "autoresearch": ["src/secretary/prompt_optimizer.py", "src/secretary/strategy_library.py"],
        "textgrad": ["src/secretary/textgrad_lite.py", "src/secretary/prompt_optimizer.py"],
        "decompos": ["src/secretary/goal_decomposition.py"],
        "replan": ["src/secretary/goal_replanner.py"],
        "escalat": ["src/secretary/goal_escalation.py"],
        "verif": ["src/secretary/goal_verification.py"],
        "trust": ["src/secretary/goal_scheduler.py"],
        "approv": ["src/secretary/goal_approval.py"],
        "event": ["src/secretary/event_bus.py", "src/secretary/ooda.py"],
        "direct": ["src/secretary/direct_agent.py"],
        "router": ["src/secretary/router.py", "src/secretary/learned_router.py"],
    }
    for key, files in mappings.items():
        if key in sub_goal_id:
            return files
    return ["src/secretary/watcher.py"]


# ── Task conversion ──────────────────────────────────────────────────────

def proposal_to_task(proposal: dict[str, Any]) -> dict[str, Any]:
    """Convert an ImprovementProposal into a campaign-style task dict.

    Tasks with ``_self_improve`` are routed through self_improve.improve()
    by the watcher instead of normal direct_agent.run().

    Prepends _SCOPE_PREAMBLE to the task prompt and validates all target
    file paths against the allowed write-path whitelist.  Forbidden paths
    are stripped and logged so the executing agent never receives them.
    """
    # Validate and filter target files against the write-path whitelist
    raw_targets = proposal.get("target_files", [])
    allowed_targets: list[str] = []
    for fpath in raw_targets:
        ok, msg = validate_file_write_path(fpath)
        if ok:
            allowed_targets.append(fpath)
        else:
            log.warning("proposal_to_task: dropping forbidden target file — %s", msg)

    # Prepend scope preamble so the agent always sees the constraints
    task_prompt = _SCOPE_PREAMBLE + proposal["task_prompt"]

    return {
        "id": f"self-improve-{proposal['proposal_id']}",
        "task": task_prompt,
        "prompt": task_prompt,
        "tier": "high",
        "source": "goals",
        "goal_id": SELF_IMPROVE_GOAL_ID,
        "_self_improve": True,          # Signals watcher to use sandbox pipeline
        "_proposal_id": proposal["proposal_id"],
        "_target_files": allowed_targets,
        "_description": proposal.get("description", ""),
    }


# ── Main entry point ─────────────────────────────────────────────────────

async def run_self_improve_analysis(
    state: dict[str, Any],
    run_log: RunLog,
    config: SecretaryConfig,
    health_log: HealthLog | None = None,
) -> list[dict[str, Any]]:
    """Analyze recent failures and generate improvement tasks.

    Called from the watcher cycle after goal review.  Returns a list of
    task dicts to be appended to the task queue.

    Steps:
    1. Check cooldown — skip if analysis ran recently
    2. Discard stale pending proposals
    3. Check pending count — skip if too many unexecuted proposals
    4. Collect recent failures from run_log
    5. Call Haiku for failure analysis → proposals
    6. Store proposals in state
    7. Convert highest-priority pending proposal to task
    """
    imp = _get_improve_state(state)
    tasks: list[dict[str, Any]] = []

    # 0. Always discard stale/invalid proposals first
    discarded = discard_stale_proposals(state)
    if discarded:
        log.info("Discarded %d stale self-improvement proposals", discarded)

    # 0a. Prune old terminal proposals to prevent state bloat
    pruned = prune_old_proposals(state)
    if pruned:
        log.info("Pruned %d old proposals from state", pruned)

    # 0b. Consecutive failure gate — pause pipeline if recent proposals keep failing
    if _check_consecutive_failures(state):
        if health_log:
            health_log.record(
                "self_improve_paused", "warning",
                f"Pipeline paused: last {CONSECUTIVE_FAIL_LIMIT} proposals failed",
                source="goal_self_improve.run_self_improve_analysis",
            )
        return tasks

    # 0c. Master-baseline-red circuit breaker — if run_log shows master's
    #     test suite is already broken, skip dispatch. Every self-improve
    #     sandbox task would fail at self_improve.py's pre-test baseline
    #     check anyway, burning sandbox setup time + Haiku analysis cost
    #     on the identical failure mode. Cheap run_log inspection — fires
    #     from the first cycle evidence appears, unlike the 3-proposal
    #     _check_consecutive_failures gate.
    if _master_baseline_appears_red(run_log):
        log.warning(
            "Self-improve paused: master baseline appears red "
            "(>=%d 'Pre-test baseline FAILED' in last %d run_log entries, "
            "most recent within %d min). Skipping dispatch until master heals.",
            BASELINE_RED_THRESHOLD,
            BASELINE_RED_LOOKBACK,
            BASELINE_RED_COOLDOWN_MINUTES,
        )
        if health_log:
            health_log.record(
                "master_baseline_red", "warning",
                "Self-improve skipped: master baseline red (pre-test failures in run_log)",
                source="goal_self_improve.run_self_improve_analysis",
            )
        return tasks

    # 1. Check cooldown
    cooldown = getattr(config.self_improve, "analysis_cooldown_hours", ANALYSIS_COOLDOWN_HOURS)
    if not is_analysis_due(state, cooldown_hours=cooldown):
        # Even if analysis isn't due, we might have pending proposals to execute
        proposal = get_pending_proposal(state)
        if proposal:
            proposal["status"] = "executing"
            tasks.append(proposal_to_task(proposal))
        elif not tasks:
            # No pending proposals and cooldown active — check for stagnation
            # Stagnation detection is independent of failure analysis cooldown
            # because it uses goal_state evidence, not run_log failures
            last_stag = imp.get("last_stagnation_check", "")
            stag_cooldown = getattr(config.self_improve, "stagnation_cooldown_hours", STAGNATION_COOLDOWN_HOURS)
            if last_stag:
                try:
                    stag_dt = datetime.fromisoformat(last_stag)
                    hours_since = (datetime.now(timezone.utc) - stag_dt).total_seconds() / 3600
                    if hours_since < stag_cooldown:
                        return tasks
                except (ValueError, TypeError):
                    pass  # bad timestamp — proceed with check
            recent = run_log.recent(100)
            stag = _detect_stagnation(state, recent, config)
            imp["last_stagnation_check"] = datetime.now(timezone.utc).isoformat()
            if stag:
                stag = _deduplicate_proposals(stag, imp["proposals"])
            if stag:
                imp["proposals"].extend(stag)
                imp["total_proposed"] = imp.get("total_proposed", 0) + len(stag)
                log.info("Stagnation analysis: %d proposal(s) from investigation-only goals", len(stag))
                proposal = get_pending_proposal(state)
                if proposal:
                    proposal["status"] = "executing"
                    tasks.append(proposal_to_task(proposal))
        return tasks

    # 2. Check pending count
    pending_count = _count_pending(state)
    if pending_count >= MAX_PENDING_PROPOSALS:
        log.info(
            "Skipping analysis: %d pending proposals (max %d)",
            pending_count, MAX_PENDING_PROPOSALS,
        )
        # Execute the top pending proposal instead
        proposal = get_pending_proposal(state)
        if proposal:
            proposal["status"] = "executing"
            tasks.append(proposal_to_task(proposal))
        return tasks

    # 4. Collect recent failures (exclude self-improve sandbox failures to prevent
    #    self-referential analysis loop — the pipeline shouldn't diagnose itself)
    recent = run_log.recent(100)
    failures = [
        e for e in recent
        if not e.success
        and e.model != "self-improve"
        and not (e.source == "campaign" and "[self-improve]" in (e.task or "")[:20])
    ]

    new_proposals: list[dict[str, Any]] = []

    # Collect pipeline health events
    health_events = health_log.recent_errors(hours=24) if health_log else []

    if failures or health_events:
        # 5a. Run failure analysis (task failures + pipeline health issues)
        log.info(
            "Running self-improvement analysis on %d failures + %d health events",
            len(failures), len(health_events),
        )
        new_proposals = await _run_failure_analysis(
            failures, imp["proposals"], config, health_events=health_events,
        )
    else:
        # 5b. No failures — check for stagnation (goals succeeding but not progressing)
        stagnation_proposals = _detect_stagnation(state, recent, config)
        if stagnation_proposals:
            new_proposals = stagnation_proposals
            log.info("Stagnation analysis: %d proposals from goals with no implementation progress", len(new_proposals))
        else:
            log.info("No recent failures and no stagnation detected")

    # 6. Store proposals (with deduplication)
    if new_proposals:
        new_proposals = _deduplicate_proposals(new_proposals, imp["proposals"])
    if new_proposals:
        imp["proposals"].extend(new_proposals)
        imp["total_proposed"] = imp.get("total_proposed", 0) + len(new_proposals)
        log.info("Generated %d improvement proposals", len(new_proposals))

    imp["last_analysis"] = datetime.now(timezone.utc).isoformat()

    # 7. Convert top proposal to task
    proposal = get_pending_proposal(state)
    if proposal:
        proposal["status"] = "executing"
        tasks.append(proposal_to_task(proposal))

    return tasks


# ── TextGrad-powered eval analysis ───────────────────────────────────────

def run_textgrad_proposals(
    state: dict[str, Any],
    config: SecretaryConfig,
    eval_results_path: Path | None = None,
    eval_tasks_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Generate improvement proposals from eval trace analysis (TextGrad-lite).

    Complements run_self_improve_analysis() which mines run_log failures.
    This function analyzes EVAL failures using textual gradients — targeted
    LLM critique of why specific eval tasks fail, producing specific prompt
    changes as proposals.

    Returns list of proposals dicts (same format as from run_self_improve_analysis).
    """
    from .config import _interpolate_env

    project_root = config.data_path.parent if config.data_path else Path(".")

    if eval_results_path is None:
        # Look for the most recent eval results
        candidates = sorted(project_root.glob("data/autoresearch_eval_*.json"))
        if not candidates:
            log.info("TextGrad: no eval results found")
            return []
        eval_results_path = candidates[-1]

    if eval_tasks_path is None:
        eval_tasks_path = project_root / "eval_tasks.json"

    if not eval_results_path.exists() or not eval_tasks_path.exists():
        log.info("TextGrad: required files not found")
        return []

    base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")
    gradients = run_textgrad_analysis(
        base_url=base_url,
        eval_results_path=eval_results_path,
        eval_tasks_path=eval_tasks_path,
        max_gradients=3,
    )

    if not gradients:
        log.info("TextGrad: no gradients generated (all tasks passed?)")
        return []

    # Convert gradients to proposals
    imp = _get_improve_state(state)
    proposals = []
    for gradient in gradients:
        raw = gradient_to_proposal(gradient)
        proposal = {
            "proposal_id": f"tg-{uuid.uuid4().hex[:8]}",
            "category": raw["category"],
            "description": raw["description"][:500],
            "target_files": raw["target_files"][:10],
            "task_prompt": raw["task_prompt"][:2000],
            "priority": raw["priority"],
            "evidence": raw["evidence"][:500],
            "status": "pending",
            "result": None,
            "created": datetime.now(timezone.utc).isoformat(),
            "executed": None,
        }
        proposals.append(proposal)

    if proposals:
        proposals = _deduplicate_proposals(proposals, imp["proposals"])
    if proposals:
        imp["proposals"].extend(proposals)
        imp["total_proposed"] = imp.get("total_proposed", 0) + len(proposals)
        log.info("TextGrad: generated %d proposals from eval failures", len(proposals))

    return proposals
