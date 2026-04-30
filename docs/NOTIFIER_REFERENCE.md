# Notifier — Documentation de référence (par fichier)

Objectif: documenter **chaque fichier** du microservice Notifier (FairFare) afin qu’un nouvel arrivant puisse diagnostiquer, modifier et opérer le service rapidement.

Ce document est organisé par fichier. Pour chaque fonction/classe:

- **Rôle / impact**: ce que la fonction garantit, ce qu’elle modifie, ce qu’elle déclenche.
- **Utilise**: dépendances directes (fonctions/classes/modules) et services externes.
- **Utilisé par**: principaux points d’appel.
- **Effets de bord**: réseau, I/O disque, métriques, traces.

---

## 1) Racine du repo

### `run.py`

#### `main() -> None`
- **Rôle / impact**: lance `uvicorn` en mode dev (reload) en pointant vers `main:app` dans `src/`.
- **Utilise**: `uvicorn.run(...)`.
- **Utilisé par**: exécution locale `python run.py`.
- **Effets de bord**: démarre un serveur HTTP + reloader; ouvre des sockets.

### `.env.example`
- **Rôle / impact**: modèle des variables d’environnement attendues par `src/config.py`.
- **Utilise**: n/a.
- **Utilisé par**: onboarding/dev (copie vers `.env`).
- **Effets de bord**: n/a.

### `pyproject.toml`
- **Rôle / impact**: manifeste du projet (dépendances runtime + dev) et configuration ruff/pytest/coverage.
- **Utilise**: n/a.
- **Utilisé par**: `pip install -e .`, CI, tooling.

### `.gitignore`
- **Rôle / impact**: ignore venv, caches, artefacts.

### Fichiers générés (caches outillage)

#### `.pytest_cache/**`
- **Rôle / impact**: cache généré par pytest (node ids, last failed, etc.).
- **Utilise**: n/a.
- **Utilisé par**: `pytest`.
- **Effets de bord**: aucun sur le runtime; peut être supprimé sans risque (se régénère).

#### `.ruff_cache/**`
- **Rôle / impact**: cache généré par ruff pour accélérer lint/format.
- **Utilise**: n/a.
- **Utilisé par**: `ruff`.
- **Effets de bord**: aucun sur le runtime; peut être supprimé sans risque (se régénère).

---

## 2) Point d’entrée HTTP & lifecycle

### `src/main.py`

#### Contexte global (imports + init)
- **Rôle / impact**:
  - Charge `.env` (au démarrage du module).
  - Initialise X-Ray via `init_xray()`.
  - Sur Windows, force une policy d’event loop compatible.
- **Utilise**: `dotenv.load_dotenv`, `xray_config.init_xray`, `asyncio.WindowsProactorEventLoopPolicy`.
- **Utilisé par**:
  - `uvicorn` via `main:app`
  - Les tests via `TestClient(app)`.
- **Effets de bord**: lecture fichier `.env`, instrumentation X-Ray (si activée), logs.

#### `class Metrics`
- **Rôle / impact**: compteur **in-memory** exposé par `/health` et `/metrics` (messages traités, emails envoyés, erreurs).
- **Utilise**: aucune dépendance externe.
- **Utilisé par**:
  - `lifespan()` (passe l’instance à `SQSConsumer(api_metrics=metrics)`).
  - Endpoints `health_check()` et `get_metrics()`.
- **Effets de bord**: mutation mémoire uniquement.
- **Méthodes**:
  - `increment_processed()`: incrémente `messages_processed`.
  - `increment_sent()`: incrémente `emails_sent`.
  - `increment_error()`: incrémente `errors`.

#### `metrics = Metrics()`
- **Rôle / impact**: instance globale partagée.
- **Utilisé par**: endpoints, `SQSConsumer`.

#### `lifespan(_app: FastAPI)` (async context manager)
- **Rôle / impact**: hook FastAPI startup/shutdown.
  - Startup: log configuration, crée et démarre `SQSConsumer` si `CONSUMER_ENABLED=true`.
  - Shutdown: stoppe le consumer si démarré.
- **Utilise**:
  - `config.settings` (flags, URLs, emails).
  - `infrastructure.messaging.sqs_consumer.SQSConsumer.start/stop`.
- **Utilisé par**: FastAPI via `FastAPI(..., lifespan=lifespan)`.
- **Effets de bord**: démarre une tâche asyncio de polling SQS + threads (executor).

#### `validation_exception_handler(_request, exc)`
- **Rôle / impact**: transforme les erreurs de validation FastAPI/Pydantic en réponse JSON 400.
- **Utilise**: `fastapi.responses.JSONResponse`.
- **Utilisé par**: FastAPI (exception handler global).
- **Effets de bord**: aucun (réponse HTTP).

#### `root()`
- **Rôle / impact**: endpoint `GET /` info service.
- **Utilise**: aucune.
- **Utilisé par**: clients + tests `test_root_endpoint`.

