"""Tests for _cmd_chat — verifies direct_agent is the default path.

Covers:
- Parser flags: --sdk, --workspace, --files
- Default path uses direct_agent (not SDK agent)
- --sdk flag uses legacy agent module
"""
from __future__ import annotations

import argparse
import pytest


# ── Parser tests ─────────────────────────────────────────────


class TestChatParser:
    """Test that the chat subcommand parser accepts expected flags."""

    def _parse(self, *args: str) -> argparse.Namespace:
        from secretary.__main__ import _build_parser
        parser = _build_parser()
        return parser.parse_args(["chat", *args])

    def test_chat_default_no_sdk(self):
        """chat defaults to --sdk=False (direct agent)."""
        args = self._parse()
        assert args.sdk is False
        assert args.command == "chat"

    def test_chat_sdk_flag(self):
        """chat --sdk enables legacy SDK path."""
        args = self._parse("--sdk")
        assert args.sdk is True

    def test_chat_tier_flag(self):
        """chat --tier high sets tier."""
        args = self._parse("--tier", "high")
        assert args.tier == "high"

    def test_chat_workspace_flag(self):
        """chat --workspace DIR enables sandboxed file tools."""
        args = self._parse("--workspace", "/tmp/ws")
        assert args.workspace == "/tmp/ws"

    def test_chat_files_flag(self):
        """chat --files enables unrestricted file access."""
        args = self._parse("--files")
        assert args.files is True

    def test_chat_default_no_workspace(self):
        """chat defaults to no workspace."""
        args = self._parse()
        assert args.workspace is None

    def test_chat_default_no_files(self):
        """chat defaults to no unrestricted files."""
        args = self._parse()
        assert args.files is False

    def test_chat_combined_flags(self):
        """chat with multiple flags parses correctly."""
        args = self._parse("--tier", "low", "--workspace", "/tmp", "--sdk")
        assert args.tier == "low"
        assert args.workspace == "/tmp"
        assert args.sdk is True


class TestChatOutputHeader:
    """Test that _cmd_chat prints the correct mode in its header."""

    @pytest.mark.asyncio
    async def test_chat_direct_mode_header(self, capsys, monkeypatch):
        """Direct mode shows 'direct mode' in header before waiting for input."""
        from secretary.__main__ import _cmd_chat
        from secretary.config import SecretaryConfig

        config = SecretaryConfig()
        args = argparse.Namespace(tier=None, sdk=False, workspace=None, files=False)

        # Simulate immediate EOF (user presses Ctrl+D)
        monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))

        await _cmd_chat(args, config)

        out = capsys.readouterr().out
        assert "direct mode" in out.lower()
        assert "Bye!" in out

    @pytest.mark.asyncio
    async def test_chat_sdk_mode_header(self, capsys, monkeypatch):
        """SDK mode shows 'premium' in header before waiting for input."""
        from secretary.__main__ import _cmd_chat
        from secretary.config import SecretaryConfig

        config = SecretaryConfig()
        args = argparse.Namespace(tier=None, sdk=True, workspace=None, files=False)

        monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))

        await _cmd_chat(args, config)

        out = capsys.readouterr().out
        assert "SDK" in out
        assert "premium" in out
        assert "Bye!" in out

    @pytest.mark.asyncio
    async def test_chat_quit_commands(self, capsys, monkeypatch):
        """'quit', 'exit', and 'q' all exit the chat loop."""
        from secretary.__main__ import _cmd_chat
        from secretary.config import SecretaryConfig

        for cmd in ("quit", "exit", "q"):
            config = SecretaryConfig()
            args = argparse.Namespace(tier=None, sdk=False, workspace=None, files=False)

            inputs = iter([cmd])
            monkeypatch.setattr("builtins.input", lambda prompt="", it=inputs: next(it))

            await _cmd_chat(args, config)

            out = capsys.readouterr().out
            assert "Bye!" in out

    @pytest.mark.asyncio
    async def test_chat_empty_input_skipped(self, capsys, monkeypatch):
        """Empty input lines are skipped without error."""
        from secretary.__main__ import _cmd_chat
        from secretary.config import SecretaryConfig

        config = SecretaryConfig()
        args = argparse.Namespace(tier=None, sdk=False, workspace=None, files=False)

        inputs = iter(["", "   ", "quit"])
        monkeypatch.setattr("builtins.input", lambda prompt="", it=inputs: next(it))

        await _cmd_chat(args, config)

        out = capsys.readouterr().out
        assert "Bye!" in out
