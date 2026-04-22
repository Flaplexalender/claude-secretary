"""Test infrastructure harness for goal-based self-improvement.

Bridges goal definitions to test generation and execution.
Validates that the test baseline is healthy before dispatching tasks.
"""
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def validate_baseline_health(data_root: Path) -> dict:
    """Check if test baseline is passing.
    
    Returns:
        {
            "healthy": bool,
            "total_tests": int,
            "failures": int,
            "message": str
        }
    """
    goal_state_path = data_root / "goal_state.json"
    if not goal_state_path.exists():
        return {
            "healthy": False,
            "total_tests": 0,
            "failures": 0,
            "message": "goal_state.json not found"
        }
    
    try:
        gs = json.loads(goal_state_path.read_text())
        baseline = gs.get("test_baseline", {})
        
        total = baseline.get("total_tests", 0)
        failures = baseline.get("failures", 0)
        healthy = failures == 0 and total > 0
        
        return {
            "healthy": healthy,
            "total_tests": total,
            "failures": failures,
            "message": "baseline healthy" if healthy else f"{failures} test failures"
        }
    except Exception as e:
        return {
            "healthy": False,
            "total_tests": 0,
            "failures": 0,
            "message": f"Error reading baseline: {e}"
        }


def should_block_goal(data_root: Path, goal_id: Optional[str] = None) -> tuple[bool, str]:
    """Determine if a goal should be blocked due to infrastructure issues.
    
    Args:
        data_root: Path to data directory
        goal_id: Optional goal ID to check (self-harness, self-improvement, etc.)
    
    Returns:
        (should_block, reason)
    """
    baseline = validate_baseline_health(data_root)
    
    if not baseline["healthy"]:
        reason = f"Test baseline unhealthy: {baseline['message']}"
        
        # Goals that depend on test infrastructure
        blocked_goals = {"self-harness", "self-improvement", "self-sustaining-autonomy", "secretary-ui"}
        
        if goal_id is None or goal_id in blocked_goals:
            return True, reason
    
    return False, ""