#### `health_check()`
- **Rôle / impact**: endpoint `GET /health` expose statut + config + métriques in-memory.
- **Utilise**: `config.settings`, `metrics`.
- **Utilisé par**: clients + Docker `HEALTHCHECK` + tests.
- **Effets de bord**: aucun.

#### `get_metrics()`
- **Rôle / impact**: endpoint `GET /metrics` expose métriques in-memory.
- **Utilise**: `metrics`.
- **Utilisé par**: clients + tests.

---

## 3) Configuration

### `src/config.py`

#### Chargement `.env`
- **Rôle / impact**: `load_dotenv` est exécuté au chargement du module, ce qui permet à `Settings` de lire les variables.
- **Effets de bord**: lecture du fichier `.env`.

#### `get_parameter(parameter_name, region_name, profile_name=None) -> Optional[str]`
- **Rôle / impact**: lit un paramètre SSM (décrypté) pour externaliser templates/settings.
- **Utilise**: `boto3` (SSM), `botocore.exceptions.ClientError`.
- **Utilisé par**: `Settings.get_email_template()`, `Settings.get_pdf_settings()`.
- **Effets de bord**: appel réseau AWS SSM.
- **Erreurs**: attrape `ClientError` et renvoie `None` (logging via `print`).

#### `@dataclass class Settings`
- **Rôle / impact**: agrège la configuration (parité `ff-notifier`), principalement via variables d’environnement.
- **Utilise**: `os.getenv`, `get_parameter`.
- **Utilisé par**: presque tous les modules (`main.py`, `sqs_consumer.py`, `xray_config.py`, use case).
- **Effets de bord**: aucun (lecture env) mais gouverne des effets de bord ailleurs.
- **Champs notables**:
  - **AWS**: `AWS_REGION`, `AWS_PROFILE`
  - **SQS**: `SQS_FARE_RESULT_QUEUE_URL`, `SQS_MAX_MESSAGES`, `SQS_WAIT_TIME_SECONDS`, `SQS_VISIBILITY_TIMEOUT`
  - **SES**: `SES_SENDER_EMAIL`, sandbox `SES_SANDBOX_MODE`, `SES_SANDBOX_TEST_EMAIL`
  - **S3 PDF archive**: `S3_PDF_ENABLED`, `S3_PDF_BUCKET`, `S3_PDF_PREFIX`, `S3_PDF_SSE_KMS_KEY_ID`
  - **PDF**: `PDF_ENABLED`, `PDF_RENDER_TIMEOUT_SECONDS`
  - **Consumer**: `CONSUMER_ENABLED`, `CONSUMER_MAX_CONCURRENT_MESSAGES`, `CONSUMER_ERROR_DELAY_SECONDS`
  - **X-Ray**: `ENABLE_XRAY`, `AWS_XRAY_DAEMON_ADDRESS`
  - **Dev ergonomie**: `REPORT_SAVE_TO_DISK`, `REPORT_OPEN_AFTER_GENERATE`
- **Méthodes**:
  - `get_email_template()`: lit SSM si `PARAMETER_STORE_ENABLED=true`.
  - `get_pdf_settings()`: lit SSM si `PARAMETER_STORE_ENABLED=true`.

#### `settings = Settings()`
- **Rôle / impact**: singleton utilisé partout.

---

## 4) Logging

### `src/logger.py`

#### Configuration module-level
- **Rôle / impact**:
  - Configure `logging.basicConfig(...)` en JSON-line (stdout).
  - Filtre le bruit WeasyPrint / PIL / fontTools si `VERBOSE_WEASYPRINT=false`.
- **Utilise**: `logging`, `os`, `sys`.
- **Utilisé par**: tous les modules via `from logger import logger`.
- **Effets de bord**: configuration globale du logging Python (root handlers).

#### `logger = logging.getLogger("ff-notifier")`
- **Rôle / impact**: logger applicatif.
- **Utilisé par**: tout le code.

---

## 5) Tracing (AWS X-Ray)

### `src/xray_config.py`

#### Objectif
Fournir une API de tracing **safe**:
- si `ENABLE_XRAY=false` → no-op, sans casser le workflow.
- si `aws-xray-sdk` absent → désactivation automatique.

#### `_ensure_xray() -> None`
- **Rôle / impact**: lazy-import de `aws_xray_sdk` + initialise `_patch_all`/`_xray_recorder`.
- **Utilise**: `aws_xray_sdk.core`.
- **Utilisé par**: `init_xray`, `subsegment`, `begin_segment`, etc.
- **Effets de bord**: peut modifier `settings.enable_xray` (désactive si SDK manquant).

#### `subsegment(name: str)`
- **Rôle / impact**: context manager pour isoler des sous-opérations (SES, S3, PDF…).
- **Utilise**: `_xray_recorder.in_subsegment`.
- **Utilisé par**: `SQSConsumer` et `ProcessFareResultUseCase`.
- **Effets de bord**: envoie des sous-segments X-Ray.

