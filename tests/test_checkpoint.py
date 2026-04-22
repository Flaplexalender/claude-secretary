"""Tests for src/secretary/checkpoint.py - Task checkpointing for resumable agent loops."""
import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from src.secretary.checkpoint import TaskCheckpoint, CheckpointManager


@pytest.fixture
def temp_checkpoint_dir():
    """Create temporary checkpoint directory."""
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def checkpoint_manager(temp_checkpoint_dir):
    """Create CheckpointManager instance."""
    return CheckpointManager(temp_checkpoint_dir)


class TestTaskCheckpoint:
    """Test TaskCheckpoint dataclass."""
    
    def test_checkpoint_creation(self):
        """Test creating a TaskCheckpoint."""
        messages = [{"role": "user", "content": "test"}]
        checkpoint = TaskCheckpoint(
            task_id="task_1",
            turn=1,
            messages=messages,
            tool_calls=["gmail_search"],
            status="in_progress"
        )
        
        assert checkpoint.task_id == "task_1"
        assert checkpoint.turn == 1
        assert checkpoint.messages == messages
        assert checkpoint.status == "in_progress"
    
    def test_checkpoint_defaults(self):
        """Test checkpoint with default values."""
        checkpoint = TaskCheckpoint(
            task_id="task_1",
            turn=0,
            messages=[],
        )
        
        assert checkpoint.tool_calls == []
        assert checkpoint.status == "pending"
        assert isinstance(checkpoint.timestamp, str)
    
    def test_checkpoint_serialization(self):
        """Test checkpoint can be serialized to dict."""
        checkpoint = TaskCheckpoint(
            task_id="task_1",
            turn=2,
            messages=[{"role": "assistant", "content": "response"}],
            tool_calls=["file_read", "grep_search"],
            status="completed"
        )
        
        # Dataclass should be JSON serializable via asdict pattern
        assert checkpoint.task_id == "task_1"
        assert len(checkpoint.tool_calls) == 2


class TestCheckpointManagerSave:
    """Test CheckpointManager.save() method."""
    
    def test_save_creates_checkpoint_file(self, checkpoint_manager, temp_checkpoint_dir):
        """Test that save() creates checkpoint file."""
        messages = [{"role": "user", "content": "test"}]
        
        checkpoint_manager.save(
            task_id="task_1",
            turn=1,
            messages=messages,
            tool_calls=["gmail_search"]
        )
        
        # File should exist
        assert (temp_checkpoint_dir / "task_1_turn_001.json").exists()
    
    def test_save_creates_directory_structure(self, checkpoint_manager, temp_checkpoint_dir):
        """Test that save() creates necessary directories."""
        messages = [{"role": "user", "content": "data"}]
        
        checkpoint_manager.save(
            task_id="complex_task_name",
            turn=5,
            messages=messages
        )
        
        # Should create file with zero-padded turn number
        files = list(temp_checkpoint_dir.glob("complex_task_name_turn_*.json"))
        assert len(files) > 0
    
    def test_save_stores_complete_data(self, checkpoint_manager, temp_checkpoint_dir):
        """Test that all checkpoint data is stored."""
        messages = [{"role": "user", "content": "hello"}]
        tool_calls = ["file_read", "run_command"]
        
        checkpoint_manager.save(
            task_id="task_1",
            turn=1,
            messages=messages,
            tool_calls=tool_calls,
            status="in_progress"
        )
        
        # Read back and verify
        file = temp_checkpoint_dir / "task_1_turn_001.json"
        data = json.loads(file.read_text())
        
        assert data["task_id"] == "task_1"
        assert data["turn"] == 1
        assert data["messages"] == messages
        assert data["tool_calls"] == tool_calls
        assert data["status"] == "in_progress"
    
    def test_save_multiple_turns(self, checkpoint_manager, temp_checkpoint_dir):
        """Test saving multiple turns for same task."""
        for turn in range(1, 4):
            checkpoint_manager.save(
                task_id="task_1",
                turn=turn,
                messages=[{"role": "user", "content": f"turn {turn}"}]
            )
        
        files = sorted(temp_checkpoint_dir.glob("task_1_turn_*.json"))
        assert len(files) == 3
        assert files[0].name == "task_1_turn_001.json"
        assert files[2].name == "task_1_turn_003.json"


