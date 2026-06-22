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
- **Status:** Nicht verifiziert, ob die neuen `risk_parity_universe`-Ergebnisse korrekt im Frontend angezeigt werden.

---

## ❌ Was komplett fehlt / noch nicht angegangen

### 1. Echte End-to-End-Live-Tests
- [x] Orderfreier Live-Smoke-Preflight: `python -m live_runner.preflight --config ... --accounts-env ...` prüft Deployment-Zuordnung, Credential-Verfügbarkeit, Paper/Demo-Modus-Warnungen und cTrader Account-ID-Form, ohne Broker-Orders zu platzieren.
- [x] Alpaca Paper: Minimaler BTC/USD-Paper-Roundtrip auf Nitro, RocketFuel und RamJet ausgeführt; Buy/Sell jeweils filled, danach `remaining_qty=0.0`. Report: `reports/live_smoke_alpaca-crypto-roundtrip_20260622T231721Z.json`.
- [x] cTrader Demo: Account-Discovery + Adapter-Connect gegen echten Demo-Account `47644617` verifiziert; Balance/Equity 100000.00, keine offenen Positionen. Report: `reports/live_smoke_account-check_20260622T231539Z.json`.
- [x] cTrader Demo: Erste EURUSD-Demo-Order auf Account `47644617` platziert, Position `37178928` per `ClosePositionReq` geschlossen und flach verifiziert. Report: `reports/live_smoke_ctrader-demo-roundtrip_20260622T232608Z.json`.
- [ ] Discord → Order: Einen echten Signal-Parsing-Durchlauf mit mindestens einem Bot

### 2. cTrader Token Refresh automatisieren
- [ ] Automatischer Refresh bei 401/expired token
- [ ] Persistenz des neuen Tokens in `.accounts.env` oder etabliertem Secret-Mechanismus
- [ ] Retry-Logik nach Refresh

### 3. Walk-Forward / OOS Validation (generisch)
- [ ] Rollierendes Window (z.B. 12 Monate Train, 3 Monate Test)
- [ ] Aggregierte Metriken (OOS-Sharpe, Worst-DD, profitable Folds %)
- [ ] CLI: `python -m runner.cli walkforward --strategy ...`

### 4. DB-Konsolidierung
- [ ] Legacy `*.trades.json` → SQLite Migration idempotent beim Server-Start
- [ ] `portfolio_optimizer.py` und `grid_tester.py` lesen aus SQLite (nicht mehr aus JSON)
- [ ] Migration-Integration-Test

### 5. Dukascopy Speed / Parallelisierung
- [ ] Aktuell: Single-Threaded-Async, viele 503-Warnungen bei längeren Downloads
- [ ] Ziel: Bulk-Jobs deutlich schneller (Connection-Pool, HTTP/2, Chunked Requests)
- [ ] Alternative: Node.js Sidecar (`scripts/dukascopy_fast/`) als subprocess

### 6. SP500 / US100 Intraday-Daten
- [ ] `strategy-comparisons/intraday_volatility_breakout` braucht SP500 M1 und US100 M15
- [ ] Problem: yfinance M1 nur ~60 Tage frei; Dukascopy hat keine US-Index-CFDs
- [ ] Optionen: Kürzerer Backtest-Zeitraum, Proxy-Symbol (SPY/QQQ), Paid-API (Polygon)

### 7. Viewer UI für Grid/Matrix
- [ ] `MatrixGridPage.tsx` mit Heatmap, Trade-Details, Backtest-ID-Links
- [ ] API-Endpunkte für `/api/grid/{strategy_id}` mit Trade-Level-Daten

### 8. Repo aufräumen
- [ ] Verwaiste Debug-/Scratch-Dateien identifizieren und entfernen
- [ ] Agent-Artefakte bereinigen
- [ ] `.accounts.env` ist bereits aus Git-Tracking entfernt ✅

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
| SP500 (ES=F) | M1 | yfinance | ~60 Tage | ⚠️ Limit |
| US100 (NQ=F) | M15 | yfinance | ~60 Tage | ⚠️ Limit |

---

## 🎯 Nächste Schritte (Priorisiert)

1. **Echte Live-Smoke-Tests** (Alpaca Paper + cTrader Demo)
2. **SP500/US100 Daten** beschaffen oder Proxy-Strategie anpassen
3. **Walk-Forward + OOS** implementieren
4. **DB-Migration** (JSON → SQLite) abschließen
5. **Dukascopy Speed** verbessern
6. **Viewer UI** für Grid/Matrix vervollständigen

---

## 📝 Wichtig

- **`todo/todo.md` ist outdated** (Stand 18. Juni, basierend auf Unit-Tests).
- Diese Datei (`todo/codex-todo.md`) ist der **realistischere** Status.
- Für den ChatGPT-Planning-Prompt siehe `docs/planning-prompt-for-external-model.md`.
