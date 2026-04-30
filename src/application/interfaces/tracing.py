from __future__ import annotations

"""Port “tracing” (clean architecture).

Décrit l’API minimale attendue pour tracer des opérations (segments/sous-segments,
annotations). Implémentation concrète actuelle: fonctions de `src/xray_config.py`.
"""

from contextlib import AbstractContextManager
from typing import Protocol


class ITracer(Protocol):
    """Port de tracing (ex: AWS X-Ray)."""

    def begin_segment(self, name: str, *, trace_header: str | None = None) -> None: ...
    def end_segment(self) -> None: ...
    def subsegment(self, name: str) -> AbstractContextManager: ...
    def put_annotation(self, key: str, value: str) -> None: ...
