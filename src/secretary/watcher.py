"""24/7 autonomous daemon — the core of Claude Secretary.

This is the main execution loop. It:
1. Wakes up on a configurable schedule (default: every 30 minutes)
2. Loads campaign tasks from YAML (email triage, calendar, research, etc.)
3. Routes each task to the appropriate model tier (Haiku/Sonnet/Opus)
4. Executes tasks via Claude SDK with Gmail/Calendar MCP tools
5. Logs all outcomes to data/run_log.jsonl for self-review
6. Updates memory with task results for cross-task context
7. Consolidates memory patterns (recurring tasks → long-term learnings)
8. Sleeps until next cycle (doubles interval on failure for backoff)

The watcher is designed to run indefinitely. It handles:
- Graceful shutdown via Ctrl+C / SIGTERM
- Failure backoff (doubles wait time when tasks fail)
- Max run limits (for testing / bounded campaigns)
- Memory persistence across cycles

Start with: secretary watch [--interval 15] [--campaign my-tasks.yaml]
"""
from __future__ import annotations

import asyncio
import hashlib
import json as json_mod
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import direct_agent
from . import oracle as oracle_module
from .direct_tools import build_tool_registry
from .config import SecretaryConfig, _interpolate_env
from .ooda import run_ooda_cycle
from .goals import GoalStore, is_review_due, run_goal_review
from .goal_reflection import run_goal_reflection
from .goal_meta_reflection import run_meta_reflection
from .goal_progress import compute_progress, format_progress_section
from .goal_escalation import evaluate_escalations
from .goal_guardrails import apply_guardrails
from .goal_approval import (
    approve_all as _approve_all,
    auto_approve_self_improve,
    get_approved,
    get_pending,
    mark_executed,
    mark_notified,
    prune_old_entries,
    queue_to_tasks,
    submit_tasks,
)
from .tool_policy import filter_tools
from .strategy_library import StrategyLibrary, maybe_extract_strategy, load_library
from .goal_decomposition import (
    get_next_step,
    get_step_plans,
    record_step_result,
    step_to_task,
)
from .goal_expectations import check_assertions, format_assertion_results
from .goal_replanner import handle_step_failure
from .goal_verification import (
    verify_step_completion,
    record_verification,
    detect_completed_goals,
    mark_goals_completed,
    PASS,
    FAIL,
)
from .goal_self_improve import run_self_improve_analysis, record_proposal_result
from .pipeline_health import HealthLog
from .goal_harness import (
    generate_goal_test,
    run_harness_test,
    format_harness_result,
    extract_context_hints,
    harness_validation_loop,
    ValidationResult,
    syntax_check,
    validate_harness,
)
from .learned_router import build_stats_from_log, extract_category, RoutingStats as LearnedRoutingStats
from .coordinator import Coordinator, TaskResult as CoordTaskResult
from .cost_monitor import CostMonitor, CostMonitorConfig
from .event_bus import (
    CalendarEventSource,
    EventBus,
    EventType,
    Event,
    FileChangeSource,
    GmailEventSource,
)
from .memory import MemoryStore, MarkdownMemory
from .metrics import MetricsCollector, TaskMetric
from .router import get_premium_cost, select_model
from .run_log import RunLog, RunLogEntry
from .service import cleanup_pidfile, install_sigbreak_handler, write_pidfile
from .task_batcher import group_into_batches

log = logging.getLogger("secretary.watcher")

# Patterns that indicate quota/rate-limit exhaustion (not transient)
_QUOTA_PATTERNS = (
    "quota", "rate limit", "rate_limit", "too many requests",
    "429", "billing", "insufficient_quota",
    "premium request", "model capacity",
)

# Pre-compiled regexes used per-task — hoisted to module level to avoid
# re-compilation inside hot loops.
_SCRATCHPAD_TASKS_RE = re.compile(
    r"\b(research|analyze|build|improve|audit|code|file|bug|tests?|implement|refactor)\b",
    re.I,
)
_PREFETCH_EMAIL_RE = re.compile(
    r"\b(emails?|gmail|inbox|unread|messages?|newsletter|mail)\b", re.I,
)
_PREFETCH_CALENDAR_RE = re.compile(
    r"\b(calendar|events?|meetings?|schedule|appointment|today.?s?\s*events)\b", re.I,
)

# Read-only tools safe to memoize within a single cycle.
_MEMOIZABLE_TOOLS = frozenset({
    "gmail_search", "gmail_read", "gmail_list_drafts", "gmail_get_draft",
    "calendar_today", "calendar_list", "calendar_search",
    "file_read", "file_list",
})


def _is_quota_error(error_text: str) -> bool:
    """Check if an error message indicates quota/rate-limit exhaustion."""
    lower = error_text.lower()
    return any(p in lower for p in _QUOTA_PATTERNS)


def _check_schedule(schedule: str, now: datetime | None = None) -> bool:
    """Check if current time matches a schedule expression.

    Supported formats:
        "hours:8-17"      — only run between 8:00 and 17:00 (local time)
        "hours:22-6"      — overnight range (10 PM to 6 AM)
        "hours:6-9,17-20" — only run between 6-9 or 17-20
        "weekdays"        — only Mon-Fri
        "weekends"        — only Sat-Sun

    Multiple rules can be combined with semicolons: "hours:8-17;weekdays"
    All rules must match for the task to run.
    """
    if now is None:
        now = datetime.now()
    rules = [r.strip() for r in schedule.split(";") if r.strip()]
    for rule in rules:
        if rule.startswith("hours:"):
            ranges_str = rule[6:]
            in_range = False
            for rng in ranges_str.split(","):
                parts = rng.strip().split("-")
                if len(parts) == 2:
                    try:
                        start_h, end_h = int(parts[0]), int(parts[1])
                    except ValueError:
                        log.warning("Malformed hours range %r in schedule — skipping rule", rng.strip())
                        continue
                    if start_h <= end_h:
                        # Normal range: e.g. 8-17
                        if start_h <= now.hour < end_h:
                            in_range = True
                            break
                    else:
                        # Overnight wrap-around: e.g. 22-6 means 22,23,0,1,2,3,4,5
                        if now.hour >= start_h or now.hour < end_h:
                            in_range = True
                            break
            if not in_range:
                return False
        elif rule == "weekdays":
            if now.weekday() >= 5:  # 5=Sat, 6=Sun
                return False
        elif rule == "weekends":
            if now.weekday() < 5:
                return False
        else:
            log.warning("Unknown schedule rule: %s", rule)
    return True


