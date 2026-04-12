# Claude Secretary

**An autonomous AI agent that wakes itself up, decides what needs doing, and acts — 24/7, without human intervention.**

Powered by Claude Agent SDK through Copilot Pro billing. The agent runs as a persistent daemon, executing scheduled campaigns (email triage, calendar management, research, self-improvement) and making decisions about what to do next. Human interaction via CLI is supported but secondary — the core design is unsupervised autonomous operation.

## Core Loop

```
┌─────────────────────────────────────────────────┐
│                 DAEMON (secretary watch)          │
│                                                   │
│   Wake up on schedule                             │
│   ├── Load campaign tasks                         │
│   ├── For each task:                              │
│   │   ├── Route to appropriate model tier         │
│   │   ├── Inject memory context                   │
│   │   ├── Execute via Claude SDK                  │
│   │   ├── Use MCP tools (Gmail, Calendar)         │
│   │   └── Store results + update memory           │
│   ├── Log outcomes                                │
│   └── Sleep until next cycle                      │
│                                                   │
│   Self-improvement (sandbox → test → promote)     │
│   can modify the agent's own code between runs    │
└─────────────────────────────────────────────────┘
```

**Human interaction** (CLI one-shot, chat) is for ad-hoc tasks and debugging — not the primary use case.

## Architecture

```
Daemon (watcher) → Campaign YAML → Router → Claude Agent SDK → copilot-api → Copilot Pro
                                              ↕
                                    MCP Tools (Gmail, Calendar)
                                    Memory (JSON, fuzzy dedup)
```

## Model Routing (Copilot Pro Economics)

| Tier | Model | Copilot Multiplier | Use For |
|------|-------|-------------------|---------|
| low | claude-haiku-4.5 | 0.33x | Simple lookups, formatting, quick Q&A |
| medium | claude-sonnet-4.6 | 1x | Code, email, research, analysis |
| high | claude-opus-4.6 | 3x | Architecture, multi-file, deep analysis |

Tasks are automatically routed based on complexity scoring (keywords, length, steps). Override with `--tier`.

## Setup

```bash
# 1. Install
cd claude-secretary
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[all]"

# 2. Start copilot-api proxy (keeps running)
npx copilot-api@latest start

# 3. Copy config
copy config.example.yaml config.yaml

# 4. Set up Google OAuth (optional — for Gmail/Calendar)
secretary auth
```

## Usage

```bash
# One-shot task (auto-routes to best model)
secretary run "Check my unread emails from today"

# Force a specific tier
secretary run --tier low "What day is it?"
secretary run --tier high "Redesign the auth system for multi-tenancy"

# Interactive chat
secretary chat

# 24/7 autonomous watcher
secretary watch
secretary watch --interval 15 --campaign my-tasks.yaml
secretary watch --dry-run --max-premium 3  # preview mode, capped budget
secretary watch --max-runs 1               # single test cycle

# Run history + cost tracking
secretary history
secretary history --last 20 --tier low
secretary history --failed                 # show only failed runs
secretary history --search "email"           # filter by keyword

# Self-improvement on a project
secretary improve "Add comprehensive error handling" --project ./my-project
secretary improve "Optimize the database queries" --project ./my-project --promote

# Status check
secretary status
```

## Components

| Module | Purpose |
|--------|---------|
| `agent.py` | Core execution engine — SDK wrapper, memory injection, MCP registration |
| `tools.py` | Gmail & Calendar as in-process SDK MCP tools (no subprocess) |
| `router.py` | Complexity scoring → model tier selection |
| `memory.py` | JSON-backed short/long memory with fuzzy dedup and adaptive decay |
| `watcher.py` | **The daemon** — 24/7 campaign loop with budget cap, retry+backoff, dedup, schedule, logging |
| `run_log.py` | JSONL persistence of all task outcomes with premium cost tracking |
| `history.py` | Run history query, stats, and formatted output |
| `self_improve.py` | Sandbox → agent → test → promote pipeline with backup/rollback |
| `campaign.py` | Campaign YAML validation, schedule parsing, dependency resolution |
| `config.py` | Pydantic config with YAML + env var support |
| `currency.py` | USD ↔ CAD conversion with configurable rate |
| `__main__.py` | CLI: 17 subcommands (run, chat, watch, improve, auth, status, history, campaign, test, config, logs, memory, heartbeat, estimate, export, audit, version) |
| `mcp_tools/google_auth.py` | OAuth2 credential management |

