"""Unit tests for agent module — pure functions only (no SDK calls)."""
from pathlib import Path

from secretary.agent import _build_system_prompt, _ensure_env, RunResult
from secretary.config import SecretaryConfig
from secretary.memory import MemoryStore


def test_build_system_prompt_empty_memory(tmp_path: Path):
    """System prompt without any memory — fallback (no workspace)."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    prompt = _build_system_prompt(mem, workspace_dir=str(tmp_path / "no_workspace"))
    assert "research assistant" in prompt
    assert "Long-term memory" not in prompt
    assert "Recent context" not in prompt


def test_build_system_prompt_with_workspace(tmp_path: Path):
    """System prompt uses workspace identity files when available."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "IDENTITY.md").write_text("# Identity\nName: TestBot", encoding="utf-8")
    (ws / "SOUL.md").write_text("# Soul\nBe concise.", encoding="utf-8")
    mem = MemoryStore(path=tmp_path / "mem.json")
    prompt = _build_system_prompt(mem, workspace_dir=str(ws))
    assert "TestBot" in prompt
    assert "Be concise" in prompt
    assert "research assistant" not in prompt


def test_build_system_prompt_with_long_memory(tmp_path: Path):
    """System prompt includes long-term entries."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("important fact")
    prompt = _build_system_prompt(mem, workspace_dir=str(tmp_path / "no_workspace"))
    assert "Long-term memory" in prompt
    assert "important fact" in prompt


def test_build_system_prompt_with_short_memory(tmp_path: Path):
    """System prompt includes recent context."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_short("just happened")
    prompt = _build_system_prompt(mem, workspace_dir=str(tmp_path / "no_workspace"))
    assert "Recent context" in prompt
    assert "just happened" in prompt


def test_build_system_prompt_limits_long_to_10(tmp_path: Path):
    """Only the last 10 long-term entries appear."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    # Use very distinct entries to avoid fuzzy dedup
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
             "golf", "hotel", "india", "juliet", "kilo", "lima",
             "mike", "november", "oscar"]
    for w in words:
        mem.add_long(f"phonetic: {w}")
    prompt = _build_system_prompt(mem, workspace_dir=str(tmp_path / "no_workspace"))
    assert "phonetic: oscar" in prompt     # last entry
    assert "phonetic: foxtrot" in prompt   # 10th from end
    assert "phonetic: echo" not in prompt  # 11th from end = excluded


def test_build_system_prompt_limits_short_to_5(tmp_path: Path):
    """Only the last 5 short-term entries appear."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
    for w in words:
        mem.add_short(f"short: {w}")
    prompt = _build_system_prompt(mem)
    assert "short: hotel" in prompt    # last entry
    assert "short: delta" in prompt    # 5th from end
    assert "short: charlie" not in prompt  # 6th from end = excluded


def test_ensure_env_sets_proxy(tmp_path: Path, monkeypatch):
    """_ensure_env should set ANTHROPIC_BASE_URL and API_KEY if unset."""
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    _ensure_env(config)
    import os
    assert os.environ["ANTHROPIC_BASE_URL"] == config.anthropic_base_url
    assert os.environ["ANTHROPIC_API_KEY"] == "copilot-proxy"


def test_ensure_env_does_not_overwrite(tmp_path: Path, monkeypatch):
    """_ensure_env should not overwrite existing env vars."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://custom:1234")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "real-key")
    config = SecretaryConfig(data_root=str(tmp_path / "data"))
    _ensure_env(config)
    import os
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://custom:1234"
    assert os.environ["ANTHROPIC_API_KEY"] == "real-key"


def test_run_result_defaults():
    """RunResult should have sensible defaults."""
    r = RunResult(task="test", routing=None)  # type: ignore[arg-type]
    assert r.text == ""
    assert r.error is None
    assert r.cost_usd == 0.0
    assert r.num_turns == 0
    assert r.tools_used == []
    assert r.messages == []


# ── Cycle 7: Additional coverage ──────────────────────────────


def test_build_system_prompt_includes_action_directive(tmp_path: Path):
    """System prompt should include directive to take action."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    prompt = _build_system_prompt(mem)
    assert "Take action" in prompt or "take action" in prompt.lower()


def test_build_system_prompt_includes_concise_directive(tmp_path: Path):
    """System prompt should tell the agent to be concise."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    prompt = _build_system_prompt(mem)
    assert "concise" in prompt.lower()


def test_build_system_prompt_both_memories(tmp_path: Path):
    """System prompt should include both memory types when present."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("persistent knowledge")
    mem.add_short("recent event")
    prompt = _build_system_prompt(mem)
    assert "Long-term memory" in prompt
    assert "Recent context" in prompt
    assert "persistent knowledge" in prompt
    assert "recent event" in prompt


def test_build_system_prompt_access_tracking(tmp_path: Path):
    """Building system prompt should increment access counts for included long entries."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("tracked entry")
    initial_count = mem._long_entries[0]["access_count"]
    _build_system_prompt(mem)
    assert mem._long_entries[0]["access_count"] == initial_count + 1


def test_build_system_prompt_access_tracking_multiple(tmp_path: Path):
    """Multiple system prompt builds should increment access each time."""
    mem = MemoryStore(path=tmp_path / "mem.json")
    mem.add_long("multi-access entry")
    _build_system_prompt(mem)
    _build_system_prompt(mem)
    _build_system_prompt(mem)
    assert mem._long_entries[0]["access_count"] == 3


def test_run_result_independent_lists():
    """Each RunResult should have independent list instances."""
    r1 = RunResult(task="a", routing=None)  # type: ignore[arg-type]
    r2 = RunResult(task="b", routing=None)  # type: ignore[arg-type]
    r1.tools_used.append("tool1")
    assert r2.tools_used == []  # Should be independent


def test_run_result_session_id_default():
    """RunResult.session_id should default to empty string."""
    r = RunResult(task="test", routing=None)  # type: ignore[arg-type]
    assert r.session_id == ""


def test_run_result_duration_ms_default():
    """RunResult.duration_ms should default to 0."""
    r = RunResult(task="test", routing=None)  # type: ignore[arg-type]
    assert r.duration_ms == 0
