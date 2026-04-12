# Gmail Drafts Folder - Tool Budget Recovery Plan

## Current Status
**⚠️ CRITICAL**: Gmail drafts check operations have failed 3x due to tool budget exhaustion (20 >= 20).

## Root Cause Analysis
The issue occurs when `gmail_search('in:drafts')` + `gmail_read()` operations are combined in parallel within the same turn:
- `gmail_search('in:drafts')` returns draft list = **1 tool call**
- `gmail_read()` for each draft (typical: 6+ drafts) = **6+ tool calls**
- **Total: 7+ calls**

With other operations consuming budget in the same session (grep_search, file_read, run_command), the 20-call budget is exhausted before draft operations complete.

## Solution Implemented

### 1. **New Module: `src/secretary/gmail_budget_tracker.py`**
Pre-flight budget checking system:
```python
from src.secretary.gmail_budget_tracker import check_gmail_safe

# Before attempting drafts check:
if check_gmail_safe("drafts_check", budget_remaining=15):
    # Safe to proceed — 15 >= 7 required calls
    search_results = gmail_search('in:drafts')
else:
    # Blocked — insufficient budget
    print("Wait for budget reset before drafts check")
```

**Key Functions**:
- `estimate_gmail_cost(operation)` — Returns expected tool calls
- `check_gmail_safe(operation, budget_remaining)` — Pre-flight validation
- `get_safe_operation_sequence(budget)` — Plan operations within budget
- `format_budget_warning(operation, budget)` — User-friendly messages

### 2. **Documentation: `TOOL_BUDGET_LIMITATION.md`**
Comprehensive guide covering:
- Error timeline and frequency
- Technical root cause analysis
- Budget conflict scenarios
- Mitigation strategies
- Safe operation patterns
- Recovery procedures

### 3. **Test Coverage: `tests/test_gmail_budget_tracker.py`**
16 tests validating:
- Cost estimation accuracy
- Pre-flight check correctness
- Safe operation sequencing
- Real-world failure scenarios
- Budget exhaustion prevention

## Recommended Usage Patterns

### ✅ SAFE: Sequential Operations Across Turns
```
Turn 1: gmail_search('in:drafts')           [1 call]  → 19 remaining
Turn 2: gmail_read() × 6 drafts             [6 calls] → 13 remaining (or fresh session)
Turn 3: Other operations (file, calendar)   [up to 13 calls]
```

### ❌ UNSAFE: Parallel Operations in Same Turn
```
Turn 1: 
  - grep_search × 2                         [2 calls]
  - file_read × 2                           [2 calls]
  - run_command × 2                         [2 calls]
  - gmail_search('in:drafts')               [1 call]
  - gmail_read() × 6                        [6 calls]
  TOTAL: 15 calls (OK)
  
BUT if Turn 1 has additional operations:
Turn 1:
  - file_edit × 3                           [3 calls]
  - run_command × 2                         [2 calls]
  - gmail_search('in:drafts') + reads       [7 calls]
  - file_read × 1                           [1 call]
  TOTAL: 13 calls + pending operations = EXHAUSTION
```

### ✅ OPTIMAL: Budget-Aware Planning
```python
# Before attempting draft-heavy operation:
remaining_budget = get_available_budget()  # 15 calls left

if not check_gmail_safe("drafts_check", remaining_budget):
    safe_ops = get_safe_operation_sequence(remaining_budget)
    # safe_ops might return: ["unread_today", "search"]
    # Skip "drafts_check", proceed with lighter operations
    
    return "Drafts check deferred — insufficient budget."
```

## Integration Points

### For Direct Agent (`src/secretary/direct_agent.py`)
Add budget tracking before parallelizing tool calls:
```python
# Line ~500 in direct_agent.py
def _plan_parallel_tools(task: str, tools_to_call: list[str], budget: int):
    """Filter tools to avoid budget exhaustion."""
    from src.secretary.gmail_budget_tracker import check_gmail_safe
    
    gmail_ops = [t for t in tools_to_call if t.startswith("gmail_")]
    if gmail_ops:
        estimated_cost = sum(estimate_gmail_cost(op) for op in gmail_ops)
        if estimated_cost > budget:
            # Remove expensive Gmail ops, reschedule for next turn
            tools_to_call = [t for t in tools_to_call if not t.startswith("gmail_")]
    
    return tools_to_call
```

### For Memory System
Add budget checkpoint to `src/secretary/memory.py`:
```python
# Track budget usage per session
_BUDGET_SNAPSHOT = {
    "session_id": "uuid",
    "total_calls": 20,
    "used_calls": 13,
    "remaining": 7,
    "last_gmail_cost": 7,
    "safe_for_next_gmail_op": False,
}
```

## Testing Validation
```bash
# Run new budget tracker tests
python -m pytest tests/test_gmail_budget_tracker.py -v

# Expected output:
# test_drafts_check_cost PASSED
# test_unsafe_drafts_check_insufficient_budget PASSED
# test_scenario_initial_batch_exhausts_budget PASSED
# ... (16/16 tests passing)
```

## Rollout Checklist
- [x] Create `gmail_budget_tracker.py` module
- [x] Document limitation in `TOOL_BUDGET_LIMITATION.md`
- [x] Write comprehensive test suite
- [ ] Integrate pre-flight checks into `direct_agent.py`
- [ ] Add budget tracking to memory system
- [ ] Update agent strategy library with budget-aware patterns
- [ ] Monitor future Gmail operations for budget compliance

## Monitoring & Prevention
After implementing these changes, the system will:
1. **Prevent** drafts check failures by validating budget before operations
2. **Recommend** safe operation sequences when budget is constrained
3. **Defer** expensive operations to future sessions/turns
4. **Track** budget consumption patterns to optimize parallelism

## References
- Error Analysis: `TOOL_BUDGET_LIMITATION.md`
- Budget Tracker: `src/secretary/gmail_budget_tracker.py`
- Tests: `tests/test_gmail_budget_tracker.py`
- Pre-loaded data: 5 unread messages (no draft data collected yet due to budget exhaustion)
