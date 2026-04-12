"""Self-improvement pipeline.

Sandbox → Agent run → Test → Promote (if tests pass + approved).
Uses the Claude SDK agent to attempt improvements on its own codebase.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from . import direct_agent
from .config import SecretaryConfig
from .direct_tools import build_tool_registry
from .pipeline_health import HealthLog

_log = logging.getLogger("secretary.self_improve")

# Directories to exclude from sandbox copies
_EXCLUDE_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".eggs", ".tox", ".ruff_cache",
    "dist", "build",
    "data",  # Runtime state — not part of source code
}
# Also skip directories whose name ends with .egg-info
_EXCLUDE_SUFFIXES = (".egg-info",)

# Maximum retry attempts when focused tests fail after agent changes
_MAX_FOCUSED_RETRIES = 2


def _kill_sandbox_processes(sandbox: Path) -> None:
    """Kill any processes whose command line references the sandbox directory.

    Uses taskkill /T to kill entire process trees (not just the parent).
    Searches ALL process types, not just python — run_command spawns cmd.exe,
    powershell, pytest, etc.
    """
    import subprocess
    sb_name = sandbox.name  # e.g. "claude-secretary_sandbox"
    try:
        # Get PIDs of ALL processes referencing the sandbox (not just python)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process | "
             f"Where-Object {{ $_.CommandLine -like '*{sb_name}*' }} | "
             f"Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=15,
        )
        my_pid = os.getpid()
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(line)
                if pid == my_pid:
                    continue  # don't kill ourselves
                _log.warning("Killing stale sandbox process PID %d (tree)", pid)
                # taskkill /T kills entire process tree
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                )
            except (ValueError, OSError):
                pass
    except Exception:
        _log.debug("Could not enumerate sandbox processes", exc_info=True)


@dataclass
class ImprovementResult:
    task: str
    sandbox_dir: str
    tests_passed: bool = False
    test_output: str = ""
    promoted: bool = False
    agent_result: str = ""
    changed_files: list[str] = field(default_factory=list)
    backup_dir: str = ""
    cost_usd: float = 0.0
    num_turns: int = 0
    error: str | None = None


def _copy_to_sandbox(source: Path, sandbox: Path) -> None:
    """Copy project to sandbox, excluding noise directories."""
    if sandbox.exists():
        _log.warning(
            "Sandbox %s already exists (stale from previous run?) — removing",
            sandbox,
        )
        # Unlink venv junction/symlink first to avoid WinError 32
        _sb_venv = sandbox / ".venv"
        if _sb_venv.is_symlink() or _sb_venv.is_junction():
            _sb_venv.unlink() if _sb_venv.is_symlink() else os.rmdir(str(_sb_venv))
        shutil.rmtree(sandbox, ignore_errors=True)
        # If rmtree couldn't fully clean (locked files), kill stale processes and retry
        if sandbox.exists():
            _kill_sandbox_processes(sandbox)
            import time
            time.sleep(2)
            shutil.rmtree(sandbox, ignore_errors=True)
        # Last resort: one more retry with longer delay
        if sandbox.exists():
            import time
            time.sleep(3)
            shutil.rmtree(sandbox, ignore_errors=True)
        if sandbox.exists():
            raise RuntimeError(f"Cannot remove stale sandbox {sandbox} (files locked by another process)")

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        return {
            c for c in contents
            if c in _EXCLUDE_DIRS or any(c.endswith(s) for s in _EXCLUDE_SUFFIXES)
        }

    shutil.copytree(source, sandbox, ignore=_ignore)


def _detect_changes(source: Path, sandbox: Path) -> list[str]:
    """Compare sandbox against source, return list of changed file descriptions."""
    changes = []
    for sandbox_file in sandbox.rglob("*"):
        if sandbox_file.is_dir():
            continue
        if any(part in _EXCLUDE_DIRS for part in sandbox_file.parts):
            continue
        if any(part.endswith(s) for part in sandbox_file.parts for s in _EXCLUDE_SUFFIXES):
            continue

        rel = sandbox_file.relative_to(sandbox)
        source_file = source / rel

        if not source_file.exists():
            changes.append(f"NEW: {rel}")
        elif source_file.read_bytes() != sandbox_file.read_bytes():
            changes.append(f"MOD: {rel}")

    # Check for deletions
    for source_file in source.rglob("*"):
        if source_file.is_dir():
            continue
        if any(part in _EXCLUDE_DIRS for part in source_file.parts):
            continue
        if any(part.endswith(s) for part in source_file.parts for s in _EXCLUDE_SUFFIXES):
            continue
        rel = source_file.relative_to(source)
        if not (sandbox / rel).exists():
            changes.append(f"DEL: {rel}")

    return changes


async def _get_test_python(sandbox: Path, source: Path) -> tuple[str, dict[str, str]]:
    """Set up sandbox venv and return (python_exe, env_dict) for test runs."""
    import sys

    if sys.platform == "win32":
        venv_python = source / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = source / ".venv" / "bin" / "python"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable

    sandbox_venv = sandbox / ".venv"
    if not sandbox_venv.exists():
        # Try to link the source project's venv instead of creating a new one.
        # Self-improve only modifies source files, never dependencies, so sharing
        # the venv is safe and saves ~3 minutes on Windows.
        source_venv = source / ".venv"
        if source_venv.exists():
            try:
                if sys.platform == "win32":
                    # NTFS junction — no admin/developer mode required
                    import subprocess
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(sandbox_venv), str(source_venv)],
                        check=True, capture_output=True,
                    )
                else:
                    sandbox_venv.symlink_to(source_venv, target_is_directory=True)
                _log.info("Sandbox venv: linked from source project (fast)")
            except (OSError, subprocess.CalledProcessError):
                _log.info("Venv link failed, creating isolated sandbox venv: %s", sandbox_venv)
                proc = await asyncio.create_subprocess_exec(
                    python_exe, "-m", "venv", "--system-site-packages", str(sandbox_venv),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
                    if proc.returncode != 0:
                        raise RuntimeError(stdout.decode("utf-8", errors="replace"))
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise RuntimeError("Sandbox venv creation timed out")
        else:
            _log.info("Creating isolated sandbox venv: %s", sandbox_venv)
            proc = await asyncio.create_subprocess_exec(
                python_exe, "-m", "venv", "--system-site-packages", str(sandbox_venv),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
                if proc.returncode != 0:
                    raise RuntimeError(stdout.decode("utf-8", errors="replace"))
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("Sandbox venv creation timed out")

    if sys.platform == "win32":
        sandbox_python = sandbox_venv / "Scripts" / "python.exe"
    else:
        sandbox_python = sandbox_venv / "bin" / "python"
    test_python = str(sandbox_python) if sandbox_python.exists() else python_exe

    env = os.environ.copy()
    src_dir = sandbox / "src"
    env["PYTHONPATH"] = str(src_dir) + os.pathsep + env.get("PYTHONPATH", "")
    return test_python, env


async def _run_tests_ci(
    sandbox: Path, source: Path, test_files: list[str] | None = None, timeout: int = 300,
) -> tuple[bool, str]:
    """Run tests via GitHub Actions CI instead of locally.

    Copies sandbox changes to a temp branch, pushes, waits for CI result.
    Returns (passed, output) matching the local _run_tests interface.
    """
    import subprocess
    import time as _time

    branch_name = f"test/self-improve-{int(_time.time())}"
    original_branch = None

    try:
        # 1. Get current branch name
        original_branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(source), text=True, timeout=10,
        ).strip()

        # 2. Create temp branch
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=str(source), check=True, capture_output=True, timeout=10,
        )

        # 3. Copy sandbox changes to source (overwrite)
        changes = _detect_changes(source, sandbox)
        if changes:
            for change in changes:
                action, rel_str = change.split(": ", 1)
                rel = Path(rel_str)
                if action in ("NEW", "MOD"):
                    src_file = sandbox / rel
                    dst_file = source / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src_file), str(dst_file))
                elif action == "DEL":
                    dst_file = source / rel
                    if dst_file.exists():
                        dst_file.unlink()

        # 4. Stage and commit
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(source), check=True, capture_output=True, timeout=10,
        )

        # Check if there's anything to commit
        status = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(source), capture_output=True, timeout=10,
        )
        if status.returncode == 0:
            # No changes to test — pass by default
            return True, "No changes detected — tests pass by default."

        subprocess.run(
            ["git", "commit", "-m", f"[ci-test] self-improve validation ({branch_name})"],
            cwd=str(source), check=True, capture_output=True, timeout=15,
        )

        # 5. Push temp branch and trigger CI via workflow_dispatch
        subprocess.run(
            ["git", "push", "origin", branch_name],
            cwd=str(source), check=True, capture_output=True, timeout=30,
        )
        _log.info("Pushed temp branch %s for CI testing", branch_name)

        # Trigger workflow explicitly (push to non-master branches won't auto-trigger)
        subprocess.run(
            ["gh", "workflow", "run", "test.yml", "--ref", branch_name],
            cwd=str(source), check=True, capture_output=True, timeout=15,
        )

        # 6. Wait for CI run to appear and complete
        #    Poll until the run starts (may take a few seconds)
        run_id = None
        for _attempt in range(15):
            await asyncio.sleep(4)
            try:
                out = subprocess.check_output(
                    ["gh", "run", "list", "--branch", branch_name,
                     "--limit", "1", "--json", "databaseId,status",
                     "--jq", ".[0]"],
                    cwd=str(source), text=True, timeout=15,
                )
                if out.strip():
                    import json as _json
                    run_info = _json.loads(out.strip())
                    run_id = run_info.get("databaseId")
                    if run_id:
                        break
            except Exception:
                continue

        if not run_id:
            return False, "CI run did not start within 60s — could not verify tests."

        # 7. Wait for CI completion
        _log.info("Waiting for CI run %s to complete...", run_id)
        proc = await asyncio.create_subprocess_exec(
            "gh", "run", "watch", str(run_id), "--exit-status",
            cwd=str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            ci_output = stdout.decode("utf-8", errors="replace") if stdout else ""
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"CI run timed out after {timeout}s"

        passed = proc.returncode == 0

        # 8. If failed, get failure details
        if not passed:
            try:
                fail_out = subprocess.check_output(
                    ["gh", "run", "view", str(run_id), "--log-failed"],
                    cwd=str(source), text=True, timeout=30,
                )
                # Extract test failure lines
                failure_lines = []
                for line in fail_out.splitlines():
                    if any(kw in line for kw in ["FAILED", "ERROR", "assert", "AssertionError"]):
                        failure_lines.append(line.split("\t")[-1] if "\t" in line else line)
                tail = fail_out.splitlines()[-30:]
                output = "CI TEST FAILURE:\n" + "\n".join(failure_lines[-20:] + tail)
            except Exception:
                output = f"CI tests failed (run {run_id}) but could not fetch failure log."
        else:
            output = f"CI tests passed (run {run_id})."

        return passed, output

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else str(e)
        return False, f"CI test setup failed: {stderr}"
    except Exception as e:
        return False, f"CI test error: {e}"
    finally:
        # 9. Cleanup: switch back to original branch, delete temp branch
        try:
            if original_branch:
                # Reset any uncommitted changes before switching
                subprocess.run(
                    ["git", "checkout", "--", "."],
                    cwd=str(source), capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["git", "checkout", original_branch],
                    cwd=str(source), capture_output=True, timeout=10,
                )
            subprocess.run(
                ["git", "branch", "-D", branch_name],
                cwd=str(source), capture_output=True, timeout=10,
            )
            # Delete remote branch
            subprocess.run(
                ["git", "push", "origin", "--delete", branch_name],
                cwd=str(source), capture_output=True, timeout=15,
            )
        except Exception as cleanup_err:
            _log.warning("CI test cleanup failed: %s", cleanup_err)


async def _run_tests(sandbox: Path, source: Path, timeout: int = 300) -> tuple[bool, str]:
    """Run the full test suite via CI (GitHub Actions)."""
    return await _run_tests_ci(sandbox, source, timeout=timeout)


async def _run_tests_subset(
    sandbox: Path, source: Path, test_files: list[str], timeout: int = 300,
) -> tuple[bool, str]:
    """Run tests via CI. The full suite runs regardless (CI doesn't support subsets)."""
    return await _run_tests_ci(sandbox, source, test_files=test_files, timeout=timeout)


def _promote_changes(source: Path, sandbox: Path, changes: list[str]) -> None:
    """Copy changed files from sandbox back to source."""
    for change in changes:
        action, rel_str = change.split(": ", 1)
        rel = Path(rel_str)

        if action in ("NEW", "MOD"):
            src_file = sandbox / rel
            dst_file = source / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
        elif action == "DEL":
            dst_file = source / rel
            if dst_file.exists():
                dst_file.unlink()


def _git_commit_promoted(source: Path, changes: list[str], task: str, description: str = "") -> str | None:
    """Git add+commit promoted changes. Returns commit hash or None on failure."""
    import subprocess

    # Collect file paths for git add
    files_to_add: list[str] = []
    files_to_rm: list[str] = []
    for change in changes:
        action, rel_str = change.split(": ", 1)
        if action in ("NEW", "MOD"):
            files_to_add.append(rel_str)
        elif action == "DEL":
            files_to_rm.append(rel_str)

    try:
        if files_to_add:
            subprocess.run(
                ["git", "add", "--"] + files_to_add,
                cwd=str(source), check=True, capture_output=True, timeout=30,
            )
        if files_to_rm:
            subprocess.run(
                ["git", "rm", "--cached", "--"] + files_to_rm,
                cwd=str(source), check=True, capture_output=True, timeout=30,
            )

        # Build concise commit message
        file_summary = ", ".join(Path(f).name for f in (files_to_add + files_to_rm)[:5])
        if len(files_to_add) + len(files_to_rm) > 5:
            file_summary += f" (+{len(files_to_add) + len(files_to_rm) - 5} more)"
        subject = description[:120] if description else task[:120]
        msg = (
            f"[self-improve] {subject}\n\n"
            f"Auto-promoted by self-improvement pipeline.\n"
            f"Files: {file_summary}"
        )
        result = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=str(source), check=True, capture_output=True, timeout=30,
        )
        # Extract commit hash
        out = result.stdout.decode("utf-8", errors="replace")
        for line in out.splitlines():
            if line.strip().startswith("["):
                # e.g. "[main abc1234] commit message"
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1].rstrip("]")
        return "committed"  # Fallback if hash not parseable
    except Exception as e:
        _log.error("Git commit failed for promoted changes: %s", e)
        HealthLog().record("pipeline_error", "error", f"Git commit failed: {e}", source="self_improve._git_commit_promoted")
        # Return None to signal failure so caller can implement fallback (e.g. dry-run mode)
        return None


