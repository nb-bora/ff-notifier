from __future__ import annotations

"""Schéma `NotificationEvent` (contrat partagé) – Notifier.

Ce module définit le payload JSON consommé depuis la queue notifications
(`fairfare-box-notifications`) tel que décrit dans:
- `Ingestion/docs/NOTIFICATIONS_CONTRACT.md`

Objectif:
- Valider strictement le contrat côté Notifier.
- Permettre un routing stable par `template_id`, `category`, `recipient`.
"""

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class NotificationRecipient(BaseModel):
    type: Literal["user", "support"]
    email: Optional[str] = None
    locale: Literal["fr", "en"] = "fr"


class NotificationContext(BaseModel):
    sender: Optional[str] = None
    subject: Optional[str] = None
    source_message_id: Optional[str] = None
    received_at: Optional[str] = None
    trace_id: Optional[str] = None
    correlation_id: Optional[str] = None
    sqs_source_message_id: Optional[str] = None
    receive_count: Optional[int] = None


class NotificationEvent(BaseModel):
    schema_version: int = Field(..., description="Schema version (currently 1)")
    event_id: str
    occurred_at: str
    service: str
    environment: str
    category: Literal["user_untreatable", "support_alert"]
    severity: Literal["info", "warning", "error", "critical"]
    template_id: str
    failure_code: str
    recipient: NotificationRecipient
    context: NotificationContext = Field(default_factory=NotificationContext)
    variables: Dict[str, Any] = Field(default_factory=dict)


def is_supported_schema_version(version: int) -> bool:
    return version == 1

