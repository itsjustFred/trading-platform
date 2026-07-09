# Copilot Instructions — trading-platform

Diese Regeln sind verbindlich für Änderungen in diesem Repository.

## Architektur-Regeln

1. **API-Source of Truth ist `viewer/main.py`.**  
   Neue/angepasste Endpunkte immer synchron in:
   - `viewer/main.py`
   - `frontend/src/api/client.ts`
   - `frontend/src/api/types.ts`

2. **Kanonische Datenverträge liegen in `shared/models.py`.**  
   API- und Persistenz-Payloads auf bestehende Pydantic-Modelle abstimmen statt ad-hoc Felder einzuführen.

3. **`DATA_DIR` nie hardcoden.**  
   Immer über `shared/constants.py` beziehen (`DATA_DIR`, optionaler Fallback `TRADING_DATA_DIR`).

4. **Timeframes immer normalisieren.**  
   Für Inputs/Metadaten konsequent `normalize_timeframe()` aus `shared/constants.py` verwenden.

5. **Signals vor Execution immer durch Risk Sizing.**  
   Vor Broker-Ausführung muss `RiskEngine.calculate_position_size()` (`risk/engine.py`) laufen.

6. **Background-Workflows als async Jobs.**  
   Start-Endpoints liefern `202 Accepted`; Fortschritt über Task-Status und Streaming (`/api/tasks`, `/api/data/tasks`, `/ws/tasks/{job_id}`).

## Backend-Stack (Python)

- FastAPI + Pydantic v2
- SQLModel (auf SQLAlchemy ORM)
- async-first API- und Task-Orchestrierung
- Type hints durchgängig

## Frontend-Stack

- React 19 + TypeScript (`strict: true`)
- Alle HTTP-API-Calls über `frontend/src/api/client.ts`
- Server-State über TanStack Query
- Tabellen mit AG Grid
- Trading-Charts mit Lightweight Charts

## Signal-Bots

- Neue Prozessoren müssen den bestehenden Processor-Contract einhalten (kompatibel zu den vorhandenen Prozessoren in `signal_bots/*/processor.py`).
- Jeden neuen Prozessor in `signal_bots/registry.py` registrieren:
  - `SIGNAL_PROCESSORS`
  - `SIGNAL_PROCESSOR_OPTIONS`

## Deployment-Kontext

- Zielumgebung: Oracle Cloud, Ubuntu 24.04, ARM64 (aarch64).
- Neue Dependencies und Binaries nur mit bestätigter ARM-Kompatibilität einführen.
- Viewer-Standardport: `5556` (`uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log`).

## Tests

- Backend: `pytest`
- E2E/UI: Playwright
