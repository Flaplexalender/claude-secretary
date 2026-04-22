"""Goal planning and dispatch — skips tasks if baseline is unhealthy."""
import json
import logging
from pathlib import Path
from typing import Optional

from .goal_harness import should_block_goal

log = logging.getLogger(__name__)


def can_dispatch_task(data_root: Path, goal_id: str) -> tuple[bool, Optional[str]]:
    """Check if a task can be dispatched.
    
    Returns:
        (can_dispatch, reason_if_blocked)
    """
    blocked, reason = should_block_goal(data_root, goal_id)
    
    if blocked:
        log.warning(f"Goal {goal_id} blocked: {reason}")
        return False, reason
    
    return True, None


def get_next_sub_goal(data_root: Path, goal_id: str) -> Optional[str]:
    """Get the next sub-goal to work on for a given goal.
    
    Returns the first incomplete sub-goal, or None if all are done/blocked.
    """
    goal_state_path = data_root / "goal_state.json"
    if not goal_state_path.exists():
        return None
    
    try:
        gs = json.loads(goal_state_path.read_text())
        sub_goal_status = gs.get("sub_goal_status", {})
        
        for sub_goal_id, state in sub_goal_status.items():
            status = state.get("status")
            # Return first incomplete sub-goal
            if status in ["pending", "in-progress"]:
                return sub_goal_id
        
        return None
    except Exception as e:
        log.error(f"Error reading goal state: {e}")
        return None
