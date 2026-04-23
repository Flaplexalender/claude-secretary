"""Shipment signal extraction — classify carrier emails into actionable events.

Motivation
----------
Alexander missed a Dragonfly return pickup on 2026-04-10 because the
reschedule-confirmation email sat unread in a "Updates" Gmail category
(and a subsequent one was moved to Trash).  A useful secretary should
surface these — a detector that runs against a batch of carrier emails
and flags **pending** return-pickup activity, failed deliveries, and
other time-sensitive events.

Design
------
* Pure functions.  Inputs are plain dicts; no live Gmail/HTTP in this
  module.  The watcher (or a goal task) supplies messages already
  fetched via ``direct_tools.gmail_search`` / ``gmail_read``.
* Classifier is keyword / regex based — deterministic, no LLM cost,
  easy to update when a carrier changes wording.
* Output is a small dataclass the caller can serialise into a
  heartbeat block or a goal-planner input without further processing.

Supported carriers (2026-04 baseline)
-------------------------------------
* Dragonfly / Intelcom (``dragonflyshipping.c[oa]``,
  ``ca.dragonflyinternational.com``, ``intelcom.ca``)
* Canada Post (``canadapost.ca``, ``postescanada``)
* UPS (``ups.com``)
* FedEx (``fedex.com``)
* Purolator (``purolator.com``)
* Amazon logistics (``amazon.ca``, ``amazon.com`` — return-related)

New carriers are a 1-line addition to ``_CARRIER_DOMAINS``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

# Kinds of shipment event we care about, ordered by operational urgency.
# Keep this list small — each kind should map to a clear operator action.
SHIPMENT_KINDS: tuple[str, ...] = (
    "return_pickup_rescheduled",  # carrier agreed to a new pickup date
    "return_pickup_scheduled",    # initial pickup booked
    "return_pickup_missed",       # we know a pickup was attempted + failed
    "delivery_failed",            # driver couldn't deliver — action needed
    "out_for_delivery",           # informational, high-tempo
    "delivered",                  # informational, archival
    "in_transit",                 # informational
    "other",                      # carrier email we recognised but can't classify
)

# Map carrier name → set of From-domain substrings.  Case-insensitive.
_CARRIER_DOMAINS: dict[str, tuple[str, ...]] = {
    "dragonfly": (
        "dragonflyshipping.ca",
        "dragonflyshipping.com",
        "ca.dragonflyinternational.com",
        "intelcom.ca",
    ),
    "canada_post": ("canadapost.ca", "canadapost-postescanada.ca", "postescanada"),
    "ups": ("ups.com",),
    "fedex": ("fedex.com",),
    "purolator": ("purolator.com",),
    "amazon": ("amazon.ca", "amazon.com"),
}

# Subject-line regex → event kind.  First match wins, evaluated in order.
# Designed from real email corpus: data/run_log.jsonl + live Gmail sample
# (2026-04-23 Dragonfly thread).
_SUBJECT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Reschedule wording: Dragonfly sends "We received your request!" plus the
    # body mentions "reschedule".  We match on body in _match_body as a fallback.
    (re.compile(r"\brescheduled?\b", re.I), "return_pickup_rescheduled"),
    (re.compile(r"\bpickup\s+(scheduled|confirmed)\b", re.I),
     "return_pickup_scheduled"),
    (re.compile(r"\b(missed|unsuccessful)\s+pickup\b", re.I),
     "return_pickup_missed"),
    (re.compile(r"\b(delivery\s+(failed|unsuccessful)|we\s+missed\s+you)\b", re.I),
     "delivery_failed"),
    (re.compile(r"\b(out\s+for\s+delivery|on\s+the\s+way|in\s+the\s+next\s+hour)\b", re.I),
     "out_for_delivery"),
    (re.compile(r"\b(delivered|was\s+delivered|hooray)\b", re.I), "delivered"),
    (re.compile(r"\b(scheduled\s+for\s+today|arrives|shipment\s+has\s+shipped)\b", re.I),
     "in_transit"),
)

# Body fallback patterns — checked only if subject didn't match.
_BODY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"return\s+pickup\s+has\s+been\s+rescheduled", re.I),
     "return_pickup_rescheduled"),
    (re.compile(r"reschedule\s+your\s+pickup\s+to\s*:?\s*\d", re.I),
     "return_pickup_rescheduled"),
    (re.compile(r"your\s+return\s+pickup\s+will\s+be\s+modified", re.I),
     "return_pickup_rescheduled"),
)

# Kinds considered "pending" (still need Alexander's attention).
PENDING_KINDS: frozenset[str] = frozenset({
    "return_pickup_rescheduled",
    "return_pickup_scheduled",
    "return_pickup_missed",
    "delivery_failed",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShipmentSignal:
    """A single classified carrier-email event."""

    message_id: str
    thread_id: str
    carrier: str
    kind: str
    subject: str
    sender: str
    date: str                 # raw Date: header
    tracking_id: Optional[str] = None
    scheduled_date: Optional[str] = None  # parsed YYYY-MM-DD where available
    snippet: str = ""

    def is_pending(self) -> bool:
        return self.kind in PENDING_KINDS


@dataclass
class ShipmentSummary:
    """Aggregated snapshot suitable for heartbeat / dashboard."""

    generated_at: str
    total: int = 0
    pending: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_carrier: dict[str, int] = field(default_factory=dict)
    pending_signals: list[ShipmentSignal] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "total": self.total,
            "pending": self.pending,
            "by_kind": dict(self.by_kind),
            "by_carrier": dict(self.by_carrier),
            "pending_signals": [
                {
                    "message_id": s.message_id,
                    "thread_id": s.thread_id,
                    "carrier": s.carrier,
                    "kind": s.kind,
                    "subject": s.subject,
                    "sender": s.sender,
                    "date": s.date,
                    "tracking_id": s.tracking_id,
                    "scheduled_date": s.scheduled_date,
                }
                for s in self.pending_signals
            ],
        }


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

_TRACKING_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Dragonfly / Intelcom
    re.compile(r"\b(INTLCM[A-Z0-9]{6,})\b"),
    # Canada Post
    re.compile(r"\b([0-9]{16})\b"),
    # UPS
    re.compile(r"\b(1Z[A-Z0-9]{16})\b"),
    # FedEx (12 or 15 digits)
    re.compile(r"\b([0-9]{12}|[0-9]{15})\b"),
)

_SCHEDULED_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"),       # 2026/04/10
    re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"),       # 2026-04-10
    re.compile(r"\b(?:on|to|for)\s+([A-Z][a-z]+\s+\d{1,2})\b"),
)


def _classify_carrier(sender: str) -> Optional[str]:
    """Return carrier name for the From-header or None if unrecognised."""
    if not sender:
        return None
    low = sender.lower()
    for carrier, domains in _CARRIER_DOMAINS.items():
        for d in domains:
            if d in low:
                return carrier
    return None


def _classify_kind(subject: str, body: str) -> str:
    """Return event kind from subject + body patterns.  'other' if none match."""
    subj = subject or ""
    for pat, kind in _SUBJECT_PATTERNS:
        if pat.search(subj):
            return kind
    if body:
        for pat, kind in _BODY_PATTERNS:
            if pat.search(body):
                return kind
    return "other"


def _extract_tracking(text: str) -> Optional[str]:
    """Return the first tracking-id-looking string in ``text``, or None."""
    if not text:
        return None
    for pat in _TRACKING_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def _extract_scheduled_date(body: str) -> Optional[str]:
    """Return a normalised YYYY-MM-DD string if one appears in a likely-schedule
    context.  Returns None if no match or if parsing is ambiguous."""
    if not body:
        return None
    for pat in _SCHEDULED_DATE_PATTERNS[:2]:  # numeric only (first two)
        m = pat.search(body)
        if m:
            try:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2020 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    return f"{y:04d}-{mo:02d}-{d:02d}"
            except (ValueError, IndexError):
                continue
    return None


def classify_message(msg: dict) -> Optional[ShipmentSignal]:
    """Classify a single Gmail-shaped message dict.

    Expected input shape (matches what ``gmail_read`` produces plus the
    ``id`` / ``threadId`` fields from ``gmail_search``)::

        {
            "id": "...",
            "threadId": "...",
            "headers": {"From": ..., "Subject": ..., "Date": ...},
            "body": "...",
            "snippet": "...",          # optional
        }

    Returns ``None`` if the sender isn't a recognised carrier.
    """
    headers = msg.get("headers") or {}
    sender = headers.get("From") or headers.get("from") or ""
    carrier = _classify_carrier(sender)
    if carrier is None:
        return None
    subject = headers.get("Subject") or headers.get("subject") or ""
    date = headers.get("Date") or headers.get("date") or ""
    body = msg.get("body") or ""
    snippet = msg.get("snippet") or ""
    kind = _classify_kind(subject, body)
    tracking = _extract_tracking(subject + "\n" + body)
    scheduled = (
        _extract_scheduled_date(body)
        if kind in PENDING_KINDS
        else None
    )
    return ShipmentSignal(
        message_id=msg.get("id") or "",
        thread_id=msg.get("threadId") or msg.get("thread_id") or "",
        carrier=carrier,
        kind=kind,
        subject=subject,
        sender=sender,
        date=date,
        tracking_id=tracking,
        scheduled_date=scheduled,
        snippet=snippet[:300],
    )


def summarize(
    messages: Iterable[dict],
    *,
    now: Optional[datetime] = None,
) -> ShipmentSummary:
    """Classify + aggregate a batch of messages.

    Non-carrier messages are silently dropped.  Stable sort order:
    pending signals come first, within pending they're sorted by date
    descending (most recent first) when the Date header parses.
    """
    now = now or datetime.now(timezone.utc)
    signals = [s for s in (classify_message(m) for m in messages) if s is not None]

    by_kind: dict[str, int] = {}
    by_carrier: dict[str, int] = {}
    for s in signals:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
        by_carrier[s.carrier] = by_carrier.get(s.carrier, 0) + 1

    pending = [s for s in signals if s.is_pending()]
    # newest pending first (best-effort: RFC-2822 dates sometimes missing)
    def _sort_key(s: ShipmentSignal) -> tuple:
        # Fall back to raw string comparison if parse fails.
        return (s.date or "",)
    pending.sort(key=_sort_key, reverse=True)

    return ShipmentSummary(
        generated_at=now.isoformat(),
        total=len(signals),
        pending=len(pending),
        by_kind=by_kind,
        by_carrier=by_carrier,
        pending_signals=pending,
    )
