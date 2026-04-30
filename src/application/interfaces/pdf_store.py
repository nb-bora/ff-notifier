from __future__ import annotations

"""Port “PDF store” (clean architecture).

Décrit un composant qui stocke un PDF (ex: S3) et renvoie une référence (URI).
Implémentation concrète actuelle: `infrastructure.messaging.sqs_consumer.S3Client`.
"""

from typing import Protocol


class IPdfStore(Protocol):
    """Port de stockage de PDF (ex: S3)."""

    def upload_pdf(
        self, *, pdf_bytes: bytes, key: str, audit_template: str = ""
    ) -> str: ...
