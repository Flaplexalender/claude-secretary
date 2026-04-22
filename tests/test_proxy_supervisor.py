"""Tests for proxy_supervisor — copilot-api health monitoring."""
from __future__ import annotations

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from secretary.proxy_supervisor import (
    ProxySupervisor,
    SupervisorStats,
    is_proxy_healthy,
)


# ---------------------------------------------------------------------------
# is_proxy_healthy
# ---------------------------------------------------------------------------

class TestIsProxyHealthy:
    def test_healthy_response(self):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("secretary.proxy_supervisor.urllib.request.urlopen",
                   return_value=resp) as mock_open:
            assert is_proxy_healthy("http://localhost:4141") is True
            mock_open.assert_called_once()
            # url gets /v1/models appended
            called_url = mock_open.call_args.args[0]
            assert called_url == "http://localhost:4141/v1/models"

    def test_does_not_double_suffix(self):
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("secretary.proxy_supervisor.urllib.request.urlopen",
                   return_value=resp) as mock_open:
            is_proxy_healthy("http://localhost:4141/v1/models")
            called_url = mock_open.call_args.args[0]
            assert called_url == "http://localhost:4141/v1/models"

    def test_connection_refused_is_unhealthy(self):
        err = urllib.error.URLError(ConnectionRefusedError("10061"))
        with patch("secretary.proxy_supervisor.urllib.request.urlopen",
                   side_effect=err):
            assert is_proxy_healthy("http://localhost:4141") is False

    def test_http_error_still_considered_up(self):
        # Proxy returning 401 means it's listening — we care about reachability
        err = urllib.error.HTTPError(
            "http://localhost:4141/v1/models", 401, "Unauthorized", {}, None,
        )
        with patch("secretary.proxy_supervisor.urllib.request.urlopen",
                   side_effect=err):
            assert is_proxy_healthy("http://localhost:4141") is True

    def test_generic_exception_is_unhealthy(self):
        with patch("secretary.proxy_supervisor.urllib.request.urlopen",
                   side_effect=OSError("some os error")):
            assert is_proxy_healthy("http://localhost:4141") is False


# ---------------------------------------------------------------------------
# ProxySupervisor — core behavior
# ---------------------------------------------------------------------------

class TestProxySupervisorHealthCheck:
    def test_is_healthy_updates_stats(self):
        sup = ProxySupervisor(base_url="http://localhost:4141")
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=True):
            assert sup.is_healthy() is True
        assert sup.stats.checks_total == 1
        assert sup.stats.checks_healthy == 1
        assert sup.stats.last_healthy_at > 0

    def test_is_healthy_counts_misses(self):
        sup = ProxySupervisor(base_url="http://localhost:4141")
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=False):
            assert sup.is_healthy() is False
        assert sup.stats.checks_total == 1
        assert sup.stats.checks_healthy == 0
        assert sup.stats.last_healthy_at == 0

    def test_ensure_healthy_fast_path(self):
        """When proxy is up, ensure_healthy returns True without restarting."""
        sup = ProxySupervisor(base_url="http://localhost:4141", auto_start=True)
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=True):
            assert sup.ensure_healthy() is True
        assert sup.stats.restart_attempts == 0