#### `init_xray(extra_config: dict | None = None) -> None`
- **Rôle / impact**: configure le recorder X-Ray (AsyncContext, daemon address, patch_all).
- **Utilise**: `aws_xray_sdk.core.async_context.AsyncContext`.
- **Utilisé par**: `src/main.py` au démarrage.
- **Effets de bord**: instrumentation boto3/requests/etc via `patch_all()`.

#### `xray_capture(segment_name: str)`
- **Rôle / impact**: décorateur de capture conditionnel (no-op si désactivé).
- **Utilise**: `_xray_recorder.capture`.
- **Utilisé par**: (potentiel futur) instrumentation de fonctions.

#### `_parse_trace_header(trace_header: str) -> tuple[trace_id|None, parent_id|None]`
- **Rôle / impact**: parse l’en-tête `X-Amzn-Trace-Id` (Root/Parent).
- **Utilise**: parsing string.
- **Utilisé par**: `begin_segment`.

#### `begin_segment(name: str, trace_header: str | None = None) -> None`
- **Rôle / impact**: démarre un segment; supporte la propagation de trace depuis SQS.
- **Utilise**: `_xray_recorder.begin_segment`.
- **Utilisé par**: `SQSConsumer._process_message_inner`.
- **Effets de bord**: crée un segment X-Ray côté daemon.

#### `end_segment() -> None`
- **Rôle / impact**: termine le segment courant (suppression exceptions).
- **Utilise**: `_xray_recorder.end_segment`.
- **Utilisé par**: `SQSConsumer._process_message_inner` (finally).

#### `current_trace_header() -> str | None`
- **Rôle / impact**: renvoie un header Root=… si un segment est actif (utile propagation).
- **Utilise**: `_xray_recorder.current_segment`.
- **Utilisé par**: pas utilisé actuellement (extension possible).

#### `put_annotation(key: str, value: str) -> None`
- **Rôle / impact**: annoter le segment courant (ex: `fare_event_id`).
- **Utilise**: `seg.put_annotation`.
- **Utilisé par**: `SQSConsumer._process_message_inner`.

---

## 6) “Shared” helpers (email + compat)

### `src/shared/notifier_helpers.py`

#### `AUDIT_REPORT_LAYOUT = "executive"`
- **Rôle / impact**: valeur canonique de layout (parité).
- **Utilisé par**: `sqs_consumer.py`, `process_fare_result_use_case.py`.

#### `get_status_color(status: str) -> str`
- **Rôle / impact**: map status → couleur HTML.
- **Utilise**: logique interne.
- **Utilisé par**:
  - indirect via wrappers privés `_get_status_color` dans `sqs_consumer.py`.
- **Effets de bord**: aucun.

#### `extract_user_name(recipient_email: str, metadata: Optional[dict] = None) -> str`
- **Rôle / impact**: déduit le prénom/nom à afficher dans l’email.
- **Utilise**: `metadata["name"]` sinon transformation de l’email.
- **Utilisé par**:
  - `process_fare_result_use_case.py`
  - wrappers dans `sqs_consumer.py`.

#### `success_email_body_html(...) -> str`
- **Rôle / impact**: construit le body HTML de l’email succès (PDF en pièce jointe).
- **Utilise**: `html.escape`.
- **Utilisé par**:
  - `process_fare_result_use_case.py`
  - wrapper `_success_email_body_html` dans `sqs_consumer.py`.
- **Effets de bord**: aucun (pur).

---

## 7) Domaine (DDD)

### `src/domain/entities/fare_result.py`

#### `class FareResult(BaseModel)`
- **Rôle / impact**: modèle Pydantic décrivant le payload SQS (output de l’intelligence engine).
- **Utilise**: `pydantic.BaseModel`, `Field`.
- **Utilisé par**:
  - `SQSConsumer._process_message_inner` (parse `FareResult(**body)`).
  - `ProcessFareResultUseCase.execute` (décision par `status` + metadata).
  - tests (`test_use_case.py`).
- **Effets de bord**: validation/normalisation Pydantic; peut lever exceptions.

### `src/domain/enums/fare_result_status.py`

#### `class FareResultStatus(str, Enum)`
- **Rôle / impact**: enum canonical des statuts.
- **Utilisé par**: `_normalize_status` dans le use case.

### `src/domain/value_objects/*.py`

#### `EmailRecipient`
- **Rôle / impact**: VO minimal pour typage.
- **Utilisé par**: actuellement non utilisé dans l’orchestration (prévu pour renforcer la couche application).

#### `PdfReport`
- **Rôle / impact**: VO pour transporter un PDF (nom + content-type + bytes).
- **Utilisé par**: non utilisé actuellement (pipeline manipule directement `bytes`).

#### `QrPayload`
- **Rôle / impact**: VO pour texte à encoder en QR.
- **Utilisé par**: non utilisé actuellement (PDF generator manipule des strings).

---

## 8) Application (Use Case)

### `src/application/use_cases/process_fare_result_use_case.py`

#### `@dataclass(frozen=True) class ProcessResult`
- **Rôle / impact**: résultat “adapter-friendly” pour le consumer SQS.
  - `should_delete_message`: si true → suppression SQS (ack).
  - `emails_sent`: compteur pour métriques.
  - `errors`: compteur pour métriques.
