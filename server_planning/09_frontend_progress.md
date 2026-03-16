# Phase 3 — Frontend & Backend Expansion Progress

> Gestartet: 2026-02-27
> Letztes Update: 2026-03-08

---

## Übersicht

| Bereich | Status | Details |
|---------|--------|---------|
| React Frontend Setup | ✅ Fertig | Vite + React + TypeScript + Tailwind |
| Backend API Erweiterung | ✅ Fertig | 23 Routen, Stats normalisiert, SPA Fallback |
| Frontend Pages | ✅ Redesign | 10 Seiten komplett neugeschrieben |
| Shared Components | ✅ Redesign | StatCard, DataTable, FilterBar, Modal, Badge |
| API Client Layer | ✅ Fertig | TypeScript Types + fetch-Client |
| Data Consistency | ✅ Fertig | 16 Strategien + 23 Backtests verifiziert |
| Build & Integration | ✅ Fertig | 283 KB JS (86 KB gzip), Flask served auf 5556 |
| Bug Fixes (10 Issues) | ✅ Fertig | Alle 10 Issues behoben |

---

## 1. React Frontend

### Tech Stack
- **React 19** + TypeScript
- **Vite 7.3** als Bundler
- **Tailwind CSS** (v4, via @tailwindcss/vite Plugin)
- **React Router** für Navigation
- **lightweight-charts** (installiert, für zukünftige TradingView-Charts)
- **Recharts** (installiert, für Chart-Komponenten)
- **Lucide React** für Icons

### Dark Theme
CSS Variables aus den Example-HTMLs übernommen:
- `--bg: #0d1117` (Hintergrund)
- `--surface: #161b22` (Karten, Sidebar)
- `--surface2: #1c2333` (Hover, Inputs)
- `--border: #30363d`
- `--text: #c9d1d9` / `--text-muted: #8b949e`
- `--green: #3fb950` / `--red: #f85149` / `--blue: #58a6ff`

### Seiten (10 Stück)

| Seite | Datei | Features |
|-------|-------|----------|
| **Backtests** | `BacktestsPage.tsx` | Kachel-Grid, Suche, Engine-Filter, Multi-Select für Compare |
| **Backtest Detail** | `BacktestDetailPage.tsx` | 8 StatCards, SVG Equity-Chart, Trade-Tabelle mit Klick |
| **Trade Chart** | `TradeChartPage.tsx` | SVG Candlestick mit Entry/Exit-Markern, Pfeiltasten-Navigation |
| **Compare** | `ComparePage.tsx` | Stats-Vergleichstabelle, Korrelation |
| **Strategies** | `StrategiesPage.tsx` | Kacheln mit Typ/Complexity-Badges, Filter |
| **Strategy Detail** | `StrategyDetailPage.tsx` | Tags, verknüpfte Backtests-Tabelle |
| **Portfolios** | `PortfoliosPage.tsx` | Kachel-Grid, Create-Modal mit Strategy-Auswahl |
| **Portfolio Detail** | `PortfolioDetailPage.tsx` | Combined Stats, Equity-Chart, Strategy Contributions |
| **Logging** | `LoggingPage.tsx` | Log-File-Auswahl, Level-Filter, Suche, Farbcodierung |
| **Sidebar** | `Sidebar.tsx` | Collapsible, 5 Nav-Items (Accounts ausgegraut) |

### Shared Components (5 Stück)

| Komponente | Beschreibung |
|------------|-------------|
| `StatCard` | Metrik-Karte mit Auto-Färbung (grün/rot) für Geld/Prozent |
| `DataTable` | Sortierbare Tabelle mit Spalten-Config und Row-Click |
| `Modal` | Overlay-Dialog mit Close-Button |
| `FilterBar` | Suchfeld + Dropdown-Filter + Sort-Selector |
| `Badge` | Farbige Pill-Labels für Tags/Status |

### API Client

| Datei | Beschreibung |
|-------|-------------|
| `api/types.ts` | 25 TypeScript Interfaces für alle Datentypen |
| `api/client.ts` | fetch-basierter Client mit allen Endpoint-Funktionen |

---

## 2. Backend API Erweiterung

### Neue Endpoints (10 Stück)

