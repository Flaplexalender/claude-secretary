# 15-Min Feasibility Analysis: Telegram Integration & Deterministic Pipelines

**Analysis Date:** 2026-06-XX | **Time Budget:** 15 minutes | **Evaluator:** Claude Secretary

---

## EXECUTIVE SUMMARY

| Feature | Feasibility | Priority | MVP Effort | Sequencing | Start? |
|---------|-------------|----------|-----------|-----------|--------|
| **Telegram Integration** | ✅ FEASIBLE | Medium | 3-4h | Can start independently | YES |
| **Deterministic Pipelines** | ✅ FEASIBLE | Medium | 2-3h | Can start independently | YES |
| **Parallel Execution** | ✅ YES | - | 5-7h total | No blocking deps | YES |

---

## 1. TELEGRAM INTEGRATION

### 1.1 Success Criteria (from goals.yaml)
```
description: "Telegram bot interface for mobile interaction"
priority: 5 (stretch feature)
status: not-started
```

### 1.2 Minimum Viable Features (MVP)
| # | Feature | Why Essential | Est. Time |
|---|---------|---------------|-----------|
| 1 | Bot token + webhook setup | Receive messages from Telegram API | 20m |
| 2 | Message receive handler | Parse incoming messages, extract intent | 30m |
| 3 | Route to secretary agent | Delegate to existing `direct_agent.run()` | 20m |
| 4 | Response send handler | Format result, send back to user | 20m |
| 5 | Error handling + logging | Graceful failures, audit trail | 15m |
| 6 | Unit tests | Validate token handling, message routing | 30m |
| **MVP Total** | | | **2.75h** |

### 1.3 Dependencies Analysis

#### INCOMING DEPENDENCIES (what telegram-integration needs)
```
✅ AVAILABLE NOW:
  - direct_agent.run(task, tier='low', ...) 
    → Existing agent infrastructure, ready to use
  - config.yaml + SecretaryConfig 
    → Can add telegram_token, webhook_url config
  - Error handling patterns (direct_agent.py, watcher.py)
    → Proven error recovery patterns

❌ NO BLOCKING DEPENDENCIES:
  - Does NOT need web-dashboard (can use polling or webhook separately)
  - Does NOT need deterministic-pipelines (handles LLM tasks fine)
```

#### OUTGOING DEPENDENCIES (what depends on telegram-integration)
```
None identified. Telegram integration is a leaf feature — doesn't feed into other goals.
```

#### External Dependencies
```
✅ ZERO blocking external deps:
  - python-telegram-bot (pip install, no auth conflicts)
  - Telegram Bot API (webhook or polling, both simple)
  - No database needed for MVP (state can go to config.yaml)
```

### 1.4 Architecture Sketch

```python
# telegram_integration/bot.py (NEW ~200 LOC)

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message.text
    # Parse intent (simple regex or let agent infer)
    result = await direct_agent.run(task=user_msg, tier='low', max_turns=3)
    await update.message.reply_text(result.text[:4096])  # Telegram char limit

async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    await app.run_polling()  # or run_webhook() for production
```

### 1.5 Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Telegram API rate limiting | Low | Medium | Add per-user cooldown (simple dict) |
| Long-running tasks timeout | Medium | Low | Use `max_turns=3`, timeout=60s, notify user |
| Message size limits | Low | Low | Truncate response to 4096 chars + "..." |
| Token leakage in logs | Low | High | Store token in .env, never log it |

---

## 2. DETERMINISTIC PIPELINES

### 2.1 Success Criteria (from goals.yaml)
```
description: "Code-based pipelines for simple tasks (no LLM needed)"
priority: 5 (stretch feature)
status: not-started
```

### 2.2 Minimum Viable Features (MVP)
| # | Feature | Why Essential | Est. Time |
|---|---------|---------------|-----------|
| 1 | Pipeline schema (steps, inputs, outputs) | Define structure for reproducibility | 20m |
| 2 | YAML pipeline loader | Read pipeline definitions from files | 20m |
| 3 | Step executor (function call) | Execute Python functions by name | 20m |
| 4 | Data flow plumbing | Chain outputs → inputs across steps | 25m |
| 5 | Conditional branching (if/else) | Handle simple decision logic | 20m |
| 6 | Error handling + logging | Fail gracefully, audit trail | 15m |
| 7 | Integration with watcher | Route deterministic tasks away from LLM | 20m |
| 8 | Unit tests | Validate schema, execution, data flow | 30m |
| **MVP Total** | | | **2.9h** |

