"""
Tests for web dashboard Flask app and data layer.
"""

import json
import pytest
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

# Import dashboard components
try:
    from secretary.web_dashboard import app, dashboard
    from secretary.dashboard_data import DashboardData
except ImportError:
    # Fallback for import issues
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from secretary.web_dashboard import app, dashboard
    from secretary.dashboard_data import DashboardData


@pytest.fixture
def client():
    """Flask test client."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def temp_data_dir(tmp_path):
    """Create a temporary data directory with sample files."""
    # Create sample heartbeat.json
    heartbeat = {
        "uptime_seconds": 3600,
        "status": "running",
        "pass_rate": 0.95
    }
    with open(tmp_path / "heartbeat.json", "w") as f:
        json.dump(heartbeat, f)

    # Create sample run_log.jsonl
    with open(tmp_path / "run_log.jsonl", "w") as f:
        for i in range(10):
            entry = {
                "id": f"task-{i}",
                "task": f"Sample Task {i}",
                "tier": "basic",
                "status": "pass" if i % 2 == 0 else "fail",
                "duration_seconds": 2.5,
                "cost_total": 0.01,
                "model_premium": 0.0,
                "timestamp": datetime.now().isoformat(),
                "cycle": 1
            }
            f.write(json.dumps(entry) + "\n")

    return tmp_path


class TestDashboardData:
    """Tests for DashboardData layer."""

    def test_init(self, temp_data_dir):
        """Test DashboardData initialization."""
        data = DashboardData(data_dir=temp_data_dir)
        assert data.data_dir == temp_data_dir

    def test_get_heartbeat(self, temp_data_dir):
        """Test get_heartbeat returns correct structure."""
        data = DashboardData(data_dir=temp_data_dir)
        heartbeat = data.get_heartbeat()

        assert "uptime_seconds" in heartbeat
        assert "uptime_human" in heartbeat
        assert "status" in heartbeat
        assert "pass_rate" in heartbeat
        assert "pass_rate_percent" in heartbeat
        assert heartbeat["status"] == "running"
        assert heartbeat["pass_rate"] == 0.95

    def test_get_heartbeat_uptime_formatting(self, temp_data_dir):
        """Test uptime formatting."""
        data = DashboardData(data_dir=temp_data_dir)
        heartbeat = data.get_heartbeat()
        # 3600 seconds = 1h 0m
        assert heartbeat["uptime_human"] == "1h 0m"

    def test_get_metrics(self, temp_data_dir):
        """Test get_metrics returns correct structure and values."""
        data = DashboardData(data_dir=temp_data_dir)
        metrics = data.get_metrics()

        assert "cycle_count" in metrics
        assert "passed" in metrics
        assert "failed" in metrics
        assert "total" in metrics
        assert "pass_rate" in metrics
        assert "cost_usd" in metrics
        
        # With 10 tasks (5 pass, 5 fail)
        assert metrics["total"] == 10
        assert metrics["passed"] == 5
        assert metrics["failed"] == 5

    def test_get_metrics_caching(self, temp_data_dir):
        """Test that metrics are cached."""
        data = DashboardData(data_dir=temp_data_dir)
        
        # First call
        metrics1 = data.get_metrics()
        
        # Second call within TTL should return cached value
        metrics2 = data.get_metrics()
        
        assert metrics1 == metrics2

    def test_get_recent_tasks(self, temp_data_dir):
        """Test get_recent_tasks returns recent tasks."""
        data = DashboardData(data_dir=temp_data_dir)
        tasks = data.get_recent_tasks(limit=5)

        assert len(tasks) <= 5
        assert all("id" in t for t in tasks)
        assert all("task" in t for t in tasks)
        assert all("status" in t for t in tasks)

    def test_get_recent_tasks_filter(self, temp_data_dir):
        """Test get_recent_tasks with status filter."""
        data = DashboardData(data_dir=temp_data_dir)
        
        passed_tasks = data.get_recent_tasks(limit=20, status_filter="pass")
        assert all(t["status"] == "pass" for t in passed_tasks)

        failed_tasks = data.get_recent_tasks(limit=20, status_filter="fail")
        assert all(t["status"] == "fail" for t in failed_tasks)

    def test_get_task_detail(self, temp_data_dir):
        """Test get_task_detail returns full task info."""
        data = DashboardData(data_dir=temp_data_dir)
        
        # Get a task ID from recent tasks
        tasks = data.get_recent_tasks(limit=1)
        if tasks:
            task_id = tasks[0]["id"]
            detail = data.get_task_detail(task_id)
            
            assert detail is not None
            assert detail["id"] == task_id

    def test_missing_files(self, tmp_path):
        """Test behavior when data files are missing."""
        data = DashboardData(data_dir=tmp_path)
        
        heartbeat = data.get_heartbeat()
        assert heartbeat["status"] == "unknown"
        
        metrics = data.get_metrics()
        assert metrics["total"] == 0


class TestWebDashboardFlask:
    """Tests for Flask web dashboard endpoints."""

    def test_health_endpoint(self, client):
        """Test /health endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"
        assert "timestamp" in data

    def test_index_endpoint(self, client):
        """Test / endpoint returns HTML."""
        response = client.get("/")
        assert response.status_code == 200
        assert b"Claude Secretary Dashboard" in response.data

    def test_api_heartbeat_endpoint(self, client, temp_data_dir):
        """Test /api/heartbeat endpoint."""
        # Mock the dashboard data layer
        with patch("secretary.web_dashboard.dashboard.get_heartbeat") as mock_get:
            mock_get.return_value = {
                "uptime_seconds": 3600,
                "uptime_human": "1h 0m",
                "status": "running",
                "pass_rate": 0.95,
                "pass_rate_percent": "95.0%"
            }
            
            response = client.get("/api/heartbeat")
            assert response.status_code == 200
            data = response.get_json()
            assert data["status"] == "running"

    def test_api_metrics_endpoint(self, client):
        """Test /api/metrics endpoint."""
        with patch("secretary.web_dashboard.dashboard.get_metrics") as mock_get:
            mock_get.return_value = {
                "cycle_count": 5,
                "passed": 40,
                "failed": 10,
                "total": 50,
                "pass_rate": 0.8,
                "cost_usd": "12.50"
            }
            
            response = client.get("/api/metrics")
            assert response.status_code == 200
            data = response.get_json()
            assert data["total"] == 50

    def test_api_recent_tasks_endpoint(self, client):
        """Test /api/recent-tasks endpoint."""
        with patch("secretary.web_dashboard.dashboard.get_recent_tasks") as mock_get:
            mock_get.return_value = [
                {
                    "id": "task-1",
                    "task": "Sample Task",
                    "status": "pass",
                    "duration": "2.5s"
                }
            ]
            
            response = client.get("/api/recent-tasks")
            assert response.status_code == 200
            data = response.get_json()
            assert len(data) > 0

    def test_api_task_detail_endpoint(self, client):
        """Test /api/task/<task_id> endpoint."""
        with patch("secretary.web_dashboard.dashboard.get_task_detail") as mock_get:
            mock_get.return_value = {
                "id": "task-1",
                "task": "Sample Task",
                "status": "pass"
            }
            
            response = client.get("/api/task/task-1")
            assert response.status_code == 200
            data = response.get_json()
            assert data["id"] == "task-1"

    def test_api_task_detail_not_found(self, client):
        """Test /api/task/<task_id> returns 404 when not found."""
        with patch("secretary.web_dashboard.dashboard.get_task_detail") as mock_get:
            mock_get.return_value = None
            
            response = client.get("/api/task/nonexistent")
            assert response.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
