"""Tests for CLI commands — config, logs. No API calls."""
from __future__ import annotations

import json
from pathlib import Path

from secretary.config import SecretaryConfig
from secretary.run_log import RunLog, RunLogEntry


# ── secretary config ─────────────────────────────────────────


def test_config_full_dump(capsys):
    """secretary config (no key) prints all config."""
    from secretary.__main__ import _cmd_config
    import argparse

    config = SecretaryConfig()
    args = argparse.Namespace(key=None)
    _cmd_config(args, config)

    out = capsys.readouterr().out
    assert "data_root:" in out
    assert "routing:" in out
    assert "watcher:" in out


def test_config_dotted_key(capsys):
    """secretary config routing.default_tier prints the value."""
    from secretary.__main__ import _cmd_config
    import argparse

    config = SecretaryConfig()
    args = argparse.Namespace(key="routing.default_tier")
    _cmd_config(args, config)

    out = capsys.readouterr().out.strip()
    assert out == "medium"


def test_config_nested_dict(capsys):
    """secretary config routing.tiers prints tier dict."""
    from secretary.__main__ import _cmd_config
    import argparse

    config = SecretaryConfig()
    args = argparse.Namespace(key="routing.tiers")
    _cmd_config(args, config)

    out = capsys.readouterr().out
    assert "low:" in out
    assert "haiku" in out.lower()  # model name may vary (claude-haiku, claude-3-haiku-...)


def test_config_invalid_key(capsys):
    """secretary config bad.key exits with error."""
    from secretary.__main__ import _cmd_config
    import argparse
    import pytest

    config = SecretaryConfig()
    args = argparse.Namespace(key="nonexistent.key")
    with pytest.raises(SystemExit):
        _cmd_config(args, config)


# ── secretary logs ───────────────────────────────────────────


def _make_entries(tmp_path: Path, n: int = 5) -> RunLog:
    """Create a run log with N sample entries."""
    log = RunLog(tmp_path / "run_log.jsonl")
    for i in range(n):
        log.append(RunLogEntry(
            timestamp=RunLog.now(),
            cycle=0,
            task=f"Test task {i}",
            tier="low" if i % 2 == 0 else "medium",
            model="claude-3-haiku-20240307" if i % 2 == 0 else "claude-3-5-sonnet-20241022",
            success=i != 2,  # task 2 fails
            output_preview="ok",
            error="some error" if i == 2 else None,
            duration_s=1.0,
            premium_cost=0.33 if i % 2 == 0 else 1.0,
            cost_usd=0.001,
        ))
    return log


def test_logs_table_output(tmp_path: Path, capsys):
    """secretary logs shows a formatted table."""
    from secretary.__main__ import _cmd_logs
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(
        search=None, tier=None, failed=False, last=20, json_output=False,
    )
    _cmd_logs(args, config)

    out = capsys.readouterr().out
    assert "Test task" in out
    assert "5 entries shown" in out


def test_logs_filter_failed(tmp_path: Path, capsys):
    """secretary logs --failed shows only failures."""
    from secretary.__main__ import _cmd_logs
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(
        search=None, tier=None, failed=True, last=20, json_output=False,
    )
    _cmd_logs(args, config)

    out = capsys.readouterr().out
    assert "1 entries shown" in out


def test_logs_filter_tier(tmp_path: Path, capsys):
    """secretary logs --tier low shows only low-tier entries."""
    from secretary.__main__ import _cmd_logs
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(
        search=None, tier="low", failed=False, last=20, json_output=False,
    )
    _cmd_logs(args, config)

    out = capsys.readouterr().out
    assert "3 entries shown" in out


def test_logs_search(tmp_path: Path, capsys):
    """secretary logs --search filters by task text."""
    from secretary.__main__ import _cmd_logs
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(
        search="task 3", tier=None, failed=False, last=20, json_output=False,
    )
    _cmd_logs(args, config)

    out = capsys.readouterr().out
    assert "1 entries shown" in out


