# Status Check — 2026-03-04

## API Endpoints
| Endpoint | Status | Notes |
|----------|--------|-------|
| GET /api/strategies | ✅ | 16 strategies |
| GET /api/backtests | ✅ | 31 backtests, sparklines + stats |
| GET /api/portfolios | ✅ | 3 portfolios, stats as % |
| GET /api/logs | ✅ | 2 log files |
| GET /api/data/overview | ✅ | 6 categories (ETFs, Energy, Equities, Forex, Indices, Metals) |
| GET /api/data/dukascopy/genesis | ✅ | Reads `~/data/dukascopy_genesis.json`, returns per-symbol + per-timeframe genesis and computed lower bound |
| GET /api/symbols | ✅ | ~278KB response |
| GET /api/benchmark/SPY | ✅ | Works |
| GET /api/benchmark/QQQ | ✅ | Fixed — 6787 data points |
| GET /api/benchmark/GLD | ✅ | Works |
| GET /api/backtest/{id}/trade/{nr} | ✅ | OHLCV (49+ bars) + trade data |

## Frontend Pages
| Page | Status | Notes |
|------|--------|-------|
| Strategies | ✅ | 16 strategies with tags, filter works |
| Backtests | ✅ | 31 backtests, sparklines, Sharpe/CAGR/DD/PnL/PSR stats |
| Backtest Detail | ✅ | Stats cards (incl PSR), interactive equity chart with hover tooltip, benchmark overlay, trade table |
| Portfolios | ✅ | 3 portfolios, sparklines + stats |
| Data Management | ✅ | 6 categories (no "Other"), accordion expand, Download/Resample, Dukascopy start-date lower bound now reacts to symbol + timeframe |
| Logging | ✅ | Log file list, content display |
| Create Backtest Modal | ✅ | Strategy/engine/symbol/risk-pct fields |
| Trade Chart Page | ✅ | Candlestick chart (lightweight-charts v5), entry/exit markers, volume, price lines, keyboard nav, Trade Context panel for vectorbt + pynecore metadata |

## Known Issues (Updated 2026-03-03)
1. ~~**XAUUSD miscategorized**~~ ✅ Fixed — Override in `_derive_category()`, now shows as Metals
2. ~~**QQQ benchmark missing**~~ ✅ Fixed — Downloaded via yfinance (6787 bars, 1999-2026)
3. ~~**Trade chart placeholder**~~ ✅ Fixed — Full candlestick chart with lightweight-charts v5
4. ~~**Risk sizing all-in**~~ ✅ Fixed — Risk-per-trade with stop-loss or fractional sizing
5. ~~**No PSR**~~ ✅ Fixed — PSR in stats.py, app.py, and frontend cards/detail
6. ~~**Equity chart static SVG**~~ ✅ Fixed — Interactive lightweight-charts with hover tooltip (date, equity, return, drawdown)
7. ~~**"Other" category**~~ ✅ Fixed — SPY/VIX → Indices, no more "Other"
8. ~~**No indicator overlays**~~ ✅ Fixed — trade endpoint now returns parsed/computed indicators (`type` + `style`) and TradeChart renders overlay/panel indicators

## Data Inventory
| Category | Symbols | Size |
|----------|---------|------|
| ETFs | 23 (incl QQQ) | ~1.5 MB |
| Energy | 2 (UKOIL, USOIL) | 0.81 MB |
| Equities | 20,055 | 929.49 MB |
| Forex | 6 | 6.5 MB |
| Indices | 5 (US30, US500, USTEC, SPY, VIX) | 1.0 MB |
| Metals | 2 (XAUUSD, XAGUSD) | 1.0 MB |
| **Dolthub Repos** | stocks, earnings, options, rates | ~13 GB |

## Update — 2026-03-04 (Targeted Fixes)

### 1) Risk-Sizing (Critical)
- `runner/engines/vectorbt_adapter.py` updated:
  - keeps risk sizing with `risk_amount = equity * risk_per_trade_pct`
  - fallback order when no explicit stop-loss: ATR-like signal column -> fixed 2% stop distance
  - hard cap enforced: max position notional `<= equity * 20%`
  - warning log added when fixed default stop is used
- Verification run:
  - command: `python -m runner.cli run ... --risk-pct 0.01`
  - output: fallback warnings visible
  - result check: worst trade loss ≈ `0.728%` of 300k baseline; max notional aligns with 20% cap logic

### 2) Data Management — Dolthub Repos
- `viewer/app.py`:
  - added scan of `~/data/dolthub/{stocks,earnings,options,rates}` with real recursive size
  - `/api/data/overview` now includes dedicated category **Dolthub Repos**
  - `/api/data/category/Dolthub%20Repos` returns repo rows (size/path/last_updated)