- **Utilisé par**: `SQSConsumer._process_message_inner`.

#### `_normalize_status(status: str) -> str`
- **Rôle / impact**: convertit une string en valeur `FareResultStatus` si possible, sinon renvoie la string brute.
- **Utilise**: `FareResultStatus(status)`.
- **Utilisé par**: `execute()` + email erreur.

#### `class ProcessFareResultUseCase`
- **Rôle / impact**: **orchestration métier**: décide “email erreur” vs “audit PDF + email + (optionnel) S3”.
- **Dépendances injectées**:
  - `ses_client`: doit exposer `send_email(...) -> bool` (impl concrète dans `sqs_consumer.SESClient`).
  - `s3_client`: doit exposer `upload_pdf(...) -> str` (impl `sqs_consumer.S3Client`).
  - `metrics_client`: doit exposer `emit(...)` (impl `sqs_consumer.MetricsClient`).
- **Utilisé par**: `SQSConsumer.__init__` (instancié 1 fois).

##### `execute(fare_result, recipient_email, build_report) -> ProcessResult`
- **Rôle / impact**:
  - `parsing_failed|validation_error`: envoie email d’erreur. Si l’envoi échoue → demande retry (ne supprime pas le message).
  - `analysis_complete`: build PDF (via callback `build_report`), (optionnel) upload S3, envoie email avec pièce jointe. En cas d’échec → retry.
  - statut inconnu: warning + suppression (no-op).
- **Utilise**:
  - `_send_error_email`, `_send_success_email`.
  - callback `build_report(fare_result)` fourni par l’adaptateur (consumer).
- **Utilisé par**: `SQSConsumer._process_message_inner`.
- **Effets de bord**: SES send, S3 upload, métriques CloudWatch, logs, X-Ray subsegments.

##### `_send_error_email(fare_result, recipient_email) -> bool`
- **Rôle / impact**: email HTML “error” (threading via `Re:` sur subject).
- **Utilise**: `extract_user_name`, `subsegment("notifier_send_error_email")`, `ses_client.send_email`.
- **Utilisé par**: `execute()` pour `parsing_failed|validation_error`.
- **Effets de bord**: SES send; X-Ray.

##### `_send_success_email(...) -> bool`
- **Rôle / impact**:
  - construit le subject (inclut ref short).
  - génère le PDF via `build_report` avec timeout `PDF_RENDER_TIMEOUT_SECONDS`.
  - upload S3 optionnel.
  - envoie email succès avec PDF attach.
- **Utilise**:
  - `asyncio.wait_for` (timeout).
  - `success_email_body_html`.
  - `metrics_client.emit`.
  - `ses_client.send_email`, `s3_client.upload_pdf`.
- **Utilisé par**: `execute()`.
- **Effets de bord**: CPU/IO (PDF), réseau (S3/SES), métriques, X-Ray.

---

## 9) Infrastructure — Messaging (SQS/SES/S3/CloudWatch)

### `src/infrastructure/messaging/sqs_consumer.py`

#### `class SESClient`
- **Rôle / impact**: adaptateur concret SES.
- **Utilise**: `boto3.client("ses")`, sandbox routing si `SES_SANDBOX_MODE=true`.
- **Utilisé par**: `SQSConsumer` et `ProcessFareResultUseCase` (via injection).
- **Effets de bord**: appels réseau SES.

##### `send_email(recipient, subject, body, attachment_data=None, attachment_name="report.pdf") -> bool`
- **Rôle / impact**:
  - Sans pièce jointe: `send_email` SES.
  - Avec pièce jointe: construit un MIME multipart et envoie via `send_raw_email`.
  - En sandbox: force `actual_recipient=SES_SANDBOX_TEST_EMAIL` et annote le subject.
- **Utilise**: `email.mime.*`, `settings.ses_sender_email`, client SES.
- **Utilisé par**:
  - `ProcessFareResultUseCase`
  - `SQSConsumer._send_parsing_error_email`
  - `SQSConsumer._send_success_email` (ancienne voie compat).
- **Effets de bord**: réseau SES; logs.

#### `class S3Client`
- **Rôle / impact**: adaptateur concret S3 pour archiver le PDF.
- **Utilise**: `boto3.client("s3")`.
- **Utilisé par**: `SQSConsumer` et use case.
- **Effets de bord**: réseau S3.

##### `upload_pdf(pdf_bytes, key, audit_template="") -> str`
- **Rôle / impact**: `put_object` avec `ContentType=application/pdf` + metadata template + (optionnel) SSE-KMS.
- **Utilise**: `settings.s3_pdf_*`.
- **Utilisé par**: `ProcessFareResultUseCase._send_success_email` et `SQSConsumer._send_success_email`.
- **Effets de bord**: écrit un objet S3.
- **Erreurs**: lève `RuntimeError` si S3 désactivé ou bucket manquant.