| Methode | Route | Beschreibung |
|---------|-------|-------------|
| GET | `/api/strategies` | Scannt Strategy-Files, liest STRATEGY_META |
| GET | `/api/strategies/<name>` | Detail + verknüpfte Backtests + Portfolios |
| POST | `/api/backtests/run` | Startet Backtest asynchron (Thread, Job-ID) |
| GET | `/api/backtests/run/<job_id>/status` | Status eines laufenden Backtests |
| GET | `/api/portfolios` | Liste aller Portfolios |
| POST | `/api/portfolios` | Erstellt Portfolio mit Risk-Sized Trade-Simulation |
| GET | `/api/portfolios/<id>` | Portfolio-Details |
| DELETE | `/api/portfolios/<id>` | Portfolio löschen |
| GET | `/api/portfolios/compare` | Portfolio-Vergleich |
| GET | `/api/logs` | Log-Dateien auflisten |
| GET | `/api/logs/<name>` | Log-Inhalt mit Level-Filter + Suche |
| GET | `/api/benchmark/<symbol>` | Buy & Hold Equity-Curve |

### Portfolio-Logik
- Trades aller Strategien werden zeitlich gemergt
- Risk-Sizing: `risk_amount = capital × risk_pct`, skaliert PnL proportional
- Combined Equity wird aus dem simulierten Account berechnet
- Stats (Sharpe, CAGR, Max DD) aus der Combined Equity Curve
- Strategy Contributions zeigen PnL-Anteil pro Strategie

### Bestehende Endpoints (13 Stück, unverändert)
- `/api/backtests`, `/api/backtest/<id>`, `/api/backtest/<id>/info`
- `/api/backtest/<id>/stats`, `/api/backtest/<id>/trades`
- `/api/backtest/<id>/trade/<nr>`, `/api/backtest/<id>/ohlcv`
- `/api/backtest/<id>/equity`, `/api/compare`
- `/` (Frontend-Serving)

---

## 3. Data Consistency Check

### Strategien (16/16 ✅)
Alle 16 Strategy-Files haben vollständiges STRATEGY_META mit:
- `name`, `version`, `tags.type`, `tags.asset_class`, `tags.complexity`

### Backtests (23/23 ✅)
Alle 23 Backtest-JSONs haben korrektes Format:
- `strategies` als Liste, `data.symbols`, `data.timeframes`
- `backtest_start`, `backtest_end` vorhanden

---

## 4. Build & Integration

| Check | Status |
|-------|--------|
| TypeScript Compilation | ✅ Keine Fehler |
| Vite Production Build | ✅ 271 KB JS (gzip: 84 KB) |
| Flask Backend Tests | ✅ 30/30 Tests bestehen |
| Vite Proxy → Flask :5556 | ✅ Funktioniert |
| API Endpoints (test_client) | ✅ Alle 23 Routen getestet |

### Server starten
```bash
# Backend (Flask)
cd ~/Desktop/trading-platform && source .venv/bin/activate
python -m viewer.app  # Port 5556

# Frontend (Vite Dev)
cd ~/Desktop/trading-platform/frontend
npx vite --host 0.0.0.0 --port 3000  # Proxy zu :5556
```

---

## 5. Dateistruktur (neu)

```
~/Desktop/trading-platform/frontend/
├── index.html
├── package.json
├── vite.config.ts          # Proxy + Tailwind
├── tsconfig.json
├── dist/                   # Production Build
├── src/
│   ├── main.tsx           # BrowserRouter
│   ├── App.tsx            # Routes + Layout
│   ├── Sidebar.tsx        # Collapsible Navigation
│   ├── index.css          # Tailwind + Dark Theme
│   ├── api/
│   │   ├── types.ts       # 25 Interfaces
│   │   └── client.ts      # API-Client
│   ├── components/
│   │   ├── StatCard.tsx
│   │   ├── DataTable.tsx
│   │   ├── Modal.tsx
│   │   ├── FilterBar.tsx
│   │   ├── Badge.tsx
│   │   └── index.ts
│   └── pages/
│       ├── BacktestsPage.tsx
│       ├── BacktestDetailPage.tsx
│       ├── TradeChartPage.tsx
│       ├── ComparePage.tsx
│       ├── StrategiesPage.tsx
│       ├── StrategyDetailPage.tsx
│       ├── PortfoliosPage.tsx
│       ├── PortfolioDetailPage.tsx
│       └── LoggingPage.tsx
```

---

## 6. Bug Fixes (2026-02-28)

### Behobene Issues

