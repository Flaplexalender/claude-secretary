"""Tests for model routing — all offline, no API calls."""
from secretary.config import SecretaryConfig, RoutingConfig, ModelTier
from secretary.router import (
    estimate_complexity,
    select_model,
    get_premium_cost,
    TIER_MULTIPLIERS,
    RoutingDecision,
)


def test_simple_question_routes_low():
    level, score, _, confidence = estimate_complexity("What is Python?")
    assert level == "low"


def test_complex_task_routes_high():
    level, score, _, confidence = estimate_complexity(
        "Refactor the entire authentication system to support multi-tenant "
        "architecture with cross-cutting security concerns across all modules"
    )
    assert level == "high"


def test_medium_task():
    level, score, _, confidence = estimate_complexity("Implement a retry mechanism for the HTTP client")
    assert level == "medium"


def test_short_prompt_is_low():
    level, score, _, confidence = estimate_complexity("fix typo")
    assert level == "low"


def test_numbered_steps_increase_score():
    task = """
    1. Read the config file
    2. Parse the YAML
    3. Validate the schema
    4. Apply defaults
    5. Return the config object
    """
    level, score, _, confidence = estimate_complexity(task)
    assert score >= 3


def test_select_model_default():
    config = SecretaryConfig()
    decision = select_model(config, "Implement a new feature")
    assert decision.tier in ("low", "medium", "high")
    assert decision.model


def test_select_model_forced_tier():
    config = SecretaryConfig()
    decision = select_model(config, "anything", force_tier="high")
    assert decision.tier == "high"
    assert decision.model == "claude-opus-4.7"


def test_select_model_low_tier():
    config = SecretaryConfig()
    decision = select_model(config, "What time is it?", force_tier="low")
    assert decision.model == "claude-haiku-4.5"


def test_scope_reducer():
    level, score, reason, confidence = estimate_complexity("just this file, only one small fix needed")
    assert level == "low"
    assert "scope reducer" in reason


def test_long_prompt_boosts_score():
    task = "word " * 100  # 100 words
    _, score, _, confidence = estimate_complexity(task)
    assert score >= 2


# --- New tests: question framing, file paths, code blocks, confidence ---

def test_question_framing_lowers_score():
    _, score_q, reason, _ = estimate_complexity("How do I configure logging?")
    _, score_cmd, _, _ = estimate_complexity("Configure logging for the project")
    assert "question framing" in reason
    assert score_q < score_cmd


def test_where_question_is_question():
    level, _, reason, _ = estimate_complexity("Where is the config file?")
    assert "question framing" in reason
    assert level == "low"


def test_file_paths_boost_score():
    _, score_with, reason, _ = estimate_complexity("Update src/secretary/router.py to add validation")
    _, score_without, _, _ = estimate_complexity("Update the router to add validation")
    assert "file paths" in reason
    assert score_with > score_without


def test_windows_file_path_detected():
    _, score, reason, _ = estimate_complexity("Read C:\\Users\\jdoe\\project\\main.py and fix the bug")
    assert "file paths" in reason


def test_code_blocks_boost_score():
    task = 'Fix this code:\n```python\ndef foo():\n    pass\n```\nIt should return 42.'
    _, score, reason, _ = estimate_complexity(task)
    assert score >= 2


def test_confidence_high_when_score_decisive():
    # High score → high confidence
    _, score, _, confidence = estimate_complexity(
        "Refactor the entire authentication system to support multi-tenant "
        "architecture with cross-cutting security concerns across all modules"
    )
    assert score >= 4
    assert confidence == "high"


def test_confidence_high_when_score_very_low():
    _, score, _, confidence = estimate_complexity("What is 2+2?")
    assert score <= -1
    assert confidence == "high"


def test_confidence_medium_when_score_ambiguous():
    _, score, _, confidence = estimate_complexity("Implement a logging wrapper")
    assert 0 <= score < 4
    assert confidence == "medium"


def test_estimate_complexity_empty_task():
    """Empty string should not crash."""
    level, score, reason, confidence = estimate_complexity("")
    assert level in ("low", "medium", "high")
    assert isinstance(score, (int, float))
    assert isinstance(reason, str)


def test_routing_decision_has_confidence():
    """RoutingDecision includes confidence from complexity estimation."""
    config = SecretaryConfig()
    decision = select_model(config, "What is 2+2?")
    assert decision.confidence in ("high", "medium")


def test_forced_tier_has_high_confidence():
    """Forced tier should always have high confidence."""
    config = SecretaryConfig()
    decision = select_model(config, "anything", force_tier="low")
    assert decision.confidence == "high"


# ── Cycle 7: Additional coverage ──────────────────────────────


def test_get_premium_cost_known_models():
    """get_premium_cost should return correct multipliers for known models."""
    assert get_premium_cost("claude-haiku-4.5") == 0.33
    assert get_premium_cost("claude-sonnet-4.6") == 1.0
    assert get_premium_cost("claude-opus-4.7") == 3.0


def test_get_premium_cost_unknown_model():
    """Unknown models should default to 1.0 multiplier."""
    assert get_premium_cost("claude-future-5.0") == 1.0
    assert get_premium_cost("unknown") == 1.0


