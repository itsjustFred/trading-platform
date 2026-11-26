# Architecture

Map of the top-level packages. Detail lives in the linked docs; decisions in
`docs/adr/`.

## Packages

- `runner/` — backtesting. `runner/engine.py` + `runner/engine_pkg/` is the
  native event-based engine (`TradeEngine`); `runner/engines/` holds the
  engine adapters (`native_adapter.py` is the single strategy engine going
  forward per ADR 0001 / #318; `vectorbt`, `pynecore`, `lean` and
  `signal_replay` adapters are frozen). Also the CLI (`cli.py`), grid/matrix
  runners, optimizer, walk-forward and `stats.py`. Contracts:
  `docs/cross-engine-backtest-contract.md`, `docs/sizing-semantics.md`,
  `docs/cleanup-16d1-money-path-semantics.md`.
- `shared/` — canonical models (`models.py`), SQLite persistence
  (`database.py`), constants (`DATA_DIR`, timeframes), money/fill-key/lot-grid
  helpers, logging, Sentry, indicators. Largest and most depended-on package.
- `data_tools/` — OHLCV data: `catalog_builder.py`, `validate.py`,
  `resample.py`, `auto_download.py`, `dolthub_manager.py`,
  `downloaders/`. Layout: `docs/DATA_SOURCES.md`.
- `risk/` — position sizing and risk gating (`engine.py`, `live.py`,
  `models.py`, `config.py`) shared by backtest and live paths.
- `live_runner/` — live trading: `daemon.py`, broker adapters
  (`alpaca_adapter.py`, `ctrader_adapter.py`, `base_adapter.py`),
  reconciliation/kill-switch/loss-breaker, `signal_sources/`, preflight.
  Semantics: `docs/live-risk-consistency.md`; ops:
  `docs/live-deploy-runbook.md`, `docs/live-go-live-checklist.md`.
- `viewer/` — FastAPI backend (`main.py` + `core.py`) serving the API,
  WebSocket task/live streams, and the built frontend.
- `frontend/` — React + TypeScript + Vite SPA (`src/`).
- `validation/` — imports external backtest reports (MT5 XLSX/HTML,
  QuantConnect) into `ValidationReference` and compares them against
  `BacktestResult` (`importers/`, `comparison/`, `risk/`, `stats/`,
  `readiness/`). Schemas: `docs/mt5_xlsx_schema.md`.
- `signal_bots/` — Discord signal processors (`ramjet/`, `rocketfuel/`,
  `nitro/`). See `docs/signal_bots.md`.
- `scheduler/` — APScheduler jobs (`scheduler.py`, `jobs/`).
- `strategies/` — strategy implementations; data for the runner, discovered
  via `shared/strategy_registry.py` (see "Strategy layout" below).
- `scripts/` — ops one-offs (no package). `tests/` — pytest suite.
  `docs/` — this documentation.

## End-to-end flow

```text
strategy source -> runner/cli.py -> engine adapter -> shared.models.BacktestResult
    -> $DATA_DIR/backtests.db -> viewer/main.py -> frontend/src/api/client.ts -> React pages
```

Live signals flow the other direction: `signal_bots/`/`live_runner/signal_sources/`
-> `live_runner/daemon.py` -> broker adapter -> ledger in `shared/database.py`.

## Strategy layout

```text
strategies/<slug>/<slug>.py   (+ optional README.md, helper context files)
```

The folder name is the stable slug; backtests store it in `meta.strategies`.
LLM-generated strategy code is untrusted until a human approves it: drafts are
statically screened (`runner/codegen_safety.py`), marked, and only runnable
after a SHA256-bound approval (`runner/approval_gate.py`,
`POST /api/strategies/{file}/approve`).

## Conventions worth knowing

- Sizing semantics (`max_position_exposure_pct`, `risk_per_trade_pct`,
  `max_total_exposure`): `docs/sizing-semantics.md`.
- Money-path decision records: `docs/cleanup-16d1-money-path-semantics.md`.
- Reference-parity fixtures spec: `docs/reports/parity-reference-libraries.md`.
- Test sandbox and OHLCV-gated strategy tests: see `AGENTS.md`.
