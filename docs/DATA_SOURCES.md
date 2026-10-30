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
| Dukascopy | `python -m data_tools.downloaders.jforex_dl` (fallback: `dukascopy_native`) | Main downloader path for FX, metals, indices, energy | `forex`, `metals`, `indices`, `energy` |
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

- The viewer download API launches `jforex_dl` for source `dukascopy` and falls back to `dukascopy_native` when JForex/JPype/JVM is unavailable.
- Once the JForex JVM is started it is never explicitly shut down: the process
  exits and the OS reclaims it. JPype's interpreter-exit hook is disabled
  (`jpype.config.onexit = False`, armed by `jforex_dl` immediately before it
  starts the JVM) because an in-teardown JVM detach deadlocks in HotSpot's
  `Threads::destroy_vm()` — this previously hung `pytest` after its summary until
  CI's job timeout. The flag is process-global, so it is set at the JVM start
  path rather than at import. A JVM started in one process cannot be restarted
  after a shutdown, so treat the start as one-shot per interpreter.
- It can merge bid/ask exports for `tick` and `s1` using `--merge-tick` or
  `--merge-s1`.
- Dukascopy coverage is exposed per timeframe in the API response
  (`coverage.<timeframe>.available/start`) so the UI can show explicit
  availability rows like `tick ✅ seit 2003-05-04` and `h4 ❌ Not found`.
- Requested start dates are validated only against known timeframe-specific
  genesis entries before the job starts.
- Bulk downloads can raise `--pipeline-batches` to keep multiple URL batches in
  flight before the inter-batch pause is applied. This improves long M1/tick
  jobs while preserving the existing throttling backoff.

### Example

```bash
python -m data_tools.downloaders.dukascopy_native \
  --symbols EURUSD,XAUUSD \
  --timeframes h1,d1 \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --price-type bid \
  --pipeline-batches 2
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

Files: `data_tools/downloaders/yfinance_dl.py` (live downloader), `data_tools/migrate_csv.py` (legacy CSV import)

### Current truth

The live downloader module is `data_tools/downloaders/yfinance_dl.py`
(`YFinanceDownloader`). The `yfinance` source name also appears in data paths
and in some UI labels because the platform stores equity datasets under:

```text
$TRADING_DATA_DIR/ohlcv/yfinance/equity/US/{symbol}/{timeframe}.parquet
```

Existing CSV archives are additionally populated through
`data_tools/migrate_csv.py`.

### 4h bucket convention (DV-B20)

yfinance only provides 60m bars for the `4h` output timeframe. The downloader
resamples those onto the canonical UTC-aligned 4h grid (00:00 UTC origin;
labels 00/04/08/12/16/20 UTC) and keeps the provider's native bucket size in
the `source_bucket_timeframe` column. Cross-source comparisons must resample
all sources onto the same bucket convention first.

### Offer-side semantics (Dukascopy tick)

Hourly Dukascopy tick files (`.bi5`) carry **both** offer sides per record, so
the native downloader fetches each file once and splits bid/ask afterwards.
For `tick` timeframes the `offer_side`/`price_type` value is therefore
**informational only**: it is validated (`bid`, `ask`, `both`) but does not
change which files are downloaded or which columns the output contains. This
differs from the JForex engine, which rejects `offer_side='both'` for tick
outputs because it queries one side per request. Bar (`m1`+) downloads use the
side to pick the per-side URL set, so there `price_type` is load-bearing.

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

- `dukascopy` -> `python -m data_tools.downloaders.jforex_dl` with automatic fallback to `data_tools.downloaders.dukascopy_native`
- `jforex` -> `python -m data_tools.downloaders.jforex_dl` with automatic fallback to `data_tools.downloaders.dukascopy_native`
- `dolthub` -> `python -m data_tools.dolthub_manager update`
- `yfinance` -> `python -m data_tools.downloaders.yfinance_dl`
  (`YFinanceDownloader`; CSV archives are additionally migrated via
  `data_tools/migrate_csv.py`)

The data pages and strategy/backtest runners ultimately consume normalized
parquet files plus `catalog.json`, not the raw source-specific formats.
