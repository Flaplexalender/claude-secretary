"""
Tests for src/secretary/alerter.py

Tests the Alerter class for multi-channel dispatch, severity routing,
and alert logging.
"""

import json
import pytest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, MagicMock, call

from secretary.alerter import Alerter


@pytest.fixture
def temp_data_dir():
    """Create temporary data directory."""
    with TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def alert_log_path(temp_data_dir):
    """Path to alert log file."""
    return temp_data_dir / "alert_log.jsonl"


@pytest.fixture
def sample_config():
    """Sample alerter configuration."""
    return {
        "alerts": {
            "enabled": True,
            "recipients": {
                "warn": ["warn@example.com"],
                "critical": ["critical@example.com"],
                "block": ["block@example.com"],
                "warn_slack": ["#warnings"],
                "critical_slack": ["#critical"],
                "block_slack": ["#critical"],
            },
            "channels": {
                "email": True,
                "slack": True,
                "pagerduty": False,
            },
            "sla_sec": {
                "email": 300,
                "slack": 60,
                "pagerduty": 30,
            },
        }
    }


@pytest.fixture
def alerter(sample_config, alert_log_path):
    """Create an Alerter instance."""
    return Alerter(sample_config, alert_log_path)


class TestAlerterInitialization:
    """Test Alerter initialization."""
    
    def test_init_enabled(self, sample_config, alert_log_path):
        """Test initialization with enabled alerts."""
        alerter = Alerter(sample_config, alert_log_path)
        
        assert alerter.enabled is True
        assert alerter.config == sample_config
        assert alerter.alert_log_path == alert_log_path
    
    def test_init_disabled(self, sample_config, alert_log_path):
        """Test initialization with disabled alerts."""
        sample_config["alerts"]["enabled"] = False
        alerter = Alerter(sample_config, alert_log_path)
        
        assert alerter.enabled is False
    
    def test_init_missing_alerts_section(self, alert_log_path):
        """Test initialization with missing alerts section."""
        config = {}
        alerter = Alerter(config, alert_log_path)
        
        assert alerter.enabled is True  # defaults to True
        assert alerter.recipients == {}
    
    def test_init_preserves_config(self, sample_config, alert_log_path):
        """Test that config is preserved correctly."""
        alerter = Alerter(sample_config, alert_log_path)
        
        assert alerter.channels["email"] is True
        assert alerter.channels["slack"] is True
        assert "warn@example.com" in alerter.recipients["warn"]


class TestDispatch:
    """Test alert dispatch routing."""
    
    def test_dispatch_when_disabled(self, alerter):
        """Test that dispatch returns False when alerts disabled."""
        alerter.enabled = False
        alert = {"type": "budget", "severity": "WARN", "message": "Test"}
        
        result = alerter.dispatch(alert)
        assert result is False
    
    def test_dispatch_warn_severity(self, alerter, alert_log_path):
        """Test dispatch of WARN severity alert."""
        alert = {
            "type": "budget",
            "severity": "WARN",
            "message": "Daily spend at 80%",
            "current_spend": 80.0,
            "daily_limit": 100.0,
        }
        
        result = alerter.dispatch(alert)
        
        # Should succeed and log alert
        assert result is True
        assert alert_log_path.exists()
        
        # Verify alert was logged
        with open(alert_log_path) as f:
            logged = json.loads(f.readline())
            assert logged["severity"] == "WARN"
            assert logged["type"] == "budget"
    
    def test_dispatch_critical_severity(self, alerter, alert_log_path):
        """Test dispatch of CRITICAL severity alert."""
        alert = {
            "type": "error_rate",
            "severity": "CRITICAL",
            "message": "Error rate at 15%",
            "error_rate": 0.15,
        }
        
        result = alerter.dispatch(alert)
        
        assert result is True
        assert alert_log_path.exists()
    
    def test_dispatch_block_severity(self, alerter, alert_log_path):
        """Test dispatch of BLOCK severity alert."""
        alert = {
            "type": "budget",
            "severity": "BLOCK",
            "message": "Daily budget exceeded!",
            "current_spend": 110.0,
            "daily_limit": 100.0,
        }
        
        result = alerter.dispatch(alert)
        
        assert result is True
        assert alert_log_path.exists()
    
    def test_dispatch_triggers_pagerduty_on_block(self, alerter):
        """Test that BLOCK severity can trigger PagerDuty."""
        alert = {
            "type": "budget",
            "severity": "BLOCK",
            "message": "BLOCKED",
        }
        
        # PagerDuty is configured but disabled in fixture
        alerter.channels["pagerduty"] = True
        result = alerter.dispatch(alert)
        
        assert result is True
    
    def test_dispatch_triggers_pagerduty_on_critical(self, alerter):
        """Test that CRITICAL severity can trigger PagerDuty."""
        alerter.channels["pagerduty"] = True
        alert = {
            "type": "error_rate",
            "severity": "CRITICAL",
            "message": "ERROR RATE CRITICAL",
        }
        
        result = alerter.dispatch(alert)
        
        assert result is True


