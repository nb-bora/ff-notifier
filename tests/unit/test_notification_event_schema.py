import pytest

from domain.entities.notification_event import NotificationEvent


def test_notification_event_schema_minimal_user():
    ev = NotificationEvent(
        schema_version=1,
        event_id="e1",
        occurred_at="2026-05-05T10:00:00Z",
        service="ff-ingestion",
        environment="dev",
        category="user_untreatable",
        severity="warning",
        template_id="user.untreatable.parse_failed",
        failure_code="PARSE_FAILED",
        recipient={"type": "user", "email": "user@example.com", "locale": "fr"},
        context={"sender": "user@example.com"},
        variables={"human_summary": "oops"},
    )
    assert ev.recipient.type == "user"
    assert ev.variables["human_summary"] == "oops"


def test_notification_event_rejects_bad_locale():
    with pytest.raises(Exception):
        NotificationEvent(
            schema_version=1,
            event_id="e1",
            occurred_at="2026-05-05T10:00:00Z",
            service="ff-ingestion",
            environment="dev",
            category="user_untreatable",
            severity="warning",
            template_id="user.untreatable.parse_failed",
            failure_code="PARSE_FAILED",
            recipient={"type": "user", "email": "user@example.com", "locale": "de"},
            context={"sender": "user@example.com"},
            variables={},
        )

