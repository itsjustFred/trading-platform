# Test Audit Report

## 1. Executive Summary

Die Plattform ist **weitgehend automatisch testbar und in den Kernpfaden stabil**, aber nicht vollständig release-ready:

- **Backend/Test-Suite:** stabil (`296 passed, 70 skipped`).
- **Frontend-Build:** stabil (`npm run build` erfolgreich).
- **Frontend-Qualitätstor:** **fehlerhaft** (`npm run lint` mit 97 Problemen, Exit 1).
- **E2E-Abdeckung:** vorhanden, aber aktuell eingeschränkt (pytest-e2e standardmäßig skip wegen fehlendem Chromium; separates Node-E2E-Skript schlägt teilweise fehl).
- **Live-/Broker-Pfade:** nur teilweise sicher automatisierbar; produktionsnahe Verifikation bleibt manuell.

## 2. Test Scope

Geprüft wurden:

- Architektur/Entry-Points (`viewer/main.py`, `frontend/src/App.tsx`, `frontend/src/api/*`, `shared/models.py`).
- Automatisierte Tests (pytest gesamt + Marker-Läufe).
- Build/Lint-Checks (Frontend + Ruff).
- API-Smoketests (GET/POST/PUT/DELETE inkl. Task- und WebSocket-Flows).
- Frontend-Routen-Smokes per Playwright.
- Vertrags-/Integrationschecks (Backend OpenAPI ↔ Frontend API Client/Types, Registry/Provider, DATA_DIR, Timeframe-Normalisierung, Risk Sizing vor Execution).
- Betriebsrelevante Runtime-Signale (laufende Prozesse, Ports, Logs, Env-Abhängigkeiten).

Nicht durchgeführt: riskante Live-Aktionen (echte Orders/Broker-Ausführung, produktive Signal-Injektion).

## 3. Test Environment / Voraussetzungen

- OS/Architektur: Linux, **aarch64**
- Python: 3.12.3 (`.venv` vorhanden)
- Node/NPM: Node 24.13.1, npm 11.8.0
- Java: OpenJDK 21 vorhanden
- Dolt: 1.83.0 vorhanden
- Bereits laufend: Backend/Viewer-Prozess(e), Live-Daemon, Frontend auf Port 3000
- Verifiziert erreichbar:
  - `http://127.0.0.1:5556` (API + SPA)
  - `http://127.0.0.1:3000` (Frontend)

## 4. Verwendete Tools und Methoden

- Code-Inspektion: `glob`, `rg`, `view`
- Laufzeit/Build/Test: `pytest`, `npm`, `ruff`, `curl`, Python-Skripte
- Browser-Smokes: Playwright (MCP)
- API-/Contract-Checks:
  - OpenAPI-Fetch (`/openapi.json`)
  - Endpoint-Smokes inkl. Task-Polling/WebSocket
  - AST-basierter Pfadvergleich Frontend API Client ↔ OpenAPI

## 5. Automatisch ausgeführte Tests

### 5.1 Vorhandene Test-Suites

| Suite | Befehl | Ergebnis |
|---|---|---|
| Gesamt | `.venv/bin/python -m pytest tests -q -ra` | **296 passed, 70 skipped, 11 warnings** |
| Integration-Marker | `.venv/bin/python -m pytest tests -m integration -q -ra` | **8 passed, 358 deselected** |
| AI-Code-Marker | `.venv/bin/python -m pytest tests -m ai_code -q -ra` | **3 passed, 1 skipped, 362 deselected** |
| E2E-Marker | `.venv/bin/python -m pytest tests -m e2e -q -ra` | **1 skipped** (Playwright-Browser-Gating) |
| Unit-Marker | `.venv/bin/python -m pytest tests -m unit -q -ra` | **0 selektiert** (Exit 5, 366 deselected) |

Wichtigste Skip-Gründe:

- fehlende lokale OHLCV-Datensätze/-Coverage für viele Strategie-Tests.
- e2e-Setup wählt standardmäßig Chromium; in dieser Umgebung war nur Firefox lauffähig.