| # | Issue | Fix |
|---|-------|-----|
| 1 | Design passte nicht zum Reference-Stil | Alle Seiten + Komponenten komplett neugeschrieben mit inline CSS vars matching view_report.py |
| 2 | Filter und Sort fehlten | FilterBar mit Filter-Icon (Funnel), Sort-Icon (ArrowUpDown), collapsible Filter-Panel |
| 3 | Backtest-Detail war leer | Stats-Felder normalisiert im Backend (sharpe_ratio→sharpe, total_pnl→net_profit, win_rate 0-1→0-100) |
| 4 | Browser Zurück-Button ging nicht | SPA Fallback in Flask — prüft ob Datei existiert, sonst index.html |
| 5 | Portfolio: Strategies statt Backtests | Create-Modal zeigt jetzt Strategies mit Checkboxen, Risk/Trade, Max Positions |
| 6 | Sidebar-Pfeil falsche Position | Collapse-Toggle jetzt ganz oben, PanelLeftClose/PanelLeftOpen Icons |
| 7 | Icons fehlten | Lucide React Icons überall: Sidebar, Buttons, Filter, Navigation |
| 8 | Sidebar-Reihenfolge falsch | Strategies → Backtests → Portfolio → Logging → Accounts (ausgegraut) |
| 9 | Port 5556 statt 3000 | Flask served React dist/ auf Port 5556, REACT_DIST Fallback |
| 10 | Zeitraum-Logik | Start/End in Backtest-Liste angezeigt |

### Backend-Änderungen
- Stats-Normalisierung in `/api/backtest/<id>/stats` (20 normalisierte Felder)
- `start`, `end`, `timeframes` zu `/api/backtests` Liste hinzugefügt
- `timeframes` zu `/api/backtest/<id>/info` hinzugefügt
- `REACT_DIST` als Primary Static Dir, Fallback auf `viewer/static/`
- SPA Fallback: `/<path:path>` prüft Datei-Existenz, sonst index.html

### Frontend-Änderungen (alle Dateien neugeschrieben)
- **Sidebar.tsx** — Collapse oben, Icons, neue Reihenfolge, kein Branding
- **StatCard.tsx** — Inline styles mit CSS vars, auto color detection
- **DataTable.tsx** — 0.78rem tabular-nums, sticky thead, hover highlight
- **FilterBar.tsx** — Collapsible Filter-Panel, Filter/Sort Icons, multiple configs
- **Modal.tsx** — Inline styles, X-Icon (Lucide)
- **BacktestsPage.tsx** — Card-Grid, 3 Filter (Engine/Strategy/Timeframe), 3 Sort, Checkboxen
- **BacktestDetailPage.tsx** — 12 StatCards, SVG Equity mit Grid/Fill, Trade Summary Bar, Trade Table
- **StrategiesPage.tsx** — 4 Filter (Type/Source/Asset/Complexity), Tag-Badges
- **StrategyDetailPage.tsx** — Header mit Tags, Backtest-Grid
- **PortfoliosPage.tsx** — Strategy-basiertes Create-Modal
- **PortfolioDetailPage.tsx** — StatCards, Equity placeholder, Strategy list
- **LoggingPage.tsx** — Level-Farben, File-Selector, Search
- **TradeChartPage.tsx** — Keyboard navigation, Trade-Info cards
- **ComparePage.tsx** — Color-coded legend, Stats comparison table

---

## 7. Bug Fixes Session 6 (2026-02-28)

### Behobene Issues

| # | Bug | Fix | Datei(en) |
|---|-----|-----|-----------|
| 1 | Content nur ~40% Breite | `maxWidth: '1400px'` + `margin: '0 auto'` entfernt, jetzt `padding: '24px'` full-width | Alle 9 Page-Dateien |
| 2 | Logging-Seite kaputt | `getLogs()` gibt Objects zurück, LoggingPage extrahiert jetzt `.name` | `LoggingPage.tsx` |
| 3 | Browser Zurück-Button geht nicht | Route-Order in App.tsx: `/backtests/compare` jetzt VOR `/backtests/:id` | `App.tsx` |
| 4 | Create Backtest Button macht nichts | Neues `CreateBacktestModal` mit Strategy/Engine/Symbols/Timeframe/Capital, POST + Polling | `CreateBacktestModal.tsx`, `BacktestsPage.tsx` |
| 5 | Create Portfolio Button macht nichts | Backend akzeptiert jetzt Strategy-`name` (nicht nur backtest_id), fuzzy matching (case-insensitive, underscore=space) | `app.py`, `PortfoliosPage.tsx` |
| 6 | Strategy Detail: Create Backtest fehlt | "Run Backtest" Button + CreateBacktestModal mit preselectedStrategy | `StrategyDetailPage.tsx` |

