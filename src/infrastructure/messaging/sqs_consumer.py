from __future__ import annotations

"""Adaptateur SQS/SES/S3 pour le microservice Notifier.

Ce module implémente l’infrastructure “messaging”:

- **Long-poll SQS**: reçoit des messages contenant un `FareResult` (JSON).
- **Tracing X-Ray**: récupère `X-Amzn-Trace-Id` (MessageAttributes) et crée un
  segment par message + sous-segments par sous-opération (PDF, SES, S3).
- **Orchestration technique**: contrôle de concurrence (sémaphore) + exécution
  boto3 dans un `ThreadPoolExecutor` (boto3 est bloquant).
- **Ports concrets**: `SESClient`, `S3Client`, `MetricsClient` injectés dans le
  use case `ProcessFareResultUseCase`.

Effets de bord:
- Appels réseau AWS (SQS/SES/S3/CloudWatch)
- CPU/IO (rendu PDF via `infrastructure.pdf.pdf_generator`)
- I/O disque optionnel si `REPORT_SAVE_TO_DISK=true`
"""

import asyncio
import contextlib
import json
import os
import pathlib
import subprocess  # nosec B404
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from jinja2 import Template

from application.use_cases.process_fare_result_use_case import ProcessFareResultUseCase
from config import settings
from domain.entities.fare_result import FareResult
from infrastructure.pdf.pdf_generator import render_audit_pdf, resolve_audit_report_html
from logger import logger
from shared.notifier_helpers import (
    AUDIT_REPORT_LAYOUT,
    extract_user_name,
    greeting_word,
    get_status_color,
    success_email_body_html,
)
from xray_config import begin_segment, end_segment, put_annotation, subsegment


_EMAIL_TEMPLATE_CACHE: dict[str, Template] = {}
_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"


def _render_email_template(relative_path: str, **ctx: object) -> str:
    """Rend un template HTML email depuis `src/templates/` (cache en mémoire)."""
    tpl = _EMAIL_TEMPLATE_CACHE.get(relative_path)
    if tpl is None:
        raw = (_TEMPLATE_DIR / relative_path).read_text(encoding="utf-8")
        tpl = Template(raw)
        _EMAIL_TEMPLATE_CACHE[relative_path] = tpl
    return tpl.render(**ctx)

_AUDIT_REPORT_LAYOUT = AUDIT_REPORT_LAYOUT


def _email_domain(addr: str | None) -> str:
    """Retourne un domaine d’email pour logs/metrics (sans PII).

    Exemples:
    - "user@example.com" -> "example.com"
    - None / invalide -> "unknown"
    """
    if not addr:
        return "unknown"
    try:
        parts = str(addr).split("@", 1)
        return (parts[1] if len(parts) == 2 else "unknown").lower()
    except Exception:
        return "unknown"


