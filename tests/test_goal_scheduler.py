"""Tests for Layer 20: goal_scheduler.py — Goal Scheduling & Trust Scoring."""

from __future__ import annotations

import pytest

from secretary.goal_scheduler import (
    CURRICULUM_LEVELS,
    GRADUATION_COOLDOWN_CYCLES,
    GRADUATION_LEVEL_ORDER,
    GRADUATION_POLICIES,
    MAX_GRADUATION_HISTORY,
    MAX_TRUST_SNAPSHOTS,
    MIN_GRADUATION_SAMPLES,
    MIN_STABLE_SNAPSHOTS,
    TRUST_LEVELS,
    _build_sub_goal_to_goal_map,
    apply_auto_graduation,
    build_execution_report,
    check_goal_graduation_rollback,
    check_graduation_eligibility,
    check_graduation_rollback,
    compute_all_trust_scores,
    compute_effective_level,
    compute_trust_score,
    evaluate_trust_graduation,
    format_graduation_history,
    format_schedule_section,
    format_trust_section,
    get_current_level_from_config,
    get_goal_policy,
    get_graduation_overrides,
    is_per_goal_overrides,
    record_execution_report,
    record_graduation_recommendations,
    record_trust_snapshot,
    select_active_goals,
    suggest_policy,
)


# ── Fixtures ─────────────────────────────────────────────────

@pytest.fixture()
def sample_goals() -> list[dict]:
    return [
        {"id": "prefix-survival", "priority": 1, "status": "in-progress",
         "sub_goals": [
             {"id": "oracle-production", "status": "done"},
             {"id": "learned-router", "status": "done"},
             {"id": "cost-monitoring", "status": "not-started"},
         ]},
        {"id": "self-sustaining-autonomy", "priority": 2, "status": "in-progress",
         "sub_goals": [
             {"id": "event-bus", "status": "done"},
             {"id": "goal-planner", "status": "in-progress"},
         ]},
        {"id": "self-improvement", "priority": 3, "status": "in-progress",
         "sub_goals": [
             {"id": "failure-analysis", "status": "in-progress"},
         ]},
        {"id": "autoresearch", "priority": 3, "status": "in-progress",
         "sub_goals": []},
        {"id": "oracle-default", "priority": 4, "status": "in-progress",
         "sub_goals": []},
        {"id": "stretch-features", "priority": 5, "status": "not-started",
         "sub_goals": []},
    ]


@pytest.fixture()
def state() -> dict:
    return {}


# ── select_active_goals ─────────────────────────────────────

class TestSelectActiveGoals:
    def test_level0_returns_empty(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=0)
        assert result == []

    def test_level1_only_priority_1_2(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=1)
        ids = [g["id"] for g in result]
        assert "prefix-survival" in ids
        assert "self-sustaining-autonomy" in ids
        assert "self-improvement" not in ids
        assert len(result) <= 2

    def test_level2_priority_up_to_4(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=2)
        ids = [g["id"] for g in result]
        assert "prefix-survival" in ids
        assert "self-improvement" in ids or "autoresearch" in ids
        assert "stretch-features" not in ids
        assert len(result) <= 3

    def test_level3_all_goals(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=3, max_active=10)
        assert len(result) == 6
        ids = [g["id"] for g in result]
        assert "stretch-features" in ids

    def test_sorted_by_priority(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=3, max_active=10)
        priorities = [g["priority"] for g in result]
        assert priorities == sorted(priorities)

    def test_done_goals_excluded(self, sample_goals: list) -> None:
        sample_goals[0]["status"] = "done"
        result = select_active_goals(sample_goals, curriculum_level=1)
        ids = [g["id"] for g in result]
        assert "prefix-survival" not in ids

    def test_max_active_override(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=3, max_active=1)
        assert len(result) == 1
        assert result[0]["id"] == "prefix-survival"

    def test_max_active_none_uses_curriculum_default(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=1, max_active=None)
        assert len(result) <= CURRICULUM_LEVELS[1]["max_active"]

    def test_empty_goals(self) -> None:
        result = select_active_goals([], curriculum_level=3)
        assert result == []

    def test_invalid_level_defaults_to_1(self, sample_goals: list) -> None:
        result = select_active_goals(sample_goals, curriculum_level=99)
        ids = [g["id"] for g in result]
        # Falls back to level 1 gate
        assert len(result) <= 2


# ── _build_sub_goal_to_goal_map ──────────────────────────────

class TestSubGoalMap:
    def test_maps_sub_goals(self, sample_goals: list) -> None:
        mapping = _build_sub_goal_to_goal_map(sample_goals)
        assert mapping["oracle-production"] == "prefix-survival"
        assert mapping["learned-router"] == "prefix-survival"
        assert mapping["event-bus"] == "self-sustaining-autonomy"
        assert mapping["failure-analysis"] == "self-improvement"

    def test_goals_without_sub_goals(self) -> None:
        goals = [{"id": "test", "sub_goals": []}]
        mapping = _build_sub_goal_to_goal_map(goals)
        assert mapping == {}

    def test_missing_sub_goals_key(self) -> None:
        goals = [{"id": "test"}]
        mapping = _build_sub_goal_to_goal_map(goals)
        assert mapping == {}


# ── compute_trust_score ──────────────────────────────────────

