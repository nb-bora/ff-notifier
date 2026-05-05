import os
from pathlib import Path

import pytest


def _base_fare_result_dict(*, trip_type: str, return_date: str) -> dict:
    return {
        "id": "r-res-doc-1",
        "fare_event_id": "fe-res-doc-1",
        "timestamp": "2026-01-01T10:15:00+00:00",
        "status": "analysis_complete",
        "metadata": {
            "sender": "user@example.com",
            "subject": "Voyage Paris → Londres",
            "extracted_travel": {
                "origin": "PAR",
                "destination": "LON",
                "trip_type": trip_type,
                "cabin_class": "economy",
                "departure_date": "2026-06-15",
                "return_date": return_date,
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
                    # Optional: round-trip support (used by the round-trip template)
                    "return_flight_details": {
                        "origin": "LON",
                        "destination": "PAR",
                        "departure_at": "2026-06-25T18:45:00+01:00",
                        "arrival_at": "2026-06-25T21:15:00+02:00",
                        "duration": "PT1H30M",
                        "stops": 0,
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
                    "return_flight_details": {
                        "origin": "LON",
                        "destination": "PAR",
                        "departure_at": "2026-06-25T10:20:00+01:00",
                        "arrival_at": "2026-06-25T12:50:00+02:00",
                        "duration": "PT1H30M",
                        "stops": 0,
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
                    "return_flight_details": {
                        "origin": "LON",
                        "destination": "PAR",
                        "departure_at": "2026-06-25T20:05:00+01:00",
                        "arrival_at": "2026-06-25T22:30:00+02:00",
                        "duration": "PT1H25M",
                        "stops": 0,
                    },
                },
            ],
        },
    }


@pytest.mark.parametrize(
    "trip_type,return_date,expected_layout",
    [
        ("one_way", "", "aller-simple"),
        ("round_trip", "2026-06-25", "aller-retour"),
    ],
)
def test_pdf_generation_reservation_documents_smoke(trip_type: str, return_date: str, expected_layout: str):
    try:
        from infrastructure.pdf.pdf_generator import (  # type: ignore[import-not-found]
            resolve_audit_report_html,
            render_audit_pdf,
        )
    except Exception as e:  # pragma: no cover
        pytest.skip(f"PDF stack unavailable: {e}")

    fare_result_dict = _base_fare_result_dict(trip_type=trip_type, return_date=return_date)
    html = resolve_audit_report_html(fare_result_dict, "reservation_documents")
    assert isinstance(html, str)
    assert len(html) > 5000
    assert "FAIRFARE" in html.upper()

    try:
        pdf = render_audit_pdf(html, fare_result_dict)
    except Exception as e:  # pragma: no cover
        pytest.skip(f"PDF rendering unavailable in this environment: {e}")

    assert isinstance(pdf, (bytes, bytearray))
    assert bytes(pdf).startswith(b"%PDF")
    assert len(pdf) > 10_000

    if os.getenv("PDF_TEST_WRITE_ARTIFACT", "").strip().lower() in {"1", "true", "yes"}:
        artifacts_dir = Path(__file__).resolve().parents[1] / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        out = artifacts_dir / f"reservation-doc-{expected_layout}.pdf"
        out.write_bytes(bytes(pdf))
        print(f"PDF artifact written: {out.resolve()}")

