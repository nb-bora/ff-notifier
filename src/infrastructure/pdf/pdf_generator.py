from __future__ import annotations

"""Génération du rapport d’audit (HTML + PDF) pour Notifier.

Ce module transforme un `fare_result_dict` (principalement `metadata`) en:
- **HTML** (template Jinja2: `src/templates/audit_executive.html`)
- **PDF** à partir de l’HTML:
  - primaire: Playwright/Chromium (rendu très fidèle)
  - fallback: WeasyPrint (plus léger, dépend de libs système)
- **QR code** embarqué dans le PDF (data URI) contenant une synthèse textuelle
  de l’audit (format “executive procurement audit”).

Points importants:
- La majorité des fonctions sont **pures** (formatage / mapping / sélection) et
  sont utilisées uniquement par `generate_audit_report_html_executive`.
- Des effets de bord existent au chargement du module (lecture du logo SVG) et
  lors du rendu PDF (chromium/headless ou weasyprint).
"""

import base64
import contextlib
import io
import re
import string
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import qrcode
from jinja2 import Template

from config import settings
from logger import logger

_EXECUTIVE_TEMPLATE: Optional[Template] = None


def _get_executive_template() -> Template:
    """Retourne le template Jinja2 compilé (cache en mémoire).

    - **Rôle / impact**: évite de relire/recompiler `audit_executive.html` à chaque message.
    - **Comportement**: identique (même fichier, même rendu), mais plus rapide.
    """
    global _EXECUTIVE_TEMPLATE
    if _EXECUTIVE_TEMPLATE is None:
        tpl_path = _TEMPLATE_DIR / "audit_executive.html"
        _EXECUTIVE_TEMPLATE = Template(
            tpl_path.read_text(encoding="utf-8"),
            autoescape=True,
        )
    return _EXECUTIVE_TEMPLATE