def test_logs_json_output(tmp_path: Path, capsys):
    """secretary logs --json outputs valid JSON."""
    from secretary.__main__ import _cmd_logs
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(
        search=None, tier=None, failed=False, last=20, json_output=True,
    )
    _cmd_logs(args, config)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 5
    assert data[0]["task"] == "Test task 0"


def test_logs_empty(tmp_path: Path, capsys):
    """secretary logs with no log file prints message."""
    from secretary.__main__ import _cmd_logs
    import argparse

    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(
        search=None, tier=None, failed=False, last=20, json_output=False,
    )
    _cmd_logs(args, config)

    out = capsys.readouterr().out
    assert "No run logs found" in out


# ── secretary memory ─────────────────────────────────────────


def test_memory_show_empty(tmp_path: Path, capsys):
    """secretary memory show with empty store."""
    from secretary.__main__ import _cmd_memory
    import argparse

    config = SecretaryConfig(data_root=str(tmp_path))
    config.memory.path = str(tmp_path / "memory.json")
    args = argparse.Namespace(action="show")
    _cmd_memory(args, config)

    out = capsys.readouterr().out
    assert "(empty)" in out
    assert "Short-term (0/" in out


def test_memory_show_with_data(tmp_path: Path, capsys):
    """secretary memory show with entries."""
    from secretary.__main__ import _cmd_memory
    from secretary.memory import MemoryStore
    import argparse

    mem_path = tmp_path / "memory.json"
    config = SecretaryConfig(data_root=str(tmp_path))
    config.memory.path = str(mem_path)

    mem = MemoryStore(path=mem_path)
    mem.add_short("Recent task result")
    mem.add_long("Important learned pattern")
    mem.save()

    args = argparse.Namespace(action="show")
    _cmd_memory(args, config)

    out = capsys.readouterr().out
    assert "Recent task result" in out
    assert "Important learned pattern" in out
    assert "Short-term (1/" in out
    assert "Long-term (1/" in out


def test_memory_clear_short(tmp_path: Path, capsys):
    """secretary memory clear-short clears only short-term."""
    from secretary.__main__ import _cmd_memory
    from secretary.memory import MemoryStore
    import argparse

    mem_path = tmp_path / "memory.json"
    config = SecretaryConfig(data_root=str(tmp_path))
    config.memory.path = str(mem_path)

    mem = MemoryStore(path=mem_path)
    mem.add_short("short entry")
    mem.add_long("long entry")
    mem.save()

    args = argparse.Namespace(action="clear-short")
    _cmd_memory(args, config)

    reloaded = MemoryStore.load(mem_path)
    assert reloaded.short == []
    assert reloaded.long == ["long entry"]


def test_memory_clear_all(tmp_path: Path, capsys):
    """secretary memory clear-all clears everything."""
    from secretary.__main__ import _cmd_memory
    from secretary.memory import MemoryStore
    import argparse

    mem_path = tmp_path / "memory.json"
    config = SecretaryConfig(data_root=str(tmp_path))
    config.memory.path = str(mem_path)

    mem = MemoryStore(path=mem_path)
    mem.add_short("short")
    mem.add_long("long")
    mem.save()

    args = argparse.Namespace(action="clear-all")
    _cmd_memory(args, config)

    reloaded = MemoryStore.load(mem_path)
    assert reloaded.short == []
    assert reloaded.long == []


# ── secretary export ─────────────────────────────────────────


def test_export_json(tmp_path: Path, capsys):
    """secretary export json outputs valid JSON."""
    from secretary.__main__ import _cmd_export
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(format="json", output=None, last=0)
    _cmd_export(args, config)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 5


def test_export_csv(tmp_path: Path, capsys):
    """secretary export csv outputs CSV with header."""
    from secretary.__main__ import _cmd_export
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(format="csv", output=None, last=0)
    _cmd_export(args, config)

    out = capsys.readouterr().out
    lines = out.strip().split("\n")
    assert lines[0].startswith("timestamp,")  # CSV header
    assert len(lines) == 6  # header + 5 entries


