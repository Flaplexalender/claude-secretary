"""JSON-backed memory with fuzzy deduplication and adaptive decay.

Adapted from Captain v2's memory system.
Short-term: recent task summaries (auto-trimmed).
Long-term: persistent learnings (deduplicated, with recency decay).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


_SIMILARITY_THRESHOLD = 0.85
_PATTERN_THRESHOLD = 0.6
_PATTERN_MIN_COUNT = 3
_DECAY_DAYS = 14
_DECAY_MIN_ACCESSES = 2


def _is_similar(a: str, b: str, threshold: float = _SIMILARITY_THRESHOLD) -> bool:
    """Return True if strings a and b are similar above the threshold (0-1)."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _normalize_long_entry(entry: Any) -> dict:
    """Convert a long entry to the rich dict format. Handles backward compat."""
    if isinstance(entry, str):
        return {"text": entry, "ts": _now_iso(), "access_count": 0}
    if isinstance(entry, dict) and "text" in entry:
        entry.setdefault("ts", _now_iso())
        entry.setdefault("access_count", 0)
        return entry
    return {"text": str(entry), "ts": _now_iso(), "access_count": 0}


@dataclass
class MemoryStore:
    """JSON-backed memory store with short-term and long-term entries.

    Short-term: recent task summaries, auto-trimmed to ``short_max``.
    Long-term: persistent learnings with fuzzy deduplication and recency decay.
    """
    short: list[str] = field(default_factory=list)
    _long_entries: list[dict] = field(default_factory=list)
    path: Path = field(default_factory=lambda: Path("data/memory.json"))
    short_max: int = 20
    long_max: int = 50

    @property
    def long(self) -> list[str]:
        """Text-only view for backward compatibility."""
        return [e["text"] for e in self._long_entries]

    @long.setter
    def long(self, value: list) -> None:
        """Accept both str list and dict list for backward compat."""
        self._long_entries = [_normalize_long_entry(v) for v in value]

    def add_short(self, entry: str) -> None:
        """Add an entry to short-term memory, trimming to short_max."""
        self.short.append(entry)
        if len(self.short) > self.short_max:
            self.short = self.short[-self.short_max:]

    def add_long(self, entry: str) -> None:
        """Add an entry to long-term memory, deduplicating similar entries."""
        # Deduplicate against existing long-term entries
        for existing in self._long_entries:
            if _is_similar(entry, existing["text"]):
                return
        self._long_entries.append({
            "text": entry,
            "ts": _now_iso(),
            "access_count": 0,
        })
        if len(self._long_entries) > self.long_max:
            self._long_entries = self._long_entries[-self.long_max:]

    def get_short(self) -> list[str]:
        """Return a copy of the short-term memory entries."""
        return list(self.short)

    def get_long(self) -> list[str]:
        """Return text-only list for API compatibility."""
        return [e["text"] for e in self._long_entries]

    def access_long(self, idx: int) -> None:
        """Increment access_count for a long-term entry by index."""
        if 0 <= idx < len(self._long_entries):
            self._long_entries[idx]["access_count"] += 1

    def consolidate(self) -> None:
        """Find recurring patterns in short-term, promote to long-term.
        Also prune decayed long-term entries (old + rarely accessed).
        """
        tasks: list[str] = []
        for entry in self.short:
            m = re.match(r"Task:\s*(.+)", entry)
            if m:
                tasks.append(m.group(1).strip())

        if len(tasks) >= _PATTERN_MIN_COUNT:
            # Group similar tasks
            groups: list[list[str]] = []
            used: set[int] = set()
            for i, t in enumerate(tasks):
                if i in used:
                    continue
                group = [t]
                used.add(i)
                for j in range(i + 1, len(tasks)):
                    if j not in used and _is_similar(t, tasks[j], _PATTERN_THRESHOLD):
                        group.append(tasks[j])
                        used.add(j)
                groups.append(group)

            # Promote groups with enough occurrences
            for group in groups:
                if len(group) >= _PATTERN_MIN_COUNT:
                    pattern = f"Recurring pattern ({len(group)}x): {group[0]}"
                    self.add_long(pattern)

        # Deduplicate long-term (backwards, keep newest) — before decay
        seen: list[str] = []
        deduped: list[dict] = []
        for entry in reversed(self._long_entries):
            is_dup = any(_is_similar(entry["text"], s) for s in seen)
            if not is_dup:
                seen.append(entry["text"])
                deduped.append(entry)
        self._long_entries = list(reversed(deduped))

        # Decay pruning: remove old, rarely-accessed entries
        cutoff = datetime.now(timezone.utc) - timedelta(days=_DECAY_DAYS)
        surviving: list[dict] = []
        for entry in self._long_entries:
            try:
                ts = datetime.fromisoformat(entry["ts"])
                # Handle naive timestamps from migrated entries
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                surviving.append(entry)
                continue
            if ts < cutoff and entry.get("access_count", 0) < _DECAY_MIN_ACCESSES:
                log.debug("Pruning decayed memory: %s", entry["text"][:50])
                continue
            surviving.append(entry)
        self._long_entries = surviving

    def save(self) -> None:
        """Persist memory store to disk via atomic write (temp file + rename)."""
        import os
        import tempfile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"short": self.short, "long": self._long_entries}
        content = json.dumps(data, indent=2)
        # Atomic write: temp file + rename → no partial write on crash
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, suffix=".tmp", prefix=".memory_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            Path(tmp_path).replace(self.path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    @classmethod
    def load(cls, path: Path | str = "data/memory.json") -> MemoryStore:
        """Load memory store from a JSON file, or return an empty store if missing/corrupted."""
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                log.warning("Corrupted memory file %s: %s — starting fresh", p, e)
                return cls(path=p)
            store = cls(
                short=data.get("short", []),
                path=p,
            )
            # Backward-compatible load: long entries can be plain strings or dicts
            raw_long = data.get("long", [])
            store._long_entries = [_normalize_long_entry(e) for e in raw_long]
            return store
        return cls(path=p)


