from __future__ import annotations

"""Helpers partagés Notifier (HTML emails + petits mappings).

Ce module contient des fonctions “pures” (sans I/O) utilisées par:
- `ProcessFareResultUseCase` (construction du body HTML succès, nom utilisateur)
- `SQSConsumer` (wrappers de compat pour les mêmes helpers)

But:
- garder de petites fonctions réutilisables, faciles à tester
- éviter de dupliquer du HTML/formatage dans les adaptateurs
"""

import html
import datetime as _dt
from pathlib import Path
from typing import Optional

from jinja2 import Template

AUDIT_REPORT_LAYOUT = "executive"


def get_status_color(status: str) -> str:
    """Mappe un status de résultat vers une couleur HTML (hex).

    - **Utilisé par**: email d’erreur (consumer legacy) et compat.
    - **Effets de bord**: aucun.
    """
    if status == "analysis_complete":
        return "#2ecc71"
    if status == "parsing_failed" or status == "validation_error":
        return "#e74c3c"
    return "#3498db"


def extract_user_name(recipient_email: str, metadata: Optional[dict] = None) -> str:
    """Déduit un nom d’affichage pour l’email.

    Priorité:
    1) `metadata["name"]` si présent
    2) partie locale de l’email (`john.doe` → `John Doe`)
    3) fallback `"there"`
    """
    if metadata and metadata.get("name"):
        return metadata.get("name")
    if recipient_email:
        return recipient_email.split("@")[0].replace(".", " ").title()
    return "there"


def greeting_word(now: _dt.datetime | None = None) -> str:
    """Retourne 'Bonjour' en journée, 'Bonsoir' en soirée (heure locale)."""
    t = now or _dt.datetime.now()
    title = "M./Mme/Mlle"
    return f"Bonsoir, {title}" if t.hour >= 18 else f"Bonjour, {title}"


def success_email_body_html(
    *,
    user_name: str,
    ref_short: str,
    fare_event_id: str,
    original_subject: str,
    s3_uri: Optional[str],
) -> str:
    """Construit le body HTML de l’email “succès” (audit PDF en PJ).

    - **Utilise**: `html.escape` pour éviter l’injection HTML.
    - **Utilisé par**: `ProcessFareResultUseCase._send_success_email`.
    - **Effets de bord**: aucun (pur).
    """
    subj = html.escape((original_subject or "").strip() or "your request")
    un = html.escape(user_name)
    rs = html.escape(ref_short)
    fe = html.escape(fare_event_id)
    # Intentionally not surfaced in the HTML yet (keeps current output stable),
    # but referenced to avoid unused-parameter warnings.
    _unused_s3_uri = s3_uri
    archive_html = ""
    return _render_email_template(
        "emails/success_email.html",
        greeting_word=greeting_word(),
        user_name=un,
        subject=subj,
        ref_short=rs,
        fare_event_id=fe,
        archive_html=archive_html,
    )


_EMAIL_TEMPLATE_CACHE: dict[str, Template] = {}
_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates"


def _render_email_template(relative_path: str, **ctx: object) -> str:
    """Rend un template HTML email depuis `src/templates/` (cache en mémoire)."""
    tpl = _EMAIL_TEMPLATE_CACHE.get(relative_path)
    if tpl is None:
        raw = (_TEMPLATE_DIR / relative_path).read_text(encoding="utf-8")
        tpl = Template(raw)
        _EMAIL_TEMPLATE_CACHE[relative_path] = tpl
    return tpl.render(**ctx)
