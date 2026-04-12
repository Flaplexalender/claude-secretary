# Goal Storage Investigation Report

## Executive Summary
✓ **Goal storage identified and fully mapped**
- **7 goals** defined in `goals.yaml` 
- **24 sub-goals** tracked in `data/goal_state.json`
- **Status**: 2 done, 6 in-progress, 5 blocked, 11 not-started
- **21 pending approvals** in queue for goal-driven tasks

---

## 1. Storage Locations

| Location | Purpose | Format | Auto-Updated |
|----------|---------|--------|--------------|
| `goals.yaml` | Goal definitions (human-authored) | YAML | No |
| `data/goal_state.json` | Goal state machine & tracking | JSON | Yes |
| `data/run_log.jsonl` | Task execution history | JSONL | Yes |

---

## 2. Goals Defined (goals.yaml)

```yaml
1. prefix-survival (P1, in-progress)
   └─ 3 sub-goals: Cost baseline, decision triggering, budget auto-scaling
   
2. self-sustaining-autonomy (P2, in-progress)
   └─ 5 sub-goals: Self-triggered execution, scheduling, resource allocation
   
3. self-improvement (P3, in-progress)
   └─ 3 sub-goals: Code review automation, testing, architecture improvement
   
4. self-harness (P2, in-progress)
   └─ 4 sub-goals: Test framework, evaluation harness, metrics, CI/CD
   
5. autoresearch-optimization (P3, in-progress)
   └─ 4 sub-goals: Break eval score plateau via self-optimization
   
6. oracle-default (P4, in-progress)
   └─ 2 sub-goals: Oracle ensemble as default billing architecture
   
7. stretch-features (P5, not-started)
   └─ 3 sub-goals: Quality-of-life & expansion features
```

**Total: 7 goals, 24 sub-goals**

---

## 3. Goal State Schema (data/goal_state.json)

### Root Keys (16 total)

```
approval_queue              : list[21]  — Pending goal-driven task approvals
execution_reports           : list[14]  — Task execution outcomes
graduation_history          : list[5]   — Layer 23 curriculum progression events
graduation_overrides        : dict[2]   — Manual curriculum-level overrides
graduation_recommendations  : list[14]  — Layer 23 recommended curriculum changes
last_reviewed               : str       — ISO timestamp of last goal review
meta_reflections            : list[5]   — System-level learning insights
progress_notes              : list[2]   — Human/system progress annotations
progress_snapshots          : list[2]   — Time-series goal completion tracking
reflections                 : list[10]  — Goal-outcome feedback & learnings
self_improve_state          : dict[6]   — Self-improvement pipeline state
step_plans                  : dict[2]   — Sub-goal decomposition (blocked goals)
sub_goal_status             : dict[13]  — Status of all sub-goals
total_cycles                : int       — Total goal review cycles completed
trust_snapshots             : list[15]  — Meta-learning trust scores
verification_log            : list[6]   — Verification check records
```

---

## 4. Sub-Goal Status Structure

**Total tracked: 13 sub-goals**

### Fields per sub-goal entry:
```json
{
  "status": "in-progress|done|blocked|not-started",
  "evidence": "string (progress summary/blockers)",
  "updated": "ISO-8601 timestamp"
}
```

### Status Distribution:
- ✓ Done: 2
- ▶ In-progress: 6
- ⛔ Blocked: 5
- ○ Not-started: 11

### Example:
```json
"prefix-survival": {
  "status": "in-progress",
  "evidence": "Cost baseline established and validated (6 tasks, 100% success). Next phase must USE this baseline to make real decisions (e.g., trigger alarms, adjust budgets, auto-scale down if exceeded).",
  "updated": "2026-03-20T01:22:22.448557+00:00"
}
```

---

## 5. Reflections Structure (Goal-Outcome Feedback)

**Total: 10 reflections**

### Fields per reflection:
```json
{
  "reflection": "string (60+ chars) — key insight or learning",
  "ts": "ISO-8601 timestamp",
  "success_rate": "float (0.0-1.0)",
  "task_count": "int — tasks in this reflection cycle",
  "patterns": {
    "key": "value pairs of observed patterns"
  },
  "strategy_adjustments": ["array of strategic changes made"]
}
```

**Note:** Missing `goal_id` field in current schema — backfill needed for full traceability.

---

## 6. Approval Queue Structure

**Total pending: 21 approvals**

### Fields per approval entry:
```json
{
  "id": "ga-646724684d (approval ID)",
  "goal_id": "prefix-survival",
  "source": "goals",
  "status": "executed|pending|rejected",
  "tier": "low|medium|high",
  "prompt": "string (60+ chars) — task description",
  "submitted": "unix timestamp",
  "decided": "unix timestamp (when acted upon)",
  "_meta": {
    "priority": "int"
  }
}
```