#### Wrappers “compat”
- `_get_status_color`, `_extract_user_name`, `_success_email_body_html`
- **Rôle / impact**: conserver d’anciens noms internes sans casser des appels (compat/transition).
- **Utilise**: fonctions de `shared.notifier_helpers`.
- **Utilisé par**: méthodes de `SQSConsumer` dans ce fichier.
- **Effets de bord**: aucun.

#### `class MetricsClient`
- **Rôle / impact**: émetteur de métriques CloudWatch (failures “silently dropped”).
- **Utilise**: `boto3.client("cloudwatch")`.
- **Utilisé par**: `SQSConsumer` + use case (injection).
- **Effets de bord**: réseau CloudWatch (si dispo).

##### `emit(metric_name, value, unit="Count") -> None`
- **Rôle / impact**: `put_metric_data` dans le namespace `FairFare/Notifier` + dimension Environment.
- **Utilise**: `_cw.put_metric_data`.
- **Utilisé par**: pipeline (durations, erreurs PDF, succès email, upload S3…).

#### `class SQSConsumer`
- **Rôle / impact**: adaptateur SQS “long polling” + orchestration technique (concurrency, ack).
- **Utilise**:
  - SQS `receive_message/delete_message`
  - `ThreadPoolExecutor` pour boto3 (bloquant)
  - `asyncio` pour concurrence + sémaphore
  - X-Ray segments/annotations
  - `ProcessFareResultUseCase` comme orchestration métier
  - PDF pipeline via `resolve_audit_report_html` + `render_audit_pdf`
- **Utilisé par**: `src/main.py` (lifespan).
- **Effets de bord**: polling SQS, suppression messages, rendu PDF, envois SES, uploads S3, métriques.

##### `__init__(api_metrics: object | None = None)`
- **Rôle / impact**: crée clients AWS (SQS/SES/S3/CW) et instancie le use case.
- **Utilise**: `settings` + `boto3.Session` si profile.
- **Utilisé par**: `lifespan()` dans `main.py`.

##### `start()`
- **Rôle / impact**: démarre la boucle consumer via une tâche asyncio.
- **Utilise**: `asyncio.create_task(self._consume_messages())`.
- **Utilisé par**: `lifespan()` (startup).

##### `stop()`
- **Rôle / impact**: arrête la boucle, annule la tâche, shutdown de l’executor.
- **Utilisé par**: `lifespan()` (shutdown).

##### `_consume_messages()` (boucle)
- **Rôle / impact**: long-poll SQS; si messages, lance `_process_message` en parallèle.
- **Utilise**: `receive_message` via `run_in_executor`.
- **Utilisé par**: `start()`.
- **Effets de bord**: trafic SQS + logs; sleep en cas d’erreur.

##### `_process_message(message)`
- **Rôle / impact**: applique la limite de concurrence via sémaphore.
- **Utilise**: `asyncio.Semaphore`.
- **Utilisé par**: `_consume_messages()`.

##### `_process_message_inner(message)`
- **Rôle / impact**:
  - récupère header X-Ray depuis `MessageAttributes`.
  - démarre segment X-Ray.
  - parse JSON body → `FareResult`.
  - en cas de parsing/validation: envoie email “parsing error” (si possible) puis supprime.
  - sinon, récupère `metadata.sender`; si absent → supprime.
  - appelle `ProcessFareResultUseCase.execute(...)`.
  - selon `ProcessResult`: incrémente métriques in-memory, supprime SQS si demandé.
  - `finally`: émet durée + termine segment X-Ray.
- **Utilise**:
  - `FareResult`, `begin_segment/end_segment/put_annotation`.
  - `self._use_case.execute`.
  - `_delete_message`.
- **Utilisé par**: `_process_message()`.
- **Effets de bord**: SES, SQS delete, métriques, traces, logs.

##### `_inc_messages_processed/_inc_emails_sent/_inc_errors`
- **Rôle / impact**: adapter vers `main.Metrics` si fourni.
- **Utilise**: introspection `getattr(..., "increment_*")`.
- **Utilisé par**: `_process_message_inner`.

##### `_send_error_email(fare_result, recipient_email) -> bool`
- **Rôle / impact**: email HTML erreur (ancienne voie “consumer-centric”).
- **Utilise**: `SESClient.send_email`, `subsegment`.
- **Utilisé par**: potentiellement des chemins legacy; le use case a son propre `_send_error_email`.

##### `_send_parsing_error_email(recipient_email, error_message, raw_body)`
- **Rôle / impact**: email spécial pour payload invalide (FareResult non parseable).
- **Utilise**: `SESClient.send_email`, `subsegment`.
- **Utilisé par**: `_process_message_inner` lors d’exception `FareResult(**body)`.

##### `_build_report(fare_result) -> (html_str, pdf_bytes|None, out_html|None, out_pdf|None)`
- **Rôle / impact**:
  - produit HTML via `resolve_audit_report_html(...)`.
  - rend PDF via `render_audit_pdf(...)` (dans un thread).
  - optionnel: écrit `.html`/`.pdf` sur disque + (optionnel) ouvre le PDF.
