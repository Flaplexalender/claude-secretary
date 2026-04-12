# Test Harness Template Specification

## Overview

This document describes the **reference test harness** for LLM-generated test suites.
It uses a simple, completed goal (**report formatting**) as the template.

See: `tests/test_report_formatter.py` (implementation)

---

## 1. Structure (4 Sections)

Every test harness should follow this structure:

```
tests/test_[feature].py
├── Ground-Truth Data (fixtures, test cases)
├── System Under Test (minimal SUT implementation)
├── Test Cases (one per behavior)
└── Invariant Tests (universal properties)
```

### 1.1 Ground-Truth Data

**Purpose:** Define minimal, reusable test scenarios with *expected* outputs.

**Pattern:**
```python
@dataclass
class TestCase:
    name: str                      # Unique identifier
    input_field_1: ...             # Setup data
    input_field_2: ...
    expected_contains: list[str]   # Assertions: what MUST appear

GROUND_TRUTH_CASES = [
    TestCase(name="case_1", ...),
    TestCase(name="case_2", ...),
]
```

**Why it works:**
- Decouples test data from test logic
- LLM can auto-generate similar cases
- Parameterized tests run all cases automatically

---

### 1.2 System Under Test (SUT)

**Purpose:** Minimal implementation of the feature being tested.

**Pattern:**
```python
class ReportFormatter:
    @staticmethod
    def format_report(...) -> str:
        """Docstring: inputs, outputs, raises."""
        # Implementation
        return output
```

**Why it works:**
- Keeps SUT small and focused (< 50 lines)
- Easy to understand what you're testing
- LLM can implement SUT from this template

---

### 1.3 Test Cases (Behavior-Driven)

**One test per behavior**, not one test per assertion.

**Pattern:**
```python
def test_report_duration_milliseconds(self):
    """Test: Very fast tasks show duration in milliseconds.
    
    Ground truth: A task taking 0.42s should display as "420ms", not "0.4s".
    This tests humanization logic for sub-second durations.
    """
    # Arrange
    report = ReportFormatter.format_report(
        task_name="Quick task",
        status="success",
        duration_sec=0.42,
        tools_used=[],
        output_text="Instant.",
    )
    
    # Assert
    assert "420ms" in report, "Sub-second duration should show as milliseconds"
```

**Docstring structure:**
1. **Test:** One-line title (what is being tested?)
2. **Ground truth:** Why this behavior matters; what's the expected output?
3. **Implementation comment:** (optional) How the test validates it.

**Why it works:**
- Each test is self-contained and easy to understand
- The "ground truth" comment explains the *why*, not just the *what*
- Easy for LLM to generate variations

---

### 1.4 Invariant Tests

**Purpose:** Test properties that MUST ALWAYS hold.

**Pattern:**
```python
class TestReportFormatterInvariants:
    """Tests for universal properties."""
    
    def test_report_always_includes_task_name(self):
        """Invariant: Report always includes the task name."""
        for name in ["Task A", "Very long task name"]:
            report = ReportFormatter.format_report(task_name=name, ...)
            assert name in report
```

**Why it works:**
- Catches regression bugs
- Forces you to think about universal requirements
- LLM can auto-generate these for any feature

---

## 2. Assertion Patterns

Use 2–4 assertions per test. Avoid assertion spam.

### Pattern 1: Substring presence
```python
assert "expected text" in report
```

### Pattern 2: Emoji validation
```python
assert "✅" in report
assert "❌" not in report
```

### Pattern 3: Humanization/formatting
```python
assert "4.2s" in report  # NOT "4.2" or "4200ms"
```

### Pattern 4: Exception handling
```python
with pytest.raises(ValueError, match="Invalid status"):
    ReportFormatter.format_report(..., status="invalid")
```

### Pattern 5: Property validation
```python
lines = report.split("\n")
assert lines[0].startswith("# ")  # markdown heading
```

---

## 3. Ground-Truth Data Template

For your feature, define:

| Element | Example | Why |
|---------|---------|-----|
| **Minimal input** | task_name="Check email", status="success" | Tests happy path |
| **Edge case input** | duration_sec=0.001, tools_used=[] | Tests boundary conditions |
| **Error input** | status="invalid_status" | Tests validation |
| **Expected output** | ["✅", "success", "Check email"] | Defines success criteria |

