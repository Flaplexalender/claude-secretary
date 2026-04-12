"""Test gmail_budget_tracker — verify pre-flight checks prevent exhaustion."""

import pytest
from secretary.gmail_budget_tracker import (
    estimate_gmail_cost,
    check_gmail_safe,
    get_safe_operation_sequence,
    format_budget_warning,
)


class TestGmailBudgetEstimates:
    """Test cost estimation for various Gmail operations."""
    
    def test_search_cost(self):
        assert estimate_gmail_cost("search") == 1
    
    def test_read_cost(self):
        assert estimate_gmail_cost("read", num_messages=1) == 1
        assert estimate_gmail_cost("read", num_messages=5) == 5
    
    def test_drafts_check_cost(self):
        # search (1) + read × 6 typical drafts = 7
        assert estimate_gmail_cost("drafts_check") == 7
    
    def test_unread_today_cost(self):
        # search (1) + read × 3 important = 4
        assert estimate_gmail_cost("unread_today") == 4
    
    def test_unknown_operation_conservative(self):
        assert estimate_gmail_cost("unknown_op") == 2


class TestGmailBudgetPreFlight:
    """Test pre-flight budget checks."""
    
    def test_safe_drafts_check_adequate_budget(self):
        # 15 budget >= 7 cost → safe
        assert check_gmail_safe("drafts_check", budget_remaining=15) is True
    
    def test_unsafe_drafts_check_insufficient_budget(self):
        # 5 budget < 7 cost → unsafe
        assert check_gmail_safe("drafts_check", budget_remaining=5) is False
    
    def test_safe_unread_with_medium_budget(self):
        # 4 budget >= 4 cost → safe
        assert check_gmail_safe("unread_today", budget_remaining=4) is True
    
    def test_safe_search_minimal_budget(self):
        # 1 budget >= 1 cost → safe
        assert check_gmail_safe("search", budget_remaining=1) is True
    
    def test_boundary_exact_budget(self):
        # When budget == cost, operation is safe
        assert check_gmail_safe("unread_today", budget_remaining=4) is True
        assert check_gmail_safe("unread_today", budget_remaining=3) is False


class TestSafeOperationSequence:
    """Test planning safe operation sequences."""
    
    def test_all_ops_available_with_high_budget(self):
        safe = get_safe_operation_sequence(budget_remaining=20)
        assert "drafts_check" in safe
        assert "unread_today" in safe
        assert "search" in safe
    
    def test_medium_budget_excludes_expensive_ops(self):
        safe = get_safe_operation_sequence(budget_remaining=6)
        assert "drafts_check" not in safe
        assert "unread_today" in safe  # 4 <= 6
        assert "search" in safe
    
    def test_low_budget_only_search(self):
        safe = get_safe_operation_sequence(budget_remaining=1)
        assert "drafts_check" not in safe
        assert "unread_today" not in safe
        assert "search" in safe
    
    def test_zero_budget_no_ops(self):
        safe = get_safe_operation_sequence(budget_remaining=0)
        assert len(safe) == 0


class TestBudgetWarningFormat:
    """Test user-friendly warning messages."""
    
    def test_safe_operation_message(self):
        msg = format_budget_warning("search", budget_remaining=10)
        assert "✓" in msg
        assert "Safe" in msg
    
    def test_unsafe_operation_message(self):
        msg = format_budget_warning("drafts_check", budget_remaining=5)
        assert "⚠️" in msg
        assert "Blocked" in msg
        assert "Wait for budget reset" in msg
    
    def test_shows_shortfall(self):
        # drafts_check costs 7, budget is 3, shortfall is 4
        msg = format_budget_warning("drafts_check", budget_remaining=3)
        assert "4 short" in msg


class TestBudgetRealWorldScenarios:
    """Test real-world budget exhaustion scenarios."""
    
    def test_scenario_initial_batch_exhausts_budget(self):
        """Simulates: grep_search×2 + file_read×2 + run_command×2 = 6 calls.
        
        With only 14 remaining, drafts_check (7 calls) is still safe.
        But if followed by more operations, budget exhausts quickly.
        """
        budget = 20
        budget -= 6  # Initial analysis batch
        assert budget == 14
        
        # Check 1: drafts_check is safe
        assert check_gmail_safe("drafts_check", budget_remaining=budget) is True
        
        # Check 2: After drafts_check, 7 calls remain (exactly at boundary)
        budget -= 7
        assert budget == 7
        
        # Check 3: drafts_check costs exactly 7, so 7 remaining is exactly enough
        assert check_gmail_safe("drafts_check", budget_remaining=budget) is True
        
        # Check 3b: But 6 remaining would be unsafe
        assert check_gmail_safe("drafts_check", budget_remaining=6) is False
        
        # Check 4: But unread_today (cost 4) would still be safe
        assert check_gmail_safe("unread_today", budget_remaining=budget) is True
    
    def test_scenario_sequential_gmail_operations_safe(self):
        """Simulates safe operation sequence:
        Turn 1: gmail_search('in:drafts') = 1 call
        Turn 2: gmail_read() × drafts found = 6 calls
        """
        budget = 20
        
        # Turn 1: Just search
        assert check_gmail_safe("search", budget_remaining=budget) is True
        budget -= 1
        
        # Turn 2: Sequential reads (new session/turn, budget reset assumed)
        # OR continuing: need sequential reads
        assert budget == 19
        
        # If doing 6 reads sequentially in same turn
        assert check_gmail_safe("read", budget_remaining=budget, num_messages=6) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
