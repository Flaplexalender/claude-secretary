# Goal-Driven Reactive Task Generation: Analysis & Insertion Points

## Executive Summary

The Secretary system has a **fully functional reactive task-generation pipeline**:

```
Event Bus (poll) → OODA/Goals (decide) → Task Queue (batch) → Executor (run) → Reflection (learn)
```

**Goal-driven task generation currently happens at 3 distinct points in the cycle.** This document identifies those points and proposes how to make them **proactive** (predictive, not just reactive).

---

## 1. Current Reactive Pipeline

### Flow: Event → Task

```
Each watcher cycle:

  1. event_bus.poll_all()
     ├─ Gmail source: hash unread emails → Event(type=EMAIL_RECEIVED, payload={...})
     ├─ Calendar source: hash upcoming events → Event(type=CALENDAR_EVENT)
     ├─ File source: track mtimes → Event(type=FILE_CHANGED)
     └─ Result: list[Event] for this cycle

  2. Reactive trigger matching (watcher._run_cycle, line ~1400)
     For each campaign task with "trigger: 'event:gmail:unread'":
       ├─ event_bus.matches_trigger(trigger_spec) → filter matching events
       ├─ If match: enqueue task for execution
       └─ If no match: skip task

  3. OODA Decision Loop (ooda.py:run_ooda_cycle, line ~100)
     if cycle_events AND ooda_enabled:
       ├─ Input: events, recent_log, memory
       ├─ Prompt cheap LLM (Haiku): "Decide what tasks to generate from these events"
       ├─ Parse response → ad-hoc tasks
       └─ tasks.extend(ooda_tasks)  # injected into queue

  4. Goal Planner (watcher._run_cycle, line ~1410+)
     if goals_enabled:
       ├─ Goal store loads goals from YAML
       ├─ compute_progress(goals) → metrics per goal
       ├─ if is_review_due(goal):
       │    └─ run_goal_review() → generates reflection + task ideas
       ├─ get_step_plans(goals) → active decomposition plans
       ├─ for each plan:
       │    ├─ nxt = get_next_step(plan)
       │    ├─ st = step_to_task(nxt)
       │    └─ _goal_tasks.append(st)
       └─ apply_guardrails(_goal_tasks) → final approval

  5. Execution (watcher._run_cycle, line ~1600+)
     for batch in batches:
       ├─ check trigger (if specified)
       ├─ check dependencies
       ├─ run task via agent
       └─ log result

  6. Reflection (watcher._run_cycle, line ~1800+)
     ├─ run_goal_reflection() → analyze goal task outcomes
     ├─ record_step_result() → update step status
     ├─ detect_completed_goals() → check if goals finished
     └─ save goal state
```

---

## 2. Three Key Insertion Points for Goal-Driven Task Generation

### [P1] OODA Decision Point: Goal-Aware Event Classification

**Location:** `src/secretary/ooda.py:run_ooda_cycle()`

**Current Behavior:**
- Receives events from event_bus.poll_all()
- Cheap LLM (Haiku) decides: "Should I create ad-hoc tasks from these events?"
- Tasks are purely event-reactive, not goal-aligned

**Hook Point:**
- After event classification in the planner prompt
- Before `_parse_planner_response()` converts response to tasks

**Opportunity: Goal Prediction**
```
INJECT: List of active goals + event summary into the planner prompt

New question for LLM:
  "Based on these events and the following goals, which goal-related tasks 
   should be generated to capitalize on these events?"

Example:
  Events: [Gmail: "Database migration complete", Calendar: "Q1 planning meeting tomorrow"]
  Active Goals: [Goal1: "Migrate to PostgreSQL", Goal2: "Finalize Q1 roadmap"]
  
  Planner response:
    - Task: "Review migration logs and run post-migration validation" (Goal1-related)
    - Task: "Prepare Q1 roadmap summary for tomorrow's meeting" (Goal2-related)
```

**Implementation:**
1. Fetch active goal list from goal_store before calling run_ooda_cycle()
2. Format goals + descriptions into prompt section
3. Modify planner prompt to ask: "Which goal-related opportunities does this event surface?"
4. Parse response for goal_id tagging
5. Tag generated tasks with goal_id for cross-linking

---

### [P2] Progress Review: Predictive Planning & Critical Path

