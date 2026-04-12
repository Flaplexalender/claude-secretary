# Goal-Planner Injection Points Analysis

## Executive Summary

The project has a **reactive OODA loop** (events → tasks) and a **proactive Goal Planner** (goals → tasks).
This analysis identifies **3 specific code locations** where goal-planning logic can hook into the reactive pipeline.

---

## Event → Task Flow (2–3 sentences with function names)

**GmailEventSource.poll()** (event_bus.py:~121) detects new emails and emits Event objects with dedup keys → 
**EventBus.poll_all()** (event_bus.py:~235) collects and deduplicates events from all sources (Gmail, Calendar, FileChange) → 
**run_ooda_cycle()** (ooda.py:~152–173) passes event summaries to Haiku LLM which generates ad-hoc task JSON → 
**watcher cycle** (watcher.py:~1395–1410) extends the main task queue with OODA results and executes them via direct_agent.run().

---

## 3 Specific Goal-Planning Injection Points

### POINT 1: Event Filtering by Goal Relevance
**Location:** `src/secretary/event_bus.py:235` (in `EventBus.poll_all()` return statement)

**Function:** `EventBus.poll_all() → list[Event]`

**What to inject:**
- After deduplication, before returning event list, annotate each event with which **active goals** it relates to.
- Goal planner can then pre-filter events by relevance, avoiding irrelevant events in OODA decision.
- Reduces OODA token usage (doesn't process unrelated emails/calendar entries).

**Pseudocode:**
```python
async def poll_all(self, active_goals: list[Goal] = None) -> list[Event]:
    events = [... collect & dedup from sources ...]
    if active_goals:
        for event in events:
            event.payload['related_goals'] = [
                g.id for g in match_event_to_goals(event, active_goals)
            ]
    return events  # <-- INJECTION POINT: Enhanced event.payload with goal context
```

**Verification:** Can query event.payload['related_goals'] downstream.

---

### POINT 2: OODA Prompt Injection with Active Goals
**Location:** `src/secretary/ooda.py:117` (in `_build_ooda_prompt()` call within `run_ooda_cycle()`)

**Function:** `_build_ooda_prompt()` receives events; add `active_goals` parameter

**What to inject:**
- Pass the list of active goals + current progress to the planner prompt.
- Haiku planner can then:
  - Decompose goals into task steps (e.g., "Email research for Goal[marketing-audit] → search competitors").
  - Tag generated tasks with goal_id and milestone markers.
  - Recognize if an event advances a goal (credit assignment).
- Enables **goal-aware reactive planning**: events are routed toward active goals, not just answered in isolation.

**Pseudocode:**
```python
async def run_ooda_cycle(
    event_bus, run_log, memory, config,
    active_goals: list[Goal] = None  # <-- NEW PARAMETER
) -> list[dict[str, Any]]:
    events = await event_bus.poll_all(active_goals)  # Pass goals to event filtering
    prompt = _build_ooda_prompt(
        events, recent_log, memory,
        active_goals=active_goals  # <-- INJECTION: Goals context in prompt
    )
    # Haiku sees: "Active goals: [goal1, goal2]. Recent events: [...]. React."
    tasks = await planner.decide(prompt)
    return tasks
```

**Verification:** Planner response includes goal_id in task dict, demonstrating awareness.

---

### POINT 3: Task Prioritization & Enrichment Post-OODA
**Location:** `src/secretary/watcher.py:~1400` (right after `tasks.extend(ooda_tasks)`)

**Function:** Task queue integration loop in watcher cycle

**What to inject:**
- After OODA returns tasks, call a **goal-aware prioritizer** to:
  - Re-order tasks by goal urgency vs event importance.
  - Assign goal_id + milestone metadata to each task (enables credit assignment).
  - Skip tasks that would conflict with goal constraints (e.g., don't schedule deep work on low-tier goals if high-tier goal is stalled).
  - Enable **goal credit assignment**: track which tasks advance which goals in run_log.
- This allows goal progress metrics (goals.py:compute_progress) to correlate executed tasks → goal completion.

**Pseudocode:**
```python
# In watcher main cycle, after OODA results:
ooda_tasks = await run_ooda_cycle(event_bus, run_log, memory, config)
if ooda_tasks:
    log.info("OODA injected %d ad-hoc task(s)", len(ooda_tasks))
    
    # <-- INJECTION POINT: Goal-aware enrichment
    if active_goals:
        ooda_tasks = goal_planner.enrich_with_goal_context(
            ooda_tasks, active_goals, run_log
        )
        ooda_tasks = goal_planner.reorder_by_goal_urgency(
            ooda_tasks, active_goals, goal_store.progress_snapshots
        )
    
    all_tasks.extend(ooda_tasks)  # Then add to queue
```

**Verification:** 
- Each task in run_log contains `goal_id` field.
- Progress metrics show task→goal linkage.
- Stalled goals trigger task prioritization boost.

---

## Integration Summary

| Point | When | What Changes | Benefit |
|-------|------|--------------|---------|
| **1: event_bus.py:235** | Event collection | Add `related_goals` to event.payload | Filter irrelevant events early; reduce OODA token cost |
| **2: ooda.py:117** | OODA prompt build | Pass `active_goals` to _build_ooda_prompt() | Haiku planner sees goals; generates goal-aligned tasks; tags tasks with goal_id |
| **3: watcher.py:1400** | Task queue integration | Call goal_planner.enrich_with_goal_context() | Re-order by goal urgency; credit assignment; stalled-goal escalation |

All three hooks are **non-breaking**: existing code path works unchanged if goals are None/empty.

---

## Implementation Roadmap

1. **Phase 1:** Add goal_ids to event.payload (Point 1).  
   File: `src/secretary/event_bus.py`  
   Lines: 230–240 (in poll_all method)

2. **Phase 2:** Extend OODA prompt with goal context (Point 2).  
   File: `src/secretary/ooda.py`  
   Lines: 115–120 (function signature), 117–145 (prompt build)

3. **Phase 3:** Add task enrichment post-OODA (Point 3).  
   File: `src/secretary/watcher.py`  
   Lines: 1390–1410 (cycle integration)

All tie into existing `goals.py` infrastructure for state tracking and progress metrics.
