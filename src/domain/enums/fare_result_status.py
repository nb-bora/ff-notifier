from __future__ import annotations

"""Enums du domaine Notifier."""

from enum import Enum


class FareResultStatus(str, Enum):
    """Statuts canoniques d’un `FareResult`."""

    parsing_failed = "parsing_failed"
    validation_error = "validation_error"
    analysis_complete = "analysis_complete"
