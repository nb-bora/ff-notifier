"""Instrumentation AWS X-Ray pour Notifier.

Objectifs:
- **Optionnel**: si `ENABLE_XRAY=false` ⇒ toutes les fonctions deviennent no-op.
- **Robuste**: si `aws-xray-sdk` n’est pas installé ⇒ désactivation sans casser
  le service.
- **Propagation**: support d’un header `X-Amzn-Trace-Id` entrant (ex: SQS message
  attributes) pour relier les traces entre services.

Utilisation dans Notifier:
- `main.py` appelle `init_xray()` au démarrage.
- `sqs_consumer.py` utilise `begin_segment/end_segment/subsegment/put_annotation`.
"""

import logging
from contextlib import contextmanager, suppress
from typing import Any, Generator

from config import settings
from logger import logger

SERVICE_NAME = "ff-notifier"

_patch_all = None
_xray_recorder = None


def _ensure_xray() -> None:
    """Initialise paresseusement les objets X-Ray (import + singletons).

    - **Rôle / impact**: évite d’imposer la dépendance runtime; si le SDK manque,
      force `settings.enable_xray = False`.
    - **Utilisé par**: toutes les fonctions publiques de ce module.
    """
    global _patch_all, _xray_recorder
    if _xray_recorder is not None:
        return
    try:
        from aws_xray_sdk.core import patch_all as pa
        from aws_xray_sdk.core import xray_recorder as xr
    except ModuleNotFoundError:
        # Robustesse: si la dépendance n'est pas installée, on désactive X-Ray sans
        # impacter le workflow principal.
        logger.warning("aws-xray-sdk not installed; disabling X-Ray instrumentation")
        settings.enable_xray = False  # type: ignore[attr-defined]
        return

    _patch_all = pa
    _xray_recorder = xr


def _noop_capture(_name: str):
    def decorator(fn):
        return fn

    return decorator


@contextmanager
def subsegment(name: str) -> Generator[Any, None, None]:
    """Context manager de sous-segment X-Ray.

    - **Rôle / impact**: encapsuler une sous-opération (SES/S3/PDF) dans une trace.
    - **Utilisé par**: `SQSConsumer` + `ProcessFareResultUseCase`.
    - **Effets de bord**: envoi au daemon X-Ray si activé.
    """
    if getattr(settings, "enable_xray", False):
        _ensure_xray()
        with _xray_recorder.in_subsegment(name) as sub:
            yield sub
    else:
        yield None


def init_xray(extra_config: dict[str, Any] | None = None) -> None:
    """Configure l’enregistreur X-Ray (async context + patching libs).

    - **Utilise**: `aws_xray_sdk.core.async_context.AsyncContext`, `patch_all()`.
    - **Utilisé par**: `main.py` au démarrage du service.
    - **Effets de bord**: instrumente certaines libs (boto3, etc.) si activé.
    """
    enable_xray = getattr(settings, "enable_xray", False)

    if not enable_xray:
        logger.debug("AWS X-Ray disabled for ff-notifier")
        return

    _ensure_xray()
    if _xray_recorder is None:
        return
    from aws_xray_sdk.core.async_context import AsyncContext

    config: dict[str, Any] = {"service": SERVICE_NAME, "context": AsyncContext()}
    config["context_missing"] = "IGNORE_ERROR"
    daemon_address = getattr(settings, "xray_daemon_address", None)
    if daemon_address:
        config["daemon_address"] = daemon_address
    if extra_config:
        config.update(extra_config)

    _xray_recorder.configure(**config)
    _patch_all()
    logging.getLogger("aws_xray_sdk").setLevel(logging.ERROR)
    logger.info("AWS X-Ray initialized for ff-notifier")


def xray_capture(segment_name: str):
    """Retourne un décorateur `capture` si X-Ray est activé, sinon un no-op."""
    if getattr(settings, "enable_xray", False):
        _ensure_xray()
        return _xray_recorder.capture(segment_name)
    return _noop_capture(segment_name)


def _parse_trace_header(trace_header: str) -> tuple[str | None, str | None]:
    """Parse un header `X-Amzn-Trace-Id` (Root/Parent)."""
    trace_id = None
    parent_id = None
    for part in trace_header.split(";"):
        part = part.strip()
        if part.startswith("Root="):
            trace_id = part[5:]
        elif part.startswith("Parent="):
            parent_id = part[7:]
    return trace_id, parent_id


def begin_segment(name: str, trace_header: str | None = None) -> None:
    """Démarre un segment X-Ray, avec propagation optionnelle.

    - **Utilisé par**: `SQSConsumer._process_message_inner`.
    - **Effets de bord**: crée un segment X-Ray si activé.
    """
    if getattr(settings, "enable_xray", False):
        _ensure_xray()
        if _xray_recorder is None:
            return
        traceid, parent_id = None, None
        if trace_header:
            traceid, parent_id = _parse_trace_header(trace_header)
        _xray_recorder.begin_segment(name, traceid=traceid, parent_id=parent_id)


def end_segment() -> None:
    """Termine le segment courant (best-effort)."""
    if getattr(settings, "enable_xray", False) and _xray_recorder is not None:
        with suppress(Exception):
            _xray_recorder.end_segment()


def current_trace_header() -> str | None:
    """Construit un header Root=... pour propagation sortante (si segment actif)."""
    if not getattr(settings, "enable_xray", False) or _xray_recorder is None:
        return None
    _ensure_xray()
    with suppress(Exception):
        seg = _xray_recorder.current_segment()
        if seg is not None:
            return f"Root={seg.trace_id};Sampled=1"
    return None


def put_annotation(key: str, value: str) -> None:
    """Ajoute une annotation au segment courant (best-effort)."""
    if not getattr(settings, "enable_xray", False) or _xray_recorder is None:
        return
    _ensure_xray()
    with suppress(Exception):
        seg = _xray_recorder.current_segment()
        if seg is not None:
            seg.put_annotation(key, value)
