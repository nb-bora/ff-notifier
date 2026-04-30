import asyncio

import pytest

from application.use_cases.process_fare_result_use_case import ProcessFareResultUseCase
from domain.entities.fare_result import FareResult


class _Ses:
    def __init__(self, *, ok: bool = True):
        self.ok = ok
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return self.ok


class _S3:
    def __init__(self):
        self.calls = []

    def upload_pdf(self, **kwargs):
        self.calls.append(kwargs)
        return "s3://bucket/key"


class _Metrics:
    def __init__(self):
        self.events = []

    def emit(self, metric_name: str, value: float, unit: str = "Count"):
        self.events.append((metric_name, value, unit))


@pytest.mark.asyncio
async def test_use_case_error_status_sends_error_email_and_deletes():
    ses = _Ses(ok=True)
    uc = ProcessFareResultUseCase(
        ses_client=ses, s3_client=_S3(), metrics_client=_Metrics()
    )

    fr = FareResult(
        id="r1",
        fare_event_id="fe1",
        timestamp="2026-01-01T00:00:00+00:00",
        status="parsing_failed",
        error_message="oops",
        metadata={"sender": "user@example.com", "subject": "Hello"},
    )

    async def _build_report(_):
        raise AssertionError("should not build report for parsing_failed")

    result = await uc.execute(
        fare_result=fr, recipient_email="user@example.com", build_report=_build_report
    )
    assert result.should_delete_message is True
    assert result.emails_sent == 1
    assert result.errors == 0
    assert len(ses.calls) == 1


@pytest.mark.asyncio
async def test_use_case_analysis_complete_sends_success_email_and_deletes():
    ses = _Ses(ok=True)
    uc = ProcessFareResultUseCase(
        ses_client=ses, s3_client=_S3(), metrics_client=_Metrics()
    )

    fr = FareResult(
        id="r1",
        fare_event_id="fe1",
        timestamp="2026-01-01T00:00:00+00:00",
        status="analysis_complete",
        metadata={"sender": "user@example.com", "subject": "Hello"},
    )

    async def _build_report(_):
        return ("<html/>", b"%PDF-1.4", None, None)

    result = await uc.execute(
        fare_result=fr, recipient_email="user@example.com", build_report=_build_report
    )
    assert result.should_delete_message is True
    assert result.emails_sent == 1
    assert result.errors == 0
    assert len(ses.calls) == 1


@pytest.mark.asyncio
async def test_use_case_send_failure_requests_retry():
    ses = _Ses(ok=False)
    uc = ProcessFareResultUseCase(
        ses_client=ses, s3_client=_S3(), metrics_client=_Metrics()
    )

    fr = FareResult(
        id="r1",
        fare_event_id="fe1",
        timestamp="2026-01-01T00:00:00+00:00",
        status="validation_error",
        error_message="oops",
        metadata={"sender": "user@example.com", "subject": "Hello"},
    )

    async def _build_report(_):
        raise AssertionError("should not build report for validation_error")

    result = await uc.execute(
        fare_result=fr, recipient_email="user@example.com", build_report=_build_report
    )
    assert result.should_delete_message is False
    assert result.errors == 1


@pytest.mark.asyncio
async def test_use_case_pdf_timeout_requests_retry(monkeypatch):
    ses = _Ses(ok=True)
    metrics = _Metrics()
    uc = ProcessFareResultUseCase(ses_client=ses, s3_client=_S3(), metrics_client=metrics)

    # Force a tiny timeout for the test
    monkeypatch.setattr("application.use_cases.process_fare_result_use_case.settings.pdf_render_timeout_seconds", 0.01)

    fr = FareResult(
        id="r1",
        fare_event_id="fe-timeout",
        timestamp="2026-01-01T00:00:00+00:00",
        status="analysis_complete",
        metadata={"sender": "user@example.com", "subject": "Hello"},
    )

    async def _build_report(_):
        await asyncio.sleep(0.2)
        return ("<html/>", b"%PDF-1.4", None, None)

    result = await uc.execute(
        fare_result=fr, recipient_email="user@example.com", build_report=_build_report
    )
    assert result.should_delete_message is False
    assert result.errors == 1
