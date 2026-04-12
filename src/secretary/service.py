"""Service support — PID file, signal handling, and preflight checks.

Enables running the watcher as a Windows Service (via NSSM) with:
- PID file for external status checks
- SIGBREAK handler for NSSM graceful shutdown
- Preflight proxy health check before entering the watcher loop
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("secretary.service")


def write_pidfile(data_dir: Path) -> Path:
    """Write PID file for external status checks. Returns the path."""
    pidfile = data_dir / "secretary.pid"
    data_dir.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    log.info("PID file: %s (PID %d)", pidfile, os.getpid())
    return pidfile


def cleanup_pidfile(data_dir: Path) -> None:
    """Remove PID file on clean shutdown."""
    pidfile = data_dir / "secretary.pid"
    if pidfile.exists():
        pidfile.unlink()
        log.debug("PID file removed")


def install_sigbreak_handler(stop_callback) -> None:
    """Install SIGBREAK handler for NSSM graceful shutdown on Windows.

    NSSM sends CTRL_BREAK_EVENT to stop the process. Python exposes this
    as signal.SIGBREAK on Windows. The callback should set a stop flag.
    """
    if sys.platform == "win32" and hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda signum, frame: stop_callback())
        log.debug("SIGBREAK handler installed for NSSM support")


def wait_for_proxy(
    url: str = "http://localhost:4141/v1/models",
    timeout: int = 120,
    interval: int = 5,
) -> bool:
    """Block until copilot-api proxy is reachable, or timeout.

    Returns True if proxy is up, False if timeout exceeded.
    """
    log.info("Waiting for copilot-api proxy at %s (timeout=%ds)...", url, timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=3)  # noqa: S310
            log.info("Copilot-api proxy is UP")
            return True
        except urllib.error.URLError as e:
            remaining = int(deadline - time.monotonic())
            if isinstance(e.reason, ConnectionRefusedError):
                log.debug("Proxy refused connection, retrying in %ds (%ds left)...", interval, remaining)
            else:
                log.debug("Network error: %s, retrying in %ds (%ds left)...", e.reason, interval, remaining)
            time.sleep(interval)
        except Exception as e:
            remaining = int(deadline - time.monotonic())
            log.debug("Proxy not ready (%s), retrying in %ds (%ds left)...", type(e).__name__, interval, remaining)
            time.sleep(interval)
    log.error("Copilot-api proxy not reachable after %ds", timeout)
    return False