def _backup_originals(source: Path, changes: list[str]) -> Path:
    """Create a backup of files that will be overwritten during promotion."""
    backup = source.parent / f"{source.name}_backup"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True)
    for change in changes:
        action, rel_str = change.split(": ", 1)
        if action == "MOD":
            src_file = source / rel_str
            dst_file = backup / rel_str
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
    return backup


def rollback(source: Path, backup: Path) -> list[str]:
    """Restore original files from a backup created by _backup_originals."""
    restored = []
    for f in backup.rglob("*"):
        if f.is_dir():
            continue
        rel = f.relative_to(backup)
        dst = source / rel
        shutil.copy2(f, dst)
        restored.append(str(rel))
    shutil.rmtree(backup, ignore_errors=True)
    return restored


def _log_to_run_log(
    config: SecretaryConfig,
    task: str,
    result: ImprovementResult,
) -> None:
    """Log self-improvement result to run_log.jsonl for analytics visibility.

    Separated from improve() to make error handling explicit and testable.
    Logs at ERROR level with traceback on failure (not silently swallowed).
    """
    try:
        from .run_log import RunLog, RunLogEntry
        run_log = RunLog(config.data_path / "run_log.jsonl")
        _si_tier = config.self_improve.tier
        _model_name = config.routing.tiers.get(
            _si_tier,
            next(iter(config.routing.tiers.values())),
        ).model
        run_log.append(RunLogEntry(
            timestamp=RunLog.now(),
            cycle=0,  # 0 = one-shot / CLI invocation
            task=f"[self-improve] {task[:180]}",
            tier=_si_tier,
            model=_model_name,
            success=result.tests_passed and not result.error,
            output_preview=(
                f"changes={len(result.changed_files)}, "
                f"tests={'PASS' if result.tests_passed else 'FAIL'}, "
                f"promoted={result.promoted}, "
                f"files={','.join(result.changed_files[:5])}"
            )[:500],
            error=result.error,
            duration_s=0.0,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
            tools_used=["file_read", "file_write"],
        ))
    except Exception:
        _log.error(
            "Failed to log self-improvement result to run_log.jsonl:\n%s",
            traceback.format_exc(),
        )
        HealthLog().record("pipeline_error", "error", "Failed to log self-improvement result to run_log", source="self_improve._log_result")


