"""Tests for strategy_library — Voyager-inspired learned knowledge store."""
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from secretary.strategy_library import (
    Strategy,
    StrategyLibrary,
    extract_strategy_from_entry,
    maybe_extract_strategy,
    load_library,
    _similar,
    MAX_STRATEGIES_PER_CATEGORY,
    MAX_TOTAL_STRATEGIES,
    MIN_QUALITY_SCORE,
    DECAY_RATE,
)


# ── Strategy dataclass ────────────────────────────────────────

class TestStrategy:
    def test_creation(self):
        s = Strategy(
            category="email",
            description="Search first, then read matching results in parallel",
            source_task="Check new emails",
        )
        assert s.category == "email"
        assert s.quality_score == 1.0
        assert s.use_count == 0
        assert s.created_at > 0  # auto-set

    def test_custom_fields(self):
        s = Strategy(
            category="research",
            description="Use grep_search for patterns",
            source_task="Research topic",
            tools_used=["grep_search", "file_read"],
            quality_score=0.8,
            use_count=5,
            success_count=4,
        )
        assert s.tools_used == ["grep_search", "file_read"]
        assert s.success_count == 4


# ── StrategyLibrary core ──────────────────────────────────────

class TestStrategyLibrary:
    def test_empty_library(self):
        lib = StrategyLibrary()
        assert lib.size == 0
        assert lib.retrieve("email") == []
        assert lib.format_for_prompt("email") == ""

    def test_add_and_retrieve(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email",
            description="Search then read in parallel",
            source_task="Check emails",
        )
        assert lib.add_strategy(s) is True
        assert lib.size == 1

        retrieved = lib.retrieve("email")
        assert len(retrieved) == 1
        assert retrieved[0].description == "Search then read in parallel"

    def test_retrieve_wrong_category(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="test", source_task="t",
        ))
        assert lib.retrieve("calendar") == []

    def test_retrieve_sorted_by_quality(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="low quality strategy",
            source_task="t1", quality_score=0.5,
        ))
        lib.add_strategy(Strategy(
            category="email", description="high quality strategy",
            source_task="t2", quality_score=1.5,
        ))
        lib.add_strategy(Strategy(
            category="email", description="medium quality strategy",
            source_task="t3", quality_score=0.9,
        ))

        retrieved = lib.retrieve("email", max_results=2)
        assert len(retrieved) == 2
        assert retrieved[0].quality_score == 1.5
        assert retrieved[1].quality_score == 0.9

    def test_format_for_prompt(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search then read in parallel",
            source_task="Check emails",
        ))
        text = lib.format_for_prompt("email")
        assert "Learned Strategies" in text
        assert "Search then read in parallel" in text
        assert "[new]" in text  # no uses yet

    def test_format_for_prompt_updates_use_count(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email",
            description="Search then read",
            source_task="Check emails",
        )
        lib.add_strategy(s)
        lib.format_for_prompt("email")
        assert s.use_count == 1

    def test_format_with_success_rate(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email", description="test", source_task="t",
            use_count=10, success_count=8,
        )
        lib.add_strategy(s)
        text = lib.format_for_prompt("email")
        assert "[8/10]" in text

    def test_dedup_boosts_existing(self):
        lib = StrategyLibrary()
        s1 = Strategy(
            category="email",
            description="Search first then read matching results",
            source_task="Check emails",
            quality_score=0.8,
        )
        lib.add_strategy(s1)

        s2 = Strategy(
            category="email",
            description="Search first then read matching emails",
            source_task="Read emails",
        )
        added = lib.add_strategy(s2)
        assert added is False  # duplicate detected
        assert lib.size == 1  # no duplicate added
        assert s1.quality_score == 0.9  # boosted

    def test_no_dedup_different_categories(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search first then read",
            source_task="t1",
        ))
        added = lib.add_strategy(Strategy(
            category="calendar",
            description="Search first then read",
            source_task="t2",
        ))
        assert added is True
        assert lib.size == 2


# ── Record outcome ────────────────────────────────────────────