### How the daemon works (watcher.py)

```
secretary watch [--interval 15] [--campaign tasks.yaml] [--dry-run] [--max-premium 3] [--max-runs 5]

1. Load campaign.yaml → list of tasks with prompts and optional tier overrides
2. For each task:
   a. Dedup by prompt hash — skip if already run this cycle
   b. Calculate premium cost from model tier (haiku=0.33x, sonnet=1x, opus=3x)
   c. Check against premium budget — skip if cycle budget exhausted
   d. Route to model tier based on complexity scoring (or forced tier)
   e. Inject long-term and short-term memory into system prompt
   f. Execute via Claude Agent SDK with Gmail/Calendar MCP tools available
   g. On failure: retry up to max_retries times with exponential backoff
   h. Log result to data/run_log.jsonl (timestamp, task, tier, success, cost, output)
   i. Update short-term memory with task summary
3. Consolidate memory — promote recurring patterns to long-term
4. Sleep for interval (doubles on failure for backoff)
5. Repeat forever (or until max_runs / Ctrl+C)

Flags:
  --dry-run        Show what would run without calling the API
  --max-premium N  Cap premium spend per cycle (e.g. 3.0 = three 1x requests)
  --max-runs N     Stop after N cycles
```

### How memory works (memory.py)

- **Short-term** (20 max): Recent task summaries, auto-trimmed FIFO
- **Long-term** (50 max): Persistent learnings, fuzzy-deduplicated (0.85 similarity threshold)
- **Consolidation**: After each watcher cycle, recurring task patterns (3+ similar) get promoted to long-term memory
- Memory is injected into every Claude SDK call's system prompt

### How self-improvement works (self_improve.py)

```
secretary improve "Add better error messages" --project . [--promote]

1. Copy project to sandbox (excludes .git, .venv, __pycache__)
2. Run Claude agent in sandbox with improvement task
3. Run pytest in sandbox — verify no regressions
4. If tests pass + --promote: copy changes back to source
5. If tests fail: report what broke, discard sandbox
```

### How run logging works (run_log.py)

All task executions (one-shot and watcher) are logged to `data/run_log.jsonl`:
```json
{"timestamp": "2026-03-11T...", "cycle": 3, "task": "Check Gmail...",
 "tier": "low", "model": "claude-haiku-4.5", "success": true,
 "output_preview": "Found 2 messages...", "duration_s": 4.2, "premium_cost": 0.33}
```

The agent can review its own run history to identify patterns, recurring failures, and opportunities for self-improvement.

## MCP Tools

Gmail and Calendar tools run **in-process** via the Claude Agent SDK's `create_sdk_mcp_server()` — no subprocess management needed. They auto-activate when Google OAuth is configured.

**Gmail tools** (6): `gmail_search`, `gmail_read`, `gmail_draft`, `gmail_send`, `gmail_list_drafts`, `gmail_get_draft`
**Calendar tools** (4): `calendar_today`, `calendar_list`, `calendar_search`, `calendar_create`

## Testing

```powershell
# IMPORTANT: Close VS Code before running tests!
# Tests crash VS Code due to an Electron 39 bug (network service crash + no recovery).
# This workaround is needed until VS Code ships Electron 42 (~July 2026, VS Code 1.117+).
# See: agents-home/projects/vscode-stability/README.md for full details.
# Track: https://github.com/electron/electron/issues/49572

# Option 1: Use the wrapper script (closes VS Code, runs tests, reopens it)
.\run-tests.ps1

# Option 2: Close VS Code manually, then run tests
python -m pytest tests/ -v

# Tests cover: agent, config, memory, routing, self-improvement, run logging,
# tools, watcher, campaign, currency, CLI, history (13 test files)
```

## Project Origin

Successor to Captain v2. Captain v2 built a custom LLM agent loop (2000+ lines in agent.py, 891 tests, 102 CLI commands). This project replaces that with the Claude SDK — same capabilities in ~1/10th the code. The SDK provides file editing, web search, terminal, sub-agents, and MCP natively.
