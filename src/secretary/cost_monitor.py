"""Cost monitoring — automated daily/weekly budget alerts.

Tracks premium API spend against configured thresholds and triggers alerts
when budget thresholds are breached. Logs all alerts with timestamp, current
spend, and threshold exceeded.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class CostAlert:
    """A single cost alert event."""
    timestamp: str  # ISO 8601 timestamp
    current_spend_usd: float
    spend_multiplier: float  # Premium-weighted cost (e.g., 3.0 for Opus)
    daily_limit_usd: float | None
    weekly_limit_usd: float | None
    threshold_pct: int  # 80 = 80% threshold breached
    alert_type: str  # "daily" | "weekly"
    message: str


@dataclass
class CostMonitorConfig:
    """Cost monitoring settings."""
    enabled: bool = True
    daily_limit_usd: float = 10.0  # Stop new tasks if daily spend >= this
    weekly_limit_usd: float = 50.0  # Stop new tasks if weekly spend >= this
    alert_threshold_pct: int = 80  # Alert when 80%+ of budget consumed
    log_path: str = "data/cost_alerts.jsonl"
    check_interval_seconds: int = 300  # Check every 5 minutes


class CostMonitor:
    """Tracks and alerts on API spend against budget thresholds."""

    def __init__(self, config: CostMonitorConfig, run_log_path: Path | str = "data/run_log.jsonl"):
        """Initialize the cost monitor.

        Args:
            config: CostMonitorConfig with budget limits and alert settings
            run_log_path: Path to run_log.jsonl for reading spend data
        """
        self.config = config
        self.run_log_path = Path(run_log_path)
        self.alert_log_path = Path(config.log_path)
        self._last_alert_timestamp: dict[str, datetime] = {}  # Debounce alerts

    def _read_run_log(self) -> list[dict[str, Any]]:
        """Read and parse the run_log.jsonl file."""
        if not self.run_log_path.exists():
            return []
        try:
            lines = self.run_log_path.read_text(encoding="utf-8").strip().split("\n")
            return [json.loads(line) for line in lines if line.strip()]
        except (json.JSONDecodeError, OSError) as e:
            log.warning("Failed to read run_log: %s", e)
            return []

    def _calculate_daily_spend(self) -> tuple[float, float]:
        """Calculate today's spend in USD and premium-weighted multiplier units.

        Returns:
            (total_spend_usd, total_premium_multiplier_units)
        """
        entries = self._read_run_log()
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        total_spend_usd = 0.0
        total_premium_units = 0.0

        for entry in entries:
            try:
                entry_time = datetime.fromisoformat(entry["timestamp"].replace("+00:00", ""))
                if entry_time < today_start:
                    continue  # Not today

                # USD spend (direct cost)
                if entry.get("success"):
                    cost_usd = entry.get("cost_usd", 0.0)
                    total_spend_usd += cost_usd

                    # Premium multiplier (e.g., 3.0 for Opus, 0.33 for Haiku)
                    premium_cost = entry.get("premium_cost", 0.0)
                    total_premium_units += premium_cost
            except (ValueError, KeyError, TypeError):
                continue

        return total_spend_usd, total_premium_units

    def _calculate_weekly_spend(self) -> tuple[float, float]:
        """Calculate this week's spend in USD and premium-weighted multiplier units.

        Returns:
            (total_spend_usd, total_premium_multiplier_units)
        """
        entries = self._read_run_log()
        now = datetime.now()
        week_start = now - timedelta(days=now.weekday())  # Monday
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

        total_spend_usd = 0.0
        total_premium_units = 0.0

        for entry in entries:
            try:
                entry_time = datetime.fromisoformat(entry["timestamp"].replace("+00:00", ""))
                if entry_time < week_start:
                    continue  # Before this week

                # USD spend
                if entry.get("success"):
                    cost_usd = entry.get("cost_usd", 0.0)
                    total_spend_usd += cost_usd

                    # Premium multiplier
                    premium_cost = entry.get("premium_cost", 0.0)
                    total_premium_units += premium_cost
            except (ValueError, KeyError, TypeError):
                continue

        return total_spend_usd, total_premium_units

    def _should_alert(self, alert_type: str) -> bool:
        """Check if we should send an alert (debounce: max once per period).

        Args:
            alert_type: "daily" or "weekly"

        Returns:
            True if enough time has passed since last alert of this type
        """
        last = self._last_alert_timestamp.get(alert_type)
        if last is None:
            return True
        
        interval = timedelta(hours=1) if alert_type == "daily" else timedelta(hours=4)
        return datetime.now() - last > interval

    def check_and_alert(self) -> CostAlert | None:
        """Check budgets and log alert if threshold breached.

        Returns:
            CostAlert if alert triggered, else None
        """
        if not self.config.enabled:
            return None

        # Check daily budget
        daily_spend_usd, daily_premium = self._calculate_daily_spend()
        daily_pct = int(
            (daily_spend_usd / self.config.daily_limit_usd * 100)
            if self.config.daily_limit_usd > 0
            else 0
        )

        if (
            daily_pct >= self.config.alert_threshold_pct
            and self.config.daily_limit_usd > 0
            and self._should_alert("daily")
        ):
            alert = CostAlert(
                timestamp=datetime.now(timezone.utc).isoformat(),
                current_spend_usd=daily_spend_usd,
                spend_multiplier=daily_premium,
                daily_limit_usd=self.config.daily_limit_usd,
                weekly_limit_usd=None,
                threshold_pct=daily_pct,
                alert_type="daily",
                message=(
                    f"⚠️ DAILY BUDGET ALERT: {daily_pct}% of daily limit consumed "
                    f"(${daily_spend_usd:.2f} / ${self.config.daily_limit_usd:.2f})"
                ),
            )
            self._log_alert(alert)
            self._last_alert_timestamp["daily"] = datetime.now()
            log.warning(alert.message)
            return alert

        # Check weekly budget
        weekly_spend_usd, weekly_premium = self._calculate_weekly_spend()
        weekly_pct = int(
            (weekly_spend_usd / self.config.weekly_limit_usd * 100)
            if self.config.weekly_limit_usd > 0
            else 0
        )

        if (
            weekly_pct >= self.config.alert_threshold_pct
            and self.config.weekly_limit_usd > 0
            and self._should_alert("weekly")
        ):
            alert = CostAlert(
                timestamp=datetime.now(timezone.utc).isoformat(),
                current_spend_usd=weekly_spend_usd,
                spend_multiplier=weekly_premium,
                daily_limit_usd=None,
                weekly_limit_usd=self.config.weekly_limit_usd,
                threshold_pct=weekly_pct,
                alert_type="weekly",
                message=(
                    f"⚠️ WEEKLY BUDGET ALERT: {weekly_pct}% of weekly limit consumed "
                    f"(${weekly_spend_usd:.2f} / ${self.config.weekly_limit_usd:.2f})"
                ),
            )
            self._log_alert(alert)
            self._last_alert_timestamp["weekly"] = datetime.now()
            log.warning(alert.message)
            return alert

        return None

    def _log_alert(self, alert: CostAlert) -> None:
        """Log alert to cost_alerts.jsonl."""
        try:
            # Ensure parent directory exists
            self.alert_log_path.parent.mkdir(parents=True, exist_ok=True)

            # Append alert as JSON line
            with open(self.alert_log_path, "a", encoding="utf-8") as f:
                alert_dict = {
                    "timestamp": alert.timestamp,
                    "current_spend_usd": alert.current_spend_usd,
                    "spend_multiplier": alert.spend_multiplier,
                    "daily_limit_usd": alert.daily_limit_usd,
                    "weekly_limit_usd": alert.weekly_limit_usd,
                    "threshold_pct": alert.threshold_pct,
                    "alert_type": alert.alert_type,
                    "message": alert.message,
                }
                f.write(json.dumps(alert_dict) + "\n")
        except OSError as e:
            log.error("Failed to log alert: %s", e)

    def is_budget_exhausted(self) -> bool:
        """Check if either daily or weekly budget is exhausted (>= limit).

        Used by watcher to decide whether to accept new tasks.

        Returns:
            True if either daily or weekly budget is exceeded
        """
        daily_spend_usd, _ = self._calculate_daily_spend()
        weekly_spend_usd, _ = self._calculate_weekly_spend()

        daily_exhausted = (
            self.config.daily_limit_usd > 0 and daily_spend_usd >= self.config.daily_limit_usd
        )
        weekly_exhausted = (
            self.config.weekly_limit_usd > 0 and weekly_spend_usd >= self.config.weekly_limit_usd
        )

        return daily_exhausted or weekly_exhausted

    def get_spend_summary(self) -> dict[str, Any]:
        """Return current spend vs limits for display purposes."""
        daily_usd, daily_premium = self._calculate_daily_spend()
        weekly_usd, weekly_premium = self._calculate_weekly_spend()
        return {
            "daily_usd": round(daily_usd, 4),
            "daily_premium": round(daily_premium, 2),
            "daily_limit_usd": self.config.daily_limit_usd,
            "daily_pct": int(daily_usd / self.config.daily_limit_usd * 100) if self.config.daily_limit_usd > 0 else 0,
            "weekly_usd": round(weekly_usd, 4),
            "weekly_premium": round(weekly_premium, 2),
            "weekly_limit_usd": self.config.weekly_limit_usd,
            "weekly_pct": int(weekly_usd / self.config.weekly_limit_usd * 100) if self.config.weekly_limit_usd > 0 else 0,
            "exhausted": self.is_budget_exhausted(),
        }

    def send_alert_email(self, alert: CostAlert, notify_email: str, data_path: Path) -> bool:
        """Send a budget alert via Gmail draft.

        Args:
            alert: The cost alert to send
            notify_email: Email address to draft alert to
            data_path: Path to data dir (for Gmail OAuth tokens)

        Returns:
            True if draft created successfully
        """
        try:
            from .mcp_tools.google_auth import build_gmail_service
            from .currency import format_cost, usd_to_cad
            import base64
            from email.mime.text import MIMEText

            cad_spend = usd_to_cad(alert.current_spend_usd)
            limit = alert.daily_limit_usd if alert.alert_type == "daily" else alert.weekly_limit_usd
            limit_cad = usd_to_cad(limit) if limit else 0

            subject = f"[Secretary] Budget Alert: {alert.alert_type} spend at {alert.threshold_pct}%"
            body = (
                f"{alert.message}\n\n"
                f"Current spend: {format_cost(alert.current_spend_usd)}\n"
                f"Budget limit:  ${limit:.2f} USD (${limit_cad:.2f} CAD)\n"
                f"Premium units: {alert.spend_multiplier:.1f}x\n\n"
                f"Check with: secretary budget\n"
            )

            svc = build_gmail_service(data_path)
            message = MIMEText(body)
            message["To"] = notify_email
            message["Subject"] = subject
            raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
            svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
            log.info("Budget alert drafted to %s: %s", notify_email, alert.alert_type)
            return True
        except Exception as e:
            log.warning("Could not send budget alert email: %s", e)
            return False
