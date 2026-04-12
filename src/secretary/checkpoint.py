"""Checkpoint/resume mechanism for long-running agent tasks.

Saves agent state (messages, tools used, progress) every 60s to allow
resumption from the last checkpoint if a task times out.
"""
import json
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any


@dataclass
class TaskCheckpoint:
    """A savepoint for an in-progress task."""
    task_id: str
    turn: int
    messages: list[dict[str, Any]]
    tools_used: list[str]
    text_output: str
    timestamp: float
    created_at: str


class CheckpointManager:
    """Save and restore agent checkpoints."""
    
    def __init__(self, checkpoint_dir: Path):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    
    def save(self, task_id: str, turn: int, messages: list[dict[str, Any]], 
             tools_used: list[str], text: str) -> str:
        """Save a checkpoint. Returns checkpoint path."""
        checkpoint = TaskCheckpoint(
            task_id=task_id,
            turn=turn,
            messages=messages,
            tools_used=tools_used,
            text_output=text,
            timestamp=time.time(),
            created_at=datetime.now().isoformat()
        )
        path = self.checkpoint_dir / f"{task_id}_turn{turn}.json"
        with open(path, 'w') as f:
            json.dump(asdict(checkpoint), f)
        return str(path)
    
    def load(self, task_id: str, turn: int) -> TaskCheckpoint | None:
        """Load a checkpoint by task_id and turn number."""
        path = self.checkpoint_dir / f"{task_id}_turn{turn}.json"
        if not path.exists():
            return None
        with open(path) as f:
            data = json.load(f)
        return TaskCheckpoint(**data)
    
    def latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        """Find the most recent checkpoint for a task."""
        matching = list(self.checkpoint_dir.glob(f"{task_id}_turn*.json"))
        if not matching:
            return None
        latest = max(matching, key=lambda p: p.stat().st_mtime)
        with open(latest) as f:
            data = json.load(f)
        return TaskCheckpoint(**data)
    
    def cleanup_task(self, task_id: str):
        """Delete all checkpoints for a completed task."""
        for path in self.checkpoint_dir.glob(f"{task_id}_turn*.json"):
            path.unlink()
