# trading-platform

Universal backtesting framework — run strategies across multiple engines (vectorbt, LEAN, PyneCore) and view results in a unified trade viewer with React frontend.

## Quick Start

```bash
# Clone and setup
git clone <repo-url> && cd trading-platform
python -m venv .venv

# Activate virtual environment
source .venv/bin/activate        # Linux/macOS / Git Bash
# .venv\Scripts\activate         # Windows (PowerShell / cmd)

# Install dependencies
pip install -e ".[all]"
# or: pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — set DATA_DIR (default: ./data)

# Production: build frontend, then start viewer
cd frontend && npm install && npm run build && cd ..
python -m uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log
# Open http://localhost:5556

# Development (two terminals; Vite on :3000 proxies /api to backend on :5556)
# Terminal 1:
python -m uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log
# Terminal 2:
cd frontend && npm install && npm run dev
# Open http://localhost:3000
# Strategy Lab live conversion runs also require NVIDIA_API_KEY
```

## Data Directory

All runtime data (OHLCV, backtests, catalogs) lives in `DATA_DIR` (default: `./data` relativ zum Projekt-Root):

```
$DATA_DIR/
  ohlcv/{source}/{asset_class}/{region}/{symbol}/{timeframe}.parquet
  backtests.db
  backtests/*.trades.json
  universes/
  catalog.json
```

Setze `DATA_DIR` in `.env` oder Shell. Für Legacy-Kompatibilität wird auch `TRADING_DATA_DIR` unterstützt, wenn `DATA_DIR` nicht gesetzt ist.

FastAPI nutzt primär `backtests.db` (SQLite via SQLModel). JSON-Dateien unter `backtests/*.trades.json` bleiben kompatibel und werden beim Start nach SQLite migriert.

## Usage

```bash
# Run a backtest
python -m runner.cli run \
  --engine vectorbt \
  --strategy strategies/example_sma/example_sma.py \
  --symbols AAPL \
  --timeframe 1h \
  --start 2024-01-01 --end 2025-12-31

# Convert existing results
python -m runner.converters.cli stockkit path/to/trades.csv -o output.trades.json
python -m runner.converters.cli lean path/to/lean_result.json -o output.trades.json

# Use the Strategy Lab auto-conversion agent from the web UI
# 1. Export NVIDIA_API_KEY
# 2. Start the viewer
# 3. Open http://localhost:5556/lab

# Build data catalog
python -m data_tools.catalog_builder -o "$DATA_DIR/catalog.json"

# Migrate CSV data to Parquet
python -m data_tools.migrate_csv --execute

# Grid testing for strategy optimization
python -m runner.backtest_grid --strategy strategies/example_sma.py --symbols EURUSD,GBPUSD,USDJPY --timeframes 1h,4h,1d

# Portfolio optimization
python -m runner.portfolio_optimizer --backtest-ids bt_001,bt_002,bt_003

# Walk-forward validation
python -m runner.cli walkforward --strategy example_sma --train-months 12 --test-months 3
```

## Advanced Workflows

- **Grid Testing:** `runner/backtest_grid.py` - Symbol × Timeframe × ParamSet grid execution (canonical engine); `runner/backtest_matrix.py` - multi-strategy orchestration/aggregation over it
- **Portfolio Optimization:** `runner/portfolio_optimizer.py` - Multi-strategy allocation with OOS validation
- **Walk-Forward Analysis:** Stability validation with train/test splits. Note: folds run one fixed parameter set — no in-sample parameter selection happens (see `docs/walk_forward.md`, "Expectation Gap")
- **Signal Bots:** `signal_bots/health_check.py` - Registry/config validation for ramjet, rocketfuel, and nitro processors
- **Live Risk Consistency:** Backtest vs live position sizing behavior and stop-direction validation

Documentation: See `docs/backtest_grid.md`, `docs/portfolio_optimizer.md`, `docs/walk_forward.md`, `docs/signal_bots.md`, and `docs/live-risk-consistency.md`

## Structure

