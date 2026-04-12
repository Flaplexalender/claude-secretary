---
name: file-ops
description: Read, write, edit, search, and execute files and commands
---

## When to use
- Any task involving code, docs, or file manipulation
- Running scripts, checking project state
- Searching codebase for patterns

## Tools
- `file_read(path)` — Read any file. Prefer reading large chunks over many small reads.
- `file_write(path, content)` — Write to allowed directories only (see below).
- `file_edit(path, old_text, new_text)` — Edit in-place. old_text must match exactly.
- `grep_search(pattern, path, flags)` — Search file contents. Use for finding code patterns.
- `run_command(command, cwd)` — Shell command. Be careful with destructive commands.
- `run_python(script)` — Execute a Python script directly (300s timeout, 30KB output cap).

## Allowed Write Directories
- `src/secretary/` — source code
- `tests/` — test files
- `campaigns/` — campaign YAML files
- `docs/` — documentation
- `workspace/` — identity, memory, skills
- `goals.yaml` — goal definitions

## Forbidden
- `config.yaml` — manual edit only
- `.env` — secrets
- `data/google_credentials.json`, `data/google_token.json` — credentials
- `data/` generally — runtime state, don't corrupt

## Tips
- Use `run_python` for bulk operations instead of many individual file_read/file_write calls
- `grep_search` before `file_edit` to confirm exact match text
- Always verify edits didn't break things (run tests or check syntax)
