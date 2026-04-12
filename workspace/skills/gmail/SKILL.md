---
name: gmail
description: Search, read, draft, and send Gmail messages via Google API
---

## When to use
- "Check email" / "any new messages" → `gmail_search` with recent date filter
- "Read this email" → `gmail_read` with message_id
- "Reply to X" / "Draft an email" → `gmail_draft` (NEVER gmail_send without approval)
- "Send it" (after the user confirms a draft) → `gmail_send`

## Tools
- `gmail_search(query, max_results)` — Google query syntax. Use `newer_than:1d` for recent.
- `gmail_read(message_id)` — Full body + headers. Use after search to get details.
- `gmail_draft(to, subject, body, reply_to)` — Safe. Always draft first.
- `gmail_send(to, subject, body, reply_to)` — REQUIRES explicit approval.
- `gmail_list_drafts()` — Check existing drafts
- `gmail_get_draft(draft_id)` — Read a specific draft

## Gotchas
- Gmail search uses Google's query syntax, not regex. Example: `from:someone@email.com newer_than:1d`
- MIME headers are case-sensitive in the API response — use case-insensitive lookup
- Always batch: search first, then read multiple interesting messages in parallel
- Draft before send — this is a hard rule, not a suggestion
