"""Tests for prompt_optimizer — OPRO + TextGrad closed-loop prompt optimization."""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from secretary.prompt_optimizer import (
    Experiment,
    PromptProposal,
    load_trajectory,
    get_current_baseline,
    analyze_trajectory,
    read_current_targets,
    build_meta_prompt,
    generate_proposal,
    format_as_agent_instructions,
    run_optimization_cycle,
    save_proposal,
    load_proposal,
    _format_trajectory,
    _format_gradients,
    _format_dimension_summary,
    _format_targets,
)
from secretary.textgrad_lite import TextualGradient


# ── Fixtures ──────────────────────────────────────────────────

_TSV_HEADER = "timestamp\tcommit\teval_score\tmedian\tstatus\tdimension\tdescription\trun_scores\n"

_TSV_DATA = (
    _TSV_HEADER
    + "2026-03-16T22:57:35Z\t6cc98fe\t0.752\t0.752\tbaseline\tbaseline\tInitial baseline\t0.752,0.774,0.747\n"
    "2026-03-17T00:28:02Z\t6cc98fe\t0.776\t0.776\tkeep\tprompt_hints\tOracle file hint\t0.777,0.776,0.754\n"
    "2026-03-17T01:09:30Z\t102d8bc\t0.779\t0.779\tkeep\tvoting\tLongest-text tiebreaker\t0.758,0.779,0.779\n"
    "2026-03-17T02:02:30Z\t6ddd73d\t0.773\t0.773\tdiscard\tescalation\tText-divergence Opus escalation\t0.773,0.775,0.764\n"
    "2026-03-17T02:53:21Z\tcf114e1\t0.763\t0.763\tdiscard\tworker_config\tReplace gpt-5-mini with gpt-5.1\t0.774,0.755,0.763\n"
)


def _make_gradients() -> list[TextualGradient]:
    return [
        TextualGradient(
            task_id="instr-04",
            root_cause="Missing 2/3 agreement threshold detail",
            critique="Doesn't mention exact voting ratio",
            target="oracle.worker_rules",
            suggested_change="Add rule: when explaining voting, state 2/3 threshold",
            confidence=0.85,
            category="general",
        ),
        TextualGradient(
            task_id="hard-09",
            root_cause="Doesn't compare prescriptive vs vague styles",
            critique="Missing specific comparison",
            target="oracle._ORACLE_TASK_HINTS",
            suggested_change="Add hint for comparison tasks",
            confidence=0.7,
            category="general",
        ),
    ]


# ── Experiment dataclass ──────────────────────────────────────

class TestExperiment:
    def test_creation(self):
        exp = Experiment(
            timestamp="2026-03-16T22:57:35Z",
            commit="6cc98fe",
            eval_score=0.752,
            median=0.752,
            status="baseline",
            dimension="baseline",
            description="Initial baseline",
            run_scores="0.752,0.774,0.747",
        )
        assert exp.eval_score == 0.752
        assert exp.status == "baseline"


# ── PromptProposal dataclass ─────────────────────────────────

class TestPromptProposal:
    def test_creation(self):
        prop = PromptProposal(
            target_file="src/secretary/oracle.py",
            target_variable="_ORACLE_TASK_HINTS",
            change_description="Add hint for comparison tasks",
            exact_change='Add: "compare": "APPROACH: List both items, then contrast each aspect."',
            rationale="hard-09 fails because no comparison approach",
            expected_improvements=["hard-09"],
            risk_tasks=["instr-04"],
            confidence=0.8,
            source_gradient="hard-09",
        )
        assert prop.confidence == 0.8
        assert "hard-09" in prop.expected_improvements


# ── load_trajectory ───────────────────────────────────────────