def create_qr_code(data: str) -> bytes:
    """Construit un QR code PNG (bytes) à partir d’un texte.

    - **Utilise**: `qrcode.QRCode` + PIL (via `make_image`).
    - **Utilisé par**: `qr_code_to_base64`.
    - **Effets de bord**: CPU/mémoire (pas d’I/O).
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img_byte_arr = io.BytesIO()
    img.save(img_byte_arr, format="PNG")
    img_byte_arr.seek(0)
    return img_byte_arr.getvalue()


def qr_code_to_base64(data: str) -> str:
    """Encode un QR code en base64 (pour un `data:image/png;base64,...`)."""
    qr_bytes = create_qr_code(data)
    return base64.b64encode(qr_bytes).decode("utf-8")


def _render_pdf_playwright(html_content: str) -> bytes:
    """Rend un PDF via Playwright/Chromium à partir d’un HTML complet.

    - **Rôle / impact**: produire un rendu A4 très proche d’un navigateur.
    - **Utilise**: `playwright.async_api` + fichier HTML temporaire (sur disque).
    - **Utilisé par**: `render_audit_pdf` (chemin primaire).
    - **Effets de bord**: lance un navigateur headless, écrit/supprime un fichier temp.
    """
    import asyncio

    from playwright.async_api import async_playwright

    async def _run() -> bytes:
        def _write_tmp_html(content: str) -> str:
            # Run in a thread to avoid blocking the event loop.
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".html", encoding="utf-8", delete=False
            ) as f:
                f.write(content)
                return f.name

        browser = None
        html_path = None
        try:
            launch_args: list[str] = []
            if sys.platform.startswith("linux"):
                launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=launch_args)
                page = await browser.new_page(viewport={"width": 794, "height": 1122})

                if getattr(settings, "pdf_playwright_use_set_content", False):
                    await page.set_content(html_content, wait_until="load")
                    html_path = None
                else:
                    html_path = await asyncio.to_thread(_write_tmp_html, html_content)

                await page.emulate_media(media="print")
                if html_path is not None:
                    await page.goto(Path(html_path).as_uri(), wait_until="load")
                await page.evaluate("() => document.fonts && document.fonts.ready")
                return await page.pdf(
                    format="A4",
                    prefer_css_page_size=True,
                    scale=1,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                    print_background=True,
                )
        finally:
            if browser is not None:
                await browser.close()
            if html_path is not None:
                with contextlib.suppress(FileNotFoundError):
                    Path(html_path).unlink()

    return asyncio.run(_run())


def _render_pdf_weasyprint(
    html_content: str, *, base_url: Optional[str] = None
) -> bytes:
    """Rend un PDF via WeasyPrint (fallback).

    - **Utilise**: `weasyprint.HTML(...).write_pdf()`.
    - **Utilisé par**: `render_audit_pdf` si Playwright échoue.
    - **Effets de bord**: CPU/IO interne WeasyPrint; dépend de libs système (pango).
    """
    from weasyprint import HTML

    if base_url is not None:
        return HTML(string=html_content, base_url=base_url).write_pdf()
    return HTML(string=html_content).write_pdf()


_MONTHS_EN = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]

_SIG_COLOR = {"VERT": "#1a7a3c", "JAUNE": "#cc8800", "ROUGE": "#c0392b"}
_SIG_CLASS = {"VERT": "sig-g", "JAUNE": "sig-o", "ROUGE": "sig-r"}
_FEE_APPLIES = "Fee applies"
_MODERATE_FLEX = "Moderate flexibility"


def _exec_na(v: object, default: str = "N/A") -> str:
    """Normalise une valeur possiblement vide vers un défaut."""
    return str(v) if v not in (None, "", []) else default


def _exec_fare_family(signals: list[str], fd: dict) -> str:
    """Déduit une famille tarifaire (direct/1-stop/cabin)."""
    if "DIRECT_FLIGHT" in signals:
        return "Direct Economy"
    if "ONE_STOP_FLIGHT" in signals:
        return "Economy (1-Stop)"
    return (fd.get("cabin") or "Economy").capitalize()


def _exec_changeable_label(fd: dict, t2: dict) -> str:
    """Déduit un label de changeabilité (free/fee/no) pour une offre."""
    if fd.get("changes_free"):
        return "Yes (Free)"
    reason = ((t2.get("change_rule") or {}).get("reason") or "").lower()
    if "fee" in reason:
        return _FEE_APPLIES
    return "No"


def _exec_airline_line(carriers: list) -> str:
    """Retourne une ligne compagnie lisible."""
    return ", ".join(carriers) if carriers else "N/A"


def _exec_outbound_label(fd: dict) -> str:
    """Construit la cellule “outbound” (HH:MM – HH:MM) ou N/A."""
    dep_t = _fmt_time(fd.get("departure_at") or "")
    arr_t = _fmt_time(fd.get("arrival_at") or "")
    return f"{dep_t} – {arr_t}" if dep_t else "N/A"


def _exec_parse_inputs(fare_result_dict: dict) -> tuple[dict, str, dict]:
    """Extrait (meta, fare_id, travel) depuis le dict sérialisé."""
    meta = fare_result_dict.get("metadata") or {}
    fare_id = fare_result_dict.get("fare_event_id", "UNKNOWN")
    travel = meta.get("extracted_travel") or {}
    return meta, str(fare_id), travel


def _exec_route_meta(
    origin: str,
    destination: str,
    trip_type: str,
    dep_fmt: str,
    ret_fmt: str,
) -> tuple[str, str]:
    """Construit (route_title, travel_date_meta) à afficher sur le rapport."""
    if trip_type == "round_trip" and ret_fmt:
        return f"{origin} → {destination} → {origin}", f"{dep_fmt} – {ret_fmt}"
    return f"{origin} → {destination}", dep_fmt


def _exec_build_travel_context(*, meta: dict, fare_id: str, travel: dict) -> dict:
    origin = travel.get("origin", "N/A")
    destination = travel.get("destination", "N/A")
    trip_type = travel.get("trip_type", "one_way")
    cabin = (travel.get("cabin_class") or "Economy").capitalize()
    dep_fmt = _fmt_date(travel.get("departure_date", ""))
    ret_raw = travel.get("return_date", "")
    ret_fmt = _fmt_date(ret_raw) if ret_raw else ""
    adults = int(travel.get("passengers_adults") or 1)
    children = int(travel.get("passengers_children") or 0)

    passenger = _extract_sender_name(meta.get("sender", ""))
    audit_id = f"FFA-{fare_id[:8].upper()}"
    route_title, travel_date_meta = _exec_route_meta(origin, destination, trip_type, dep_fmt, ret_fmt)
    pax_str = f"{adults} Adult{'s' if adults > 1 else ''}"
    if children:
        pax_str += f", {children} Child{'ren' if children > 1 else ''}"

    return {
        "origin": origin,
        "destination": destination,
        "trip_type": trip_type,
        "cabin": cabin,
        "dep_fmt": dep_fmt,
        "ret_fmt": ret_fmt,
        "passenger": passenger,
        "audit_id": audit_id,
        "route_title": route_title,
        "travel_date_meta": travel_date_meta,
        "pax_str": pax_str,
    }


def _exec_primary_airline(offers: List[dict]) -> str:
    c0 = (offers[0].get("flight_details") or {}).get("carriers") if offers else None
    if c0:
        return (c0 or ["—"])[0]
    return "—"


def _exec_sector_meta(*, offers: List[dict], tctx: dict, primary_airline: str) -> str:
    fd0 = (offers[0].get("flight_details") or {}) if offers else {}
    o0 = tctx["origin"] if tctx["origin"] != "N/A" else fd0.get("origin") or "—"
    d0 = tctx["destination"] if tctx["destination"] != "N/A" else fd0.get("destination") or "—"
    return _format_sector_meta(offers[0] if offers else None, primary_airline, o0, d0)


def _exec_recommendation_context(*, offers: List[dict], labels: List[str]) -> dict:
    rec = offers[0] if offers else None
    rec_title = f"Why Offer {labels[0]}" if labels else "No recommendation"
    rec_bullets = _rec_reasons(rec) if rec else ["No qualifying offer found."]
    rec_sig = (rec.get("ovi_signal") or "ROUGE").upper() if rec else "ROUGE"
    return {
        "rec_title": rec_title,
        "rec_bullets": rec_bullets,
        "policy_status": _executive_policy_line(rec_sig),
        "flexibility_status": _executive_flex_line(rec) if rec else "—",
    }


def _exec_prices(offers: List[dict]) -> List[float]:
    out: List[float] = []
    for o in offers:
        fd = o.get("flight_details") or {}
        try:
            out.append(float(fd.get("price") or 0))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def _exec_exec_compare_sub(n: int) -> str:
    if n == 0:
        return "No shortlisted offers for comparison."
    if n == 3:
        return "High-level procurement view across the three shortlisted options."
    opt_word = "option" if n == 1 else "options"
    return f"High-level procurement view across {n} shortlisted {opt_word}."


def _exec_page2_subtitle(*, route_title: str, primary_airline: str, labels: List[str]) -> str:
    return f"{route_title} • {primary_airline} • Reviewed options {_options_range_letters(labels)}"


def _exec_selected_letter(labels: List[str]) -> str:
    return labels[0] if labels else "—"


def _exec_build_offer_and_comparison_context(*, meta: dict, tctx: dict) -> dict:
    offers = _select_top3(meta.get("top_offers") or [])
    labels = list(string.ascii_uppercase[: len(offers)])
    det_data = build_det_data_for_offers(offers)
    n = len(offers)

    primary_airline = _exec_primary_airline(offers)
    route_sub = f"{tctx['cabin']} cabin • {tctx['pax_str']} • {primary_airline} • Route review"
    sector_meta = _exec_sector_meta(offers=offers, tctx=tctx, primary_airline=primary_airline)
    rec_ctx = _exec_recommendation_context(offers=offers, labels=labels)
    prices = _exec_prices(offers)

    offer_heads = [f"Offer {lb}" for lb in labels] if labels else ["—"]
    offer_heads_det = list(offer_heads)
    pax_chip = _pax_chip_short(tctx["pax_str"])

    offer_cards = _exec_build_offer_cards(
        labels=labels,
        offers=offers,
        prices=prices,
        n=n,
        cabin=tctx["cabin"],
        pax_chip=pax_chip,
        origin=tctx["origin"],
        destination=tctx["destination"],
        route_title=tctx["route_title"],
        travel_date_meta=tctx["travel_date_meta"],
    )
    quick_rows = _exec_build_quick_rows(det_data=det_data, offers=offers, prices=prices, n=n)
    detail_blocks = _exec_build_detail_blocks(
        offers=offers,
        det_data=det_data,
        prices=prices,
        n=n,
        route_title=tctx["route_title"],
        dep_fmt=tctx["dep_fmt"],
        cabin=tctx["cabin"],
        ret_fmt=tctx["ret_fmt"],
    )

    return {
        "offers": offers,
        "labels": labels,
        "det_data": det_data,
        "n": n,
        "primary_airline": primary_airline,
        "route_sub": route_sub,
        "sector_meta": sector_meta,
        "prices": prices,
        "offer_heads": offer_heads,
        "offer_heads_det": offer_heads_det,
        "offer_cards": offer_cards,
        "quick_rows": quick_rows,
        "detail_blocks": detail_blocks,
        "exec_compare_sub": _exec_exec_compare_sub(n),
        "page2_subtitle": _exec_page2_subtitle(
            route_title=tctx["route_title"],
            primary_airline=primary_airline,
            labels=labels,
        ),
        "selected_letter": _exec_selected_letter(labels),
        **rec_ctx,
    }


def _exec_qr_args(*, fare_id: str, tctx: dict, octx: dict) -> ExecutiveQrPayloadArgs:
    return ExecutiveQrPayloadArgs(
        fare_event_id=fare_id,
        audit_id=tctx["audit_id"],
        route_title=tctx["route_title"],
        route_sub=octx["route_sub"],
        travel_date_meta=tctx["travel_date_meta"],
        passenger_meta=tctx["passenger"],
        sector_meta=octx["sector_meta"],
        cabin_meta=tctx["cabin"],
        rec_title=octx["rec_title"],
        rec_bullets=octx["rec_bullets"],
        policy_status=octx["policy_status"],
        flexibility_status=octx["flexibility_status"],
        offer_cards=octx["offer_cards"],
        offer_heads=octx["offer_heads"],
        quick_rows=octx["quick_rows"],
        detail_blocks=octx["detail_blocks"],
        selected_letter=octx["selected_letter"],
    )


def _exec_qr_data_uri(*, qr_plain: str, fare_id: str, audit_id: str, route_title: str, n: int) -> str:
    try:
        return "data:image/png;base64," + qr_code_to_base64(qr_plain)
    except ValueError as exc:
        logger.warning(
            "Executive QR payload too large for QR matrix (%s); using minimal fallback",
            exc,
        )
        fallback = _trim_executive_plain_qr_text(
            "\n".join(
                [
                    "FAIRFARE — EXECUTIVE PROCUREMENT AUDIT",
                    "",
                    f"Audit number: {audit_id}",
                    f"Trip reference: {fare_id}",
                    f"Route: {route_title}",
                    f"Options reviewed: {n}",
                ]
            ),
            EXECUTIVE_QR_MAX_UTF8_BYTES,
        )
        return "data:image/png;base64," + qr_code_to_base64(fallback)


def _fmt_date(iso: str) -> str:
    """Formate une date ISO (`YYYY-MM-DD...`) en libellé anglais lisible."""
    try:
        d = datetime.fromisoformat(iso[:10])
        return f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year}"
    except Exception:
        return iso


def _fmt_datetime(iso: str) -> str:
    """Formate un datetime ISO en `D Month YYYY – HH:MM UTC` (best-effort)."""
    try:
        d = datetime.fromisoformat(iso)
        return f"{d.day} {_MONTHS_EN[d.month - 1]} {d.year} – {d.hour:02d}:{d.minute:02d} UTC"
    except Exception:
        return iso


def _fmt_time(iso: str) -> str:
    """Extrait `HH:MM` depuis un datetime ISO (best-effort)."""
    try:
        d = datetime.fromisoformat(iso)
        return f"{d.hour:02d}:{d.minute:02d}"
    except Exception:
        return ""


def _fmt_duration(pt: str) -> str:
    """Formate une durée type ISO-8601 `PT#H#M` en `xhmm` (best-effort)."""
    try:
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(pt))
        if m:
            h, mn = int(m.group(1) or 0), int(m.group(2) or 0)
            if h and mn:
                return f"{h}h{mn:02d}"
            if h:
                return f"{h}h00"
            return f"{mn}min"
    except Exception:
        return str(pt)
    return str(pt)


def _extract_sender_name(sender: str) -> str:
    """Normalise un expéditeur email `Name <x@y>` vers un nom d’affichage."""
    try:
        name = sender.split("<")[0].strip().strip('"').strip()
        return f"Mr. {name}" if name else sender
    except Exception:
        return sender


def _compliance_html(signal: str) -> str:
    """Retourne un snippet HTML indiquant la conformité (Full/Partial/Non-compliant)."""
    s = (signal or "").upper()
    if s == "VERT":
        return '<span class="c-full">&#10003; Full</span>'
    if s == "JAUNE":
        return '<span class="c-part">&#9888; Partial</span>'
    return '<span style="color:#c0392b; font-weight:600;">&#10007; Non-Compliant</span>'


def _compliance_label(signal: str) -> str:
    """Retourne un label court de conformité (Full/Partial/Non-Compliant)."""
    s = (signal or "").upper()
    if s == "VERT":
        return "Full"
    if s == "JAUNE":
        return "Partial"
    return "Non-Compliant"


def _routing(stops) -> str:
    """Formate le nombre d’escales en libellé (Direct/1 Stop/n Stops)."""
    n = int(stops or 0)
    if n == 0:
        return "Direct"
    if n == 1:
        return "1 Stop"
    return f"{n} Stops"


def _risk_badge(offer: dict) -> tuple:
    """Mappe le score OVI en badge de risque (LOW/MODERATE/ELEVATED + style)."""
    if not offer:
        return "N/A", ""
    ovi = float(offer.get("ovi_normalized") or 0)
    if ovi >= 0.7:
        return (
            "LOW",
            'style="display:inline-block; background:#d4f1e0; color:#1a5c32;'
            " font-weight:700; font-size:10px; padding:1px 8px; border-radius:3px;"
            ' letter-spacing:1px; margin-left:4px;"',
        )
    if ovi >= 0.5:
        return (
            "MODERATE",
            'style="display:inline-block; background:#fff3cd; color:#856404;'
            " font-weight:700; font-size:10px; padding:1px 8px; border-radius:3px;"
            ' letter-spacing:1px; margin-left:4px;"',
        )
    return (
        "ELEVATED",
        'style="display:inline-block; background:#fde8e8; color:#c0392b;'
        " font-weight:700; font-size:10px; padding:1px 8px; border-radius:3px;"
        ' letter-spacing:1px; margin-left:4px;"',
    )


def _observation(offer: dict) -> str:
    """Construit une observation texte synthétique pour une offre."""
    if not offer:
        return "N/A"
    fd = offer.get("flight_details") or {}
    stops = int(fd.get("stops") or 0)
    t2 = offer.get("tier2") or {}
    signals = [s.upper() for s in (offer.get("signals") or [])]

    parts = ["Direct" if stops == 0 else f"{stops}-stop"]
    if "POLICY_ADJUSTMENT" in signals:
        parts.append("policy-compliant")
    flex = (t2.get("fare_flexibility") or {}).get("reason") or ""
    if "partial" in flex.lower():
        parts.append("partial refund only")
    elif "full" in flex.lower():
        parts.append("fully refundable")
    chg = (t2.get("change_rule") or {}).get("reason") or ""
    if "fee" in chg.lower():
        parts.append("change fee applies")
    return "; ".join(parts)


def _rec_reasons(offer: dict) -> list:
    """Construit une liste de raisons de recommandation à partir des signaux/scoring."""
    if not offer:
        return ["Best balanced score across all criteria"]
    signals = [s.upper() for s in (offer.get("signals") or [])]
    t2 = offer.get("tier2") or {}
    reasons = []
    if "DIRECT_FLIGHT" in signals:
        reasons.append("Direct routing – minimal operational disruption exposure")
    elif "ONE_STOP_FLIGHT" in signals:
        reasons.append("Single-connection itinerary – best available routing")
    if "POLICY_ADJUSTMENT" in signals:
        reasons.append("Fare is aligned with corporate travel policy guidelines")
    if float(offer.get("ovi_normalized") or 0) >= 0.7:
        reasons.append("Highest overall quality score among all analyzed offers")
    t2_score = float(t2.get("tier2_score") or 0)
    if t2_score >= 0.7:
        reasons.append("Favorable fare flexibility and change conditions")
    return reasons or ["Best balanced score across all evaluation criteria"]


def _select_top3(top_offers: list) -> list:
    """Sélectionne jusqu’à 3 offres (tri par qualité puis prix, dédoublonnage simple)."""
    if not top_offers:
        return []

    def sort_key(o):
        fd = o.get("flight_details") or {}
        ovi = float(o.get("ovi_normalized") or 0)
        price = float(fd.get("price") or 999_999)
        return (-ovi, price)

    seen: set = set()
    result = []
    for offer in sorted(top_offers, key=sort_key):
        fd = offer.get("flight_details") or {}
        carriers = fd.get("carriers") or []
        key = (carriers[0] if carriers else "??", int(fd.get("stops") or 0))
        if key not in seen:
            seen.add(key)
            result.append(offer)
        if len(result) >= 3:
            break
    return result


def _fmt_yesno(val) -> str:
    """Normalise un bool/valeur vers `Yes`/`No`/`N/A`."""
    if val is True:
        return "Yes"
    if val is False:
        return "No"
    return str(val) if val is not None else "N/A"


def _currency_display(code: Optional[str]) -> str:
    """Retourne un symbole monétaire simplifié (ex: EUR -> €) ou le code brut."""
    if not code:
        return ""
    c = str(code).strip().upper()
    if c == "EUR":
        return "€"
    return str(code).strip()


def _format_price_display(
    currency_code: Optional[str],
    price: Any,
    *,
    missing_price: str = "N/A",
) -> str:
    """Formate un prix + devise en affichage court (ex: `€ 123.45`)."""
    sym = _currency_display(currency_code)
    p = missing_price if price is None or price == "" else str(price).strip()

    if sym:
        return f"{sym} {p}"
    return p


def build_det_data_for_offers(offers: List[dict]) -> List[dict]:
    """Construit la structure “detail data” pour les offres (utilisée par le template).

    Fonction pure: ne fait que transformer la liste d’offres en dicts normalisés.
    """

    det_data: List[dict] = []
    for offer in offers:
        fd = offer.get("flight_details") or {}
        sig = (offer.get("ovi_signal") or "ROUGE").upper()
        signals = [s.upper() for s in (offer.get("signals") or [])]
        t2 = offer.get("tier2") or {}
        carriers = fd.get("carriers") or []

        fare_family = _exec_fare_family(signals, fd)
        changeable = _exec_changeable_label(fd, t2)
        outbound = _exec_outbound_label(fd)

        det_data.append(
            {
                "airline": _exec_airline_line(carriers),
                "price": _format_price_display(fd.get("currency"), fd.get("price")),
                "fare_family": fare_family,
                "refundable": _fmt_yesno(fd.get("refundable")),
                "changeable": changeable,
                "no_show": "N/A",
                "baggage": _exec_na(fd.get("baggage_allowance"), "N/A"),
                "outbound": outbound,
                "stops_out": _routing(fd.get("stops", 0)),
                "duration_out": _fmt_duration(fd.get("duration") or ""),
                "return_seg": "N/A",
                "stops_ret": "N/A",
                "duration_ret": "N/A",
                "compliance": _compliance_label(sig),
                "risk": _risk_badge(offer)[0],
                "dot_color": _SIG_COLOR.get(sig, "#888"),
                "dep_at": _exec_na(fd.get("departure_at"), ""),
                "arr_at": _exec_na(fd.get("arrival_at"), ""),
            }
        )
    return det_data


def _executive_policy_line(signal: str) -> str:
    """Normalise un signal VERT/JAUNE/ROUGE vers un libellé procurement."""
    s = (signal or "").upper()
    if s == "VERT":
        return "Full compliance"
    if s == "JAUNE":
        return "Partial compliance"
    return "Non-compliant"


def _executive_flex_line(offer: dict) -> str:
    """Retourne une ligne courte sur la flexibilité/remboursabilité d’une offre."""
    if not offer:
        return "—"
    fd = offer.get("flight_details") or {}
    t2 = offer.get("tier2") or {}
    flex = ((t2.get("fare_flexibility") or {}).get("reason") or "").strip()
    if flex:
        return flex[:80] + ("..." if len(flex) > 80 else "")
    if fd.get("refundable"):
        return "Refundable fare"
    return "Restricted refund"


def _offer_flex_card_line(offer: dict) -> str:
    """Retourne une ligne courte pour la carte offre (refund/change)."""
    fd = offer.get("flight_details") or {}
    t2 = offer.get("tier2") or {}
    bits = []
    bits.append("Refundable" if fd.get("refundable") else "Non-refundable")
    chg = (t2.get("change_rule") or {}).get("reason") or ""
    if fd.get("changes_free"):
        bits.append("Free changes")
    elif "fee" in chg.lower():
        bits.append("Changes with fee")
    else:
        bits.append("Changes per fare rules")
    return " • ".join(bits)


def _tier2_field(t2: dict, *keys: str) -> str:
    """Accès sûr (best-effort) à un champ imbriqué `tier2`."""
    cur: object = t2
    for k in keys:
        if not isinstance(cur, dict):
            return "—"
        cur = cur.get(k)  # type: ignore[assignment]
    if cur is None or cur == "":
        return "—"
    return str(cur)


def _executive_footer_note(idx: int, prices: List[float]) -> str:
    """Retourne une note de bas de carte en fonction du rang/prix relatif."""
    if idx == 0:
        return "Balanced executive choice"
    if not prices or len(prices) < 2:
        return "Alternative"
    lo, hi = (
        min(range(len(prices)), key=lambda i: prices[i]),
        max(range(len(prices)), key=lambda i: prices[i]),
    )
    if idx == lo:
        return "Lower cost option"
    if idx == hi:
        return "Premium / flexibility option"
    return "Alternative"


def _executive_view_cell(idx: int, prices: List[float]) -> str:
    """Retourne un libellé “executive view” par colonne (Recommended/Lower cost/...)."""
    if idx == 0:
        return "Recommended"
    if not prices or len(prices) < 2:
        return "Alternative"
    lo = min(range(len(prices)), key=lambda i: prices[i])
    hi = max(range(len(prices)), key=lambda i: prices[i])
    if idx == lo:
        return "Lower cost"
    if idx == hi:
        return "Higher price band"
    return "Alternative"


def _options_range_letters(labels: List[str]) -> str:
    """Retourne une plage de lettres (A–C) pour l’entête page 2."""
    if not labels:
        return "—"
    if len(labels) == 1:
        return labels[0]
    return f"{labels[0]}–{labels[-1]}"


def _format_sector_meta(
    lead_offer: Optional[dict], primary_airline: str, o0: str, d0: str
) -> str:
    """Construit la meta “sector” (compagnie/vol/route) à afficher dans le rapport."""
    if primary_airline in ("—", "N/A", ""):
        return f"{o0} → {d0}"
    if not lead_offer:
        return f"{primary_airline} • {o0} → {d0}"
    fd = lead_offer.get("flight_details") or {}
    fn = (
        fd.get("flight_number")
        or fd.get("flight_no")
        or fd.get("marketing_flight")
        or fd.get("flight")
    )
    if fn:
        return f"{primary_airline} {fn} • {o0} → {d0}"
    return f"{primary_airline} • {o0} → {d0}"


def _pax_chip_short(pax_str: str) -> str:
    """Réduit une description passagers en un chip court."""
    return pax_str.split(",")[0].strip() if pax_str else "1 Adult"


def _offer_ranking_chip(
    idx: int, prices: List[float], n_offers: int, pax_short: str
) -> str:
    """Retourne un chip de ranking (Lowest cost/More flexible/Alternative)."""
    if n_offers <= 1 or idx == 0:
        return pax_short
    if len(prices) != n_offers:
        return pax_short
    lo_i = min(range(n_offers), key=lambda i: prices[i])
    hi_i = max(range(n_offers), key=lambda i: prices[i])
    if idx == lo_i:
        return "Lowest cost"
    if idx == hi_i:
        return "More flexible"
    return "Alternative"


def _refundability_compare_cell(offer: dict) -> str:
    """Cellule comparaison remboursabilité (Yes/No/Restricted)."""
    fd = offer.get("flight_details") or {}
    t2 = offer.get("tier2") or {}
    if fd.get("refundable") is True:
        return "Yes"
    if fd.get("refundable") is False:
        return "No"
    flex = ((t2.get("fare_flexibility") or {}).get("reason") or "").lower()
    if "partial" in flex or "restricted" in flex:
        return "Restricted"
    return "No"


def _refundability_long_for_offer(offer: dict) -> str:
    """Texte long remboursabilité pour la section “Commercial conditions”."""
    fd = offer.get("flight_details") or {}
    t2 = offer.get("tier2") or {}
    if fd.get("refundable") is True:
        return "Refundable before departure per fare rules"
    if fd.get("refundable") is False:
        return "Not refundable before departure"
    flex = ((t2.get("fare_flexibility") or {}).get("reason") or "").lower()
    if "partial" in flex or "restricted" in flex:
        return "Restricted refund conditions"
    return "See fare rules"


def _change_rule_verbose(offer: dict, variant: str) -> str:
    """Texte long de règle de changement (variant standard/lower/higher)."""
    t2 = offer.get("tier2") or {}
    fd = offer.get("flight_details") or {}
    raw = ((t2.get("change_rule") or {}).get("reason") or "").strip()
    if raw:
        return f"Allowed with fee ({raw})"
    if fd.get("changes_free"):
        return "Allowed; changes included"
    if variant == "higher":
        return "Allowed with higher fee exposure"
    if variant == "lower":
        return "Allowed with lower penalty"
    return "Allowed with fee per fare rules"


def _change_profile_quick_cell(
    idx: int, offers: List[dict], prices: List[float]
) -> str:
    """Cellule courte du profil de changement (fee/free/higher/lower exposure)."""
    n = len(offers)
    if n <= 1:
        return _change_rule_verbose(offers[0], "standard") if offers else "—"
    lo, hi = _exec_rank_indices(prices)
    if idx == lo and idx != 0:
        return "Higher fee exposure"
    if idx == hi and hi != lo:
        return "Lower penalty"
    if idx == 0:
        o = offers[idx]
        if _exec_offer_has_fee_reason(o):
            return _FEE_APPLIES
        if (o.get("flight_details") or {}).get("changes_free"):
            return "Free changes"
        return _FEE_APPLIES
    return _FEE_APPLIES


def _exec_rank_indices(ps: List[float]) -> tuple[int, int]:
    """Retourne indices (lo, hi) de prix min/max."""
    lo_i = min(range(len(ps)), key=lambda i: ps[i])
    hi_i = max(range(len(ps)), key=lambda i: ps[i])
    return lo_i, hi_i


def _exec_offer_has_fee_reason(offer: dict) -> bool:
    """Détecte une raison de changement contenant 'fee'."""
    t2 = offer.get("tier2") or {}
    return "fee" in ((t2.get("change_rule") or {}).get("reason") or "").lower()


def _compliance_partial_extended(idx: int, n: int, base: str) -> str:
    """Ajuste un label “Partial” pour certains cas (nuance procurement)."""
    if n >= 3 and idx == 1 and base == "Partial":
        return "Partial with weaker commercial position"
    return base


def _flexibility_assessment_exec(idx: int, n: int) -> str:
    """Retourne un label de flexibilité procurement pour la page 2."""
    if n >= 3:
        return (_MODERATE_FLEX, "Lower flexibility", "Higher flexibility")[min(idx, 2)]
    if n == 2:
        return (_MODERATE_FLEX, "Alternative flexibility profile")[min(idx, 1)]
    return _MODERATE_FLEX


def _operational_risk_exec(idx: int, n: int, det_data: List[dict]) -> str:
    """Retourne un label de risque opérationnel (Low/Moderate/Elevated)."""
    if n >= 3 and idx == 1:
        return "Moderate to elevated"
    if idx < len(det_data):
        r = (det_data[idx].get("risk") or "Moderate").lower()
        if "elevated" in r or "high" in r:
            return "Elevated"
        if "low" in r:
            return "Low"
        return "Moderate"
    return "Moderate"


def _procurement_narrative_cell(idx: int, n: int) -> str:
    """Retourne un narratif procurement synthétique (selon le rang)."""
    if n >= 3:
        texts = (
            "Best cost-control and approval balance among reviewed options.",
            "Lowest entry price, but less resilient if itinerary changes are required.",
            "Improved flexibility profile at a higher executive review price point.",
        )
        return texts[min(idx, 2)]
    if n == 2:
        return (
            "Strongest balance for typical approval workflows.",
            "Alternative worth considering if initial price drives the decision.",
        )[min(idx, 1)]
    return "Recommended on balance across reviewed criteria."


def _analyst_note_cell(idx: int, n: int) -> str:
    """Retourne une note analyste synthétique (selon le rang)."""
    if n >= 3:
        texts = (
            "Recommended on balance rather than on lowest price alone.",
            "Best suited only where lowest initial spend is the overriding criterion.",
            "Viable where policy can accommodate a premium for improved conditions.",
        )
        return texts[min(idx, 2)]
    if n == 2:
        return (
            "Preferred option unless price-only optimization applies.",
            "Consider if lowest spend outweighs flexibility needs.",
        )[min(idx, 1)]
    return "Align ticketing with this option where operationally practical."


def _cabin_fare_family_label(idx: int, n: int, base: str) -> str:
    """Retourne un label “Cabin / Fare family” adapté au contexte executive."""
    if n >= 3:
        return (
            "Economy • Standard reviewed fare",
            "Economy • Lower-cost fare",
            "Economy • Flex-oriented fare",
        )[min(idx, 2)]
    b = base or "Economy"
    return f"{b}" if "•" in b else f"{b} • Reviewed fare"


def _minimum_stay_cell(offer: dict, dep_fmt: str) -> str:
    """Retourne la cellule “minimum stay” (tier2 ou fallback)."""
    t2 = offer.get("tier2") or {}
    v = _tier2_field(t2, "minimum_stay")
    if v != "—":
        return v
    fd = offer.get("flight_details") or {}
    dep = (fd.get("departure_at") or "")[:10]
    if dep:
        try:
            return f"Travel must commence on/after {_fmt_date(dep)}"
        except Exception as e:
            logger.warning(f"Date formatting failed: {e}")
    if dep_fmt:
        return f"Travel must commence on/after {dep_fmt}"
    return "—"


def _maximum_stay_cell(offer: dict, ret_travel_fmt: str) -> str:
    """Retourne la cellule “maximum stay” (tier2 ou fallback)."""
    t2 = offer.get("tier2") or {}
    v = _tier2_field(t2, "maximum_stay")
    if v != "—":
        return v
    if ret_travel_fmt:
        return f"Travel must commence before {ret_travel_fmt}"
    return "—"


EXECUTIVE_QR_MAX_UTF8_BYTES = 2680


def _trim_executive_plain_qr_text(
    body: str, max_bytes: int = EXECUTIVE_QR_MAX_UTF8_BYTES
) -> str:
    """Tronque un texte QR pour respecter une limite UTF-8 en bytes."""
    def ulen(s: str) -> int:
        return len(s.encode("utf-8"))

    s = body.strip()
    if ulen(s) <= max_bytes:
        return s
    lines = s.split("\n")
    while lines and ulen("\n".join(lines)) > max_bytes:
        lines.pop()
    s = "\n".join(lines)
    while ulen(s) > max_bytes and s:
        s = s[:-1]
    return s.rstrip()


@dataclass(frozen=True)
class ExecutiveQrPayloadArgs:
    """Arguments normalisés pour construire le payload texte du QR executive."""
    fare_event_id: str
    audit_id: str
    route_title: str
    route_sub: str
    travel_date_meta: str
    passenger_meta: str
    sector_meta: str
    cabin_meta: str
    rec_title: str
    rec_bullets: List[str]
    policy_status: str
    flexibility_status: str
    offer_cards: List[dict]
    offer_heads: List[str]
    quick_rows: List[dict]
    detail_blocks: List[dict]
    selected_letter: str
    max_bytes: int = EXECUTIVE_QR_MAX_UTF8_BYTES


def _executive_plain_qr_payload(
    args: ExecutiveQrPayloadArgs,
) -> str:
    """Construit le texte “plain” encodé dans le QR (executive audit)."""
    lines: List[str] = []
    lines.append("FAIRFARE — EXECUTIVE PROCUREMENT AUDIT")
    lines.append("")
    _qr_append_meta(args, lines)
    lines.append("")
    _qr_append_recommendation(args, lines)
    lines.append("")
    _qr_append_offers(args, lines)
    lines.append("")
    _qr_append_quick_rows(args, lines)
    lines.append("")
    _qr_append_details(args, lines)

    raw = "\n".join(lines)
    trimmed = _trim_executive_plain_qr_text(raw, args.max_bytes)
    enc = trimmed.encode("utf-8")
    if len(enc) > 2953:
        logger.warning(
            "Executive QR text is %d UTF-8 bytes; may exceed QR version-40 capacity",
            len(enc),
        )
    return trimmed


def _qr_append_meta(args: ExecutiveQrPayloadArgs, lines: List[str]) -> None:
    lines.append(f"Audit number: {args.audit_id}")
    lines.append(f"Trip reference: {args.fare_event_id}")
    lines.append(f"Route: {args.route_title}")
    if (args.route_sub or "").strip():
        lines.append(f"Itinerary: {args.route_sub}")
    lines.append(f"Travel dates: {args.travel_date_meta}")
    lines.append(f"Passengers: {args.passenger_meta}")
    if (args.sector_meta or "").strip():
        lines.append(f"Sector: {args.sector_meta}")
    lines.append(f"Cabin: {args.cabin_meta}")


def _qr_append_recommendation(args: ExecutiveQrPayloadArgs, lines: List[str]) -> None:
    lines.append("RECOMMENDATION")
    lines.append(args.rec_title)
    for b in args.rec_bullets:
        lines.append(f"• {b}")
    lines.append(f"Policy alignment: {args.policy_status}")
    lines.append(f"Flexibility: {args.flexibility_status}")


def _qr_append_offers(args: ExecutiveQrPayloadArgs, lines: List[str]) -> None:
    lines.append("SHORTLISTED OPTIONS")
    for o in args.offer_cards:
        tag = str(o.get("badge_text") or ("Recommended" if o.get("recommended") else "Alternative"))
        lines.append(f"— Offer {o['letter']} ({tag}) — {o['airline_line']}")
        lines.append(
            f"  {o['dep_time']} → {o['arr_time']} · {o['origin_code']}–{o['dest_code']} · {o['price_display']}"
        )
        chips = o.get("chips") or []
        if chips:
            lines.append("  " + " · ".join(str(c) for c in chips))
        lines.append(f"  Fare conditions: {o['flex_line']}")
        rs = o.get("route_subline") or ""
        if rs:
            lines.append(f"  {rs}")


def _qr_append_quick_rows(args: ExecutiveQrPayloadArgs, lines: List[str]) -> None:
    lines.append("COMPARISON AT A GLANCE")
    for row in args.quick_rows:
        cells = row.get("cells") or []
        parts = [
            f"{args.offer_heads[i]}: {cells[i]}"
            for i in range(min(len(args.offer_heads), len(cells)))
        ]
        lines.append(f"{row['label']}: " + " | ".join(parts))


def _qr_append_details(args: ExecutiveQrPayloadArgs, lines: List[str]) -> None:
    lines.append(f"Preferred option on file: {args.selected_letter}")
    lines.append("")
    lines.append("DETAILED SUMMARY")
    for block in args.detail_blocks:
        lines.append(block["title"])
        for r in block["rows"]:
            cs = r.get("cells") or []
            parts = [
                f"{args.offer_heads[i]}: {cs[i]}"
                for i in range(min(len(args.offer_heads), len(cs)))
            ]
            lines.append(f"  {r['label']}: " + " | ".join(parts))


_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates"

def _data_uri_for_image(path: Path) -> str:
    """Retourne une data-URI base64 cohérente avec l'extension (png/svg)."""
    suf = path.suffix.lower()
    if suf == ".png":
        mime = "image/png"
    elif suf == ".svg":
        mime = "image/svg+xml"
    else:
        mime = "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