```
runner/              — Backtest runner
  cli.py             — Runner CLI (python -m runner.cli run ...)
  stats.py           — Stats computation (Sharpe, Sortino, Win-Rate, etc.)
  engines/           — Engine adapters
    base.py          — EngineAdapter ABC + BacktestConfig
    vectorbt_adapter — Signal-based vectorized backtesting
    lean_adapter     — QuantConnect LEAN bridge
    pynecore_adapter — PyneCore Pine-style bridge
  converters/        — Result format converters

viewer/              — Trade viewer web app
  main.py            — FastAPI backend (python -m uvicorn viewer.main:app ...)
  core.py            — Internal helper module used by main.py

frontend/            — React SPA (Vite + Tailwind)

shared/              — Shared models and constants
  models.py          — Trade, TradeExit, BacktestMeta, BacktestResult
  constants.py       — DATA_DIR, timeframes, directions, helpers
  logging.py         — Structured JSON logging

data_tools/          — Data management
  config/            — Bundled configuration (genesis dates)
  catalog_builder.py — Scan $TRADING_DATA_DIR → catalog.json
  downloaders/       — Dukascopy, HistData, DoltHub, cTrader, HuggingFace

strategies/          — Per-strategy folders
  <name>/            — Strategy assets
    <name>.py        — Executable strategy module
    README.md        — Strategy dossier / conversion notes

tests/               — Test suite
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `./data` | Root for OHLCV, backtests, catalogs |
| `TRADING_DATA_DIR` | `./data` | Legacy fallback if `DATA_DIR` is unset |
| `LOG_DIR` | `./logs` | Log file directory |
| `LOG_LEVEL` | `INFO` | Logging level |
| `SENTRY_DSN` | — | Sentry project DSN. Empty disables Sentry unless `SENTRY_ENABLED=true` |
| `SENTRY_ENABLED` | auto | Optional override. Defaults to enabled when `SENTRY_DSN` is present |
| `SENTRY_ENVIRONMENT` | `local` | Sentry environment name |
| `SENTRY_RELEASE` | auto | Sentry release. Defaults to package version plus git SHA when available |
| `SENTRY_SAMPLE_RATE` | `1.0` | Error event sample rate from `0.0` to `1.0` |
| `SENTRY_TRACES_SAMPLE_RATE` | — | Optional performance trace sample rate from `0.0` to `1.0` |
| `SENTRY_PROFILES_SAMPLE_RATE` | `0.0` | Optional profile sample rate from `0.0` to `1.0` |
| `SENTRY_SEND_DEFAULT_PII` | `false` | Include default personally identifiable request data in Sentry events |
| `SENTRY_ENABLE_LOGS` | `true` | Send SDK log records to Sentry |
| `SENTRY_DEBUG` | `false` | Enable Sentry SDK debug logging |
| `SENTRY_ATTACH_STACKTRACE` | `true` | Attach stacktraces to captured events |
| `VITE_SENTRY_DSN` | — | Frontend Sentry DSN (same shared project as `SENTRY_DSN`). Empty disables Sentry in the frontend |
| `VITE_SENTRY_ENVIRONMENT` | `local` | Frontend Sentry environment name |
| `VITE_SENTRY_SAMPLE_RATE` | `1.0` | Frontend error event sample rate from `0.0` to `1.0` |
| `VITE_SENTRY_TRACES_SAMPLE_RATE` | — | Optional frontend performance trace sample rate |
| `UVICORN_HOST` | `0.0.0.0` | Recommended host for `python -m uvicorn viewer.main:app` |
| `UVICORN_PORT` | `5556` | Recommended port for `python -m uvicorn viewer.main:app` |
| `NVIDIA_API_KEY` | — | API key for the Strategy Lab auto-conversion agent via NVIDIA NIM |

## FastAPI + WebSocket

- Start server: `python -m uvicorn viewer.main:app --host 0.0.0.0 --port 5556 --no-access-log`
- React dev proxy forwards:
  - `/api` → `http://localhost:5556`
  - `/ws` → `ws://localhost:5556`
- Live terminal uses `GET /ws/tasks/{job_id}` WebSocket stream (tail-like task logs).

## Sentry (Frontend)

The frontend reports to the same shared Sentry project as the backend Python
services (`shared/sentry.py`) and is identified by the `service: frontend` tag.
Release naming mirrors the backend convention:
`trading-platform-frontend@<version>+<git-sha>`, with `<version>` taken from
`frontend/package.json` and `<git-sha>` resolved at build time in
`frontend/vite.config.ts` (compile-time `__APP_VERSION__` / `__GIT_SHA__`
globals).

- Configure via `frontend/.env` (see `frontend/.env.example`): `VITE_SENTRY_DSN`
  (blank disables Sentry locally), `VITE_SENTRY_ENVIRONMENT`,
  `VITE_SENTRY_SAMPLE_RATE`, `VITE_SENTRY_TRACES_SAMPLE_RATE`.
- Initialization lives in `frontend/src/lib/sentryConfig.ts` and is kicked off
  from `frontend/src/main.tsx`. The SDK's default global-handlers integration
  then reports uncaught errors and unhandled rejections automatically.

## Writing a Strategy (vectorbt)

Create a strategy folder such as `strategies/my_strategy/` with a main module
`my_strategy.py`. The preferred vectorbt contract is to return `entries`, `exits`,
and an `indicators_dict` that will be written into each trade's `meta` payload
by the adapter:

```python
def generate_signals(df: pd.DataFrame, params: dict):
    fast = df["close"].rolling(params.get("fast", 10)).mean()
    slow = df["close"].rolling(params.get("slow", 30)).mean()
    entries = ((fast.shift(1) <= slow.shift(1)) & (fast > slow)).fillna(False)
    exits = ((fast.shift(1) >= slow.shift(1)) & (fast < slow)).fillna(False)
    indicators_dict = {
        "SMA_Fast": fast,
        "SMA_Slow": slow,
    }
    return entries, exits, indicators_dict
```

Legacy `DataFrame` returns with `entries` / `exits` columns are still supported for
older strategies, but new or converted strategies should use the tuple format above.

Run with parameters:
```bash
python -m runner.cli run -e vectorbt -s strategies/my_strategy/my_strategy.py --symbols AAPL -tf 1h \
  --params '{"fast": 5, "slow": 20}'
```

## Universal Trade Format

All backtests produce JSON with this structure:
- **meta**: Strategy name, engine, dates, capital, symbols
- **trades**: List of trades with entry/exit info, partial exit support
- **equity_curve**: Optional equity points

See `shared/models.py` for the full schema.
