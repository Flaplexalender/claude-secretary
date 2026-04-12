"""Multi-instance coordination — file-based task claiming and shared results.

Enables multiple Secretary instances to run in parallel without duplicating
work.  Uses atomic file creation for task claiming (cross-platform safe) and
a shared results directory for inter-instance communication.

Coordination layout under data/shared/:
    instances.json    — registry of active instances (heartbeat-based)
    queue/            — per-task claim files ({hash}.claim)
    results/          — per-task result files ({hash}.json)
    metrics.json      — aggregated cross-instance metrics

Claiming protocol (race-safe):
    1. Instance hashes task prompt → deterministic ID
    2. Tries to create queue/{hash}.claim (exclusive create, fails if exists)
    3. If created → instance owns the task → execute
    4. If exists → another instance claimed it → skip
    5. On completion → write results/{hash}.json
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("secretary.coordinator")

# Instances not seen for this many seconds are considered dead
_INSTANCE_STALE_SECONDS = 300  # 5 minutes


@dataclass
class InstanceInfo:
    """An active Secretary instance."""
    instance_id: str
    role: str = ""                    # researcher, triager, builder, monitor, "" = generalist
    pid: int = 0
    started_at: str = ""
    last_seen: str = ""
    tasks_completed: int = 0
    tasks_failed: int = 0
    total_turns: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0


@dataclass
class TaskClaim:
    """A claimed task."""
    task_hash: str
    instance_id: str
    claimed_at: str
    prompt_preview: str = ""


@dataclass
class TaskResult:
    """Result of a completed task, shared across instances."""
    task_hash: str
    instance_id: str
    prompt: str
    success: bool
    output_preview: str = ""
    error: str | None = None
    completed_at: str = ""
    duration_s: float = 0.0
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tier: str = ""
    model: str = ""
    task_id: str = ""  # human-readable task identifier for depends_on resolution


class Coordinator:
    """File-based coordinator for multi-instance task distribution.

    All state is under ``shared_dir`` (default: data/shared/).
    Thread-safe and multi-process-safe via atomic file ops.
    """

    def __init__(self, shared_dir: Path, instance_id: str, role: str = ""):
        """Initialize coordinator with shared directory, instance ID, and optional role."""
        self.shared_dir = shared_dir
        self.instance_id = instance_id
        self.role = role
        self._queue_dir = shared_dir / "queue"
        self._results_dir = shared_dir / "results"
        self._instances_file = shared_dir / "instances.json"
        self._metrics_file = shared_dir / "metrics.json"

        # Create directories
        self._queue_dir.mkdir(parents=True, exist_ok=True)
        self._results_dir.mkdir(parents=True, exist_ok=True)

    # ── Instance registry ─────────────────────────────────────

    def register(self) -> None:
        """Register this instance in the shared registry."""
        now = datetime.now(timezone.utc).isoformat()
        info = InstanceInfo(
            instance_id=self.instance_id,
            role=self.role,
            pid=os.getpid(),
            started_at=now,
            last_seen=now,
        )
        registry = self._load_instances()
        registry[self.instance_id] = asdict(info)
        self._save_instances(registry)
        log.debug("Registered instance '%s' (role=%s, pid=%d)", self.instance_id, self.role or "generalist", os.getpid())

    def heartbeat(self, stats: dict[str, Any] | None = None) -> None:
        """Update this instance's heartbeat timestamp and optional stats."""
        registry = self._load_instances()
        entry = registry.get(self.instance_id, {})
        entry["last_seen"] = datetime.now(timezone.utc).isoformat()
        if stats:
            entry.update(stats)
        registry[self.instance_id] = entry
        self._save_instances(registry)

    def deregister(self) -> None:
        """Remove this instance from the registry."""
        registry = self._load_instances()
        registry.pop(self.instance_id, None)
        self._save_instances(registry)
        log.debug("Deregistered instance '%s'", self.instance_id)

    def get_active_instances(self) -> list[dict[str, Any]]:
        """Return list of instances that have heartbeated recently."""
        registry = self._load_instances()
        now = time.time()
        active = []
        for inst_id, info in registry.items():
            last_seen = info.get("last_seen", "")
            if last_seen:
                try:
                    ts = datetime.fromisoformat(last_seen).timestamp()
                    if now - ts < _INSTANCE_STALE_SECONDS:
                        active.append(info)
                        continue
                except (ValueError, TypeError):
                    pass
            # Stale — skip but don't auto-remove (let owner clean up)
        return active

    def _load_instances(self) -> dict[str, Any]:
        """Load the shared instance registry from disk (JSON)."""
        if self._instances_file.exists():
            try:
                return json.loads(self._instances_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_instances(self, data: dict[str, Any]) -> None:
        """Atomically write the instance registry to disk."""
        _atomic_write_json(self._instances_file, data)

    # ── Task claiming ─────────────────────────────────────────

    @staticmethod
    def task_hash(prompt: str) -> str:
        """Deterministic hash for a task prompt."""
        return hashlib.sha256(prompt.encode()).hexdigest()[:16]

    def try_claim(self, prompt: str, task_hash: str | None = None) -> bool:
        """Attempt to exclusively claim a task. Returns True if claimed.

        Uses atomic file creation — if the claim file already exists,
        another instance owns it.
        """
        h = task_hash or self.task_hash(prompt)
        claim_path = self._queue_dir / f"{h}.claim"
        claim = TaskClaim(
            task_hash=h,
            instance_id=self.instance_id,
            claimed_at=datetime.now(timezone.utc).isoformat(),
            prompt_preview=prompt[:120],
        )
        try:
            # Exclusive create — fails if file exists (atomic on all platforms)
            fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(claim), f)
            log.debug("Claimed task %s: %s", h, prompt[:60])
            return True
        except FileExistsError:
            log.debug("Task %s already claimed by another instance", h)
            return False
        except OSError as e:
            log.warning("Failed to claim task %s: %s", h, e)
            return False

    def is_claimed(self, prompt: str, task_hash: str | None = None) -> bool:
        """Check if a task is already claimed (by any instance)."""
        h = task_hash or self.task_hash(prompt)
        return (self._queue_dir / f"{h}.claim").exists()

    def get_claim_owner(self, prompt: str, task_hash: str | None = None) -> str | None:
        """Return the instance_id that claimed a task, or None."""
        h = task_hash or self.task_hash(prompt)
        claim_path = self._queue_dir / f"{h}.claim"
        if claim_path.exists():
            try:
                data = json.loads(claim_path.read_text(encoding="utf-8"))
                return data.get("instance_id")
            except (json.JSONDecodeError, OSError):
                return "unknown"
        return None

    def release_claim(self, prompt: str, task_hash: str | None = None) -> None:
        """Release a task claim (e.g., on timeout or instance shutdown)."""
        h = task_hash or self.task_hash(prompt)
        claim_path = self._queue_dir / f"{h}.claim"
        claim_path.unlink(missing_ok=True)

    def release_all_claims(self) -> int:
        """Release all claims owned by this instance. Returns count released."""
        count = 0
        for claim_file in self._queue_dir.glob("*.claim"):
            try:
                data = json.loads(claim_file.read_text(encoding="utf-8"))
                if data.get("instance_id") == self.instance_id:
                    claim_file.unlink()
                    count += 1
            except (json.JSONDecodeError, OSError):
                pass
        return count

    def cleanup_stale_claims(self, max_age_seconds: int = 3600) -> int:
        """Remove claims older than max_age_seconds. Returns count removed."""
        now = time.time()
        count = 0
        for claim_file in self._queue_dir.glob("*.claim"):
            try:
                data = json.loads(claim_file.read_text(encoding="utf-8"))
                claimed_at = data.get("claimed_at", "")
                ts = datetime.fromisoformat(claimed_at).timestamp()
                if now - ts > max_age_seconds:
                    claim_file.unlink()
                    count += 1
                    log.info("Cleaned stale claim: %s (age: %ds)", claim_file.name, now - ts)
            except (json.JSONDecodeError, OSError, ValueError):
                pass
        return count

    # ── Results sharing ───────────────────────────────────────

    def publish_result(self, result: TaskResult) -> None:
        """Publish a task result for other instances to see."""
        path = self._results_dir / f"{result.task_hash}.json"
        _atomic_write_json(path, asdict(result))

    def get_result(self, prompt: str, task_hash: str | None = None) -> TaskResult | None:
        """Get the shared result for a task, if any."""
        h = task_hash or self.task_hash(prompt)
        path = self._results_dir / f"{h}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return TaskResult(**data)
            except (json.JSONDecodeError, OSError, TypeError):
                return None
        return None

    def get_all_results(self) -> list[TaskResult]:
        """Get all shared results from this coordination cycle."""
        results = []
        for path in self._results_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                results.append(TaskResult(**data))
            except (json.JSONDecodeError, OSError, TypeError):
                pass
        return results

    def clear_cycle(self) -> tuple[int, int]:
        """Clear all claims and results for a new cycle. Returns (claims, results) removed."""
        claims = 0
        results = 0
        for f in self._queue_dir.glob("*.claim"):
            f.unlink(missing_ok=True)
            claims += 1
        for f in self._results_dir.glob("*.json"):
            f.unlink(missing_ok=True)
            results += 1
        return claims, results

    # ── Role-based filtering ──────────────────────────────────

    def filter_tasks_for_role(
        self, tasks: list[dict[str, Any]], role: str = ""
    ) -> list[dict[str, Any]]:
        """Filter campaign tasks that match this instance's role.

        Tasks can have a ``role`` field (str or list[str]):
            - If task has no role → any instance can take it
            - If task has role(s) → only matching instances take it
            - If instance has no role → it takes unroled tasks only when
              there are role-specific instances active for the roled tasks
        """
        effective_role = role or self.role
        if not effective_role:
            # Generalist: take all unroled tasks + roled tasks with no specialist online
            active = self.get_active_instances()
            active_roles = {inst.get("role", "") for inst in active if inst.get("role")}
            out = []
            for t in tasks:
                task_roles = _normalize_roles(t.get("role"))
                if not task_roles:
                    out.append(t)  # no role →  any instance
                elif not task_roles.intersection(active_roles):
                    out.append(t)  # no specialist online → generalist picks it up
            return out

        # Specialist: take matching-role tasks + unroled tasks
        out = []
        for t in tasks:
            task_roles = _normalize_roles(t.get("role"))
            if not task_roles or effective_role in task_roles:
                out.append(t)
        return out


def _normalize_roles(role_field: Any) -> set[str]:
    """Normalize a task's role field to a set of role strings."""
    if not role_field:
        return set()
    if isinstance(role_field, str):
        return {role_field}
    if isinstance(role_field, list):
        return set(role_field)
    return set()


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomic write — temp file + rename to prevent corruption."""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp).replace(path)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
