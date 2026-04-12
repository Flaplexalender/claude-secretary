"""GEPA-style Prompt Evolution Schema.

Maps failure patterns to prompt mutation rules. Supports iterative prompt
refinement across campaigns by learning from historical failures.

GEPA = Genetic/Evolutionary Prompt Adaptation: models use failure signals
to mutate and improve prompts across generations.

Mutation Rules:
  1. CLARITY mutations: expand ambiguous terms, add context, remove pronouns
  2. SPECIFICITY mutations: add constraints, narrow scope, enumerate options
  3. INSTRUCTION_ORDER mutations: reorder steps, prioritize checks, lead with goal
  4. CONTEXT_INJECTION mutations: provide examples, templates, format specs
  5. CONSTRAINT_ADDITION mutations: add guardrails, error handling, edge cases
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class MutationType(Enum):
    """Mutation rule types in GEPA schema."""
    CLARITY = "clarity"                          # Remove ambiguity
    SPECIFICITY = "specificity"                  # Add constraints/narrowing
    INSTRUCTION_ORDER = "instruction_order"      # Reorder steps/priorities
    CONTEXT_INJECTION = "context_injection"      # Examples, templates, specs
    CONSTRAINT_ADDITION = "constraint_addition"  # Guardrails, error handling


class FailurePattern(Enum):
    """Failure categories linked to mutation rules."""
    # Clarity failures: model misunderstands the goal
    AMBIGUOUS_GOAL = "ambiguous_goal"            # Goal unclear → CLARITY
    MISINTERPRETED_CONSTRAINT = "misinterpreted_constraint"  # Constraint not followed → CLARITY
    PRONOUN_CONFUSION = "pronoun_confusion"      # "it" vs "them" mismatched → CLARITY
    VAGUE_SCOPE = "vague_scope"                  # Unclear what to include → CLARITY

    # Specificity failures: model produces too broad/narrow output
    OVER_GENERALIZATION = "over_generalization"  # Too broad answer → SPECIFICITY
    MISSING_DETAILS = "missing_details"          # Incomplete output → SPECIFICITY
    WRONG_SCOPE = "wrong_scope"                  # Worked on wrong files/items → SPECIFICITY
    TOOL_MISUSE = "tool_misuse"                  # Used wrong tool → SPECIFICITY

    # Instruction order failures: model skips steps or gets order wrong
    SKIPPED_VALIDATION = "skipped_validation"    # Didn't validate before proceeding → INSTRUCTION_ORDER
    WRONG_SEQUENCE = "wrong_sequence"            # Did steps in wrong order → INSTRUCTION_ORDER
    PREMATURE_TERMINATION = "premature_termination"  # Stopped too early → INSTRUCTION_ORDER
    MISSING_FINAL_CHECK = "missing_final_check"  # No verification step → INSTRUCTION_ORDER

    # Context injection failures: model lacks reference material
    NO_TEMPLATE = "no_template"                  # Produced wrong format → CONTEXT_INJECTION
    HALLUCINATED_API = "hallucinated_api"        # Invented tool behavior → CONTEXT_INJECTION
    INCONSISTENT_STYLE = "inconsistent_style"    # Format mismatch → CONTEXT_INJECTION
    MISSING_EXAMPLE = "missing_example"          # Didn't understand pattern → CONTEXT_INJECTION

    # Constraint failures: model ignored guardrails
    CONSTRAINT_VIOLATION = "constraint_violation"  # Ignored "don't X" → CONSTRAINT_ADDITION
    EDGE_CASE_ERROR = "edge_case_error"          # Failed on boundary case → CONSTRAINT_ADDITION
    TIMEOUT_EXCEEDED = "timeout_exceeded"        # Too slow/expensive → CONSTRAINT_ADDITION
    RESOURCE_EXHAUSTION = "resource_exhaustion"  # Memory/token limit hit → CONSTRAINT_ADDITION


# ── Failure Pattern → Mutation Rule Mapping ────────────────────────────

FAILURE_TO_MUTATIONS: dict[FailurePattern, list[MutationType]] = {
    # Clarity mutations
    FailurePattern.AMBIGUOUS_GOAL: [MutationType.CLARITY, MutationType.SPECIFICITY],
    FailurePattern.MISINTERPRETED_CONSTRAINT: [MutationType.CLARITY, MutationType.CONSTRAINT_ADDITION],
    FailurePattern.PRONOUN_CONFUSION: [MutationType.CLARITY],
    FailurePattern.VAGUE_SCOPE: [MutationType.CLARITY, MutationType.SPECIFICITY],

    # Specificity mutations
    FailurePattern.OVER_GENERALIZATION: [MutationType.SPECIFICITY, MutationType.CONSTRAINT_ADDITION],
    FailurePattern.MISSING_DETAILS: [MutationType.SPECIFICITY, MutationType.CONTEXT_INJECTION],
    FailurePattern.WRONG_SCOPE: [MutationType.SPECIFICITY, MutationType.CONSTRAINT_ADDITION],
    FailurePattern.TOOL_MISUSE: [MutationType.SPECIFICITY, MutationType.CONTEXT_INJECTION],

    # Instruction order mutations
    FailurePattern.SKIPPED_VALIDATION: [MutationType.INSTRUCTION_ORDER],
    FailurePattern.WRONG_SEQUENCE: [MutationType.INSTRUCTION_ORDER],
    FailurePattern.PREMATURE_TERMINATION: [MutationType.INSTRUCTION_ORDER, MutationType.CONSTRAINT_ADDITION],
    FailurePattern.MISSING_FINAL_CHECK: [MutationType.INSTRUCTION_ORDER],

    # Context injection mutations
    FailurePattern.NO_TEMPLATE: [MutationType.CONTEXT_INJECTION, MutationType.SPECIFICITY],
    FailurePattern.HALLUCINATED_API: [MutationType.CONTEXT_INJECTION, MutationType.SPECIFICITY],
    FailurePattern.INCONSISTENT_STYLE: [MutationType.CONTEXT_INJECTION],
    FailurePattern.MISSING_EXAMPLE: [MutationType.CONTEXT_INJECTION],

    # Constraint mutations
    FailurePattern.CONSTRAINT_VIOLATION: [MutationType.CONSTRAINT_ADDITION, MutationType.CLARITY],
    FailurePattern.EDGE_CASE_ERROR: [MutationType.CONSTRAINT_ADDITION, MutationType.SPECIFICITY],
    FailurePattern.TIMEOUT_EXCEEDED: [MutationType.CONSTRAINT_ADDITION],
    FailurePattern.RESOURCE_EXHAUSTION: [MutationType.CONSTRAINT_ADDITION],
}


# ── Concrete Mutation Rules ─────────────────────────────────────────────

@dataclasses.dataclass
class MutationRule:
    """Single mutation rule: pattern + transformation."""
    name: str                                    # "expand_pronouns", "add_enumeration", etc.
    mutation_type: MutationType
    pattern: str                                 # Regex to match in prompt
    replacement: str                             # Template to substitute
    description: str                             # Explain what it fixes
    failure_patterns: list[FailurePattern]       # Triggered by these failures


# ━━ MUTATION RULE LIBRARY ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Each rule targets a specific failure pattern → mutation pairing.
# Patterns use Python regex; replacement can reference captured groups ($1, $2).

MUTATION_RULES: list[MutationRule] = [
    # ────────────────────────────────────────────────────────────────
    # 1. CLARITY Mutations: Remove ambiguity, expand vague terms, resolve pronouns
    # ────────────────────────────────────────────────────────────────

    MutationRule(
        name="expand_pronouns",
        mutation_type=MutationType.CLARITY,
        pattern=r"\b(it|them|they|this|that)\b",
        replacement="[explicitly name the preceding noun: e.g., use 'the email' instead of 'it']",
        description="Pronoun confusion: expand ambiguous pronouns to explicit references",
        failure_patterns=[FailurePattern.PRONOUN_CONFUSION, FailurePattern.AMBIGUOUS_GOAL],
    ),
    MutationRule(
        name="clarify_scope",
        mutation_type=MutationType.CLARITY,
        pattern=r"analyze (the|all|some|your)",
        replacement=r"analyze only the\1 [EXPLICITLY LIST FILES/DOMAINS, do not infer]",
        description="Vague scope: force explicit enumeration of what to analyze",
        failure_patterns=[FailurePattern.VAGUE_SCOPE, FailurePattern.WRONG_SCOPE],
    ),
    MutationRule(
        name="inline_definition",
        mutation_type=MutationType.CLARITY,
        pattern=r"(high|low|medium|complex|simple|important)",
        replacement=r"\1 (defined as: [INSERT MEASURABLE CRITERIA])",
        description="Ambiguous adjectives: add measurable criteria (e.g., 'high = >5 years experience')",
        failure_patterns=[FailurePattern.AMBIGUOUS_GOAL, FailurePattern.MISINTERPRETED_CONSTRAINT],
    ),
    MutationRule(
        name="reword_double_negatives",
        mutation_type=MutationType.CLARITY,
        pattern=r"don't\s+(?:not\s+|un|dis)(\w+)",
        replacement=r"explicitly avoid \1 — instead [INSERT POSITIVE ACTION]",
        description="Double negatives confuse models: convert to positive instructions",
        failure_patterns=[FailurePattern.MISINTERPRETED_CONSTRAINT],
    ),

    # ────────────────────────────────────────────────────────────────
    # 2. SPECIFICITY Mutations: Narrow scope, add constraints, enumerate options
    # ────────────────────────────────────────────────────────────────

    MutationRule(
        name="add_enumeration",
        mutation_type=MutationType.SPECIFICITY,
        pattern=r"consider (the following|these options|all [^.]+)",
        replacement=r"consider ONLY these options, in this priority order: (1) [OPTION A] (2) [OPTION B] (3) [OPTION C]",
        description="Over-generalization: force enumeration and prioritization of options",
        failure_patterns=[FailurePattern.OVER_GENERALIZATION, FailurePattern.MISSING_DETAILS],
    ),
    MutationRule(
        name="bound_scope_to_files",
        mutation_type=MutationType.SPECIFICITY,
        pattern=r"analyze (the (codebase|project))",
        replacement=r"analyze ONLY these files: [src/main.py, src/utils.py] (do not edit outside this list)",
        description="Wrong scope: explicitly list files to include/exclude",
        failure_patterns=[FailurePattern.WRONG_SCOPE, FailurePattern.TOOL_MISUSE],
    ),
    MutationRule(
        name="add_cardinality_constraint",
        mutation_type=MutationType.SPECIFICITY,
        pattern=r"find (all|any|some) (\w+)",
        replacement=r"find exactly [NUMBER] \2 (if more exist, report only top 3 by [RANKING CRITERIA])",
        description="Missing details or over-generalization: add cardinality constraints",
        failure_patterns=[FailurePattern.MISSING_DETAILS, FailurePattern.OVER_GENERALIZATION],
    ),
    MutationRule(
        name="specify_tool_usage",
        mutation_type=MutationType.SPECIFICITY,
        pattern=r"(find|search|read) (\w+)",
        replacement=r"\1 \2 using [TOOL: grep_search | file_read | run_command]. Do NOT use [FORBIDDEN TOOL]",
        description="Tool misuse: explicitly list allowed/forbidden tools",
        failure_patterns=[FailurePattern.TOOL_MISUSE],
    ),

    # ────────────────────────────────────────────────────────────────
    # 3. INSTRUCTION_ORDER Mutations: Reorder steps, prioritize checks, lead with goal
    # ────────────────────────────────────────────────────────────────

    MutationRule(
        name="add_validation_gate",
        mutation_type=MutationType.INSTRUCTION_ORDER,
        pattern=r"(then|next|after that) (fix|implement|edit)",
        replacement=r"[VALIDATION: verify [INPUT STATE] before proceeding]\n\1 \2",
        description="Skipped validation: add explicit validation step before mutations",
        failure_patterns=[FailurePattern.SKIPPED_VALIDATION],
    ),
    MutationRule(
        name="lead_with_goal",
        mutation_type=MutationType.INSTRUCTION_ORDER,
        pattern=r"^(Read|Check|Search)",
        replacement=r"**GOAL: [INSERT PRIMARY OBJECTIVE]**\n\nTo achieve this:\n\1",
        description="Wrong sequence: lead with the desired outcome, then detailed steps",
        failure_patterns=[FailurePattern.WRONG_SEQUENCE, FailurePattern.AMBIGUOUS_GOAL],
    ),
    MutationRule(
        name="add_final_verification",
        mutation_type=MutationType.INSTRUCTION_ORDER,
        pattern=r"(finally|in summary|your task is)(.+?)$",
        replacement=r"\1\2\n\n**FINAL VERIFICATION**: Confirm [CHECKSUM/VALIDATION] before reporting results",
        description="Missing final check: add explicit verification step at end",
        failure_patterns=[FailurePattern.MISSING_FINAL_CHECK, FailurePattern.PREMATURE_TERMINATION],
    ),
    MutationRule(
        name="number_all_steps",
        mutation_type=MutationType.INSTRUCTION_ORDER,
        pattern=r"(?<![\d\.])(then|next|after|also|additionally) ",
        replacement=r"Step N: ",  # N will be auto-numbered in apply()
        description="Wrong sequence: number all steps to enforce order",
        failure_patterns=[FailurePattern.WRONG_SEQUENCE],
    ),

    # ────────────────────────────────────────────────────────────────
    # 4. CONTEXT_INJECTION Mutations: Add examples, templates, format specs
    # ────────────────────────────────────────────────────────────────

    MutationRule(
        name="add_format_example",
        mutation_type=MutationType.CONTEXT_INJECTION,
        pattern=r"return (the result|your answer|the output)",
        replacement=r"return the result in this EXACT format:\n```\n[EXAMPLE OUTPUT]\n```",
        description="No template: add concrete example of expected output format",
        failure_patterns=[FailurePattern.NO_TEMPLATE, FailurePattern.INCONSISTENT_STYLE],
    ),
    MutationRule(
        name="document_api_truthfully",
        mutation_type=MutationType.CONTEXT_INJECTION,
        pattern=r"(use|call) (the|this) (\w+) (tool|function|api)",
        replacement=r"\1 \2 \3 \4. BEHAVIOR: [DOCUMENT EXACT SIGNATURE, return type, allowed args]. CONSTRAINTS: [EDGE CASES]",
        description="Hallucinated API: document actual API signatures and constraints",
        failure_patterns=[FailurePattern.HALLUCINATED_API],
    ),
    MutationRule(
        name="inject_reference_example",
        mutation_type=MutationType.CONTEXT_INJECTION,
        pattern=r"(analyze|review|examine|check) (.+?)(for|to)",
        replacement=r"\1 \2 for\3:\n\n**REFERENCE**: Previous successful analysis of [SIMILAR ITEM]:\n[EXAMPLE]\n",
        description="Missing example: inject reference material/previous solutions",
        failure_patterns=[FailurePattern.MISSING_EXAMPLE],
    ),
    MutationRule(
        name="specify_encoding_and_format",
        mutation_type=MutationType.CONTEXT_INJECTION,
        pattern=r"(write|save|create) (a file|output)",
        replacement=r"\1 \2 with encoding=utf-8, format=[JSON|CSV|YAML], line_ending=\\n",
        description="Inconsistent style: specify exact encoding/format/whitespace",
        failure_patterns=[FailurePattern.INCONSISTENT_STYLE],
    ),

    # ────────────────────────────────────────────────────────────────
    # 5. CONSTRAINT_ADDITION Mutations: Add guardrails, error handling, edge cases
    # ────────────────────────────────────────────────────────────────

    MutationRule(
        name="add_dont_list",
        mutation_type=MutationType.CONSTRAINT_ADDITION,
        pattern=r"(your task|you should|instructions)",
        replacement=r"\1. DO NOT: [LIST PROHIBITED ACTIONS]. IF YOU VIOLATE THIS, [CONSEQUENCE]",
        description="Constraint violation: make prohibitions explicit and severe",
        failure_patterns=[FailurePattern.CONSTRAINT_VIOLATION],
    ),
    MutationRule(
        name="guard_edge_cases",
        mutation_type=MutationType.CONSTRAINT_ADDITION,
        pattern=r"(if|when) (\w+) (.+?):",
        replacement=r"\1 \2 \3:\n  THEN [STANDARD ACTION]\nELSE IF [EDGE CASE 1]: [RECOVERY]\nELSE IF [EDGE CASE 2]: [RECOVERY]",
        description="Edge case error: explicitly handle boundary conditions",
        failure_patterns=[FailurePattern.EDGE_CASE_ERROR],
    ),
    MutationRule(
        name="add_timeout_constraint",
        mutation_type=MutationType.CONSTRAINT_ADDITION,
        pattern=r"(complete|finish|perform|execute)(.+?)(task|analysis|check)",
        replacement=r"\1\2\3 within [TIME_LIMIT]. If you exceed it, stop and report progress so far",
        description="Timeout exceeded: add explicit time/turn limit with early exit",
        failure_patterns=[FailurePattern.TIMEOUT_EXCEEDED],
    ),
    MutationRule(
        name="add_resource_guard",
        mutation_type=MutationType.CONSTRAINT_ADDITION,
        pattern=r"(process|iterate|loop)",
        replacement=r"\1 [AT MOST N ITEMS]. If more exist, [PRIORITIZATION RULE]. Stop if resources run low",
        description="Resource exhaustion: add limits on iterations/batching",
        failure_patterns=[FailurePattern.RESOURCE_EXHAUSTION],
    ),
]


# ── Mutation Application Engine ─────────────────────────────────────────────

@dataclasses.dataclass
class PromptMutation:
    """Record of a mutation applied to a prompt."""
    rule_name: str
    mutation_type: MutationType
    original_segment: str
    mutated_segment: str
    failure_pattern: FailurePattern
    reason: str


def apply_mutation(prompt: str, rule: MutationRule, failure: FailurePattern) -> tuple[str, PromptMutation | None]:
    """Apply a single mutation rule to a prompt.

    Returns: (mutated_prompt, mutation_record)
    Returns (original_prompt, None) if the rule pattern doesn't match.
    """
    try:
        match = re.search(rule.pattern, prompt, re.IGNORECASE | re.MULTILINE)
        if not match:
            return prompt, None

        original_segment = match.group(0)
        mutated_segment = rule.replacement

        # Handle special cases (e.g., auto-numbering)
        if rule.name == "number_all_steps":
            steps = re.findall(r"Step \d+:", prompt)
            next_num = len(steps) + 1
            mutated_segment = f"Step {next_num}: "

        mutated_prompt = prompt[:match.start()] + mutated_segment + prompt[match.end():]

        record = PromptMutation(
            rule_name=rule.name,
            mutation_type=rule.mutation_type,
            original_segment=original_segment,
            mutated_segment=mutated_segment,
            failure_pattern=failure,
            reason=rule.description,
        )
        log.debug(f"Applied mutation '{rule.name}' for {failure.value}: {original_segment[:50]}...")
        return mutated_prompt, record
    except Exception as e:
        log.warning(f"Mutation rule '{rule.name}' failed: {e}")
        return prompt, None


def evolve_prompt(prompt: str, failure_pattern: FailurePattern, num_mutations: int = 2) -> tuple[str, list[PromptMutation]]:
    """Evolve a prompt by applying mutations for a detected failure pattern.

    GEPA algorithm:
      1. Look up failure pattern → mutation rules
      2. Select top N rules by priority
      3. Apply each rule in sequence
      4. Return evolved prompt + audit trail

    Args:
      prompt: Original prompt text
      failure_pattern: Detected failure (e.g., AMBIGUOUS_GOAL)
      num_mutations: How many rules to apply (default: 2, max 3 to avoid over-mutating)

    Returns:
      (evolved_prompt, mutations_applied)
    """
    if failure_pattern not in FAILURE_TO_MUTATIONS:
        log.warning(f"Unknown failure pattern: {failure_pattern}")
        return prompt, []

    # Get applicable mutation types for this failure
    mutation_types = FAILURE_TO_MUTATIONS[failure_pattern]

    # Find all rules matching these types
    applicable_rules = [
        r for r in MUTATION_RULES
        if r.mutation_type in mutation_types and failure_pattern in r.failure_patterns
    ]

    if not applicable_rules:
        log.warning(f"No rules found for {failure_pattern.value}")
        return prompt, []

    # Apply top N rules
    evolved = prompt
    mutations: list[PromptMutation] = []
    for rule in applicable_rules[:num_mutations]:
        evolved, mutation = apply_mutation(evolved, rule, failure_pattern)
        if mutation:
            mutations.append(mutation)

    if mutations:
        log.info(f"Evolved prompt via {len(mutations)} mutations for {failure_pattern.value}")
    return evolved, mutations


# ── Persistence & Reporting ────────────────────────────────────────────────

@dataclasses.dataclass
class PromptEvolutionLog:
    """Track all mutations applied to a prompt over its lifetime."""
    original_prompt: str
    current_prompt: str
    mutations: list[PromptMutation] = dataclasses.field(default_factory=list)
    generation: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {
            "original_prompt": self.original_prompt,
            "current_prompt": self.current_prompt,
            "generation": self.generation,
            "mutations": [
                {
                    "rule_name": m.rule_name,
                    "mutation_type": m.mutation_type.value,
                    "failure_pattern": m.failure_pattern.value,
                    "reason": m.reason,
                    "original_segment": m.original_segment,
                    "mutated_segment": m.mutated_segment,
                }
                for m in self.mutations
            ],
        }

    def save(self, path: Path) -> None:
        """Save evolution log to JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        log.info(f"Saved prompt evolution log: {path}")

    @staticmethod
    def load(path: Path) -> PromptEvolutionLog:
        """Load evolution log from JSON."""
        with open(path) as f:
            data = json.load(f)
        log_obj = PromptEvolutionLog(
            original_prompt=data["original_prompt"],
            current_prompt=data["current_prompt"],
            generation=data.get("generation", 0),
        )
        # Rebuild mutations from JSON
        for m_data in data.get("mutations", []):
            m = PromptMutation(
                rule_name=m_data["rule_name"],
                mutation_type=MutationType(m_data["mutation_type"]),
                original_segment=m_data["original_segment"],
                mutated_segment=m_data["mutated_segment"],
                failure_pattern=FailurePattern(m_data["failure_pattern"]),
                reason=m_data["reason"],
            )
            log_obj.mutations.append(m)
        return log_obj


def report_mutations(mutations: list[PromptMutation]) -> str:
    """Generate human-readable mutation report."""
    if not mutations:
        return "(no mutations applied)"
    lines = []
    lines.append(f"\n{'='*70}")
    lines.append(f"PROMPT EVOLUTION REPORT ({len(mutations)} mutations)")
    lines.append(f"{'='*70}\n")
    for i, mut in enumerate(mutations, 1):
        lines.append(f"{i}. {mut.rule_name} [{mut.mutation_type.value}]")
        lines.append(f"   Failure: {mut.failure_pattern.value}")
        lines.append(f"   Reason: {mut.reason}")
        lines.append(f"   Before: {mut.original_segment[:60]}...")
        lines.append(f"   After:  {mut.mutated_segment[:60]}...")
        lines.append("")
    return "\n".join(lines)
