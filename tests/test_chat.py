"""Tests for the chat command — parser flags, direct/SDK dispatch, slash commands.

These are unit tests that verify the CLI argument parsing and the dispatch
logic without actually running interactive input loops or making API calls.
"""
from __future__ import annotations

import argparse
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secretary.__main__ import _build_parser, _cmd_chat
from secretary.config import SecretaryConfig


# ── Chat parser args ─────────────────────────────────────────


class TestChatParser:
    """Verify --sdk, --workspace, --files, --tier on chat subcommand."""

    def _parse(self, *args: str) -> argparse.Namespace:
        parser = _build_parser()
        return parser.parse_args(["chat", *args])

    def test_chat_default_no_sdk(self):
        """Default chat mode should NOT use SDK."""
        args = self._parse()
        assert args.sdk is False

    def test_chat_sdk_flag(self):
        """--sdk flag enables SDK mode."""
        args = self._parse("--sdk")
        assert args.sdk is True

    def test_chat_workspace_flag(self):
        """--workspace sets directory for sandboxed file tools."""
        args = self._parse("--workspace", "/tmp/mydir")
        assert args.workspace == "/tmp/mydir"

    def test_chat_workspace_default(self):
        """workspace defaults to None."""
        args = self._parse()
        assert args.workspace is None

    def test_chat_files_flag(self):
        """--files enables unrestricted file access."""
        args = self._parse("--files")
        assert args.files is True

    def test_chat_files_default(self):
        """files defaults to False."""
        args = self._parse()
        assert args.files is False

    def test_chat_tier_flag(self):
        """--tier sets model tier."""
        args = self._parse("--tier", "high")
        assert args.tier == "high"

    def test_chat_tier_default(self):
        """tier defaults to None."""
        args = self._parse()
        assert args.tier is None

    def test_chat_invalid_tier_rejected(self):
        """Invalid tier value is rejected by argparse."""
        with pytest.raises(SystemExit):
            self._parse("--tier", "mega")

    def test_chat_combined_flags(self):
        """Multiple flags can be combined."""
        args = self._parse("--tier", "low", "--workspace", "/tmp", "--sdk")
        assert args.tier == "low"
        assert args.workspace == "/tmp"
        assert args.sdk is True


# ── Chat dispatch logic ──────────────────────────────────────


