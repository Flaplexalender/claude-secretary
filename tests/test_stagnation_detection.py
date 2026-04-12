"""Tests for stagnation detection and anti-analysis-paralysis features."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

from secretary.goal_self_improve import (
    _detect_stagnation,
    _guess_target_files,
    discard_stale_proposals,
    prune_old_proposals,
    _check_consecutive_failures,
    _get_improve_state,
    _WRITE_TOOLS,
    run_self_improve_analysis,
    record_proposal_result,
    STAGNATION_COOLDOWN_HOURS,
)
from secretary.goal_guardrails import apply_guardrails


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_entry(*, source="goals", success=True, tools_used=None, goal_id="g1"):
    """Create a minimal RunLogEntry-like object."""
    e = MagicMock()
    e.source = source
    e.success = success
    e.tools_used = tools_used or ["file_read", "grep_search"]
    e.goal_id = goal_id
    e.task = "test task"
    e.tier = "medium"
    e.model = "test"
    e.error = None
    e.output_preview = ""
    e.duration_s = 1.0
    e.num_turns = 1
    return e


def _make_config():
    c = MagicMock()
    c.anthropic_base_url = "http://localhost:8080/v1"
    c.data_path = MagicMock()
    c.data_path.parent = MagicMock()
    return c


# ── Stagnation detection ─────────────────────────────────────────────────

class TestStagnationDetection:
    def test_detects_investigation_only_goals(self):
        """Goals with stagnation keywords in evidence should generate proposals."""
        state = {
            "sub_goal_status": {
                "goal-planner": {
                    "status": "blocked",
                    "evidence": "generated 0 executable write tasks, investigation only",
                },
            },
        }
        entries = [_make_entry(source="goals", tools_used=["file_read"])]
        proposals = _detect_stagnation(state, entries, _make_config())
        assert len(proposals) >= 1
        assert proposals[0]["category"] == "stagnation-fix"
        assert "goal-planner" in proposals[0]["description"]

    def test_no_stagnation_when_write_tools_used(self):
        """If the stagnant goal's own recent tasks used write tools, no stagnation."""
        state = {
            "sub_goal_status": {
                "goal-planner": {
                    "status": "blocked",
                    "evidence": "no code changes, investigation only",
                },
            },
        }
        # Entry goal_id matches stagnant sub-goal parent → counts as that goal's activity
        entries = [_make_entry(source="goals", tools_used=["file_read", "file_write"], goal_id="goal-planner")]
        proposals = _detect_stagnation(state, entries, _make_config())
        assert len(proposals) == 0

    def test_stagnation_not_cleared_by_unrelated_goal_writes(self):
        """Write tools from a different goal should not clear stagnation."""
        state = {
            "sub_goal_status": {
                "goal-planner": {
                    "status": "blocked",
                    "evidence": "no code changes, investigation only",
                },
            },
        }
        # Entry is for different goal → should NOT prevent stagnation detection
        entries = [_make_entry(source="goals", tools_used=["file_read", "file_write"], goal_id="other-goal")]
        proposals = _detect_stagnation(state, entries, _make_config())
        assert len(proposals) >= 1

    def test_no_stagnation_for_done_goals(self):
        """Done goals shouldn't trigger stagnation."""
        state = {
            "sub_goal_status": {
                "textgrad-analysis": {
                    "status": "done",
                    "evidence": "read-only but completed",
                },
            },
        }
        entries = [_make_entry(source="goals")]
        proposals = _detect_stagnation(state, entries, _make_config())
        assert len(proposals) == 0

    def test_stagnation_keywords_case_insensitive(self):
        """Keywords matched case-insensitively."""
        state = {
            "sub_goal_status": {
                "self-improvement": {
                    "status": "in-progress",
                    "evidence": "All tasks are Read-Only investigations with No Implementation",
                },
            },
        }
        entries = [_make_entry(source="goals")]
        proposals = _detect_stagnation(state, entries, _make_config())
        assert len(proposals) >= 1

    def test_proposal_has_required_fields(self):
        """Stagnation proposals have all required fields."""
        state = {
            "sub_goal_status": {
                "failure-analysis": {
                    "status": "blocked",
                    "evidence": "zero executable outputs, read-only file parsing",
                },
            },
        }
        entries = [_make_entry(source="goals")]
        proposals = _detect_stagnation(state, entries, _make_config())
        assert len(proposals) == 1
        p = proposals[0]
        assert p["proposal_id"].startswith("stag-")
        assert p["status"] == "pending"
        assert "task_prompt" in p
        assert "target_files" in p
        assert "file_edit" in p["task_prompt"]  # Must mention write tools

    def test_max_proposals_capped(self):
        """At most MAX_PROPOSALS_PER_ANALYSIS stagnation proposals."""
        state = {
            "sub_goal_status": {
                f"sg-{i}": {
                    "status": "blocked",
                    "evidence": "investigation only, no code changes",
                }
                for i in range(10)
            },
        }
        entries = [_make_entry(source="goals")]
        proposals = _detect_stagnation(state, entries, _make_config())
        assert len(proposals) <= 3


