from __future__ import annotations

"""Value Objects (DDD) — EmailRecipient.

Objet valeur minimal (immuable) pour représenter un destinataire email dans le domaine.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmailRecipient:
    """Adresse email d’un destinataire (VO)."""

    email: str
