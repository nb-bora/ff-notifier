from __future__ import annotations

"""Value Objects (DDD) — QrPayload.

Objet valeur immuable représentant le texte encodé dans un QR code.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class QrPayload:
    """Payload texte d’un QR code (VO)."""

    text: str
