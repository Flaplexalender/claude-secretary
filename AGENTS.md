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
- Proposal outcome feedback loop: `src/secretary/proposal_outcomes.py` + wire-ins in `self_improve.py` (promotion → baseline) and `watcher.py` (per-cycle measure) and `goal_self_improve.py` (prompt injection). Data: `data/proposal_outcomes.jsonl`.

## SOTA alignment (2026-04 audit)

Secretary is aligned with 2026 self-improving scaffolding SOTA. Verified:

- **Voyager-style skill library**: `strategy_library.py` + live JSON at `data/strategy_library.json`. Extracted on task success (`watcher.py` L1503), scored on outcome, retrieved by category into system prompt (`direct_agent.py` L507, `oracle.py` L151). Top strategy currently 684 uses, 38% success. **Fully wired — not a gap.**
- **Reflexion-style verbal feedback**: `goal_self_improve.py` `_build_analysis_prompt` surfaces prior proposal successes/failures + test-failure output. **Fully wired.**
- **STOP-like sandbox+test gate**: `self_improve.py` sandbox → tests → promote → post-promote regression verify → rollback on failure. **Fully wired.**
- **ACE/empirical feedback on self-improve proposals** (THE gap closed in commit after this): `proposal_outcomes.py` measures real cost-per-success delta over N tasks after promotion; feeds verdicts back into next Haiku analysis via `format_recent_outcomes_for_prompt`. This closes the "tests-passing ≠ metric-improving" blind spot.

Remaining SOTA bets (future work, not gaps today):

- **Alita-G-style tool synthesis**: auto-generate new `run_*` tools from recurring successful trajectories. Large effort; skill library already captures the prompt-level analog.
- **Multi-agent reflection synthesis**: generative-agents-style cross-agent memory. Lower priority — single-agent Reflexion already productive.

## Session end checklist

1. Commit + push. CI is the test oracle.
2. If you touched router/prompt/budget/routing, update the "Verified cost levers" table above.
3. If you found a new borrow-source that worked, add it to "Core operating principle".
4. Memory + handoff update in `agents-home` as usual.
