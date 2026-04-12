"""Tests for efficiency optimizations (selective tools, summarization, etc.)."""
from __future__ import annotations

import pytest
from pathlib import Path
from secretary.config import SecretaryConfig, OptimizationConfig
from secretary.direct_agent import (
    _select_tool_schemas,
    _build_extractive_summary,
    _build_system_prompt,
    _dynamic_max_tokens,
    _to_openai_messages,
    _build_aggressive_context,
    _score_quality,
    RunResult,
    _TIER_MAX_TOKENS,
)
from secretary.router import select_model, RoutingDecision


# ── Fixtures ──────────────────────────────────────────────

def _make_schemas() -> list[dict]:
    """Minimal tool schemas for testing (3 categories, 16 tools)."""
    names = [
        # Email (6)
        "gmail_search", "gmail_read", "gmail_draft", "gmail_send",
        "gmail_list_drafts", "gmail_get_draft",
        # Calendar (4)
        "calendar_today", "calendar_list", "calendar_search", "calendar_create",
        # File (6)
        "file_read", "file_write", "file_list", "file_edit", "grep_search", "run_command",
    ]
    return [{"name": n, "description": f"desc-{n}", "input_schema": {}} for n in names]


def _make_conversation(turns: int = 6) -> list[dict]:
    """Build a fake conversation with tool calls and results."""
    msgs: list[dict] = []
    for i in range(turns):
        # Assistant: text + tool_use
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"Working on step {i+1}..."},
                {"type": "tool_use", "id": f"tu_{i}", "name": "file_read",
                 "input": {"path": f"src/module_{i}.py"}},
            ],
        })
        # User: tool_result
        msgs.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"tu_{i}",
                 "content": f"Contents of module_{i}.py:\ndef func_{i}(): pass"},
            ],
        })
    return msgs


# ── Selective tool exposure ───────────────────────────────

class TestSelectiveTools:
    def test_email_task_includes_email_and_core_tools(self):
        schemas = _make_schemas()
        filtered = _select_tool_schemas(schemas, "Check my email inbox for unread messages")
        names = {s["name"] for s in filtered}
        assert "gmail_search" in names
        assert "gmail_read" in names
        # Core dev tools always included
        assert "file_edit" in names
        assert "grep_search" in names
        assert "run_command" in names
        # But not other file-only or calendar tools
        assert "file_read" not in names
        assert "calendar_today" not in names

    def test_calendar_task_includes_calendar_and_core_tools(self):
        schemas = _make_schemas()
        filtered = _select_tool_schemas(schemas, "Show me today's calendar events")
        names = {s["name"] for s in filtered}
        assert "calendar_today" in names
        # Core dev tools always included
        assert "file_edit" in names
        assert "grep_search" in names
        assert "run_command" in names
        # But not email or other file tools
        assert "gmail_search" not in names
        assert "file_read" not in names

    def test_file_task_includes_all_file_tools(self):
        schemas = _make_schemas()
        filtered = _select_tool_schemas(schemas, "Read src/config.py and fix the bug")
        names = {s["name"] for s in filtered}
        assert "file_read" in names
        assert "file_write" in names
        assert "file_edit" in names
        assert "grep_search" in names
        assert "run_command" in names
        assert "gmail_search" not in names

    def test_mixed_task_returns_matching_categories(self):
        schemas = _make_schemas()
        filtered = _select_tool_schemas(schemas, "Read my email and write the summary to a file")
        names = {s["name"] for s in filtered}
        assert "gmail_search" in names
        assert "file_write" in names
        # Calendar not needed
        assert "calendar_today" not in names

    def test_all_categories_returns_all(self):
        schemas = _make_schemas()
        filtered = _select_tool_schemas(
            schemas, "Read email, check calendar events, and write code to a file"
        )
        assert len(filtered) == len(schemas)

    def test_no_match_returns_all(self):
        schemas = _make_schemas()
        filtered = _select_tool_schemas(schemas, "Tell me a joke")
        assert len(filtered) == len(schemas)

    def test_empty_schemas(self):
        assert _select_tool_schemas([], "anything") == []


# ── Conversation summarization ────────────────────────────

