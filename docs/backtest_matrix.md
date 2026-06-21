# Backtest Matrix Runner

Run the symbol/timeframe backtest grid across every strategy in `strategies/`.

The matrix runner wraps the normal grid runner, so each strategy/cell uses the same validation, statistics, risk sizing, output JSON, and optional SQLite persistence as `runner.cli run` and `runner.cli grid`.

## Default universe

```text
Strategies: all importable market-data strategies under strategies/
Symbols:    XAUUSD, SP500, NAS100, EURUSD, USDJPY, OIL
Timeframes: 1m, 5m, 15m, 1h, 4h, 1d
Start:      2020-01-01
```

Strategies that cannot be imported because an optional dependency is missing are recorded in `skipped_strategies` instead of aborting the whole matrix.

## Download data

```bash
python -m data_tools.download_grid_market_data \
  --symbols XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL \
  --timeframes 1m,5m,15m,1h,4h,1d \
  --start 2020-01-01 \
  --workers 6
```

## Run the full strategy matrix

```bash
python -m runner.cli matrix \
  --engine vectorbt \
  --max-workers 6 \
  --timeout-seconds 3600 \
  --skip-sqlite
```

Useful options:

- `--strategy STRATEGY` repeatable to limit the matrix to one or more strategy slugs/names/paths.
- `--symbols` and `--timeframes` override the default universe.
- `--start` / `--end` cap the backtest period.
- `--resume` rehydrates existing cell JSON files, attaches daily equity/returns, recomputes stats, and skips rerunning the cell.
- `--subprocess` runs each grid cell through `runner.cli run` for isolation.
- `--skip-sqlite` is recommended for large matrices to avoid SQLite write contention.

## Outputs

Outputs are written to `data/backtests/matrix_runner/` by default:

- `matrix_summary.json` — full run metadata, per-cell statuses, skipped strategies, and aggregate counts.
- `matrix_matrix.csv` — one row per strategy × symbol × timeframe cell with trades, PnL, return, Sharpe, CAGR, drawdown, win rate, and `daily_return_count`.
- `matrix_daily_equity.csv` — daily `equity` and `daily_return` for every successful cell, intended for downstream portfolio allocation / BayesOpt.
- `*/grid_summary.json` and `*/grid_<strategy>_<symbol>_<timeframe>.trades.json` — per-strategy grid outputs in the same format as `runner.cli grid`.

## Normal single backtests

`runner.cli run` and `runner.cli grid` now attach daily equity points and daily returns to every saved backtest JSON before computing stats. This means the same files can be consumed by the portfolio optimizer or external allocation code without reconstructing a daily series from sparse trade timestamps.
