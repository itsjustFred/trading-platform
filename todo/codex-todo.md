# Codex Rewrite — Was gemacht wurde, was fehlt

**Branch:** `codex_rewrite`
**Basis:** Codex CLI Session (OpenAI OAuth)
**Datum:** 2026-06-21
**Repo:** `/home/ubuntu/Desktop/trading-platform`

---

## ✅ Was Codex fertig gemacht hat (Code existiert + verifiziert)

### 1. Auto-Download-Layer
- **Dateien:** `data_tools/auto_download.py`, `runner/cli.py`, `runner/backtest_grid.py`
- **Inhalt:** Gemeinsame Coverage-Prüfung vor Backtests. Fehlende OHLCV-Parquets werden automatisch via passendem Downloader nachgeladen (Dukascopy für FX, yfinance für US-Equities, etc.).
- **Verifiziert:** `python -m compileall` erfolgreich.

### 2. Risk-Parity-Optimizer erweitert
- **Dateien:** `runner/optimizer.py`, Frontend (`OptimizeModal`, API-Types)
- **Inhalt:**
  - `optimization_mode="risk_parity_universe"` hinzugefügt
  - Feste Symbol-Listen (`symbol_universes`) möglich
  - Optuna wählt pro Trial ein Universum
  - Speichert `symbols` und `best_symbols` pro Trial
- **Verifiziert:** `python -m compileall` + `npm run build` erfolgreich.

### 3. Matrix/Grid stabilisiert
- **Dateien:** `runner/backtest_grid.py`, `runner/backtest_matrix.py`, `shared/database.py`, `scripts/generate_matrix_grid_overview.py`
- **Inhalt:**
  - Stabile `run_id` als `BacktestMeta.id` pro Matrix-Zelle
  - `--result-id` wird an Subprocess-Runs weitergegeben
  - `backtest_id` + `trade_data_available` in Grid-Summaries
- **Verifiziert:** `python -m compileall` erfolgreich.

### 4. Frontend: React Router Link-Migration
- **Dateien:** 8 Seiten (StrategiesPage, BacktestsPage, LiveHub, LiveDetail, LiveViewer, OptimizationsPage, OptimizationResultsPage, PortfoliosPage)
- **Inhalt:** Imperative `navigate()` + `onClick` durch semantische `<Link>` ersetzt. Mittelklick/Strg-Klick funktioniert jetzt.
- **Verifiziert:** `npm run build` erfolgreich.

### 5. Strategy Comparisons angelegt
- **Pfad:** `strategy-comparisons/`
- **Inhalt:**
  - `intraday_volatility_breakout/` — HTML-Reports + Charts für EURUSD_H1, SP500_M1, US100_M15
  - `risk_parity/` — QuantConnect-Report + Strategy-Code

### 6. Daten beschafft
- **Risk Parity ETFs (Daily, 2016–2024):** SPY, QQQ, EFA, EEM, TLT, IEF, LQD, TIP, GLD, DBC, VNQ ✅ via yfinance
- **EURUSD H1 (2018–2026):** ✅ via Dukascopy native download (52.575 Zeilen)
- **Status:** Parquet-Dateien liegen unter `/mnt/block-volume-data/ohlcv/` und sind über `data/catalog.json` erfasst.

### 7. Tests & Tooling
- **Unit-Tests:** 63+ Tests grün (ctrader_adapter, live_execution_safety, live_risk_sizing, signal_bot_health_check, grid_tester_sqlite, portfolio_optimizer_oos)
- **Graphify:** `graphify update .` ausgeführt (4007 Nodes, 11625 Edges, 503 Communities)
- **Git:** Branch `codex_rewrite` sauber aufgesetzt, `hermes-master` gemergt.

---

## ⚠️ Teilweise fertig / Code vorhanden, aber nicht live verifiziert

### 1. cTrader Live-Integration
- **Code:** `live_runner/ctrader_adapter.py`, `live_runner/daemon.py`, `live_runner/account_discovery.py`
- **Status:** Unit-Tests grün + Account-Discovery und Adapter-Connect gegen echte cTrader Demo-API am 2026-06-22 erfolgreich (`47644617`, Balance/Equity 100000.00, 0 Positionen). Siehe `reports/live_smoke_account-check_20260622T231539Z.json`.
- **Risiko:** Erste echte cTrader Demo-Order + Fill ist noch offen. Token-Refresh (`CH_ACCESS_TOKEN_INVALID`) ist im Code vorhanden, aber ob er gegen den echten Refresh-Endpoint funktioniert, ist weiter unklar, weil `.accounts.env` keinen `CTRADER_REFRESH_TOKEN` enthält.