_LOGO_PNG_PATH = _TEMPLATE_DIR / "fairfare-logo-primary.png"
_LOGO_SVG_PATH = _TEMPLATE_DIR / "fairfare-logo-primary.svg"
_LOGO_DATA_URI: str = (
    _data_uri_for_image(_LOGO_PNG_PATH)
    if _LOGO_PNG_PATH.exists()
    else _data_uri_for_image(_LOGO_SVG_PATH)
)


def _exec_build_offer_cards(
    *,
    labels: List[str],
    offers: List[dict],
    prices: List[float],
    n: int,
    cabin: str,
    pax_chip: str,
    origin: str,
    destination: str,
    route_title: str,
    travel_date_meta: str,
) -> List[dict]:
    """Builder: cartes offres (page 1) pour le template executive."""
    cards: List[dict] = []
    for idx, (lbl, offer) in enumerate(zip(labels, offers)):
        cards.append(
            _exec_offer_card(
                idx=idx,
                lbl=lbl,
                offer=offer,
                prices=prices,
                n=n,
                cabin=cabin,
                pax_chip=pax_chip,
                origin=origin,
                destination=destination,
                route_title=route_title,
                travel_date_meta=travel_date_meta,
            )
        )
    return cards


def _exec_offer_card(
    *,
    idx: int,
    lbl: str,
    offer: dict,
    prices: List[float],
    n: int,
    cabin: str,
    pax_chip: str,
    origin: str,
    destination: str,
    route_title: str,
    travel_date_meta: str,
) -> dict:
    fd = offer.get("flight_details") or {}
    carriers = fd.get("carriers") or []
    airline_line = ", ".join(carriers) if carriers else "—"
    oc = fd.get("origin") or origin or "?"
    dc = fd.get("destination") or destination or "?"
    chips = [
        cabin,
        _routing(fd.get("stops", 0)),
        _offer_ranking_chip(idx, prices, n, pax_chip),
    ]
    route_subline = f"{route_title} • {travel_date_meta}"
    price_disp = _format_price_display(
        fd.get("currency"),
        fd.get("price"),
        missing_price="—",
    )
    return {
        "letter": lbl,
        "airline_line": airline_line,
        "chips": chips,
        "dep_time": _fmt_time(fd.get("departure_at") or "") or "—",
        "arr_time": _fmt_time(fd.get("arrival_at") or "") or "—",
        "origin_code": str(oc),
        "dest_code": str(dc),
        "route_subline": route_subline,
        "price_display": price_disp or "—",
        "flex_line": _offer_flex_card_line(offer),
        "footer_note": _executive_footer_note(idx, prices),
        "recommended": idx == 0,
        "badge_class": "selected" if idx == 0 else "alt",
        "badge_text": "Recommended" if idx == 0 else "Alternative",
    }


