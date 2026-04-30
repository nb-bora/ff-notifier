from __future__ import annotations

"""Use case principal du microservice Notifier.

Ce module contient l’orchestration métier de haut niveau:

- **Entrée**: un `FareResult` (résultat d’analyse produit par un service amont) et
  une adresse email de destinataire.
- **Décision**: selon `fare_result.status`, envoyer soit un email d’erreur, soit
  un email de succès avec un PDF en pièce jointe (+ archivage S3 optionnel).
- **Sortie**: un `ProcessResult` qui indique à l’adaptateur SQS s’il faut
  supprimer le message (ack) ou le laisser visible pour retry.

Ce module ne parle pas directement à SQS: il reçoit des dépendances “ports”
(duck-typed) pour SES/S3/métriques et une callback `build_report(...)` fournie
par l’adaptateur infrastructure.

Effets de bord (indirects, via dépendances injectées):
- Appels réseau SES (envoi email)
- Appels réseau S3 (upload PDF) si activé
- Emission de métriques (CloudWatch ou autre sink)
- Traces X-Ray via `subsegment(...)`
"""

import asyncio
import time
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Awaitable, Callable
from typing import Optional

from jinja2 import Template

from config import settings
from application.interfaces.email_sender import IEmailSender
from application.interfaces.metrics_sink import IMetricsSink
from application.interfaces.pdf_store import IPdfStore
from domain.entities.fare_result import FareResult
from domain.enums.fare_result_status import FareResultStatus
from logger import logger
from shared.notifier_helpers import (
    AUDIT_REPORT_LAYOUT,
    extract_user_name,
    greeting_word,
    success_email_body_html,
)
from xray_config import subsegment


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


@dataclass(frozen=True)
class ProcessResult:
    """Résultat d’exécution du workflow (adapté au consumer SQS).

    - **should_delete_message**: `True` ⇒ le consumer peut supprimer le message
      SQS (ack). `False` ⇒ le message doit rester (retry ultérieur).
    - **emails_sent**: compteur logique (utilisé pour métriques in-memory/API).
    - **errors**: compteur logique (utilisé pour métriques in-memory/API).
    """

    should_delete_message: bool
    emails_sent: int = 0
    errors: int = 0


def _normalize_status(status: str) -> str:
    """Normalise `status` vers une valeur canonique.

    - **Rôle / impact**: si `status` correspond à `FareResultStatus`, renvoie la
      valeur Enum (string). Sinon renvoie la string brute.
    - **Utilise**: `domain.enums.fare_result_status.FareResultStatus`.
    - **Utilisé par**: `execute()` et `_send_error_email()` pour appliquer une
      logique stable même si le payload amont varie.
    - **Effets de bord**: aucun.
    """
    try:
        return FareResultStatus(status).value
    except Exception:
        return str(status)


