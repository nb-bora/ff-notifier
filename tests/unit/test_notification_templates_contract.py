from __future__ import annotations

from pathlib import Path

from jinja2 import Template

from domain.entities.notification_event import NotificationEvent


def _render(template_file: Path, *, event_dict: dict) -> str:
    raw = template_file.read_text(encoding="utf-8")
    tpl = Template(raw)
    event = NotificationEvent(**event_dict)
    return tpl.render(event=event)


def test_all_notification_templates_render_with_minimal_payloads():
    base = Path(__file__).resolve().parents[2] / "src" / "templates" / "notifications"

    # user.untreatable.parse_failed
    _render(
        base / "template-demande-intraitable-parse-impossible.html",
        event_dict={
            "schema_version": 1,
            "event_id": "e-1",
            "occurred_at": "2026-05-05T10:00:00Z",
            "service": "ff-ingestion",
            "environment": "dev",
            "category": "user_untreatable",
            "severity": "warning",
            "template_id": "user.untreatable.parse_failed",
            "failure_code": "PARSE_FAILED",
            "recipient": {"type": "user", "email": "user@example.com", "locale": "fr"},
            "context": {"sender": "user@example.com", "source_message_id": "<m@x>"},
            "variables": {"human_summary": "x", "missing_fields": [], "original_email": {"subject": "s"}},
        },
    )

    # user.untreatable.tier1_hard
    _render(
        base / "template-demande-intraitable-tier1-hard.html",
        event_dict={
            "schema_version": 1,
            "event_id": "e-2",
            "occurred_at": "2026-05-05T10:00:00Z",
            "service": "ff-intelligence-engine",
            "environment": "dev",
            "category": "user_untreatable",
            "severity": "warning",
            "template_id": "user.untreatable.tier1_hard",
            "failure_code": "T1_R3_CITY_DATE_REQUIRED",
            "recipient": {"type": "user", "email": "user@example.com", "locale": "fr"},
            "context": {"sender": "user@example.com", "source_message_id": "<m@x>"},
            "variables": {
                "missing_fields": [
                    {"code": "T1_R3_CITY_DATE_REQUIRED", "path": "x", "label": "lbl", "expected": "exp", "found": None, "fix_hint": "hint"}
                ],
                "non_blocking_rules": [],
                "support_contact": None,
            },
        },
    )

    # user.untreatable.poison_message
    _render(
        base / "template-demande-intraitable-poison-message.html",
        event_dict={
            "schema_version": 1,
            "event_id": "e-3",
            "occurred_at": "2026-05-05T10:00:00Z",
            "service": "ff-ingestion",
            "environment": "dev",
            "category": "user_untreatable",
            "severity": "warning",
            "template_id": "user.untreatable.poison_message",
            "failure_code": "POISON_MESSAGE",
            "recipient": {"type": "user", "email": "user@example.com", "locale": "fr"},
            "context": {"sender": "user@example.com"},
            "variables": {"human_summary": "x"},
        },
    )

    # support.server_error (requires error.class key)
    _render(
        base / "template-support-alerte-incident-serveur.html",
        event_dict={
            "schema_version": 1,
            "event_id": "e-4",
            "occurred_at": "2026-05-05T10:00:00Z",
            "service": "ff-ingestion",
            "environment": "dev",
            "category": "support_alert",
            "severity": "error",
            "template_id": "support.server_error",
            "failure_code": "OPENAI_UNAVAILABLE",
            "recipient": {"type": "support", "email": "support@example.com", "locale": "fr"},
            "context": {"receive_count": 3},
            "variables": {
                "human_summary": "x",
                "error": {"class": "X", "message": "msg", "file": "f", "line": 1, "function": "fn", "module": "m"},
                "source_artifact": {"queue_url": "q", "sqs_message_id": "id"},
            },
        },
    )

    # support.poison_message
    _render(
        base / "template-support-alerte-poison-message.html",
        event_dict={
            "schema_version": 1,
            "event_id": "e-5",
            "occurred_at": "2026-05-05T10:00:00Z",
            "service": "ff-ingestion",
            "environment": "dev",
            "category": "support_alert",
            "severity": "error",
            "template_id": "support.poison_message",
            "failure_code": "POISON_MESSAGE",
            "recipient": {"type": "support", "email": "support@example.com", "locale": "fr"},
            "context": {"receive_count": 4},
            "variables": {"human_summary": "x", "source_artifact": {"queue_url": "q", "sqs_message_id": "id"}},
        },
    )

    # support.missing_sender
    _render(
        base / "template-support-alerte-expediteur-introuvable.html",
        event_dict={
            "schema_version": 1,
            "event_id": "e-6",
            "occurred_at": "2026-05-05T10:00:00Z",
            "service": "ff-ingestion",
            "environment": "dev",
            "category": "support_alert",
            "severity": "error",
            "template_id": "support.missing_sender",
            "failure_code": "MISSING_SENDER",
            "recipient": {"type": "support", "email": "support@example.com", "locale": "fr"},
            "context": {},
            "variables": {"source_artifact": {"queue_url": "q", "sqs_message_id": "id"}},
        },
    )

