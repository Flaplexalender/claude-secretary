"""Tests for goal_authoring — autonomous goal and campaign creation."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from secretary.goal_authoring import (
    MAX_GOALS_PER_CYCLE,
    MIN_AUTO_PRIORITY,
    generate_campaign,
    propose_goal,
)


@pytest.fixture
def goals_file(tmp_path: Path) -> Path:
    """Create a minimal goals.yaml for testing."""
    gf = tmp_path / "goals.yaml"
    data = {
        "goals": [
            {
                "id": "existing-goal",
                "description": "An existing goal",
                "success_criteria": "Tests pass",
                "priority": 2,
                "status": "in-progress",
            }
        ]
    }
    gf.write_text(yaml.dump(data), encoding="utf-8")
    return gf


@pytest.fixture
def campaigns_dir(tmp_path: Path) -> Path:
    """Create a campaigns directory for testing."""
    cd = tmp_path / "campaigns"
    cd.mkdir()
    return cd


class TestProposeGoal:
    def test_create_new_goal(self, goals_file: Path) -> None:
        ok, msg = propose_goal(
            goals_file,
            goal_id="improve-testing",
            description="Better test coverage",
            success_criteria="90% line coverage",
            priority=4,
        )
        assert ok is True
        assert "created" in msg.lower()

        # Verify it was written
        data = yaml.safe_load(goals_file.read_text(encoding="utf-8"))
        ids = [g["id"] for g in data["goals"]]
        assert "improve-testing" in ids

    def test_duplicate_rejected(self, goals_file: Path) -> None:
        ok, msg = propose_goal(
            goals_file,
            goal_id="existing-goal",
            description="Duplicate",
            success_criteria="N/A",
        )
        assert ok is False
        assert "already exists" in msg.lower()

    def test_high_priority_rejected(self, goals_file: Path) -> None:
        ok, msg = propose_goal(
            goals_file,
            goal_id="critical-thing",
            description="Very important",
            success_criteria="Done",
            priority=1,
        )
        assert ok is False
        assert "priority" in msg.lower()

    def test_priority_boundary(self, goals_file: Path) -> None:
        # Priority 3 should be allowed (MIN_AUTO_PRIORITY)
        ok, _ = propose_goal(
            goals_file,
            goal_id="boundary-test",
            description="Priority boundary",
            success_criteria="Done",
            priority=MIN_AUTO_PRIORITY,
        )
        assert ok is True

    def test_missing_fields_rejected(self, goals_file: Path) -> None:
        ok, msg = propose_goal(
            goals_file,
            goal_id="",
            description="",
            success_criteria="",
        )
        assert ok is False
        assert "required" in msg.lower()

    def test_goal_id_sanitized(self, goals_file: Path) -> None:
        ok, _ = propose_goal(
            goals_file,
            goal_id="  My Goal Name  ",
            description="Test sanitization",
            success_criteria="ID is kebab-case",
            priority=4,
        )
        assert ok is True
        data = yaml.safe_load(goals_file.read_text(encoding="utf-8"))
        ids = [g["id"] for g in data["goals"]]
        assert "my-goal-name" in ids

    def test_with_sub_goals(self, goals_file: Path) -> None:
        ok, _ = propose_goal(
            goals_file,
            goal_id="multi-step",
            description="Multi-step goal",
            success_criteria="All sub-goals done",
            priority=4,
            sub_goals=[
                {"id": "step-1", "description": "First step"},
                {"id": "step-2", "description": "Second step"},
            ],
        )
        assert ok is True
        data = yaml.safe_load(goals_file.read_text(encoding="utf-8"))
        goal = [g for g in data["goals"] if g["id"] == "multi-step"][0]
        assert len(goal["sub_goals"]) == 2
        assert goal["sub_goals"][0]["status"] == "not-started"

    def test_auto_created_flag(self, goals_file: Path) -> None:
        ok, _ = propose_goal(
            goals_file,
            goal_id="flagged",
            description="Should have auto_created flag",
            success_criteria="Flag present",
        )
        assert ok is True
        data = yaml.safe_load(goals_file.read_text(encoding="utf-8"))
        goal = [g for g in data["goals"] if g["id"] == "flagged"][0]
        assert goal.get("auto_created") is True

    def test_nonexistent_file_creates_it(self, tmp_path: Path) -> None:
        gf = tmp_path / "new_goals.yaml"
        ok, _ = propose_goal(
            gf,
            goal_id="fresh-goal",
            description="Brand new goals file",
            success_criteria="File exists",
        )
        assert ok is True
        assert gf.exists()

    def test_depends_on_filters_invalid(self, goals_file: Path) -> None:
        ok, _ = propose_goal(
            goals_file,
            goal_id="dependent",
            description="With dependencies",
            success_criteria="Done",
            depends_on=["existing-goal", "nonexistent-goal"],
        )
        assert ok is True
        data = yaml.safe_load(goals_file.read_text(encoding="utf-8"))
        goal = [g for g in data["goals"] if g["id"] == "dependent"][0]
        # Only valid dependency should remain
        assert goal.get("depends_on") == ["existing-goal"]


class TestGenerateCampaign:
    def test_create_campaign(self, campaigns_dir: Path) -> None:
        ok, msg = generate_campaign(
            campaigns_dir,
            campaign_name="test-sprint",
            tasks=[
                {"prompt": "Do thing 1", "tier": "low"},
                {"prompt": "Do thing 2", "tier": "medium"},
            ],
            description="Test campaign",
        )
        assert ok is True
        assert "created" in msg.lower()
        assert (campaigns_dir / "test-sprint.yaml").exists()

    def test_duplicate_rejected(self, campaigns_dir: Path) -> None:
        # Create first
        generate_campaign(campaigns_dir, "existing", [{"prompt": "X"}])
        # Try duplicate
        ok, msg = generate_campaign(campaigns_dir, "existing", [{"prompt": "Y"}])
        assert ok is False
        assert "already exists" in msg.lower()

    def test_empty_tasks_rejected(self, campaigns_dir: Path) -> None:
        ok, msg = generate_campaign(campaigns_dir, "empty", [])
        assert ok is False

    def test_yaml_extension_added(self, campaigns_dir: Path) -> None:
        ok, _ = generate_campaign(
            campaigns_dir,
            "no-extension",
            [{"prompt": "Test"}],
        )
        assert ok is True
        assert (campaigns_dir / "no-extension.yaml").exists()

    def test_campaign_content_valid(self, campaigns_dir: Path) -> None:
        generate_campaign(
            campaigns_dir,
            "valid",
            [
                {"prompt": "Task 1", "tier": "low", "schedule": "weekdays"},
                {"prompt": "Task 2", "tier": "medium"},
            ],
        )
        content = yaml.safe_load(
            (campaigns_dir / "valid.yaml").read_text(encoding="utf-8")
        )
        assert len(content["tasks"]) == 2
        assert content["tasks"][0]["tier"] == "low"
        assert content["tasks"][0]["schedule"] == "weekdays"