### 5.2 API-Checks

Ausgeführt wurde ein API-Smoketest-Set (40 Checks, inkl. dynamischer Backtest-ID, Task-Polling, CRUD-Pfade).

- Ergebnis: **38 OK**, 2 erwartete 422-Validierungsantworten bei absichtlich ungültigen Payloads.
- Verifizierte Kernendpunkte (Beispiele):
  - `/api/strategies`, `/api/backtests`, `/api/backtests/{id}/info|stats|trades|equity`
  - `/api/live/accounts|stats|balances|trades|positions|signals`
  - `/api/services`, `/api/data/overview`, `/api/tasks`, `/api/data/tasks`
  - async Job-Flow `/api/backtests/run` + `/api/backtests/run/{job_id}/status` (completed)
- WebSocket-Check erfolgreich: `/ws/tasks/{job_id}` lieferte Log-Stream.

### 5.3 Frontend-/Playwright-Checks

- Route-Smokes über 11 zentrale Routen (u. a. `/strategies`, `/backtests`, `/live`, `/services`, `/portfolios`, `/lab`, `/data`) mit HTTP 200 und ohne Console-Errors.
- Einzelne `requestfailed`-Events vom Typ `NS_BINDING_ABORTED` beim Routenwechsel auf `/data` (typisch bei Abbruch alter Requests durch Navigation).
- Separates Node-E2E-Skript:
  - direkt `node tests/e2e_frontend.js`: **fehlt `playwright` Modul im Root-Node-Context**
  - mit `NODE_PATH=frontend/node_modules`: **11 passed, 2 failed** (Modal-/Autocomplete-Assertions)

### 5.4 Build-/Startfähigkeits-Checks

| Bereich | Befehl | Ergebnis |
|---|---|---|
| Frontend Build | `cd frontend && npm run build` | **OK** |
| Frontend Lint | `cd frontend && npm run lint` | **FAIL** (97 Probleme, Exit 1) |
| Backend Lint | `.venv/bin/ruff check .` | **FAIL** (535 Fehler) |
| Frontend Dev Start | `npm run dev -- --host 127.0.0.1 --port 3001` | **Startbar** |
| Backend Start | `uvicorn viewer.main:app ... --port 5556` | startbar, aber Port-Konflikte durch bereits laufende Prozesse |

### 5.5 Vertrags-/Integrations-Checks

- **Backend↔Frontend API-Pfade:** AST-basierter Vergleich ergab **keine fehlenden Client-Pfade** gegenüber OpenAPI.
- OpenAPI-Endpoints ohne Frontend-Referenz (8) sind v. a. Alias-/Backend-only-Pfade (z. B. `/api/compare`, `/api/data/validate`, `/api/data/calibrate`).
- **Service-Enums konsistent**:
  - Backend: `running|paused|failed|stopped` (`viewer/main.py`, `shared/models.py`)
  - Frontend: `ServiceStatus` identisch (`frontend/src/api/types.ts`)
- **Risk Sizing vor Order-Submit erzwungen**:
  - `size_live_signal_for_submission()` + `submit_order_with_risk()` in `live_runner/daemon.py`
  - `RiskEngine.calculate_position_size()` vor `adapter.submit_order(...)`
- **DATA_DIR / Timeframe-Normalisierung**:
  - zentrale Auflösung in `shared/constants.py`
  - breite Nutzung von `normalize_timeframe(...)` in `viewer`, `runner`, `data_tools`, `live_runner`.

## 6. Ergebnisse nach Bereich

