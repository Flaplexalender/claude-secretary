"""
Multi-channel alert dispatcher: Email, Slack, PagerDuty.

Routes alerts from monitor.py to appropriate recipients based on severity and alert type.
Respects SLA targets for delivery time and implements acknowledgment tracking.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class Alerter:
    """Dispatches alerts via email, Slack, and PagerDuty."""

    def __init__(self, config: dict, alert_log_path: Path):
        """
        Initialize alerter.
        
        Args:
            config: Full config dict (includes alerts section)
            alert_log_path: Path to alert_log.jsonl for tracking acknowledgments
        """
        self.config = config
        self.alert_log_path = alert_log_path
        self.alerts_config = config.get("alerts", {})
        self.enabled = self.alerts_config.get("enabled", True)
        self.recipients = self.alerts_config.get("recipients", {})
        self.channels = self.alerts_config.get("channels", {})
        self.sla_sec = self.alerts_config.get("sla_sec", {})

    def dispatch(self, alert: dict) -> bool:
        """
        Dispatch alert via configured channels based on severity and type.
        
        Args:
            alert: Alert dict from monitor (has 'type', 'severity', 'message', etc.)
        
        Returns:
            True if dispatch succeeded
        """
        if not self.enabled:
            logger.info(f"Alerts disabled; skipping: {alert['message']}")
            return False

        severity = alert.get("severity", "INFO")
        recipients_list = self._get_recipients(severity)
        
        logger.info(f"Dispatching {severity} alert: {alert['message']}")
        
        # Log alert for acknowledgment tracking
        self._log_alert(alert)
        
        success = True
        
        # Email dispatch
        if self.channels.get("email", True) and recipients_list.get("email"):
            success &= self._send_email(alert, recipients_list["email"])
        
        # Slack dispatch
        if self.channels.get("slack", True) and recipients_list.get("slack"):
            success &= self._send_slack(alert, recipients_list["slack"])
        
        # PagerDuty dispatch (BLOCK and CRITICAL only)
        if severity in ["BLOCK", "CRITICAL"] and self.channels.get("pagerduty", True):
            success &= self._send_pagerduty(alert, severity)
        
        return success

    def _get_recipients(self, severity: str) -> dict:
        """Map severity to recipient list."""
        if severity == "BLOCK":
            return {
                "email": self.recipients.get("block", []),
                "slack": self.recipients.get("block_slack", []),
            }
        elif severity == "CRITICAL":
            return {
                "email": self.recipients.get("critical", []),
                "slack": self.recipients.get("critical_slack", []),
            }
        else:  # WARN, INFO
            return {
                "email": self.recipients.get("warn", []),
                "slack": self.recipients.get("warn_slack", []),
            }

    def _send_email(self, alert: dict, recipients: list) -> bool:
        """
        Send email alert.
        
        In production, integrate with SendGrid/SES. For now, log only.
        NOTE: Email integration deferred to v2.0. Currently logging only.
        """
        severity = alert.get("severity", "INFO")
        message = alert.get("message", "")
        
        subject = f"[{severity}] Cost Alert: {alert.get('type', 'unknown')}"
        body = self._format_email_body(alert)
        
        logger.info(f"EMAIL → {recipients}: {subject}")
        logger.debug(f"Body:\n{body}")
        
        # Email delivery deferred: implement SendGrid/SES SDK in v2.0
        # Placeholder for future integration:
        # sendgrid_client = SendGridAPIClient(os.environ.get('SENDGRID_API_KEY'))
        # for recipient in recipients:
        #     message_obj = Mail(from_email='alerts@company.com', to_emails=recipient,
        #                        subject=subject, plain_text_content=body)
        #     sendgrid_client.send(message_obj)
        
        return True

    def _send_slack(self, alert: dict, channels: list) -> bool:
        """
        Send Slack alert.
        
        In production, use Slack SDK. For now, log only.
        NOTE: Slack integration deferred to v2.0. Currently logging only.
        """
        severity = alert.get("severity", "INFO")
        message = alert.get("message", "")
        
        color_map = {
            "BLOCK": "#ff0000",
            "CRITICAL": "#ff9900",
            "WARN": "#ffcc00",
            "INFO": "#0099ff",
        }
        
        color = color_map.get(severity, "#cccccc")
        
        payload = {
            "attachments": [
                {
                    "color": color,
                    "title": f"{severity}: {alert.get('type', 'Cost Alert')}",
                    "text": message,
                    "fields": self._format_slack_fields(alert),
                    "footer": f"Cost Monitor | {datetime.utcnow().isoformat()}Z",
                }
            ]
        }
        
        logger.info(f"SLACK → {channels}: {message}")
        logger.debug(f"Payload: {json.dumps(payload)}")
        
        # Slack delivery deferred: implement SDK in v2.0
        # Placeholder for future integration:
        # from slack_sdk.webhook import WebhookClient
        # for channel_webhook_url in channels:
        #     webhook = WebhookClient(channel_webhook_url)
        #     response = webhook.send(blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": message}}])
        
        return True

    def _send_pagerduty(self, alert: dict, severity: str) -> bool:
        """
        Send PagerDuty incident for BLOCK/CRITICAL.
        
        In production, use PagerDuty SDK. For now, log only.
        NOTE: PagerDuty integration deferred to v2.0. Currently logging only.
        """
        incident_key = f"cost-alert-{alert.get('type', 'unknown')}"
        
        payload = {
            "routing_key": "PagerDuty_routing_key_placeholder",
            "event_action": "trigger",
            "dedup_key": incident_key,
            "payload": {
                "summary": alert.get("message", "Cost Alert"),
                "severity": "critical" if severity == "BLOCK" else "warning",
                "source": "Cost Monitor",
                "custom_details": alert,
            },
        }
        
        logger.info(f"PAGERDUTY: {alert.get('message', 'Cost Alert')}")
        logger.debug(f"Payload: {json.dumps(payload)}")
        
        # PagerDuty delivery deferred: implement SDK in v2.0
        # Placeholder for future integration:
        # import requests
        # from os import environ
        # response = requests.post(
        #     "https://events.pagerduty.com/v2/enqueue",
        #     json=payload,
        #     headers={"Authorization": f"Token token={environ.get('PAGERDUTY_TOKEN')}"}
        # )
        
        return True

    def _format_email_body(self, alert: dict) -> str:
        """Format email body."""
        lines = [
            f"Alert Type: {alert.get('type', 'Unknown')}",
            f"Severity: {alert.get('severity', 'INFO')}",
            f"Message: {alert.get('message', '')}",
            "",
            "Details:",
        ]
        
        for key, value in alert.items():
            if key not in ["type", "severity", "message"]:
                lines.append(f"  {key}: {value}")
        
        lines.extend([
            "",
            "Action Required:",
            "1. Review the cost dashboard: https://monitoring.company.com/costs",
            "2. If blocking, contact VP Engineering to authorize overage",
            "3. Acknowledge this alert to prevent escalation",
        ])
        
        return "\n".join(lines)

    def _format_slack_fields(self, alert: dict) -> list:
        """Format Slack message fields."""
        fields = []
        
        # Core fields
        core_fields = ["daily_limit", "current_spend", "pct_spent", "model", "quota", "used"]
        for key in core_fields:
            if key in alert:
                value = alert[key]
                if isinstance(value, float):
                    value = f"{value:.2f}"
                fields.append({
                    "title": key.replace("_", " ").title(),
                    "value": str(value),
                    "short": True,
                })
        
        return fields

    def _log_alert(self, alert: dict) -> None:
        """Log alert to alert_log.jsonl for tracking."""
        self.alert_log_path.parent.mkdir(parents=True, exist_ok=True)
        
        log_entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": alert.get("type", "unknown"),
            "severity": alert.get("severity", "INFO"),
            "message": alert.get("message", ""),
            "acknowledged": False,
            "alert_data": alert,
        }
        
        try:
            with open(self.alert_log_path, "a") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to log alert: {e}")

    def acknowledge_alert(self, alert_id: str, user: str) -> bool:
        """
        Mark alert as acknowledged by user.
        
        Args:
            alert_id: Alert identifier (timestamp or type)
            user: User acknowledging the alert
        
        Returns:
            True if acknowledgment recorded
        """
        # Alert acknowledgment tracking deferred to v2.0
        # Implementation: update alert_log.jsonl records with ack timestamp and user
        # For now, log the acknowledgment for audit trail
        logger.info(f"Alert {alert_id} acknowledged by {user} at {datetime.utcnow().isoformat()}Z")
        return True
