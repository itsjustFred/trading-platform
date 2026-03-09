# trading-platform

Universal backtesting framework — run strategies across multiple engines (vectorbt, LEAN, PyneCore) and view results in a unified trade viewer with React frontend.

## Quick Start

```bash
# Clone and setup
git clone <repo-url> && cd trading-platform
python -m venv .venv

# Activate virtual environment
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# Install dependencies
pip install -e ".[all]"
# or: pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env — set TRADING_DATA_DIR to your data location

# Build frontend
cd frontend && npm install && npm run build && cd ..

# Start the viewer
python -m viewer.app --port 5556
# Open http://localhost:5556
# Strategy Lab live conversion runs also require NVIDIA_API_KEY
```

## Data Directory

All runtime data (OHLCV, backtests, catalogs) lives outside the repo in `$TRADING_DATA_DIR` (default: `~/data`):

```
$TRADING_DATA_DIR/
  ohlcv/{source}/{asset_class}/{region}/{symbol}/{timeframe}.parquet
  backtests/*.trades.json
  universes/
  catalog.json
```

Set the `TRADING_DATA_DIR` environment variable (in `.env` or your shell) to customize the location.

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
python -m data_tools.catalog_builder -o "$TRADING_DATA_DIR/catalog.json"

# Migrate CSV data to Parquet
python -m data_tools.migrate_csv --execute
```

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
  app.py             — Flask backend (python -m viewer.app)

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
| `TRADING_DATA_DIR` | `~/data` | Root for OHLCV, backtests, catalogs |
| `LOG_DIR` | `~/logs` | Log file directory |
| `LOG_LEVEL` | `INFO` | Logging level |
| `FLASK_PORT` | `5556` | Viewer server port |
| `NVIDIA_API_KEY` | — | API key for the Strategy Lab auto-conversion agent via NVIDIA NIM |

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
