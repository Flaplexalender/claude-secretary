"""Tests for event_bus — Event, EventBus, sources, and trigger matching."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from secretary.event_bus import (
    CalendarEventSource,
    Event,
    EventBus,
    EventType,
    FileChangeSource,
    GmailEventSource,
    _extract_text,
)


# ── Event dataclass ───────────────────────────────────────────


def test_event_creation():
    ev = Event(type=EventType.NEW_EMAIL, source="gmail", payload={"subject": "hi"})
    assert ev.type == "new_email"
    assert ev.source == "gmail"
    assert ev.payload["subject"] == "hi"
    assert ev.timestamp > 0


def test_event_is_frozen():
    ev = Event(type=EventType.CYCLE_START)
    with pytest.raises(AttributeError):
        ev.type = "other"  # type: ignore[misc]


def test_event_summary_new_email():
    ev = Event(type=EventType.NEW_EMAIL, payload={"from": "alice@x.com", "subject": "Hello"})
    assert "alice@x.com" in ev.summary
    assert "Hello" in ev.summary


def test_event_summary_calendar():
    ev = Event(type=EventType.CALENDAR_SOON, payload={"title": "Standup", "minutes_until": 15})
    assert "15m" in ev.summary
    assert "Standup" in ev.summary


def test_event_summary_file_changed():
    ev = Event(type=EventType.FILE_CHANGED, payload={"path": "/tmp/notes.md"})
    assert "/tmp/notes.md" in ev.summary


def test_event_summary_unknown_type():
    ev = Event(type="custom_thing", payload={"k": "v"})
    assert "custom_thing" in ev.summary


# ── EventBus ──────────────────────────────────────────────────


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


def test_bus_emit_and_events(bus: EventBus):
    ev = Event(type=EventType.CYCLE_START, source="test")
    bus.emit(ev)
    assert len(bus.events) == 1
    assert bus.events[0].type == EventType.CYCLE_START


def test_bus_dedup(bus: EventBus):
    ev1 = Event(type=EventType.NEW_EMAIL, dedup_key="mail:abc")
    ev2 = Event(type=EventType.NEW_EMAIL, dedup_key="mail:abc")
    bus.emit(ev1)
    bus.emit(ev2)
    assert len(bus.events) == 1


def test_bus_no_dedup_without_key(bus: EventBus):
    ev1 = Event(type=EventType.CYCLE_START)
    ev2 = Event(type=EventType.CYCLE_START)
    bus.emit(ev1)
    bus.emit(ev2)
    assert len(bus.events) == 2


def test_bus_event_context_empty(bus: EventBus):
    assert bus.event_context() == ""


def test_bus_event_context_with_events(bus: EventBus):
    bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "bob", "subject": "Test"}))
    ctx = bus.event_context()
    assert "1" in ctx
    assert "bob" in ctx


# ── Trigger matching ──────────────────────────────────────────


def test_matches_trigger_basic(bus: EventBus):
    bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "alice@x.com"}))
    assert len(bus.matches_trigger("event:new_email")) == 1


def test_matches_trigger_no_match(bus: EventBus):
    bus.emit(Event(type=EventType.CALENDAR_SOON))
    assert bus.matches_trigger("event:new_email") == []


def test_matches_trigger_with_filter(bus: EventBus):
    bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "boss@company.com"}))
    bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "spam@junk.com"}))
    matches = bus.matches_trigger("event:new_email:from=boss@")
    assert len(matches) == 1
    assert matches[0].payload["from"] == "boss@company.com"


def test_matches_trigger_filter_case_insensitive(bus: EventBus):
    bus.emit(Event(type=EventType.NEW_EMAIL, payload={"from": "Boss@Company.COM"}))
    matches = bus.matches_trigger("event:new_email:from=boss@")
    assert len(matches) == 1


def test_matches_trigger_empty_string(bus: EventBus):
    bus.emit(Event(type=EventType.NEW_EMAIL))
    assert bus.matches_trigger("") == []


def test_matches_trigger_no_event_prefix(bus: EventBus):
    bus.emit(Event(type=EventType.NEW_EMAIL))
    assert bus.matches_trigger("new_email") == []


def test_matches_trigger_task_completed(bus: EventBus):
    bus.emit(Event(type=EventType.TASK_COMPLETED, payload={"task_id": "email_triage"}))
    matches = bus.matches_trigger("event:task_completed:task_id=email_triage")
    assert len(matches) == 1


# ── poll_all ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_all_collects_from_sources(bus: EventBus):
    class FakeSource:
        async def poll(self):
            return [Event(type=EventType.FILE_CHANGED, payload={"path": "/a"})]

    bus.add_source(FakeSource())
    events = await bus.poll_all()
    assert len(events) == 1
    assert events[0].type == EventType.FILE_CHANGED


@pytest.mark.asyncio
async def test_poll_all_dedup_across_sources(bus: EventBus):
    class Source1:
        async def poll(self):
            return [Event(type=EventType.NEW_EMAIL, dedup_key="dup1")]
    class Source2:
        async def poll(self):
            return [Event(type=EventType.NEW_EMAIL, dedup_key="dup1")]

    bus.add_source(Source1())
    bus.add_source(Source2())
    events = await bus.poll_all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_poll_all_clears_previous_cycle(bus: EventBus):
    class OnceSource:
        def __init__(self):
            self.called = False
        async def poll(self):
            if not self.called:
                self.called = True
                return [Event(type=EventType.CYCLE_START)]
            return []

    src = OnceSource()
    bus.add_source(src)
    events1 = await bus.poll_all()
    assert len(events1) == 1
    events2 = await bus.poll_all()
    assert len(events2) == 0


@pytest.mark.asyncio
async def test_poll_all_handles_source_error(bus: EventBus):
    class BrokenSource:
        async def poll(self):
            raise RuntimeError("oops")

    class GoodSource:
        async def poll(self):
            return [Event(type=EventType.CYCLE_START)]

    bus.add_source(BrokenSource())
    bus.add_source(GoodSource())
    events = await bus.poll_all()
    assert len(events) == 1  # broken source skipped, good source collected


# ── GmailEventSource ─────────────────────────────────────────


def _make_tool_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


@pytest.mark.asyncio
async def test_gmail_source_first_poll_no_events():
    func = AsyncMock(return_value=_make_tool_result("Message 1: hello"))
    tools = {"gmail_search": {"func": func}}
    src = GmailEventSource(tools)
    events = await src.poll()
    assert events == []  # first poll seeds, no events


@pytest.mark.asyncio
async def test_gmail_source_detects_change():
    call_count = 0
    async def fake_func(args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_tool_result("Message 1: hello")
        return _make_tool_result("Message 1: hello\nMessage 2: world")

    tools = {"gmail_search": {"func": fake_func}}
    src = GmailEventSource(tools)
    await src.poll()  # seed
    events = await src.poll()  # detect change
    assert len(events) == 1
    assert events[0].type == EventType.NEW_EMAIL


@pytest.mark.asyncio
async def test_gmail_source_no_change():
    func = AsyncMock(return_value=_make_tool_result("Message 1: hello"))
    tools = {"gmail_search": {"func": func}}
    src = GmailEventSource(tools)
    await src.poll()
    events = await src.poll()
    assert events == []


@pytest.mark.asyncio
async def test_gmail_source_missing_tool():
    src = GmailEventSource({})
    assert await src.poll() == []


@pytest.mark.asyncio
async def test_gmail_source_error_handling():
    func = AsyncMock(side_effect=RuntimeError("auth failed"))
    tools = {"gmail_search": {"func": func}}
    src = GmailEventSource(tools)
    events = await src.poll()
    assert events == []  # error suppressed


# ── CalendarEventSource ───────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_source_first_poll_no_events():
    func = AsyncMock(return_value=_make_tool_result("Meeting at 10am"))
    tools = {"calendar_today": {"func": func}}
    src = CalendarEventSource(tools)
    events = await src.poll()
    assert events == []


@pytest.mark.asyncio
async def test_calendar_source_detects_change():
    call_count = 0
    async def fake_func(args):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_tool_result("Meeting at 10am")
        return _make_tool_result("Meeting at 10am\nLunch at 12pm")

    tools = {"calendar_today": {"func": fake_func}}
    src = CalendarEventSource(tools)
    await src.poll()
    events = await src.poll()
    assert len(events) == 1
    assert events[0].type == EventType.CALENDAR_SOON


@pytest.mark.asyncio
async def test_calendar_source_missing_tool():
    src = CalendarEventSource({})
    assert await src.poll() == []


# ── FileChangeSource ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_source_first_poll_no_events(tmp_path: Path):
    f = tmp_path / "watched.txt"
    f.write_text("initial")
    src = FileChangeSource([f])
    events = await src.poll()
    assert events == []  # first poll seeds


@pytest.mark.asyncio
async def test_file_source_detects_modification(tmp_path: Path):
    f = tmp_path / "watched.txt"
    f.write_text("v1")
    src = FileChangeSource([f])
    await src.poll()  # seed

    # Ensure mtime advances (some FS have 1-second resolution)
    time.sleep(0.05)
    f.write_text("v2")

    events = await src.poll()
    assert len(events) == 1
    assert events[0].type == EventType.FILE_CHANGED
    assert str(f) in events[0].payload["path"]


@pytest.mark.asyncio
async def test_file_source_no_change(tmp_path: Path):
    f = tmp_path / "watched.txt"
    f.write_text("static")
    src = FileChangeSource([f])
    await src.poll()
    events = await src.poll()
    assert events == []


@pytest.mark.asyncio
async def test_file_source_missing_file(tmp_path: Path):
    f = tmp_path / "nonexistent.txt"
    src = FileChangeSource([f])
    events = await src.poll()
    assert events == []


@pytest.mark.asyncio
async def test_file_source_multiple_files(tmp_path: Path):
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("a")
    f2.write_text("b")
    src = FileChangeSource([f1, f2])
    await src.poll()

    time.sleep(0.05)
    f1.write_text("a-modified")
    f2.write_text("b-modified")

    events = await src.poll()
    assert len(events) == 2


# ── _extract_text helper ─────────────────────────────────────


def test_extract_text_from_tool_result():
    result = {"content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]}
    assert _extract_text(result) == "hello\nworld"


def test_extract_text_non_dict():
    assert _extract_text("plain string") == "plain string"


def test_extract_text_empty_content():
    assert _extract_text({"content": []}) == ""


# ── Integration: EventBus with sources ────────────────────────


@pytest.mark.asyncio
async def test_bus_with_file_source(tmp_path: Path):
    f = tmp_path / "target.md"
    f.write_text("v1")

    bus = EventBus()
    bus.add_source(FileChangeSource([f]))

    # First poll: seed
    await bus.poll_all()
    assert bus.events == []

    # Modify file
    time.sleep(0.05)
    f.write_text("v2")

    events = await bus.poll_all()
    assert len(events) == 1
    assert bus.matches_trigger("event:file_changed") == events


# ── Watcher trigger integration (unit level) ─────────────────


def test_event_config_defaults():
    from secretary.config import EventConfig
    cfg = EventConfig()
    assert cfg.enabled is False
    assert cfg.gmail_source is True
    assert cfg.calendar_source is True
    assert cfg.calendar_window_minutes == 30
    assert cfg.watch_files == []


def test_event_config_in_secretary_config():
    from secretary.config import SecretaryConfig
    cfg = SecretaryConfig(events={"enabled": True, "watch_files": ["/tmp/a.txt"]})
    assert cfg.events.enabled is True
    assert cfg.events.watch_files == ["/tmp/a.txt"]


def test_watcher_event_bus_disabled_by_default(tmp_path: Path):
    from secretary.config import SecretaryConfig
    from secretary.watcher import Watcher
    cfg = SecretaryConfig(data_root=str(tmp_path / "data"))
    w = Watcher(config=cfg)
    assert w._event_bus is None


def test_watcher_event_bus_enabled(tmp_path: Path):
    from secretary.config import SecretaryConfig
    from secretary.watcher import Watcher
    cfg = SecretaryConfig(data_root=str(tmp_path / "data"), events={"enabled": True})
    w = Watcher(config=cfg)
    assert w._event_bus is not None
