"""Tests for gmail_filter.py — message categorization and filtering."""
import pytest
from secretary.gmail_filter import GmailMessage, filter_messages


class TestGmailMessage:
    """Tests for GmailMessage dataclass."""

    def test_is_automated_newsletter(self):
        """Should detect newsletter senders."""
        msg = GmailMessage(
            sender="noreply@newsletter.example.com",
            subject="Weekly Digest",
            snippet="This week's updates",
            message_id="msg_123"
        )
        assert msg.is_automated()

    def test_is_automated_notification(self):
        """Should detect automated notification keywords."""
        msg = GmailMessage(
            sender="alerts@system.example.com",
            subject="System Notification: Status Update",
            snippet="Your system is running normally",
            message_id="msg_456"
        )
        assert msg.is_automated()

    def test_is_automated_delivery_failure(self):
        """Should detect delivery failure messages."""
        msg = GmailMessage(
            sender="mailer-daemon@example.com",
            subject="Mail Delivery Failed",
            snippet="Could not deliver to recipient",
            message_id="msg_789"
        )
        assert msg.is_automated()

    def test_is_not_automated_human(self):
        """Should not flag human messages."""
        msg = GmailMessage(
            sender="alice@example.com",
            subject="Let's catch up",
            snippet="Hey, how are you?",
            message_id="msg_human"
        )
        assert not msg.is_automated()

    def test_get_urgency_urgent_keywords(self):
        """Should detect urgent keywords in subject."""
        msg = GmailMessage(
            sender="boss@company.com",
            subject="URGENT: Action Required Immediately",
            snippet="This needs your attention asap",
            message_id="msg_urgent"
        )
        assert msg.get_urgency() == "urgent"

    def test_get_urgency_critical_in_snippet(self):
        """Should detect critical keyword in snippet."""
        msg = GmailMessage(
            sender="ops@company.com",
            subject="System Status",
            snippet="Critical issue in production — blocked all users",
            message_id="msg_critical"
        )
        assert msg.get_urgency() == "urgent"

    def test_get_urgency_normal(self):
        """Should return normal for non-urgent messages."""
        msg = GmailMessage(
            sender="friend@example.com",
            subject="How was your weekend?",
            snippet="Hope you had a good time",
            message_id="msg_normal"
        )
        assert msg.get_urgency() == "normal"

    def test_one_line_summary_normal(self):
        """Should format normal message summary correctly."""
        msg = GmailMessage(
            sender="alice@example.com",
            subject="Project Update",
            snippet="Finished the Q1 review",
            message_id="msg_001",
            is_unread=True
        )
        summary = msg.one_line_summary()
        assert "alice@example.com" in summary
        assert "Project Update" in summary
        assert "[URGENT]" not in summary

    def test_one_line_summary_urgent(self):
        """Should mark urgent messages in summary."""
        msg = GmailMessage(
            sender="boss@company.com",
            subject="Urgent Review Needed",
            snippet="Please review asap",
            message_id="msg_002",
            is_unread=True
        )
        summary = msg.one_line_summary()
        assert "[URGENT]" in summary
        assert "boss@company.com" in summary

    def test_dataclass_fields(self):
        """Should have all required fields."""
        msg = GmailMessage(
            sender="test@example.com",
            subject="Test",
            snippet="Test snippet",
            message_id="test_id"
        )
        assert msg.sender == "test@example.com"
        assert msg.subject == "Test"
        assert msg.snippet == "Test snippet"
        assert msg.message_id == "test_id"
        assert msg.is_unread is True  # default

    def test_case_insensitive_pattern_matching(self):
        """Should match patterns regardless of case."""
        msg = GmailMessage(
            sender="NoReply@EXAMPLE.COM",
            subject="NEWSLETTER from our store",
            snippet="Check out our latest products",
            message_id="msg_case"
        )
        assert msg.is_automated()


