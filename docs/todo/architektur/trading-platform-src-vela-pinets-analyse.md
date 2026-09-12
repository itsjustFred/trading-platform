# Trading-Platform: `src/`-Layout + Vela / PineTS Bewertung

Stand: 2026-09-06 · Repo: `/home/ubuntu/Desktop/trading-platform` · Modus: Analyse, keine Codeänderung

## 1. Befund Codebase

Flat-Layout, kein `src/`-Verzeichnis.

Top-Level-Pakete (alle mit absolutem Repo-Root-Import, 0 relative Imports):

| Paket | Zweck |
|---|---|
| `runner/` | Backtest-Kern: native `TradeEngine` (`runner/engine.py`, `runner/engine_pkg/trading_engine.py`), Adapter `vectorbt/lean/pynecore/signal-replay` (`runner/engines/`), CLI/Grid/Matrix/Optimizer/Walkforward (`runner/cli.py`, `backtest_grid.py`, `backtest_matrix.py`, `optimizer.py`, `walkforward.py`, `stats.py`, `capm.py`) |
| `data_tools/` | OHLCV: `cache_manager.py`, `catalog_builder.py`, `auto_download.py`, `downloaders/yfinance|dukascopy|alpaca|ctrader|dolthub|histdata|huggingface|jforex`, `resample.py`, `validate.py`, `migrate_csv.py`, `signal_import.py` |
| `shared/` | Kontrakte/Infra: `models.py` (`Trade/TradeExit/BacktestMeta/BacktestResult`), `constants.py` (`DATA_DIR`, Timeframes), `database.py`, `strategy_registry.py` (AST-Discovery, kein Exec), `logging.py`, `sentry.py`, `sql.py`, `atomic_io.py` |
| `viewer/` | FastAPI-Backend (`viewer/main.py`, ~11k Zeilen) + Helper (`viewer/core.py`) + Legacy-Static-UI (`viewer/static/index.html`) |
| `risk/` | Sizing/Gating (`risk/config.py`, `engine.py`, `models.py`, `live.py`) |
| `validation/` | Fremdergebnis-Validierung (`reference.py`, `comparison/`, `importers/`, `statistics/`, `cli.py`) |
| `scheduler/` | APScheduler-Jobs (`scheduler/scheduler.py`, `jobs/weekly_data_refresh.py`) |
| `live_runner/` | Live-Daemon + Broker (`daemon.py`, `alpaca_adapter.py`, `ctrader_adapter.py`, `base_adapter.py`) |
| `signal_bots/` | Discord/Signal-Prozessoren (`ramjet/`, `rocketfuel/`, `nitro/`, `registry.py`) |
| `strategies/` | ~25 Strategie-Ordner, `__init__.py` leer — eher Daten als Library |
| `scripts/` | Ops-Einzelskripte, kein Package |
| `tests/` | ~100x `test_*.py` flach + `strategies/`, `validation/`, `e2e/`, `integration/`, `fixtures/`, `conftest.py` |

Kritische Stellen für einen Move:

- `pyproject.toml:52-57` (hatchling): `packages=["shared","runner","viewer","data_tools","strategies","risk","signal_bots"]` — `live_runner,scheduler,validation,scripts` werden heute **nicht** geshippt.
- `pyproject.toml:35`: `pynecore=["pynesys-pynecore>=0.1.0"]` (optional).
- `pyproject.toml:66-67` vs `pytest.ini:1-10`: `pytest.ini` gewinnt (`testpaths=tests`, `timeout=180`).
- `tests/conftest.py:10-11,49+`: kein `sys.path`-Hack, aber `PROJECT_ROOT=Path(__file__).parents[1]`, `DATA_DIR`-Sandbox via `monkeypatch shared.database.*`.
- `shared/strategy_registry.py:29-30`: `DEFAULT_STRATEGIES_DIR=PROJECT_ROOT/strategies`.
- Import-Stil durchgehend absolut, z.B. `from runner.engines.base import BacktestConfig` (`runner/walkforward.py:17`), `from shared.models` (`tests/test_engine_contract.py:37-61`), `from data_tools.auto_download` (`runner/optimizer.py:15`).

Aktuelle Lib-Nutzung:

