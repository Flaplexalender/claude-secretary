"""Tests for src/secretary/textgrad_evolution.py

Covers:
- PromptVariant / PromptEvolutionRound dataclasses & serialization
- save_evolution_round / load_evolution_rounds round-trip
- format_evolution_report structure
- variant_to_experiment_config output
- _format_traces_for_analysis (empty + populated)
- _extract_failure_patterns (keyword detection)
- generate_evolved_prompts (mocked LLM)
"""
from __future__ import annotations

import json
import pathlib
import tempfile
from unittest.mock import patch

import pytest

from secretary.textgrad_evolution import (
    PromptEvolutionRound,
    PromptVariant,
    _extract_failure_patterns,
    _format_traces_for_analysis,
    format_evolution_report,
    generate_evolved_prompts,
    load_evolution_rounds,
    save_evolution_round,
    variant_to_experiment_config,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

def make_variant(index: int = 1, confidence: float = 0.75) -> PromptVariant:
    return PromptVariant(
        variant_id=f"var-test{index:02d}",
        index=index,
        original_prompt="Do the thing carefully.",
        evolved_prompt="Do the thing carefully. Verify afterwards.",
        task_category="file",
        reasoning="Adds verification step to catch incomplete work.",
        changes_summary="- Added: verify step\n- Kept: original structure",
        confidence=confidence,
        expected_improvement_areas=["completeness", "verification"],
        risks=["may increase token usage"],
        source_traces=["task-001", "task-002"],
    )


def make_round(num_variants: int = 2) -> PromptEvolutionRound:
    return PromptEvolutionRound(
        round_id="round-testabcd1234",
        timestamp="2026-04-22T12:00:00+00:00",
        original_prompt="Do the thing carefully.",
        variants=[make_variant(i + 1, 0.7 + i * 0.05) for i in range(num_variants)],
        num_traces_analyzed=5,
        meta_analysis="- Incomplete task: 3 traces\n- Tool parallelization: 2 traces",
    )


# ── PromptVariant ──────────────────────────────────────────────────────────

class TestPromptVariant:
    def test_defaults(self):
        v = PromptVariant()
        assert v.variant_id.startswith("var-")
        assert v.index == 0
        assert v.confidence == 0.0
        assert v.expected_improvement_areas == []
        assert v.risks == []
        assert v.source_traces == []

    def test_to_dict_keys(self):
        v = make_variant()
        d = v.to_dict()
        assert set(d.keys()) == {
            "variant_id", "index", "original_prompt", "evolved_prompt",
            "task_category", "reasoning", "changes_summary", "confidence",
            "expected_improvement_areas", "risks", "source_traces",
        }

    def test_to_dict_values(self):
        v = make_variant(index=3, confidence=0.88)
        d = v.to_dict()
        assert d["index"] == 3
        assert d["confidence"] == 0.88
        assert d["task_category"] == "file"
        assert d["expected_improvement_areas"] == ["completeness", "verification"]

    def test_to_dict_is_json_serializable(self):
        v = make_variant()
        d = v.to_dict()
        # Should not raise
        json.dumps(d)

    def test_unique_variant_ids(self):
        ids = {PromptVariant().variant_id for _ in range(20)}
        assert len(ids) == 20  # All unique


# ── PromptEvolutionRound ─────────────────────────────────────────────────

class TestPromptEvolutionRound:
    def test_defaults(self):
        r = PromptEvolutionRound()
        assert r.round_id.startswith("round-")
        assert r.variants == []
        assert r.num_traces_analyzed == 0

    def test_to_dict_structure(self):
        r = make_round(2)
        d = r.to_dict()
        assert d["round_id"] == "round-testabcd1234"
        assert d["num_traces_analyzed"] == 5
        assert len(d["variants"]) == 2
        assert d["meta_analysis"].startswith("- Incomplete")

    def test_to_dict_variants_serialized(self):
        r = make_round(2)
        d = r.to_dict()
        v0 = d["variants"][0]
        assert "variant_id" in v0
        assert "evolved_prompt" in v0
        assert "confidence" in v0

    def test_to_dict_is_json_serializable(self):
        r = make_round(3)
        json.dumps(r.to_dict())  # Must not raise


# ── save / load round-trip ────────────────────────────────────────────────

class TestPersistence:
    def test_save_creates_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rounds.jsonl"
            r = make_round(2)
            save_evolution_round(r, p)
            assert p.exists()
            content = p.read_text()
            assert "round-testabcd1234" in content

    def test_load_empty_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "missing.jsonl"
            assert load_evolution_rounds(p) == []

    def test_save_load_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rounds.jsonl"
            original = make_round(2)
            save_evolution_round(original, p)
            loaded = load_evolution_rounds(p)

            assert len(loaded) == 1
            r = loaded[0]
            assert r.round_id == original.round_id
            assert r.num_traces_analyzed == original.num_traces_analyzed
            assert r.meta_analysis == original.meta_analysis
            assert len(r.variants) == 2

    def test_save_multiple_rounds_appends(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rounds.jsonl"
            r1 = make_round(2)
            r2 = PromptEvolutionRound(
                round_id="round-second000000",
                original_prompt="Other prompt",
                variants=[make_variant(1)],
                num_traces_analyzed=3,
            )
            save_evolution_round(r1, p)
            save_evolution_round(r2, p)
            loaded = load_evolution_rounds(p)
            assert len(loaded) == 2
            ids = {r.round_id for r in loaded}
            assert "round-testabcd1234" in ids
            assert "round-second000000" in ids

    def test_variant_fields_survive_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "rounds.jsonl"
            r = make_round(1)
            original_v = r.variants[0]
            save_evolution_round(r, p)
            loaded_v = load_evolution_rounds(p)[0].variants[0]
            assert loaded_v.variant_id == original_v.variant_id
            assert loaded_v.confidence == original_v.confidence
            assert loaded_v.expected_improvement_areas == original_v.expected_improvement_areas
            assert loaded_v.source_traces == original_v.source_traces

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "subdir" / "nested" / "rounds.jsonl"
            save_evolution_round(make_round(1), p)
            assert p.exists()


# ── format_evolution_report ──────────────────────────────────────────────

class TestFormatEvolutionReport:
    def test_contains_header(self):
        report = format_evolution_report(make_round(2))
        assert "TEXTGRAD PROMPT EVOLUTION REPORT" in report

    def test_contains_round_metadata(self):
        r = make_round(2)
        report = format_evolution_report(r)
        assert r.round_id in report
        assert "5" in report  # num_traces_analyzed

    def test_contains_variant_sections(self):
        r = make_round(2)
        report = format_evolution_report(r)
        assert "VARIANT 1" in report
        assert "VARIANT 2" in report

    def test_contains_confidence(self):
        report = format_evolution_report(make_round(1))
        assert "0.70" in report or "Confidence" in report

    def test_contains_meta_analysis(self):
        r = make_round(1)
        report = format_evolution_report(r)
        assert "Incomplete task" in report

    def test_contains_evolved_prompt_preview(self):
        r = make_round(1)
        report = format_evolution_report(r)
        assert "Evolved Prompt Preview" in report

    def test_contains_risks(self):
        r = make_round(1)
        report = format_evolution_report(r)
        assert "may increase token usage" in report or "Risks" in report


# ── variant_to_experiment_config ──────────────────────────────────────────

class TestVariantToExperimentConfig:
    def test_required_keys(self):
        v = make_variant()
        config = variant_to_experiment_config(v)
        assert "name" in config
        assert "system_prompt" in config
        assert "variant_id" in config
        assert "confidence" in config

    def test_name_format(self):
        v = make_variant()
        config = variant_to_experiment_config(v)
        assert config["name"].startswith("textgrad-")
        assert v.variant_id in config["name"]

    def test_system_prompt_is_evolved_prompt(self):
        v = make_variant()
        config = variant_to_experiment_config(v)
        assert config["system_prompt"] == v.evolved_prompt

    def test_expected_improvements_present(self):
        v = make_variant()
        config = variant_to_experiment_config(v)
        assert config["expected_improvements"] == v.expected_improvement_areas

    def test_risks_present(self):
        v = make_variant()
        config = variant_to_experiment_config(v)
        assert config["risks"] == v.risks


# ── _format_traces_for_analysis ──────────────────────────────────────────

class TestFormatTracesForAnalysis:
    def test_empty_returns_no_traces_message(self):
        result = _format_traces_for_analysis([])
        assert "no traces" in result.lower()

    def test_single_trace_contains_fields(self):
        trace = {
            "task_id": "task-abc",
            "judge_score": 0.3,
            "judge_reason": "Did not parallelize tool calls.",
            "category": "file",
        }
        result = _format_traces_for_analysis([trace])
        assert "task-abc" in result
        assert "0.3" in result
        assert "Did not parallelize" in result
        assert "FAILURE #1" in result

    def test_multiple_traces_numbered(self):
        traces = [
            {"task_id": f"task-{i}", "judge_score": 0.5, "judge_reason": f"Reason {i}"}
            for i in range(3)
        ]
        result = _format_traces_for_analysis(traces)
        assert "FAILURE #1" in result
        assert "FAILURE #2" in result
        assert "FAILURE #3" in result

    def test_includes_response_preview_if_present(self):
        trace = {
            "task_id": "task-xyz",
            "judge_score": 0.0,
            "judge_reason": "Incomplete",
            "response_text": "I started the task but\ndid not finish it\ndue to budget",
        }
        result = _format_traces_for_analysis([trace])
        assert "Response Preview" in result
        assert "I started the task" in result


# ── _extract_failure_patterns ─────────────────────────────────────────────

class TestExtractFailurePatterns:
    def test_empty_traces(self):
        result = _extract_failure_patterns([])
        assert "no patterns" in result.lower()

    def test_detects_parallelization(self):
        traces = [
            {"judge_reason": "Agent did not parallelize tool calls"},
            {"judge_reason": "Should have parallelized the search queries"},
        ]
        result = _extract_failure_patterns(traces)
        assert "paralleliz" in result.lower() or "Tool parallelization" in result

    def test_detects_incomplete(self):
        traces = [
            {"judge_reason": "Task was incomplete, missing final step"},
            {"judge_reason": "Response was incomplete and cut off"},
        ]
        result = _extract_failure_patterns(traces)
        assert "incomplete" in result.lower() or "Incomplete" in result

    def test_counts_multiple_patterns(self):
        traces = [
            {"judge_reason": "Did not parallelize tool calls"},
            {"judge_reason": "Response was incomplete"},
            {"judge_reason": "Did not parallelize anything"},
        ]
        result = _extract_failure_patterns(traces)
        # parallelization appears twice, incomplete once
        assert "2 traces" in result or "3" in result or "paralleliz" in result.lower()

    def test_no_matching_pattern_returns_generic(self):
        traces = [{"judge_reason": "Something very unusual happened xyz"}]
        result = _extract_failure_patterns(traces)
        assert result  # Non-empty


# ── generate_evolved_prompts (mocked LLM) ─────────────────────────────────

class TestGenerateEvolvedPrompts:
    """Test generate_evolved_prompts with mocked HTTP calls."""

    _MOCK_VARIANTS = [
        {
            "evolved_prompt": "Do the thing carefully. Always verify output before finishing.",
            "reasoning": "Added verification step to address incomplete task failures.",
            "changes_summary": "- Added verify step\n- Kept original structure",
            "confidence": 0.80,
            "expected_improvements": ["completeness"],
            "risks": ["slightly longer"],
        },
        {
            "evolved_prompt": "Do the thing carefully. Parallelize when multiple searches needed.",
            "reasoning": "Added parallelization guidance for tool-heavy tasks.",
            "changes_summary": "- Added parallel tool instruction",
            "confidence": 0.75,
            "expected_improvements": ["throughput"],
            "risks": [],
        },
    ]

    def _make_traces(self) -> list[dict]:
        return [
            {
                "task_id": f"task-{i:03d}",
                "judge_score": 0.5,
                "judge_reason": f"Failure reason {i}",
                "category": "file",
            }
            for i in range(3)
        ]

    def test_success_returns_round(self):
        with patch(
            "secretary.textgrad_evolution._call_textgrad_llm",
            return_value=self._MOCK_VARIANTS,
        ):
            result = generate_evolved_prompts(
                original_prompt="Do the thing carefully.",
                traces=self._make_traces(),
                base_url="http://localhost:8000",
                category="file",
            )
        assert result is not None
        assert isinstance(result, PromptEvolutionRound)
        assert len(result.variants) == 2

    def test_round_has_correct_metadata(self):
        with patch(
            "secretary.textgrad_evolution._call_textgrad_llm",
            return_value=self._MOCK_VARIANTS,
        ):
            result = generate_evolved_prompts(
                original_prompt="Do the thing carefully.",
                traces=self._make_traces(),
                base_url="http://localhost:8000",
                category="file",
            )
        assert result.num_traces_analyzed == 3
        assert result.original_prompt == "Do the thing carefully."

    def test_variant_fields_populated(self):
        with patch(
            "secretary.textgrad_evolution._call_textgrad_llm",
            return_value=self._MOCK_VARIANTS,
        ):
            result = generate_evolved_prompts(
                original_prompt="Do the thing carefully.",
                traces=self._make_traces(),
                base_url="http://localhost:8000",
            )
        v = result.variants[0]
        assert v.evolved_prompt != ""
        assert v.reasoning != ""
        assert v.confidence == 0.80
        assert v.task_category == "general"
        assert len(v.source_traces) == 3

    def test_fewer_than_2_variants_returns_none(self):
        with patch(
            "secretary.textgrad_evolution._call_textgrad_llm",
            return_value=[self._MOCK_VARIANTS[0]],  # Only 1 variant
        ):
            result = generate_evolved_prompts(
                original_prompt="Do the thing.",
                traces=self._make_traces(),
                base_url="http://localhost:8000",
            )
        assert result is None

    def test_llm_error_returns_none(self):
        with patch(
            "secretary.textgrad_evolution._call_textgrad_llm",
            return_value=None,
        ):
            result = generate_evolved_prompts(
                original_prompt="Do the thing.",
                traces=self._make_traces(),
                base_url="http://localhost:8000",
            )
        assert result is None

    def test_variants_have_unique_ids(self):
        with patch(
            "secretary.textgrad_evolution._call_textgrad_llm",
            return_value=self._MOCK_VARIANTS,
        ):
            result = generate_evolved_prompts(
                original_prompt="Prompt",
                traces=self._make_traces(),
                base_url="http://localhost:8000",
            )
        ids = [v.variant_id for v in result.variants]
        assert len(ids) == len(set(ids))

    def test_category_passed_to_variants(self):
        with patch(
            "secretary.textgrad_evolution._call_textgrad_llm",
            return_value=self._MOCK_VARIANTS,
        ):
            result = generate_evolved_prompts(
                original_prompt="Prompt",
                traces=self._make_traces(),
                base_url="http://localhost:8000",
                category="email",
            )
        for v in result.variants:
            assert v.task_category == "email"
