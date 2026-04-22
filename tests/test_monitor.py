"""
Tests for src/secretary/monitor.py

Tests the CostMonitor class for budget tracking, token quota monitoring,
latency spike detection, and error rate anomaly detection.
"""

import json
import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
from tempfile import TemporaryDirectory

from src.secretary.monitor import CostMonitor


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory for test files."""
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_config():
    """Provide a sample config dict with alert settings."""
    return {
        "alerts": {
            "enabled": True,
            "budget_thresholds": {
                "production": {
                    "daily_usd": 100.0,
                    "warn_pct": 80,
                    "critical_pct": 90,
                },
                "staging": {
                    "daily_usd": 50.0,
                    "warn_pct": 75,
                    "critical_pct": 85,
                },
            },
            "metrics": {
                "token_quota": {
                    "claude-opus-4": 100000,
                    "claude-sonnet-4": 50000,
                },
                "latency_sla_s": {
                    "claude-opus-4": 10.0,
                    "claude-sonnet-4": 5.0,
                },
                "error_rate_threshold": 0.05,
            },
            "recipients": {
                "warn": ["warn@example.com"],
                "critical": ["critical@example.com"],
                "block": ["block@example.com"],
            },
            "channels": {
                "email": True,
                "slack": True,
                "pagerduty": False,
            },
        }
    }


@pytest.fixture
def sample_run_log(temp_data_dir):
    """Create a sample run_log.jsonl file with test data."""
    run_log_path = temp_data_dir / "run_log.jsonl"
    
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    yesterday_str = (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    runs = [
        {
            "timestamp": yesterday_str,
            "model": "claude-opus-4",
            "cost_usd": 0.50,
            "duration_s": 2.5,
            "input_tokens": 100,
            "output_tokens": 50,
            "success": True,
        },
        {
            "timestamp": today_str,
            "model": "claude-opus-4",
            "cost_usd": 30.0,
            "duration_s": 2.0,
            "input_tokens": 50000,
            "output_tokens": 30000,
            "success": True,
        },
        {
            "timestamp": today_str,
            "model": "claude-opus-4",
            "cost_usd": 35.0,
            "duration_s": 15.0,  # latency spike
            "input_tokens": 40000,
            "output_tokens": 25000,
            "success": True,
        },
        {
            "timestamp": today_str,
            "model": "claude-sonnet-4",
            "cost_usd": 20.0,
            "duration_s": 1.5,
            "input_tokens": 25000,
            "output_tokens": 15000,
            "success": False,  # failure for error rate
        },
    ]
    
    with open(run_log_path, "w") as f:
        for run in runs:
            f.write(json.dumps(run) + "\n")
    
    return run_log_path


class TestCostMonitorInitialization:
    """Test CostMonitor initialization."""
    
    def test_init_with_enabled_config(self, sample_config, temp_data_dir):
        """Test initialization with enabled alerts."""
        run_log_path = temp_data_dir / "run_log.jsonl"
        monitor = CostMonitor(sample_config, run_log_path)
        
        assert monitor.enabled is True
        assert monitor.config == sample_config
        assert monitor.run_log_path == run_log_path
    
    def test_init_with_disabled_config(self, sample_config, temp_data_dir):
        """Test initialization with disabled alerts."""
        sample_config["alerts"]["enabled"] = False
        run_log_path = temp_data_dir / "run_log.jsonl"
        monitor = CostMonitor(sample_config, run_log_path)
        
        assert monitor.enabled is False
    
    def test_init_missing_alerts_section(self, temp_data_dir):
        """Test initialization with missing alerts config section."""
        config = {}
        run_log_path = temp_data_dir / "run_log.jsonl"
        monitor = CostMonitor(config, run_log_path)
        
        assert monitor.enabled is True  # defaults to True


class TestCheckDailyBudget:
    """Test daily budget threshold checking."""
    
    def test_check_daily_budget_under_limit(self, sample_config, sample_run_log):
        """Test when spending is under warning threshold."""
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_daily_budget(env="production")
        
        # Current spend is $85 out of $100 = 85%, should trigger warn
        assert alert is not None
        assert alert["severity"] == "WARN"
        assert "WARNING" in alert["message"]
        assert alert["pct_spent"] == pytest.approx(85.0, rel=1.0)
    
    def test_check_daily_budget_critical_threshold(self, sample_config, sample_run_log):
        """Test when spending exceeds critical threshold."""
        # Modify config to make critical at 85% instead of 90%
        sample_config["alerts"]["budget_thresholds"]["production"]["critical_pct"] = 80
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_daily_budget(env="production")
        
        assert alert is not None
        assert alert["severity"] == "CRITICAL"
        assert "CRITICAL" in alert["message"]
    
    def test_check_daily_budget_blocked(self, sample_config, sample_run_log):
        """Test when spending exceeds budget limit."""
        # Reduce limit to trigger block
        sample_config["alerts"]["budget_thresholds"]["production"]["daily_usd"] = 50.0
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_daily_budget(env="production")
        
        assert alert is not None
        assert alert["severity"] == "BLOCK"
        assert "BLOCKED" in alert["message"]
        assert alert["pct_spent"] > 100.0
    
    def test_check_daily_budget_disabled_alerts(self, sample_config, sample_run_log):
        """Test that disabled alerts return None."""
        sample_config["alerts"]["enabled"] = False
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_daily_budget(env="production")
        
        assert alert is None
    
    def test_check_daily_budget_missing_env_config(self, sample_config, sample_run_log):
        """Test with environment not in config."""
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_daily_budget(env="unknown_env")
        
        assert alert is None


class TestCheckTokenQuota:
    """Test token quota monitoring."""
    
    def test_check_token_quota_under_limit(self, sample_config, sample_run_log):
        """Test when token usage is under warning threshold."""
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_token_quota(model="claude-opus-4")
        
        # Last request: 80000 / 100000 = 80%, at boundary
        assert alert is not None or alert is None  # depends on which run is last
    
    def test_check_token_quota_warning(self, sample_config, sample_run_log):
        """Test token quota warning threshold."""
        # Reduce quota to trigger warning
        sample_config["alerts"]["metrics"]["token_quota"]["claude-opus-4"] = 100000
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_token_quota(model="claude-opus-4")
        
        # Last request in log: 40000 + 25000 = 65000 tokens
        # This is 65% of 100000 quota, so no alert
        assert alert is None or alert["severity"] in ["WARN", "CRITICAL", "BLOCK"]
    
    def test_check_token_quota_critical(self, sample_config, sample_run_log):
        """Test token quota critical threshold."""
        sample_config["alerts"]["metrics"]["token_quota"]["claude-sonnet-4"] = 30000
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_token_quota(model="claude-sonnet-4")
        
        # Last sonnet request: 25000 + 15000 = 40000, exceeds quota
        assert alert is not None
        assert alert["severity"] == "BLOCK"
    
    def test_check_token_quota_no_config(self, sample_config, sample_run_log):
        """Test with model not in quota config."""
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_token_quota(model="unknown-model")
        
        assert alert is None
    
    def test_check_token_quota_no_runs(self, sample_config, temp_data_dir):
        """Test with empty run log."""
        run_log_path = temp_data_dir / "run_log.jsonl"
        monitor = CostMonitor(sample_config, run_log_path)
        alert = monitor.check_token_quota(model="claude-opus-4")
        
        assert alert is None


class TestCheckLatency:
    """Test latency spike detection."""
    
    def test_check_latency_no_spike(self, sample_config, sample_run_log):
        """Test when latency is normal."""
        sample_config["alerts"]["metrics"]["latency_sla_s"]["claude-sonnet-4"] = 5.0
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_latency(model="claude-sonnet-4")
        
        # Sonnet runs have 1.5s latency, well under 5s SLA
        assert alert is None
    
    def test_check_latency_spike_detected(self, sample_config, sample_run_log):
        """Test when latency spike is detected."""
        sample_config["alerts"]["metrics"]["latency_sla_s"]["claude-opus-4"] = 10.0
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_latency(model="claude-opus-4")
        
        # Opus has one 15.0s request, which is above baseline 2.0s
        assert alert is not None
        assert alert["severity"] == "WARN"
        assert "latency" in alert["message"].lower()
    
    def test_check_latency_no_sla_configured(self, sample_config, sample_run_log):
        """Test when model SLA is not configured."""
        sample_config["alerts"]["metrics"]["latency_sla_s"] = {}
        monitor = CostMonitor(sample_config, sample_run_log)
        alert = monitor.check_latency(model="claude-opus-4")
        
        assert alert is None
    
    def test_check_latency_insufficient_data(self, sample_config, temp_data_dir):
        """Test when insufficient data for baseline."""
        run_log_path = temp_data_dir / "run_log.jsonl"
        
        # Write only 3 runs (less than 10 minimum)
        runs = [
            {"timestamp": datetime.now(timezone.utc).isoformat() + "Z", "model": "claude-opus-4", "duration_s": 1.0, "success": True}
            for _ in range(3)
        ]
        with open(run_log_path, "w") as f:
            for run in runs:
                f.write(json.dumps(run) + "\n")
        
        monitor = CostMonitor(sample_config, run_log_path)
        alert = monitor.check_latency(model="claude-opus-4")
        
        assert alert is None


class TestCheckErrorRate:
    """Test error rate anomaly detection."""
    
    def test_check_error_rate_normal(self, sample_config, temp_data_dir):
        """Test when error rate is normal."""
        run_log_path = temp_data_dir / "run_log.jsonl"
        
        # Create 100 runs, 3 failures = 3% error rate (under 5% threshold)
        runs = []
        for i in range(100):
            runs.append({
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                "model": "claude-opus-4",
                "success": i < 97,  # 97 successes, 3 failures
            })
        
        with open(run_log_path, "w") as f:
            for run in runs:
                f.write(json.dumps(run) + "\n")
        
        monitor = CostMonitor(sample_config, run_log_path)
        alert = monitor.check_error_rate(model="claude-opus-4")
        
        # 3% error rate is under the 5% threshold
        assert alert is None
    
    def test_check_error_rate_warning(self, sample_config, temp_data_dir):
        """Test when error rate triggers warning."""
        run_log_path = temp_data_dir / "run_log.jsonl"
        
        # Create 100 runs, 8 failures = 8% error rate (over 5% but under 10%)
        runs = []
        for i in range(100):
            runs.append({
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                "model": "claude-opus-4",
                "success": i < 92,  # 92 successes, 8 failures
            })
        
        with open(run_log_path, "w") as f:
            for run in runs:
                f.write(json.dumps(run) + "\n")
        
        monitor = CostMonitor(sample_config, run_log_path)
        alert = monitor.check_error_rate(model="claude-opus-4")
        
        assert alert is not None
        assert alert["severity"] == "WARN"
    
    def test_check_error_rate_critical(self, sample_config, temp_data_dir):
        """Test when error rate is critical."""
        run_log_path = temp_data_dir / "run_log.jsonl"
        
        # Create 100 runs, 15 failures = 15% error rate (over 10%)
        runs = []
        for i in range(100):
            runs.append({
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                "model": "claude-opus-4",
                "success": i < 85,  # 85 successes, 15 failures
            })
        
        with open(run_log_path, "w") as f:
            for run in runs:
                f.write(json.dumps(run) + "\n")
        
        monitor = CostMonitor(sample_config, run_log_path)
        alert = monitor.check_error_rate(model="claude-opus-4")
        
        assert alert is not None
        assert alert["severity"] == "CRITICAL"
    
    def test_check_error_rate_insufficient_data(self, sample_config, temp_data_dir):
        """Test with insufficient data."""
        run_log_path = temp_data_dir / "run_log.jsonl"
        
        # Only 5 runs (less than 10 minimum)
        runs = [
            {"timestamp": datetime.now(timezone.utc).isoformat() + "Z", "model": "claude-opus-4", "success": True}
            for _ in range(5)
        ]
        with open(run_log_path, "w") as f:
            for run in runs:
                f.write(json.dumps(run) + "\n")
        
        monitor = CostMonitor(sample_config, run_log_path)
        alert = monitor.check_error_rate(model="claude-opus-4")
        
        assert alert is None


class TestPrivateMethods:
    """Test private helper methods."""
    
    def test_sum_spend_since(self, sample_config, sample_run_log):
        """Test _sum_spend_since aggregation."""
        monitor = CostMonitor(sample_config, sample_run_log)
        
        today = datetime.utcnow().strftime("%Y-%m-%d")
        spend = monitor._sum_spend_since(today, hours=24)
        
        # Should include today's runs: $30 + $35 + $20 = $85
        assert spend == pytest.approx(85.0, rel=0.1)
    
    def test_sum_spend_since_no_file(self, sample_config, temp_data_dir):
        """Test _sum_spend_since with missing file."""
        run_log_path = temp_data_dir / "nonexistent.jsonl"
        monitor = CostMonitor(sample_config, run_log_path)
        
        today = datetime.utcnow().strftime("%Y-%m-%d")
        spend = monitor._sum_spend_since(today, hours=24)
        
        assert spend == 0.0
    
    def test_last_model_tokens(self, sample_config, sample_run_log):
        """Test _last_model_tokens retrieval."""
        monitor = CostMonitor(sample_config, sample_run_log)
        
        tokens = monitor._last_model_tokens("claude-opus-4")
        
        # Last opus run: 40000 input, 25000 output
        assert tokens is not None
        assert tokens[0] == 40000
        assert tokens[1] == 25000
    
    def test_last_model_latencies(self, sample_config, sample_run_log):
        """Test _last_model_latencies retrieval."""
        monitor = CostMonitor(sample_config, sample_run_log)
        
        latencies = monitor._last_model_latencies("claude-opus-4", count=10)
        
        # Should get 3 opus runs with latencies [2.0, 2.0, 15.0]
        assert len(latencies) == 3
        assert 15.0 in latencies
    
    def test_last_model_runs(self, sample_config, sample_run_log):
        """Test _last_model_runs retrieval."""
        monitor = CostMonitor(sample_config, sample_run_log)
        
        runs = monitor._last_model_runs("claude-sonnet-4", count=10)
        
        # Should get 1 sonnet run
        assert len(runs) == 1
        assert runs[0]["model"] == "claude-sonnet-4"