**Location:** `src/secretary/watcher.py:compute_progress() + run_goal_review()` (lines 1465-1500)

**Current Behavior:**
- Computes quantitative progress metrics (completion %, velocity)
- Evaluates stalls (if progress stalled → escalation)
- Runs review cycle if is_review_due()
- Review generates reflection + suggestions

**Hook Point:**
- After progress metrics calculated (velocity, time-to-completion)
- Before generating review tasks

**Opportunity: Predictive Task Injection**
```
INJECT: Predictive planner that forecasts goal completion

New logic:
  1. Compute current velocity (tasks/week)
  2. Forecast: days_to_completion = (100% - current_progress) / velocity
  3. If days_to_completion > deadline:
       => Goal is OFF-TRACK
       => Identify critical-path steps that MUST complete
       => Insert prerequisite/blocking tasks ASAP

Example:
  Goal: "Complete 30-day project" (deadline: Day 15)
  Progress: 40% (6 days completed)
  Current velocity: 5%/day (on track for Day 20 completion)
  
  Forecast: 12 days remaining, but only 5 days left before deadline
  => Action: Fast-track by inserting parallel prerequisite tasks
  => New tasks: "Parallelize sub-task A+B", "Pre-stage infrastructure for phase 2"
```

**Implementation:**
1. Extend compute_progress() to forecast completion date
2. Add logic: if (forecast_date - deadline) > threshold → off-track
3. Identify critical-path steps in step plans
4. For each critical step:
   - Find prerequisites
   - Insert prerequisite tasks before next step
5. Tag tasks with "_critical_path": true for expedited execution

---

### [P3] Step Plan Execution: Dependency Validation & Prerequisite Injection

**Location:** `src/secretary/watcher.py:get_next_step() → step_to_task()` (lines 1510-1540)

**Current Behavior:**
- Fetches next unexecuted step from decomposition plan
- Converts step to task definition
- Executes step when it reaches front of queue

**Hook Point:**
- After fetching next step via get_next_step()
- Before converting to task via step_to_task()

**Opportunity: Dependency Chain Validation**
```
INJECT: Goal-aware scheduler that checks preconditions

New logic:
  1. When fetching next step from plan, validate its dependency tree
  2. For each prerequisite step:
       - Is it completed? (check step_status in goal_state)
       - If not: insert it into queue BEFORE current step
  3. For inter-goal dependencies:
       - Does step X depend on step Y from a different goal?
       - If Y not done: block X, generate notification task
  4. Detect circular dependencies and report

Example:
  Goal1 Step3: "Deploy to production" (depends on Step2)
  Goal1 Step2: "Pass load tests" (depends on Step1)
  Goal1 Step1: "Set up staging environment" (not started)
  
  Scenario: Step planner wants to execute Step3 next
  Validation: Preconditions not met! Step1 and Step2 must run first
  Action: Insert Step1 + Step2 into task queue ahead of Step3
```

**Implementation:**
1. Extend get_next_step() to return full dependency tree
2. Check each prerequisite's status in goal_state
3. For unmet prerequisites: generate intermediate task
4. Tag generated tasks with "_prerequisite_for": step_id
5. Return task queue as [prereq1, prereq2, step3] (reordered)

---

## 3. Cycle Timeline (Detailed)

