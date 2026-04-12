#!/usr/bin/env python3
"""Example: GEPA Prompt Evolution in Action.

Demonstrates how the schema automatically improves prompts based on failure patterns.
"""
from src.secretary.prompt_evolution import (
    evolve_prompt,
    FailurePattern,
    report_mutations,
    PromptEvolutionLog,
)

def example_clarity_mutation():
    """Example 1: Fix pronoun confusion via CLARITY mutation."""
    print("\n" + "="*70)
    print("EXAMPLE 1: CLARITY Mutation (Pronoun Confusion)")
    print("="*70)
    
    # Original task failed because "it" was ambiguous
    original_prompt = "Read the file and analyze it for bugs. Fix it if needed."
    print(f"\nOriginal prompt:\n  {original_prompt}")
    
    # Evolution
    evolved, mutations = evolve_prompt(
        original_prompt,
        FailurePattern.PRONOUN_CONFUSION,
        num_mutations=1
    )
    print(f"\nEvolved prompt:\n  {evolved}")
    print(report_mutations(mutations))


def example_specificity_mutation():
    """Example 2: Reduce over-generalization via SPECIFICITY mutation."""
    print("\n" + "="*70)
    print("EXAMPLE 2: SPECIFICITY Mutation (Over-Generalization)")
    print("="*70)
    
    # Original task was too broad
    original_prompt = "Find all bugs in the codebase and fix them."
    print(f"\nOriginal prompt:\n  {original_prompt}")
    
    # Evolution
    evolved, mutations = evolve_prompt(
        original_prompt,
        FailurePattern.OVER_GENERALIZATION,
        num_mutations=1
    )
    print(f"\nEvolved prompt:\n  {evolved}")
    print(report_mutations(mutations))


def example_instruction_order_mutation():
    """Example 3: Add validation via INSTRUCTION_ORDER mutation."""
    print("\n" + "="*70)
    print("EXAMPLE 3: INSTRUCTION_ORDER Mutation (Skipped Validation)")
    print("="*70)
    
    # Original task skipped validation step
    original_prompt = "Then edit the configuration file to enable debug mode."
    print(f"\nOriginal prompt:\n  {original_prompt}")
    
    # Evolution
    evolved, mutations = evolve_prompt(
        original_prompt,
        FailurePattern.SKIPPED_VALIDATION,
        num_mutations=1
    )
    print(f"\nEvolved prompt:\n  {evolved}")
    print(report_mutations(mutations))


def example_context_injection_mutation():
    """Example 4: Add format example via CONTEXT_INJECTION mutation."""
    print("\n" + "="*70)
    print("EXAMPLE 4: CONTEXT_INJECTION Mutation (No Template)")
    print("="*70)
    
    # Original task lacked format specification
    original_prompt = "Analyze the results and return the output."
    print(f"\nOriginal prompt:\n  {original_prompt}")
    
    # Evolution
    evolved, mutations = evolve_prompt(
        original_prompt,
        FailurePattern.NO_TEMPLATE,
        num_mutations=1
    )
    print(f"\nEvolved prompt:\n  {evolved}")
    print(report_mutations(mutations))


def example_constraint_addition_mutation():
    """Example 5: Add guardrails via CONSTRAINT_ADDITION mutation."""
    print("\n" + "="*70)
    print("EXAMPLE 5: CONSTRAINT_ADDITION Mutation (Constraint Violation)")
    print("="*70)
    
    # Original task didn't enforce prohibitions
    original_prompt = "Your task is to improve the codebase."
    print(f"\nOriginal prompt:\n  {original_prompt}")
    
    # Evolution
    evolved, mutations = evolve_prompt(
        original_prompt,
        FailurePattern.CONSTRAINT_VIOLATION,
        num_mutations=1
    )
    print(f"\nEvolved prompt:\n  {evolved}")
    print(report_mutations(mutations))


def example_multi_mutation_evolution():
    """Example 6: Multi-round evolution for complex failures."""
    print("\n" + "="*70)
    print("EXAMPLE 6: Multi-Mutation Evolution (Comprehensive)")
    print("="*70)
    
    # Complex failure requiring multiple mutations
    original_prompt = "Analyze the code and consider improvements. Fix issues if found."
    print(f"\nOriginal prompt (generation 0):\n  {original_prompt}")
    
    # Round 1: Fix ambiguity
    evolved_1, mutations_1 = evolve_prompt(
        original_prompt,
        FailurePattern.AMBIGUOUS_GOAL,
        num_mutations=2
    )
    print(f"\nAfter CLARITY mutations (generation 1):\n  {evolved_1}")
    
    # Round 2: Reduce scope via specificity
    evolved_2, mutations_2 = evolve_prompt(
        evolved_1,
        FailurePattern.OVER_GENERALIZATION,
        num_mutations=2
    )
    print(f"\nAfter SPECIFICITY mutations (generation 2):\n  {evolved_2}")
    
    # Persist evolution history
    log = PromptEvolutionLog(
        original_prompt=original_prompt,
        current_prompt=evolved_2,
        mutations=mutations_1 + mutations_2,
        generation=2,
    )
    
    print(f"\n\nEvolution Log Summary:")
    print(f"  Original: {original_prompt}")
    print(f"  Current:  {evolved_2}")
    print(f"  Generations: {log.generation}")
    print(f"  Total mutations applied: {len(log.mutations)}")
    for i, mut in enumerate(log.mutations, 1):
        print(f"    {i}. {mut.rule_name} ({mut.mutation_type.value})")


if __name__ == "__main__":
    example_clarity_mutation()
    example_specificity_mutation()
    example_instruction_order_mutation()
    example_context_injection_mutation()
    example_constraint_addition_mutation()
    example_multi_mutation_evolution()
    
    print("\n" + "="*70)
    print("All examples completed!")
    print("="*70)
