# Template Adoption Guide: Creating Test Harnesses for New Features

## Quick Start

To create a test harness for **your feature**, follow this 6-step process:

```
Step 1: Copy template          → tests/test_your_feature.py
Step 2: Define ground-truth    → Update dataclass + test cases
Step 3: Implement SUT          → ~30 lines of feature code
Step 4: Write behavior tests   → One per behavior (2–4 assertions)
Step 5: Add invariant tests    → 2–3 universal properties
Step 6: Validate              → pytest + coverage + checklist
```

---

## Step-by-Step Process

### Step 1: Copy Template

```bash
cp tests/test_report_formatter.py tests/test_your_feature.py
```

### Step 2: Define Ground-Truth Data

Update the `@dataclass` and `GROUND_TRUTH_CASES`:

```python
@dataclass
class YourFeatureTestCase:
    """Ground-truth: a minimal scenario for your feature."""
    name: str
    input_field_1: str          # ← customize these fields
    input_field_2: int
    expected_output: str        # ← what you expect
    expected_contains: list[str]

GROUND_TRUTH_CASES = [
    YourFeatureTestCase(
        name="happy_path",
        input_field_1="...",
        input_field_2=42,
        expected_output="...",
        expected_contains=["keyword1", "keyword2"],
    ),
    YourFeatureTestCase(
        name="edge_case",
        input_field_1="",
        input_field_2=0,
        expected_output="...",
        expected_contains=["empty", "handled"],
    ),
    YourFeatureTestCase(
        name="error_case",
        input_field_1=None,
        input_field_2=-1,
        expected_output=None,  # None if expecting exception
        expected_contains=[],
    ),
]
```

**Key points:**
- 3–5 test cases (not more, not less)
- One for happy path, one for edge case, one for error
- `expected_contains`: strings that MUST appear in output
- Use empty strings, zeros, negative numbers, None for edges

### Step 3: Implement System Under Test (SUT)

Replace `ReportFormatter` with your feature class (~30 lines):

```python
class YourFeature:
    """Short description of what this does."""
    
    @staticmethod
    def process(input_1: str, input_2: int) -> str:
        """Process inputs and return output.
        
        Args:
            input_1: Description.
            input_2: Description.
        
        Returns:
            Processed output string.
        
        Raises:
            ValueError: If inputs are invalid.
            TypeError: If inputs have wrong type.
        """
        if not input_1:
            raise ValueError("input_1 cannot be empty")
        if input_2 < 0:
            raise ValueError("input_2 must be non-negative")
        
        # Implementation: ~20 lines
        result = f"Processed: {input_1} x {input_2}"
        return result
```

**Size constraint:** Keep SUT < 50 lines. If it's bigger, break it into multiple functions/classes.

### Step 4: Write Behavior Tests

Replace `TestReportFormatter` with your behavior tests:

```python
class TestYourFeature:
    """Behavior tests for YourFeature."""
    
    @pytest.mark.parametrize("case", GROUND_TRUTH_CASES, ids=lambda c: c.name)
    def test_process_produces_expected_output(self, case):
        """Test: process() produces expected output format.
        
        Ground truth: For each test case, the output must contain
        all strings in `expected_contains`. This validates that
        the processing logic is correct.
        """
        result = YourFeature.process(case.input_1, case.input_2)
        for expected in case.expected_contains:
            assert expected in result
    
    def test_process_specific_behavior_1(self):
        """Test: First specific behavior.
        
        Ground truth: Describe what should happen and why.
        """
        result = YourFeature.process("input", 42)
        assert "expected" in result
    
    def test_process_specific_behavior_2(self):
        """Test: Second specific behavior.
        
        Ground truth: Describe what should happen and why.
        """
        result = YourFeature.process("other", 0)
        assert "handled" in result
```

**Key points:**
- Parameterize the first test with `GROUND_TRUTH_CASES`
- Add 3–5 behavior-specific tests
- Each test: 2–4 assertions
- Docstring: "Test: ...", "Ground truth: ..."