# ── File target guessing ─────────────────────────────────────────────────

class TestGuessTargetFiles:
    def test_oracle_maps_to_oracle_py(self):
        files = _guess_target_files("oracle-all-tiers")
        assert "src/secretary/oracle.py" in files

    def test_goal_planner_maps_to_goals_py(self):
        files = _guess_target_files("goal-planner")
        assert "src/secretary/goals.py" in files

    def test_unknown_defaults_to_watcher(self):
        files = _guess_target_files("unknown-thing")
        assert "src/secretary/watcher.py" in files


# ── Stale proposal cleanup ──────────────────────────────────────────────

class TestDiscardStaleProposals:
    def test_cleans_nonstandard_status(self):
        """Proposals with non-standard statuses like 'expired' get discarded."""
        now = datetime.now(timezone.utc)
        state = {
            "self_improve_state": {
                "proposals": [
                    {"status": "expired", "created": (now - timedelta(hours=80)).isoformat()},
                    {"status": "pending", "created": (now - timedelta(hours=1)).isoformat(), "task_prompt": "Fix X"},
                ],
                "total_discarded": 0,
            },
        }
        count = discard_stale_proposals(state, max_age_hours=72.0)
        assert count == 1  # "expired" cleaned up
        assert state["self_improve_state"]["proposals"][0]["status"] == "discarded"
        assert state["self_improve_state"]["proposals"][1]["status"] == "pending"

    def test_preserves_valid_terminal_statuses(self):
        """completed, failed, discarded are all valid terminal states."""
        state = {
            "self_improve_state": {
                "proposals": [
                    {"status": "completed", "created": "2026-03-10T00:00:00+00:00"},
                    {"status": "failed", "created": "2026-03-10T00:00:00+00:00"},
                    {"status": "discarded", "created": "2026-03-10T00:00:00+00:00"},
                ],
                "total_discarded": 0,
            },
        }
        count = discard_stale_proposals(state, max_age_hours=72.0)
        assert count == 0  # None should be touched

    def test_discards_empty_task_pending(self):
        """Pending proposals with empty/missing task_prompt get discarded."""
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).isoformat()
        state = {
            "self_improve_state": {
                "proposals": [
                    {"status": "pending", "created": recent},
                    {"status": "pending", "created": recent, "task_prompt": ""},
                    {"status": "pending", "created": recent, "task_prompt": "Fix X"},
                ],
                "total_discarded": 0,
            },
        }
        count = discard_stale_proposals(state, max_age_hours=72.0)
        assert count == 2  # Both empty-task proposals discarded
        statuses = [p["status"] for p in state["self_improve_state"]["proposals"]]
        assert statuses == ["discarded", "discarded", "pending"]


class TestPruneOldProposals:
    def test_prunes_old_failed_and_discarded(self):
        """Old failed/discarded proposals are removed, completed are kept."""
        proposals = []
        # 25 old failed proposals
        for i in range(25):
            proposals.append({"status": "failed", "created": f"2026-03-{i+1:02d}T00:00:00+00:00"})
        # 5 completed proposals (should always be kept)
        for i in range(5):
            proposals.append({"status": "completed", "created": f"2026-03-{i+1:02d}T00:00:00+00:00"})
        # 1 pending (should be kept)
        proposals.append({"status": "pending", "created": "2026-03-22T00:00:00+00:00", "task_prompt": "Fix Y"})

        state = {"self_improve_state": {"proposals": proposals}}
        removed = prune_old_proposals(state, keep_recent=10)
        assert removed == 15  # 25 failed - 10 kept = 15 removed
        remaining = state["self_improve_state"]["proposals"]
        # 5 completed + 1 pending + 10 recent failed = 16
        assert len(remaining) == 16
        statuses = [p["status"] for p in remaining]
        assert statuses.count("completed") == 5
        assert statuses.count("pending") == 1
        assert statuses.count("failed") == 10

    def test_no_prune_when_under_limit(self):
        """Don't prune if fewer than keep_recent terminal proposals."""
        proposals = [
            {"status": "failed", "created": "2026-03-20T00:00:00+00:00"},
            {"status": "discarded", "created": "2026-03-19T00:00:00+00:00"},
        ]
        state = {"self_improve_state": {"proposals": proposals}}
        removed = prune_old_proposals(state, keep_recent=20)
        assert removed == 0
        assert len(state["self_improve_state"]["proposals"]) == 2


