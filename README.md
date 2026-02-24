# trading-platform

Universal backtesting framework — run strategies across multiple engines (LEAN, vectorbt) and view results in a unified trade viewer.

## Quick Start

```bash
# Clone and setup
cd ~/Desktop/trading-platform
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"

# Run a backtest
python -m runner.cli run \
  --engine vectorbt \
  --strategy strategies/example_sma.py \
  --symbols AAPL \
  --timeframe 1h \
  --start 2024-01-01 --end 2025-12-31

# Convert existing results
python -m runner.converters.cli stockkit path/to/trades.csv -o output.trades.json
python -m runner.converters.cli lean path/to/lean_result.json -o output.trades.json

# Start the viewer
python -m viewer.app --port 8080
# Open http://localhost:8080

# Build data catalog
python -m data_tools.catalog_builder -o ~/data/catalog.json

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
  converters/        — Result format converters
    cli.py           — Converter CLI
    lean_converter   — LEAN JSON → universal format
    stockkit_converter — Stockkit CSV → universal format

viewer/              — Trade viewer web app
  app.py             — Flask backend (python -m viewer.app)
  static/index.html  — Frontend (lightweight-charts)

shared/              — Shared models
  models.py          — Trade, TradeExit, BacktestMeta, BacktestResult
  constants.py       — Timeframes, directions, defaults

data_tools/          — Data management
  migrate_csv.py     — CSV → Parquet migration
  catalog_builder.py — Scan ~/data/ → catalog.json

strategies/          — Strategy collection
  example_sma.py     — SMA crossover example

tests/               — Test suite (28 tests)
```

## Data Directory

All data lives in `~/data/`:

```
~/data/
  ohlcv/{source}/{asset_class}/{region}/{symbol}/{timeframe}.parquet
  backtests/*.trades.json
  universes/
  catalog.json
```

## Writing a Strategy (vectorbt)

Create a Python file with a `generate_signals` function:

```python
def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    fast = df["close"].rolling(params.get("fast", 10)).mean()
    slow = df["close"].rolling(params.get("slow", 30)).mean()
    return pd.DataFrame({
        "entries": (fast.shift(1) <= slow.shift(1)) & (fast > slow),
        "exits":   (fast.shift(1) >= slow.shift(1)) & (fast < slow),
    }).fillna(False)
```

Run with parameters:
```bash
python -m runner.cli run -e vectorbt -s my_strategy.py --symbols AAPL -tf 1h \
  --params '{"fast": 5, "slow": 20}'
```

## Universal Trade Format

All backtests produce JSON with this structure:
- **meta**: Strategy name, engine, dates, capital, symbols
- **trades**: List of trades with entry/exit info, partial exit support
- **equity_curve**: Optional equity points

See `shared/models.py` for the full schema.