### 2.3 Dependencies Analysis

#### INCOMING DEPENDENCIES (what deterministic-pipelines needs)
```
✅ AVAILABLE NOW:
  - campaign.yaml schema + loader (watcher.py)
    → Can piggyback on existing YAML infrastructure
  - Task execution hooks (watcher.py run_task())
    → Can inject deterministic executor here
  - Memory store (memory.py)
    → Can pass to pipelines for state
  - Tool library (direct_tools.py: file_read, run_command, etc.)
    → Pipelines can call these as steps

❌ NO BLOCKING DEPENDENCIES:
  - Does NOT need telegram-integration (independent feature)
  - Does NOT need web-dashboard (can log to file)
```

#### OUTGOING DEPENDENCIES (what depends on deterministic-pipelines)
```
Potential:
  - Watcher might use pipelines for "easy" tasks → lighter resource use
  - Telegraph might call pipelines for simple Telegram commands
  (But neither is required for MVP)
```

#### External Dependencies
```
✅ ZERO new external deps:
  - Uses PyYAML (already required)
  - Uses pathlib, json (stdlib)
  - No database, no network calls
```

### 2.4 Architecture Sketch

```python
# src/secretary/deterministic_pipeline.py (NEW ~300 LOC)

@dataclass
class PipelineStep:
    name: str
    action: str  # 'call_function', 'if', 'for', 'read_file', etc.
    function: str  # e.g. 'file_read', 'run_command'
    args: dict  # {'path': 'src/main.py'}
    input_binding: dict  # {'path': '{{ steps[0].output.path }}'} (template)
    output_name: str  # 'file_content'

@dataclass
class Pipeline:
    name: str
    description: str
    steps: list[PipelineStep]
    
async def execute_pipeline(pipeline: Pipeline, context: dict) -> dict:
    """Execute steps sequentially, binding outputs to next step inputs."""
    results = {}
    for step in pipeline.steps:
        # Bind inputs: {{ steps[0].output.x }} → actual value
        bound_args = template_bind(step.input_binding, results)
        # Execute step (call function or conditional)
        output = await executor.execute(step.function, **bound_args)
        results[step.output_name] = output
    return results
```

### 2.5 Example Pipeline (YAML)

```yaml
pipelines:
  - name: "email-summary"
    description: "Fetch unread emails, extract summaries (no LLM)"
    steps:
      - name: "fetch-emails"
        action: "call_function"
        function: "gmail_search"
        args: { query: "is:unread newer_than:1d" }
        output_name: "emails"
        
      - name: "parse-summary"
        action: "for_each"
        iterable: "{{ steps[0].output.emails }}"
        function: "extract_subject"  # Custom function
        output_name: "summaries"
        
      - name: "filter-important"
        action: "if"
        condition: "len(summaries) > 0"
        steps:
          - function: "log_to_file"
            args: { path: "data/email_summary.txt", content: "{{ summaries }}" }
```

### 2.6 Risk Assessment
| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Complex pipelines → reinvent control flow | Medium | High | Start MVP with seq + if/else only, no loops |
| Data binding bugs (template resolution) | Medium | Medium | Thorough unit tests, verbose error messages |
| Step dependencies → hard to debug | Medium | Low | Explicit input_binding in schema, clear naming |
| Pipeline execution timeout | Low | Medium | Add global timeout + per-step timeout config |

---

## 3. DEPENDENCY MATRIX

### 3.1 Can These Run in Parallel?

```
telegram-integration ─ NO DEPS ─ deterministic-pipelines

    Both are LEAF features — no blocking dependencies on each other.
    Both have independent external APIs (Telegram, file I/O).
    Both integrate with existing watcher/agent infrastructure.
    
    ✅ VERDICT: Can run in PARALLEL
```

### 3.2 Does telegram-integration Need web-dashboard?

```
NO. 
  - Telegram provides the mobile UI (user sends message → bot replies)
  - Dashboard is for monitoring/analytics, not required for MVP
  - Can add dashboard later to show Telegram message history
```

### 3.3 Does telegram-integration Need deterministic-pipelines?

```
NO for MVP.
  - Can route Telegram tasks to direct_agent.run() (LLM-powered)
  - Deterministic pipelines are an optimization for "simple" tasks
  - Nice-to-have: later, Telegram can route simple tasks to pipelines
    (e.g., "list my files" → run deterministic pipeline instead of LLM)
```

### 3.4 Does deterministic-pipelines Need web-dashboard?

