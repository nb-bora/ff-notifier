from __future__ import annotations

"""Port “metrics sink” (clean architecture).

Permet d’émettre des métriques applicatives vers un backend (CloudWatch, StatsD…).
Implémentation concrète actuelle: `infrastructure.messaging.sqs_consumer.MetricsClient`.
"""

from typing import Protocol


class IMetricsSink(Protocol):
    """Port d’émission de métriques (best-effort)."""

    def emit(self, metric_name: str, value: float, unit: str = "Count") -> None: ...