class TestFilterMessages:
    """Tests for filter_messages function."""

    def test_filter_removes_automated(self):
        """Should remove automated/newsletter messages."""
        messages = [
            GmailMessage(
                sender="noreply@service.com",
                subject="Newsletter",
                snippet="Weekly update",
                message_id="auto_1"
            ),
            GmailMessage(
                sender="alice@example.com",
                subject="Let's meet",
                snippet="Are you free tomorrow?",
                message_id="human_1"
            ),
        ]
        result = filter_messages(messages)
        assert len(result) == 1
        assert result[0][0].sender == "alice@example.com"

    def test_filter_sorts_urgent_first(self):
        """Should place urgent messages first."""
        messages = [
            GmailMessage(
                sender="bob@example.com",
                subject="Nice article",
                snippet="Thought you'd enjoy this",
                message_id="normal_1"
            ),
            GmailMessage(
                sender="mgr@company.com",
                subject="URGENT: Approval Needed",
                snippet="This is critical",
                message_id="urgent_1"
            ),
        ]
        result = filter_messages(messages)
        assert len(result) == 2
        # Urgent should be first
        assert result[0][0].message_id == "urgent_1"
        assert result[1][0].message_id == "normal_1"

    def test_filter_empty_list(self):
        """Should handle empty message list."""
        result = filter_messages([])
        assert result == []

    def test_filter_all_automated(self):
        """Should return empty list if all messages are automated."""
        messages = [
            GmailMessage(
                sender="noreply@a.com",
                subject="Newsletter A",
                snippet="Update",
                message_id="auto_a"
            ),
            GmailMessage(
                sender="notifications@b.com",
                subject="Alert",
                snippet="Notification",
                message_id="auto_b"
            ),
        ]
        result = filter_messages(messages)
        assert len(result) == 0

    def test_filter_returns_tuples(self):
        """Should return list of (message, summary) tuples."""
        messages = [
            GmailMessage(
                sender="alice@example.com",
                subject="Quick question",
                snippet="Do you have time?",
                message_id="msg_q"
            ),
        ]
        result = filter_messages(messages)
        assert len(result) == 1
        msg, summary = result[0]
        assert isinstance(msg, GmailMessage)
        assert isinstance(summary, str)
        assert "alice@example.com" in summary
        assert "Quick question" in summary

    def test_filter_multiple_urgent(self):
        """Should preserve order of multiple urgent messages."""
        messages = [
            GmailMessage(
                sender="alice@example.com",
                subject="urgent: task 1",
                snippet="First task",
                message_id="urgent_a"
            ),
            GmailMessage(
                sender="bob@example.com",
                subject="critical: task 2",
                snippet="Second task",
                message_id="urgent_b"
            ),
        ]
        result = filter_messages(messages)
        assert len(result) == 2
        # Both urgent, should be sorted by summary (alphanumeric)
        summaries = [s for _, s in result]
        assert summaries == sorted(summaries)

    def test_filter_mixed_urgency(self):
        """Should sort mixed normal and urgent correctly."""
        messages = [
            GmailMessage(
                sender="friend@example.com",
                subject="Hello",
                snippet="Just saying hi",
                message_id="normal_hello"
            ),
            GmailMessage(
                sender="urgent@example.com",
                subject="URGENT: Please respond",
                snippet="Need immediate action",
                message_id="urgent_action"
            ),
            GmailMessage(
                sender="colleague@example.com",
                subject="Status update",
                snippet="Here's what I did today",
                message_id="normal_status"
            ),
        ]
        result = filter_messages(messages)
        assert len(result) == 3
        # First should be urgent
        assert "URGENT" in result[0][1]
        # Rest should be normal (sorted alphabetically)
        assert "URGENT" not in result[1][1]
        assert "URGENT" not in result[2][1]

    def test_filter_respects_unread_status(self):
        """Should preserve is_unread field in results."""
        messages = [
            GmailMessage(
                sender="alice@example.com",
                subject="Test",
                snippet="Snippet",
                message_id="msg_read",
                is_unread=False
            ),
            GmailMessage(
                sender="bob@example.com",
                subject="Test 2",
                snippet="Snippet 2",
                message_id="msg_unread",
                is_unread=True
            ),
        ]
        result = filter_messages(messages)
        messages_only = [msg for msg, _ in result]
        unread_msgs = [msg for msg in messages_only if msg.is_unread]
        assert len(unread_msgs) == 1
        assert unread_msgs[0].message_id == "msg_unread"


class TestIntegration:
    """Integration tests combining multiple components."""

    def test_realistic_inbox(self):
        """Test filtering realistic inbox with mix of message types."""
        messages = [
            # Newsletters/noise
            GmailMessage(
                sender="noreply@newsletter.com",
                subject="Weekly Digest",
                snippet="Top stories this week",
                message_id="newsletter_1"
            ),
            # Automated alerts
            GmailMessage(
                sender="alerts@service.com",
                subject="System notification: build complete",
                snippet="Your build has finished",
                message_id="auto_1"
            ),
            # Important human message
            GmailMessage(
                sender="boss@company.com",
                subject="URGENT: Review Needed ASAP",
                snippet="Please review the proposal before 5pm",
                message_id="urgent_1"
            ),
            # Normal human message
            GmailMessage(
                sender="colleague@company.com",
                subject="Lunch tomorrow?",
                snippet="Want to grab lunch?",
                message_id="normal_1"
            ),
            # Delivery failure
            GmailMessage(
                sender="mailer-daemon@example.com",
                subject="Mail Delivery Failed",
                snippet="Unable to deliver",
                message_id="bounce_1"
            ),
            # Another normal message
            GmailMessage(
                sender="friend@example.com",
                subject="How's the project going?",
                snippet="Just checking in",
                message_id="normal_2"
            ),
        ]

        result = filter_messages(messages)

        # Should filter out 3 automated messages (newsletter, alert, bounce)
        assert len(result) == 3

        # Extract message IDs for easier checking
        result_ids = [msg.message_id for msg, _ in result]

        # Should keep the human messages and urgent message
        assert "urgent_1" in result_ids
        assert "normal_1" in result_ids
        assert "normal_2" in result_ids

        # Should not keep automated messages
        assert "newsletter_1" not in result_ids
        assert "auto_1" not in result_ids
        assert "bounce_1" not in result_ids

        # Urgent should be first
        assert result[0][0].message_id == "urgent_1"

    def test_edge_case_special_characters_in_sender(self):
        """Should handle special characters in sender."""
        msg = GmailMessage(
            sender="info+tag@example.com",
            subject="Message",
            snippet="Content",
            message_id="special_chars"
        )
        result = filter_messages([msg])
        assert len(result) == 1
        assert "info+tag@example.com" in result[0][1]

    def test_edge_case_very_long_subject(self):
        """Should handle very long subjects."""
        msg = GmailMessage(
            sender="alice@example.com",
            subject="This is a very long subject line " * 10,
            snippet="Content",
            message_id="long_subject"
        )
        result = filter_messages([msg])
        assert len(result) == 1
        assert len(result[0][1]) > 0

    def test_edge_case_empty_strings(self):
        """Should handle empty subject/snippet."""
        msg = GmailMessage(
            sender="alice@example.com",
            subject="",
            snippet="",
            message_id="empty"
        )
        result = filter_messages([msg])
        assert len(result) == 1
        summary = result[0][1]
        assert "alice@example.com" in summary
