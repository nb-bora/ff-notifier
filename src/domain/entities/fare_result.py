from __future__ import annotations

"""Entités domaine Notifier.

`FareResult` représente le payload métier consommé depuis SQS (JSON) et produit
par le service d’analyse amont.

Notes:
- la validation est faite via Pydantic; un payload invalide déclenche un chemin
  “parsing error” côté consumer.
- `metadata` contient la majorité des informations nécessaires au rendu PDF.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class FareResult(BaseModel):
    """Résultat d’analyse (payload entrant) – parité `ff-notifier`.

    - **Utilisé par**:
      - `SQSConsumer._process_message_inner` (parse/validation)
      - `ProcessFareResultUseCase.execute` (branching par `status`)
      - `SQSConsumer._build_report` (sérialisation `model_dump()` pour PDF)
    - **Effets de bord**: peut lever une exception de validation lors de l’instanciation.
    """

    id: str = Field(..., description="Unique result ID")
    fare_event_id: str = Field(..., description="Reference to original FareEvent")
    timestamp: str = Field(..., description="Result timestamp (ISO UTC)")
    status: str = Field(
        ...,
        description="Result status: parsing_failed, validation_error, analysis_complete",
    )

    quoted_price: Optional[float] = Field(
        None, description="Price quoted in original email"
    )
    market_price: Optional[float] = Field(
        None, description="Market price from Amadeus API"
    )
    price_difference: Optional[float] = Field(
        None, description="Difference between quoted and market price"
    )
    confidence_score: Optional[float] = Field(
        None, description="Confidence in analysis (0-1)"
    )
    anomaly_detected: Optional[bool] = Field(
        None, description="Whether price anomaly was detected"
    )

    evidence_s3_key: Optional[str] = Field(None, description="S3 key for evidence JSON")
    error_message: Optional[str] = Field(
        None, description="Error or failure description"
    )
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional context")

    message_id: Optional[str] = Field(
        None, description="Email Message-ID header for threading"
    )
    in_reply_to: Optional[str] = Field(
        None, description="Email In-Reply-To header for threading"
    )
    references: Optional[str] = Field(
        None, description="Email References header for threading"
    )
    reply_to: Optional[str] = Field(
        None, description="Email Reply-To header for addressing"
    )
