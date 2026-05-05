from __future__ import annotations

import asyncio
import json
import time
import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import boto3
from jinja2 import Template

from config import settings
from domain.entities.notification_event import NotificationEvent, is_supported_schema_version
from infrastructure.dedup.dedup_store import build_dedup_store
from infrastructure.messaging.sqs_consumer import SESClient, _email_domain
from logger import logger
from xray_config import begin_segment, end_segment, put_annotation

_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"
_TPL_CACHE: dict[str, Template] = {}


_TEMPLATE_BY_ID: dict[str, str] = {
    "user.untreatable.parse_failed": "notifications/template-demande-intraitable-parse-impossible.html",
    "user.untreatable.tier1_hard": "notifications/template-demande-intraitable-tier1-hard.html",
    "user.untreatable.poison_message": "notifications/template-demande-intraitable-poison-message.html",
    "support.server_error": "notifications/template-support-alerte-incident-serveur.html",
    "support.poison_message": "notifications/template-support-alerte-poison-message.html",
    "support.missing_sender": "notifications/template-support-alerte-expediteur-introuvable.html",
}

_SUPPORT_SERVER_ERROR_TEMPLATE_ID = "support.server_error"

def _render_notification_template(template_path: str, *, event: NotificationEvent) -> str:
    tpl = _TPL_CACHE.get(template_path)
    if tpl is None:
        raw = (_TEMPLATE_DIR / template_path).read_text(encoding="utf-8")
        tpl = Template(raw)
        _TPL_CACHE[template_path] = tpl
    return tpl.render(event=event)


def _subject_for(event: NotificationEvent) -> str:
    # Keep it stable and short; templates contain detailed content.
    if event.category == "support_alert":
        return f"[{event.service}] Support alert: {event.failure_code}"
    return "FairFare — Action requise"


def _recipient_email_for(event: NotificationEvent) -> str | None:
    if event.recipient.type == "user":
        return event.recipient.email or event.context.sender
    # support
    return event.recipient.email or (settings.notifications_support_email or None)


def _build_support_fallback_event(
    *,
    failure_code: str,
    human_summary: str,
    raw_body_excerpt: str | None,
    original_event_id: str | None,
) -> NotificationEvent:
    return NotificationEvent(
        schema_version=1,
        event_id=original_event_id or f"fallback-{int(time.time())}",
        occurred_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        service=getattr(settings, "service_name", "ff-notifier"),
        environment=settings.environment,
        category="support_alert",
        severity="error",
        template_id=_SUPPORT_SERVER_ERROR_TEMPLATE_ID,
        failure_code=failure_code,
        recipient={"type": "support", "email": settings.notifications_support_email, "locale": "fr"},
        context={"receive_count": None},
        variables={
            "human_summary": human_summary,
            "error": {"class": "NotificationEventError", "message": human_summary},
            "source_artifact": {"raw_body_excerpt": raw_body_excerpt},
        },
    )


