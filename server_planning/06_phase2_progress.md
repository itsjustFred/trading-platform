# Phase 2 — Fortschritt

Stand: 2026-02-25

---

## Phase 2a: Viewer Upgrade

### ✅ Task 1: Backend-Endpoints (app.py rewrite)
**Status:** Erledigt  
**Komplexität:** M  
**Dateien:**
- `viewer/app.py` — Komplett neugeschrieben (~310 Zeilen)
- `viewer/comparison.py` — Neu erstellt (Strategy-Vergleich portiert von custom_lean_integration)

**Neue Endpoints:**
| Endpoint | Beschreibung |
|----------|-------------|
| `GET /api/backtests` | Liste aller Backtests |
| `GET /api/backtest/<id>/info` | Metadata für Topbar |
| `GET /api/backtest/<id>/trades` | Alle Trades im Viewer-Format |
| `GET /api/backtest/<id>/trade/<nr>` | Einzelner Trade + OHLCV-Kontext (60 Bars davor, 25 danach) |
| `GET /api/backtest/<id>/equity` | Equity Curve |
| `GET /api/backtest/<id>/stats` | Statistiken (via runner/stats.py) |
| `GET /api/backtest/<id>/ohlcv` | Rohe OHLCV-Daten |
| `GET /api/compare?ids=id1,id2,...` | Strategy-Vergleich mit Combined Equity, Drawdown, Korrelationen |

**Getestet:** Alle Endpoints via Flask test_client verifiziert.

---

### ✅ Task 2: HTML/JS Migration
**Status:** Erledigt  
**Komplexität:** L  
**Dateien:**
- `viewer/static/index.html` — Komplett ersetzt (333 → 1536 Zeilen)
- `viewer/static/index.html.bak` — Backup der alten Version

**Basis:** `custom_lean_integration/static/trade_viewer.html`  
**Anpassungen:** API-Layer komplett auf Universal Trade Format umgeschrieben, alle Endpoints neu.

---

### ✅ Task 3: Equity Curve Panel
**Status:** Erledigt  
**Komplexität:** M  
**Details:**
- Lightweight-charts LineSeries unter dem Hauptchart
- Orange Marker zeigt aktuelle Trade-Position
- Zeitachse synchronisiert mit Hauptchart
- Automatisches Resize

---

### ✅ Task 4: Trade-Tabelle
**Status:** Erledigt  
**Komplexität:** S  
**Details:**
- Sortierbar nach allen Spalten (Nr, Symbol, Direction, Entry, Exit, PnL, Duration)
- Klick auf Zeile navigiert zum Trade
- Aktive Zeile hervorgehoben
- Farbcodierung: Grün = Gewinn, Rot = Verlust

---

### ✅ Task 5: Stats Panel im Sidebar
**Status:** Erledigt  
**Komplexität:** S  
**Details:**
- Grid-Layout im rechten Sidebar
- Zeigt: Total PnL, Win Rate, Sharpe, Sortino, Profit Factor, Max DD, Avg Winner/Loser, etc.
- Stats werden beim Backtest-Wechsel aktualisiert

---

### ✅ Task 6: Multi-Symbol Support
**Status:** Erledigt  
**Komplexität:** M  
**Details:**
- Backend: `/api/backtest/<id>/trade/<nr>` liefert OHLCV des jeweiligen Trade-Symbols
- Frontend: Chart wechselt automatisch Symbol beim Trade-Navigieren
- Infobar zeigt aktuelles Symbol
- Getestet mit `stockkit_ram_jet` (CRWD, AXSM, APLD)
- **Hinweis:** AXSM hat keine OHLCV-Daten → Chart zeigt keine Candles (graceful fallback)

---

### ✅ Task 7: Partial Exits + Indikator-Format
**Status:** Erledigt  
**Komplexität:** M  

**Partial Exits:**
- Jeder Exit als separater Marker auf dem Chart
- Unterschiedliche Farben für TP vs. andere Exits
- Getestet mit `stockkit_ram_jet` Trade 3 (APLD: 2 Exits — TP1 + Supertrend Exit)

**Indikator-Format (definiert, bereit für Nutzung):**
- `IndicatorSeries` Dataclass in `shared/models.py` hinzugefügt
- `indicators` Feld in `BacktestResult` (optional, default = [])
- Frontend rendert `type: "overlay"` Indikatoren auf dem Hauptchart
- `type: "panel"` Indikatoren werden gespeichert aber erst in Phase 3 angezeigt

**Format-Spezifikation:**
```json
{
  "indicators": [
    {
      "name": "EMA 20",
      "type": "overlay",
      "style": {"color": "#58a6ff", "width": 1, "dash": "dashed"},
      "data": [{"time": "2021-01-01T10:00:00Z", "value": 121.5}]
    }
  ]
}
```

---

### ✅ Task 8: Comparison Mode
**Status:** Erledigt  
**Komplexität:** L  
**Details:**
- Portiert von `custom_lean_integration/compare_strategies.py` + `view_compare.py`
- Backend `/api/compare` liefert: Equity Curves, Combined 50/50 Equity, Drawdown-Serien, Korrelationen
- Frontend: Vergleichsansicht ersetzt Chart-Area bei 2+ ausgewählten Backtests
  - Combined Portfolio Stat-Cards (Sharpe, Sortino, MaxDD, Net Return, Correlations)
  - Side-by-side Stats-Vergleichstabelle
  - Überlagerte Equity Curves (farbcodiert pro Strategie + Combined-Linie)
  - Drawdown-Vergleichschart