- `frontend/src/pages/DataManagementPage.tsx`:
  - size formatting now auto-switches MB/GB
  - Dolthub repo rows are rendered and shown with true GB sizes
- Verification:
  - API shows 4 repos, total ~13136 MB
  - Firefox page test confirms Dolthub Repos category + GB display

### 3) Dukascopy Downloader Migration to dukascopy-node
- Installed: `npm install -g dukascopy-node`
- Rewrote `data_tools/downloaders/dukascopy_dl.py` to use CLI subprocess:
  - `dukascopy-node` CSV output -> pandas -> Parquet
  - output path unchanged: `~/data/ohlcv/dukascopy/{asset_class}/{symbol}/{timeframe}.parquet`
  - supports required classes/timeframes (Forex/Metals/Indices/Energy; 1m/5m/15m/1h/4h/1d)
  - logs requested start/end range
  - writes requested range metadata to `~/data/source_meta.json` for catalog integration
- `viewer/app.py` download endpoint now forwards optional `start/end` to Dukascopy downloader.
- `DataManagementPage` download modal now includes optional Start/End fields.
- Verification:
  - manual downloader run succeeded (`EURUSD 1d`, bounded date range)
  - `/api/data/download` task run confirms log contains requested_start/requested_end
  - `source_meta.json` contains `dukascopy/forex/EURUSD/1d` with requested range metadata

### Build
- `npm run build` ✅ (Vite production build successful)

## Update — 2026-03-04 (Risk Leverage + Dukascopy UI Polish + Indicator Parsing)

### 1) Risk Management Deepdive (SuperOSC)
- Trade `#118` analyzed (`pine_superosc_vumanchu_20260304_091438`):
  - entry `4191.415`, exit `4181.755`, qty `26.1685`, pnl `-252.79`
  - notional `~109,683` on `~109,430` equity, move `-0.2305%` (small realized loss is mathematically consistent)
- Implemented configurable risk controls:
  - `BacktestConfig`: added `max_position_pct` + `max_leverage`
  - `runner.cli`: added `--max-position-pct` + `--max-leverage`
  - `/api/backtests/run`: now forwards both params
  - vectorbt sizing now supports cap + leverage path:
    - default cap: equities/ETFs `1.0`, non-equities `0.20` (unless explicitly set)
    - leverage used when required to meet risk sizing (bounded by `max_leverage`)
    - underfilled-risk warnings when leverage limit prevents full target risk
- Verification:
  - cap-only run (`max_position_pct=0.2`, `max_leverage=0.2`) -> first trade notional ratio `0.2`
  - leverage run (`max_position_pct=0.2`, `max_leverage=2.0`) -> first trade notional ratio `0.5`
  - API backtest run with these params completed successfully.

### 2) Dukascopy Download UI Polish
- Created `frontend/src/components/DownloadModal.tsx` and wired it into Data Management page.
- Modal improvements:
  - timeframe selector chips/buttons: `tick, sec, m1, m5, m15, m30, h1, h4, d1, mn1`
  - Dukascopy-specific `price_type` selector (`Bid`/`Ask`, default Bid)
  - instrument autocomplete via curated common instrument/symbol list
  - explicit default-start hint shown when start date is empty
- Backend/Downloader updates:
  - `/api/data/download` accepts `price_type` and forwards to downloader
  - `dukascopy_dl.py` accepts `--price-type`, supports timeframe aliases (`sec->s1`, `m1`, `mn1`, `tick`, etc.)
  - metadata now stores selected `price_type` in `source_meta.json`
- Verification:
  - Firefox UI check: timeframe chips + Bid/Ask + autocomplete + default-start hint present
  - UI submit path starts task and task banner updates
  - API task log confirms `price_type=ask` and selected timeframe.

### 3) Indicator-Erkennung aus Source-Code
- `viewer/app.py` indicator pipeline tightened:
  - improved regex parsing for vectorbt/ta patterns (SMA/EMA/RSI/ATR/BB/MACD/SuperTrend)
  - period extraction where available
  - normalized response fields: `type` + `style` (while keeping `pane` backward-compatible)
- `TradeChartPage.tsx` updated to read `type`/`style` with fallback to legacy `pane`/`color`.
- Verification:
  - `/api/backtest/{id}/trade/{nr}` returns indicators with `type` and `style`
  - Firefox trade chart renders successfully (7 canvas layers, no console errors).

### Build
- `npm run build` ✅ (Vite production build successful after all changes)

## Update — 2026-03-07 (Dukascopy Genesis Discovery + Trade Context)