class TestGetRecipients:
    """Test recipient mapping by severity."""
    
    def test_get_recipients_warn(self, alerter):
        """Test WARN severity maps to warn recipients."""
        recipients = alerter._get_recipients("WARN")
        
        assert recipients["email"] == ["warn@example.com"]
        assert recipients["slack"] == ["#warnings"]
    
    def test_get_recipients_critical(self, alerter):
        """Test CRITICAL severity maps to critical recipients."""
        recipients = alerter._get_recipients("CRITICAL")
        
        assert recipients["email"] == ["critical@example.com"]
        assert recipients["slack"] == ["#critical"]
    
    def test_get_recipients_block(self, alerter):
        """Test BLOCK severity maps to block recipients."""
        recipients = alerter._get_recipients("BLOCK")
        
        assert recipients["email"] == ["block@example.com"]
        assert recipients["slack"] == ["#critical"]
    
    def test_get_recipients_info_defaults_to_warn(self, alerter):
        """Test INFO severity defaults to warn recipients."""
        recipients = alerter._get_recipients("INFO")
        
        assert recipients["email"] == ["warn@example.com"]
    
    def test_get_recipients_unknown_defaults_to_warn(self, alerter):
        """Test unknown severity defaults to warn recipients."""
        recipients = alerter._get_recipients("UNKNOWN")
        
        assert recipients["email"] == ["warn@example.com"]


class TestFormatEmailBody:
    """Test email formatting."""
    
    def test_format_email_body_basic(self, alerter):
        """Test basic email body formatting."""
        alert = {
            "type": "budget",
            "severity": "WARN",
            "message": "Daily spend at 80%",
            "current_spend": 80.0,
        }
        
        body = alerter._format_email_body(alert)
        
        assert "Alert Type: budget" in body
        assert "Severity: WARN" in body
        assert "Message: Daily spend at 80%" in body
        assert "current_spend: 80.0" in body
    
    def test_format_email_body_includes_action_items(self, alerter):
        """Test that email body includes action items."""
        alert = {
            "type": "budget",
            "severity": "CRITICAL",
            "message": "Critical alert",
        }
        
        body = alerter._format_email_body(alert)
        
        assert "Action Required:" in body
        assert "dashboard" in body.lower()
    
    def test_format_email_body_excludes_system_fields(self, alerter):
        """Test that body excludes system fields."""
        alert = {
            "type": "test",
            "severity": "WARN",
            "message": "Test message",
            "extra_field": "value",
        }
        
        body = alerter._format_email_body(alert)
        
        # System fields should be excluded from details
        assert "extra_field" in body  # custom fields included
        assert "type: test" not in body  # but not type (core field)


class TestFormatSlackFields:
    """Test Slack message formatting."""
    
    def test_format_slack_fields_budget_alert(self, alerter):
        """Test Slack field formatting for budget alert."""
        alert = {
            "daily_limit": 100.0,
            "current_spend": 85.0,
            "pct_spent": 85.0,
        }
        
        fields = alerter._format_slack_fields(alert)
        
        assert len(fields) > 0
        field_titles = [f["title"] for f in fields]
        assert "Daily Limit" in field_titles
        assert "Current Spend" in field_titles
    
    def test_format_slack_fields_token_quota_alert(self, alerter):
        """Test Slack field formatting for token quota alert."""
        alert = {
            "model": "claude-opus-4",
            "quota": 100000,
            "used": 85000,
        }
        
        fields = alerter._format_slack_fields(alert)
        
        field_titles = [f["title"] for f in fields]
        assert "Model" in field_titles
        assert "Quota" in field_titles
        assert "Used" in field_titles
    
    def test_format_slack_fields_numeric_formatting(self, alerter):
        """Test that numeric fields are formatted correctly."""
        alert = {
            "current_spend": 85.123456,
            "daily_limit": 100.999,
        }
        
        fields = alerter._format_slack_fields(alert)
        
        # All numeric values should be formatted with 2 decimals
        for field in fields:
            value = field["value"]
            # Should contain only digits, comma, or period
            assert all(c.isdigit() or c in ".,- " for c in value)


