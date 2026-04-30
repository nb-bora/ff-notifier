"""Schemas Pydantic (presentation layer) pour les endpoints FastAPI."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Contrat de réponse de `GET /health`."""

    status: str
    service: str
    version: str
    environment: str
    consumer_enabled: bool
    aws_region: str
    pdf_enabled: bool
    metrics: dict
