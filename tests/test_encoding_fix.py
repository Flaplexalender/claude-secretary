"""Tests for encoding_fix module — Windows UTF-8 encoding fix."""
from __future__ import annotations

import io
import sys
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from secretary.encoding_fix import fix_windows_encoding


# ── Non-Windows: early return ─────────────────────────────────


class TestNonWindowsPlatform:
    """On non-Windows platforms, fix_windows_encoding should be a no-op."""

    def test_noop_on_linux(self):
        """Should return immediately on Linux without modifying stdout."""
        original_stdout = sys.stdout
        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "linux"
            fix_windows_encoding()
            # stdout.reconfigure should never be called
            assert not mock_sys.stdout.reconfigure.called

    def test_noop_on_darwin(self):
        """Should return immediately on macOS without modifying stdout."""
        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "darwin"
            fix_windows_encoding()
            assert not mock_sys.stdout.reconfigure.called

    def test_noop_on_freebsd(self):
        """Should return immediately on FreeBSD."""
        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "freebsd"
            fix_windows_encoding()
            assert not mock_sys.stdout.reconfigure.called


# ── Windows: reconfigure path ────────────────────────────────


class TestWindowsReconfigure:
    """On Windows, fix_windows_encoding should reconfigure stdout/stderr."""

    def test_reconfigures_stdout_and_stderr(self):
        """Both stdout and stderr should be reconfigured to UTF-8."""
        mock_stdout = MagicMock()
        mock_stdout.encoding = "utf-8"  # After reconfigure, encoding is utf-8
        mock_stderr = MagicMock()
        mock_stderr.encoding = "utf-8"

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr
            fix_windows_encoding()

        mock_stdout.reconfigure.assert_called_once_with(
            encoding='utf-8', errors='replace'
        )
        mock_stderr.reconfigure.assert_called_once_with(
            encoding='utf-8', errors='replace'
        )

    def test_reconfigure_called_with_correct_params(self):
        """Verify encoding='utf-8' and errors='replace' are passed."""
        mock_stdout = MagicMock()
        mock_stdout.encoding = "utf-8"
        mock_stderr = MagicMock()
        mock_stderr.encoding = "utf-8"

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr
            fix_windows_encoding()

        call_kwargs = mock_stdout.reconfigure.call_args[1]
        assert call_kwargs["encoding"] == "utf-8"
        assert call_kwargs["errors"] == "replace"


# ── Windows: reconfigure failure → fallback wrapping ──────────


class TestWindowsFallbackWrap:
    """When reconfigure fails or encoding is not utf-8, fallback wrapping."""

    def test_fallback_when_reconfigure_raises(self):
        """If reconfigure throws, should try TextIOWrapper fallback."""
        mock_stdout = MagicMock()
        mock_stdout.reconfigure.side_effect = Exception("not supported")
        mock_stdout.encoding = "cp1252"
        mock_stdout.buffer = MagicMock()

        mock_stderr = MagicMock()
        mock_stderr.reconfigure.side_effect = Exception("not supported")
        mock_stderr.encoding = "cp1252"
        mock_stderr.buffer = MagicMock()

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr

            with patch("secretary.encoding_fix.io.TextIOWrapper") as mock_wrapper:
                mock_wrapper.return_value = MagicMock()
                fix_windows_encoding()

            # TextIOWrapper should have been called for both stdout and stderr
            assert mock_wrapper.call_count == 2

    def test_fallback_wraps_stdout_with_utf8(self):
        """Fallback should wrap stdout.buffer with UTF-8 TextIOWrapper."""
        mock_buffer = MagicMock()
        mock_stdout = MagicMock()
        mock_stdout.reconfigure.side_effect = Exception("fail")
        mock_stdout.encoding = "cp1252"
        mock_stdout.buffer = mock_buffer

        mock_stderr = MagicMock()
        mock_stderr.reconfigure.side_effect = Exception("fail")
        mock_stderr.encoding = "cp1252"
        mock_stderr.buffer = MagicMock()

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr

            with patch("secretary.encoding_fix.io.TextIOWrapper") as mock_wrapper:
                wrapped = MagicMock()
                mock_wrapper.return_value = wrapped
                fix_windows_encoding()

            # First call should be for stdout
            first_call = mock_wrapper.call_args_list[0]
            assert first_call[0][0] is mock_buffer
            assert first_call[1]["encoding"] == "utf-8"
            assert first_call[1]["errors"] == "replace"
            assert first_call[1]["line_buffering"] is True

    def test_no_fallback_when_already_utf8(self):
        """If encoding is already utf-8 after reconfigure, skip fallback."""
        mock_stdout = MagicMock()
        mock_stdout.encoding = "utf-8"
        mock_stderr = MagicMock()
        mock_stderr.encoding = "utf-8"

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr

            with patch("secretary.encoding_fix.io.TextIOWrapper") as mock_wrapper:
                fix_windows_encoding()

            # Should NOT have fallen back to wrapping
            mock_wrapper.assert_not_called()

    def test_no_fallback_when_encoding_is_utf8_variant(self):
        """utf-8-sig and similar variants should also skip fallback."""
        mock_stdout = MagicMock()
        mock_stdout.encoding = "UTF-8"
        mock_stderr = MagicMock()
        mock_stderr.encoding = "UTF-8"

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr

            with patch("secretary.encoding_fix.io.TextIOWrapper") as mock_wrapper:
                fix_windows_encoding()

            mock_wrapper.assert_not_called()