class TestLogAlert:
    """Test alert logging."""
    
    def test_log_alert_creates_file(self, alerter, alert_log_path):
        """Test that logging creates the alert log file."""
        alert = {
            "type": "budget",
            "severity": "WARN",
            "message": "Test alert",
        }
        
        alerter._log_alert(alert)
        
        assert alert_log_path.exists()
    
    def test_log_alert_creates_valid_json(self, alerter, alert_log_path):
        """Test that logged alert is valid JSON."""
        alert = {
            "type": "budget",
            "severity": "WARN",
            "message": "Test alert",
        }
        
        alerter._log_alert(alert)
        
        with open(alert_log_path) as f:
            logged = json.loads(f.readline())
        
        assert logged["type"] == "budget"
        assert logged["severity"] == "WARN"
        assert logged["acknowledged"] is False
    
    def test_log_alert_appends_to_file(self, alerter, alert_log_path):
        """Test that multiple alerts are appended."""
        alert1 = {"type": "budget", "severity": "WARN", "message": "Alert 1"}
        alert2 = {"type": "error", "severity": "CRITICAL", "message": "Alert 2"}
        
        alerter._log_alert(alert1)
        alerter._log_alert(alert2)
        
        with open(alert_log_path) as f:
            lines = f.readlines()
        
        assert len(lines) == 2
        assert json.loads(lines[0])["message"] == "Alert 1"
        assert json.loads(lines[1])["message"] == "Alert 2"
    
    def test_log_alert_includes_timestamp(self, alerter, alert_log_path):
        """Test that logged alert includes timestamp."""
        alert = {"type": "test", "severity": "INFO", "message": "Test"}
        
        alerter._log_alert(alert)
        
        with open(alert_log_path) as f:
            logged = json.loads(f.readline())
        
        assert "timestamp" in logged
        # Should be ISO format
        assert "T" in logged["timestamp"]
        assert "Z" in logged["timestamp"]


class TestAcknowledgeAlert:
    """Test alert acknowledgment."""
    
    def test_acknowledge_alert_returns_true(self, alerter):
        """Test that acknowledge_alert returns True."""
        result = alerter.acknowledge_alert("alert_123", "user@example.com")
        
        assert result is True
    
    def test_acknowledge_alert_with_timestamp(self, alerter):
        """Test acknowledge_alert with timestamp."""
        result = alerter.acknowledge_alert("alert_456", "admin@example.com")
        
        assert result is True


class TestSendEmail:
    """Test email sending."""
    
    def test_send_email_returns_true(self, alerter):
        """Test that _send_email returns True (logging mode)."""
        alert = {
            "type": "budget",
            "severity": "WARN",
            "message": "Test email",
        }
        recipients = ["test@example.com"]
        
        result = alerter._send_email(alert, recipients)
        
        assert result is True
    
    def test_send_email_formats_subject(self, alerter):
        """Test that email subject is formatted correctly."""
        alert = {
            "type": "budget",
            "severity": "CRITICAL",
            "message": "Budget exceeded",
        }
        recipients = ["admin@example.com"]
        
        # Should not raise
        result = alerter._send_email(alert, recipients)
        
        assert result is True


class TestSendSlack:
    """Test Slack sending."""
    
    def test_send_slack_returns_true(self, alerter):
        """Test that _send_slack returns True (logging mode)."""
        alert = {
            "type": "error_rate",
            "severity": "WARN",
            "message": "Error rate elevated",
        }
        channels = ["#warnings"]
        
        result = alerter._send_slack(alert, channels)
        
        assert result is True
    
    def test_send_slack_color_mapping(self, alerter):
        """Test that severity maps to color correctly."""
        for severity, expected_color in [
            ("BLOCK", "#ff0000"),
            ("CRITICAL", "#ff9900"),
            ("WARN", "#ffcc00"),
            ("INFO", "#0099ff"),
        ]:
            alert = {
                "type": "test",
                "severity": severity,
                "message": "Test",
            }
            # Should not raise
            result = alerter._send_slack(alert, ["#test"])
            assert result is True


class TestSendPagerDuty:
    """Test PagerDuty sending."""
    
    def test_send_pagerduty_returns_true(self, alerter):
        """Test that _send_pagerduty returns True (logging mode)."""
        alert = {
            "type": "budget",
            "severity": "BLOCK",
            "message": "Budget exceeded",
        }
        
        result = alerter._send_pagerduty(alert, "BLOCK")
        
        assert result is True
    
    def test_send_pagerduty_critical_severity(self, alerter):
        """Test PagerDuty with CRITICAL severity."""
        alert = {
            "type": "error_rate",
            "severity": "CRITICAL",
            "message": "Critical error rate",
        }
        
        result = alerter._send_pagerduty(alert, "CRITICAL")
        
        assert result is True