class TestRecordOutcome:
    def test_success_boosts(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email", description="test", source_task="t",
            use_count=1, quality_score=1.0,
        )
        lib.add_strategy(s)
        lib.record_outcome("email", success=True)
        assert s.success_count == 1
        assert s.quality_score == 1.05

    def test_failure_decreases(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email", description="test", source_task="t",
            use_count=1, quality_score=1.0,
        )
        lib.add_strategy(s)
        lib.record_outcome("email", success=False)
        assert s.quality_score == 0.9

    def test_only_affects_used_strategies(self):
        lib = StrategyLibrary()
        s1 = Strategy(
            category="email", description="test used", source_task="t",
            use_count=1, quality_score=1.0,
        )
        s2 = Strategy(
            category="email", description="never used strategy here",
            source_task="t", use_count=0, quality_score=1.0,
        )
        lib.add_strategy(s1)
        lib.add_strategy(s2)
        lib.record_outcome("email", success=True)
        assert s1.success_count == 1
        assert s2.success_count == 0  # not used → not updated


# ── Consolidation ─────────────────────────────────────────────

class TestConsolidation:
    def test_decay_reduces_quality(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email", description="test", source_task="t",
            quality_score=1.0,
        )
        lib.add_strategy(s)
        lib.consolidate()
        assert abs(s.quality_score - DECAY_RATE) < 0.001

    def test_prunes_low_quality(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="good strategy",
            source_task="t", quality_score=1.0,
        ))
        lib.add_strategy(Strategy(
            category="email", description="bad strategy almost dead",
            source_task="t", quality_score=0.31,
        ))
        pruned = lib.consolidate()
        assert pruned == 1  # bad strategy pruned after decay
        assert lib.size == 1

    def test_per_category_cap(self):
        lib = StrategyLibrary()
        for i in range(MAX_STRATEGIES_PER_CATEGORY + 3):
            lib.add_strategy(Strategy(
                category="email",
                description=f"strategy number {i} quite unique",
                source_task=f"task {i}",
                quality_score=1.0 - i * 0.01,
            ))
        # After pruning, should have at most MAX_STRATEGIES_PER_CATEGORY
        lib.consolidate()
        email_strats = lib.retrieve("email", MAX_STRATEGIES_PER_CATEGORY + 5)
        assert len(email_strats) <= MAX_STRATEGIES_PER_CATEGORY


# ── Persistence ───────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load(self, tmp_path):
        path = tmp_path / "strategies.json"
        lib = StrategyLibrary(path)
        lib.add_strategy(Strategy(
            category="email",
            description="Search then read",
            source_task="Check emails",
            tools_used=["gmail_search", "gmail_read"],
        ))

        # Load in new instance
        lib2 = StrategyLibrary(path)
        assert lib2.size == 1
        s = lib2.retrieve("email")[0]
        assert s.description == "Search then read"
        assert s.tools_used == ["gmail_search", "gmail_read"]

    def test_load_corrupted(self, tmp_path):
        path = tmp_path / "strategies.json"
        path.write_text("not json", encoding="utf-8")
        lib = StrategyLibrary(path)
        assert lib.size == 0

    def test_load_missing(self, tmp_path):
        lib = StrategyLibrary(tmp_path / "nonexistent.json")
        assert lib.size == 0

    def test_auto_save_on_add(self, tmp_path):
        path = tmp_path / "strategies.json"
        lib = StrategyLibrary(path)
        lib.add_strategy(Strategy(
            category="email", description="test", source_task="t",
        ))
        # Verify file exists and has content
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data) == 1

    def test_roundtrip_preserves_all_fields(self, tmp_path):
        path = tmp_path / "strategies.json"
        lib = StrategyLibrary(path)
        lib.add_strategy(Strategy(
            category="email",
            description="test",
            source_task="task text",
            tools_used=["t1", "t2"],
            quality_score=0.77,
            use_count=5,
            success_count=3,
            created_at=1000.0,
            last_used_at=2000.0,
        ))

        lib2 = StrategyLibrary(path)
        s = lib2.retrieve("email")[0]
        assert s.quality_score == 0.77
        assert s.use_count == 5
        assert s.success_count == 3
        assert s.created_at == 1000.0


# ── Stats ─────────────────────────────────────────────────────

class TestStats:
    def test_categories(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="e1", source_task="t",
        ))
        lib.add_strategy(Strategy(
            category="email", description="e2 unique strategy words",
            source_task="t",
        ))
        lib.add_strategy(Strategy(
            category="calendar", description="c1", source_task="t",
        ))
        cats = lib.categories()
        assert cats == {"email": 2, "calendar": 1}


# ── LLM extraction ───────────────────────────────────────────