class Watcher:
    """Runs campaign tasks on a schedule, indefinitely.

    Each cycle: load campaign → run all tasks → log results → sleep.
    All outcomes are persisted to data/run_log.jsonl.
    """

    def __init__(
        self,
        config: SecretaryConfig,
        campaign_file: str | Path | None = None,
        dry_run: bool = False,
    ):
        self.config = config
        # Support comma-separated campaign files
        raw = str(campaign_file or config.watcher.campaign_file)
        self.campaign_files = [Path(f.strip()) for f in raw.split(",")]
        self.campaign_file = self.campaign_files[0]  # primary (for logging)
        self.interval = config.watcher.interval_minutes
        self.max_runs = config.watcher.max_runs
        self.max_premium_per_cycle = config.watcher.max_premium_per_cycle
        self.max_retries = config.watcher.max_retries
        self.retry_base_delay = config.watcher.retry_base_delay
        self.pause_on_failure = config.watcher.pause_on_failure
        self.dry_run = dry_run
        self.run_log = RunLog(config.data_path / "run_log.jsonl")
        self.health_log = HealthLog(config.data_path / "pipeline_health.jsonl")
        self._stop = False
        self._quota_exhausted = False
        self._runs_completed = 0
        self._runs_failed = 0
        self._total_passed = 0
        self._total_task_failures = 0
        self._total_premium_spent = 0.0
        self._total_cost_usd = 0.0
        self._start_time: float | None = None
        self._consolidate_every = 3  # consolidate memory every N cycles
        # Cross-cycle dedup: prompt_hash → last cycle number where it succeeded
        self._dedup_path = config.data_path / "dedup_history.json"
        self._success_history: dict[str, int] = self._load_dedup_history()

        # Multi-instance coordination (optional)
        self._coordinator: Coordinator | None = None
        self._metrics: MetricsCollector | None = None
        if config.multi.coordinate and config.instance_id:
            self._coordinator = Coordinator(
                shared_dir=config.shared_data_path,
                instance_id=config.instance_id,
                role=config.multi.role,
            )
            self._metrics = MetricsCollector(config.metrics_path)
        elif config.instance_id:
            # Metrics even without full coordination
            self._metrics = MetricsCollector(config.metrics_path)

        # Cross-cycle file cache: avoids re-reading the same files every cycle.
        # Keyed by file path → (mtime, content). Invalidated on mtime change.
        self._file_cache: dict[str, tuple[float, str]] = {}
        self._file_cache_hits = 0
        self._file_cache_misses = 0

        # Cross-task tool result memoization: caches tool output within a single cycle.
        # Key = (tool_name, sorted_args_json) → (timestamp, result_str). TTL-based.
        self._tool_memo: dict[str, tuple[float, str]] = {}
        self._tool_memo_hits = 0
        self._tool_memo_misses = 0

        # Event bus: opt-in reactive triggers for campaign tasks.
        self._event_bus: EventBus | None = None
        if config.events.enabled:
            self._event_bus = EventBus()

        # Learned router: adaptive routing stats (refreshed periodically)
        self._learned_stats: LearnedRoutingStats | None = None
        self._learned_stats_cycle: int = -1  # cycle when stats were last built

        # Strategy library: Voyager-inspired learned knowledge (Layer 13)
        self._strategy_library = load_library(config.data_path / "strategy_library.json")

        # Cost monitoring: budget alerts and spend gating
        budget_enabled = config.watcher.budget_daily_usd > 0 or config.watcher.budget_weekly_usd > 0
        self._cost_monitor = CostMonitor(
            CostMonitorConfig(
                enabled=budget_enabled,
                daily_limit_usd=config.watcher.budget_daily_usd,
                weekly_limit_usd=config.watcher.budget_weekly_usd,
                alert_threshold_pct=config.watcher.budget_alert_pct,
                log_path=str(config.data_path / "cost_alerts.jsonl"),
            ),
            run_log_path=config.data_path / "run_log.jsonl",
        )

    def _load_dedup_history(self) -> dict[str, int]:
        """Load persistent dedup history from disk."""
        if self._dedup_path.exists():
            try:
                data = json_mod.loads(self._dedup_path.read_text(encoding="utf-8"))
                return {k: int(v) for k, v in data.items()}
            except Exception:
                return {}
        return {}

    def _save_dedup_history(self) -> None:
        """Persist dedup history to disk (atomic write — no corruption on crash)."""
        import os
        import tempfile
        self._dedup_path.parent.mkdir(parents=True, exist_ok=True)
        # Fix #4: Prune stale entries to prevent unbounded growth
        cutoff = self._runs_completed - 2
        self._success_history = {k: v for k, v in self._success_history.items() if v >= cutoff}
        content = json_mod.dumps(self._success_history)
        fd, tmp_path = tempfile.mkstemp(
            dir=self._dedup_path.parent, suffix=".tmp", prefix=".dedup_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(self._dedup_path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def _cached_file_read(self, path: Path, max_chars: int = 0) -> str | None:
        """Read file with mtime-based cache. Returns None if file doesn't exist."""
        if not self.config.optimizations.file_cache:
            # Cache disabled — read directly
            if not path.exists():
                return None
            try:
                text = path.read_text(encoding="utf-8")
                return text[:max_chars] if max_chars else text
            except OSError:
                return None

        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None

        cached = self._file_cache.get(key)
        if cached and cached[0] == mtime:
            self._file_cache_hits += 1
            text = cached[1]
            return text[:max_chars] if max_chars else text

        # Cache miss — read and store
        self._file_cache_misses += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        self._file_cache[key] = (mtime, text)
        return text[:max_chars] if max_chars else text

    async def _predictive_prefetch(self, prompt: str, tools: dict) -> str:
        """Pre-fetch predictable tool results based on task keywords.

        For email tasks, pre-runs gmail_search. For calendar tasks, pre-runs
        calendar_today/calendar_list. Returns formatted context string to inject
        into the agent prompt, saving 1-2 agent turns.
        """
        if not self.config.optimizations.predictive_prefetch:
            return ""

        parts: list[str] = []

        try:
            if _PREFETCH_EMAIL_RE.search(prompt) and "gmail_search" in tools:
                func = tools["gmail_search"]["func"]
                result = await func({"query": "is:unread newer_than:1d", "max_results": 10})
                text = result.get("content", [{}])[0].get("text", "") if isinstance(result, dict) else str(result)
                if text and len(text) > 20:
                    parts.append(f"[PRE-FETCHED: gmail_search('is:unread newer_than:1d')]\n{text[:2000]}")

            if _PREFETCH_CALENDAR_RE.search(prompt) and "calendar_today" in tools:
                func = tools["calendar_today"]["func"]
                result = await func({"max_results": 10})
                text = result.get("content", [{}])[0].get("text", "") if isinstance(result, dict) else str(result)
                if text and len(text) > 20:
                    parts.append(f"[PRE-FETCHED: calendar_today()]\n{text[:2000]}")
        except Exception as e:
            log.debug("Prefetch failed (non-fatal): %s", e)
            return ""

        if parts:
            log.info("Predictive prefetch: injected %d result(s) into prompt", len(parts))
        return "\n\n".join(parts)

    def _memo_key(self, tool_name: str, args: dict) -> str:
        """Build a stable cache key for tool memoization."""
        sorted_args = json_mod.dumps(args, sort_keys=True)
        return f"{tool_name}:{sorted_args}"

    def _memo_get(self, key: str) -> str | None:
        """Retrieve memoized tool result if TTL hasn't expired."""
        if not self.config.optimizations.tool_memoization:
            return None
        cached = self._tool_memo.get(key)
        if cached is None:
            return None
        ts, result = cached
        ttl = self.config.optimizations.tool_memo_ttl_seconds
        if time.time() - ts > ttl:
            del self._tool_memo[key]
            return None
        self._tool_memo_hits += 1
        return result

    def _memo_set(self, key: str, result: str) -> None:
        """Store a tool result in the memoization cache."""
        if not self.config.optimizations.tool_memoization:
            return
        self._tool_memo_misses += 1
        self._tool_memo[key] = (time.time(), result)

    def _setup_event_sources(self, tools: dict[str, Any]) -> None:
        """Register event sources on the bus (once — preserves baseline hashes across cycles)."""
        if self._event_bus is None:
            return
        # Only set up sources on the first cycle; subsequent cycles reuse
        # existing sources so their _prev_hash baselines carry over.
        if self._event_bus._sources:
            return
        cfg = self.config.events
        if cfg.gmail_source:
            self._event_bus.add_source(GmailEventSource(tools))
        if cfg.calendar_source:
            self._event_bus.add_source(CalendarEventSource(tools, cfg.calendar_window_minutes))
        if cfg.watch_files:
            self._event_bus.add_source(FileChangeSource(cfg.watch_files))

    def _write_heartbeat(self, last_passed: int, last_failed: int) -> None:
        """Write a JSON heartbeat file with current watcher status."""
        try:
            elapsed = time.monotonic() - self._start_time if self._start_time else 0
            # Compute autonomous ratio from recent run log
            summary = self.run_log.summary()
            by_source = summary.get("by_source", {})
            autonomous_ratio = summary.get("autonomous_ratio", 0.0)
            heartbeat = {
                "status": "running",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cycle": self._runs_completed,
                "last_cycle_passed": last_passed,
                "last_cycle_failed": last_failed,
                "total_passed": self._total_passed,
                "total_failed": self._total_task_failures,
                "total_premium": round(self._total_premium_spent, 2),
                "total_cost_usd": round(self._total_cost_usd, 4),
                "uptime_seconds": round(elapsed),
                "dry_run": self.dry_run,
                "campaigns": [str(f) for f in self.campaign_files],
                "file_cache_hits": self._file_cache_hits,
                "file_cache_misses": self._file_cache_misses,
                "tool_memo_hits": self._tool_memo_hits,
                "tool_memo_misses": self._tool_memo_misses,
                "subsystems": {
                    "events": self.config.events.enabled,
                    "ooda": self.config.events.ooda_enabled
                    if self.config.events.enabled
                    else False,
                    "goals": self.config.goals.enabled,
                },
                "by_source": by_source,
                "autonomous_ratio": autonomous_ratio,
            }
            hb_path = self.config.data_path / "heartbeat.json"
            hb_path.parent.mkdir(parents=True, exist_ok=True)
            hb_path.write_text(json_mod.dumps(heartbeat, indent=2), encoding="utf-8")
        except OSError as e:
            log.error("Failed to write heartbeat: %s", e)
        # Also update the health status endpoint
        self._write_health_status(last_passed, last_failed)

    def _write_health_status(self, last_passed: int, last_failed: int) -> None:
        """Write a JSON health status file for lightweight health check polling.

        This provides a simple, always-fresh endpoint at data/health_status.json
        that external monitors can poll without running `secretary health`.
        """
        try:
            elapsed = time.monotonic() - self._start_time if self._start_time else 0
            total_tasks = self._total_passed + self._total_task_failures
            pass_rate = self._total_passed / total_tasks if total_tasks else 1.0
            # Determine health: degraded if recent cycle had failures or pass rate < 80%
            if last_failed > 0 and last_passed == 0:
                status = "error"
            elif last_failed > 0 or pass_rate < 0.8:
                status = "degraded"
            else:
                status = "ok"

            health = {
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": round(elapsed),
                "cycle": self._runs_completed,
                "pass_rate": round(pass_rate, 3),
                "last_cycle": {
                    "passed": last_passed,
                    "failed": last_failed,
                },
                "totals": {
                    "passed": self._total_passed,
                    "failed": self._total_task_failures,
                    "premium": round(self._total_premium_spent, 2),
                    "cost_usd": round(self._total_cost_usd, 4),
                },
            }
            hs_path = self.config.data_path / "health_status.json"
            hs_path.parent.mkdir(parents=True, exist_ok=True)
            hs_path.write_text(json_mod.dumps(health, indent=2), encoding="utf-8")
        except OSError as e:
            log.error("Failed to write health status: %s", e)

    def _load_campaign(self) -> list[dict[str, Any]]:
        """Load campaign tasks from all campaign YAML files, sorted by priority."""
        all_tasks: list[dict[str, Any]] = []
        for cf in self.campaign_files:
            if not cf.exists():
                log.error("Campaign file not found: %s — skipping", cf)
                continue
            try:
                data = yaml.safe_load(cf.read_text(encoding="utf-8"))
            except yaml.YAMLError as e:
                log.error("Failed to parse campaign %s: %s — skipping", cf, e)
                continue
            if not isinstance(data, dict):
                log.error("Campaign %s is not a YAML dict — skipping", cf)
                continue
            tasks = data.get("tasks", [])
            if not tasks:
                log.warning("Campaign %s has no tasks — check YAML structure", cf.name)
            all_tasks.extend(tasks)
        # Sort by priority (lower number = higher priority, default 10)
        all_tasks.sort(key=lambda t: t.get("priority", 10))
        return all_tasks

    async def _run_cycle(self, memory: MemoryStore) -> tuple[int, int, float, float]:
        """Run one cycle of all campaign tasks. Returns (passed, failed, premium_spent, cost_usd).

        Each task is logged to run_log.jsonl with timing, outcome, and output preview.
        Respects premium budget cap: stops adding tasks when budget exhausted.
        Failed tasks are retried up to max_retries times with exponential backoff.
        In dry-run mode, logs what would run without calling the API.
        """
        tasks = self._load_campaign()

        # Clear tool memoization cache at cycle start (fresh data each cycle)
        self._tool_memo.clear()

        # Multi-instance: filter tasks by role and clean stale claims
        if self._coordinator:
            self._coordinator.cleanup_stale_claims()
            tasks = self._coordinator.filter_tasks_for_role(tasks)
            log.info("Coordinator: %d tasks available for role '%s'",
                     len(tasks), self._coordinator.role or "generalist")

        passed = 0
        failed = 0
        cycle_premium_spent = 0.0
        cycle_cost_usd = 0.0

        # Dedup: skip identical prompts already run this cycle
        seen_prompts: set[str] = set()

        # Dependency tracking: task_id → True if passed, False if failed
        task_outcomes: dict[str, bool] = {}

        # Build tool registry once per cycle (shared across all tasks and retry attempts)
        _unrestricted = self.config.file_tools
        _workspace_root = (
            Path(self.config.file_workspace).resolve()
            if self.config.file_workspace and not _unrestricted
            else None
        )
        # Use base data_root (not instance-scoped data_path) for tool registry
        # so google_token.json is found regardless of --instance flag.
        _tools = build_tool_registry(
            Path(self.config.data_root),
            workspace_root=_workspace_root,
            unrestricted_files=_unrestricted,
        )

        # Wrap tool functions with cross-task memoization (same-cycle only).
        # Read-only tools (search/read/list) are safe to cache; mutating tools are not.
        # Fix: check sentinel to prevent double-wrapping on subsequent cycles.
        if self.config.optimizations.tool_memoization:
            for tname, tdef in _tools.items():
                if tname not in _MEMOIZABLE_TOOLS:
                    continue
                _orig = tdef["func"]
                # Skip if already wrapped (prevents closure chain accumulation)
                if getattr(_orig, "_memo_wrapped", False):
                    continue

                def _make_memo_wrapper(orig_func, name):
                    """Create a memoizing async wrapper for a read-only tool function."""
                    async def _memo_wrapper(args):
                        """Async wrapper that checks memo cache before calling the original tool."""
                        key = self._memo_key(name, args)
                        cached = self._memo_get(key)
                        if cached is not None:
                            log.debug("Memo HIT: %s", key[:80])
                            return {"content": [{"type": "text", "text": cached}]}
                        result = await orig_func(args)
                        # Extract text for caching
                        if isinstance(result, dict) and "content" in result:
                            texts = [b.get("text", "") for b in result["content"]
                                     if isinstance(b, dict) and b.get("type") == "text"]
                            self._memo_set(key, "\n".join(texts))
                        return result
                    _memo_wrapper._memo_wrapped = True  # sentinel to prevent re-wrapping
                    return _memo_wrapper

                tdef["func"] = _make_memo_wrapper(_orig, tname)

        # Task batching: group compatible tasks BEFORE the main loop.
        # batch_compatible tasks with the same tier get merged into single agent calls.
        batches = group_into_batches(
            tasks,
            enabled=self.config.optimizations.task_batching,
            max_batch_size=self.config.optimizations.max_batch_size,
            default_tier=self.config.routing.default_tier,
        )
        # Note: group_into_batches already logs merge stats at INFO level

        # Event bus: poll sources and emit cycle_start event.
        cycle_events: list[Event] = []
        if self._event_bus is not None:
            self._setup_event_sources(_tools)
            cycle_events = await self._event_bus.poll_all()
            self._event_bus.emit(Event(type=EventType.CYCLE_START, source="watcher"))
            n_sources = len(self._event_bus._sources)
            if cycle_events:
                log.info("Event bus: %d event(s) from %d source(s)", len(cycle_events), n_sources)
            else:
                log.info("Event bus: polled %d source(s), no new events", n_sources)

        # OODA decision loop: ask a cheap LLM to generate ad-hoc tasks
        # based on the events detected this cycle.
        if (
            cycle_events
            and self._event_bus is not None
            and self.config.events.ooda_enabled
        ):
            ooda_tasks = await run_ooda_cycle(
                self._event_bus, self.run_log, memory, self.config,
            )
            if ooda_tasks:
                log.info("OODA injected %d ad-hoc task(s)", len(ooda_tasks))
                tasks.extend(ooda_tasks)
                # Re-batch with the new tasks included
                batches = group_into_batches(
                    tasks,
                    enabled=self.config.optimizations.task_batching,
                    max_batch_size=self.config.optimizations.max_batch_size,
                    default_tier=self.config.routing.default_tier,
                )

        # Goal planner: proactive task generation from long-horizon goals.
        # Runs on a configurable interval (default: every 8 hours), not every cycle.
        goal_store = None  # Set in goals block, used by step result recording below
        if self.config.goals.enabled:
            goals_file = Path(self.config.goals.goals_file)
            if not goals_file.is_absolute():
                goals_file = Path.cwd() / goals_file
            state_file = Path(self.config.data_root) / "goal_state.json"

            # Pre-flight: log goals config for diagnostics
            if not goals_file.exists():
                log.error(
                    "Goals enabled but goals_file not found: %s — "
                    "goal cycle will be no-op",
                    goals_file,
                )
            _gc = self.config.goals
            log.info(
                "Goal planner active: curriculum=L%d, approval=%s, "
                "tool_policy=%s, max_tier=%s, max_active=%d",
                _gc.curriculum_level, _gc.approval_mode,
                _gc.tool_policy, _gc.max_tier, _gc.max_active_goals,
            )
            goal_store = GoalStore(goals_file, state_file)
            goal_store.load()

            # Persistent cycle counter for graduation cooldown (survives --max-runs 1)
            _persistent_cycle = goal_store._state.get("total_cycles", 0) + 1
            goal_store._state["total_cycles"] = _persistent_cycle

            # Layer 20: Goal Scheduling — filter goals by curriculum level + priority.
            from .goal_scheduler import (
                GRADUATION_LEVEL_ORDER,
                MIN_GRADUATION_SAMPLES,
                apply_auto_graduation,
                build_execution_report,
                check_goal_graduation_rollback,
                check_graduation_eligibility,
                check_graduation_rollback,
                compute_all_trust_scores,
                compute_effective_level,
                evaluate_trust_graduation,
                get_current_level_from_config,
                get_goal_policy,
                get_graduation_overrides,
                is_per_goal_overrides,
                record_execution_report,
                record_graduation_recommendations,
                record_trust_snapshot,
                select_active_goals,
                suggest_policy,
            )

            # Layer 26: Per-goal graduation overrides — no global config mutation.
            # get_goal_policy() is called at task-routing time per-task.
            _active_goals = select_active_goals(
                goal_store.goals,
                curriculum_level=self.config.goals.curriculum_level,
                max_active=self.config.goals.max_active_goals,
                sub_goal_overrides=goal_store._state.get("sub_goal_status", {}),
            )
            if not _active_goals:
                log.info("Goal scheduler: no goals active at curriculum L%d", self.config.goals.curriculum_level)
            # Replace goal_store.goals with active subset for this cycle
            _all_goals = goal_store.goals  # Keep original for trust scoring
            goal_store.goals = _active_goals

            # Collect ALL goal-originated tasks before applying guardrails.
            _goal_tasks: list[dict] = []

            # Reflection: analyze outcomes of previous goal-tasks before review.
            # This feeds verbal feedback into the next goal review prompt.
            try:
                await run_goal_reflection(goal_store, self.run_log, self.config)
            except Exception as e:
                log.warning("Goal reflection failed (non-fatal): %s", e)
                self.health_log.record("reflection_error", "error", f"Goal reflection failed: {e}", source="goal_reflection", cycle=self._runs_completed)

            # Cross-goal meta-reflection: synthesise patterns across all goals.
            try:
                await run_meta_reflection(goal_store, self.run_log, self.config)
            except Exception as e:
                log.warning("Meta-reflection failed (non-fatal): %s", e)
                self.health_log.record("reflection_error", "error", f"Meta-reflection failed: {e}", source="goal_meta_reflection", cycle=self._runs_completed)

            # Progress scoring: compute quantitative metrics and log summary.
            try:
                _snapshots = goal_store._state.get("progress_snapshots", [])
                _progress = compute_progress(
                    goal_store.goals,
                    goal_store._state.get("sub_goal_status", {}),
                    self.run_log,
                    _snapshots,
                )
                stalled = [gid for gid, gp in _progress.items() if gp.stalled]
                if stalled:
                    log.warning("Stalled goals: %s", ", ".join(stalled))
                    # Stall escalation: evaluate and act on stalled goals
                    try:
                        esc_actions = await evaluate_escalations(
                            goal_store.goals, _progress,
                            goal_store._state, self.config,
                        )
                        for ea in esc_actions:
                            log.info(
                                "Escalation [%s] for %s: %s",
                                ea.strategy, ea.goal_id, ea.summary[:100],
                            )
                            if ea.sub_goal_updates:
                                goal_store.apply_updates(ea.sub_goal_updates)
                            if ea.note:
                                goal_store.add_progress_note(ea.note)
                            if ea.tasks:
                                _goal_tasks.extend(ea.tasks)
                        if esc_actions:
                            goal_store.save_state()
                    except Exception as esc_err:
                        log.warning("Stall escalation failed (non-fatal): %s", esc_err)
            except Exception as e:
                log.warning("Goal progress scoring failed (non-fatal): %s", e)
                self.health_log.record("pipeline_error", "error", f"Goal progress scoring failed: {e}", source="goal_progress", cycle=self._runs_completed)

            if is_review_due(goal_store, self.config.goals.review_interval_hours):
                # Prune orphan + blocked goals before review to reduce prompt bloat
                goal_store.prune_orphan_statuses()
                goal_store.prune_stale_goals()
                goal_tasks = await run_goal_review(
                    goal_store, self.run_log, memory, self.config,
                )
                if goal_tasks:
                    log.info("Goal planner injected %d proactive task(s)", len(goal_tasks))
                    _goal_tasks.extend(goal_tasks)

            # Step plan execution: pick up next pending step from active plans.
            # Runs every cycle (not just on review) — steps advance incrementally.
            try:
                step_plans = get_step_plans(goal_store._state)
                step_tasks = []
                for sg_id, plan in step_plans.items():
                    if plan.get("completed") or plan.get("blocked"):
                        continue
                    nxt = get_next_step(goal_store._state, sg_id)
                    if nxt:
                        # Layer 27: Check preconditions before executing
                        _preconditions = nxt.get("preconditions", [])
                        if _preconditions:
                            _base = os.getcwd()
                            _pre_results = check_assertions(_preconditions, _base)
                            _pre_failed = [r for r in _pre_results if not r["passed"]]
                            if _pre_failed:
                                _step_id = nxt.get("step_id", "?")
                                _details = "; ".join(r["detail"] for r in _pre_failed)
                                log.warning(
                                    "Step %s preconditions FAILED (%d/%d): %s — skipping",
                                    _step_id, len(_pre_failed), len(_pre_results), _details[:200],
                                )
                                _fail_reason = f"Precondition failed: {_details[:300]}"
                                record_step_result(
                                    goal_store._state, sg_id, _step_id,
                                    False, _fail_reason,
                                )
                                # Fall through to replanner below
                                nxt = None

                    if not nxt:
                        # No next step — check if a failed step is blocking progress
                        _steps = plan.get("steps", [])
                        _failed = [s for s in _steps if s.get("status") == "failed"]
                        if _failed:
                            _last_failed = _failed[-1]
                            _step_id = _last_failed.get("step_id", "?")
                            _fail_reason = _last_failed.get("result", "Step failed")
                            log.info("Step %s blocked by failed step %s — invoking replanner", sg_id, _step_id)
                            try:
                                _sg_dict = _parent_dict = None
                                _goal_id = plan.get("goal_id", "")
                                for g in goal_store.goals:
                                    if g.get("id") == _goal_id:
                                        _parent_dict = g
                                        for _sg in g.get("sub_goals", []):
                                            if _sg.get("id") == sg_id:
                                                _sg_dict = _sg
                                                break
                                        break
                                _strategy = await handle_step_failure(
                                    goal_store._state, sg_id, _step_id, _fail_reason,
                                    sub_goal=_sg_dict,
                                    parent_goal=_parent_dict,
                                    config=self.config,
                                )
                                log.info(
                                    "Replanner applied '%s' for blocked step %s (sub-goal %s)",
                                    _strategy, _step_id, sg_id,
                                )
                                if _strategy == "block":
                                    goal_store.apply_updates([{
                                        "sub_goal_id": sg_id,
                                        "new_status": "blocked",
                                        "evidence": f"All recovery strategies exhausted for step {_step_id}",
                                    }])
                            except Exception as _rp_err:
                                log.warning("Replanning for blocked step failed (non-fatal): %s", _rp_err)
                            goal_store.save_state()
                        continue  # Try next plan

                    goal_id = plan.get("goal_id", "")
                    _tier_ov = goal_store._state.get("tier_overrides", {}).get(sg_id)
                    st = step_to_task(nxt, sg_id, goal_id, tier_override=_tier_ov)
                    step_tasks.append(st)
                    break  # One step per cycle to keep progress deliberate
                if step_tasks:
                    log.info(
                        "Step plan: executing step %s for sub-goal %s",
                        step_tasks[0].get("id", "?"),
                        step_tasks[0].get("_sub_goal_id", "?"),
                    )
                    _goal_tasks.extend(step_tasks)
            except Exception as e:
                log.warning("Step plan execution failed (non-fatal): %s", e)

            # Self-improvement analysis: mine failures, generate proposals, inject tasks.
            # Runs on its own cooldown (default: 24h).  Proposals become _self_improve tasks.
            # Prioritized: self-improve runs BEFORE other goal tasks to ensure it gets
            # a chance even when other steps are slow or timeout.
            try:
                si_tasks = await run_self_improve_analysis(
                    goal_store._state, self.run_log, self.config,
                    health_log=self.health_log,
                )
                if si_tasks:
                    log.info(
                        "Self-improvement: %d task(s) from failure analysis",
                        len(si_tasks),
                    )
                    # Prepend (not append) so self-improve executes first
                    _goal_tasks = si_tasks + _goal_tasks
                # Always save — analysis modifies state (last_analysis, stale discards)
                # even when no tasks are returned.
                goal_store.save_state()
            except Exception as si_err:
                log.warning("Self-improvement analysis failed (non-fatal): %s", si_err)
                self.health_log.record("analysis_failure", "error", f"Self-improvement analysis failed: {si_err}", source="goal_self_improve", cycle=self._runs_completed)

            # Layer 16: Apply safety guardrails to ALL goal tasks before execution.
            if _goal_tasks:
                # Collect tier overrides from capability-failure detection
                _tier_overrides = goal_store._state.get("tier_overrides", {}) if goal_store else {}
                gr = apply_guardrails(
                    _goal_tasks,
                    max_tier=self.config.goals.max_tier,
                    max_tasks_per_cycle=self.config.goals.max_tasks_per_cycle,
                    tier_overrides=_tier_overrides,
                )
                if gr.warnings:
                    for w in gr.warnings:
                        log.info("Goal guardrail: %s", w)
                if gr.rejected:
                    log.warning(
                        "Goal guardrails rejected %d task(s)", len(gr.rejected),
                    )
                if gr.accepted:
                    # Layer 26: Per-goal approval routing.
                    _review_tasks: list[dict] = []
                    _exec_tasks: list[dict] = []
                    for _task in gr.accepted:
                        _tgid = _task.get("goal_id", "")
                        _policy = get_goal_policy(
                            _tgid, goal_store._state,
                            self.config.goals.approval_mode,
                            self.config.goals.tool_policy,
                        )
                        _am = _policy["approval_mode"]
                        if _am == "review":
                            _review_tasks.append(_task)
                        elif _am == "notify":
                            _exec_tasks.append(_task)
                        else:  # auto
                            _exec_tasks.append(_task)

                    if _review_tasks:
                        ids = submit_tasks(goal_store._state, _review_tasks)
                        log.info(
                            "Approval queue: %d task(s) queued for review (%s)",
                            len(ids), ", ".join(ids),
                        )
                    # Notify-mode tasks: log to queue but execute
                    _notify_tasks = [
                        t for t in _exec_tasks
                        if get_goal_policy(
                            t.get("goal_id", ""), goal_store._state,
                            self.config.goals.approval_mode,
                            self.config.goals.tool_policy,
                        )["approval_mode"] == "notify"
                    ]
                    if _notify_tasks:
                        nids = submit_tasks(goal_store._state, _notify_tasks)
                        for tid in nids:
                            mark_notified(goal_store._state, tid)
                    tasks.extend(_exec_tasks)

            # Auto-approve scoped self-improve proposals if enabled.
            if getattr(self.config.goals, "auto_approve_self_improve", False):
                _auto_ids = auto_approve_self_improve(
                    goal_store._state,
                    max_tier=self.config.goals.max_tier,
                )
                if _auto_ids:
                    log.info(
                        "Auto-approved %d scoped self-improve proposal(s): %s",
                        len(_auto_ids), ", ".join(_auto_ids),
                    )

            # Pick up previously-approved tasks from the queue.
            _approved = get_approved(goal_store._state)
            if _approved:
                approved_tasks = queue_to_tasks(_approved)
                for entry in _approved:
                    mark_executed(goal_store._state, entry["id"])
                log.info(
                    "Approval queue: executing %d approved task(s)",
                    len(approved_tasks),
                )
                tasks.extend(approved_tasks)

            # Housekeeping: prune old queue entries (auto-rejects stale pending).
            _stale_days = getattr(self.config.goals, "auto_reject_pending_days", 3.0)
            prune_old_entries(
                goal_store._state,
                stale_pending_seconds=_stale_days * 86400 if _stale_days > 0 else 0,
            )

            # Layer 20: Trust scoring — compute and record per-goal trust.
            _trust: dict = {}
            _graduation_recs: list = []
            try:
                _rl_entries = [
                    {"goal_id": e.goal_id, "success": e.success}
                    for e in self.run_log.recent(100)
                    if e.source == "goals"
                ]
                _trust = compute_all_trust_scores(
                    _all_goals, goal_store._state, _rl_entries,
                )
                record_trust_snapshot(goal_store._state, _trust)

                # Layer 22: Trust-based graduation — evaluate policy changes.
                _graduation_recs = evaluate_trust_graduation(
                    _trust,
                    current_approval_mode=self.config.goals.approval_mode,
                    current_tool_policy=self.config.goals.tool_policy,
                    state=goal_store._state,
                )
                if _graduation_recs:
                    record_graduation_recommendations(
                        goal_store._state, _graduation_recs,
                    )
                    for rec in _graduation_recs:
                        if rec["action"] == "upgrade":
                            log.info(
                                "Trust graduation: %s → %s recommended for %s (%s)",
                                rec["current_level"], rec["suggested_level"],
                                rec["goal_id"], rec["reason"],
                            )
                        elif rec["action"] == "downgrade":
                            log.warning(
                                "Trust regression: %s → %s for %s (%s)",
                                rec["current_level"], rec["suggested_level"],
                                rec["goal_id"], rec["reason"],
                            )

                # Layer 26: Per-goal auto-graduation — each goal graduates independently.
                if self.config.goals.auto_graduate and _trust:
                    for _gid, _gdata in _trust.items():
                        _total_samples = sum(_gdata.get("sample_sizes", {}).values())
                        if _total_samples < MIN_GRADUATION_SAMPLES:
                            continue

                        _gpol = get_goal_policy(
                            _gid, goal_store._state,
                            self.config.goals.approval_mode,
                            self.config.goals.tool_policy,
                        )
                        _cur_level = _gpol["level"]

                        _suggested = suggest_policy(_gdata["trust_score"])
                        _eff_level = _suggested["level"]

                        # Rollback first (safety-critical, no cooldown)
                        _rollback = check_goal_graduation_rollback(
                            goal_store._state, _gid, _gdata,
                            _persistent_cycle,
                        )
                        if _rollback:
                            log.warning(
                                "AUTO-GRADUATION ROLLBACK [%s]: %s → %s "
                                "(trust degraded)",
                                _gid, _rollback["old_level"],
                                _rollback["new_level"],
                            )
                        elif _eff_level != _cur_level:
                            # Step one level at a time for upgrades
                            _cur_idx = GRADUATION_LEVEL_ORDER.index(_cur_level)
                            _eff_idx = GRADUATION_LEVEL_ORDER.index(_eff_level)
                            if _eff_idx > _cur_idx:
                                _target = GRADUATION_LEVEL_ORDER[_cur_idx + 1]
                            else:
                                _target = _eff_level

                            _eligible, _reason = check_graduation_eligibility(
                                goal_store._state,
                                _persistent_cycle,
                                _target,
                                _cur_level,
                                goal_id=_gid,
                            )
                            if _eligible:
                                _grad_event = apply_auto_graduation(
                                    goal_store._state,
                                    _persistent_cycle,
                                    _target,
                                    _cur_level,
                                    _trust,
                                    goal_id=_gid,
                                )
                                log.info(
                                    "AUTO-GRADUATION [%s]: %s → %s "
                                    "(approval=%s, tools=%s)",
                                    _gid,
                                    _grad_event["old_level"],
                                    _grad_event["new_level"],
                                    _grad_event["approval_mode"],
                                    _grad_event["tool_policy"],
                                )
                            else:
                                log.debug(
                                    "Auto-graduation [%s] not eligible: %s",
                                    _gid, _reason,
                                )
            except Exception as trust_err:
                log.warning("Trust scoring failed (non-fatal): %s", trust_err)
                self.health_log.record("pipeline_error", "error", f"Trust scoring failed: {trust_err}", source="trust_scoring", cycle=self._runs_completed)

            # Layer 22: Execution report — structured post-cycle summary.
            try:
                _n_generated = len(_goal_tasks)
                _n_approved = len(_approved) if _approved else 0
                _n_direct = 0
                if _goal_tasks:
                    try:
                        _n_direct = len(_exec_tasks)
                    except (NameError, UnboundLocalError):
                        pass
                _n_executed = _n_approved + _n_direct
                _report = build_execution_report(
                    goal_store._state,
                    cycle=self._runs_completed,
                    tasks_generated=_n_generated,
                    tasks_approved=_n_approved,
                    tasks_executed=_n_executed,
                    trust_scores=_trust,
                    graduation_recs=_graduation_recs,
                )
                record_execution_report(goal_store._state, _report)
            except Exception as rpt_err:
                log.warning("Execution report failed (non-fatal): %s", rpt_err)

            # Restore full goal list for state consistency
            goal_store.goals = _all_goals
            goal_store.save_state()

            if tasks:
                batches = group_into_batches(
                    tasks,
                    enabled=self.config.optimizations.task_batching,
                    max_batch_size=self.config.optimizations.max_batch_size,
                    default_tier=self.config.routing.default_tier,
                )

        for batch in batches:
            if self._stop or self._quota_exhausted:
                break

            # For batches, use merged_prompt; for solo, use original task
            is_merged = batch.is_batch
            # Process the first task's metadata for routing/deps/dedup (batch inherits from first task)
            task_def = batch.tasks[0]
            prompt = batch.merged_prompt if is_merged else task_def.get("prompt", task_def.get("task", ""))
            tier = task_def.get("tier", None)
            task_id = task_def.get("id")

            if not prompt:
                log.warning("Skipping task with no prompt: %s", task_def)
                continue

            # Event trigger check: tasks with a ``trigger`` field only run
            # when a matching event was detected this cycle.
            trigger = task_def.get("trigger")
            trigger_events: list[Event] = []
            if trigger and self._event_bus is not None:
                trigger_events = self._event_bus.matches_trigger(trigger)
                if not trigger_events:
                    log.debug("Skipping (trigger '%s' not matched): %s", trigger, prompt[:60])
                    continue
            elif trigger and self._event_bus is None:
                # Trigger specified but event bus disabled — skip silently
                log.debug("Skipping (event bus disabled): %s", prompt[:60])
                continue

            # Check dependency: skip if depends_on task didn't pass
            depends_on = task_def.get("depends_on")
            # Normalize list form ([\"x\"] → \"x\")
            if isinstance(depends_on, list):
                depends_on = depends_on[0] if depends_on else None
            if depends_on:
                dep_result = task_outcomes.get(depends_on)
                # Cross-instance: check shared results if local outcome not found
                if dep_result is None and self._coordinator:
                    for sr in self._coordinator.get_all_results():
                        if sr.task_id == depends_on:
                            dep_result = sr.success
                            break
                if dep_result is None:
                    log.warning("Skipping (dependency '%s' not found): %s", depends_on, prompt[:60])
                    if task_id:
                        task_outcomes[task_id] = False
                    continue
                if not dep_result:
                    log.info("Skipping (dependency '%s' failed): %s", depends_on, prompt[:60])
                    if task_id:
                        task_outcomes[task_id] = False
                    continue

            # Track task provenance for feedback loops
            task_source = task_def.get("source", "campaign")
            task_goal_id = task_def.get("goal_id", "")

            # Dedup by prompt hash
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
            if prompt_hash in seen_prompts:
                log.info("Skipping duplicate task: %s", prompt[:60])
                continue
            seen_prompts.add(prompt_hash)

            # Cross-cycle dedup: skip tasks that succeeded in the immediately previous cycle
            skip_if_recent = task_def.get("skip_if_recent", True)
            if skip_if_recent and prompt_hash in self._success_history:
                last_success = self._success_history[prompt_hash]
                if last_success == self._runs_completed - 1:
                    log.info("Skipping (succeeded last cycle): %s", prompt[:60])
                    passed += 1  # count as passed since it already succeeded
                    continue

            # Schedule check: skip tasks outside their time window
            schedule = task_def.get("schedule")
            if schedule and not _check_schedule(schedule):
                log.info("Skipping (schedule: %s): %s", schedule, prompt[:60])
                continue

            # Estimate premium cost before running
            # Refresh learned routing stats every 3 cycles
            if (
                self._learned_stats is None
                or self._runs_completed - self._learned_stats_cycle >= 3
            ):
                try:
                    self._learned_stats = build_stats_from_log(self.run_log)
                    self._learned_stats_cycle = self._runs_completed
                except Exception as exc:
                    log.debug("Learned router stats refresh failed: %s", exc)

            routing = select_model(
                self.config, prompt, tier,
                learned_stats=self._learned_stats,
            )
            task_cost = get_premium_cost(routing.model)

            # Check premium budget
            if self.max_premium_per_cycle > 0:
                if cycle_premium_spent + task_cost > self.max_premium_per_cycle:
                    log.warning(
                        "Premium budget exhausted (%.2f/%.2f) — skipping: %s",
                        cycle_premium_spent, self.max_premium_per_cycle, prompt[:60],
                    )
                    continue

            # Multi-instance: try to claim this task exclusively
            if self._coordinator:
                if not self._coordinator.try_claim(prompt, prompt_hash):
                    owner = self._coordinator.get_claim_owner(prompt, prompt_hash)
                    log.info("Skipping (claimed by %s): %s", owner, prompt[:60])
                    continue

            if self.dry_run:
                log.info(
                    "[DRY RUN] Would run: %s (tier=%s, model=%s, cost=%.2fx)",
                    prompt[:80], routing.tier, routing.model, task_cost,
                )
                passed += 1
                self._success_history[prompt_hash] = self._runs_completed
                if task_id:
                    task_outcomes[task_id] = True
                continue

            # Context preloading: enrich prompt with scratchpad contents
            # Only inject for research/code tasks — email/calendar tasks don't need it
            effective_prompt = prompt
            if (self.config.optimizations.context_preload
                    and _SCRATCHPAD_TASKS_RE.search(prompt)):
                scratchpad_path = self.config.data_path / "scratchpad.md"
                sp_text = self._cached_file_read(scratchpad_path, max_chars=3000)
                if sp_text:
                    effective_prompt = (
                        f"{prompt}\n\n---\n"
                        f"Pre-loaded context (data/scratchpad.md):\n{sp_text}"
                    )

            # Predictive prefetch: pre-fetch gmail/calendar data for matching tasks.
            # Saves 1-2 agent turns by providing data the agent would have fetched anyway.
            prefetched = await self._predictive_prefetch(prompt, _tools)
            if prefetched:
                effective_prompt = (
                    f"{effective_prompt}\n\n---\n"
                    f"Pre-fetched tool results (ALREADY FETCHED — do NOT call these tools again, the data is right here):\n{prefetched}"
                )

            # Event context injection: for triggered tasks, append the triggering event(s)
            # so the agent knows *why* it was activated and has the event payload.
            if trigger_events:
                ev_lines = [f"[Triggered by {len(trigger_events)} event(s)]"]
                for tev in trigger_events[:5]:
                    ev_lines.append(f"  - {tev.summary}")
                    # Include raw text payload for tools that provided it
                    raw = tev.payload.get("raw_text", "")
                    if raw:
                        ev_lines.append(f"    Data: {raw[:1000]}")
                effective_prompt = (
                    f"{effective_prompt}\n\n---\n"
                    + "\n".join(ev_lines)
                )

            # Run task with retry logic
            task_passed = False
            last_error: str | None = None
            last_result: Any = None  # track for metrics
            attempts = 1 + self.max_retries  # 1 initial + N retries

            # Tier escalation: on retry, bump to next tier if escalate_on_retry
            escalate = task_def.get("escalate_on_retry", True)
            tier_order = ["low", "medium", "high"]
            base_tier_idx = tier_order.index(routing.tier) if routing.tier in tier_order else 1

            for attempt in range(attempts):
                if self._stop:
                    break

                # Compute effective tier for this attempt (escalate on retry)
                effective_tier = tier
                if attempt > 0 and escalate and tier is None:
                    escalated_idx = min(base_tier_idx + attempt, len(tier_order) - 1)
                    effective_tier = tier_order[escalated_idx]

                # Delay and logging for retries
                if attempt > 0:
                    delay = self.retry_base_delay * (2 ** (attempt - 1))
                    if effective_tier != tier:
                        retry_routing = select_model(self.config, prompt, effective_tier)
                        task_cost = get_premium_cost(retry_routing.model)
                        log.info(
                            "Retry %d/%d for: %s (after %.0fs, escalated to %s)",
                            attempt, self.max_retries, prompt[:60], delay, effective_tier,
                        )
                    else:
                        log.info("Retry %d/%d for: %s (after %.0fs)", attempt, self.max_retries, prompt[:60], delay)
                    await asyncio.sleep(delay)

                log.info("Running task%s: %s", f" (attempt {attempt + 1})" if attempt > 0 else "", prompt[:80])
                t0 = time.monotonic()
                # Deep-tier tasks get a much longer timeout (hours, not minutes)
                # Oracle goal tasks get a shorter timeout to prevent 600s+ runaway
                if effective_tier == "deep" or tier == "deep":
                    _default_timeout = self.config.watcher.deep_work_timeout
                elif effective_tier == "oracle" and task_source in ("goals", "escalation"):
                    _default_timeout = min(self.config.watcher.task_timeout, 300)
                else:
                    _default_timeout = self.config.watcher.task_timeout
                task_timeout = task_def.get("timeout", _default_timeout)
                progress = direct_agent.RunProgress()
                try:
                    # Self-improvement tasks: route through sandbox pipeline
                    _is_self_improve = task_def.get("_self_improve", False)
                    _det_result = None  # deterministic pipeline result (if matched)
                    if _is_self_improve:
                        from .self_improve import improve as _si_improve
                        from .router import RoutingDecision
                        _si_result = await _si_improve(
                            task=effective_prompt,
                            project_dir=Path.cwd(),
                            config=self.config,
                            auto_promote=self.config.self_improve.auto_promote,
                            target_files=task_def.get("_target_files"),
                            description=task_def.get("_description", ""),
                        )
                        # Adapt ImprovementResult → RunResult-like object
                        result = direct_agent.RunResult(
                            task=effective_prompt,
                            routing=RoutingDecision(
                                tier=self.config.self_improve.tier,
                                model="self-improve",
                                max_turns=0, max_budget_usd=0,
                                reason="self-improvement pipeline",
                            ),
                            text=_si_result.agent_result or "",
                            error=_si_result.error,
                            cost_usd=_si_result.cost_usd,
                            num_turns=_si_result.num_turns,
                            tools_used=["self_improve"],
                        )
                        # Record proposal result in goal state
                        _prop_id = task_def.get("_proposal_id", "")
                        if _prop_id and goal_store:
                            record_proposal_result(
                                goal_store._state, _prop_id,
                                success=_si_result.tests_passed,
                                promoted=_si_result.promoted,
                                changed_files=_si_result.changed_files,
                                error=_si_result.error,
                                test_output=_si_result.test_output,
                            )
                            goal_store.save_state()
                    # Oracle ensemble: route to oracle_run for "oracle" tier
                    elif effective_tier == "oracle":
                        # Layer 26: Per-goal tool policy for goal tasks
                        _effective_tools = _tools
                        if task_source in ("goals", "escalation") and goal_store:
                            _gp = get_goal_policy(
                                task_goal_id, goal_store._state,
                                self.config.goals.approval_mode,
                                self.config.goals.tool_policy,
                            )
                            _effective_tools = filter_tools(
                                _tools, policy=_gp["tool_policy"],
                            )
                        coro = oracle_module.oracle_run(
                            task=effective_prompt,
                            config=self.config,
                            memory=memory,
                            tools=_effective_tools,
                            max_turns=8 if task_source in ("goals", "escalation") else None,
                            _progress=progress,
                            strategy_library=self._strategy_library,
                        )
                    else:
                        # Layer 26: Per-goal tool policy for goal tasks
                        _effective_tools = _tools
                        if task_source in ("goals", "escalation") and goal_store:
                            _gp = get_goal_policy(
                                task_goal_id, goal_store._state,
                                self.config.goals.approval_mode,
                                self.config.goals.tool_policy,
                            )
                            _effective_tools = filter_tools(
                                _tools, policy=_gp["tool_policy"],
                            )
                        # Try deterministic pipeline for simple campaign tasks
                        # (skip for goals/escalation/self-improve — those need LLM reasoning)
                        _det_result = None
                        if task_source not in ("goals", "escalation", "self_improve"):
                            from .deterministic import try_deterministic
                            _det_result = await try_deterministic(
                                effective_prompt, _effective_tools, self.config,
                            )
                        if _det_result is not None:
                            result = _det_result
                            duration = time.monotonic() - t0
                            log.info(
                                "Deterministic pipeline handled task: %s (%dms)",
                                prompt[:80], result.duration_ms,
                            )
                        else:
                            coro = direct_agent.run(
                                task=effective_prompt,
                                config=self.config,
                                memory=memory,
                                force_tier=effective_tier,
                                tools=_effective_tools,
                                max_turns=12 if task_source in ("goals", "escalation") else None,
                                # Budget tuning (v3, 2026-04-20): Sonnet analysis tasks consistently
                                # need 15-25 tool calls (run_python/grep/file_read loops). Default 20
                                # was too tight — bumped to 35. Goals/escalation keep 75 headroom.
                                max_tool_calls=75 if task_source in ("goals", "escalation") else 35,
                                _progress=progress,
                                strategy_library=self._strategy_library,
                            )
                    if _det_result is None and not _is_self_improve:
                        if task_timeout > 0:
                            result = await asyncio.wait_for(coro, timeout=task_timeout)
                        else:
                            result = await coro
                    duration = time.monotonic() - t0

                    # Use actual premium consumed from result if available,
                    # otherwise fall back to single-request multiplier estimate.
                    actual_premium = getattr(result, 'premium_requests', 0.0)
                    effective_premium = actual_premium if actual_premium > 0 else task_cost

                    entry = RunLogEntry(
                        timestamp=RunLog.now(),
                        cycle=self._runs_completed,
                        task=prompt[:200],
                        tier=result.routing.tier,
                        model=result.routing.model,
                        success=result.error is None,
                        output_preview=result.text[:500] if result.text else "",
                        error=result.error,
                        duration_s=round(duration, 1),
                        premium_cost=effective_premium,
                        cost_usd=result.cost_usd,
                        num_turns=result.num_turns,
                        tools_used=result.tools_used,
                        source=task_source,
                        goal_id=task_goal_id,
                    )
                    self.run_log.append(entry)
                    cycle_premium_spent += effective_premium
                    cycle_cost_usd += result.cost_usd

                    if result.error:
                        last_error = result.error
                        last_result = result
                        log.error("Task failed: %s — %s", prompt[:60], result.error)
                        # Log failure to daily markdown log
                        try:
                            md_mem = MarkdownMemory(self.config.workspace_dir)
                            md_mem.append_daily(f"FAILED: {result.error[:150]}", task=prompt[:60])
                        except Exception:
                            pass
                        # Continue to retry if attempts remain
                    else:
                        log.info("Task passed: %s (%.1fs, %.2fx premium)", prompt[:60], duration, task_cost)
                        task_passed = True
                        last_result = result

                        # Strategy extraction (Layer 13): learn from successful tasks
                        try:
                            _base = _interpolate_env(self.config.anthropic_base_url).rstrip("/")
                            maybe_extract_strategy(
                                entry_task=prompt[:200],
                                entry_success=True,
                                entry_tools=result.tools_used,
                                entry_output=result.text[:300] if result.text else "",
                                entry_duration=round(duration, 1),
                                entry_turns=result.num_turns,
                                entry_source=task_source,
                                entry_campaign=str(self.campaign_file.stem),
                                library=self._strategy_library,
                                base_url=_base,
                            )
                        except Exception as _strat_err:
                            log.debug("Strategy extraction skipped: %s", _strat_err)

                        # Append to daily markdown log (OpenClaw memory)
                        try:
                            md_mem = MarkdownMemory(self.config.workspace_dir)
                            summary = result.text[:200] if result.text else "completed"
                            md_mem.append_daily(summary, task=prompt[:60])
                        except Exception as _md_err:
                            log.debug("Markdown memory write skipped: %s", _md_err)

                        break  # Success — no more retries
                except asyncio.TimeoutError:
                    duration = time.monotonic() - t0
                    last_error = f"Task timed out after {task_timeout}s"
                    log.error(
                        "Task timeout: %s — %ds (%d turns, %d tools before cancel)",
                        prompt[:60], task_timeout, progress.num_turns, len(progress.tools_used),
                    )
                    self.run_log.append(RunLogEntry(
                        timestamp=RunLog.now(),
                        cycle=self._runs_completed,
                        task=prompt[:200],
                        tier=progress.tier or tier or self.config.routing.default_tier,
                        model=progress.model,
                        success=False,
                        output_preview="",
                        error=last_error,
                        duration_s=round(duration, 1),
                        premium_cost=progress.premium_requests,
                        num_turns=progress.num_turns,
                        tools_used=list(progress.tools_used),
                        source=task_source,
                        goal_id=task_goal_id,
                    ))
                    # Premium is tentative — API calls happened but may have been interrupted
                except Exception as e:
                    duration = time.monotonic() - t0
                    last_error = str(e)
                    log.error("Task exception: %s — %s", prompt[:60], e)
                    self.run_log.append(RunLogEntry(
                        timestamp=RunLog.now(),
                        cycle=self._runs_completed,
                        task=prompt[:200],
                        tier=tier or self.config.routing.default_tier,
                        model="unknown",
                        success=False,
                        output_preview="",
                        error=str(e),
                        duration_s=round(duration, 1),
                        premium_cost=0,
                        source=task_source,
                        goal_id=task_goal_id,
                    ))
                    # Don't charge premium — no confirmed API usage
                    # Check for quota exhaustion — stop the cycle immediately
                    if _is_quota_error(str(e)):
                        log.error("\u26a0\ufe0f QUOTA EXHAUSTED: %s \u2014 pausing cycle", str(e)[:100])
                        self.health_log.record("quota_exhaustion", "error", f"Quota exhausted: {str(e)[:200]}", source="watcher", cycle=self._runs_completed)
                        self._quota_exhausted = True
                        failed += 1
                        if task_id:
                            task_outcomes[task_id] = False
                        break                    # Continue to retry if attempts remain

            if task_passed:
                passed += 1
                self._success_history[prompt_hash] = self._runs_completed
                if task_id:
                    task_outcomes[task_id] = True
                # For merged batches, mark ALL constituent tasks as passed
                if is_merged:
                    passed += batch.task_count - 1  # first already counted
                    for sub_task in batch.tasks[1:]:
                        sub_id = sub_task.get("id")
                        if sub_id:
                            task_outcomes[sub_id] = True
                        sub_prompt = sub_task.get("prompt", sub_task.get("task", ""))
                        sub_hash = hashlib.sha256(sub_prompt.encode()).hexdigest()[:16]
                        self._success_history[sub_hash] = self._runs_completed
            elif not self._quota_exhausted:
                if self.max_retries > 0 and last_error:
                    log.error("Task exhausted %d retries: %s — %s", self.max_retries, prompt[:60], last_error)
                failed += 1
                if task_id:
                    task_outcomes[task_id] = False
                # For merged batches, mark all as failed
                if is_merged:
                    failed += batch.task_count - 1
                    for sub_task in batch.tasks[1:]:
                        sub_id = sub_task.get("id")
                        if sub_id:
                            task_outcomes[sub_id] = False

            # Strategy outcome recording (Layer 13): update quality scores
            try:
                _cat = extract_category(
                    prompt, source=task_source,
                    campaign=str(self.campaign_file.stem),
                )
                self._strategy_library.record_outcome(_cat, task_passed)
            except Exception as _out_err:
                log.debug("Strategy outcome recording skipped: %s", _out_err)

            # Emit task completion/failure events for downstream triggers
            if self._event_bus is not None and task_id:
                ev_type = EventType.TASK_COMPLETED if task_passed else EventType.TASK_FAILED
                self._event_bus.emit(Event(
                    type=ev_type,
                    source="watcher",
                    payload={"task_id": task_id, "prompt": prompt[:200]},
                    dedup_key=f"task:{task_id}:{self._runs_completed}",
                ))

            # Record step plan result if this was a decomposed step task.
            if task_def.get("_step_id") and goal_store is not None:
                try:
                    _sg_id = task_def.get("_sub_goal_id", "")
                    _step_id = task_def["_step_id"]
                    _evidence = ""
                    if last_result:
                        # Build evidence from full conversation (tool results + final text)
                        _parts = []
                        for msg in getattr(last_result, "messages", []):
                            role = msg.get("role", "")
                            content = msg.get("content", "")
                            if role == "assistant" and isinstance(content, str):
                                _parts.append(content)
                            elif role == "user" and isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "tool_result":
                                        _parts.append(str(block.get("content", ""))[:500])
                        _evidence = "\n---\n".join(_parts)[:4000] if _parts else getattr(last_result, "text", "")[:2000]

                    # Layer 19: Verify step completion against criteria
                    _step_verified = task_passed

                    # Layer 28: Run environment assertions FIRST so results
                    # can be injected into the verification judge prompt.
                    _expected_effects = task_def.get("_expected_effects", [])
                    _assertion_text = ""
                    _eff_results: list[dict] = []
                    if _expected_effects:
                        try:
                            _base = os.getcwd()
                            _eff_results = check_assertions(_expected_effects, _base)
                            _assertion_text = format_assertion_results(_eff_results)
                        except Exception as _eff_err:
                            log.debug("Environment assertion check failed (non-fatal): %s", _eff_err)

                    if task_passed and task_def.get("_verification"):
                        try:
                            _vresult = await verify_step_completion(
                                action=task_def.get("_action", prompt[:300]),
                                verification=task_def["_verification"],
                                agent_output=_evidence,
                                config=self.config,
                                assertion_results_text=_assertion_text,
                            )
                            record_verification(
                                goal_store._state, _step_id, _sg_id,
                                _vresult["verdict"], _vresult.get("reasoning", ""),
                            )
                            if _vresult["verdict"] == FAIL:
                                log.warning(
                                    "Step %s passed execution but FAILED verification: %s",
                                    _step_id, _vresult.get("reasoning", "")[:100],
                                )
                                _step_verified = False
                        except Exception as _verr:
                            log.debug("Verification skipped (fail-open): %s", _verr)

                    # Environment assertions still hard-override verdict as safety net
                    if _step_verified and _eff_results:
                        _eff_failed = [r for r in _eff_results if not r["passed"]]
                        if _eff_failed:
                            log.warning(
                                "Step %s PASSED verification but FAILED %d/%d "
                                "environment assertions — overriding to FAIL",
                                _step_id, len(_eff_failed), len(_eff_results),
                            )
                            _step_verified = False
                            _evidence += f"\n\n{_assertion_text}"
                            record_verification(
                                goal_store._state, _step_id, _sg_id,
                                FAIL,
                                f"Environment assertions failed ({len(_eff_failed)}/{len(_eff_results)}): "
                                + "; ".join(r["detail"] for r in _eff_failed)[:200],
                            )
                        else:
                            log.info(
                                "Step %s environment assertions: %d/%d passed",
                                _step_id, len(_eff_results), len(_eff_results),
                            )

                    record_step_result(
                        goal_store._state, _sg_id, _step_id,
                        _step_verified, _evidence,
                    )
                    if _step_verified:
                        # If plan just completed, mark sub-goal as done
                        _plan = goal_store._state.get("step_plans", {}).get(_sg_id, {})
                        if _plan.get("completed"):
                            _goal_id = _plan.get("goal_id", "")
                            goal_store.apply_updates([{
                                "sub_goal_id": _sg_id,
                                "new_status": "done",
                                "evidence": f"All {len(_plan.get('steps', []))} decomposed steps completed and verified",
                            }])
                            log.info("Sub-goal %s completed via step plan", _sg_id)
                            # Layer 19: Check if this completes the parent goal
                            _completed_goals = detect_completed_goals(
                                goal_store.goals, goal_store._state,
                            )
                            if _completed_goals:
                                # Layer 29: Validated harness loop — 3 consecutive passes required
                                for _cg_id in list(_completed_goals):
                                    try:
                                        _cg = next((g for g in goal_store.goals if g.get("id") == _cg_id), None)
                                        if _cg and _cg.get("success_criteria"):
                                            _base = os.getcwd()
                                            _hints = extract_context_hints(_cg)
                                            _best_code, _vresults = await harness_validation_loop(
                                                goal_id=_cg_id,
                                                goal_description=_cg.get("description", ""),
                                                success_criteria=_cg["success_criteria"],
                                                config=self.config,
                                                known_good_dir=_base,
                                                context_hints=_hints,
                                            )
                                            if _best_code is None:
                                                _n_fail = sum(1 for r in _vresults if not r.passed)
                                                log.warning(
                                                    "Goal %s harness validation FAILED — %d attempts, not marking complete",
                                                    _cg_id, _n_fail,
                                                )
                                                _completed_goals.remove(_cg_id)
                                            else:
                                                log.info(
                                                    "Goal %s harness validation COMPLETE (%d attempts, 3 consecutive passes)",
                                                    _cg_id, len(_vresults),
                                                )
                                    except Exception as _h_err:
                                        log.debug("Harness validation skipped for goal %s (non-fatal): %s", _cg_id, _h_err)
                                if _completed_goals:
                                    mark_goals_completed(
                                        goal_store.goals, goal_store._state, _completed_goals,
                                    )
                    else:
                        # Step failed — invoke adaptive replanner
                        try:
                            _sg_dict = None
                            _parent_dict = None
                            _goal_id = goal_store._state.get("step_plans", {}).get(_sg_id, {}).get("goal_id", "")
                            for g in goal_store.goals:
                                if g.get("id") == _goal_id:
                                    _parent_dict = g
                                    for sg in g.get("sub_goals", []):
                                        if sg.get("id") == _sg_id:
                                            _sg_dict = sg
                                            break
                                    break
                            strategy = await handle_step_failure(
                                goal_store._state, _sg_id, _step_id, _evidence,
                                sub_goal=_sg_dict,
                                parent_goal=_parent_dict,
                                config=self.config,
                            )
                            log.info(
                                "Replanner applied '%s' for step %s (sub-goal %s)",
                                strategy, _step_id, _sg_id,
                            )
                            if strategy == "block":
                                goal_store.apply_updates([{
                                    "sub_goal_id": _sg_id,
                                    "new_status": "blocked",
                                    "evidence": f"All recovery strategies exhausted for step {_step_id}",
                                }])
                        except Exception as replan_err:
                            log.warning("Replanning failed (non-fatal): %s", replan_err)
                    goal_store.save_state()
                except Exception as e:
                    log.warning("Step result recording failed (non-fatal): %s", e)

            # Layer 30: Record verification for non-step goal tasks
            # (fixes Execution-to-Verification Gap identified by meta-reflection)
            elif task_source in ("goals", "escalation") and goal_store is not None:
                try:
                    _verdict = PASS if task_passed else FAIL
                    _evidence_text = ""
                    if last_result:
                        _evidence_text = getattr(last_result, "text", "")[:2000]
                    elif last_error:
                        _evidence_text = str(last_error)[:300]
                    record_verification(
                        goal_store._state,
                        step_id=task_id or f"task-{prompt_hash}",
                        sub_goal_id="",
                        verdict=_verdict,
                        reasoning=f"Non-step goal task {'passed' if task_passed else 'failed'}: {_evidence_text[:200]}",
                        goal_id=task_goal_id,
                    )
                    goal_store.save_state()
                except Exception as e:
                    log.debug("Non-step verification recording failed (non-fatal): %s", e)

            # Record metrics and publish result for coordination
            self._record_task_metrics(
                prompt, prompt_hash, routing, task_passed, last_result, last_error,
                task_id=task_id,
            )

        if self.max_premium_per_cycle > 0:
            log.info("Cycle premium spend: %.2f / %.2f", cycle_premium_spent, self.max_premium_per_cycle)

        # Record cycle summary to pipeline health log for self-visibility
        self.health_log.record(
            "cycle_metadata", "info",
            f"Cycle {self._runs_completed}: {passed} passed, {failed} failed, "
            f"{cycle_premium_spent:.2f}x premium, ${cycle_cost_usd:.2f} USD",
            source="watcher",
            details=f"quota_hit={self._quota_exhausted}",
            cycle=self._runs_completed,
        )

        return passed, failed, cycle_premium_spent, cycle_cost_usd

    async def run(self) -> None:
        """Main watcher loop. Runs until stopped or max_runs reached."""
        mode = "[DRY RUN] " if self.dry_run else ""
        budget_str = (
            f", premium cap: {self.max_premium_per_cycle}x/cycle"
            if self.max_premium_per_cycle > 0 else ""
        )
        log.info(
            "%sWatch mode started: %s every %d minutes%s",
            mode, self.campaign_file, self.interval, budget_str,
        )

        # PID file for external status checks
        write_pidfile(self.config.data_path)

        # Set up signal handlers for graceful shutdown
        install_sigbreak_handler(self._request_stop)
        if sys.platform != "win32":
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._request_stop)
        # On Windows, KeyboardInterrupt is caught in the except block

        memory = MemoryStore.load(self.config.memory_path)
        self._start_time = time.monotonic()

        # Register with coordinator (multi-instance)
        if self._coordinator:
            self._coordinator.register()
            log.info("Multi-instance coordination active (role=%s)", self._coordinator.role or "generalist")

        while not self._stop:
            self._runs_completed += 1
            now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            log.info("Watch run #%d starting at %s", self._runs_completed, now)

            try:
                passed, failed, premium_spent, cost_usd = await self._run_cycle(memory)
            except KeyboardInterrupt:
                log.info("Interrupted — shutting down gracefully")
                memory.consolidate()
                memory.save()
                self._save_dedup_history()
                break
            except Exception as e:
                log.error("Watch cycle error: %s", e)
                passed, failed, premium_spent, cost_usd = 0, 1, 0.0, 0.0

            self._total_passed += passed
            self._total_task_failures += failed
            self._total_premium_spent += premium_spent
            self._total_cost_usd += cost_usd
            if failed > 0:
                self._runs_failed += 1
                # Send failure notification if configured
                if self.config.watcher.notify_email:
                    self._notify_failures(failed, passed)

            log.info(
                "Watch run #%d done: %d passed, %d failed (%.2fx premium)",
                self._runs_completed, passed, failed, premium_spent,
            )

            # === Housekeeping: ALWAYS runs, even on quota exhaustion ===
            # (Fix #3: moved above quota check so `continue` can't skip these)

            # Consolidate memory periodically (every N cycles to reduce overhead)
            if self._runs_completed % self._consolidate_every == 0:
                memory.consolidate()
                # Strategy library consolidation: decay quality, prune stale strategies
                try:
                    pruned = self._strategy_library.consolidate()
                    if pruned:
                        log.info("Strategy library: pruned %d stale strategies", pruned)
                except Exception as _cons_err:
                    log.debug("Strategy consolidation skipped: %s", _cons_err)
            memory.save()

            # Persist dedup history
            self._save_dedup_history()

            # Write heartbeat
            self._write_heartbeat(passed, failed)

            # Budget monitoring: check spend limits and send alerts
            budget_alert = self._cost_monitor.check_and_alert()
            if budget_alert and self.config.watcher.notify_email:
                self._cost_monitor.send_alert_email(
                    budget_alert, self.config.watcher.notify_email, self.config.data_path,
                )
            if self._cost_monitor.is_budget_exhausted():
                log.warning(
                    "Budget exhausted — pausing for 60 minutes. "
                    "Daily/weekly USD spend has reached the configured limit."
                )
                self.health_log.record(
                    "budget_exhausted", "warning",
                    "Daily or weekly budget limit reached — pausing watcher",
                    source="watcher.budget_monitor",
                    cycle=self._runs_completed,
                )
                try:
                    await asyncio.sleep(60 * 60)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    break
                continue

            # Coordinator: heartbeat + clear cycle claims for next round
            if self._coordinator:
                self._coordinator.heartbeat({
                    "tasks_completed": self._total_passed,
                    "tasks_failed": self._total_task_failures,
                    "total_turns": 0,  # aggregated from metrics if needed
                })
                self._coordinator.clear_cycle()

            # If quota was exhausted this cycle, pause for a long time
            if self._quota_exhausted:
                log.warning(
                    "⚠️ Quota exhausted — pausing for 60 minutes before retrying. "
                    "Check your Copilot Pro premium quota."
                )
                self._quota_exhausted = False  # Reset for next cycle
                try:
                    await asyncio.sleep(60 * 60)  # 60-minute cooldown
                except (asyncio.CancelledError, KeyboardInterrupt):
                    break
                continue

            # Check max runs
            if self.max_runs > 0 and self._runs_completed >= self.max_runs:
                log.info("Max runs (%d) reached — stopping", self.max_runs)
                break

            # Wait for next cycle
            wait_minutes = self.interval
            if failed > 0 and self.pause_on_failure:
                _mult = max(1.0, getattr(self.config.watcher, "pause_on_failure_multiplier", 1.0))
                if _mult > 1.0:
                    wait_minutes = int(wait_minutes * _mult)
                    log.info("Failures detected — next run in %d minutes (x%.1f)", wait_minutes, _mult)
                else:
                    log.info("Failures detected — iterating at normal pace (%d min)", wait_minutes)
            else:
                log.info("Next run in %d minutes. Press Ctrl+C to stop.", wait_minutes)

            try:
                await asyncio.sleep(wait_minutes * 60)
            except (asyncio.CancelledError, KeyboardInterrupt):
                break

        self._print_summary()
        self._write_stopped_heartbeat()
        cleanup_pidfile(self.config.data_path)

        # Coordinator: release claims and deregister
        if self._coordinator:
            released = self._coordinator.release_all_claims()
            if released:
                log.info("Released %d stale claims", released)
            self._coordinator.deregister()

    def _notify_failures(self, failed: int, passed: int) -> None:
        """Draft a failure notification email via Gmail API (no LLM needed)."""
        try:
            from .mcp_tools.google_auth import build_gmail_service
            from .currency import format_cost
            import base64
            from email.mime.text import MIMEText

            to = self.config.watcher.notify_email
            subject = f"[Secretary] Cycle #{self._runs_completed}: {failed} task(s) failed"

            # Gather recent failures for details
            recent = self.run_log.recent(20)
            cycle_failures = [
                e for e in recent
                if e.cycle == self._runs_completed and not e.success
            ]
            failure_details = ""
            if cycle_failures:
                failure_details = "\nFailed tasks:\n"
                for e in cycle_failures:
                    err = e.error or "unknown error"
                    failure_details += f"  - {e.task[:80]} ({e.tier}): {err[:120]}\n"

            body = (
                f"Watcher cycle #{self._runs_completed} completed with {failed} failure(s) "
                f"and {passed} success(es).\n"
                f"{failure_details}\n"
                f"Total premium spent: {self._total_premium_spent:.2f}x\n"
                f"Total cost: {format_cost(self._total_cost_usd)}\n\n"
                f"Check logs with: secretary logs --failed --last 10\n"
            )
            svc = build_gmail_service(self.config.data_path)
            message = MIMEText(body)
            message["To"] = to
            message["Subject"] = subject
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
            log.info("Failure notification drafted to %s", to)
        except Exception as e:
            log.warning("Could not send failure notification: %s (type: %s)", e, type(e).__name__)

    def _write_stopped_heartbeat(self) -> None:
        """Write a final heartbeat marking watcher as stopped."""
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        heartbeat = {
            "status": "stopped",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cycle": self._runs_completed,
            "total_passed": self._total_passed,
            "total_failed": self._total_task_failures,
            "total_premium": round(self._total_premium_spent, 2),
            "uptime_seconds": round(elapsed),
        }
        hb_path = self.config.data_path / "heartbeat.json"
        hb_path.parent.mkdir(parents=True, exist_ok=True)
        hb_path.write_text(json_mod.dumps(heartbeat, indent=2), encoding="utf-8")

        # Write stopped health status
        total_tasks = self._total_passed + self._total_task_failures
        pass_rate = self._total_passed / total_tasks if total_tasks else 1.0
        health = {
            "status": "stopped",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "uptime_seconds": round(elapsed),
            "cycle": self._runs_completed,
            "pass_rate": round(pass_rate, 3),
            "last_cycle": {"passed": 0, "failed": 0},
            "totals": {
                "passed": self._total_passed,
                "failed": self._total_task_failures,
                "premium": round(self._total_premium_spent, 2),
                "cost_usd": round(self._total_cost_usd, 4),
            },
        }
        hs_path = self.config.data_path / "health_status.json"
        hs_path.parent.mkdir(parents=True, exist_ok=True)
        hs_path.write_text(json_mod.dumps(health, indent=2), encoding="utf-8")

    def _print_summary(self) -> None:
        """Print a rich session summary on exit."""
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        total_tasks = self._total_passed + self._total_task_failures
        rate = f"{self._total_passed / total_tasks:.0%}" if total_tasks else "N/A"

        from .currency import format_cost
        cost_str = format_cost(self._total_cost_usd) if self._total_cost_usd > 0 else "N/A"
        log.info(
            "Watcher stopped. Cycles: %d (%d with failures), "
            "Tasks: %d passed / %d failed (%s), "
            "Premium: %.2fx, Cost: %s, Uptime: %s",
            self._runs_completed, self._runs_failed,
            self._total_passed, self._total_task_failures, rate,
            self._total_premium_spent, cost_str,
            self._format_duration(elapsed),
        )

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format a duration in seconds as a human-readable string (e.g. '2h 15m')."""
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h {m}m"
        if m:
            return f"{m}m {s}s"
        return f"{s}s"

    def _record_task_metrics(
        self,
        prompt: str,
        prompt_hash: str,
        routing: Any,
        task_passed: bool,
        result: Any,
        error: str | None,
        task_id: str | None = None,
    ) -> None:
        """Record task-level metrics and publish coordination results."""
        if self._metrics and result:
            metric = TaskMetric(
                instance_id=self.config.instance_id or "default",
                task_hash=prompt_hash,
                prompt_preview=prompt[:120],
                tier=routing.tier,
                model=routing.model,
                success=task_passed,
                num_turns=result.num_turns,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                duration_s=result.duration_ms / 1000.0,
                cost_usd=result.cost_usd,
                reasoning_effort=self.config.reasoning_effort,
                tools_used=result.tools_used,
                tool_calls_total=len(result.tools_used),
                quality_score=getattr(result, 'quality_score', 0.0),
            )
            try:
                self._metrics.record(metric)
            except OSError as e:
                log.warning("Failed to record metrics: %s", e)

        if self._coordinator and result:
            coord_result = CoordTaskResult(
                task_hash=prompt_hash,
                instance_id=self.config.instance_id,
                prompt=prompt[:500],
                success=task_passed,
                task_id=task_id or "",
                output_preview=result.text[:300] if result.text else "",
                error=error,
                completed_at=datetime.now(timezone.utc).isoformat(),
                duration_s=result.duration_ms / 1000.0,
                num_turns=result.num_turns,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cost_usd=result.cost_usd,
                tier=routing.tier,
                model=routing.model,
            )
            try:
                self._coordinator.publish_result(coord_result)
            except OSError as e:
                log.warning("Failed to publish coordination result: %s", e)

    def _request_stop(self) -> None:
        """Signal the watcher loop to stop after the current cycle."""
        log.info("Stop requested")
        self._stop = True