### Neue Komponente
- **`CreateBacktestModal.tsx`** (~200 Zeilen)
  - Strategy-Dropdown aus `/api/strategies` (value = Dateiname)
  - Engine: vectorbt / pynecore / lean
  - Symbols als Freitext-Input
  - Timeframe: 1m/5m/15m/1h/4h/1d
  - Optionaler Zeitraum (Start/End)
  - Initial Capital
  - POST `/api/backtests/run` → Polling alle 2.5s → Navigation zum Ergebnis
  - `preselectedStrategy` Prop für Nutzung auf Strategy-Detail-Seite

### Backend-Änderungen
- Portfolio-Erstellung: Fuzzy-Matching für Strategy-Namen (`strat_name_input.lower().replace("_"," ")` vs gespeicherte Namen)
- Findet automatisch den neuesten Backtest für eine Strategie

### Build
- TypeScript: ✅ Keine Fehler
- Vite Build: ✅ 288 KB JS (87 KB gzip)
- Backend Tests: ✅ 30/30 bestanden
- Flask auf Port 5556: ✅ Läuft

---

## 8. Offene Punkte / Nächste Schritte

### Noch zu tun
- [ ] lightweight-charts Integration (aktuell SVG-Fallback für Charts)
- [ ] Drawing Tools vom alten Viewer portieren
- [ ] Monthly Returns Heatmap (wie im backtest-report-example.html)
- [ ] Portfolio Correlation Matrix berechnen und anzeigen
- [ ] Portfolio Compare Seite implementieren
- [ ] Live-Tail für Logs (WebSocket oder Polling)
- [ ] Responsive Design für Mobile verfeinern

### Bekannte Limitierungen
- Charts sind aktuell SVG-basiert (funktional, aber nicht interaktiv)
- Portfolio Correlation Matrix ist ein Platzhalter (leeres Dict)
- Accounts-Seite ist ausgegraut (Phase 4/5)

### Performance
- Frontend Build: 295 KB JS (gzip: 89 KB)
- Backend: 26 API-Routen in viewer/app.py
- Strategy-Scan lädt alle .py Files — bei >100 Strategien ggf. Caching nötig

---

## 9. Mega-Update (2026-03-02)

### Bug Fixes

| # | Bug | Fix | Datei(en) |
|---|-----|-----|-----------|
| 1 | Content Breite nicht voll | `w-full` auf App.tsx root div — flex items ohne width nehmen nur content-width | `App.tsx` |
| 2 | Backtest Date Range Error | CLI: `_detect_date_range()` ermittelt Zeitraum aus Parquet-Dateien, Backend: --start/--end nur wenn nicht leer | `cli.py`, `base.py`, `app.py` |
| 3 | Portfolio Detail leer | Frontend las `detail.stats` statt `combined_stats`, komplett neu geschrieben mit SVG Equity Chart | `PortfolioDetailPage.tsx` |

### QuantStats Integration

- **quantstats 0.0.81** installiert
- `runner/stats.py` komplett refactored:
  - QuantStats für: Sharpe, Sortino, Calmar, CAGR, Max Drawdown, Total Return
  - Manuelle Fallbacks für: Win Rate, Profit Factor, Expectancy, Avg Bars
  - Neue Metriken: `max_consecutive_wins`, `max_drawdown_duration`, `cagr`, `total_return`
- **15 Tests** angepasst und bestanden

### Daten-Downloads

| Quelle | Typ | Umfang |
|--------|-----|--------|
| **Dolthub** | Stocks OHLCV | 20.055 Symbole (US Equities), Daily |
| **Dolthub** | Earnings | 10 Tabellen (EPS, Income, Balance Sheet, Cash Flow, etc.) |
| **Dolthub** | Rates | US Treasury Rates (9.031 Rows) |
| **Dolthub** | Options | Volatility History + Option Chain (Letter A) |
| **Dukascopy/yfinance** | Forex | 6 Paare (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF) 1d+1h |
| **Dukascopy/yfinance** | Metals | XAGUSD 1d+1h |
| **Dukascopy/yfinance** | Indices | US30, US500, USTEC 1d+1h |
| **Dukascopy/yfinance** | Energy | UKOIL, USOIL 1d+1h |

**Catalog**: 20.193 Datasets, 20.058 Symbole, 3 Quellen, ~1.26 GB

### Resample Tool

- **`data_tools/resample.py`**: CLI-Tool für Timeframe-Aggregation
- Unterstützt: 1m→5m/15m/30m/1h/4h/1d/1w
- OHLCV-Regeln: Open=first, High=max, Low=min, Close=last, Volume=sum
- Batch-Modus für ganze Verzeichnisse
- Catalog-Auto-Update nach Resample

### Frontend Features