```
START OF CYCLE
  |
  +-- T=100ms: event_bus.poll_all() [LINE 1370]
  |   Collect events from Gmail, Calendar, Files
  |   Result: list[Event] for this cycle
  |
  +-- T=150ms: Event trigger matching [LINE 1400]
  |   For each campaign task with "trigger: event:...", check if event matches
  |   Skip non-matching tasks
  |
  +-- T=200ms: OODA cycle [LINE 1395] [INSERTION POINT P1 - GOAL CONTEXT]
  |   if cycle_events AND ooda_enabled:
  |     ooda_tasks = run_ooda_cycle(
  |       events=cycle_events,
  |       active_goals=goal_store.goals,  <-- INJECT HERE
  |     )
  |     tasks.extend(ooda_tasks)
  |
  +-- T=300ms: Goal planner [LINE 1410+]
  |   if goals_enabled:
  |     goal_store.load()
  |     _goal_tasks = []
  |
  |     +-- T=350ms: Reflection [LINE 1430]
  |     |   run_goal_reflection() -> analyze previous outcomes
  |     |
  |     +-- T=450ms: Progress scoring [LINE 1465] [INSERTION POINT P2 - PREDICTIVE]
  |     |   _progress = compute_progress(goals)  <-- INJECT FORECASTING HERE
  |     |   stalled = [g for g, p in _progress if p.stalled]
  |     |   if stalled:
  |     |     esc_actions = evaluate_escalations()
  |     |     _goal_tasks.extend(esc_actions.tasks)
  |     |
  |     +-- T=550ms: Review cycle [LINE 1490]
  |     |   if is_review_due(goal):
  |     |     goal_tasks = run_goal_review()
  |     |     _goal_tasks.extend(goal_tasks)
  |     |
  |     +-- T=650ms: Step planner [LINE 1510] [INSERTION POINT P3 - DEPENDENCIES]
  |     |   step_plans = get_step_plans()
  |     |   for sg_id, plan in step_plans:
  |     |     if not plan.completed:
  |     |       nxt = get_next_step(plan)  <-- INJECT VALIDATION HERE
  |     |       if nxt:
  |     |         validate_preconditions(nxt)  <-- INJECT PREREQUISITE CHECK
  |     |         st = step_to_task(nxt)
  |     |         _goal_tasks.append(st)
  |     |
  |     +-- T=750ms: Guardrails [LINE 1560]
  |     |   apply_guardrails(_goal_tasks)
  |     |   route through approval if needed
  |     |
  |     +-- T=800ms: Extend task queue
  |         tasks.extend(_goal_tasks)
  |
  +-- T=850ms: Task batching [LINE 1380]
  |   batches = group_into_batches(tasks)
  |
  +-- T=900ms: Execute batches [LINE 1600+]
  |   for batch in batches:
  |     check trigger matching
  |     check dependencies
  |     run task via agent
  |     log result to run_log.jsonl
  |     record metrics
  |
  +-- T=END-100ms: Reflection cycle [LINE 1800+]
  |   run_goal_reflection()
  |   record_step_result() for completed steps
  |   detect_completed_goals()
  |   mark_goals_completed()
  |   save goal_state.json
  |
END OF CYCLE
  Sleep for interval_minutes, then repeat
```

---

## 4. Interfaces & Hook Points (Code-Level)

### Hook P1: OODA Goal Context Injection

**File:** `src/secretary/ooda.py`

**Current signature:**
```python
async def run_ooda_cycle(
    event_bus: EventBus,
    run_log: RunLog,
    memory: MemoryStore,
    config: SecretaryConfig,
) -> list[dict]:
    """Generate ad-hoc tasks from events."""
```

**Proposed enhancement:**
```python
async def run_ooda_cycle(
    event_bus: EventBus,
    run_log: RunLog,
    memory: MemoryStore,
    config: SecretaryConfig,
    active_goals: list[dict] | None = None,  # NEW: goal context
) -> list[dict]:
    """Generate ad-hoc tasks from events, optionally aligned to active goals."""
```

**Injection point (in planner prompt):**
```python
if active_goals:
    goal_section = "Active goals:\n" + "\n".join(
        f"  - {g['description']} (id={g['id']}, priority={g.get('priority', 5)})"
        for g in active_goals
    )
    planner_prompt += "\n\n" + goal_section
    planner_prompt += "\nIdentify how these events relate to the active goals. Generate goal-aligned tasks."
```

---

### Hook P2: Predictive Planning During Progress Review

**File:** `src/secretary/goal_progress.py` or `src/secretary/watcher.py`

**Current signature:**
```python
def compute_progress(
    goals: list[dict],
    sub_goal_status: dict,
    run_log: RunLog,
    progress_snapshots: list[dict],
) -> dict[str, GoalProgress]:
    """Compute quantitative progress metrics for all goals."""
```

**Proposed enhancement:**
```python
def compute_progress(
    goals: list[dict],
    sub_goal_status: dict,
    run_log: RunLog,
    progress_snapshots: list[dict],
    forecast_enable: bool = True,  # NEW
) -> dict[str, GoalProgress]:
    """Compute progress + forecast completion dates. Return critical-path tasks if needed."""
    # Compute velocity from recent snapshots
    # Forecast completion date
    # If off-track: identify and return critical-path prerequisite tasks
```