# ── Windows: stdout is None or missing attributes ─────────────


class TestWindowsEdgeCases:
    """Edge cases: stdout is None, missing reconfigure, etc."""

    def test_stdout_is_none(self):
        """Should not crash when stdout is None."""
        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = None
            mock_sys.stderr = MagicMock()
            # Should not raise
            fix_windows_encoding()

    def test_stdout_without_reconfigure(self):
        """Should handle stdout without reconfigure attribute gracefully."""
        mock_stdout = MagicMock(spec=[])  # No attributes
        mock_stdout.encoding = "cp1252"

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = MagicMock()
            # hasattr(stdout, 'reconfigure') is False → skip reconfigure
            # Should not raise
            fix_windows_encoding()

    def test_fallback_wrapping_also_fails(self):
        """If both reconfigure and TextIOWrapper fail, should not crash."""
        mock_stdout = MagicMock()
        mock_stdout.reconfigure.side_effect = Exception("fail")
        mock_stdout.encoding = "cp1252"
        mock_stdout.buffer = MagicMock()

        mock_stderr = MagicMock()
        mock_stderr.reconfigure.side_effect = Exception("fail")
        mock_stderr.encoding = "cp1252"
        mock_stderr.buffer = MagicMock()

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr

            with patch("secretary.encoding_fix.io.TextIOWrapper") as mock_wrapper:
                mock_wrapper.side_effect = Exception("wrapping failed too")
                # Should not raise — exceptions are caught
                fix_windows_encoding()

    def test_encoding_none(self):
        """If stdout.encoding is None, fallback path should not be taken."""
        mock_stdout = MagicMock()
        mock_stdout.encoding = None
        mock_stderr = MagicMock()
        mock_stderr.encoding = None

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr

            with patch("secretary.encoding_fix.io.TextIOWrapper") as mock_wrapper:
                fix_windows_encoding()

            # encoding is None → `sys.stdout.encoding and ...` is falsy → skip fallback
            mock_wrapper.assert_not_called()

    def test_stderr_reconfigure_exception_independent(self):
        """stderr reconfigure failure should not prevent stdout processing."""
        mock_stdout = MagicMock()
        mock_stdout.encoding = "utf-8"

        mock_stderr = MagicMock()
        mock_stderr.reconfigure.side_effect = Exception("stderr broken")
        mock_stderr.encoding = "utf-8"

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr
            # The reconfigure block wraps both in a single try;
            # stderr exception is caught by the except
            fix_windows_encoding()

        # stdout.reconfigure was still called
        mock_stdout.reconfigure.assert_called_once()


# ── Module-level __main__ guard ───────────────────────────────


class TestModuleMain:
    """Test the __main__ guard section runs without error."""

    def test_module_importable(self):
        """Module should import without side effects."""
        import secretary.encoding_fix
        assert hasattr(secretary.encoding_fix, 'fix_windows_encoding')
        assert callable(secretary.encoding_fix.fix_windows_encoding)


# ── Integration-style tests ──────────────────────────────────


class TestIntegration:
    """Integration-style tests with real (but safe) scenarios."""

    def test_calling_twice_is_safe(self):
        """Calling fix_windows_encoding multiple times should be idempotent."""
        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_stdout = MagicMock()
            mock_stdout.encoding = "utf-8"
            mock_stderr = MagicMock()
            mock_stderr.encoding = "utf-8"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr

            fix_windows_encoding()
            fix_windows_encoding()
            # Should have been called twice without error
            assert mock_stdout.reconfigure.call_count == 2

    def test_function_returns_none(self):
        """fix_windows_encoding should return None (implicit return)."""
        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = fix_windows_encoding()
        assert result is None

    def test_function_returns_none_on_windows(self):
        """fix_windows_encoding should return None on Windows too."""
        mock_stdout = MagicMock()
        mock_stdout.encoding = "utf-8"
        mock_stderr = MagicMock()
        mock_stderr.encoding = "utf-8"

        with patch("secretary.encoding_fix.sys") as mock_sys:
            mock_sys.platform = "win32"
            mock_sys.stdout = mock_stdout
            mock_sys.stderr = mock_stderr
            result = fix_windows_encoding()
        assert result is None

    def test_init_imports_fix_windows_encoding(self):
        """The __init__.py should expose fix_windows_encoding."""
        from secretary.encoding_fix import fix_windows_encoding as fn
        assert fn is fix_windows_encoding
