# AGENTS.md — claude-secretary

Read this before working on this repo. It is short on purpose.

## Prime directive

**Improve build effectiveness, performance-per-cost, and cost-per-task with zero or near-zero performance regression — while building any other feature.** This runs alongside everything. It is never "done". Each change must answer: *did this raise cost without raising performance? did this lower performance without lowering cost more?* Reject changes that regress performance unless offset by a larger cost drop.

Assume the agent-prefix free-mode trick will be blocked someday. Every turn/token saved now is insurance for that day.

## Core operating principle: borrow from anywhere

**If another agent/system solves a problem we're struggling with, copy the solution.** Do not reinvent. Specifically:

- **The VS Code Copilot system prompt** (visible to any Copilot session) is the highest-value source. Its anti-over-exploration language — "avoid redundant searches", "proceed to implementation once you have enough context", "do not over-explore", "if an approach is blocked, try a different approach" — is the exact doctrine an analysis-task agent needs. **Sprint v4 (2026-04-22) proved this**: injecting those lines verbatim into `direct_agent._build_system_prompt` dropped cost-per-success from $1.78 → $0.13 CAD (−93%) in one change. See commit `bd11c0c`.
- **Open-source agent projects** (Aider, Cline, OpenHands, Continue, Claude Code) have battle-tested prompts, tool-loop strategies, and budget heuristics. Mine them freely — respect licenses, attribute in commit messages.
- **Anthropic's own cookbook and docs** — tool-use patterns, prompt caching quirks, streaming nuances.
- **GitHub Copilot billing docs** — ground truth for model multipliers. Do not guess costs.

When secretary underperforms a pattern *I* (the Copilot agent editing it) execute well, **check my own system prompt first** and copy the relevant lever.

Our unique value is the self-improvement loop, scheduler, dedup, guardrails, approval queue, trust graduation, and predictive prefetch. Primitives (system prompts, routing heuristics, budget ceilings) are commodities — borrow them.

## Hard rules

1. **No local pytest execution from any VS Code terminal.** The integrated terminal crashes VS Code's Electron runtime on test runs. CI only. Fix expected by Electron 42 (~2026-07).
2. **Respect the proxy routing config.** The operator has a specific proxy + header setup that keeps costs down; do not change proxy, port, or header logic without explicit approval.
3. **Never regress perf-cost-balance without a larger offsetting win.** See prime directive.
4. **Router-recon tasks go to Haiku.** `analyze`, `review`, `check`, `list`, `identify`, `research`, `count`, `summarize` keywords are Haiku's domain. Haiku 4.5: 33/34 across sprints v1-v4 (97%). Sonnet+reasoning=high on recon: 1/49 (2%). Do not route recon to Sonnet.

## Verified cost levers (2026-04 sprint progression)

| Lever | Cost/success delta | Commit |
|---|---|---|
| Redundant file_read pre-exec interception | $3.90 → $1.68 CAD (−57%) | `b394f4d` |
| Tool budget 20→35, batch run_python hint | $1.68 → $1.78 (+6%) | `91d6bb3` |
| Copilot stop-doctrine + router recon override + campaign YAML tier | $1.78 → **$0.13 (−93%)** | `bd11c0c` + `e4e72ee` |

**Cumulative: 30× cost-per-success reduction** ($3.90 → $0.13 CAD).

Next lever candidate: fix broken master tests that block self-improve pre-test baseline (currently 9/18 v4 failures).

## Where to look

- Router: `src/secretary/router.py` (`_LOW_PATTERNS`, `_RECON_PATTERNS`, `estimate_complexity`)
- Direct agent system prompt: `src/secretary/direct_agent.py` (`_build_system_prompt`)
- Campaign definitions: `campaigns/self-build.yaml` — `tier:` becomes `force_tier` and bypasses router
- Goal state: `data/goal_state.json` — schema is `{last_reviewed, sub_goal_status, progress_notes, total_cycles, meta_reflections}`; keep flat
- Sprint reports: `data/sprint-v*-report.md`
- Self-improve pipeline: `src/secretary/self_improve.py` (uses CI, NOT local tests)

## Session end checklist

1. Commit + push. CI is the test oracle.
2. If you touched router/prompt/budget/routing, update the "Verified cost levers" table above.
3. If you found a new borrow-source that worked, add it to "Core operating principle".
4. Memory + handoff update in `agents-home` as usual.
