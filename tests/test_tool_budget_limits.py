"""Tests for the tool_budget_limits module.

Covers: constants, estimate_call_count(), can_fit_in_budget(),
typical workflow cost constants, and edge cases.
"""
from __future__ import annotations

import pytest

from secretary.tool_budget_limits import (
    TOOL_CALLS_PER_TURN_MAX,
    MAX_PARALLEL_TOOLS,
    GMAIL_SEARCH_COST,
    GMAIL_READ_COST,
    GMAIL_SEND_COST,
    GMAIL_DRAFT_COST,
    GMAIL_LIST_DRAFTS_COST,
    COST_GMAIL_SEARCH_READ_5_MESSAGES,
    COST_GMAIL_DRAFTS_CHECK_AND_DELETE,
    COST_FILE_ANALYSIS,
    COST_FULL_WORKFLOW,
    GMAIL_DRAFTS_REQUIRES_DEDICATED_TURN,
    estimate_call_count,
    can_fit_in_budget,
)


# ── Constants ──────────────────────────────────────────────────────────


class TestConstants:
    """Verify hard limits and cost constants are correct."""

    def test_tool_calls_per_turn_max(self):
        assert TOOL_CALLS_PER_TURN_MAX == 20

    def test_max_parallel_tools(self):
        assert MAX_PARALLEL_TOOLS == 3

    def test_gmail_search_cost(self):
        assert GMAIL_SEARCH_COST == 1

    def test_gmail_read_cost(self):
        assert GMAIL_READ_COST == 1

    def test_gmail_send_cost(self):
        assert GMAIL_SEND_COST == 1

    def test_gmail_draft_cost(self):
        assert GMAIL_DRAFT_COST == 1

    def test_gmail_list_drafts_cost(self):
        assert GMAIL_LIST_DRAFTS_COST == 1

    def test_cost_gmail_search_read_5_messages(self):
        """1 search + 5 reads = 6."""
        assert COST_GMAIL_SEARCH_READ_5_MESSAGES == 6

    def test_cost_gmail_drafts_check_and_delete(self):
        """list(1) + read(3) + delete(3) = 7."""
        assert COST_GMAIL_DRAFTS_CHECK_AND_DELETE == 7

    def test_cost_file_analysis(self):
        """grep_search(2) + file_read(2) = 4."""
        assert COST_FILE_ANALYSIS == 4

    def test_cost_full_workflow(self):
        """gmail(6) + files(4) + commands(3) = 13."""
        assert COST_FULL_WORKFLOW == 13

    def test_gmail_drafts_requires_dedicated_turn(self):
        assert GMAIL_DRAFTS_REQUIRES_DEDICATED_TURN is True


# ── estimate_call_count ────────────────────────────────────────────────