class ProcessFareResultUseCase:
    """Orchestre le workflow Notifier (parité `ff-notifier`).

    Dépendances (duck-typed):
    - **ses_client**: `send_email(...) -> bool`
    - **s3_client**: `upload_pdf(...) -> str`
    - **metrics_client**: `emit(metric_name, value, unit="Count") -> None`

    Ce use case:
    - ne connaît pas SQS (c’est l’adaptateur `SQSConsumer` qui gère l’ack),
    - ne génère pas directement le PDF: il appelle `build_report(fare_result)`
      fourni par l’infrastructure, afin de garder une séparation “métier vs IO”.
    """

    def __init__(
        self,
        *,
        ses_client: IEmailSender,
        s3_client: IPdfStore,
        metrics_client: IMetricsSink,
    ) -> None:
        self._ses = ses_client
        self._s3 = s3_client
        self._metrics = metrics_client

    async def execute(
        self,
        *,
        fare_result: FareResult,
        recipient_email: str,
        build_report: Callable[
            [FareResult], Awaitable[tuple[str, Optional[bytes], object, object]]
        ],
    ) -> ProcessResult:
        """Exécute le workflow de notification pour un `FareResult`.

        - **Rôle / impact**:
          - `parsing_failed` / `validation_error` ⇒ email d’erreur.
          - `analysis_complete` ⇒ génération PDF + (optionnel) upload S3 + email.
          - statut inconnu ⇒ no-op + suppression (parité actuelle).
        - **Utilise**:
          - `_send_error_email`, `_send_success_email`
          - `build_report(fare_result)` (callback infrastructure)
        - **Utilisé par**: `infrastructure.messaging.sqs_consumer.SQSConsumer`.
        - **Effets de bord** (indirects): SES/S3/métriques/tracing/logging.
        """
        status = _normalize_status(fare_result.status)
        if status in ("parsing_failed", "validation_error"):
            sent = await self._send_error_email(fare_result, recipient_email)
            if not sent:
                return ProcessResult(should_delete_message=False, errors=1)
            return ProcessResult(should_delete_message=True, emails_sent=1)

        if status == "analysis_complete":
            ok = await self._send_success_email(
                fare_result=fare_result,
                recipient_email=recipient_email,
                build_report=build_report,
            )
            if not ok:
                return ProcessResult(should_delete_message=False, errors=1)
            return ProcessResult(should_delete_message=True, emails_sent=1)

        # statut inconnu: parité actuelle = no-op + delete
        logger.warning("Unknown fare_result.status=%s; deleting message", status)
        return ProcessResult(should_delete_message=True)

    async def _send_error_email(
        self, fare_result: FareResult, recipient_email: str
    ) -> bool:
        """Envoie un email d’erreur (HTML) à l’expéditeur initial.

        - **Rôle / impact**: informer l’utilisateur que l’analyse n’a pas pu
          aboutir, en conservant un threading email (`Re:`).
        - **Utilise**: `extract_user_name`, `subsegment`, `self._ses.send_email`.
        - **Utilisé par**: `execute()` pour statuts `parsing_failed|validation_error`.
        - **Effets de bord**: appel réseau SES + trace X-Ray + logs.
        """
        original_subject = (
            fare_result.metadata.get("subject", "") if fare_result.metadata else ""
        )
        user_name = extract_user_name(recipient_email, fare_result.metadata)
        subject_line = f"Re: {original_subject}"
        error_content = fare_result.error_message or "An unknown error occurred"

        # Couleur identique à ff-notifier
        color = (
            "#e74c3c"
            if _normalize_status(fare_result.status) != "analysis_complete"
            else "#2ecc71"
        )

        body = _render_email_template(
            "emails/error_email.html",
            greeting_word=greeting_word(),
            user_name=user_name,
            color=color,
            error_content=error_content,
            fare_event_id=fare_result.fare_event_id,
        )

        def _do_send() -> bool:
            with subsegment("notifier_send_error_email"):
                return self._ses.send_email(
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

    async def _send_success_email(
        self,
        *,
        fare_result: FareResult,
        recipient_email: str,
        build_report: Callable[
            [FareResult], Awaitable[tuple[str, Optional[bytes], object, object]]
        ],
    ) -> bool:
        """Envoie l’email de succès avec PDF (et archive S3 optionnelle).

        Pipeline:
        1) **Build report** via `build_report(fare_result)` avec timeout
           `settings.pdf_render_timeout_seconds`.
        2) **Upload S3** (optionnel) si `settings.s3_pdf_enabled`.
        3) **Send SES** avec pièce jointe PDF.

        - **Utilise**: `asyncio.wait_for`, `success_email_body_html`, `subsegment`,
          `self._s3.upload_pdf`, `self._ses.send_email`, `self._metrics.emit`.
        - **Utilisé par**: `execute()` pour statut `analysis_complete`.
        - **Effets de bord**: CPU/IO PDF (indirect), réseau (S3/SES), métriques, traces.
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

        # 1) Generate the PDF (guardrails)
        t0 = time.monotonic()
        try:
            _html_for_pdf, pdf, _out_html, _out_pdf = await asyncio.wait_for(
                build_report(fare_result),
                timeout=float(settings.pdf_render_timeout_seconds),
            )
        except asyncio.TimeoutError:
            logger.error(
                "PDF rendering timeout (>%ss) for fare %s",
                settings.pdf_render_timeout_seconds,
                ref_id,
            )
            self._metrics.emit("PdfGenerationErrors", 1)
            return False
        except Exception as e:
            logger.error(
                "PDF build failed for fare %s: %s", ref_id, str(e), exc_info=True
            )
            self._metrics.emit("PdfGenerationErrors", 1)
            return False
        dur_ms = (time.monotonic() - t0) * 1000
        logger.info("PDF pipeline duration: fare=%s duration_ms=%.2f", ref_id, dur_ms)
        self._metrics.emit("PdfPipelineDuration", dur_ms, unit="Milliseconds")

        if not pdf:
            logger.error("PDF generation failed for fare %s", ref_id)
            self._metrics.emit("PdfGenerationErrors", 1)
            return False
        self._metrics.emit("PdfGenerationSuccess", 1)

        # 2) Upload S3 — same bytes as attachment
        s3_uri: Optional[str] = None
        if settings.s3_pdf_enabled:
            key = f"{settings.s3_pdf_prefix.rstrip('/')}/{ref_id}.pdf"
            try:
                t_up = time.monotonic()
                with subsegment("notifier_upload_pdf_s3"):
                    s3_uri = self._s3.upload_pdf(
                        pdf_bytes=pdf,
                        key=key,
                        audit_template=AUDIT_REPORT_LAYOUT,
                    )
                self._metrics.emit(
                    "S3PdfUploadDuration",
                    (time.monotonic() - t_up) * 1000,
                    unit="Milliseconds",
                )
                logger.info(
                    "PDF uploaded to S3 (layout=%s) %s key=%s",
                    AUDIT_REPORT_LAYOUT,
                    s3_uri,
                    key,
                )
                self._metrics.emit("S3PdfUploadSuccess", 1)
            except Exception as e:
                logger.error(
                    "PDF upload to S3 failed; continuing without archive link: %s",
                    str(e),
                    exc_info=True,
                )
                self._metrics.emit("S3PdfUploadErrors", 1)
                s3_uri = None
        else:
            logger.info("S3 PDF upload disabled (S3_PDF_ENABLED=false)")

        # 3) Send email with PDF attachment
        user_name = extract_user_name(recipient_email, fare_result.metadata)
        body_html = success_email_body_html(
            user_name=user_name,
            ref_short=ref_short,
            fare_event_id=ref_id,
            original_subject=original_subject,
            s3_uri=s3_uri,
        )

        with subsegment("notifier_send_audit_email"):
            t_ses = time.monotonic()
            sent = self._ses.send_email(
                recipient=recipient_email,
                subject=subject_line,
                body=body_html,
                attachment_data=pdf,
                attachment_name=f"FairFare_Audit_{ref_short}_{AUDIT_REPORT_LAYOUT}.pdf",
            )
        self._metrics.emit(
            "SesSendDuration",
            (time.monotonic() - t_ses) * 1000,
            unit="Milliseconds",
        )

        if sent:
            logger.info(
                "Audit email sent (fare %s, layout=%s)", ref_id, AUDIT_REPORT_LAYOUT
            )
            self._metrics.emit("AuditEmailSuccess", 1)
            return True
        logger.error("Audit email failed (fare %s)", ref_id)
        self._metrics.emit("AuditEmailErrors", 1)
        return False
