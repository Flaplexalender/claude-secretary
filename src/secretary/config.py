"""Configuration for Claude Secretary.

Loads from config.yaml with environment variable interpolation.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, model_validator


_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env(value: str) -> str:
    """Replace ${VAR} and ${VAR:-default} with environment values."""
    def _replace(m: re.Match) -> str:
        """Replace a single ${VAR} or ${VAR:-default} match with its env value."""
        var = m.group(1)
        if ":-" in var:
            name, default = var.split(":-", 1)
            return os.environ.get(name.strip(), default.strip())
        return os.environ.get(var.strip(), m.group(0))
    return _ENV_VAR_RE.sub(_replace, value)


def _interpolate_dict(d: dict) -> dict:
    """Recursively interpolate environment variables in a nested dict."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = _interpolate_env(v)
        elif isinstance(v, dict):
            out[k] = _interpolate_dict(v)
        elif isinstance(v, list):
            out[k] = [_interpolate_env(i) if isinstance(i, str) else i for i in v]
        else:
            out[k] = v
    return out


class ModelTier(BaseModel):
    """Model assignment for a complexity tier."""
    model: str                         # e.g. "claude-sonnet-4.6"
    max_turns: int = 30
    max_budget_usd: float = 0.0       # 0 = no SDK-level cap
    description: str = ""

    @model_validator(mode="after")
    def _check_max_turns(self) -> ModelTier:
        """Validate max_turns >= 1 and max_budget_usd >= 0."""
        if self.max_turns < 1:
            raise ValueError(f"max_turns must be >= 1, got {self.max_turns}")
        if self.max_budget_usd < 0:
            raise ValueError(f"max_budget_usd must be >= 0, got {self.max_budget_usd}")
        return self


class RoutingConfig(BaseModel):
    """Model routing: which model for which complexity."""
    tiers: dict[str, ModelTier] = {
        "free": ModelTier(
            model="gpt-4.1",
            max_turns=3,
            description="Free tier (0×) — trivial lookups, formatting, health checks",
        ),
        "low": ModelTier(
            model="claude-haiku-4.5",
            max_turns=10,
            description="Cheap tier (0.33x) — simple lookups, formatting, quick Q&A",
        ),
        "medium": ModelTier(
            model="claude-sonnet-4.6",
            max_turns=25,
            description="Standard work — code, email, research",
        ),
        "high": ModelTier(
            model="claude-opus-4.7",
            max_turns=30,
            max_budget_usd=5.0,
            description="Complex — architecture, multi-file, deep analysis",
        ),
        "deep": ModelTier(
            model="claude-opus-4.7",
            max_turns=200,
            max_budget_usd=0.0,
            description="Deep work — long-horizon tasks, unlimited exploration, hours-long sessions",
        ),
        "oracle": ModelTier(
            model="oracle-ensemble",
            max_turns=12,
            max_budget_usd=0.0,
            description="Oracle ensemble — free workers + Opus checkpoints (post-prefix fallback)",
        ),
    }
    default_tier: str = "medium"

    @model_validator(mode="after")
    def _check_default_tier(self) -> RoutingConfig:
        """Validate that default_tier is one of the defined tiers."""
        # Ensure "deep" and "oracle" tiers always exist (user config.yaml may omit them)
        if "deep" not in self.tiers:
            self.tiers["deep"] = ModelTier(
                model="claude-opus-4.7",
                max_turns=200,
                max_budget_usd=0.0,
                description="Deep work — long-horizon tasks, unlimited exploration, hours-long sessions",
            )
        if "oracle" not in self.tiers:
            self.tiers["oracle"] = ModelTier(
                model="oracle-ensemble",
                max_turns=12,
                max_budget_usd=0.0,
                description="Oracle ensemble — free workers + Opus checkpoints (post-prefix fallback)",
            )
        if self.default_tier not in self.tiers:
            raise ValueError(
                f"default_tier '{self.default_tier}' not in tiers: {list(self.tiers.keys())}"
            )
        return self


class WatcherConfig(BaseModel):
    """24/7 watcher settings."""
    interval_minutes: int = 30
    max_runs: int = 0                  # 0 = unlimited
    pause_on_failure: bool = True
    campaign_file: str = "campaign.yaml"
    max_premium_per_cycle: float = 0.0  # 0 = unlimited, e.g. 3.0 = stop after 3x premium
    max_retries: int = 3               # retries per failed task with exponential backoff (0 = no retry)
    retry_base_delay: float = 5.0      # seconds, doubles each retry
    task_timeout: int = 600            # max seconds per task (10 min, reduced from 20 to prevent gateway 504s)
    deep_work_timeout: int = 14400     # timeout for deep-tier tasks (4 hours, increased from 3h for long-horizon work)
    notify_email: str = ""             # email address for failure notifications (empty = disabled)
    budget_daily_usd: float = 0.0      # Alert + skip tasks when daily spend exceeds this (0 = disabled)
    budget_weekly_usd: float = 0.0     # Alert + skip tasks when weekly spend exceeds this (0 = disabled)
    budget_alert_pct: int = 80         # Alert at this % of budget consumed

    @model_validator(mode="after")
    def _check_watcher_values(self) -> WatcherConfig:
        """Validate watcher interval, retry count, and timeout values."""
        if self.interval_minutes < 1:
            raise ValueError(f"interval_minutes must be >= 1, got {self.interval_minutes}")
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.task_timeout < 0:
            raise ValueError(f"task_timeout must be >= 0, got {self.task_timeout}")
        return self