class TestLoadTrajectory:
    def test_loads_tsv(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        tsv.write_text(_TSV_DATA, encoding="utf-8")

        experiments = load_trajectory(tsv)
        assert len(experiments) == 5
        assert experiments[0].status == "baseline"
        assert experiments[1].status == "keep"
        assert experiments[3].status == "discard"

    def test_missing_file(self, tmp_path):
        experiments = load_trajectory(tmp_path / "nonexistent.tsv")
        assert experiments == []

    def test_parses_scores(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        tsv.write_text(_TSV_DATA, encoding="utf-8")

        experiments = load_trajectory(tsv)
        assert experiments[1].eval_score == 0.776
        assert experiments[1].median == 0.776

    def test_malformed_row(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        data = _TSV_HEADER + "bad\tdata\tnot_a_number\t\t\t\t\t\n"
        tsv.write_text(data, encoding="utf-8")

        experiments = load_trajectory(tsv)
        assert len(experiments) == 0  # malformed row skipped


# ── get_current_baseline ──────────────────────────────────────

class TestGetCurrentBaseline:
    def test_finds_latest_keep(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        tsv.write_text(_TSV_DATA, encoding="utf-8")
        experiments = load_trajectory(tsv)

        baseline = get_current_baseline(experiments)
        assert baseline == 0.779  # last keep row (voting experiment)

    def test_empty(self):
        assert get_current_baseline([]) == 0.0

    def test_only_baseline(self):
        experiments = [Experiment(
            "ts", "abc", 0.75, 0.75, "baseline", "baseline", "Initial", "",
        )]
        assert get_current_baseline(experiments) == 0.75


# ── analyze_trajectory ────────────────────────────────────────

class TestAnalyzeTrajectory:
    def test_counts(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        tsv.write_text(_TSV_DATA, encoding="utf-8")
        experiments = load_trajectory(tsv)

        analysis = analyze_trajectory(experiments)
        assert analysis["total_experiments"] == 4  # excludes baseline
        assert analysis["kept_count"] == 2
        assert analysis["discarded_count"] == 2
        assert analysis["success_rate"] == 0.5

    def test_dimension_stats(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        tsv.write_text(_TSV_DATA, encoding="utf-8")
        experiments = load_trajectory(tsv)

        analysis = analyze_trajectory(experiments)
        dims = analysis["dimension_stats"]
        assert "prompt_hints" in dims
        assert "voting" in dims
        assert len(dims["prompt_hints"]["kept"]) == 1
        assert len(dims["escalation"]["discarded"]) == 1

    def test_empty(self):
        analysis = analyze_trajectory([])
        assert analysis["total_experiments"] == 0
        assert analysis["baseline"] == 0.0


# ── read_current_targets ──────────────────────────────────────

class TestReadCurrentTargets:
    def test_reads_real_targets(self):
        """Integration test — reads actual source files."""
        project_root = Path(__file__).resolve().parent.parent
        targets = read_current_targets(project_root)
        # Should find at least _TASK_HINTS and _ORACLE_TASK_HINTS
        assert "direct_agent._TASK_HINTS" in targets
        assert "oracle._ORACLE_TASK_HINTS" in targets
        # Values should contain actual hint text
        assert "email" in targets["direct_agent._TASK_HINTS"].lower() or "Focus" in targets["direct_agent._TASK_HINTS"]

    def test_missing_project_root(self, tmp_path):
        targets = read_current_targets(tmp_path / "nonexistent")
        assert targets == {}


# ── Format helpers ────────────────────────────────────────────

class TestFormatHelpers:
    def test_format_trajectory_empty(self):
        result = _format_trajectory([])
        assert "first optimization" in result.lower()

    def test_format_trajectory_with_data(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        tsv.write_text(_TSV_DATA, encoding="utf-8")
        experiments = load_trajectory(tsv)

        result = _format_trajectory(experiments)
        assert "BASELINE" in result
        assert "KEPT" in result
        assert "DISCARDED" in result

    def test_format_gradients_empty(self):
        result = _format_gradients([])
        assert "No TextGrad" in result

    def test_format_gradients_with_data(self):
        gradients = _make_gradients()
        result = _format_gradients(gradients)
        assert "instr-04" in result
        assert "hard-09" in result
        assert "0.85" in result

    def test_format_dimension_summary(self):
        dim_stats = {
            "prompt_hints": {
                "kept": ["Oracle file hint"],
                "discarded": ["Cite-specific-values"],
                "scores": [0.776, 0.777],
            },
        }
        result = _format_dimension_summary(dim_stats)
        assert "prompt_hints" in result
        assert "1 kept" in result
        assert "1 discarded" in result

    def test_format_targets(self):
        targets = {"oracle._ORACLE_TASK_HINTS": "_ORACLE_TASK_HINTS = {\"email\": \"test\"}"}
        result = _format_targets(targets)
        assert "oracle._ORACLE_TASK_HINTS" in result
        assert "```python" in result

    def test_format_targets_empty(self):
        result = _format_targets({})
        assert "Could not read" in result


# ── build_meta_prompt ─────────────────────────────────────────

class TestBuildMetaPrompt:
    def test_includes_all_sections(self, tmp_path):
        tsv = tmp_path / "results.tsv"
        tsv.write_text(_TSV_DATA, encoding="utf-8")
        experiments = load_trajectory(tsv)
        gradients = _make_gradients()
        targets = {"oracle._ORACLE_TASK_HINTS": "_ORACLE = {\"test\": \"val\"}"}

        prompt = build_meta_prompt(experiments, gradients, targets)
        assert "0.779" in prompt  # baseline
        assert "Previous Experiments" in prompt
        assert "TextGrad" in prompt
        assert "Optimization Targets" in prompt
        assert "instr-04" in prompt  # gradient task
        assert "oracle" in prompt  # target

    def test_empty_inputs(self):
        prompt = build_meta_prompt([], [], {})
        assert "first optimization" in prompt.lower()
        assert "No TextGrad" in prompt


# ── generate_proposal ─────────────────────────────────────────

class TestGenerateProposal:
    def _mock_response(self, proposal_json: dict):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {"content": json.dumps(proposal_json)},
            }],
        }
        mock_resp.raise_for_status = lambda: None
        return mock_resp

    def test_generates_proposal(self):
        proposal_data = {
            "target_file": "src/secretary/oracle.py",
            "target_variable": "_ORACLE_TASK_HINTS",
            "change_description": "Add comparison hint",
            "exact_change": '"compare": "APPROACH: contrast each aspect"',
            "rationale": "hard-09 fails without comparison approach",
            "expected_improvements": ["hard-09"],
            "risk_tasks": ["instr-04"],
            "confidence": 0.8,
            "source_gradient": "hard-09",
        }
        with patch("secretary.prompt_optimizer.httpx.post") as mock_post:
            mock_post.return_value = self._mock_response(proposal_data)
            proposal = generate_proposal("meta prompt text", "http://localhost:4141")

        assert proposal is not None
        assert proposal.target_variable == "_ORACLE_TASK_HINTS"
        assert proposal.confidence == 0.8
        assert "hard-09" in proposal.expected_improvements

    def test_handles_api_failure(self):
        with patch("secretary.prompt_optimizer.httpx.post") as mock_post:
            mock_post.side_effect = Exception("connection refused")
            proposal = generate_proposal("meta prompt", "http://localhost:4141")

        assert proposal is None

    def test_handles_code_fences(self):
        proposal_data = {
            "target_file": "src/secretary/oracle.py",
            "target_variable": "worker_rules",
            "change_description": "test",
            "exact_change": "test",
            "rationale": "test",
            "expected_improvements": ["t1"],
            "risk_tasks": [],
            "confidence": 0.6,
            "source_gradient": "t1",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {"content": f"```json\n{json.dumps(proposal_data)}\n```"},
            }],
        }
        mock_resp.raise_for_status = lambda: None

        with patch("secretary.prompt_optimizer.httpx.post") as mock_post:
            mock_post.return_value = mock_resp
            proposal = generate_proposal("prompt", "http://localhost:4141")

        assert proposal is not None
        assert proposal.target_variable == "worker_rules"


# ── format_as_agent_instructions ──────────────────────────────

class TestFormatAsAgentInstructions:
    def test_includes_all_fields(self):
        proposal = PromptProposal(
            target_file="src/secretary/oracle.py",
            target_variable="_ORACLE_TASK_HINTS",
            change_description="Add comparison hint for hard tasks",
            exact_change='"compare": "List both items, contrast each"',
            rationale="hard-09 fails without structured comparison",
            expected_improvements=["hard-09"],
            risk_tasks=["instr-04"],
            confidence=0.82,
            source_gradient="hard-09",
        )
        text = format_as_agent_instructions(proposal)
        assert "oracle.py" in text
        assert "_ORACLE_TASK_HINTS" in text
        assert "comparison hint" in text
        assert "hard-09" in text
        assert "instr-04" in text
        assert "0.82" in text
        assert "eval 3x" in text


# ── Persistence ───────────────────────────────────────────────

class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        proposal = PromptProposal(
            target_file="src/secretary/oracle.py",
            target_variable="_ORACLE_TASK_HINTS",
            change_description="test",
            exact_change="test",
            rationale="test",
            expected_improvements=["t1"],
            risk_tasks=["t2"],
            confidence=0.9,
            source_gradient="t1",
        )
        path = tmp_path / "proposal.json"
        save_proposal(proposal, path)

        loaded = load_proposal(path)
        assert loaded is not None
        assert loaded.target_variable == "_ORACLE_TASK_HINTS"
        assert loaded.confidence == 0.9
        assert loaded.expected_improvements == ["t1"]

    def test_load_missing_file(self, tmp_path):
        assert load_proposal(tmp_path / "nonexistent.json") is None

    def test_load_corrupted_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        assert load_proposal(path) is None


# ── run_optimization_cycle (full pipeline) ────────────────────

class TestRunOptimizationCycle:
    def test_no_trajectory_no_gradients(self, tmp_path):
        """Returns None when no data available at all."""
        with patch("secretary.prompt_optimizer.generate_proposal") as mock_gen:
            mock_gen.return_value = None  # No proposal from empty context
            result = run_optimization_cycle(
                base_url="http://localhost:4141",
                project_root=tmp_path,
            )
        assert result is None

    def test_with_trajectory_generates_proposal(self, tmp_path):
        """Loads trajectory and generates a proposal."""
        # Write TSV
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "autoresearch_results.tsv").write_text(
            _TSV_DATA, encoding="utf-8"
        )

        mock_proposal = PromptProposal(
            target_file="src/secretary/oracle.py",
            target_variable="_ORACLE_TASK_HINTS",
            change_description="test",
            exact_change="test",
            rationale="test",
            expected_improvements=["t1"],
            risk_tasks=[],
            confidence=0.8,
            source_gradient="t1",
        )

        with patch("secretary.prompt_optimizer.generate_proposal") as mock_gen:
            mock_gen.return_value = mock_proposal
            result = run_optimization_cycle(
                base_url="http://localhost:4141",
                project_root=tmp_path,
            )

        assert result is not None
        assert result.confidence == 0.8
        # Verify generate_proposal was called with a meta-prompt containing trajectory
        call_args = mock_gen.call_args[0][0]
        assert "0.779" in call_args  # baseline from trajectory

    def test_loads_cached_gradients(self, tmp_path):
        """Uses cached gradients when available."""
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "autoresearch_results.tsv").write_text(
            _TSV_DATA, encoding="utf-8"
        )

        # Write cached gradients
        from secretary.textgrad_lite import save_gradients
        gradients = _make_gradients()
        cache_path = tmp_path / "data" / "gradients.json"
        save_gradients(gradients, cache_path)

        with patch("secretary.prompt_optimizer.generate_proposal") as mock_gen:
            mock_gen.return_value = None
            run_optimization_cycle(
                base_url="http://localhost:4141",
                project_root=tmp_path,
                gradients_cache_path=cache_path,
            )

        # Meta-prompt should include gradient data
        call_args = mock_gen.call_args[0][0]
        assert "instr-04" in call_args
        assert "hard-09" in call_args

    def test_generates_gradients_from_eval(self, tmp_path):
        """Generates TextGrad gradients when eval results exist but no cache."""
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "autoresearch_results.tsv").write_text(
            _TSV_DATA, encoding="utf-8"
        )

        # Write eval results
        eval_results = {
            "metrics": {"eval_score": 0.77},
            "tasks": [
                {
                    "id": "instr-04", "score": 0.0, "success": False,
                    "turns": 3, "response_text": "Voting works by majority",
                    "judge_reason": "Missing threshold", "category": "instruction",
                },
            ],
        }
        eval_tasks = {
            "tasks": [
                {
                    "id": "instr-04", "prompt": "Explain voting",
                    "score_type": "llm_judge", "judge_criteria": "Must mention 2/3",
                    "expected": "vote",
                },
            ],
        }
        eval_path = tmp_path / "data" / "autoresearch_eval_0.json"
        eval_path.write_text(json.dumps(eval_results), encoding="utf-8")
        tasks_path = tmp_path / "eval_tasks.json"
        tasks_path.write_text(json.dumps(eval_tasks), encoding="utf-8")

        gradient_data = {
            "root_cause": "Missing threshold", "critique": "No 2/3",
            "target": "oracle.worker_rules",
            "suggested_change": "Add rule", "confidence": 0.8,
            "category": "general",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(gradient_data)}}],
        }
        mock_resp.raise_for_status = lambda: None

        with patch("secretary.textgrad_lite.httpx.post") as mock_textgrad, \
             patch("secretary.prompt_optimizer.generate_proposal") as mock_gen:
            mock_textgrad.return_value = mock_resp
            mock_gen.return_value = None
            run_optimization_cycle(
                base_url="http://localhost:4141",
                project_root=tmp_path,
                eval_results_path=eval_path,
                eval_tasks_path=tasks_path,
            )

        # Meta-prompt should include generated gradient
        call_args = mock_gen.call_args[0][0]
        assert "instr-04" in call_args


# ── Cross-task interference tracking ──────────────────────────

class TestCrossTaskTracking:
    def test_trajectory_captures_interference(self, tmp_path):
        """Verify trajectory analysis captures the cross-task interference pattern."""
        # Simulate: Exp #10 fixed hard-09 but broke instr-01
        data = (
            _TSV_DATA
            + "2026-03-17T21:44:46Z\te08199d\t0.756\t0.756\tdiscard\tprompt_hints\t"
            "direct_agent file hint — hard-09 fixed but instr-01 regressed\t0.770,0.756,0.756\n"
        )
        tsv = tmp_path / "results.tsv"
        tsv.write_text(data, encoding="utf-8")
        experiments = load_trajectory(tsv)

        analysis = analyze_trajectory(experiments)
        dims = analysis["dimension_stats"]

        # prompt_hints should show both kept and discarded
        assert len(dims["prompt_hints"]["kept"]) == 1
        assert len(dims["prompt_hints"]["discarded"]) == 1
        # The discarded description captures the interference
        assert "regressed" in dims["prompt_hints"]["discarded"][0]