### 2. Alpaca Live-Integration
- **Code:** `live_runner/alpaca_adapter.py`
- **Status:** Unit-Tests grün + orderfreier Connectivity-Preflight gegen echte Alpaca Paper-Accounts am 2026-06-22 erfolgreich (Nitro, RocketFuel, RamJet; paper=true; Balance/Positions lesbar). Siehe `reports/live-smoke-progress-2026-06-22.md`.
- **Risiko:** Echte Paper-Demo-Order mit fill/cancel/close/reconcile ist noch offen; Account-Keys selbst sind gültig genug für Account-/Positionsabfragen.

### 3. Discord Signal Routing
- **Code:** `live_runner/signal_sources/discord_source.py`, `signal_bots/`
- **Status:** Health-Check 5/5 OK (Bots erreichbar). **Nicht** getestet, ob Signale geparsed und zu Orders routen.
- **Risiko:** Signal-Format könnte sich geändert haben; Mapping Nitro→Nitro, RamJet→RamJet, RocketFuel→RocketFuel ist konfiguriert, aber nicht end-to-end verifiziert.

### 4. Portfolio Optimizer UI-Anbindung
- **Code:** Backend existiert, Frontend-Seiten (`PortfoliosPage.tsx`, `OptimizationsPage.tsx`) existieren
- **Status:** Verifiziert am 2026-06-23: Optimizer-API-Summary reicht `optimization_mode`, `symbols`, `best_symbols` und `symbol_universes` weiter; Optimizer-Archiv und Detailseite zeigen Modus, bestes Universum, Kandidaten-Universen und Trial-Symbole an. `npm run build` und `tests/test_viewer_features.py::test_optimizations_list_and_detail_endpoints` grün.

---

## ❌ Was komplett fehlt / noch nicht angegangen

### 1. Echte End-to-End-Live-Tests
- [x] Orderfreier Live-Smoke-Preflight: `python -m live_runner.preflight --config ... --accounts-env ...` prüft Deployment-Zuordnung, Credential-Verfügbarkeit, Paper/Demo-Modus-Warnungen und cTrader Account-ID-Form, ohne Broker-Orders zu platzieren.
- [x] Alpaca Paper: Minimaler BTC/USD-Paper-Roundtrip auf Nitro, RocketFuel und RamJet ausgeführt; Buy/Sell jeweils filled, danach `remaining_qty=0.0`. Report: `reports/live_smoke_alpaca-crypto-roundtrip_20260622T231721Z.json`.
- [x] cTrader Demo: Account-Discovery + Adapter-Connect gegen echten Demo-Account `47644617` verifiziert; Balance/Equity 100000.00, keine offenen Positionen. Report: `reports/live_smoke_account-check_20260622T231539Z.json`.
- [x] cTrader Demo: Erste EURUSD-Demo-Order auf Account `47644617` platziert, Position `37178928` per `ClosePositionReq` geschlossen und flach verifiziert. Report: `reports/live_smoke_ctrader-demo-roundtrip_20260622T232608Z.json`.
- [x] Discord → Order: Kontrollierter Replay-Smoke mit Discord-Parser, Channel-Mapping, Deployment-Symbolfilter, Risk-Sizing und FakeAdapter-Order-Submission ausgeführt. Report: `reports/live_smoke_discord-replay_20260622T233019Z.json`.

### 2. cTrader Token Refresh automatisieren
- [x] Automatischer Refresh bei 401/expired token: `CTraderLiveAdapter` erkennt terminale Token/Auth-Fehler (`CH_ACCESS_TOKEN_INVALID`, invalid/expired token, unauthorized) und nutzt Refresh-Retry für Symbols, Order, Balance, Reconcile und Order-Details.
- [x] Persistenz des neuen Tokens in `.accounts.env` oder etabliertem Secret-Mechanismus: Refresh aktualisiert Runtime-Env und schreibt `CTRADER_ACCESS_TOKEN`/`CTRADER_REFRESH_TOKEN` über den konfigurierten Accounts-Env-Pfad zurück.
- [x] Retry-Logik nach Refresh: Requests werden nach erfolgreichem Refresh einmal wiederholt; Account-Auth baut den Auth-Request nach Refresh mit dem neuen Access Token neu. Verifiziert mit `tests/test_ctrader_adapter.py` und `tests/test_live_preflight.py`.

