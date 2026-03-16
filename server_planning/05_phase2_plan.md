# Phase 2 — Viewer Upgrade, Logging & Auth

> Ziel: Der Trade-Viewer sieht gut aus und funktioniert smooth (basierend auf dem custom_lean_integration Viewer), alle Services loggen einheitlich, und das Frontend ist hinter einem Login geschützt.

---

## 1. Viewer Upgrade (Priorität 1)

### 1.1 Ausgangslage

**Aktueller Viewer in `trading-platform`** (`viewer/static/index.html`, 333 Zeilen):
- Einfaches Grid-Layout mit Sidebar
- Dropdown-Backtest-Picker
- Candle-Chart + Equity-Curve (synced)
- Trade-Tabelle mit Click-to-Highlight
- Stats-Grid in Sidebar
- **Problem:** Sieht nicht gut aus, kein poliertes UI, keine Drawing Tools, keine Trade-Navigation, keine Keyboard-Shortcuts

**Besserer Viewer in `custom_lean_integration`** (`static/trade_viewer.html`, 1491 Zeilen):
- TradingView-style Dark Theme (CSS Custom Properties, poliert)
- Top Bar mit Mode-Switching (Trades / Overview)
- Linke Toolbar mit Drawing Tools (Trendline, Horizontal, Ray, Rectangle, Fibonacci, Measure)
- Rechte Sidebar mit Trade-Liste (ein-/ausklappbar)
- Trade Info Bar mit Details zum aktuellen Trade
- Chart Legend mit OHLCV-Werten bei Crosshair-Bewegung
- Trade-Navigation (Prev/Next/First/Last mit Buttons + Pfeiltasten)
- Keyboard Shortcuts (1-5 für Tools, T für Trade-Liste, O für Overview, M für Measure)
- Screenshot-Export
- Volume-Series
- Indikator-Overlays (EMA, SuperTrend, Swing Highs/Lows)
- Consolidation Zones
- Chart-Typ-Wechsel (Candle/Bar)
- Canvas-basierte Zeichnungen mit Selection, Drag-Move, Undo, Color/Style/Width
- Loading-Overlay, Toast-Notifications, Bottom Bar

**Problem:** Der custom_lean_integration Viewer ist auf LEAN-Output hardcoded — importiert aus `view_trades.py` (Indikatoren, Trade-Parsing), unterstützt nur ein einzelnes Symbol pro Session, kennt kein Universal Trade Format.

### 1.2 Migrations-Strategie

**Basis:** `trade_viewer.html` aus `custom_lean_integration` nehmen (UI/UX, Drawing Tools, Layout, CSS).

**Anpassen:**
1. **Frontend:** Universal Trade Format unterstützen (exits-Array, Multi-Symbol, Meta-Block)
2. **Frontend:** Backtest-Picker integrieren (wie aktuell im trading-platform Viewer)
3. **Frontend:** Equity-Curve als dedizierter Chart-Bereich (synced mit Hauptchart)
4. **Frontend:** Trade-Tabelle (sortierbar, aus aktuellem Viewer übernehmen aber mit besserem Styling)
5. **Frontend:** API-Calls anpassen auf die bestehenden Endpoints von `viewer/app.py`
6. **Backend:** Neue Endpoints für Trade-Navigation (einzelne Trades mit OHLCV-Kontext)
7. **Backend:** LEAN-spezifische Imports entfernen, stattdessen Universal Format direkt nutzen

**Beibehalten:**
- Komplettes Drawing-Tool-System (Trendline, Horizontal, Ray, Rectangle, Fibonacci, Measure)
- TradingView-style Dark Theme + alle CSS
- Trade-Navigation (Prev/Next mit Pfeiltasten)
- Chart Legend, Indicator Labels, Volume Series
- Mode-Switching (Trade-Ansicht / Overview-Ansicht)
- Keyboard Shortcuts
- Screenshot-Export
- Toast-Notifications, Loading-Overlay
- Right Sidebar mit Trade-Liste

**Entfernen/Ersetzen:**
- Import von `view_trades.py` (EMA, ATR, SMA, StructureEngine, etc.) — nicht mehr nötig
- Hardcoded LEAN-Format (trade_to_dict mit nr/direction/entry_time/exit_time) → Universal Format
- Hardcoded Config-Loading (load_config, strategy_meta.json) → Meta aus Universal JSON
- Indikatoren-Berechnung im Backend → erstmal ohne Indikatoren, später optional

### 1.3 Backend-Änderungen (`viewer/app.py`)

**Bestehende Endpoints beibehalten:**
- `GET /api/backtests` — Liste aller Backtests (bleibt)
- `GET /api/backtest/<id>` — Backtest laden (bleibt)
- `GET /api/backtest/<id>/stats` — Stats berechnen (bleibt)
- `GET /api/backtest/<id>/ohlcv` — OHLCV-Daten (bleibt)
- `GET /api/compare` — Vergleich (bleibt)

