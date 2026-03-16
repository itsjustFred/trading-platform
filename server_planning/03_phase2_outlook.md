# Phase 2+ Ausblick

> Orientierung, kein detaillierter Plan. Ziel: Phase 1 trifft keine Entscheidungen die spätere Features blockieren.
>
> **Letzte Aktualisierung:** 2026-02-24 — Docker von Phase 2 nach Phase 3 verschoben

---

## Phase 2 — Viewer Upgrade, Logging & Auth

### Viewer Upgrade (Priorität 1)
- Den besseren Viewer aus `custom_lean_integration` (trade_viewer.html, 1491 Zeilen) als Basis nehmen
- TradingView-style UI mit Drawing Tools, Trade-Navigation, Keyboard-Shortcuts, Indicators
- Integrieren in `trading-platform` mit Unterstützung für das Universal Trade Format
- Bestehende API-Endpoints beibehalten/erweitern, Backtest-Picker, Equity-Curve, Trade-Tabelle
- **Details:** Siehe `05_phase2_plan.md`

### Zentrales Logging (Priorität 2)
- Alle Services (Runner, Viewer, Bots) loggen ins gleiche Format (structured JSON logs)
- Log-Aggregation: einfaches File-basiertes Logging mit Rotation, evtl. Loki/Grafana wenn es sich lohnt
- Mindestens: ein Logfile pro Service in `~/logs/` statt verstreut in Projekt-Verzeichnissen

### Auth/Login fürs Frontend (Priorität 3)
- Flask-Login oder einfaches JWT
- Erstmal Single-User (kein Multi-Tenant)
- Viewer hinter Auth-Middleware, API-Keys für programmatischen Zugriff

---

## Phase 3 — Containerisierung & Features

### Docker-Containerisierung
- Runner und Viewer als separate Container (`docker-compose.yml`)
- `~/data/` als Docker Volume gemounted → Container-Code greift über den gleichen Pfad zu
- Vorteil: reproduzierbare Umgebungen, einfacheres Deployment, Isolation der Engines (LEAN braucht z.B. .NET)
- **Verschoben von Phase 2:** Erstmal alles auf der aktuellen Version stabil laufen haben bevor Container dazukommen

### Alerts (Telegram/Discord)
- Notification-Service der Events empfängt: Backtest fertig, Backtest fehlgeschlagen, Live-Bot-Events
- Kanäle: Telegram (primär), Discord (optional)
- Events kommen vom Runner (nach Backtest-Run) und von stockkit-Bots (nach Trade-Execution)

### Risk-Management-Layer
- Position Limits (max $ pro Position, max Positionen gesamt)
- Drawdown Kill-Switch (pausiert Bots automatisch bei X% Drawdown)
- Korrelations-Check (warnt wenn mehrere Positionen stark korreliert)
- Läuft als eigener Service, sitzt zwischen Signal und Order-Execution

### Live-Performance-Dashboard
- Erweitert den Viewer um Live-Daten: offene Positionen, Tages-PnL, Equity-Curve in Echtzeit
- Liest stockkit Bot-State-Files (`bot_state_*.json`) und Trade-History
- WebSocket oder Polling für Updates

### ML-Strategie-Integration
- trading_ml-Repo als Engine-Adapter anbinden
- ML-Modelle können Signale generieren → fließen ins gleiche Trade-Format
- Feature-Store für berechnete Features (Parquet in ~/data/features/)

### Parameter-Sensitivity / Walk-Forward
- Runner CLI erweitern: `--sweep param=start:end:step`
- Erzeugt N Backtests, speichert alle als Multi-Result-JSON
- Viewer: Heatmap / 3D-Surface für Parameter-Sensitivität
- Walk-Forward: Rolling Window Backtests mit re-optimierten Parametern