- **Utilise**: `resolve_audit_report_html`, `render_audit_pdf`, `settings.report_*`, `asyncio.to_thread`.
- **Utilisé par**:
  - `ProcessFareResultUseCase._send_success_email` via callback `build_report`.
  - `_send_success_email` (voie legacy).
- **Effets de bord**: CPU/IO (PDF), I/O disque, ouverture d’application locale, traces.

##### `_send_success_email(fare_result, recipient_email) -> bool`
- **Rôle / impact**: ancienne impl “tout-en-un” (génère PDF, upload S3, envoie email).
- **Statut**: le pipeline recommandé passe par `ProcessFareResultUseCase`.
- **Utilise**: `_build_report`, `S3Client.upload_pdf`, `SESClient.send_email`.

##### `_delete_message(receipt_handle)`
- **Rôle / impact**: ack définitif: supprime le message de la queue SQS.
- **Utilise**: `sqs_client.delete_message` via `run_in_executor`.
- **Utilisé par**: `_process_message_inner`.
- **Effets de bord**: réseau SQS.

---

## 10) Infrastructure — PDF (HTML → PDF + QR)

### `src/infrastructure/pdf/pdf_generator.py`

#### Vue d’ensemble
Ce module transforme le `fare_result_dict` (principalement `metadata`) en:
- HTML via Jinja2 (`src/templates/audit_executive.html`)
- PDF via Playwright (fallback WeasyPrint)
- QR code (PNG base64) contenant un résumé “executive procurement audit”

Le module contient beaucoup de fonctions utilitaires **pures** (formatage, sélection, labels).

#### `create_qr_code(data: str) -> bytes`
- **Rôle / impact**: génère un QR PNG binaire.
- **Utilise**: `qrcode` + `PIL` via `make_image`.
- **Utilisé par**: `qr_code_to_base64`.
- **Effets de bord**: CPU/mémoire.

#### `qr_code_to_base64(data: str) -> str`
- **Rôle / impact**: QR PNG → base64 (string) pour `data:image/png;base64,...`.
- **Utilise**: `create_qr_code`.
- **Utilisé par**: `generate_audit_report_html_executive` (QR embed).

#### `_render_pdf_playwright(html_content: str) -> bytes`
- **Rôle / impact**: rendu PDF headless via Chromium (Playwright) en A4.
- **Utilise**: `playwright.async_api`, `asyncio.run`, fichier temporaire HTML.
- **Utilisé par**: `render_audit_pdf` (chemin primaire).
- **Effets de bord**: démarre un navigateur headless; I/O fichier temp; CPU; dépendances système.

#### `_render_pdf_weasyprint(html_content: str, base_url: str | None = None) -> bytes`
- **Rôle / impact**: rendu PDF via WeasyPrint.
- **Utilise**: `weasyprint.HTML`.
- **Utilisé par**: `render_audit_pdf` (fallback).

#### Fonctions de formatage (pures)
- `_fmt_date`, `_fmt_datetime`, `_fmt_time`: ISO → affichage.
- `_fmt_duration`: `PT#H#M` → `xhmm`.
- `_fmt_yesno`: bool → Yes/No/N/A.
- `_currency_display`, `_format_price_display`: format prix.
- **Utilise**: uniquement des opérations Python standard (`datetime`, `re`, conversions).
- **Utilisé par**: **exclusivement** des fonctions du même module, principalement `generate_audit_report_html_executive` et `_executive_plain_qr_payload` (via les builders de tableaux/cartes).
- **Effets de bord**: aucun (fonctions pures).

#### Fonctions “business formatting” (pures)
- `_extract_sender_name`: transforme “Name <email>” → “Mr. Name”.
- `_routing`: stops → “Direct/1 Stop/n Stops”.
- `_risk_badge`: OVI → LOW/MODERATE/ELEVATED + style.
- `_observation`: construit une observation lisible à partir des signaux.
- `_rec_reasons`: raisons de recommandation (signaux + scores).
- `_select_top3`: shortlist top offers (dedupe par carrier/stops).
- `build_det_data_for_offers`: prépare une liste de dicts utilisée par le template.
- **Utilise**: lecture de champs `offer`/`flight_details`/`tier2`/`signals` (données `metadata.top_offers[*]`).
- **Utilisé par**: **exclusivement** `generate_audit_report_html_executive` (qui construit le contexte de template et le payload QR).
- **Effets de bord**: aucun (fonctions pures).