- PyneCore: 4 echte Importstellen — `runner/engines/pynecore_adapter.py:298` (`import pynecore` im try, Ausführung via `pyne`-CLI per `subprocess` in `:231-293`, Pipeline Parquet→CSV→Trade-CSV→Universal Format), `strategies/pynecore_sma_crossover/...py:7-8`, `strategies/pynecore_orb/...py:12-13`, `tests/test_engine_contract.py:1158-1159`. Rest nur Labels/Routing (`runner/cli.py:70-73`, `runner/backtest_grid.py:229-230`, `runner/strategy_ir/models.py:101`, Frontend-Badge `CreateBacktestModal.tsx:122,281`).
- Lightweight-Charts: nur Frontend. `frontend/package.json:21` `lightweight-charts: ^5.1.0`. Zentral `frontend/src/components/TradingViewChart.tsx:9-11,391` (801 Zeilen) + 4x dupliziertes `createChart`-Bootstrap in `LiveViewer.tsx:644`, `BacktestDetailPage.tsx:1046`, `LiveDetail.tsx:547`, `PortfolioDetailPage.tsx:807`. Legacy `viewer/static/index.html:7` noch v4-global. Python nutzt `plotly>=6.0` (`pyproject.toml:16`). Chart-Daten: `viewer/main.py:7076ff` (`backtest/{id}`, `equity`, `trades`, `replay`, `ohlcv`, `compare`), Helper `viewer/core.py` (`_equity_to_viewer:875`, `_load_ohlcv_df:444`, `_slice_indicators_to_timestamps:7459`, ...). Daneben `recharts ^3.7.0` + `ag-grid ^35.2.0` (ersetzt Vela nicht).

## 2. Vela (LuxAlgo/Vela) vs. Lightweight-Charts

Was Vela ist (per README/docs verifiziert):

- Headless-Core + nativer Renderer (WebGL2, Canvas2D-Fallback) + Workspace-App + Plugin-SDK (`registerChartType`, `registerRendererLayer`).
- `@luxalgo/vela`: Chart ohne Scripting-Engine. `@luxalgo/vela/workspace`: Voll-App (Grid, Topbar, Object-Tree, Sync-Groups, Persistenz, Keyboard-UX). `@luxalgo/vela/ui`: Komponenten/Token. `@luxalgo/vela/plugin`: SDK.
- 70+ eingebaute Indikatoren (rechnen aus eigenen Bars, kein Engine nötig), Volume + Volume-Profile.
- Provider `binance/coinbase/hyperliquid` oder eigene Bars offline (`data`-Option).
- Stand: jung — Repo angelegt 07/2026, ~228 Stars / 32 Forks / 405 Commits. Core Apache-2.0, aber `NOTICE` mit **Attributionspflicht** (Watermark default-an, aus nur bei gleichwertiger sichtbarer Attribution auf derselben Seite).
- Scripting bewusst **nicht** gebundelt (ADR-0003): Pine via separates `@luxalgo/vela-pinets` (AGPL, siehe §3).

Was Lightweight-Charts ist:

- Apache-2.0, TradingView-gepflegt, ~915k Downloads/Woche, `v5.2.1` (08/2026) aktiv: `hoveredSeriesOnTop`, Hit-Testing, `tickMarkDensity`, Marker-/Crosshair-Perf-Fixes, AI-Skill. Basis ~35kB, große Community, stabile v5-API (ESM, `addSeries(CandlestickSeries)` etc.).

Empfehlung: **Lightweight-Charts behalten, Vela nicht als Austausch — allenfalls isolierter Prototype.**

- Vela löst euer akutes Problem nicht (dupliziertes Bootstrap, Replay/Indikator-Kontrakte in `viewer/core.py`), sondern kostet Migration: 5 Pages + Fassade + Drawings/Marker/Volume/Profile + Persistenz + Playwright-Tests, bei junger Lib mit Churn-Risiko und Watermark-Pflicht.
- Vela lohnt sich erst, wenn ihr Workspace-Features wirklich braucht (Multi-Chart-Grid/Sync, Object-Tree, native Indikatoren ohne Engine, eigenes Chart-Type-/Layer-SDK).

## 3. PineTS (LuxAlgo/PineTS) vs. PyneCore

Was PyneCore ist (per pynecore.org/PyPI/GitHub verifiziert):

