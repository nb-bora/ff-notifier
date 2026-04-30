from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ExtractedTravelSchema(BaseModel):
    """
    Schéma Pydantic pour `fare_event.extracted_travel`.

    Objectif
    --------
    - Valider/structurer la sortie d’extraction OpenAI incluse dans un `FareEvent`.
    - Permettre `model_validate(...)` sur un payload JSON venant des logs ou de SQS.
    """

    origin: str | None = None
    destination: str | None = None

    trip_type: Literal["one_way", "round_trip"] | None = None
    cabin_class: Literal["economy", "premium_economy", "business", "first"] | None = (
        None
    )

    departure_date: str | None = None
    return_date: str | None = None

    passengers: int | None = None
    passengers_adults: int | None = None
    passengers_children: int | None = None

    missing_fields: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] | None = None


class FareEventSchema(BaseModel):
    """
    Schéma Pydantic pour la clé `fare_event` telle que publiée downstream.

    Usage
    -----
    ```python
    fare_event = FareEventSchema.model_validate(payload["fare_event"])
    ```
    """

    id: str
    sender: str
    parsed_at: str
    email_body_length: int
    status: Literal["parsed", "parsing_failed"]
    subject: str | None = None

    extracted_travel: ExtractedTravelSchema | dict = Field(default_factory=dict)
    openai_response_id: str | None = None
    failure_reasons: list[str] | None = None

    message_id: str | None = None
    in_reply_to: str | None = None
    references: str | None = None
    reply_to: str | None = None
