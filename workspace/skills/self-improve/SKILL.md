---
name: self-improve
description: Autonomous code improvement pipeline — propose, sandbox, test, promote
---

## How It Works
1. **Analysis**: Review run_log.jsonl for failures and patterns
2. **Proposal**: Generate a specific code change targeting a file+function
3. **Sandbox**: Copy project to isolated directory, apply change
4. **Test**: Run full test suite in sandbox (300s timeout)
5. **Promote**: If tests pass, commit the change to the real project

## Rules
- Only modify files in `src/secretary/` — NEVER touch `tests/`
- `self_improve.py` and `goal_self_improve.py` are manually maintained — do not auto-modify
- Test timeout is 300s — don't reduce, sandbox overhead is real
- Maximum tier: medium (Sonnet). Never use Opus for self-improve — $4 CAD per failure.
- Self-improve must NOT analyze its own failures — creates self-referential loops

## Anti-Patterns (Learned the Hard Way)
- Don't change mock sleep values in tests — they test cancellation paths
- Don't rename fields that tests reference (e.g. `num_turns` not `total_turns`)
- Don't change heartbeat threshold (3600) — it keeps getting reverted
- Text-similarity dedup isn't enough — use structural dedup (target file+function)
- Proposals must be SPECIFIC (target file, exact function, what to change). Vague "improve X" → wasted turns.
- Scope constraints must be consistent across ALL prompts (main, retry, preamble)
