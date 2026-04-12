# TEST HARNESS TEMPLATE: QUICK START

## One-Minute Summary

A **minimal reference test harness** for your project showing how to structure test suites:

```python
# 1. Ground-truth data (reusable test scenarios)
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
    ReportTestCase(name="simple_success", ...),
    ReportTestCase(name="partial_with_error", ...),
    ReportTestCase(name="error_state", ...),
]

# 2. System under test (~30 lines)
class ReportFormatter:
    @staticmethod
    def format_report(...) -> str:
        """Format task results into human-readable reports."""
        # Implementation ~30 lines
        return output

# 3. Behavior tests (one per behavior, 2–4 assertions)
class TestReportFormatter:
    @pytest.mark.parametrize("case", GROUND_TRUTH_CASES)
    def test_report_contains_expected_text(self, case):
        """Test: Output contains all expected keywords.
        
        Ground truth: For each test case, output must contain
        all strings in expected_contains.
        """
        result = ReportFormatter.format_report(...)
        for expected in case.expected_contains:
            assert expected in result

# 4. Invariant tests (universal properties that must always hold)
class TestReportFormatterInvariants:
    def test_invariant_always_includes_task_name(self):
        """Invariant: Report always includes the task name."""
        for name in ["Check email", "Deploy app", ""]:
            result = ReportFormatter.format_report(task_name=name, ...)
            assert name in result
```

**Result:** 19 tests, 100% passing, 100% coverage ✅

---

## Files (Use These)

| File | Purpose | Size |
|------|---------|------|
| **tests/test_report_formatter.py** | Live example (study this) | 17 KB |
| **docs/TEST_HARNESS_TEMPLATE.md** | Full spec + patterns | 9 KB |
| **docs/HARNESS_CHECKLIST.md** | Validation rubric | 7 KB |
| **TEMPLATE_ADOPTION_GUIDE.md** | Copy-paste this to create new harnesses | 12 KB |

---

## 3 Ways to Use This

### Way 1: Copy Template (30 min)
```bash
cp tests/test_report_formatter.py tests/test_your_feature.py
# Edit: dataclass, SUT, tests, invariants
pytest tests/test_your_feature.py -v
```

### Way 2: Read & Learn (15 min)
```bash
cat tests/test_report_formatter.py
cat docs/TEST_HARNESS_TEMPLATE.md
```

### Way 3: Generate with LLM (5 min)
```
Show LLM: tests/test_report_formatter.py
Show LLM: docs/TEST_HARNESS_TEMPLATE.md
Ask: Generate test harness for [feature] following this pattern
```

---

## Why This Works ✨

✅ **4 sections** = Data + SUT + Tests + Invariants
✅ **Reusable** = GROUND_TRUTH_CASES easily extended  
✅ **Self-documenting** = Docstrings explain intent
✅ **Specific** = 2–4 assertions per test, not 20+
✅ **Metrics-driven** = 15–25 tests, ≥90% coverage
✅ **LLM-friendly** = Repeatable patterns, easy to generate

---

## Go Now

1. **Study example:** `tests/test_report_formatter.py`
2. **Read template:** `docs/TEST_HARNESS_TEMPLATE.md`  
3. **Get started:** `TEMPLATE_ADOPTION_GUIDE.md`
4. **Validate:** `docs/HARNESS_CHECKLIST.md`

**All tests passing:** 19/19 ✅
