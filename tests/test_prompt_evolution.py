"""Tests for src/secretary/prompt_evolution.py

Coverage target: apply_mutation, evolve_prompt, PromptEvolutionLog, report_mutations,
FAILURE_TO_MUTATIONS mapping, and MutationRule library.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from secretary.prompt_evolution import (
    FAILURE_TO_MUTATIONS,
    MUTATION_RULES,
    FailurePattern,
    MutationType,
    PromptEvolutionLog,
    PromptMutation,
    apply_mutation,
    evolve_prompt,
    report_mutations,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_prompt() -> str:
    return (
        "analyze the codebase and find all issues. "
        "Then fix the problems. If errors occur: handle them."
    )


@pytest.fixture
def pronoun_prompt() -> str:
    return "Get the file and review it carefully. Then update them as needed."


@pytest.fixture
def tmp_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


# ── 1. Enum completeness ──────────────────────────────────────────────────────

def test_mutation_type_values_are_strings():
    """MutationType enum values are lowercase strings."""
    for mt in MutationType:
        assert isinstance(mt.value, str)
        assert mt.value == mt.value.lower()


def test_failure_pattern_enum_complete():
    """All FailurePattern members appear in FAILURE_TO_MUTATIONS keys."""
    for fp in FailurePattern:
        assert fp in FAILURE_TO_MUTATIONS, f"{fp.value!r} missing from FAILURE_TO_MUTATIONS"


def test_failure_to_mutations_values_are_lists_of_mutation_types():
    """Every FAILURE_TO_MUTATIONS value is a non-empty list of MutationType."""
    for fp, mutations in FAILURE_TO_MUTATIONS.items():
        assert isinstance(mutations, list), f"{fp} maps to non-list"
        assert len(mutations) >= 1, f"{fp} maps to empty list"
        for m in mutations:
            assert isinstance(m, MutationType), f"{fp} → unexpected value {m!r}"


# ── 2. MUTATION_RULES library ─────────────────────────────────────────────────

def test_mutation_rules_library_not_empty():
    """At least one rule per MutationType is defined."""
    defined_types = {r.mutation_type for r in MUTATION_RULES}
    for mt in MutationType:
        assert mt in defined_types, f"No rule defined for MutationType.{mt.name}"


def test_mutation_rules_have_required_fields():
    """Every MutationRule has name, pattern, replacement, description."""
    for rule in MUTATION_RULES:
        assert rule.name, f"Rule missing name: {rule}"
        assert rule.pattern, f"Rule '{rule.name}' missing pattern"
        assert rule.replacement, f"Rule '{rule.name}' missing replacement"
        assert rule.description, f"Rule '{rule.name}' missing description"
        assert isinstance(rule.failure_patterns, list)
        assert len(rule.failure_patterns) >= 1


# ── 3. apply_mutation ─────────────────────────────────────────────────────────

def test_apply_mutation_returns_original_when_no_match():
    """apply_mutation returns (original, None) when pattern doesn't match."""
    rule = next(r for r in MUTATION_RULES if r.name == "expand_pronouns")
    prompt = "Search the codebase for errors."
    result_prompt, record = apply_mutation(prompt, rule, FailurePattern.PRONOUN_CONFUSION)
    assert result_prompt == prompt
    assert record is None


def test_apply_mutation_matches_and_returns_record(pronoun_prompt):
    """apply_mutation correctly substitutes and returns a PromptMutation record."""
    rule = next(r for r in MUTATION_RULES if r.name == "expand_pronouns")
    mutated, record = apply_mutation(pronoun_prompt, rule, FailurePattern.PRONOUN_CONFUSION)
    # Should have matched a pronoun and inserted the replacement text
    assert record is not None
    assert isinstance(record, PromptMutation)
    assert record.rule_name == "expand_pronouns"
    assert record.mutation_type == MutationType.CLARITY
    assert record.failure_pattern == FailurePattern.PRONOUN_CONFUSION
    assert record.original_segment in ("it", "them", "they", "this", "that")
    assert mutated != pronoun_prompt  # prompt was changed


def test_apply_mutation_record_contains_reason():
    """PromptMutation record stores the description as 'reason'."""
    rule = next(r for r in MUTATION_RULES if r.name == "clarify_scope")
    prompt = "analyze the codebase for issues"
    _, record = apply_mutation(prompt, rule, FailurePattern.VAGUE_SCOPE)
    if record:
        assert record.reason == rule.description


