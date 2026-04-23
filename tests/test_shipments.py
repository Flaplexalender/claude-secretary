"""Tests for secretary.shipments — carrier email classifier."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from secretary.shipments import (
    PENDING_KINDS,
    ShipmentSignal,
    ShipmentSummary,
    classify_message,
    summarize,
)


# ---------------------------------------------------------------------------
# Fixtures — realistic samples pulled from Alexander's actual Gmail (redacted)
# ---------------------------------------------------------------------------

DRAGONFLY_RESCHEDULE_APR9 = {
    "id": "19d705aed5b63d4c",
    "threadId": "19d705aed5b63d4c",
    "headers": {
        "From": "Dragonfly <noreplyweb@dragonflyshipping.com>",
        "Subject": "We received your request!",
        "Date": "Thu, 9 Apr 2026 03:48:19 +0000",
    },
    "body": (
        "Your return pickup will be modified!\n"
        "Hi Alexander,\n"
        "Your return pickup has been rescheduled.\n"
        "We have received your request to modify your scheduled return "
        "pickup.\n"
        "As you requested, we will try to reschedule your pickup to : "
        "2026/04/10 . We will send you a notification on the day of the "
        "pickup.\n"
    ),
    "snippet": "Your return pickup will be modified! Hi Alexander, Your return pickup has been rescheduled.",
}

DRAGONFLY_DELIVERED = {
    "id": "19d4c55dd7a1",
    "threadId": "19d4c55dd7a1",
    "headers": {
        "From": "Dragonfly Notification <notifications@ca.dragonflyinternational.com>",
        "Subject": "Hooray! Your package was delivered!",
        "Date": "Thu, 2 Apr 2026 02:03:50 +0000",
    },
    "body": "Your package tracking-id=INTLCMI046287564 was delivered to your door.",
    "snippet": "delivered",
}

DRAGONFLY_OUT_FOR_DELIVERY = {
    "id": "19d4bddc5a0",
    "threadId": "19d4bddc5a0",
    "headers": {
        "From": "Dragonfly Notification <notifications@ca.dragonflyinternational.com>",
        "Subject": "Your package will be there in the next hour!",
        "Date": "Thu, 2 Apr 2026 01:45:18 +0000",
    },
    "body": "tracking-id=INTLCMI046287564",
    "snippet": "on the way",
}

CANADAPOST_IN_TRANSIT = {
    "id": "cp001",
    "threadId": "cp001",
    "headers": {
        "From": "notifications@canadapost.ca",
        "Subject": "Your shipment has shipped",
        "Date": "Mon, 20 Apr 2026 10:00:00 +0000",
    },
    "body": "Tracking number: 1234567890123456",
}

UPS_DELIVERY_FAILED = {
    "id": "ups001",
    "threadId": "ups001",
    "headers": {
        "From": "mcinfo@ups.com",
        "Subject": "Delivery unsuccessful — we missed you",
        "Date": "Tue, 21 Apr 2026 15:00:00 +0000",
    },
    "body": "Tracking 1Z999AA10123456784. Driver attempted delivery.",
}

NEWSLETTER = {
    "id": "nl001",
    "threadId": "nl001",
    "headers": {
        "From": "Balaji <balajis@substack.com>",
        "Subject": "Popups Are the New Startups",
        "Date": "Sun, 12 Oct 2025 19:23:30 +0000",
    },
    "body": "nothing to do with shipping",
}


# ---------------------------------------------------------------------------
# classify_message
# ---------------------------------------------------------------------------

class TestClassifyMessage:
    def test_dragonfly_reschedule_body_detected(self):
        """The exact email Alexander missed — must be caught."""
        sig = classify_message(DRAGONFLY_RESCHEDULE_APR9)
        assert sig is not None
        assert sig.carrier == "dragonfly"
        assert sig.kind == "return_pickup_rescheduled"
        assert sig.is_pending() is True
        assert sig.scheduled_date == "2026-04-10"

    def test_dragonfly_delivered_not_pending(self):
        sig = classify_message(DRAGONFLY_DELIVERED)
        assert sig is not None
        assert sig.carrier == "dragonfly"
        assert sig.kind == "delivered"
        assert sig.is_pending() is False
        assert sig.tracking_id == "INTLCMI046287564"

    def test_dragonfly_out_for_delivery(self):
        sig = classify_message(DRAGONFLY_OUT_FOR_DELIVERY)
        assert sig is not None
        assert sig.kind == "out_for_delivery"

    def test_canada_post_in_transit(self):
        sig = classify_message(CANADAPOST_IN_TRANSIT)
        assert sig is not None
        assert sig.carrier == "canada_post"
        assert sig.kind == "in_transit"

    def test_ups_delivery_failed_is_pending(self):
        sig = classify_message(UPS_DELIVERY_FAILED)
        assert sig is not None
        assert sig.carrier == "ups"
        assert sig.kind == "delivery_failed"
        assert sig.is_pending() is True
        assert sig.tracking_id == "1Z999AA10123456784"

    def test_non_carrier_returns_none(self):
        assert classify_message(NEWSLETTER) is None

    def test_missing_headers_returns_none(self):
        assert classify_message({"id": "x", "body": "hi"}) is None

    def test_empty_dict_returns_none(self):
        assert classify_message({}) is None

    def test_handles_lowercase_header_keys(self):
        msg = dict(DRAGONFLY_DELIVERED)
        msg["headers"] = {
            "from": DRAGONFLY_DELIVERED["headers"]["From"],
            "subject": DRAGONFLY_DELIVERED["headers"]["Subject"],
            "date": DRAGONFLY_DELIVERED["headers"]["Date"],
        }
        sig = classify_message(msg)
        assert sig is not None
        assert sig.kind == "delivered"

    def test_unknown_carrier_kind_is_other(self):
        """Carrier recognised, but subject/body don't match any pattern."""
        msg = {
            "id": "x",
            "threadId": "x",
            "headers": {
                "From": "Dragonfly <noreply@dragonflyshipping.com>",
                "Subject": "Survey invitation",
                "Date": "Wed, 22 Apr 2026 00:00:00 +0000",
            },
            "body": "Please rate your experience.",
        }
        sig = classify_message(msg)
        assert sig is not None
        assert sig.kind == "other"
        assert sig.is_pending() is False