| Feature | Beschreibung |
|---------|-------------|
| **Benchmark Overlay** | Dropdown auf Backtest-Detail (SPY, QQQ, IWM, etc.), dashed blue benchmark line im Equity Chart |
| **Symbol Autocomplete** | `SymbolInput` Komponente mit Live-Suche aus 20K+ Symbolen, Kategorien (Equity/Forex/Metals/etc.) |

### Neue Komponenten

| Komponente | Beschreibung |
|------------|-------------|
| `SymbolInput.tsx` | Autocomplete-Input für Symbol-Auswahl, Dropdown mit Kategorien |
| `PortfolioEquityChart` | SVG-basierter Equity-Chart für Portfolio-Detail |

### Neue API-Endpoints

| Methode | Route | Beschreibung |
|---------|-------|-------------|
| GET | `/api/symbols` | Symbol-Katalog mit Kategorien für Autocomplete |

### Tests

| Test-Suite | Tests | Status |
|------------|-------|--------|
| `test_api.py` | 17 Tests (Strategies, Backtests, Portfolios, Logs, Benchmark, Symbols) | ✅ |
| `test_resample.py` | 8 Tests (OHLCV Rules, File Resample, Multi-TF) | ✅ |
| `test_stats.py` | 15 Tests (QuantStats Integration) | ✅ |
| `test_catalog.py` | 2 Tests | ✅ |
| `test_models.py` | 14 Tests | ✅ |
| `e2e_frontend.js` | 13 Tests (Playwright Firefox: Width, Pages, Modal, Autocomplete, Benchmark) | ✅ |
| **Gesamt** | **69 Tests** | **✅ Alle bestanden** |

### Build

| Check | Status |
|-------|--------|
| TypeScript Compilation | ✅ Keine Fehler |
| Vite Production Build | ✅ 295 KB JS (gzip: 89 KB) |
| Backend Tests (pytest) | ✅ 56/56 bestanden |
## 10. Data Management & Refinement (2026-03-02)

### Bug Fixes
| # | Bug | Fix | Datei(en) |
|---|-----|-----|-----------|
| 1 | Portfolio Stats falsch gewichtet | `create_portfolio` nutzt jetzt tägliche Returns gewichtet nach `risk_per_trade_pct`, summiert diese und berechnet Stats auf der kombinierten Equity | `viewer/app.py` |
| 2 | Max Drawdown Format (0.22%) | Backend liefert jetzt `max_drawdown_pct` als Prozent (0-100) statt Dezimal, Frontend zeigt korrekt "-21.00%" | `viewer/app.py`, `BacktestDetailPage.tsx` |
| 3 | Create Backtest Engine manuell | Automatische Engine-Erkennung anhand Dateiname (pynecore_ prefix), Feld wird read-only | `CreateBacktestModal.tsx` |
| 4 | Symbol Kategorien unübersichtlich | `SymbolInput` gruppiert jetzt nach Asset-Klasse (Forex, Metals, Crypto, etc.) mit Accordion-UI | `SymbolInput.tsx`, `app.py` |

### Neues Modul: Data Management
- **Frontend Page**: `DataManagementPage.tsx` (Route `/data`)
  - Übersicht aller Daten-Kategorien (Anzahl, Größe)
  - Detail-Ansicht mit Lösch-Funktion
  - Download-Modal (Dukascopy, YFinance, Dolthub)
  - Resample-Modal (Batch-Verarbeitung)
  - Task-Status Anzeige (Polling von Background-Tasks)
- **Backend API**:
  - `GET /api/data/overview`, `GET /api/data/category/<name>`
  - `POST /api/data/download`, `POST /api/data/resample`
  - `DELETE /api/data/symbol`
  - `GET /api/data/tasks/<id>`
- **Infrastructure**:
  - `data_tools/dolthub_manager.py`: Clone/Pull/Export Logik für Dolt-Repos
  - Background-Task System in `viewer/app.py` (Threading + Subprocess)

### Status
- **Build**: ✅ Vite Build erfolgreich
- **Tests**: ✅ Manuelle Verifikation der Stats-Logik und API-Endpoints

### Update (2026-03-02 - Layout & Stats Fixes)
- **Max Drawdown**: Fallback auf manuelle Berechnung wenn QuantStats 0 liefert (z.B. fehlender Datetime Index) oder unplausibel ist.
- **Layout**: Grid-Layouts auf `auto-fit` umgestellt um "Leerraum rechts" bei wenigen Items zu vermeiden (Backtests, Strategies, Portfolios).
- **Data Page**: Voll funktionsfähig (Download/Resample Tasks).

### Phase 5 (2026-03-02 - Dukascopy Downloader, Dolthub Fix, Sparkline Cards)

