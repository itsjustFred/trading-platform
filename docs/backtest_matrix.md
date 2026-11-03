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
- `--resume` rehydrates an existing cell JSON file, attaches daily equity/returns, recomputes stats, and skips rerunning the cell — but only when the cell's recorded **grid identity** still holds (see below). A cell that fails the check is recomputed, never reused.
- `--subprocess` runs each grid cell through `runner.cli run` for isolation.
- `--skip-sqlite` is recommended for large matrices to avoid SQLite write contention.

## Grid identity: when a cached cell may be reused

Every finished cell stamps its `.trades.json` with
`meta.data["grid_identity"]` (`runner/grid_identity.py`): the config it was
produced from (engine, strategy file bytes, symbol, timeframe, resolved window,
capital, every risk/modelling knob, strategy parameters, data root) and the
`(mtime, size)` fingerprint of every parquet the cell could have read. Both
halves are hashed into one `fingerprint`.

`--resume` reuses a cell only when that recorded identity equals the identity
of the request as it stands now. It refuses — and recomputes — when:

- the artifact carries no identity, or one from another identity version
  (a legacy artifact promises nothing about what it was computed from);
- any config field differs, including a strategy file edited after the run and
  a risk knob the artifact schema cannot express;
- the underlying frame was re-downloaded or rewritten, even though not one
  artifact field changed;
- either data half could not be resolved (missing = unknown, and unknown is a
  refusal, never "unchanged").

A cell whose identity could not be stamped is reported as **failed**, not
`ok`: an unstamped artifact is one the next `--resume` is obliged to throw away.

The viewer applies the same rule to what it serves.
`/api/backtests/matrix/summary` and `/api/grid/{id}` re-stat the data files each
served row names and answer **409** with the stale rows and the reason, instead
of presenting a ranking computed from data that no longer exists.

## Outputs

Outputs are written to `data/backtests/matrix_runner/` by default:

- `matrix_summary.json` — full run metadata, per-cell statuses, skipped strategies, and aggregate counts.
- `matrix_matrix.csv` — one row per strategy × symbol × timeframe cell with trades, PnL, return, Sharpe, CAGR, drawdown, win rate, and `daily_return_count`.
- `matrix_daily_equity.csv` — daily `equity` and `daily_return` for every successful cell, intended for downstream portfolio allocation / BayesOpt.
- `*/grid_summary.json` and `*/grid_<strategy>_<symbol>_<timeframe>.trades.json` — per-strategy grid outputs in the same format as `runner.cli grid`.

## Normal single backtests

`runner.cli run` and `runner.cli grid` now attach daily equity points and daily returns to every saved backtest JSON before computing stats. This means the same files can be consumed by the portfolio optimizer or external allocation code without reconstructing a daily series from sparse trade timestamps.
