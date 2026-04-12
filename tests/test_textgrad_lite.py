"""Tests for TextGrad-lite — textual gradient eval failure analysis."""
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from secretary.textgrad_lite import (
    FailedTrace,
    TextualGradient,
    collect_failed_traces,
    generate_gradient,
    generate_gradients,
    gradient_to_proposal,
    format_gradients_for_autoresearch,
    run_textgrad_analysis,
    save_gradients,
    load_gradients,
)


# ── Fixtures ──────────────────────────────────────────────────

def _make_eval_tasks() -> dict:
    """Minimal eval_tasks.json structure."""
    return {
        "tasks": [
            {
                "id": "instr-04",
                "prompt": "Explain how majority voting works in the oracle ensemble",
                "category": "instruction",
                "score_type": "llm_judge",
                "judge_criteria": "Must mention 2/3 agreement threshold and tool call handling",
                "expected": "vote",
            },
            {
                "id": "hard-09",
                "prompt": "Compare ORACLE_TASK_HINTS vs TASK_HINTS style differences",
                "category": "hard",
                "score_type": "llm_judge",
                "judge_criteria": "Must describe prescriptive vs vague approaches",
                "expected": "prescriptive",
            },
            {
                "id": "comp-01",
                "prompt": "Calculate 17 * 23",
                "category": "computation",
                "score_type": "contains",
                "expected": "391",
            },
        ],
    }


def _make_eval_results(include_response=True) -> dict:
    """Minimal eval results JSON with failures."""
    results = {
        "metrics": {"eval_score": 0.77, "mean_score": 0.85},
        "tasks": [
            {
                "id": "instr-04",
                "category": "instruction",
                "score": 0.0,
                "success": False,
                "first_turn": False,
                "turns": 3,
                "elapsed_s": 12.5,
                "error": None,
                "response_text": "The oracle uses majority voting where workers vote on tool calls." if include_response else "",
                "judge_reason": "Missing 2/3 agreement threshold detail",
            },
            {
                "id": "hard-09",
                "category": "hard",
                "score": 0.0,
                "success": False,
                "first_turn": False,
                "turns": 4,
                "elapsed_s": 15.0,
                "error": None,
                "response_text": "ORACLE_TASK_HINTS and TASK_HINTS differ in style." if include_response else "",
                "judge_reason": "Does not describe prescriptive vs vague",
            },
            {
                "id": "comp-01",
                "category": "computation",
                "score": 1.0,
                "success": True,
                "first_turn": True,
                "turns": 1,
                "elapsed_s": 2.0,
                "error": None,
                "response_text": "391",
                "judge_reason": "",
            },
        ],
    }
    return results


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


# ── FailedTrace dataclass ────────────────────────────────────

class TestFailedTrace:
    def test_creation(self):
        trace = FailedTrace(
            task_id="instr-04",
            task_prompt="Explain voting",
            judge_criteria="Must mention threshold",
            response_text="Voting works by majority",
            judge_reason="Missing threshold",
            turns=3,
            category="instruction",
            score_type="llm_judge",
        )
        assert trace.task_id == "instr-04"
        assert trace.score_type == "llm_judge"


# ── TextualGradient dataclass ────────────────────────────────

class TestTextualGradient:
    def test_creation(self):
        g = TextualGradient(
            task_id="instr-04",
            root_cause="Agent doesn't mention threshold",
            critique="Missing 2/3 agreement detail",
            target="oracle.worker_rules",
            suggested_change="Add rule about mentioning thresholds",
            confidence=0.85,
            category="general",
        )
        assert g.confidence == 0.85
        assert g.target == "oracle.worker_rules"


# ── collect_failed_traces ────────────────────────────────────