def _exec_build_quick_rows(
    *,
    det_data: List[dict],
    offers: List[dict],
    prices: List[float],
    n: int,
) -> List[dict]:
    """Builder: lignes de comparaison rapide (page 1)."""
    return [
        {"label": "Price", "cells": [d.get("price", "—") for d in det_data] or ["—"]},
        {
            "label": "Refundability",
            "cells": ([_refundability_compare_cell(offers[i]) for i in range(n)] if n else ["—"]),
        },
        {
            "label": "Change profile",
            "cells": ([_change_profile_quick_cell(i, offers, prices) for i in range(n)] if n else ["—"]),
        },
        {
            "label": "Compliance",
            "cells": (
                [
                    _compliance_partial_extended(i, n, det_data[i].get("compliance", "Partial"))
                    for i in range(n)
                ]
                if n
                else ["—"]
            ),
        },
        {
            "label": "Executive view",
            "cells": ([_executive_view_cell(i, prices) for i in range(len(offers))] or ["—"]),
        },
    ]


def _exec_change_departure_row_cells(
    *, offers: List[dict], prices: List[float]
) -> List[str]:
    """Builder: cellules ‘Change before/after departure’ (page 2)."""
    n = len(offers)
    if not n:
        return ["—"]
    lo = min(range(n), key=lambda i: prices[i])
    hi = max(range(n), key=lambda i: prices[i])
    out: List[str] = []
    for i in range(n):
        if n >= 3:
            if i == hi:
                v = "lower"
            elif i == lo and i != 0:
                v = "higher"
            else:
                v = "standard"
        else:
            v = "standard"
        out.append(_change_rule_verbose(offers[i], v))
    return out


