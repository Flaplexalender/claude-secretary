# Claude Secretary — TODO

## Scaffolding Cleanup (Session 2026-04-20, Copilot Opus 4.7)
- [x] **Delete 14 dead "cargo-cult" flag constants** from `direct_agent.py` — `_GMAIL_*`, `_AUTH_*`, `_ENABLE_*`, `_FORCE_UTF8_ON_WINDOWS`, `_TOOL_TIMEOUT_S` etc. All were module-level `= True` assignments with "✅ ACTIVE" comments but ZERO references anywhere in the codebase. Self-improve hallucinated the features.
- [x] **Wire `_MAX_PARALLEL_TOOLS = 6` with `asyncio.Semaphore`** around the per-turn tool gather so the cap is actually enforced. Previously the constant was just a number sitting in the file; `asyncio.gather` ran all N tools unthrottled regardless.
- [x] **Fix flaky Monday-only test failures** in `test_cost_monitor.py` — `test_weekly_spend_includes_this_week` and `test_weekly_alert_when_daily_is_ok` used `hours_ago=24/48` which crossed ISO-week boundaries when CI ran on Mondays/Tuesdays. Added `frozen_now` fixture patching `cost_monitor.datetime` to a fixed Wednesday (2026-04-22), anchored entry timestamps to that fake clock. CI green on run 24691965609.

## Core Features (Working)
- [x] CLI: `secretary run` — one-shot task execution
- [x] CLI: `secretary chat` — interactive multi-turn chat
- [x] CLI: `secretary status` — config/memory/routing/premium info
- [x] CLI: `secretary history` — run log stats, table, cost column
- [x] CLI: `secretary auth` — Google OAuth setup
- [x] CLI: `secretary test` — validate setup (proxy, auth, campaign, config)
- [x] CLI: `secretary improve` — self-improvement pipeline (sandbox → test → promote)
- [x] CLI: `secretary watch` — 24/7 watcher daemon
- [x] Watcher: `--dry-run` (preview without API calls)
- [x] Watcher: `--max-premium` (premium budget cap per cycle, default 3.0x)
- [x] Watcher: `--max-runs` (CLI override for run limit)
- [x] Model routing — Haiku (0.33x) / Sonnet (1x) / Opus (3x)
- [x] Memory — JSON-backed with fuzzy dedup, short + long term
- [x] MCP tools — Gmail (search, read, draft, send) + Calendar (today, list, search, create)
- [x] Run logging — JSONL append-only with premium cost tracking
- [x] Premium cost tracking (per-task, per-cycle, in history + status)
- [x] Self-improve: sandbox tests use source venv + PYTHONPATH
- [x] Copilot-vs-Secretary benchmark (Copilot 19/19, Secretary 17/19)
- [x] Git initialized with .gitignore

## Next Up
- [x] **Upgraded `claude-code-sdk` → `claude-agent-sdk` v0.1.48** (deprecated SDK removed)
- [x] Removed dead code: `gmail_server.py` + `calendar_server.py` (413 lines deleted)
- [x] Error recovery: retry failed tasks with backoff in watcher
- [x] Watcher: skip duplicate tasks within cycle (dedup by prompt hash)
- [x] SDK cost/turns tracking from `ResultMessage` (`total_cost_usd`, `num_turns`, `duration_ms`)
- [x] `secretary test` command — validates proxy, Google auth, campaign, config
- [x] Watcher shutdown summary (cycles, pass rate, premium spent, uptime)
- [x] Cross-cycle dedup (skip tasks that succeeded in the previous cycle, per-task opt-out)
- [x] Cost tracking in history, status, and run log summary
- [x] Campaign templates: pre-built YAML files for common workflows (email-triage, daily-planner, self-monitor)
- [x] Add `secretary config` command to show/edit config (dot-notation key lookup)
- [x] Campaign schedule expressions (hours, weekdays/weekends, combined rules)
- [x] `secretary logs` command (search, filter by tier/status, JSON export)
- [x] `secretary memory` command (view/manage memory store)
- [x] `secretary config` command (dot-notation key lookup, full dump)
- [x] `secretary heartbeat` command (check watcher status)
- [x] `secretary export` command (CSV/JSON export of run history)
- [x] Persistent task dedup across restarts (dedup_history.json)
- [x] Task timeout (per-task or global config.watcher.task_timeout)
- [x] Task priority ordering (priority field in campaign YAML)
- [x] Multi-campaign support (comma-separated campaign files)
- [x] Campaign schedule display in preview (shows ⏰ for scheduled tasks)
- [x] Adopt `@tool` decorator for all 8 tools (gmail + calendar)
- [x] SDK PostToolUse hooks — tools_used tracking on every run
- [x] Tier escalation on retry (low → medium → high, per-task opt-out)
- [x] Task dependencies (id + depends_on fields in campaign YAML)
- [x] Failure notification via Gmail draft (notify_email config)