class TestCollectFailedTraces:
    def test_collects_only_failures(self, tmp_path):
        _write_json(tmp_path / "results.json", _make_eval_results())
        _write_json(tmp_path / "tasks.json", _make_eval_tasks())

        traces = collect_failed_traces(
            tmp_path / "results.json",
            tmp_path / "tasks.json",
        )
        assert len(traces) == 2  # Only instr-04 and hard-09 (comp-01 passed)
        assert traces[0].task_id == "instr-04"
        assert traces[1].task_id == "hard-09"

    def test_skips_empty_response(self, tmp_path):
        _write_json(tmp_path / "results.json", _make_eval_results(include_response=False))
        _write_json(tmp_path / "tasks.json", _make_eval_tasks())

        traces = collect_failed_traces(
            tmp_path / "results.json",
            tmp_path / "tasks.json",
        )
        assert len(traces) == 0  # No response text → skipped

    def test_missing_file(self, tmp_path):
        traces = collect_failed_traces(
            tmp_path / "nonexistent.json",
            tmp_path / "tasks.json",
        )
        assert traces == []

    def test_pairs_with_task_defs(self, tmp_path):
        _write_json(tmp_path / "results.json", _make_eval_results())
        _write_json(tmp_path / "tasks.json", _make_eval_tasks())

        traces = collect_failed_traces(
            tmp_path / "results.json",
            tmp_path / "tasks.json",
        )
        # Should have judge_criteria from task def
        assert "2/3 agreement" in traces[0].judge_criteria
        assert "prescriptive" in traces[1].judge_criteria

    def test_caps_response_text(self, tmp_path):
        results = _make_eval_results()
        results["tasks"][0]["response_text"] = "x" * 5000
        _write_json(tmp_path / "results.json", results)
        _write_json(tmp_path / "tasks.json", _make_eval_tasks())

        traces = collect_failed_traces(
            tmp_path / "results.json",
            tmp_path / "tasks.json",
        )
        assert len(traces[0].response_text) <= 2000


# ── generate_gradient ─────────────────────────────────────────