class TestEstimateCallCount:
    """Tests for estimate_call_count()."""

    def test_empty_operations_list(self):
        assert estimate_call_count([]) == 0

    def test_single_gmail_search(self):
        assert estimate_call_count(["gmail_search"]) == 1

    def test_single_gmail_read(self):
        assert estimate_call_count(["gmail_read"]) == 1

    def test_single_gmail_send(self):
        assert estimate_call_count(["gmail_send"]) == 1

    def test_single_gmail_draft(self):
        assert estimate_call_count(["gmail_draft"]) == 1

    def test_single_gmail_list_drafts(self):
        assert estimate_call_count(["gmail_list_drafts"]) == 1

    def test_single_file_read(self):
        assert estimate_call_count(["file_read"]) == 1

    def test_single_file_write(self):
        assert estimate_call_count(["file_write"]) == 1

    def test_single_file_list(self):
        assert estimate_call_count(["file_list"]) == 1

    def test_single_file_edit(self):
        assert estimate_call_count(["file_edit"]) == 1

    def test_single_grep_search(self):
        assert estimate_call_count(["grep_search"]) == 1

    def test_single_run_command(self):
        assert estimate_call_count(["run_command"]) == 1

    def test_single_run_python(self):
        assert estimate_call_count(["run_python"]) == 1

    def test_single_calendar_today(self):
        assert estimate_call_count(["calendar_today"]) == 1

    def test_unknown_operation_defaults_to_1(self):
        """Unknown ops should cost 1 (default)."""
        assert estimate_call_count(["totally_unknown_tool"]) == 1

    def test_multiple_unknown_operations(self):
        assert estimate_call_count(["foo", "bar", "baz"]) == 3

    def test_mixed_known_and_unknown(self):
        ops = ["gmail_search", "unknown_tool", "file_read"]
        assert estimate_call_count(ops) == 3

    def test_gmail_search_plus_5_reads(self):
        """Typical gmail workflow: 1 search + 5 reads = 6."""
        ops = ["gmail_search"] + ["gmail_read"] * 5
        assert estimate_call_count(ops) == 6

    def test_full_workflow_estimate(self):
        """Simulate full workflow: gmail(6) + files(4) + commands(3) = 13."""
        gmail_ops = ["gmail_search"] + ["gmail_read"] * 5
        file_ops = ["grep_search"] * 2 + ["file_read"] * 2
        cmd_ops = ["run_command"] * 2 + ["run_python"]
        all_ops = gmail_ops + file_ops + cmd_ops
        assert estimate_call_count(all_ops) == 13

    def test_duplicate_operations_counted_each(self):
        """Each operation counts individually, even duplicates."""
        ops = ["gmail_read"] * 10
        assert estimate_call_count(ops) == 10

    def test_returns_int(self):
        result = estimate_call_count(["gmail_search", "file_read"])
        assert isinstance(result, int)

    def test_twenty_operations_hits_max(self):
        """20 operations of cost 1 should equal the budget limit."""
        ops = ["gmail_read"] * 20
        assert estimate_call_count(ops) == TOOL_CALLS_PER_TURN_MAX

    def test_exceeds_budget_limit(self):
        """21 operations exceeds the per-turn max."""
        ops = ["gmail_read"] * 21
        assert estimate_call_count(ops) > TOOL_CALLS_PER_TURN_MAX


# ── can_fit_in_budget ──────────────────────────────────────────────────


