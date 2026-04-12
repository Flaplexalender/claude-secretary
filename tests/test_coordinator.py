"""Tests for coordinator.py — multi-instance task claiming and coordination."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from secretary.coordinator import Coordinator, TaskResult, _atomic_write_json, _normalize_roles


@pytest.fixture
def shared_dir(tmp_path):
    """Shared coordination directory."""
    return tmp_path / "shared"


@pytest.fixture
def coord_a(shared_dir):
    """Instance A coordinator."""
    return Coordinator(shared_dir, instance_id="worker-a", role="researcher")


@pytest.fixture
def coord_b(shared_dir):
    """Instance B coordinator."""
    return Coordinator(shared_dir, instance_id="worker-b", role="triager")


@pytest.fixture
def coord_generalist(shared_dir):
    """Generalist coordinator (no role)."""
    return Coordinator(shared_dir, instance_id="default", role="")


# ── Instance registry ─────────────────────────────────────

class TestInstanceRegistry:
    def test_register_and_get_active(self, coord_a, coord_b):
        coord_a.register()
        coord_b.register()
        active = coord_a.get_active_instances()
        ids = {inst["instance_id"] for inst in active}
        assert "worker-a" in ids
        assert "worker-b" in ids

    def test_deregister(self, coord_a):
        coord_a.register()
        assert len(coord_a.get_active_instances()) == 1
        coord_a.deregister()
        assert len(coord_a.get_active_instances()) == 0

    def test_heartbeat_updates_last_seen(self, coord_a):
        coord_a.register()
        time.sleep(0.01)
        coord_a.heartbeat({"tasks_completed": 5})
        active = coord_a.get_active_instances()
        assert len(active) == 1
        assert active[0]["tasks_completed"] == 5

    def test_stale_instances_excluded(self, coord_a, shared_dir):
        coord_a.register()
        # Manually backdate the last_seen to make it stale
        instances = json.loads((shared_dir / "instances.json").read_text())
        instances["worker-a"]["last_seen"] = "2020-01-01T00:00:00+00:00"
        (shared_dir / "instances.json").write_text(json.dumps(instances))
        assert len(coord_a.get_active_instances()) == 0


# ── Task claiming ─────────────────────────────────────────

class TestTaskClaiming:
    def test_claim_succeeds(self, coord_a):
        assert coord_a.try_claim("do something") is True

    def test_claim_fails_if_already_claimed(self, coord_a, coord_b):
        assert coord_a.try_claim("shared task") is True
        assert coord_b.try_claim("shared task") is False

    def test_is_claimed(self, coord_a):
        assert coord_a.is_claimed("test task") is False
        coord_a.try_claim("test task")
        assert coord_a.is_claimed("test task") is True

    def test_get_claim_owner(self, coord_a, coord_b):
        coord_a.try_claim("my task")
        assert coord_a.get_claim_owner("my task") == "worker-a"
        assert coord_b.get_claim_owner("my task") == "worker-a"

    def test_unclaimed_task_owner_is_none(self, coord_a):
        assert coord_a.get_claim_owner("nobody took this") is None

    def test_release_claim(self, coord_a, coord_b):
        coord_a.try_claim("releasable")
        coord_a.release_claim("releasable")
        # Now B can claim it
        assert coord_b.try_claim("releasable") is True

    def test_release_all_claims(self, coord_a, coord_b):
        coord_a.try_claim("task 1")
        coord_a.try_claim("task 2")
        coord_b.try_claim("task 3")
        released = coord_a.release_all_claims()
        assert released == 2
        # B's claim still exists
        assert coord_b.is_claimed("task 3") is True

    def test_cleanup_stale_claims(self, coord_a, shared_dir):
        coord_a.try_claim("old task")
        # Backdate the claim
        claim_hash = Coordinator.task_hash("old task")
        claim_path = shared_dir / "queue" / f"{claim_hash}.claim"
        data = json.loads(claim_path.read_text())
        data["claimed_at"] = "2020-01-01T00:00:00+00:00"
        claim_path.write_text(json.dumps(data))
        cleaned = coord_a.cleanup_stale_claims(max_age_seconds=60)
        assert cleaned == 1
        assert not coord_a.is_claimed("old task")

    def test_task_hash_deterministic(self):
        h1 = Coordinator.task_hash("hello world")
        h2 = Coordinator.task_hash("hello world")
        h3 = Coordinator.task_hash("different task")
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 16


# ── Results sharing ───────────────────────────────────────

class TestResultsSharing:
    def test_publish_and_get_result(self, coord_a, coord_b):
        result = TaskResult(
            task_hash=Coordinator.task_hash("my task"),
            instance_id="worker-a",
            prompt="my task",
            success=True,
            output_preview="done",
            num_turns=3,
            cost_usd=0.01,
        )
        coord_a.publish_result(result)
        # B can read it
        fetched = coord_b.get_result("my task")
        assert fetched is not None
        assert fetched.success is True
        assert fetched.instance_id == "worker-a"
        assert fetched.num_turns == 3

    def test_get_result_missing(self, coord_a):
        assert coord_a.get_result("nonexistent") is None

    def test_get_all_results(self, coord_a):
        for i in range(3):
            result = TaskResult(
                task_hash=f"hash{i}",
                instance_id="worker-a",
                prompt=f"task {i}",
                success=True,
            )
            coord_a.publish_result(result)
        all_results = coord_a.get_all_results()
        assert len(all_results) == 3

    def test_clear_cycle(self, coord_a):
        coord_a.try_claim("claim me")
        coord_a.publish_result(TaskResult(
            task_hash="h1", instance_id="worker-a", prompt="t", success=True,
        ))
        claims, results = coord_a.clear_cycle()
        assert claims == 1
        assert results == 1
        assert not coord_a.is_claimed("claim me")
        assert coord_a.get_result("t", task_hash="h1") is None


# ── Role-based filtering ─────────────────────────────────

class TestRoleFiltering:
    def test_specialist_takes_matching_and_unroled(self, coord_a):
        tasks = [
            {"prompt": "research X", "role": "researcher"},
            {"prompt": "triage emails", "role": "triager"},
            {"prompt": "generic task"},
        ]
        filtered = coord_a.filter_tasks_for_role(tasks)
        prompts = {t["prompt"] for t in filtered}
        assert "research X" in prompts       # matches role
        assert "generic task" in prompts     # no role = any instance
        assert "triage emails" not in prompts  # wrong role

    def test_generalist_takes_unroled_and_orphaned(self, coord_generalist, shared_dir):
        # No specialists online → generalist takes everything
        tasks = [
            {"prompt": "research X", "role": "researcher"},
            {"prompt": "generic task"},
        ]
        filtered = coord_generalist.filter_tasks_for_role(tasks)
        prompts = {t["prompt"] for t in filtered}
        assert "generic task" in prompts
        assert "research X" in prompts  # no researcher online

    def test_generalist_defers_when_specialist_online(self, coord_generalist, coord_a):
        # Register researcher specialist
        coord_a.register()
        tasks = [
            {"prompt": "research X", "role": "researcher"},
            {"prompt": "generic task"},
        ]
        filtered = coord_generalist.filter_tasks_for_role(tasks)
        prompts = {t["prompt"] for t in filtered}
        assert "generic task" in prompts
        assert "research X" not in prompts  # researcher is online

    def test_multi_role_task(self, coord_a, coord_b):
        tasks = [{"prompt": "multi", "role": ["researcher", "triager"]}]
        a_filtered = coord_a.filter_tasks_for_role(tasks)
        b_filtered = coord_b.filter_tasks_for_role(tasks)
        assert len(a_filtered) == 1  # researcher matches
        assert len(b_filtered) == 1  # triager matches


# ── Helpers ───────────────────────────────────────────────

class TestHelpers:
    def test_normalize_roles_none(self):
        assert _normalize_roles(None) == set()
        assert _normalize_roles("") == set()

    def test_normalize_roles_string(self):
        assert _normalize_roles("researcher") == {"researcher"}

    def test_normalize_roles_list(self):
        assert _normalize_roles(["a", "b"]) == {"a", "b"}

    def test_atomic_write_json(self, tmp_path):
        path = tmp_path / "test.json"
        _atomic_write_json(path, {"key": "value"})
        assert json.loads(path.read_text()) == {"key": "value"}

    def test_atomic_write_json_overwrite(self, tmp_path):
        path = tmp_path / "test.json"
        _atomic_write_json(path, {"v": 1})
        _atomic_write_json(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}
