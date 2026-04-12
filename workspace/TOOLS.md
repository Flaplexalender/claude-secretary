# Tools & Environment

## API Access

- **Anthropic/Claude**: Via copilot-api proxy at `http://localhost:4141`
- **Message prefix**: Few-shot priming improves tool-use density and parallel execution
- **Models**: claude-haiku-4.5 (low), claude-sonnet-4.6 (medium), claude-opus-4.6 (high)
- **Google OAuth**: credentials at `data/google_credentials.json`, token at `data/google_token.json`

## Gmail Tools

- `gmail_search(query, max_results)` — Google query syntax, not regex. Always use dateRestrict for recent.
- `gmail_read(message_id)` — Returns full message body + headers
- `gmail_draft(to, subject, body, reply_to)` — Safe. Always draft before send.
- `gmail_send(to, subject, body, reply_to)` — DANGEROUS. Requires explicit approval.
- `gmail_list_drafts()`, `gmail_get_draft(draft_id)` — Check existing drafts

## Calendar Tools

- `calendar_list(time_min, time_max)` — List events in range
- `calendar_search(query)` — Search by text
- `calendar_create(summary, start, end, description)` — end must be after start

## File Tools

- `file_read(path)` — Read any file
- `file_write(path, content)` — Write to allowed directories only
- `file_edit(path, old_text, new_text)` — Edit existing files
- `grep_search(pattern, path, flags)` — Search file contents
- `run_command(command, cwd)` — Shell command execution
- `run_python(script)` — Bulk Python execution (300s timeout, 30KB output)

## Paths

- Project root: (auto-detected from working directory)
- Source: `src/secretary/`
- Tests: `tests/`
- Data: `data/` (runtime state, logs, credentials)
- Workspace: `workspace/` (identity files, memory, skills)
- Venv: `.venv/` (Python 3.12)