### 1) Dukascopy Genesis Discovery
- Added `data_tools/dukascopy_discovery.py`:
  - probes **single days only** via `dukascopy-node`
  - coarse search from 2020 with `-5 / +1` year heuristic
  - binary search for start month
  - binary search + corrective back-scan for start day
  - writes results to `~/data/dukascopy_genesis.json`
- Added reference validation coverage in `tests/test_dukascopy_discovery.py`:
  - stubbed `dukascopy-node` confirms `EURUSD = 2003-05-04`
  - stubbed `dukascopy-node` confirms `XAUUSD = 2003-05-04`
- Backend:
  - new endpoint `GET /api/data/dukascopy/genesis`
  - `/api/data/download` now rejects Dukascopy start dates earlier than known genesis
- Frontend:
  - `DownloadModal.tsx` now fetches genesis data
  - Dukascopy start-date field gets `min=<genesis>`
  - modal shows earliest available start for selected instruments
  - default empty start still uses a safe recent date, but never below the known lower bound
- Local seed file written for immediate UX validation:
  - `~/data/dukascopy_genesis.json`
  - currently seeded with `EURUSD` + `XAUUSD` = `2003-05-04`

### 2) Trade Context / Strategy Meta in Viewer
- `runner/engines/vectorbt_adapter.py` now extracts strategy context columns into `Trade.meta`:
  - entry-side columns with prefix `meta_`
  - exit-side columns with prefix `exit_meta_`
  - `stop_loss` is exposed as `sl_price` when available
- `strategies/example_sma.py` now emits demo meta fields:
  - `SMA_Fast`
  - `Signal_Strength`
- `viewer/app.py` now exposes `trade.meta` in viewer responses.
- `frontend/src/pages/TradeChartPage.tsx` now renders a **Trade Context** panel when meta exists:
  - dark theme card styling
  - key/value list
  - floats formatted to max 4 decimals
- Also hardened chart locale handling so Playwright/Firefox no longer throws the previous
  `"invalid language tag: undefined"` error on this page.

### 3) Verification
- Python test suite: `.venv/bin/python -m pytest tests -q` ✅ (`60 passed`)
- Frontend build: `cd frontend && npm run build` ✅
- Demo backtest:
  - `example_sma_trade_meta_demo.trades.json` created
  - first trade contains `meta = {"SMA_Fast": 183.3333, "Signal_Strength": "High"}`
- Browser validation (Playwright / Firefox) ✅
  - Trade Chart page shows **Trade Context** box with `Signal_Strength` and `SMA_Fast`
  - Dukascopy Download modal shows earliest start hint and `min="2003-05-04"` for `EURUSD`

## Update — 2026-03-08 (Timeframe Genesis + Strategy Meta Retrofit)

### 1) Dukascopy Genesis Discovery per Timeframe
- `data_tools/dukascopy_discovery.py` refactored to probe all required Dukascopy granularities:
  - `d1`, `h1`, `m30`, `m15`, `m5`, `m1`, `s1`, `tick`
- Output format changed from flat symbol mapping to nested symbol/timeframe mapping:
  ```json
  {
    "EURUSD": {
      "d1": "1999-01-01",
      "s1": "2003-05-04",
      "tick": "2003-05-04"
    }
  }
  ```
- `viewer/app.py` now:
  - normalizes timeframe aliases (e.g. `sec -> s1`)
  - computes Dukascopy lower bounds per selected symbol/timeframe combination
  - rejects `/api/data/download` requests that start before any selected symbol/timeframe genesis
- `frontend/src/components/DownloadModal.tsx` now recomputes the start-date `min` dynamically from both selected symbols and selected timeframes.
- Validation:
  - `tests/test_dukascopy_discovery.py` ✅
  - `tests/test_viewer_features.py` genesis/download checks ✅
  - Playwright / Firefox on `/data` ✅
    - `EURUSD + d1` shows earliest start `1999-01-01`
    - toggling `s1` updates the hint and input `min` to `2003-05-04`

### 2) Strategy Conversion Agent Rule Hardened
- `.github/agents/strategy-conversion.md` updated:
  - coding step now explicitly requires **every logic-relevant indicator** to land in `trade.meta`
  - rule covers entry/exit filters, stop-loss inputs, ATR-based sizing, and trailing-stop levels
  - protocol now distinguishes vectorbt `indicators_dict` and pynecore `build_trade_context(...)` enrichment path

### 3) Vectorbt + PyneCore Trade Meta Retrofit
- `runner/engines/vectorbt_adapter.py` now supports:
  - legacy DataFrame returns
  - preferred tuple contract: `entries, exits, indicators_dict`