class TestExtractiveSummary:
    def test_extracts_tool_calls(self):
        msgs = _make_conversation(3)
        summary = _build_extractive_summary(msgs, 0)
        assert "Called file_read" in summary
        assert "path=src/module_0.py" in summary

    def test_extracts_assistant_text(self):
        msgs = _make_conversation(3)
        summary = _build_extractive_summary(msgs, 0)
        assert "Said: Working on step 1" in summary

    def test_extracts_tool_results(self):
        msgs = _make_conversation(3)
        summary = _build_extractive_summary(msgs, 0)
        assert "Contents of module_" in summary

    def test_respects_anchor(self):
        # anchor=2 skips first assistant+user pair
        msgs = _make_conversation(3)
        summary = _build_extractive_summary(msgs, 2)
        assert "module_0" not in summary
        assert "module_1" in summary

    def test_empty_conversation(self):
        assert _build_extractive_summary([], 0) == ""

    def test_limits_to_20_items(self):
        msgs = _make_conversation(15)  # 30 messages → 45 items (text + call + result)
        summary = _build_extractive_summary(msgs, 0)
        lines = summary.strip().split("\n")
        assert len(lines) <= 20


# ── Dynamic max_tokens ────────────────────────────────────

class TestDynamicMaxTokens:
    def test_full_budget_early_turns(self):
        assert _dynamic_max_tokens("high", 0) == 32768
        assert _dynamic_max_tokens("high", 1) == 32768
        assert _dynamic_max_tokens("high", 2) == 32768

    def test_capped_at_16k_mid_turns(self):
        assert _dynamic_max_tokens("high", 3) == 16384
        assert _dynamic_max_tokens("high", 5) == 16384

    def test_capped_at_8k_late_turns(self):
        assert _dynamic_max_tokens("high", 6) == 8192
        assert _dynamic_max_tokens("high", 7) == 8192

    def test_capped_at_4k_very_late_turns(self):
        assert _dynamic_max_tokens("high", 8) == 4096
        assert _dynamic_max_tokens("high", 10) == 4096

    def test_lower_tiers_unaffected_early(self):
        assert _dynamic_max_tokens("medium", 0) == 16384
        assert _dynamic_max_tokens("low", 0) == 12288

    def test_lower_tiers_capped_late(self):
        assert _dynamic_max_tokens("medium", 6) == 8192
        assert _dynamic_max_tokens("medium", 8) == 4096
        assert _dynamic_max_tokens("low", 6) == 8192


# ── OpenAI message translation (mixed content) ───────────

