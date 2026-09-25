# ARCHITECTURE

This is the current "state of the union" for the trading-platform repository.

## Stack summary

- **Backend API:** FastAPI (`viewer/main.py`)
- **Frontend:** React + TypeScript + Vite (`frontend/`)
- **Primary backtest engine:** native event-based engine (`runner/engines/native_adapter.py`)
- **Additional engines:** vectorbt, PyneCore and LEAN adapters
- **Performance/statistics:** `runner/stats.py`, QuantStats-derived metrics, custom p-value / PSR handling
- **Persistence:** SQLite (`$DATA_DIR/backtests.db`) + JSON compatibility (`$DATA_DIR/backtests/*.trades.json`)

## End-to-end flow

```text
strategy source
    |
    v
runner/cli.py
    |
    +--> vectorbt adapter
    +--> pynecore adapter
    +--> lean adapter
    |
    v
shared.models.BacktestResult
    |
    v
$DATA_DIR/backtests.db
    |
    v
viewer/main.py
    |
    v
frontend/src/api/client.ts
    |
    v
React pages (backtests, strategies, portfolios, data, lab)
```

## Backend layers

### `shared/`

Shared models and cross-cutting helpers.

- `shared/models.py`
  - canonical dataclasses for trades, exits, backtest metadata, equity points, stats
- `shared/constants.py`
  - resolves `DATA_DIR`, `LOG_DIR`, timeframe constants, executable lookup
- `shared/logging.py`
  - structured JSON logging
- `shared/strategy_registry.py`
  - strategy discovery, stable slugs, markdown lookup, and `STRATEGY_META` rename support

### `runner/`

Execution and conversion layer.

- `runner/cli.py`
  - constructs `BacktestConfig`
  - dispatches to an engine adapter
  - persists results to `$DATA_DIR/backtests` (JSON) and `$DATA_DIR/backtests.db` (SQLite)
- `runner/stats.py`
  - computes Sharpe, Sortino, Calmar, drawdown, PSR, p-value, and related metrics
- `runner/backtest_grid.py`
  - canonical grid execution engine: one strategy over a symbol × timeframe grid;
    every cell runs through the same validation/statistics/risk/SQLite path as a
    manual backtest
- `runner/backtest_matrix.py`
  - multi-strategy orchestration/aggregation wrapper over `backtest_grid`
    (matrix CSVs, daily-equity export) plus the legacy single-strategy quick grid
    (`run_strategy_grid`)
- `runner/auto_agent.py`
  - NVIDIA NIM / Kimi-backed strategy conversion worker (orchestration loop)
  - companion modules: `runner/agent_prompts.py` (LLM prompts),
    `runner/agent_response_parsing.py` (response extraction + STRATEGY_META
    normalization), `runner/agent_backtest_target.py` (target resolution +
    artifact paths), `runner/agent_process.py` (backtest subprocess control)
  - writes screened draft assets into `strategies/<slug>/` and returns an awaiting-approval result
  - leaves execution to `runner.auto_agent.run_validated_backtest` after human approval
- `runner/engine.py` + `runner/engine_pkg/`
  - `runner/engine.py` is the public import shim (`TradeEngine`,
    `ExecutionModelSettings`, `OrderType`, `Order`, `Position`); the
    implementation lives in `runner/engine_pkg/`:
    `trading_engine.py` (bar loop, order lifecycle, risk sizing),
    `execution_model.py` (fill/trigger assumptions recorded per trade),
    `orders.py` (Order/Position dataclasses), `result.py`
    (indicator presentation + `BacktestResult` assembly)
  - refuses to overwrite an existing strategy slug
  - generated drafts carry a provenance marker and are not executable until approved
  - approved execution runs with an allowlisted child environment (no API keys) and a wall-clock timeout
- `runner/codegen_safety.py`
  - static screening of LLM-generated strategy code before it is written or executed
  - denies process spawning, dynamic execution, network egress, untrusted deserialization, and reflection escape hatches
- `runner/approval_gate.py`
  - human review gate for generated strategy code (Decision Record 2026-08-31)
  - approvals live beside the code as `<file>.approved.json` and are bound to the file's SHA256 hash
  - any change to the file after approval invalidates the approval
  - REST surface: `GET /api/strategies/{file}/approval` and `POST /api/strategies/{file}/approve`
- `runner/engines/`
  - `vectorbt_adapter.py`: fast vectorized backtests (per-symbol processing in
    `_process_symbol`, per-trade sizing in `_size_trade`; drawing and
    indicator-series presentation shared via `_drawings.py`/`_indicator_series.py`)
  - `pynecore_adapter.py`: Pine-style strategies through PyneCore
  - `lean_adapter.py`: QuantConnect LEAN result ingestion / execution bridge
