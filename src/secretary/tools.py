"""Gmail & Calendar as in-process SDK MCP tools.

Uses the @tool decorator and create_sdk_mcp_server to register tools that run
in the agent's process — no subprocess management, no IPC. Tools call Google
APIs directly.

Shared helpers (body extraction, header formatting, retry logic, etc.) live
in _tool_helpers.py to avoid duplication with direct_tools.py.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

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
)

# Tool names per server — used by agent.py for allowed_tools
MCP_TOOL_NAMES: dict[str, list[str]] = {
    "gmail": ["gmail_search", "gmail_read", "gmail_draft", "gmail_send", "gmail_list_drafts", "gmail_get_draft"],
    "calendar": ["calendar_today", "calendar_list", "calendar_search", "calendar_create"],
}


# ---------------------------------------------------------------------------
# Gmail tools
# ---------------------------------------------------------------------------

def _build_gmail_tools(data_root: Path) -> list[SdkMcpTool]:
    """Build the six Gmail MCP tools (search, read, draft, send, list_drafts, get_draft).

    Each tool is an async function decorated with @tool that calls the Gmail API
    via the shared helpers in _tool_helpers.py. Returns a list of SdkMcpTool
    instances ready for registration with create_sdk_mcp_server.
    """

    @tool(
        "gmail_search",
        "Search Gmail messages. Use Gmail search syntax (from:, subject:, is:unread, etc).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query"},
                "max_results": {"type": "integer", "description": "Max messages (1-50)", "default": 10},
            },
            "required": ["query"],
        },
    )
    async def gmail_search(args: dict) -> dict[str, Any]:
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

    @tool(
        "gmail_read",
        "Read a full Gmail message by ID (from gmail_search results).",
        {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "The message ID to read"},
            },
            "required": ["message_id"],
        },
    )
    async def gmail_read(args: dict) -> dict[str, Any]:
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

    @tool(
        "gmail_draft",
        "Create a Gmail draft (not sent — safe to review first).",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
            },
            "required": ["to", "subject", "body"],
        },
    )
    async def gmail_draft(args: dict) -> dict[str, Any]:
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

    @tool(
        "gmail_send",
        "Send an email immediately. IRREVERSIBLE — use gmail_draft if unsure.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email"},
                "subject": {"type": "string", "description": "Subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
            },
            "required": ["to", "subject", "body"],
        },
    )
    async def gmail_send(args: dict) -> dict[str, Any]:
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

    @tool(
        "gmail_list_drafts",
        "List Gmail drafts with subject, recipient, and date.",
        {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max drafts (1-50)", "default": 10},
            },
        },
    )
    async def gmail_list_drafts(args: dict) -> dict[str, Any]:
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

    @tool(
        "gmail_get_draft",
        "Read a specific Gmail draft by ID (from gmail_list_drafts results).",
        {
            "type": "object",
            "properties": {
                "draft_id": {"type": "string", "description": "The draft ID to read"},
            },
            "required": ["draft_id"],
        },
    )
    async def gmail_get_draft(args: dict) -> dict[str, Any]:
        svc = build_gmail_service(data_root)
        draft = await _call_with_retry(
            svc.users().drafts().get(userId="me", id=args["draft_id"], format="full").execute
        )
        msg = draft.get("message", {})
        headers = _format_headers(msg.get("payload", {}).get("headers", []))
        body = _extract_body(msg.get("payload", {}))
        if len(body) > 15000:
            body = body[:15000] + "\n...[truncated]"
        header_text = "\n".join(f"{k}: {v}" for k, v in headers.items())
        return _text(f"Draft ID: {args['draft_id']}\n{header_text}\n\n{body}")

    return [gmail_search, gmail_read, gmail_draft, gmail_send, gmail_list_drafts, gmail_get_draft]


# ---------------------------------------------------------------------------
# Calendar tools
# ---------------------------------------------------------------------------

def _build_calendar_tools(data_root: Path) -> list[SdkMcpTool]:
    """Build the four Calendar MCP tools (today, list, search, create).

    Each tool is an async function decorated with @tool that calls the Google
    Calendar API via the shared helpers in _tool_helpers.py. Returns a list of
    SdkMcpTool instances ready for registration with create_sdk_mcp_server.
    """

    @tool(
        "calendar_today",
        "List all calendar events for today.",
        {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "description": "Max events (1-25)", "default": 10},
            },
        },
    )
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

    @tool(
        "calendar_list",
        "List upcoming calendar events.",
        {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Days ahead (1-30)", "default": 7},
                "max_results": {"type": "integer", "description": "Max events (1-50)", "default": 20},
            },
        },
    )
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

    @tool(
        "calendar_search",
        "Search calendar events by text (title, description, location).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for"},
                "days_ahead": {"type": "integer", "description": "Days ahead (1-90)", "default": 30},
                "max_results": {"type": "integer", "description": "Max events (1-50)", "default": 10},
            },
            "required": ["query"],
        },
    )
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

    @tool(
        "calendar_create",
        "Create a new calendar event.",
        {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title"},
                "start_time": {"type": "string", "description": "ISO 8601 start time"},
                "end_time": {"type": "string", "description": "ISO 8601 end time"},
                "description": {"type": "string", "description": "Event description"},
                "location": {"type": "string", "description": "Event location"},
            },
            "required": ["summary", "start_time", "end_time"],
        },
    )
    async def calendar_create(args: dict) -> dict[str, Any]:
        # Validate ISO 8601 datetime format and temporal ordering
        parsed_times = {}
        for field in ("start_time", "end_time"):
            try:
                parsed_times[field] = datetime.fromisoformat(args[field].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return _error(f"Invalid ISO 8601 datetime for {field}: {args[field]}")
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

    return [calendar_today, calendar_list, calendar_search, calendar_create]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_mcp_servers(data_root: Path) -> dict:
    """Build in-process MCP servers for Gmail and Calendar.

    Returns empty dict if Google OAuth token is not configured.
    Tools run in the agent's process — no subprocess management.
    """
    token_path = data_root / "google_token.json"
    if not token_path.exists():
        return {}

    servers = {}

    gmail_tools = _build_gmail_tools(data_root)
    servers["gmail"] = create_sdk_mcp_server(name="gmail", tools=gmail_tools)

    calendar_tools = _build_calendar_tools(data_root)
    servers["calendar"] = create_sdk_mcp_server(name="calendar", tools=calendar_tools)

    return servers
