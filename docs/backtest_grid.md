# Backtest Grid Runner

Run one strategy over a symbol/timeframe matrix with the same risk settings for every cell.

## Default universe

```text
Symbols:    XAUUSD, SP500, NAS100, EURUSD, USDJPY, OIL
Timeframes: 1m, 5m, 15m, 1h, 4h, 1d
Start:      2020-01-01
```

Aliases used for data downloads:

```text
SP500  -> US500
NAS100 -> USTEC
OIL    -> USOIL
```

## Download data

The downloader fetches one-minute Dukascopy data once per canonical symbol and resamples locally to the other grid timeframes.

```bash
python -m data_tools.download_grid_market_data \
  --symbols XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL \
  --timeframes 1m,5m,15m,1h,4h,1d \
  --start 2020-01-01 \
  --workers 6
```

Useful options:

- `--end YYYY-MM-DD` to cap the download.
- `--workers N` for parallel symbol downloads.
- `--batch-size N` and `--pause-ms N` to tune Dukascopy batch behavior.
- `--oil-type UKOIL` if Brent is desired instead of WTI.
- `--dry-run` to print the planned download plan without network calls.

## Run a grid backtest

```bash
python -m runner.cli grid \
  --strategy strategies/trend_following/trend_following.py \
  --symbols XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL \
  --timeframes 1m,5m,15m,1h,4h,1d \
  --start 2020-01-01 \
  --capital 100000 \
  --risk-pct 0.01 \
  --max-workers 4
```

Every cell uses the same engine/strategy/risk path as a normal `runner.cli run` call. The grid runner executes cells in-process by default for speed and keeps the normal JSON output format; use `--subprocess` if you need subprocess isolation. For large grids, `--skip-sqlite` is recommended to avoid SQLite write contention.

Recommended fast verification run:

```bash
python -m runner.cli grid \
  --strategy strategies/example_sma/example_sma.py \
  --symbols XAUUSD,SP500,NAS100,EURUSD,USDJPY,OIL \
  --timeframes 1m,5m,15m,1h,4h,1d \
  --start 2020-01-01 \
  --end 2020-12-31 \
  --engine vectorbt \
  --capital 100000 \
  --risk-pct 0.01 \
  --max-workers 6 \
  --skip-sqlite
```

Outputs are written to `data/backtests/grid_runner/`:

- `grid_summary.json` — full per-cell status, return codes, stats, and errors.
- `grid_matrix.csv` — compact matrix for comparing assets/timeframes.
- `grid_<strategy>_<symbol>_<timeframe>.trades.json` — one normal backtest output per cell.

Each saved JSON now also contains daily `equity_curve` points and `stats.daily_returns`, so sparse trade-based equity curves are suitable for Sharpe/correlation/portfolio-allocation work.

For a quick smoke test, limit the universe and period:

```bash
python -m runner.cli grid \
  --strategy strategies/trend_following/trend_following.py \
  --symbols XAUUSD,US500 \
  --timeframes 1h,4h \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --risk-pct 0.01 \
  --max-workers 2
```

## Run a strategy matrix across all strategies

`runner.cli matrix` wraps the grid runner for every selected strategy and writes:

```text
matrix_summary.json      — all strategy/cell results and per-strategy grid summaries
matrix_matrix.csv        — one row per strategy × symbol × timeframe cell
matrix_daily_equity.csv  — daily equity/returns for downstream portfolio allocation
<strategy_slug>/...      — per-strategy grid outputs from runner.cli grid
```

Example for a fast smoke run of the SMA strategy on one symbol/timeframe:

```bash
python -m runner.cli matrix \
  --strategy example_sma \
  --symbols XAUUSD \
  --timeframes 1h \
  --start 2025-01-01 \
  --end 2025-12-31 \
  --engine vectorbt \
  --capital 100000 \
  --risk-pct 0.01 \
  --max-workers 1 \
  --skip-sqlite
```

Omit `--strategy` to run all discovered market-data strategies. Repeating `--strategy` filters to multiple slugs/names/paths.