class TestComputeTrustScore:
    def test_no_data_returns_neutral(self, sample_goals: list) -> None:
        result = compute_trust_score("prefix-survival", {}, sample_goals)
        assert result["trust_score"] == 0.75
        assert result["verification_rate"] == 0.75
        assert result["approval_rate"] == 0.75
        assert result["success_rate"] == 0.75
        assert result["step_rate"] == 0.75

    def test_perfect_verification(self, sample_goals: list) -> None:
        state = {
            "verification_log": [
                {"sub_goal_id": "oracle-production", "verdict": "pass"},
                {"sub_goal_id": "learned-router", "verdict": "pass"},
                {"sub_goal_id": "cost-monitoring", "verdict": "pass"},
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert result["verification_rate"] == 1.0
        assert result["sample_sizes"]["verifications"] == 3

    def test_mixed_verification(self, sample_goals: list) -> None:
        state = {
            "verification_log": [
                {"sub_goal_id": "oracle-production", "verdict": "pass"},
                {"sub_goal_id": "cost-monitoring", "verdict": "fail"},
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert result["verification_rate"] == 0.5

    def test_approval_rate_all_approved(self, sample_goals: list) -> None:
        state = {
            "approval_queue": [
                {"goal_id": "prefix-survival", "status": "approved"},
                {"goal_id": "prefix-survival", "status": "executed"},
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert result["approval_rate"] == 1.0

    def test_approval_rate_with_rejections(self, sample_goals: list) -> None:
        state = {
            "approval_queue": [
                {"goal_id": "prefix-survival", "status": "approved"},
                {"goal_id": "prefix-survival", "status": "rejected"},
                {"goal_id": "prefix-survival", "status": "executed"},
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        # 1 rejection out of 3 decided → 2/3
        assert abs(result["approval_rate"] - 0.667) < 0.01

    def test_pending_approvals_ignored(self, sample_goals: list) -> None:
        state = {
            "approval_queue": [
                {"goal_id": "prefix-survival", "status": "pending"},
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert result["approval_rate"] == 0.75  # neutral (no decided)

    def test_run_log_success_rate(self, sample_goals: list) -> None:
        entries = [
            {"goal_id": "prefix-survival", "success": True},
            {"goal_id": "prefix-survival", "success": True},
            {"goal_id": "prefix-survival", "success": False},
        ]
        result = compute_trust_score("prefix-survival", {}, sample_goals, entries)
        assert abs(result["success_rate"] - 0.667) < 0.01
        assert result["sample_sizes"]["tasks"] == 3

    def test_other_goal_entries_excluded(self, sample_goals: list) -> None:
        entries = [
            {"goal_id": "prefix-survival", "success": True},
            {"goal_id": "other-goal", "success": False},
        ]
        result = compute_trust_score("prefix-survival", {}, sample_goals, entries)
        assert result["success_rate"] == 1.0
        assert result["sample_sizes"]["tasks"] == 1

    def test_step_completion_rate(self, sample_goals: list) -> None:
        state = {
            "step_plans": {
                "cost-monitoring": {
                    "goal_id": "prefix-survival",
                    "steps": [
                        {"status": "done"},
                        {"status": "done"},
                        {"status": "pending"},
                    ],
                },
            },
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert abs(result["step_rate"] - 0.667) < 0.01
        assert result["sample_sizes"]["steps"] == 3

    def test_full_trust_components(self, sample_goals: list) -> None:
        state = {
            "verification_log": [
                {"sub_goal_id": "oracle-production", "verdict": "pass"},
            ],
            "approval_queue": [
                {"goal_id": "prefix-survival", "status": "approved"},
            ],
            "step_plans": {
                "cost-monitoring": {
                    "goal_id": "prefix-survival",
                    "steps": [{"status": "done"}],
                },
            },
        }
        entries = [{"goal_id": "prefix-survival", "success": True}]
        result = compute_trust_score("prefix-survival", state, sample_goals, entries)
        # All components 1.0 → weighted avg = 1.0
        assert result["trust_score"] == 1.0

    def test_verification_filters_by_goal(self, sample_goals: list) -> None:
        """Verifications for other goals' sub-goals should not count."""
        state = {
            "verification_log": [
                {"sub_goal_id": "event-bus", "verdict": "fail"},  # self-sustaining's sub-goal
                {"sub_goal_id": "oracle-production", "verdict": "pass"},  # prefix's sub-goal
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert result["verification_rate"] == 1.0  # only oracle-production counts

    def test_goal_id_direct_match_in_verification(self, sample_goals: list) -> None:
        """Layer 30: Non-step goal tasks with goal_id match directly."""
        state = {
            "verification_log": [
                {"sub_goal_id": "", "goal_id": "prefix-survival", "verdict": "pass"},
                {"sub_goal_id": "", "goal_id": "prefix-survival", "verdict": "pass"},
                {"sub_goal_id": "", "goal_id": "prefix-survival", "verdict": "fail"},
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert abs(result["verification_rate"] - 0.667) < 0.01
        assert result["sample_sizes"]["verifications"] == 3

    def test_goal_id_and_sub_goal_id_combined(self, sample_goals: list) -> None:
        """Layer 30: Both sub_goal_id via mapping and direct goal_id contribute."""
        state = {
            "verification_log": [
                {"sub_goal_id": "oracle-production", "verdict": "pass"},  # via sub_goal mapping
                {"sub_goal_id": "", "goal_id": "prefix-survival", "verdict": "pass"},  # direct
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert result["verification_rate"] == 1.0
        assert result["sample_sizes"]["verifications"] == 2

    def test_goal_id_direct_excludes_other_goals(self, sample_goals: list) -> None:
        """Layer 30: goal_id entries for other goals don't leak in."""
        state = {
            "verification_log": [
                {"sub_goal_id": "", "goal_id": "self-sustaining-autonomy", "verdict": "fail"},
                {"sub_goal_id": "", "goal_id": "prefix-survival", "verdict": "pass"},
            ],
        }
        result = compute_trust_score("prefix-survival", state, sample_goals)
        assert result["verification_rate"] == 1.0
        assert result["sample_sizes"]["verifications"] == 1


# ── compute_all_trust_scores ─────────────────────────────────

class TestComputeAllTrustScores:
    def test_returns_scores_for_all_goals(self, sample_goals: list) -> None:
        result = compute_all_trust_scores(sample_goals, {})
        assert len(result) == 6
        assert "prefix-survival" in result
        assert "stretch-features" in result

    def test_each_score_has_fields(self, sample_goals: list) -> None:
        result = compute_all_trust_scores(sample_goals, {})
        for data in result.values():
            assert "trust_score" in data
            assert "verification_rate" in data
            assert "sample_sizes" in data


# ── suggest_policy ───────────────────────────────────────────

class TestSuggestPolicy:
    def test_low_trust_untrusted(self) -> None:
        p = suggest_policy(0.1)
        assert p["level"] == "untrusted"
        assert p["approval_mode"] == "review"
        assert p["tool_policy"] == "read-only"

    def test_cautious(self) -> None:
        p = suggest_policy(0.4)
        assert p["level"] == "cautious"
        assert p["approval_mode"] == "review"
        assert p["tool_policy"] == "supervised"

    def test_trusted(self) -> None:
        p = suggest_policy(0.7)
        assert p["level"] == "trusted"
        assert p["approval_mode"] == "notify"
        assert p["tool_policy"] == "supervised"

    def test_autonomous(self) -> None:
        p = suggest_policy(0.9)
        assert p["level"] == "autonomous"
        assert p["approval_mode"] == "auto"
        assert p["tool_policy"] == "full"

    def test_boundary_030(self) -> None:
        p = suggest_policy(0.3)
        assert p["level"] == "cautious"

    def test_boundary_060(self) -> None:
        p = suggest_policy(0.6)
        assert p["level"] == "trusted"

    def test_boundary_080(self) -> None:
        p = suggest_policy(0.8)
        assert p["level"] == "autonomous"

    def test_zero(self) -> None:
        p = suggest_policy(0.0)
        assert p["level"] == "untrusted"

    def test_one(self) -> None:
        p = suggest_policy(1.0)
        assert p["level"] == "autonomous"


# ── record_trust_snapshot ────────────────────────────────────

class TestRecordTrustSnapshot:
    def test_creates_snapshot(self, state: dict) -> None:
        scores = {
            "goal-a": {"trust_score": 0.75, "other": "data"},
        }
        record_trust_snapshot(state, scores)
        assert len(state["trust_snapshots"]) == 1
        snap = state["trust_snapshots"][0]
        assert "ts" in snap
        assert snap["scores"] == {"goal-a": 0.75}

    def test_multiple_snapshots(self, state: dict) -> None:
        for i in range(5):
            record_trust_snapshot(state, {"g": {"trust_score": i * 0.1}})
        assert len(state["trust_snapshots"]) == 5

    def test_capped_at_max(self, state: dict) -> None:
        for i in range(MAX_TRUST_SNAPSHOTS + 10):
            record_trust_snapshot(state, {"g": {"trust_score": 0.5}})
        assert len(state["trust_snapshots"]) == MAX_TRUST_SNAPSHOTS


# ── format_trust_section ─────────────────────────────────────

class TestFormatTrustSection:
    def test_empty(self) -> None:
        assert format_trust_section({}) == ""

    def test_includes_goal_id_and_score(self) -> None:
        scores = {
            "prefix-survival": {
                "trust_score": 0.75,
                "verification_rate": 0.8,
                "approval_rate": 0.9,
                "success_rate": 0.7,
                "step_rate": 0.5,
                "sample_sizes": {"verifications": 5, "approvals": 3, "tasks": 10, "steps": 4},
            },
        }
        text = format_trust_section(scores)
        assert "prefix-survival" in text
        assert "0.75" in text
        assert "trusted" in text

    def test_no_data_shows_neutral(self) -> None:
        scores = {
            "new-goal": {
                "trust_score": 0.75,
                "verification_rate": 0.75,
                "approval_rate": 0.75,
                "success_rate": 0.75,
                "step_rate": 0.75,
                "sample_sizes": {"verifications": 0, "approvals": 0, "tasks": 0, "steps": 0},
            },
        }
        text = format_trust_section(scores)
        assert "no data yet" in text


# ── format_schedule_section ──────────────────────────────────

class TestFormatScheduleSection:
    def test_shows_active_and_excluded(self, sample_goals: list) -> None:
        active = sample_goals[:2]
        text = format_schedule_section(active, sample_goals, curriculum_level=1)
        assert "ACTIVE" in text
        assert "excluded" in text
        assert "curriculum L1" in text

    def test_done_goals_show_done_reason(self, sample_goals: list) -> None:
        sample_goals[-1]["status"] = "done"
        text = format_schedule_section([], sample_goals, curriculum_level=1)
        assert "done" in text


# ── CURRICULUM_LEVELS consistency ────────────────────────────

class TestCurriculumLevels:
    def test_all_levels_defined(self) -> None:
        for level in range(4):
            assert level in CURRICULUM_LEVELS

    def test_level_0_blocks_everything(self) -> None:
        gate = CURRICULUM_LEVELS[0]
        assert gate["max_priority"] == 0
        assert gate["max_active"] == 0

    def test_level_3_allows_all(self) -> None:
        gate = CURRICULUM_LEVELS[3]
        assert gate["max_priority"] == 5
        assert gate["max_active"] == 5

    def test_progressive_gates(self) -> None:
        prev_priority = 0
        for level in range(4):
            gate = CURRICULUM_LEVELS[level]
            assert gate["max_priority"] >= prev_priority
            prev_priority = gate["max_priority"]


# ── TRUST_LEVELS consistency ─────────────────────────────────

class TestTrustLevels:
    def test_covers_full_range(self) -> None:
        assert TRUST_LEVELS[0][0] == 0.0
        assert TRUST_LEVELS[-1][1] > 1.0

    def test_no_gaps(self) -> None:
        for i in range(len(TRUST_LEVELS) - 1):
            assert TRUST_LEVELS[i][1] == TRUST_LEVELS[i + 1][0]

    def test_all_levels_named(self) -> None:
        names = [t[2] for t in TRUST_LEVELS]
        assert len(set(names)) == len(names)  # unique names


# ── evaluate_trust_graduation ────────────────────────────────

def _make_trust(score: float, samples: int = 10) -> dict:
    """Helper to build a trust score dict with given score and sample count."""
    per = samples // 4 or 1
    return {
        "trust_score": score,
        "verification_rate": score,
        "approval_rate": score,
        "success_rate": score,
        "step_rate": score,
        "sample_sizes": {
            "verifications": per, "approvals": per,
            "tasks": per, "steps": per,
        },
    }


class TestEvaluateTrustGraduation:
    def test_no_change_when_matching(self) -> None:
        trust = {"g1": _make_trust(0.1)}  # untrusted → review/read-only
        recs = evaluate_trust_graduation(trust, "review", "read-only")
        assert recs == []

    def test_upgrade_recommended(self) -> None:
        trust = {"g1": _make_trust(0.85)}  # autonomous level
        recs = evaluate_trust_graduation(trust, "review", "read-only")
        assert len(recs) == 1
        assert recs[0]["action"] == "upgrade"
        assert recs[0]["suggested_level"] == "autonomous"

    def test_downgrade_recommended(self) -> None:
        trust = {"g1": _make_trust(0.15)}  # untrusted
        recs = evaluate_trust_graduation(trust, "auto", "full")
        assert len(recs) == 1
        assert recs[0]["action"] == "downgrade"
        assert recs[0]["suggested_level"] == "untrusted"

    def test_insufficient_samples_skips(self) -> None:
        trust = {"g1": {
            "trust_score": 0.85,
            "verification_rate": 0.85,
            "approval_rate": 0.85,
            "success_rate": 0.85,
            "step_rate": 0.85,
            "sample_sizes": {"verifications": 0, "approvals": 0, "tasks": 1, "steps": 0},
        }}
        recs = evaluate_trust_graduation(trust, "review", "read-only")
        assert recs == []

    def test_multiple_goals(self) -> None:
        trust = {
            "g1": _make_trust(0.85),
            "g2": _make_trust(0.1),
        }
        recs = evaluate_trust_graduation(trust, "notify", "supervised")
        actions = {r["goal_id"]: r["action"] for r in recs}
        assert actions["g1"] == "upgrade"
        assert actions["g2"] == "downgrade"


class TestRecordGraduationRecommendations:
    def test_stores_recommendations(self) -> None:
        state: dict = {}
        recs = [{"goal_id": "g1", "action": "upgrade", "reason": "test"}]
        record_graduation_recommendations(state, recs)
        assert len(state["graduation_recommendations"]) == 1
        assert state["graduation_recommendations"][0]["recommendations"] == recs

    def test_empty_recs_noop(self) -> None:
        state: dict = {}
        record_graduation_recommendations(state, [])
        assert "graduation_recommendations" not in state

    def test_bounded_at_20(self) -> None:
        state: dict = {}
        for i in range(25):
            record_graduation_recommendations(
                state, [{"goal_id": "g1", "action": "upgrade", "reason": f"r{i}"}],
            )
        assert len(state["graduation_recommendations"]) == 20


# ── build_execution_report ───────────────────────────────────

class TestBuildExecutionReport:
    def test_basic_report(self) -> None:
        state: dict = {"verification_log": [], "step_plans": {}}
        trust = {"g1": _make_trust(0.7)}
        report = build_execution_report(
            state, cycle=5, tasks_generated=3, tasks_approved=2,
            tasks_executed=2, trust_scores=trust,
        )
        assert report["cycle"] == 5
        assert report["tasks_generated"] == 3
        assert report["tasks_executed"] == 2
        assert "ts" in report
        assert report["trust_scores"]["g1"] == 0.7

    def test_with_verification_data(self) -> None:
        state: dict = {
            "verification_log": [
                {"verdict": "pass"}, {"verdict": "fail"}, {"verdict": "pass"},
            ],
            "step_plans": {},
        }
        report = build_execution_report(
            state, cycle=1, tasks_generated=3, tasks_approved=3,
            tasks_executed=3, trust_scores={},
        )
        assert report["verification"]["pass"] == 2
        assert report["verification"]["fail"] == 1

    def test_with_graduation_recs(self) -> None:
        state: dict = {"verification_log": [], "step_plans": {}}
        recs = [{"goal_id": "g1", "action": "upgrade"}]
        report = build_execution_report(
            state, cycle=1, tasks_generated=0, tasks_approved=0,
            tasks_executed=0, trust_scores={}, graduation_recs=recs,
        )
        assert len(report["graduation_recommendations"]) == 1


class TestRecordExecutionReport:
    def test_stores_report(self) -> None:
        state: dict = {}
        record_execution_report(state, {"cycle": 1, "ts": "now"})
        assert len(state["execution_reports"]) == 1

    def test_bounded_at_50(self) -> None:
        state: dict = {}
        for i in range(55):
            record_execution_report(state, {"cycle": i})
        assert len(state["execution_reports"]) == 50


# ── Layer 23: Auto-Graduation Tests ──────────────────────────

def _make_trust(
    score: float,
    samples: int = 10,
    sample_sizes: dict | None = None,
) -> dict:
    """Helper: build a trust score dict."""
    if sample_sizes is None:
        per = samples // 4 or 1
        sample_sizes = {
            "verifications": per,
            "approvals": per,
            "tasks": per,
            "steps": per,
        }
    return {
        "trust_score": score,
        "verification_rate": score,
        "approval_rate": score,
        "success_rate": score,
        "step_rate": score,
        "sample_sizes": sample_sizes,
    }


class TestGetCurrentLevelFromConfig:
    def test_exact_match_untrusted(self) -> None:
        assert get_current_level_from_config("review", "read-only") == "untrusted"

    def test_exact_match_cautious(self) -> None:
        assert get_current_level_from_config("review", "supervised") == "cautious"

    def test_exact_match_trusted(self) -> None:
        assert get_current_level_from_config("notify", "supervised") == "trusted"

    def test_exact_match_autonomous(self) -> None:
        assert get_current_level_from_config("auto", "full") == "autonomous"

    def test_fallback_by_approval_mode(self) -> None:
        result = get_current_level_from_config("notify", "read-only")
        assert result == "trusted"  # Falls back to approval_mode match

    def test_unknown_defaults_untrusted(self) -> None:
        assert get_current_level_from_config("unknown", "unknown") == "untrusted"


class TestComputeEffectiveLevel:
    def test_no_data_returns_untrusted(self) -> None:
        trust = {"g1": _make_trust(0.9, sample_sizes={"verifications": 0, "approvals": 0, "tasks": 0, "steps": 0})}
        assert compute_effective_level(trust) == "untrusted"

    def test_single_goal_trusted(self) -> None:
        trust = {"g1": _make_trust(0.7)}
        assert compute_effective_level(trust) == "trusted"

    def test_single_goal_autonomous(self) -> None:
        trust = {"g1": _make_trust(0.9)}
        assert compute_effective_level(trust) == "autonomous"

    def test_multiple_goals_conservative(self) -> None:
        trust = {
            "g1": _make_trust(0.9),   # autonomous
            "g2": _make_trust(0.45),  # cautious
        }
        assert compute_effective_level(trust) == "cautious"

    def test_mixed_data_sufficiency(self) -> None:
        trust = {
            "g1": _make_trust(0.9),   # autonomous, has data
            "g2": _make_trust(0.2, sample_sizes={"verifications": 0, "approvals": 0, "tasks": 0, "steps": 0}),  # no data, ignored
        }
        assert compute_effective_level(trust) == "autonomous"


class TestCheckGraduationEligibility:
    def _state_with_snapshots(self, n: int, scores: dict | None = None) -> dict:
        """Create state with n trust snapshots."""
        if scores is None:
            scores = {"g1": 0.85}
        return {
            "trust_snapshots": [
                {"ts": f"2026-03-18T{i:02d}:00:00Z", "scores": scores}
                for i in range(n)
            ]
        }

    def test_already_at_level(self) -> None:
        state = self._state_with_snapshots(3)
        eligible, reason = check_graduation_eligibility(
            state, 10, "trusted", "trusted",
        )
        assert not eligible
        assert "Already" in reason

    def test_multi_level_upgrade_blocked(self) -> None:
        state = self._state_with_snapshots(3)
        eligible, reason = check_graduation_eligibility(
            state, 10, "autonomous", "untrusted",
        )
        assert not eligible
        assert "Multi-level" in reason

    def test_cooldown_blocks_upgrade(self) -> None:
        state = self._state_with_snapshots(3)
        state["graduation_history"] = [{"cycle": 8}]
        eligible, reason = check_graduation_eligibility(
            state, 10, "cautious", "untrusted",
        )
        assert not eligible
        assert "Cooldown" in reason

    def test_cooldown_passed(self) -> None:
        state = self._state_with_snapshots(3, scores={"g1": 0.45})
        state["graduation_history"] = [{"cycle": 3}]
        eligible, reason = check_graduation_eligibility(
            state, 10, "cautious", "untrusted",
        )
        assert eligible

    def test_insufficient_snapshots(self) -> None:
        state = self._state_with_snapshots(1)
        eligible, reason = check_graduation_eligibility(
            state, 10, "cautious", "untrusted",
        )
        assert not eligible
        assert "snapshots" in reason

    def test_unstable_trust_blocks(self) -> None:
        state = {
            "trust_snapshots": [
                {"ts": "t1", "scores": {"g1": 0.85}},
                {"ts": "t2", "scores": {"g1": 0.25}},  # Dip below trusted
            ]
        }
        eligible, reason = check_graduation_eligibility(
            state, 10, "trusted", "cautious",
        )
        assert not eligible
        assert "unstable" in reason

    def test_eligible_upgrade(self) -> None:
        state = self._state_with_snapshots(3, scores={"g1": 0.75})
        eligible, reason = check_graduation_eligibility(
            state, 10, "trusted", "cautious",
        )
        assert eligible
        assert reason == "Eligible"

    def test_downgrade_skips_cooldown(self) -> None:
        state = self._state_with_snapshots(3)
        state["graduation_history"] = [{"cycle": 9}]  # Just 1 cycle ago
        eligible, reason = check_graduation_eligibility(
            state, 10, "cautious", "trusted",
        )
        assert eligible  # Downgrades bypass cooldown

    def test_per_goal_cooldown_independent(self) -> None:
        """Per-goal cooldown: goal A's graduation shouldn't block goal B."""
        state = self._state_with_snapshots(3, scores={"g1": 0.45, "g2": 0.45})
        # Goal A graduated recently at cycle 8
        state["graduation_history"] = [{"cycle": 8, "goal_id": "goal-a"}]
        # Goal B should be eligible (no prior graduation)
        eligible, reason = check_graduation_eligibility(
            state, 10, "cautious", "untrusted", goal_id="goal-b",
        )
        assert eligible
        # Goal A should still be blocked
        eligible_a, reason_a = check_graduation_eligibility(
            state, 10, "trusted", "cautious", goal_id="goal-a",
        )
        assert not eligible_a
        assert "Cooldown" in reason_a

    def test_per_goal_stability_ignores_other_goals(self) -> None:
        """Per-goal stability: low-trust goal B shouldn't block high-trust goal A."""
        state = {
            "trust_snapshots": [
                {"ts": "t1", "scores": {"goal-a": 0.75, "goal-b": 0.35}},
                {"ts": "t2", "scores": {"goal-a": 0.80, "goal-b": 0.30}},
            ]
        }
        # goal-a → trusted should pass (0.75, 0.80 both support trusted)
        eligible, reason = check_graduation_eligibility(
            state, 10, "trusted", "cautious", goal_id="goal-a",
        )
        assert eligible
        # Without goal_id, goal-b's low trust blocks the upgrade
        eligible_global, reason_global = check_graduation_eligibility(
            state, 10, "trusted", "cautious",
        )
        assert not eligible_global
        assert "unstable" in reason_global


class TestApplyAutoGraduation:
    def test_creates_event_and_overrides(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.75)}
        event = apply_auto_graduation(state, 10, "trusted", "cautious", trust)
        assert event["action"] == "upgrade"
        assert event["old_level"] == "cautious"
        assert event["new_level"] == "trusted"
        assert event["approval_mode"] == "notify"
        assert event["tool_policy"] == "supervised"
        assert state["graduation_overrides"]["level"] == "trusted"
        assert len(state["graduation_history"]) == 1

    def test_downgrade_event(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.25)}
        event = apply_auto_graduation(state, 5, "untrusted", "cautious", trust)
        assert event["action"] == "downgrade"

    def test_history_bounded(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.5)}
        for i in range(MAX_GRADUATION_HISTORY + 5):
            apply_auto_graduation(state, i, "cautious", "untrusted", trust)
        assert len(state["graduation_history"]) == MAX_GRADUATION_HISTORY

    def test_overrides_updated_each_time(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.5)}
        apply_auto_graduation(state, 1, "cautious", "untrusted", trust)
        assert state["graduation_overrides"]["applied_cycle"] == 1
        apply_auto_graduation(state, 10, "trusted", "cautious", trust)
        assert state["graduation_overrides"]["applied_cycle"] == 10
        assert state["graduation_overrides"]["level"] == "trusted"


class TestGetGraduationOverrides:
    def test_none_when_empty(self) -> None:
        assert get_graduation_overrides({}) is None

    def test_none_when_no_level(self) -> None:
        assert get_graduation_overrides({"graduation_overrides": {}}) is None

    def test_returns_overrides(self) -> None:
        state = {"graduation_overrides": {"level": "trusted", "approval_mode": "notify"}}
        result = get_graduation_overrides(state)
        assert result is not None
        assert result["level"] == "trusted"


class TestCheckGraduationRollback:
    def test_no_overrides_returns_none(self) -> None:
        assert check_graduation_rollback({}, {"g1": _make_trust(0.1)}, 10) is None

    def test_already_at_minimum(self) -> None:
        state = {"graduation_overrides": {"level": "untrusted"}}
        assert check_graduation_rollback(state, {"g1": _make_trust(0.1)}, 10) is None

    def test_rollback_when_trust_drops(self) -> None:
        state = {"graduation_overrides": {"level": "trusted"}}
        trust = {"g1": _make_trust(0.35)}  # cautious level
        event = check_graduation_rollback(state, trust, 10)
        assert event is not None
        assert event["action"] == "downgrade"
        assert event["new_level"] == "cautious"

    def test_no_rollback_when_trust_holds(self) -> None:
        state = {"graduation_overrides": {"level": "trusted"}}
        trust = {"g1": _make_trust(0.75)}  # trusted level
        event = check_graduation_rollback(state, trust, 10)
        assert event is None

    def test_rollback_insufficient_data_triggers(self) -> None:
        state = {"graduation_overrides": {"level": "trusted"}}
        # No data = untrusted, which is below trusted
        trust = {"g1": _make_trust(0.9, sample_sizes={"verifications": 0, "approvals": 0, "tasks": 0, "steps": 0})}
        event = check_graduation_rollback(state, trust, 10)
        assert event is not None
        assert event["action"] == "downgrade"


class TestFormatGraduationHistory:
    def test_no_overrides(self) -> None:
        result = format_graduation_history({})
        assert "No active graduation override" in result

    def test_with_overrides(self) -> None:
        state = {
            "graduation_overrides": {
                "level": "trusted",
                "approval_mode": "notify",
                "tool_policy": "supervised",
                "applied_cycle": 10,
                "applied_at": "2026-03-18T12:00:00Z",
            }
        }
        result = format_graduation_history(state)
        assert "trusted" in result
        assert "notify" in result

    def test_with_history(self) -> None:
        state = {
            "graduation_overrides": {"level": "trusted", "approval_mode": "notify",
                                     "tool_policy": "supervised", "applied_cycle": 10,
                                     "applied_at": "2026-03-18T12:00:00Z"},
            "graduation_history": [
                {"cycle": 5, "old_level": "untrusted", "new_level": "cautious",
                 "action": "upgrade", "ts": "2026-03-18T10:00:00Z"},
                {"cycle": 10, "old_level": "cautious", "new_level": "trusted",
                 "action": "upgrade", "ts": "2026-03-18T12:00:00Z"},
            ],
        }
        result = format_graduation_history(state)
        assert "⬆" in result
        assert "cautious" in result
        assert "trusted" in result


# ── Layer 26: Per-Goal Graduation Tests ──────────────────────


class TestIsPerGoalOverrides:
    def test_empty_dict(self) -> None:
        assert not is_per_goal_overrides({})

    def test_legacy_global_format(self) -> None:
        ov = {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"}
        assert not is_per_goal_overrides(ov)

    def test_per_goal_format(self) -> None:
        ov = {
            "g1": {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"},
        }
        assert is_per_goal_overrides(ov)

    def test_mixed_not_detected(self) -> None:
        # Legacy fields at top level don't count
        ov = {"level": "trusted", "g1": {"level": "cautious"}}
        # Has at least one per-goal entry
        assert is_per_goal_overrides(ov)


class TestGetGoalPolicy:
    def test_no_overrides_uses_defaults(self) -> None:
        state: dict = {}
        result = get_goal_policy("g1", state, "review", "read-only")
        assert result["approval_mode"] == "review"
        assert result["tool_policy"] == "read-only"
        assert result["level"] == "untrusted"

    def test_per_goal_override(self) -> None:
        state = {
            "graduation_overrides": {
                "g1": {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"},
            }
        }
        result = get_goal_policy("g1", state, "review", "read-only")
        assert result["approval_mode"] == "notify"
        assert result["tool_policy"] == "supervised"
        assert result["level"] == "trusted"

    def test_other_goal_gets_defaults(self) -> None:
        state = {
            "graduation_overrides": {
                "g1": {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"},
            }
        }
        result = get_goal_policy("g2", state, "review", "read-only")
        assert result["approval_mode"] == "review"
        assert result["tool_policy"] == "read-only"

    def test_legacy_global_override_applies_to_all(self) -> None:
        state = {
            "graduation_overrides": {
                "level": "cautious",
                "approval_mode": "review",
                "tool_policy": "supervised",
            }
        }
        result = get_goal_policy("any-goal", state, "review", "read-only")
        assert result["tool_policy"] == "supervised"
        assert result["level"] == "cautious"

    def test_empty_goal_id_uses_global(self) -> None:
        state = {
            "graduation_overrides": {
                "level": "trusted",
                "approval_mode": "notify",
                "tool_policy": "supervised",
            }
        }
        result = get_goal_policy("", state, "review", "read-only")
        assert result["approval_mode"] == "notify"

    def test_per_goal_takes_precedence_over_defaults(self) -> None:
        """Different goals can have different policies."""
        state = {
            "graduation_overrides": {
                "g1": {"level": "autonomous", "approval_mode": "auto", "tool_policy": "full"},
                "g2": {"level": "cautious", "approval_mode": "review", "tool_policy": "supervised"},
            }
        }
        r1 = get_goal_policy("g1", state, "review", "read-only")
        r2 = get_goal_policy("g2", state, "review", "read-only")
        assert r1["approval_mode"] == "auto"
        assert r2["approval_mode"] == "review"
        assert r1["tool_policy"] == "full"
        assert r2["tool_policy"] == "supervised"


class TestApplyAutoGraduationPerGoal:
    def test_per_goal_override_stored(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.75)}
        event = apply_auto_graduation(state, 10, "trusted", "cautious", trust, goal_id="g1")
        assert event["goal_id"] == "g1"
        assert "g1" in state["graduation_overrides"]
        assert state["graduation_overrides"]["g1"]["level"] == "trusted"

    def test_multiple_goals_independent(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.75), "g2": _make_trust(0.45)}
        apply_auto_graduation(state, 10, "trusted", "cautious", trust, goal_id="g1")
        apply_auto_graduation(state, 10, "cautious", "untrusted", trust, goal_id="g2")
        assert state["graduation_overrides"]["g1"]["level"] == "trusted"
        assert state["graduation_overrides"]["g2"]["level"] == "cautious"

    def test_legacy_global_when_no_goal_id(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.75)}
        event = apply_auto_graduation(state, 10, "trusted", "cautious", trust)
        assert "level" in state["graduation_overrides"]
        assert state["graduation_overrides"]["level"] == "trusted"

    def test_history_records_goal_id(self) -> None:
        state: dict = {}
        trust = {"g1": _make_trust(0.5)}
        apply_auto_graduation(state, 5, "cautious", "untrusted", trust, goal_id="g1")
        assert state["graduation_history"][0]["goal_id"] == "g1"

    def test_per_goal_override_updates_not_replaces(self) -> None:
        """Upgrading g1 doesn't affect g2's override."""
        state: dict = {}
        trust = {"g1": _make_trust(0.7), "g2": _make_trust(0.5)}
        apply_auto_graduation(state, 5, "cautious", "untrusted", trust, goal_id="g2")
        apply_auto_graduation(state, 10, "trusted", "cautious", trust, goal_id="g1")
        assert state["graduation_overrides"]["g2"]["level"] == "cautious"
        assert state["graduation_overrides"]["g1"]["level"] == "trusted"


class TestCheckGoalGraduationRollback:
    def test_no_override_returns_none(self) -> None:
        state: dict = {}
        result = check_goal_graduation_rollback(state, "g1", _make_trust(0.1), 10)
        assert result is None

    def test_already_at_minimum(self) -> None:
        state = {"graduation_overrides": {"g1": {"level": "untrusted"}}}
        result = check_goal_graduation_rollback(state, "g1", _make_trust(0.1), 10)
        assert result is None

    def test_rollback_when_trust_drops(self) -> None:
        state = {
            "graduation_overrides": {
                "g1": {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"},
            }
        }
        result = check_goal_graduation_rollback(state, "g1", _make_trust(0.35), 10)
        assert result is not None
        assert result["action"] == "downgrade"
        assert result["goal_id"] == "g1"
        assert result["new_level"] == "cautious"

    def test_no_rollback_when_trust_holds(self) -> None:
        state = {
            "graduation_overrides": {
                "g1": {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"},
            }
        }
        result = check_goal_graduation_rollback(state, "g1", _make_trust(0.75), 10)
        assert result is None

    def test_rollback_only_affects_target_goal(self) -> None:
        state = {
            "graduation_overrides": {
                "g1": {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"},
                "g2": {"level": "autonomous", "approval_mode": "auto", "tool_policy": "full"},
            }
        }
        result = check_goal_graduation_rollback(state, "g1", _make_trust(0.35), 10)
        assert result is not None
        # g2 remains untouched
        assert state["graduation_overrides"]["g2"]["level"] == "autonomous"


class TestGetGraduationOverridesPerGoal:
    def test_returns_per_goal_format(self) -> None:
        state = {
            "graduation_overrides": {
                "g1": {"level": "trusted", "approval_mode": "notify", "tool_policy": "supervised"},
            }
        }
        result = get_graduation_overrides(state)
        assert result is not None
        assert "g1" in result

    def test_returns_legacy_format(self) -> None:
        state = {
            "graduation_overrides": {
                "level": "trusted", "approval_mode": "notify", "tool_policy": "supervised",
            }
        }
        result = get_graduation_overrides(state)
        assert result is not None
        assert result["level"] == "trusted"


class TestFormatGraduationHistoryPerGoal:
    def test_per_goal_display(self) -> None:
        state = {
            "graduation_overrides": {
                "g1": {"level": "trusted", "approval_mode": "notify",
                       "tool_policy": "supervised", "applied_cycle": 10},
                "g2": {"level": "cautious", "approval_mode": "review",
                       "tool_policy": "supervised", "applied_cycle": 8},
            },
            "graduation_history": [
                {"cycle": 8, "old_level": "untrusted", "new_level": "cautious",
                 "action": "upgrade", "ts": "2026-03-18T10:00:00Z", "goal_id": "g2"},
                {"cycle": 10, "old_level": "cautious", "new_level": "trusted",
                 "action": "upgrade", "ts": "2026-03-18T12:00:00Z", "goal_id": "g1"},
            ],
        }
        result = format_graduation_history(state)
        assert "Per-goal" in result
        assert "g1: trusted" in result
        assert "g2: cautious" in result