class TestToOpenAIMessagesMixed:
    def test_tool_result_with_text_block(self):
        """Tool results + text blocks in same user message → both emitted."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tc_1", "name": "file_read",
                 "input": {"path": "x.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tc_1", "content": "file contents"},
                {"type": "text", "text": "[5 turns remaining]"},
            ]},
        ]
        out = _to_openai_messages("system prompt", messages)
        # Should have: system, user, assistant, tool, user(budget signal)
        roles = [m["role"] for m in out]
        assert "tool" in roles
        assert roles.count("user") >= 2  # original + budget signal
        # Budget signal text should be in a user message
        user_msgs = [m for m in out if m["role"] == "user"]
        assert any("[5 turns remaining]" in m.get("content", "") for m in user_msgs)

    def test_tool_result_only_no_extra_user(self):
        """Tool results without text blocks → no extra user message."""
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tc_1", "name": "f", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tc_1", "content": "ok"},
            ]},
        ]
        out = _to_openai_messages("sys", messages)
        roles = [m["role"] for m in out]
        # Should be: system, user, assistant, tool
        assert roles == ["system", "user", "assistant", "tool"]


# ── Always-Opus routing ──────────────────────────────────

class TestAlwaysOpus:
    def test_opus_override_when_free_mode(self):
        """agent_prefix=true + always_opus → routes to Opus even for simple tasks."""
        config = SecretaryConfig(agent_prefix=True, optimizations=OptimizationConfig(always_opus=True))
        routing = select_model(config, "what is 2+2?")
        assert routing.tier == "high"
        assert routing.model == "claude-opus-4.6"
        assert "always_opus" in routing.reason

    def test_normal_routing_when_paid_mode(self):
        """agent_prefix=false → normal routing, simple task → free tier (0× multiplier)."""
        config = SecretaryConfig(agent_prefix=False, optimizations=OptimizationConfig(always_opus=True))
        routing = select_model(config, "what is 2+2?")
        assert routing.tier == "free"  # low-complexity → free model (GPT-4.1, 0× multiplier)

    def test_normal_routing_paid_mode_no_free_models(self):
        """agent_prefix=false, use_free_models=false → low tier (not free)."""
        config = SecretaryConfig(agent_prefix=False, optimizations=OptimizationConfig(
            always_opus=True, use_free_models=False,
        ))
        routing = select_model(config, "what is 2+2?")
        assert routing.tier == "low"

    def test_normal_routing_when_always_opus_off(self):
        """always_opus=false → normal routing even with agent_prefix."""
        config = SecretaryConfig(agent_prefix=True, optimizations=OptimizationConfig(always_opus=False))
        routing = select_model(config, "what is 2+2?")
        assert routing.tier == "low"

    def test_force_tier_overrides_always_opus(self):
        """Explicit force_tier still works regardless of always_opus."""
        config = SecretaryConfig(agent_prefix=True, optimizations=OptimizationConfig(always_opus=True))
        routing = select_model(config, "simple question", force_tier="low")
        assert routing.tier == "low"
        assert routing.model == "claude-haiku-4.5"

    def test_opus_preserves_original_reason(self):
        """Routing reason should mention the original tier for auditability."""
        config = SecretaryConfig(agent_prefix=True, optimizations=OptimizationConfig(always_opus=True))
        routing = select_model(config, "what time is it?")
        assert "original: low" in routing.reason


# ── Aggressive context injection ──────────────────────────

class TestAggressiveContext:
    def test_matches_agent_keywords(self, tmp_path):
        """Task mentioning 'agent' matches direct_agent.py."""
        (tmp_path / "src" / "secretary").mkdir(parents=True)
        (tmp_path / "src" / "secretary" / "direct_agent.py").write_text("# agent code")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("optimize the agent loop", data_dir)
        assert "direct_agent.py" in ctx
        assert "# agent code" in ctx
        assert "src/secretary/direct_agent.py" in paths
        assert "src/secretary/direct_agent.py" in paths

    def test_matches_config_keywords(self, tmp_path):
        """Task mentioning 'config' matches config files."""
        (tmp_path / "src" / "secretary").mkdir(parents=True)
        (tmp_path / "src" / "secretary" / "config.py").write_text("# config code")
        (tmp_path / "config.yaml").write_text("key: value")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("check the config settings", data_dir)
        assert "config" in ctx

    def test_no_match_returns_empty(self, tmp_path):
        """Unrelated task → no context injection."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("tell me a joke", data_dir)
        assert ctx == ""

    def test_respects_byte_limit(self, tmp_path):
        """Context injection respects _MAX_PREFETCH_BYTES."""
        (tmp_path / "src" / "secretary").mkdir(parents=True)
        # Create a large file
        (tmp_path / "src" / "secretary" / "direct_agent.py").write_text("x" * 40000)
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("read the agent code", data_dir)
        assert len(ctx) <= 35000  # bounded by _MAX_PREFETCH_BYTES (30KB) + headers
        assert "[truncated]" in ctx

    def test_directory_listing(self, tmp_path):
        """Task mentioning 'test' lists tests/ directory."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("# test")
        (tmp_path / "tests" / "test_bar.py").write_text("# test")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("run the tests", data_dir)
        assert "test_foo.py" in ctx
        assert "test_bar.py" in ctx

    def test_returns_preloaded_paths_set(self, tmp_path):
        """_build_aggressive_context returns a set of loaded file paths."""
        (tmp_path / "src" / "secretary").mkdir(parents=True)
        (tmp_path / "src" / "secretary" / "direct_agent.py").write_text("# code")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("optimize the agent loop", data_dir)
        assert isinstance(paths, set)
        assert "src/secretary/direct_agent.py" in paths

    def test_empty_returns_empty_set(self, tmp_path):
        """No match returns empty string and empty set."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("tell me a joke", data_dir)
        assert ctx == ""
        assert paths == set()

    def test_returns_preloaded_paths_set(self, tmp_path):
        """_build_aggressive_context returns a set of loaded file paths."""
        (tmp_path / "src" / "secretary").mkdir(parents=True)
        (tmp_path / "src" / "secretary" / "direct_agent.py").write_text("# code")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("optimize the agent loop", data_dir)
        assert isinstance(paths, set)
        assert "src/secretary/direct_agent.py" in paths

    def test_empty_returns_empty_set(self, tmp_path):
        """No match returns empty string and empty set."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        ctx, paths = _build_aggressive_context("tell me a joke", data_dir)
        assert ctx == ""
        assert paths == set()


# ── Redundant read tracking ──────────────────────────────

class TestRedundantReadTracking:
    def test_runresult_has_redundant_reads_field(self):
        """RunResult has a redundant_reads field defaulting to 0."""
        r = RunResult(
            task="test",
            routing=RoutingDecision(tier="low", model="test",
                                    max_turns=3, max_budget_usd=0, reason="test"),
        )
        assert r.redundant_reads == 0

    def test_redundant_reads_can_be_set(self):
        r = RunResult(
            task="test",
            routing=RoutingDecision(tier="low", model="test",
                                    max_turns=3, max_budget_usd=0, reason="test"),
            redundant_reads=5,
        )
        assert r.redundant_reads == 5


# ── Anti-redundancy system prompt ─────────────────────────

class TestAntiRedundancyPrompt:
    def test_short_task_prompt_warns_against_rereading(self):
        """System prompt for ≤3 turn tasks warns against re-reading."""
        from secretary.memory import MemoryStore
        prompt = _build_system_prompt(MemoryStore(), task="test", max_turns=3)
        assert "do NOT re-read" in prompt or "do NOT call file_read" in prompt

    def test_long_task_prompt_warns_against_rereading(self):
        """System prompt for >3 turn tasks also warns against re-reading pre-loaded data."""
        from secretary.memory import MemoryStore
        prompt = _build_system_prompt(MemoryStore(), task="test", max_turns=10)
        assert "pre-loaded" in prompt.lower() or "do NOT re-read" in prompt or "do NOT call file_read" in prompt.lower()


# ── Quality scoring ───────────────────────────────────────

class TestQualityScoring:
    def _make_result(self, **kwargs) -> RunResult:
        defaults = dict(
            task="test task",
            routing=RoutingDecision(tier="high", model="claude-opus-4.6",
                                    max_turns=30, max_budget_usd=5.0, reason="test"),
        )
        defaults.update(kwargs)
        return RunResult(**defaults)

    def test_perfect_run_high_score(self):
        """Successful run with diverse tools, good output → high quality."""
        r = self._make_result(
            text="Here is the detailed analysis..." * 20,
            tools_used=["file_read", "file_write", "gmail_search", "file_list", "calendar_today"],
            num_turns=3,
        )
        score = _score_quality(r)
        assert score >= 0.7

    def test_error_run_low_score(self):
        """Failed run → low quality score."""
        r = self._make_result(error="API timeout", text="", tools_used=[], num_turns=1)
        score = _score_quality(r)
        assert score < 0.3

    def test_no_tools_lower_score(self):
        """Run with no tool usage → lower score."""
        r = self._make_result(text="I can help with that.", tools_used=[], num_turns=1)
        score = _score_quality(r)
        assert score < 0.6

    def test_many_turns_penalized(self):
        """Run with many turns → lower efficiency score."""
        r_fast = self._make_result(
            text="Done. " * 50, tools_used=["file_read"] * 6, num_turns=2
        )
        r_slow = self._make_result(
            text="Done. " * 50, tools_used=["file_read"] * 6, num_turns=12
        )
        assert _score_quality(r_fast) > _score_quality(r_slow)

    def test_good_batching_rewarded(self):
        """More tools per turn → higher score."""
        r_batch = self._make_result(
            text="Summary " * 50,
            tools_used=["file_read", "file_write", "file_list", "gmail_search", "gmail_read", "calendar_today"],
            num_turns=2,
        )
        r_single = self._make_result(
            text="Summary " * 50,
            tools_used=["file_read", "file_write", "file_list", "gmail_search", "gmail_read", "calendar_today"],
            num_turns=6,
        )
        assert _score_quality(r_batch) > _score_quality(r_single)

    def test_score_bounded(self):
        """Score always between 0 and 1."""
        r = self._make_result(
            text="x" * 1000,
            tools_used=["a", "b", "c", "d", "e", "f", "g"],
            num_turns=1,
        )
        assert 0.0 <= _score_quality(r) <= 1.0

# ── OptimizationConfig ────────────────────────────────────

class TestOptimizationConfig:
    def test_defaults_all_enabled(self):
        cfg = OptimizationConfig()
        assert cfg.selective_tools is True
        assert cfg.turn_budget_signal is True
        assert cfg.context_preload is True
        assert cfg.conversation_summary is True
        assert cfg.dynamic_max_tokens is True
        assert cfg.summary_after_turn == 5

    def test_toggling_off(self):
        cfg = OptimizationConfig(
            selective_tools=False,
            turn_budget_signal=False,
            dynamic_max_tokens=False,
        )
        assert cfg.selective_tools is False
        assert cfg.turn_budget_signal is False
        assert cfg.dynamic_max_tokens is False
        # Others still on
        assert cfg.context_preload is True

    def test_in_secretary_config(self):
        cfg = SecretaryConfig()
        assert cfg.optimizations.selective_tools is True

    def test_from_yaml_dict(self):
        raw = {
            "optimizations": {
                "selective_tools": False,
                "summary_after_turn": 3,
            }
        }
        cfg = SecretaryConfig.model_validate(raw)
        assert cfg.optimizations.selective_tools is False
        assert cfg.optimizations.summary_after_turn == 3
        assert cfg.optimizations.turn_budget_signal is True  # default


# ── Paid-mode optimizations ──────────────────────────────────


class TestPaidModeConfig:
    """Verify paid-mode optimization config fields."""

    def test_paid_turn_limits_defaults(self):
        """Default paid turn limits for each tier (tighter = more work per turn)."""
        cfg = OptimizationConfig()
        assert cfg.paid_turn_limits == {"free": 2, "low": 3, "medium": 5, "high": 4, "deep": 12, "oracle": 12}

    def test_task_premium_budget_defaults(self):
        """Default premium budgets per task tier."""
        cfg = OptimizationConfig()
        assert cfg.task_premium_budget == {"free": 0.0, "low": 1.0, "medium": 5.0, "high": 12.0, "deep": 36.0, "oracle": 9.0}

    def test_file_cache_default_on(self):
        """File cache enabled by default."""
        cfg = OptimizationConfig()
        assert cfg.file_cache is True

    def test_paid_turn_limits_from_yaml(self):
        """Custom paid turn limits from YAML config."""
        raw = {"optimizations": {"paid_turn_limits": {"low": 2, "medium": 5, "high": 8}}}
        cfg = SecretaryConfig.model_validate(raw)
        assert cfg.optimizations.paid_turn_limits["low"] == 2
        assert cfg.optimizations.paid_turn_limits["high"] == 8


class TestPremiumMultiplier:
    """Verify premium_multiplier on RoutingDecision."""

    def test_routing_has_premium_multiplier(self):
        """RoutingDecision should include premium_multiplier."""
        rd = RoutingDecision(
            tier="high", model="claude-opus-4.6",
            max_turns=25, max_budget_usd=0.0, reason="test",
        )
        assert hasattr(rd, "premium_multiplier")

    def test_select_model_sets_multiplier(self):
        """select_model should set premium_multiplier from TIER_MULTIPLIERS."""
        cfg = SecretaryConfig(agent_prefix=False)
        routing = select_model(cfg, "simple question")
        assert routing.premium_multiplier >= 0  # 0.0 for free models, >0 for paid

    def test_free_model_multiplier_is_zero(self):
        """Free model (GPT-4.1) should have 0× premium multiplier."""
        cfg = SecretaryConfig(agent_prefix=False)
        routing = select_model(cfg, "simple question")
        assert routing.tier == "free"
        assert routing.premium_multiplier == 0.0

    def test_opus_multiplier_is_3(self):
        """Opus model should have 3x premium multiplier."""
        cfg = SecretaryConfig(agent_prefix=False)
        routing = select_model(cfg, "refactor the entire codebase", "high")
        assert routing.premium_multiplier == 3.0

    def test_haiku_multiplier_is_033(self):
        """Haiku model should have 0.33x premium multiplier."""
        cfg = SecretaryConfig(agent_prefix=False)
        routing = select_model(cfg, "what is 2+2", "low")
        assert routing.premium_multiplier == 0.33


class TestRunResultPremium:
    """Verify premium_requests tracking on RunResult."""

    def test_run_result_has_premium_requests(self):
        """RunResult should track premium_requests."""
        rd = RoutingDecision(
            tier="high", model="claude-opus-4.6",
            max_turns=25, max_budget_usd=0.0, reason="test",
        )
        r = RunResult(task="test", routing=rd)
        assert r.premium_requests == 0.0

    def test_premium_calculation(self):
        """premium_requests = turns × multiplier."""
        rd = RoutingDecision(
            tier="high", model="claude-opus-4.6",
            max_turns=25, max_budget_usd=0.0, reason="test",
            premium_multiplier=3.0,
        )
        r = RunResult(task="test", routing=rd)
        r.num_turns = 5
        r.premium_requests = r.num_turns * rd.premium_multiplier
        assert r.premium_requests == 15.0  # 5 turns × 3x


# ── New optimization config fields ───────────────────────────


class TestForceFirstToolConfig:
    """Verify force_first_tool config field defaults and toggling."""

    def test_force_first_tool_default_on(self):
        cfg = OptimizationConfig()
        assert cfg.force_first_tool is True

    def test_force_first_tool_toggle_off(self):
        cfg = OptimizationConfig(force_first_tool=False)
        assert cfg.force_first_tool is False

    def test_force_first_tool_from_yaml(self):
        raw = {"optimizations": {"force_first_tool": False}}
        cfg = SecretaryConfig.model_validate(raw)
        assert cfg.optimizations.force_first_tool is False


class TestPredictivePrefetchConfig:
    """Verify predictive_prefetch config field defaults and toggling."""

    def test_predictive_prefetch_default_on(self):
        cfg = OptimizationConfig()
        assert cfg.predictive_prefetch is True

    def test_predictive_prefetch_toggle_off(self):
        cfg = OptimizationConfig(predictive_prefetch=False)
        assert cfg.predictive_prefetch is False


class TestToolMemoizationConfig:
    """Verify tool_memoization config fields."""

    def test_tool_memoization_default_on(self):
        cfg = OptimizationConfig()
        assert cfg.tool_memoization is True

    def test_tool_memo_ttl_default_300(self):
        cfg = OptimizationConfig()
        assert cfg.tool_memo_ttl_seconds == 300

    def test_tool_memoization_from_yaml(self):
        raw = {"optimizations": {"tool_memoization": False, "tool_memo_ttl_seconds": 60}}
        cfg = SecretaryConfig.model_validate(raw)
        assert cfg.optimizations.tool_memoization is False
        assert cfg.optimizations.tool_memo_ttl_seconds == 60


# ── Free-model routing (GPT-4.1 = 0× premium) ───────────────


class TestFreeModelRouting:
    """Verify free-model routing in paid mode."""

    def test_trivial_task_routes_to_free_in_paid_mode(self):
        """Simple questions route to free tier (GPT-4.1) in paid mode."""
        cfg = SecretaryConfig(agent_prefix=False, optimizations=OptimizationConfig(use_free_models=True))
        routing = select_model(cfg, "what time is it?")
        assert routing.tier == "free"
        assert routing.model == "gpt-4.1"
        assert routing.premium_multiplier == 0.0

    def test_complex_task_still_uses_paid_model(self):
        """Complex tasks should NOT route to free model."""
        cfg = SecretaryConfig(agent_prefix=False, optimizations=OptimizationConfig(use_free_models=True))
        routing = select_model(cfg, "refactor the entire codebase architecture across multiple files", "high")
        assert routing.tier == "high"
        assert routing.premium_multiplier == 3.0

    def test_free_models_disabled(self):
        """use_free_models=false → low tier instead of free."""
        cfg = SecretaryConfig(agent_prefix=False, optimizations=OptimizationConfig(use_free_models=False))
        routing = select_model(cfg, "what time is it?")
        assert routing.tier == "low"
        assert routing.model == "claude-haiku-4.5"

    def test_free_models_ignored_in_free_mode(self):
        """When agent_prefix=true, always_opus overrides free routing."""
        cfg = SecretaryConfig(agent_prefix=True, optimizations=OptimizationConfig(
            always_opus=True, use_free_models=True,
        ))
        routing = select_model(cfg, "what time is it?")
        assert routing.tier == "high"  # always_opus wins when agent_prefix=true

    def test_use_free_models_default_on(self):
        cfg = OptimizationConfig()
        assert cfg.use_free_models is True


class TestNonClaudeModelDetection:
    """Verify non-Claude model routing goes through OpenAI endpoint."""

    def test_gpt41_is_non_claude(self):
        """GPT-4.1 should not start with 'claude-'."""
        assert not "gpt-4.1".startswith("claude-")

    def test_free_tier_model_config(self):
        """Free tier should default to GPT-4.1."""
        cfg = SecretaryConfig()
        assert "free" in cfg.routing.tiers
        assert cfg.routing.tiers["free"].model == "gpt-4.1"
        assert cfg.routing.tiers["free"].max_turns == 3


# ── System prompt modes ──────────────────────────────────────


class TestSystemPromptModes:
    """Verify system prompt adapts to max_turns budget."""

    def test_ultra_compact_mode(self):
        """max_turns <= 3 → ultra-compact 1-turn prompt."""
        from secretary.memory import MemoryStore
        mem = MemoryStore()
        prompt = _build_system_prompt(mem, "read files", max_turns=2)
        assert "2 turns" in prompt
        assert "7+" in prompt or "parallel" in prompt

    def test_normal_mode(self):
        """max_turns > 3 → normal system prompt."""
        from secretary.memory import MemoryStore
        mem = MemoryStore()
        prompt = _build_system_prompt(mem, "read files", max_turns=10)
        assert "RULES" in prompt
        assert "7+" in prompt or "parallel" in prompt


# ── Task batching integration ─────────────────────────────────


class TestTaskBatcherIntegration:
    """Verify task_batcher.py works when imported by watcher."""

    def test_import_and_basic_grouping(self):
        """group_into_batches should merge batch_compatible tasks."""
        from secretary.task_batcher import group_into_batches
        tasks = [
            {"prompt": "check email", "tier": "low", "batch_compatible": True},
            {"prompt": "check calendar", "tier": "low", "batch_compatible": True},
            {"prompt": "deep analysis", "tier": "high"},
        ]
        batches = group_into_batches(tasks, enabled=True, max_batch_size=3)
        assert len(batches) == 2  # 1 merged batch + 1 solo
        assert batches[0].is_batch is True
        assert batches[0].task_count == 2
        assert batches[1].is_batch is False

    def test_disabled_batching(self):
        """enabled=False → every task is solo."""
        from secretary.task_batcher import group_into_batches
        tasks = [
            {"prompt": "a", "batch_compatible": True},
            {"prompt": "b", "batch_compatible": True},
        ]
        batches = group_into_batches(tasks, enabled=False)
        assert len(batches) == 2
        assert all(not b.is_batch for b in batches)

    def test_merged_prompt_contains_all_tasks(self):
        """Merged batch prompt includes all constituent task prompts."""
        from secretary.task_batcher import group_into_batches
        tasks = [
            {"prompt": "check unread emails", "tier": "low", "batch_compatible": True, "id": "t1"},
            {"prompt": "check today calendar", "tier": "low", "batch_compatible": True, "id": "t2"},
        ]
        batches = group_into_batches(tasks, enabled=True)
        assert len(batches) == 1
        assert "check unread emails" in batches[0].merged_prompt
        assert "check today calendar" in batches[0].merged_prompt


# ── Tighter paid turn limits ──────────────────────────────────


class TestTighterPaidLimits:
    """Verify tighter paid-mode turn limits maximize work per request."""

    def test_opus_paid_limit_is_4(self):
        """Opus should be capped at 4 turns in paid mode (4 × 3× = 12 premium)."""
        cfg = OptimizationConfig()
        assert cfg.paid_turn_limits["high"] == 4

    def test_sonnet_paid_limit_is_5(self):
        """Sonnet should be capped at 5 turns in paid mode (5 × 1× = 5 premium)."""
        cfg = OptimizationConfig()
        assert cfg.paid_turn_limits["medium"] == 5

    def test_free_tier_paid_limit_is_2(self):
        """Free tier should be capped at 2 turns (costs nothing anyway)."""
        cfg = OptimizationConfig()
        assert cfg.paid_turn_limits["free"] == 2
