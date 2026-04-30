"""Schemas Pydantic (presentation layer) pour les endpoints FastAPI."""

from pydantic import BaseModel


class MetricsResponse(BaseModel):
    """Contrat de réponse de `GET /metrics`."""

    messages_processed: int
    emails_sent: int
    errors: int
