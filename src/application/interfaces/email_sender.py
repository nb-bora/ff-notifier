from __future__ import annotations

"""Port “email sender” (clean architecture).

Ce Protocol définit la forme attendue d’un composant capable d’envoyer un email
HTML, avec pièce jointe optionnelle.

Implémentation concrète actuelle: `infrastructure.messaging.sqs_consumer.SESClient`.
"""

from typing import Optional, Protocol


class IEmailSender(Protocol):
    """Port d’envoi email (SES ou autre provider)."""

    def send_email(
        self,
        *,
        recipient: str,
        subject: str,
        body_html: str,
        attachment_data: Optional[bytes] = None,
        attachment_name: str = "report.pdf",
    ) -> bool: ...
