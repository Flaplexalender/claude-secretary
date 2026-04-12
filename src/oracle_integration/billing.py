"""Oracle integration — billing and tier-routing logic.

Maps task tiers (low / medium / high) to appropriate OracleConfig settings,
controlling worker count, checkpoint frequency, and escalation strategy.
"""
from __future__ import annotations

from typing import Any

from secretary.oracle import OracleConfig, FREE_MODELS, CHECKPOINT_MODEL, oracle_run
from secretary.config import SecretaryConfig
from secretary.memory import MemoryStore


# ---------------------------------------------------------------------------
# Tier → OracleConfig mapping
# ---------------------------------------------------------------------------

def tier_to_oracle_config(tier: str) -> OracleConfig:
    """Map a task tier to an OracleConfig with appropriate resource settings.

    Tiers
    -----
    low    — cheap/fast: 2 workers, infrequent checkpoints, few turns.
    medium — balanced:   2 workers, moderate checkpoints, standard turns.
    high   — thorough:   2 workers, aggressive checkpoints, more turns.

    All tiers route through the oracle ensemble (not a single-model path).
    """
    tier = tier.lower().strip()

    if tier == "low":
        return OracleConfig(
            worker_models=list(FREE_MODELS),   # 2 free workers
            checkpoint_model=CHECKPOINT_MODEL,
            checkpoint_interval=8,             # Opus checks every 8 worker turns (rare)
            max_turns=10,                      # Short budget for cheap tasks
            max_checkpoints=1,                 # At most 1 Opus review
            escalate_on_disagreement=False,    # Skip escalation — save cost
            escalation_cooldown=0,
            parallel_workers=True,
            worker_timeout=20.0,
        )

    elif tier == "medium":
        return OracleConfig(
            worker_models=list(FREE_MODELS),   # 2 free workers
            checkpoint_model=CHECKPOINT_MODEL,
            checkpoint_interval=6,             # Opus checks every 6 worker turns
            max_turns=14,                      # Standard budget
            max_checkpoints=2,                 # Up to 2 Opus reviews
            escalate_on_disagreement=True,     # Escalate on worker disagreement
            escalation_cooldown=2,
            parallel_workers=True,
            worker_timeout=30.0,
        )

    elif tier in ("high", "deep"):
        return OracleConfig(
            worker_models=list(FREE_MODELS),   # 2 free workers
            checkpoint_model=CHECKPOINT_MODEL,
            checkpoint_interval=4,             # Frequent Opus reviews
            max_turns=20,                      # Generous budget for complex tasks
            max_checkpoints=4,                 # Up to 4 Opus reviews
            escalate_on_disagreement=True,     # Always escalate disagreements
            escalation_cooldown=1,             # Short cooldown for aggressiveness
            parallel_workers=True,
            worker_timeout=45.0,
        )

    else:
        # Fallback: treat unknown tiers as medium
        return tier_to_oracle_config("medium")


# ---------------------------------------------------------------------------
# Tier router — decides whether a task routes to oracle ensemble
# ---------------------------------------------------------------------------

_ORACLE_TIERS = frozenset({"low", "medium", "high", "deep"})


def should_route_to_oracle(tier: str) -> bool:
    """Return True for all tiers that the oracle ensemble handles.

    Previously only the 'oracle'-specific tier was forwarded; this function
    ensures low / medium / high all flow through oracle_run as well.
    """
    return tier.lower().strip() in _ORACLE_TIERS


async def route_task(
    task: str,
    tier: str,
    config: SecretaryConfig | None = None,
    memory: MemoryStore | None = None,
    tools: dict[str, Any] | None = None,
    max_turns: int | None = None,
) -> Any:
    """Route a task to the oracle ensemble based on its tier.

    Parameters
    ----------
    task:      Natural-language task description.
    tier:      One of 'low', 'medium', 'high' (or 'deep').
    config:    SecretaryConfig (optional).
    memory:    MemoryStore (optional).
    tools:     Tool registry (optional).
    max_turns: Override max turns from OracleConfig (optional).

    Returns
    -------
    RunResult from oracle_run.
    """
    oracle_config = tier_to_oracle_config(tier)
    if max_turns is not None:
        oracle_config.max_turns = max_turns

    return await oracle_run(
        task=task,
        config=config,
        memory=memory,
        tools=tools,
        oracle_config=oracle_config,
        max_turns=max_turns,
    )