class TestConsecutiveFailureGate:
    def test_pauses_after_consecutive_failures(self):
        """Pipeline pauses when last N proposals all failed recently."""
        now = datetime.now(timezone.utc).isoformat()
        proposals = [
            {"status": "failed", "executed": now},
            {"status": "failed", "executed": now},
            {"status": "failed", "executed": now},
        ]
        state = {"self_improve_state": {"proposals": proposals}}
        assert _check_consecutive_failures(state) is True

    def test_resumes_after_success(self):
        """Pipeline resumes when a recent proposal succeeded."""
        now = datetime.now(timezone.utc).isoformat()
        proposals = [
            {"status": "failed", "executed": now},
            {"status": "completed", "executed": now},
            {"status": "failed", "executed": now},
        ]
        state = {"self_improve_state": {"proposals": proposals}}
        assert _check_consecutive_failures(state) is False

    def test_resumes_after_cooldown(self):
        """Pipeline resumes when cooldown expires."""
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        proposals = [
            {"status": "failed", "executed": old},
            {"status": "failed", "executed": old},
            {"status": "failed", "executed": old},
        ]
        state = {"self_improve_state": {"proposals": proposals}}
        assert _check_consecutive_failures(state) is False

    def test_not_enough_history(self):
        """Don't trigger with fewer than CONSECUTIVE_FAIL_LIMIT proposals."""
        now = datetime.now(timezone.utc).isoformat()
        proposals = [
            {"status": "failed", "executed": now},
        ]
        state = {"self_improve_state": {"proposals": proposals}}
        assert _check_consecutive_failures(state) is False


# ── Guardrails self-improve priority ─────────────────────────────────────

class TestGuardrailsSelfImprovePriority:
    def test_self_improve_tasks_survive_cap(self):
        """Self-improvement tasks should not be dropped by task count cap."""
        tasks = [
            {"prompt": "regular task 1", "tier": "medium", "source": "goals", "id": "r1"},
            {"prompt": "regular task 2", "tier": "medium", "source": "goals", "id": "r2"},
            {"prompt": "regular task 3", "tier": "medium", "source": "goals", "id": "r3"},
            {"prompt": "self-improve task", "tier": "medium", "source": "goals",
             "id": "si1", "_self_improve": True},
        ]
        result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=3)
        # The self-improve task should be in accepted, one regular dropped
        accepted_ids = [t["id"] for t in result.accepted]
        assert "si1" in accepted_ids
        assert len(result.accepted) == 3
        assert len(result.rejected) == 1

    def test_all_regular_if_under_cap(self):
        """When under cap, all tasks accepted regardless."""
        tasks = [
            {"prompt": "regular task prompt number one", "tier": "medium", "source": "goals", "id": "t1"},
            {"prompt": "regular task prompt number two", "tier": "medium", "source": "goals", "id": "t2"},
        ]
        result = apply_guardrails(tasks, max_tier="medium", max_tasks_per_cycle=3)
        assert len(result.accepted) == 2
        assert len(result.rejected) == 0


# ── Stagnation cooldown ──────────────────────────────────────────────────