**Neue Endpoints:**
- `GET /api/backtest/<id>/trades` — Alle Trades im Viewer-Format (flattened für Navigation)
- `GET /api/backtest/<id>/trade/<trade_nr>` — Einzelner Trade mit OHLCV-Kontext (Bars vor/nach Trade), analog zu `/api/trade/<nr>` im alten Viewer
- `GET /api/backtest/<id>/fullchart` — Voller Chart mit allen Trade-Markern (für Overview-Modus)
- `GET /api/backtest/<id>/info` — Backtest-Metadata fürs Top-Bar-Label

**Anpassungen:**
- Trade-Daten aus Universal Format lesen (exits-Array → Entry/Exit-Zeitpunkte)
- OHLCV-Kontext: Bars vor Entry und nach letztem Exit ausschneiden (BARS_BEFORE=60, BARS_AFTER=25)
- Multi-Symbol: Wenn Backtest mehrere Symbole hat, OHLCV für das Symbol des aktuellen Trades laden

### 1.4 Frontend-Änderungen (`viewer/static/index.html`)

**Neu schreiben basierend auf `trade_viewer.html`:**

1. **Backtest-Picker im Top Bar** — Dropdown links neben dem Symbol-Label, lädt `/api/backtests`, wechselt Backtest on-change
2. **Universal Trade Format Support:**
   - Trade-Liste: ID, Symbol, Direction, Entry, Exits (letzter Exit als Hauptzeit), PnL, PnL%, R-Multiple
   - Trade Info Bar: Symbol + Direction + Entry + Exit + PnL + Exit-Reason + Duration
   - Partial Exits: Mehrere Exit-Marker pro Trade (aus exits-Array)
3. **Equity-Curve Panel:**
   - Eigener Chart-Bereich unter dem Hauptchart (ca. 20% Höhe)
   - Synced Time-Scale mit Hauptchart
   - Linie zeigt Equity-Verlauf (aus equity_curve-Block oder berechnet aus Trades)
4. **Trade-Tabelle:**
   - Unter der Equity-Curve (ca. 20% Höhe, scrollbar)
   - Spalten: #, Symbol, Dir, Entry Time, Exit Time, PnL, PnL%, R-Multiple
   - Sortierbar per Click auf Header
   - Click auf Zeile → navigiert zum Trade im Chart
   - Win/Loss-Coloring (grüner/roter Rand links)
5. **Stats in Sidebar:**
   - Stats-Grid mit Cards (Total Trades, Win Rate, PnL, Profit Factor, Sharpe, Sortino, MaxDD, etc.)
   - Geladen von `/api/backtest/<id>/stats`

### 1.5 Offene Entscheidungen

| Frage | Optionen | Empfehlung |
|-------|----------|------------|
| **Indikatoren im neuen Viewer?** | A) Ohne Indikatoren (erstmal) B) Generische Indikator-Anzeige wenn im JSON vorhanden | **A** — Indikatoren kamen aus dem LEAN-spezifischen Backend. Ohne LEAN-Import gibt es keine Berechnung. Später kann ein optionaler Indikator-Block im Universal Format ergänzt werden. |
| **Layout: 3 Panels vertikal oder Tabs?** | A) Chart + Equity + Tabelle gestapelt B) Tabs (Chart / Equity / Tabelle) | **A** — Drei Panels gestapelt wie im aktuellen trading-platform Viewer, aber mit besserem Styling. Die Trade-Navigation per Pfeiltasten zeigt den Hauptchart, Equity + Tabelle synchronisieren mit. |
| **Sidebar: Stats oder Trade-Liste?** | A) Beides in einer Sidebar (Tabs) B) Stats oben, Trade-Liste unten C) Trade-Liste rechts (ein-/ausklappbar wie im original), Stats im Tabelle-Bereich | **C** — Trade-Liste als rechte Sidebar beibehalten (wie im original Viewer), Stats als Cards in die Sidebar integrieren (über der Trade-Liste). |

---

## 2. Aufgabenliste mit Reihenfolge