class TestGenerateGradient:
    def _mock_response(self, gradient_json: dict):
        """Create a mock httpx response."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": json.dumps(gradient_json),
                },
            }],
        }
        mock_resp.raise_for_status = lambda: None
        return mock_resp

    def test_generates_gradient(self):
        trace = FailedTrace(
            task_id="instr-04",
            task_prompt="Explain voting",
            judge_criteria="Must mention threshold",
            response_text="Voting works by majority",
            judge_reason="Missing threshold",
            turns=3,
            category="instruction",
            score_type="llm_judge",
        )
        gradient_data = {
            "root_cause": "Agent omits the 2/3 threshold",
            "critique": "Response doesn't mention exact agreement ratio",
            "target": "oracle.worker_rules",
            "suggested_change": "Add: When explaining voting, always state the 2/3 agreement threshold",
            "confidence": 0.82,
            "category": "general",
        }
        with patch("secretary.textgrad_lite.httpx.post") as mock_post:
            mock_post.return_value = self._mock_response(gradient_data)
            gradient = generate_gradient(trace, "http://localhost:4141")

        assert gradient is not None
        assert gradient.task_id == "instr-04"
        assert gradient.confidence == 0.82
        assert gradient.target == "oracle.worker_rules"

    def test_handles_api_failure(self):
        trace = FailedTrace(
            task_id="instr-04",
            task_prompt="Explain voting",
            judge_criteria="Must mention threshold",
            response_text="Voting works",
            judge_reason="Missing threshold",
            turns=3,
            category="instruction",
            score_type="llm_judge",
        )
        with patch("secretary.textgrad_lite.httpx.post") as mock_post:
            mock_post.side_effect = Exception("connection refused")
            gradient = generate_gradient(trace, "http://localhost:4141")

        assert gradient is None

    def test_handles_code_fences(self):
        trace = FailedTrace(
            task_id="test-01",
            task_prompt="Test",
            judge_criteria="Test",
            response_text="Response",
            judge_reason="Reason",
            turns=1,
            category="test",
            score_type="llm_judge",
        )
        gradient_json = {
            "root_cause": "test",
            "critique": "test",
            "target": "direct_agent._TASK_HINTS",
            "suggested_change": "test",
            "confidence": 0.5,
            "category": "file",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": f"```json\n{json.dumps(gradient_json)}\n```",
                },
            }],
        }
        mock_resp.raise_for_status = lambda: None

        with patch("secretary.textgrad_lite.httpx.post") as mock_post:
            mock_post.return_value = mock_resp
            gradient = generate_gradient(trace, "http://localhost:4141")

        assert gradient is not None
        assert gradient.target == "direct_agent._TASK_HINTS"


# ── generate_gradients (batch) ────────────────────────────────

class TestGenerateGradients:
    def test_prioritizes_llm_judge(self):
        traces = [
            FailedTrace("comp-01", "calc", "", "wrong", "", 1, "computation", "contains"),
            FailedTrace("instr-04", "explain", "criteria", "wrong", "reason", 3, "instruction", "llm_judge"),
        ]
        gradient_data = {
            "root_cause": "test",
            "critique": "test",
            "target": "oracle.worker_rules",
            "suggested_change": "test",
            "confidence": 0.7,
            "category": "general",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(gradient_data)}}],
        }
        mock_resp.raise_for_status = lambda: None

        with patch("secretary.textgrad_lite.httpx.post") as mock_post:
            mock_post.return_value = mock_resp
            gradients = generate_gradients(traces, "http://localhost:4141", max_gradients=2)

        # Should have processed both, llm_judge first
        assert len(gradients) == 2

    def test_respects_max_gradients(self):
        traces = [
            FailedTrace(f"task-{i}", "test", "c", "r", "reason", 1, "x", "llm_judge")
            for i in range(10)
        ]
        gradient_data = {
            "root_cause": "t", "critique": "t", "target": "oracle.worker_rules",
            "suggested_change": "t", "confidence": 0.5, "category": "general",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(gradient_data)}}],
        }
        mock_resp.raise_for_status = lambda: None

        with patch("secretary.textgrad_lite.httpx.post") as mock_post:
            mock_post.return_value = mock_resp
            gradients = generate_gradients(traces, "http://localhost:4141", max_gradients=3)

        assert len(gradients) == 3

    def test_sorts_by_confidence(self):
        traces = [
            FailedTrace("t1", "test", "c", "r", "reason", 1, "x", "llm_judge"),
            FailedTrace("t2", "test", "c", "r", "reason", 1, "x", "llm_judge"),
        ]
        call_count = [0]

        def mock_post_fn(*args, **kwargs):
            call_count[0] += 1
            confidence = 0.3 if call_count[0] == 1 else 0.9
            data = {
                "root_cause": "t", "critique": "t", "target": "oracle.worker_rules",
                "suggested_change": "t", "confidence": confidence, "category": "general",
            }
            resp = MagicMock()
            resp.json.return_value = {
                "choices": [{"message": {"content": json.dumps(data)}}],
            }
            resp.raise_for_status = lambda: None
            return resp

        with patch("secretary.textgrad_lite.httpx.post", side_effect=mock_post_fn):
            gradients = generate_gradients(traces, "http://localhost:4141")

        assert gradients[0].confidence > gradients[1].confidence


# ── gradient_to_proposal ──────────────────────────────────────

class TestGradientToProposal:
    def test_basic_conversion(self):
        gradient = TextualGradient(
            task_id="instr-04",
            root_cause="Missing threshold detail",
            critique="Needs 2/3 agreement",
            target="oracle._ORACLE_TASK_HINTS",
            suggested_change="Add threshold mention hint",
            confidence=0.85,
            category="general",
        )
        proposal = gradient_to_proposal(gradient)

        assert proposal["category"] == "failure-fix"
        assert "TextGrad" in proposal["description"]
        assert "src/secretary/oracle.py" in proposal["target_files"]
        assert "TextGrad-generated" in proposal["task_prompt"]
        assert proposal["priority"] == 0.85
        assert proposal["source"] == "textgrad"

    def test_maps_direct_agent_target(self):
        gradient = TextualGradient(
            task_id="t1",
            root_cause="r",
            critique="c",
            target="direct_agent._TASK_HINTS",
            suggested_change="s",
            confidence=0.5,
            category="file",
        )
        proposal = gradient_to_proposal(gradient)
        assert "src/secretary/direct_agent.py" in proposal["target_files"]

    def test_maps_unknown_target_to_direct_agent(self):
        gradient = TextualGradient(
            task_id="t1",
            root_cause="r",
            critique="c",
            target="unknown_variable",
            suggested_change="s",
            confidence=0.5,
            category="general",
        )
        proposal = gradient_to_proposal(gradient)
        assert "src/secretary/direct_agent.py" in proposal["target_files"]

    def test_includes_gradient_data(self):
        gradient = TextualGradient(
            task_id="t1",
            root_cause="root",
            critique="crit",
            target="oracle.worker_rules",
            suggested_change="change",
            confidence=0.9,
            category="general",
        )
        proposal = gradient_to_proposal(gradient)
        assert "gradient" in proposal
        assert proposal["gradient"]["task_id"] == "t1"


# ── format_gradients_for_autoresearch ─────────────────────────

class TestFormatGradients:
    def test_empty(self):
        result = format_gradients_for_autoresearch([])
        assert "No gradients" in result

    def test_formats_multiple(self):
        gradients = [
            TextualGradient("t1", "r1", "c1", "oracle.worker_rules", "s1", 0.9, "general"),
            TextualGradient("t2", "r2", "c2", "direct_agent._TASK_HINTS", "s2", 0.7, "file"),
        ]
        result = format_gradients_for_autoresearch(gradients)
        assert "t1" in result
        assert "t2" in result
        assert "Gradient 1" in result
        assert "Gradient 2" in result
        assert "Recommendation" in result

    def test_includes_all_fields(self):
        gradient = TextualGradient(
            "instr-04", "root cause here", "critique here",
            "oracle._ORACLE_TASK_HINTS", "suggested change here",
            0.85, "general",
        )
        result = format_gradients_for_autoresearch([gradient])
        assert "root cause here" in result
        assert "critique here" in result
        assert "suggested change here" in result
        assert "0.85" in result


# ── Persistence ───────────────────────────────────────────────

class TestPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        gradients = [
            TextualGradient("t1", "r1", "c1", "target1", "s1", 0.9, "general"),
            TextualGradient("t2", "r2", "c2", "target2", "s2", 0.7, "file"),
        ]
        path = tmp_path / "gradients.json"
        save_gradients(gradients, path)

        loaded = load_gradients(path)
        assert len(loaded) == 2
        assert loaded[0].task_id == "t1"
        assert loaded[0].confidence == 0.9
        assert loaded[1].task_id == "t2"

    def test_load_missing_file(self, tmp_path):
        loaded = load_gradients(tmp_path / "nonexistent.json")
        assert loaded == []

    def test_load_corrupted_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        loaded = load_gradients(path)
        assert loaded == []


# ── run_textgrad_analysis (full pipeline) ─────────────────────

class TestRunTextgradAnalysis:
    def test_no_failures(self, tmp_path):
        results = {"metrics": {}, "tasks": [
            {"id": "comp-01", "score": 1.0, "success": True, "turns": 1},
        ]}
        tasks = {"tasks": [
            {"id": "comp-01", "prompt": "calc", "score_type": "contains", "expected": "391"},
        ]}
        _write_json(tmp_path / "results.json", results)
        _write_json(tmp_path / "tasks.json", tasks)

        gradients = run_textgrad_analysis(
            "http://localhost:4141",
            tmp_path / "results.json",
            tmp_path / "tasks.json",
        )
        assert gradients == []

    def test_full_pipeline(self, tmp_path):
        _write_json(tmp_path / "results.json", _make_eval_results())
        _write_json(tmp_path / "tasks.json", _make_eval_tasks())

        gradient_data = {
            "root_cause": "test",
            "critique": "test",
            "target": "oracle.worker_rules",
            "suggested_change": "add rule",
            "confidence": 0.8,
            "category": "general",
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps(gradient_data)}}],
        }
        mock_resp.raise_for_status = lambda: None

        with patch("secretary.textgrad_lite.httpx.post") as mock_post:
            mock_post.return_value = mock_resp
            gradients = run_textgrad_analysis(
                "http://localhost:4141",
                tmp_path / "results.json",
                tmp_path / "tasks.json",
            )

        assert len(gradients) == 2  # Two failures → two gradients
        assert all(g.confidence == 0.8 for g in gradients)


# ── Integration: run_eval.py _score_task return type ─────────

class TestRunEvalEnhancements:
    """Verify run_eval.py changes are backwards compatible."""

    def test_score_task_returns_tuple(self):
        """_score_task should now return (score, reason) tuple."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from run_eval import _score_task

        # contains type
        score, reason = _score_task("The answer is 391", {"score_type": "contains", "expected": "391"})
        assert score == 1.0
        assert reason == ""

        # exact_match type
        score, reason = _score_task("391", {"score_type": "exact_match", "expected": "391"})
        assert score == 1.0
        assert reason == ""

    def test_score_task_contains_fail(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from run_eval import _score_task

        score, reason = _score_task("No match here", {"score_type": "contains", "expected": "391"})
        assert score == 0.0
        assert reason == ""


# ── Integration: goal_self_improve ────────────────────────────

class TestSelfImproveIntegration:
    def test_run_textgrad_proposals_no_files(self, tmp_path):
        """Returns empty when no eval results exist."""
        from secretary.config import SecretaryConfig
        from secretary.goal_self_improve import run_textgrad_proposals

        (tmp_path / "data").mkdir()
        config = SecretaryConfig(data_root=str(tmp_path / "data"))

        state = {}
        proposals = run_textgrad_proposals(state, config)
        assert proposals == []

    def test_run_textgrad_proposals_with_results(self, tmp_path):
        """Generates proposals from eval results."""
        from secretary.config import SecretaryConfig
        from secretary.goal_self_improve import run_textgrad_proposals

        (tmp_path / "data").mkdir()
        config = SecretaryConfig(data_root=str(tmp_path / "data"))

        # Write eval files
        eval_results = tmp_path / "eval_results.json"
        eval_tasks = tmp_path / "eval_tasks.json"
        _write_json(eval_results, _make_eval_results())
        _write_json(eval_tasks, _make_eval_tasks())

        gradient_data_1 = {
            "root_cause": "timeout in network calls", "critique": "needs retry logic",
            "target": "oracle.worker_rules",
            "suggested_change": "add retry", "confidence": 0.8,
            "category": "general",
        }
        gradient_data_2 = {
            "root_cause": "missing validation", "critique": "input not checked",
            "target": "oracle.checkpoint_rules",
            "suggested_change": "add validation", "confidence": 0.7,
            "category": "general",
        }
        mock_resp_1 = MagicMock()
        mock_resp_1.json.return_value = {
            "choices": [{"message": {"content": json.dumps(gradient_data_1)}}],
        }
        mock_resp_1.raise_for_status = lambda: None
        mock_resp_2 = MagicMock()
        mock_resp_2.json.return_value = {
            "choices": [{"message": {"content": json.dumps(gradient_data_2)}}],
        }
        mock_resp_2.raise_for_status = lambda: None

        state = {}
        with patch("secretary.textgrad_lite.httpx.post") as mock_post:
            mock_post.side_effect = [mock_resp_1, mock_resp_2]
            proposals = run_textgrad_proposals(
                state, config,
                eval_results_path=eval_results,
                eval_tasks_path=eval_tasks,
            )

        assert len(proposals) == 2
        assert all(p["proposal_id"].startswith("tg-") for p in proposals)
        assert all(p["status"] == "pending" for p in proposals)

        # Should be stored in state
        imp_state = state["self_improve_state"]
        assert imp_state["total_proposed"] == 2
        assert len(imp_state["proposals"]) == 2