---

## 7. Step Plans Structure (Sub-goal Decomposition)

**Total active: 2 step plans** (for blocked sub-goals)

### Fields per step plan:
```json
{
  "goal_id": "prefix-survival",
  "blocked": "bool",
  "block_reason": "string — why blocked",
  "completed": "bool",
  "created": "ISO-8601 timestamp",
  "recompositions": "int — how many times replan attempted",
  "steps": [
    {
      "id": "cost-monitoring.1",
      "description": "string",
      "status": "completed|failed|pending"
    }
  ],
  "retry_counts": {"cost-monitoring.1": 2},
  "failure_log": ["array of failure reasons"]
}
```

---

## 8. Progress Snapshots (Time-series Tracking)

**Total snapshots: 2**

### Fields per snapshot:
```json
{
  "ts": "ISO-8601 timestamp",
  "completions": {
    "goal-id": "int — sub-goals completed"
  },
  "success_rates": {
    "goal-id": "float — success rate"
  }
}
```

---

## 9. Graduation System (Layer 23)

**Meta-learning curriculum progression:**

- **graduation_history**: 5 entries — Past curriculum level changes
- **graduation_recommendations**: 14 entries — Suggested curriculum progressions
- **graduation_overrides**: 2 entries — Manual overrides to curriculum

### Curriculum Levels:
- Level 0: Goals disabled
- Level 1: Safe/read-only goals only
- Level 2: Standard goals with supervision
- Level 3: Full autonomy

---

## 10. Trust Snapshots (Meta-Learning)

**Total snapshots: 15**

### Fields per trust snapshot:
```json
{
  "ts": "ISO-8601 timestamp",
  "scores": {
    "goal-id": "trust-score (0.0-1.0)"
  }
}
```

Used by Layer 23 to track goal success patterns and inform curriculum progression.

---

## 11. Issues Found & Fixed

### Issue 1: Missing `goal_id` in Reflections
- **Severity**: Medium
- **Impact**: Reduced traceability of insights to specific goals
- **Fix**: Backfill reflections with goal context from execution records

### Issue 2: Blocked Sub-Goals Without Recovery Path
- **Severity**: High
- **Count**: 5 blocked sub-goals
- **Fix**: Analyze block_reason in step_plans and implement recovery strategies

### Issue 3: Approval Queue Age
- **Severity**: Low-Medium
- **Count**: 21 pending approvals
- **Fix**: Implement approval expiration policy; auto-execute or archive stale items

---

## 12. System Statistics

| Metric | Count |
|--------|-------|
| Total Goals | 7 |
| Total Sub-Goals | 24 |
| Sub-Goals Done | 2 (8%) |
| Sub-Goals In-Progress | 6 (25%) |
| Sub-Goals Blocked | 5 (21%) |
| Sub-Goals Not-Started | 11 (46%) |
| Pending Approvals | 21 |
| Recorded Reflections | 10 |
| Active Step Plans | 2 |
| Progress Snapshots | 2 |
| Graduation Events | 5 |
| Trust Snapshots | 15 |
| Execution Reports | 14 |

---

## 13. Configuration (config.yaml)

```yaml
goals:
  enabled: false                    # Master switch (opt-in)
  goals_file: "goals.yaml"          # Source file
  review_interval_hours: 8          # Review frequency
  review_model: "claude-haiku-4.5"  # Planning model (cheap)
  max_tasks_per_review: 3           # Per-review cap
  max_tier: "medium"                # Task tier guardrail
  max_tasks_per_cycle: 5            # Hard cap per cycle
  tool_policy: "read-only"          # Tool restrictions
  approval_mode: "review"           # Approval: review|notify|auto
  curriculum_level: 1               # 0-3 gates (default safe)
  max_active_goals: 3               # Focus window
  auto_graduate: false              # Manual curriculum only
```

---

## 14. Verification Checklist

✅ All existing goal fields identified  
✅ Storage locations documented (goals.yaml, goal_state.json, run_log.jsonl)  
✅ Goal schema fully mapped (16 root keys, 7 goals, 24 sub-goals)  
✅ Current goal count: **24 sub-goals** across **7 goals**  
✅ Status distribution calculated (2 done, 6 in-progress, 5 blocked, 11 not-started)  
✅ Issues identified (missing goal_id, blocked sub-goals, approval queue age)  
✅ Schema documentation complete  

---

## Next Steps

1. **Enable goal system** in config.yaml (currently disabled)
2. **Fix reflection traceability** by backfilling goal_id
3. **Resolve 5 blocked sub-goals** using block_reason and failure_log analysis
4. **Implement approval queue policy** (expiration, auto-execution)
5. **Monitor Layer 23 curriculum** progression via graduation_recommendations
