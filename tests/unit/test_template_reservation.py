from pathlib import Path
from datetime import datetime
import os

import pytest
from jinja2 import Environment, StrictUndefined


def _render_template_reservation(**ctx: object) -> str:
    template_path = (
        Path(__file__).resolve().parents[2] / "src" / "templates" / "template-reservation.html"
    )
    raw = template_path.read_text(encoding="utf-8")
    env = Environment(undefined=StrictUndefined, autoescape=False)
    tpl = env.from_string(raw)
    return tpl.render(**ctx)


def _base_context(*, offer_cards: list[dict], route_sub: str, travel_date_meta: str) -> dict:
    offer_heads = [f"Offer {o['letter']}" for o in offer_cards]
    return {
        "logo_data_uri": "data:image/png;base64,AA==",
        "partner_label": "Partenaire",
        "partner_title": "Contenu sponsorisé",
        "route_title": "PAR → LON",
        "route_sub": route_sub,
        "travel_date_meta": travel_date_meta,
        "passenger_meta": "1 Adulte",
        "sector_meta": "Paris (PAR) → Londres (LON)",
        "cabin_meta": "Economy",
        "audit_id": "FFA-TEST",
        "rec_title": "Pourquoi l’option A",
        "rec_bullets": ["Prix optimisé", "Bon compromis flexibilité/risque"],
        "policy_status": "Aligned",
        "flexibility_status": "Good",
        "ranked_offers_sub": "Vue synthétique des options classées.",
        "offer_cards": offer_cards,
        "exec_compare_sub": "Comparatif procurement synthétique des options retenues.",
        "offer_heads": offer_heads,
        "quick_rows": [
            {"label": "Price", "cells": [o.get("price_display", "—") for o in offer_cards]},
            {"label": "Change profile", "cells": [o.get("flex_line", "—") for o in offer_cards]},
        ],
        "disclaimer_text": "Document informatif (données et conditions susceptibles d’évoluer).",
        "qr_data_uri": "data:image/png;base64,AA==",
        "page2_subtitle": f"{route_sub} • {travel_date_meta}",
        "selected_letter": offer_cards[0]["letter"] if offer_cards else "—",
        "offer_heads_det": offer_heads,
        "detail_blocks": [
            {
                "title": "Flight details",
                "rows": [
                    {"label": "Airline / Flight", "cells": [o.get("airline_line", "—") for o in offer_cards]},
                    {"label": "Stops", "cells": ["—" for _ in offer_cards]},
                ],
            }
        ],
    }


def test_template_reservation_renders_one_way_offer():
    offer = {
        "round_trip": False,
        "recommended": True,
        "letter": "A",
        "airline_line": "AF",
        "badge_class": "selected",
        "badge_text": "Recommandé",
        "price_display": "€ 180.00",
        "chips": ["Economy", "Direct", "1 pax"],
        "dep_time": "09:30",
        "arr_time": "10:05",
        "origin_code": "PAR",
        "dest_code": "LON",
        "route_subline": "PAR → LON • 15 Jun 2026",
        "flex_line": "Changements inclus",
        "footer_note": "Meilleur équilibre coût / conditions",
    }
    html = _render_template_reservation(
        **_base_context(
            offer_cards=[offer],
            route_sub="Aller simple • 1 pax • AF",
            travel_date_meta="15 Jun 2026",
        )
    )
    assert "Ranked offers" in html
    assert "Outbound" not in html  # round-trip header should not appear
    assert "Return" not in html
    assert "Offer A" in html
    assert "PAR" in html and "LON" in html


def test_template_reservation_renders_round_trip_offer():
    offer = {
        "round_trip": True,
        "recommended": True,
        "letter": "A",
        "airline_line": "AF",
        "badge_class": "selected",
        "badge_text": "Recommandé",
        "price_display": "€ 320.00",
        "chips": ["Economy", "Direct", "1 pax"],
        # outbound
        "outbound_date": "15 Jun 2026",
        "outbound_arr_date": "15 Jun 2026",
        "outbound_duration": "1h35",
        "outbound_stops": "Direct",
        "dep_time": "09:30",
        "arr_time": "10:05",
        "origin_code": "PAR",
        "dest_code": "LON",
        # inbound
        "inbound_date": "18 Jun 2026",
        "inbound_arr_date": "18 Jun 2026",
        "inbound_duration": "1h40",
        "inbound_stops": "Direct",
        "ret_dep_time": "19:10",
        "ret_arr_time": "21:50",
        "flex_line": "Changements inclus",
        "footer_note": "Option la plus robuste",
    }

    # Cohérence dates (retour après l'aller).
    d_out = datetime.strptime(offer["outbound_date"], "%d %b %Y").date()
    d_in = datetime.strptime(offer["inbound_date"], "%d %b %Y").date()
    assert d_in > d_out
    assert offer["outbound_arr_date"] == offer["outbound_date"]
    assert offer["inbound_arr_date"] == offer["inbound_date"]

    # Cohérence horaires (format HH:MM).
    datetime.strptime(offer["dep_time"], "%H:%M")
    datetime.strptime(offer["arr_time"], "%H:%M")
    datetime.strptime(offer["ret_dep_time"], "%H:%M")
    datetime.strptime(offer["ret_arr_time"], "%H:%M")

    html = _render_template_reservation(
        **_base_context(
            offer_cards=[offer],
            route_sub="Aller-retour • 1 pax • AF",
            travel_date_meta="15 Jun 2026 – 18 Jun 2026",
        )
    )
    assert "Ranked offers" in html
    assert "Outbound" in html
    assert "Return" in html
    assert "Offer A" in html
    assert "PAR" in html and "LON" in html

    # Optional: generate a real PDF artifact for manual inspection.
    if os.getenv("PDF_TEST_WRITE_ARTIFACT", "").strip().lower() in {"1", "true", "yes"}:
        try:
            from infrastructure.pdf.pdf_generator import (  # type: ignore[import-not-found]
                render_audit_pdf,
            )
        except Exception as e:  # pragma: no cover
            pytest.skip(f"PDF stack unavailable: {e}")

        try:
            pdf = render_audit_pdf(html, {})
        except Exception as e:  # pragma: no cover
            pytest.skip(f"PDF rendering unavailable in this environment: {e}")

        artifacts_dir = Path(__file__).resolve().parents[1] / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        out = artifacts_dir / "template-reservation-roundtrip.pdf"
        out.write_bytes(bytes(pdf))
        print(f"PDF artifact written: {out.resolve()}")

