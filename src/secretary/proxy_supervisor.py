"""Proxy supervisor — keep the copilot-api proxy alive across watcher cycles.

The copilot-api proxy (launched via ``npx copilot-api@latest start --port
4141``) occasionally dies silently between cycles.  When that happens every
downstream call fails with ``[WinError 10061] No connection could be made``
(see ``data/run_log.jsonl`` 2026-03-29, 2026-04-12) until Alexander notices
and restarts it manually.

This module provides:

* :func:`is_proxy_healthy` — cheap ``GET /v1/models`` reachability check.
* :class:`ProxySupervisor` — optional subprocess manager that can spawn
  ``npx copilot-api@latest start`` on demand and restart it when the
  health check fails.  Subprocess management is **opt-in** (config flag
  ``watcher.proxy_supervisor.auto_start``) because spawning an npx child
  on Windows is fragile (batch wrapper, PATH resolution, child orphaning
  on hard kill).  When disabled, the supervisor still performs health
  checks and logs/reports degraded state.

Design invariants:
    * The supervisor only manages a subprocess *it spawned itself*.  An
      externally-started proxy (Alexander's own ``npx`` in a separate
      terminal) is never killed by this code.
    * Restart attempts are rate-limited by ``restart_cooldown_s`` to
      prevent rapid crash-loops from eating CPU.
    * Every health-check / restart / failure is emitted to the caller's
      ``HealthLog`` so the self-improve pipeline can observe and learn.
    * ``ensure_healthy`` NEVER raises — it returns ``False`` on any
      error and lets the watcher continue its normal failure-handling
      path.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover — import only for type checking
    from .pipeline_health import HealthLog

log = logging.getLogger("secretary.proxy_supervisor")


def is_proxy_healthy(url: str, timeout: float = 3.0) -> bool:
    """Return True if ``GET <url>/v1/models`` responds within ``timeout``.

    ``url`` may be a full URL ending in ``/v1/models`` or a base URL — in
    the latter case ``/v1/models`` is appended automatically.  Any error
    (connection refused, network, DNS, non-2xx status) is treated as
    unhealthy and logged at DEBUG level.
    """
    probe = url.rstrip("/")
    if not probe.endswith("/v1/models"):
        probe = probe + "/v1/models"
    try:
        with urllib.request.urlopen(probe, timeout=timeout) as resp:  # noqa: S310
            # 2xx / 3xx are healthy; 4xx/5xx treated as unhealthy.
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        # /v1/models returning 401/403 still means the proxy is listening.
        # Treat any HTTP response as "process is up" — auth is a separate
        # concern handled by the agent call path.
        log.debug("Proxy responded with HTTP %s (still considered up)", e.code)
        return True
    except Exception as e:  # noqa: BLE001 — intentionally broad
        log.debug(
            "Proxy health check failed (%s): %s",
            type(e).__name__, str(e)[:120],
        )
        return False


@dataclass
class SupervisorStats:
    """Observability counters for the supervisor's decisions."""
    checks_total: int = 0
    checks_healthy: int = 0
    restart_attempts: int = 0
    restart_successes: int = 0
    restart_failures: int = 0
    last_healthy_at: float = 0.0
    last_restart_at: float = 0.0
    managed_pid: int | None = None

    def as_dict(self) -> dict:
        """Return a JSON-serialisable snapshot of the counters.

        Used by the watcher heartbeat to surface proxy health to
        operators without coupling them to the dataclass type.
        """
        return {
            "checks_total": self.checks_total,
            "checks_healthy": self.checks_healthy,
            "restart_attempts": self.restart_attempts,
            "restart_successes": self.restart_successes,
            "restart_failures": self.restart_failures,
            "last_healthy_at": self.last_healthy_at,
            "last_restart_at": self.last_restart_at,
            "managed_pid": self.managed_pid,
        }


