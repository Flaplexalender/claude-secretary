"""Tests for src/secretary/harness_generator.py

Covers:
1. criteria parsing (plain text, bullets, numbered lists)
2. API call mocking (Claude client patched, no real network call)
3. code generation format (contains def test_ and assert)
4. malformed input handling (None-like / garbage input)
5. empty criteria edge case (raises ValueError)
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from secretary.harness_generator import (
    _parse_criteria,
    generate_pytest_from_criteria,
)


# ---------------------------------------------------------------------------
# 1. Criteria parsing
# ---------------------------------------------------------------------------

class TestParseCriteria:
    def test_plain_text_lines(self):
        text = "The function returns a string\nThe string is non-empty\nNo exceptions raised"
        result = _parse_criteria(text)
        assert result == [
            "The function returns a string",
            "The string is non-empty",
            "No exceptions raised",
        ]

    def test_bullet_list(self):
        text = "- First criterion\n* Second criterion\n• Third criterion"
        result = _parse_criteria(text)
        assert result == ["First criterion", "Second criterion", "Third criterion"]

    def test_numbered_list(self):
        text = "1. Output is valid Python\n2. Contains def test_\n3. Contains assert"
        result = _parse_criteria(text)
        assert result == [
            "Output is valid Python",
            "Contains def test_",
            "Contains assert",
        ]

    def test_mixed_and_blank_lines(self):
        text = "\n- Item one\n\n2. Item two\n\n* Item three\n"
        result = _parse_criteria(text)
        assert len(result) == 3
        assert "Item one" in result
        assert "Item two" in result
        assert "Item three" in result

    def test_empty_string_returns_empty_list(self):
        assert _parse_criteria("") == []

    def test_whitespace_only_returns_empty_list(self):
        assert _parse_criteria("   \n\n   \t  \n") == []


# ---------------------------------------------------------------------------
# 2. API call mocking
# ---------------------------------------------------------------------------

def _make_mock_client(code: str) -> MagicMock:
    """Build a mock anthropic.Anthropic() whose .messages.create() returns *code*."""
    content_block = SimpleNamespace(text=code)
    response = SimpleNamespace(content=[content_block])
    mock_client = MagicMock()
    mock_client.messages.create.return_value = response
    return mock_client


class TestAPICallMocking:
    def test_create_called_once(self):
        valid_code = "def test_example():\n    assert True\n"
        mock_client = _make_mock_client(valid_code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            result = generate_pytest_from_criteria("goal-1", "It should work correctly")

        mock_client.messages.create.assert_called_once()

    def test_create_receives_user_message(self):
        valid_code = "def test_example():\n    assert 1 == 1\n"
        mock_client = _make_mock_client(valid_code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            generate_pytest_from_criteria("goal-abc", "Returns non-empty string")

        call_kwargs = mock_client.messages.create.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs.args[0] if call_kwargs.args else None
        if messages is None:
            # positional fallback
            messages = call_kwargs[1].get("messages") or call_kwargs[0][0]
        # At least one user-role message containing the goal_id
        user_messages = [m for m in messages if m.get("role") == "user"]
        assert len(user_messages) >= 1
        assert "goal-abc" in user_messages[0]["content"]

    def test_markdown_fences_stripped(self):
        fenced_code = "```python\ndef test_fenced():\n    assert True\n```"
        mock_client = _make_mock_client(fenced_code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            result = generate_pytest_from_criteria("goal-2", "Works correctly")

        assert "```" not in result
        assert "def test_fenced" in result


# ---------------------------------------------------------------------------
# 3. Code generation format
# ---------------------------------------------------------------------------

class TestCodeGenerationFormat:
    def test_result_contains_def_test(self):
        code = "def test_basic():\n    x = compute()\n    assert x is not None\n"
        mock_client = _make_mock_client(code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            result = generate_pytest_from_criteria("goal-3", "Output is not None")

        assert "def test_" in result

    def test_result_contains_assert(self):
        code = "def test_value():\n    assert get_value() == 42\n"
        mock_client = _make_mock_client(code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            result = generate_pytest_from_criteria("goal-4", "Value equals 42")

        assert "assert" in result

    def test_result_is_string(self):
        code = "def test_str():\n    assert isinstance('x', str)\n"
        mock_client = _make_mock_client(code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            result = generate_pytest_from_criteria("goal-5", "Returns a string")

        assert isinstance(result, str)

    def test_multiple_test_functions_preserved(self):
        code = (
            "def test_one():\n    assert 1 == 1\n\n"
            "def test_two():\n    assert 2 == 2\n"
        )
        mock_client = _make_mock_client(code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            result = generate_pytest_from_criteria("goal-6", "Two things must hold")

        assert result.count("def test_") == 2


# ---------------------------------------------------------------------------
# 4. Malformed input handling
# ---------------------------------------------------------------------------

class TestMalformedInputHandling:
    def test_api_returns_no_def_test_raises(self):
        bad_code = "# no functions here\nx = 1\nassert x == 1\n"
        mock_client = _make_mock_client(bad_code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            with pytest.raises(ValueError, match="def test_"):
                generate_pytest_from_criteria("goal-bad", "Something should happen")

    def test_api_returns_no_assert_raises(self):
        bad_code = "def test_missing_assert():\n    x = compute()\n"
        mock_client = _make_mock_client(bad_code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            with pytest.raises(ValueError, match="assert"):
                generate_pytest_from_criteria("goal-bad2", "Another criterion")

    def test_garbage_criteria_text_still_calls_api(self):
        """Non-empty garbage text should not raise — it is forwarded to the API."""
        code = "def test_garbage():\n    assert True\n"
        mock_client = _make_mock_client(code)

        with patch("secretary.harness_generator.anthropic.Anthropic", return_value=mock_client):
            result = generate_pytest_from_criteria("goal-garbage", "!@#$%^&*()")

        mock_client.messages.create.assert_called_once()
        assert "def test_" in result


# ---------------------------------------------------------------------------
# 5. Empty criteria edge case
# ---------------------------------------------------------------------------

class TestEmptyCriteriaEdgeCase:
    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            generate_pytest_from_criteria("goal-empty", "")

    def test_whitespace_only_raises_value_error(self):
        with pytest.raises(ValueError):
            generate_pytest_from_criteria("goal-ws", "   \n\t  \n  ")

    def test_error_message_mentions_success_criteria(self):
        with pytest.raises(ValueError, match="success_criteria"):
            generate_pytest_from_criteria("goal-msg", "")