class TestExtractStrategy:
    def _mock_response(self, data: dict):
        mock = MagicMock()
        mock.json.return_value = {
            "choices": [{
                "message": {"content": json.dumps(data)},
            }],
        }
        mock.raise_for_status = lambda: None
        return mock

    def test_extracts_strategy(self):
        data = {
            "description": "Search emails first then read top 3 in parallel",
            "tools_pattern": ["gmail_search", "gmail_read"],
        }
        with patch("secretary.strategy_library.httpx.post") as mock_post:
            mock_post.return_value = self._mock_response(data)
            result = extract_strategy_from_entry(
                task="Check for important emails",
                category="email",
                tools_used=["gmail_search", "gmail_read"],
                output_preview="Found 3 emails",
                duration_s=12.5,
                num_turns=3,
                base_url="http://localhost:4141",
            )

        assert result is not None
        assert result.category == "email"
        assert "Search emails" in result.description
        assert "gmail_search" in result.tools_used

    def test_handles_code_fences(self):
        data = {
            "description": "Read file then edit",
            "tools_pattern": ["file_read"],
        }
        mock = MagicMock()
        mock.json.return_value = {
            "choices": [{
                "message": {"content": f"```json\n{json.dumps(data)}\n```"},
            }],
        }
        mock.raise_for_status = lambda: None

        with patch("secretary.strategy_library.httpx.post") as mock_post:
            mock_post.return_value = mock
            result = extract_strategy_from_entry(
                task="Edit config",
                category="file",
                tools_used=["file_read", "file_edit"],
                output_preview="Done",
                duration_s=5.0,
                num_turns=2,
                base_url="http://localhost:4141",
            )

        assert result is not None

    def test_handles_api_failure(self):
        with patch("secretary.strategy_library.httpx.post") as mock_post:
            mock_post.side_effect = Exception("connection refused")
            result = extract_strategy_from_entry(
                task="Check emails",
                category="email",
                tools_used=["gmail_search"],
                output_preview="",
                duration_s=5.0,
                num_turns=1,
                base_url="http://localhost:4141",
            )

        assert result is None


# ── maybe_extract_strategy (conditional pipeline) ─────────────

class TestMaybeExtractStrategy:
    def test_skips_failed_tasks(self):
        lib = StrategyLibrary()
        result = maybe_extract_strategy(
            entry_task="failed task",
            entry_success=False,
            entry_tools=["t1", "t2"],
            entry_output="error",
            entry_duration=10.0,
            entry_turns=3,
            entry_source="campaign",
            entry_campaign="email-check",
            library=lib,
            base_url="http://localhost:4141",
        )
        assert result is None

    def test_skips_trivial_tasks(self):
        lib = StrategyLibrary()
        result = maybe_extract_strategy(
            entry_task="simple task",
            entry_success=True,
            entry_tools=["t1"],  # only 1 tool = trivial
            entry_output="done",
            entry_duration=2.0,
            entry_turns=1,
            entry_source="campaign",
            entry_campaign="health",
            library=lib,
            base_url="http://localhost:4141",
        )
        assert result is None

    def test_skips_saturated_category(self):
        lib = StrategyLibrary()
        # Fill email category with high-quality strategies
        # Descriptions must be distinct enough to pass dedup (< 70% word overlap)
        _descs = [
            "Search inbox using gmail_search with date filters",
            "Read full thread via gmail_read for context gathering",
            "Draft reply using gmail_draft with template formatting",
            "Send automated follow-up using gmail_send scheduler",
            "List and purge old drafts using gmail_list_drafts cleanup",
        ]
        for i in range(MAX_STRATEGIES_PER_CATEGORY):
            lib.add_strategy(Strategy(
                category="email",
                description=_descs[i],
                source_task=f"t{i}",
                quality_score=1.0,
            ))

        result = maybe_extract_strategy(
            entry_task="another email task",
            entry_success=True,
            entry_tools=["gmail_search", "gmail_read"],
            entry_output="done",
            entry_duration=10.0,
            entry_turns=3,
            entry_source="campaign",
            entry_campaign="email-check",
            library=lib,
            base_url="http://localhost:4141",
        )
        assert result is None

    def test_extracts_and_adds(self):
        lib = StrategyLibrary()
        strategy_data = {
            "description": "Search then read in parallel for emails",
            "tools_pattern": ["gmail_search", "gmail_read"],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {"content": json.dumps(strategy_data)},
            }],
        }
        mock_resp.raise_for_status = lambda: None

        with patch("secretary.strategy_library.httpx.post") as mock_post:
            mock_post.return_value = mock_resp
            result = maybe_extract_strategy(
                entry_task="Check important emails from today",
                entry_success=True,
                entry_tools=["gmail_search", "gmail_read", "gmail_read"],
                entry_output="Found 3 important emails",
                entry_duration=12.0,
                entry_turns=3,
                entry_source="campaign",
                entry_campaign="email-check",
                library=lib,
                base_url="http://localhost:4141",
            )

        assert result is not None
        assert lib.size == 1
        assert result.category == "email"


