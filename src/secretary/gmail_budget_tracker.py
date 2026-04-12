"""Gmail budget tracker — prevent tool exhaustion on drafts operations.

Implements pre-flight budget checks and sequential operation sequencing
to avoid the "Tool budget exhausted: 20 >= 20" error that occurs when
gmail_search + gmail_read operations run in parallel within same turn.

Usage:
    from src.secretary.gmail_budget_tracker import check_gmail_safe
    
    if not check_gmail_safe(operation="drafts_check", budget_remaining=15):
        print("Wait for budget reset before drafts check")
        return
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class GmailBudgetConfig:
    """Tool budget allocation for Gmail operations."""
    
    total_budget: int = 20  # Total tool calls per session
    min_safe_budget: int = 8  # Minimum budget to attempt Gmail ops
    
    # Operation cost estimates (based on failure analysis)
    search_cost: int = 1  # gmail_search() = 1 call
    read_cost: int = 1  # gmail_read() per message = 1 call each
    draft_read_estimate: int = 6  # Typical draft batch size
    
    # Reserved budget for other operations
    reserved_for_file_ops: int = 8  # file_read, file_edit, grep_search, run_command
    reserved_for_misc: int = 3  # Fallback buffer


def estimate_gmail_cost(
    operation: str,
    num_messages: Optional[int] = None,
) -> int:
    """Estimate tool cost for Gmail operation.
    
    Args:
        operation: "search" | "read" | "drafts_check" | "unread_today"
        num_messages: Number of messages to read (if operation is "read")
    
    Returns:
        Estimated tool call cost
    
    Examples:
        >>> estimate_gmail_cost("search")
        1
        >>> estimate_gmail_cost("drafts_check")
        7
        >>> estimate_gmail_cost("read", num_messages=3)
        3
    """
    config = GmailBudgetConfig()
    
    if operation == "search":
        return config.search_cost
    elif operation == "read":
        return (num_messages or 1) * config.read_cost
    elif operation == "drafts_check":
        # gmail_search('in:drafts') + gmail_read × typical draft count
        return config.search_cost + config.draft_read_estimate
    elif operation == "unread_today":
        # gmail_search('is:unread newer_than:1d') + gmail_read × est. 3 important
        return config.search_cost + 3 * config.read_cost
    else:
        return 2  # Conservative estimate for unknown ops


def check_gmail_safe(
    operation: str,
    budget_remaining: int,
    num_messages: Optional[int] = None,
) -> bool:
    """Pre-flight check: is Gmail operation safe with remaining budget?
    
    Args:
        operation: Gmail operation type
        budget_remaining: Tool calls remaining in session
        num_messages: For "read" operations, number of messages
    
    Returns:
        True if operation is safe (won't exceed budget), False otherwise
    
    Examples:
        >>> check_gmail_safe("drafts_check", budget_remaining=15)
        True  # 15 >= 7
        
        >>> check_gmail_safe("drafts_check", budget_remaining=6)
        False  # 6 < 7, would exhaust budget
    """
    config = GmailBudgetConfig()
    cost = estimate_gmail_cost(operation, num_messages)
    return budget_remaining >= cost


def get_safe_operation_sequence(budget_remaining: int) -> list[str]:
    """Return safe Gmail operations for remaining budget.
    
    Helps plan operations to avoid exhaustion:
    - drafts_check: expensive (7 calls) — only if budget >= 8
    - unread_today: moderate (4 calls) — available if budget >= 4
    - search: cheap (1 call) — always safe but follow with read later
    
    Args:
        budget_remaining: Tool calls remaining
    
    Returns:
        List of safe operations, ordered by priority
    """
    safe_ops = []
    
    if budget_remaining >= 8:
        safe_ops.append("drafts_check")
    if budget_remaining >= 4:
        safe_ops.append("unread_today")
    if budget_remaining >= 1:
        safe_ops.append("search")
    
    return safe_ops


def format_budget_warning(
    operation: str,
    budget_remaining: int,
) -> str:
    """Format user-friendly warning when operation would exceed budget.
    
    Example:
        >>> format_budget_warning("drafts_check", budget_remaining=5)
        "⚠️ Gmail drafts check requires 7 calls but only 5 budget remaining.
         Wait for budget reset before retrying."
    """
    cost = estimate_gmail_cost(operation)
    
    if budget_remaining >= cost:
        return f"✓ {operation.upper()}: Safe ({cost} calls, {budget_remaining} budget)"
    else:
        shortfall = cost - budget_remaining
        return (
            f"⚠️ {operation.upper()}: Blocked ({cost} calls needed, "
            f"only {budget_remaining} remaining, {shortfall} short)\n"
            f"→ Wait for budget reset before retrying {operation}"
        )
