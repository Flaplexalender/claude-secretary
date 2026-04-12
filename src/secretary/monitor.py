"""
Real-time cost monitoring and alert triggering.

Monitors run_log.jsonl for budget threshold breaches, token overages, latency spikes,
and error rate anomalies. Triggers multi-channel alerts (email, Slack, PagerDuty) via alerter.py.
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class CostMonitor:
    """Tracks spend against budget thresholds; triggers alerts on breach."""

    def __init__(self, config: dict, run_log_path: Path):
        self.config = config
        self.run_log_path = run_log_path
        self.alerts_config = config.get("alerts", {})
        self.enabled = self.alerts_config.get("enabled", True)

    def check_daily_budget(self, env: str = "production") -> Optional[dict]:
        """
        Check if daily budget threshold breached.
        
        Returns alert dict if threshold hit, else None.
        """
        if not self.enabled:
            return None

        threshold_config = self.alerts_config.get("budget_thresholds", {}).get(env)
        if not threshold_config:
            logger.warning(f"No budget config for env: {env}")
            return None

        daily_limit = threshold_config.get("daily_usd", 100.0)
        warn_pct = threshold_config.get("warn_pct", 80)
        critical_pct = threshold_config.get("critical_pct", 90)

        # Sum costs from today
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        today_spend = self._sum_spend_since(today_str, hours=24)

        pct_spent = (today_spend / daily_limit * 100) if daily_limit > 0 else 0

        alert = None
        if pct_spent >= 100:
            alert = {
                "type": "budget_blocked",
                "severity": "BLOCK",
                "env": env,
                "daily_limit": daily_limit,
                "current_spend": today_spend,
                "pct_spent": pct_spent,
                "message": f"Daily budget BLOCKED: ${today_spend:.2f}/${daily_limit:.2f} ({pct_spent:.0f}%)",
            }
        elif pct_spent >= critical_pct:
            alert = {
                "type": "budget_critical",
                "severity": "CRITICAL",
                "env": env,
                "daily_limit": daily_limit,
                "current_spend": today_spend,
                "pct_spent": pct_spent,
                "message": f"Daily budget CRITICAL: ${today_spend:.2f}/${daily_limit:.2f} ({pct_spent:.0f}%)",
            }
        elif pct_spent >= warn_pct:
            alert = {
                "type": "budget_warn",
                "severity": "WARN",
                "env": env,
                "daily_limit": daily_limit,
                "current_spend": today_spend,
                "pct_spent": pct_spent,
                "message": f"Daily budget WARNING: ${today_spend:.2f}/${daily_limit:.2f} ({pct_spent:.0f}%)",
            }

        return alert

    def check_token_quota(self, model: str) -> Optional[dict]:
        """
        Check if token quota (per request) exceeded for a model.
        
        Returns alert if recent request used >80% of quota.
        """
        if not self.enabled:
            return None

        quotas = self.alerts_config.get("metrics", {}).get("token_quota", {})
        quota = quotas.get(model)
        if not quota:
            return None

        # Get last request tokens for this model
        last_tokens = self._last_model_tokens(model)
        if not last_tokens:
            return None

        input_tokens, output_tokens = last_tokens
        total_tokens = input_tokens + output_tokens
        pct_used = (total_tokens / quota * 100) if quota > 0 else 0

        if pct_used >= 100:
            return {
                "type": "token_quota_blocked",
                "severity": "BLOCK",
                "model": model,
                "quota": quota,
                "used": total_tokens,
                "pct_used": pct_used,
                "message": f"Token quota BLOCKED for {model}: {total_tokens}/{quota} ({pct_used:.0f}%)",
            }
        elif pct_used >= 95:
            return {
                "type": "token_quota_critical",
                "severity": "CRITICAL",
                "model": model,
                "quota": quota,
                "used": total_tokens,
                "pct_used": pct_used,
                "message": f"Token quota CRITICAL for {model}: {total_tokens}/{quota} ({pct_used:.0f}%)",
            }
        elif pct_used >= 80:
            return {
                "type": "token_quota_warn",
                "severity": "WARN",
                "model": model,
                "quota": quota,
                "used": total_tokens,
                "pct_used": pct_used,
                "message": f"Token quota WARNING for {model}: {total_tokens}/{quota} ({pct_used:.0f}%)",
            }

        return None

    def check_latency(self, model: str) -> Optional[dict]:
        """
        Check if latency spike detected (>50% above baseline or >2x absolute).
        
        Returns alert if latency exceeded threshold.
        """
        if not self.enabled:
            return None

        sla_sec = self.alerts_config.get("metrics", {}).get("latency_sla_s", {}).get(model)
        if not sla_sec:
            return None

        # Get last 100 requests for this model
        recent_latencies = self._last_model_latencies(model, count=100)
        if len(recent_latencies) < 10:
            return None  # Not enough data

        baseline_p50 = sorted(recent_latencies)[len(recent_latencies) // 2]
        spike_threshold = max(baseline_p50 * 1.5, sla_sec * 2)

        # Get last request
        last_duration = recent_latencies[-1] if recent_latencies else 0

        if last_duration > spike_threshold:
            return {
                "type": "latency_spike",
                "severity": "WARN",
                "model": model,
                "baseline_p50": baseline_p50,
                "current": last_duration,
                "threshold": spike_threshold,
                "sla": sla_sec,
                "message": f"Latency spike for {model}: {last_duration:.2f}s (baseline {baseline_p50:.2f}s, SLA {sla_sec}s)",
            }

        return None

    def check_error_rate(self, model: str) -> Optional[dict]:
        """
        Check if error rate spike detected (>5% in last 100 runs).
        
        Returns alert if error rate exceeds threshold.
        """
        if not self.enabled:
            return None

        error_threshold = self.alerts_config.get("metrics", {}).get("error_rate_threshold", 0.05)

        # Get last 100 runs for this model
        recent_runs = self._last_model_runs(model, count=100)
        if len(recent_runs) < 10:
            return None  # Not enough data

        failed = sum(1 for run in recent_runs if not run.get("success", True))
        error_rate = failed / len(recent_runs) if recent_runs else 0

        if error_rate > 0.10:
            return {
                "type": "error_rate_critical",
                "severity": "CRITICAL",
                "model": model,
                "error_rate": error_rate,
                "threshold": error_threshold,
                "failed_count": failed,
                "total_count": len(recent_runs),
                "message": f"Error rate CRITICAL for {model}: {error_rate*100:.1f}% ({failed}/{len(recent_runs)} tasks)",
            }
        elif error_rate > error_threshold:
            return {
                "type": "error_rate_warn",
                "severity": "WARN",
                "model": model,
                "error_rate": error_rate,
                "threshold": error_threshold,
                "failed_count": failed,
                "total_count": len(recent_runs),
                "message": f"Error rate WARNING for {model}: {error_rate*100:.1f}% ({failed}/{len(recent_runs)} tasks)",
            }

        return None

    def _sum_spend_since(self, date_str: str, hours: int = 24) -> float:
        """Sum premium_cost from run_log.jsonl since date_str."""
        if not self.run_log_path.exists():
            return 0.0

        cutoff = datetime.fromisoformat(date_str) - timedelta(hours=hours)
        total = 0.0

        try:
            with open(self.run_log_path, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    run = json.loads(line)
                    run_time = datetime.fromisoformat(run["timestamp"].replace("Z", "+00:00"))
                    if run_time >= cutoff:
                        total += run.get("cost_usd", 0)
        except Exception as e:
            logger.error(f"Error reading run_log: {e}")

        return total

    def _last_model_tokens(self, model: str) -> Optional[tuple]:
        """Get (input_tokens, output_tokens) from last request for model."""
        if not self.run_log_path.exists():
            return None

        try:
            with open(self.run_log_path, "r") as f:
                for line in reversed(list(f)):
                    if not line.strip():
                        continue
                    run = json.loads(line)
                    if run.get("model") == model:
                        return (
                            run.get("input_tokens", 0),
                            run.get("output_tokens", 0),
                        )
        except Exception as e:
            logger.error(f"Error reading run_log: {e}")

        return None

    def _last_model_latencies(self, model: str, count: int = 100) -> list:
        """Get last N durations (seconds) for model from run_log."""
        if not self.run_log_path.exists():
            return []

        durations = []
        try:
            with open(self.run_log_path, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    run = json.loads(line)
                    if run.get("model") == model:
                        durations.append(run.get("duration_s", 0))
                        if len(durations) >= count:
                            break
        except Exception as e:
            logger.error(f"Error reading run_log: {e}")

        return durations

    def _last_model_runs(self, model: str, count: int = 100) -> list:
        """Get last N full run records for model from run_log."""
        if not self.run_log_path.exists():
            return []

        runs = []
        try:
            with open(self.run_log_path, "r") as f:
                for line in f:
                    if not line.strip():
                        continue
                    run = json.loads(line)
                    if run.get("model") == model:
                        runs.append(run)
                        if len(runs) >= count:
                            break
        except Exception as e:
            logger.error(f"Error reading run_log: {e}")

        return runs