#### FIX 1: Dukascopy Downloader erstellt
- Neue Datei: `data_tools/downloaders/dukascopy_dl.py`
- Implementiert `Downloader` ABC aus `base.py`
- `dukascopy-python 4.0.1` installiert; nutzt interne `_fetch()` API (public `fetch()` ist buggy)
- Symbol-Mapping: EURUSD→EUR/USD, XAUUSD→XAU/USD, US30→USA30.IDX/USD, UKOIL→BRENT.CMD/USD
- Timeframe-Mapping: 1m→INTERVAL_MIN_1, 1h→INTERVAL_HOUR_1, 1d→INTERVAL_DAY_1
- Pagination für >30K Rows (max pro API-Call)
- Auto-Detection der Asset-Klasse aus Symbolname
- CLI: `python -m data_tools.downloaders.dukascopy_dl --symbols EURUSD --timeframes 1d`
- Output: `~/data/ohlcv/dukascopy/{asset_class}/{SYMBOL}/{timeframe}.parquet`
- Im Data Management UI über "Dukascopy" Quelle aufrufbar

#### FIX 2: Dolthub Export gefixt
- `dolthub_manager.py`: CSV-Parsing gefixt (`Tables_in_{db}` statt `Table`)
- Neue Helfer: `_list_tables()`, `_export_table_csv()`
- Nutzt `dolt table export` statt `dolt sql -r csv` (einfacher, robuster)
- `ohlcv` Tabelle nutzt `act_symbol` Spalte (nicht `symbol`)
- Export nach `equity/US/` Pfad, überspringe bereits exportierte Dateien

#### FIX 3: Sparkline Equity Curves + Stats auf Kacheln
- **Backend** (`viewer/app.py`):
  - Neue Funktion `_quick_stats_and_equity()`: Berechnet Sharpe, CAGR, MaxDD, Net PnL% aus Trade-Daten
  - `GET /api/backtests` liefert jetzt: `sharpe`, `cagr`, `max_drawdown_pct`, `net_profit_pct`, `equity_mini` (50 Punkte)
  - `GET /api/portfolios` liefert jetzt: `equity_mini` (aus combined_equity), Stats als Prozent
- **Frontend**:
  - Neue Komponente: `Sparkline.tsx` — SVG Sparkline (grün wenn profitabel, rot wenn Verlust)
  - `BacktestsPage.tsx`: Sparkline + Stats-Zeile (Sharpe, CAGR, DD, PnL%) auf jeder Kachel
  - `PortfoliosPage.tsx`: Sparkline + Stats-Zeile auf jeder Kachel
- 33 SVG Sparklines auf Backtests-Seite, 3 auf Portfolios-Seite verifiziert

#### Test-Ergebnisse
| Seite | Status | Details |
|-------|--------|---------|
| Backtests | ✅ | 33 Sparklines, Sharpe/CAGR/DD/PnL sichtbar |
| Portfolios | ✅ | 3 Sparklines, Stats korrekt (CAGR 0.1-1.0%, DD -0.2-0.4%) |
| Strategies | ✅ | Strategien mit Tags |
| Data Management | ✅ | 7 Kategorien (Forex, Metals, Indices, Energy, ETFs, Equities, Dolthub) |
| Backtest Detail | ✅ | Sharpe, Equity Curve, Max Drawdown |
| Logging | ✅ | Log-Dateien angezeigt |

#### Build
- Vite Build: ✅ 306 KB JS (91 KB gzip)
- Flask Server: ✅ Port 5556
- Alle 6 Seiten funktionsfähig

### Phase 4b: Stabilization (2026-03-02)

#### Backend Health Check
Alle Endpoints getestet und funktional:
- `GET /api/strategies` → 16 Strategien ✅
- `GET /api/backtests` → 28 Backtests ✅
- `GET /api/portfolios` → 3 Portfolios ✅
- `GET /api/logs` → 2 Log-Dateien ✅
- `GET /api/data/overview` → 7 Kategorien ✅

#### Data Management: Fehlende Kategorien gefixt
**Problem**: Metals (XAGUSD), Indices (US30, US500, USTEC) und Energy (UKOIL, USOIL) existierten auf Disk unter `~/data/ohlcv/dukascopy/` aber fehlten in der Übersicht (catalog.json hatte keine Einträge dafür).

**Fix** (`viewer/app.py`):
- Neuer Hybrid-Ansatz: `data_overview` und `data_category_details` scannen jetzt sowohl `catalog.json` als auch das Dateisystem (`~/data/ohlcv/`)
- `_categorize_path()` Helper für konsistente Kategorie-Ableitung aus Pfaden
- `_scan_ohlcv_filesystem()` scannt rekursiv nach `.parquet` Dateien
- Ergebnis: 7 Kategorien statt vorher 4 (ETFs, Energy, Equities, Forex, Indices, Metals, Other)

