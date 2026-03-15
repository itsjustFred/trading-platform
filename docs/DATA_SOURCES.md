# DATA SOURCES

This document describes the data sources that currently exist in the repository,
how they are ingested, and where they land inside `$TRADING_DATA_DIR`.

## Canonical storage layout

All normalized OHLCV data is stored as parquet under:

```text
$TRADING_DATA_DIR/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{timeframe}.parquet
```

Examples:

```text
~/data/ohlcv/dukascopy/forex/EURUSD/1h.parquet
~/data/ohlcv/yfinance/equity/US/AAPL/1d.parquet
~/data/ohlcv/dolthub/equity/US/MSFT/1d.parquet
~/data/ohlcv/huggingface/crypto/BTCUSD/1h.parquet
```

The catalog builder reads that tree and writes `catalog.json`:

```bash
python -m data_tools.catalog_builder -o "$TRADING_DATA_DIR/catalog.json"
```

## Source overview

| Source | Primary entrypoint | Current role | Typical asset classes |
| --- | --- | --- | --- |
| Dukascopy | `python -m data_tools.downloaders.dukascopy_native` | Main downloader for FX, metals, indices, energy | `forex`, `metals`, `indices`, `energy` |
| Dolthub | `python -m data_tools.dolthub_manager update` and `python -m data_tools.downloaders.dolthub_dl` | Repo sync plus targeted SQL exports | `equity`, `options`, fundamentals |
| HuggingFace | `python -m data_tools.downloaders.huggingface_dl` | Direct parquet / dataset ingestion | `equity`, `crypto`, dataset-dependent |
| HistData | `python -m data_tools.downloaders.histdata_dl` | Free historical FX import | `forex` |
| cTrader | `python -m data_tools.downloaders.ctrader_dl` | CSV import path for cTrader exports | `forex`, `index` |
| yfinance | `python -m data_tools.migrate_csv --execute` | Legacy import path into canonical storage | `equity` |

## Dukascopy

File: `data_tools/downloaders/dukascopy_native.py`

### What it uses

- `aiohttp` for async `.bi5` file downloads directly from `datafeed.dukascopy.com`
- `data_tools/config/dukascopy_genesis.json` plus optional runtime overrides in
  `$TRADING_DATA_DIR/dukascopy_genesis.json`
- Source metadata in `$TRADING_DATA_DIR/source_meta.json`

### What it supports

- Forex majors and crosses
- Metals (`XAUUSD`, `XAGUSD`)
- Indices (`US30`, `US500`, `USTEC`)
- Energy (`UKOIL`, `USOIL`)
- Tick, second, minute, hour, day, and monthly frames depending on instrument

### Notable behavior

- The viewer download API launches it directly for source `dukascopy`.
- It can merge bid/ask exports for `tick` and `s1` using `--merge-tick` or
  `--merge-s1`.
- Requested start dates are clamped by genesis dates so the UI can reject
  impossible ranges before the job starts.

### Example

```bash
python -m data_tools.downloaders.dukascopy_native \
  --symbols EURUSD,XAUUSD \
  --timeframes h1,d1 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --price-type bid
```

## Dolthub

Files:

- `data_tools/dolthub_manager.py`
- `data_tools/downloaders/dolthub_dl.py`

### Two distinct workflows

1. **Repo sync/export path** (`dolthub_manager.py`)
   - Used by the viewer for source `dolthub`
   - Clones or updates repos under `$TRADING_DATA_DIR/dolthub/`
   - Exports canonical parquet files under:
     - `ohlcv/dolthub/equity/US/{symbol}/1d.parquet`
     - `fundamentals/dolthub/earnings/*.parquet`
     - `fundamentals/dolthub/rates/*.parquet`

2. **Targeted REST query exporter** (`dolthub_dl.py`)
   - Uses the Dolthub HTTP API for narrow symbol/table exports
   - Best for scripted one-off extracts

### Repositories currently encoded in the repo

- `post-no-preference/stocks`
- `post-no-preference/earnings`
- `post-no-preference/options`
- `post-no-preference/rates`

### Example

```bash
python -m data_tools.dolthub_manager update
```

```bash
python -m data_tools.downloaders.dolthub_dl \
  --repo post-no-preference/stocks \
  --symbol AAPL \
  --start 2024-01-01 \
  --end 2024-12-31
```

## HuggingFace

File: `data_tools/downloaders/huggingface_dl.py`

### What it uses

- HuggingFace dataset parquet endpoints
- Optional `datasets` library fallback
- Optional `HF_TOKEN`

### Current behavior

- Downloads parquet-backed datasets or streams rows via the hub fallback.
- Writes normalized parquet under `ohlcv/huggingface/...`.
- Ships with a short allowlist of known finance datasets, but also accepts a
  dataset argument.

### Example

```bash
python -m data_tools.downloaders.huggingface_dl \
  --dataset edarchimbaud/timeseries-1d-sp500 \
  --symbol AAPL \
  --timeframe 1d
```

## HistData

File: `data_tools/downloaders/histdata_dl.py`

### Current role

- Imports free FX minute data from histdata.com
- Resamples `1m` to higher frames locally after download
- Writes into `ohlcv/histdata/forex/{symbol}/{timeframe}.parquet`

### Example

```bash
python -m data_tools.downloaders.histdata_dl --symbol EURUSD --years 2020-2025
```

## cTrader

File: `data_tools/downloaders/ctrader_dl.py`

### Current role

- Primarily a CSV import bridge
- Reads local cTrader exports and normalizes them into parquet
- There is no fully implemented authenticated live cTrader download path in
  this repo today

### Example

```bash
python -m data_tools.downloaders.ctrader_dl \
  --symbol EURUSD \
  --timeframe 1h \
  --csv-file /path/to/export.csv
```

## yfinance

File: `data_tools/migrate_csv.py`

### Current truth

There is **no dedicated live `yfinance_dl.py` module** in the repository at the
moment. The `yfinance` source name still appears in data paths and in some UI
labels because the platform already stores equity datasets under:

```text
$TRADING_DATA_DIR/ohlcv/yfinance/equity/US/{symbol}/{timeframe}.parquet
```

Those files are currently populated by migrating existing CSV archives through
`data_tools.migrate_csv.py`.

### What gets migrated

- Legacy LEAN equity CSVs into `1h`, `15m`, and `1d`
- Stockkit CSVs into `5m`
- Shared universe files into `$TRADING_DATA_DIR/universes/`

### Example

```bash
python -m data_tools.migrate_csv --execute
```

## Viewer integration

The FastAPI viewer uses these source names in `/api/data/download`:

- `dukascopy` -> `python -m data_tools.downloaders.dukascopy_native`
- `dolthub` -> `python -m data_tools.dolthub_manager update`
- `yfinance` -> legacy source label; current repo data is populated via
  `data_tools.migrate_csv.py`, not a direct downloader module

The data pages and strategy/backtest runners ultimately consume normalized
parquet files plus `catalog.json`, not the raw source-specific formats.