# ── Markdown Memory (OpenClaw pattern) ───────────────────────────────────


class MarkdownMemory:
    """File-based memory: daily logs + curated MEMORY.md.

    Sits alongside MemoryStore (JSON) as a parallel system.
    Daily logs: ``workspace/memory/YYYY-MM-DD.md``
    Long-term: ``workspace/MEMORY.md``

    Does NOT replace MemoryStore — both run in parallel until migration is done.
    """

    def __init__(self, workspace_dir: Path | str):
        self.workspace_dir = Path(workspace_dir)
        self.memory_dir = self.workspace_dir / "memory"
        self.memory_md = self.workspace_dir / "MEMORY.md"

    def _today_log_path(self) -> Path:
        return self.memory_dir / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"

    def _yesterday_log_path(self) -> Path:
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        return self.memory_dir / f"{yesterday.strftime('%Y-%m-%d')}.md"

    def append_daily(self, summary: str, task: str = "") -> None:
        """Append a task summary to today's daily log."""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        path = self._today_log_path()
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        header = f"- **{ts}**"
        if task:
            header += f" [{task[:60]}]"
        entry = f"{header}: {summary}\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)

    def read_curated(self) -> str | None:
        """Read MEMORY.md (curated long-term memory)."""
        if self.memory_md.is_file():
            try:
                return self.memory_md.read_text(encoding="utf-8").strip()
            except OSError:
                return None
        return None

    def read_daily(self, days: int = 2) -> str | None:
        """Read recent daily logs (default: today + yesterday)."""
        parts: list[str] = []
        base = datetime.now(timezone.utc)
        for i in range(days):
            date = base - timedelta(days=i)
            path = self.memory_dir / f"{date.strftime('%Y-%m-%d')}.md"
            if path.is_file():
                try:
                    content = path.read_text(encoding="utf-8").strip()
                    if content:
                        parts.append(f"## {date.strftime('%Y-%m-%d')}\n{content}")
                except OSError:
                    continue
        return "\n\n".join(parts) if parts else None

    def get_context(self) -> str | None:
        """Get full memory context for system prompt injection.

        Returns MEMORY.md + recent daily logs combined.
        """
        sections: list[str] = []
        curated = self.read_curated()
        if curated:
            sections.append(curated)
        daily = self.read_daily()
        if daily:
            sections.append(f"# Recent Activity\n\n{daily}")
        return "\n\n".join(sections) if sections else None
