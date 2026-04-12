"""Gmail filtering utilities for intelligent message categorization.

Filters newsletters, automated notifications, and provides one-line summaries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_GMAIL_IGNORE_PATTERNS = (
    "noreply", "no-reply", "alert", "notification", "update", "newsletter",
    "automated", "system", "robot", "bot", "digest", "report-", "automated-",
    "bounced", "delivery status", "mailer-daemon", "postmaster", "Mail Delivery Failed"
)

_GMAIL_PRIORITY_KEYWORDS = (
    "urgent", "asap", "critical", "action required", "deadline", "confirm",
    "approve", "review", "important", "response needed", "blocked", "failed"
)


@dataclass
class GmailMessage:
    """Structured representation of a Gmail message for filtering."""
    sender: str
    subject: str
    snippet: str
    message_id: str
    is_unread: bool = True
    
    def is_automated(self) -> bool:
        """True if this is a newsletter or automated notification."""
        sender_lower = self.sender.lower()
        for pattern in _GMAIL_IGNORE_PATTERNS:
            if pattern in sender_lower or pattern in self.subject.lower():
                return True
        return False
    
    def get_urgency(self) -> str:
        """Return 'urgent' or 'normal' based on subject/snippet keywords."""
        full_text = (self.subject + " " + self.snippet).lower()
        for keyword in _GMAIL_PRIORITY_KEYWORDS:
            if keyword in full_text:
                return "urgent"
        return "normal"
    
    def one_line_summary(self) -> str:
        """Return one-line summary: 'Sender | Subject [URGENCY]'."""
        urgency = self.get_urgency()
        tag = f" [{urgency.upper()}]" if urgency == "urgent" else ""
        return f"{self.sender} | {self.subject}{tag}"


def filter_messages(messages: list[GmailMessage]) -> list[tuple[GmailMessage, str]]:
    """Filter out automated messages and return important ones with one-line summaries.
    
    Returns list of (message, summary) tuples sorted by urgency.
    """
    important = []
    for msg in messages:
        if not msg.is_automated():
            summary = msg.one_line_summary()
            important.append((msg, summary))
    
    # Sort: urgent first, then normal
    important.sort(key=lambda x: (x[0].get_urgency() != "urgent", x[1]))
    return important
