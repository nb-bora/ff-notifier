import pytest
import os
from pathlib import Path


def _mock_fare_result_dict() -> dict:
    """Payload minimal mais cohérent pour `audit_executive.html`."""
    return {
        "id": "r-pdf-1",
        "fare_event_id": "fe-pdf-smoke-1",
        "timestamp": "2026-01-01T10:15:00+00:00",
        "status": "analysis_complete",
        "metadata": {
            "sender": "user@example.com",
            "subject": "Voyage Paris → Londres",
            "extracted_travel": {
                "origin": "PAR",
                "destination": "LON",
                "trip_type": "one_way",
                "cabin_class": "economy",
                "departure_date": "2026-06-15",
                "return_date": "",
                "passengers_adults": 1,
                "passengers_children": 0,
            },
            "top_offers": [
                {
                    "ovi_signal": "VERT",
                    "signals": ["DIRECT_FLIGHT"],
                    "tier2": {"change_rule": {"reason": ""}},
                    "flight_details": {
                        "origin": "PAR",
                        "destination": "LON",
                        "departure_at": "2026-06-15T09:30:00+02:00",
                        "arrival_at": "2026-06-15T10:05:00+01:00",
                        "duration": "PT1H35M",
                        "stops": 0,
                        "carriers": ["AF", "BA"],
                        "cabin": "economy",
                        "currency": "EUR",
                        "price": 180.0,
                        "refundable": False,
                        "changes_free": True,
                        "baggage_allowance": "1 carry-on",
                    },
                },
                {
                    "ovi_signal": "JAUNE",
                    "signals": ["ONE_STOP_FLIGHT"],
                    "tier2": {"change_rule": {"reason": "Fee"}},
                    "flight_details": {
                        "origin": "PAR",
                        "destination": "LON",
                        "departure_at": "2026-06-15T12:15:00+02:00",
                        "arrival_at": "2026-06-15T14:35:00+01:00",
                        "duration": "PT3H20M",
                        "stops": 1,
                        "carriers": ["LH"],
                        "cabin": "economy",
                        "currency": "EUR",
                        "price": 165.0,
                        "refundable": False,
                        "changes_free": False,
                        "baggage_allowance": "1 checked bag",
                    },
                },
                {
                    "ovi_signal": "ROUGE",
                    "signals": [],
                    "tier2": {"change_rule": {"reason": "Fee applies"}},
                    "flight_details": {
                        "origin": "PAR",
                        "destination": "LON",
                        "departure_at": "2026-06-15T18:45:00+02:00",
                        "arrival_at": "2026-06-15T19:25:00+01:00",
                        "duration": "PT1H40M",
                        "stops": 0,
                        "carriers": ["U2"],
                        "cabin": "economy",
                        "currency": "EUR",
                        "price": 95.0,
                        "refundable": False,
                        "changes_free": False,
                        "baggage_allowance": "",
                    },
                },
            ],
        },
    }


def test_pdf_generation_uses_audit_executive_template_smoke():
    """Smoke test: rend l'HTML executive et tente une génération PDF.

    - Vérifie que le template `src/templates/audit_executive.html` est utilisable
      avec un payload cohérent.
    - Tente la génération PDF via Playwright puis fallback WeasyPrint.

    Le test se skip si la stack PDF n'est pas disponible localement/CI.
    """
    # Import inside test to keep collection lightweight.
    try:
        from infrastructure.pdf.pdf_generator import (  # type: ignore[import-not-found]
            resolve_audit_report_html,
            render_audit_pdf,
        )
    except Exception as e:  # pragma: no cover
        pytest.skip(f"PDF stack unavailable: {e}")

    fare_result_dict = _mock_fare_result_dict()
    html = resolve_audit_report_html(fare_result_dict)
    assert isinstance(html, str)
    assert len(html) > 5000  # template executive is large
    assert "FAIRFARE" in html.upper()

    try:
        pdf = render_audit_pdf(html, fare_result_dict)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"PDF rendering unavailable in this environment: {e}")

    assert isinstance(pdf, (bytes, bytearray))
    assert bytes(pdf).startswith(b"%PDF")
    assert len(pdf) > 10_000

    # Optional: write artifact to disk for manual inspection.
    if os.getenv("PDF_TEST_WRITE_ARTIFACT", "").strip().lower() in {"1", "true", "yes"}:
        artifacts_dir = Path(__file__).resolve().parents[1] / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        out = artifacts_dir / f"{fare_result_dict['fare_event_id']}.pdf"
        out.write_bytes(bytes(pdf))
        print(f"PDF artifact written: {out.resolve()}")