class SESClient:
    """Client SES concret (boto3) pour envoyer des emails HTML, avec PJ optionnelle.

    - **Utilisé par**: `ProcessFareResultUseCase` (injecté) et certains chemins
      d’erreur du consumer.
    - **Effets de bord**: appels réseau SES.
    - **Particularité sandbox**: si `SES_SANDBOX_MODE=true` et
      `SES_SANDBOX_TEST_EMAIL` défini, l’email est routé vers ce destinataire
      unique (et le sujet est annoté avec le destinataire initial).
    """

    def __init__(self):
        if settings.aws_profile:
            session = boto3.Session(profile_name=settings.aws_profile)
            self.client = session.client("ses", region_name=settings.aws_region)
        else:
            self.client = boto3.client("ses", region_name=settings.aws_region)

    def send_email(
        self,
        recipient: str,
        subject: str,
        body: str,
        attachment_data: Optional[bytes] = None,
        attachment_name: str = "report.pdf",
    ) -> bool:
        """Envoie un email via SES.

        - **Rôle / impact**:
          - sans PJ: `ses.send_email`
          - avec PJ: construit un MIME multipart et utilise `ses.send_raw_email`
        - **Utilise**: `settings.ses_sender_email`, sandbox flags, boto3 SES.
        - **Utilisé par**: `ProcessFareResultUseCase` et `SQSConsumer`.
        - **Effets de bord**: réseau (SES) + logs.
        - **Retour**: `True` si l’appel SES a réussi, sinon `False` (exceptions
          absorbées et loguées).
        """
        try:
            actual_recipient = recipient

            if settings.ses_sandbox_mode and settings.ses_sandbox_test_email:
                actual_recipient = settings.ses_sandbox_test_email
                subject = f"{subject} (Original recipient: {recipient})"

            if attachment_data:
                from email.mime.application import MIMEApplication
                from email.mime.multipart import MIMEMultipart
                from email.mime.text import MIMEText

                msg = MIMEMultipart()
                msg["Subject"] = subject
                msg["From"] = settings.ses_sender_email
                msg["To"] = actual_recipient

                msg.attach(MIMEText(body, "html", "utf-8"))

                attachment = MIMEApplication(attachment_data, _subtype="pdf")
                attachment.add_header(
                    "Content-Disposition", "attachment", filename=attachment_name
                )
                msg.attach(attachment)

                self.client.send_raw_email(
                    Source=settings.ses_sender_email,
                    Destinations=[actual_recipient],
                    RawMessage={"Data": msg.as_string()},
                )
            else:
                self.client.send_email(
                    Source=settings.ses_sender_email,
                    Destination={"ToAddresses": [actual_recipient]},
                    Message={
                        "Subject": {"Data": subject, "Charset": "UTF-8"},
                        "Body": {"Html": {"Data": body, "Charset": "UTF-8"}},
                    },
                )

            logger.info(
                "Email sent (from_domain=%s to_domain=%s intended_domain=%s)",
                _email_domain(settings.ses_sender_email),
                _email_domain(actual_recipient),
                _email_domain(recipient),
            )
            return True

        except Exception as e:
            error_msg = str(e)
            if "MessageRejected" in error_msg and "not verified" in error_msg:
                logger.error(
                    "SES Error: Recipient email not verified. "
                    "To send to unverified emails, enable SES_SANDBOX_MODE=true and set SES_SANDBOX_TEST_EMAIL in .env. "
                    "Recipient: %s. Error: %s",
                    recipient,
                    error_msg,
                )
            else:
                logger.error("Error sending email: %s", error_msg)
            return False


class S3Client:
    """Client S3 concret (boto3) pour archiver les PDFs générés.

    - **Utilisé par**: `ProcessFareResultUseCase` (injecté) et legacy consumer.
    - **Effets de bord**: écriture d’objets S3.
    """

    def __init__(self):
        if settings.aws_profile:
            session = boto3.Session(profile_name=settings.aws_profile)
            self.client = session.client("s3", region_name=settings.aws_region)
        else:
            self.client = boto3.client("s3", region_name=settings.aws_region)

    def upload_pdf(
        self, *, pdf_bytes: bytes, key: str, audit_template: str = ""
    ) -> str:
        """Upload un PDF vers S3 et renvoie l’URI `s3://...`.

        - **Préconditions**:
          - `S3_PDF_ENABLED=true`
          - `S3_PDF_BUCKET` non vide
        - **Sécurité**: support optionnel SSE-KMS via `S3_PDF_SSE_KMS_KEY_ID`.
        - **Utilisé par**: pipeline succès (use case / consumer legacy).
        - **Effets de bord**: réseau (S3).
        - **Erreurs**: lève `RuntimeError` si la feature est désactivée ou mal configurée.
        """
        if not settings.s3_pdf_enabled:
            raise RuntimeError("S3 PDF upload disabled (S3_PDF_ENABLED=false)")
        if not settings.s3_pdf_bucket:
            raise RuntimeError("Missing S3_PDF_BUCKET for PDF upload")

        put_kwargs = {"ContentType": "application/pdf"}
        if settings.s3_pdf_sse_kms_key_id:
            put_kwargs["ServerSideEncryption"] = "aws:kms"
            put_kwargs["SSEKMSKeyId"] = settings.s3_pdf_sse_kms_key_id

        self.client.put_object(
            Bucket=settings.s3_pdf_bucket,
            Key=key,
            Body=pdf_bytes,
            Metadata={
                "ff_audit_template": (audit_template or "").strip().lower() or "unknown"
            },
            **put_kwargs,
        )
        return f"s3://{settings.s3_pdf_bucket}/{key}"


def _get_status_color(status: str) -> str:
    """Compat: wrapper historique pour `shared.notifier_helpers.get_status_color`."""
    return get_status_color(status)


def _extract_user_name(recipient_email: str, metadata: Optional[dict] = None) -> str:
    """Compat: wrapper historique pour `shared.notifier_helpers.extract_user_name`."""
    return extract_user_name(recipient_email, metadata)