class MemoryConfig(BaseModel):
    """Memory persistence settings."""
    short_max: int = 20
    long_max: int = 50
    path: str = "data/memory.json"


class SelfImproveConfig(BaseModel):
    """Self-improvement pipeline settings."""
    auto_promote: bool = False         # Require human approval by default
    test_timeout: int = 300            # seconds (2162+ tests need ~100s; 120 was too tight)
    sandbox_dir: str = ""              # auto-generated if empty
    keep_sandbox: bool = False         # keep sandbox after run for manual review
    tier: str = "high"                 # Model tier for sandbox agent (high = Opus)
    analysis_cooldown_hours: float = 0.5   # Min time between failure analyses
    stagnation_cooldown_hours: float = 1.0 # Min time between stagnation checks


class CurrencyConfig(BaseModel):
    """Currency display settings."""
    display_currency: str = "CAD"     # Display currency for costs
    usd_to_cad_rate: float = 1.44     # USD → CAD exchange rate

    @model_validator(mode="after")
    def _check_rate(self) -> CurrencyConfig:
        """Validate that the exchange rate is positive."""
        if self.usd_to_cad_rate <= 0:
            raise ValueError(f"usd_to_cad_rate must be > 0, got {self.usd_to_cad_rate}")
        return self


_VALID_REASONING = {"", "low", "medium", "high", "max"}


class OptimizationConfig(BaseModel):
    """Efficiency optimizations — toggleable for A/B testing via multi-instance."""
    selective_tools: bool = True     # Filter tool schemas by task keywords
    turn_budget_signal: bool = True  # Inject remaining turn count after turn 2
    context_preload: bool = True     # Pre-read scratchpad into task prompt
    conversation_summary: bool = True  # Compress old messages after threshold
    summary_after_turn: int = 5      # Turn at which to summarize history
    dynamic_max_tokens: bool = True  # Scale down output budget on later turns
    always_opus: bool = True         # When agent_prefix=true, always use Opus regardless of routing
    aggressive_context: bool = True  # Pre-read project files matching task keywords into prompt
    one_shot_simple: bool = True     # Force simple tasks to max_turns=3 with full context injection
    # Use free models (GPT-4.1 = 0× multiplier) for trivial tasks in paid mode
    use_free_models: bool = True
    # Turn budgets when agent_prefix=false — cap turns per tier.
    # Free=2 (0× each), Haiku=3 ($0.013 each), Sonnet=5 ($0.04 each), Opus=4 ($0.12 each).
    # Tighter limits force more work per turn. Opus at 4 turns = 12× premium max.
    paid_turn_limits: dict[str, int] = {"free": 2, "low": 3, "medium": 5, "high": 4, "deep": 12, "oracle": 12}
    # Per-task premium budget — max premium multiplier units per task tier.
    # Free=0 (costs nothing), Haiku=1.0, Sonnet=5.0, Opus=12.0, Oracle=9.0 (~3 checkpoints).
    task_premium_budget: dict[str, float] = {"free": 0.0, "low": 1.0, "medium": 5.0, "high": 12.0, "deep": 36.0, "oracle": 9.0}
    # Cross-cycle file cache in watcher (avoid re-reading files every cycle)
    file_cache: bool = True
    # Force tool_choice="any" on first turn to eliminate planning-only waste turns
    force_first_tool: bool = True
    # Pre-fetch predictable tool results (gmail/calendar) before agent starts
    predictive_prefetch: bool = True
    # Cross-task tool result memoization within a watcher cycle
    tool_memoization: bool = True
    tool_memo_ttl_seconds: int = 300  # 5-minute TTL for memoized tool results
    # Task batching — merge consecutive batch_compatible tasks into single agent calls.
    # Saves 2-3 agent invocations per watcher cycle for monitoring campaigns.
    task_batching: bool = True
    max_batch_size: int = 3            # Max tasks to merge into one agent call
    # Haiku-as-planner: use cheap model (Haiku) to plan complex tasks before Opus executes.
    # Saves 2-3 Opus exploration turns by giving it a clear plan upfront.
    haiku_planner: bool = True


