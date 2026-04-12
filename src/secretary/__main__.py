"""CLI entry point for Claude Secretary.

Commands:
    secretary run <task>         Run a one-shot task
    secretary chat               Interactive multi-turn chat
    secretary watch              Start 24/7 watcher with campaign
    secretary improve <task>     Self-improvement pipeline
    secretary auth               Set up Google OAuth
    secretary status             Show config, memory, routing info
    secretary history            Show run history and statistics
    secretary test               Validate setup (proxy, auth, campaign)
    secretary campaign [file]    Preview campaign tasks and costs
    secretary config [key]       Show configuration (dot-notation key)
    secretary logs               Search and filter run logs
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from .config import SecretaryConfig


def _setup_logging(verbose: bool = False) -> None:
    """Configure root logger with timestamp format. Debug level when verbose."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser with all subcommands and their options."""
    p = argparse.ArgumentParser(
        prog="secretary",
        description="Claude SDK research assistant & secretary",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    p.add_argument("-c", "--config", default="config.yaml", help="Config file path")

    _tier_kwargs = dict(choices=["free", "low", "medium", "high", "deep", "oracle"], help="Force model tier")

    sub = p.add_subparsers(dest="command")

    # run
    run_p = sub.add_parser("run", help="Run a one-shot task")
    run_p.add_argument("task", nargs="+", help="Task description")
    run_p.add_argument("--tier", **_tier_kwargs)
    run_p.add_argument("--cwd", help="Working directory for the agent")
    run_p.add_argument("--workspace", metavar="DIR",
                       help="Enable file tools sandboxed to this directory")
    run_p.add_argument("--files", action="store_true",
                       help="Enable unrestricted file read/write (any path)")
    run_p.add_argument("--max-turns", type=int, default=None, dest="max_turns",
                       help="Override max turns for this run")
    run_p.add_argument("--sdk", action="store_true",
                       help="Use legacy Claude Agent SDK instead of direct API")

    # chat
    chat_p = sub.add_parser("chat", help="Interactive multi-turn chat")
    chat_p.add_argument("--tier", **_tier_kwargs)
    chat_p.add_argument("--workspace", metavar="DIR",
                        help="Enable file tools sandboxed to this directory")
    chat_p.add_argument("--files", action="store_true",
                        help="Enable unrestricted file read/write (any path)")
    chat_p.add_argument("--sdk", action="store_true",
                        help="Use legacy Claude Agent SDK instead of direct API")

    # watch
    watch_p = sub.add_parser("watch", help="Start 24/7 watcher")
    watch_p.add_argument("--campaign", help="Campaign YAML file")
    watch_p.add_argument("--interval", type=int, help="Minutes between runs")
    watch_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="Show what would run without calling the API")
    watch_p.add_argument("--max-premium", type=float, dest="max_premium", default=0,
                         help="Max premium multiplier per cycle (e.g. 3.0 = three 1x requests)")
    watch_p.add_argument("--max-runs", type=int, dest="max_runs", default=0,
                         help="Stop after N cycles (0 = unlimited)")
    watch_p.add_argument("--max-retries", type=int, dest="max_retries", default=None,
                         help="Retries per failed task (default: 2)")
    watch_p.add_argument("--instance", dest="instance_id", default="",
                         help="Instance ID for parallel runs (namespaces data files)")
    watch_p.add_argument("--role", default="",
                         help="Instance role: researcher, triager, builder, monitor (empty = generalist)")
    watch_p.add_argument("--coordinate", action="store_true",
                         help="Enable cross-instance coordination (claim-based task distribution)")

    # improve
    imp_p = sub.add_parser("improve", help="Self-improvement pipeline")
    imp_p.add_argument("task", nargs="+", help="Improvement task description")
    imp_p.add_argument("--tier", **_tier_kwargs)
    imp_p.add_argument("--project", default=".", help="Project directory to improve")
    imp_p.add_argument("--promote", action="store_true", help="Auto-promote if tests pass")
    imp_p.add_argument("--keep-sandbox", action="store_true", dest="keep_sandbox",
                       help="Keep sandbox directory after completion for review")
    imp_p.add_argument("--max-turns", type=int, default=None, dest="max_turns",
                       help="Override max turns for this run")
    imp_p.add_argument("--target", nargs="+", dest="target_files", default=None,
                       help="Target source files (e.g. src/secretary/self_improve.py)")
    imp_p.add_argument("--sdk", action="store_true",
                       help="Use legacy Claude Agent SDK instead of direct API")

    # auth
    sub.add_parser("auth", help="Set up Google OAuth for Gmail/Calendar")

    # version
    sub.add_parser("version", help="Show package, Python, and SDK versions")

    # history
    hist_p = sub.add_parser("history", help="Show run history and statistics")
    hist_p.add_argument("--tier", **_tier_kwargs)
    hist_p.add_argument("--last", type=int, default=10, help="Number of recent runs to show")
    hist_p.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")
    hist_p.add_argument("--failed", action="store_true", help="Show only failed runs")
    hist_p.add_argument("--search", type=str, default=None, help="Filter tasks by keyword (case-insensitive)")

    # status
    sub.add_parser("status", help="Show current configuration and status")

    # campaign
    camp_p = sub.add_parser("campaign", help="Validate and preview a campaign file")
    camp_p.add_argument("file", nargs="?", default=None, help="Campaign YAML file (default: from config)")

    # test
    sub.add_parser("test", help="Validate setup: proxy, Google auth, campaign, config")

    # config
    cfg_p = sub.add_parser("config", help="Show current configuration or get a specific value")
    cfg_p.add_argument("key", nargs="?", default=None,
                       help="Config key (dot-notation, e.g. routing.default_tier, watcher.interval_minutes)")

    # logs
    logs_p = sub.add_parser("logs", help="Search and filter run logs")
    logs_p.add_argument("--search", "-s", help="Search text in task descriptions")
    logs_p.add_argument("--tier", choices=["free", "low", "medium", "high", "deep", "oracle"], help="Filter by tier")
    logs_p.add_argument("--failed", action="store_true", help="Show only failed runs")
    logs_p.add_argument("--last", type=int, default=20, help="Number of recent entries (default: 20)")
    logs_p.add_argument("--json", action="store_true", dest="json_output", help="Output as JSON")

    # memory
    mem_p = sub.add_parser("memory", help="View and manage the memory store")
    mem_p.add_argument("action", nargs="?", default="show",
                       choices=["show", "clear-short", "clear-long", "clear-all"],
                       help="Action: show (default), clear-short, clear-long, clear-all")

    # heartbeat
    sub.add_parser("heartbeat", help="Check watcher heartbeat status")

    # health
    sub.add_parser("health", help="Run a health check on the secretary system")

    # check — one-stop quick dashboard
    sub.add_parser("check", help="Quick dashboard: daemon alive? recent runs? self-improve state?")

    # goals
    goals_p = sub.add_parser("goals", help="Show per-goal progress, strategies, and health")
    goals_p.add_argument("--json", action="store_true", dest="json_out",
                         help="Output raw JSON instead of formatted text")
    goals_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                         help="Simulate a goal cycle: show what tasks would be generated")
    goals_p.add_argument("--pending", action="store_true",
                         help="Show tasks awaiting approval")
    goals_p.add_argument("--approve", metavar="ID",
                         help="Approve a pending task (or 'all' for all pending)")
    goals_p.add_argument("--reject", metavar="ID",
                         help="Reject a pending task by ID")
    goals_p.add_argument("--trust", action="store_true",
                         help="Show per-goal trust scores and policy suggestions")

    # estimate
    est_p = sub.add_parser("estimate", help="Preview routing for a task without executing")
    est_p.add_argument("task", nargs="+", help="Task description")
    est_p.add_argument("--tier", **_tier_kwargs)

    # export
    exp_p = sub.add_parser("export", help="Export run history to CSV or JSON")
    exp_p.add_argument("format", choices=["csv", "json"], help="Export format")
    exp_p.add_argument("-o", "--output", help="Output file (default: stdout)")
    exp_p.add_argument("--last", type=int, default=0, help="Last N entries (0 = all)")

    # audit
    sub.add_parser("audit", help="Analyze run history for cost optimization opportunities")

    # analyze
    sub.add_parser("analyze", help="Deep campaign analysis — reliability, patterns, suggestions")

    # budget
    sub.add_parser("budget", help="Show current daily/weekly spend vs budget limits")

    # forecast
    fc_p = sub.add_parser("forecast", help="Predict future costs based on recent history")
    fc_p.add_argument("--days", type=int, default=30, help="Number of days to forecast (default: 30)")

    # mode
    mode_p = sub.add_parser("mode", help="Switch between free (proxy) and paid (direct API) mode")
    mode_p.add_argument("billing", nargs="?", choices=["free", "paid"], help="Set billing mode")

    # metrics
    met_p = sub.add_parser("metrics", help="View multi-instance metrics and benchmark comparisons")
    met_p.add_argument("action", nargs="?", default="show",
                       choices=["show", "instances", "benchmarks"],
                       help="show=per-instance summary, instances=active instances, benchmarks=comparison history")

    return p