def _map_changed_to_tests(changed: list[str], sandbox: Path) -> list[str]:
    """Map changed source files to related test files for focused testing.

    Uses prefix matching: a change to ``foo.py`` picks up both
    ``test_foo.py`` **and** ``test_foo_*.py`` variants.
    """
    test_files: set[str] = set()
    tests_dir = sandbox / "tests"
    if not tests_dir.is_dir():
        return []
    for change in changed:
        _, rel = change.split(": ", 1)
        stem = Path(rel).stem  # e.g. goal_decomposition
        prefix = f"test_{stem}"
        for candidate in tests_dir.iterdir():
            if candidate.is_file() and candidate.suffix == ".py" and (
                candidate.stem == prefix or candidate.stem.startswith(prefix + "_")
            ):
                test_files.add(str(candidate.relative_to(sandbox)).replace("\\", "/"))
    return sorted(test_files)


def _extract_failure_summary(test_output: str) -> str:
    """Extract structured failure info: test names + assertion messages."""
    lines = test_output.splitlines()
    failed_tests: list[str] = []
    assertion_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("FAILED "):
            failed_tests.append(stripped)
        elif "AssertionError" in stripped or "assert " in stripped:
            assertion_lines.append(stripped)
        elif stripped.startswith("E ") and len(stripped) < 200:
            assertion_lines.append(stripped)
    summary_parts: list[str] = []
    if failed_tests:
        summary_parts.append("FAILING TESTS:\n" + "\n".join(failed_tests[:10]))
    if assertion_lines:
        summary_parts.append("KEY ERRORS:\n" + "\n".join(assertion_lines[:10]))
    # Always include raw tail for context
    summary_parts.append("RAW OUTPUT (last 1000 chars):\n" + test_output[-1000:])
    return "\n\n".join(summary_parts)