### Phase 2a — Viewer Upgrade (Priorität 1)

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 1 | **Viewer-Backend erweitern** — Neue Endpoints: `/api/backtest/<id>/trades`, `/api/backtest/<id>/trade/<nr>` (mit OHLCV-Kontext), `/api/backtest/<id>/fullchart`, `/api/backtest/<id>/info`. Universal Format → Viewer-Format Mapping. | M | — |
| 2 | **trade_viewer.html migrieren** — Kopie von `custom_lean_integration/static/trade_viewer.html` ins Monorepo. LEAN-spezifische API-Calls durch neue Endpoints ersetzen. Backtest-Picker im Top Bar ergänzen. Trade-Format auf Universal Format umstellen (exits-Array, Symbol pro Trade). | L | 1 |
| 3 | **Equity-Curve Panel integrieren** — Eigener lightweight-charts Bereich unter dem Hauptchart. Synced Time-Scale. Daten aus `equity_curve`-Block oder berechnet aus Trades. | S | 2 |
| 4 | **Trade-Tabelle integrieren** — Sortierbare HTML-Tabelle unter Equity-Curve. Spalten: #, Symbol, Dir, Entry, Exit, PnL, PnL%, R-Multiple. Click navigiert zum Trade. Win/Loss-Coloring. | S | 2 |
| 5 | **Stats-Panel in Sidebar** — Stats-Grid (Cards) über der Trade-Liste in der rechten Sidebar. Lädt von `/api/backtest/<id>/stats`. | S | 2 |
| 6 | **Multi-Symbol Support** — Wenn Backtest mehrere Symbole hat: OHLCV pro Symbol laden, Chart wechselt mit Trade-Navigation. Symbol-Anzeige im Top Bar aktualisieren. | M | 2 |
| 7 | **Partial Exit Markers** — Mehrere Exit-Marker pro Trade aus dem exits-Array. Jeder Exit bekommt eigenen Marker + Price-Line. Exit-Reason pro Exit anzeigen. | S | 2 |
| 8 | **Regressionstests** — Manuelles Testing: Backtest-Picker funktioniert, Trade-Navigation (Pfeiltasten + Buttons), Overview-Modus, Drawing Tools, Screenshot, Equity-Curve sync, Trade-Tabelle click-to-navigate. | S | 3, 4, 5, 6, 7 |

### Phase 2b — Zentrales Logging (Priorität 2)

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 9 | **Logging-Modul erstellen** — `shared/logging.py`: Einheitliche Logger-Factory die structured JSON logs erzeugt. Konfigurierbar (Log-Level, Output: File/Stdout). Log-Rotation mit `logging.handlers.RotatingFileHandler`. | S | — |
| 10 | **Log-Verzeichnis einrichten** — `~/logs/` anlegen. Ein Logfile pro Service: `viewer.log`, `runner.log`. Rotation bei 10 MB, max 5 Dateien. | S | 9 |
| 11 | **Services umstellen** — `viewer/app.py` und `runner/cli.py` auf das neue Logging-Modul umstellen. Bestehende `print()`/`logging.basicConfig()` ersetzen. | S | 9, 10 |
| 12 | **stockkit-Bots Logging** (optional) — Logging-Guide schreiben wie stockkit_ai_bot das Logging-Modul einbinden kann. Kein Code-Change am laufenden Bot nötig, nur Dokumentation für späteren Einbau. | S | 9 |

### Phase 2c — Auth fürs Frontend (Priorität 3)

| # | Task | Komplexität | Abhängigkeit |
|---|------|-------------|-------------|
| 13 | **Auth-Modul erstellen** — `shared/auth.py`: Simple Login mit Flask-Login oder JWT. Single-User Credentials aus `.env` (Username/Passwort-Hash). Session-basiert (Cookie). | M | — |
| 14 | **Login-Page** — Einfache HTML Login-Seite (`viewer/static/login.html`). Dark Theme passend zum Viewer. Redirect nach erfolgreichem Login. | S | 13 |
| 15 | **Viewer hinter Auth** — Alle Viewer-Routen und API-Endpoints hinter Auth-Middleware. `/api/*` akzeptiert auch API-Key (Header `X-API-Key`) für programmatischen Zugriff. Login-Page ist die einzige öffentliche Route. | S | 13, 14 |
| 16 | **API-Key Management** — API-Key in `.env` konfigurierbar. Einfache Validierung im Auth-Middleware. Doku im README wie man den API-Key nutzt. | S | 15 |

---

## 3. Reihenfolge-Diagramm

```
Phase 2a — Viewer Upgrade (Priorität 1)
1 (Backend Endpoints)
└──→ 2 (HTML migrieren)  ──→ 3 (Equity-Curve)
                          ├──→ 4 (Trade-Tabelle)
                          ├──→ 5 (Stats-Panel)
                          ├──→ 6 (Multi-Symbol)
                          └──→ 7 (Partial Exits)
                                    └──→ 8 (Testing)

Phase 2b — Logging (Priorität 2, parallel zu 2a möglich)
9 (Logging-Modul) ──→ 10 (Log-Dir) ──→ 11 (Services umstellen)
                                    └──→ 12 (stockkit Guide)

Phase 2c — Auth (Priorität 3, nach 2a)
13 (Auth-Modul) ──→ 14 (Login-Page) ──→ 15 (Viewer hinter Auth)
                                    └──→ 16 (API-Key)
```

---

## 4. Technische Details