- Python-nativ per AST-Transformation, Apache-2.0, aktiv (v6.5.0, 44 Releases, Push 06/2026).
- Pine-Semantik in Python: `Series`, `var`-Persistenz, Funktions-Isolation, `ta/math/array/matrix/map/string/request/strategy`, Drawings.
- Strategy-Modell implementiert (nicht approximiert): `entry/exit/order/close/cancel`, OCA, Pyramiding, Commission, `risk.*`, `closedtrades/opentrades`.
- CLI `pyne run/compile`, optional Pine→Pyne-Compiler via pynesys.io (API-Key, closed source).
- Passt exakt in eure Python-Pipeline (führt zu `BacktestResult` in `shared/models.py`).

Was PineTS ist (per README/docs verifiziert):

- **Reines TypeScript/JS** (`npm i pinets`): Node/Deno/Bun/Browser. Natives Pine v6 + JS-Syntax (`$.data`/`$.pine`), 60+ TA-Funktionen, Streaming (`stream()`), MTF (`request.security()`), eigene OHLCV-Arrays oder Provider (Binance/FMP/Alpaca). Plots als plain JS-Series für Bots/Alerts/ML.
- **Kein Python-Runtime.** Im Backend nur via Node-Sidecar/Subprozess/IPC nutzbar (Daten-Duplizierung, Determinismus-/Versionsrisiko, kein `BacktestResult`-Kontrakt).
- Lizenz: **dual AGPL-3.0 / kommerziell**. Copyleft greift bei Verteilung **oder** Network-Service (euer FastAPI-Viewer!) — dann Source offenlegen oder kommerzielle Lizenz (`business@luxalgo.com`). Gleiches gilt für `@luxalgo/vela-pinets` (`PineEngine` in-Thread / `PineWorkerEngine` im Worker), bewusst separiert damit Vela-Core Apache-2.0 bleibt.

Empfehlung: **PyneCore behalten, PineTS nicht als Ersatz.**

- PineTS kann PyneCore im Python-Runner nicht ersetzen ohne Brücke, die mehr Probleme schafft als sie löst.
- Sinnvoll allenfalls als **Frontend-Ergänzung**: native Pine-Vorschau im Strategy-Lab / Indikator-Overlay via `Vela ↔ vela-pinets ↔ pinets`, strikt hinter Feature-Flag und **erst nach Lizenz-Entscheidung** (AGPL tragbar? sonst kommerzielle Lizenz klären).

## 4. `src/`-Layout: sinnvoll?

Ja, langfristig sinnvoll, aber nicht dringend. Nutzen: sauberes Packaging (installiert vs. Repo-Root), kein Shadowing, Standard für Hatch/pytest/ruff/CI/Docker. Kosten: alle Run-Befehle, Build-Config, Pfad-Annahmen und ~100 Test-Files anfassen (Import-Strings bleiben gleich, `PROJECT_ROOT`-Annahmen nicht).

### Phasenplan

1. **Vorbereitung (ohne Move):**
   - `hatch.packages` fixen: `live_runner,scheduler,validation` aufnehmen oder bewusstes Weglassen dokumentieren; `strategies/` als Daten (nicht Wheel-Package) deklarieren.
   - `pytest.ini`-Duplikat vs. `pyproject` bereinigen.
   - `TradingViewChart`-Duplikate in eine Fassade ziehen (senkt späteres Vela-Risiko unabhängig vom Move).
2. **Move:**
   - `mkdir src/`, `git mv runner data_tools shared viewer risk validation scheduler live_runner signal_bots src/`.
   - `scripts/`, `strategies/`, `tests/`, `frontend/`, `data/` bleiben oben.
   - `pyproject.toml`: Wheel-Packages auf `src/`-Pfade, `pip install -e .` als Standardweg, `tool.ruff`/`pytest` `pythonpath=["src"]`.
3. **Fixups:**
   - `PROJECT_ROOT`-Annahmen in `shared/constants.py`, `shared/strategy_registry.py`, `tests/conftest.py`, `viewer/core.py`-OHLCV-Pfade auf installiertes Layout + Env-Override (`DATA_DIR`) umstellen.
   - README-Befehle re-runnable machen: `python -m runner.cli run`, `uvicorn viewer.main:app`, `data_tools.catalog_builder`, Grid/Optimizer/Walkforward.