async def _retry_with_failure_context(
    sandbox: Path,
    project: Path,
    config: "SecretaryConfig",
    task: str,
    change_plan: dict | None,
    test_output: str,
    prev_changed: list[str],
    tier: str,
    tools: list[str],
    max_turns: int,
    timeout: int,
    *,
    attempt: int = 1,
) -> dict | None:
    """Re-run the sandbox agent with test failure feedback, return new changes or None."""
    from . import direct_agent

    failure_summary = _extract_failure_summary(test_output)
    attempt_warning = ""
    if attempt > 1:
        attempt_warning = (
            f"WARNING: This is retry attempt {attempt}/{_MAX_FOCUSED_RETRIES}. "
            "Previous retry also failed. Be MORE careful this time — "
            "re-read the test expectations before editing.\n\n"
        )
    fix_prompt = (
        f"{attempt_warning}"
        "Your previous code change BROKE tests. Fix it.\n\n"
        "SCOPE (violating = instant task failure):\n"
        "- ONLY modify files under src/secretary/\n"
        "- NEVER modify, delete, or create files in tests/\n"
        "- Do NOT weaken or delete existing test assertions\n"
        "- Fix the implementation, NOT the tests\n\n"
        f"{failure_summary}\n\n"
        f"Previously changed files: {prev_changed}\n\n"
        "INSTRUCTIONS:\n"
        "1. file_read the file(s) you changed to see the current (broken) state\n"
        "2. file_edit to fix the issue — make the tests pass\n"
        "3. Do NOT revert to the original — improve your change so it works\n\n"
        f"Original task: {task[:500]}\n"
    )
    if change_plan:
        fix_prompt += "\nORIGINAL CHANGE PLAN (stay focused on this — do NOT drift):\n"
        if isinstance(change_plan, dict):
            fix_prompt += (
                f"Target file: {change_plan.get('file', '?')}\n"
                f"Target function: {change_plan.get('function', '?')}\n"
                f"Description: {change_plan.get('description', '?')}\n"
            )
        else:
            # change_plan is a string (formatted plan text)
            fix_prompt += f"{str(change_plan)[:600]}\n"

    try:
        retry_result = await direct_agent.run(
            task=fix_prompt,
            config=config,
            force_tier=tier,
            tools=tools,
            max_turns=max_turns,
            max_tool_calls=30,
        )
        if retry_result.error:
            _log.warning("Retry agent returned error: %s", retry_result.error)
            HealthLog().record("pipeline_error", "warning", f"Sandbox retry agent error: {retry_result.error[:200]}", source="self_improve._retry_in_sandbox")
            return None
        raw = _detect_changes(project, sandbox)
        filtered = _filter_allowed_changes(raw)
        if not filtered:
            return None
        _log.info("Retry produced changes: %s", filtered)
        return {"changed_files": filtered}
    except Exception as e:
        _log.warning("Retry failed: %s", e)
        HealthLog().record("pipeline_error", "warning", f"Sandbox retry failed: {e}", source="self_improve._retry_in_sandbox")
        return None
def _filter_allowed_changes(changes: list[str]) -> list[str]:
    """Filter changes to only allowed directory (src/secretary/).
    
    Validates that sandbox modifications stay within safe boundaries to prevent
    the agent from accidentally modifying config files, data files, test files,
    or other critical infrastructure outside the intended scope.
    """
    allowed = []
    rejected = []
    for change in changes:
        _, rel = change.split(": ", 1)
        rel_posix = rel.replace("\\", "/")
        # Only allow changes to src/secretary/ directory
        # SECURITY: Block tests/, _tmp_* temporary files, and data/ directory
        if rel_posix.startswith("_tmp_"):
            rejected.append(change)
            _log.warning("Sandbox change blocked — temporary file outside scope: %s", change)
        elif rel_posix.startswith("data/"):
            rejected.append(change)
            _log.warning("Sandbox change blocked — data/ is off-limits: %s", change)
        elif rel_posix.startswith("tests/"):
            rejected.append(change)
            _log.warning("Sandbox change blocked — tests/ is read-only: %s", change)
        elif rel_posix.startswith("src/secretary/"):
            allowed.append(change)
        else:
            rejected.append(change)
            _log.warning("Sandbox change outside allowed scope — skipping: %s", change)
    
    # If all changes were rejected, log them for debugging but don't fail hard
    if rejected and not allowed:
        _log.error("All %d agent changes were outside allowed scope: %s", len(rejected), rejected[:3])
        HealthLog().record(
            "scope_violation", "error",
            f"All {len(rejected)} agent changes outside scope",
            source="self_improve._filter_allowed_changes",
            details=str(rejected[:3]),
        )
    
    return allowed