- Indicator values are mapped into `trade.meta` at entry or exit time depending on key semantics (e.g. `_at_exit` / `exit_...`).
- All existing vectorbt strategies in `strategies/` were migrated to emit relevant indicator series via `indicators_dict`.
- `runner/engines/pynecore_adapter.py` now:
  - loads companion `*_context.py` helpers
  - enriches parsed PyneCore trades with per-trade metadata after the run
  - copies the strategy to a temp path before invoking `pyne`, avoiding the stale local `.toml` issue that broke `input.source("close")`
- Added companion context helpers:
  - `strategies/pynecore_sma_crossover_context.py`
  - `strategies/pynecore_orb_context.py`

### 4) Validation
- Vectorbt validation:
  - `post_vix_meta_validation.trades.json` created via `post_vix_dip_buy`
  - all trades include `VIX` + `RSI` in `meta`
- PyneCore smoke validation:
  - `pynecore_sma_meta_validation.trades.json` created
  - trades include `SMA_Fast` + `SMA_Slow` in `meta`
- Browser validation (Playwright / Firefox, viewer restarted on **port 5556**) ✅
  - `/backtests/post_vix_meta_validation/trade/1` renders **Trade Context**
  - visible keys include `VIX`, `RSI`, `SMA`, `VIX_Data_Available`, `VIX_Fallback_Used`
- Automated checks:
  - `tests/test_strategy_meta_adapters.py` ✅
  - `.venv/bin/python -m pytest tests -q` ✅ (`63 passed`)
  - `cd frontend && npm run build` ✅

## Update — 2026-03-08 (Data Management Backend Hardening)

### 1) Dukascopy Discovery Smart Skipping
- `data_tools/dukascopy_discovery.py` now:
  - loads `~/data/dukascopy_genesis.json` before scanning
  - skips already known symbol/timeframe pairs individually
  - logs e.g. `Skipping EURUSD h4, already known: 2003-05-04`
  - supports `--force` to ignore cached values and overwrite them
  - persists `dukascopy_genesis.json` immediately after each newly discovered timeframe
- Validation:
  - `tests/test_dukascopy_discovery.py` extended with skip / force / interrupt-persistence coverage ✅

### 2) Parquet Date Ranges (`null -> null` fix)
- `data_tools/catalog_builder.py` now reads OHLCV metadata via `pyarrow.parquet` instead of loading full DataFrames:
  - rows / columns from parquet metadata
  - start / end from timestamp statistics or first+last row-group reads
  - API-facing dates normalized to `YYYY-MM-DD`
- catalog reuse added:
  - unchanged files reuse cached metadata from `~/data/catalog.json` using `size_bytes + mtime_ns`
  - avoids rescanning unchanged parquet files
- `viewer/app.py` now returns correct `start` / `end` fields in:
  - `GET /api/data/overview`
  - `GET /api/data/category/<name>`
- Real API check after rebuild/restart:
  - `GET http://localhost:5556/api/data/category/Equities`
  - result: `5396` symbols, `null_ranges=0` ✅

### 3) Dolthub Parquet Exposure
- legacy flat Dolthub exports under `~/data/ohlcv/dolthub/equity/US/<SYMBOL>.parquet` are now parsed as:
  - `symbol = <SYMBOL>`
  - `timeframe = 1d`
- this makes Dolthub equities appear in the normal **Equities** category/API flow instead of only as repo blocks
- `data_tools/dolthub_manager.py` now writes future exports to the canonical OHLCV layout:
  - `~/data/ohlcv/dolthub/equity/US/<SYMBOL>/1d.parquet`
  - existing legacy flat exports are migrated to the canonical path on export
- Verification:
  - Equities API now returns Dolthub rows with real symbol/timeframe/date-range fields ✅

### 4) Backend Task Log Streaming
- background download/resample tasks now write to:
  - `~/logs/tasks/task_<job_id>.log`
- subprocesses are launched unbuffered (`python -u` + `PYTHONUNBUFFERED=1`) so logs stream into the file while running
- new endpoint added:
  - `GET /api/data/tasks/<job_id>/logs`
  - returns full log text + task status
  - missing log file returns empty `logs` plus current status
- existing `GET /api/data/tasks/<job_id>` remains intact and still returns tail/progress data
- catalog refresh after task completion no longer points to the missing `data_tools.catalog_update`; it now rebuilds `catalog.json` directly

### 5) Validation / Runtime
- Rebuilt real catalog cache:
  - `.venv/bin/python -m data_tools.catalog_builder -o ~/data/catalog.json` ✅
  - output: `5470 datasets`
- Restarted viewer backend on port `5556` ✅
- Full backend test suite:
  - `.venv/bin/python -m pytest tests -q` ✅ (`76 passed`, `1 warning`)
