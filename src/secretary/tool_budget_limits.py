"""Tool budget management and constraints.

Documents hard limits and strategies for staying within tool budget (20 calls/turn).
This module tracks tool usage patterns to prevent exhaustion failures.
"""

# HARD LIMITS (non-negotiable)
TOOL_CALLS_PER_TURN_MAX = 20
MAX_PARALLEL_TOOLS = 3

# GMAIL OPERATION COSTS (typical tool calls per operation)
GMAIL_SEARCH_COST = 1
GMAIL_READ_COST = 1  # per message
GMAIL_DRAFT_COST = 1
GMAIL_SEND_COST = 1
GMAIL_LIST_DRAFTS_COST = 1

# TYPICAL WORKFLOW COSTS
COST_GMAIL_SEARCH_READ_5_MESSAGES = 1 + (5 * 1)  # = 6 calls
COST_GMAIL_DRAFTS_CHECK_AND_DELETE = 1 + 3 + 3  # list(1) + read(3) + delete(3) = 7 calls
COST_FILE_ANALYSIS = 2 + 2  # grep_search(2) + file_read(2) = 4 calls
COST_FULL_WORKFLOW = 6 + 4 + 3  # gmail(6) + files(4) + commands(3) = 13 calls

# BUDGET ALLOCATION RECOMMENDATIONS
# Turn structure: Allocate 20 tools across 3 categories:
#   - Gmail operations: max 8 calls (1 search + 5 reads + 2 drafts)
#   - File operations: max 6 calls (2 grep + 2 reads + 2 edits)
#   - Commands/tests: max 6 calls (2 commands + 2 python + 2 searches)

# KNOWN FAILURE PATTERN
# ❌ Attempted: gmail_search('in:drafts') + parallel file_read + grep_search → 19+ calls
# ❌ Result: Budget exceeded (20 >= 20)
# ✅ Fix: Defer drafts checks to dedicated turn (1-2 calls only)

GMAIL_DRAFTS_REQUIRES_DEDICATED_TURN = True
"""
Once drafts folder operations exceed budget in a turn, defer to next turn.
Dedicate entire turn to: gmail_list_drafts + selective gmail_read + cleanup.
"""

def estimate_call_count(operations: list[str]) -> int:
    """Estimate total tool calls for a set of operations.
    
    Args:
        operations: List of operation names (e.g., ["gmail_search", "file_read"])
    
    Returns:
        Estimated call count
    """
    costs = {
        "gmail_search": 1,
        "gmail_read": 1,
        "gmail_draft": 1,
        "gmail_send": 1,
        "gmail_list_drafts": 1,
        "file_read": 1,
        "file_write": 1,
        "file_list": 1,
        "file_edit": 1,
        "grep_search": 1,
        "run_command": 1,
        "run_python": 1,
        "calendar_today": 1,
    }
    return sum(costs.get(op, 1) for op in operations)


def can_fit_in_budget(current_count: int, new_operations: list[str]) -> bool:
    """Check if new operations fit within remaining budget.
    
    Args:
        current_count: Current tool calls used in turn
        new_operations: Proposed operations to add
    
    Returns:
        True if total <= TOOL_CALLS_PER_TURN_MAX
    """
    additional = estimate_call_count(new_operations)
    return (current_count + additional) <= TOOL_CALLS_PER_TURN_MAX


if __name__ == "__main__":
    # Example: Check if drafts workflow fits
    gmail_ops = ["gmail_search", "gmail_read", "gmail_read", "gmail_read"]
    file_ops = ["grep_search", "file_read"]
    
    total = estimate_call_count(gmail_ops + file_ops)
    print(f"Gmail ops: {estimate_call_count(gmail_ops)} calls")
    print(f"File ops: {estimate_call_count(file_ops)} calls")
    print(f"Total: {total} calls")
    print(f"Fits in budget: {total <= TOOL_CALLS_PER_TURN_MAX}")
