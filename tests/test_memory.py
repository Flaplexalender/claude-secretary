"""Tests for the memory system — all offline, no API calls."""
import json
from pathlib import Path

from secretary.memory import MemoryStore, _normalize_long_entry, _is_similar


def test_add_short(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_short("Task: hello")
    assert mem.short == ["Task: hello"]


def test_short_trimming(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json", short_max=3)
    for i in range(5):
        mem.add_short(f"Task: {i}")
    assert len(mem.short) == 3
    assert mem.short[0] == "Task: 2"


def test_add_long_dedup(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("Always use type hints")
    mem.add_long("Always use type hints")  # exact dup
    assert len(mem.long) == 1


def test_add_long_fuzzy_dedup(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("Always use type hints in Python functions")
    mem.add_long("Always use type hints in Python function")  # fuzzy dup
    assert len(mem.long) == 1


def test_add_long_different(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("Always use type hints")
    mem.add_long("Never use global state")  # different
    assert len(mem.long) == 2


def test_consolidate_promotes_patterns(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    # Add recurring pattern (3+ similar tasks)
    for i in range(4):
        mem.add_short(f"Task: Fix the linting errors in module {i}")
    mem.consolidate()
    assert len(mem.long) == 1
    assert "Recurring pattern" in mem.long[0]


def test_consolidate_ignores_sparse(tmp_path: Path):
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_short("Task: Fix linting")
    mem.add_short("Task: Deploy server")
    mem.add_short("Task: Write docs")
    mem.consolidate()
    assert len(mem.long) == 0  # No recurring pattern


def test_save_and_load(tmp_path: Path):
    path = tmp_path / "mem.json"
    mem = MemoryStore(path=path)
    mem.add_short("Task: hello")
    mem.add_long("Lesson learned")
    mem.save()

    loaded = MemoryStore.load(path)
    assert loaded.short == ["Task: hello"]
    assert loaded.long == ["Lesson learned"]


def test_load_nonexistent(tmp_path: Path):
    mem = MemoryStore.load(tmp_path / "nope.json")
    assert mem.short == []
    assert mem.long == []


def test_load_corrupted_json(tmp_path: Path):
    """Corrupted JSON file should return empty memory instead of crashing."""
    path = tmp_path / "mem.json"
    path.write_text("{invalid json content!!!", encoding="utf-8")
    mem = MemoryStore.load(path)
    assert mem.short == []
    assert mem.long == []


# --- Adaptive Memory Decay Tests ---


def test_decay_prunes_old_unaccessed(tmp_path: Path):
    """Entries older than 14 days with access_count < 2 should be pruned."""
    from datetime import datetime, timezone, timedelta

    mem = MemoryStore(path=tmp_path / "mem.json")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    mem._long_entries = [
        {"text": "old unaccessed", "ts": old_ts, "access_count": 0},
        {"text": "old accessed once", "ts": old_ts, "access_count": 1},
    ]
    mem.consolidate()
    texts = [e["text"] for e in mem._long_entries]
    assert "old unaccessed" not in texts
    assert "old accessed once" not in texts  # access_count 1 < 2, also pruned


def test_decay_keeps_old_frequently_accessed(tmp_path: Path):
    """Old entries with access_count >= 2 should survive decay."""
    from datetime import datetime, timezone, timedelta

    mem = MemoryStore(path=tmp_path / "mem.json")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    mem._long_entries = [
        {"text": "old but important", "ts": old_ts, "access_count": 5},
    ]
    mem.consolidate()
    assert len(mem._long_entries) == 1
    assert mem._long_entries[0]["text"] == "old but important"


def test_access_count_increment(tmp_path: Path):
    """access_long(idx) should increment the access_count."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("test entry")
    assert mem._long_entries[0]["access_count"] == 0
    mem.access_long(0)
    assert mem._long_entries[0]["access_count"] == 1
    mem.access_long(0)
    assert mem._long_entries[0]["access_count"] == 2


def test_backward_compat_load_plain_strings(tmp_path: Path):
    """Loading old memory.json with plain string long entries should work."""
    path = tmp_path / "mem.json"
    old_data = {"short": ["hi"], "long": ["lesson 1", "lesson 2"]}
    path.write_text(json.dumps(old_data), encoding="utf-8")
    mem = MemoryStore.load(path)
    assert mem.get_long() == ["lesson 1", "lesson 2"]
    # Internal entries should be normalized to dicts
    assert all(isinstance(e, dict) for e in mem._long_entries)
    assert all("ts" in e and "access_count" in e for e in mem._long_entries)


def test_naive_timezone_entries_handled(tmp_path: Path):
    """Entries with naive (no tz) timestamps should still be pruned correctly."""
    from datetime import datetime, timedelta, UTC

    mem = MemoryStore(path=tmp_path / "mem.json")
    # Naive ISO timestamp (no +00:00 suffix) — simulates migrated data
    old_naive = (datetime.now(UTC) - timedelta(days=20)).replace(tzinfo=None).isoformat()
    mem._long_entries = [
        {"text": "naive old entry", "ts": old_naive, "access_count": 0},
    ]
    mem.consolidate()
    # Should be pruned (old + unaccessed), not crash on comparison
    assert len(mem._long_entries) == 0


def test_no_prune_when_recently_added(tmp_path: Path):
    """Entries added today should never be pruned regardless of access_count."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("fresh entry")
    mem.consolidate()
    assert mem.get_long() == ["fresh entry"]


def test_save_preserves_rich_entries(tmp_path: Path):
    """Save/load round-trip should preserve ts and access_count."""
    path = tmp_path / "mem.json"
    mem = MemoryStore(path=path)
    mem.add_long("entry 1")
    mem.access_long(0)
    mem.access_long(0)
    mem.save()

    loaded = MemoryStore.load(path)
    assert loaded.get_long() == ["entry 1"]
    assert loaded._long_entries[0]["access_count"] == 2
    assert "ts" in loaded._long_entries[0]


def test_consolidate_is_idempotent(tmp_path: Path):
    """Calling consolidate() twice should not duplicate entries."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    for i in range(6):
        mem.add_short(f"Task: linting module {i}")

    mem.consolidate()
    count_after_first = len(mem.get_long())

    mem.consolidate()
    count_after_second = len(mem.get_long())

    assert count_after_second == count_after_first


# ── Cycle 7: Additional coverage ──────────────────────────────


def test_long_max_trimming(tmp_path: Path):
    """Adding more than long_max entries should trim the oldest."""
    mem = MemoryStore(path=tmp_path / "mem.json", long_max=3)
    # Use very distinct entries to avoid fuzzy dedup
    mem.add_long("unique alpha 111")
    mem.add_long("unique bravo 222")
    mem.add_long("unique charlie 333")
    mem.add_long("unique delta 444")
    assert len(mem.get_long()) == 3
    # Oldest should be trimmed
    assert "alpha" not in mem.get_long()[0]
    assert "delta" in mem.get_long()[-1]


def test_access_long_out_of_bounds(tmp_path: Path):
    """access_long with invalid index should not crash."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("only entry")
    # These should silently do nothing
    mem.access_long(-1)
    mem.access_long(5)
    mem.access_long(100)
    assert mem._long_entries[0]["access_count"] == 0


def test_long_setter_with_strings(tmp_path: Path):
    """Setting .long with a list of strings should normalize to dicts."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.long = ["plain text entry", "another plain entry"]
    assert len(mem._long_entries) == 2
    assert all(isinstance(e, dict) for e in mem._long_entries)
    assert mem._long_entries[0]["text"] == "plain text entry"
    assert mem._long_entries[0]["access_count"] == 0
    assert "ts" in mem._long_entries[0]


def test_long_setter_with_dicts(tmp_path: Path):
    """Setting .long with dict entries should preserve them."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.long = [{"text": "existing", "ts": "2026-01-01T00:00:00Z", "access_count": 5}]
    assert mem._long_entries[0]["text"] == "existing"
    assert mem._long_entries[0]["access_count"] == 5


def test_normalize_long_entry_non_standard_type():
    """_normalize_long_entry should handle non-dict/non-str values."""
    result = _normalize_long_entry(42)
    assert result["text"] == "42"
    assert result["access_count"] == 0

    result2 = _normalize_long_entry(["a", "list"])
    assert result2["text"] == "['a', 'list']"


def test_normalize_long_entry_dict_missing_ts():
    """Dict entries missing ts/access_count get defaults."""
    result = _normalize_long_entry({"text": "hello"})
    assert result["text"] == "hello"
    assert "ts" in result
    assert result["access_count"] == 0


def test_is_similar_exact_match():
    assert _is_similar("hello world", "hello world") is True


def test_is_similar_case_insensitive():
    assert _is_similar("Hello World", "hello world") is True


def test_is_similar_different_strings():
    assert _is_similar("hello", "completely different") is False


def test_get_short_returns_copy(tmp_path: Path):
    """get_short() should return a copy, not a reference."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_short("entry")
    result = mem.get_short()
    result.append("mutated")
    assert len(mem.short) == 1  # Original unchanged


def test_get_long_returns_text_list(tmp_path: Path):
    """get_long() returns text-only list, not dicts."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("entry one")
    mem.add_long("entry two is different")
    result = mem.get_long()
    assert all(isinstance(s, str) for s in result)
    assert result == ["entry one", "entry two is different"]


def test_save_creates_parent_dirs(tmp_path: Path):
    """save() should create parent directories if needed."""
    path = tmp_path / "deep" / "nested" / "mem.json"
    mem = MemoryStore(path=path)
    mem.add_short("test")
    mem.save()
    assert path.exists()


def test_consolidate_with_no_task_entries(tmp_path: Path):
    """consolidate() with non-Task short entries should not crash."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_short("Error: something broke")
    mem.add_short("Random note")
    mem.add_short("Another note")
    mem.consolidate()
    assert len(mem.get_long()) == 0


def test_consolidate_deduplicates_long_entries(tmp_path: Path):
    """consolidate() should deduplicate similar long entries."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    mem = MemoryStore(path=tmp_path / "mem.json")
    # Bypass add_long dedup by inserting directly; use recent timestamps to avoid decay pruning
    mem._long_entries = [
        {"text": "Always use type hints in functions", "ts": now, "access_count": 0},
        {"text": "Always use type hints in function", "ts": now, "access_count": 0},
    ]
    mem.consolidate()
    assert len(mem._long_entries) == 1


def test_load_corrupted_unicode(tmp_path: Path):
    """Binary/undecodable file should return fresh memory."""
    path = tmp_path / "mem.json"
    path.write_bytes(b"\xff\xfe invalid unicode")
    mem = MemoryStore.load(path)
    assert mem.short == []
    assert mem.long == []


def test_consolidate_decay_with_missing_ts(tmp_path: Path):
    """Entries missing 'ts' key should survive decay (not crash)."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem._long_entries = [
        {"text": "no timestamp entry", "access_count": 0},
    ]
    mem.consolidate()
    # Should survive because decay can't determine age
    assert len(mem._long_entries) == 1


def test_consolidate_decay_with_invalid_ts(tmp_path: Path):
    """Entries with unparseable timestamps should survive decay."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem._long_entries = [
        {"text": "bad timestamp entry", "ts": "not-a-date", "access_count": 0},
    ]
    mem.consolidate()
    assert len(mem._long_entries) == 1