# ── _similar helper ───────────────────────────────────────────

class TestSimilar:
    def test_identical(self):
        assert _similar("hello world", "hello world") is True

    def test_high_overlap(self):
        assert _similar(
            "search first then read matching results",
            "search first then read matching emails",
        ) is True

    def test_low_overlap(self):
        assert _similar(
            "search email inbox for messages",
            "create calendar event tomorrow",
        ) is False

    def test_empty(self):
        assert _similar("", "") is False
        assert _similar("hello", "") is False


# ── load_library helper ──────────────────────────────────────

class TestLoadLibrary:
    def test_creates_new(self, tmp_path):
        lib = load_library(tmp_path / "new.json")
        assert lib.size == 0

    def test_loads_existing(self, tmp_path):
        path = tmp_path / "existing.json"
        path.write_text(json.dumps([{
            "category": "email",
            "description": "test",
            "source_task": "t",
            "tools_used": [],
            "quality_score": 1.0,
            "use_count": 0,
            "success_count": 0,
            "created_at": 1000.0,
            "last_used_at": 0.0,
        }]), encoding="utf-8")
        lib = load_library(path)
        assert lib.size == 1


# ── Integration: prompt_optimizer _format_strategies ──────────

class TestPromptOptimizerIntegration:
    def test_format_strategies_empty(self):
        from secretary.prompt_optimizer import _format_strategies
        result = _format_strategies([])
        assert "No learned strategies" in result

    def test_format_strategies_with_data(self):
        from secretary.prompt_optimizer import _format_strategies
        strategies = [
            Strategy(
                category="email",
                description="Search then read in parallel",
                source_task="t",
                quality_score=1.2,
                use_count=5,
                success_count=4,
            ),
            Strategy(
                category="research",
                description="Use grep_search for code patterns",
                source_task="t",
                quality_score=0.9,
            ),
        ]
        result = _format_strategies(strategies)
        assert "email" in result
        assert "research" in result
        assert "4/5" in result
        assert "q=1.20" in result

    def test_build_meta_prompt_includes_strategies(self):
        from secretary.prompt_optimizer import build_meta_prompt
        strategies = [
            Strategy(
                category="email",
                description="Search then read",
                source_task="t",
            ),
        ]
        prompt = build_meta_prompt([], [], {}, strategies)
        assert "Learned Strategy Library" in prompt
        assert "Search then read" in prompt

    def test_build_meta_prompt_no_strategies(self):
        from secretary.prompt_optimizer import build_meta_prompt
        prompt = build_meta_prompt([], [], {})
        assert "Learned Strategy Library" in prompt
        assert "No learned strategies" in prompt


# ── Integration: _build_system_prompt + _build_oracle_system_prompt ───

class TestSystemPromptStrategyInjection:
    """Test that strategy library content is injected into system prompts."""

    def _make_memory(self):
        from secretary.memory import MemoryStore
        return MemoryStore()

    def _make_library_with_email_strategy(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email",
            description="Search inbox then batch-read matching threads",
            source_task="Check emails",
            tools_used=["gmail_search", "gmail_read"],
            quality_score=1.0,
        ))
        return lib

    def test_direct_agent_injects_strategies(self):
        from secretary.direct_agent import _build_system_prompt
        mem = self._make_memory()
        lib = self._make_library_with_email_strategy()

        prompt = _build_system_prompt(mem, "Check my email inbox", strategy_library=lib)
        assert "Learned Strategies" in prompt
        assert "Search inbox then batch-read" in prompt

    def test_direct_agent_no_strategies_for_unmatched_category(self):
        from secretary.direct_agent import _build_system_prompt
        mem = self._make_memory()
        lib = self._make_library_with_email_strategy()

        # "calendar" task won't match "email" strategies
        prompt = _build_system_prompt(mem, "Check my calendar events", strategy_library=lib)
        assert "Search inbox then batch-read" not in prompt

    def test_direct_agent_no_library(self):
        from secretary.direct_agent import _build_system_prompt
        mem = self._make_memory()

        prompt = _build_system_prompt(mem, "Check my email inbox")
        assert "Learned Strategies" not in prompt

    def test_oracle_injects_strategies_for_workers(self):
        from secretary.oracle import _build_oracle_system_prompt
        mem = self._make_memory()
        lib = self._make_library_with_email_strategy()

        prompt = _build_oracle_system_prompt(mem, "Check my email inbox", 10, strategy_library=lib)
        assert "Learned Strategies" in prompt
        assert "Search inbox then batch-read" in prompt

    def test_oracle_no_strategies_for_checkpoints(self):
        from secretary.oracle import _build_oracle_system_prompt
        mem = self._make_memory()
        lib = self._make_library_with_email_strategy()

        prompt = _build_oracle_system_prompt(
            mem, "Check my email inbox", 10,
            is_checkpoint=True, strategy_library=lib,
        )
        assert "Learned Strategies" not in prompt

    def test_oracle_no_library(self):
        from secretary.oracle import _build_oracle_system_prompt
        mem = self._make_memory()

        prompt = _build_oracle_system_prompt(mem, "Check my email inbox", 10)
        assert "Learned Strategies" not in prompt