- Getestet: 2-way und 3-way Vergleiche funktionieren

---

### ✅ Task 9: Testing + Dokumentation
**Status:** Erledigt  
**Komplexität:** S  
**Details:**
- Alle API-Endpoints via test_client verifiziert
- Multi-Symbol-Backtest getestet
- Partial Exits verifiziert
- Comparison (2-way, 3-way) getestet
- Indikator-Format Round-Trip getestet
- Fortschrittsdokument erstellt

---

## Features im neuen Viewer

### Implementiert
- ✅ TradingView-Style Dark Theme
- ✅ Drawing Tools (Trendline, Horizontal, Ray, Rectangle, Fibonacci, Measure)
- ✅ Drawing Persistence via localStorage (pro Backtest-ID)
- ✅ Trade Navigation (Prev/Next/First/Last + Arrow Keys)
- ✅ Backtest Picker Dropdown
- ✅ Equity Curve Chart (synchronisiert)
- ✅ Sortierbare Trade-Tabelle
- ✅ Stats Panel (Sidebar)
- ✅ Multi-Symbol Auto-Switch
- ✅ Partial Exit Markers
- ✅ Indikator Overlays (Format definiert, Rendering bereit)
- ✅ Strategy-Vergleichsmodus
- ✅ Keyboard Shortcuts (Pfeiltasten, 1-5 Tools, M, T, Esc)
- ✅ Color Picker für Drawing Tools

### Aufgeschoben (Phase 3+)
- ⬜ Panel-Indikatoren (RSI, MACD) — Sub-Charts
- ⬜ Drawing Persistence per API (aktuell nur localStorage)
- ⬜ Overview-Modus
- ⬜ Trade-Filter/Sort (Multi-Symbol)
- ⬜ Screenshot-Export

---

## Geänderte Dateien (Gesamtübersicht)

| Datei | Aktion | Zeilen |
|-------|--------|--------|
| `viewer/app.py` | Neugeschrieben | ~310 |
| `viewer/comparison.py` | Neu erstellt | ~110 |
| `viewer/static/index.html` | Ersetzt | 1536 |
| `viewer/static/index.html.bak` | Backup | 333 |
| `shared/models.py` | Erweitert (+IndicatorSeries) | ~330 |

---

## Offene Punkte / Hinweise

1. **Browser-Test steht noch aus** — Server läuft auf Port 5556, manueller Test empfohlen
2. **AXSM OHLCV fehlt** — Kein Parquet in ~/data/ohlcv/ vorhanden, Viewer zeigt leeren Chart (OK)
3. **Equity Curve bei stockkit_ram_jet** — Kein equity_curve im JSON → Equity-Panel bleibt leer (OK)
4. **Indikator-Testdaten** — Keine Backtests mit Indikatoren vorhanden, Frontend-Rendering nicht getestet
5. **Combined Portfolio 50/50** — Funktioniert, aber Korrelation = 1.0 bei Testdaten (gleiche Strategie)

---

## Phase 2b: Zentrales Logging

### ✅ Task 9: Logging-Modul erstellen
**Status:** Erledigt  
**Komplexität:** S  
**Dateien:**
- `shared/logging.py` — Neu erstellt (~85 Zeilen)

**Features:**
- `get_logger(service)` Factory — erzeugt Logger mit JSON-Output
- `JsonFormatter` — Structured JSON mit timestamp, level, service, module, message, data
- `RotatingFileHandler` — 10 MB max, 5 Backup-Dateien
- Dual-Output: File (`~/logs/<service>.log`) + Stdout
- Konfigurierbar via `LOG_LEVEL` und `LOG_DIR` Env-Vars

---

### ✅ Task 10: Log-Verzeichnis einrichten
**Status:** Erledigt  
**Komplexität:** S  
**Details:**
- `~/logs/` wird automatisch durch das Logging-Modul angelegt (`mkdir -p`)
- Ein Logfile pro Service: `viewer.log`, `runner.log`
- Rotation: 10 MB, max 5 Backup-Dateien

---

### ✅ Task 11: Services umstellen
**Status:** Erledigt  
**Komplexität:** S  
**Dateien:**
- `viewer/app.py` — `logging.basicConfig()` ersetzt durch `get_logger("viewer")`
- `runner/cli.py` — `logging.basicConfig()` ersetzt durch `get_logger("runner")`

**Details:**
- Bestehende `logger.info/warning/error` Aufrufe bleiben kompatibel
- Ungenutzter `import logging` entfernt
- Beide Module importieren korrekt (verifiziert)

---

### ✅ Task 12: stockkit Logging-Guide
**Status:** Erledigt  
**Komplexität:** S  
**Dateien:**
- `docs/logging_guide.md` — Neu erstellt

**Inhalt:**
- Schnellstart-Anleitung
- Log-Format-Dokumentation (JSON-Struktur)
- Konfiguration (Env-Vars)
- File-Rotation Erklärung
- Schritt-für-Schritt Einbindung in stockkit_ai_bot
- Beispiele: print() → logger, Fehlerbehandlung

---

## Geänderte Dateien Phase 2b

| Datei | Aktion | Zeilen |
|-------|--------|--------|
| `shared/logging.py` | Neu erstellt | ~85 |
| `docs/logging_guide.md` | Neu erstellt | ~95 |
| `viewer/app.py` | Editiert (2 Zeilen ersetzt) | ~405 |
| `runner/cli.py` | Editiert (2 Zeilen ersetzt) | ~130 |

---

## Phase 2c: Noch nicht begonnen
- Phase 2c: Frontend Auth