### 3. Walk-Forward / OOS Validation (generisch)
- [x] Rollierendes Window (z.B. 12 Monate Train, 3 Monate Test): `runner.walkforward.run_walkforward` baut echte rollierende Train/Test-Folds und führt pro Fold IS/OOS-Backtests über den gewählten Engine-Adapter aus.
- [x] Aggregierte Metriken (OOS-Sharpe, Worst-DD, profitable Folds %): Summary enthält Fold-Anzahl, durchschnittlichen OOS-Sharpe, Worst-OOS-Drawdown und profitable Fold-%.
- [x] CLI: `python -m runner.cli walkforward --strategy ...` akzeptiert Engine, Symbols, Timeframe, Start/Ende, Risk-/Leverage-Parameter, Params JSON und Output-Dir. Verifiziert mit `tests/test_walkforward.py` und `tests/test_runner_cli.py`.

### 4. DB-Konsolidierung
- [x] Legacy `*.trades.json` → SQLite Migration idempotent beim Server-Start: `viewer.main` startet die Migration im Lifespan, `migrate_json_to_sqlite()` überspringt bestehende Backtest-IDs und zählt `skipped` explizit.
- [x] `portfolio_optimizer.py` und `grid_tester.py` lesen aus SQLite (nicht mehr aus JSON): Portfolio-Equity lädt über `load_backtest_result`; Grid-Tester bietet `load_grid_results()` über `grid_results`.
- [x] Migration-Integration-Test: `tests/test_database_migrations.py` prüft wiederholte Legacy-JSON-Migration ohne doppelte Trade-/Equity-Zeilen; zusätzlich grün mit `tests/test_portfolio_optimizer_oos.py` und `tests/test_grid_tester_sqlite.py`.

### 5. Dukascopy Speed / Parallelisierung
- [x] Aktuell: Single-Threaded-Async, viele 503-Warnungen bei längeren Downloads: Native Downloader unterstützt jetzt `pipeline_batches`, behält Backoff/Cooldown bei und kann mehrere URL-Batches pro Pause-Fenster parallel halten.
- [x] Ziel: Bulk-Jobs deutlich schneller (Connection-Pool, HTTP/2, Chunked Requests): `download_bi5_files()` skaliert Connector/Semaphore auf `batch_size * pipeline_batches`; `download_grid_market_data` nutzt standardmäßig `--pipeline-batches 2`.
- [x] Alternative: Node.js Sidecar (`scripts/dukascopy_fast/`) als subprocess: nicht nötig; bestehender Python/aiohttp-Pfad wurde erweitert und mit `tests/test_dukascopy_downloader.py` sowie `tests/test_grid_market_data_download.py` verifiziert.

### 6. Viewer UI für Grid/Matrix
- [x] `MatrixGridPage.tsx` mit Heatmap, Trade-Details, Backtest-ID-Links: Seite lädt Matrix/Grid-Daten über API, Zellen und Tabellen-Rows verlinken auf Backtest-Details und zeigen Trade-Samples/Trade-API an.
- [x] API-Endpunkte für `/api/grid/{strategy_id}` mit Trade-Level-Daten: FastAPI-Endpunkt filtert Strategie-Rows, ergänzt Backtest-/Trades-URLs und lädt Trade-Samples aus SQLite. Verifiziert mit `tests/test_viewer_features.py` und `npm run build`.

### 7. Repo aufräumen
- [x] Verwaiste Debug-/Scratch-Dateien identifizieren und entfernen: getrackte Backup-Datei `viewer/static/index.html.bak` entfernt; unversionierte supersedete cTrader-Fehlversuchsreports bereinigt.
- [x] Agent-Artefakte bereinigen: `.github/agents/*`, `runner/auto_agent.py` und Tests als getrackte Projektfeatures eingeordnet; keine wegwerfbaren Agent-Artefakte mehr im Git-Status.
- [x] `.accounts.env` ist bereits aus Git-Tracking entfernt ✅

### 8. Strategy-Comparison-Nacharbeit
- [x] Plattform-Backtests gegen `strategy-comparisons/` gestartet und Abweichungen dokumentiert. Report: `reports/strategy-comparison-platform-check-2026-06-23.md`.
- [x] Runner/Viewer-Auto-Download korrigiert: `SP500`, `NAS100` und `US100` werden als Dukascopy-Index-CFD-Aliase behandelt statt als yfinance/HuggingFace-US-Aktien.
- [x] `volatility_breakout` näher an die MT5-Referenz angepasst: Long/Short-Breakouts, ATR-Multiplier 0.25, ATR-Stop, Regimefilter und Trading-Hours.

