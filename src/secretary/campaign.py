"""Campaign YAML validation.

Validates campaign files before they're used by the watcher.
Catches structural issues, missing fields, and invalid values early.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_VALID_TIERS = {"free", "low", "medium", "high", "deep", "oracle"}
_VALID_TASK_KEYS = {
    "prompt", "task", "tier", "id", "depends_on", "schedule",
    "skip_if_recent", "priority", "timeout", "escalate_on_retry",
    "batch_compatible",  # Flag: task can be merged with adjacent same-tier tasks
}


@dataclass
class ValidationResult:
    """Result of campaign validation."""
    valid: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        """Record a validation error and mark the result as invalid."""
        self.errors.append(msg)
        self.valid = False

    def warn(self, msg: str) -> None:
        """Record a non-fatal validation warning."""
        self.warnings.append(msg)


def validate_campaign(path: Path | str) -> ValidationResult:
    """Validate a campaign YAML file structure and content."""
    result = ValidationResult()
    path = Path(path)

    if not path.exists():
        result.error(f"Campaign file not found: {path}")
        return result

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        result.error(f"Cannot read {path}: {e}")
        return result

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        result.error(f"Invalid YAML in {path}: {e}")
        return result

    if not isinstance(data, dict):
        result.error(f"Campaign must be a YAML mapping, got {type(data).__name__}")
        return result

    if "tasks" not in data:
        result.error("Campaign missing 'tasks' key")
        return result

    tasks = data["tasks"]
    if not isinstance(tasks, list):
        result.error(f"'tasks' must be a list, got {type(tasks).__name__}")
        return result

    if len(tasks) == 0:
        result.warn("Campaign has no tasks")
        return result

    seen_ids: set[str] = set()
    dependency_targets: set[str] = set()

    for i, task in enumerate(tasks, 1):
        prefix = f"Task {i}"

        if not isinstance(task, dict):
            result.error(f"{prefix}: must be a mapping, got {type(task).__name__}")
            continue

        # Check for unknown keys
        unknown = set(task.keys()) - _VALID_TASK_KEYS
        if unknown:
            result.warn(f"{prefix}: unknown keys: {', '.join(sorted(unknown))}")

        # Prompt is required
        prompt = task.get("prompt", task.get("task", ""))
        if not prompt or not str(prompt).strip():
            result.error(f"{prefix}: missing or empty 'prompt'")

        # Tier validation
        tier = task.get("tier")
        if tier is not None and tier not in _VALID_TIERS:
            result.error(f"{prefix}: invalid tier '{tier}' (must be one of: {', '.join(sorted(_VALID_TIERS))})")

        # ID uniqueness
        task_id = task.get("id")
        if task_id is not None:
            if task_id in seen_ids:
                result.error(f"{prefix}: duplicate id '{task_id}'")
            seen_ids.add(task_id)

        # Dependency tracking — normalize list to string (YAML [x] → x)
        depends_on = task.get("depends_on")
        if depends_on is not None:
            if isinstance(depends_on, list):
                depends_on = depends_on[0] if depends_on else None
            if depends_on is not None:
                dependency_targets.add(depends_on)

        # batch_compatible validation
        batch_compat = task.get("batch_compatible")
        if batch_compat is not None and not isinstance(batch_compat, bool):
            result.error(f"{prefix}: 'batch_compatible' must be a boolean, got {type(batch_compat).__name__}")

        # Priority validation
        priority = task.get("priority")
        if priority is not None and not isinstance(priority, (int, float)):
            result.error(f"{prefix}: 'priority' must be a number, got {type(priority).__name__}")

        # Timeout validation
        timeout = task.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout < 0:
                result.error(f"{prefix}: 'timeout' must be a non-negative number")

        # Schedule validation
        schedule = task.get("schedule")
        if schedule is not None:
            _validate_schedule(schedule, prefix, result)

    # Check dependency references
    missing_deps = dependency_targets - seen_ids
    if missing_deps:
        result.error(f"Unresolved dependencies: {', '.join(sorted(missing_deps))}")

    # Check for circular dependencies (DFS cycle detection)
    dep_graph: dict[str, str | None] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = task.get("id")
        dep = task.get("depends_on")
        # Normalize list form (["x"] → "x")
        if isinstance(dep, list):
            dep = dep[0] if dep else None
        if task_id is not None:
            dep_graph[task_id] = dep

    def _has_cycle(start: str) -> list[str] | None:
        """DFS cycle detection starting from ``start``. Returns cycle path or None."""
        visited: set[str] = set()
        path: list[str] = []
        node: str | None = start
        while node is not None:
            if node in visited:
                path.append(node)
                return path
            visited.add(node)
            path.append(node)
            node = dep_graph.get(node)
        return None

    for task_id in dep_graph:
        cycle = _has_cycle(task_id)
        if cycle:
            result.error(f"Circular dependency: {' → '.join(cycle)}")
            break

    return result


def _validate_schedule(schedule: str, prefix: str, result: ValidationResult) -> None:
    """Validate a schedule expression."""
    rules = [r.strip() for r in str(schedule).split(";") if r.strip()]
    for rule in rules:
        if rule.startswith("hours:"):
            ranges_str = rule[6:]
            for rng in ranges_str.split(","):
                parts = rng.strip().split("-")
                if len(parts) != 2:
                    result.error(f"{prefix}: invalid hours range '{rng}' (expected 'start-end')")
                    continue
                try:
                    start, end = int(parts[0]), int(parts[1])
                    if not (0 <= start <= 23 and 0 <= end <= 24):
                        result.error(f"{prefix}: hours must be 0-24, got {start}-{end}")
                except ValueError:
                    result.error(f"{prefix}: invalid hours '{rng}' (not integers)")
        elif rule in ("weekdays", "weekends"):
            pass  # valid
        else:
            result.warn(f"{prefix}: unknown schedule rule '{rule}'")
