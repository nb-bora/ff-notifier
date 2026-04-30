# Notifier

Microservice **Notifier** (FairFare) – copie fonctionnelle de `ff-notifier`, mais structurée en **DDD + Clean Architecture** (comme `Ingestion`).

## Workflows (parité `ff-notifier`)

- **SQS FareResult** → consumer long-polling
- **Branching** par `status`:
  - `parsing_failed` / `validation_error` → email d’erreur via SES
  - `analysis_complete` → génération du PDF d’audit (QR inclus) → email SES avec pièce jointe
- **Optionnel**:
  - Upload PDF sur **S3**
  - Templates/settings via **SSM Parameter Store**
  - **CloudWatch custom metrics**
  - **AWS X-Ray** (segments/subsegments + propagation du trace header)

## Lancer en local (Windows / PowerShell)

```powershell
cd C:\Users\user\Pictures\FairFareHQ\Notifier
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
# édite .env
python run.py
```

Ou directement:

```powershell
python -m uvicorn main:app --app-dir src --host 0.0.0.0 --port 8000 --reload
```

## Endpoints

- `GET /` info service
- `GET /health` health + config + métriques in-memory
- `GET /metrics` métriques in-memory