def test_tier_multipliers_dict():
    """TIER_MULTIPLIERS should have entries for all known models."""
    assert len(TIER_MULTIPLIERS) >= 3
    assert all(v >= 0 for v in TIER_MULTIPLIERS.values())  # 0.0 is valid (free models)
    # Must include our three main Claude tiers
    assert "claude-haiku-4.5" in TIER_MULTIPLIERS
    assert "claude-sonnet-4.6" in TIER_MULTIPLIERS
    assert "claude-opus-4.7" in TIER_MULTIPLIERS
    # Must include free models
    assert "gpt-4.1" in TIER_MULTIPLIERS
    assert TIER_MULTIPLIERS["gpt-4.1"] == 0.0


def test_select_model_fallback_to_default_tier():
    """If complexity returns a tier not in config, fall back to default_tier."""
    # Create config with only "medium" tier
    config = SecretaryConfig(
        routing=RoutingConfig(
            tiers={"medium": ModelTier(model="claude-sonnet-4.6")},
            default_tier="medium",
        )
    )
    # A simple question would route to "low", but "low" doesn't exist
    decision = select_model(config, "What is 2+2?")
    assert decision.tier == "medium"  # Fell back to default


def test_select_model_returns_routing_decision():
    """select_model returns a RoutingDecision with all fields populated."""
    config = SecretaryConfig()
    decision = select_model(config, "Implement a new feature")
    assert isinstance(decision, RoutingDecision)
    assert decision.tier in ("low", "medium", "high")
    assert isinstance(decision.model, str)
    assert isinstance(decision.max_turns, int)
    assert isinstance(decision.max_budget_usd, float)
    assert isinstance(decision.reason, str)
    assert decision.confidence in ("high", "medium")


def test_routing_decision_default_confidence():
    """RoutingDecision defaults to 'medium' confidence."""
    rd = RoutingDecision(
        tier="low", model="test", max_turns=5,
        max_budget_usd=0.0, reason="test"
    )
    assert rd.confidence == "medium"


def test_multiple_high_keywords_stack():
    """Multiple high-complexity keywords should stack the score."""
    level, score, reason, _ = estimate_complexity(
        "Refactor and redesign the security audit system with a complete overhaul"
    )
    assert score >= 6  # Each high keyword adds +2
    assert level == "high"


def test_medium_and_low_keywords_mixed():
    """Mixed keywords should balance out."""
    _, score, reason, _ = estimate_complexity(
        "Implement a simple fix for the trivial formatting issue"
    )
    # "implement" (+1), "simple" (-1), "trivial" (-1), "fix" matched by medium
    assert "standard keywords" in reason or "simple keywords" in reason


def test_numbered_steps_two():
    """2 numbered steps give +1 boost."""
    task = "1. Read the file\n2. Write the output"
    _, score1, _, _ = estimate_complexity(task)
    task_none = "Read the file. Write the output."
    _, score2, _, _ = estimate_complexity(task_none)
    assert score1 > score2


def test_very_short_prompt_penalty():
    """Very short prompts (≤5 words) get a penalty."""
    _, _, reason, _ = estimate_complexity("hi")
    assert "very short prompt" in reason


def test_moderate_length_no_reason():
    """Moderate length (6-40 words) should not add length reason."""
    task = "This is a moderately sized prompt with about ten words"
    _, _, reason, _ = estimate_complexity(task)
    assert "long prompt" not in reason
    assert "very short prompt" not in reason


def test_multi_step_task_routes_high_via_select_model():
    """A multi-step refactor task with numbered steps should route to 'high' tier."""
    task = (
        "1. Refactor the authentication module in src/secretary/auth.py\n"
        "2. Redesign the session management across all services\n"
        "3. Migrate the user store from flat file to SQLite\n"
        "4. Add cross-cutting security audit logging\n"
        "5. Rewrite the integration tests for the new architecture\n"
    )
    # Verify complexity estimation rates it high with high confidence
    level, score, reason, confidence = estimate_complexity(task)
    assert level == "high", f"Expected 'high' but got '{level}' (score={score}, reason={reason})"
    assert score >= 4
    assert confidence == "high"

    # Verify select_model actually routes to the high tier
    config = SecretaryConfig(agent_prefix=False)  # paid mode, no always_opus override
    decision = select_model(config, task)
    assert decision.tier == "high"
    assert decision.model == "claude-opus-4.7"
    assert decision.max_turns == 30
    assert decision.premium_multiplier == 3.0


def test_multi_step_task_always_opus_free_mode():
    """When agent_prefix=True and always_opus, multi-step tasks route to 'high' via Opus."""
    task = (
        "1. Analyze the current database schema and identify bottlenecks\n"
        "2. Design a migration plan for PostgreSQL with rollback strategy\n"
        "3. Implement the migration scripts across all services\n"
        "4. Update all repository classes to use the new schema\n"
    )
    # agent_prefix=True with always_opus=True (default) → always routes to high
    config = SecretaryConfig(agent_prefix=True)
    decision = select_model(config, task)
    assert decision.tier == "high"
    assert decision.model == "claude-opus-4.7"
    assert "always_opus" in decision.reason
    assert decision.confidence == "high"
    # The underlying complexity should independently be high
    level, score, _, confidence = estimate_complexity(task)
    assert level == "high", f"Expected 'high' but got '{level}' (score={score})"
    assert score >= 4