| Bereich/Komponente | Kategorie | Was geprüft wurde | Ergebnis | Evidenz | Reproduzierbarkeit | Schweregrad | Empfohlene nächste Aktion |
|---|---|---|---|---|---|---|---|
| Backend/API | **1. GETESTET UND OK** | pytest-Kernsuite, API-Smokes, Fehlerpfade, Task-WebSocket | Kernendpunkte stabil, Async-Jobflow funktioniert | `pytest tests -q -ra`; API-Smokes (40 Checks); WS `/ws/tasks/{id}` | Hoch | Mittel | API-Smokes in CI übernehmen |
| Frontend | **2. GETESTET UND FEHLERHAFT** | Route-Smokes, Build, Lint, Node-E2E | Build OK, Route-Smokes OK, aber Lint und E2E-Skript fehlerhaft | `npm run lint` (97 Probleme), `NODE_PATH=... node tests/e2e_frontend.js` (2 Fails) | Hoch | Hoch | Lint-Breaker priorisiert beheben, E2E-Skript stabilisieren |
| Daten-Tools | **3. TEILWEISE GETESTET** | `/api/data/check`, `/api/data/download`, `/api/data/validate` inkl. Task-Logs | Check/Validate laufen; `validate` meldet Datenwarnungen (Gaps/OHLC-Verletzungen) | `POST /api/data/validate` Task `aa2d...`; Logs zeigen 4 Warn-Dateien für AAPL | Hoch | Mittel | Datenqualitäts-Warnungen systematisch triagieren |
| Backtesting | **1. GETESTET UND OK** | Runner-Tests + echter `/api/backtests/run` Smoke + Detailendpunkte | End-to-End Jobstatus completed, Details abrufbar | Task `ce67d270` completed; `/api/backtests/{id}/...` 200 | Hoch | Mittel | Smoke-Backtest als CI-Sanity behalten |
| Strategy Lab / LLM Provider | **3. TEILWEISE GETESTET** | ai_code-Validator-Tests, `/lab` Route-Smoke, Provider-Fabrik-Codeinspektion | Validierungs-Tests OK, UI ladbar; echte Auto-Conversion nicht ausgeführt | `pytest -m ai_code`; `runner/llm_providers/__init__.py` | Mittel | Mittel | dedizierten non-prod E2E-Lab-Test mit explizitem Provider-Flag einführen |
| Live Trading | **4. NICHT AUTOMATISCH TESTBAR** | nur Read-/API-/Model-/Risk-Pfade, keine echten Order-Ausführungen | Produktionsnahe Broker-Ausführung nicht sicher automatisierbar | Live-Endpoints 200; Daemon läuft; echte Orderflows absichtlich nicht ausgelöst | Mittel | Kritisch | Paper-Account-Manuallauf mit klaren Guardrails |
| Signal Bots | **3. TEILWEISE GETESTET** | Registry-Tests, Service-Status/Discovery, API-Read | Registry/Unit-Logik getestet; echte Discord-Ingestion nicht voll E2E-validiert | `tests/test_signal_processor_registry.py`; `/api/services` | Mittel | Hoch | dedizierte Discord-Sandbox-E2E (isolierter Kanal) |
| Scheduler / Services | **3. TEILWEISE GETESTET** | Service-CRUD, Status-Update, Scheduler-Reaktion auf ungültigen Job | Scheduler markiert ungültige Jobs korrekt als `failed`; echter Zeitplanlauf nicht voll verifiziert | erzeugter `scheduled_job` wurde nach Sync von `running` zu `failed` gesetzt | Mittel | Mittel | Cron-Happy-Path (gültiger Job) in isolierter Umgebung minütlich verifizieren |
| Persistence / DB / Filesystem | **1. GETESTET UND OK** | DB-basierte Listen/Details (Backtests/Portfolios/Tasks/Logs), Log-Endpunkte, Datenpfadnutzung | Persistenzpfade und API-Lesezugriffe funktionieren | `/api/backtests`, `/api/portfolios`, `/api/tasks`, `/api/logs` alle 200 | Hoch | Mittel | DB-Migrations-/Recovery-Tests für größere Datenmengen ergänzen |
| E2E-Browser-Pipeline | **5. NICHT GETESTET WEGEN BLOCKER** | pytest-e2e Standardlauf | skip, da Standardbrowser Chromium nicht installiert | `pytest -m e2e` → Skip aus `tests/conftest.py` | Hoch | Mittel | CI auf `--browser firefox` oder Chromium installieren |

