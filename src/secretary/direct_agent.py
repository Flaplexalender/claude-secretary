"""Direct agent — anthropic SDK with conversation priming for proxy calls.

Replaces the Claude Agent SDK wrapper with direct Anthropic Messages API
calls through the copilot-api proxy. Optional few-shot message priming
improves tool-use density by demonstrating parallel tool calls.

When ``config.reasoning_effort`` is set, requests go through the proxy's
OpenAI ``/v1/chat/completions`` endpoint (full JSON pass-through), which
supports ``reasoning_effort`` for extended thinking.  Otherwise the
Anthropic ``/v1/messages`` endpoint is used via the SDK.

Tool execution happens in-process (no MCP server, no subprocess).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx

from .config import SecretaryConfig, _interpolate_env
from .context_builder import build_identity_prompt, build_skill_context
from .router import select_model, RoutingDecision
from .memory import MemoryStore
from .strategy_library import StrategyLibrary
from .goal_harness import should_block_goal

log = logging.getLogger(__name__)

# Few-shot message prefix for tool-use density demonstration.
# The full prefix data is in _local_prefix.py (gitignored).
# When absent (e.g. fresh clone), falls back to empty — the agent
# still works but without few-shot priming.
try:
    from ._local_prefix import AGENT_PREFIX, AGENT_PREFIX_OAI
except ImportError:
    AGENT_PREFIX: list[dict[str, Any]] = []  # type: ignore[no-redef]
    AGENT_PREFIX_OAI: list[dict[str, Any]] = []  # type: ignore[no-redef]

# Token budgets per tier.  Opus uses streaming to avoid SDK 10-min timeout,
# which lets us push max_tokens higher → more work produced per turn.
_TIER_MAX_TOKENS: dict[str, int] = {
    "deep": 32768,    # Deep work — same as Opus, streaming
    "high": 32768,    # Opus — streaming (unlocks high budget)
    "medium": 16384,  # Sonnet — sync OK at 16K
    "low": 12288,     # Haiku — doesn't need more
    "free": 8192,     # GPT-4.1 — small budget, trivial tasks only
}

# Windows encoding fix: force UTF-8 to prevent cp1252 UnicodeEncodeError
import sys
import io
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        else:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass  # Fallback: continue with default encoding
    # Set PYTHONIOENCODING to prevent future encoding breakdowns
    import os
    os.environ['PYTHONIOENCODING'] = 'utf-8'

# Parallel tool-call throttle.  The system prompt encourages 6+ parallel tool
# calls per turn.  This cap bounds how many actually run simultaneously —
# high enough to keep the prompt honest, low enough to avoid thrashing
# subprocesses (run_python, run_command) and hitting proxy 504s.
_MAX_PARALLEL_TOOLS = 6
_STREAMING_TIERS = {"high", "deep"}  # tiers that use streaming API


# ── Selective tool exposure ─────────────────────────────────────
# Reduces input tokens by ~800-2000/turn by omitting irrelevant tool schemas.
_TOOL_CATEGORIES: dict[str, set[str]] = {
    "email": {"gmail_search", "gmail_read", "gmail_draft", "gmail_send", "gmail_list_drafts", "gmail_get_draft"},
    "calendar": {"calendar_today", "calendar_list", "calendar_search", "calendar_create"},
    "file": {"file_read", "file_write", "file_list", "file_edit", "grep_search", "run_command", "run_python"},
    "implement": {"file_read", "file_write", "file_list", "file_edit", "grep_search", "run_command", "run_python"},
    "research": {"file_read", "file_write", "file_list", "file_edit", "grep_search", "run_command", "run_python"},
}
_CATEGORY_KEYWORDS: dict[str, re.Pattern] = {
    "email": re.compile(
        r"\b(email|gmail|inbox|draft|send|message|unread|reply|forward|newsletter|mail)\b", re.I,
    ),
    "calendar": re.compile(
        r"\b(calendar|event|meeting|schedule|appointment|today.?s?\s*events)\b", re.I,
    ),
    "file": re.compile(
        r"\b(file|read|write|code|src|test|config|\.py|\.ts|\.js|\.md|"
        r"bug|implement|refactor|log|data|scratchpad)\b", re.I,
    ),
    "implement": re.compile(
        r"\b(implement|add|create|build|write|edit|change|fix|patch|modify|refactor|port|migrate)\b", re.I,
    ),
    "research": re.compile(
        r"\b(research|analyze|audit|review|investigate|compare|benchmark|study)\b", re.I,
    ),
}


_ALWAYS_INCLUDE_TOOLS = frozenset({"file_edit", "grep_search", "run_command", "run_python"})


def _select_tool_schemas(
    full_schemas: list[dict[str, Any]],
    task: str,
) -> list[dict[str, Any]]:
    """Filter tool schemas to only include categories relevant to the task.

    Saves ~800-2000 input tokens per turn by omitting irrelevant tool schemas.
    Falls back to all schemas if no category matches or all match.
    """
    needed: set[str] = set()
    for cat, pattern in _CATEGORY_KEYWORDS.items():
        if pattern.search(task):
            needed.add(cat)
    if not needed or len(needed) == len(_TOOL_CATEGORIES):
        return full_schemas
    allowed: set[str] = set()
    for cat in needed:
        allowed.update(_TOOL_CATEGORIES.get(cat, set()))
    # Always include core dev tools — they're useful for ALL task types
    # (e.g. grep_search to find email templates, run_command for diagnostics).
    allowed.update(_ALWAYS_INCLUDE_TOOLS)
    filtered = [s for s in full_schemas if s["name"] in allowed]
    if not filtered:
        return full_schemas
    log.debug(
        "Selective tools: %d/%d schemas (%s)",
        len(filtered), len(full_schemas), ", ".join(sorted(needed)),
    )
    return filtered


# ── Aggressive context injection ────────────────────────────
# Pre-read project files matching task keywords and inject into the prompt.
# Saves 1-3 entire turns of file_read tool calls.

# Map task keywords to files worth pre-reading
_CONTEXT_FILE_PATTERNS: dict[re.Pattern, list[str]] = {
    re.compile(r"\b(direct_agent|agent|run|turn|tool)\b", re.I): [
        "src/secretary/direct_agent.py",
    ],
    re.compile(r"\b(watcher|watch|campaign|cycle|daemon)\b", re.I): [
        "src/secretary/watcher.py",
    ],
    re.compile(r"\b(config|setting|optimization|tier|routing)\b", re.I): [
        "src/secretary/config.py",
        "config.yaml",
    ],
    re.compile(r"\b(router|model|tier|complexity)\b", re.I): [
        "src/secretary/router.py",
    ],
    re.compile(r"\b(metric|benchmark|compare|A/B)\b", re.I): [
        "src/secretary/metrics.py",
    ],
    re.compile(r"\b(tool|gmail|calendar|file_read|file_write|file_edit|grep_search|run_command)\b", re.I): [
        "src/secretary/direct_tools.py",
    ],
    re.compile(r"\b(memory|remember|long.?term|short.?term)\b", re.I): [
        "src/secretary/memory.py",
    ],
    re.compile(r"\b(tests?|pytest|assert|coverage)\b", re.I): [
        "tests/",
    ],
    re.compile(r"\b(scratchpad|findings|notes|previous)\b", re.I): [
        "data/scratchpad.md",
    ],
}

_MAX_PREFETCH_BYTES = 15000  # Cap to ~15KB — fits 4-5 files for aggressive context injection


def _build_aggressive_context(task: str, data_root: Path) -> tuple[str, set[str]]:
    """Pre-read project files matching task keywords.

    Returns a (context_block, preloaded_paths) tuple. The context block is
    appended to the prompt; the paths set is used to detect redundant
    file_read calls.
    """
    project_root = data_root.parent  # data/ → project root
    matched_files: list[str] = []
    for pattern, files in _CONTEXT_FILE_PATTERNS.items():
        if pattern.search(task):
            matched_files.extend(files)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for f in matched_files:
        if f not in seen:
            seen.add(f)
            unique.append(f)

    if not unique:
        return "", set()

    parts: list[str] = []
    loaded_paths: set[str] = set()
    total_bytes = 0
    for rel_path in unique:
        full_path = project_root / rel_path
        if full_path.is_dir():
            # List directory contents instead of reading
            try:
                entries = sorted(full_path.iterdir())
                listing = "\n".join(
                    f"  {'📁 ' if e.is_dir() else ''}{e.name}" for e in entries[:30]
                )
                parts.append(f"--- {rel_path} (directory listing) ---\n{listing}")
                total_bytes += len(listing)
                loaded_paths.add(rel_path.rstrip("/"))
            except OSError:
                continue
        elif full_path.is_file():
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
                if total_bytes + len(content) > _MAX_PREFETCH_BYTES:
                    # Truncate to fit budget
                    remaining = _MAX_PREFETCH_BYTES - total_bytes
                    if remaining > 500:
                        content = content[:remaining] + "\n... [truncated]"
                    else:
                        continue
                parts.append(f"--- {rel_path} ({len(content):,} chars) ---\n{content}")
                total_bytes += len(content)
                loaded_paths.add(rel_path)
            except OSError:
                continue
        if total_bytes >= _MAX_PREFETCH_BYTES:
            break

    if not parts:
        return "", set()

    log.debug("Aggressive context: injected %d files (%d bytes)", len(parts), total_bytes)
    return "\n\n".join(parts), loaded_paths


# ── Quality scoring (heuristic, zero-cost) ──────────────────

def _score_quality(result: 'RunResult') -> float:
    """Score task result quality 0.0-1.0 based on heuristics.

    Higher scores indicate more productive runs. Used for A/B comparison.
    Dimensions: tool diversity, output substance, error-free execution, efficiency.
    Weights efficiency heavily — fewer turns = dramatically higher score.
    """
    score = 0.0
    # 1. Success (no error) = 0.25
    if not result.error:
        score += 0.25
    # 2. Tool usage diversity (unique tools / 5, max 0.15)
    unique_tools = len(set(result.tools_used))
    score += min(unique_tools / 5.0, 1.0) * 0.15
    # 3. Output substance (non-trivial text, max 0.15)
    text_len = len(result.text.strip())
    if text_len > 200:
        score += 0.15
    elif text_len > 50:
        score += 0.08
    # 4. Efficiency (fewer turns = better, max 0.25) — HEAVILY weighted
    if result.num_turns > 0:
        # 1 turn = perfect, 2-3 = excellent, 4-5 = good, 6+ = diminishing
        turn_score = max(0, 1.0 - (result.num_turns - 1) / 6.0)
        score += turn_score * 0.25
    # 5. Tool batching (tools per turn, max 0.20) — rewards parallel tool use
    if result.num_turns > 0:
        tools_per_turn = len(result.tools_used) / result.num_turns
        batch_score = min(tools_per_turn / 3.0, 1.0)
        score += batch_score * 0.20
    return round(min(score, 1.0), 3)


# ── Conversation summarization (extractive, zero API cost) ───────

def _build_extractive_summary(messages: list[dict[str, Any]], anchor: int) -> str:
    """Extract key facts from old conversation turns.

    Compresses history into compact bullets so subsequent API calls ship
    less context.  No API call needed — purely heuristic extraction.
    """
    items: list[str] = []
    for msg in messages[anchor:]:
        role = msg.get("role", "")
        content = msg.get("content")
        if role == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "").strip()
                    if text:
                        items.append(f"Said: {text.split(chr(10))[0][:150]}")
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    args = block.get("input", {})
                    arg_str = ", ".join(
                        f"{k}={str(v)[:30]}" for k, v in list(args.items())[:3]
                    )
                    items.append(f"Called {name}({arg_str})")
        elif role == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    rt = block.get("content", "")
                    if isinstance(rt, str) and rt.strip():
                        items.append(f"\u2192 {rt.strip().split(chr(10))[0][:100]}")
    return "\n".join(items[-20:])


def _sanitize_tool_pairs(messages: list[dict[str, Any]], anchor: int) -> list[dict[str, Any]]:
    """Ensure every tool_use ID has a matching tool_result and vice versa.

    Context pruning and conversation summarization can split tool_use/tool_result
    pairs, leaving orphaned IDs that cause the API to return 400.  This function
    removes orphaned blocks so the message array is always valid.

    The *anchor* (prefix + task messages) is never modified.
    """
    # Collect all tool_use IDs and tool_result IDs from non-anchor messages.
    use_ids: set[str] = set()
    result_ids: set[str] = set()
    for msg in messages[anchor:]:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                use_ids.add(block["id"])
            elif block.get("type") == "tool_result":
                result_ids.add(block["tool_use_id"])

    orphan_uses = use_ids - result_ids
    orphan_results = result_ids - use_ids
    if not orphan_uses and not orphan_results:
        return messages  # nothing to fix

    log.warning(
        "Sanitizing tool pairs: %d orphan tool_use, %d orphan tool_result",
        len(orphan_uses), len(orphan_results),
    )

    # Strip orphaned blocks from non-anchor messages.
    cleaned: list[dict[str, Any]] = list(messages[:anchor])
    for msg in messages[anchor:]:
        content = msg.get("content")
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        new_blocks = []
        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue
            if block.get("type") == "tool_use" and block["id"] in orphan_uses:
                continue  # drop orphaned tool_use
            if block.get("type") == "tool_result" and block["tool_use_id"] in orphan_results:
                continue  # drop orphaned tool_result
            new_blocks.append(block)
        if new_blocks:
            cleaned.append({**msg, "content": new_blocks})
        elif msg.get("role") == "assistant":
            # Assistant message lost all blocks — replace with placeholder
            # to keep alternation valid.
            cleaned.append({"role": "assistant", "content": [{"type": "text", "text": "..."}]})
        # User messages with no remaining blocks are dropped entirely
        # (this is safe — they were only tool_result carriers).
    return cleaned


def _dynamic_max_tokens(tier: str, turn: int) -> int:
    """Scale down max_tokens on later turns to encourage conciseness.

    Opening turns get full budget for large reads/writes.  Later turns
    typically need less output (summaries, small fixes).

    Deep tier keeps full budget throughout — long-horizon work needs it.
    """
    base = _TIER_MAX_TOKENS.get(tier, 16384)
    if tier == "deep":
        return base  # no throttling for deep work
    if turn <= 2:
        return base
    if turn <= 5:
        return min(base, 16384)
    if turn <= 7:
        return min(base, 8192)
    return min(base, 4096)  # Turn 8+: summaries only, force conciseness


@dataclass
class RunProgress:
    """Mutable progress tracker — readable even after timeout cancellation.

    Passed into run() so the caller (watcher) can read partial turn/tool data
    when asyncio.wait_for() fires a TimeoutError and cancels the coroutine.
    """
    num_turns: int = 0
    tools_used: list[str] = field(default_factory=list)
    model: str = "unknown"
    tier: str = ""
    cost_usd: float = 0.0
    text: str = ""
    premium_requests: float = 0.0


@dataclass
class RunResult:
    """Result of a direct agent run."""
    task: str
    routing: RoutingDecision
    messages: list[dict[str, Any]] = field(default_factory=list)
    text: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    session_id: str = ""
    tools_used: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    quality_score: float = 0.0  # heuristic quality 0.0-1.0
    premium_requests: float = 0.0  # total premium request units consumed
    redundant_reads: int = 0  # file_read calls on already-preloaded content
    planner_model: str = ""  # model used for planning (empty = no plan)
    planner_steps: int = 0  # number of planned steps (0 = no plan)


# Task-specific prompt hints — matched via _CATEGORY_KEYWORDS patterns.
_TASK_HINTS: dict[str, str] = {
    "email": "Focus: Gmail. Search→Read→Draft in one flow. Batch multiple searches.",
    "calendar": "Focus: Calendar. Check today + search in parallel.",
    "file": "Focus: Files. Use grep_search to find patterns, file_edit for changes, run_command for tests. Read ALL files in parallel.",
    "implement": "Focus: Implementation. Batch ALL file_edits in ONE response — edit A + B + C + run_command(pytest) + grep_search(verify) = 6 tools. NEVER do 1 edit per turn.",
    "research": "Focus: Analysis. Use grep_search + file_read in parallel for exploration. Use run_command for data analysis. Write findings to data/scratchpad.md.",
}


def _build_system_prompt(
    memory: MemoryStore,
    task: str = "",
    max_turns: int = 30,
    tier: str = "",
    strategy_library: StrategyLibrary | None = None,
    workspace_dir: str = "workspace",
) -> str:
    """Build system prompt with memory context and task-specific rules.

    Compact 4-rule format: ~200 tokens vs ~600 tokens previously.
    Research shows LLMs follow short numbered rules more reliably than long
    bullet lists.  Each rule is unique (no redundancy).

    When max_turns is low (1-3), switches to ultra-compact 1-turn mode
    that maximizes work per single API call.

    Deep mode (tier="deep") gets a different prompt optimized for long-horizon
    exploratory work — think like a senior engineer with hours to spare.
    """
    if tier == "deep":
        parts = [
            f"AI agent. {max_turns} turns available — use as many as needed.",
            "DEEP WORK MODE: You have hours. Explore thoroughly, form hypotheses, gather evidence, iterate.",
            "RULES: (1) 6+ tool calls per response — all parallel. (2) No text until you have real findings. "
            "(3) Use run_python for bulk operations (read/edit/search many files in one script). "
            "(4) grep_search to find, file_edit to change, run_command to test. "
            "(5) Write intermediate findings to data/scratchpad.md. "
            "(6) Think big — investigate root causes, not just symptoms.",
        ]
    elif max_turns <= 3:
        parts = [
            f"AI agent. {max_turns} turns. RULES: (1) 6+ tools/response, parallel. (2) No text until last turn. (3) Pre-loaded files below — do NOT re-read them with file_read.",
        ]
    else:
        parts = [
            f"AI agent. {max_turns} turns total.",
            "RULES: (1) 6+ tool calls per response — all parallel. (2) No text until final turn. (3) grep_search to find, file_edit to change, run_command to test. (4) If data is pre-loaded below, do NOT re-read it with file_read or gmail_search — it's already there. (5) BATCH run_python: do all analysis in ONE script (load data, compute, print) — not 10+ sequential calls.",
            # STOP-EXPLORING DOCTRINE (borrowed from VS Code Copilot's own system prompt, verified 2026-04-21)
            # Sonnet+reasoning=high over-explores: 2% success across 49 attempts in sprints v1-v3.
            # These rules shift the bias from "keep searching" to "act with what you have".
            "STOP RULES: (a) Avoid redundant searches for information already found. (b) If multiple queries return overlapping results, you have sufficient context — stop searching and act. (c) Once you've identified the relevant files and understand the structure, PROCEED to implementation; do not continue searching. (d) Gather sufficient context to act confidently, then proceed. Do not over-explore when you already have enough. (e) If an approach is blocked, try a DIFFERENT approach — do not brute-force the same approach repeatedly. (f) For analysis tasks: 3-5 tool calls to read the relevant files, then write your answer. Stop.",
        ]

    # Task-specific guidance (~20-40 tokens instead of generic ~400)
    _matched_cat = ""
    if task:
        for cat, pattern in _CATEGORY_KEYWORDS.items():
            if pattern.search(task):
                hint = _TASK_HINTS.get(cat)
                if hint:
                    parts.append(f"\n{hint}")
                _matched_cat = cat
                break

    # Inject learned strategies for this task category (Layer 13)
    if strategy_library and _matched_cat:
        strat_section = strategy_library.format_for_prompt(_matched_cat)
        if strat_section:
            parts.append(f"\n{strat_section}")

    # Inject workspace identity + skill context (OpenClaw orientation)
    identity = build_identity_prompt(workspace_dir)
    if identity:
        parts.append(f"\n{identity}")

    skill_ctx = build_skill_context(workspace_dir, task)
    if skill_ctx:
        parts.append(f"\n{skill_ctx}")

    long_mem = memory.get_long()
    if long_mem:
        parts.append("\n## Memory")
        for i, entry in enumerate(long_mem[-10:]):
            parts.append(f"- {entry}")
            offset = max(0, len(long_mem) - 10)
            memory.access_long(offset + i)

    short_mem = memory.get_short()
    if short_mem:
        parts.append("\n## Recent")
        for entry in short_mem[-5:]:
            parts.append(f"- {entry}")

    return "\n".join(parts)


def _build_client(config: SecretaryConfig) -> anthropic.Anthropic:
    """Create an Anthropic client pointing at the copilot-api proxy."""
    from .config import _interpolate_env
    base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")
    return anthropic.Anthropic(
        base_url=base_url,
        api_key="copilot-proxy",
    )


# Ordered from weakest to strongest — clamps work by picking
# the strongest supported level that is <= the requested level.
_REASONING_LEVELS: tuple[str, ...] = ("low", "medium", "high")

# Per-model maximum supported ``reasoning_effort`` via copilot-api.
# ``None`` means the model rejects reasoning_effort entirely (HTTP 400).
# Empirically verified against copilot-api 2026-04-20:
#   - Haiku 4.5: no reasoning support at all
#   - Opus 4.7: only "medium" accepted (high/low rejected)
#   - Sonnet 4/4.5/4.6 + Opus 4.5/4.6: full range
_MODEL_REASONING_CAP: dict[str, str | None] = {
    "claude-haiku-4.5": None,
    "claude-opus-4.7": "medium",
}


def _clamp_reasoning_effort(model: str, requested: str | None) -> str | None:
    """Return the strongest reasoning_effort ``model`` accepts that is
    <= ``requested``, or ``None`` if the model doesn't support any.

    Unknown Claude models default to the full range (backwards-compatible).
    Non-Claude models (GPT-4.1 etc.) are handled separately by the caller
    and should not invoke this helper.
    """
    if not requested:
        return None
    cap = _MODEL_REASONING_CAP.get(model, "high")
    if cap is None:
        return None
    # Clamp: take min(requested, cap) by index in _REASONING_LEVELS.
    try:
        req_idx = _REASONING_LEVELS.index(requested)
    except ValueError:
        return cap  # unknown level — fall back to model cap
    try:
        cap_idx = _REASONING_LEVELS.index(cap)
    except ValueError:
        return requested
    return _REASONING_LEVELS[min(req_idx, cap_idx)]


def _stream_call(client: anthropic.Anthropic, **kwargs) -> anthropic.types.Message:
    """Execute a streaming API call and return the final Message.

    Streaming keeps the connection alive so the SDK doesn't hit its 10-minute
    timeout on large max_tokens budgets (32K+).  The returned Message object
    is identical to what ``client.messages.create()`` returns.
    """
    with client.messages.stream(**kwargs) as stream:
        return stream.get_final_message()


def _build_tool_schemas(tools: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert our tool registry to Anthropic API tool schemas.

    Each tool in the registry is: {name: str, description: str, input_schema: dict, func: callable}
    Output: list of dicts with {name, description, input_schema} for the API.
    """
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "input_schema": tool["input_schema"],
        }
        for tool in tools.values()
    ]