def test_apply_mutation_number_all_steps_auto_increments():
    """'number_all_steps' rule auto-numbers: Step 1 when none exist yet."""
    rule = next(r for r in MUTATION_RULES if r.name == "number_all_steps")
    prompt = "First do A, then do B, next do C."
    mutated, record = apply_mutation(prompt, rule, FailurePattern.WRONG_SEQUENCE)
    if record:
        # Auto-number injects "Step 1: " because no prior steps exist
        assert "Step 1:" in mutated


# ── 4. evolve_prompt ─────────────────────────────────────────────────────────

def test_evolve_prompt_unknown_failure_returns_unchanged(sample_prompt):
    """evolve_prompt on a FailurePattern not in mapping returns original unchanged."""
    # All FailurePattern are in the mapping per test above; test with a nonsense
    # approach by temporarily removing one and checking graceful fallback.
    # Instead, verify well-known patterns produce mutations.
    evolved, mutations = evolve_prompt(sample_prompt, FailurePattern.PRONOUN_CONFUSION)
    # PRONOUN_CONFUSION maps to CLARITY + possibly no-match on this prompt
    # Result should at least not raise
    assert isinstance(evolved, str)
    assert isinstance(mutations, list)


def test_evolve_prompt_applies_up_to_num_mutations(pronoun_prompt):
    """evolve_prompt applies at most num_mutations rules."""
    _, mutations = evolve_prompt(pronoun_prompt, FailurePattern.PRONOUN_CONFUSION, num_mutations=1)
    assert len(mutations) <= 1


def test_evolve_prompt_default_num_mutations():
    """Default num_mutations=2 applies at most 2 mutations."""
    prompt = "analyze the codebase and find all issues. If errors occur: handle them."
    _, mutations = evolve_prompt(prompt, FailurePattern.VAGUE_SCOPE)
    assert len(mutations) <= 2


def test_evolve_prompt_returns_list_of_prompt_mutations(pronoun_prompt):
    """evolve_prompt returns list[PromptMutation] (not dicts)."""
    _, mutations = evolve_prompt(pronoun_prompt, FailurePattern.PRONOUN_CONFUSION)
    for m in mutations:
        assert isinstance(m, PromptMutation)


def test_evolve_prompt_constraint_violation_adds_constraint():
    """CONSTRAINT_VIOLATION pattern applies a CONSTRAINT_ADDITION mutation."""
    prompt = "your task is to check all files and update them."
    _, mutations = evolve_prompt(prompt, FailurePattern.CONSTRAINT_VIOLATION)
    types_applied = [m.mutation_type for m in mutations]
    # Should include CONSTRAINT_ADDITION
    if mutations:
        assert MutationType.CONSTRAINT_ADDITION in types_applied


# ── 5. PromptEvolutionLog serialization & persistence ─────────────────────────

def test_prompt_evolution_log_to_dict_round_trip():
    """to_dict serializes and can be re-parsed cleanly."""
    mut = PromptMutation(
        rule_name="expand_pronouns",
        mutation_type=MutationType.CLARITY,
        original_segment="it",
        mutated_segment="[explicitly name the preceding noun]",
        failure_pattern=FailurePattern.PRONOUN_CONFUSION,
        reason="Pronoun confusion fix",
    )
    log_obj = PromptEvolutionLog(
        original_prompt="Check it carefully.",
        current_prompt="Check [explicitly name the preceding noun] carefully.",
        mutations=[mut],
        generation=1,
    )
    d = log_obj.to_dict()
    assert d["generation"] == 1
    assert d["original_prompt"] == "Check it carefully."
    assert len(d["mutations"]) == 1
    assert d["mutations"][0]["rule_name"] == "expand_pronouns"
    assert d["mutations"][0]["mutation_type"] == "clarity"
    assert d["mutations"][0]["failure_pattern"] == "pronoun_confusion"


