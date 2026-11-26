# Architecture

Map of the top-level packages. Detail lives in the linked docs; decisions in
`docs/adr/`.

## Packages

- `runner/` — backtesting. `runner/engine.py` + `runner/engine_pkg/` is the
  native event-based engine (`TradeEngine`); `runner/engines/` holds the
  engine adapters (`native_adapter.py` is the single strategy engine going
  forward per the native-single-strategy-engine ADR / #318; `vectorbt`, `pynecore`, `lean` and
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
Helper context files beside the main module may only import a fixed whitelist
(`pandas`, `numpy`, `math`, `statistics`, `decimal`, `datetime`, `typing`,
`collections`, `itertools`, `functools`, `dataclasses`, `__future__`) and
define `build_trade_context` / `build_drawings` — they are compiled inside the
runner process, so the whitelist in
`runner/engines/pynecore_adapter.py` (`HELPER_ALLOWED_IMPORT_ROOTS`) is a
security gate, not a style rule.
LLM-generated strategy code is untrusted until a human approves it: drafts are
statically screened (`runner/codegen_safety.py`), marked, and only runnable
after a SHA256-bound approval (`runner/approval_gate.py`,
`POST /api/strategies/{file}/approve`) — any change to the file after approval
invalidates it. Any new execution route for generated
strategy files MUST call `runner.approval_gate.require_generated_strategy_approval`
(or `runner.auto_agent.run_validated_backtest`) before importing or executing
the module. Engine adapters and grid entry points re-check the approval as a
second layer (`require_generated_strategy_approval` in each adapter and
`runner/backtest_grid.py`), which also re-runs the `codegen_safety` source
screening — defence in depth; the auto-agent validation route runs the approved
backtest in a subprocess with an allowlisted environment
(`runner/agent_process.build_backtest_child_env` — no API keys) and a
wall-clock timeout.

## Conventions worth knowing

- Sizing semantics (`max_position_exposure_pct`, `risk_per_trade_pct`,
  `max_total_exposure`): `docs/sizing-semantics.md`.
- Money-path decision records: `docs/cleanup-16d1-money-path-semantics.md`.
- Reference-parity fixtures spec: `docs/reports/parity-reference-libraries.md`.
- Grid/matrix backtest docs: `docs/backtest_grid.md`, `docs/backtest_matrix.md`.
- Frontend↔backend contract: `frontend/src/api/client.ts` and `types.ts` mirror
  `viewer/main.py`/`viewer/core.py` — keep them in sync when fields change.
- Test sandbox and OHLCV-gated strategy tests: see `AGENTS.md`.

## Optimization and walk-forward invariants

- Portfolio optimization normalizes each equity curve to its first observation
  before correlation, weighting and OOS metrics (`_normalize_equity_matrix` in
  `runner/portfolio_optimizer.py`); persisted portfolio artifacts carry a UUID
  suffix so same-second runs cannot collide.
- Walk-forward steps default to the test-window length; custom step, purge or
  embargo windows require explicit `start`/`end` boundaries so OOS folds
  cannot overlap silently (`runner/walkforward.py` raises otherwise).
- The optimizer's three-way split, embargo and result-payload contract:
  `docs/walk_forward.md`. Its `diagnostics` block (plateau, selection-bias,
  survivorship) never changes which trial wins — it reports how much the
  selection should be trusted.

## Metrics and chart semantics

- `runner/stats.py` keeps max drawdown as a decimal fraction;
  `/api/backtest/<id>/stats` converts it to percentage points for the UI.
- p-value is a one-sided t-test on daily returns against zero.
- The backtest equity endpoint normalizes timestamps, sorts points and
  collapses duplicate timestamps before charting.