## Recent Additions (Session 2026-03-XX)
- [x] **CAD currency support** — All cost displays now show CAD primary with USD secondary. Configurable rate (default 1.44). New `currency.py` module.
- [x] **Quota exhaustion detection** — Watcher detects API quota/rate-limit errors, enters 60-min cooldown instead of burning retries.
- [x] **Self-improve backup/rollback** — Before promoting sandbox changes, originals are backed up. `rollback()` method available. `--keep-sandbox` flag.
- [x] **Tool input validation** — Email format validation in gmail_draft/send, ISO 8601 validation in calendar_create, _extract_body error handling.

## Recent Additions (Session 2026-03-17)
- [x] **Windows UTF-8 stdout** — Reconfigure stdout/stderr to UTF-8 at startup to prevent cp1252 UnicodeEncodeError on arrows/checkmarks.
- [x] **Self-improve stale sandbox** — Warn + remove stale sandboxes from previous failed runs.
- [x] **Schedule robustness** — Malformed hour ranges caught gracefully instead of crashing.
- [x] **Empty campaign warning** — `_load_campaign()` warns when YAML has no tasks key.
- [x] **Deprecation cleanup** — Replaced `datetime.utcnow()` and `asyncio.get_event_loop()` — 0 warnings.
- [x] **Agent tests** — New `test_agent.py` with 8 tests (system prompt, env setup, RunResult).
- [x] **211 total tests, 0 warnings**.

## Recent Additions (Current Session)
- [x] **`--max-turns` CLI flag** — Override tier's default max_turns for `secretary run` and `secretary improve`.
- [x] **`secretary version` command** — Shows package version, Python version, Claude Agent SDK version.
- [x] **Config validators** — Pydantic validators for ModelTier (budget ≥ 0), RoutingConfig (default_tier ∈ tiers), WatcherConfig (interval ≥ 1, retries ≥ 0, timeout ≥ 0), CurrencyConfig (rate > 0).
- [x] **Fixed Ctrl+C dedup loss** — Watcher now saves dedup history on KeyboardInterrupt instead of losing state.
- [x] **History `--failed` and `--search` filters** — `secretary history --failed` shows only failures, `--search email` filters by keyword.
- [x] **Extended `secretary test`** — Now checks run_log writable, dedup history writable, Google Calendar auth (9 checks total).
- [x] **Currency `set_rate()` validation** — Rejects zero, negative, NaN, and infinity values.
- [x] **Resilient campaign loading** — Malformed YAML or missing campaign files are logged and skipped instead of crashing the watcher.
- [x] **Heartbeat error handling** — Disk write failures no longer crash the watcher.
- [x] **Efficient run_log** — `recent()` uses `collections.deque` for O(n) tail reads instead of loading entire file.
- [x] **Run log boundary fix** — `recent(0)` and `recent(-1)` return empty list instead of crashing.
- [x] **256 total tests, 0 warnings**.

## Recent Additions (Session 2026-06-12)
- [x] **Multi-instance coordination** — `coordinator.py`: file-based task claiming (atomic `O_CREAT|O_EXCL`), instance registry with heartbeat-based stale detection, role-based task filtering, results sharing. Zero new dependencies.
- [x] **Metrics framework** — `metrics.py`: per-instance/per-task metrics (turns, tokens, cost, duration), aggregation by instance/config, A/B benchmark comparison with weighted scoring, JSONL persistence.
- [x] **CLI: `secretary metrics`** — `show` (per-instance efficiency), `instances` (active instances), `benchmarks` (comparison history).
- [x] **`--role` and `--coordinate` flags** — `secretary watch --instance worker-1 --role researcher --coordinate` for role-based multi-instance collaboration.
- [x] **Example multi-instance campaign** — `campaigns/multi-instance.yaml` with role-tagged tasks (triager, researcher, builder).
- [x] **43 new tests** — 26 coordinator tests, 17 metrics tests. All passing.
- [x] **Efficiency optimizations** — 5 toggleable optimizations in `OptimizationConfig` for A/B testing:
  - **Selective tool exposure**: Filters tool schemas by task keywords (~800-2000 input tokens saved/turn)
  - **Turn budget signaling**: Injects remaining turn count → model batches more aggressively
  - **Context preloading**: Pre-reads scratchpad.md into task prompt (saves 1+ entire turns)
  - **Conversation summarization**: Extractive zero-cost compression of old conversation turns
  - **Dynamic max_tokens**: Scales down output budget on later turns (32K→16K→8K)