#### Fonctions spécifiques “executive report”
- `_executive_policy_line`, `_executive_flex_line`, `_offer_flex_card_line`, `_tier2_field`
- `_executive_footer_note`, `_executive_view_cell`
- `_options_range_letters`
- `_format_sector_meta`
- `_pax_chip_short`, `_offer_ranking_chip`
- `_refundability_compare_cell`, `_refundability_long_for_offer`
- `_change_rule_verbose`, `_change_profile_quick_cell`
- `_compliance_partial_extended`, `_flexibility_assessment_exec`, `_operational_risk_exec`
- `_procurement_narrative_cell`, `_analyst_note_cell`
- `_cabin_fare_family_label`
- `_minimum_stay_cell`, `_maximum_stay_cell`
- **Rôle / impact**: transformer les “offers” en un langage de décision procurement (labels, narratifs).
- **Utilise**: principalement les champs de `metadata.top_offers[*]` (et `tier2`).
- **Utilisé par**: **exclusivement** `generate_audit_report_html_executive` (directement ou via des builders intermédiaires).
- **Effets de bord**: aucun (fonctions pures).

#### QR payload “plain text”

##### `EXECUTIVE_QR_MAX_UTF8_BYTES = 2680`
- **Rôle / impact**: limite de taille pour rester scannable.

##### `_trim_executive_plain_qr_text(body, max_bytes=...) -> str`
- **Rôle / impact**: tronque sans dépasser `max_bytes` en UTF-8.
- **Utilisé par**: `_executive_plain_qr_payload` + fallback QR.

##### `_executive_plain_qr_payload(...) -> str`
- **Rôle / impact**: assemble un rapport textuel compact (audit, route, recommandations, tableau synthèse).
- **Utilisé par**: `generate_audit_report_html_executive`.
- **Effets de bord**: aucun (pur), mais peut logger un warning si trop gros.

#### Assets templates
- `_TEMPLATE_DIR`, `_LOGO_SVG_PATH`, `_LOGO_DATA_URI`
- **Rôle / impact**: calcule le chemin `src/templates` et encode le logo en data URI.
- **Effets de bord**: lit le fichier SVG au chargement du module.

#### `generate_audit_report_html_executive(fare_result_dict: dict) -> str`
- **Rôle / impact**: fonction principale HTML.
  - extrait `metadata.extracted_travel`, `metadata.top_offers`, etc.
  - construit context de template (cartes, tableaux, disclaimer, QR…)
  - rend `templates/audit_executive.html` via Jinja2.
- **Utilise**: toutes les helpers ci-dessus + `jinja2.Template`.
- **Utilisé par**: `resolve_audit_report_html` → consumer/use case.
- **Effets de bord**: lecture du template HTML; CPU.

#### `resolve_audit_report_html(fare_result_dict: dict) -> str`
- **Rôle / impact**: point d’aiguillage “layout → generator”.
- **Utilisé par**: `SQSConsumer._build_report`.

#### `render_audit_pdf(html: str, _fare_result_dict: dict) -> bytes`
- **Rôle / impact**: rend PDF avec fallback.
- **Utilise**: `_render_pdf_playwright` puis `_render_pdf_weasyprint`.
- **Utilisé par**: `SQSConsumer._build_report`.

#### `generate_audit_report_pdf(fare_result_dict: dict) -> bytes`
- **Rôle / impact**: convenience pour produire un PDF depuis un dict.
- **Utilisé par**: pas utilisé actuellement (utile en CLI/tests).

---

## 11) Presentation (schemas)

### `src/presentation/schemas/health_response_schema.py`
- **`class HealthResponse(BaseModel)`**
  - **Rôle / impact**: contrat de réponse `/health`.
  - **Utilisé par**: `main.health_check(response_model=HealthResponse)`.

### `src/presentation/schemas/metrics_response_schema.py`
- **`class MetricsResponse(BaseModel)`**
  - **Rôle / impact**: contrat de réponse `/metrics`.
  - **Utilisé par**: `main.get_metrics(response_model=MetricsResponse)`.

---

## 12) Application — Interfaces (ports)

### `src/application/interfaces/*.py`
Ces Protocols décrivent des ports (clean architecture). Les implémentations concrètes actuelles sont dans `infrastructure/messaging/sqs_consumer.py` et `infrastructure/pdf/pdf_generator.py`.

- `IEmailSender`: `send_email(...) -> bool`
- `IMetricsSink`: `emit(...) -> None`
- `IPdfRenderer`: `render_pdf(...) -> bytes`
- `IPdfStore`: `upload_pdf(...) -> str`
- `ITracer`: begin/end segment + subsegment + annotations

**Note**: aujourd’hui, le code injecte des objets concrets “duck-typed” (Any). Ces interfaces servent de garde-fou et de cible de refactor.

---

## 13) Tests

### `tests/conftest.py`
- **Rôle / impact**: fixtures pytest.
  - `mock_aws_credentials`: injecte env minimales.
  - `mock_boto3_client`: patch `boto3.client` (évite appels AWS).
  - `test_client`: construit `TestClient(app)` depuis `main`.
- **Effets de bord**: patch env + patch boto3.

### `tests/unit/test_main.py`
- **Rôle / impact**: tests contractuels des endpoints `/`, `/health`, `/metrics`.
- **Utilise**: fixture `test_client`.

### `tests/unit/test_use_case.py`
- **Rôle / impact**: tests du use case en isolation (SES/S3/Metrics fakes).
- **Couvre**:
  - statut erreur → email erreur + delete.
  - success → build_report + email + delete.
  - échec d’envoi → retry (ne pas delete).

