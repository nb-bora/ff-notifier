import json
from unittest.mock import MagicMock

import pytest

from infrastructure.messaging.notifications_sqs_consumer import NotificationsSQSConsumer


@pytest.mark.asyncio
async def test_notifications_consumer_sends_email_and_deletes(monkeypatch):
    c = NotificationsSQSConsumer(api_metrics=None)
    c.sqs_client = MagicMock()
    c.ses_client = MagicMock()
    c.ses_client.send_email.return_value = True

    msg = {
        "MessageId": "m1",
        "ReceiptHandle": "rh",
        "Body": json.dumps(
            {
                "schema_version": 1,
                "event_id": "ev-1",
                "occurred_at": "2026-05-05T10:00:00Z",
                "service": "ff-ingestion",
                "environment": "dev",
                "category": "user_untreatable",
                "severity": "warning",
                "template_id": "user.untreatable.parse_failed",
                "failure_code": "PARSE_FAILED",
                "recipient": {"type": "user", "email": "user@example.com", "locale": "fr"},
                "context": {"sender": "user@example.com", "receive_count": 1},
                "variables": {"human_summary": "oops"},
            }
        ),
        "MessageAttributes": {},
    }

    await c._process_message_inner(msg)  # type: ignore[attr-defined]
    assert c.ses_client.send_email.called
    assert c.sqs_client.delete_message.called


@pytest.mark.asyncio
async def test_notifications_consumer_dedup_skips_second_send():
    c = NotificationsSQSConsumer(api_metrics=None)
    c.sqs_client = MagicMock()
    c.ses_client = MagicMock()
    c.ses_client.send_email.return_value = True

    body = {
        "schema_version": 1,
        "event_id": "ev-dup",
        "occurred_at": "2026-05-05T10:00:00Z",
        "service": "ff-ingestion",
        "environment": "dev",
        "category": "support_alert",
        "severity": "error",
        "template_id": "support.poison_message",
        "failure_code": "POISON_MESSAGE",
        "recipient": {"type": "support", "email": "support@example.com", "locale": "fr"},
        "context": {"receive_count": 3},
        "variables": {"human_summary": "x"},
    }

    msg1 = {
        "MessageId": "m1",
        "ReceiptHandle": "rh1",
        "Body": json.dumps(body),
        "MessageAttributes": {},
    }
    msg2 = {
        "MessageId": "m2",
        "ReceiptHandle": "rh2",
        "Body": json.dumps(body),
        "MessageAttributes": {},
    }

    await c._process_message_inner(msg1)  # type: ignore[attr-defined]
    await c._process_message_inner(msg2)  # type: ignore[attr-defined]

    assert c.ses_client.send_email.call_count == 1
    assert c.sqs_client.delete_message.call_count == 2