class TestStagnationCooldown:
    """Test that stagnation detection is rate-limited independently."""

    def _make_cooldown_config(self):
        """Config with real self_improve attributes (MagicMock breaks getattr defaults)."""
        c = MagicMock()
        c.anthropic_base_url = "http://localhost:8080/v1"
        c.data_path = MagicMock()
        c.data_path.parent = MagicMock()
        c.self_improve.analysis_cooldown_hours = 0.5
        c.self_improve.stagnation_cooldown_hours = STAGNATION_COOLDOWN_HOURS
        return c

    def _make_state_with_stagnation(self, last_stagnation_iso=None):
        """State with a stagnant goal and no pending proposals."""
        state = {
            "sub_goal_status": {
                "goal-planner": {
                    "status": "blocked",
                    "evidence": "0 executable write tasks, investigation only",
                },
            },
            "self_improve_state": {
                "proposals": [],
                "total_proposed": 0,
                "total_executed": 0,
                "total_promoted": 0,
                "total_discarded": 0,
                "last_analysis": datetime.now(timezone.utc).isoformat(),  # failure cooldown active
            },
        }
        if last_stagnation_iso is not None:
            state["self_improve_state"]["last_stagnation_check"] = last_stagnation_iso
        return state

    @pytest.mark.asyncio
    async def test_stagnation_skipped_when_cooldown_active(self):
        """Stagnation check should be skipped if checked recently."""
        recent_check = datetime.now(timezone.utc).isoformat()
        state = self._make_state_with_stagnation(last_stagnation_iso=recent_check)

        run_log = MagicMock()
        run_log.recent.return_value = [_make_entry(source="goals")]
        config = self._make_cooldown_config()

        tasks = await run_self_improve_analysis(state, run_log, config)
        # Should return no tasks — stagnation cooldown prevents check
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_stagnation_runs_when_cooldown_expired(self):
        """Stagnation check should run if last check was long enough ago."""
        old_check = (
            datetime.now(timezone.utc) - timedelta(hours=STAGNATION_COOLDOWN_HOURS + 1)
        ).isoformat()
        state = self._make_state_with_stagnation(last_stagnation_iso=old_check)

        run_log = MagicMock()
        run_log.recent.return_value = [_make_entry(source="goals")]
        config = self._make_cooldown_config()

        tasks = await run_self_improve_analysis(state, run_log, config)
        # Should generate a stagnation proposal task
        assert len(tasks) >= 1
        assert tasks[0].get("_self_improve") is True

    @pytest.mark.asyncio
    async def test_stagnation_runs_on_first_ever_check(self):
        """First stagnation check (no timestamp) should proceed."""
        state = self._make_state_with_stagnation(last_stagnation_iso=None)

        run_log = MagicMock()
        run_log.recent.return_value = [_make_entry(source="goals")]
        config = self._make_cooldown_config()

        tasks = await run_self_improve_analysis(state, run_log, config)
        # No prior timestamp → should run stagnation detection
        assert len(tasks) >= 1
        # Check that the timestamp got set
        imp = state["self_improve_state"]
        assert "last_stagnation_check" in imp


class TestRecordProposalResult:
    def test_stores_test_output_on_failure(self):
        state = {
            "self_improve_state": {
                "proposals": [{
                    "proposal_id": "test-123",
                    "status": "executing",
                    "category": "stagnation-fix",
                }],
                "total_executed": 0,
                "total_promoted": 0,
            }
        }
        record_proposal_result(
            state, "test-123",
            success=False, promoted=False,
            changed_files=["MOD: src/secretary/goals.py"],
            error=None,
            test_output="FAILED tests/test_goals.py::test_foo - AssertionError",
        )
        result = state["self_improve_state"]["proposals"][0]["result"]
        assert result["test_output"] == "FAILED tests/test_goals.py::test_foo - AssertionError"
        assert state["self_improve_state"]["proposals"][0]["status"] == "failed"

    def test_no_test_output_on_success(self):
        state = {
            "self_improve_state": {
                "proposals": [{
                    "proposal_id": "test-456",
                    "status": "executing",
                    "category": "stagnation-fix",
                }],
                "total_executed": 0,
                "total_promoted": 0,
            }
        }
        record_proposal_result(
            state, "test-456",
            success=True, promoted=True,
            changed_files=["MOD: src/secretary/goals.py"],
            test_output="all tests pass",
        )
        result = state["self_improve_state"]["proposals"][0]["result"]
        assert result["test_output"] == "all tests pass"
        assert state["self_improve_state"]["proposals"][0]["status"] == "completed"

    def test_stagnation_proposals_include_prior_failure_context(self):
        """When prior stagnation proposals failed, new ones should reference the failure."""
        state = {
            "sub_goal_status": {
                "prefix-survival": {
                    "status": "blocked",
                    "evidence": "No code changes. investigation only."
                }
            },
            "self_improve_state": {
                "proposals": [{
                    "proposal_id": "stag-old",
                    "status": "failed",
                    "category": "stagnation-fix",
                    "description": "Sub-goal 'prefix-survival' is stuck",
                    "result": {
                        "success": False,
                        "promoted": False,
                        "changed_files": ["MOD: src/secretary/watcher.py"],
                        "test_output": "FAILED test_watcher.py::test_foo - NameError",
                    },
                }],
                "total_proposed": 1,
            }
        }
        entries = [_make_entry(source="goals", tools_used=["file_read"], goal_id="prefix-survival")]
        config = _make_config()

        proposals = _detect_stagnation(state, entries, config)
        assert len(proposals) >= 1
        task_prompt = proposals[0]["task_prompt"]
        assert "PRIOR FAILED ATTEMPT" in task_prompt
        assert "DIFFERENT change" in task_prompt
