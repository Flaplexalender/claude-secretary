"""Task checkpointing for timeout recovery.

Enables long-running agent tasks to save state every 60s and resume from
checkpoint if interrupted by timeout.
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class TaskCheckpoint:
    """Snapshot of task state for resumption."""
    task_id: str
    timestamp: str
    turn: int
    messages: list[dict]  # full message history
    tool_results: list[dict]  # recent tool results
    context: dict  # task-specific context
    status: str  # "in_progress", "interrupted", "completed"


class CheckpointManager:
    """Manages task checkpointing and resumption."""
    
    def __init__(self, checkpoint_dir: Path = Path("data/.checkpoints")):
        self.checkpoint_dir = checkpoint_dir
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self.last_checkpoint_time = 0
        self.checkpoint_interval = 60  # seconds
    
    def should_checkpoint(self) -> bool:
        """Check if interval since last checkpoint has elapsed."""
        now = time.time()
        if now - self.last_checkpoint_time >= self.checkpoint_interval:
            self.last_checkpoint_time = now
            return True
        return False
    
    def save_checkpoint(self, checkpoint: TaskCheckpoint) -> Path:
        """Save checkpoint to disk."""
        path = self.checkpoint_dir / f"{checkpoint.task_id}.json"
        path.write_text(json.dumps(asdict(checkpoint), indent=2, default=str))
        return path
    
    def load_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        """Load checkpoint from disk, returns None if not found."""
        path = self.checkpoint_dir / f"{task_id}.json"
        if path.exists():
            data = json.loads(path.read_text())
            return TaskCheckpoint(**data)
        return None
    
    def resume_from_checkpoint(self, checkpoint: TaskCheckpoint) -> dict:
        """Convert checkpoint into resumption context for agent."""
        return {
            "resumed": True,
            "from_checkpoint": checkpoint.timestamp,
            "from_turn": checkpoint.turn,
            "previous_tool_results": checkpoint.tool_results[-3:],  # last 3 results
            "context": checkpoint.context,
        }
    
    def clear_checkpoint(self, task_id: str):
        """Remove checkpoint file after successful task completion."""
        path = self.checkpoint_dir / f"{task_id}.json"
        if path.exists():
            path.unlink()


def with_checkpoint(manager: CheckpointManager):
    """Decorator to auto-checkpoint long-running tasks."""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            task_id = kwargs.get('task_id', 'unknown')
            
            # Check for existing checkpoint
            existing = manager.load_checkpoint(task_id)
            if existing and existing.status == "interrupted":
                print(f"Resuming {task_id} from checkpoint at turn {existing.turn}")
                resumption = manager.resume_from_checkpoint(existing)
                kwargs['resumed_from'] = resumption
            
            try:
                result = await func(*args, **kwargs)
                # Clear checkpoint on success
                manager.clear_checkpoint(task_id)
                return result
            except TimeoutError:
                # Save checkpoint before re-raising
                checkpoint = TaskCheckpoint(
                    task_id=task_id,
                    timestamp=datetime.now().isoformat(),
                    turn=kwargs.get('turn', 0),
                    messages=kwargs.get('messages', []),
                    tool_results=kwargs.get('recent_results', []),
                    context=kwargs.get('context', {}),
                    status="interrupted"
                )
                manager.save_checkpoint(checkpoint)
                print(f"Checkpoint saved for {task_id}. Run again to resume.")
                raise
        return wrapper
    return decorator
