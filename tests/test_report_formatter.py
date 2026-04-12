"""Minimal reference test harness: Report formatter (completed goal).

This is a TEMPLATE specification for LLM-generated test suites.
It demonstrates the standard structure, assertion patterns, and ground-truth data.

Goal: Format task results into a human-readable report.
Inputs: task name, status, duration, tools used, output text.
Outputs: formatted markdown report with summary, metrics, and details.

Template structure:
  1. Fixtures: reusable ground-truth data (minimal datasets)
  2. Test cases: one per behavior (success, edge case, error)
  3. Assertions: 2-4 per test, checking structure + content + invariants
  4. Docstrings: explain the ground truth and why it matters
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


# ─────────────────────────────────────────────────────────────
# GROUND-TRUTH DATA (minimal, reusable test fixtures)
# ─────────────────────────────────────────────────────────────

@dataclass
class ReportTestCase:
    """Ground-truth: a minimal report generation scenario."""
    name: str
    task_name: str
    status: str  # "success" | "error" | "partial"
    duration_sec: float
    tools_used: list[str]
    output_text: str
    expected_contains: list[str]  # assertions: strings that MUST appear


GROUND_TRUTH_CASES = [
    ReportTestCase(
        name="simple_success",
        task_name="Check email",
        status="success",
        duration_sec=4.2,
        tools_used=["gmail_search", "gmail_read"],
        output_text="Found 3 unread emails from today.",
        expected_contains=[
            "Check email",           # task name
            "SUCCESS",               # status (uppercase in output)
            "4.2s",                  # duration (humanized)
            "2 tools",               # tool count summary
            "gmail_search",          # first tool
        ],
    ),
    ReportTestCase(
        name="partial_with_error",
        task_name="Analyze logs",
        status="partial",
        duration_sec=12.8,
        tools_used=["file_read", "grep_search", "run_python"],
        output_text="Found 42 errors. Failed to connect to remote server.",
        expected_contains=[
            "Analyze logs",
            "PARTIAL",               # status (uppercase in output)
            "12.8s",
            "3 tools",
        ],
    ),
    ReportTestCase(
        name="error_state",
        task_name="Sync database",
        status="error",
        duration_sec=2.1,
        tools_used=["run_command"],
        output_text="Connection timeout after 2s.",
        expected_contains=[
            "Sync database",
            "ERROR",                 # status (uppercase in output)
            "2.1s",
            "1 tool",
        ],
    ),
]


# ─────────────────────────────────────────────────────────────
# MINIMAL FORMATTER IMPLEMENTATION (ground truth: what we test)
# ─────────────────────────────────────────────────────────────

class ReportFormatter:
    """Simple report formatter (the SUT — System Under Test)."""

    @staticmethod
    def format_report(
        task_name: str,
        status: str,
        duration_sec: float,
        tools_used: list[str],
        output_text: str,
    ) -> str:
        """Format a task result into a markdown report.

        Args:
            task_name: Name of the completed task.
            status: One of "success", "partial", "error".
            duration_sec: Execution time in seconds.
            tools_used: List of tool names used (e.g., ["gmail_search", "grep_search"]).
            output_text: Human-readable result text.

        Returns:
            Formatted markdown report (multiline string).

        Raises:
            ValueError: If status not in ("success", "partial", "error").
        """
        if status not in ("success", "partial", "error"):
            raise ValueError(f"Invalid status: {status}")

        # Status emoji
        status_emoji = {"success": "✅", "partial": "⚠", "error": "❌"}[status]

        # Duration humanization
        if duration_sec < 1:
            duration_str = f"{duration_sec*1000:.0f}ms"
        elif duration_sec < 60:
            duration_str = f"{duration_sec:.1f}s"
        else:
            duration_str = f"{duration_sec/60:.1f}m"

        # Tool summary
        tool_count = len(tools_used)
        tool_word = "tool" if tool_count == 1 else "tools"
        tool_list = ", ".join(tools_used)

        # Build report
        lines = [
            f"# {status_emoji} {task_name}",
            "",
            f"**Status:** {status.upper()}",
            f"**Duration:** {duration_str}",
            f"**Tools:** {tool_count} {tool_word} ({tool_list})",
            "",
            "## Result",
            output_text,
        ]

        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# TEST CASES (one per behavior)
# ─────────────────────────────────────────────────────────────

class TestReportFormatter:
    """Test suite for report formatter."""

    @pytest.mark.parametrize("case", GROUND_TRUTH_CASES, ids=lambda c: c.name)
    def test_report_contains_expected_text(self, case: ReportTestCase):
        """Test: Report contains all expected text fragments.

        Ground truth: For each test case, ALL strings in `expected_contains`
        must appear in the formatted report. This validates that the formatter
        correctly includes task metadata, status, duration, and tool info.
        """
        report = ReportFormatter.format_report(
            task_name=case.task_name,
            status=case.status,
            duration_sec=case.duration_sec,
            tools_used=case.tools_used,
            output_text=case.output_text,
        )
        for expected in case.expected_contains:
            assert expected in report, (
                f"Expected '{expected}' in report for {case.name}.\n"
                f"Report:\n{report}"
            )

    def test_report_structure_has_heading(self):
        """Test: Report always starts with a markdown heading.

        Ground truth: The first line MUST be a heading (# prefix) containing
        the task name and status emoji. This ensures reports are scannable.
        """
        report = ReportFormatter.format_report(
            task_name="Sample task",
            status="success",
            duration_sec=1.0,
            tools_used=["file_read"],
            output_text="Done.",
        )
        lines = report.split("\n")
        assert lines[0].startswith("#"), "Report must start with markdown heading"
        assert "Sample task" in lines[0]
        assert "✅" in lines[0], "Heading must include status emoji"

    def test_report_duration_milliseconds(self):
        """Test: Very fast tasks show duration in milliseconds.

        Ground truth: A task taking 0.42s should display as "420ms", not "0.4s".
        This tests humanization logic for sub-second durations.
        """
        report = ReportFormatter.format_report(
            task_name="Quick task",
            status="success",
            duration_sec=0.42,
            tools_used=[],
            output_text="Instant.",
        )
        assert "420ms" in report, "Sub-second duration should show as milliseconds"

    def test_report_duration_seconds(self):
        """Test: Moderate tasks show duration in seconds.

        Ground truth: A 4.2s task should show as "4.2s", not "0.1m" or "4200ms".
        """
        report = ReportFormatter.format_report(
            task_name="Normal task",
            status="success",
            duration_sec=4.2,
            tools_used=[],
            output_text="Done.",
        )
        assert "4.2s" in report

    def test_report_duration_minutes(self):
        """Test: Long tasks show duration in minutes.

        Ground truth: A 125s task should show as "2.1m", not "125s".
        """
        report = ReportFormatter.format_report(
            task_name="Long task",
            status="success",
            duration_sec=125.0,
            tools_used=[],
            output_text="Finished.",
        )
        assert "2.1m" in report

    def test_report_tool_singular_plural(self):
        """Test: Tool count uses correct singular/plural form.

        Ground truth:
          - 1 tool: "1 tool" (not "1 tools")
          - 2+ tools: "N tools"
        """
        # Singular
        report1 = ReportFormatter.format_report(
            task_name="One",
            status="success",
            duration_sec=1.0,
            tools_used=["file_read"],
            output_text=".",
        )
        assert "1 tool" in report1, "Single tool must use singular form"
        assert "1 tools" not in report1

        # Plural
        report2 = ReportFormatter.format_report(
            task_name="Many",
            status="success",
            duration_sec=1.0,
            tools_used=["file_read", "grep_search"],
            output_text=".",
        )
        assert "2 tools" in report2

    def test_report_status_emoji_success(self):
        """Test: Success status displays correct emoji.

        Ground truth: "success" → ✅ (check mark).
        """
        report = ReportFormatter.format_report(
            task_name="Win",
            status="success",
            duration_sec=1.0,
            tools_used=[],
            output_text=".",
        )
        assert "✅" in report
        assert "⚠" not in report
        assert "❌" not in report

    def test_report_status_emoji_partial(self):
        """Test: Partial status displays correct emoji.

        Ground truth: "partial" → ⚠ (warning).
        """
        report = ReportFormatter.format_report(
            task_name="Partial",
            status="partial",
            duration_sec=1.0,
            tools_used=[],
            output_text=".",
        )
        assert "⚠" in report
        assert "✅" not in report

    def test_report_status_emoji_error(self):
        """Test: Error status displays correct emoji.

        Ground truth: "error" → ❌ (cross mark).
        """
        report = ReportFormatter.format_report(
            task_name="Failed",
            status="error",
            duration_sec=1.0,
            tools_used=[],
            output_text=".",
        )
        assert "❌" in report
        assert "✅" not in report

    def test_report_invalid_status_raises(self):
        """Test: Invalid status raises ValueError.

        Ground truth: Only "success", "partial", "error" are valid.
        Anything else must raise ValueError immediately.
        """
        with pytest.raises(ValueError, match="Invalid status"):
            ReportFormatter.format_report(
                task_name="Bad",
                status="invalid_status",
                duration_sec=1.0,
                tools_used=[],
                output_text=".",
            )

    def test_report_empty_tools_list(self):
        """Test: Reports work correctly with no tools used.

        Ground truth: Empty tool list is valid (e.g., info-only tasks).
        Should display "0 tools" cleanly.
        """
        report = ReportFormatter.format_report(
            task_name="No tools",
            status="success",
            duration_sec=0.5,
            tools_used=[],
            output_text="Info task.",
        )
        assert "0 tools" in report
        assert "()  # empty tools" not in report  # no garbage formatting

    def test_report_many_tools_listed(self):
        """Test: Report correctly lists many tools.

        Ground truth: All tools in the input list appear comma-separated
        in the Tools line.
        """
        tools = ["file_read", "grep_search", "run_command", "run_python"]
        report = ReportFormatter.format_report(
            task_name="Many tools",
            status="success",
            duration_sec=1.0,
            tools_used=tools,
            output_text="Done.",
        )
        # Check count
        assert "4 tools" in report
        # Check all tools are listed
        for tool in tools:
            assert tool in report

    def test_report_output_text_preserved(self):
        """Test: Output text is preserved exactly (no truncation).

        Ground truth: The `output_text` parameter should appear verbatim
        in the report under the "Result" section.
        """
        output = "Found 3 unread emails:\n  - From: alice@example.com\n  - From: bob@example.com"
        report = ReportFormatter.format_report(
            task_name="Email check",
            status="success",
            duration_sec=2.0,
            tools_used=["gmail_search"],
            output_text=output,
        )
        assert output in report

    def test_report_markdown_structure(self):
        """Test: Report follows markdown structure.

        Ground truth:
          - Line 1: # heading (task name + status emoji)
          - Line 3+: **Status:** ...
          - Line 4: **Duration:** ...
          - Line 5: **Tools:** ...
          - Line 7: ## Result
          - Line 8+: Output text
        """
        report = ReportFormatter.format_report(
            task_name="Markdown test",
            status="success",
            duration_sec=1.5,
            tools_used=["tool1"],
            output_text="Result here.",
        )
        lines = report.split("\n")
        assert lines[0].startswith("# ")
        assert any("**Status:**" in line for line in lines)
        assert any("**Duration:**" in line for line in lines)
        assert any("**Tools:**" in line for line in lines)
        assert any("## Result" in line for line in lines)


# ─────────────────────────────────────────────────────────────
# INVARIANT TESTS (properties that must always hold)
# ─────────────────────────────────────────────────────────────

class TestReportFormatterInvariants:
    """Tests for universal properties of the formatter."""

    def test_report_always_includes_task_name(self):
        """Invariant: Report always includes the task name."""
        for name in ["Task A", "Very long task name", "Task_123"]:
            report = ReportFormatter.format_report(
                task_name=name,
                status="success",
                duration_sec=1.0,
                tools_used=[],
                output_text=".",
            )
            assert name in report

    def test_report_never_exceeds_max_length_unreasonably(self):
        """Invariant: Report length is roughly proportional to input length.

        Ground truth: A report with short inputs should be < 500 chars.
        A report with very long output (1MB) might be larger, but the
        overhead (metadata, markdown) should be < 5% of total.
        """
        report = ReportFormatter.format_report(
            task_name="Short",
            status="success",
            duration_sec=1.0,
            tools_used=["tool"],
            output_text="Done.",
        )
        assert len(report) < 500, "Minimal report should be compact"

    def test_report_is_deterministic(self):
        """Invariant: Same inputs produce identical output.

        Ground truth: The formatter is a pure function; calling it twice
        with identical arguments must produce identical reports.
        """
        args = {
            "task_name": "Deterministic",
            "status": "success",
            "duration_sec": 3.14159,
            "tools_used": ["file_read", "grep_search"],
            "output_text": "Result text.",
        }
        report1 = ReportFormatter.format_report(**args)
        report2 = ReportFormatter.format_report(**args)
        assert report1 == report2, "Formatter must be deterministic"