class TestCanFitInBudget:
    """Tests for can_fit_in_budget()."""

    def test_zero_current_single_op_fits(self):
        assert can_fit_in_budget(0, ["gmail_search"]) is True

    def test_zero_current_empty_ops_fits(self):
        assert can_fit_in_budget(0, []) is True

    def test_at_limit_empty_ops_fits(self):
        """20 used + 0 new = 20 ≤ 20."""
        assert can_fit_in_budget(20, []) is True

    def test_at_limit_one_op_exceeds(self):
        """20 used + 1 new = 21 > 20."""
        assert can_fit_in_budget(20, ["gmail_search"]) is False

    def test_one_below_limit_one_op_fits(self):
        """19 used + 1 new = 20 ≤ 20."""
        assert can_fit_in_budget(19, ["gmail_search"]) is True

    def test_fits_exactly_at_max(self):
        """0 used + 20 ops = 20 ≤ 20."""
        ops = ["file_read"] * 20
        assert can_fit_in_budget(0, ops) is True

    def test_exceeds_by_one(self):
        """0 used + 21 ops = 21 > 20."""
        ops = ["file_read"] * 21
        assert can_fit_in_budget(0, ops) is False

    def test_half_budget_used(self):
        """10 used + 5 ops = 15 ≤ 20."""
        assert can_fit_in_budget(10, ["gmail_read"] * 5) is True

    def test_half_budget_overflow(self):
        """10 used + 11 ops = 21 > 20."""
        assert can_fit_in_budget(10, ["gmail_read"] * 11) is False

    def test_returns_bool(self):
        result = can_fit_in_budget(5, ["gmail_search"])
        assert isinstance(result, bool)

    def test_typical_gmail_workflow_from_zero(self):
        """Search + 5 reads = 6 calls from 0 → fits."""
        ops = ["gmail_search"] + ["gmail_read"] * 5
        assert can_fit_in_budget(0, ops) is True

    def test_typical_gmail_workflow_from_14(self):
        """14 used + 6 gmail = 20 → fits exactly."""
        ops = ["gmail_search"] + ["gmail_read"] * 5
        assert can_fit_in_budget(14, ops) is True

    def test_typical_gmail_workflow_from_15(self):
        """15 used + 6 gmail = 21 → exceeds."""
        ops = ["gmail_search"] + ["gmail_read"] * 5
        assert can_fit_in_budget(15, ops) is False

    def test_drafts_workflow_from_zero(self):
        """7 calls from 0 → fits."""
        # list(1) + read(3) + delete(3) = 7
        ops = ["gmail_list_drafts"] + ["gmail_read"] * 3 + ["gmail_draft"] * 3
        assert can_fit_in_budget(0, ops) is True

    def test_drafts_workflow_from_14(self):
        """14 used + 7 = 21 → exceeds (why dedicated turn is needed)."""
        ops = ["gmail_list_drafts"] + ["gmail_read"] * 3 + ["gmail_draft"] * 3
        assert can_fit_in_budget(14, ops) is False

    def test_full_workflow_from_zero(self):
        """13 calls from 0 → fits."""
        gmail_ops = ["gmail_search"] + ["gmail_read"] * 5
        file_ops = ["grep_search"] * 2 + ["file_read"] * 2
        cmd_ops = ["run_command"] * 2 + ["run_python"]
        assert can_fit_in_budget(0, gmail_ops + file_ops + cmd_ops) is True

    def test_full_workflow_from_8(self):
        """8 + 13 = 21 → exceeds."""
        gmail_ops = ["gmail_search"] + ["gmail_read"] * 5
        file_ops = ["grep_search"] * 2 + ["file_read"] * 2
        cmd_ops = ["run_command"] * 2 + ["run_python"]
        assert can_fit_in_budget(8, gmail_ops + file_ops + cmd_ops) is False

    def test_unknown_ops_default_cost_1(self):
        """Unknown operations default to cost 1."""
        assert can_fit_in_budget(19, ["some_new_tool"]) is True
        assert can_fit_in_budget(20, ["some_new_tool"]) is False


# ── Integration / Consistency ──────────────────────────────────────────


class TestConsistency:
    """Verify constants and functions are consistent with each other."""

    def test_search_read_5_matches_estimate(self):
        """COST_GMAIL_SEARCH_READ_5_MESSAGES should match estimate."""
        ops = ["gmail_search"] + ["gmail_read"] * 5
        assert estimate_call_count(ops) == COST_GMAIL_SEARCH_READ_5_MESSAGES

    def test_file_analysis_matches_estimate(self):
        """COST_FILE_ANALYSIS should match estimate for 2 grep + 2 read."""
        ops = ["grep_search"] * 2 + ["file_read"] * 2
        assert estimate_call_count(ops) == COST_FILE_ANALYSIS

    def test_can_fit_uses_estimate_internally(self):
        """can_fit_in_budget should give same answer as manual check."""
        ops = ["gmail_search", "gmail_read", "file_read"]
        current = 15
        manual = (current + estimate_call_count(ops)) <= TOOL_CALLS_PER_TURN_MAX
        assert can_fit_in_budget(current, ops) == manual

    def test_boundary_consistency(self):
        """At exactly TOOL_CALLS_PER_TURN_MAX, budget is met but not exceeded."""
        n = TOOL_CALLS_PER_TURN_MAX
        assert can_fit_in_budget(n, []) is True       # n + 0 = 20
        assert can_fit_in_budget(n - 1, ["x"]) is True  # 19 + 1 = 20
        assert can_fit_in_budget(n, ["x"]) is False     # 20 + 1 = 21
