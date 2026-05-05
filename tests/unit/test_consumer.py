import json
from unittest.mock import MagicMock

import pytest

from infrastructure.messaging.sqs_consumer import SQSConsumer
import asyncio


@pytest.mark.asyncio
async def test_consumer_deletes_message_after_success(monkeypatch):
    c = SQSConsumer(api_metrics=None)
    c.sqs_client = MagicMock()

    async def _execute(**kwargs):
        await asyncio.sleep(0)
        class _R:
            should_delete_message = True
            emails_sent = 1
            errors = 0

        return _R()

    c._use_case.execute = _execute  # type: ignore[attr-defined]

    msg = {
        "MessageId": "m1",
        "ReceiptHandle": "rh",
        "Body": json.dumps(
            {
                "id": "r1",
                "fare_event_id": "fe1",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "status": "analysis_complete",
                "metadata": {"sender": "user@example.com"},
            }
        ),
        "MessageAttributes": {},
    }

    await c._process_message_inner(msg)  # type: ignore[attr-defined]
    assert c.sqs_client.delete_message.called


@pytest.mark.asyncio
async def test_consumer_extends_visibility_while_processing(monkeypatch):
    c = SQSConsumer(api_metrics=None)
    c.sqs_client = MagicMock()
    c.sqs_client.change_message_visibility.return_value = {}

    # Make heartbeat fast
    monkeypatch.setattr("infrastructure.messaging.sqs_consumer.settings.sqs_heartbeat_interval_seconds", 1)
    monkeypatch.setattr("infrastructure.messaging.sqs_consumer.settings.sqs_heartbeat_extend_seconds", 10)

    async def _execute(**kwargs):
        await asyncio.sleep(2)

        class _R:
            should_delete_message = True
            emails_sent = 0
            errors = 0

        return _R()

    import asyncio

    c._use_case.execute = _execute  # type: ignore[attr-defined]

    msg = {
        "MessageId": "m-hb",
        "ReceiptHandle": "rh-hb",
        "Body": json.dumps(
            {
                "id": "r1",
                "fare_event_id": "fe1",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "status": "analysis_complete",
                "metadata": {"sender": "user@example.com"},
            }
        ),
        "MessageAttributes": {},
    }

    await c._process_message_inner(msg)  # type: ignore[attr-defined]
    assert c.sqs_client.change_message_visibility.called


@pytest.mark.asyncio
async def test_consumer_deletes_message_when_sender_missing():
    c = SQSConsumer(api_metrics=None)
    c.sqs_client = MagicMock()

    msg = {
        "MessageId": "m2",
        "ReceiptHandle": "rh2",
        "Body": json.dumps(
            {
                "id": "r2",
                "fare_event_id": "fe2",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "status": "analysis_complete",
                "metadata": {},  # missing sender
            }
        ),
        "MessageAttributes": {},
    }

    await c._process_message_inner(msg)  # type: ignore[attr-defined]
    assert c.sqs_client.delete_message.called


@pytest.mark.asyncio
async def test_consumer_invalid_payload_sends_parsing_email_and_deletes():
    c = SQSConsumer(api_metrics=None)
    c.sqs_client = MagicMock()
    c.ses_client = MagicMock()
    c.ses_client.send_email.return_value = True

    # Missing required fields for FareResult but includes metadata.sender so we notify.
    msg = {
        "MessageId": "m3",
        "ReceiptHandle": "rh3",
        "Body": json.dumps({"metadata": {"sender": "user@example.com", "subject": "Hi"}}),
        "MessageAttributes": {},
    }

    await c._process_message_inner(msg)  # type: ignore[attr-defined]
    assert c.ses_client.send_email.called
    assert c.sqs_client.delete_message.called