class NotificationsSQSConsumer:
    """Consumer SQS (long polling) pour `NotificationEvent` (queue notifications)."""

    def __init__(self, *, api_metrics: object | None = None):
        if settings.aws_profile:
            session = boto3.Session(profile_name=settings.aws_profile)
            self.sqs_client = session.client("sqs", region_name=settings.aws_region)
        else:
            self.sqs_client = boto3.client("sqs", region_name=settings.aws_region)

        self.ses_client = SESClient()
        self._api_metrics = api_metrics
        self._dedup = build_dedup_store()
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(settings, "consumer_executor_max_workers", 2)))
        )
        self._semaphore = asyncio.Semaphore(
            max(1, int(getattr(settings, "consumer_max_concurrent_messages", 10)))
        )

    def start(self) -> None:
        if self.running:
            logger.warning("Notifications SQS Consumer already running")
            return
        self.running = True
        logger.info(
            "Starting Notifications SQS Consumer for queue: %s",
            settings.sqs_notifications_queue_url,
        ) 
        self.task = asyncio.create_task(self._consume_messages())

    async def stop(self) -> None:
        logger.info("Stopping Notifications SQS Consumer")
        self.running = False
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.executor.shutdown(wait=False)

    async def _consume_messages(self) -> None:
        logger.info("Notifications consumer loop started - waiting for messages...")
        while self.running:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                self.executor,
                lambda: self.sqs_client.receive_message(
                    QueueUrl=settings.sqs_notifications_queue_url,
                    MaxNumberOfMessages=settings.sqs_max_messages,
                    WaitTimeSeconds=settings.sqs_wait_time_seconds,
                    VisibilityTimeout=settings.sqs_visibility_timeout,
                    MessageAttributeNames=["X-Amzn-Trace-Id"],
                ),
            )
            messages = response.get("Messages", [])
            if not messages:
                continue
            await asyncio.gather(*(self._process_message(m) for m in messages))

    async def _process_message(self, message: dict) -> None:
        async with self._semaphore:
            await self._process_message_inner(message)

    async def _process_message_inner(self, message: dict) -> None:
        begin_segment("notifier_sqs_process_notification")
        _t0 = time.monotonic()
        receipt_handle = message.get("ReceiptHandle")
        message_id = message.get("MessageId")
        try:
            body = json.loads(message.get("Body", "{}"))
            if not isinstance(body, dict):
                logger.error("Notification body is not JSON object: %s", message_id)
                await self._send_support_fallback_if_possible(
                    failure_code="POISON_MESSAGE",
                    human_summary="Notification body is not a JSON object",
                    raw_body_excerpt=str(body)[:1000],
                    original_event_id=None,
                )
                await self._delete_if_possible(receipt_handle)
                return
            event = NotificationEvent(**body)
            if not is_supported_schema_version(event.schema_version):
                logger.error(
                    "Unsupported NotificationEvent schema_version=%s id=%s",
                    event.schema_version,
                    event.event_id,
                )
                await self._send_support_fallback_if_possible(
                    failure_code="POISON_MESSAGE",
                    human_summary=f"Unsupported schema_version={event.schema_version}",
                    raw_body_excerpt=json.dumps(body, ensure_ascii=False)[:1000],
                    original_event_id=event.event_id,
                )
                await self._delete_if_possible(receipt_handle)
                return

            # Dedup (best-effort)
            if await self._dedup.seen_or_mark(event_id=event.event_id):
                logger.info("Duplicate NotificationEvent skipped: %s", event.event_id)
                await self._delete_if_possible(receipt_handle)
                return

            put_annotation("notification_category", event.category)
            put_annotation("recipient_domain", _email_domain(_recipient_email_for(event)))

            tpl_path = _TEMPLATE_BY_ID.get(event.template_id)
            if not tpl_path:
                logger.error("Unknown template_id=%s event_id=%s", event.template_id, event.event_id)
                await self._send_support_fallback_if_possible(
                    failure_code="POISON_MESSAGE",
                    human_summary=f"Unknown template_id={event.template_id}",
                    raw_body_excerpt=json.dumps(body, ensure_ascii=False)[:1000],
                    original_event_id=event.event_id,
                )
                await self._delete_if_possible(receipt_handle)
                return

            recipient = _recipient_email_for(event)
            if not recipient:
                logger.error(
                    "No recipient email for event_id=%s recipient.type=%s",
                    event.event_id,
                    event.recipient.type,
                )
                await self._send_support_fallback_if_possible(
                    failure_code="MISSING_SENDER",
                    human_summary="No recipient email resolvable for notification",
                    raw_body_excerpt=json.dumps(body, ensure_ascii=False)[:1000],
                    original_event_id=event.event_id,
                )
                await self._delete_if_possible(receipt_handle)
                return

            subject = _subject_for(event)
            html = _render_notification_template(tpl_path, event=event)

            def _do_send() -> bool:
                return self.ses_client.send_email(recipient=recipient, subject=subject, body=html)

            sent = await asyncio.to_thread(_do_send)
            if not sent:
                # Retry: do not delete message
                self._inc_errors(1)
                return

            self._inc_emails_sent(1)
            await self._delete_if_possible(receipt_handle)
            self._inc_messages_processed(1)
        except Exception as e:
            logger.error("Error processing notification message %s: %s", message_id, str(e), exc_info=True)
            self._inc_errors(1)
        finally:
            end_segment()
            _ = _t0  # placeholder for future duration metrics

    async def _send_support_fallback_if_possible(
        self,
        *,
        failure_code: str,
        human_summary: str,
        raw_body_excerpt: str | None,
        original_event_id: str | None,
    ) -> None:
        if not settings.notifications_support_email:
            return
        try:
            ev = _build_support_fallback_event(
                failure_code=failure_code,
                human_summary=human_summary,
                raw_body_excerpt=raw_body_excerpt,
                original_event_id=original_event_id,
            )
            tpl = _TEMPLATE_BY_ID[_SUPPORT_SERVER_ERROR_TEMPLATE_ID]
            html = _render_notification_template(tpl, event=ev)
            subject = _subject_for(ev)

            def _do_send() -> bool:
                return self.ses_client.send_email(
                    recipient=settings.notifications_support_email,
                    subject=subject,
                    body=html,
                )

            await asyncio.to_thread(_do_send)
        except Exception:
            logger.warning("Failed to send support fallback notification", exc_info=True)

    async def _delete_if_possible(self, receipt_handle: str | None) -> None:
        if not receipt_handle:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self.executor,
            lambda: self.sqs_client.delete_message(
                QueueUrl=settings.sqs_notifications_queue_url,
                ReceiptHandle=receipt_handle,
            ),
        )

    def _inc_messages_processed(self, n: int = 1) -> None:
        if self._api_metrics is None:
            return
        inc = getattr(self._api_metrics, "increment_processed", None)
        if callable(inc):
            for _ in range(max(0, int(n))):
                inc()

    def _inc_emails_sent(self, n: int = 1) -> None:
        if self._api_metrics is None:
            return
        inc = getattr(self._api_metrics, "increment_sent", None)
        if callable(inc):
            for _ in range(max(0, int(n))):
                inc()

    def _inc_errors(self, n: int = 1) -> None:
        if self._api_metrics is None:
            return
        inc = getattr(self._api_metrics, "increment_error", None)
        if callable(inc):
            for _ in range(max(0, int(n))):
                inc()

