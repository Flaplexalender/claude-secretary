"""Tool registry for direct agent — no MCP, just async callables.

Reuses shared Gmail/Calendar helpers from _tool_helpers.py and packages them
as a simple dict registry for the Anthropic Messages API tool-use loop.
Each tool: {name: str, description: str, input_schema: dict, func: async callable}
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from .mcp_tools.google_auth import build_gmail_service, build_calendar_service

# Import shared helpers — canonical implementations live in _tool_helpers
from ._tool_helpers import (
    _text,
    _error,
    _call_with_retry,
    _extract_body,
    _format_headers,
    _validate_email,
    _validate_body,
    _format_event,
    _is_token_error,
    _EMAIL_RE,
    _MAX_EMAIL_BODY_BYTES,
    _TRANSIENT,
    _TOKEN_ERRORS,
)

# Re-export for backwards compatibility — existing tests import from here
__all__ = [
    "_text", "_error", "_call_with_retry", "_extract_body", "_format_headers",
    "_validate_email", "_validate_body", "_format_event", "_is_token_error",
    "_EMAIL_RE", "_MAX_EMAIL_BODY_BYTES", "_TRANSIENT", "_TOKEN_ERRORS",
]


# ---------------------------------------------------------------------------
# File tool registry (no external services — always free)
# ---------------------------------------------------------------------------

_MAX_FILE_BYTES = 200_000  # 200 KB hard cap for read/write


def build_file_registry(
    workspace_root: Path | None = None,
    write_scope: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build file read/write/list tools.

    If workspace_root is given, paths are sandboxed to that directory.
    If workspace_root is None, paths are unrestricted (resolve to absolute).
    If write_scope is given (e.g. "src/secretary"), file_write and file_edit
    reject any path not under that subdirectory — gives the agent immediate
    feedback instead of wasting turns on writes that get filtered post-hoc.
    Pure local I/O — no API calls, free.
    """
    root = workspace_root.resolve() if workspace_root is not None else None
    _write_prefix = write_scope.replace("\\", "/").rstrip("/") + "/" if write_scope else None

    def _safe_path(rel: str) -> Path | None:
        """Return resolved path, sandboxed to root if set."""
        try:
            if root is None:
                return Path(rel).resolve()
            target = (root / rel).resolve()
            target.relative_to(root)  # raises ValueError if outside
            return target
        except (ValueError, RuntimeError):
            return None

    def _check_write_scope(rel: str) -> str | None:
        """Return error message if path is outside write_scope, else None."""
        if _write_prefix is None:
            return None
        rel_posix = rel.replace("\\", "/")
        if not rel_posix.startswith(_write_prefix):
            return (
                f"Write blocked: '{rel}' is outside allowed scope '{write_scope}/'. "
                f"You may only modify files under {write_scope}/."
            )
        return None

    async def file_read(args: dict) -> dict[str, Any]:
        """Read a file's contents, returning text with size header. Sandboxed to workspace root."""
        path = args.get("path")
        if not path:
            return _error("Missing required parameter 'path'. Usage: file_read({path: 'relative/path/to/file'})")
        target = _safe_path(path)
        if target is None:
            return _error(f"Invalid path: {path}")
        if not target.exists():
            return _error(f"File not found: {path}")
        if not target.is_file():
            return _error(f"Not a file: {path}")
        size = target.stat().st_size
        if size > _MAX_FILE_BYTES:
            return _error(
                f"File too large: {size:,} bytes (max {_MAX_FILE_BYTES:,}). "
                f"Use file_list to explore or split into smaller reads."
            )
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return _error(f"Cannot read {path}: {e}")
        return _text(f"--- {path} ({size:,} bytes) ---\n{content}")

    async def file_write(args: dict) -> dict[str, Any]:
        """Write content to a file, creating parent directories as needed."""
        path = args.get("path")
        content = args.get("content")
        if not path:
            return _error("Missing required parameter 'path'. Usage: file_write({path: 'file.py', content: 'text...'})")
        if content is None:
            return _error("Missing required parameter 'content'. Usage: file_write({path: 'file.py', content: 'text...'})")
        scope_err = _check_write_scope(path)
        if scope_err:
            return _error(scope_err)
        target = _safe_path(path)
        if target is None:
            return _error(f"Invalid path: {path}")
        encoded_size = len(content.encode("utf-8"))
        if encoded_size > _MAX_FILE_BYTES:
            return _error(
                f"Content too large: {encoded_size:,} bytes (max {_MAX_FILE_BYTES:,})"
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return _error(f"Cannot write {path}: {e}")
        return _text(f"Written: {path} ({encoded_size:,} bytes)")

    async def file_list(args: dict) -> dict[str, Any]:
        """List directory contents with file sizes and directory indicators."""
        rel = args.get("path", ".")
        target = _safe_path(rel)
        if target is None:
            return _error(f"Invalid path: {rel}")
        if not target.exists():
            return _error(f"Path not found: {rel}")
        if not target.is_dir():
            return _error(f"Not a directory: {rel}")
        try:
            entries = []
            for item in sorted(target.iterdir()):
                if item.is_dir():
                    entries.append(f"  {item.name}/")
                else:
                    size = item.stat().st_size
                    entries.append(f"  {item.name} ({size:,} bytes)")
        except Exception as e:  # noqa: BLE001
            return _error(f"Cannot list {rel}: {e}")
        if not entries:
            return _text(f"{rel}: (empty directory)")
        return _text(f"{rel}/:\n" + "\n".join(entries))

    # --- file_edit: search & replace (like Copilot's replace_string_in_file) ---
    async def file_edit(args: dict) -> dict[str, Any]:
        """Search-and-replace exactly one occurrence of old_string with new_string in a file."""
        path = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not path:
            return _error("Missing 'path'. Usage: file_edit({path: 'file.py', old_string: 'old', new_string: 'new'})")
        if old_string is None:
            return _error("Missing 'old_string'")
        if new_string is None:
            return _error("Missing 'new_string'")
        scope_err = _check_write_scope(path)
        if scope_err:
            return _error(scope_err)
        target = _safe_path(path)
        if target is None:
            return _error(f"Invalid path: {path}")
        if not target.exists() or not target.is_file():
            return _error(f"File not found: {path}")
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return _error(f"Cannot read {path}: {e}")
        count = content.count(old_string)
        if count == 0:
            # Show first 200 chars to help debug
            return _error(f"old_string not found in {path}. File starts with: {content[:200]!r}")
        if count > 1:
            return _error(f"old_string matches {count} locations in {path}. Add more context to match exactly 1.")
        new_content = content.replace(old_string, new_string, 1)
        try:
            target.write_text(new_content, encoding="utf-8")
        except Exception as e:
            return _error(f"Cannot write {path}: {e}")
        return _text(f"Edited {path}: 1 replacement")

    # --- grep_search: regex search across files (like Copilot's grep_search) ---
    async def grep_search(args: dict) -> dict[str, Any]:
        """Regex search across files in a directory tree, returning matching lines."""
        import re as _re
        pattern = args.get("pattern")
        search_path = args.get("path", ".")
        max_results = max(1, min(100, args.get("max_results", 15)))  # reduced from 20 to save tokens, max 100
        if not pattern:
            return _error("Missing 'pattern'. Usage: grep_search({pattern: 'def main', path: 'src/'})")
        target = _safe_path(search_path)
        if target is None:
            return _error(f"Invalid path: {search_path}")
        if not target.exists():
            return _error(f"Path not found: {search_path}")
        try:
            regex = _re.compile(pattern, _re.IGNORECASE)
        except _re.error as e:
            return _error(f"Invalid regex: {e}")
        results: list[str] = []
        _SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache", ".pytest_cache"}
        _TEXT_EXTS = {".py", ".js", ".ts", ".md", ".yaml", ".yml", ".json", ".txt", ".toml", ".cfg", ".ini", ".sh", ".bat", ".ps1", ".html", ".css"}

        def _walk(d: Path, depth: int = 0) -> None:
            if depth > 6 or len(results) >= max_results:
                return
            try:
                items = sorted(d.iterdir())
            except OSError:
                return
            for item in items:
                if len(results) >= max_results:
                    return
                if item.is_dir():
                    if item.name not in _SKIP_DIRS:
                        _walk(item, depth + 1)
                elif item.is_file() and item.suffix in _TEXT_EXTS and item.stat().st_size < _MAX_FILE_BYTES:
                    try:
                        text = item.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    for i, line in enumerate(text.splitlines(), 1):
                        if regex.search(line):
                            rel = str(item.relative_to(target)) if target.is_dir() else item.name
                            results.append(f"{rel}:{i}: {line.strip()[:90]}")
                            if len(results) >= max_results:
                                return

        if target.is_file():
            _walk(target.parent)
        else:
            _walk(target)
        if not results:
            return _text(f"No matches for /{pattern}/ in {search_path}")
        return _text(f"Found {len(results)} matches for /{pattern}/:\n" + "\n".join(results))

    # --- run_command: shell execution (like Copilot's run_in_terminal) ---
    async def run_command(args: dict) -> dict[str, Any]:
        """Execute a shell command with timeout (30s max to prevent 504 gateway errors)."""
        import asyncio as _asyncio
        command = args.get("command")
        timeout = min(30, max(5, args.get("timeout", 30)))
        if not command:
            return _error("Missing 'command'. Usage: run_command({command: 'python -m pytest tests/ -q'})")
        # Security: block dangerous commands
        _BLOCKED = {"rm -rf /", "del /s /q c:\\", "format ", "mkfs", "dd if=", ":(){", "fork bomb"}
        cmd_lower = command.lower()
        for blocked in _BLOCKED:
            if blocked.lower() in cmd_lower:
                return _error(f"Blocked dangerous command pattern: {blocked}")
        try:
            proc = await _asyncio.create_subprocess_shell(
                command,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.STDOUT,
                cwd=str(root) if root else None,
            )
            stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            # Truncate to prevent JSON parse buffer overflow (8KB = ~2000 tokens max)
            if len(output) > 8000:
                # Find safe split point to avoid mid-line truncation
                head = output[:4000]
                tail = output[-4000:]
                output = head + "\n... [TRUNCATED] ...\n" + tail
            # Only show exit code on failure (success = common case, save tokens)
            if proc.returncode != 0:
                return _text(f"[exit {proc.returncode}]\n{output}")
            return _text(output)
        except _asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return _error(f"Command timed out after {timeout}s: {command}")
        except Exception as e:
            return _error(f"Command failed: {e}")

    # --- run_python: execute a Python script for bulk operations in one tool call ---
    async def run_python(args: dict) -> dict[str, Any]:
        """Execute a Python script (45s max to prevent gateway timeouts on concurrent calls). Uses UTF-8 encoding."""
        import asyncio as _asyncio
        import tempfile as _tempfile

        code = args.get("code")
        timeout = min(45, max(5, args.get("timeout", 45)))
        if not code:
            return _error("Missing 'code'. Usage: run_python({code: 'print(42)'})")
        # Write to temp file and execute — avoids shell quoting issues
        # Force UTF-8 encoding to prevent Windows cp1252 UnicodeDecodeError
        with _tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8", errors="replace",
            dir=str(root) if root else None,
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            proc = await _asyncio.create_subprocess_exec(
                "python", tmp_path,
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.STDOUT,
                cwd=str(root) if root else None,
            )
            stdout, _ = await _asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            # Cap at 8KB to prevent parse buffer overflow (JSON safe)
            if len(output) > 8000:
                head = output[:4000]
                tail = output[-4000:]
                output = head + "\n... [TRUNCATED] ...\n" + tail
            if proc.returncode != 0:
                return _text(f"[exit {proc.returncode}]\n{output}")
            return _text(output)
        except _asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return _error(f"Script timed out after {timeout}s")
        except Exception as e:
            return _error(f"Script failed: {e}")
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass

    return {
        "file_read": {
            "name": "file_read",
            "description": "Read a file. Example: {path: 'src/config.py'}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative or absolute file path"},
                },
                "required": ["path"],
            },
            "func": file_read,
        },
        "file_write": {
            "name": "file_write",
            "description": "Write/create a file (creates dirs). Example: {path: 'data/report.md', content: '# Report\n...'}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
            "func": file_write,
        },
        "file_list": {
            "name": "file_list",
            "description": "List directory contents. Example: {path: 'src/secretary'}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path",
                        "default": ".",
                    },
                },
            },
            "func": file_list,
        },
        "file_edit": {
            "name": "file_edit",
            "description": "Search & replace in a file (exact match, 1 occurrence). Example: {path: 'src/main.py', old_string: 'x = 1', new_string: 'x = 2'}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "old_string": {"type": "string", "description": "Exact text to find (must match exactly 1 location)"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            "func": file_edit,
        },
        "grep_search": {
            "name": "grep_search",
            "description": "Regex search across files in a directory. Example: {pattern: 'def run\\\\(', path: 'src/'}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex pattern (case-insensitive)"},
                    "path": {"type": "string", "description": "Directory or file to search", "default": "."},
                    "max_results": {"type": "integer", "description": "Max matches (1-100)", "default": 20},
                },
                "required": ["pattern"],
            },
            "func": grep_search,
        },
        "run_command": {
            "name": "run_command",
            "description": "Run a shell command. Example: {command: 'python -m pytest tests/ -q --tb=short', timeout: 60}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (5-120)", "default": 30},
                },
                "required": ["command"],
            },
            "func": run_command,
        },
        "run_python": {
            "name": "run_python",
            "description": "Execute a Python script (bulk operations in one call). Example: {code: 'import pathlib\\nfor f in pathlib.Path(\"src\").rglob(\"*.py\"): print(f)', timeout: 120}",
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source code to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (5-300)", "default": 120},
                },
                "required": ["code"],
            },
            "func": run_python,
        },
    }