**Injection point (before run_goal_review):**
```python
if forecast_enable and goal.get("deadline"):
    forecast = forecast_completion(goal, progress_data, velocity)
    if forecast["days_to_deadline"] < forecast["days_to_completion"]:
        critical_tasks = extract_critical_path_tasks(goal, goal_state)
        _goal_tasks.extend(critical_tasks)
        log.warning("Goal %s off-track: %d days to deadline, %d days forecast",
                   goal["id"], forecast["days_to_deadline"], forecast["days_to_completion"])
```

---

### Hook P3: Dependency Validation at Step Execution

**File:** `src/secretary/goal_decomposition.py`

**Current signature:**
```python
def get_next_step(
    goal_state: dict,
    sub_goal_id: str,
) -> dict | None:
    """Get the next unexecuted step from a decomposition plan."""
```

**Proposed enhancement:**
```python
def get_next_step(
    goal_state: dict,
    sub_goal_id: str,
    validate_preconditions: bool = True,  # NEW
) -> dict | None:
    """Get next step, optionally validating preconditions. Return None if blocked."""
    nxt = _fetch_next_unexecuted_step(goal_state, sub_goal_id)
    if nxt and validate_preconditions:
        unmet = _get_unmet_prerequisites(nxt, goal_state)
        if unmet:
            # Don't return this step yet—prerequisites not met
            # Return the earliest unmet prerequisite instead
            return _get_prerequisite_step(unmet[0], goal_state)
    return nxt
```

**Injection point (in watcher._run_cycle, line 1510):**
```python
# Layer 27: Check preconditions before executing
nxt = get_next_step(goal_store._state, sg_id, validate_preconditions=True)
if nxt:
    # Check for unmet prerequisites and inject blocking tasks
    unmet_prereqs = get_unmet_prerequisites(nxt, goal_store._state)
    if unmet_prereqs:
        for prereq_id in unmet_prereqs:
            prereq_task = step_to_task(
                _get_step_by_id(goal_store._state, prereq_id),
                sg_id, goal_id, _prerequisite_for=nxt["step_id"]
            )
            _goal_tasks.append(prereq_task)
    else:
        # No blockers—execute the step
        st = step_to_task(nxt, sg_id, goal_id)
        _goal_tasks.append(st)
```

---

## 5. Summary: Enabling Goal-Driven Proactive Task Generation

### Current State (Reactive)
- ✅ Event bus polls external sources
- ✅ OODA loop generates tasks from events
- ✅ Goal planner executes decomposed steps
- ❌ No predictive urgency (steps run in order, not by criticality)
- ❌ No inter-goal dependency awareness
- ❌ No forecast-based task acceleration

### Proposed Enhancements (Proactive)

| Insertion Point | Enhancement | Benefit |
|---|---|---|
| **P1: OODA Goal Context** | Inject active goals into event decision | Tasks aligned to goals, not just event-reactive |
| **P2: Predictive Planning** | Forecast completion vs deadline, inject critical-path tasks | Early detection of off-track goals, time-critical prioritization |
| **P3: Dependency Validation** | Validate step preconditions, insert blockers | Steps execute in dependency order, no waiting on circular dependencies |

### Implementation Roadmap

1. **Week 1:** Implement P1 (OODA goal context injection)
   - Modify run_ooda_cycle() to accept active_goals parameter
   - Enhance planner prompt with goal descriptions
   - Tag generated tasks with goal_id

2. **Week 2:** Implement P2 (predictive planning)
   - Add forecast_completion() function in goal_progress.py
   - Compute critical path via topological sort of step DAG
   - Insert critical-path tasks before review cycle

3. **Week 3:** Implement P3 (dependency validation)
   - Extend get_next_step() with precondition validation
   - Build prerequisite injection logic in watcher._run_cycle()
   - Add circular dependency detection

4. **Week 4:** Testing & optimization
   - Run synthetic goal scenarios to validate insertion points
   - Measure: task volume, execution order, goal completion time
   - A/B test: with/without proactive task generation

---

## 6. Verification Checklist

- [x] Explain flow from event → task generation ✓
- [x] Identify 3+ insertion points for goal-planning logic ✓
- [x] Document hook points (code locations) ✓
- [x] Propose implementation for each insertion point ✓
- [x] Map cycle timeline to code line numbers ✓

**Next Steps:** Implement Hook P1 (OODA goal context) as proof-of-concept.
