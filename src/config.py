"""Configuration (settings) du microservice Notifier.

Ce module centralise la lecture des variables d’environnement (via `.env`) et
expose un singleton `settings: Settings`.

Principes:
- **Compatibilité**: les noms d’env sont alignés sur `ff-notifier`.
- **Séparation**: ce module ne doit pas déclencher de workflows métier; il ne fait
  que lire de la config et (optionnellement) charger des valeurs depuis SSM.

Effets de bord (au chargement du module):
- lecture du fichier `.env` à la racine du repo.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


def get_parameter(
    parameter_name: str, region_name: str, profile_name: Optional[str] = None
) -> Optional[str]:
    """Récupère un paramètre chiffré depuis AWS SSM Parameter Store.

    - **Rôle / impact**: externaliser certains templates/settings dans AWS.
    - **Utilise**: boto3 SSM `get_parameter(WithDecryption=True)`.
    - **Utilisé par**: `Settings.get_email_template`, `Settings.get_pdf_settings`.
    - **Effets de bord**: appel réseau AWS SSM.
    - **Erreurs**: renvoie `None` si le paramètre est introuvable / accès refusé.
    """
    try:
        if profile_name:
            session = boto3.Session(profile_name=profile_name)
            client = session.client("ssm", region_name=region_name)
        else:
            client = boto3.client("ssm", region_name=region_name)

        response = client.get_parameter(Name=parameter_name, WithDecryption=True)
        return response["Parameter"]["Value"]

    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        if error_code == "ParameterNotFound":
            print(f"Parameter {parameter_name} not found in region {region_name}")
        elif error_code == "AccessDeniedException":
            print(f"Access denied to parameter {parameter_name}")
        else:
            print(f"Error retrieving parameter {parameter_name}: {e}")
        return None


@dataclass
class Settings:
    """Settings compatibles `ff-notifier` (variables d’env identiques).

    - **Rôle / impact**: fournir un accès typé/centralisé à la configuration.
    - **Utilisé par**: `main`, `sqs_consumer`, `pdf_generator`, `xray_config`, tests.
    - **Effets de bord**: aucun (lecture env). Les champs contrôlent des effets de bord
      ailleurs (poll SQS, SES, S3, PDF…).
    """

    aws_region: str = os.getenv("AWS_REGION", "af-south-1")
    aws_profile: str = os.getenv("AWS_PROFILE", "")

    s3_pdf_enabled: bool = os.getenv("S3_PDF_ENABLED", "false").lower() == "true"
    s3_pdf_bucket: str = os.getenv("S3_PDF_BUCKET", "")
    s3_pdf_prefix: str = os.getenv("S3_PDF_PREFIX", "pdf_reports/").lstrip("/")
    s3_pdf_sse_kms_key_id: str = os.getenv("S3_PDF_SSE_KMS_KEY_ID", "")

    sqs_fare_result_queue_url: str = os.getenv("SQS_FARE_RESULT_QUEUE_URL", "")
    sqs_max_messages: int = int(os.getenv("SQS_MAX_MESSAGES", "10"))
    sqs_wait_time_seconds: int = int(os.getenv("SQS_WAIT_TIME_SECONDS", "20"))
    sqs_visibility_timeout: int = int(os.getenv("SQS_VISIBILITY_TIMEOUT", "300"))

    ses_sender_email: str = os.getenv("SES_SENDER_EMAIL", "")
    ses_max_retries: int = int(os.getenv("SES_MAX_RETRIES", "3"))
    ses_sandbox_mode: bool = os.getenv("SES_SANDBOX_MODE", "false").lower() == "true"
    ses_sandbox_test_email: str = os.getenv("SES_SANDBOX_TEST_EMAIL", "")

    pdf_enabled: bool = os.getenv("PDF_ENABLED", "true").lower() == "true"
    qr_code_enabled: bool = os.getenv("QR_CODE_ENABLED", "true").lower() == "true"

    parameter_store_enabled: bool = (
        os.getenv("PARAMETER_STORE_ENABLED", "true").lower() == "true"
    )
    email_template_parameter: str = os.getenv(
        "EMAIL_TEMPLATE_PARAMETER", "/fairfare/email-template"
    )
    pdf_settings_parameter: str = os.getenv(
        "PDF_SETTINGS_PARAMETER", "/fairfare/pdf-settings"
    )

    service_name: str = "ff-notifier"
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    environment: str = os.getenv("ENVIRONMENT", "dev")
    host: str = os.getenv("HOST", "0.0.0.0")  # nosec B104
    port: int = int(os.getenv("PORT", "8000"))  # nosec B104
    report_save_to_disk: bool = (
        os.getenv("REPORT_SAVE_TO_DISK", "false").lower() == "true"
    )
    report_open_after_generate: bool = (
        os.getenv("REPORT_OPEN_AFTER_GENERATE", "false").lower() == "true"
    )

    consumer_enabled: bool = os.getenv("CONSUMER_ENABLED", "true").lower() == "true"
    consumer_max_retries: int = int(os.getenv("CONSUMER_MAX_RETRIES", "3"))
    consumer_error_delay_seconds: int = int(
        os.getenv("CONSUMER_ERROR_DELAY_SECONDS", "5")
    )
    consumer_max_concurrent_messages: int = int(
        os.getenv("CONSUMER_MAX_CONCURRENT_MESSAGES", "10")
    )
    consumer_executor_max_workers: int = int(
        os.getenv("CONSUMER_EXECUTOR_MAX_WORKERS", "2")
    )
    # Optional: limit PDF render parallelism (0 = no extra limit; keep current behavior)
    pdf_max_concurrent_renders: int = int(os.getenv("PDF_MAX_CONCURRENT_RENDERS", "0"))
    # Optional: prefer Playwright page.set_content over temp file (default keeps behavior)
    pdf_playwright_use_set_content: bool = (
        os.getenv("PDF_PLAYWRIGHT_USE_SET_CONTENT", "false").lower() == "true"
    )

    enable_xray: bool = os.getenv("ENABLE_XRAY", "true").lower() == "true"
    xray_daemon_address: Optional[str] = (
        os.getenv("AWS_XRAY_DAEMON_ADDRESS") or "localhost:2000"
    )

    pdf_render_timeout_seconds: int = int(
        os.getenv("PDF_RENDER_TIMEOUT_SECONDS", "120")
    )

    def get_email_template(self) -> Optional[str]:
        """Retourne un template email (optionnel) depuis SSM.

        - **Impact**: actuellement non branché dans le pipeline principal, mais
          garde la compatibilité “configurable via Parameter Store”.
        """
        if not self.parameter_store_enabled:
            return None
        return get_parameter(
            self.email_template_parameter,
            self.aws_region,
            self.aws_profile if self.aws_profile else None,
        )

    def get_pdf_settings(self) -> Optional[str]:
        """Retourne des settings PDF (optionnels) depuis SSM (non utilisés actuellement)."""
        if not self.parameter_store_enabled:
            return None
        return get_parameter(
            self.pdf_settings_parameter,
            self.aws_region,
            self.aws_profile if self.aws_profile else None,
        )


settings = Settings()