### `tests/unit/test_consumer.py`
- **Rôle / impact**: test d’ack SQS sur succès via `_process_message_inner`.
- **Utilise**: `MagicMock` sur `sqs_client`, injection d’un fake `_use_case.execute`.

---

## 14) Build & Exécution (ops)

### `Makefile`
- **Rôle / impact**: commandes locales (install/dev/lint/test/build/run).

### `Dockerfile`
- **Rôle / impact**:
  - multi-stage: installe deps python + libs pango/fontconfig nécessaires à WeasyPrint.
  - crée user `ff-user`.
  - healthcheck via `/health`.
- **Effets de bord**: exigences système PDF (libpango…).

### `docker-entrypoint.sh`
- **Rôle / impact**: rend `/tmp` writable (WeasyPrint) puis exécute en `ff-user` via `gosu`.

### `Dockerfile.dev`
- **Rôle / impact**: image dev avec hot-reload (`uvicorn --reload`) + outils (git/curl) + deps PDF.
- **Utilisé par**: workflows de dev container.

---

## 15) Templates & assets (rendu PDF)

### `src/templates/audit_executive.html`
- **Rôle / impact**: template Jinja2 du **rapport PDF “executive”** (2 pages) rendu ensuite par Playwright/WeasyPrint.
- **Utilise**: variables injectées par `src/infrastructure/pdf/pdf_generator.py::generate_audit_report_html_executive`.
- **Utilisé par**:
  - `pdf_generator.generate_audit_report_html_executive` (lecture + rendu)
  - indirectement: `SQSConsumer._build_report` → `resolve_audit_report_html`.
- **Effets de bord**: aucun (c’est un asset); mais conditionne le layout final du PDF.

#### Contrat des variables Jinja (à fournir)
- **Branding / header**:
  - `logo_data_uri` (string, `data:image/svg+xml;base64,...`)
  - `partner_label` (string)
  - `partner_title` (string)
- **Meta report**:
  - `route_title` (string)
  - `route_sub` (string)
  - `travel_date_meta` (string)
  - `passenger_meta` (string)
  - `sector_meta` (string)
  - `cabin_meta` (string)
  - `audit_id` (string)
- **Bloc recommandation**:
  - `rec_title` (string)
  - `rec_bullets` (liste de strings)
  - `policy_status` (string)
  - `flexibility_status` (string)
- **Bloc “ranked offers” (cartes)**:
  - `ranked_offers_sub` (string)
  - `offer_cards` (liste d’objets/dicts `o` avec au minimum):
    - `o.letter`, `o.airline_line`, `o.badge_class`, `o.badge_text`, `o.chips`
    - `o.dep_time`, `o.arr_time`, `o.origin_code`, `o.dest_code`, `o.route_subline`
    - `o.price_display`, `o.flex_line`, `o.footer_note`, `o.recommended`
- **Bloc comparaison (page 1)**:
  - `exec_compare_sub` (string)
  - `offer_heads` (liste de strings)
  - `quick_rows` (liste d’objets/dicts `row` avec `row.label` + `row.cells` (liste))
- **Footer / disclaimer / QR**:
  - `disclaimer_text` (string)
  - `qr_data_uri` (string, `data:image/png;base64,...`)
- **Page 2**:
  - `page2_subtitle` (string)
  - `selected_letter` (string)
  - `offer_heads_det` (liste de strings)
  - `detail_blocks` (liste de dicts `block`):
    - `block.title` (string)
    - `block.rows` (liste de dicts `row`): `row.label` (string) + `row.cells` (liste)

### `src/templates/fairfare-logo-primary.svg`
- **Rôle / impact**: logo FairFare encodé en base64 par `pdf_generator` (data URI) pour intégration offline dans le PDF.
- **Utilise**: n/a.
- **Utilisé par**: `pdf_generator.py` au chargement du module (construction de `_LOGO_DATA_URI`).
- **Effets de bord**: lecture du fichier au chargement de `pdf_generator.py`.

---

## 16) Fichiers “package markers” (`__init__.py`)

### `src/__init__.py`, `src/domain/__init__.py`, `src/domain/entities/__init__.py`, `src/domain/enums/__init__.py`, `src/domain/value_objects/__init__.py`, `src/application/__init__.py`, `src/application/interfaces/__init__.py`, `src/application/use_cases/__init__.py`, `src/infrastructure/__init__.py`, `src/infrastructure/messaging/__init__.py`, `src/infrastructure/pdf/__init__.py`, `src/presentation/__init__.py`, `src/presentation/schemas/__init__.py`, `src/shared/__init__.py`
- **Rôle / impact**: matérialise les packages Python et peut exposer des symboles via imports (ici: principalement vide / ré-export léger).
- **Utilisé par**: import système Python, packaging setuptools.
- **Effets de bord**: typiquement aucun (sauf si du code est exécuté à l’import; ici ce n’est pas le cas pour les `__init__.py` lus dans ce repo).

