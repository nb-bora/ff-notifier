from __future__ import annotations

"""Value Objects (DDD) — PdfReport.

Objet valeur immuable représentant un PDF (nom + content-type + bytes).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PdfReport:
    """Rapport PDF (VO)."""

    filename: str
    content_type: str
    data: bytes