def _success_email_body_html(
    *,
    user_name: str,
    ref_short: str,
    fare_event_id: str,
    original_subject: str,
    _s3_uri: Optional[str],
) -> str:
    """Compat: wrapper historique pour `shared.notifier_helpers.success_email_body_html`."""
    return success_email_body_html(
        user_name=user_name,
        ref_short=ref_short,
        fare_event_id=fare_event_id,
        original_subject=original_subject,
        s3_uri=_s3_uri,
    )


class MetricsClient:
    """Émetteur de métriques CloudWatch (best-effort).

    - **Rôle / impact**: publie des métriques applicatives dans le namespace
      `FairFare/Notifier` avec une dimension `Environment`.
    - **Utilisé par**: `SQSConsumer` et `ProcessFareResultUseCase` (injection).
    - **Effets de bord**: réseau (CloudWatch). Les échecs sont volontairement
      ignorés pour ne pas perturber le workflow principal.
    """

    NAMESPACE = "FairFare/Notifier"

    def __init__(self):
        try:
            self._cw = boto3.client("cloudwatch", region_name=settings.aws_region)
            self._env = settings.environment
        except Exception:
            self._cw = None

    def emit(self, metric_name: str, value: float, unit: str = "Count") -> None:
        """Publie une métrique CloudWatch.

        - **Utilise**: `cloudwatch.put_metric_data`.
        - **Utilisé par**: pipeline message (durée), PDF success/errors, S3 upload,
          email success/errors.
        - **Effets de bord**: réseau (CloudWatch).
        """
        if self._cw is None:
            return
        with contextlib.suppress(Exception):
            self._cw.put_metric_data(
                Namespace=self.NAMESPACE,
                MetricData=[
                    {
                        "MetricName": metric_name,
                        "Value": value,
                        "Unit": unit,
                        "Dimensions": [{"Name": "Environment", "Value": self._env}],
                    }
                ],
            )