```
NO.
  - Dashboard is for visualization, not required for pipelines to work
  - Pipelines log execution to file and memory
  - Dashboard can later query pipeline results for display
```

---

## 4. SEQUENCING RECOMMENDATION

### Option A: Parallel (RECOMMENDED)
```
Week 1, Mon-Tue: 
  - Dev A: Telegram Integration (2-3 hours)
  - Dev B: Deterministic Pipelines (2-3 hours)
  
Week 1, Wed:
  - Integration test: Telegram routes to pipelines (optional for MVP)
  
Advantage: Both ship faster, independent progress
Risk: Minimal — no blocking dependencies
```

### Option B: Sequential (safer, but slower)
```
Week 1, Mon-Tue: Deterministic Pipelines (foundational)
Week 1, Wed-Thu: Telegram Integration (can optionally use pipelines)

Advantage: Pipeline foundation might benefit Telegram routing
Risk: Delays Telegram by 1 day, no technical blocker
```

**RECOMMENDATION: Go with Option A (Parallel)**

---

## 5. EASIEST SUB-GOAL TO START WITH

### 5.1 Comparison

| Sub-goal | Complexity | Dependencies | First-to-MVP | Why? |
|----------|-----------|--------------|--------------|------|
| **Telegram: Message Handler** | ⭐⭐ (low) | None | **1st** | Simplest API, proven patterns |
| Telegram: Response Sender | ⭐⭐ | Message Handler | 2nd | Depends on msg handler |
| Deterministic: Schema + Loader | ⭐⭐⭐ (med) | YAML | 3rd | More moving parts (data binding) |
| Deterministic: Step Executor | ⭐⭐⭐ | Schema | 4th | Needs schema first |

### 5.2 EASIEST START: Telegram Message Handler

**Why this one?**
1. **Smallest scope**: Single function, ~50 LOC
2. **Zero dependencies**: No other features needed
3. **Proven pattern**: Similar to watcher's task handler
4. **Quick feedback loop**: Can test with real Telegram bot in 10 minutes
5. **Foundation for rest**: Once handler works, sender + routing trivial

**Definition of Done:**
- [ ] Receive message from Telegram API
- [ ] Extract user text
- [ ] Route to `direct_agent.run()`
- [ ] Return result to user
- [ ] Graceful error handling (invalid token, timeout)
- [ ] Unit test with mock Telegram API

**Estimated Time:** **45 minutes** (tight MVP)

---

## 6. INITIAL STEP PLAN: Telegram Message Handler

### Phase 1: Setup (10 min)
```
1. Create src/secretary/telegram_integration.py (stub)
2. Create tests/test_telegram_integration.py (stub)
3. Add config fields:
   - config.yaml: telegram_token, telegram_webhook_url
   - SecretaryConfig: telegram: TelegramConfig(token=str, webhook_url=str)
4. Run `poetry add python-telegram-bot` (if not present)
```

### Phase 2: Message Handler (20 min)
```
5. Implement telegram_integration.handle_message(message_text: str) -> str:
   a. Create minimal Update/Message mock objects
   b. Parse message text
   c. Call direct_agent.run(task=message_text, tier='low', max_turns=3)
   d. Extract result.text (or result.error)
   e. Return formatted response
   f. Add try-catch for errors (timeout, invalid token, etc.)
   
6. Implement telegram_integration.send_response(user_id: int, text: str):
   a. Create Bot(token=config.telegram.token)
   b. bot.send_message(chat_id=user_id, text=text)
   c. Handle rate limiting (log warning, don't crash)
   d. Handle message size limit (chunk if >4096 chars)
```

### Phase 3: Unit Tests (10 min)
```
7. Test handle_message():
   a. Mock direct_agent.run() → return success
   b. Verify message is extracted correctly
   c. Verify result is returned
   
8. Test send_response():
   a. Mock Bot.send_message()
   b. Verify message is sent to correct user
   c. Test chunking for large messages
   
9. Test error handling:
   a. Mock direct_agent.run() → raise TimeoutError
   b. Verify error is caught, user notified
```

### Phase 4: Integration (5 min)
```
10. Add CLI command: `secretary telegram --token=XXX`
    (optional for MVP, can just be function import)
    
11. Wire into watcher (future): optional route in campaign.yaml
    (skip for MVP v1)
```

**Total: ~45 minutes for working message handler**

---

## 7. EFFORT BREAKDOWN & TIMELINE