def _exec_cells_key(det_data: List[dict], key: str) -> List[str]:
    """Helper: extrait une colonne depuis `det_data` avec fallback `—`."""
    return [d.get(key, "—") for d in det_data] if det_data else ["—"]


def _exec_build_detail_blocks(
    *,
    offers: List[dict],
    det_data: List[dict],
    prices: List[float],
    n: int,
    route_title: str,
    dep_fmt: str,
    cabin: str,
    ret_fmt: str,
) -> List[dict]:
    """Builder: blocs détaillés (page 2) pour le template executive."""
    change_row_cells = _exec_change_departure_row_cells(offers=offers, prices=prices)
    return [
        _exec_flight_details_block(
            offers=offers,
            det_data=det_data,
            n=n,
            route_title=route_title,
            dep_fmt=dep_fmt,
            cabin=cabin,
        ),
        _exec_commercial_conditions_block(
            offers=offers,
            det_data=det_data,
            n=n,
            dep_fmt=dep_fmt,
            ret_fmt=ret_fmt,
            change_row_cells=change_row_cells,
        ),
        _exec_procurement_assessment_block(det_data=det_data, n=n),
    ]


def _exec_flight_details_block(
    *, offers: List[dict], det_data: List[dict], n: int, route_title: str, dep_fmt: str, cabin: str
) -> dict:
    cells_key = lambda k: _exec_cells_key(det_data, k)
    return {
        "title": "Flight details",
        "rows": [
            {"label": "Airline / Flight", "cells": cells_key("airline")},
            {"label": "Route", "cells": [route_title] * n if n else ["—"]},
            {
                "label": "Departure",
                "cells": _exec_departure_cells(offers, n, dep_fmt),
            },
            {
                "label": "Arrival",
                "cells": _exec_arrival_cells(offers, n),
            },
            {"label": "Stops", "cells": cells_key("stops_out")},
            {
                "label": "Cabin / Fare family",
                "cells": _exec_cabin_cells(det_data, n, cabin),
            },
        ],
    }