## 7. Fehlgeschlagene Tests und Defekte

1. **Frontend-Lint bricht hart** (Hoch)  
   - `npm run lint` → Exit 1, 97 Probleme (u. a. `react-hooks/refs`, `react-hooks/set-state-in-effect` in `TradeChartPage.tsx`).

2. **Node-E2E-Skript nicht out-of-the-box lauffähig** (Mittel)  
   - `node tests/e2e_frontend.js` → `Cannot find module 'playwright'`.
   - Ursache: Playwright als Frontend-Dependency, Skript liegt aber im Repo-Root-Kontext.

3. **Node-E2E-Skript mit 2 fachlichen Fehlschlägen** (Mittel)  
   - Mit `NODE_PATH=frontend/node_modules` ausführbar, aber:
     - „Create Backtest modal opens“ failed (`Engine` nicht gefunden)
     - „Symbol autocomplete works“ failed (GOOG/GOOGL Erwartung)

4. **E2E-Pytest standardmäßig blockiert** (Mittel)  
   - Standardbrowser Chromium fehlt; Firefox ist verfügbar.

5. **Lint/Codehygiene Backend aktuell nicht gatebar** (Mittel)  
   - `ruff check .` → 535 Fehler.

6. **Betriebsauffälligkeit: mehrere langlebige uvicorn-Prozesse ohne Listen-Socket** (Mittel)  
   - Prozesssnapshot zeigt mehrere alte `uvicorn viewer.main` Instanzen, aber nur ein Prozess hört auf 5556.

7. **Betriebsauffälligkeit Live-Daemon: wiederholte Credential-Mismatch-Warnings** (Mittel)  
   - `live-runner.log`: wiederholt „No exact Alpaca credential block match; using first configured block“.

## 8. Nicht getestete oder nicht testbare Bereiche

- Echte Broker-Orderausführung (Alpaca/cTrader) inkl. Fill/Slippage/Reject in realen Konten.
- End-to-End Discord-Ingestion mit realen Bot-Tokens/Channels unter Last.
- Strategy-Lab Vollfluss mit realem externem LLM-Provider (Kosten/Rate-Limits/Auth-abhängig).
- Zeitbasierte Scheduler-Happy-Path-Läufe über reale Cron-Zeitpunkte und Neustarts.
- JForex/Dukascopy-Produktionsläufe mit echten Zugangsdaten/JVM-seitigen Laufzeitbedingungen.
- Produktionsnahe Deployment-/systemd-Szenarien (Restart, Crash-Recovery, Boot-Reihenfolge).

## 9. Warum bestimmte Bereiche nicht testbar waren

- **Sicherheits-/Side-Effect-Risiko:** echte Orders/Live-Signale dürfen im Audit nicht blind ausgelöst werden.
- **Externe Abhängigkeiten:** reale Broker/Discord/LLM/JForex sind netzwerk-, credential- und kontobasiert.
- **Zeitabhängigkeit:** Scheduler-Cron-Verifikation braucht kontrollierte Zeitfenster/isolierte Umgebung.
- **Browser-Tooling-Gap:** pytest-e2e-Standardpfad erwartet Chromium, in dieser Umgebung war Firefox der verfügbare Browser.

## 10. Manuelle Test-Checkliste