_CHANGE_PLAN_SYSTEM = """\
You are a code change planner. Given a task description and source file contents, \
produce a SPECIFIC change plan that another AI agent will execute using file_edit.

Your plan must include:
1. EXACTLY which file to modify
2. EXACTLY which function/method to change
3. WHAT the change is (add error handling, fix bug, add feature, etc.)
4. A BEFORE snippet (5-10 lines of the current code that will be replaced)
5. An AFTER snippet (the replacement code)

Rules:
- Pick ONE small, testable change. Not multiple changes.
- The BEFORE snippet must be an EXACT copy from the file (the agent uses exact string matching).
- The AFTER snippet must be valid Python that passes tests.
- Only suggest changes to src/secretary/ source files.
- Do NOT suggest changes to data files, config files, or the project root.
- You MAY suggest adding new test functions in tests/ to cover your change,
  but do NOT weaken or remove existing test assertions.
- Prefer improvements that add resilience: error handling, input validation, \
better logging, edge case fixes.
- IMPORTANT: If test files are shown below, study them to understand what the \
tests expect.  Your AFTER code must still pass all existing test assertions.

Respond with ONLY JSON (no markdown fences):
{
  "file": "src/secretary/module.py",
  "function": "function_name",
  "description": "What the change does and why",
  "before": "exact lines from the file to replace",
  "after": "replacement lines"
}\
"""


def _extract_function_source(file_content: str, func_name: str) -> str | None:
    """Extract a function/method's source from file content by name.

    Uses regex to find 'def func_name(' and extracts until the next def at same
    or lower indentation level.
    """
    import re
    lines = file_content.splitlines()
    pattern = re.compile(rf"^(\s*)(?:async\s+)?def\s+{re.escape(func_name)}\s*\(")
    start = None
    indent = None
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            start = i
            indent = len(m.group(1))
            break
    if start is None:
        return None
    # Find the end: next def/class at same or lower indent, or end of file
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped and not stripped.startswith("#"):
            cur_indent = len(lines[i]) - len(stripped)
            if cur_indent <= indent and (
                stripped.startswith("def ") or stripped.startswith("async def ")
                or stripped.startswith("class ")
            ):
                end = i
                break
    # Strip trailing blank lines
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])


