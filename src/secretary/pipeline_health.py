"""Pipeline health log — captures non-task events invisible to run_log.

The self-improvement analysis engine only sees run_log.jsonl (task outcomes).
This module captures everything else: analysis failures, reflection errors,
skipped tasks, cycle metadata, config issues, and pipeline breakages.

Events are written to data/pipeline_health.jsonl and read by the analysis
engine alongside run_log failures to give the self-improvement loop full
visibility into its own operation.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class HealthEvent:
    """A single pipeline health event."""

    timestamp: str
    category: str       # "analysis_failure", "reflection_error", "cycle_metadata",
                        # "config_error", "pipeline_error", "skipped_task",
                        # "quota_exhaustion", "scope_violation"
    severity: str       # "info", "warning", "error"
    message: str        # human-readable description
    source: str = ""    # module/function that generated the event
    details: str = ""   # extra context (truncated to 500 chars on write)
    cycle: int = 0


class HealthLog:
    """Append-only JSONL log of pipeline health events."""

    _MAX_BYTES = 2 * 1024 * 1024  # 2 MB — much smaller than run_log

    def __init__(self, path: Path | str = "data/pipeline_health.jsonl"):
        self.path = Path(path)

    def record(
        self,
        category: str,
        severity: str,
        message: str,
        *,
        source: str = "",
        details: str = "",
        cycle: int = 0,
    ) -> None:
        """Record a health event."""
        event = HealthEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            category=category,
            severity=severity,
            message=message[:300],
            source=source,
            details=details[:500],
            cycle=cycle,
        )
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Rotate if too large
            if self.path.exists() and self.path.stat().st_size >= self._MAX_BYTES:
                archive = self.path.with_suffix(".jsonl.1")
                archive.unlink(missing_ok=True)
                self.path.replace(archive)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        except OSError:
            pass  # Can't log health events if filesystem is broken

    def recent(self, n: int = 50) -> list[HealthEvent]:
        """Read the last N health events."""
        if not self.path.exists():
            return []
        events: list[HealthEvent] = []
        try:
            import collections
            with open(self.path, "r", encoding="utf-8") as f:
                tail = collections.deque(f, maxlen=n)
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    events.append(HealthEvent(**d))
                except (json.JSONDecodeError, TypeError):
                    pass
        except OSError:
            pass
        return events

    def recent_errors(self, n: int = 20, hours: float = 24.0) -> list[HealthEvent]:
        """Read recent warning/error events within the last N hours."""
        events = self.recent(n * 3)  # read extra to filter
        cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
        result: list[HealthEvent] = []
        for e in events:
            if e.severity not in ("warning", "error"):
                continue
            try:
                ts = datetime.fromisoformat(e.timestamp).timestamp()
                if ts < cutoff:
                    continue
            except (ValueError, TypeError):
                continue
            result.append(e)
            if len(result) >= n:
                break
        return result
