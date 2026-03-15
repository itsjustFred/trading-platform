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
- `runner/auto_agent.py`
  - NVIDIA NIM / Kimi-backed strategy conversion worker
  - writes strategy assets into `strategies/<slug>/`
  - launches a validation backtest after generation
- `runner/engines/`
  - `vectorbt_adapter.py`: fast vectorized backtests
  - `pynecore_adapter.py`: Pine-style strategies through PyneCore
  - `lean_adapter.py`: QuantConnect LEAN result ingestion / execution bridge

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
- backtests usually store the slug in `meta.strategies`

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
uvicorn viewer.main:app --host 0.0.0.0 --port 5556
```

Frontend development uses Vite; production/runtime serving uses FastAPI with
built `frontend/dist` assets, plus SPA/static middleware and WebSocket endpoints.