class TestChatDispatch:
    """Verify _cmd_chat dispatches to the right sub-handler."""

    @pytest.mark.asyncio
    async def test_chat_dispatches_to_direct_by_default(self):
        """Without --sdk, _cmd_chat should call _cmd_chat_direct."""
        args = argparse.Namespace(sdk=False, tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        with patch("secretary.__main__._cmd_chat_direct", new_callable=AsyncMock) as mock_direct, \
             patch("secretary.__main__._cmd_chat_sdk", new_callable=AsyncMock) as mock_sdk:
            await _cmd_chat(args, config)
            mock_direct.assert_awaited_once_with(args, config)
            mock_sdk.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_chat_dispatches_to_sdk_with_flag(self):
        """With --sdk, _cmd_chat should call _cmd_chat_sdk."""
        args = argparse.Namespace(sdk=True, tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        with patch("secretary.__main__._cmd_chat_direct", new_callable=AsyncMock) as mock_direct, \
             patch("secretary.__main__._cmd_chat_sdk", new_callable=AsyncMock) as mock_sdk:
            await _cmd_chat(args, config)
            mock_sdk.assert_awaited_once_with(args, config)
            mock_direct.assert_not_awaited()


# ── Chat SDK path ────────────────────────────────────────────


class TestChatSDK:
    """Verify _cmd_chat_sdk uses agent.run (SDK path)."""

    @pytest.mark.asyncio
    async def test_sdk_chat_uses_agent_module(self):
        """_cmd_chat_sdk should import from agent module."""
        from secretary.__main__ import _cmd_chat_sdk
        args = argparse.Namespace(tier=None)
        config = SecretaryConfig()

        # Simulate user typing "quit" immediately
        with patch("builtins.input", return_value="quit"):
            await _cmd_chat_sdk(args, config)
            # No error = it used the right path


    @pytest.mark.asyncio
    async def test_sdk_chat_prints_sdk_mode_banner(self, capsys):
        """_cmd_chat_sdk should indicate SDK mode in its banner."""
        from secretary.__main__ import _cmd_chat_sdk
        args = argparse.Namespace(tier=None)
        config = SecretaryConfig()

        with patch("builtins.input", return_value="quit"):
            await _cmd_chat_sdk(args, config)

        out = capsys.readouterr().out
        assert "SDK" in out


# ── Chat Direct path ─────────────────────────────────────────


class TestChatDirect:
    """Verify _cmd_chat_direct uses direct_agent.run."""

    @pytest.mark.asyncio
    async def test_direct_chat_prints_direct_mode_banner(self, capsys):
        """_cmd_chat_direct should indicate direct mode in its banner."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        with patch("builtins.input", return_value="quit"):
            await _cmd_chat_direct(args, config)

        out = capsys.readouterr().out
        assert "direct" in out.lower()

    @pytest.mark.asyncio
    async def test_direct_chat_shows_tool_names(self, capsys, tmp_path):
        """_cmd_chat_direct should list available tools in banner."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=str(tmp_path), files=False)
        config = SecretaryConfig(data_root=str(tmp_path))

        with patch("builtins.input", return_value="quit"):
            await _cmd_chat_direct(args, config)

        out = capsys.readouterr().out
        # Should show file tools since workspace is set
        assert "file_read" in out

    @pytest.mark.asyncio
    async def test_direct_chat_slash_tier_command(self, capsys):
        """'/tier high' should switch tiers without making an API call."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        # Simulate: /tier high → quit
        inputs = iter(["/tier high", "quit"])
        with patch("builtins.input", side_effect=lambda _="": next(inputs)):
            await _cmd_chat_direct(args, config)

        out = capsys.readouterr().out
        assert "high" in out.lower()
        assert "opus" in out.lower() or "Switched" in out

    @pytest.mark.asyncio
    async def test_direct_chat_slash_tier_invalid(self, capsys):
        """'/tier mega' should show usage."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        inputs = iter(["/tier mega", "quit"])
        with patch("builtins.input", side_effect=lambda _="": next(inputs)):
            await _cmd_chat_direct(args, config)

        out = capsys.readouterr().out
        assert "Usage" in out

    @pytest.mark.asyncio
    async def test_direct_chat_empty_input_ignored(self, capsys):
        """Empty input should be silently ignored."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        inputs = iter(["", "quit"])
        with patch("builtins.input", side_effect=lambda _="": next(inputs)):
            await _cmd_chat_direct(args, config)
        # No crash = success

    @pytest.mark.asyncio
    async def test_direct_chat_eof_exits_cleanly(self, capsys):
        """EOFError (Ctrl+D) should exit cleanly."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        with patch("builtins.input", side_effect=EOFError):
            await _cmd_chat_direct(args, config)

        out = capsys.readouterr().out
        assert "Bye" in out

    @pytest.mark.asyncio
    async def test_direct_chat_keyboard_interrupt_exits(self, capsys):
        """KeyboardInterrupt (Ctrl+C) should exit cleanly."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            await _cmd_chat_direct(args, config)

        out = capsys.readouterr().out
        assert "Bye" in out

    @pytest.mark.asyncio
    async def test_direct_chat_calls_direct_agent(self):
        """An actual message should invoke direct_agent.run."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        # Mock the run result
        mock_result = MagicMock()
        mock_result.text = "Hello! I can help."
        mock_result.error = None
        mock_result.cost_usd = 0.001
        mock_result.num_turns = 1

        inputs = iter(["hello", "quit"])
        with patch("builtins.input", side_effect=lambda _="": next(inputs)), \
             patch("secretary.direct_agent.run", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            await _cmd_chat_direct(args, config)

            mock_run.assert_awaited_once()
            call_kwargs = mock_run.call_args
            # The task should contain "hello"
            assert "hello" in call_kwargs.kwargs.get("task", "") or "hello" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_direct_chat_error_display(self, capsys):
        """API errors should be displayed nicely."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        mock_result = MagicMock()
        mock_result.text = ""
        mock_result.error = "Connection refused"
        mock_result.cost_usd = 0
        mock_result.num_turns = 0

        inputs = iter(["hello", "quit"])
        with patch("builtins.input", side_effect=lambda _="": next(inputs)), \
             patch("secretary.direct_agent.run", new_callable=AsyncMock, return_value=mock_result):
            await _cmd_chat_direct(args, config)

        out = capsys.readouterr().out
        assert "Error" in out
        assert "Connection refused" in out

    @pytest.mark.asyncio
    async def test_direct_chat_history_builds_context(self):
        """Second message should include context from first exchange."""
        from secretary.__main__ import _cmd_chat_direct
        args = argparse.Namespace(tier=None, workspace=None, files=False)
        config = SecretaryConfig()

        mock_result = MagicMock()
        mock_result.text = "First response"
        mock_result.error = None
        mock_result.cost_usd = 0.001
        mock_result.num_turns = 1

        captured_tasks = []

        async def capture_run(**kwargs):
            captured_tasks.append(kwargs.get("task", ""))
            return mock_result

        inputs = iter(["hello", "follow up", "quit"])
        with patch("builtins.input", side_effect=lambda _="": next(inputs)), \
             patch("secretary.direct_agent.run", side_effect=capture_run):
            await _cmd_chat_direct(args, config)

        # Second call should contain conversation context
        assert len(captured_tasks) == 2
        # First call: just the user message (no prior context)
        assert "hello" in captured_tasks[0]
        # Second call: should have context from first exchange
        assert "follow up" in captured_tasks[1]
        assert "hello" in captured_tasks[1]  # prior user msg in context
        assert "First response" in captured_tasks[1]  # prior assistant response in context


# ── Backward compatibility ────────────────────────────────────


class TestChatBackwardCompat:
    """Ensure existing parser behavior is preserved."""

    def test_chat_command_still_exists(self):
        """'chat' is a valid subcommand."""
        parser = _build_parser()
        args = parser.parse_args(["chat"])
        assert args.command == "chat"

    def test_chat_tier_choices_preserved(self):
        """--tier still accepts low/medium/high."""
        parser = _build_parser()
        for tier in ("low", "medium", "high"):
            args = parser.parse_args(["chat", "--tier", tier])
            assert args.tier == tier