def _exec_departure_cells(offers: List[dict], n: int, dep_fmt: str) -> List[str]:
    if not n:
        return ["—"]
    out: List[str] = []
    for i in range(n):
        t = _fmt_time((offers[i].get("flight_details") or {}).get("departure_at") or "")
        out.append(f"{dep_fmt}, {t}" if t else dep_fmt)
    return out


def _exec_arrival_cells(offers: List[dict], n: int) -> List[str]:
    if not n:
        return ["—"]
    out: List[str] = []
    for i in range(n):
        a = (offers[i].get("flight_details") or {}).get("arrival_at")
        if not a:
            out.append("—")
            continue
        out.append(f"{_fmt_date(str(a)[:10])}, {_fmt_time(str(a))}")
    return out


def _exec_cabin_cells(det_data: List[dict], n: int, cabin: str) -> List[str]:
    if not n:
        return ["—"]
    return [
        _cabin_fare_family_label(i, n, det_data[i].get("fare_family", cabin))
        for i in range(n)
    ]


def _exec_commercial_conditions_block(
    *,
    offers: List[dict],
    det_data: List[dict],
    n: int,
    dep_fmt: str,
    ret_fmt: str,
    change_row_cells: List[str],
) -> dict:
    cells_key = lambda k: _exec_cells_key(det_data, k)
    return {
        "title": "Commercial conditions",
        "rows": [
            {"label": "Price", "cells": cells_key("price")},
            {
                "label": "Refundability",
                "cells": ([_refundability_long_for_offer(offers[i]) for i in range(n)] if n else ["—"]),
            },
            {"label": "Change before departure", "cells": list(change_row_cells)},
            {"label": "Change after departure", "cells": list(change_row_cells)},
            {
                "label": "Minimum stay",
                "cells": ([_minimum_stay_cell(offers[i], dep_fmt) for i in range(n)] if n else ["—"]),
            },
            {
                "label": "Maximum stay",
                "cells": ([_maximum_stay_cell(offers[i], ret_fmt) for i in range(n)] if n else ["—"]),
            },
        ],
    }