### Telegram Integration (2.75h MVP)
```
Slack Effort (actual work blocks, no waiting):
  - Setup (pip, config, stubs)                   :  20m
  - Message handler (receive + route)             :  30m
  - Response sender (format + send)               :  20m
  - Error handling + logging                      :  15m
  - Unit tests (3-4 tests)                        :  30m
  - Integration with watcher (optional)           :  30m
  ────────────────────────────────────────────────
  TOTAL                                           : 2h 45m

Blocking risks:
  - Telegram API auth (25% chance 10m debugging)
  - Rate limiting (15% chance 5m throttling add)
  - Timeout handling (20% chance 10m extra logic)
```

### Deterministic Pipelines (2.9h MVP)
```
Slack Effort:
  - Schema design (dataclasses)                   :  20m
  - YAML loader                                  :  20m
  - Step executor (function dispatch)             :  20m
  - Data binding (template resolution)            :  25m
  - Conditional branching (if step)               :  20m
  - Error handling                                :  15m
  - Unit tests (5-6 tests)                        :  30m
  - Watcher integration (optional)                :  20m
  ────────────────────────────────────────────────
  TOTAL                                           : 2h 50m

Blocking risks:
  - Template binding bugs (30% chance 15m debugging)
  - Step executor dispatch (20% chance 10m refactor)
  - YAML schema evolution (15% chance 5m cleanup)
```

### Combined (if parallel)
```
Dev A (Telegram):              2h 45m
Dev B (Deterministic):         2h 50m
Parallel overhead (reviews):   +30m
────────────────────────────────────
CALENDAR TIME:                 ~4-5 hours (both done, ready for integration)
EFFORT:                        5h 35m (two developers working in parallel)

vs Sequential:
  Calendar: 6+ hours (serial work)
  Efficiency: 5h 35m still (no savings)
```

---

## 8. RISK & CONTINGENCY

### High Risk Factors
| Factor | Probability | Mitigation |
|--------|------------|-----------|
| Telegram token/auth issues | 25% | Have backup: test with `curl` first |
| Data binding syntax ambiguity | 30% | Steal from template: `{{ var }}` (proven) |
| Agent timeout on Telegram tasks | 20% | Cap max_turns=3, timeout=60s |

### Contingency Plan
```
IF Telegram integration takes 4+ hours:
  → Focus on message receive only (skip send for v1)
  → Can still demo "bot receives msg, console prints response"
  → Add send_response() in v2

IF Deterministic pipelines take 3.5+ hours:
  → Cut to sequential + if/else (no loops/for)
  → Defer advanced features to v2
  → Still functional for common cases
```

---

## 9. GO/NO-GO DECISION

### Readiness Checklist
- [x] No blocking external dependencies
- [x] Can run in parallel without coordination
- [x] Existing infrastructure supports both (agent, config, watcher)
- [x] MVP scope is clear and achievable
- [x] Risks are identified and mitigable
- [x] Start can begin TODAY (no waiting)

### RECOMMENDATION: **GO IMMEDIATELY**

**Start with:** Telegram Message Handler (easiest, 45m to first working version)

**Then:** Deterministic Pipelines OR Telegram Response Sender (both ~30m)

**Then:** Optional integration work (watcher hooks, dashboard links)

---

## 10. APPENDIX: File Structure (Post-Implementation)

```
src/secretary/
├── telegram_integration.py (NEW, 200-300 LOC)
│   ├── TelegramConfig (dataclass)
│   ├── handle_message(text: str) -> str
│   ├── send_response(user_id: int, text: str)
│   └── async main() → polling loop
│
├── deterministic_pipeline.py (NEW, 300-400 LOC)
│   ├── PipelineStep (dataclass)
│   ├── Pipeline (dataclass)
│   ├── PipelineLoader
│   ├── PipelineExecutor (async)
│   └── template_bind(template: dict, context: dict) -> dict
│
├── config.py (MODIFIED, +30 LOC)
│   ├── TelegramConfig (new)
│   └── SecretaryConfig.telegram (new field)
│
└── watcher.py (OPTIONAL HOOK, +20 LOC)
    ├── is_deterministic_task(task) -> bool
    └── route_to_pipeline_or_agent()

tests/
├── test_telegram_integration.py (NEW, 100+ LOC)
├── test_deterministic_pipeline.py (NEW, 150+ LOC)
└── ...

campaigns/
├── telegram-bot.yaml (NEW example)
└── deterministic-examples.yaml (NEW examples)
```

---

**Analysis Complete. Ready to execute. 🚀**