class SQSConsumer:
    """Consumer SQS (long polling) qui traite des `FareResult`.

    Architecture:
    - boto3 est bloquant ⇒ appels SQS/SES/S3 exécutés via `ThreadPoolExecutor`.
    - chaque message est traité dans `_process_message_inner` avec un segment X-Ray.
    - la décision métier (quel email / PDF / S3) est déléguée au use case
      `ProcessFareResultUseCase`.

    `api_metrics` (optionnel) est un objet “in-memory metrics” (cf. `main.Metrics`)
    qui expose `increment_processed`, `increment_sent`, `increment_error`.

    Effets de bord: réseau (SQS/SES/S3/CW), CPU/IO PDF, logs, traces, I/O disque
    optionnel si `REPORT_SAVE_TO_DISK=true`.
    """

    def __init__(self, *, api_metrics: object | None = None):
        """Construit le consumer et ses dépendances concrètes.

        - **Utilise**: `settings.aws_profile` pour choisir Session vs client direct.
        - **Crée**: `SESClient`, `S3Client`, `MetricsClient`, `ProcessFareResultUseCase`.
        - **Effets de bord**: aucun appel réseau immédiat (clients créés, non utilisés).
        """
        if settings.aws_profile:
            session = boto3.Session(profile_name=settings.aws_profile)
            self.sqs_client = session.client("sqs", region_name=settings.aws_region)
        else:
            self.sqs_client = boto3.client("sqs", region_name=settings.aws_region)

        self.ses_client = SESClient()
        self.s3_client = S3Client()
        self.metrics = MetricsClient()
        self._api_metrics = api_metrics
        self._use_case = ProcessFareResultUseCase(
            ses_client=self.ses_client,
            s3_client=self.s3_client,
            metrics_client=self.metrics,
        )
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(getattr(settings, "consumer_executor_max_workers", 2)))
        )
        self._semaphore = asyncio.Semaphore(
            max(1, int(getattr(settings, "consumer_max_concurrent_messages", 10)))
        )

        logger.debug(
            "SQSConsumer initialized with queue: %s", settings.sqs_fare_result_queue_url
        )

    def start(self) -> None:
        """Démarre la boucle de consommation en arrière-plan.

        - **Utilisé par**: `main.lifespan` au startup.
        - **Effets de bord**: crée une `asyncio.Task` qui exécute `_consume_messages`.
        """
        if self.running:
            logger.warning("SQS Consumer already running")
            return

        self.running = True
        logger.info(
            "Starting SQS Consumer for queue: %s", settings.sqs_fare_result_queue_url
        )
        logger.info(
            "Consumer settings - max_messages: %s, wait_time: %ss, visibility_timeout: %ss",
            settings.sqs_max_messages,
            settings.sqs_wait_time_seconds,
            settings.sqs_visibility_timeout,
        )

        self.task = asyncio.create_task(self._consume_messages())
        logger.info("SQS Consumer background task created")

    async def stop(self):
        """Arrête proprement le consumer.

        - **Utilisé par**: `main.lifespan` au shutdown.
        - **Effets de bord**: annule la tâche + ferme l’executor (best-effort).
        """
        logger.info("Stopping SQS Consumer")
        self.running = False

        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.info("SQS Consumer task cancelled")
                raise

        logger.debug("Shutting down thread pool executor")
        self.executor.shutdown(wait=False)

    async def _consume_messages(self):
        """Boucle principale de long-poll SQS.

        - **Rôle / impact**: appelle `receive_message` en long polling; si messages,
          traite en parallèle via `asyncio.gather`.
        - **Utilise**: `self.sqs_client.receive_message` dans `run_in_executor`.
        - **Utilisé par**: `start()` (tâche background).
        - **Effets de bord**: réseau (SQS), logs; attend `CONSUMER_ERROR_DELAY_SECONDS`
          en cas d’erreur non fatale.
        """
        logger.info("SQS Consumer loop started - waiting for messages...")
        poll_count = 0
        err_streak = 0

        while self.running:
            try:
                poll_count += 1
                logger.debug("Polling SQS (attempt #%d)...", poll_count)

                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    self.executor,
                    lambda: self.sqs_client.receive_message(
                        QueueUrl=settings.sqs_fare_result_queue_url,
                        MaxNumberOfMessages=settings.sqs_max_messages,
                        WaitTimeSeconds=settings.sqs_wait_time_seconds,
                        VisibilityTimeout=settings.sqs_visibility_timeout,
                        MessageAttributeNames=["X-Amzn-Trace-Id"],
                    ),
                )

                messages = response.get("Messages", [])

                if not messages:
                    logger.debug("No messages received from SQS (poll #%d)", poll_count)
                    continue

                logger.info("Received %d messages from SQS", len(messages))

                await asyncio.gather(
                    *(self._process_message(message) for message in messages)
                )
                err_streak = 0

            except asyncio.CancelledError:
                logger.info("SQS consumer cancelled")
                raise
            except Exception as e:
                logger.error("Error in SQS consumer loop: %s", str(e), exc_info=True)
                err_streak += 1
                base = max(0.0, float(settings.consumer_error_delay_seconds))
                delay = min(30.0, base * (2 ** min(err_streak - 1, 5))) if base else 0.0
                if delay:
                    await asyncio.sleep(delay)

    async def _process_message(self, message: dict):
        """Wrapper concurrence: applique la limite de messages simultanés.

        - **Rôle / impact**: évite de surcharger CPU/mémoire/SES/PDF en limitant
          le nombre de `_process_message_inner` en parallèle.
        - **Utilise**: `self._semaphore`.
        - **Utilisé par**: `_consume_messages()`.
        - **Effets de bord**: aucun (contrôle de flux).
        """
        async with self._semaphore:
            await self._process_message_inner(message)

    async def _process_message_inner(self, message: dict):
        """Traite un message SQS unique (parse → use case → ack éventuel).

        Étapes:
        - parse trace header SQS (`X-Amzn-Trace-Id`) et démarre un segment X-Ray
        - decode JSON body
        - valide `FareResult(**body)`
          - si invalide: tente d’emailer une erreur de parsing (si `metadata.sender`)
            puis supprime le message (pour éviter boucle infinie).
        - extrait `recipient_email = metadata.sender`
          - si absent: supprime le message (pas de destinataire).
        - appelle `self._use_case.execute(...)` en injectant `self._build_report`
        - selon `ProcessResult`: incrémente métriques in-memory + supprime SQS si demandé
        - finally: émet la durée CloudWatch + termine le segment X-Ray

        - **Utilise**: `FareResult`, `ProcessFareResultUseCase`, `_delete_message`,
          `_send_parsing_error_email`, X-Ray helpers, métriques.
        - **Utilisé par**: `_process_message()`.
        - **Effets de bord**: réseau (SES/SQS/CW), traces X-Ray, logs; PDF/IO via callback.
        """
        incoming_trace_header = self._extract_trace_header(message)
        begin_segment("notifier_sqs_process_message", trace_header=incoming_trace_header)
        _t0 = time.monotonic()
        message_id = message.get("MessageId")
        receipt_handle = message.get("ReceiptHandle")

        try:
            hb_task = None
            if receipt_handle:
                hb_task = asyncio.create_task(
                    self._visibility_heartbeat(receipt_handle=receipt_handle)
                )
            body = self._decode_body_json(message)
            logger.info("Processing message %s", message_id)
            fare_result, parse_err = self._try_parse_fare_result(body)
            if fare_result is None:
                await self._handle_invalid_fare_result(
                    raw_body=body,
                    error_message=parse_err or "invalid FareResult payload",
                    receipt_handle=receipt_handle,
                )
                return

            self._annotate_message_context(
                fare_result=fare_result,
                message_id=message_id,
                recipient_email=self._extract_sender_email(fare_result),
            )

            recipient_email = self._extract_sender_email(fare_result)
            if not recipient_email:
                await self._handle_missing_sender(receipt_handle=receipt_handle, message_id=message_id)
                return

            await self._execute_use_case_and_finalize(
                fare_result=fare_result,
                recipient_email=recipient_email,
                receipt_handle=receipt_handle,
            )

        except asyncio.CancelledError:
            # Respect cancellation for fast shutdown while still ending the trace.
            raise
        except Exception as e:
            logger.error(
                "Error processing message %s: %s", message_id, str(e), exc_info=True
            )
            self._inc_errors(1)
        finally:
            if hb_task is not None:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task
            self.metrics.emit(
                "MessageProcessingDuration",
                (time.monotonic() - _t0) * 1000,
                unit="Milliseconds",
            )
            end_segment()

    def _extract_trace_header(self, message: dict) -> str | None:
        msg_attrs = message.get("MessageAttributes") or {}
        trace_attr = msg_attrs.get("X-Amzn-Trace-Id")
        return trace_attr.get("StringValue") if trace_attr else None

    def _decode_body_json(self, message: dict) -> object:
        return json.loads(message.get("Body", "{}"))

    def _try_parse_fare_result(self, body: object) -> tuple[FareResult | None, str | None]:
        try:
            if not isinstance(body, dict):
                return None, "Body is not a JSON object"
            return FareResult(**body), None
        except Exception as e:
            logger.error("Validation error parsing FareResult: %s", str(e))
            return None, str(e)

    def _extract_sender_email(self, fare_result: FareResult) -> str | None:
        return fare_result.metadata.get("sender") if fare_result.metadata else None

    def _annotate_message_context(
        self, *, fare_result: FareResult, message_id: object, recipient_email: str | None
    ) -> None:
        put_annotation("fare_event_id", fare_result.fare_event_id or "unknown")
        put_annotation("message_id", str(message_id or "unknown"))
        put_annotation("recipient_domain", _email_domain(recipient_email))

    async def _handle_invalid_fare_result(
        self,
        *,
        raw_body: object,
        error_message: str,
        receipt_handle: str | None,
    ) -> None:
        recipient_email = None
        if isinstance(raw_body, dict):
            recipient_email = (raw_body.get("metadata") or {}).get("sender")
        if recipient_email:
            await self._send_parsing_error_email(
                str(recipient_email),
                error_message,
                raw_body,
            )
            self._inc_emails_sent(1)
        if receipt_handle:
            await self._delete_message(receipt_handle)
            self._inc_messages_processed(1)

    async def _handle_missing_sender(
        self, *, receipt_handle: str | None, message_id: object
    ) -> None:
        logger.warning("Message %s missing sender in metadata", message_id)
        if receipt_handle:
            await self._delete_message(receipt_handle)
            self._inc_messages_processed(1)

    async def _execute_use_case_and_finalize(
        self,
        *,
        fare_result: FareResult,
        recipient_email: str,
        receipt_handle: str | None,
    ) -> None:
        result = await self._use_case.execute(
            fare_result=fare_result,
            recipient_email=recipient_email,
            build_report=self._build_report,
        )
        if result.emails_sent:
            self._inc_emails_sent(result.emails_sent)
        if result.errors:
            self._inc_errors(result.errors)
        if result.should_delete_message and receipt_handle:
            await self._delete_message(receipt_handle)
            self._inc_messages_processed(1)
        if not result.should_delete_message:
            self.metrics.emit("MessageRetryRequested", 1)

    def _inc_messages_processed(self, n: int = 1) -> None:
        """Incrémente le compteur “messages processed” exposé par l’API.

        - **Utilise**: `api_metrics.increment_processed` si fourni.
        - **Utilisé par**: `_process_message_inner`.
        - **Effets de bord**: mutation mémoire (compteur in-memory).
        """
        if self._api_metrics is None:
            return
        inc = getattr(self._api_metrics, "increment_processed", None)
        if callable(inc):
            for _ in range(max(0, int(n))):
                inc()

    def _inc_emails_sent(self, n: int = 1) -> None:
        """Incrémente le compteur “emails sent” exposé par l’API."""
        if self._api_metrics is None:
            return
        inc = getattr(self._api_metrics, "increment_sent", None)
        if callable(inc):
            for _ in range(max(0, int(n))):
                inc()

    def _inc_errors(self, n: int = 1) -> None:
        """Incrémente le compteur “errors” exposé par l’API."""
        if self._api_metrics is None:
            return
        inc = getattr(self._api_metrics, "increment_error", None)
        if callable(inc):
            for _ in range(max(0, int(n))):
                inc()

    async def _send_error_email(
        self, fare_result: FareResult, recipient_email: str
    ) -> bool:
        """(Legacy) Envoie un email d’erreur pour un `FareResult`.

        Note: le chemin recommandé passe par `ProcessFareResultUseCase`.
        Cette méthode reste pour compat / parity et pour garder des chemins
        d’exécution simples côté consumer.
        """
        original_subject = (
            fare_result.metadata.get("subject", "") if fare_result.metadata else ""
        )
        user_name = _extract_user_name(recipient_email, fare_result.metadata)
        color = _get_status_color(fare_result.status)

        subject_line = f"Re: {original_subject}"
        error_content = fare_result.error_message or "An unknown error occurred"

        body = _render_email_template(
            "emails/error_email.html",
            greeting_word=greeting_word(),
            user_name=user_name,
            color=color,
            error_content=error_content,
            fare_event_id=fare_result.fare_event_id,
        )

        # boto3 SES is blocking; run in a thread to keep async semantics.
        def _do_send() -> bool:
            with subsegment("notifier_send_error_email"):
                return self.ses_client.send_email(
                    recipient=recipient_email,
                    subject=subject_line,
                    body=body,
                )

        sent = await asyncio.to_thread(_do_send)
        if sent:
            logger.info(
                "Error email sent to %s for fare %s",
                recipient_email,
                fare_result.fare_event_id,
            )
        else:
            logger.error(
                "Failed to send error email to %s for fare %s",
                recipient_email,
                fare_result.fare_event_id,
            )
        return sent

    async def _send_parsing_error_email(
        self, recipient_email: str, error_message: str, raw_body: object
    ) -> None:
        """Envoie un email quand le payload SQS est invalide (FareResult non parseable).

        - **Rôle / impact**: signaler une erreur “technique” (schema/validation)
          plutôt qu’un échec d’analyse classique.
        - **Utilise**: `SESClient.send_email`, `subsegment`.
        - **Utilisé par**: `_process_message_inner` lors d’exception `FareResult(**body)`.
        - **Effets de bord**: réseau SES + logs + trace X-Ray.

        Design choice:
        - le message est ensuite supprimé pour éviter un retry infini d’un payload
          structurellement invalide.
        """
        metadata = raw_body.get("metadata", {}) if isinstance(raw_body, dict) else {}
        original_subject = metadata.get("subject", "Unknown Subject")
        fare_event_id = (
            raw_body.get("fare_event_id", "UNKNOWN")
            if isinstance(raw_body, dict)
            else "UNKNOWN"
        )
        user_name = _extract_user_name(
            recipient_email, metadata if isinstance(raw_body, dict) else None
        )

        subject_line = f"Re: {original_subject}"

        body = _render_email_template(
            "emails/parsing_error_email.html",
            greeting_word=greeting_word(),
            user_name=user_name,
            error_message=error_message,
            fare_event_id=fare_event_id,
        )

        def _do_send() -> None:
            with subsegment("notifier_send_parsing_error_email"):
                self.ses_client.send_email(
                    recipient=recipient_email,
                    subject=subject_line,
                    body=body,
                )

        await asyncio.to_thread(_do_send)
        logger.info(
            "Parsing error email sent to %s for fare %s", recipient_email, fare_event_id
        )

    async def _build_report(
        self, fare_result: FareResult
    ) -> tuple[str, Optional[bytes], Optional[pathlib.Path], Optional[pathlib.Path]]:
        """Construit le HTML et (optionnellement) le PDF d’audit.

        - **Rôle / impact**:
          - génère l’HTML via `resolve_audit_report_html(fare_payload)`
          - rend le PDF via `render_audit_pdf` si `PDF_ENABLED=true`
          - optionnel: écrit `.html`/`.pdf` sur disque si `REPORT_SAVE_TO_DISK=true`
          - optionnel: ouvre le PDF localement si `REPORT_OPEN_AFTER_GENERATE=true`
        - **Utilise**: `resolve_audit_report_html`, `render_audit_pdf`,
          `asyncio.to_thread`, `settings.report_*`, `subsegment("notifier_generate_pdf")`.
        - **Utilisé par**:
          - `ProcessFareResultUseCase` via callback `build_report`
          - `_send_success_email` (legacy)
        - **Effets de bord**: CPU/IO PDF, I/O disque, ouverture applicative locale, logs, traces.
        """
        ref_id = fare_result.fare_event_id or "unknown"
        ref_short = ref_id[:8].upper()
        fare_payload = fare_result.model_dump()
        logger.info(
            "Building audit report (layout=%s, templates/audit_executive.html) for fare %s",
            _AUDIT_REPORT_LAYOUT,
            ref_id,
        )

        out_html, out_pdf = self._resolve_report_output_paths(ref_short)

        html_str = self._render_and_optionally_save_html(
            fare_payload=fare_payload, ref_id=ref_id, out_html=out_html
        )

        pdf = await self._render_and_optionally_save_pdf(
            html_str=html_str,
            fare_payload=fare_payload,
            ref_id=ref_id,
            out_pdf=out_pdf,
        )

        return html_str, pdf, out_html, out_pdf

    def _resolve_report_output_paths(
        self, ref_short: str
    ) -> tuple[Optional[pathlib.Path], Optional[pathlib.Path]]:
        if not settings.report_save_to_disk:
            return None, None
        out_dir = pathlib.Path(__file__).resolve().parent.parent.parent
        out_pdf = out_dir / f"fairfare_audit_{ref_short}_{_AUDIT_REPORT_LAYOUT}.pdf"
        out_html = out_dir / f"fairfare_audit_{ref_short}_{_AUDIT_REPORT_LAYOUT}.html"
        return out_html, out_pdf

    def _render_and_optionally_save_html(
        self,
        *,
        fare_payload: dict,
        ref_id: str,
        out_html: Optional[pathlib.Path],
    ) -> str:
        html_str = resolve_audit_report_html(fare_payload, _AUDIT_REPORT_LAYOUT)
        logger.info("Audit HTML rendered (layout=%s, fare=%s)", _AUDIT_REPORT_LAYOUT, ref_id)
        if out_html is not None:
            out_html.write_text(html_str, encoding="utf-8")
            logger.info("HTML saved %s", out_html)
        return html_str

    async def _render_and_optionally_save_pdf(
        self,
        *,
        html_str: str,
        fare_payload: dict,
        ref_id: str,
        out_pdf: Optional[pathlib.Path],
    ) -> Optional[bytes]:
        if not settings.pdf_enabled:
            logger.info("PDF disabled (PDF_ENABLED=false)")
            return None
        try:
            with subsegment("notifier_generate_pdf"):
                pdf = await asyncio.to_thread(render_audit_pdf, html_str, fare_payload)
            if pdf is not None:
                logger.info(
                    "Audit PDF rendered (layout=%s, fare=%s, %d bytes)",
                    _AUDIT_REPORT_LAYOUT,
                    ref_id,
                    len(pdf),
                )
            if out_pdf is not None and pdf is not None:
                out_pdf.write_bytes(pdf)
                logger.info("PDF saved %s (%d bytes)", out_pdf, len(pdf))
                if settings.report_open_after_generate:
                    await asyncio.to_thread(self._open_report_file, out_pdf)
            return pdf
        except Exception as e:
            logger.error("PDF rendering failed: %s", str(e), exc_info=True)
            return None

    def _open_report_file(self, path: pathlib.Path) -> None:
        if sys.platform == "win32":
            os.startfile(str(path))  # nosec B606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)  # nosec B603 B607
        else:
            subprocess.run(["xdg-open", str(path)], check=False)  # nosec B603 B607

    async def _send_success_email(
        self, fare_result: FareResult, recipient_email: str
    ) -> bool:
        """(Legacy) Pipeline succès “consumer-centric”.

        Cette méthode ré-implémente le flux succès (build PDF → upload S3 → email),
        mais le chemin recommandé passe par `ProcessFareResultUseCase`.

        - **Utilisé par**: non utilisé dans le chemin principal actuel (conservé
          pour parité/compat).
        - **Effets de bord**: CPU/IO PDF + réseau (S3/SES) + métriques + traces.
        """
        original_subject = (
            fare_result.metadata.get("subject", "") if fare_result.metadata else ""
        )
        ref_id = fare_result.fare_event_id or "unknown"
        ref_short = ref_id[:8].upper()
        osub = (original_subject or "").strip()
        subject_line = (
            f"Re: {osub} — Your FairFare comparative analysis [{ref_short}]"
            if osub
            else f"Your FairFare comparative analysis [{ref_short}]"
        )

        _html_for_pdf, pdf, _out_html, _out_pdf = await self._build_report(fare_result)
        if not pdf:
            logger.error("PDF generation failed for fare %s", ref_id)
            self.metrics.emit("PdfGenerationErrors", 1)
            return False
        self.metrics.emit("PdfGenerationSuccess", 1)

        s3_uri: Optional[str] = None
        if settings.s3_pdf_enabled:
            key = f"{settings.s3_pdf_prefix.rstrip('/')}/{ref_id}.pdf"
            try:
                with subsegment("notifier_upload_pdf_s3"):
                    s3_uri = self.s3_client.upload_pdf(
                        pdf_bytes=pdf,
                        key=key,
                        audit_template=_AUDIT_REPORT_LAYOUT,
                    )
                logger.info(
                    "PDF uploaded to S3 (layout=%s) %s key=%s",
                    _AUDIT_REPORT_LAYOUT,
                    s3_uri,
                    key,
                )
                self.metrics.emit("S3PdfUploadSuccess", 1)
            except (ClientError, Exception) as e:
                logger.error(
                    "PDF upload to S3 failed; continuing without archive link: %s",
                    str(e),
                    exc_info=True,
                )
                self.metrics.emit("S3PdfUploadErrors", 1)
                s3_uri = None
        else:
            logger.info("S3 PDF upload disabled (S3_PDF_ENABLED=false)")

        user_name = _extract_user_name(recipient_email, fare_result.metadata)
        body_html = _success_email_body_html(
            user_name=user_name,
            ref_short=ref_short,
            fare_event_id=ref_id,
            original_subject=original_subject,
            _s3_uri=s3_uri,
        )

        with subsegment("notifier_send_audit_email"):
            sent = self.ses_client.send_email(
                recipient=recipient_email,
                subject=subject_line,
                body=body_html,
                attachment_data=pdf,
                attachment_name=f"FairFare_Audit_{ref_short}_{_AUDIT_REPORT_LAYOUT}.pdf",
            )

        if sent:
            logger.info(
                "Audit email sent (fare %s, layout=%s)", ref_id, _AUDIT_REPORT_LAYOUT
            )
            self.metrics.emit("AuditEmailSuccess", 1)
            return True
        logger.error("Audit email failed (fare %s)", ref_id)
        self.metrics.emit("AuditEmailErrors", 1)
        return False

    async def _delete_message(self, receipt_handle: str):
        """Supprime (ack) un message SQS.

        - **Rôle / impact**: retire définitivement le message de la queue.
        - **Utilise**: `sqs.delete_message` via `run_in_executor` (boto3 bloquant).
        - **Utilisé par**: `_process_message_inner` (après succès, payload invalide, etc.).
        - **Effets de bord**: réseau (SQS).
        """
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self.executor,
                lambda: self.sqs_client.delete_message(
                    QueueUrl=settings.sqs_fare_result_queue_url,
                    ReceiptHandle=receipt_handle,
                ),
            )
            logger.info("Message successfully deleted from SQS queue")
        except Exception as e:
            logger.error("Error deleting message from SQS: %s", str(e), exc_info=True)

    async def _visibility_heartbeat(self, *, receipt_handle: str) -> None:
        """
        Prolonge périodiquement le visibility timeout pendant le traitement.

        Sans heartbeat, un rendu PDF ou une latence SES > `SQS_VISIBILITY_TIMEOUT`
        peut rendre le message visible et déclencher un traitement en double.
        """
        interval = max(1, int(getattr(settings, "sqs_heartbeat_interval_seconds", 60)))
        extend = max(1, int(getattr(settings, "sqs_heartbeat_extend_seconds", 120)))
        # This task is scoped to a single message processing; it will be cancelled
        # in `_process_message_inner` once processing finishes.
        while True:
            await asyncio.sleep(interval)
            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(
                    self.executor,
                    lambda: self.sqs_client.change_message_visibility(
                        QueueUrl=settings.sqs_fare_result_queue_url,
                        ReceiptHandle=receipt_handle,
                        VisibilityTimeout=extend,
                    ),
                )
                logger.debug("Extended message visibility by %ss", extend)
            except Exception as exc:
                logger.warning(
                    "Failed to extend message visibility (will rely on SQS retry): %s",
                    str(exc),
                )