def _exec_procurement_assessment_block(*, det_data: List[dict], n: int) -> dict:
    return {
        "title": "Procurement assessment",
        "rows": [
            {
                "label": "Policy compliance",
                "cells": [
                    _compliance_partial_extended(i, n, det_data[i].get("compliance", "Partial"))
                    for i in range(n)
                ]
                if n
                else ["—"],
            },
            {
                "label": "Flexibility assessment",
                "cells": ([_flexibility_assessment_exec(i, n) for i in range(n)] if n else ["—"]),
            },
            {
                "label": "Operational risk",
                "cells": ([_operational_risk_exec(i, n, det_data) for i in range(n)] if n else ["—"]),
            },
            {
                "label": "Procurement assessment",
                "cells": ([_procurement_narrative_cell(i, n) for i in range(n)] if n else ["—"]),
            },
            {
                "label": "Analyst note",
                "cells": ([_analyst_note_cell(i, n) for i in range(n)] if n else ["—"]),
            },
        ],
    }


def generate_audit_report_html_executive(fare_result_dict: dict) -> str:
    """Construit l’HTML du rapport “executive” (2 pages).

    - **Entrée**: `fare_result_dict` (dict sérialisé, issu typiquement de
      `FareResult.model_dump()` côté consumer).
    - **Données attendues**:
      - `fare_result_dict["metadata"]["extracted_travel"]` (origin, destination, etc.)
      - `fare_result_dict["metadata"]["top_offers"]` (liste d’offres et détails)
    - **Sortie**: HTML complet (string) conforme au template Jinja2
      `src/templates/audit_executive.html`.
    - **Utilise**:
      - helpers de formatage/sélection dans ce module (`_select_top3`, etc.)
      - QR payload (plain text) encodé en PNG base64
      - `jinja2.Template(...).render(...)`
    - **Utilisé par**:
      - `resolve_audit_report_html` (appelé par `SQSConsumer._build_report`)
    - **Effets de bord**: lecture du template HTML sur disque; CPU (rendu Jinja2 + QR).
    """
    meta, fare_id, travel = _exec_parse_inputs(fare_result_dict)
    tctx = _exec_build_travel_context(meta=meta, fare_id=fare_id, travel=travel)
    octx = _exec_build_offer_and_comparison_context(meta=meta, tctx=tctx)

    ranked_offers_sub = "Scannable comparison for executive review. Full conditions are expanded on page 2."
    disclaimer_text = (
        "This report is provided for informational and decision-support purposes only. "
        "All fares, availability, and fare conditions remain subject to airline change until ticketed. "
        "FairFare conducts an independent comparative review based on the data available at the time of analysis. "
        "Final booking decisions remain subject to inventory control, fare rule enforcement, and internal approval."
    )

    qr_plain = _executive_plain_qr_payload(_exec_qr_args(fare_id=fare_id, tctx=tctx, octx=octx))
    qr_data_uri = _exec_qr_data_uri(
        qr_plain=qr_plain,
        fare_id=fare_id,
        audit_id=tctx["audit_id"],
        route_title=tctx["route_title"],
        n=octx["n"],
    )

    template = _get_executive_template()
    return template.render(
        logo_data_uri=_LOGO_DATA_URI,
        partner_label="Partner placement",
        partner_title="Promote travel services that complement the audit",
        route_title=tctx["route_title"],
        route_sub=octx["route_sub"],
        travel_date_meta=tctx["travel_date_meta"],
        passenger_meta=tctx["passenger"],
        sector_meta=octx["sector_meta"],
        cabin_meta=tctx["cabin"],
        audit_id=tctx["audit_id"],
        rec_title=octx["rec_title"],
        rec_bullets=octx["rec_bullets"],
        policy_status=octx["policy_status"],
        flexibility_status=octx["flexibility_status"],
        ranked_offers_sub=ranked_offers_sub,
        offer_cards=octx["offer_cards"],
        offer_heads=octx["offer_heads"],
        offer_heads_det=octx["offer_heads_det"],
        quick_rows=octx["quick_rows"],
        exec_compare_sub=octx["exec_compare_sub"],
        disclaimer_text=disclaimer_text,
        qr_data_uri=qr_data_uri,
        page2_subtitle=octx["page2_subtitle"],
        selected_letter=octx["selected_letter"],
        detail_blocks=octx["detail_blocks"],
    )