### Step 5: Add Invariant Tests

Add 2–3 tests for properties that must always hold:

```python
class TestYourFeatureInvariants:
    """Tests for universal properties."""
    
    def test_invariant_always_returns_string(self):
        """Invariant: process() always returns a string (never None)."""
        for case in GROUND_TRUTH_CASES:
            if case.expected_output is not None:  # skip error cases
                result = YourFeature.process(case.input_1, case.input_2)
                assert isinstance(result, str), "Must return str"
    
    def test_invariant_deterministic(self):
        """Invariant: Same inputs produce identical output."""
        result1 = YourFeature.process("data", 42)
        result2 = YourFeature.process("data", 42)
        assert result1 == result2, "Must be deterministic"
    
    def test_invariant_input_preserved(self):
        """Invariant: Input data appears in output (if applicable)."""
        result = YourFeature.process("MyData", 42)
        assert "MyData" in result, "Output must include input"
```

### Step 6: Validate

Run tests and verify metrics:

```bash
# Run tests
pytest tests/test_your_feature.py -v

# Expected: All tests pass
# ✓ 15–25 tests
# ✓ 2–4 assertions per test
# ✓ 0 failures

# Check coverage
pytest tests/test_your_feature.py --cov=src.your_feature --cov-report=term-missing

# Expected: ≥ 90% coverage

# Use checklist
# See: docs/HARNESS_CHECKLIST.md
```

---

## Common Patterns

### Pattern 1: String Processing

```python
class TextProcessor:
    @staticmethod
    def uppercase_words(text: str) -> str:
        return " ".join(word.upper() for word in text.split())

# Test case
@dataclass
class TextTestCase:
    name: str
    input_text: str
    expected_contains: list[str]

GROUND_TRUTH_CASES = [
    TextTestCase("simple", "hello world", ["HELLO", "WORLD"]),
    TextTestCase("empty", "", [""]),
    TextTestCase("unicode", "café naïve", ["CAFÉ"]),
]
```

### Pattern 2: Data Validation

```python
class Validator:
    @staticmethod
    def validate_email(email: str) -> bool:
        if "@" not in email:
            raise ValueError("Invalid email")
        return True

# Test case
def test_invalid_email_raises():
    with pytest.raises(ValueError, match="Invalid email"):
        Validator.validate_email("not-an-email")
```

### Pattern 3: List/Dict Processing

```python
class ListProcessor:
    @staticmethod
    def deduplicate(items: list[str]) -> list[str]:
        return list(dict.fromkeys(items))

# Test case
def test_deduplicate_preserves_order():
    result = ListProcessor.deduplicate(["a", "b", "a", "c"])
    assert result == ["a", "b", "c"]
    assert len(result) == 3
```

---

## Anti-Patterns (What to Avoid)

| ❌ Don't | ✅ Do |
|---------|------|
| Hard-code test data in each test | Use GROUND_TRUTH_CASES dataclass |
| Write 20 assertions in one test | Write 2–4 assertions per test |
| Have no docstrings | Include docstring: "Test: ...", "Ground truth: ..." |
| Test implementation details | Test behavior/contracts |
| Ignore edge cases | Include 3+ edge case scenarios |
| Make SUT > 100 lines | Keep SUT < 50 lines |
| No invariant tests | Include 2–3 universal property tests |
| Copy-paste assertions | Use specific assertions with clear messages |
| No parameterization | Use `@pytest.mark.parametrize` for multiple cases |

---

## Metrics Checklist

After creating your harness, verify:

| Metric | Target | How to Check |
|--------|--------|--------------|
| **Test count** | 15–25 | `grep "def test_" tests/test_*.py | wc -l` |
| **Assertions/test** | 2–4 | `grep "assert " tests/test_*.py | wc -l` then divide by test count |
| **Coverage** | ≥ 90% | `pytest --cov=src --cov-report=term-missing` |
| **SUT lines** | < 50 | `wc -l src/your_feature.py` |
| **Test lines** | 200–300 | `wc -l tests/test_your_feature.py` |
| **Docstrings** | 100% on tests | Every test has a docstring with "Test:" and "Ground truth:" |

