"""Goal Authoring — autonomous goal creation by the secretary.

Allows the secretary to propose new goals and sub-goals based on:
- Patterns observed in run_log (recurring failures, missing capabilities)
- Self-improvement insights (test gaps, code quality findings)
- Research discoveries (new techniques, architecture improvements)

Safety:
- New goals are added with status "not-started" and priority >= 3 (non-critical)
- Goals must have measurable success_criteria
- Duplicate detection prevents goal sprawl
- A human can always edit/remove goals from goals.yaml
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("secretary.goal_authoring")

# Maximum goals the secretary can create per review cycle
MAX_GOALS_PER_CYCLE = 2

# Minimum priority for auto-created goals (lower = more important)
# 3+ ensures they don't outrank human-set critical goals
MIN_AUTO_PRIORITY = 3


def propose_goal(
    goals_file: Path,
    goal_id: str,
    description: str,
    success_criteria: str,
    priority: int = 4,
    sub_goals: list[dict[str, str]] | None = None,
    depends_on: list[str] | None = None,
) -> tuple[bool, str]:
    """Add a new goal to goals.yaml if it doesn't already exist.

    Parameters
    ----------
    goals_file : Path to goals.yaml
    goal_id : unique id (kebab-case, e.g. "improve-test-coverage")
    description : what this goal achieves
    success_criteria : measurable outcome
    priority : 3-5 (auto-created goals can't be priority 1-2)
    sub_goals : optional list of sub-goal dicts
    depends_on : optional list of prerequisite goal IDs

    Returns
    -------
    (success: bool, message: str)
    """
    # Validate priority range
    if priority < MIN_AUTO_PRIORITY:
        return False, (
            f"Auto-created goals must have priority >= {MIN_AUTO_PRIORITY}. "
            f"Got priority={priority}. Only humans can set high-priority goals."
        )

    # Validate required fields
    if not goal_id or not description or not success_criteria:
        return False, "goal_id, description, and success_criteria are required."

    # Sanitize goal_id
    goal_id = goal_id.strip().lower().replace(" ", "-")

    # Load existing goals
    try:
        with open(goals_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {"goals": []}

    goals = data.get("goals", [])

    # Check for duplicates
    existing_ids = {g.get("id", "") for g in goals}
    if goal_id in existing_ids:
        return False, f"Goal '{goal_id}' already exists. Use goal updates instead."

    # Build the new goal
    new_goal: dict[str, Any] = {
        "id": goal_id,
        "description": description,
        "success_criteria": success_criteria,
        "priority": priority,
        "status": "not-started",
        "auto_created": True,
    }

    if sub_goals:
        new_goal["sub_goals"] = [
            {
                "id": sg.get("id", f"{goal_id}-step-{i}"),
                "description": sg.get("description", ""),
                "status": "not-started",
            }
            for i, sg in enumerate(sub_goals, 1)
        ]

    if depends_on:
        # Only include dependencies that actually exist
        valid_deps = [d for d in depends_on if d in existing_ids]
        if valid_deps:
            new_goal["depends_on"] = valid_deps

    # Append and save
    goals.append(new_goal)
    data["goals"] = goals

    try:
        with open(goals_file, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        log.info("Created new goal: %s (priority %d)", goal_id, priority)
        return True, f"Goal '{goal_id}' created successfully."
    except Exception as e:
        log.error("Failed to write goals.yaml: %s", e)
        return False, f"Failed to write goals.yaml: {e}"


def generate_campaign(
    campaigns_dir: Path,
    campaign_name: str,
    tasks: list[dict[str, str]],
    description: str = "",
) -> tuple[bool, str]:
    """Create a new campaign YAML file.

    Parameters
    ----------
    campaigns_dir : Path to campaigns/ directory
    campaign_name : filename without extension (e.g. "research-sprint")
    tasks : list of task dicts with 'prompt' and optional 'tier', 'schedule'
    description : optional description comment

    Returns
    -------
    (success: bool, message: str)
    """
    if not campaign_name or not tasks:
        return False, "campaign_name and tasks are required."

    # Sanitize name
    campaign_name = campaign_name.strip().lower().replace(" ", "-")
    if not campaign_name.endswith((".yaml", ".yml")):
        campaign_name += ".yaml"

    target = campaigns_dir / campaign_name

    # Don't overwrite existing campaigns
    if target.exists():
        return False, f"Campaign '{campaign_name}' already exists."

    # Build campaign structure
    campaign_data: dict[str, Any] = {}
    if description:
        campaign_data["_description"] = description

    campaign_tasks = []
    for task in tasks:
        t: dict[str, Any] = {"prompt": task["prompt"]}
        if "tier" in task:
            t["tier"] = task["tier"]
        if "schedule" in task:
            t["schedule"] = task["schedule"]
        if "priority" in task:
            t["priority"] = int(task["priority"])
        campaign_tasks.append(t)

    campaign_data["tasks"] = campaign_tasks

    try:
        campaigns_dir.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            if description:
                f.write(f"# {description}\n\n")
            yaml.dump(
                {"tasks": campaign_tasks},
                f,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
        log.info("Created campaign: %s (%d tasks)", campaign_name, len(tasks))
        return True, f"Campaign '{campaign_name}' created with {len(tasks)} tasks."
    except Exception as e:
        log.error("Failed to write campaign: %s", e)
        return False, f"Failed to write campaign: {e}"