### 4.1 Trade-Format Mapping (Universal → Viewer)

Der Viewer braucht ein flacheres Format für die Navigation. Das Backend mapped:

```python
# Universal Format (aus JSON)
{
    "id": 1,
    "symbol": "AAPL",
    "direction": "LONG",
    "entry_time": "2021-03-15T10:30:00Z",
    "entry_price": 121.50,
    "quantity": 82,
    "exits": [
        {"time": "2021-03-18T15:45:00Z", "price": 124.80, "quantity": 82, "reason": "rsi_target"}
    ],
    "pnl": 270.60
}

# → Viewer Format (für Frontend)
{
    "nr": 1,
    "symbol": "AAPL",
    "direction": "LONG",
    "entry_time": 1615801800,       # Unix timestamp
    "entry_price": 121.50,
    "exit_time": 1616082300,        # Letzter Exit
    "exit_price": 124.80,           # Letzter Exit
    "quantity": 82,
    "pnl": 270.60,
    "pnl_pct": 2.72,
    "exit_reason": "rsi_target",
    "exits": [                      # Alle Exits für Partial-Exit-Marker
        {"time": 1616082300, "price": 124.80, "quantity": 82, "reason": "rsi_target"}
    ]
}
```

### 4.2 OHLCV-Kontext für Trade-Ansicht

Für die Trade-by-Trade-Navigation (wie im custom_lean_integration Viewer):

```python
BARS_BEFORE = 60  # Bars vor dem Entry
BARS_AFTER = 25   # Bars nach dem letzten Exit

# Endpoint: GET /api/backtest/<id>/trade/<nr>
# 1. Trade aus Universal Format lesen
# 2. Symbol + Timeframe aus Backtest-Meta
# 3. OHLCV aus ~/data/ohlcv/ laden (Parquet)
# 4. Zeitbereich ausschneiden: entry_time - BARS_BEFORE ... last_exit + BARS_AFTER
# 5. Response: { trade, ohlcv, markers }
```

### 4.3 Logging-Format

```json
{
    "timestamp": "2026-02-24T14:30:22.123Z",
    "level": "INFO",
    "service": "viewer",
    "module": "app",
    "message": "Backtest loaded",
    "data": {"backtest_id": "mean_reversion_rsi2_lean_20260224-143022", "trade_count": 142}
}
```

### 4.4 Auth-Flow

```
Browser → GET /                   → 302 Redirect → /login
Browser → GET /login              → Login-Page (HTML)
Browser → POST /login             → Validate credentials → Set session cookie → 302 → /
Browser → GET /api/backtests      → Session cookie valid? → 200 JSON
Script  → GET /api/backtests      → X-API-Key header valid? → 200 JSON
         (kein Cookie/Key)        → 401 Unauthorized
```

---

## 5. Was passiert mit den existierenden Dateien

| Datei | Aktion |
|-------|--------|
| `viewer/static/index.html` (333 Zeilen) | **Ersetzt** durch migrierte `trade_viewer.html` |
| `viewer/app.py` | **Erweitert** um neue Endpoints + Auth-Middleware |
| `custom_lean_integration/static/trade_viewer.html` | **Quelle** — wird kopiert und angepasst, Original bleibt unverändert |
| `custom_lean_integration/view_trades_web.py` | **Referenz** — API-Pattern werden übernommen, Datei bleibt unverändert |
| `shared/logging.py` | **Neu** |
| `shared/auth.py` | **Neu** |
| `viewer/static/login.html` | **Neu** |

---

## 6. Nicht in Phase 2

| Feature | Warum nicht | Wann |
|---------|------------|------|
| **Docker** | Erstmal alles auf der aktuellen Version stabil laufen haben bevor Container dazukommen | Phase 3 |
| **Alerts (Telegram/Discord)** | Kein dringender Bedarf, Viewer reicht für Monitoring | Phase 3 |
| **Risk-Management** | Braucht stabiles Live-Trading als Voraussetzung | Phase 3 |
| **ML-Integration** | trading_ml ist pausiert, kein akuter Bedarf | Phase 3+ |
| **Parameter-Sensitivity** | Nice-to-have, Runner CLI reicht erstmal | Phase 3+ |
| **Indikatoren im Viewer** | Ohne LEAN-Backend keine Berechnung; kann später als optionaler Block im Universal Format ergänzt werden | Phase 2.5 oder Phase 3 |

---

## 7. Aufwandsschätzung

| Phase | Tasks | Gesamt-Komplexität |
|-------|-------|-------------------|
| 2a — Viewer Upgrade | 8 Tasks (1L, 3M, 4S) | **Hauptarbeit** |
| 2b — Logging | 4 Tasks (4S) | Klein |
| 2c — Auth | 4 Tasks (1M, 3S) | Klein-Mittel |