4. **Verifikation:**
   - `pip install -e ".[all]"`, `pytest -q`, `ruff check`, `frontend: tsc -b && vite build`, je 1 Backtest pro Engine (vectorbt/native/pynecore).
5. **Vela/PineTS-Spike (getrennt, optional):**
   - `ChartAdapter`-Interface (`OHLCV+Trades+Indikatoren+Replay`), 1 Page (z.B. `BacktestDetailPage`) hinter Flag auf `VelaWorkspace` mit Offline-`data:`-Bars; PineTS dort mit 1–2 nativen Pine-Skripten testen + Lizenznotiz. Kein Full-Replace ohne dieses Ergebnis.

### Offene Entscheidungen

- Bleiben `strategies/` + `scripts/` oben (Daten/Ops) oder mit unter `src/`?
- Ist AGPL für Viewer/Lab kategorisch ausgeschlossen (dann PineTS/vela-pinets nur kommerziell)?
- Vela-Spike gewünscht, oder erst `src/`-Cleanup + Chart-Fassade und dann neu bewerten?
- Nautilus-Spike gewünscht (als zusätzliche Engine neben nativ/vectorbt/pynecore), oder kein weiterer Backtest-Stack?

## 5. NautilusTrader (nautechsystems/nautilus_trader) — Einordnung

Was NautilusTrader ist (per README + Docs verifiziert):

- Produktionsreife, **Rust-native event-driven Trading-Engine** (deterministisch, Nanosekunden-Auflösung), **Python als Control-Plane** via PyO3-Bindings (`Strategy`-Subklasse, `on_start/on_bar/on_stop`, `BacktestEngine` Low-Level bzw. `BacktestNode` High-Level mit Parquet-Katalog). Rein-Rust-Systeme ebenfalls möglich.
- **Ein Code für Research + Live**: gleiche Strategie-/Actor-/Exec-Algo-Semantik in Backtest (`BacktestEngine`/`BacktestNode`) und Live (`TradingNode`); Doku warnt dennoch explizit vor Backtest/Live-Differenzen (Venue, Transport, Timing, Persistenz, Reconciliation).
- Funktionsumfang: Multi-Venue/-Instrument/-Strategie, Quote-/Trade-Ticks, Bars, Order-Book, Custom-Data; Order-TIF `IOC/FOK/GTC/GTD/DAY/AT_THE_OPEN/AT_THE_CLOSE`, Order-Typen + Conditional-Triggers, `post-only/reduce-only`/Icebergs, Contingency `OCO/OUO/OTO`; Redis-Persistenz optional; eigene Indikatoren (`nautilus_trader.indicators`, z.B. EMA) + native Exec-Algos (z.B. TWAP).
- Adapter-Ökosystem (modular, REST/WebSocket): Binance, Bybit, Coinbase, BitMEX, Deribit, dYdX, Hyperliquid, Kraken, OKX, Polymarket, Interactive Brokers, Databento, Tardis, Betfair, AX u.a. (Status je Adapter `stable/beta/building`, siehe `ADAPTERS.md` + `docs/integrations/`).
- Stand: groß und aktiv — ~28,6k Stars / 3,7k Forks / ~21k Commits, seit 2018, v2-Linie als Release-Candidate (`2.0.0rcN`, `pip install nautilus_trader --pre`), v1 nur noch Security-Backports. Python 3.12–3.14 (passt zu `requires-python = ">=3.12"`), Prebuilt-Wheels (kein Rust-Toolchain nötig), Precision-Modes (Standard 64-bit / High 128-bit).
- Lizenz: **LGPL-3.0-only**. Library-Änderungen müssen offengelegt werden; proprietärer Code, der nur dynamisch gegen die unveränderte Lib linkt/importiert, bleibt möglich. **Kein AGPL-Netzwerk-Copyleft** — reines Self-Hosten/SaaS löst allein keine Offenlegungspflicht aus (anders als PineTS/vela-pinets). Trotzdem: Lizenz-Check vor Produktiveinsatz, CLA bei Contributions.
- Scope-Grenze laut Roadmap: Single-Node Backtest + Live für Einzel-/Kleinteams; **keine UI-Dashboards, kein verteiltes Orchestrieren, kein eingebautes AI/ML-Tooling**.