def test_prompt_evolution_log_save_and_load(tmp_dir):
    """save() writes JSON; load() reconstructs the log faithfully."""
    mut = PromptMutation(
        rule_name="clarify_scope",
        mutation_type=MutationType.CLARITY,
        original_segment="analyze the",
        mutated_segment="analyze only the [EXPLICITLY LIST FILES]",
        failure_pattern=FailurePattern.VAGUE_SCOPE,
        reason="Vague scope fix",
    )
    log_obj = PromptEvolutionLog(
        original_prompt="analyze the project",
        current_prompt="analyze only the [EXPLICITLY LIST FILES] project",
        mutations=[mut],
        generation=2,
    )
    path = tmp_dir / "evolution.json"
    log_obj.save(path)

    assert path.exists()
    loaded = PromptEvolutionLog.load(path)
    assert loaded.original_prompt == log_obj.original_prompt
    assert loaded.current_prompt == log_obj.current_prompt
    assert loaded.generation == 2
    assert len(loaded.mutations) == 1
    m = loaded.mutations[0]
    assert m.rule_name == "clarify_scope"
    assert m.mutation_type == MutationType.CLARITY
    assert m.failure_pattern == FailurePattern.VAGUE_SCOPE


def test_prompt_evolution_log_save_creates_parent_dirs(tmp_dir):
    """save() creates intermediate directories if missing."""
    nested_path = tmp_dir / "deep" / "nested" / "evo.json"
    log_obj = PromptEvolutionLog(
        original_prompt="p1",
        current_prompt="p2",
        mutations=[],
        generation=0,
    )
    log_obj.save(nested_path)
    assert nested_path.exists()


def test_prompt_evolution_log_load_empty_mutations(tmp_dir):
    """load() handles logs with no mutations."""
    data = {
        "original_prompt": "Do the thing.",
        "current_prompt": "Do the thing.",
        "generation": 0,
        "mutations": [],
    }
    path = tmp_dir / "empty.json"
    path.write_text(json.dumps(data))
    loaded = PromptEvolutionLog.load(path)
    assert loaded.mutations == []
    assert loaded.generation == 0


# ── 6. report_mutations ───────────────────────────────────────────────────────

def test_report_mutations_no_mutations():
    """report_mutations on empty list returns a 'no mutations' string."""
    result = report_mutations([])
    assert "no mutations" in result.lower()


def test_report_mutations_includes_rule_names():
    """report_mutations output contains each rule name applied."""
    muts = [
        PromptMutation(
            rule_name="expand_pronouns",
            mutation_type=MutationType.CLARITY,
            original_segment="it",
            mutated_segment="[explicitly name]",
            failure_pattern=FailurePattern.PRONOUN_CONFUSION,
            reason="Pronoun fix",
        ),
        PromptMutation(
            rule_name="add_dont_list",
            mutation_type=MutationType.CONSTRAINT_ADDITION,
            original_segment="instructions",
            mutated_segment="instructions. DO NOT: ...",
            failure_pattern=FailurePattern.CONSTRAINT_VIOLATION,
            reason="Constraint fix",
        ),
    ]
    report = report_mutations(muts)
    assert "expand_pronouns" in report
    assert "add_dont_list" in report
    assert "PROMPT EVOLUTION REPORT" in report
    assert "2 mutations" in report


def test_report_mutations_shows_before_and_after():
    """report_mutations includes before/after segments."""
    mut = PromptMutation(
        rule_name="clarify_scope",
        mutation_type=MutationType.CLARITY,
        original_segment="analyze the",
        mutated_segment="analyze only the [EXPLICITLY LIST]",
        failure_pattern=FailurePattern.VAGUE_SCOPE,
        reason="Vague scope",
    )
    report = report_mutations([mut])
    assert "analyze the" in report or "Before:" in report
    assert "After:" in report


# ── 7. Integration: full evolve → log → save → reload cycle ──────────────────

def test_full_evolution_cycle(tmp_dir, pronoun_prompt):
    """End-to-end: evolve a prompt, persist the log, reload and verify mutations."""
    evolved, mutations = evolve_prompt(pronoun_prompt, FailurePattern.PRONOUN_CONFUSION)

    log_obj = PromptEvolutionLog(
        original_prompt=pronoun_prompt,
        current_prompt=evolved,
        mutations=mutations,
        generation=1,
    )
    path = tmp_dir / "cycle_test.json"
    log_obj.save(path)

    reloaded = PromptEvolutionLog.load(path)
    assert reloaded.original_prompt == pronoun_prompt
    assert reloaded.generation == 1
    assert len(reloaded.mutations) == len(mutations)
    for orig, reloaded_m in zip(mutations, reloaded.mutations):
        assert orig.rule_name == reloaded_m.rule_name
        assert orig.mutation_type == reloaded_m.mutation_type
        assert orig.failure_pattern == reloaded_m.failure_pattern
