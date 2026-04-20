# Verification and Review Report

## 1. Executive Summary
Es wurde ein integriertes Verification-Evidence-System auf Basis der bestehenden SQLite/SQLModel-Infrastruktur umgesetzt und produktiv genutzt. Evidence wird jetzt strukturiert gespeichert, auswertbar gemacht und klar von aktueller automatischer Validierung getrennt.

Zusätzlich wurden sichere Discord-Validierungen (ohne Orderausführung), ein echter Backtest-End-to-End-Flow sowie ein `coderabbit review --plain` durchgeführt. Das CodeRabbit-Feedback wurde geprüft und das valide Finding umgesetzt.

## 2. Implementiertes Verification-Evidence-System
- **Persistenz:** Neue Tabelle `verification_evidence` in `shared/database.py` (SQLModel + SQLite).
- **Kanonische Verträge:** Neue Modelle in `shared/models.py`:
  - `VerificationEvidenceStatus`
  - `VerificationEvidenceRecord`
  - `VerificationEvidenceSummary`
- **DB-Funktionen:**
  - `upsert_verification_evidence(...)`
  - `list_verification_evidence_records(...)`
  - `list_verification_evidence_summary(...)`
- **Auto-Capture für Background-Tasks:** In `upsert_background_task_record(...)` wird bei Final-Status automatisch Evidence geschrieben (u. a. Backtest-Tasks, Service-/Scheduler-Tasks).
- **API-Lesepfade + Write-Pfad in `viewer/main.py`:**
  - `GET /api/verification/evidence`
  - `GET /api/verification/evidence/summary`
  - `POST /api/verification/evidence`
- **Frontend API-Sync:**
  - `frontend/src/api/types.ts` (neue Evidence-Typen)
  - `frontend/src/api/client.ts` (neue Client-Funktionen)

## 3. Architekturentscheidungen
- **Minimal-invasiv:** Bestehende DB, Task-Persistenz und API-Struktur wurden erweitert statt ein separates Subsystem einzuführen.
- **Evidence als Ergänzung, nicht Ersatz:** Das System unterscheidet bewusst zwischen:
  - aktuell automatisch getestet,
  - historisch erfolgreich beobachtet,
  - nicht aktuell validiert,
  - manuell weiter zu testen.
- **Sicherheitsfokus:** Discord-Validierung wurde strikt auf Auth/Connect/Parsing/Risk-Sizing vor Broker begrenzt; keine Broker-Submission, keine Live-Order.

## 4. Welche zeitbasierten/externalen Pfade jetzt historisch nachverfolgbar sind
Nachvollziehbar über `verification_evidence` inkl. Zeitstempel, Status, Metadaten, Fehler, Revision (git SHA), Modus, manueller Validierungspflicht:
- Background-Task-Finals (automatisch), inkl. Backtest-Tasks.
- Service-/Scheduler-Task-Endzustände (über Task-Persistenz).
- Discord-Validierungsläufe (manuell eingetragen, aber strukturiert persistiert).
- Backtest-E2E-Verifikation (manuell eingetragen + automatisch über Task-Ende).

## 5. Discord-Validierung
### 5.1 Was getestet wurde
- Bots aus Konfiguration erkannt: `RAMJET`, `ROCKETFUEL`, `NITRO`.
- Für jeden Bot:
  - Token-Präsenz geprüft (ohne Token offenzulegen).
  - Auth-/Connect-Versuch via `DiscordSignalSource.connect()`.
  - Sichere Parsing-Prüfung über Sample-Message.
  - Risk-Sizing bis vor Broker mit `RiskEngine.calculate_position_size()`.
- Evidence-Einträge wurden für jeden Bot und für einen Suite-Lauf geschrieben.

### 5.2 Was sicher nicht getestet wurde
- Keine echte Broker-Orderausführung.
- Keine Live-Order-Submission via Adapter.
- Keine destruktiven Broker-Aktionen.

### 5.3 Welche Guardrails galten
- `orders_submitted = false`, `broker_submission_attempted = false` in Evidence-Metadaten.
- Validierung ausschließlich auf Connect/Auth/Parse/Risk-Precheck begrenzt.

## 6. Backtest-Validierung
- Echter Flow ausgeführt (`example_sma`, `native`, `AAPL`, `1d`, 2024-01-01 bis 2024-03-31).
- Geprüft:
  - Task-Erstellung (`job_id`: `f0f02235`)
  - Runner-Ausführung (CLI-Run über Backtest-Task)
  - Final-Task-Status `completed`
  - Result-Persistenz in DB (`backtest_id`: `native-example_sma-20260419-173911`)
  - JSON-Artefakt vorhanden (`/mnt/block-volume-data/backtests/example_sma_20260419_173911.trades.json`)
  - API-Layer-Checks für Result/Info erfolgreich
- Evidence wurde sowohl automatisch (Task-Final) als auch als E2E-Verifikations-Record gespeichert.

## 7. CodeRabbit Plain Review Findings
- Ausgeführt: `coderabbit review --plain`
- Findings: **1**
  - `viewer/main.py` im Summary-Endpunkt: konsistente Fehlerbehandlung (ValueError -> HTTP 400 statt 500).

## 8. Welche Findings gefixt wurden
- Das Finding wurde umgesetzt:
  - `get_verification_evidence_summary(...)` fängt jetzt `ValueError` ab und mapped auf `HTTPException(status_code=400, ...)`.

## 9. Welche Findings bewusst nicht übernommen wurden und warum
- Keine weiteren Findings vorhanden.
- Daher keine abgelehnten CodeRabbit-Empfehlungen.

## 10. Verbleibende manuelle Tests
- Discord in echter Zielumgebung mit **gültigen** Tokens erneut prüfen (aktuell Auth-Fehler „Invalid token“ beobachtet).
- End-to-End-Signalfluss mit realen Discord-Nachrichten bis zur sicheren Broker-Precheck-Grenze (ohne Orderausführung) verifizieren.
- Optional: gezielte Scheduler-/Daemon-Szenarien mit realen Zeittriggern über längere Laufzeit beobachten.

## 11. Risiken / Grenzen des Evidence-Ansatzes
- Historische Evidence erhöht Zuversicht, ersetzt aber weder aktuelle automatische Tests noch manuelle Abnahmen.
- Ein „success“ in historischer Evidence garantiert keine aktuelle Funktionsfähigkeit bei geänderter Umgebung/Secrets/externen APIs.
- Aggregierte Summary-Werte sind nur so gut wie die erfassten Runs; fehlende Runs bleiben manuelle Lücken.

## 12. Nächste sinnvolle Schritte
- Discord-Tokens korrigieren/rotieren und sichere Validierung erneut laufen lassen.
- Dashboard/Seite im Frontend ergänzen, die `verification/evidence` + `summary` visualisiert.
- Optional: gezielte Evidence-Hooks für zusätzliche externe Pfade (z. B. daemon/websocket/provider) ergänzen, falls dort noch keine Task-Final-Capture greift.
