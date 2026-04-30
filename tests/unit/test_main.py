import pytest


@pytest.mark.asyncio
async def test_root_endpoint(test_client):
    response = test_client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "FairFare Notifier Service"
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_health_endpoint(test_client):
    response = test_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["service"] == "ff-notifier"
    assert "metrics" in data


@pytest.mark.asyncio
async def test_metrics_endpoint(test_client):
    response = test_client.get("/metrics")
    assert response.status_code == 200
    data = response.json()
    assert "messages_processed" in data
    assert "emails_sent" in data
    assert "errors" in data
