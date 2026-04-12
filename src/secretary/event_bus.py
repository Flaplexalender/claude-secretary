"""Event bus for reactive, event-driven campaign execution.

Provides a lightweight pub/sub system where event sources (Gmail, Calendar,
file system) emit events, and campaign tasks can subscribe via trigger rules
in their YAML definition.

Architecture:
    EventSource  →  EventBus  →  Campaign tasks with ``trigger`` rules
    (poll-based)    (in-mem)     (activated only when a matching event fires)

Event sources are polled once at the start of each watcher cycle.  This keeps
the architecture simple — no background threads, no OS-level file watchers —
while still enabling reactive behaviour at cycle granularity.

Campaign YAML trigger syntax::

    tasks:
      - id: urgent_email_handler
        prompt: "Triage this urgent email: {event.subject}"
        trigger: "event:new_email"              # any new email activates
      - id: vip_email_handler
        prompt: "Handle VIP email: {event.subject}"
        trigger: "event:new_email:from=boss@"   # only emails matching filter
      - id: meeting_prep
        prompt: "Prepare briefing for: {event.title}"
        trigger: "event:calendar_soon"
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

log = logging.getLogger("secretary.event_bus")


# ── Event types ───────────────────────────────────────────────


class EventType:
    """Known event type constants.  Raw strings also accepted."""

    NEW_EMAIL = "new_email"
    CALENDAR_SOON = "calendar_soon"
    FILE_CHANGED = "file_changed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    CYCLE_START = "cycle_start"
    CYCLE_END = "cycle_end"


@dataclass(frozen=True)
class Event:
    """An immutable event emitted by a source."""

    type: str
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    dedup_key: str = ""

    @property
    def summary(self) -> str:
        if self.type == EventType.NEW_EMAIL:
            subj = self.payload.get("subject", "")
            sender = self.payload.get("from", "")
            return f"New email from {sender}: {subj}"
        if self.type == EventType.CALENDAR_SOON:
            title = self.payload.get("title", "")
            mins = self.payload.get("minutes_until", "?")
            return f"Calendar event in {mins}m: {title}"
        if self.type == EventType.FILE_CHANGED:
            path = self.payload.get("path", "")
            return f"File changed: {path}"
        return f"{self.type}: {self.payload}"


# ── Event sources ─────────────────────────────────────────────


class EventSource(Protocol):
    """Protocol for anything that can be polled for events."""

    async def poll(self) -> list[Event]: ...


class GmailEventSource:
    """Detects new unread emails by hashing the gmail_search result across cycles."""

    def __init__(self, tools: dict[str, Any]) -> None:
        self._tools = tools
        self._prev_hash: str | None = None

    async def poll(self) -> list[Event]:
        if "gmail_search" not in self._tools:
            return []
        try:
            func = self._tools["gmail_search"]["func"]
            result = await func({"query": "is:unread newer_than:1d", "max_results": 10})
            text = _extract_text(result)
            cur_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

            if self._prev_hash is None:
                # First poll — seed baseline, don't fire
                self._prev_hash = cur_hash
                log.debug("Gmail source initialised (hash=%s)", cur_hash)
                return []

            if cur_hash == self._prev_hash:
                return []

            self._prev_hash = cur_hash
            ev = Event(
                type=EventType.NEW_EMAIL,
                source="gmail",
                payload={"raw_text": text[:2000]},
                dedup_key=f"gmail:{cur_hash}",
            )
            log.info("Gmail: new unread email(s) detected")
            return [ev]
        except Exception as e:
            log.warning("Gmail event source error: %s", e)
            return []


class CalendarEventSource:
    """Detects upcoming calendar events within a configurable window."""

    def __init__(self, tools: dict[str, Any], window_minutes: int = 30) -> None:
        self._tools = tools
        self._window_minutes = window_minutes
        self._prev_hash: str | None = None

    async def poll(self) -> list[Event]:
        if "calendar_today" not in self._tools:
            return []
        try:
            func = self._tools["calendar_today"]["func"]
            result = await func({"max_results": 10})
            text = _extract_text(result)
            cur_hash = hashlib.sha256(text.encode()).hexdigest()[:16]

            if self._prev_hash is None:
                self._prev_hash = cur_hash
                log.debug("Calendar source initialised (hash=%s)", cur_hash)
                return []

            if cur_hash == self._prev_hash:
                return []

            self._prev_hash = cur_hash
            ev = Event(
                type=EventType.CALENDAR_SOON,
                source="calendar",
                payload={"raw_text": text[:2000]},
                dedup_key=f"cal:{cur_hash}",
            )
            log.info("Calendar: schedule changed")
            return [ev]
        except Exception as e:
            log.warning("Calendar event source error: %s", e)
            return []


class FileChangeSource:
    """Detects file modifications by tracking mtimes."""

    def __init__(self, watch_paths: list[Any]) -> None:
        from pathlib import Path

        self._watch_paths = [Path(p) for p in watch_paths]
        self._mtimes: dict[str, float] = {}
        self._initialised = False

    async def poll(self) -> list[Event]:
        events: list[Event] = []
        current: dict[str, float] = {}
        for path in self._watch_paths:
            if not path.exists():
                continue
            try:
                mtime = path.stat().st_mtime
                key = str(path)
                current[key] = mtime
                if self._initialised and key in self._mtimes and mtime > self._mtimes[key]:
                    events.append(Event(
                        type=EventType.FILE_CHANGED,
                        source="filesystem",
                        payload={"path": key, "mtime": mtime},
                        dedup_key=f"file:{key}:{mtime}",
                    ))
            except OSError:
                continue
        self._mtimes = current
        if not self._initialised:
            self._initialised = True
        if events:
            log.info("Filesystem: %d file(s) changed", len(events))
        return events


# ── Event bus ─────────────────────────────────────────────────


class EventBus:
    """Central event bus — collects events from sources, matches trigger rules."""

    def __init__(self) -> None:
        self._sources: list[EventSource] = []
        self._events: list[Event] = []
        self._seen_dedup: set[str] = set()

    def add_source(self, source: EventSource) -> None:
        self._sources.append(source)

    async def poll_all(self) -> list[Event]:
        """Poll all registered sources.  Called once per watcher cycle."""
        self._events.clear()
        self._seen_dedup.clear()
        for source in self._sources:
            try:
                new_events = await source.poll()
                for ev in new_events:
                    if ev.dedup_key and ev.dedup_key in self._seen_dedup:
                        continue
                    if ev.dedup_key:
                        self._seen_dedup.add(ev.dedup_key)
                    self._events.append(ev)
            except Exception as e:
                log.warning("Event source poll error: %s", e)
        return list(self._events)

    def emit(self, event: Event) -> None:
        """Manually emit an event (e.g. task_completed, cycle_start)."""
        if event.dedup_key and event.dedup_key in self._seen_dedup:
            return
        if event.dedup_key:
            self._seen_dedup.add(event.dedup_key)
        self._events.append(event)

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def matches_trigger(self, trigger: str) -> list[Event]:
        """Check if any current events match a trigger expression.

        Formats::

            "event:new_email"             → any new_email event
            "event:new_email:from=boss@"  → payload['from'] contains 'boss@'
            "event:file_changed"          → any file change
            "event:calendar_soon"         → any upcoming calendar event

        Returns matching events (empty list → no match).
        """
        if not trigger or not trigger.startswith("event:"):
            return []
        parts = trigger[len("event:"):].split(":", 1)
        event_type = parts[0]
        filter_expr = parts[1] if len(parts) > 1 else None

        matches = []
        for ev in self._events:
            if ev.type != event_type:
                continue
            if filter_expr and "=" in filter_expr:
                fkey, fval = filter_expr.split("=", 1)
                payload_val = str(ev.payload.get(fkey, ""))
                if fval.lower() not in payload_val.lower():
                    continue
            matches.append(ev)
        return matches

    def event_context(self) -> str:
        """Summary of all events this cycle, for injection into task prompts."""
        if not self._events:
            return ""
        lines = [f"[Events this cycle: {len(self._events)}]"]
        for ev in self._events[:10]:
            lines.append(f"  - {ev.summary}")
        if len(self._events) > 10:
            lines.append(f"  ... and {len(self._events) - 10} more")
        return "\n".join(lines)


# ── Helpers ───────────────────────────────────────────────────


def _extract_text(result: Any) -> str:
    """Pull text from a tool-result dict ``{"content": [{"type":"text","text":"..."}]}``."""
    if isinstance(result, dict) and "content" in result:
        parts = [
            b.get("text", "")
            for b in result.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(result)
