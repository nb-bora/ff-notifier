"""Entrypoint FastAPI du microservice Notifier.

Ce module expose:
- **API HTTP**: `/`, `/health`, `/metrics`
- **Lifecycle**: démarre/stoppe le `SQSConsumer` en tâche de fond via `lifespan`.

Rôle du service:
- consommer des messages SQS “FareResult”
- générer (optionnellement) un PDF d’audit
- envoyer des emails via SES (avec PJ PDF en succès)
- publier des métriques (CloudWatch + compteurs in-memory)
- tracer les traitements via AWS X-Ray (optionnel)

Effets de bord (au chargement du module):
- lecture du fichier `.env`
- initialisation X-Ray (`init_xray`) si activé
"""

import asyncio
import sys
import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from config import settings
from infrastructure.messaging.sqs_consumer import SQSConsumer
from logger import logger
from presentation.schemas import HealthResponse, MetricsResponse
from xray_config import init_xray

if sys.platform == "win32" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

init_xray()


class Metrics:
    """Compteurs in-memory exposés par l’API.

    - **Rôle / impact**: fournir un état simple observable via `/health` et `/metrics`.
    - **Utilisé par**: `SQSConsumer` (via callbacks `increment_*`) et endpoints.
    - **Effets de bord**: mutation mémoire uniquement (pas persistant).
    """

    def __init__(self):
        self.messages_processed = 0
        self.emails_sent = 0
        self.errors = 0

    def increment_processed(self):
        """Incrémente le compteur de messages traités (ack SQS)."""
        self.messages_processed += 1

    def increment_sent(self):
        """Incrémente le compteur d’emails envoyés (succès logique)."""
        self.emails_sent += 1

    def increment_error(self):
        """Incrémente le compteur d’erreurs (échecs logiques/techniques)."""
        self.errors += 1


metrics = Metrics()
sqs_consumer = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Hook startup/shutdown FastAPI.

    Startup:
    - log la configuration essentielle
    - démarre le `SQSConsumer` si `CONSUMER_ENABLED=true`

    Shutdown:
    - stoppe le consumer si démarré

    - **Utilise**: `config.settings`, `SQSConsumer.start/stop`.
    - **Effets de bord**: crée une tâche de polling SQS + threads (executor).
    """
    global sqs_consumer

    logger.info("Starting FairFare Notifier Service")
    logger.info(f"Consumer enabled: {settings.consumer_enabled}")
    logger.info(f"PDF generation enabled: {settings.pdf_enabled}")
    logger.info(
        "Audit PDF layout: executive (fixed, src/templates/audit_executive.html)"
    )
    if not settings.sqs_fare_result_queue_url:
        logger.warning("SQS_FARE_RESULT_QUEUE_URL is empty; consumer will fail to poll")
    if not settings.ses_sender_email:
        logger.warning("SES_SENDER_EMAIL is empty; SES sends will fail")

    try:
        if settings.consumer_enabled:
            sqs_consumer = SQSConsumer(api_metrics=metrics)
            sqs_consumer.start()
            logger.info("SQS Consumer started")
        else:
            logger.info("SQS Consumer is disabled")

        logger.info("Application startup complete")
    except Exception as e:
        logger.error(f"Error during startup: {str(e)}", exc_info=True)
        raise

    yield

    logger.info("Shutting down")
    if sqs_consumer:
        with contextlib.suppress(asyncio.CancelledError):
            await sqs_consumer.stop()
    logger.info("Shutdown complete")


app = FastAPI(
    title="FairFare Notifier Service",
    description="Consume fare events and send audit reports via email",
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request, exc):
    """Normalise les erreurs de validation FastAPI en HTTP 400 JSON."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": str(exc)},
    )


@app.get("/")
async def root():
    """Endpoint racine: information de service (diagnostic rapide)."""
    return {
        "service": "FairFare Notifier Service",
        "version": "0.1.0",
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Endpoint de santé: expose statut + config + compteurs in-memory."""
    return {
        "status": "healthy",
        "service": "ff-notifier",
        "version": "0.1.0",
        "environment": settings.environment,
        "consumer_enabled": settings.consumer_enabled,
        "aws_region": settings.aws_region,
        "pdf_enabled": settings.pdf_enabled,
        "metrics": {
            "messages_processed": metrics.messages_processed,
            "emails_sent": metrics.emails_sent,
            "errors": metrics.errors,
        },
    }


@app.get("/metrics", response_model=MetricsResponse)
async def get_metrics():
    """Endpoint métriques: expose uniquement les compteurs in-memory."""
    return {
        "messages_processed": metrics.messages_processed,
        "emails_sent": metrics.emails_sent,
        "errors": metrics.errors,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
