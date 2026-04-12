"""Core agent — thin wrapper around Claude Code SDK.

Runs tasks through the Claude Code SDK with model routing, memory injection,
and Gmail/Calendar MCP tools. All API calls route through the copilot-api
proxy for Copilot Pro billing.

This is the execution engine for both one-shot CLI tasks and the 24/7
autonomous watcher loop.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import query, ClaudeAgentOptions, Message, HookMatcher

from .config import SecretaryConfig
from .context_builder import build_identity_prompt
from .router import select_model, RoutingDecision
from .memory import MemoryStore
from .tools import build_mcp_servers, MCP_TOOL_NAMES


def _make_streaming_prompt(task: str, done: asyncio.Event):
    """Create an async generator that yields the prompt then keeps stdin alive.

    The Claude Code SDK closes stdin when the generator exhausts. MCP tool
    responses flow through stdin, so we must keep it open until the
    conversation finishes.
    """
    async def _gen() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "user", "message": {"role": "user", "content": task}}
        await done.wait()
    return _gen()


@dataclass
class RunResult:
    """Result of a single agent run."""
    task: str
    routing: RoutingDecision
    messages: list[Message] = field(default_factory=list)
    text: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    session_id: str = ""
    tools_used: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def _build_system_prompt(memory: MemoryStore, workspace_dir: str = "workspace") -> str:
    """Build system prompt with memory context.

    Uses workspace identity files (SOUL.md, USER.md, etc.) when available,
    falling back to hardcoded defaults if the workspace directory is missing.
    """
    identity = build_identity_prompt(workspace_dir)
    if identity:
        parts = [identity]
    else:
        parts = [
            "You are a capable research assistant, email secretary, and calendar manager.",
            "You have access to Gmail and Google Calendar via MCP tools.",
            "Be concise. Take action directly rather than explaining what you would do.",
        ]

    long_mem = memory.get_long()
    if long_mem:
        parts.append("\n## Long-term memory (persistent learnings)")
        for i, entry in enumerate(long_mem[-10:]):
            parts.append(f"- {entry}")
            # Track access for decay system — use offset from end
            offset = max(0, len(long_mem) - 10)
            memory.access_long(offset + i)

    short_mem = memory.get_short()
    if short_mem:
        parts.append("\n## Recent context")
        for entry in short_mem[-5:]:
            parts.append(f"- {entry}")

    return "\n".join(parts)


def _ensure_env(config: SecretaryConfig) -> None:
    """Set environment variables for copilot-api proxy routing."""
    if not os.environ.get("ANTHROPIC_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = config.anthropic_base_url
    if not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = "copilot-proxy"


async def run(
    task: str,
    config: SecretaryConfig | None = None,
    memory: MemoryStore | None = None,
    force_tier: str | None = None,
    cwd: str | None = None,
    hooks: dict | None = None,
    max_turns: int | None = None,
) -> RunResult:
    """Run a task through the Claude SDK with model routing.

    Args:
        task: The prompt / task description.
        config: Secretary config (loads default if None).
        memory: Memory store to inject context (loads default if None).
        force_tier: Override model routing ("low", "medium", "high").
        cwd: Working directory for the SDK agent.
        hooks: SDK hook dict to pass to ClaudeAgentOptions (e.g. PostToolUse).
        max_turns: Override the tier's default max_turns for this run.
    """
    if config is None:
        config = SecretaryConfig.load()
    if memory is None:
        memory = MemoryStore.load(config.memory_path)

    routing = select_model(config, task, force_tier)
    _ensure_env(config)

    system_prompt = _build_system_prompt(memory, workspace_dir=getattr(config, 'workspace_dir', 'workspace'))

    options = ClaudeAgentOptions(
        model=routing.model,
        system_prompt=system_prompt,
        max_turns=max_turns if max_turns is not None else routing.max_turns,
    )
    if cwd:
        options.cwd = cwd
    if routing.max_budget_usd > 0:
        options.max_budget_usd = routing.max_budget_usd

    # Register Gmail/Calendar as in-process MCP tools (if Google auth is set up)
    mcp_servers = build_mcp_servers(config.data_path)
    if mcp_servers:
        options.mcp_servers = mcp_servers
        # MCP tools need explicit permission bypass for autonomous operation
        options.permission_mode = "bypassPermissions"
        # Whitelist our MCP tools so the agent can call them
        options.allowed_tools = [f"mcp__{s}__{t}" for s in mcp_servers for t in MCP_TOOL_NAMES.get(s, [])]

    # Built-in hook: track which tools the agent uses
    async def _track_tool_use(hook_input, matcher, ctx):
        """PostToolUse hook — record each tool invocation for run metrics."""
        tool_name = hook_input.get("tool_name", "")
        if tool_name:
            result.tools_used.append(tool_name)
        return {"outputToClient": False}

    builtin_hooks: dict = {
        "PostToolUse": [HookMatcher(matcher=None, hooks=[_track_tool_use])],
    }

    # Merge caller-supplied hooks
    if hooks:
        for event, matchers in hooks.items():
            if event in builtin_hooks:
                builtin_hooks[event].extend(matchers)
            else:
                builtin_hooks[event] = matchers
    options.hooks = builtin_hooks

    result = RunResult(task=task, routing=routing)

    # MCP tools need streaming mode so stdin stays open for tool responses.
    # The keep-alive Event prevents the generator from exhausting (which would
    # close stdin before tool results can be written back).
    done = asyncio.Event()
    prompt: Any = _make_streaming_prompt(task, done) if mcp_servers else task

    try:
        async for msg in query(prompt=prompt, options=options):
            result.messages.append(msg)
            # Extract text from assistant messages
            if hasattr(msg, "content") and isinstance(msg.content, str):
                result.text += msg.content
            elif hasattr(msg, "content") and isinstance(msg.content, list):
                for block in msg.content:
                    if hasattr(block, "text"):
                        result.text += block.text
            # Release the keep-alive generator once the conversation ends.
            # ResultMessage is the SDK's final message — releasing the
            # generator lets stream_input() finish so the TaskGroup exits
            # cleanly instead of waiting for a 45-second subprocess timeout.
            if type(msg).__name__ == "ResultMessage":
                # Extract rich metrics from ResultMessage
                if hasattr(msg, "total_cost_usd") and msg.total_cost_usd is not None:
                    result.cost_usd = msg.total_cost_usd
                if hasattr(msg, "num_turns"):
                    result.num_turns = msg.num_turns
                if hasattr(msg, "duration_ms"):
                    result.duration_ms = msg.duration_ms
                if hasattr(msg, "session_id"):
                    result.session_id = msg.session_id
                done.set()
    except BaseException as e:
        if isinstance(e, Exception):
            result.error = str(e)
    finally:
        done.set()  # Ensure release even on error

    # Update memory with task summary
    memory.add_short(f"Task: {task[:100]}")
    if result.error:
        memory.add_short(f"Error: {result.error[:100]}")
    memory.save()

    return result