def resolve_audit_report_html(fare_result_dict: dict) -> str:
    """Routeur “layout → HTML generator”.

    - **Rôle / impact**: point d’extension si plusieurs layouts existent.
    - **Utilisé par**: `SQSConsumer._build_report`.
    """
    return generate_audit_report_html_executive(fare_result_dict)


def render_audit_pdf(html: str, _fare_result_dict: dict) -> bytes:
    """Rend un PDF à partir d’un HTML.

    - **Rôle / impact**: tente Playwright en premier; en cas d’échec, bascule sur
      WeasyPrint (avec `base_url` pointant sur `src/templates` pour les assets).
    - **Utilisé par**: `SQSConsumer._build_report`, `generate_audit_report_pdf`.
    - **Effets de bord**: peut lancer Chromium headless; peut utiliser WeasyPrint;
      logs d’avertissement en cas de fallback.
    """
    # Optional: limit render concurrency (keeps current behavior when 0)
    sem_n = int(getattr(settings, "pdf_max_concurrent_renders", 0) or 0)
    if sem_n > 0:
        import threading

        if not hasattr(render_audit_pdf, "_sem"):
            render_audit_pdf._sem = threading.Semaphore(sem_n)  # type: ignore[attr-defined]
        sem = render_audit_pdf._sem  # type: ignore[attr-defined]
    else:
        sem = None

    try:
        if sem is not None:
            with sem:
                return _render_pdf_playwright(html)
        return _render_pdf_playwright(html)
    except Exception as e:
        logger.warning(
            "Playwright audit PDF render failed (%s). Falling back to WeasyPrint.",
            str(e),
            exc_info=True,
        )
        if getattr(settings, "pdf_playwright_use_set_content", False):
            # Emit a signal for ops; caller may also emit metrics.
            logger.warning("Playwright fallback triggered; using WeasyPrint")
        return _render_pdf_weasyprint(html, base_url=str(_TEMPLATE_DIR))


def generate_audit_report_pdf(fare_result_dict: dict) -> bytes:
    """Convenience: génère directement un PDF depuis un `fare_result_dict`."""
    html = resolve_audit_report_html(fare_result_dict)
    return render_audit_pdf(html, fare_result_dict)
