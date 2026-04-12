# Long-Term Memory

Curated learnings. Distilled from daily logs. Updated periodically during heartbeats.

## Lessons Learned

- **NEVER run pytest/tests locally** — causes VS Code Electron crashes. Always use CI: `git add/commit/push` and let GitHub Actions run the test suite. The `run_command` tool blocks pytest.
- Message prefix: few-shot priming via copilot-api proxy. This is the primary tool-use optimization.
- Self-improve pipeline must NOT analyze its own failures — creates self-referential loops.
- Tests with timeout/cancellation semantics need slow mocks (10s). "Faster" mocks break the cancellation path.
- Scope constraints must be consistent across ALL prompts (main, retry, preamble). Contradictions waste money.
- `.get()` on dataclasses crashes — always use `getattr()` or `isinstance()` check first.
- Sandbox test environment must match production (same exclusions, same dependencies).
- `campaign.yaml` heartbeat threshold: 3600 — DO NOT REDUCE. Self-improve keeps reverting this.
- File-similarity dedup is insufficient for LLM proposals — use structural dedup (target file+function).
- Default parameter values that are "reasonable" can be disastrous in different contexts. Audit all callers.

## Active Goals

- Self-improvement pipeline: operational, auto-graduated to autonomous
- Prefix survival: message prefix working, oracle ensemble as backup
- Budget optimization: deep work mode, billing efficiency, free-tier routing

## Key Facts

- 1000+ tests across 75 test files
- 90+ source modules
- Budget: $5/day USD, $25/week USD
- Daemon: 30-min watcher cycle
- Routing: Bayesian bandit learned_router with 75% success rate threshold
