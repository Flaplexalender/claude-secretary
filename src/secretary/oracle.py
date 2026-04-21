"""Oracle Ensemble — post-prefix fallback architecture.

When the message prefix is unavailable, this module provides
a cost-efficient alternative: 3 free models (GPT-4.1, GPT-4o, GPT-5-mini)
do the actual work, with Opus checkpoints every N turns for course correction.

Cost model:
  - Free worker turns: GPT-4.1 etc. (0× multiplier)
  - Opus checkpoints: every checkpoint_interval turns (3× multiplier)
  - Typical task (8 turns, interval=4): 2 Opus checkpoints
  - vs. pure Opus: significant savings

Architecture:
  1. Free models generate tool calls independently (parallel queries)
  2. Majority vote selects which tools to execute
  3. Every checkpoint_interval turns, Opus reviews progress + corrects
  4. If free models all disagree (no majority), Opus decides that turn

Usage:
  Direct:   result = await oracle_run(task, config, tools=tool_registry)
  Watcher:  force_tier="oracle" in campaign YAML
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import SecretaryConfig, _interpolate_env
from .context_builder import build_identity_prompt
from .direct_agent import (
    RunResult,
    RunProgress,
    _build_tool_schemas,
    _CATEGORY_KEYWORDS,
    _execute_tool,
    _openai_stream_call,
    _parse_openai_response,
    _to_openai_messages,
    _to_openai_tools,
    _score_quality,
)
from .memory import MemoryStore
from .router import RoutingDecision, get_premium_cost
from .strategy_library import StrategyLibrary

log = logging.getLogger(__name__)

# Free models (0× multiplier) — these are the "oracle workers"
FREE_MODELS = ["gpt-4.1", "gpt-4o"]

# Opus for checkpoint reviews
CHECKPOINT_MODEL = "claude-opus-4.7"

# Minimum agreement threshold for majority vote (2 of 3 = 66%)
MAJORITY_THRESHOLD = 2

# Oracle-specific task hints — more prescriptive than direct_agent hints
# to maximize worker agreement on approach (convergence > flexibility).
_ORACLE_TASK_HINTS: dict[str, str] = {
    "research": (
        "APPROACH: Use grep_search(pattern, include_pattern=file) for each target file. "
        "Call all searches in parallel. Do NOT read entire files — grep finds patterns faster."
    ),
    "file": (
        "APPROACH: file_read all mentioned files in parallel. "
        "For 'explain' or 'describe' questions, read the ENTIRE file — "
        "comprehension requires full context, look for ALL code paths "
        "(conditionals, fallbacks, edge cases). "
        "Use grep_search only for pattern searches across many files."
    ),
    "implement": (
        "APPROACH: grep_search to locate targets, then file_edit all changes in one batch. "
        "Run tests with run_command after edits."
    ),
    "email": "APPROACH: gmail_search first, then gmail_read matching results in parallel.",
    "calendar": "APPROACH: calendar_today + calendar_search in parallel.",
}


@dataclass
class OracleConfig:
    """Oracle ensemble settings."""
    worker_models: list[str] = field(default_factory=lambda: list(FREE_MODELS))
    checkpoint_model: str = CHECKPOINT_MODEL
    checkpoint_interval: int = 6    # Opus reviews every N worker turns
    max_turns: int = 14             # Total turns (workers + checkpoints)
    max_checkpoints: int = 3        # Cap Opus calls per task
    escalate_on_disagreement: bool = True  # Opus decides when workers can't agree
    escalation_cooldown: int = 2    # Suppress disagreement escalation for N turns after checkpoint
    escalation_threshold_minutes: int = 5  # Minutes before escalating stalled tasks to Opus
    parallel_workers: bool = True   # Query all workers in parallel
    worker_timeout: float = 30.0    # Per-worker timeout in seconds (0 = no timeout)
    max_prompt_size_bytes: int = 100_000  # 100 KB safety threshold to prevent 400 errors


@dataclass
class VoteResult:
    """Result of a majority vote across workers."""
    tool_calls: list[dict[str, Any]]     # Winning tool calls (exact match)
    text: str                             # Winning text response
    agreement: float                       # 0.0-1.0 agreement ratio
    escalated: bool = False               # True if Opus had to decide
    voter_count: int = 0                  # How many models voted
    soft_tool_calls: list[dict[str, Any]] = field(default_factory=list)  # Tool-name-only majority (different args)


def _build_oracle_system_prompt(
    memory: MemoryStore | None,
    task: str,
    max_turns: int,
    is_checkpoint: bool = False,
    strategy_library: StrategyLibrary | None = None,
    workspace_dir: str = "workspace",
) -> str:
    """Build system prompt for oracle workers or checkpoint reviews."""
    if is_checkpoint:
        parts = [
            f"You are reviewing work done by smaller AI models on this task.",
            "ROLE: Senior reviewer. Check for errors, missed steps, wrong approaches.",
            "RULES: (1) If the work so far looks correct, say 'CONTINUE' and optionally suggest next steps.",
            "(2) If you see errors, provide corrected tool calls to fix them.",
            "(3) If the task is nearly complete, provide the final answer.",
            "(4) Be concise — your time is expensive.",
        ]
    else:
        parts = [
            f"AI agent. {max_turns} turns total.",
            "RULES: (1) Call tools to complete the task. (2) Be direct — no narration. "
            "(3) Prefer parallel tool calls when possible. "
            "(4) For explanation tasks, address every part of the question — completeness matters.",
        ]
        # Add category-specific strategy hints so workers converge on approach
        _matched_cat = ""
        for cat, pattern in _CATEGORY_KEYWORDS.items():
            if pattern.search(task):
                hint = _ORACLE_TASK_HINTS.get(cat)
                if hint:
                    parts.append(f"\n{hint}")
                _matched_cat = cat
                break

        # Inject learned strategies for this task category (Layer 13)
        if strategy_library and _matched_cat:
            strat_section = strategy_library.format_for_prompt(_matched_cat)
            if strat_section:
                parts.append(f"\n{strat_section}")

    # Inject workspace identity (OpenClaw orientation)
    identity = build_identity_prompt(workspace_dir)
    if identity:
        parts.append(f"\n{identity}")

    if memory:
        long_mem = memory.get_long()
        short_mem = memory.get_short()
        if long_mem or short_mem:
            mem_parts = []
            if long_mem:
                mem_parts.append("## Memory")
                for entry in long_mem[-5:]:
                    mem_parts.append(f"- {entry}")
            if short_mem:
                mem_parts.append("## Recent")
                for entry in short_mem[-3:]:
                    mem_parts.append(f"- {entry}")
            parts.append("\n" + "\n".join(mem_parts))

    return "\n".join(parts)


def _normalize_tool_call(tc: dict[str, Any]) -> str:
    """Create a hashable signature for a tool call (name + sorted args).

    Used for majority voting — we want to match tool calls that do the same
    thing even if argument order differs.
    """
    name = tc.get("name", "")
    args = tc.get("input", tc.get("arguments", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}
    # Sort args for deterministic comparison
    sorted_args = json.dumps(args, sort_keys=True)
    return f"{name}:{sorted_args}"


def _majority_vote(
    responses: list[dict[str, Any]],
    threshold: int = MAJORITY_THRESHOLD,
) -> VoteResult:
    """Take majority vote across worker responses.

    Votes on individual tool calls — if 2/3 workers call the same tool with
    the same args, that tool call wins. Also votes on text responses for
    non-tool turns.

    Returns VoteResult with the winning tool calls and agreement score.
    """
    if not responses:
        return VoteResult(tool_calls=[], text="", agreement=0.0, voter_count=0)

    n = len(responses)

    # Extract tool calls from each response
    all_tool_calls: list[list[dict[str, Any]]] = []
    all_texts: list[str] = []

    for resp in responses:
        tool_calls = []
        text_parts = []
        for block in resp.get("content", []):
            if block.get("type") == "tool_use":
                tool_calls.append(block)
            elif block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        all_tool_calls.append(tool_calls)
        all_texts.append(" ".join(text_parts).strip())

    # Vote on tool calls
    # Build frequency map of normalized tool calls
    call_counter: Counter[str] = Counter()
    sig_to_call: dict[str, dict[str, Any]] = {}
    for worker_calls in all_tool_calls:
        # Deduplicate per worker (a worker shouldn't double-vote)
        seen_sigs: set[str] = set()
        for tc in worker_calls:
            sig = _normalize_tool_call(tc)
            if sig not in seen_sigs:
                call_counter[sig] += 1
                seen_sigs.add(sig)
                # Keep one representative for each signature
                if sig not in sig_to_call:
                    sig_to_call[sig] = tc

    # Select tool calls that meet the majority threshold
    winning_calls = []
    for sig, count in call_counter.items():
        if count >= threshold:
            winning_calls.append(sig_to_call[sig])

    # Calculate agreement
    if call_counter:
        max_agreement = max(call_counter.values()) / n
    else:
        max_agreement = 1.0  # All agree on no tools

    # Soft tool-name voting: if workers call the same tool but with different
    # args, pick the tool name + use the most-voted arg variant as a fallback.
    # This prevents escalation when the disagreement is only about arguments.
    soft_calls: list[dict[str, Any]] = []
    if not winning_calls and call_counter:
        name_counter: Counter[str] = Counter()
        name_to_best: dict[str, tuple[int, dict[str, Any]]] = {}
        for worker_calls in all_tool_calls:
            seen_names: set[str] = set()
            for tc in worker_calls:
                tname = tc.get("name", "")
                if tname and tname not in seen_names:
                    name_counter[tname] += 1
                    seen_names.add(tname)
                    # Track the best (most-voted exact sig) variant
                    sig = _normalize_tool_call(tc)
                    prev_count = name_to_best.get(tname, (0, tc))[0]
                    sig_count = call_counter.get(sig, 0)
                    if sig_count > prev_count:
                        name_to_best[tname] = (sig_count, tc)
        for tname, count in name_counter.items():
            if count >= threshold:
                soft_calls.append(name_to_best[tname][1])

    # Vote on text: pick majority text, or longest when all unique (better for
    # LLM-judged comprehension tasks where thorough answers score higher).
    non_empty = [t for t in all_texts if t]
    if not non_empty:
        winning_text = ""
    else:
        text_counter: Counter[str] = Counter(non_empty)
        best_text, best_count = text_counter.most_common(1)[0]
        if best_count >= threshold:
            winning_text = best_text
        else:
            # No majority — pick the longest (most thorough) response
            winning_text = max(non_empty, key=len)

    return VoteResult(
        tool_calls=winning_calls,
        text=winning_text,
        agreement=max_agreement,
        voter_count=n,
        soft_tool_calls=soft_calls,
    )


async def _query_worker(
    base_url: str,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    oai_tools: list[dict[str, Any]],
    max_tokens: int = 8192,
) -> dict[str, Any]:
    """Query a single free worker model via OpenAI endpoint.

    Returns parsed response in Anthropic-style format (content blocks).
    """
    oai_messages = _to_openai_messages(system_prompt, messages)
    payload: dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
    }
    if oai_tools:
        payload["tools"] = oai_tools

    try:
        raw = await asyncio.to_thread(_openai_stream_call, base_url, payload)
        response = _parse_openai_response(raw)

        # Convert back to Anthropic-style content blocks for unified handling
        content = []
        for block in response.content:
            if block.type == "text" and block.text:
                content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        return {
            "content": content,
            "stop_reason": response.stop_reason,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "model": model,
        }
    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as e:
        log.warning("Worker %s failed: %s", model, e)
        return {"content": [], "stop_reason": "error", "usage": {}, "model": model, "error": str(e)}


async def _query_checkpoint(
    base_url: str,
    model: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
    oai_tools: list[dict[str, Any]],
    task: str,
    turn_history_summary: str,
    reasoning_effort: str = "high",
    max_tokens: int = 16384,
) -> dict[str, Any]:
    """Query Opus for a checkpoint review with extended thinking."""
    # Inject review context as a user message
    review_prompt = (
        f"CHECKPOINT REVIEW — The following work has been done by cheaper models:\n\n"
        f"{turn_history_summary}\n\n"
        f"Original task: {task}\n\n"
        f"Review the progress. If correct, respond with tool calls for the next steps. "
        f"If there are errors, provide corrected tool calls."
    )
    checkpoint_messages = list(messages)
    checkpoint_messages.append({"role": "user", "content": review_prompt})

    oai_messages = _to_openai_messages(system_prompt, checkpoint_messages)
    payload: dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
        "reasoning_effort": reasoning_effort,
    }
    if oai_tools:
        payload["tools"] = oai_tools

    raw = await asyncio.to_thread(_openai_stream_call, base_url, payload)
    response = _parse_openai_response(raw)

    content = []
    for block in response.content:
        if block.type == "text" and block.text:
            content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
    return {
        "content": content,
        "stop_reason": response.stop_reason,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
        "model": model,
    }


def _summarize_turns(messages: list[dict[str, Any]], anchor: int) -> str:
    """Build a concise summary of tool calls and results for checkpoint review."""
    parts = []
    turn_num = 0
    for i in range(anchor, len(messages)):
        msg = messages[i]
        role = msg["role"]
        content = msg.get("content", "")
        if role == "assistant":
            turn_num += 1
            if isinstance(content, list):
                tools = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if tools:
                    tool_names = [t["name"] for t in tools]
                    parts.append(f"Turn {turn_num}: Called {', '.join(tool_names)}")
                if texts:
                    text = " ".join(t for t in texts if t)
                    if text:
                        parts.append(f"  Text: {text[:200]}")
            elif isinstance(content, str) and content.strip():
                parts.append(f"Turn {turn_num}: {content[:200]}")
        elif role == "user" and isinstance(content, list):
            # Tool results
            results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
            for r in results:
                result_text = r.get("content", "")
                if isinstance(result_text, str) and result_text.strip():
                    parts.append(f"  Result: {result_text[:150]}")
    return "\n".join(parts) if parts else "No work done yet."


# ── Dynamic checkpoint interval ──────────────────────────────────────────────
# Complex tasks (implement, analyze, audit) → tighter interval → more Opus reviews.
# Simple tasks (read, list, check) → wider interval → fewer Opus reviews.
_COMPLEX_TASK_RE = re.compile(
    r"\b(implement|build|create|refactor|migrate|port|audit|analyze|debug"
    r"|fix|investigate|research|benchmark|optimize|redesign|improve|evaluate)\b",
    re.I,
)
_SIMPLE_TASK_RE = re.compile(
    r"\b(read|list|check|show|find|search|report|count|get|fetch"
    r"|what|which|how.?many|summarize|describe)\b",
    re.I,
)


def _dynamic_checkpoint_interval(task: str, base_interval: int) -> int:
    """Return a task-complexity-adjusted checkpoint interval.

    Complex tasks get a shorter interval (more Opus reviews for course-correction).
    Simple tasks get a longer interval (fewer Opus reviews = fewer premium units).
    Result is clamped to [4, 10] so as not to create runaway costs or no-ops.
    """
    is_complex = bool(_COMPLEX_TASK_RE.search(task))
    is_simple = bool(_SIMPLE_TASK_RE.search(task))
    if is_complex and not is_simple:
        return max(4, min(10, base_interval - 2))   # tighter = more reviews
    if is_simple and not is_complex:
        return max(4, min(10, base_interval + 2))  # wider = fewer reviews
    return max(4, min(10, base_interval))  # mixed signals — use default, still clamped


async def oracle_run(
    task: str,
    config: SecretaryConfig | None = None,
    memory: MemoryStore | None = None,
    tools: dict[str, Any] | None = None,
    oracle_config: OracleConfig | None = None,
    max_turns: int | None = None,
    _progress: RunProgress | None = None,
    strategy_library: StrategyLibrary | None = None,
) -> RunResult:
    """Run a task using the oracle ensemble architecture.

    Flow:
      1. Worker turns: Query free models, majority vote, execute tools
      2. Checkpoint turns: Every N worker turns, Opus reviews and corrects
      3. Escalation: If workers disagree, Opus decides that turn

    Returns a RunResult compatible with the existing watcher/logging system.
    """
    if config is None:
        config = SecretaryConfig.load()
    if memory is None:
        memory = MemoryStore.load(config.memory_path)
    if oracle_config is None:
        oracle_config = OracleConfig()

    # Task-adaptive checkpoint interval: simple → wider gap, complex → tighter
    _base_interval = _dynamic_checkpoint_interval(task, oracle_config.checkpoint_interval)
    if _base_interval != oracle_config.checkpoint_interval:
        log.info(
            "Dynamic interval: %d → %d (task complexity adjustment)",
            oracle_config.checkpoint_interval, _base_interval,
        )

    effective_max_turns = max_turns or oracle_config.max_turns
    base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")

    # Build tool schemas
    tool_schemas = _build_tool_schemas(tools) if tools else []
    oai_tools = _to_openai_tools(tool_schemas) if tool_schemas else []

    # System prompts
    _ws_dir = getattr(config, 'workspace_dir', 'workspace')
    worker_prompt = _build_oracle_system_prompt(memory, task, effective_max_turns, strategy_library=strategy_library, workspace_dir=_ws_dir)
    checkpoint_prompt = _build_oracle_system_prompt(memory, task, effective_max_turns, is_checkpoint=True, workspace_dir=_ws_dir)

    # Initialize message history (Anthropic format for internal tracking)
    messages: list[dict[str, Any]] = [{"role": "user", "content": task}]
    anchor = 1  # First message is the task

    # Result tracking
    routing = RoutingDecision(
        tier="oracle",
        model="oracle-ensemble",
        max_turns=effective_max_turns,
        max_budget_usd=0.0,
        reason="oracle ensemble (free workers + Opus checkpoints)",
        confidence="high",
        premium_multiplier=0.0,  # Mixed — tracked per-turn below
    )
    result = RunResult(task=task, routing=routing)
    if _progress is not None:
        _progress.model = "oracle-ensemble"
        _progress.tier = "oracle"

    import time
    t0 = time.monotonic()
    total_premium = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    checkpoints_used = 0
    worker_turns = 0
    all_tools_used: list[str] = []
    final_text = ""
    consecutive_stalls = 0  # Consecutive turns with no tools and low agreement
    last_checkpoint_worker_turn = 0  # Track worker turn count at last checkpoint

    try:
        for turn in range(effective_max_turns):
            # Adaptive checkpoint: trigger earlier when recent agreement is low
            effective_interval = _base_interval
            if consecutive_stalls >= 2:
                effective_interval = 1  # Escalate immediately after repeated stalls
            worker_turns_since_checkpoint = worker_turns - last_checkpoint_worker_turn
            is_checkpoint_turn = (
                worker_turns > 0
                and worker_turns_since_checkpoint >= effective_interval
                and checkpoints_used < oracle_config.max_checkpoints
            )

            if is_checkpoint_turn:
                # ── Opus checkpoint ──
                log.info("Oracle checkpoint %d (after %d worker turns)",
                         checkpoints_used + 1, worker_turns)

                turn_summary = _summarize_turns(messages, anchor)
                checkpoint_resp = await _query_checkpoint(
                    base_url=base_url,
                    model=oracle_config.checkpoint_model,
                    system_prompt=checkpoint_prompt,
                    messages=messages,
                    oai_tools=oai_tools,
                    task=task,
                    turn_history_summary=turn_summary,
                    reasoning_effort=config.reasoning_effort or "high",
                )

                checkpoints_used += 1
                last_checkpoint_worker_turn = worker_turns
                opus_premium = get_premium_cost(oracle_config.checkpoint_model)
                total_premium += opus_premium
                total_input_tokens += checkpoint_resp.get("usage", {}).get("input_tokens", 0)
                total_output_tokens += checkpoint_resp.get("usage", {}).get("output_tokens", 0)

                # Process checkpoint response
                content_blocks = checkpoint_resp.get("content", [])
                tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
                text_blocks = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
                checkpoint_text = " ".join(t for t in text_blocks if t)

                if not tool_uses:
                    # Opus says task is done or says CONTINUE
                    if checkpoint_text:
                        final_text = checkpoint_text
                    if "CONTINUE" not in checkpoint_text.upper():
                        log.info("Opus checkpoint: task complete")
                        break
                    # CONTINUE — let workers keep going
                    continue

                # Opus provided corrective tool calls — execute them
                messages.append({"role": "assistant", "content": content_blocks})
                tool_results = []
                for tc in tool_uses:
                    tool_result = await _execute_tool(tc["name"], tc["input"], tools or {})
                    all_tools_used.append(tc["name"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": tool_result,
                    })
                messages.append({"role": "user", "content": tool_results})

            else:
                # ── Worker turn (free models) ──
                if oracle_config.parallel_workers:
                    # Query all workers in parallel (with per-worker timeout)
                    async def _timed_worker(m: str) -> dict[str, Any]:  # Per-worker timeout is independent of cumulative task time
                        timeout = oracle_config.worker_timeout
                        coro = _query_worker(
                            base_url=base_url,
                            model=m,
                            system_prompt=worker_prompt,
                            messages=messages,
                            oai_tools=oai_tools,
                        )
                        if timeout > 0:
                            try:
                                return await asyncio.wait_for(coro, timeout=timeout)
                            except asyncio.TimeoutError:
                                log.warning("Worker %s timed out after %.0fs", m, timeout)
                                return {"content": [], "stop_reason": "error", "usage": {}, "model": m, "error": "timeout"}
                        return await coro

                    worker_responses = await asyncio.gather(
                        *[_timed_worker(m) for m in oracle_config.worker_models]
                    )
                else:
                    # Sequential (for debugging)
                    worker_responses = []
                    for model in oracle_config.worker_models:
                        resp = await _query_worker(
                            base_url=base_url,
                            model=model,
                            system_prompt=worker_prompt,
                            messages=messages,
                            oai_tools=oai_tools,
                        )
                        worker_responses.append(resp)

                # Filter out failed workers
                good_responses = [r for r in worker_responses if r.get("stop_reason") != "error"]
                if not good_responses:
                    log.error("All workers failed on turn %d", turn + 1)
                    result.error = "All oracle workers failed"
                    break

                # Track token usage from workers (free, but useful for monitoring)
                for wr in good_responses:
                    total_input_tokens += wr.get("usage", {}).get("input_tokens", 0)
                    total_output_tokens += wr.get("usage", {}).get("output_tokens", 0)
                # Workers are free (0× multiplier) — no premium cost
                # total_premium += 0

                # Majority vote
                vote = _majority_vote(good_responses, threshold=MAJORITY_THRESHOLD)

                # Use soft tool-name matches as fallback before escalating
                effective_calls = vote.tool_calls
                if not effective_calls and vote.soft_tool_calls:
                    log.info("Turn %d: using soft tool-name match (%d tools, workers agreed on tool but not args)",
                             turn + 1, len(vote.soft_tool_calls))
                    effective_calls = vote.soft_tool_calls

                # Escalation: if workers disagree and Opus is available
                # Use agreement-based threshold instead of "completely empty" check
                # Suppress escalation during cooldown after a checkpoint —
                # workers need turns to incorporate Opus corrections
                in_cooldown = (
                    checkpoints_used > 0
                    and (worker_turns - last_checkpoint_worker_turn)
                        < oracle_config.escalation_cooldown
                )
                needs_escalation = (
                    not effective_calls
                    and vote.agreement < 0.5
                    and oracle_config.escalate_on_disagreement
                    and checkpoints_used < oracle_config.max_checkpoints
                    and not in_cooldown
                )
                if needs_escalation:
                    log.info("Workers disagree on turn %d (agreement=%.1f%%) — escalating to Opus",
                             turn + 1, vote.agreement * 100)

                    turn_summary = _summarize_turns(messages, anchor)
                    checkpoint_resp = await _query_checkpoint(
                        base_url=base_url,
                        model=oracle_config.checkpoint_model,
                        system_prompt=checkpoint_prompt,
                        messages=messages,
                        oai_tools=oai_tools,
                        task=task,
                        turn_history_summary=turn_summary,
                        reasoning_effort=config.reasoning_effort or "high",
                    )
                    checkpoints_used += 1
                    last_checkpoint_worker_turn = worker_turns
                    total_premium += get_premium_cost(oracle_config.checkpoint_model)
                    total_input_tokens += checkpoint_resp.get("usage", {}).get("input_tokens", 0)
                    total_output_tokens += checkpoint_resp.get("usage", {}).get("output_tokens", 0)

                    # Use Opus response instead of vote
                    content_blocks = checkpoint_resp.get("content", [])
                    escalated_tools = [b for b in content_blocks if b.get("type") == "tool_use"]
                    escalated_text = " ".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
                    effective_calls = escalated_tools
                    vote = VoteResult(
                        tool_calls=escalated_tools,
                        text=escalated_text,
                        agreement=1.0,
                        escalated=True,
                        voter_count=1,
                    )

                log.info("Turn %d: vote agreement=%.0f%% escalated=%s tools=%d",
                         turn + 1, vote.agreement * 100, vote.escalated,
                         len(effective_calls))

                worker_turns += 1

                if not effective_calls:
                    # No tools this turn — track stalls for adaptive escalation
                    consecutive_stalls += 1

                    if vote.text:
                        final_text = vote.text

                    # Check if majority of workers signaled end_turn (not all — more lenient)
                    stop_reasons = [r.get("stop_reason") for r in good_responses]
                    end_turn_count = sum(1 for sr in stop_reasons if sr == "end_turn")
                    if end_turn_count >= MAJORITY_THRESHOLD:
                        log.info("%d/%d workers signal task complete",
                                 end_turn_count, len(good_responses))
                        break

                    # Append text to conversation so workers see evolving context
                    if vote.text:
                        messages.append({"role": "assistant", "content": [
                            {"type": "text", "text": vote.text}
                        ]})
                        messages.append({"role": "user", "content":
                            "Continue with the task. If you need to use tools, call them now. "
                            "If the task is complete, provide the final answer."
                        })
                    continue
                else:
                    consecutive_stalls = 0  # Reset on productive turn

                # Build assistant message from voted tool calls
                assistant_content = []
                if vote.text:
                    assistant_content.append({"type": "text", "text": vote.text})
                for tc in effective_calls:
                    assistant_content.append(tc)
                messages.append({"role": "assistant", "content": assistant_content})

                # Execute winning tool calls
                tool_results = []
                for tc in effective_calls:
                    tool_result = await _execute_tool(tc["name"], tc["input"], tools or {})
                    all_tools_used.append(tc["name"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": tool_result,
                    })
                messages.append({"role": "user", "content": tool_results})

            # Update progress
            if _progress is not None:
                _progress.num_turns = turn + 1
                _progress.tools_used = list(all_tools_used)
                _progress.text = final_text
                _progress.premium_requests = total_premium

    except Exception as e:
        log.exception("Oracle run failed: %s", e)
        result.error = str(e)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    result.messages = messages
    result.text = final_text
    result.num_turns = worker_turns + checkpoints_used
    result.tools_used = all_tools_used
    result.input_tokens = total_input_tokens
    result.output_tokens = total_output_tokens
    result.premium_requests = total_premium
    result.duration_ms = elapsed_ms
    result.quality_score = _score_quality(result)

    log.info(
        "Oracle complete: %d worker turns, %d checkpoints, %.1f premium, %d tools, %dms",
        worker_turns, checkpoints_used, total_premium,
        len(all_tools_used), elapsed_ms,
    )

    return result