- [x] **OpenAI message translation fix** — Mixed tool_result + text content blocks now handled correctly
- [x] **A/B benchmark campaign** — `campaigns/benchmark-optimizations.yaml` + `scripts/benchmark-ab.ps1` launcher
- [x] **67 new tests this session** (26 coordinator + 17 metrics + 24 optimizations). 893 total pass.

## Efficiency Sprint Results (2026-06-18)
Secretary Opus self-implemented these via `campaigns/efficiency-sprint.yaml` (81× premium, $12.77 USD, 100% pass rate):
- [x] **Aggressive early exit** — Consecutive tool error breaker (3 strikes → stop), turn budget after turn 2
- [x] **Shorter tool descriptions** — All 12 tools: 2-3 word descriptions (~51% schema token reduction), helpers extracted to `_tool_helpers.py`
- [x] **Task batching** — `task_batcher.py`: merge consecutive `batch_compatible` tasks of same tier into single agent calls
- [x] **Quality scoring** — `_score_quality()` heuristic 0-1 score for A/B comparison
- [x] **Aggressive context injection** — `_build_aggressive_context()` pre-reads matched project files (15KB cap)
- [x] **Overnight schedule fix** — `hours:22-6` wraps around midnight correctly
- [x] **Atomic dedup writes** — tempfile + fsync + rename prevents corruption
- [x] **988 total tests, 0 failures, 2 skipped**

## Recent Additions (Session S49)
- [x] **Layer 29: Self-Generated Test Harness** — `goal_harness.py`: Haiku generates pytest tests from goal success_criteria, runs in subprocess sandbox (30s timeout), blocks goal completion on failure. Wired into watcher. 31 new tests.
- [x] **New `self-harness` goal** — Added to goals.yaml (priority 2, 4 sub-goals: generation, verification, evolution, CI).
- [x] **Research**: Eureka (LLM writes eval code), CodeAct (code as actions), MAST (failure taxonomy), Kwa (time-horizon scaling).
- [x] **1995 total tests, 0 failures, 2 skipped**

## Recent Additions (Session S50 cont. #4)
- [x] **Fix: MIN_GRADUATION_SAMPLES deadlock** — Lowered 3→2. Untrusted goals couldn't accumulate enough samples to ever graduate (chicken-and-egg). Stability check + cooldown are the real guards.
- [x] **Fix: Decomposition JSON parsing** — Added string-aware brace-depth extraction + truncated JSON recovery to `goal_decomposition.py`. Bumped DECOMP_MAX_TOKENS 1024→2048.
- [x] **Fix: direct_agent syntax error** — Two return statements mashed together in `quality_score`.
- [x] **Self-improvement graduated** — untrusted → cautious at cycle 28 (review + supervised).
- [x] **Self-harness graduated** — untrusted → cautious at cycle 30 (review + supervised).
- [x] **Goal tasks producing outputs** — alerter.py, monitor.py, test harness templates, cost alert spec generated by autonomous goal execution.
- [x] **5 watcher cycles (26-30)** — all clean, escalations running, meta-reflections generating cross-patterns.

## Recent Additions (Session — Public Launch)
- [x] **PII pre-commit hook** — `.pii-patterns` + `hooks/pre-commit.py` scans staged diffs for personal data. Blocks commits with PII.
- [x] **Haiku-as-planner** — `planner.py`: cheap model (Haiku 0.33x) generates execution plans for complex tasks before expensive model (Opus 3x) executes. Integrated into `direct_agent.py`. 25+ tests.
- [x] **CONTRIBUTING.md** — Contributor guide: setup, pre-commit hook install, testing rules, project structure, code style.
- [x] **Repo made public** — Full PII audit, git history squashed, force-pushed clean tree.

## Stretch / Future
- [ ] Per-sandbox venv isolation (prevents editable install collision)
- [ ] Telegram integration (receive/send messages)
- [ ] Web dashboard for run history
- [x] ~~Multi-agent: delegate sub-tasks to cheaper models~~ — Haiku-as-planner (planner.py)
- [ ] Auto-start watcher on system boot (Windows Scheduled Task)
- [ ] Dynamic task selection agent (replace static YAML with decision-making)
- [ ] Harness-verification: inject harness results into verification judge prompt
- [ ] Harness-evolution: secretary evolves test suite as goals change
- [ ] Harness-ci: auto-run harness tests before goal graduation
- [x] ~~Email body size limit enforcement~~ — 256KB limit with _validate_body(), done in b726f35
- [x] ~~Fix self-improvement CLI~~ — Was venv path bug (.exe missing on Windows), fixed in b21c94d
- [x] ~~Retry logic for transient Google API failures~~ — `_call_with_retry()` with exponential backoff, done in 813e915
