---
name: calendar
description: List, search, and create Google Calendar events
---

## When to use
- "What's on my calendar" / "upcoming events" → `calendar_list` with time range
- "When is X" / "find meeting about Y" → `calendar_search`
- "Schedule a meeting" / "add event" → `calendar_create` (ask first if not obvious)

## Tools
- `calendar_list(time_min, time_max)` — List events in a time range (ISO 8601 format)
- `calendar_search(query)` — Text search across event titles and descriptions
- `calendar_create(summary, start, end, description)` — Create new event. End MUST be after start.

## Gotchas
- The user's timezone is configured in USER.md. Times in API are UTC — convert appropriately.
- End time must be after start time — API will reject otherwise.
- For "today" requests, use current date 00:00 to 23:59 in user's timezone.
- Ask before creating events unless the user explicitly requested it.