def test_export_to_file(tmp_path: Path, capsys):
    """secretary export json -o file writes to file."""
    from secretary.__main__ import _cmd_export
    import argparse

    _make_entries(tmp_path)
    config = SecretaryConfig(data_root=str(tmp_path))
    out_file = str(tmp_path / "export.json")
    args = argparse.Namespace(format="json", output=out_file, last=0)
    _cmd_export(args, config)

    assert Path(out_file).exists()
    data = json.loads(Path(out_file).read_text(encoding="utf-8"))
    assert len(data) == 5

    out = capsys.readouterr().out
    assert "Exported 5 entries" in out


def test_export_empty(tmp_path: Path, capsys):
    """secretary export with no logs prints message."""
    from secretary.__main__ import _cmd_export
    import argparse

    config = SecretaryConfig(data_root=str(tmp_path))
    args = argparse.Namespace(format="json", output=None, last=0)
    _cmd_export(args, config)

    out = capsys.readouterr().out
    assert "No run logs to export" in out


# ── secretary estimate ───────────────────────────────────────


def test_estimate_simple_task(capsys):
    """secretary estimate shows routing info without executing."""
    from secretary.__main__ import _cmd_estimate
    import argparse

    config = SecretaryConfig()
    args = argparse.Namespace(task=["what", "is", "2+2"], tier=None)
    _cmd_estimate(args, config)

    out = capsys.readouterr().out
    assert "Task: what is 2+2" in out
    assert "Complexity:" in out
    assert "Model:" in out
    assert "Premium cost:" in out


def test_estimate_forced_tier(capsys):
    """secretary estimate --tier high forces high tier."""
    from secretary.__main__ import _cmd_estimate
    import argparse

    config = SecretaryConfig()
    args = argparse.Namespace(task=["refactor", "the", "entire", "codebase"], tier="high")
    _cmd_estimate(args, config)

    out = capsys.readouterr().out
    assert "high" in out.lower()
    assert "3.0x" in out


# ── --max-turns parser arg ───────────────────────────────────


def test_max_turns_run_parser():
    """--max-turns is parsed correctly on the run subcommand."""
    from secretary.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["run", "do something", "--max-turns", "5"])
    assert args.max_turns == 5
    assert args.command == "run"


def test_max_turns_run_parser_default():
    """--max-turns defaults to None when not provided on run."""
    from secretary.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["run", "do something"])
    assert args.max_turns is None


def test_max_turns_improve_parser():
    """--max-turns is parsed correctly on the improve subcommand."""
    from secretary.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["improve", "add tests", "--max-turns", "10"])
    assert args.max_turns == 10
    assert args.command == "improve"


def test_max_turns_improve_parser_default():
    """--max-turns defaults to None when not provided on improve."""
    from secretary.__main__ import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["improve", "add tests"])
    assert args.max_turns is None


# ── secretary version ────────────────────────────────────────


def test_version_output(capsys):
    """secretary version prints package, Python, and SDK versions."""
    from secretary.__main__ import _cmd_version

    _cmd_version()

    out = capsys.readouterr().out
    assert "claude-secretary 0.2.0" in out
    assert "Python" in out
    assert "claude-agent-sdk" in out


def test_version_python_version_format(capsys):
    """secretary version prints a valid Python version string."""
    import sys
    from secretary.__main__ import _cmd_version

    _cmd_version()

    out = capsys.readouterr().out
    expected_py = f"Python {sys.version.split()[0]}"
    assert expected_py in out


# ── history parser args ──────────────────────────────────────


def test_history_failed_flag():
    """--failed flag is parsed correctly."""
    from secretary.__main__ import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["history", "--failed"])
    assert args.failed is True


def test_history_failed_flag_default():
    """--failed defaults to False."""
    from secretary.__main__ import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["history"])
    assert args.failed is False


def test_history_search_flag():
    """--search flag is parsed correctly."""
    from secretary.__main__ import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["history", "--search", "email"])
    assert args.search == "email"


def test_history_search_flag_default():
    """--search defaults to None."""
    from secretary.__main__ import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["history"])
    assert args.search is None