class TestCheckpointManagerLoad:
    """Test CheckpointManager.load() method."""
    
    def test_load_existing_checkpoint(self, checkpoint_manager, temp_checkpoint_dir):
        """Test loading an existing checkpoint."""
        checkpoint_manager.save(
            task_id="task_1",
            turn=1,
            messages=[{"role": "user", "content": "test"}],
            tool_calls=["gmail_search"]
        )
        
        loaded = checkpoint_manager.load(task_id="task_1", turn=1)
        
        assert loaded is not None
        assert loaded.task_id == "task_1"
        assert loaded.turn == 1
        assert loaded.messages == [{"role": "user", "content": "test"}]
        assert loaded.tool_calls == ["gmail_search"]
    
    def test_load_nonexistent_checkpoint(self, checkpoint_manager):
        """Test loading a checkpoint that doesn't exist."""
        loaded = checkpoint_manager.load(task_id="nonexistent", turn=1)
        
        assert loaded is None
    
    def test_load_wrong_turn(self, checkpoint_manager):
        """Test loading a turn that doesn't exist for task."""
        checkpoint_manager.save(
            task_id="task_1",
            turn=1,
            messages=[]
        )
        
        loaded = checkpoint_manager.load(task_id="task_1", turn=999)
        
        assert loaded is None
    
    def test_load_returns_taskcheckpoint_object(self, checkpoint_manager):
        """Test that load returns TaskCheckpoint object."""
        checkpoint_manager.save(
            task_id="task_1",
            turn=2,
            messages=[],
            status="completed"
        )
        
        loaded = checkpoint_manager.load(task_id="task_1", turn=2)
        
        assert isinstance(loaded, TaskCheckpoint)
        assert loaded.status == "completed"


class TestCheckpointManagerExists:
    """Test CheckpointManager.exists() method."""
    
    def test_exists_returns_true_for_saved_checkpoint(self, checkpoint_manager):
        """Test exists() returns True for saved checkpoint."""
        checkpoint_manager.save(
            task_id="task_1",
            turn=1,
            messages=[]
        )
        
        assert checkpoint_manager.exists(task_id="task_1", turn=1) is True
    
    def test_exists_returns_false_for_missing_checkpoint(self, checkpoint_manager):
        """Test exists() returns False for missing checkpoint."""
        assert checkpoint_manager.exists(task_id="task_1", turn=1) is False
    
    def test_exists_differentiates_turns(self, checkpoint_manager):
        """Test exists() differentiates between turns."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        
        assert checkpoint_manager.exists(task_id="task_1", turn=1) is True
        assert checkpoint_manager.exists(task_id="task_1", turn=2) is False


class TestCheckpointManagerListCheckpoints:
    """Test CheckpointManager.list_checkpoints() method."""
    
    def test_list_checkpoints_returns_empty_list(self, checkpoint_manager):
        """Test listing checkpoints with no saved data."""
        checkpoints = checkpoint_manager.list_checkpoints()
        
        assert checkpoints == []
    
    def test_list_checkpoints_returns_all_tasks(self, checkpoint_manager):
        """Test listing all checkpoints."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        checkpoint_manager.save(task_id="task_2", turn=1, messages=[])
        checkpoint_manager.save(task_id="task_1", turn=2, messages=[])
        
        checkpoints = checkpoint_manager.list_checkpoints()
        
        assert len(checkpoints) == 3
    
    def test_list_checkpoints_includes_metadata(self, checkpoint_manager):
        """Test that listed checkpoints include metadata."""
        checkpoint_manager.save(
            task_id="task_1",
            turn=1,
            messages=[{"role": "user", "content": "test"}],
            status="completed"
        )
        
        checkpoints = checkpoint_manager.list_checkpoints()
        
        assert len(checkpoints) == 1
        assert checkpoints[0]["task_id"] == "task_1"
        assert checkpoints[0]["turn"] == 1
        assert checkpoints[0]["status"] == "completed"
    
    def test_list_checkpoints_sorted_by_task_and_turn(self, checkpoint_manager):
        """Test that checkpoints are sorted properly."""
        checkpoint_manager.save(task_id="task_2", turn=1, messages=[])
        checkpoint_manager.save(task_id="task_1", turn=2, messages=[])
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        
        checkpoints = checkpoint_manager.list_checkpoints()
        
        # Should be sorted by task_id, then turn
        assert checkpoints[0]["task_id"] == "task_1"
        assert checkpoints[0]["turn"] == 1
        assert checkpoints[1]["task_id"] == "task_1"
        assert checkpoints[1]["turn"] == 2


