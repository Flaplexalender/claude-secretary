# Test Harness Generation Checklist

Use this checklist when creating a new test harness or evaluating an LLM-generated harness.

## Phase 1: Structure (Does it follow the template?)

- [ ] **Ground-truth data present?**
  - [ ] Dataclass or fixture defining test cases
  - [ ] 3–5 minimal test scenarios
  - [ ] Each case has `name`, `inputs`, `expected_contains`

- [ ] **System Under Test (SUT) included?**
  - [ ] Minimal implementation (< 50 lines)
  - [ ] Docstring with inputs, outputs, raises
  - [ ] Class-based (e.g., `ReportFormatter`)

- [ ] **Test cases organized?**
  - [ ] Grouped in `TestFeatureName` class
  - [ ] One behavior per test method
  - [ ] Descriptive names (`test_feature_behavior_condition`)

- [ ] **Invariant tests present?**
  - [ ] Separate class: `TestFeatureNameInvariants`
  - [ ] 2–3 universal property tests
  - [ ] Tests that must always hold

---

## Phase 2: Test Quality (Are tests well-written?)

For **each test**, check:

- [ ] **Docstring present?**
  - [ ] Title: "Test: What is being tested?"
  - [ ] Ground truth: "Expected output: ..."
  - [ ] Why it matters (optional but recommended)

- [ ] **Assertions count: 2–4?**
  - [ ] Not 1 (too simple)
  - [ ] Not 5+ (assertion spam; split into multiple tests)

- [ ] **Assertions are specific?**
  - [ ] ❌ `assert report` (too vague)
  - [ ] ✅ `assert "4.2s" in report` (specific)

- [ ] **Setup is minimal?**
  - [ ] Uses ground-truth data where possible
  - [ ] Arrange → Act → Assert pattern clear

- [ ] **Error cases included?**
  - [ ] Invalid inputs raise exceptions
  - [ ] Exception type and message are checked

---

## Phase 3: Coverage (What scenarios are tested?)

- [ ] **Happy path:** Typical usage, all valid inputs ✓
- [ ] **Edge cases:** 
  - [ ] Boundary values (0, 1, very large, empty)
  - [ ] Special characters, unicode
  - [ ] Very long strings
- [ ] **Error cases:**
  - [ ] Invalid status/enum
  - [ ] Negative numbers
  - [ ] None/null values
- [ ] **Formatting:**
  - [ ] Output structure (markdown, JSON, etc.)
  - [ ] Emojis, special characters
  - [ ] Whitespace, line breaks
- [ ] **Invariants:**
  - [ ] Determinism (same input → same output)
  - [ ] Universal properties (always includes field X, never exceeds length Y)
  - [ ] Idempotence (if applicable)

---

## Phase 4: Metrics

After running tests:

```bash
pytest tests/test_[feature].py -v --tb=short
```

Check:

- [ ] **All tests pass?** (exit code 0)
- [ ] **Test count: 15–25?**
  ```
  # Count: grep "def test_" tests/test_feature.py | wc -l
  ```
- [ ] **Coverage ≥ 90%?** (for the SUT)
  ```bash
  pytest tests/test_feature.py --cov=src.feature --cov-report=term-missing
  ```
- [ ] **No skipped/xfail tests?**
  ```
  # Verify all tests ran
  ```

---

## Phase 5: LLM-Friendliness (Can it be auto-generated?)

- [ ] **Parameterized tests present?**
  - [ ] `@pytest.mark.parametrize` used for multiple cases
  - [ ] Test IDs meaningful (`ids=lambda c: c.name`)

- [ ] **Reusable fixtures?**
  - [ ] Ground-truth data in separate section
  - [ ] Easy to extend (add new test case to list)

- [ ] **Clear naming?**
  - [ ] Test names follow pattern: `test_feature_behavior_condition`
  - [ ] No ambiguous names

- [ ] **Docstrings explain intent?**
  - [ ] LLM can understand "why" from docstring
  - [ ] Ground truth clearly stated

---

## Phase 6: Run the Checklist

```bash
#!/bin/bash
# Run all checks
FEATURE="report_formatter"

echo "=== Running tests ==="
pytest tests/test_${FEATURE}.py -v --tb=short

echo "=== Checking coverage ==="
pytest tests/test_${FEATURE}.py --cov=src.${FEATURE} --cov-report=term-missing

echo "=== Counting tests ==="
TEST_COUNT=$(grep -c "def test_" tests/test_${FEATURE}.py)
echo "Total tests: $TEST_COUNT"

echo "=== Counting assertions ==="
ASSERT_COUNT=$(grep -c "assert " tests/test_${FEATURE}.py)
echo "Total assertions: $ASSERT_COUNT"

echo "=== Lines of code ==="
SUT_LINES=$(wc -l < src/${FEATURE}.py)
TEST_LINES=$(wc -l < tests/test_${FEATURE}.py)
echo "SUT: $SUT_LINES lines, Tests: $TEST_LINES lines"
```

---

## Phase 7: Evaluation Rubric

| Criterion | Poor | Good | Excellent |
|-----------|------|------|-----------|
| **Structure** | No organization | Some classes | 4-section structure (data, SUT, tests, invariants) |
| **Test count** | < 10 | 10–15 | 15–25 |
| **Docstrings** | None | Some tests | All tests + ground truth |
| **Assertions/test** | 1 or 5+ | 2–3 | 2–4 with specific checks |
| **Coverage** | < 70% | 70–85% | ≥ 90% |
| **Edge cases** | None | 1–2 | 3+ (boundaries, empty, special chars) |
| **Error handling** | Not tested | Basic | Comprehensive (exception type + message) |
| **Invariants** | None | 1 | 2–3 universal properties |
| **Parameterization** | Hard-coded data | Lists of cases | Dataclass + @pytest.mark.parametrize |
| **LLM-friendly** | Not designed | Somewhat reusable | Clear structure, easy to extend |

---

## Failure Modes

If tests fail, check:

| Symptom | Diagnosis | Fix |
|---------|-----------|-----|
| AssertionError: "expected text" in report | Missing output field | Check SUT implementation; verify docstring; add to ground truth |
| ValueError: Invalid status | Validation not implemented | Add status validation in SUT |
| Test passes but coverage < 80% | Branch not tested | Add test for error case or edge condition |
| Parameterized test shows only 1 case | Ground truth list incomplete | Add more test cases to GROUND_TRUTH_CASES |
| Hard to understand test intent | Missing docstring | Add "Test: ...", "Ground truth: ..." to docstring |

---

## Continuous Improvement

After initial harness creation:

1. **Run tests weekly** to catch regressions
2. **Review coverage** monthly; add tests for uncovered branches
3. **Extend ground truth** when new edge cases are discovered
4. **Refactor SUT** if needed; re-run tests to verify correctness
5. **Update docstrings** as requirements evolve
6. **Share with team** as the template for similar features

---

## Sign-Off

- [ ] Structure follows 4-section template
- [ ] All tests pass (exit code 0)
- [ ] Coverage ≥ 90%
- [ ] Docstrings explain ground truth
- [ ] Assertions are specific (2–4 per test)
- [ ] Edge cases tested
- [ ] Error cases tested
- [ ] Invariants tested
- [ ] Can be extended by LLM (parameterized, reusable data)

**Ready for production? ✓**