- Sizing semantics: `max_position_exposure_pct` (position-size), `risk_per_trade_pct`
  (max loss per trade) and `max_total_exposure` (aggregate exposure limit) have one
  documented meaning each — see `docs/sizing-semantics.md`. `max_position_pct` /
  `--max-position-pct` remain as deprecated aliases.

### Optimization and walk-forward artifacts

Portfolio optimization normalizes each equity curve to its first observation
before correlation, weighting, and OOS metrics. Persisted portfolio artifacts
carry a UUID suffix to avoid same-second collisions. Walk-forward steps default
to the test-window length; custom steps, purge windows, and embargo windows
require explicit start and end boundaries so OOS folds cannot overlap silently.

### `viewer/`

Operational FastAPI application.

- Lists backtests, strategies, portfolios, logs, and data catalog entries
- Builds equity/drawdown payloads for charts
- Reconstructs missing equity curves from trades when needed
- Starts background jobs for:
  - data downloads
  - resampling
  - strategy auto-conversion
- Serves the built frontend from `frontend/dist`
- Streams task logs over WebSocket (`/ws/tasks/{job_id}`)

## Frontend structure

The SPA lives in `frontend/src/`.

### Routing

- `App.tsx` registers page routes
- `Sidebar.tsx` drives the left navigation

### Main pages

- `StrategiesPage.tsx`
  - strategy catalog, filters, sort options
- `StrategyDetailPage.tsx`
  - strategy metadata, rendered markdown dossier, related backtests/portfolios
- `BacktestsPage.tsx`
  - sortable backtest list with quick stats and sparklines
- `BacktestDetailPage.tsx`
  - stats grid, equity curve, benchmark overlay, trade table
- `PortfoliosPage.tsx` / `PortfolioDetailPage.tsx`
  - combined portfolio analytics
- `StrategyLabPage.tsx`
  - raw-code input + live terminal for the autonomous conversion worker
- `DataManagementPage.tsx`
  - downloader, catalog, and resampling UI

### API contract

`frontend/src/api/client.ts` mirrors backend endpoints. When fields change in the
backend, the following files must stay synchronized:

- `viewer/main.py`
- `viewer/core.py` (shared helper logic)
- `frontend/src/api/client.ts`
- `frontend/src/api/types.ts`

## Data subsystem

### Canonical data path

```text
$DATA_DIR/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{timeframe}.parquet
```

### Supporting tools

- `data_tools/catalog_builder.py`
  - scans the data directory and writes `catalog.json`
- `data_tools/validate.py`
  - validates parquet structure and dates
- `data_tools/resample.py`
  - derives higher timeframes from an existing parquet source
- `data_tools/downloaders/*`
  - source-specific importers / downloaders
- `data_tools/dolthub_manager.py`
  - syncs and exports Dolthub repos into canonical parquet files

## Strategy layout

Strategies are now grouped by folder:

```text
strategies/
  short_term_reversal/
    short_term_reversal.py
    README.md
  pynecore_orb/
    pynecore_orb.py
    README.md
    pynecore_orb.toml
    pynecore_orb_context.py
```

Conventions:

- the folder name is the stable **slug**
- the main Python file is `<slug>.py`
- optional documentation lives in `README.md`
- helper context files stay beside the main module
- helper context files may only contain imports (pandas, numpy, math, statistics, decimal, datetime, typing, collections, itertools, functools, dataclasses) plus `build_trade_context` / `build_drawings` functions — they are compiled in the runner process
- backtests usually store the slug in `meta.strategies`

### LLM-generated strategies (trust boundary)

Strategy code produced by the auto-conversion agent is untrusted until a human
has reviewed it:

1. the generated module is statically screened (`runner/codegen_safety.py`);
   unscreenable or banned code never reaches disk,
2. the module is written as a draft with a generated-strategy marker and no
   approval file, so it is never runnable through an execution route,
3. `POST /api/strategies/{file}/approve` records a SHA256-bound approval after
   human review, and
4. only then can the strategy run; if the file changes afterwards, the
   approval is void and the gate refuses execution.

The approved execution route is `runner.auto_agent.run_validated_backtest`.
Engine and grid execution boundaries also reject marked drafts that lack a
valid approval, and re-apply static screening as defense in depth. Any new
execution route for generated strategy files MUST call
`runner.approval_gate.require_generated_strategy_approval` (or
`run_validated_backtest`) before importing or executing the module.

## Metrics and chart semantics

- `runner/stats.py` keeps max drawdown as a decimal fraction
- `/api/backtest/<id>/stats` converts drawdown to percentage points for the UI
- p-value is computed from daily returns using a one-sided t-test against zero
- the backtest equity endpoint normalizes timestamps, sorts points, and collapses duplicate timestamps before charting

## Deployment / local runtime

Typical local workflow:

```bash
.venv/bin/python -m pytest tests -q
cd frontend && npm run build
uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log
```

Frontend development uses Vite; production/runtime serving uses FastAPI with
built `frontend/dist` assets, plus SPA/static middleware and WebSocket endpoints.