#### Catalog.json aktualisiert
- 37 neue Einträge hinzugefügt (Metals, Indices, Energy, fehlende Forex-Timeframes)
- Gesamt: 20.214 Datasets im Katalog

#### Dolthub als echte Dolt-Repos geklont
Alle 4 Repos erfolgreich geklont nach `~/data/dolthub/`:

| Repo | Größe | Tabellen |
|------|-------|----------|
| **stocks** | 4.3 GB | ohlcv, dividend, split, symbol |
| **earnings** | 1.5 GB | 10 Tabellen (EPS, Income, Balance Sheet, etc.) |
| **options** | 7.2 GB | option_chain, volatility_history |
| **rates** | 12 MB | us_treasury |

- `dolt pull` Update-Mechanismus über `data_tools/dolthub_manager.py`
- Im Data Management UI: "Dolthub" Quelle im Download-Modal nutzt `dolt pull` + Re-Export

#### Playwright-Tests (Headless Firefox)
| Seite | Status | Details |
|-------|--------|---------|
| Strategies | ✅ | Zeigt Strategien mit Tags, Filter funktioniert |
| Backtests | ✅ | Zeigt 28+ Backtests, Engine/Strategy Filter |
| Backtest Detail | ✅ | Stats (MaxDD -21.30%, Sharpe 0.537), Equity Curve, Trades |
| Logging | ✅ | Log-Dateien, Level-Filter |
| Data Management | ✅ | 7 Kategorien, Accordion expandierbar, Download/Resample |
| Portfolios | ✅ | Portfolio-Liste mit Create-Modal |
| Content Width | ✅ | 1200px von 1200px verfügbar (100%) |

#### Build
| Check | Status |
|-------|--------|
| Vite Build | ✅ 304 KB JS (90 KB gzip) |
| Flask Server | ✅ Port 5556 |
| Alle Seiten erreichbar | ✅ |

---

## Phase 6 — Status Check, Fixes & Enhancements (2026-03-03)

### Completed

| Feature | Status | Details |
|---------|--------|---------|
| QQQ Benchmark | ✅ | Via yfinance, 6787 bars (1999-2026) |
| XAUUSD Category Fix | ✅ | Moved from Forex → Metals in _derive_category() |
| "Other" Category Fix | ✅ | SPY/VIX → Indices, no more "Other" |
| Probabilistic Sharpe Ratio | ✅ | PSR in stats.py + app.py + Frontend (cards + detail) |
| Risk-Per-Trade Sizing | ✅ | BacktestConfig + vectorbt adapter + CLI + Frontend modal |
| Trade Chart (lightweight-charts v5) | ✅ | Candlestick OHLCV, volume, entry/exit markers, price lines, keyboard nav |
| Equity Chart Upgrade | ✅ | Replaced SVG with lightweight-charts AreaSeries + drawdown histogram + hover tooltip |
| Benchmark Overlay | ✅ | SPY/QQQ/GLD dashed line on equity chart, scaled to same start capital |
| Dolthub Auto-Pull | ✅ | Non-blocking `dolt pull` before each backtest run |
| Firefox Locale Fix | ✅ | `localization: { locale: 'en-US' }` fixes blank canvas in headless Firefox |

### Technical Notes

- **lightweight-charts v5 API**: Uses `chart.addSeries(CandlestickSeries, opts)` instead of v4's `chart.addCandlestickSeries(opts)`. Markers use `createSeriesMarkers(series, markers)`.
- **Firefox headless bug**: Canvas renders blank (300×150 default) without explicit `locale: 'en-US'` in Playwright page context AND `localization: { locale: ... }` in chart options.
- **PSR formula**: Uses per-observation (non-annualized) SR to avoid PSR≈1.0 for all strategies. `PSR = Φ((SR_obs - 0) × √(n-1) / √(1 - skew×SR + ((kurt-1)/4)×SR²))`
- **Risk sizing**: When `risk_per_trade_pct` set: uses stop_loss distance if available, else fixed fractional. Tracks running equity.

### Build
| Check | Status |
|-------|--------|
| Vite Build | ✅ 483 KB JS (149 KB gzip) — includes lightweight-charts |
| Flask Server | ✅ Port 5556 |
| All pages functional | ✅ 6/6 core pages verified with Playwright |
| Trade Chart | ✅ Candlesticks render, navigation works |
| Equity Hover Tooltip | ✅ Date, equity, return, drawdown |