class EventConfig(BaseModel):
    """Event-driven campaign triggers.  Opt-in: disabled by default."""
    enabled: bool = False                   # Master switch for event bus
    watch_files: list[str] = []             # File paths to monitor for changes
    gmail_source: bool = True               # Poll new-email events (requires gmail tools)
    calendar_source: bool = True            # Poll calendar-change events
    calendar_window_minutes: int = 30       # How far ahead to look for calendar events
    ooda_enabled: bool = False              # OODA decision loop: LLM assesses events and generates ad-hoc tasks
    ooda_model: str = "claude-haiku-4.5"  # Model for OODA planner (Haiku = cheapest)


class GoalConfig(BaseModel):
    """Proactive goal-driven planning.  Opt-in: disabled by default."""
    enabled: bool = False                     # Master switch for goal planner
    goals_file: str = "goals.yaml"            # Path to goal definitions YAML
    review_interval_hours: int = 8            # How often to review goals (hours)
    review_model: str = "claude-3-haiku-20240307"    # Cheap model for planning
    max_tasks_per_review: int = 3             # Cap proactive tasks per review
    max_tier: str = "medium"                  # Max tier for goal-generated tasks (guardrail)
    max_tasks_per_cycle: int = 5              # Hard cap on ALL goal tasks per cycle
    tool_policy: str = "read-only"            # Tool restriction: read-only | supervised | full
    approval_mode: str = "review"             # Approval: review (queue), notify (exec+log), auto (silent)
    curriculum_level: int = 1                 # 0-3: gates which goals can run (0=none, 1=safe, 2=standard, 3=full)
    max_active_goals: int = 3                 # Focus window — max goals active per cycle (overrides curriculum default)
    auto_graduate: bool = False               # Layer 23: auto-apply graduation recs when trust data warrants


class MultiInstanceConfig(BaseModel):
    """Multi-instance coordination settings."""
    role: str = ""                # Instance specialization: researcher, triager, builder, monitor, "" = generalist
    coordinate: bool = False      # Enable cross-instance coordination (claim-based task distribution)
    shared_dir: str = "data/shared"  # Shared coordination directory


class SecretaryConfig(BaseModel):
    """Top-level configuration."""
    data_root: str = "data"
    anthropic_base_url: str = "${ANTHROPIC_BASE_URL:-http://localhost:4141}"
    agent_prefix: bool = True  # Prepend few-shot conversation prefix for tool-use priming
    reasoning_effort: str = ""  # "" = off, "low"/"medium"/"high"/"max" = use OpenAI endpoint with thinking
    file_workspace: str = ""   # Path for sandboxed file tools (empty = disabled)
    workspace_dir: str = "workspace"  # Identity/soul/skills files (OpenClaw orientation)
    file_tools: bool = False   # Unrestricted file read/write/list (no sandbox)
    instance_id: str = ""      # Multi-instance: unique ID namespaces data files (empty = default)
    multi: MultiInstanceConfig = MultiInstanceConfig()
    optimizations: OptimizationConfig = OptimizationConfig()
    routing: RoutingConfig = RoutingConfig()
    watcher: WatcherConfig = WatcherConfig()
    memory: MemoryConfig = MemoryConfig()
    self_improve: SelfImproveConfig = SelfImproveConfig()
    currency: CurrencyConfig = CurrencyConfig()
    events: EventConfig = EventConfig()
    goals: GoalConfig = GoalConfig()
    mcp_servers: dict[str, dict[str, Any]] = {}

    @model_validator(mode="after")
    def _check_reasoning_effort(self) -> SecretaryConfig:
        """Validate reasoning_effort is one of the allowed values."""
        if self.reasoning_effort not in _VALID_REASONING:
            raise ValueError(
                f"reasoning_effort must be one of {_VALID_REASONING}, got '{self.reasoning_effort}'"
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def interpolate_strings(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _interpolate_dict(data)
        return data

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> SecretaryConfig:
        """Load configuration from a YAML file with env var interpolation, or return defaults."""
        p = Path(path)
        if p.exists():
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            return cls.model_validate(raw)
        return cls()

    @property
    def data_path(self) -> Path:
        """Data directory path, namespaced by instance_id if set."""
        base = Path(self.data_root)
        if self.instance_id:
            return base / self.instance_id
        return base

    @property
    def memory_path(self) -> Path:
        """Memory file path, namespaced by instance_id to avoid write conflicts."""
        if self.instance_id:
            # Namespace memory per instance to avoid concurrent write conflicts
            p = Path(self.memory.path)
            return p.parent / f"{p.stem}-{self.instance_id}{p.suffix}"
        return Path(self.memory.path)

    @property
    def shared_data_path(self) -> Path:
        """Path for shared cross-instance coordination data."""
        return Path(self.multi.shared_dir)

    @property
    def metrics_path(self) -> Path:
        """Path for metrics data (shared across instances)."""
        return Path(self.multi.shared_dir) / "metrics"