---

## 4. Test Naming Convention

```
test_[feature]_[behavior]_[condition]

Examples:
  test_report_duration_milliseconds          # Tests duration humanization
  test_report_status_emoji_error             # Tests error emoji
  test_report_output_text_preserved          # Tests output preservation
  test_report_always_includes_task_name      # Invariant: universal property
```

---

## 5. Coverage Checklist

For your feature, test:

- [ ] **Happy path:** Typical usage, all inputs valid
- [ ] **Edge cases:** Boundary values (0, 1, very large, empty)
- [ ] **Error cases:** Invalid inputs, raises correct exceptions
- [ ] **Formatting:** Output structure, humanization, emojis
- [ ] **Invariants:** Properties that must always hold
- [ ] **Determinism:** Same inputs → same outputs (no random state)

---

## 6. LLM-Friendly Patterns

### Pattern A: Parameterized testing
```python
@pytest.mark.parametrize("case", GROUND_TRUTH_CASES, ids=lambda c: c.name)
def test_report_contains_expected_text(self, case):
    # One test, multiple cases
```

**Why:** LLM can auto-generate more test cases by extending `GROUND_TRUTH_CASES`.

### Pattern B: Nested test classes
```python
class TestReportFormatter:          # Behavior tests
    def test_...

class TestReportFormatterInvariants:  # Property tests
    def test_...
```

**Why:** Organizes tests logically; LLM can generate one class at a time.

### Pattern C: Docstring ground truth
```python
def test_duration_milliseconds(self):
    """Test: Very fast tasks show duration in milliseconds.
    
    Ground truth: 0.42s → "420ms", not "0.4s".
    """
```

**Why:** The ground truth comment lets LLM understand *why* the test exists.

---

## 7. Running the Tests

```bash
# All tests in the harness
pytest tests/test_report_formatter.py -v

# Specific test class
pytest tests/test_report_formatter.py::TestReportFormatter -v

# Parameterized test cases (shows each case)
pytest tests/test_report_formatter.py::TestReportFormatter::test_report_contains_expected_text -v
```

---

## 8. Metrics

After running the test harness, measure:

| Metric | Target | How |
|--------|--------|-----|
| **Coverage** | ≥ 90% | `pytest --cov=src --cov-report=term-missing` |
| **Test count** | 15–25 per feature | One behavior per test |
| **Assertion count** | 2–4 per test | Avoid assertion spam |
| **Lines of code (SUT)** | < 50 | Keep implementation minimal |
| **Lines of code (tests)** | 200–300 | Roughly 5–10x the SUT |

---

## 9. Example: Adapting for Your Feature

To create a test harness for a *new* feature:

1. **Define the SUT** (minimal implementation, < 50 lines)
2. **Create ground-truth data** (3–5 test cases with expected outputs)
3. **Write behavior tests** (one per behavior, 2–4 assertions each)
4. **Add invariant tests** (2–3 universal properties)
5. **Run pytest** and verify all pass
6. **Measure coverage** (aim for ≥ 90%)

---

## 10. Anti-Patterns to Avoid

| ❌ Anti-Pattern | ✅ Better | Why |
|-----------------|-----------|-----|
| One big test with 20 assertions | One test per behavior, 2–4 assertions | Easier to debug; clearer failure messages |
| Hard-coded test data inside tests | Ground-truth dataclass with parameterization | Reusable, LLM-friendly |
| No docstrings on tests | Docstring: title + ground truth + why | Self-documenting; LLM understands intent |
| Testing implementation details | Testing behavior/contracts | Less brittle; survives refactoring |
| No invariant tests | 2–3 universal property tests | Catches regressions |
| Ignoring edge cases | Dedicated edge-case test cases | Robustness |

---

## References

- Implementation: `tests/test_report_formatter.py`
- SUT: `ReportFormatter` class (simple report formatting)
- Ground truth: `GROUND_TRUTH_CASES` (minimal test scenarios)
- Test patterns: Behavior-driven, parameterized, invariant-based
