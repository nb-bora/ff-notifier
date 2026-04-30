from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_aws_credentials():
    with patch.dict(
        "os.environ",
        {
            "AWS_REGION": "af-south-1",
            "AWS_PROFILE": "",
            "SQS_FARE_RESULT_QUEUE_URL": "https://sqs.af-south-1.amazonaws.com/123/test-queue",
            "SES_SENDER_EMAIL": "test@example.com",
        },
    ):
        yield


@pytest.fixture
def mock_boto3_client():
    with patch("boto3.client") as mock:
        yield mock


@pytest.fixture
def test_client(mock_aws_credentials, mock_boto3_client):
    from main import app

    return TestClient(app)