Was das für dieses Repo bedeutet:

- Nautilus ist **kein Drop-in-Ersatz** für `runner/engines/*`, `live_runner/`, `risk/` oder `viewer/`, sondern ein **eigener schwerer Stack** mit eigenem Daten-/Instrumenten-Modell (`InstrumentId/Venue/BarType`), eigenem Katalog/Persistenz und eigener Live-Schicht. Integration hieße: neue `NautilusAdapter(EngineAdapter)`-Schicht, die Parquet-OHLCV in Nautilus-Objekte (Instrument + Bars/Ticks via Wrangler/Katalog) überführt, Strategien auf Nautilus-`Strategy`-API mappt (oder Wrapper-Strategie für bestehende Signal-Logik schreibt), Engine laufen lässt und Fills/Positions/Account-Report zurück in `BacktestResult` (`shared/models.py`: `Trade/TradeExit/BacktestMeta/EquityPoint`) + `contract.EngineStatsKeys`-kanonische Stats übersetzt. Live-Seite analog gegen `live_runner/BaseLiveAdapter`-Interface.
- Stärken im Vergleich zum Bestand: echter event-driven Simulator statt signal-vektorisiert (vectorbt) bzw. Pine-Emulation (pynecore); Tick-/Book-Treue, Latenz-/Slippage-/Order-Typ-Modellierung, Multi-Venue, identischer Strategiepfad Richtung Live, große Community und viele fertige Venue-Adapter (ersetzt potentiell Teile von `data_tools/downloaders/*` + `live_runner/*`-Eigenbau).
- Kosten/Risiken: Lernkurve (Nautilus-Domänenmodell, `TraderId/Venue/OmsType/AccountType`, Katalog-Schema), Build-/Dep-Gewicht (Rust-Wheels, Versionierung RC, Precision-Mode beachten), Ergebnis-Parität zum Universal-Format muss erst hergestellt + gegen `runner/stats.py`/`validation/` verifiziert werden, Live-Parität (`docs/concepts/live.md`) bleibt trotzdem Arbeit. Kein Ersatz für Viewer/Charts (Nautilus liefert bewusst keine Dashboards — `viewer/` + Lightweight-Charts bleiben).

Empfehlung: **derzeit nicht integrieren oder austauschen; optional als zusätzliche Engine evaluieren.**

- Kurzfristig: Bestand (nativ/vectorbt/pynecore + `live_runner` + `risk/` + `validation/`) behalten — leichtgewichtig, passt zum Universal-Format und Viewer.
- Falls Tick-/Book-Treue, Order-Typ-Realismus oder Multi-Venue-Live wirklich zum Engpass werden: **isolierter Spike** — 1 Strategie (z.B. `example_sma`/EMA-Cross) über `BacktestEngine` mit eigenem Venue (`SIM`/Binance-Spot), Ergebnis nach `BacktestResult` mappen, Stats-Drift gegen native/vectorbt messen, danach entscheiden ob dauerhafter `nautilus`-Engine-Eintrag (`runner/cli.py get_engine`, `strategy_ir/models.py`, Grid/Optimizer/Walkforward-Kompatibilität) lohnt.
- Langfristig denkbar: Nautilus als High-Fidelity-Engine neben den schnellen Engines + als Referenz für `live_runner`-Konsolidierung; Lean/pynecore bleiben für ihre Nischen (LEAN-Bridge, Pine-Kompatibilität).

## Quellen

- `github.com/LuxAlgo/Vela` (README, `docs/user/*`, `docs/architecture/adr/0003-no-default-scripting-engine`).
- `github.com/LuxAlgo/PineTS` (README, `docs.luxalgo.com/developers/pinets/*`, `using-within-charts`, `scripting-engines`).
- `pynecore.org`, `pypi.org/project/pynesys-pynecore`, `github.com/PyneSys/pynecore`.
- `github.com/tradingview/lightweight-charts` (Releases v5.2.1/v5.2.0, npm `lightweight-charts`).
- `github.com/nautechsystems/nautilus_trader` (README `develop`, `MIGRATION_V2.md`, `ADAPTERS.md`, `ROADMAP.md`).
- `nautilustrader.io/docs` (Quickstart, `backtest_low_level`, `backtest_high_level`, `concepts/live`).
