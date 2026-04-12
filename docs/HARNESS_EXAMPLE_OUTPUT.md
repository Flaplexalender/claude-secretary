# Test Harness Example Output

## Reference Test Suite: Report Formatter

This document shows the **output structure** of the minimal reference test harness.

### Test Execution

```bash
$ pytest tests/test_report_formatter.py -v
```

#### Expected Output:

```
============================= test session starts ==============================
platform win32 -- Python 3.13.2, pytest-9.0.2, pluggy-1.6.0
rootdir: /path/to/claude-secretary, configfile: pyproject.toml
collected 20 items

tests/test_report_formatter.py::TestReportFormatter::test_report_contains_task_name PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_duration_humanized PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_status_uppercase PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_tools_summary PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_with_long_output PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_duration_milliseconds PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_status_emoji_success PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_status_emoji_partial PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_status_emoji_error PASSED
tests/test_report_formatter.py::TestReportFormatter::test_report_no_tools_zero_summary PASSED
tests/test_report_formatter.py::TestReportFormatterInvariants::test_invariant_always_includes_task_name PASSED
tests/test_report_formatter.py::TestReportFormatterInvariants::test_invariant_always_includes_duration PASSED
tests/test_report_formatter.py::TestReportFormatterInvariants::test_invariant_status_always_present PASSED
tests/test_report_formatter.py::TestReportFormatterInvariants::test_invariant_output_text_preserved PASSED
tests/test_report_formatter.py::TestReportFormatterInvariants::test_invariant_deterministic PASSED
tests/test_report_formatter.py::TestReportFormatterInvariants::test_invariant_no_injection_attack PASSED
tests/test_report_formatter.py::TestReportFormatterErrorHandling::test_error_missing_task_name PASSED
tests/test_report_formatter.py::TestReportFormatterErrorHandling::test_error_invalid_status PASSED
tests/test_report_formatter.py::TestReportFormatterErrorHandling::test_error_negative_duration PASSED
tests/test_report_formatter.py::TestReportFormatterErrorHandling::test_error_none_duration PASSED

======================== 20 passed in 0.42s ===========================
```

---

## Coverage Report

```bash
$ pytest tests/test_report_formatter.py --cov=tests.test_report_formatter --cov-report=term-missing
```

#### Expected Output:

```
============================= test session starts ==============================
collected 20 items

tests/test_report_formatter.py::TestReportFormatter::... (20 tests) ...

======================== 20 passed in 0.48s ==========================

---------- coverage: platform win32-3.13.2-64bit -----------
Name                                    Stmts   Miss  Cover   Missing
---------------------------------------------------------------------
tests/test_report_formatter.py             145      0   100%
---------------------------------------------------------------------
TOTAL                                       145      0   100%
```

---

## Test Structure Breakdown

### 1. Ground-Truth Data Section

```python
@dataclass
class ReportTestCase:
    name: str
    task_name: str
    status: str
    duration_sec: float
    tools_used: list[str]
    output_text: str
    expected_contains: list[str]

GROUND_TRUTH_CASES = [
    ReportTestCase(
        name="simple_success",
        task_name="Check email",
        status="success",
        duration_sec=4.2,
        tools_used=["gmail_search", "gmail_read"],
        output_text="Found 3 unread emails.",
        expected_contains=["Check email", "SUCCESS", "4.2s", "2 tools"],
    ),
    # ... 2 more cases
]
```

**Purpose:** Minimal test data for 3 scenarios (happy path, partial, error).

---

### 2. System Under Test (SUT)

```python
class ReportFormatter:
    """Format agent run results into human-readable reports."""
    
    @staticmethod
    def format_report(
        task_name: str,
        status: str,
        duration_sec: float,
        tools_used: list[str],
        output_text: str,
    ) -> str:
        """Format a task result into a markdown report.
        
        Raises:
            ValueError: If status not in {success, partial, error}
            TypeError: If duration_sec is None or negative
        """
        # Implementation: ~30 lines
```

**Size:** 30 lines (minimal, focused, testable).

---

### 3. Test Cases (Behavior-Driven)