class TestCheckpointManagerDeleteCheckpoint:
    """Test CheckpointManager.delete_checkpoint() method."""
    
    def test_delete_checkpoint_removes_file(self, checkpoint_manager, temp_checkpoint_dir):
        """Test that delete removes checkpoint file."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        
        assert checkpoint_manager.exists(task_id="task_1", turn=1)
        
        checkpoint_manager.delete_checkpoint(task_id="task_1", turn=1)
        
        assert not checkpoint_manager.exists(task_id="task_1", turn=1)
    
    def test_delete_checkpoint_returns_true_on_success(self, checkpoint_manager):
        """Test delete returns True on success."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        
        result = checkpoint_manager.delete_checkpoint(task_id="task_1", turn=1)
        
        assert result is True
    
    def test_delete_checkpoint_returns_false_if_not_exists(self, checkpoint_manager):
        """Test delete returns False if checkpoint doesn't exist."""
        result = checkpoint_manager.delete_checkpoint(task_id="task_1", turn=1)
        
        assert result is False
    
    def test_delete_checkpoint_only_deletes_target(self, checkpoint_manager):
        """Test that delete only removes target checkpoint."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        checkpoint_manager.save(task_id="task_1", turn=2, messages=[])
        
        checkpoint_manager.delete_checkpoint(task_id="task_1", turn=1)
        
        assert not checkpoint_manager.exists(task_id="task_1", turn=1)
        assert checkpoint_manager.exists(task_id="task_1", turn=2)


class TestCheckpointManagerCleanuUp:
    """Test CheckpointManager.cleanup() method."""
    
    def test_cleanup_old_checkpoints(self, checkpoint_manager):
        """Test cleanup removes old checkpoints."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        checkpoint_manager.save(task_id="task_1", turn=2, messages=[])
        checkpoint_manager.save(task_id="task_1", turn=3, messages=[])
        
        # Keep only last 2 turns
        checkpoint_manager.cleanup(task_id="task_1", keep_last_n=2)
        
        assert not checkpoint_manager.exists(task_id="task_1", turn=1)
        assert checkpoint_manager.exists(task_id="task_1", turn=2)
        assert checkpoint_manager.exists(task_id="task_1", turn=3)
    
    def test_cleanup_all_checkpoints_for_task(self, checkpoint_manager):
        """Test cleanup can remove all checkpoints for a task."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        checkpoint_manager.save(task_id="task_1", turn=2, messages=[])
        
        checkpoint_manager.cleanup(task_id="task_1", keep_last_n=0)
        
        assert not checkpoint_manager.exists(task_id="task_1", turn=1)
        assert not checkpoint_manager.exists(task_id="task_1", turn=2)
    
    def test_cleanup_doesnt_affect_other_tasks(self, checkpoint_manager):
        """Test cleanup only affects specified task."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[])
        checkpoint_manager.save(task_id="task_2", turn=1, messages=[])
        
        checkpoint_manager.cleanup(task_id="task_1", keep_last_n=0)
        
        assert not checkpoint_manager.exists(task_id="task_1", turn=1)
        assert checkpoint_manager.exists(task_id="task_2", turn=1)


class TestCheckpointIntegration:
    """Integration tests for checkpoint save/load cycle."""
    
    def test_save_and_load_cycle(self, checkpoint_manager):
        """Test complete save/load cycle preserves data."""
        original_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"}
        ]
        original_tools = ["gmail_search", "file_read"]
        
        checkpoint_manager.save(
            task_id="task_1",
            turn=5,
            messages=original_messages,
            tool_calls=original_tools,
            status="in_progress"
        )
        
        loaded = checkpoint_manager.load(task_id="task_1", turn=5)
        
        assert loaded.messages == original_messages
        assert loaded.tool_calls == original_tools
        assert loaded.status == "in_progress"
    
    def test_multiple_tasks_independence(self, checkpoint_manager):
        """Test that different tasks don't interfere."""
        checkpoint_manager.save(task_id="task_1", turn=1, messages=[{"role": "user", "content": "task1"}])
        checkpoint_manager.save(task_id="task_2", turn=1, messages=[{"role": "user", "content": "task2"}])
        
        loaded1 = checkpoint_manager.load(task_id="task_1", turn=1)
        loaded2 = checkpoint_manager.load(task_id="task_2", turn=1)
        
        assert loaded1.messages[0]["content"] == "task1"
        assert loaded2.messages[0]["content"] == "task2"