async def _cmd_run(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Execute a one-shot task via the agent, log result, and print output."""
    from .run_log import RunLog, RunLogEntry
    import time

    use_sdk = getattr(args, "sdk", False)

    task = " ".join(args.task)

    print(f"Task: {task}")

    from .router import select_model, get_premium_cost
    routing = select_model(config, task, args.tier)
    task_cost = get_premium_cost(routing.model)
    mode = "SDK" if use_sdk else "direct"
    print(f"Model: {routing.model} ({routing.tier}, {task_cost}x premium) [{mode}] — {routing.reason}")
    print("---")

    t0 = time.monotonic()
    if use_sdk:
        from . import agent
        result = await agent.run(
            task=task,
            config=config,
            force_tier=args.tier,
            cwd=args.cwd,
            max_turns=args.max_turns,
        )
    else:
        from . import direct_agent
        from .direct_tools import build_tool_registry
        unrestricted = getattr(args, 'files', False) or config.file_tools
        workspace = getattr(args, 'workspace', None) or config.file_workspace or None
        workspace_root = Path(workspace).resolve() if workspace and not unrestricted else None
        tools = build_tool_registry(
            config.data_path,
            workspace_root=workspace_root,
            unrestricted_files=unrestricted,
        )
        # Oracle ensemble: route to oracle_run for "oracle" tier
        if args.tier == "oracle" or routing.tier == "oracle":
            from .oracle import oracle_run
            result = await oracle_run(
                task=task,
                config=config,
                tools=tools,
                max_turns=args.max_turns,
            )
        else:
            # Try deterministic pipeline first (zero-LLM, zero-cost)
            from .deterministic import try_deterministic
            result = await try_deterministic(task, tools, config)
            if result is not None:
                print("[deterministic — no LLM needed]")
            else:
                result = await direct_agent.run(
                    task=task,
                    config=config,
                    force_tier=args.tier,
                    tools=tools,
                    max_turns=args.max_turns,
                )
    duration = time.monotonic() - t0

    # Log the run
    run_log = RunLog(config.data_path / "run_log.jsonl")
    run_log.append(RunLogEntry(
        timestamp=RunLog.now(),
        cycle=0,
        task=task[:200],
        tier=routing.tier,
        model=routing.model,
        success=result.error is None,
        output_preview=result.text[:500] if result.text else "",
        error=result.error,
        duration_s=round(duration, 1),
        premium_cost=task_cost,
        cost_usd=result.cost_usd,
        num_turns=result.num_turns,
        tools_used=result.tools_used,
    ))

    if result.error:
        print(f"\nError: {result.error}", file=sys.stderr)
        sys.exit(1)
    else:
        print(result.text)
        # Show SDK-reported metrics if available
        from .currency import format_cost
        parts = []
        if result.cost_usd > 0:
            parts.append(format_cost(result.cost_usd))
        if result.num_turns > 0:
            parts.append(f"{result.num_turns} turns")
        if result.input_tokens > 0 or result.output_tokens > 0:
            parts.append(f"{result.input_tokens + result.output_tokens} tokens")
        parts.append(f"{duration:.1f}s")
        if parts:
            print(f"\n--- [{' | '.join(parts)}]")


async def _cmd_chat_sdk(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Legacy SDK chat path (1x premium per request)."""
    from .memory import MemoryStore
    from .router import select_model, get_premium_cost
    from . import agent

    memory = MemoryStore.load(config.memory_path)
    print("Claude Secretary — interactive chat [SDK (premium)] (type 'quit' to exit)")
    print(f"Default tier: {config.routing.default_tier}")
    print("---")

    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        routing = select_model(config, user_input, args.tier)
        cost = get_premium_cost(routing.model)
        print(f"[{routing.model} / {routing.tier} / {cost}x premium]")

        result = await agent.run(
            task=user_input,
            config=config,
            memory=memory,
            force_tier=args.tier,
        )

        if result.error:
            print(f"Error: {result.error}")
        else:
            print(result.text)


async def _cmd_chat_direct(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Direct agent chat path (proxy mode)."""
    from .memory import MemoryStore
    from .router import select_model
    from . import direct_agent
    from .direct_tools import build_tool_registry

    memory = MemoryStore.load(config.memory_path)

    unrestricted = getattr(args, 'files', False) or config.file_tools
    workspace = getattr(args, 'workspace', None) or config.file_workspace or None
    workspace_root = Path(workspace).resolve() if workspace and not unrestricted else None
    tools = build_tool_registry(
        config.data_path,
        workspace_root=workspace_root,
        unrestricted_files=unrestricted,
    )

    current_tier = getattr(args, 'tier', None)

    print("Claude Secretary — interactive chat [direct mode] (type 'quit' to exit)")
    print(f"Tools: {', '.join(sorted(tools.keys()))}")
    print(f"Default tier: {config.routing.default_tier}")
    print("---")

    history: list[dict[str, str]] = []

    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Bye!")
            break

        # Slash commands
        if user_input.startswith("/tier"):
            parts = user_input.split()
            if len(parts) == 2 and parts[1] in ("free", "low", "medium", "high", "deep", "oracle"):
                current_tier = parts[1]
                routing = select_model(config, "test", current_tier)
                print(f"Switched to {current_tier} tier ({routing.model})")
            else:
                print("Usage: /tier free|low|medium|high|deep|oracle")
            continue

        # Build task with conversation context
        if history:
            context = "\n".join(
                f"User: {h['user']}\nAssistant: {h['assistant']}"
                for h in history
            )
            task = f"Previous conversation:\n{context}\n\nUser: {user_input}"
        else:
            task = user_input

        routing = select_model(config, user_input, current_tier)
        print(f"[{routing.model} / {routing.tier}]")

        result = await direct_agent.run(
            task=task,
            config=config,
            memory=memory,
            force_tier=current_tier,
            tools=tools,
        )

        if result.error:
            print(f"Error: {result.error}")
        else:
            print(result.text)
            history.append({"user": user_input, "assistant": result.text})

        # Show metrics
        from .currency import format_cost
        parts = []
        if result.cost_usd > 0:
            parts.append(format_cost(result.cost_usd))
        if result.num_turns > 0:
            parts.append(f"{result.num_turns} turns")
        if parts:
            print(f"  [{' | '.join(parts)}]")


async def _cmd_chat(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Interactive multi-turn chat — dispatches to SDK or direct path."""
    use_sdk = getattr(args, "sdk", False)
    if use_sdk:
        await _cmd_chat_sdk(args, config)
    else:
        await _cmd_chat_direct(args, config)


async def _cmd_watch(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Start the 24/7 watcher daemon with campaign tasks and optional coordination."""
    from .service import wait_for_proxy
    from .watcher import Watcher

    if args.instance_id:
        config.instance_id = args.instance_id
        # Ensure namespaced data directory exists
        config.data_path.mkdir(parents=True, exist_ok=True)
    if getattr(args, 'role', ''):
        config.multi.role = args.role
    if getattr(args, 'coordinate', False):
        config.multi.coordinate = True
    if args.interval:
        config.watcher.interval_minutes = args.interval
    if args.max_premium > 0:
        config.watcher.max_premium_per_cycle = args.max_premium
    if args.max_runs > 0:
        config.watcher.max_runs = args.max_runs
    if args.max_retries is not None:
        config.watcher.max_retries = args.max_retries

    # Preflight: validate campaign before entering the loop
    campaign_raw = args.campaign or config.watcher.campaign_file
    campaign_path = Path(campaign_raw)
    if campaign_path.exists():
        from .campaign import validate_campaign
        result = validate_campaign(str(campaign_path))
        if not result.valid:
            print("Campaign validation failed:", file=sys.stderr)
            for err in result.errors:
                print(f"  ✗ {err}", file=sys.stderr)
            sys.exit(1)

    # Preflight: wait for copilot-api proxy before starting the loop
    from .config import _interpolate_env
    proxy_url = _interpolate_env(config.anthropic_base_url).rstrip("/") + "/v1/models"
    if not args.dry_run and not wait_for_proxy(proxy_url, timeout=60):
        print("Error: copilot-api proxy not reachable. Start it with: npx copilot-api@latest start", file=sys.stderr)
        sys.exit(1)

    watcher = Watcher(
        config=config,
        campaign_file=campaign_raw,
        dry_run=args.dry_run,
    )
    await watcher.run()


async def _cmd_improve(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Run the self-improvement pipeline: sandbox → agent → test → promote."""
    from .self_improve import improve

    task = " ".join(args.task)
    project = Path(args.project).resolve()
    print(f"Self-improvement: {task}")
    print(f"Project: {project}")
    print("---")

    result = await improve(
        task=task,
        project_dir=project,
        config=config,
        auto_promote=args.promote,
        keep_sandbox=args.keep_sandbox,
        max_turns=args.max_turns,
        target_files=args.target_files,
    )

    if result.error:
        print(f"Error: {result.error}", file=sys.stderr)
    if result.changed_files:
        print(f"\nChanged files ({len(result.changed_files)}):")
        for f in result.changed_files:
            print(f"  {f}")
    print(f"\nTests passed: {result.tests_passed}")
    if not result.tests_passed and result.test_output:
        print(f"\nTest output:\n{result.test_output[-2000:]}")
    if result.promoted:
        print("Changes promoted to source!")
        if result.backup_dir:
            print(f"Backup: {result.backup_dir}")
    elif result.tests_passed and not result.promoted:
        print("Tests passed but auto-promote disabled. Run with --promote to apply.")
        print(f"Sandbox: {result.sandbox_dir}")

    # Show cost info
    if result.cost_usd > 0 or result.num_turns > 0:
        from .currency import format_cost
        parts = []
        if result.cost_usd > 0:
            parts.append(format_cost(result.cost_usd))
        if result.num_turns > 0:
            parts.append(f"{result.num_turns} turns")
        if parts:
            print(f"\n--- [{' | '.join(parts)}]")

    if not result.tests_passed:
        sys.exit(1)


def _cmd_auth(config: SecretaryConfig) -> None:
    """Run the Google OAuth interactive flow and save credentials."""
    from .mcp_tools.google_auth import run_oauth_flow

    data_root = config.data_path
    print(f"Setting up Google OAuth (tokens stored in {data_root}/)")
    creds = run_oauth_flow(data_root)
    print(f"Authenticated successfully. Token saved.")
    return creds


def _cmd_version() -> None:
    """Print package version, Python version, and claude-agent-sdk version."""
    import sys
    from . import __version__

    print(f"claude-secretary {__version__}")
    print(f"Python {sys.version.split()[0]}")

    try:
        from importlib.metadata import version as pkg_version
        sdk_ver = pkg_version("claude-agent-sdk")
        print(f"claude-agent-sdk {sdk_ver}")
    except Exception:
        print("claude-agent-sdk (unknown)")


def _cmd_status(config: SecretaryConfig) -> None:
    """Display current configuration, memory stats, routing info, and run history."""
    from .memory import MemoryStore
    from .run_log import RunLog

    print("=== Claude Secretary Status ===\n")

    print(f"Proxy URL: {config.anthropic_base_url}")
    print(f"Data root: {config.data_root}")
    print()

    print("Model routing:")
    for name, tier in config.routing.tiers.items():
        budget = f", ${tier.max_budget_usd} cap" if tier.max_budget_usd else ""
        print(f"  {name}: {tier.model} (max {tier.max_turns} turns{budget})")
    print(f"  Default: {config.routing.default_tier}")
    print()

    memory = MemoryStore.load(config.memory_path)
    mem_size = ""
    if config.memory_path.exists():
        mem_size = f" ({config.memory_path.stat().st_size / 1024:.1f} KB)"
    print(f"Memory: {len(memory.short)} short-term, {len(memory.long)} long-term{mem_size}")

    # Check Google auth + MCP tools
    token_path = config.data_path / "google_token.json"
    google_ok = token_path.exists()
    print(f"Google auth: {'✓ configured' if google_ok else '✗ not configured (run: secretary auth)'}")
    if google_ok:
        print(f"MCP tools: ✓ gmail (6 tools) + calendar (4 tools) — in-process SDK")
    else:
        print(f"MCP tools: ✗ disabled (no Google auth)")

    # Check campaign
    campaign = Path(config.watcher.campaign_file)
    print(f"Campaign: {'✓ ' + str(campaign) if campaign.exists() else '✗ no campaign file'}")
    budget = config.watcher.max_premium_per_cycle
    budget_str = f"{budget}x/cycle" if budget > 0 else "unlimited"
    retry_str = f", retry: {config.watcher.max_retries}x" if config.watcher.max_retries > 0 else ""
    print(f"Watcher: every {config.watcher.interval_minutes} min, {'unlimited' if config.watcher.max_runs == 0 else str(config.watcher.max_runs)} runs, premium budget: {budget_str}{retry_str}")

    # Run log stats
    run_log = RunLog(config.data_path / "run_log.jsonl")
    stats = run_log.summary()
    if stats["total"] > 0:
        premium_str = f", {stats.get('total_premium', 0)}x premium" if stats.get("total_premium") else ""
        cost_str = ""
        if stats.get("total_cost_usd"):
            cost_str = f", ${stats.get('total_cost_cad', 0):.4f} CAD (${stats.get('total_cost_usd', 0):.4f} USD)"
        print(f"\nRun history: {stats['total']} runs, {stats['pass_rate']} pass rate{premium_str}{cost_str}")
        for tier, counts in stats.get("by_tier", {}).items():
            print(f"  {tier}: {counts['passed']}/{counts['total']} passed")

        # Cost forecast
        fc = run_log.forecast(30)
        if fc["confidence"] != "none":
            from .currency import format_cost
            print(f"\n30-day forecast ({fc['confidence']} confidence): {format_cost(fc['projected_usd'])}  ({fc['projected_premium']:.1f}x premium)")

        # Autonomous ratio
        from .autonomous_ratio import autonomous_task_ratio, format_ratio_summary
        ratio_stats = autonomous_task_ratio(config.data_path / "run_log.jsonl")
        if ratio_stats["total"] > 0:
            print(f"\n{format_ratio_summary(ratio_stats)}")
    else:
        print(f"\nRun history: no runs yet")


def _cmd_history(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Display run history with optional filtering by tier, status, or keyword."""
    from .history import query_history, format_history, format_history_json
    from .run_log import RunLog

    log = RunLog(config.data_path / "run_log.jsonl")
    result = query_history(
        log,
        tier=args.tier,
        last=args.last,
        failed_only=args.failed,
        search=args.search,
    )

    if args.json_output:
        print(format_history_json(result))
    else:
        print(format_history(result))


def _cmd_test(config: SecretaryConfig) -> None:
    """Validate setup: proxy reachable, Google auth, campaign, config."""
    import urllib.request
    import urllib.error

    checks_passed = 0
    checks_failed = 0

    def _check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal checks_passed, checks_failed
        status = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"  [{status}] {name}{suffix}")
        if ok:
            checks_passed += 1
        else:
            checks_failed += 1

    print("=== Secretary Setup Test ===\n")

    # 1. Config loads
    _check("Config loaded", True, f"data_root={config.data_root}")

    # 2. Model routing configured
    tiers = list(config.routing.tiers.keys())
    _check("Model tiers", len(tiers) >= 1, ", ".join(tiers))

    # 3. Proxy reachable
    proxy_url = config.anthropic_base_url.rstrip("/")
    try:
        req = urllib.request.Request(proxy_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            _check("Proxy reachable", True, f"{proxy_url} → {resp.status}")
    except urllib.error.URLError as e:
        _check("Proxy reachable", False, f"{proxy_url} → {e.reason}")
    except Exception as e:
        _check("Proxy reachable", False, f"{proxy_url} → {e}")

    # 4. Google auth
    token_path = config.data_path / "google_token.json"
    if token_path.exists():
        try:
            from .mcp_tools.google_auth import build_gmail_service
            svc = build_gmail_service(config.data_path)
            profile = svc.users().getProfile(userId="me").execute()
            email = profile.get("emailAddress", "?")
            _check("Google auth", True, email)
        except Exception as e:
            _check("Google auth", False, str(e))
    else:
        _check("Google auth", False, "no token — run: secretary auth")

    # 5. Campaign file — structure validation
    campaign_path = Path(config.watcher.campaign_file)
    if campaign_path.exists():
        from .campaign import validate_campaign
        vr = validate_campaign(campaign_path)
        if vr.valid:
            import yaml
            data = yaml.safe_load(campaign_path.read_text(encoding="utf-8"))
            count = len(data.get("tasks", []))
            detail = f"{campaign_path} — {count} tasks, valid"
            if vr.warnings:
                detail += f" ({len(vr.warnings)} warnings)"
            _check("Campaign file", True, detail)
        else:
            _check("Campaign file", False, f"{campaign_path} — {'; '.join(vr.errors)}")
    else:
        _check("Campaign file", False, f"{campaign_path} not found")

    # 6. Data directory
    data_dir = config.data_path
    data_dir.mkdir(parents=True, exist_ok=True)
    _check("Data directory", data_dir.is_dir(), str(data_dir))

    # 7. Run log writable
    run_log_path = data_dir / "run_log.jsonl"
    try:
        run_log_path.touch(exist_ok=True)
        _check("Run log writable", True, str(run_log_path))
    except OSError as e:
        _check("Run log writable", False, str(e))

    # 8. Dedup history writable
    dedup_path = data_dir / "dedup_history.json"
    try:
        dedup_path.touch(exist_ok=True)
        _check("Dedup history writable", True, str(dedup_path))
    except OSError as e:
        _check("Dedup history writable", False, str(e))

    # 9. Google Calendar auth
    if token_path.exists():
        try:
            from .mcp_tools.google_auth import build_calendar_service
            svc = build_calendar_service(config.data_path)
            svc.calendarList().list(maxResults=1).execute()
            _check("Google Calendar", True, "readable")
        except Exception as e:
            _check("Google Calendar", False, str(e))
    else:
        _check("Google Calendar", False, "no token — run: secretary auth")

    # Summary
    total = checks_passed + checks_failed
    print(f"\n{checks_passed}/{total} checks passed", end="")
    if checks_failed:
        print(f" ({checks_failed} failed)")
    else:
        print(" — all good!")


def _cmd_heartbeat(config: SecretaryConfig) -> None:
    """Check watcher heartbeat status."""
    import json as json_mod

    hb_path = config.data_path / "heartbeat.json"
    if not hb_path.exists():
        print("No heartbeat found — watcher has not been started.")
        return

    data = json_mod.loads(hb_path.read_text(encoding="utf-8"))
    status = data.get("status", "unknown")
    ts = data.get("timestamp", "?")[:19].replace("T", " ")
    cycle = data.get("cycle", 0)
    passed = data.get("total_passed", 0)
    failed = data.get("total_failed", 0)
    premium = data.get("total_premium", 0)
    uptime = data.get("uptime_seconds", 0)

    # Format uptime
    m, s = divmod(int(uptime), 60)
    h, m = divmod(m, 60)
    uptime_str = f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"

    icon = "🟢" if status == "running" else "🔴"
    print(f"{icon} Watcher: {status}")
    print(f"  Last update: {ts}")
    print(f"  Cycles: {cycle}")
    print(f"  Tasks: {passed} passed, {failed} failed")
    print(f"  Premium: {premium:.2f}x")
    print(f"  Uptime: {uptime_str}")

    if data.get("dry_run"):
        print("  Mode: DRY RUN")

    campaigns = data.get("campaigns", [])
    if campaigns:
        print(f"  Campaigns: {', '.join(campaigns)}")


def _cmd_health(config: SecretaryConfig) -> None:
    """Run a health check on the secretary system and write data/health_status.json."""
    import json as json_mod
    from datetime import datetime, timezone

    checks: list[dict] = []
    all_ok = True

    # 1. Config loadable
    checks.append({"name": "config", "ok": True, "detail": f"data_root={config.data_root}"})

    # 2. Data directory writable
    data_dir = config.data_path
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        test_file = data_dir / "_healthcheck_tmp"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        checks.append({"name": "data_dir_writable", "ok": True, "detail": str(data_dir)})
    except OSError as e:
        checks.append({"name": "data_dir_writable", "ok": False, "detail": str(e)})
        all_ok = False

    # 3. Heartbeat recent (watcher alive)
    hb_path = data_dir / "heartbeat.json"
    if hb_path.exists():
        try:
            hb = json_mod.loads(hb_path.read_text(encoding="utf-8"))
            hb_status = hb.get("status", "unknown")
            hb_ts = hb.get("timestamp", "")
            if hb_status == "running":
                checks.append({"name": "watcher", "ok": True, "detail": f"running since {hb_ts[:19]}"})
            else:
                checks.append({"name": "watcher", "ok": True, "detail": f"stopped ({hb_ts[:19]})"})
        except Exception as e:
            checks.append({"name": "watcher", "ok": False, "detail": str(e)})
            all_ok = False
    else:
        checks.append({"name": "watcher", "ok": True, "detail": "no heartbeat (not started yet)"})

    # 4. Memory file accessible
    mem_path = config.memory_path
    if mem_path.exists():
        try:
            from .memory import MemoryStore
            mem = MemoryStore.load(mem_path)
            checks.append({
                "name": "memory",
                "ok": True,
                "detail": f"{len(mem.short)} short, {len(mem.long)} long",
            })
        except Exception as e:
            checks.append({"name": "memory", "ok": False, "detail": str(e)})
            all_ok = False
    else:
        checks.append({"name": "memory", "ok": True, "detail": "no memory file (fresh)"})

    # 5. Run log accessible
    log_path = data_dir / "run_log.jsonl"
    if log_path.exists():
        try:
            from .run_log import RunLog
            rl = RunLog(log_path)
            stats = rl.summary()
            checks.append({
                "name": "run_log",
                "ok": True,
                "detail": f"{stats['total']} entries, {stats['pass_rate']} pass rate",
            })
        except Exception as e:
            checks.append({"name": "run_log", "ok": False, "detail": str(e)})
            all_ok = False
    else:
        checks.append({"name": "run_log", "ok": True, "detail": "no log yet"})

    # 6. Campaign file exists
    campaign_path = Path(config.watcher.campaign_file)
    if campaign_path.exists():
        checks.append({"name": "campaign", "ok": True, "detail": str(campaign_path)})
    else:
        checks.append({"name": "campaign", "ok": False, "detail": f"{campaign_path} not found"})
        all_ok = False

    # 7. Goal state integrity (if goals enabled)
    if config.goals.enabled:
        state_path = data_dir / "goal_state.json"
        goals_file = Path(config.goals.goals_file)
        if not goals_file.exists():
            checks.append({"name": "goals", "ok": False, "detail": f"goals file not found: {goals_file}"})
            all_ok = False
        elif state_path.exists():
            try:
                state = json_mod.loads(state_path.read_text(encoding="utf-8"))
                last_rev = state.get("last_reviewed", "never")
                n_snapshots = len(state.get("progress_snapshots", []))
                # Check for stalled goals
                from .goals import GoalStore
                gs = GoalStore(goals_file, state_path)
                gs.load()
                from .goal_progress import compute_progress
                from .run_log import RunLog as _RL
                progress = compute_progress(
                    gs.goals,
                    gs._state.get("sub_goal_status", {}),
                    _RL(data_dir / "run_log.jsonl"),
                    gs._state.get("progress_snapshots", []),
                )
                stalled = [gid for gid, gp in progress.items() if gp.stalled]
                detail = f"last_reviewed={str(last_rev)[:19]}, {n_snapshots} snapshots"
                if stalled:
                    detail += f", STALLED: {', '.join(stalled)}"
                checks.append({"name": "goals", "ok": not stalled, "detail": detail})
                if stalled:
                    all_ok = False
            except Exception as e:
                checks.append({"name": "goals", "ok": False, "detail": f"state corrupt: {e}"})
                all_ok = False
        else:
            checks.append({"name": "goals", "ok": True, "detail": "enabled, no state yet (fresh)"})
    else:
        checks.append({"name": "goals", "ok": True, "detail": "disabled"})

    # Build health status
    now = datetime.now(timezone.utc).isoformat()
    health = {
        "status": "ok" if all_ok else "degraded",
        "timestamp": now,
        "checks": checks,
    }

    # Write health_status.json
    health_path = data_dir / "health_status.json"
    health_path.write_text(json_mod.dumps(health, indent=2), encoding="utf-8")

    # Print results
    overall = "🟢 OK" if all_ok else "🟡 DEGRADED"
    print(f"Health: {overall}\n")
    for c in checks:
        icon = "✓" if c["ok"] else "✗"
        print(f"  [{icon}] {c['name']}: {c['detail']}")
    print(f"\nWritten to: {health_path}")


def _cmd_check(config: SecretaryConfig) -> None:
    """Quick dashboard: daemon alive, recent runs, self-improve state."""
    import json as json_mod
    from datetime import datetime, timezone

    data_root = config.data_path

    # ── 1. Daemon heartbeat ──
    hb_path = data_root / "heartbeat.json"
    print("=== Daemon ===")
    if hb_path.exists():
        hb = json_mod.loads(hb_path.read_text(encoding="utf-8"))
        ts_str = hb.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            age_s = (datetime.now(timezone.utc) - ts).total_seconds()
            # DO NOT CHANGE: 3600s = cycle(~15min) + sleep(30min) + buffer.
            # Self-improve has reverted this to 2400 multiple times — 2400 causes
            # false-negative "STALE" warnings. 3600 is the correct threshold.
            fresh = age_s < 3600
        except Exception:
            age_s = -1
            fresh = False
        status = hb.get("status", "unknown")
        icon = "🟢" if fresh and status == "running" else "🔴"
        cycle = hb.get("cycle", 0)
        passed = hb.get("total_passed", 0)
        failed = hb.get("total_failed", 0)
        prem = hb.get("total_premium", 0)
        cost = hb.get("total_cost_usd", 0) * config.currency.usd_to_cad_rate
        uptime = hb.get("uptime_seconds", 0)
        m, s = divmod(int(uptime), 60)
        h, m = divmod(m, 60)
        up_str = f"{h}h {m}m" if h else f"{m}m {s}s"
        if age_s >= 0:
            am, _as = divmod(int(age_s), 60)
            age_str = f"{am}m ago"
        else:
            age_str = "?"
        print(f"  {icon} {status} | cycle {cycle} | {passed}✓ {failed}✗ | {prem:.1f}x prem | ${cost:.2f} CAD | up {up_str} | heartbeat {age_str}")
    else:
        print("  🔴 No heartbeat — daemon never started")

    # ── 2. Recent runs ──
    log_path = data_root / "run_log.jsonl"
    print("\n=== Recent Runs (last 8) ===")
    if log_path.exists():
        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        for line in lines[-8:]:
            try:
                d = json_mod.loads(line)
                ts = d.get("timestamp", "")[:19].replace("T", " ")
                ok = "✓" if d.get("success") else "✗"
                src = d.get("source", "?")[:8]
                dur = d.get("duration_s", 0)
                cost = d.get("cost_usd", 0) * config.currency.usd_to_cad_rate
                task = d.get("task", "")[:55]
                # Strip scope preamble from display
                if task.startswith("SCOPE CONSTRAINTS"):
                    task = "[self-improve] " + task.split("TASK:\n", 1)[-1][:40] if "TASK:\n" in task else "[self-improve task]"
                print(f"  {ts} {ok} {src:8s} {dur:5.0f}s ${cost:5.2f} {task}")
            except Exception:
                pass
    else:
        print("  No run log found")

    # ── 3. Self-improve state ──
    gs_path = data_root / "goal_state.json"
    print("\n=== Self-Improve ===")
    if gs_path.exists():
        gs = json_mod.loads(gs_path.read_text(encoding="utf-8"))
        si = gs.get("self_improve_state", {})
        proposed = si.get("total_proposed", 0)
        executed = si.get("total_executed", 0)
        promoted = si.get("total_promoted", 0)
        discarded = si.get("total_discarded", 0)
        props = si.get("proposals", [])
        pending = sum(1 for p in props if p.get("status") == "pending")
        print(f"  proposed={proposed} executed={executed} promoted={promoted} discarded={discarded} pending={pending}")
        if pending:
            for p in props:
                if p.get("status") == "pending":
                    pid = p.get("id", p.get("proposal_id", "?"))[:16]
                    tp = p.get("task_prompt", "")[:80]
                    if tp.startswith("SCOPE"):
                        tp = tp.split("\n\n", 1)[-1][:80] if "\n\n" in tp else tp[:80]
                    print(f"    [{pid}] {tp}")
    else:
        print("  No goal state found")

    # ── 4. Stalled goals ──
    if gs_path.exists():
        sgs = gs.get("sub_goal_status", {})
        stalled = [k for k, v in sgs.items() if v.get("status") == "blocked"]
        if stalled:
            print(f"\n=== Stalled Goals ({len(stalled)}) ===")
            for sg in stalled:
                print(f"  ⚠ {sg}")


def _cmd_goals(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Show per-goal progress, strategies, and health dashboard."""
    import json as json_mod
    from .goals import GoalStore
    from .goal_progress import compute_progress
    from .run_log import RunLog as _RL
    from .strategy_library import StrategyLibrary
    from .goal_escalation import STRATEGY_NAMES
    from .goal_approval import (
        approve_all as _approve_all,
        approve_task,
        get_pending,
        reject_task,
    )

    data_dir = config.data_path
    goals_file = Path(config.goals.goals_file)
    state_file = data_dir / "goal_state.json"

    # Load goal store
    gs = GoalStore(goals_file, state_file)
    gs.load()

    if not gs.goals:
        print("No goals defined.")
        return

    # ── Approval operations (mutate state and exit) ─────────────
    if getattr(args, "approve", None):
        aid = args.approve
        if aid == "all":
            count = _approve_all(gs._state)
            gs.save_state()
            print(f"Approved {count} task(s).")
        elif approve_task(gs._state, aid):
            gs.save_state()
            print(f"Approved: {aid}")
        else:
            print(f"Not found or already decided: {aid}")
        return

    if getattr(args, "reject", None):
        rid = args.reject
        if reject_task(gs._state, rid):
            gs.save_state()
            print(f"Rejected: {rid}")
        else:
            print(f"Not found or already decided: {rid}")
        return

    if getattr(args, "pending", False):
        pending = get_pending(gs._state)
        if not pending:
            print("No tasks pending approval.")
            return
        print(f"=== Pending Approval ({len(pending)} task(s)) ===\n")
        for entry in pending:
            import datetime
            ts = entry.get("submitted", 0)
            submitted = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
            print(f"  ID: {entry['id']}")
            print(f"  Prompt: {entry['prompt'][:120]}")
            print(f"  Tier: {entry.get('tier', '?')}  Goal: {entry.get('goal_id', '?')}  Source: {entry.get('source', '?')}")
            print(f"  Submitted: {submitted}")
            print()
        print(f"Approve:  secretary goals --approve <id>  (or --approve all)")
        print(f"Reject:   secretary goals --reject <id>")
        return

    # ── Trust scores ────────────────────────────────────────────
    if getattr(args, "trust", False):
        from .goal_scheduler import (
            compute_all_trust_scores,
            evaluate_trust_graduation,
            format_graduation_history,
            format_schedule_section,
            format_trust_section,
            select_active_goals,
            suggest_policy,
        )

        rl = _RL(data_dir / "run_log.jsonl")
        rl_entries = [
            {"goal_id": e.goal_id, "success": e.success}
            for e in rl.recent(100)
            if e.source == "goals"
        ]
        trust = compute_all_trust_scores(gs.goals, gs._state, rl_entries)
        active = select_active_goals(
            gs.goals,
            curriculum_level=config.goals.curriculum_level,
            max_active=config.goals.max_active_goals,
        )

        print("=== Goal Trust & Scheduling ===\n")
        print(format_schedule_section(active, gs.goals, config.goals.curriculum_level))
        print()
        print(format_trust_section(trust))

        # Graduation recommendations
        grad_recs = evaluate_trust_graduation(
            trust,
            current_approval_mode=config.goals.approval_mode,
            current_tool_policy=config.goals.tool_policy,
        )
        if grad_recs:
            print("\n## Graduation Recommendations")
            for rec in grad_recs:
                icon = "⬆" if rec["action"] == "upgrade" else "⬇"
                print(f"  {icon} {rec['goal_id']}: {rec['reason']}")
                print(f"    → set approval_mode={rec['suggested_approval_mode']}"
                      f", tool_policy={rec['suggested_tool_policy']}")

        # Execution reports (last 3)
        reports = gs._state.get("execution_reports", [])
        if reports:
            print(f"\n## Recent Execution Reports ({len(reports)} total)")
            for rpt in reports[-3:]:
                v = rpt.get("verification", {})
                print(f"  Cycle {rpt.get('cycle', '?')} ({rpt.get('ts', '?')[:16]}): "
                      f"gen={rpt.get('tasks_generated', 0)} "
                      f"approved={rpt.get('tasks_approved', 0)} "
                      f"exec={rpt.get('tasks_executed', 0)} "
                      f"pass={v.get('pass', 0)} fail={v.get('fail', 0)}")

        # Trust trend from snapshots
        snaps = gs._state.get("trust_snapshots", [])
        if snaps:
            print(f"\n  Trust snapshots: {len(snaps)} recorded")
            latest = snaps[-1]
            print(f"  Latest: {latest.get('ts', '?')}")
            for gid, score in sorted(latest.get("scores", {}).items()):
                print(f"    {gid}: {score:.3f}")

        # Layer 23: Auto-graduation history and overrides
        print()
        print(format_graduation_history(gs._state))
        print(f"\n  auto_graduate: {'ON' if config.goals.auto_graduate else 'OFF'}")

        # Layer 24: Cross-goal meta-reflection
        from .goal_meta_reflection import format_meta_reflection_section

        print()
        print(format_meta_reflection_section(gs._state))
        return

    # Compute progress
    rl = _RL(data_dir / "run_log.jsonl")
    snapshots = gs._state.get("progress_snapshots", [])
    progress = compute_progress(
        gs.goals, gs._state.get("sub_goal_status", {}), rl, snapshots,
    )

    # Strategy library
    strat_path = data_dir / "strategies.json"
    strat_lib = StrategyLibrary(strat_path if strat_path.exists() else None)

    # Escalation state
    escalation = gs._state.get("escalation_state", {})

    # Autonomous ratio from run log
    summary = rl.summary()
    autonomous_ratio = summary.get("autonomous_ratio", 0.0)

    # ── Dry-run simulation ──────────────────────────────────────
    if getattr(args, "dry_run", False):
        from .goal_decomposition import get_next_step, get_step_plans, step_to_task
        from .goal_guardrails import apply_guardrails
        from .goals import is_review_due

        print("=== Goal Dry-Run Simulation ===\n")
        print("Simulates what the next watcher cycle would do with goals.enabled=true.")
        print("No LLM calls, no side effects.\n")

        # 1. Review due?
        review_due = is_review_due(gs, config.goals.review_interval_hours)
        last_rev = gs.last_reviewed or "never"
        print(f"  Review due: {'YES' if review_due else 'no'} (last: {last_rev}, interval: {config.goals.review_interval_hours}h)")
        if review_due:
            print(f"    → Would call run_goal_review() → up to {config.goals.max_tasks_per_review} tasks")
        print()

        # 2. Step plans that would fire
        sim_tasks: list[dict] = []
        step_plans = get_step_plans(gs._state)
        active_plans = {k: v for k, v in step_plans.items() if not v.get("completed")}
        print(f"  Active step plans: {len(active_plans)}")
        for sg_id, plan in active_plans.items():
            nxt = get_next_step(gs._state, sg_id)
            if nxt:
                goal_id = plan.get("goal_id", "")
                st = step_to_task(nxt, sg_id, goal_id)
                sim_tasks.append(st)
                print(f"    → [{sg_id}] next step: {nxt.get('action', '?')[:80]}")
                print(f"      tier={st.get('tier','?')} goal={goal_id}")
                break  # One per cycle
        if not active_plans:
            print("    (none)")
        print()

        # 3. Stalled goals that would trigger escalation
        stalled_goals = [gid for gid, gp in progress.items() if gp.stalled]
        print(f"  Stalled goals: {len(stalled_goals)}")
        for gid in stalled_goals:
            esc = escalation.get(gid, {})
            lvl = esc.get("level", 0)
            print(f"    → {gid}: escalation level {lvl} — would evaluate next escalation")
        if not stalled_goals:
            print("    (none)")
        print()

        # 4. Guardrail preview on simulated tasks
        if sim_tasks:
            gr = apply_guardrails(
                sim_tasks,
                max_tier=config.goals.max_tier,
                max_tasks_per_cycle=config.goals.max_tasks_per_cycle,
            )
            print(f"  Guardrail check: {len(gr.accepted)} accepted, {len(gr.rejected)} rejected")
            for w in gr.warnings:
                print(f"    ⚠ {w}")
        else:
            print("  Guardrail check: no tasks to validate")
        print()

        # 5. Config summary
        print("  Guardrail config:")
        print(f"    max_tier: {config.goals.max_tier}")
        print(f"    max_tasks_per_cycle: {config.goals.max_tasks_per_cycle}")
        print(f"    max_tasks_per_review: {config.goals.max_tasks_per_review}")
        print(f"    tool_policy: {config.goals.tool_policy}")
        print(f"    approval_mode: {config.goals.approval_mode}")
        print(f"    goals.enabled: {config.goals.enabled}")

        # Approval mode explanation
        _mode_desc = {
            "review": "Tasks queued for human approval before execution",
            "notify": "Tasks execute immediately, logged for post-hoc review",
            "auto": "Tasks execute silently (fully trusted)",
        }
        print(f"\n  Approval mode: {config.goals.approval_mode}")
        print(f"    → {_mode_desc.get(config.goals.approval_mode, '?')}")

        # Show pending approval queue
        pending = get_pending(gs._state)
        if pending:
            print(f"\n  Pending approval: {len(pending)} task(s)")
            for p in pending:
                print(f"    - {p['id']}: {p['prompt'][:80]}...")
        else:
            print(f"\n  Pending approval: 0 tasks")

        # Show tool policy details
        from .tool_policy import POLICY_TOOLS
        from .goal_verification import detect_completed_goals
        policy = config.goals.tool_policy
        allowed = POLICY_TOOLS.get(policy, set())
        print(f"\n  Tool policy '{policy}' allows {len(allowed)} tools:")
        for t in sorted(allowed):
            print(f"    - {t}")

        # Show verification status
        vlog = gs._state.get("verification_log", [])
        if vlog:
            recent = vlog[-5:]
            pass_ct = sum(1 for v in vlog if v.get("verdict") == "pass")
            fail_ct = sum(1 for v in vlog if v.get("verdict") == "fail")
            print(f"\n  Verification log: {len(vlog)} entries ({pass_ct} pass, {fail_ct} fail)")
            for v in recent:
                print(f"    - [{v.get('verdict', '?').upper()}] {v.get('step_id', '?')}: {v.get('reasoning', '')[:60]}")
        else:
            print(f"\n  Verification log: (none yet)")

        # Goal completion detection
        completed_goals = detect_completed_goals(gs.goals, gs._state)
        already_done = gs._state.get("completed_goals", {})
        if completed_goals:
            print(f"\n  Goals ready to complete: {', '.join(completed_goals)}")
        if already_done:
            print(f"  Completed goals: {', '.join(already_done.keys())}")

        # Layer 20: Scheduling and trust summary
        from .goal_scheduler import (
            compute_all_trust_scores,
            format_schedule_section,
            select_active_goals,
            suggest_policy,
        )
        active = select_active_goals(
            gs.goals,
            curriculum_level=config.goals.curriculum_level,
            max_active=config.goals.max_active_goals,
        )
        print(f"\n  Scheduling:")
        print(f"    curriculum_level: {config.goals.curriculum_level}")
        print(f"    max_active_goals: {config.goals.max_active_goals}")
        print(f"    Active this cycle: {len(active)} — {', '.join(g.get('id', '?') for g in active) or '(none)'}")

        _rl_entries = [
            {"goal_id": e.goal_id, "success": e.success}
            for e in rl.recent(100)
            if e.source == "goals"
        ]
        _trust = compute_all_trust_scores(gs.goals, gs._state, _rl_entries)
        for gid, td in sorted(_trust.items()):
            pol = suggest_policy(td["trust_score"])
            samp = sum(td["sample_sizes"].values())
            print(f"    {gid}: trust={td['trust_score']:.2f} ({pol['level']}, {samp} samples)")
        return

    # JSON output
    if args.json_out:
        from dataclasses import asdict
        data = {
            "goals": [],
            "strategies": {"total": strat_lib.size, "categories": strat_lib.categories()},
            "autonomous_ratio": autonomous_ratio,
            "by_source": summary.get("by_source", {}),
        }
        for goal in gs.goals:
            gid = goal.get("id", "")
            gp = progress.get(gid)
            esc = escalation.get(gid, {})
            data["goals"].append({
                "id": gid,
                "title": goal.get("title", gid),
                "progress": asdict(gp) if gp else None,
                "escalation_level": esc.get("level", 0),
                "escalation_strategy": STRATEGY_NAMES.get(esc.get("level", 0), "none"),
            })
        print(json_mod.dumps(data, indent=2))
        return

    # Formatted text output
    print("=== Goal Actualization Dashboard ===\n")

    for goal in gs.goals:
        gid = goal.get("id", "")
        title = goal.get("title", gid)
        gp = progress.get(gid)
        esc = escalation.get(gid, {})

        if gp:
            pct = int(gp.completion * 100)
            bar_filled = pct // 5
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            stall_flag = " ⚠ STALLED" if gp.stalled else ""
            sr = f"{int(gp.success_rate * 100)}%" if gp.success_rate >= 0 else "n/a"
            vel = f"{gp.velocity:+.1%}" if gp.velocity != 0 else "0"

            print(f"  {title}")
            print(f"    [{bar}] {pct}%  ({gp.done_sub_goals}/{gp.total_sub_goals} sub-goals){stall_flag}")
            print(f"    Tasks: {gp.total_tasks}  Pass rate: {sr}  Velocity: {vel}")
        else:
            print(f"  {title}")
            print(f"    (no progress data)")

        esc_level = esc.get("level", 0)
        if esc_level > 0:
            esc_name = STRATEGY_NAMES.get(esc_level, "unknown")
            print(f"    Escalation: level {esc_level} ({esc_name})")

        print()

    # Strategy summary
    cats = strat_lib.categories()
    if cats:
        cat_str = ", ".join(f"{k}: {v}" for k, v in sorted(cats.items()))
        print(f"  Strategies: {strat_lib.size} total ({cat_str})")
    else:
        print(f"  Strategies: {strat_lib.size} total")

    # Autonomous ratio
    print(f"  Autonomous ratio: {autonomous_ratio:.0%}")

    # Source breakdown
    by_source = summary.get("by_source", {})
    if by_source:
        parts = []
        for src, counts in sorted(by_source.items()):
            parts.append(f"{src}: {counts['total']} ({counts['passed']} passed)")
        print(f"  Task sources: {', '.join(parts)}")


def _cmd_memory(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """View or manage the memory store."""
    from .memory import MemoryStore

    memory = MemoryStore.load(config.memory_path)
    action = args.action

    if action == "show":
        print(f"=== Memory Store ({config.memory_path}) ===\n")
        print(f"Short-term ({len(memory.short)}/{memory.short_max}):")
        if memory.short:
            for i, entry in enumerate(memory.short, 1):
                print(f"  {i}. {entry[:100]}")
        else:
            print("  (empty)")
        print(f"\nLong-term ({len(memory.long)}/{memory.long_max}):")
        if memory.long:
            for i, entry in enumerate(memory.long, 1):
                print(f"  {i}. {entry[:100]}")
        else:
            print("  (empty)")
    elif action == "clear-short":
        memory.short.clear()
        memory.save()
        print("Short-term memory cleared.")
    elif action == "clear-long":
        memory._long_entries.clear()
        memory.save()
        print("Long-term memory cleared.")
    elif action == "clear-all":
        memory.short.clear()
        memory._long_entries.clear()
        memory.save()
        print("All memory cleared.")


def _cmd_logs(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Search and filter run logs."""
    import json as json_mod
    from .run_log import RunLog

    log = RunLog(config.data_path / "run_log.jsonl")
    entries = log.recent(args.last)

    if not entries:
        print("No run logs found.")
        return

    # Apply filters
    if args.tier:
        entries = [e for e in entries if e.tier == args.tier]
    if args.failed:
        entries = [e for e in entries if not e.success]
    if args.search:
        q = args.search.lower()
        entries = [e for e in entries if q in e.task.lower()]

    if not entries:
        print("No matching entries.")
        return

    if args.json_output:
        from dataclasses import asdict
        print(json_mod.dumps([asdict(e) for e in entries], indent=2))
        return

    # Table output
    from .currency import usd_to_cad
    print(f"{'Time':<20} {'Tier':<8} {'Status':<6} {'Premium':>7} {'Cost (CAD)':>10} {'Task'}")
    print("-" * 95)
    for e in entries:
        ts = e.timestamp[:19].replace("T", " ")
        status = "PASS" if e.success else "FAIL"
        cost_str = f"${usd_to_cad(e.cost_usd):.4f}" if e.cost_usd else "\u2014"
        print(f"{ts:<20} {e.tier:<8} {status:<6} {e.premium_cost:>6.2f}x {cost_str:>10} {e.task[:40]}")

    print(f"\n{len(entries)} entries shown")


def _cmd_config(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Show configuration — full dump or a specific key via dot-notation."""
    key = args.key

    if key is None:
        # Full config dump
        data = config.model_dump()
        _print_config_tree(data)
        return

    # Traverse dot-notation: "routing.default_tier" → config.routing.default_tier
    parts = key.split(".")
    obj: Any = config.model_dump()
    for part in parts:
        if isinstance(obj, dict) and part in obj:
            obj = obj[part]
        else:
            print(f"Unknown config key: {key}", file=sys.stderr)
            sys.exit(1)

    if isinstance(obj, dict):
        _print_config_tree(obj, indent=0)
    else:
        print(obj)


def _print_config_tree(data: dict, indent: int = 0) -> None:
    """Pretty-print a config dict as a readable tree."""
    prefix = "  " * indent
    for k, v in data.items():
        if isinstance(v, dict):
            print(f"{prefix}{k}:")
            _print_config_tree(v, indent + 1)
        elif isinstance(v, list):
            print(f"{prefix}{k}: {v}")
        else:
            print(f"{prefix}{k}: {v}")


def _cmd_estimate(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Preview routing for a task without executing."""
    from .router import estimate_complexity, select_model, get_premium_cost

    task = " ".join(args.task)
    level, score, reason, confidence = estimate_complexity(task)
    routing = select_model(config, task, args.tier)
    cost = get_premium_cost(routing.model)

    print(f"Task: {task}")
    print(f"Complexity: {level} (score={score}, confidence={confidence})")
    print(f"Reason: {reason}")
    print(f"Model: {routing.model} ({routing.tier})")
    print(f"Premium cost: {cost}x")
    print(f"Max turns: {routing.max_turns}")
    if routing.max_budget_usd > 0:
        print(f"Budget cap: ${routing.max_budget_usd} USD")


def _cmd_export(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Export run history to CSV or JSON."""
    import csv
    import io
    import json as json_mod
    from dataclasses import asdict
    from .run_log import RunLog

    log = RunLog(config.data_path / "run_log.jsonl")
    n = args.last if args.last > 0 else 10000
    entries = log.recent(n)

    if not entries:
        print("No run logs to export.")
        return

    if args.format == "json":
        content = json_mod.dumps([asdict(e) for e in entries], indent=2)
    else:
        buf = io.StringIO()
        fields = ["timestamp", "cycle", "task", "tier", "model", "success",
                   "duration_s", "premium_cost", "cost_usd", "num_turns", "error"]
        writer = csv.DictWriter(buf, fieldnames=fields)
        writer.writeheader()
        for e in entries:
            row = asdict(e)
            row = {k: row[k] for k in fields}
            writer.writerow(row)
        content = buf.getvalue()

    if args.output:
        Path(args.output).write_text(content, encoding="utf-8")
        print(f"Exported {len(entries)} entries to {args.output}")
    else:
        print(content)


def _cmd_audit(config: SecretaryConfig) -> None:
    """Analyze run history for cost optimization opportunities."""
    from .run_log import RunLog

    log = RunLog(config.data_path / "run_log.jsonl")
    report = log.audit()

    print("═══ Premium Request Audit ═══\n")

    # Section A: Potential Downgrades
    print("── Potential Downgrades ──")
    print("Tasks that ran at higher tiers but produced simple output:\n")
    downgrades = report["downgrades"]
    if downgrades:
        print(f"  {'Task':<50} {'Tier':<8} {'Premium':<10} {'Action'}")
        print(f"  {'─' * 50} {'─' * 8} {'─' * 10} {'─' * 40}")
        for d in downgrades:
            print(f"  {d['task']:<50} {d['tier']:<8} {d['premium']:<10.2f} {d['action']}")
    else:
        print("  None found — all tasks appear appropriately routed.")
    print()

    # Section B: Costliest Tasks
    print("── Costliest Tasks (by cumulative premium) ──\n")
    top = report["top_tasks"]
    if top:
        print(f"  {'Task':<50} {'Premium':<10} {'Runs':<6} {'Action'}")
        print(f"  {'─' * 50} {'─' * 10} {'─' * 6} {'─' * 30}")
        for t in top:
            print(f"  {t['task']:<50} {t['total_premium']:<10.2f} {t['runs']:<6} {t['action']}")
    else:
        print("  None found — no run history.")
    print()

    # Section C: Worst Cycle
    print("── Worst Watcher Cycle ──\n")
    worst = report["worst_cycle"]
    if worst:
        print(f"  Cycle {worst['cycle']}: {worst['passed']}/{worst['total']} passed "
              f"({worst['pass_rate']}), {worst['premium_spent']}x premium")
        print(f"  Action: {worst['action']}")
    else:
        print("  No watcher cycles in history.")
    print()


def _cmd_analyze(config: SecretaryConfig) -> None:
    """Deep campaign analysis — reliability, failure patterns, and suggestions."""
    from .run_log import RunLog
    from .currency import format_cost

    log = RunLog(config.data_path / "run_log.jsonl")
    report = log.analyze()

    print("═══ Campaign Analysis ═══\n")

    # Section A: Task Reliability
    print("── Task Reliability ──\n")
    tasks = report["task_reliability"]
    if tasks:
        print(f"  {'Task':<50} {'Runs':>5} {'Pass%':>6} {'Avg(s)':>7} {'Cost':>10}")
        print(f"  {'─' * 50} {'─' * 5} {'─' * 6} {'─' * 7} {'─' * 10}")
        for t in tasks:
            cost_str = format_cost(t["total_cost_usd"]).split(" ")[0] if t["total_cost_usd"] else "$0"
            print(
                f"  {t['task']:<50} {t['total_runs']:>5} "
                f"{t['pass_rate']:>5.0%} {t['avg_duration_s']:>7.1f} {cost_str:>10}"
            )
    else:
        print("  No task data available.")
    print()

    # Section B: Failure Patterns
    print("── Failure Patterns ──\n")
    patterns = report["failure_patterns"]
    if patterns:
        for fp in patterns[:5]:
            print(f"  [{fp['count']}x] {fp['pattern']}")
    else:
        print("  No failures recorded.")
    print()

    # Section C: Performance by Hour (UTC)
    hour_perf = report["hour_performance"]
    if hour_perf:
        print("── Performance by Hour (UTC) ──\n")
        print(f"  {'Hour':>4}  {'Runs':>5}  {'Pass%':>6}  {'Bar'}")
        print(f"  {'─' * 4}  {'─' * 5}  {'─' * 6}  {'─' * 20}")
        for h, s in hour_perf.items():
            bar = "█" * int(s["pass_rate"] * 20)
            print(f"  {h:>4}  {s['total']:>5}  {s['pass_rate']:>5.0%}  {bar}")
        print()

    # Section D: Cycle Trend
    trend = report["cycle_trend"]
    if trend:
        print("── Cycle Trend (last 10) ──\n")
        for d in trend[-10:]:
            total = d["passed"] + d["failed"]
            rate = d["passed"] / max(total, 1)
            bar = "█" * int(rate * 10)
            print(f"  Cycle {d['cycle']:>3}: {d['passed']}/{total} ({rate:.0%}) {bar}")
        print()

    # Section E: Suggestions
    suggestions = report["suggestions"]
    print("── Suggestions ──\n")
    if suggestions:
        for i, s in enumerate(suggestions, 1):
            print(f"  {i}. {s}")
    else:
        print("  No issues detected — campaign is running well.")
    print()


def _cmd_budget(config: SecretaryConfig) -> None:
    """Show current daily/weekly spend vs configured budget limits."""
    from .cost_monitor import CostMonitor, CostMonitorConfig
    from .currency import format_cost

    budget_enabled = config.watcher.budget_daily_usd > 0 or config.watcher.budget_weekly_usd > 0
    monitor = CostMonitor(
        CostMonitorConfig(
            enabled=budget_enabled,
            daily_limit_usd=config.watcher.budget_daily_usd,
            weekly_limit_usd=config.watcher.budget_weekly_usd,
            alert_threshold_pct=config.watcher.budget_alert_pct,
            log_path=str(config.data_path / "cost_alerts.jsonl"),
        ),
        run_log_path=config.data_path / "run_log.jsonl",
    )

    summary = monitor.get_spend_summary()
    print("═══ Budget Monitor ═══\n")

    if not budget_enabled:
        print("  No budget limits configured.")
        print(f"  Today's spend: {format_cost(summary['daily_usd'])}")
        print(f"  This week:     {format_cost(summary['weekly_usd'])}")
        print(f"\n  Set limits in config.yaml: watcher.budget_daily_usd / budget_weekly_usd")
        return

    # Daily
    if config.watcher.budget_daily_usd > 0:
        bar = _budget_bar(summary["daily_pct"])
        print(f"  Daily:  {format_cost(summary['daily_usd'])} / {format_cost(config.watcher.budget_daily_usd)}")
        print(f"          {bar} {summary['daily_pct']}%")

    # Weekly
    if config.watcher.budget_weekly_usd > 0:
        bar = _budget_bar(summary["weekly_pct"])
        print(f"  Weekly: {format_cost(summary['weekly_usd'])} / {format_cost(config.watcher.budget_weekly_usd)}")
        print(f"          {bar} {summary['weekly_pct']}%")

    print(f"\n  Alert threshold: {config.watcher.budget_alert_pct}%")
    if summary["exhausted"]:
        print("  ⚠️  BUDGET EXHAUSTED — watcher will pause until next period")
    print()


def _budget_bar(pct: int, width: int = 20) -> str:
    """Render a simple progress bar."""
    filled = min(pct, 100) * width // 100
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]"


def _cmd_forecast(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Predict future costs based on recent run history."""
    from .run_log import RunLog
    from .currency import format_cost

    log = RunLog(config.data_path / "run_log.jsonl")
    days = args.days
    fc = log.forecast(days)

    print(f"═══ Cost Forecast ({days} days) ═══\n")

    if fc["confidence"] == "none":
        print("  No run history — nothing to forecast.")
        return

    print(f"  Based on {fc['data_days']} day(s) of history (confidence: {fc['confidence']})\n")
    print(f"  Daily rate:     {format_cost(fc['daily_rate_usd'])}  ({fc['daily_rate_premium']:.2f}x premium)")
    print(f"  {days}-day projected: {format_cost(fc['projected_usd'])}  ({fc['projected_premium']:.2f}x premium)")

    if fc["confidence"] == "low":
        print(f"\n  ⚠ Low confidence — less than 3 days of data. Forecast may be inaccurate.")
    print()


def _cmd_mode(args: argparse.Namespace, config_path: str) -> None:
    """Toggle between proxy mode and direct API mode.

    Proxy mode:  local proxy + agent_prefix=true
    Direct mode: direct API + agent_prefix=false → normal GitHub premium billing
    """
    import yaml

    p = Path(config_path)
    if not p.exists():
        print(f"Config not found: {p}")
        sys.exit(1)

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # Proxy mode settings
    FREE = {
        "anthropic_base_url": "${ANTHROPIC_BASE_URL:-http://localhost:4141}",
        "agent_prefix": True,
        "watcher": {"max_premium_per_cycle": 0},
    }
    # Paid mode settings — direct Anthropic API, prefix OFF, premium capped
    PAID = {
        "anthropic_base_url": "https://api.anthropic.com",
        "agent_prefix": False,
        "watcher": {"max_premium_per_cycle": 10},
    }

    current_url = raw.get("anthropic_base_url", "")
    is_free = "localhost" in current_url or "4141" in current_url
    prefix_on = raw.get("agent_prefix", True)

    if args.billing is None:
        # Show current mode with clear status
        mode = "PROXY (local proxy)" if is_free else "PAID (direct API)"
        print(f"Current mode: {mode}")
        print(f"  base_url:      {current_url}")
        print(f"  agent_prefix:  {'ON' if prefix_on else 'OFF'}")
        premium = raw.get("watcher", {}).get("max_premium_per_cycle", "?")
        print(f"  premium cap:   {premium}")
        print(f"\nSwitch: secretary mode free | secretary mode paid")
        return

    target = FREE if args.billing == "free" else PAID

    raw["anthropic_base_url"] = target["anthropic_base_url"]
    raw["agent_prefix"] = target["agent_prefix"]
    raw.setdefault("watcher", {})
    raw["watcher"]["max_premium_per_cycle"] = target["watcher"]["max_premium_per_cycle"]

    p.write_text(yaml.dump(raw, default_flow_style=False, sort_keys=False), encoding="utf-8")

    if args.billing == "free":
        print("Switched to PROXY mode")
        print("  agent_prefix: ON")
        print("  base_url: localhost:4141 (copilot-api proxy)")
        print("  premium cap: unlimited")
        print("  Requires: npx copilot-api@latest start")
    else:
        print("Switched to DIRECT mode")
        print("  agent_prefix: OFF → normal premium billing")
        print("  base_url: api.anthropic.com (direct)")
        print("  premium cap: 10x per cycle (safety)")
        print("  Requires: ANTHROPIC_API_KEY env var")
    print(f"  Config updated: {p}")


def _cmd_metrics(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """View multi-instance metrics and benchmark comparisons."""
    from .metrics import MetricsCollector
    import json as json_mod

    mc = MetricsCollector(config.metrics_path)
    action = args.action

    if action == "show":
        report = mc.format_instance_report()
        print(report)

    elif action == "instances":
        from .coordinator import Coordinator
        if not config.instance_id:
            print("No instance ID set. Use --instance <id> with your watcher.")
            return
        coord = Coordinator(
            shared_dir=config.shared_data_path,
            instance_id=config.instance_id,
        )
        active = coord.get_active_instances()
        if not active:
            print("No active instances found.")
            return
        print(f"Active instances ({len(active)}):\n")
        for inst in active:
            role = inst.get("role", "") or "generalist"
            pid = inst.get("pid", "?")
            last = inst.get("last_seen", "?")[:19].replace("T", " ")
            tc = inst.get("tasks_completed", 0)
            tf = inst.get("tasks_failed", 0)
            print(f"  {inst.get('instance_id', '?')} (role={role}, pid={pid})")
            print(f"    Last seen: {last} | tasks: {tc} passed, {tf} failed")

    elif action == "benchmarks":
        benchmarks = mc.get_benchmarks()
        if not benchmarks:
            print("No benchmarks recorded yet.")
            return
        print(f"Benchmark history ({len(benchmarks)} comparisons):\n")
        for b in benchmarks:
            ts = b.timestamp[:19].replace("T", " ") if b.timestamp else "?"
            print(f"  [{ts}] {b.name}: winner={b.winner}, improvement={b.improvement_pct:+.1f}%")
            print(f"    {b.summary}")
            print()


def _cmd_campaign(args: argparse.Namespace, config: SecretaryConfig) -> None:
    """Validate and preview campaign file(s) — show tasks, routing, and cost estimates."""
    from .campaign import validate_campaign
    from .router import select_model, get_premium_cost
    import yaml

    raw = args.file or config.watcher.campaign_file
    files = [Path(f.strip()) for f in raw.split(",")]

    # Validate each campaign file first
    has_errors = False
    for cf in files:
        vr = validate_campaign(cf)
        if vr.warnings:
            for w in vr.warnings:
                print(f"  ⚠ {w}")
        if not vr.valid:
            for e in vr.errors:
                print(f"  ✗ {e}", file=sys.stderr)
            has_errors = True
    if has_errors:
        sys.exit(1)

    tasks: list[dict] = []
    for cf in files:
        data = yaml.safe_load(cf.read_text(encoding="utf-8"))
        tasks.extend(data.get("tasks", []))

    names = ", ".join(str(f) for f in files)
    print(f"═══ Campaign: {names} ═══\n")
    print(f"Tasks: {len(tasks)}")

    total_cost = 0.0
    for i, task_def in enumerate(tasks, 1):
        prompt = task_def.get("prompt", task_def.get("task", ""))
        tier = task_def.get("tier", None)
        schedule = task_def.get("schedule", "")
        if not prompt:
            print(f"  {i}. ⚠ Empty prompt — will be skipped")
            continue

        routing = select_model(config, prompt, tier)
        cost = get_premium_cost(routing.model)
        total_cost += cost
        sched_str = f" ⏰{schedule}" if schedule else ""
        print(f"  {i}. [{routing.tier}] {routing.model} ({cost}x){sched_str} — {prompt.strip()[:70]}")

    print(f"\nTotal premium per cycle: {total_cost:.2f}x")
    budget = config.watcher.max_premium_per_cycle
    if budget > 0:
        if total_cost <= budget:
            print(f"Budget: {budget}x — ✓ all tasks fit within budget")
        else:
            print(f"Budget: {budget}x — ⚠ some tasks will be skipped ({total_cost:.2f}x > {budget}x)")
    else:
        print("Budget: unlimited")

    # Daily/monthly cost projection
    interval = config.watcher.interval_minutes
    cycles_per_day = (24 * 60) / interval if interval > 0 else 1
    effective_cost = min(total_cost, budget) if budget > 0 else total_cost
    daily_premium = effective_cost * cycles_per_day
    monthly_premium = daily_premium * 30
    print(f"\nProjected usage (every {interval}min):")
    print(f"  Daily:   {daily_premium:.1f}x premium ({cycles_per_day:.0f} cycles)")
    print(f"  Monthly: {monthly_premium:.1f}x premium")


def main() -> None:
    """CLI entry point — parse args, load config, and dispatch to subcommand handler."""
    # Ensure Unicode output works on Windows (cp1252 can't encode ✓/→/— etc.)
    import sys
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    parser = _build_parser()
    args = parser.parse_args()
    _setup_logging(args.verbose)

    config = SecretaryConfig.load(args.config)
    config_path = args.config

    # Initialize currency conversion rate from config
    from .currency import set_rate
    set_rate(config.currency.usd_to_cad_rate)

    if args.command == "auth":
        _cmd_auth(config)
    elif args.command == "version":
        _cmd_version()
    elif args.command == "status":
        _cmd_status(config)
    elif args.command == "test":
        _cmd_test(config)
    elif args.command == "history":
        _cmd_history(args, config)
    elif args.command == "campaign":
        _cmd_campaign(args, config)
    elif args.command == "config":
        _cmd_config(args, config)
    elif args.command == "logs":
        _cmd_logs(args, config)
    elif args.command == "memory":
        _cmd_memory(args, config)
    elif args.command == "heartbeat":
        _cmd_heartbeat(config)
    elif args.command == "health":
        _cmd_health(config)
    elif args.command == "check":
        _cmd_check(config)
    elif args.command == "goals":
        _cmd_goals(args, config)
    elif args.command == "export":
        _cmd_export(args, config)
    elif args.command == "audit":
        _cmd_audit(config)
    elif args.command == "analyze":
        _cmd_analyze(config)
    elif args.command == "budget":
        _cmd_budget(config)
    elif args.command == "forecast":
        _cmd_forecast(args, config)
    elif args.command == "estimate":
        _cmd_estimate(args, config)
    elif args.command == "mode":
        _cmd_mode(args, config_path)
    elif args.command == "metrics":
        _cmd_metrics(args, config)
    elif args.command == "run":
        asyncio.run(_cmd_run(args, config))
    elif args.command == "chat":
        asyncio.run(_cmd_chat(args, config))
    elif args.command == "watch":
        asyncio.run(_cmd_watch(args, config))
    elif args.command == "improve":
        asyncio.run(_cmd_improve(args, config))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