# ---------------------------------------------------------------------------
# summarize
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_empty(self):
        s = summarize([])
        assert s.total == 0
        assert s.pending == 0
        assert s.by_kind == {}
        assert s.by_carrier == {}
        assert s.pending_signals == []

    def test_drops_non_carrier_messages(self):
        s = summarize([NEWSLETTER, NEWSLETTER, DRAGONFLY_DELIVERED])
        assert s.total == 1
        assert s.by_carrier == {"dragonfly": 1}

    def test_counts_and_pending(self):
        s = summarize([
            DRAGONFLY_RESCHEDULE_APR9,
            DRAGONFLY_DELIVERED,
            DRAGONFLY_OUT_FOR_DELIVERY,
            UPS_DELIVERY_FAILED,
            NEWSLETTER,
        ])
        assert s.total == 4
        assert s.pending == 2   # reschedule + delivery_failed
        assert s.by_kind["return_pickup_rescheduled"] == 1
        assert s.by_kind["delivery_failed"] == 1
        assert s.by_kind["delivered"] == 1
        assert s.by_kind["out_for_delivery"] == 1
        assert s.by_carrier == {"dragonfly": 3, "ups": 1}
        # pending signals are kept in most-recent-first order
        assert len(s.pending_signals) == 2
        assert s.pending_signals[0].date.startswith("Tue, 21 Apr")

    def test_as_dict_is_json_serialisable(self):
        s = summarize([DRAGONFLY_RESCHEDULE_APR9, UPS_DELIVERY_FAILED])
        d = s.as_dict()
        # must round-trip through json
        json.dumps(d)
        assert d["pending"] == 2
        assert d["by_carrier"]["dragonfly"] == 1
        assert d["by_carrier"]["ups"] == 1
        assert d["pending_signals"][0]["carrier"] in {"dragonfly", "ups"}
        assert "snippet" not in d["pending_signals"][0]  # trimmed from export

    def test_generated_at_uses_provided_now(self):
        fixed = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
        s = summarize([], now=fixed)
        assert s.generated_at == fixed.isoformat()


# ---------------------------------------------------------------------------
# Regression: PENDING_KINDS is a closed set used by both the dataclass and
# summary.  If these drift the detector will silently stop flagging things.
# ---------------------------------------------------------------------------

class TestPendingKindsInvariant:
    def test_pending_kinds_nonempty(self):
        assert len(PENDING_KINDS) >= 3

    def test_return_pickup_rescheduled_is_pending(self):
        assert "return_pickup_rescheduled" in PENDING_KINDS

    def test_delivery_failed_is_pending(self):
        assert "delivery_failed" in PENDING_KINDS