async def _execute_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    tools: dict[str, Any],
) -> str:
    """Execute a tool and return the result as a string."""
    tool = tools.get(tool_name)
    if not tool:
        return f"Error: Unknown tool '{tool_name}'"
    try:
        result = await tool["func"](tool_input)
        # Tool functions return {"content": [{"type": "text", "text": ...}]}
        if isinstance(result, dict) and "content" in result:
            texts = []
            for block in result["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block["text"])
            return "\n".join(texts) if texts else str(result)
        return str(result)
    except Exception as e:
        log.error("Tool %s failed: %s", tool_name, e)
        return f"Error executing {tool_name}: {e}"


# ── OpenAI endpoint helpers ───────────────────────────────────
# When reasoning_effort is configured, we bypass the Anthropic SDK and call
# the copilot-api proxy's /v1/chat/completions endpoint directly.  That
# endpoint passes the payload through to the GitHub API without stripping
# fields, so reasoning_effort (extended thinking) works.


@dataclass
class _OAIBlock:
    """Duck-compatible with anthropic SDK content blocks."""
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)


@dataclass
class _OAIUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class _OAIResponse:
    """Duck-compatible with anthropic.types.Message for the main loop."""
    content: list[_OAIBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: _OAIUsage = field(default_factory=_OAIUsage)


def _to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-format message history to OpenAI chat format."""
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        role = msg["role"]
        content = msg["content"]

        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # Single-pass partition: avoids iterating content twice.
                tool_results: list[dict] = []
                text_blocks: list[dict] = []
                for b in content:
                    if isinstance(b, dict):
                        btype = b.get("type")
                        if btype == "tool_result":
                            tool_results.append(b)
                        elif btype == "text":
                            text_blocks.append(b)
                if tool_results:
                    for tr in tool_results:
                        c = tr.get("content", "")
                        out.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": c if isinstance(c, str) else str(c),
                        })
                # Emit text blocks as user message (supports mixed tool_result + text)
                if text_blocks:
                    text = " ".join(b.get("text", "") for b in text_blocks)
                    if text.strip():
                        out.append({"role": "user", "content": text})
                elif not tool_results:
                    out.append({"role": "user", "content": ""})

        elif role == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
            elif isinstance(content, list):
                text_parts: list[str] = []
                tool_calls: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block["id"],
                            "type": "function",
                            "function": {
                                "name": block["name"],
                                "arguments": json.dumps(block["input"]),
                            },
                        })
                msg_dict: dict[str, Any] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    msg_dict["tool_calls"] = tool_calls
                out.append(msg_dict)
    return out


def _to_openai_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic tool schemas to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["input_schema"],
            },
        }
        for s in schemas
    ]


def _openai_stream_call(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Streaming POST to the proxy's OpenAI chat/completions endpoint.

    Uses SSE streaming to avoid the proxy bug where non-streaming responses
    strip tool_calls when reasoning_effort is set.  Reassembles the final
    message from delta chunks — the returned dict matches the non-streaming
    response shape so _parse_openai_response() works unchanged.
    """
    url = f"{base_url}/v1/chat/completions"
    payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}

    content_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}  # index → {id, function: {name, arguments}}
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    model: str = ""

    with httpx.stream("POST", url, json=payload, timeout=600.0) as resp:
        if resp.status_code >= 400:
            resp.read()  # must read body before raise_for_status on streaming resp
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                chunk = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if not model and chunk.get("model"):
                model = chunk["model"]
            if chunk.get("usage"):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta", {})
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id": tc.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                if tc.get("id"):
                    tool_calls[idx]["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    tool_calls[idx]["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    tool_calls[idx]["function"]["arguments"] += fn["arguments"]

    # Reassemble into the same shape as a non-streaming response.
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    return {
        "choices": [{"message": message, "finish_reason": finish_reason or "stop"}],
        "usage": usage,
        "model": model,
    }


def _parse_openai_response(raw: dict[str, Any]) -> _OAIResponse:
    """Parse an OpenAI chat completion into our duck-typed response."""
    choice = raw["choices"][0]
    msg = choice["message"]
    blocks: list[_OAIBlock] = []

    if msg.get("content"):
        blocks.append(_OAIBlock(type="text", text=msg["content"]))

    for tc in msg.get("tool_calls", []):
        args_str = tc["function"]["arguments"]
        try:
            parsed = json.loads(args_str)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
        blocks.append(_OAIBlock(
            type="tool_use",
            id=tc["id"],
            name=tc["function"]["name"],
            input=parsed,
        ))

    _fr = choice.get("finish_reason")
    if _fr == "stop":
        stop = "end_turn"
    elif _fr == "length":
        stop = "max_tokens"
    else:
        stop = "tool_use"
    usage = _OAIUsage(
        input_tokens=raw.get("usage", {}).get("prompt_tokens", 0),
        output_tokens=raw.get("usage", {}).get("completion_tokens", 0),
    )
    return _OAIResponse(content=blocks, stop_reason=stop, usage=usage)


async def run(
    task: str,
    config: SecretaryConfig | None = None,
    memory: MemoryStore | None = None,
    force_tier: str | None = None,
    tools: dict[str, Any] | None = None,
    max_turns: int | None = None,
    cwd: str | None = None,
    hooks: dict | None = None,
    _progress: RunProgress | None = None,
    strategy_library: StrategyLibrary | None = None,
    max_tool_calls: int = 50,
) -> RunResult:
    """Run a task via direct Anthropic Messages API.

    Calls go through the copilot-api proxy with optional few-shot priming.

    Args:
        task: The prompt / task description.
        config: Secretary config (loads default if None).
        memory: Memory store to inject context (loads default if None).
        force_tier: Override model routing ("low", "medium", "high").
        tools: Tool registry dict {name: {name, description, input_schema, func}}.
        max_turns: Override the tier's default max_turns for this run.
        cwd: Working directory (unused in direct mode, kept for API compat).
        _progress: Mutable progress tracker for timeout visibility (watcher reads on cancel).
        hooks: Unused in direct mode, kept for API compat.
        max_tool_calls: Maximum total tool invocations before aborting (default 50).
    """
    if config is None:
        config = SecretaryConfig.load()
    if memory is None:
        memory = MemoryStore.load(config.memory_path)

    routing = select_model(config, task, force_tier)
    effective_max_turns = max_turns if max_turns is not None else routing.max_turns

    # One-shot mode: for originally-simple tasks now routed to Opus,
    # cap turns to 3 since Opus can do in 3 what Haiku does in 10.
    if (config.optimizations.one_shot_simple
            and config.agent_prefix
            and config.optimizations.always_opus
            and max_turns is None
            and "always_opus" in routing.reason):
        from .router import estimate_complexity
        orig_tier, _, _, _ = estimate_complexity(task)
        if orig_tier == "low":
            effective_max_turns = min(effective_max_turns, 3)
            log.debug("One-shot mode: capping to %d turns (was low-tier task)", effective_max_turns)
        elif orig_tier == "medium":
            effective_max_turns = min(effective_max_turns, 8)
            log.debug("One-shot mode: capping to %d turns (was medium-tier task)", effective_max_turns)

    # Turn limits: when prefix is OFF, enforce tighter per-tier turn caps.
    if not config.agent_prefix and max_turns is None:
        paid_limit = config.optimizations.paid_turn_limits.get(routing.tier)
        if paid_limit and paid_limit < effective_max_turns:
            effective_max_turns = paid_limit
            log.debug("Paid-mode turn limit: %d (tier %s)", effective_max_turns, routing.tier)

    # Per-task premium budget (in multiplier units). E.g. Opus (3x) with budget 18 = 6 turns max.
    _premium_budget = config.optimizations.task_premium_budget.get(routing.tier, 0.0)
    _premium_multiplier = routing.premium_multiplier
    if _premium_budget > 0 and _premium_multiplier > 0 and not config.agent_prefix:
        budget_turns = int(_premium_budget / _premium_multiplier)
        if budget_turns > 0 and budget_turns < effective_max_turns:
            effective_max_turns = budget_turns
            log.debug("Premium budget cap: %d turns (%.1f budget / %.2fx multiplier)",
                      effective_max_turns, _premium_budget, _premium_multiplier)

    # Aggressive context injection: pre-read relevant files into prompt
    _preloaded_paths: set[str] = set()
    if config.optimizations.aggressive_context:
        extra_context, _preloaded_paths = _build_aggressive_context(task, config.data_path)
        if extra_context:
            task = f"{task}\n\n---\nPre-loaded project files (ALREADY READ — do NOT call file_read on these again):\n{extra_context}"

    # Haiku-as-planner: cheap model plans, expensive model executes.
    # For high/deep tasks, call Haiku first to decompose the task into steps.
    # The plan is injected into the prompt so Opus doesn't waste turns exploring.
    _plan = None
    if config.optimizations.haiku_planner:
        from .planner import plan_task
        _plan = await plan_task(task, config, tier=routing.tier)
        if _plan and not _plan.is_simple:
            plan_block = _plan.format_for_prompt()
            task = f"{task}\n\n{plan_block}"
            log.info("Haiku planner: %d steps injected (complexity=%s)",
                     len(_plan.steps), _plan.estimated_complexity)

    system_prompt = _build_system_prompt(memory, task, max_turns=effective_max_turns, tier=routing.tier, strategy_library=strategy_library, workspace_dir=getattr(config, 'workspace_dir', 'workspace'))

    # Build tool schemas once (used by both API paths)
    tool_schemas = _build_tool_schemas(tools) if tools else []

    # Optimization: selective tool exposure — omit irrelevant tool schemas
    if config.optimizations.selective_tools and tool_schemas:
        tool_schemas = _select_tool_schemas(tool_schemas, task)

    # Determine API path: OpenAI endpoint (reasoning or non-Claude models) or Anthropic SDK.
    # GPT-4.1, GPT-4o etc. MUST use the OpenAI endpoint; Claude models use Anthropic SDK
    # unless reasoning_effort is set (which requires the OpenAI endpoint).
    #
    # Per-model reasoning_effort support (empirically discovered 2026-04-20):
    #   - Haiku 4.5: no reasoning_effort at all — proxy returns HTTP 400.
    #   - Sonnet 4/4.5/4.6: full range (low/medium/high).
    #   - Opus 4.7: only "medium" accepted — high/low rejected.
    # The ``_clamp_reasoning_effort`` helper returns the strongest effort the
    # model accepts, or ``None`` if extended thinking is unsupported.
    _is_non_claude = not routing.model.startswith("claude-")
    _effective_effort = _clamp_reasoning_effort(routing.model, config.reasoning_effort)
    _supports_reasoning = _effective_effort is not None
    _reasoning_active = bool(config.reasoning_effort) and _supports_reasoning
    if config.reasoning_effort and _effective_effort != config.reasoning_effort:
        if _effective_effort is None:
            log.info(
                "Ignoring reasoning_effort=%s for %s (model does not support extended thinking)",
                config.reasoning_effort, routing.model,
            )
        else:
            log.info(
                "Clamping reasoning_effort %s -> %s for %s (model cap)",
                config.reasoning_effort, _effective_effort, routing.model,
            )
    _use_openai = _reasoning_active or _is_non_claude
    if _use_openai:
        _base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")
        _oai_tools = _to_openai_tools(tool_schemas) if tool_schemas else []
        if _is_non_claude:
            log.info("Using OpenAI endpoint for non-Claude model: %s", routing.model)
        else:
            log.info("Using OpenAI endpoint with reasoning_effort=%s", _effective_effort)
    else:
        client = _build_client(config)

    # Deep tier always uses the message prefix for extended sessions.
    _use_prefix = config.agent_prefix or routing.tier == "deep"

    # Build messages — optional few-shot prefix for tool-use priming
    messages: list[dict[str, Any]] = []
    _prefix = AGENT_PREFIX_OAI if (_use_prefix and _use_openai) else AGENT_PREFIX
    if _use_prefix:
        messages.extend(_prefix)
    messages.append({"role": "user", "content": task})

    # Max message pairs to keep (prefix+task frames kept always).
    # Deep mode keeps more context (100 pairs = 200 messages) for long sessions.
    _CONTEXT_KEEP_PAIRS = 100 if routing.tier == "deep" else 50
    _anchor = (len(_prefix) + 1 if _use_prefix else 1)  # prefix messages + task(1)
    # Beyond this turn age, compress tool result payloads in the message history.
    # Recent tool results are kept verbatim; old ones get truncated to save
    # input tokens so each API call ships less fat and the model has more
    # headroom for real work.
    _TOOL_RESULT_FRESH_TURNS = 1  # keep full results for last 1 turn only (was 3; saves ~15K tokens/session)
    _TOOL_RESULT_MAX_CHARS = 300  # default cap for old tool results (type-aware truncation below)

    result = RunResult(task=task, routing=routing)
    if _plan:
        result.planner_model = _plan.planner_model
        result.planner_steps = len(_plan.steps)
    if _progress is not None:
        _progress.model = routing.model
        _progress.tier = routing.tier
    t0 = time.monotonic()
    _consecutive_tool_errors = 0  # break if tools fail repeatedly
    _MAX_CONSECUTIVE_ERRORS = 3  # stop wasting turns on broken tool calls
    _consecutive_empty = 0  # break if model keeps returning empty responses
    _MAX_CONSECUTIVE_EMPTY = 2  # 2 retries = 3 empty responses total before giving up
    _total_tool_calls = 0  # cumulative tool invocations across all turns
    _redundant_reads = 0  # count of file_read calls on already-preloaded content

    try:
        for turn in range(effective_max_turns):
            # Prune context if it grows too large (keep anchor + last N pairs)
            _max_messages = _anchor + _CONTEXT_KEEP_PAIRS * 2
            if len(messages) > _max_messages:
                messages = messages[:_anchor] + messages[-(_CONTEXT_KEEP_PAIRS * 2):]
                messages = _sanitize_tool_pairs(messages, _anchor)
                log.debug("Pruned context to %d messages (turn %d)", len(messages), turn)

            # Compress old tool results — saves input tokens per API call.
            # Keep recent results verbatim (model may need them), truncate older ones.
            _fresh_boundary = max(0, len(messages) - _TOOL_RESULT_FRESH_TURNS * 2)
            for _mi in range(_anchor, _fresh_boundary):
                msg = messages[_mi]
                if msg.get("role") != "user":
                    continue
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    text = block.get("content", "")
                    if isinstance(text, str) and len(text) > 60:
                        # Type-aware truncation: different tools need different caps
                        if text.startswith("Edited ") or text.startswith("Written:"):
                            # file_edit / file_write: just status line
                            block["content"] = text[:60]
                        elif text.startswith("---") and "bytes" in text[:80]:
                            # file_read: keep header line only (path + size)
                            header_end = text.find("\n")
                            block["content"] = text[:header_end] if header_end > 0 else text[:100]
                        elif text.startswith("Found ") and "matches" in text[:50]:
                            # grep_search: keep header + first 2-3 matches
                            block["content"] = text[:250] + ("... [truncated]" if len(text) > 250 else "")
                        elif len(text) > _TOOL_RESULT_MAX_CHARS:
                            block["content"] = text[:_TOOL_RESULT_MAX_CHARS] + "... [truncated]"

            # Conversation summarization — compress old turns periodically
            if (config.optimizations.conversation_summary
                    and turn > 0
                    and turn % config.optimizations.summary_after_turn == 0
                    and len(messages) > _anchor + 6):
                summary = _build_extractive_summary(messages, _anchor)
                if summary:
                    keep_n = min(6, len(messages) - _anchor)
                    recent = messages[-keep_n:]
                    messages = messages[:_anchor]
                    messages.append({"role": "user", "content": f"[History summary]\n{summary}"})
                    messages.append({"role": "assistant", "content": [{"type": "text", "text": "Understood, continuing."}]})
                    messages.extend(recent)
                    messages = _sanitize_tool_pairs(messages, _anchor)
                    log.debug("Summarized conversation at turn %d", turn)

            # Per-tier token budget — Opus gets more via streaming.
            if config.optimizations.dynamic_max_tokens:
                _max_tokens = _dynamic_max_tokens(routing.tier, turn)
            else:
                _max_tokens = _TIER_MAX_TOKENS.get(routing.tier, 16384)

            # Pacing: rely on proxy 429 + retry logic instead of idle sleep.
            # (Removed 0.1-0.2s sleep — saves 1-2s/task, proxy handles rate limits.)

            # Force tool use on all non-final turns to eliminate planning/narration waste.
            # "any"/"required" forces at least one tool call; only the very last turn uses "auto".
            # NOTE: Cannot combine with reasoning_effort on Claude — API rejects thinking + forced tool_choice.
            _is_last_turn = (turn >= effective_max_turns - 1)
            _force_tools = (
                config.optimizations.force_first_tool
                and not _is_last_turn
                and tool_schemas  # only if tools are available
                and (_is_non_claude or not config.reasoning_effort)  # OK for non-Claude; blocks reasoning+tool_choice on Claude
            )

            # Make the API call with transient error retry.
            _max_api_retries = 3
            for _api_attempt in range(_max_api_retries):
                try:
                    if _use_openai:
                        # OpenAI endpoint — streaming SSE, supports reasoning_effort + tools.
                        oai_messages = _to_openai_messages(system_prompt, messages)
                        payload: dict[str, Any] = {
                            "model": routing.model,
                            "messages": oai_messages,
                            "max_tokens": _max_tokens,
                        }
                        # Only add reasoning_effort for Claude models that support it,
                        # clamped to the strongest value the model accepts.
                        if _reasoning_active and _effective_effort:
                            payload["reasoning_effort"] = _effective_effort
                        if _oai_tools:
                            payload["tools"] = _oai_tools
                            if _force_tools:
                                payload["tool_choice"] = "required"
                        raw = await asyncio.to_thread(
                            _openai_stream_call, _base_url, payload
                        )
                        response = _parse_openai_response(raw)
                    else:
                        # Anthropic SDK — streaming for Opus, sync for others.
                        kwargs: dict[str, Any] = {
                            "model": routing.model,
                            "max_tokens": _max_tokens,
                            "system": system_prompt,
                            "messages": messages,
                        }
                        if tool_schemas:
                            kwargs["tools"] = tool_schemas
                            if _force_tools:
                                kwargs["tool_choice"] = {"type": "any"}
                        _use_streaming = routing.tier in _STREAMING_TIERS
                        if _use_streaming:
                            response = await asyncio.to_thread(
                                _stream_call, client, **kwargs
                            )
                        else:
                            response = await asyncio.to_thread(
                                client.messages.create, **kwargs
                            )
                    break
                except anthropic.APIStatusError as e:
                    if e.status_code in (500, 502, 503) and _api_attempt < _max_api_retries - 1:
                        _backoff = 5.0 * (2 ** _api_attempt)
                        log.warning("Transient %d on turn %d — retrying in %.0fs", e.status_code, turn + 1, _backoff)
                        await asyncio.sleep(_backoff)
                    else:
                        raise
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (500, 502, 503) and _api_attempt < _max_api_retries - 1:
                        _backoff = 5.0 * (2 ** _api_attempt)
                        log.warning("Transient %d on turn %d — retrying in %.0fs", e.response.status_code, turn + 1, _backoff)
                        await asyncio.sleep(_backoff)
                    elif (
                        e.response.status_code == 400
                        and "tool_use_id" in e.response.text
                        and _api_attempt < _max_api_retries - 1
                        and len(messages) > _anchor + 2
                    ):
                        # Proxy 400 bug: tool_use_id mismatch from context
                        # truncation or OAI→Anthropic back-translation.
                        # Sanitize tool pairs, then drop the oldest non-prefix
                        # exchange to shift context and retry.
                        messages = _sanitize_tool_pairs(messages, _anchor)
                        # If sanitization alone isn't enough (e.g. the proxy's
                        # internal translation still chokes), drop the oldest
                        # post-anchor pair to shift IDs.
                        if len(messages) > _anchor + 2:
                            del messages[_anchor:_anchor + 2]
                        _backoff = 5.0
                        log.warning(
                            "Proxy 400 tool_use_id bug on turn %d — sanitized + dropped oldest pair, retrying in %.0fs",
                            turn + 1, _backoff,
                        )
                        await asyncio.sleep(_backoff)
                    else:
                        raise
                except (httpx.StreamError, httpx.ReadError) as e:
                    if _api_attempt < _max_api_retries - 1:
                        _backoff = 5.0 * (2 ** _api_attempt)
                        log.warning("Stream error on turn %d — retrying in %.0fs: %s", turn + 1, _backoff, e)
                        await asyncio.sleep(_backoff)
                    else:
                        raise

            result.num_turns = turn + 1

            # Track premium requests consumed.
            # Deep tier: 1 × multiplier for the session.
            # Other tiers: turns × multiplier.
            if routing.tier == "deep":
                result.premium_requests = _premium_multiplier  # 1 × 3 = 3 for Opus
            else:
                result.premium_requests = result.num_turns * _premium_multiplier

            # Update progress tracker (survives timeout cancellation)
            if _progress is not None:
                _progress.num_turns = result.num_turns
                _progress.premium_requests = result.premium_requests

            # Track token usage from API response
            if hasattr(response, 'usage') and response.usage:
                result.input_tokens += getattr(response.usage, 'input_tokens', 0)
                result.output_tokens += getattr(response.usage, 'output_tokens', 0)

            # Process response content blocks
            assistant_content: list[dict[str, Any]] = []
            has_tool_use = False

            if not response.content:
                log.warning("Empty response content from API (turn %d)", turn + 1)
                _consecutive_empty += 1
                # Anthropic-recommended fix: inject continuation prompt in NEW user message
                if _consecutive_empty <= _MAX_CONSECUTIVE_EMPTY and turn < effective_max_turns - 1 and tool_schemas:
                    messages.append({"role": "assistant", "content": [{"type": "text", "text": "..."}]})
                    messages.append({"role": "user", "content": "Continue. Call the tools needed to complete the task."})
                    result.messages.append({"role": "user", "content": "Continue. Call the tools needed to complete the task."})
                    continue
                break

            for block in response.content:
                if block.type == "text":
                    result.text += block.text
                    assistant_content.append({
                        "type": "text",
                        "text": block.text,
                    })
                elif block.type == "tool_use":
                    has_tool_use = True
                    result.tools_used.append(block.name)
                    if _progress is not None:
                        _progress.tools_used.append(block.name)
                    assistant_content.append({
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

            # Append assistant message
            messages.append({"role": "assistant", "content": assistant_content})
            result.messages.append({"role": "assistant", "content": assistant_content})

            # If no tool use, we're done — the model chose to finish.
            # (_force_tools on non-final turns already prevents premature text-only responses.)
            if not has_tool_use:
                break
            # Handle max_tokens truncation: if response was cut off mid-generation,
            # the model may still have tool calls queued. Continue with prefill.
            # (Aider's "infinite output" technique — assistant prefill continuation.)
            if response.stop_reason == "max_tokens" and has_tool_use:
                log.info("max_tokens reached with pending tool calls (turn %d), continuing", turn + 1)
                # Don't break — proceed to execute the tool calls we did get

            _consecutive_empty = 0  # got real content, reset empty counter

            # Execute tools IN PARALLEL and build tool_result messages.
            # When the model returns N tool_use blocks in one response,
            # running them concurrently via asyncio.gather saves wall time
            # and lets more work fit within the timeout budget.
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_results: list[dict[str, Any]] = []
            _turn_had_error = False

            if tool_blocks and _total_tool_calls + len(tool_blocks) > max_tool_calls:
                log.warning(
                    "Tool budget exhausted: %d >= %d (turn %d) — stopping",
                    _total_tool_calls, max_tool_calls, turn + 1,
                )
                result.error = f"Tool budget exhausted: {_total_tool_calls} >= {max_tool_calls}"
                break

            if tool_blocks:
                _tool_sem = asyncio.Semaphore(_MAX_PARALLEL_TOOLS)

                # Pre-filter redundant file_reads: short-circuit with synthetic
                # response and DON'T count them against the tool budget.
                # This was the #1 cause of tool-budget exhaustion in Sonnet+high
                # reasoning sprints (3+ redundant reads per task observed).
                def _is_redundant_read(block: Any) -> bool:
                    if not _preloaded_paths or block.name != "file_read":
                        return False
                    path = block.input.get("path", "") or ""
                    for pp in _preloaded_paths:
                        if path.endswith(pp) or pp.endswith(path):
                            return True
                    return False

                async def _exec_one(block: Any) -> tuple[Any, str, bool]:
                    if _is_redundant_read(block):
                        path = block.input.get("path", "")
                        synthetic = (
                            f"[redundant file_read intercepted] '{path}' was already pre-loaded "
                            "at task start. Use the pre-loaded content in the task prompt; do not "
                            "call file_read on it again."
                        )
                        return block, synthetic, True
                    async with _tool_sem:
                        log.debug("Calling tool: %s", block.name)
                        output = await _execute_tool(block.name, block.input, tools or {})
                        return block, output, False

                parallel_results = await asyncio.gather(
                    *[_exec_one(b) for b in tool_blocks]
                )
                # Only count tool calls that were actually executed (not redundant-intercepted).
                _executed_count = sum(1 for _, _, is_red in parallel_results if not is_red)
                _total_tool_calls += _executed_count
                for block, tool_output, is_redundant in parallel_results:
                    if tool_output.startswith("Error"):
                        _turn_had_error = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": tool_output,
                    })
                    if is_redundant:
                        _redundant_reads += 1
                        log.warning(
                            "Redundant file_read intercepted: '%s' (waste #%d, NOT charged to budget)",
                            block.input.get("path", ""), _redundant_reads,
                        )

            # Track consecutive tool errors — break if stuck in a failure loop
            if _turn_had_error:
                _consecutive_tool_errors += 1
                if _consecutive_tool_errors >= _MAX_CONSECUTIVE_ERRORS:
                    log.warning(
                        "Stopping: %d consecutive tool errors (turn %d) — likely stuck",
                        _consecutive_tool_errors, turn + 1,
                    )
                    result.error = f"Stopped after {_consecutive_tool_errors} consecutive tool errors"
                    break
            else:
                _consecutive_tool_errors = 0

            # Append tool results as user message (Anthropic API format).
            # CRITICAL: Do NOT mix text blocks with tool_result blocks.
            # Anthropic docs: "Adding text immediately after tool_result causes Claude to end turn."
            messages.append({"role": "user", "content": tool_results})
            result.messages.append({"role": "user", "content": tool_results})

            # Combined density nudge + turn budget signal (single message, not two).
            # Anthropic docs: text MUST be a separate message after tool_results.
            _is_early = (turn < effective_max_turns - 2)  # not near the end
            _signal_parts: list[str] = []

            # Low-density nudge — only on early turns with truly low tool count
            if _is_early and len(tool_blocks) < 3 and tool_schemas:
                _signal_parts.append(f"Call 6+ tools in parallel")
                log.debug("Low-density nudge at turn %d (%d tools)", turn + 1, len(tool_blocks))

            # Turn budget signal — urgency when turns are running low
            if config.optimizations.turn_budget_signal and turn >= 1:
                remaining = effective_max_turns - turn - 1
                if remaining <= 2:
                    _signal_parts.append(f"{remaining} turns left — finish NOW")
                elif remaining <= 4:
                    _signal_parts.append(f"{remaining} turns left")

            if _signal_parts:
                _signal = f"[{' | '.join(_signal_parts)}]"
                messages.append({"role": "user", "content": _signal})
                result.messages.append({"role": "user", "content": _signal})

    except anthropic.APIError as e:
        result.error = f"API error: {e.message}"
        log.error("API error: %s", e)
    except httpx.HTTPStatusError as e:
        result.error = f"API error: {e.response.status_code} {e.response.text[:200]}"
        log.error("OpenAI endpoint error: %s", e)
    except Exception as e:
        result.error = str(e)
        log.error("Agent error: %s", e)

    result.duration_ms = int((time.monotonic() - t0) * 1000)

    # Estimate cost_usd from token counts (Anthropic API list prices, not Copilot)
    # Haiku: $0.80/$4 per 1M in/out, Sonnet: $3/$15, Opus: $15/$75
    _TOKEN_PRICES: dict[str, tuple[float, float]] = {
        "claude-haiku-4.5":  (0.80 / 1e6, 4.00 / 1e6),
        "claude-sonnet-4.6": (3.00 / 1e6, 15.00 / 1e6),
        "claude-opus-4.6":   (15.00 / 1e6, 75.00 / 1e6),
        "claude-opus-4.7":   (15.00 / 1e6, 75.00 / 1e6),
    }
    _in_rate, _out_rate = _TOKEN_PRICES.get(routing.model, (3.00 / 1e6, 15.00 / 1e6))
    result.cost_usd = result.input_tokens * _in_rate + result.output_tokens * _out_rate

    # Quality scoring (heuristic, zero-cost)
    result.quality_score = _score_quality(result)
    result.redundant_reads = _redundant_reads
    if _redundant_reads > 0:
        log.warning(
            "Task had %d redundant file_read(s) on pre-loaded content — wasted turns",
            _redundant_reads,
        )

    # Update memory
    memory.add_short(f"Task: {task[:100]}")
    if result.error:
        memory.add_short(f"Error: {result.error[:100]}")
    memory.save()

    return result
