"""Harness generator — generate pytest code from goal success criteria via Claude API."""
from __future__ import annotations

import re
import types

try:
    import anthropic  # type: ignore[import]
except ImportError:
    # Build a minimal stub so the module is importable when the anthropic
    # package is not installed (e.g. in test environments that mock it).
    # Tests patch `secretary.harness_generator.anthropic.Anthropic`, so the
    # name `anthropic` must exist in this module's namespace.
    _stub = types.ModuleType("anthropic")

    class _StubAnthropicClient:
        """Placeholder raised at runtime if anthropic is truly absent."""
        def __init__(self, *args, **kwargs):  # noqa: D107
            raise ImportError(
                "The 'anthropic' package is required but not installed. "
                "Run: pip install anthropic"
            )

    _stub.Anthropic = _StubAnthropicClient  # type: ignore[attr-defined]
    anthropic = _stub  # type: ignore[assignment]


def _parse_criteria(success_criteria: str) -> list[str]:
    """Parse success_criteria text into a list of individual criterion strings.

    Handles plain text lines, bullet lists (-, *, •) and numbered lists (1. 2.).
    Blank lines and whitespace-only lines are skipped.
    """
    lines = success_criteria.splitlines()
    items: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Strip leading bullet / number markers
        stripped = re.sub(r"^(\d+[.)]\s+|[-*•]\s+)", "", stripped).strip()
        if stripped:
            items.append(stripped)
    return items


def generate_pytest_from_criteria(goal_id: str, success_criteria: str) -> str:
    """Use Claude to generate pytest-compatible test code from goal success criteria.

    Parameters
    ----------
    goal_id:
        Identifier for the goal (used in the prompt so Claude knows context).
    success_criteria:
        Human-readable description of what the code under test must satisfy.

    Returns
    -------
    str
        Raw Python source code string containing one or more ``def test_`` functions
        with ``assert`` statements.

    Raises
    ------
    ValueError
        If *success_criteria* is empty / whitespace-only, or if the generated code
        does not contain both ``def test_`` and ``assert``.
    """
    criteria_items = _parse_criteria(success_criteria)
    if not criteria_items:
        raise ValueError(
            "success_criteria must not be empty — provide at least one criterion."
        )

    criteria_block = "\n".join(f"- {item}" for item in criteria_items)
    prompt = (
        f"You are an expert Python test engineer.\n"
        f"Goal ID: {goal_id}\n\n"
        f"Success criteria:\n{criteria_block}\n\n"
        f"Write pytest test functions that verify every criterion above. "
        f"Return ONLY raw Python code (no markdown fences, no explanation). "
        f"Each function must start with 'def test_' and contain at least one 'assert' statement."
    )

    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw: str = response.content[0].text

    # Strip markdown code fences if the model added them
    raw = re.sub(r"^```(?:python)?\s*\n?", "", raw.strip(), flags=re.MULTILINE)
    raw = re.sub(r"\n?```$", "", raw.strip(), flags=re.MULTILINE)
    raw = raw.strip()

    if "def test_" not in raw:
        raise ValueError(
            f"Generated code does not contain 'def test_' — cannot use as pytest suite.\n"
            f"Raw output:\n{raw[:500]}"
        )
    if not re.search(r"\bassert\b", raw):
        raise ValueError(
            f"Generated code does not contain 'assert' — cannot use as pytest suite.\n"
            f"Raw output:\n{raw[:500]}"
        )

    return raw
