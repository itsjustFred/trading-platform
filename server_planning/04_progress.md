# Phase 1 — Fortschritt

> Letzte Aktualisierung: 2026-02-24 — **Phase 1 ABGESCHLOSSEN** ✅

---

## Phase 1a — Fundament

- [x] **Task 1: Monorepo initialisieren** — `~/Desktop/trading-platform/`, git init, pyproject.toml (uv, optional deps), .gitignore, README.md, alle __init__.py
- [x] **Task 2: Trade-Format Dataclasses** — `shared/models.py` (Trade, TradeExit, BacktestMeta, BacktestResult, EquityPoint), `shared/constants.py`, 12 Tests pass
- [x] **Task 3: Stats-Modul** — `runner/stats.py` (Win-Rate, Sharpe, Sortino, MaxDD, Profit Factor, Calmar, consecutive losses), 14 Tests pass
- [x] **Task 4: Data-Verzeichnis aufsetzen** — ~/data/ Struktur erstellt, `data_tools/migrate_csv.py` migriert 97 Parquet-Dateien (18MB) + SP500-Universum. Quellen: Dukascopy XAUUSD (H1/M15), yfinance equities (H1/M15/daily/5m, 76+ Symbole)
- [x] **Task 5: Catalog-Builder** — `data_tools/catalog_builder.py` scannt ~/data/, erzeugt catalog.json (97 Datasets, 86 Symbole, 2 Quellen, 4 Timeframes), 2 Tests pass

## Phase 1b — Converter

- [x] **Task 6: LEAN Converter** — `runner/converters/lean_converter.py` — Paired LEAN orders → Trades, PnL from profitLoss dict
- [x] **Task 7: Stockkit Converter** — `runner/converters/stockkit_converter.py` — Semicolon-CSV → Trades, Partial Exits (TP1 + Runner)
- [x] **Task 8: Converter CLI** — `runner/converters/cli.py` — `python -m runner.converters.cli {lean|stockkit|list}`

## Phase 1c — Viewer

- [x] **Task 9: Viewer Backend** — `viewer/app.py` Flask API (backtests list, load, stats, OHLCV endpoints)
- [x] **Task 10: Viewer Frontend** — `viewer/static/index.html` lightweight-charts, trade markers, dark theme
- [x] **Task 11: Equity-Curve** — Integriert in Frontend (synced Equity-Curve-Chart unter Candles)
- [x] **Task 12: Trade-Tabelle** — Integriert in Frontend (Win/Loss coloring, click to highlight)

## Phase 1d — Runner

- [x] **Task 13: EngineAdapter Interface** — `runner/engines/base.py` (EngineAdapter ABC + BacktestConfig)
- [x] **Task 14: vectorbt Adapter** — `runner/engines/vectorbt_adapter.py` (signal-based, dynamic module loading)
- [x] **Task 15: LEAN Adapter** — `runner/engines/lean_adapter.py` (converts existing results or runs LEAN subprocess)
- [x] **Task 16: Runner CLI** — `runner/cli.py` (`python -m runner.cli run --engine vectorbt --strategy ... --symbols ...`)
- [x] **Task 17: Beispiel-Strategie** — `strategies/example_sma.py` (SMA Crossover, 67 Trades, $19.6K Profit auf AAPL 1h)

## Phase 1e — Polish

- [x] **Task 18: Strategie-Vergleich** — `/api/compare?ids=id1,id2` Endpoint im Viewer Backend
- [x] **Task 19: README + Doku** — Vollständiges README mit Quick Start, Struktur, Strategy Guide
- [x] **Task 20: Git-Aufräumen** — Alle Tests grün, Catalog rebuilt, saubere Commit-History

---

## Erstellte / Geänderte Dateien

**Monorepo:** `~/Desktop/trading-platform/` (9 Commits auf `main`)

| Datei | Beschreibung |
|-------|-------------|
| `shared/models.py` | Trade, TradeExit, BacktestMeta, BacktestResult, EquityPoint |
| `shared/constants.py` | Timeframes, Directions, Defaults |
| `runner/stats.py` | Stats-Berechnung (Sharpe, Sortino, MaxDD, Win-Rate, etc.) |
| `runner/cli.py` | Runner CLI (`python -m runner.cli run ...`) |
| `runner/engines/base.py` | EngineAdapter ABC + BacktestConfig |
| `runner/engines/vectorbt_adapter.py` | Vectorized Backtesting Adapter |
| `runner/engines/lean_adapter.py` | LEAN Bridge Adapter |
| `runner/converters/cli.py` | Converter CLI |
| `runner/converters/lean_converter.py` | LEAN JSON → Universal Format |
| `runner/converters/stockkit_converter.py` | Stockkit CSV → Universal Format |
| `viewer/app.py` | Flask Backend (List, Load, Stats, OHLCV, Compare) |
| `viewer/static/index.html` | Frontend (lightweight-charts, Equity Curve, Trade Table) |
| `data_tools/migrate_csv.py` | CSV → Parquet Migration (98 Dateien) |
| `data_tools/catalog_builder.py` | Data Catalog Scanner → catalog.json |
| `strategies/example_sma.py` | SMA Crossover Beispielstrategie |
| `tests/test_models.py` | 12 Tests für Models |
| `tests/test_stats.py` | 14 Tests für Stats |
| `tests/test_catalog.py` | 2 Tests für Catalog |

**Data:** `~/data/` (18 MB)
- 97 Parquet-Dateien (OHLCV: Dukascopy XAUUSD, yfinance 86 US Equities)
- 3+ Backtest-Ergebnisse (.trades.json)
- catalog.json
- universes/sp500_changes.txt

## Entscheidungen

1. **Stats als Ratio (0-1)** statt Prozent (0-100) — konsistenter für API-Consumers
2. **TradeExit.pnl hinzugefügt** — notwendig für Partial-Exit-Tracking
3. **yfinance Multi-Header-Format** in migrate_csv behandelt (Ticker/Datetime Metadaten-Zeilen)
4. **Simplified Position Sizing** im vectorbt Adapter (all-in pro Trade) — reicht für Phase 1
5. **LEAN Adapter** kann auch vorhandene JSON-Results konvertieren (kein LEAN-Run nötig)

## Offene Punkte / Probleme

- LEAN Adapter: Tatsächliche LEAN-Execution noch nicht getestet (nur JSON-Conversion)
- vectorbt Adapter: Nur Long-Entries, kein Short-Support im Signal-Format
- Viewer: OHLCV-Overlay funktioniert nur wenn Daten im ~/data/ liegen
- Stockkit Converter: TP1-Zeitpunkt unbekannt (CSV hat nur finale Exit-Zeit)
- Keine Tests für Converter und Engine Adapter (nur Integration getestet)
