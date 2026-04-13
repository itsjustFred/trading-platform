# GitHub Copilot Instructions - trading-platform

This file is the canonical AI assistant instruction set for this repository.
It captures command usage, architecture flow, and conventions that are easy to miss when reading one file at a time.

## Sources to trust first

- `README.md` (setup, runtime commands, environment variables)
- `pyproject.toml` (Python deps, pytest config, Ruff config)
- `frontend/package.json` and `frontend/vite.config.ts` (frontend scripts and dev proxy)
- `docs/ARCHITECTURE.md` (cross-module flow)
- `docs/DATA_SOURCES.md` (canonical data path and downloader entry points)
- `docs/logging_guide.md` (structured logging contract)
- `runner/cli.py`, `runner/engines/base.py`, `runner/engines/native_adapter.py`, `runner/engines/vectorbt_adapter.py` (engine contracts and execution semantics)
- `shared/constants.py`, `shared/database.py`, `shared/models.py`, `shared/strategy_registry.py` (shared contracts, path resolution, persistence, strategy discovery)
- `viewer/main.py` and `viewer/core.py` (FastAPI API shape + helper payload logic)
- `frontend/src/api/client.ts` and `frontend/src/api/types.ts` (frontend/backend contract)
- `.vscode/mcp.json` (workspace MCP server configuration)

No additional assistant-specific config files are present (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/`, `.windsurfrules`, `.clinerules`, `.cline_rules`, `CONVENTIONS.md`, `AIDER_CONVENTIONS.md`).

## Build, test, and lint commands

### Python backend/tooling

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
```

```bash
# Run a backtest
python -m runner.cli run -e native -s strategies/example_sma/example_sma.py \
  --symbols AAPL --timeframe 1h --start 2024-01-01 --end 2025-12-31

# List available engines
python -m runner.cli engines

# Convert external results into universal format
python -m runner.converters.cli stockkit path/to/trades.csv -o output.trades.json
python -m runner.converters.cli lean path/to/lean_result.json -o output.trades.json

# Start API + UI host
uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log
```

```bash
# Full test suite
.venv/bin/python -m pytest tests -q

# Single test file
.venv/bin/python -m pytest tests/test_stats.py -q

# Single test
.venv/bin/python -m pytest tests/test_runner_cli.py::test_cmd_run_normalizes_timeframe_alias -q
```

```bash
# Lint
.venv/bin/ruff check .
```

### Frontend (`frontend/`)

```bash
cd frontend && npm install
cd frontend && npm run dev
cd frontend && npm run build
cd frontend && npm run lint
```

### Data tools

```bash
python -m data_tools.catalog_builder -o "$DATA_DIR/catalog.json"
python -m data_tools.validate --data-dir "$DATA_DIR"
python -m data_tools.resample --source "$DATA_DIR/ohlcv/<source>/<asset_class>/<symbol>/1h.parquet" --timeframes 4h,1d
```

### Optional UI E2E script

```bash
# Requires viewer server running at http://localhost:5556
PLAYWRIGHT_BROWSERS_PATH=/home/ubuntu/.cache/ms-playwright node tests/e2e_frontend.js
```

## High-level architecture

### End-to-end flow

1. `runner/cli.py` builds `BacktestConfig`, detects strategy type, and dispatches to an engine adapter (`native`, `vectorbt`, `lean`, `pynecore`).
2. Adapters return a canonical `BacktestResult` (`shared/models.py`).
3. Runner persists results both to JSON (`$DATA_DIR/backtests/*.trades.json`) and SQLite (`$DATA_DIR/backtests.db` via `shared/database.py`).
4. `viewer/main.py` initializes DB, runs JSON→SQLite migration on startup, and serves `/api/*` + `/ws/*`.
5. `frontend/src/api/client.ts` (typed by `frontend/src/api/types.ts`) consumes `/api/*`; Vite proxies `/api` and `/ws` to `localhost:5556` in dev.
6. FastAPI serves SPA assets from `frontend/dist` when available, otherwise `viewer/static`.

### Module responsibilities

- `shared/`: canonical models, constants/path resolution, SQLite persistence layer, JSON logging, strategy registry.
- `runner/`: CLI orchestration, engine adapters, trade/stat computation, converters, optimizer/auto-agent.
- `viewer/`: FastAPI routes, request/task orchestration, WebSocket streams, API payload shaping.
- `frontend/`: React SPA for backtests, portfolios, strategies, live, and data management.
- `data_tools/`: source downloaders + validation/resampling/catalog pipeline into canonical OHLCV layout.
- `strategies/`: strategy modules and per-strategy documentation.

## Key conventions specific to this codebase

### Canonical result contract across modules

- `BacktestResult` / `BacktestMeta` / `Trade` / `TradeExit` in `shared/models.py` are the cross-module contract.
- Persisted timestamps are ISO-8601 strings; viewer APIs convert to Unix timestamps for chart payloads.

### Strategy execution contract (native + vectorbt)

- Strategies must expose `generate_signals(...)`.
- Preferred output is tuple/list: `(entries, exits)`, `(entries, exits, indicators_dict)`, or `(entries, exits, indicators_dict, drawings)`.
- Legacy DataFrame output is still supported, but it must contain `entries` and `exits` columns.
- Vectorbt adapter supports both `generate_signals(df, params_dict)` and kwargs-style signatures (`generate_signals(df, fast=..., slow=...)`).
- Signal execution in vectorbt is shifted by one bar (`signals.shift(1)`), so entries/exits execute on next bar open.

### Strategy metadata and discovery

- Strategy discovery is slug-based (`strategies/<slug>/<slug>.py` is the stable pattern).
- `STRATEGY_META` drives display metadata and parameter defaults.
- Strategy type detection uses `STRATEGY_META` (`strategy_type`/`type`) and falls back to class-pattern detection (`*SignalProcessor` with `process_signal`).

### Engine/persistence identity details

- Available engine identifiers are `native`, `vectorbt`, `lean`, `pynecore`.
- Default runner output filename is `{strategy_name}_{YYYYMMDD_%H%M%S}.trades.json` (engine not embedded).
- Engine adapters generate engine-specific `meta.id` prefixes (e.g., `native-...`, `vbt-...`).
- Viewer attempts DB-first reads and falls back to JSON (`backtests/<id>.trades.json`), then backfills SQLite.

### Data directory and file layout

- `DATA_DIR` is the primary env var; `TRADING_DATA_DIR` is fallback if `DATA_DIR` is unset.
- Resolved path is centralized in `shared/constants.py` as `DATA_DIR` and reused everywhere.
- Canonical OHLCV path: `$DATA_DIR/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{timeframe}.parquet`.
- Catalog path: `$DATA_DIR/catalog.json`; SQLite path: `$DATA_DIR/backtests.db`.

### Metric unit semantics

- Runner canonical stats use decimal fractions for ratio-like metrics (`total_return`, `cagr`, `win_rate`, `max_drawdown_pct`).
- Viewer endpoints convert many of these values to percentage points for UI payloads (for example `/api/backtest/{id}/stats`).

### Logging convention

- Use `shared.logging.get_logger(service)` instead of ad-hoc logger setup.
- Attach structured payloads via `extra={"data": {...}}`.
- Logs are JSON lines to stdout and rotating files in `$LOG_DIR/<service>.log` (10 MB, 5 backups).

### Frontend/backend contract discipline

- Keep API shape changes synchronized across:
  - `viewer/main.py`
  - `viewer/core.py` (shared payload helpers)
  - `frontend/src/api/client.ts`
  - `frontend/src/api/types.ts`
- Frontend assumes route and field-name stability; update all four together for API changes.