### Deklaratives Strategie-Format (YAML)
- Für einfache Indikator-Strategien: Entry/Exit-Regeln als YAML statt Python-Code
- Parser der YAML → vectorbt-Signale konvertiert
- Senkt die Einstiegshürde für neue Strategien massiv

```yaml
strategy:
  name: rsi_mean_reversion
  entry:
    - rsi(2) < 10
    - close > sma(200)
  exit:
    - rsi(2) > 70
    - holding_bars > 8
  risk:
    per_trade: 1%
    max_positions: 8
```

### PyneCore-Adapter
- Native PineScript-Ausführung in Python
- Ideal für die ~28 Strategien im Indikatoren-Repo
- Adapter nutzt das gleiche Interface wie LEAN/vectorbt

### LRU-Cache für Remote-Datasets
- Große Datasets (HuggingFace, Dolthub, etc.) nicht komplett lokal halten
- Cache-Manager: lädt benötigte Zeitbereiche on-demand, evicted nach LRU
- catalog.json erweitert um `"storage": "local" | "cached" | "remote"`

### Zweite Disk
- 100 GB Block Storage für ~/data/
- Symlink-Umstellung: `~/data/` → Mount der zweiten Disk
- Kein Code-Change nötig (Phase 1 nutzt schon ~/data/ als Pfad)

### Domain + HTTPS
- Subdomain für den Viewer (z.B. trades.example.com)
- Caddy oder nginx als Reverse Proxy mit auto-SSL
- Auth-Middleware davor (Phase 2)

---

## Architektur-Hinweise für Phase 1

Was Phase 1 beachten muss damit Phase 2+ Features nicht aufwändig nachgerüstet werden müssen:

1. **~/data/ als einziger Datenpfad** — Alle Services lesen/schreiben in ~/data/. Kein Hardcoding von Projektpfaden. Das ermöglicht Symlink auf zweite Disk und spätere Docker Volume Mounts (Phase 3).

2. **Runner als CLI, Viewer als Server** — Getrennte Prozesse, keine enge Kopplung. Ermöglicht spätere separate Docker-Container (Phase 3) und unabhängiges Scaling.

3. **JSON-Files als Kommunikation** — Runner schreibt JSON in ~/data/backtests/, Viewer liest sie. Kein Shared State, kein Message Queue nötig. Wenn später Alerts kommen, triggern sie auf neue Dateien.

4. **Stats-Berechnung in eigenem Modul** — `runner/stats.py` als Pure Function (Trade-Liste rein, Stats raus). Wird von Runner UND Viewer genutzt. Später auch vom Risk-Management-Layer.

5. **Engine-Adapter-Interface stabil halten** — `run()` → `to_universal()` Pattern. Neue Engines (PyneCore, ML, YAML-Parser) implementieren das gleiche Interface. Keine Engine-spezifische Logik im Runner-Core.

6. **Config als Dataclass, nicht als Dict** — Typisierte Config ermöglicht Validierung und IDE-Support. Später einfach um Felder zu erweitern (z.B. `sweep_params` für Parameter-Sensitivity).

7. **Trade-Format nicht ändern** — Das `exits`-Array und `meta`-Dict sind flexibel genug für alle zukünftigen Features. Neue Felder werden als optional hinzugefügt, nie als Pflicht. Bestehende JSONs bleiben lesbar.

8. **Viewer-API RESTful halten** — `/api/backtests`, `/api/backtests/{id}`, `/api/backtests/{id}/trades`. Wenn Auth kommt, wird es als Middleware davor geschaltet. Wenn Live-Daten kommen, werden neue Endpoints hinzugefügt.

9. **Logging mit Python `logging` Modul** — Jetzt schon mit strukturiertem Format (JSON oder zumindest Timestamp + Level + Module). Zentrales Logging ist dann nur noch ein Handler-Wechsel.

10. **Keine Secrets im Code** — Alles via .env oder Environment Variables. Späterer Docker-Compose (Phase 3) liest die gleichen .env-Files.