@dataclass
class ProxySupervisor:
    """Keep the copilot-api proxy reachable at ``base_url``.

    Usage (watcher)::

        sup = ProxySupervisor(
            base_url=config.anthropic_base_url,
            auto_start=config.watcher.proxy_supervisor.auto_start,
            restart_cooldown_s=60,
            health_log=self.health_log,
        )
        if not sup.ensure_healthy():
            log.warning("Proxy still down — skipping cycle")

    The supervisor is thread-safe for single-writer access (called from the
    watcher's event loop thread only).  No async entry points; all work is
    synchronous and cheap enough to run between cycles.
    """

    base_url: str
    auto_start: bool = False
    port: int = 4141
    start_timeout_s: int = 45
    restart_cooldown_s: int = 60
    health_timeout_s: float = 3.0
    health_log: Optional["HealthLog"] = None
    # The command used to spawn the proxy.  Override for tests.
    # Default resolves at .start() time from shutil.which.
    spawn_cmd: tuple[str, ...] | None = None

    # ------------------------------------------------------------------
    # Internal state
    _process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    stats: SupervisorStats = field(default_factory=SupervisorStats, init=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def is_healthy(self) -> bool:
        """Run a single health check and update stats."""
        self.stats.checks_total += 1
        ok = is_proxy_healthy(self.base_url, timeout=self.health_timeout_s)
        if ok:
            self.stats.checks_healthy += 1
            self.stats.last_healthy_at = time.time()
        return ok

    def ensure_healthy(self) -> bool:
        """Return True if the proxy is reachable, attempting a managed
        restart when ``auto_start`` is enabled and cooldown allows.

        This method never raises.  On any internal error it returns False
        and logs DEBUG-level diagnostics, deferring to the watcher's own
        error handling.
        """
        try:
            if self.is_healthy():
                return True
            log.warning("Proxy at %s is not responding", self.base_url)
            if self._record_health("warning",
                                   f"Proxy health check failed at {self.base_url}"):
                pass  # health_log write is best-effort
            if not self.auto_start:
                return False
            if not self._cooldown_elapsed():
                log.info(
                    "Proxy restart skipped: cooldown active "
                    "(last attempt %.0fs ago, cooldown %ds)",
                    time.time() - self.stats.last_restart_at,
                    self.restart_cooldown_s,
                )
                return False
            return self._attempt_restart()
        except Exception as e:  # noqa: BLE001 — never raise to caller
            log.debug("ensure_healthy crashed (%s): %s",
                      type(e).__name__, e)
            return False

    def stop_managed(self) -> None:
        """Terminate the managed subprocess if one is running.

        Safe to call from signal handlers / shutdown paths.  Never raises.
        """
        with self._lock:
            proc = self._process
            self._process = None
            self.stats.managed_pid = None
        if proc is None or proc.poll() is not None:
            return
        try:
            log.info("Stopping managed proxy subprocess (PID %d)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("Managed proxy did not exit in 10s — killing")
                proc.kill()
                proc.wait(timeout=5)
        except Exception as e:  # noqa: BLE001
            log.debug("stop_managed: %s", e)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _cooldown_elapsed(self) -> bool:
        if self.stats.last_restart_at <= 0:
            return True
        return (time.time() - self.stats.last_restart_at) >= self.restart_cooldown_s

    def _attempt_restart(self) -> bool:
        with self._lock:
            self.stats.restart_attempts += 1
            self.stats.last_restart_at = time.time()
            # If we have a dead managed process, reap it before spawning.
            if self._process is not None and self._process.poll() is not None:
                log.info(
                    "Previous managed proxy exited with code %s — respawning",
                    self._process.returncode,
                )
                self._process = None
                self.stats.managed_pid = None
            if self._process is None:
                proc = self._spawn_subprocess()
                if proc is None:
                    self.stats.restart_failures += 1
                    self._record_health(
                        "error",
                        "Proxy auto-start failed: could not spawn subprocess",
                    )
                    return False
                self._process = proc
                self.stats.managed_pid = proc.pid
        # Wait outside the lock so health checks don't block.
        ok = self._wait_until_healthy(self.start_timeout_s)
        if ok:
            self.stats.restart_successes += 1
            self._record_health(
                "info",
                f"Proxy auto-start succeeded (PID {self.stats.managed_pid})",
            )
            log.info("Managed proxy is UP (PID %d)", self.stats.managed_pid or -1)
        else:
            self.stats.restart_failures += 1
            self._record_health(
                "error",
                f"Proxy auto-start did not become healthy in "
                f"{self.start_timeout_s}s",
            )
        return ok

    def _spawn_subprocess(self) -> subprocess.Popen | None:
        """Spawn ``npx copilot-api@latest start --port <port>`` detached.

        Returns the Popen handle or None if spawn failed.  Never raises.
        On Windows the child is started with CREATE_NO_WINDOW to avoid a
        visible console flash, and CREATE_NEW_PROCESS_GROUP so the
        supervisor can terminate cleanly without affecting the parent.
        """
        cmd = self._resolve_spawn_cmd()
        if cmd is None:
            log.error("Cannot auto-start proxy: npx/npm not on PATH")
            return None
        creationflags = 0
        if sys.platform == "win32":
            # CREATE_NO_WINDOW=0x08000000, CREATE_NEW_PROCESS_GROUP=0x00000200
            creationflags = 0x08000000 | 0x00000200
        try:
            proc = subprocess.Popen(  # noqa: S603 — command set from config
                list(cmd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
                shell=False,
            )
            log.info(
                "Spawned managed proxy subprocess (PID %d): %s",
                proc.pid, " ".join(cmd),
            )
            return proc
        except FileNotFoundError:
            log.error("Proxy spawn failed: executable not found (%s)", cmd[0])
            return None
        except OSError as e:
            log.error("Proxy spawn failed: %s", e)
            return None

    def _resolve_spawn_cmd(self) -> tuple[str, ...] | None:
        if self.spawn_cmd is not None:
            return self.spawn_cmd
        # On Windows, npx ships as npx.cmd — shutil.which finds it reliably.
        exe = shutil.which("npx") or shutil.which("npx.cmd")
        if not exe:
            return None
        return (exe, "copilot-api@latest", "start", "--port", str(self.port))

    def _wait_until_healthy(self, timeout_s: int) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if is_proxy_healthy(self.base_url, timeout=self.health_timeout_s):
                self.stats.last_healthy_at = time.time()
                return True
            # Detect early exit of the managed subprocess to fail fast.
            with self._lock:
                proc = self._process
            if proc is not None and proc.poll() is not None:
                log.error(
                    "Managed proxy exited early with code %s",
                    proc.returncode,
                )
                return False
            time.sleep(1.0)
        return False

    def _record_health(self, severity: str, message: str) -> bool:
        """Best-effort write to the shared HealthLog.  Returns True on
        success, False otherwise; never raises.
        """
        if self.health_log is None:
            return False
        try:
            self.health_log.record(
                "proxy_supervisor", severity, message,
                source="proxy_supervisor",
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.debug("health_log.record failed: %s", e)
            return False
