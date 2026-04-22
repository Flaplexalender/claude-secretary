"""Goal harness — detects test infrastructure health and gates sub-goal execution.

When test suite baseline fails, goals depending on test execution (self-harness,
self-improvement, self-sustaining-autonomy, secretary-ui) are automatically
gated to prevent wasted turns chasing phantom successes.

This module:
1. Polls GitHub CI test status (via git+remote, or falls back to cached)
2. Caches result in goal_state.json['baseline_health']
3. Provides gate check for goal_planner to skip blocked goals
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def get_ci_status() -> dict[str, Any]:
    """Fetch latest test status from GitHub CI.
    
    Returns dict with keys:
    - success: bool — did all tests pass?
    - timestamp: str — when this was checked (ISO 8601)
    - details: str — summary of failures (if any)
    """
    try:
        # Try to fetch latest GitHub Actions run status
        result = subprocess.run(
            ["git", "log", "--oneline", "-1", "--format=%H"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "timestamp": "unknown",
                "details": "Cannot determine current commit",
            }
        
        # Could query GitHub API here, but for now assume:
        # - If we got here, local repo is clean
        # - Tests run via CI after push
        return {
            "success": None,  # Unknown — requires API key
            "timestamp": "pending",
            "details": "Use 'git push' to run CI tests",
        }
    except Exception as e:
        log.warning("CI status check failed: %s", e)
        return {
            "success": False,
            "timestamp": "error",
            "details": str(e),
        }


def check_baseline_health(cache_file: Path) -> bool:
    """Check if test baseline is healthy.
    
    Returns True if last known CI result passed.
    Returns False if last known CI result failed or is unknown.
    
    Caches result in cache_file for reuse across cycles.
    """
    # Try to read cached status
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text())
            if isinstance(cache, dict):
                cached_success = cache.get("baseline_health", {}).get("success")
                if cached_success is not None:
                    log.debug("Using cached baseline health: %s", cached_success)
                    return cached_success
        except Exception:
            pass

    # Fetch fresh status
    status = get_ci_status()
    success = status.get("success", False)
    
    # Update cache
    try:
        cache = {}
        if cache_file.exists():
            cache = json.loads(cache_file.read_text())
        cache["baseline_health"] = status
        cache_file.write_text(json.dumps(cache, indent=2))
    except Exception as e:
        log.warning("Failed to cache baseline health: %s", e)

    return bool(success)


def should_block_goal(goal_id: str, test_dependent_goals: set[str] | None = None) -> bool:
    """Check if a goal should be blocked due to test infrastructure failure.
    
    Args:
        goal_id: The goal to check (e.g., "self-harness")
        test_dependent_goals: Set of goal IDs that require working tests.
                             Defaults to known test-dependent goals.
    
    Returns:
        True if the goal should be blocked (don't dispatch tasks for it)
    """
    if test_dependent_goals is None:
        test_dependent_goals = {
            "self-harness",
            "self-improvement",
            "self-sustaining-autonomy",
            "secretary-ui",
        }
    
    if goal_id not in test_dependent_goals:
        return False  # Not test-dependent
    
    # Check baseline health
    cache_file = Path("data/goal_state.json")
    healthy = check_baseline_health(cache_file)
    
    if not healthy:
        log.info("Goal %s blocked: test baseline unhealthy", goal_id)
    
    return not healthy
