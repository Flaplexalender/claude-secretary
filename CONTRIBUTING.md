# Contributing to Claude Secretary

## Quick Setup

```bash
git clone https://github.com/Flaplexalender/claude-secretary.git
cd claude-secretary
python -m venv .venv

# Windows
.venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate

pip install -e ".[all]"
```

## Personal Config

Copy and fill in `workspace/USER.md` with your own details — this file is gitignored and never committed. The secretary uses it for personalized task execution.

## Pre-Commit Hook (Required)

Install the PII-scanning pre-commit hook before making any commits:

```bash
# Windows
copy hooks\pre-commit .git\hooks\pre-commit

# Linux/macOS
cp hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

This scans staged diffs against `.pii-patterns` and blocks commits containing personal information. Bypass with `git commit --no-verify` only if you're certain the match is a false positive.

## Running Tests

**Important:** Do NOT run `pytest` inside VS Code's integrated terminal — it crashes due to an Electron bug. Use one of these approaches:

1. **Push to GitHub** — CI runs automatically via GitHub Actions (~1m40s)
2. **Local (PowerShell)** — `.\run-tests-safe.ps1` (closes VS Code first, runs tests, reopens)
3. **Local with filter** — `.\run-tests-safe.ps1 -Filter "test_planner"`
4. **External terminal** — Open a standalone terminal (not VS Code) and run `pytest` directly

## Project Structure

```
src/secretary/
├── direct_agent.py    # Core agent execution (Anthropic + OpenAI paths)
├── router.py          # Model tier routing (haiku/sonnet/opus)
├── planner.py         # Haiku-as-planner (cheap model plans, expensive executes)
├── config.py          # Pydantic config models
├── watcher.py         # 24/7 daemon loop
├── campaign.py        # Campaign YAML loading
├── memory.py          # JSON-backed memory with fuzzy dedup
├── task_executor.py   # Task execution orchestration
├── self_improve.py    # Sandbox → test → promote pipeline
├── goals.py           # Goal system
├── mcp_tools/         # Gmail + Calendar MCP tool servers
└── ...                # ~50 modules total
```

## Making Changes

1. Create a branch from `master`
2. Make your changes
3. Run tests (see above) — all must pass
4. Ensure the pre-commit hook passes (no PII in diffs)
5. Open a PR against `master`

## Code Style

- Python 3.11+, type hints encouraged
- Pydantic for config/data models
- `pytest` + `pytest-asyncio` for tests
- Keep tool descriptions short (2-3 words) to minimize token usage
- Graceful error handling — the watcher must never crash

## Key Constraints

- **Copilot Pro billing**: The agent runs through `copilot-api` proxy. Model tiers map to billing multipliers (Haiku 0.33x, Sonnet 1x, Opus 3x). Cost efficiency matters.
- **No secrets in code**: Tokens, credentials, and personal data stay in `data/`, `config.yaml`, and `workspace/USER.md` — all gitignored.
- **Self-improvement safety**: The agent can modify its own code. Changes go through sandbox → test → promote. Never skip the test step.