```python
class TestReportFormatter:
    """Behavior tests for ReportFormatter."""
    
    def test_report_duration_humanized(self):
        """Test: Duration is humanized to user-friendly format.
        
        Ground truth: 4.2 seconds should display as "4.2s", not "4.2" or "4200ms".
        This tests that the duration field is properly formatted.
        """
        report = ReportFormatter.format_report(
            task_name="Task", status="success", duration_sec=4.2,
            tools_used=[], output_text="Done."
        )
        assert "4.2s" in report
```

**Key features:**
- One behavior per test
- Clear docstring: "Test: ...", "Ground truth: ..."
- 2–4 specific assertions

---

### 4. Invariant Tests (Universal Properties)

```python
class TestReportFormatterInvariants:
    """Tests for properties that MUST ALWAYS hold."""
    
    def test_invariant_always_includes_task_name(self):
        """Invariant: Report always includes the task name.
        
        This must hold for ANY task name, ensuring the formatter never
        loses the core information about what task was executed.
        """
        for name in ["Check email", "🚀 Deploy app", "Task-123", ""]:
            report = ReportFormatter.format_report(
                task_name=name, status="success",
                duration_sec=1.0, tools_used=[], output_text=""
            )
            assert name in report, f"Task name '{name}' missing from report"
```

**Key features:**
- Parameterized over boundary values
- Verifies universal property (always true, any input)
- Catches regressions

---

### 5. Error Handling Tests

```python
class TestReportFormatterErrorHandling:
    """Tests for error cases and validation."""
    
    def test_error_invalid_status(self):
        """Test: Invalid status raises ValueError with clear message.
        
        Ground truth: Only {success, partial, error} are valid.
        Any other status should raise ValueError.
        """
        with pytest.raises(ValueError, match="Invalid status"):
            ReportFormatter.format_report(
                task_name="Task", status="invalid_status",
                duration_sec=1.0, tools_used=[], output_text=""
            )
```

**Key features:**
- Verifies exception type and message
- Tests validation logic
- Ensures graceful failure

---

## Metrics Summary

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| **Test count** | 20 | 15–25 | ✓ Good |
| **Assertions** | 45 | 30–100 | ✓ Good |
| **Assertions/test** | 2.25 | 2–4 | ✓ Good |
| **Coverage** | 100% | ≥90% | ✓ Excellent |
| **SUT lines** | 30 | <50 | ✓ Good |
| **Test lines** | 250 | 200–300 | ✓ Good |
| **Test/SUT ratio** | 8:1 | 5–10:1 | ✓ Good |

---

## Key Takeaways for LLM-Generated Harnesses

### ✓ What works in this template:

1. **Ground-truth dataclass** → Easy for LLM to extend (add more test cases)
2. **Behavior-driven tests** → One clear purpose per test
3. **Docstring ground truth** → LLM understands "why" not just "what"
4. **Error handling separate** → Clear organization, easy to add validation
5. **Invariant tests** → Forces thinking about universal properties
6. **Parameterization** → `@pytest.mark.parametrize` for multiple scenarios

### ✓ Why this structure is LLM-friendly:

- Tests are **self-documenting** (docstrings explain intent)
- Data is **reusable** (GROUND_TRUTH_CASES can be extended)
- Structure is **repeatable** (apply pattern to any feature)
- Assertions are **specific** (not vague; easy to validate against output)
- Sections are **independent** (can generate each class separately)

### ✗ Anti-patterns avoided:

- ❌ Hard-coded test data → ✓ Dataclass with parameterization
- ❌ No docstrings → ✓ "Test:", "Ground truth:" in every test
- ❌ 20 assertions per test → ✓ 2–4 specific assertions
- ❌ Testing implementation → ✓ Testing behavior/contracts
- ❌ No edge cases → ✓ Invariants + error handling sections

---

## Next Steps

1. **Copy this template** to create a new test harness for your feature
2. **Adapt the 4 sections** (data, SUT, tests, invariants)
3. **Run pytest** and verify all pass
4. **Measure coverage** (aim for ≥90%)
5. **Share with LLM** as an example for auto-generation

See `docs/HARNESS_CHECKLIST.md` for the evaluation rubric.
