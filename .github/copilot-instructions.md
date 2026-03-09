# GitHub Copilot Instructions - trading-platform

This file is the canonical AI assistant instruction set for this repository.
It refines the existing instructions with corrected command expectations, naming details, and cross-module data flow.

## Sources to trust first

- `README.md` (quick start and CLI usage)
- `pyproject.toml` (Python deps, pytest config, ruff config)
- `frontend/package.json` and `frontend/vite.config.ts` (frontend scripts and API proxy)
- `docs/logging_guide.md` (structured logging usage and env vars)
- `docs/DATA_SOURCES.md` (data source and path conventions)
- `docs/ARCHITECTURE.md` (current system architecture and module responsibilities)
- `docs/pynecore_evaluation.md` (PyneCore integration constraints)

No additional assistant-specific config files were found (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/`, `.windsurfrules`, `.clinerules`, `.cline_rules`, `CONVENTIONS.md`, `AIDER_CONVENTIONS.md`).

## Build, test, and lint commands

### Python (runner/shared/viewer/data_tools/strategies)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
```

```bash
# Run a backtest
python -m runner.cli run -e vectorbt -s strategies/example_sma/example_sma.py \
  --symbols AAPL --timeframe 1h --start 2024-01-01 --end 2025-12-31

# List available engines
python -m runner.cli engines

# Convert external results into universal format
python -m runner.converters.cli stockkit path/to/trades.csv -o output.trades.json
python -m runner.converters.cli lean path/to/lean_result.json -o output.trades.json

# Start Flask API + web UI server
python -m viewer.app --port 5556
```

```bash
# Full test suite
.venv/bin/python -m pytest tests -q

# Single test file
.venv/bin/python -m pytest tests/test_stats.py -q

# Single test
.venv/bin/python -m pytest tests/test_models.py::TestTrade::test_simple_roundtrip -v
```

```bash
# Lint
.venv/bin/ruff check .
```

### Frontend (React/TypeScript in `frontend/`)

```bash
cd frontend && npm install
cd frontend && npm run dev
cd frontend && npm run build
cd frontend && npm run lint
```

### Data tools (used by viewer data-management endpoints)

```bash
python -m data_tools.catalog_builder -o "$TRADING_DATA_DIR/catalog.json"
python -m data_tools.validate --data-dir "$TRADING_DATA_DIR"
python -m data_tools.resample --source "$TRADING_DATA_DIR/ohlcv/<source>/<asset_class>/<symbol>/1h.parquet" --timeframes 4h,1d
```

### Optional UI E2E script

```bash
# Requires viewer server running at http://localhost:5556
PLAYWRIGHT_BROWSERS_PATH=/home/ubuntu/.cache/ms-playwright node tests/e2e_frontend.js
```

Current baseline in this repository state:
- `.venv/bin/python -m pytest tests -q` passes.
- `cd frontend && npm run build` passes.
- `cd frontend && npm run lint` currently reports existing violations.
- `.venv/bin/ruff check .` currently reports existing violations.

## High-level architecture

### End-to-end flow

1. `runner/cli.py` builds `BacktestConfig` and dispatches to an engine adapter (`vectorbt`, `lean`, `pynecore`).
2. The adapter returns a universal `BacktestResult` (`shared/models.py`).
3. Runner persists output under `$TRADING_DATA_DIR/backtests/*.trades.json`.
4. `viewer/app.py` loads stored results and exposes API endpoints (`/api/backtests`, `/api/backtest/<id>/...`, `/api/strategies`, `/api/portfolios`, `/api/data/*`).
5. `frontend/src/api/client.ts` consumes `/api/*`; in dev, Vite proxies `/api` to `http://localhost:5556`.
6. Viewer serves built frontend from `frontend/dist` when present, else falls back to `viewer/static`.

### Module responsibilities

- `shared/`: canonical data models and centralized JSON logging.
- `runner/`: CLI orchestration, engine adapters, stats, and import converters.
- `viewer/`: Flask API and async job/task orchestration for runs and data operations.
- `frontend/`: React SPA for backtests, strategies, portfolios, data management, and logs.
- `data_tools/`: catalog build, validation, resampling, and downloader integrations.
- `strategies/`: strategy implementations consumed by adapters.

## Key conventions specific to this codebase

### Universal data contract and serialization

- Cross-module boundary format is always `BacktestResult`/`BacktestMeta`/`Trade`/`TradeExit` from `shared/models.py`.
- Persisted timestamps are ISO-8601 strings; viewer transforms them to Unix seconds for chart payloads.

### Strategy contract (vectorbt adapter)

- Strategy modules must define:
  ```python
  def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame
  ```
- Required columns: `entries`, `exits` (boolean-like).
- Optional columns consumed by adapter logic: `direction`, `stop_loss`, and ATR-like columns.
- Optional module-level `STRATEGY_META` is read when present.

### Engine adapter contract

- All engines subclass `runner/engines/base.py::EngineAdapter` and implement `run(config, strategy_path) -> BacktestResult`.
- `validate_config` returns a list of errors; CLI exits if any errors are returned.

### Data directory and path conventions

- Base data root: `TRADING_DATA_DIR` env var (default `~/data`).
- The resolved path is centralized in `shared/constants.py` as `DATA_DIR`. All modules import from there.
- OHLCV path shape: `$TRADING_DATA_DIR/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{timeframe}.parquet`
- Backtests are read from `$TRADING_DATA_DIR/backtests/*.trades.json`.
- Catalog lives at `$TRADING_DATA_DIR/catalog.json`.
- Dukascopy genesis dates: bundled baseline in `data_tools/config/dukascopy_genesis.json`, runtime overrides in `$TRADING_DATA_DIR/dukascopy_genesis.json`.

### ID and filename nuances

- Default runner output filename in `runner/cli.py`:
  `{strategy_name}_{YYYYMMDD_%H%M%S}.trades.json` (engine is not part of filename).
- `vectorbt_adapter` `meta.id` format:
  `vbt-{strategy_name}-{YYYYMMDD-%H%M%S}`.
- Direction values are uppercase `LONG`/`SHORT`.
- Valid timeframes are defined in `shared/models.py` and `shared/constants.py`.

### Metric unit semantics

- In `runner/stats.py`, max drawdown is a decimal fraction (e.g., `0.12`).
- In viewer API responses for UI (`/api/backtest/<id>/stats`), drawdown is converted to percentage points (multiplied by 100).

### Logging convention

- Use `shared.logging.get_logger(service)` instead of ad-hoc logger setup.
- Attach structured payloads with `extra={"data": {...}}`.
- Logs are JSON lines to stdout and rotating files in `$LOG_DIR/<service>.log` (10 MB, 5 backups).

### Frontend/backend contract discipline

- Keep API shape changes synchronized between:
  - `viewer/app.py`
  - `frontend/src/api/client.ts`
  - `frontend/src/api/types.ts`
- This repository relies on API field names matching frontend expectations; update all three in the same change.
