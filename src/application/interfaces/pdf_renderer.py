from __future__ import annotations

"""Port “PDF renderer” (clean architecture).

Décrit un composant qui transforme de l’HTML en PDF (bytes).
Implémentation concrète actuelle (fonctionnelle): `infrastructure.pdf.pdf_generator.render_audit_pdf`.
"""

from typing import Protocol


class IPdfRenderer(Protocol):
    """Port de rendu HTML → PDF."""

    def render_pdf(self, *, html: str, base_url: str | None = None) -> bytes: ...
