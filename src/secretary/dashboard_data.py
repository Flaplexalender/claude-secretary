"""
Data layer for web dashboard.
Reads heartbeat.json, health_status.json, and run_log.jsonl to populate metrics.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class DashboardData:
    """Aggregates Secretary operational data for dashboard display."""

    def __init__(self, data_dir: Optional[Path] = None):
        """
        Initialize data layer.
        
        Args:
            data_dir: Path to data directory (default: ./data)
        """
        if data_dir is None:
            data_dir = Path(__file__).parent.parent.parent / "data"
        self.data_dir = Path(data_dir)
        self._metrics_cache = None
        self._cache_time = None
        self.cache_ttl_seconds = 5

    def _read_json(self, filename: str) -> Optional[Dict[str, Any]]:
        """Safely read JSON file."""
        try:
            path = self.data_dir / filename
            if not path.exists():
                logger.warning(f"File not found: {path}")
                return None
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading {filename}: {e}")
            return None

    def _is_cache_valid(self) -> bool:
        """Check if cached metrics are still fresh."""
        if self._metrics_cache is None or self._cache_time is None:
            return False
        elapsed = (datetime.now() - self._cache_time).total_seconds()
        return elapsed < self.cache_ttl_seconds

    def get_heartbeat(self) -> Dict[str, Any]:
        """
        Return live uptime, status, and pass rate.
        
        Returns:
            {
                "uptime_seconds": float,
                "uptime_human": str,  # "3h 22m"
                "status": str,  # "running" | "paused" | "stopped"
                "pass_rate": float,  # 0.0 - 1.0
                "pass_rate_percent": str,  # "94.2%"
                "last_updated": str  # ISO timestamp
            }
        """
        heartbeat = self._read_json("heartbeat.json")
        if not heartbeat:
            return {
                "uptime_seconds": 0,
                "uptime_human": "0m",
                "status": "unknown",
                "pass_rate": 0.0,
                "pass_rate_percent": "0%",
                "last_updated": datetime.now().isoformat(),
                "error": "Heartbeat file not found"
            }

        uptime_seconds = heartbeat.get("uptime_seconds", 0)
        status = heartbeat.get("status", "unknown")
        pass_rate = heartbeat.get("pass_rate", 0.0)

        # Format uptime as human-readable string
        hours = int(uptime_seconds // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        if hours > 0:
            uptime_human = f"{hours}h {minutes}m"
        else:
            uptime_human = f"{minutes}m"

        return {
            "uptime_seconds": uptime_seconds,
            "uptime_human": uptime_human,
            "status": status,
            "pass_rate": pass_rate,
            "pass_rate_percent": f"{pass_rate * 100:.1f}%",
            "last_updated": datetime.now().isoformat()
        }

    def get_metrics(self) -> Dict[str, Any]:
        """
        Return cumulative task metrics and cost tracking.
        
        Returns:
            {
                "cycle_count": int,
                "passed": int,
                "failed": int,
                "total": int,
                "pass_rate": float,
                "premium_spent": float,
                "cost_usd": float,
                "last_24h_stats": {...}
            }
        """
        # Use cache if valid
        if self._is_cache_valid():
            return self._metrics_cache

        metrics = self._parse_run_log()
        self._metrics_cache = metrics
        self._cache_time = datetime.now()
        return metrics

    def _parse_run_log(self) -> Dict[str, Any]:
        """Parse run_log.jsonl and aggregate statistics."""
        run_log_path = self.data_dir / "run_log.jsonl"
        if not run_log_path.exists():
            logger.warning(f"run_log.jsonl not found at {run_log_path}")
            return {
                "cycle_count": 0,
                "passed": 0,
                "failed": 0,
                "total": 0,
                "pass_rate": 0.0,
                "premium_spent": 0.0,
                "cost_usd": 0.0,
                "error": "run_log.jsonl not found"
            }

        try:
            passed = 0
            failed = 0
            premium_spent = 0.0
            cost_usd = 0.0
            cycles = set()
            last_24h_start = datetime.now() - timedelta(hours=24)

            # Read file in reverse order (last 100 lines)
            with open(run_log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-100:]  # Last 100 lines

            for line in lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    
                    # Track cycle
                    if "cycle" in entry:
                        cycles.add(entry["cycle"])

                    # Count pass/fail
                    if entry.get("status") == "pass":
                        passed += 1
                    elif entry.get("status") == "fail":
                        failed += 1

                    # Track costs
                    if "cost_total" in entry:
                        cost_usd += entry["cost_total"]
                    if "model_premium" in entry:
                        premium_spent += entry["model_premium"]

                except json.JSONDecodeError:
                    continue

            total = passed + failed
            pass_rate = (passed / total) if total > 0 else 0.0

            return {
                "cycle_count": len(cycles),
                "passed": passed,
                "failed": failed,
                "total": total,
                "pass_rate": pass_rate,
                "pass_rate_percent": f"{pass_rate * 100:.1f}%",
                "premium_spent": premium_spent,
                "cost_usd": f"{cost_usd:.2f}",
                "last_updated": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"Error parsing run_log.jsonl: {e}")
            return {
                "cycle_count": 0,
                "passed": 0,
                "failed": 0,
                "total": 0,
                "pass_rate": 0.0,
                "premium_spent": 0.0,
                "cost_usd": "0.00",
                "error": str(e)
            }

    def get_recent_tasks(self, limit: int = 20, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Return recent tasks with optional filtering.
        
        Args:
            limit: Max number of tasks to return
            status_filter: "pass" or "fail" to filter
            
        Returns:
            List of task dicts with: task, tier, status, duration, error
        """
        run_log_path = self.data_dir / "run_log.jsonl"
        if not run_log_path.exists():
            return []

        tasks = []
        try:
            with open(run_log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit * 2:]  # Read double to allow filtering

            for line in reversed(lines):  # Reverse to get newest first
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    
                    # Apply filter
                    if status_filter and entry.get("status") != status_filter:
                        continue

                    task_data = {
                        "id": entry.get("id", ""),
                        "task": entry.get("task", "Unknown"),
                        "tier": entry.get("tier", "unknown"),
                        "status": entry.get("status", "unknown"),
                        "duration": f"{entry.get('duration_seconds', 0):.1f}s",
                        "error": entry.get("error", ""),
                        "timestamp": entry.get("timestamp", "")
                    }
                    tasks.append(task_data)

                    if len(tasks) >= limit:
                        break

                except json.JSONDecodeError:
                    continue

            return tasks
        except Exception as e:
            logger.error(f"Error reading recent tasks: {e}")
            return []

    def get_task_detail(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Return full details of a specific task."""
        run_log_path = self.data_dir / "run_log.jsonl"
        if not run_log_path.exists():
            return None

        try:
            with open(run_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("id") == task_id:
                            return entry
                    except json.JSONDecodeError:
                        continue
            return None
        except Exception as e:
            logger.error(f"Error reading task detail: {e}")
            return None