class TestStrategyOutcomeRecording:
    """Tests for strategy outcome recording (Layer 13 feedback loop)."""

    def test_record_outcome_success_boosts_quality(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email", description="Search then batch",
            source_task="email task", quality_score=1.0,
        )
        lib.add_strategy(s)

        # Simulate prompt injection (sets use_count > 0)
        prompt_text = lib.format_for_prompt("email")
        assert "Search then batch" in prompt_text

        initial_quality = lib.all_strategies()[0].quality_score
        lib.record_outcome("email", success=True)

        # Quality should increase
        assert lib.all_strategies()[0].quality_score > initial_quality
        assert lib.all_strategies()[0].success_count == 1

    def test_record_outcome_failure_decreases_quality(self):
        lib = StrategyLibrary()
        s = Strategy(
            category="email", description="Search then batch",
            source_task="email task", quality_score=1.0,
        )
        lib.add_strategy(s)

        # Simulate prompt injection
        lib.format_for_prompt("email")

        lib.record_outcome("email", success=False)

        assert lib.all_strategies()[0].quality_score < 1.0

    def test_record_outcome_noop_without_retrieval(self):
        """record_outcome is a no-op when no strategies were retrieved (use_count=0)."""
        lib = StrategyLibrary()
        s = Strategy(
            category="email", description="Search then batch",
            source_task="email task", quality_score=1.0,
        )
        lib.add_strategy(s)

        # DON'T retrieve — call record_outcome directly
        lib.record_outcome("email", success=True)

        # Quality unchanged (no strategies with use_count > 0)
        assert lib.all_strategies()[0].quality_score == 1.0
        assert lib.all_strategies()[0].success_count == 0

    def test_record_outcome_category_scoped(self):
        """Outcome for 'email' category doesn't affect 'calendar' strategies."""
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="Email strat",
            source_task="e", quality_score=1.0,
        ))
        lib.add_strategy(Strategy(
            category="calendar", description="Calendar strat",
            source_task="c", quality_score=1.0,
        ))

        # Retrieve both via format_for_prompt (sets use_count)
        lib.format_for_prompt("email")
        lib.format_for_prompt("calendar")

        # Record failure only for email
        lib.record_outcome("email", success=False)

        email_strats = [s for s in lib.all_strategies() if s.category == "email"]
        cal_strats = [s for s in lib.all_strategies() if s.category == "calendar"]

        assert email_strats[0].quality_score < 1.0
        assert cal_strats[0].quality_score == 1.0  # unchanged


class TestStrategyConsolidation:
    """Tests for strategy consolidation (decay + prune)."""

    def test_consolidate_decays_quality(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="Strat A",
            source_task="t", quality_score=1.0,
        ))
        lib.consolidate()
        # 1.0 * 0.95 = 0.95
        assert lib.all_strategies()[0].quality_score == pytest.approx(0.95, abs=0.01)

    def test_consolidate_prunes_low_quality(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="Weak strat",
            source_task="t", quality_score=0.31,
        ))
        lib.add_strategy(Strategy(
            category="email", description="Strong strat",
            source_task="t", quality_score=1.0,
        ))

        assert lib.size == 2
        pruned = lib.consolidate()

        # 0.31 * 0.95 = 0.2945 < 0.3 → pruned
        assert pruned == 1
        assert lib.size == 1
        assert lib.all_strategies()[0].description == "Strong strat"

    def test_consolidate_returns_zero_when_nothing_pruned(self):
        lib = StrategyLibrary()
        lib.add_strategy(Strategy(
            category="email", description="Strong",
            source_task="t", quality_score=2.0,
        ))
        pruned = lib.consolidate()
        assert pruned == 0
