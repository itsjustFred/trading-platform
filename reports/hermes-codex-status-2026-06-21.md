# Codex Rewrite — Hermes Agent Status Report

**Branch:** `codex_rewrite`
**Basis:** Codex CLI Session (OpenAI OAuth)
**Weitergeführt von:** Hermes Agent
**Modell:** `kilo-auto/free` (Kilo Gateway)
**Datum:** 2026-06-21
**Repo:** `/home/ubuntu/Desktop/trading-platform`

---

## Was gemacht wurde

### Frontend-Refactoring
- 8 Seiten von imperativem `navigate()` + `onClick` auf semantische React Router `<Link>` umgestellt:
  - `BacktestsPage.tsx`
  - `LiveDetail.tsx`
  - `LiveHub.tsx`
  - `LiveViewer.tsx`
  - `OptimizationResultsPage.tsx`
  - `OptimizationsPage.tsx`
  - `PortfoliosPage.tsx`
  - `StrategiesPage.tsx`
- Verbesserte Accessibility und Konsistenz.

### Strategy Comparisons
- `strategy-comparisons/` angelegt mit ersten Configs:
  - `intraday_volatility_breakout` (EURUSD_H1, SP500_M1, US100_M15)
  - `risk_parity` (QuantConnect Report + Strategy-Code)

### Tests
- 63 relevante Tests grün:
  - `test_ctrader_adapter.py`
  - `test_live_execution_safety.py`
  - `test_live_risk_sizing.py`
  - `test_signal_bot_health_check.py`
  - `test_grid_tester_sqlite.py`
  - `test_portfolio_optimizer_oos.py`

### Graph & Docs
- `graphify update .` ausgeführt (4007 Nodes, 11625 Edges, 503 Communities)
- `overnight_report.md` aktualisiert

### Git
- Ursprünglicher Merge auf `hermes-master` durchgeführt und gepusht (siehe `hermes-master` Branch).
- **Wichtig:** `codex_rewrite` bleibt als separater Feature-Branch erhalten.

---

## Nächste Schritte / Offene Punkte

1. **Live Trading Smoke Tests**
   - Alpaca Paper Accounts (`RamJet`, `RocketFuel`, `Nitro`) end-to-end testen
   - cTrader Demo Account verbinden und erste Demo-Order plazieren

2. **Discord Signal Routing**
   - Signal-Bots (`Nitro`, `RamJet`, `RocketFuel`) an die richtigen Alpaca Accounts routen
   - Signal-Parser + Order-Routing verifizieren

3. **cTrader Token Refresh**
   - Automatischen Token-Refresh implementieren/testen
   - `CH_ACCESS_TOKEN_INVALID` Fehler beheben

4. **Portfolio Optimizer & Grid Tester produktiv machen**
   - SQLite-basierte Persistenz
   - UI-Anbindung

5. **Walk-Forward / OOS Validation**
   - Generischer Walk-Forward-Runner
   - Anti-Overfitting-Layer

---

## Usage / Ressourcen

- Provider-Pool: `kimchi` (7 Keys), `bluesminds` (2 Keys), `freemodel` (1 Key), `aerolink` (1 Key)
- Modell-Policy: MiniMax-M3 (via pi/commandcode) für Implementierung, NVIDIA NIM (Kimi 2.6) für Review/Recherche, gpt-5.5 sparsam für Architektur.

---

## Wichtig

- `.accounts.env` ist aus Git-Tracking entfernt und in `.gitignore` eingetragen.
- Secrets werden nicht commited oder im Report ausgegeben.