# ---------------------------------------------------------------------------
# Tool registry builder
# ---------------------------------------------------------------------------

def build_tool_registry(
    data_root: Path,
    workspace_root: Path | None = None,
    unrestricted_files: bool = False,
    write_scope: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build tool registry for direct agent.

    - Google tools (Gmail/Calendar) are included if data_root/google_token.json exists.
    - File tools are included if workspace_root is set (sandboxed) OR unrestricted_files=True.
    - unrestricted_files=True: read/write any path on disk (pure local I/O).
    - write_scope: if set, file_write/file_edit reject paths outside this prefix.

    Returns: {tool_name: {name, description, input_schema, func}}
    """
    token_path = data_root / "google_token.json"
    if not token_path.exists():
        if unrestricted_files:
            return build_file_registry(None, write_scope=write_scope)
        if workspace_root is not None:
            return build_file_registry(workspace_root, write_scope=write_scope)
        return {}

    registry: dict[str, dict[str, Any]] = {}

    # --- Gmail tools ---

    async def gmail_search(args: dict) -> dict[str, Any]:
        """Search Gmail messages using Gmail query syntax, returning IDs with metadata."""
        query = args["query"]
        max_results = max(1, min(50, args.get("max_results", 10)))
        svc = build_gmail_service(data_root)
        resp = await _call_with_retry(
            svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute
        )
        messages = resp.get("messages", [])
        if not messages:
            return _text(f"No messages found for: {query}")
        results = []
        for msg_info in messages:
            msg = await _call_with_retry(
                svc.users().messages().get(
                    userId="me", id=msg_info["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]
                ).execute
            )
            headers = _format_headers(msg.get("payload", {}).get("headers", []))
            results.append(
                f"ID: {msg_info['id']}\n"
                f"  Subject: {headers.get('Subject', '(none)')}\n"
                f"  From: {headers.get('From', '?')}\n"
                f"  Date: {headers.get('Date', '?')}"
            )
        return _text(f"Found {len(results)} messages:\n\n" + "\n\n".join(results))

    registry["gmail_search"] = {
        "name": "gmail_search",
        "description": "Search Gmail. Uses Gmail query syntax. Examples: {query: 'is:unread'}, {query: 'from:boss@co.com newer_than:1d'}",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max results (1-50)", "default": 10},
            },
            "required": ["query"],
        },
        "func": gmail_search,
    }

    async def gmail_read(args: dict) -> dict[str, Any]:
        """Read a full email by message ID, returning headers, labels, and body."""
        svc = build_gmail_service(data_root)
        msg = await _call_with_retry(
            svc.users().messages().get(userId="me", id=args["message_id"], format="full").execute
        )
        headers = _format_headers(msg.get("payload", {}).get("headers", []))
        body = _extract_body(msg.get("payload", {}))
        if len(body) > 15000:
            body = body[:15000] + "\n...[truncated]"
        labels = ", ".join(msg.get("labelIds", []))
        header_text = "\n".join(f"{k}: {v}" for k, v in headers.items())
        return _text(f"{header_text}\nLabels: {labels}\n\n{body}")

    registry["gmail_read"] = {
        "name": "gmail_read",
        "description": "Read full email by ID (from gmail_search). Example: {message_id: '18abc123def'}",
        "input_schema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message ID from gmail_search"},
            },
            "required": ["message_id"],
        },
        "func": gmail_read,
    }

    async def gmail_draft(args: dict) -> dict[str, Any]:
        """Create an email draft (not sent) with validated recipient and body."""
        err = _validate_email(args["to"])
        if err:
            return _error(err)
        err = _validate_body(args["body"])
        if err:
            return _error(err)
        svc = build_gmail_service(data_root)
        message = MIMEText(args["body"])
        message["To"] = args["to"]
        message["Subject"] = args["subject"]
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        draft = await _call_with_retry(
            svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute
        )
        return _text(f"Draft created — ID: {draft['id']}")

    registry["gmail_draft"] = {
        "name": "gmail_draft",
        "description": "Create email draft. Example: {to: 'user@example.com', subject: 'Re: Meeting', body: 'Sounds good, see you then.'}",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body text"},
            },
            "required": ["to", "subject", "body"],
        },
        "func": gmail_draft,
    }

    async def gmail_send(args: dict) -> dict[str, Any]:
        """Send an email immediately (irreversible) with validated recipient and body."""
        err = _validate_email(args["to"])
        if err:
            return _error(err)
        err = _validate_body(args["body"])
        if err:
            return _error(err)
        svc = build_gmail_service(data_root)
        message = MIMEText(args["body"])
        message["To"] = args["to"]
        message["Subject"] = args["subject"]
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        result = await _call_with_retry(
            svc.users().messages().send(userId="me", body={"raw": raw}).execute
        )
        return _text(f"Email sent — ID: {result['id']}")

    registry["gmail_send"] = {
        "name": "gmail_send",
        "description": "Send email immediately (cannot undo). Example: {to: 'user@example.com', subject: 'Update', body: 'Details...'}",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Email body text"},
            },
            "required": ["to", "subject", "body"],
        },
        "func": gmail_send,
    }

    async def gmail_list_drafts(args: dict) -> dict[str, Any]:
        """List existing email drafts with subject, recipient, and date."""
        max_results = max(1, min(50, args.get("max_results", 10)))
        svc = build_gmail_service(data_root)
        resp = await _call_with_retry(
            svc.users().drafts().list(userId="me", maxResults=max_results).execute
        )
        drafts = resp.get("drafts", [])
        if not drafts:
            return _text("No drafts found.")
        results = []
        for d in drafts:
            draft = await _call_with_retry(
                svc.users().drafts().get(userId="me", id=d["id"], format="metadata").execute
            )
            msg = draft.get("message", {})
            headers = _format_headers(msg.get("payload", {}).get("headers", []))
            results.append(
                f"Draft ID: {d['id']}\n"
                f"  To: {headers.get('To', '(none)')}\n"
                f"  Subject: {headers.get('Subject', '(none)')}\n"
                f"  Date: {headers.get('Date', '?')}"
            )
        return _text(f"Found {len(results)} drafts:\n\n" + "\n\n".join(results))

    registry["gmail_list_drafts"] = {
        "name": "gmail_list_drafts",
        "description": "List existing email drafts. Example: {max_results: 5}",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max results (1-50)", "default": 10},
            },
        },
        "func": gmail_list_drafts,
    }

    async def gmail_get_draft(args: dict) -> dict[str, Any]:
        """Read a full draft by ID, returning headers and body text. Returns 404 error if draft not found."""
        svc = build_gmail_service(data_root)
        try:
            draft = await _call_with_retry(
                svc.users().drafts().get(userId="me", id=args["draft_id"], format="full").execute
            )
        except Exception as e:  # Catch 404 errors gracefully
            if "404" in str(e) or "not found" in str(e).lower():
                return _error(f"Draft not found (may have been deleted): {args['draft_id']}")
            raise
        msg = draft.get("message", {})
        headers = _format_headers(msg.get("payload", {}).get("headers", []))
        body = _extract_body(msg.get("payload", {}))
        if len(body) > 15000:
            body = body[:15000] + "\n...[truncated]"
        header_text = "\n".join(f"{k}: {v}" for k, v in headers.items())
        return _text(f"Draft ID: {args['draft_id']}\n{header_text}\n\n{body}")

    registry["gmail_get_draft"] = {
        "name": "gmail_get_draft",
        "description": "Read full draft by ID. Example: {draft_id: 'r123456789'}",
        "input_schema": {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "Draft ID from gmail_list_drafts"},
            },
            "required": ["draft_id"],
        },
        "func": gmail_get_draft,
    }

    # --- Calendar tools ---

    async def calendar_today(args: dict) -> dict[str, Any]:
        max_results = max(1, min(25, args.get("max_results", 10)))
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)
        svc = build_calendar_service(data_root)
        resp = await _call_with_retry(
            svc.events().list(
                calendarId="primary",
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ).execute
        )
        events = resp.get("items", [])
        if not events:
            return _text("No events today.")
        formatted = [_format_event(e) for e in events]
        return _text(f"Today's events ({len(events)}):\n\n" + "\n\n".join(formatted))

    registry["calendar_today"] = {
        "name": "calendar_today",
        "description": "Get today's calendar events. Example: {max_results: 10}",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max events (1-25)", "default": 10},
            },
        },
        "func": calendar_today,
    }

    async def calendar_list(args: dict) -> dict[str, Any]:
        days_ahead = max(1, min(30, args.get("days_ahead", 7)))
        max_results = max(1, min(50, args.get("max_results", 20)))
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        svc = build_calendar_service(data_root)
        resp = await _call_with_retry(
            svc.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ).execute
        )
        events = resp.get("items", [])
        if not events:
            return _text(f"No events in the next {days_ahead} days.")
        formatted = [_format_event(e) for e in events]
        return _text(f"Upcoming events ({len(events)}, next {days_ahead} days):\n\n" + "\n\n".join(formatted))

    registry["calendar_list"] = {
        "name": "calendar_list",
        "description": "List upcoming events over N days. Example: {days_ahead: 7, max_results: 20}",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Days ahead (1-30)", "default": 7},
                "max_results": {"type": "integer", "description": "Max events (1-50)", "default": 20},
            },
        },
        "func": calendar_list,
    }

    async def calendar_search(args: dict) -> dict[str, Any]:
        query = args["query"]
        days_ahead = max(1, min(90, args.get("days_ahead", 30)))
        max_results = max(1, min(50, args.get("max_results", 10)))
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        svc = build_calendar_service(data_root)
        resp = await _call_with_retry(
            svc.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end.isoformat(),
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
                q=query,
            ).execute
        )
        events = resp.get("items", [])
        if not events:
            return _text(f"No events matching '{query}' in the next {days_ahead} days.")
        formatted = [_format_event(e) for e in events]
        return _text(f"Found {len(events)} events matching '{query}':\n\n" + "\n\n".join(formatted))

    registry["calendar_search"] = {
        "name": "calendar_search",
        "description": "Search calendar events by keyword. Example: {query: 'dentist', days_ahead: 30}",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword"},
                "days_ahead": {"type": "integer", "description": "Days ahead (1-90)", "default": 30},
                "max_results": {"type": "integer", "description": "Max events (1-50)", "default": 10},
            },
            "required": ["query"],
        },
        "func": calendar_search,
    }

    async def calendar_create(args: dict) -> dict[str, Any]:
        parsed_times = {}
        for fld in ("start_time", "end_time"):
            try:
                parsed_times[fld] = datetime.fromisoformat(args[fld].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return _error(f"Invalid ISO 8601 datetime for {fld}: {args[fld]}")
        if parsed_times["end_time"] <= parsed_times["start_time"]:
            return _error(f"End time must be after start time: {args['start_time']} → {args['end_time']}")
        event_body: dict = {
            "summary": args["summary"],
            "start": {"dateTime": args["start_time"]},
            "end": {"dateTime": args["end_time"]},
        }
        if args.get("description"):
            event_body["description"] = args["description"]
        if args.get("location"):
            event_body["location"] = args["location"]
        svc = build_calendar_service(data_root)
        event = await _call_with_retry(
            svc.events().insert(calendarId="primary", body=event_body).execute
        )
        return _text(f"Event created: {event.get('htmlLink', event.get('id', '?'))}")

    registry["calendar_create"] = {
        "name": "calendar_create",
        "description": "Create calendar event. Example: {summary: 'Team standup', start_time: '2025-01-15T10:00:00-08:00', end_time: '2025-01-15T10:30:00-08:00'}",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "ISO 8601 datetime with timezone"},
                "end_time": {"type": "string", "description": "ISO 8601 datetime with timezone"},
                "description": {"type": "string", "description": "Event description"},
                "location": {"type": "string", "description": "Event location"},
            },
            "required": ["summary", "start_time", "end_time"],
        },
        "func": calendar_create,
    }

    if unrestricted_files:
        registry.update(build_file_registry(None, write_scope=write_scope))
    elif workspace_root is not None:
        registry.update(build_file_registry(workspace_root, write_scope=write_scope))

    return registry