---

## Example: Email Filter Feature

Let's say you want to test an **email filter**. Here's how to apply the template:

### Ground-Truth Data

```python
@dataclass
class EmailFilterTestCase:
    name: str
    emails: list[str]
    keywords: list[str]
    expected_count: int
    expected_contains: list[str]

GROUND_TRUTH_CASES = [
    EmailFilterTestCase(
        name="find_marketing",
        emails=["promo@shop.com", "user@example.com", "sale@shop.com"],
        keywords=["promo", "sale"],
        expected_count=2,
        expected_contains=["promo", "sale"],
    ),
    EmailFilterTestCase(
        name="no_matches",
        emails=["user@example.com"],
        keywords=["promo"],
        expected_count=0,
        expected_contains=[],
    ),
]
```

### SUT

```python
class EmailFilter:
    @staticmethod
    def filter_by_keywords(emails: list[str], keywords: list[str]) -> list[str]:
        """Filter emails matching any keyword."""
        if not keywords:
            return []
        return [e for e in emails if any(k in e for k in keywords)]
```

### Tests

```python
class TestEmailFilter:
    @pytest.mark.parametrize("case", GROUND_TRUTH_CASES, ids=lambda c: c.name)
    def test_filter_returns_matching_emails(self, case):
        """Test: filter_by_keywords() returns emails matching any keyword."""
        result = EmailFilter.filter_by_keywords(case.emails, case.keywords)
        assert len(result) == case.expected_count

class TestEmailFilterInvariants:
    def test_invariant_never_returns_unmatched_emails(self):
        """Invariant: All returned emails contain at least one keyword."""
        emails = ["promo@a.com", "user@b.com", "sale@c.com"]
        keywords = ["promo", "sale"]
        result = EmailFilter.filter_by_keywords(emails, keywords)
        for email in result:
            assert any(k in email for k in keywords)
```

---

## Running Your Harness

```bash
# Run all tests in your harness
pytest tests/test_your_feature.py -v

# Run a specific test class
pytest tests/test_your_feature.py::TestYourFeature -v

# Run a specific test
pytest tests/test_your_feature.py::TestYourFeature::test_specific_behavior_1 -v

# Run with coverage
pytest tests/test_your_feature.py --cov=src.your_feature --cov-report=html

# Run and show test output
pytest tests/test_your_feature.py -v -s
```

---

## Integration with CI/CD

Add to your `pytest.ini` or `pyproject.toml`:

```toml
[tool.pytest.ini_options]
minversion = "7.0"
testpaths = ["tests"]
addopts = "-v --cov=src --cov-report=term-missing --cov-report=html"
python_files = ["test_*.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
```

Then your CI/CD can run:

```bash
pytest tests/ --cov=src --cov-report=xml
```

---

## Next Steps

1. **Review** the reference harness: `tests/test_report_formatter.py`
2. **Read** the template: `docs/TEST_HARNESS_TEMPLATE.md`
3. **Copy** the template to your feature: `cp tests/test_report_formatter.py tests/test_your_feature.py`
4. **Customize** each section (data, SUT, tests, invariants)
5. **Run pytest** and verify all pass
6. **Check coverage** (aim for ≥90%)
7. **Use checklist** to validate: `docs/HARNESS_CHECKLIST.md`

---

## Support

- **Template structure:** See `docs/TEST_HARNESS_TEMPLATE.md`
- **Evaluation rubric:** See `docs/HARNESS_CHECKLIST.md`
- **Reference implementation:** See `tests/test_report_formatter.py`
- **Example output:** See `docs/HARNESS_EXAMPLE_OUTPUT.md`

**Questions?** Check the anti-patterns section or the reference harness.