class TestProxySupervisorRestart:
    def test_ensure_healthy_without_auto_start_returns_false(self):
        """auto_start=False means health failures are reported but not restarted."""
        sup = ProxySupervisor(
            base_url="http://localhost:4141",
            auto_start=False,
        )
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=False):
            assert sup.ensure_healthy() is False
        assert sup.stats.restart_attempts == 0

    def test_ensure_healthy_with_auto_start_spawns_subprocess(self):
        sup = ProxySupervisor(
            base_url="http://localhost:4141",
            auto_start=True,
            spawn_cmd=("echo", "fake-proxy"),
            start_timeout_s=2,
        )
        fake_proc = MagicMock()
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None  # still alive
        # First call (health check) = down; subsequent calls after spawn = up
        health_calls = iter([False, True])
        with patch("secretary.proxy_supervisor.subprocess.Popen",
                   return_value=fake_proc) as mock_popen, \
             patch("secretary.proxy_supervisor.is_proxy_healthy",
                   side_effect=lambda *_a, **_kw: next(health_calls,
                                                      True)):
            ok = sup.ensure_healthy()
        assert ok is True
        assert sup.stats.restart_attempts == 1
        assert sup.stats.restart_successes == 1
        assert sup.stats.managed_pid == 99999
        mock_popen.assert_called_once()

    def test_cooldown_prevents_rapid_restart_loop(self):
        sup = ProxySupervisor(
            base_url="http://localhost:4141",
            auto_start=True,
            restart_cooldown_s=60,
            spawn_cmd=("echo", "fake"),
        )
        # Simulate a recent failed restart
        sup.stats.last_restart_at = __import__("time").time()
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=False), \
             patch("secretary.proxy_supervisor.subprocess.Popen") as mock_popen:
            assert sup.ensure_healthy() is False
        mock_popen.assert_not_called()

    def test_spawn_failure_returns_false_and_counts(self):
        sup = ProxySupervisor(
            base_url="http://localhost:4141",
            auto_start=True,
            spawn_cmd=("nonexistent-binary", "arg"),
            start_timeout_s=1,
        )
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=False), \
             patch("secretary.proxy_supervisor.subprocess.Popen",
                   side_effect=FileNotFoundError("no such file")):
            assert sup.ensure_healthy() is False
        assert sup.stats.restart_attempts == 1
        assert sup.stats.restart_failures == 1
        assert sup.stats.restart_successes == 0

    def test_ensure_healthy_never_raises(self):
        """Any internal exception should return False, not propagate."""
        sup = ProxySupervisor(base_url="http://localhost:4141", auto_start=True)
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   side_effect=RuntimeError("boom")):
            # Must not raise
            result = sup.ensure_healthy()
        assert result is False

    def test_resolve_spawn_cmd_falls_back_to_shutil_which(self):
        sup = ProxySupervisor(
            base_url="http://localhost:4141",
            auto_start=True,
            port=4242,
        )
        with patch("secretary.proxy_supervisor.shutil.which",
                   return_value="/usr/bin/npx"):
            cmd = sup._resolve_spawn_cmd()
        assert cmd == ("/usr/bin/npx", "copilot-api@latest",
                       "start", "--port", "4242")

    def test_resolve_spawn_cmd_returns_none_when_npx_missing(self):
        sup = ProxySupervisor(
            base_url="http://localhost:4141", auto_start=True,
        )
        with patch("secretary.proxy_supervisor.shutil.which",
                   return_value=None):
            assert sup._resolve_spawn_cmd() is None


class TestProxySupervisorStopManaged:
    def test_stop_managed_is_noop_when_nothing_spawned(self):
        sup = ProxySupervisor(base_url="http://localhost:4141")
        sup.stop_managed()  # must not raise

    def test_stop_managed_terminates_running_subprocess(self):
        sup = ProxySupervisor(base_url="http://localhost:4141")
        fake_proc = MagicMock()
        fake_proc.pid = 42
        fake_proc.poll.return_value = None
        sup._process = fake_proc
        sup.stats.managed_pid = 42
        sup.stop_managed()
        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called()
        assert sup._process is None
        assert sup.stats.managed_pid is None

    def test_stop_managed_kills_if_terminate_hangs(self):
        import subprocess as _sp
        sup = ProxySupervisor(base_url="http://localhost:4141")
        fake_proc = MagicMock()
        fake_proc.pid = 42
        fake_proc.poll.return_value = None
        # terminate OK, but first wait() raises TimeoutExpired; second succeeds.
        fake_proc.wait.side_effect = [_sp.TimeoutExpired("cmd", 10), 0]
        sup._process = fake_proc
        sup.stop_managed()
        fake_proc.kill.assert_called_once()


class TestProxySupervisorHealthLog:
    def test_records_to_health_log_on_failure(self):
        hl = MagicMock()
        sup = ProxySupervisor(
            base_url="http://localhost:4141",
            auto_start=False,
            health_log=hl,
        )
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=False):
            sup.ensure_healthy()
        # At least one health_log.record call with category "proxy_supervisor"
        assert any(
            call.args[0] == "proxy_supervisor"
            for call in hl.record.call_args_list
        )

    def test_health_log_failure_is_swallowed(self):
        hl = MagicMock()
        hl.record.side_effect = RuntimeError("log broken")
        sup = ProxySupervisor(
            base_url="http://localhost:4141",
            auto_start=False,
            health_log=hl,
        )
        with patch("secretary.proxy_supervisor.is_proxy_healthy",
                   return_value=False):
            # Must not raise
            assert sup.ensure_healthy() is False