---

## 📊 Daten-Status Übersicht

| Symbol | Timeframe | Quelle | Zeitraum | Status |
|--------|-----------|--------|----------|--------|
| EURUSD | H1 | Dukascopy | 2008–2026 | ✅ 52.575 Zeilen |
| SPY | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| QQQ | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| EFA | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| EEM | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| TLT | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| IEF | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| LQD | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| TIP | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| GLD | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| DBC | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| VNQ | D1 | yfinance | 2016–2024 | ✅ 2.263 Tage |
| SP500 / US500 CFD | M1 | Dukascopy | Intraday-CFD verfügbar | ✅ Quelle vorhanden |
| US100 / NAS100 CFD | M15 | Dukascopy | Intraday-CFD verfügbar | ✅ Quelle vorhanden |

---

## 🎯 Nächste Schritte (Priorisiert)

1. **Walk-Forward + OOS** implementieren
2. **DB-Migration** (JSON → SQLite) abschließen
3. **Dukascopy Speed** verbessern
4. **Viewer UI** für Grid/Matrix vervollständigen

---

## 📝 Wichtig

- **`todo/todo.md` ist outdated** (Stand 18. Juni, basierend auf Unit-Tests).
- Diese Datei (`todo/codex-todo.md`) ist der **realistischere** Status.
- Für den ChatGPT-Planning-Prompt siehe `docs/planning-prompt-for-external-model.md`.

---

## 🔧 Offene Punkte (2026-07-14)

**Discord:** Tokens in `.accounts.env` sind **Account-Tokens (User-Tokens)**, keine Bot-Tokens.
`discord.py 2.5.2` Bot-Client lehnt sie ab ("Improper token"). Gegen die **Discord-REST-API**
funktionieren sie aber (GET /channels/{id}/messages → HTTP 200, echte Signale: ENLT overbought,
APLD oversold/Buy). Fix: `DiscordSignalSource` von discord.py-Gateway auf REST-Polling umstellen
(Vorbild `stockkit_ai_bot/src/common/discord.py` `fetch_recent_messages`). Parser (`discord_source.py`)
ist bereits OK (getestet). → Codex-Implementierung nötig + Channel→Account-Mapping klären.

**cTrader-Verify:** `scripts/verify_ctrader_order.py` angelegt (ProtoOAOrderDetailsReq by order id,
Retry-Logik). Unabhängige Re-Query am 14:21 UTC gescheitert: `demo.ctraderapi.com:5035` **Timeout**
(Endpoint flaky, nicht Code). Execution-Time-Beweis steht: Deal `27678592`, Position `38833309`,
filled 100000, Close `ProtoOAExecutionEvent` (11:16 UTC). Verifier bei Endpoint-Verfügbarkeit erneut
starten → Ergebnis in PR.

---

## ✅ Live-Trading E2E — echte Broker-Trades (2026-07-14)

Ursprungs-Goal erfüllt: Strategien führen **live echte Trades** aus. Verifiziert mit
`scripts/live_smoke.py --mode live-stockkit-e2e` (Codex-CLI orchestriert, eigenständig verifiziert):

- **Alpaca PAPER (Discord StockKit → `submit_order_with_risk`):** AAPL Pre-Market, Buy+Sell gefillt,
  `filled_qty=0.6302`, `remaining_qty=0`, `extended_hours=true`. Route `nitro` (PA3E767TJ91D).
- **cTrader DEMO (Strategie `volatility_breakout` → `submit_order_with_risk`):** EURUSD,
  `filled_volume=100000`, Position geschlossen (`ProtoOAExecutionEvent`), `remaining_volume=0`.

pytest: **82 passed**, keine neue Regression. Codex hat bewusst nicht committet/pusht — separat nachgeholt.

Reports: `live_integration_report.json`, `reports/live_smoke_live-stockkit-e2e_20260714T111634Z.json`.

Code-Änderungen (dieser Branch): `live_runner/alpaca_adapter.py` (extended-hours LimitOrder),
`live_runner/ctrader_adapter.py` (Token-Refresh-Safety), `scripts/live_smoke.py`
(`live-stockkit-e2e` + cTrader-Subprocess-Worker).