def _fuzzy_find_before(before: str, real_content: str) -> str | None:
    """Try to find the real content matching a possibly-hallucinated 'before' snippet.

    Returns the matched real content if found, or None if no close match.
    """
    import difflib

    before_stripped = before.strip()

    # 1. Exact substring match (fastest)
    if before_stripped in real_content:
        return before_stripped

    # 2. Whitespace-normalized match
    before_lines = before_stripped.splitlines()
    real_lines = real_content.splitlines()
    before_norm = [ln.strip() for ln in before_lines if ln.strip()]
    for start in range(len(real_lines) - len(before_norm) + 1):
        window = [
            real_lines[start + j].strip()
            for j in range(len(before_norm))
            if real_lines[start + j].strip()
        ]
        if window == before_norm:
            # Found! Return the actual lines from the file (preserving indentation)
            end = start
            matched = 0
            while end < len(real_lines) and matched < len(before_norm):
                if real_lines[end].strip():
                    matched += 1
                end += 1
            return "\n".join(real_lines[start:end])

    # 3. Line-by-line fuzzy match — find best region using SequenceMatcher
    if len(before_lines) >= 3 and len(real_lines) >= 3:
        best_ratio = 0.0
        best_start = -1
        window_size = len(before_lines)
        for start in range(max(1, len(real_lines) - window_size - 2)):
            end = min(start + window_size + 2, len(real_lines))
            candidate = "\n".join(real_lines[start:end])
            ratio = difflib.SequenceMatcher(
                None, before_stripped, candidate,
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_start = start
        if best_ratio >= 0.75 and best_start >= 0:
            end = min(best_start + window_size + 2, len(real_lines))
            _log.info(
                "Fuzzy-matched change plan 'before' at lines %d-%d (%.0f%% similar)",
                best_start + 1, end, best_ratio * 100,
            )
            return "\n".join(real_lines[best_start:end])

    return None


async def _generate_change_plan(
    task: str,
    target_files: list[str] | None,
    project: Path,
    config: SecretaryConfig,
) -> str | None:
    """Use Haiku to generate a specific change plan before sandbox execution.

    Reads target files, sends them + task to Haiku, gets back a concrete
    before/after code change plan. Returns formatted plan string or None.
    """
    import json
    import re
    import anthropic
    from .config import _interpolate_env

    if not target_files:
        return None

    # Read target file contents — generous limits since Haiku handles 200K tokens
    # First target file gets full content; additional files capped at 10K
    file_contents: list[str] = []
    for idx, tf in enumerate(target_files[:3]):  # Max 3 files
        fp = project / tf
        if fp.exists() and fp.is_file():
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
                cap = 40_000 if idx == 0 else 10_000
                if len(text) > cap:
                    text = text[:cap] + "\n... (truncated)"
                file_contents.append(f"### {tf}\n```python\n{text}\n```")
            except Exception:
                continue

    if not file_contents:
        return None

    # Include relevant test files so planner sees test expectations
    # Deduplicate: multiple targets may map to the same test file
    test_contents: list[str] = []
    seen_tests: set[str] = set()
    tests_dir = project / "tests"
    for tf in target_files[:3]:
        stem = Path(tf).stem
        prefix = f"test_{stem}"
        if not tests_dir.is_dir():
            continue
        for test_file in sorted(tests_dir.iterdir()):
            if not (test_file.is_file() and test_file.suffix == ".py"):
                continue
            if test_file.stem != prefix and not test_file.stem.startswith(prefix + "_"):
                continue
            if test_file.name in seen_tests:
                continue
            seen_tests.add(test_file.name)
            try:
                test_text = test_file.read_text(encoding="utf-8", errors="replace")
                if len(test_text) > 8_000:
                    test_text = test_text[:8_000] + "\n... (truncated)"
                test_contents.append(
                    f"### tests/{test_file.name} (existing tests — DO NOT BREAK)\n"
                    f"```python\n{test_text}\n```"
                )
            except Exception:
                continue

    user_prompt = (
        f"## Task\n{task}\n\n"
        f"## Source Files\n" + "\n\n".join(file_contents)
    )
    if test_contents:
        user_prompt += "\n\n## Relevant Test Files\n" + "\n\n".join(test_contents)

    base_url = _interpolate_env(config.anthropic_base_url).rstrip("/")
    client = anthropic.AsyncAnthropic(
        base_url=base_url,
        api_key="copilot-proxy",
    )

    try:
        response = await client.messages.create(
            model="claude-haiku-4.5",
            max_tokens=1500,
            system=_CHANGE_PLAN_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = response.content[0].text if response.content else ""
        # Strip markdown fences (models often wrap JSON in ```json ... ```)
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text)
        # Parse JSON to validate it — handle models that add text after the JSON
        plan = None
        try:
            plan = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract first JSON object from the response
            brace_start = text.find("{")
            if brace_start >= 0:
                depth = 0
                for i, ch in enumerate(text[brace_start:], brace_start):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            try:
                                plan = json.loads(text[brace_start:i + 1])
                            except json.JSONDecodeError:
                                pass
                            break
        if plan is None:
            _log.warning("Change plan returned invalid/unparseable JSON")
            return None
        if not plan.get("file") or not plan.get("before") or not plan.get("after"):
            _log.warning("Change plan missing required fields: %s", list(plan.keys()))
            return None

        # Validate 'before' snippet against the real file with fuzzy fallback
        target_path = project / plan["file"]
        _func_name = plan.get("function", "")
        if target_path.exists() and target_path.is_file():
            try:
                real_content = target_path.read_text(encoding="utf-8", errors="replace")
                matched = _fuzzy_find_before(plan["before"], real_content)
                if matched is None and _func_name:
                    # Fallback: extract the named function's source as context
                    matched = _extract_function_source(real_content, _func_name)
                    if matched:
                        _log.info(
                            "Fuzzy match failed; using extracted function '%s' as context",
                            _func_name,
                        )
                        return (
                            f"CHANGE PLAN (modify this function):\n"
                            f"File: {plan['file']}\n"
                            f"Function: {_func_name}\n"
                            f"Description: {plan.get('description', 'N/A')}\n\n"
                            f"CURRENT FUNCTION SOURCE (read the file to get exact content):\n"
                            f"```python\n{matched}\n```\n\n"
                            f"INTENDED CHANGE:\n"
                            f"```python\n{plan['after']}\n```\n\n"
                            f"Use file_read to get the exact current content, then apply "
                            f"the intended change with file_edit using exact string matching.\n"
                        )
                if matched is None:
                    _log.warning(
                        "Change plan 'before' snippet not found in %s — returning approximate plan",
                        plan["file"],
                    )
                    # Still return the plan as approximate guidance —
                    # better than no plan (which causes agent to drift)
                    return (
                        f"CHANGE PLAN (APPROXIMATE — before snippet may not match exactly):\n"
                        f"File: {plan['file']}\n"
                        f"Function: {plan.get('function', 'N/A')}\n"
                        f"Description: {plan.get('description', 'N/A')}\n\n"
                        f"STEPS:\n"
                        f"1. Use file_read on {plan['file']} to see the REAL current code\n"
                        f"2. Find the function/section described above\n"
                        f"3. Use file_edit with the EXACT string from the file (not the plan)\n\n"
                        f"APPROXIMATE before (may not match exactly — READ THE FILE FIRST):\n"
                        f"```python\n{plan['before']}\n```\n\n"
                        f"INTENDED after:\n"
                        f"```python\n{plan['after']}\n```\n"
                    )
                # Use the REAL matched content (not Haiku's hallucinated version)
                if matched != plan["before"].strip():
                    _log.info("Using real file content instead of Haiku's approximate snippet")
                    plan["before"] = matched
            except Exception:
                pass  # If we can't read, let sandbox agent figure it out

        # Format as directive for sandbox agent
        return (
            f"CHANGE PLAN (execute this exactly):\n"
            f"File: {plan['file']}\n"
            f"Function: {plan.get('function', 'N/A')}\n"
            f"Description: {plan.get('description', 'N/A')}\n"
            f"Use file_edit with:\n"
            f"  path: {plan['file']}\n"
            f"  old_string: {plan['before']}\n"
            f"  new_string: {plan['after']}\n"
        )
    except json.JSONDecodeError as e:
        _log.warning("Change plan JSON fallback also failed: %s", e)
        return None
    except Exception as e:
        _log.warning("Change plan generation failed: %s", e)
        return None


async def improve(
    task: str,
    project_dir: str | Path,
    config: SecretaryConfig | None = None,
    auto_promote: bool | None = None,
    test_timeout: int | None = None,
    keep_sandbox: bool | None = None,
    max_turns: int | None = None,
    target_files: list[str] | None = None,
    description: str = "",
) -> ImprovementResult:
    """Run the self-improvement pipeline on a project.

    1. Generate specific change plan via Haiku (cheap pre-planning)
    2. Copy project to sandbox
    3. Run agent with improvement task + change plan in sandbox
    4. Run tests in sandbox
    5. If tests pass + auto_promote, backup originals then copy changes back

    Args:
        task: Description of the improvement to make.
        project_dir: Root of the project to improve.
        config: Secretary config (loads default if None).
        auto_promote: Override config's auto_promote setting.
        test_timeout: Override config's test_timeout setting.
        keep_sandbox: Keep sandbox after completion for review.
        max_turns: Override the tier's default max_turns for the agent run.
        target_files: List of files the agent should focus on modifying.
    """
    if config is None:
        config = SecretaryConfig.load()

    project = Path(project_dir).resolve()
    _auto = auto_promote if auto_promote is not None else config.self_improve.auto_promote
    _timeout = test_timeout if test_timeout is not None else config.self_improve.test_timeout
    _keep = keep_sandbox if keep_sandbox is not None else config.self_improve.keep_sandbox

    # Sandbox path
    sandbox_dir = config.self_improve.sandbox_dir
    if not sandbox_dir:
        sandbox = project.parent / f"{project.name}_sandbox"
    else:
        sandbox = Path(sandbox_dir)

    result = ImprovementResult(task=task, sandbox_dir=str(sandbox))

    try:
        # 0. Generate change plan (cheap Haiku call, before sandbox copy)
        _change_plan = await _generate_change_plan(
            task, target_files, project, config,
        )
        if _change_plan:
            _log.info("Change plan generated for sandbox agent")

        # 1. Copy to sandbox
        _copy_to_sandbox(project, sandbox)
        _log.info("Sandbox created: %s", sandbox)

        # 2. Run agent in sandbox via direct_agent.run()
        _tools = build_tool_registry(
            config.data_path,
            workspace_root=sandbox,
            unrestricted_files=False,
            write_scope="src/secretary",
        )

        # Build a contextualized prompt with project structure
        _src_files = sorted(
            str(f.relative_to(sandbox))
            for f in (sandbox / "src" / "secretary").rglob("*.py")
            if f.is_file()
        )
        _src_listing = "\n".join(f"  {f}" for f in _src_files[:40])

        # If target_files specified, add focused guidance
        _target_section = ""
        if target_files:
            _tf_list = "\n".join(f"  - {f}" for f in target_files)
            _target_section = (
                f"TARGET FILES (you MUST modify at least one of these):\n{_tf_list}\n"
                f"Read these files FIRST, then make a focused change.\n"
                f"Do NOT modify files outside this list unless absolutely necessary.\n\n"
            )

        _si_max_turns = max_turns or 8

        _full_prompt = (
            f"## MISSION: Make ONE code change and pass tests\n\n"
            f"SUCCESS = you called file_edit at least once AND tests pass.\n"
            f"FAILURE = you never called file_edit (reading/analyzing alone is NOT success).\n"
            f"BUDGET = {_si_max_turns} turns. Turn 1: read. Turn 2-3: edit. Turn 4: test.\n\n"
            f"SCOPE (violating = instant task failure):\n"
            f"- ONLY modify files under src/secretary/\n"
            f"- NEVER modify, delete, or create files in tests/\n"
            f"- NEVER create _tmp_*, *.txt, scratch files, or analysis dumps\n"
            f"- NEVER write to data/, config/, or project root\n\n"
            f"{_target_section}"
            f"KEY SOURCE FILES:\n{_src_listing}\n\n"
            f"TESTS: run_command {{command: 'python -m pytest tests/ -x -q --ignore=tests/test_web_dashboard.py'}}\n\n"
        )

        # Insert change plan if available — gives agent a concrete blueprint
        if _change_plan:
            _full_prompt += (
                f"{_change_plan}\n"
                f"Execute the change plan above using file_edit, then run tests.\n"
                f"If the exact old_string doesn't match, use file_read first "
                f"and copy the REAL text from the file.\n\n"
            )
        else:
            # No plan — give agent concrete context by injecting file head sections
            _file_previews = ""
            if target_files:
                for tf in target_files[:2]:
                    _fp = sandbox / tf
                    if _fp.exists() and _fp.is_file():
                        try:
                            _lines = _fp.read_text(encoding="utf-8", errors="replace").splitlines()
                            _preview = "\n".join(_lines[:200])
                            _file_previews += f"\n### {tf} (first 200 lines)\n```python\n{_preview}\n```\n"
                        except Exception:
                            pass
            _full_prompt += (
                f"WORKFLOW (strict):\n"
                f"1. file_read the target file — ONE file only\n"
                f"2. file_edit to make ONE surgical change (copy EXACT lines as old_string)\n"
                f"3. run_command to run tests\n\n"
                f"HARD RULE: If you haven't called file_edit by turn 3, STOP READING and EDIT NOW.\n"
            )
            if _file_previews:
                _full_prompt += f"\nFILE PREVIEWS:{_file_previews}\n"
            _full_prompt += "\n"

        _full_prompt += f"TASK:\n{task}"

        # 2b. Pre-test baseline: verify target-related tests pass BEFORE agent changes.
        #     If tests are already failing, the agent is set up for failure.
        focused_baseline = _map_changed_to_tests(
            [f"MOD: {tf}" for tf in (target_files or [])], sandbox,
        ) if target_files else []
        if focused_baseline:
            baseline_ok, baseline_out = await _run_tests_subset(
                sandbox, project, focused_baseline, _timeout,
            )
            if not baseline_ok:
                result.error = (
                    f"Pre-test baseline FAILED — tests broken before agent ran. "
                    f"Affected: {focused_baseline}. Output: {baseline_out[-500:]}"
                )
                return result

        _si_tier = config.self_improve.tier
        agent_result = await direct_agent.run(
            task=_full_prompt,
            config=config,
            force_tier=_si_tier,
            tools=_tools,
            max_turns=_si_max_turns,
            max_tool_calls=30,
        )
        result.agent_result = agent_result.text
        result.cost_usd = agent_result.cost_usd
        result.num_turns = agent_result.num_turns
        if agent_result.error:
            result.error = agent_result.error
            return result

        # 3. Detect changes — filter to allowed scope
        raw_changes = _detect_changes(project, sandbox)
        result.changed_files = _filter_allowed_changes(raw_changes)
        if not result.changed_files:
            if raw_changes:
                result.error = f"Agent modified files outside allowed scope: {raw_changes}"
            else:
                # CRITICAL FIX: When agent makes no changes, include the agent's reasoning
                # in the error so self-improve logs capture why the agent stalled
                result.error = "Agent made no changes. Agent reasoning: " + (
                    result.agent_result[:500] if result.agent_result else "No output"
                )
            return result

        # Warn if some changes were filtered out — partial scope violations
        rejected = [c for c in raw_changes if c not in result.changed_files]
        if rejected:
            _log.warning("Scope filter rejected %d of %d changes: %s", len(rejected), len(raw_changes), rejected[:3])
        _log.info("Sandbox changes (filtered): %s", result.changed_files)

        # 4. Run focused tests first (related to changed files), then full suite
        focused_tests = _map_changed_to_tests(result.changed_files, sandbox)
        if focused_tests:
            _log.info("Running focused tests first: %s", focused_tests)
            focused_ok, focused_out = await _run_tests_subset(
                sandbox, project, focused_tests, _timeout,
            )
            for _retry_num in range(1, _MAX_FOCUSED_RETRIES + 1):
                if focused_ok:
                    break
                _log.info("Focused tests failed — retry %d/%d", _retry_num, _MAX_FOCUSED_RETRIES)
                retry_result = await _retry_with_failure_context(
                    sandbox, project, config, task, _change_plan,
                    focused_out, result.changed_files,
                    _si_tier, _tools, _si_max_turns, _timeout,
                    attempt=_retry_num,
                )
                if retry_result is not None:
                    result.changed_files = retry_result["changed_files"]
                    retry_focused = _map_changed_to_tests(result.changed_files, sandbox)
                    if retry_focused:
                        focused_ok, focused_out = await _run_tests_subset(
                            sandbox, project, retry_focused, _timeout,
                        )
                    else:
                        focused_ok = True
                else:
                    break  # Retry produced no changes — no point continuing
            if not focused_ok:
                result.tests_passed = False
                result.test_output = f"FOCUSED TESTS FAILED (after {_MAX_FOCUSED_RETRIES} retries):\n{focused_out[-2000:]}"
                result.error = "Sandbox tests failed after code changes (including retries)"
                return result

        result.tests_passed, result.test_output = await _run_tests(sandbox, project, _timeout)
        if not result.tests_passed:
            # Full suite failed — give agent 1 retry with failure context
            _log.info("Full suite failed after focused tests passed — attempting 1 retry")
            full_retry = await _retry_with_failure_context(
                sandbox, project, config, task, _change_plan,
                result.test_output, result.changed_files,
                _si_tier, _tools, _si_max_turns, _timeout,
                attempt=1,
            )
            if full_retry is not None:
                result.changed_files = full_retry["changed_files"]
                result.tests_passed, result.test_output = await _run_tests(
                    sandbox, project, _timeout,
                )
            if not result.tests_passed:
                # Truncate but keep the tail (most useful: test names + errors)
                result.test_output = result.test_output[-3000:]
                result.error = "Sandbox tests failed after code changes (including full-suite retry)"

        # 5. Promote if approved — with backup + post-promote regression test
        if result.tests_passed and _auto:
            backup = _backup_originals(project, result.changed_files)
            result.backup_dir = str(backup)
            _promote_changes(project, sandbox, result.changed_files)
            # Post-promote safety: verify real project still passes tests
            real_passed, real_output = await _run_tests(project, project, _timeout)
            if not real_passed:
                _log.warning("Post-promote tests FAILED — auto-rolling back")
                rollback(project, backup)
                result.promoted = False
                result.error = f"Post-promote tests failed — auto-rolled back. Output: {real_output[:500]}"
            else:
                # Git commit promoted changes for traceability
                commit_hash = _git_commit_promoted(project, result.changed_files, task, description)
                if commit_hash:
                    result.promoted = True
                    _log.info("Promoted changes committed: %s", commit_hash)
                else:
                    # Git failed — rollback to keep repo clean
                    _log.warning("Git commit failed — rolling back promoted changes")
                    rollback(project, backup)
                    result.promoted = False
                    result.error = "Promoted but git commit failed — rolled back for safety"

    except Exception as e:
        result.error = str(e)
    finally:
        # Clean up sandbox unless keep_sandbox is set
        if not _keep and sandbox.exists():
            # Kill any stale processes BEFORE attempting rmtree
            _kill_sandbox_processes(sandbox)
            import time
            time.sleep(0.5)
            # Remove linked venv first to avoid deleting the real venv
            sandbox_venv = sandbox / ".venv"
            if sandbox_venv.is_symlink() or sandbox_venv.is_junction():
                sandbox_venv.unlink() if sandbox_venv.is_symlink() else os.rmdir(str(sandbox_venv))
            shutil.rmtree(sandbox, ignore_errors=True)
            # Retry once if files still locked
            if sandbox.exists():
                time.sleep(1)
                shutil.rmtree(sandbox, ignore_errors=True)
            if sandbox.exists():
                _log.warning("Sandbox %s could not be fully removed — will clean on next run", sandbox)

        # Log to run_log.jsonl for analytics visibility
        _log_to_run_log(config, task, result)

    return result