| Ziel | Vorbedingungen | Schritte | Erwartetes Ergebnis | Risiko/Hinweis |
|---|---|---|---|---|
| Discord-Bot E2E (Signal→Order intent) | Sandbox-Discord-Server, dedizierter Bot-Token, Paper-Account | 1) Deployment auf `discord` + isolierten Channel setzen 2) Testsignal senden 3) `/api/live/signals/{account}` und Daemon-Logs prüfen | Signal wird korrekt geparst, gefiltert, mit Risk-Metadata versehen | Niemals produktive Channels verwenden |
| Alpaca Paper-Ausführung | Paper API Keys, aktiviertes Paper-Konto | 1) Ein kleines internes Deployment aktivieren 2) kontrolliertes Signal erzeugen 3) Trades/Positions/Balances prüfen | Order mit positiver `qty`, konsistente Persistenz (signal/execution/balance) | Echtkonto strikt ausgeschlossen |
| cTrader Demo-Ausführung | Demo-Token + gültige `CTRADER_*` Variablen | Analog Alpaca-Paper mit kleinstem Risiko | Submit/Execution-Flow ohne Auth-/Session-Fehler | Keine Live-Konten |
| Scheduler Cron Happy-Path | Laufender Daemon + `scheduled_job` Service mit gültigem Cron/Jobtype | 1) Service auf `running` 2) auf nächsten Trigger warten 3) `/api/tasks` + Service-Status prüfen | Task wird erzeugt, läuft durch, Service bleibt `running` | Zeitbasiert, Testfenster einplanen |
| Strategy Lab reale Konvertierung | Gültiger LLM-Provider-Key, nicht-produktive Strategieeingaben | 1) `/lab` öffnen 2) Auto-Convert starten 3) generierten Code + Validator prüfen | gültiger Python-Output, Validator passed | API-Kosten/Rate-Limits beachten |
| WebSocket Live-Updates unter Last | Laufender Viewer + mehrere aktive Tasks | 1) mehrere Tasks starten 2) `/ws/tasks/{id}` parallel abonnieren 3) Abbrüche/Delay messen | keine Stream-Abbrüche, sinnvolle Latenz | Netzwerk/Browser-Limitierungen beachten |
| systemd/Boot-Recovery | Staging-VM (Oracle ARM64) | 1) Dienste per systemd starten 2) Reboot 3) Auto-Start/Port/Health prüfen | Dienste kommen sauber hoch, keine Zombie-Prozesse | Nur Staging, nicht Production-first |

## 11. Risiken / Gaps

- **Hoch:** Frontend-Lint als Qualitätsgate aktuell rot.
- **Hoch:** E2E-Abdeckung faktisch reduziert (pytest e2e skip + Root-Node-Skript fragil).
- **Hoch:** Live-Komponenten stark env-/credential-getrieben; echte Sicherheit nur über manuelle Sandbox-Läufe.
- **Mittel:** Datenqualität (AAPL-Beispiel mit Gaps/OHLC-Warnungen) kann Backtest-Validität beeinflussen.
- **Mittel:** Mehrere alte `uvicorn` Prozesse deuten auf unklare Prozessführung/Service-Hygiene.
- **Mittel:** Wiederholte Alpaca-Credential-Mismatch-Warnings können falsche Kontozuordnung begünstigen.

## 12. Priorisierte Handlungsempfehlungen

1. **P0 – Frontend-Lint beheben und als CI-Pflicht aktiv halten.**  
2. **P0 – E2E-Pipeline konsolidieren:** pytest standardmäßig auf verfügbaren Browser (`--browser firefox`) oder Chromium in CI installieren.  
3. **P1 – Node-E2E-Skript reparieren:** Dependency-Auflösung (lokales `frontend` package script) + Assertions an aktuelle UI anpassen.  
4. **P1 – Live-Daemon Credential-Mapping härten:** eindeutige Account↔Credential-Zuordnung erzwingen, Warning-Spam reduzieren.  
5. **P1 – Prozesshygiene verbessern:** Mehrfach-uvicorn-Instanzen verhindern (systemd unit guard, pid/lock, health-based restart).  
6. **P2 – Datenqualitätswarnungen operationalisieren:** `data_tools.validate` regelmäßig laufen lassen und Warn-Schwellen/Alerts definieren.  
7. **P2 – Manuelle Live-Sandbox-Regression etablieren:** standardisierte Checkliste (Discord, Alpaca Paper, cTrader Demo, Scheduler Cron).  