---

## Phase 7 — Fixes & Enhancements (2026-03-03)

### Completed

| Task | Status | Details |
|------|--------|---------|
| Benchmark fallback timeframe | ✅ | XAUUSD now works — resamples 4h→1d when 1d.parquet missing. Fallback chain: 1d→4h→1h→15m |
| Portfolio equity chart → lightweight-charts | ✅ | Replaced SVG with interactive AreaSeries + drawdown HistogramSeries + crosshair tooltip |
| Risk-sizing validation | ✅ | Default 2% stop when no SL, max 20% equity cap per position. Tested: max loss 0.39% on SPY |
| Dukascopy downloader + progress bar | ✅ | Download tested (EURUSD 1d works). Progress bar with animated fill + percentage + status text |
| Data Management 3-level accordion | ✅ | Category → Symbol (with MB/timeframe count) → Timeframe (date range, size, delete). Full-width layout |
| Catalog cleanup | ✅ | Removed 20,055 stale Dolthub phantom entries (20,214 → 159 entries) |
| Indicator overlays in trade chart | ✅ | Auto-detects indicators from strategy source (SMA/EMA/RSI/MACD/BB/ATR/SuperTrend). Computes with pandas_ta. Overlay indicators as LineSeries, panel indicators with separate price scales |

### Technical Details
- **Frontend build**: 490 KB JS (150 KB gzip) — includes lightweight-charts v5
- **Flask server**: Port 5556, uses .venv Python
- **All 6 core pages verified**: Strategies, Backtests, Portfolios, Data, Logging, Accounts
- **Trade chart**: 7 canvas elements (candlesticks + volume + indicators)
- **Indicator detection**: Regex-based parsing of strategy source for indicator patterns
- **Indicator computation**: pandas_ta with 50-bar warmup window

---

## Phase 8 — DBMS-Style Data Console & Live Terminal (2026-03-08)

### Completed

| Task | Status | Details |
|------|--------|---------|
| Data Management redesign | ✅ | `DataManagementPage.tsx` komplett auf zweispaltiges DBMS-Layout umgebaut: Tree-View links, Detail-Workspace rechts |
| Tree filtering & symbol drill-down | ✅ | Sidebar-Suche filtert Symbole direkt im Baum; Kategorie- und Symbolwechsel wurden in Firefox/Playwright verifiziert |
| Symbol detail workspace | ✅ | Header + Breadcrumbs, Action-Bar, sortierbare Timeframe-Tabelle mit Start/Ende, Größe, Health-Badge und Row-Actions |
| Live terminal component | ✅ | Neue `LiveTerminal.tsx` pollt `/api/data/tasks/<job_id>/logs` im 1-Sekunden-Takt und rendert Terminal-Output im dunklen Monospace-Panel |
| Modal terminal flow | ✅ | `DownloadModal.tsx` und `ResampleModal.tsx` schalten nach Task-Start vom Formular direkt in den Live-Terminal-Modus |
| Dukascopy verbose toggle | ✅ | `data_tools/dukascopy_discovery.py` unterstützt jetzt `--verbose`; ohne Flag bleiben nur kompakte Progress-Logs |

### Validation

- **Frontend build:** `cd frontend && npm run build` ✅ (`526.94 KB` JS, `160.17 KB` gzip)
- **Backend tests:** `.venv/bin/python -m pytest tests -q` ✅ (`76 passed, 1 warning`)
- **Viewer restart:** Flask hart neu gestartet und wieder auf **Port 5556** hochgefahren ✅
- **Playwright smoke test (Firefox):** Tree-Filter `GBPUSD`, Kategorie-Navigation `Forex`, Symbol-Drilldown und Action-Bar erfolgreich durchgeklickt ✅
- **Live terminal smoke test:** echter Download-Task fuer `GBPUSD` mit 1-Monats-Zeitraum aus dem Modal gestartet; Terminal zeigte sofort den Live-Log mit `Starting command: ... --timeframes d1 ...` ✅

### Notes

- Die Download-Defaults normalisieren Dukascopy-Timeframes jetzt korrekt (`4h -> h4`) fuer Genesis-Checks, waehrend der UI-Flow nur die explizit aktivierten Buttons an den Downloader uebergibt.
- Das Backend liefert fuer `/api/data/category/<name>` weiterhin die erweiterten Symbol-Metadaten (`validation`, `error`, `repo`, `last_updated`) nur dann, wenn sie wirklich vorhanden sind; bestehende Tests fuer den alten Payload bleiben dadurch stabil.
