"""Tests for service module — PID file, signal handling, proxy wait."""
from __future__ import annotations

import os
import signal
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from secretary.service import (
    cleanup_pidfile,
    install_sigbreak_handler,
    wait_for_proxy,
    write_pidfile,
)


# ── PID file ──────────────────────────────────────────────────


def test_write_pidfile_creates_file(tmp_path: Path):
    pidfile = write_pidfile(tmp_path)
    assert pidfile.exists()
    assert pidfile.read_text(encoding="utf-8") == str(os.getpid())


def test_write_pidfile_creates_directory(tmp_path: Path):
    nested = tmp_path / "deep" / "nested"
    pidfile = write_pidfile(nested)
    assert pidfile.exists()
    assert nested.exists()


def test_cleanup_pidfile_removes(tmp_path: Path):
    write_pidfile(tmp_path)
    assert (tmp_path / "secretary.pid").exists()
    cleanup_pidfile(tmp_path)
    assert not (tmp_path / "secretary.pid").exists()


def test_cleanup_pidfile_noop_when_missing(tmp_path: Path):
    """cleanup_pidfile doesn't error when PID file doesn't exist."""
    cleanup_pidfile(tmp_path)  # no error


# ── SIGBREAK handler ──────────────────────────────────────────


def test_install_sigbreak_handler_windows():
    """On Windows, SIGBREAK handler should be installed."""
    callback = MagicMock()
    with patch("secretary.service.sys") as mock_sys:
        mock_sys.platform = "win32"
        with patch("secretary.service.signal") as mock_signal:
            mock_signal.SIGBREAK = 21  # fake value
            install_sigbreak_handler(callback)
            mock_signal.signal.assert_called_once()
            # Verify it was called with SIGBREAK
            call_args = mock_signal.signal.call_args
            assert call_args[0][0] == 21


def test_install_sigbreak_handler_noop_non_windows():
    """On non-Windows, SIGBREAK handler is not installed."""
    callback = MagicMock()
    with patch("secretary.service.sys") as mock_sys:
        mock_sys.platform = "linux"
        with patch("secretary.service.signal") as mock_signal:
            del mock_signal.SIGBREAK  # not available on Linux
            install_sigbreak_handler(callback)
            mock_signal.signal.assert_not_called()


# ── Proxy wait ────────────────────────────────────────────────


def test_wait_for_proxy_succeeds_immediately():
    """Returns True when proxy responds immediately."""
    with patch("secretary.service.urllib.request.urlopen") as mock_open:
        mock_open.return_value = MagicMock()
        result = wait_for_proxy(timeout=5, interval=1)
    assert result is True
    mock_open.assert_called_once()


def test_wait_for_proxy_retries_then_succeeds():
    """Returns True after a few retries."""
    call_count = 0

    def flaky_open(url, timeout=3):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("not ready")
        return MagicMock()

    with patch("secretary.service.urllib.request.urlopen", side_effect=flaky_open):
        with patch("secretary.service.time.sleep"):  # don't actually sleep
            result = wait_for_proxy(timeout=30, interval=1)

    assert result is True
    assert call_count == 3


def test_wait_for_proxy_timeout():
    """Returns False when proxy never responds within timeout."""
    with patch("secretary.service.urllib.request.urlopen", side_effect=ConnectionError("down")):
        with patch("secretary.service.time.sleep"):
            with patch("secretary.service.time.monotonic") as mock_time:
                # Simulate time progressing past deadline
                mock_time.side_effect = [0, 0, 5, 10, 15, 999]
                result = wait_for_proxy(timeout=10, interval=1)

    assert result is False


# ── Cycle 7: Additional coverage ──────────────────────────────


def test_write_pidfile_returns_correct_path(tmp_path: Path):
    """write_pidfile returns a Path ending in secretary.pid."""
    pidfile = write_pidfile(tmp_path)
    assert pidfile.name == "secretary.pid"
    assert pidfile.parent == tmp_path


def test_write_pidfile_overwrites_existing(tmp_path: Path):
    """Writing PID file twice should overwrite, not append."""
    write_pidfile(tmp_path)
    write_pidfile(tmp_path)
    content = (tmp_path / "secretary.pid").read_text(encoding="utf-8")
    assert content == str(os.getpid())  # Should be just one PID


def test_install_sigbreak_handler_callback_invoked():
    """The installed handler should call the callback when triggered."""
    callback = MagicMock()
    handler = None

    with patch("secretary.service.sys") as mock_sys:
        mock_sys.platform = "win32"
        with patch("secretary.service.signal") as mock_signal:
            mock_signal.SIGBREAK = 21

            def capture_handler(signum, func):
                nonlocal handler
                handler = func

            mock_signal.signal.side_effect = capture_handler
            install_sigbreak_handler(callback)

    # Simulate signal delivery
    assert handler is not None
    handler(21, None)
    callback.assert_called_once()


def test_wait_for_proxy_url_error_connection_refused():
    """URLError with ConnectionRefusedError should retry."""
    call_count = 0

    def refused_then_ok(url, timeout=3):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        return MagicMock()

    with patch("secretary.service.urllib.request.urlopen", side_effect=refused_then_ok):
        with patch("secretary.service.time.sleep"):
            result = wait_for_proxy(timeout=30, interval=1)

    assert result is True
    assert call_count == 2


def test_wait_for_proxy_url_error_other():
    """URLError with non-connection reason should retry."""
    call_count = 0

    def network_err_then_ok(url, timeout=3):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise urllib.error.URLError("DNS resolution failed")
        return MagicMock()

    with patch("secretary.service.urllib.request.urlopen", side_effect=network_err_then_ok):
        with patch("secretary.service.time.sleep"):
            result = wait_for_proxy(timeout=30, interval=1)

    assert result is True
    assert call_count == 2


def test_wait_for_proxy_custom_url_and_params():
    """Custom URL, timeout, interval should be respected."""
    with patch("secretary.service.urllib.request.urlopen") as mock_open:
        mock_open.return_value = MagicMock()
        result = wait_for_proxy(
            url="http://custom:8080/health",
            timeout=60,
            interval=10,
        )
    assert result is True
    mock_open.assert_called_once_with("http://custom:8080/health", timeout=3)


def test_cleanup_pidfile_idempotent(tmp_path: Path):
    """Calling cleanup twice should not error."""
    write_pidfile(tmp_path)
    cleanup_pidfile(tmp_path)
    cleanup_pidfile(tmp_path)  # Second call should be safe
    assert not (tmp_path / "secretary.pid").exists()
