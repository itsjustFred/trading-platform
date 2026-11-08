# Optuna-Based Hyperparameter Optimization Backend — Implementation Guide

## 1. BACKTEST STORAGE & LOADING

### ID Mapping and File Structure
- **ID Format**: `{strategy_name}_{YYYYMMDD_HHMMSS}.trades.json`
- **Backtest ID** (used by API): stem of filename without `.trades.json` extension
  - Example: `example_sma_20260309_143022` for file `example_sma_20260309_143022.trades.json`
- **Storage Directory**: `DATA_DIR / "backtests"` (default: `~/data/backtests`)
- **Listing Pattern**: Code iterates via `BACKTESTS_DIR.glob("*.trades.json")`

### Key Models (from `/home/ubuntu/Desktop/trading-platform/shared/models.py`)
- **BacktestMeta** (line 215-299): Metadata dataclass with:
  - `id`: unique identifier
  - `created_at`: ISO 8601 UTC timestamp
  - `engine`: "vectorbt", "lean", or "pynecore"
  - `strategies`: list[str] (strategy names)
  - `backtest_start`, `backtest_end`: date strings
  - `parameters`: dict[str, Any] (strategy parameters used)
  - `tags`: list[str] (e.g., "optimized", "walk-forward")
  - `notes`: optional display text
  - `data`: dict with "symbols", "timeframes", "source", "universe"
  - `initial_capital`: float
  
- **BacktestResult** (line 442-510): Complete result object with:
  - `meta`: BacktestMeta
  - `trades`: list[Trade]
  - `equity_curve`: list[EquityPoint]
  - `stats`: dict[str, Any] (pre-computed performance metrics)
  - `indicators`, `drawings`: optional visualization data
  - Methods: `.save(path)`, `.load(path)`, `.to_json()`, `.from_json()`

### Loading Function
```python
# From viewer/app.py line 85-89
def _load_result(backtest_id: str) -> BacktestResult | None:
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return None
    return BacktestResult.load(str(path))
```

---

## 2. VECTORBT ENGINE ADAPTER DETAILS

### Invocation Pattern
**File**: `/home/ubuntu/Desktop/trading-platform/runner/engines/vectorbt_adapter.py`

#### Class: `VectorbtAdapter(EngineAdapter)` (line 260+)
**Key Method**: `run(config: BacktestConfig, strategy_path: str) -> BacktestResult`

#### BacktestConfig (base.py, line 22-54)
```python
class BacktestConfig(BaseModel):
    strategy_name: str
    symbols: list[str]           # min_length=1
    timeframe: str               # e.g., "1h", "5m", "1d"
    start_date: str              # "2021-01-01"
    end_date: str                # "2025-06-30"
    initial_capital: float       # default: 100_000.0
    risk_per_trade_pct: float | None    # Optional, 0.0–1.0 — max loss per trade
    max_position_exposure_pct: float | None  # Optional — max notional of ONE position / equity
    max_leverage: float | None          # Optional
    modeling_resolution: str     # "open_close", "1-lower", "1m", "s1", "tick"
    data_dir: str                # Override default DATA_DIR
    parameters: dict[str, Any]   # Strategy-specific parameters
```

#### Parameter Resolution
```python
# base.py, line 57-73
resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)
# Merges strategy STRATEGY_META.parameters defaults with explicit overrides
```

#### Strategy Invocation
```python
# vectorbt_adapter.py, line 111
signal_output = _call_generate_signals(strategy_mod, df, resolved_parameters)
```

**Supported signatures**:
1. **Dict-style** (preferred): `generate_signals(df: pd.DataFrame, params: dict)`
2. **Kwargs-style**: `generate_signals(df: pd.DataFrame, fast: int, slow: int, ...)`
   - Auto-detects based on function signature

#### Return Format
**Tuple/List Output** (line 745-760):
- `(entries, exits)` — minimal
- `(entries, exits, indicators_dict)` — with indicators
- `(entries, exits, indicators_dict, drawings)` — with visualization
- Or plain `pd.DataFrame` with `entries` and `exits` columns

#### Parallel Processing
- Uses `ProcessPoolExecutor` for multi-symbol backtests (line 275-302)
- **Worker function**: `_process_symbol(symbol, config, strategy_path, resolved_parameters, data_root)`
  - Returns: `(trades_list, extra_timeframes, drawings)`
- **Max workers**: `min(num_symbols, 4)` (MAX_PARALLEL_WORKERS = 4)

#### Data Loading
```python
# vectorbt_adapter.py, line 722+
def _load_ohlcv(data_root, symbol, timeframe, start_date, end_date):
    # Scans data_root / "ohlcv" / symbol / "{timeframe}.parquet"
    # Returns: (df, data_path)
```

#### Result Construction (line 330-356)
```python
meta = BacktestMeta(
    id=f"vbt-{config.strategy_name}-{now.strftime('%Y%m%d-%H%M%S')}",
    created_at=now.isoformat(),
    engine="vectorbt",
    strategies=[strategy_name],
    backtest_start=config.start_date,
    backtest_end=config.end_date,
    parameters=resolved_parameters,  # <- Important: params saved here
    data={"timeframes": used_timeframes, "symbols": config.symbols, ...},
    initial_capital=config.initial_capital,
)

return BacktestResult(
    meta=meta,
    trades=all_trades,
    equity_curve=all_equity,
    drawings=all_drawings,
)
```

**Stats Computation**: Occurs in CLI (line 143) via `compute_stats_for_result(result)`
- Populates `result.stats` with Sharpe, Sortino, CAGR, max drawdown, etc.

---

## 3. BACKGROUND TASK PATTERNS IN VIEWER/APP.PY

### Global Task Registry
```python
# app.py, line 2177-2178
DATA_TASKS: dict[str, dict[str, Any]] = {}
DATA_TASKS_LOCK = threading.Lock()
```

### Task Lifecycle Model
```python
# app.py, line 2179-2180
ACTIVE_TASK_STATUSES = {"starting", "running", "paused"}
FINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
```

### Task Record Creation (line 2281-2311)
```python
def _create_task_record(
    task_id: str,
    *,
    name: str,                      # e.g., "Optimize mean_reversion_rsi_bb"
    description: str,               # e.g., "Hyperparameter optimization..."
    kind: str,                       # "data" | "download" | "agent" | "optimization"
    source: str,                     # e.g., "viewer" | "api"
    total_items: int = 0,            # for progress tracking
    log_file: Path,                  # Path to task log file
    control_mode: str,               # "thread" | "process"
    supports_control: bool = True,   # can pause/resume/cancel
) -> dict[str, Any]:
    return {
        "id": task_id,
        "name": name,
        "description": description,
        "kind": kind,
        "source": source,
        "status": "starting",        # Initial status
        "log_file": str(log_file),
        "total_items": total_items,
        "progress": 0.0,             # 0–100%
        "progress_detail": _new_task_progress(total_items),
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "supports_control": supports_control,
        "control_mode": control_mode,
        "created_at": _task_now_iso(),
        "updated_at": _task_now_iso(),
    }
```

### Task Progress Tracking
```python
# app.py, line 2271-2278
def _new_task_progress(total_items: int = 0) -> dict[str, Any]:
    return {
        "completed": 0,
        "failed": 0,
        "total": max(total_items, 0),
        "percent": 0.0,
        "current": "",              # Last message prefixed with ✅ or ❌
    }
```

### Task State Management (thread-safe)
```python
# app.py, line 2314-2336
def _get_task(task_id: str) -> dict | None              # Get live task
def _snapshot_task(task_id: str) -> dict | None         # Get copy of task
def _update_task_fields(task_id: str, **updates) -> dict | None
def _set_task_status(task_id: str, status: str) -> dict | None
def _update_task_progress(task_id: str, *, percent, current, total) -> dict | None
```

### Logging to Task
```python
# app.py, line 2215-2239
def _write_task_log_line(
    handle,                    # File handle
    task_id: str,
    message: str,              # Text to log
    *,
    stream: str,               # "stdout" | "stderr"
    log_level: str = "info",   # "info" | "error" | "warning"
    source: str = "data-task",
) -> None:
    # Writes to file AND ingests to progress tracker
```

**Progress Ingestion** (line 2371-2395):
```python
def _ingest_task_log_line(task_id: str, text: str) -> None:
    # Logs starting with ✅ increment detail["completed"]
    # Logs starting with ❌ increment detail["failed"]
    # Auto-calculates percent = completed/(completed+failed) * 100
    # Stores last message as detail["current"]
```

### Threading Pattern for Long-Running Tasks

**Example: Data Download Task** (line 2641)
```python
@app.route("/api/data/download", methods=["POST"])
def download_data():
    task_id = str(uuid.uuid4())
    task_log = _task_log_path(task_id)
    
    # 1. Create task record
    record = _create_task_record(
        task_id,
        name="Download OHLCV",
        description=f"Download {len(symbols)} symbols...",
        kind="download",
        source="viewer",
        total_items=len(symbols),
        log_file=task_log,
        control_mode="thread",
        supports_control=True,
    )
    
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record
    
    # 2. Define task function
    def run_download():
        with task_log.open("a", encoding="utf-8") as handle:
            try:
                _set_task_status(task_id, "running")
                # ... do work ...
                # Log progress: _write_task_log_line(handle, task_id, "✅ Symbol AAPL", stream="stdout")
                _set_task_status(task_id, "completed")
            except Exception as e:
                _write_task_traceback(handle, task_id, traceback.format_exc())
                _set_task_status(task_id, "failed")
    
    # 3. Start thread (daemon = no join required)
    thread = threading.Thread(target=run_download, daemon=True)
    thread.start()
    
    return jsonify({"task_id": task_id}), 202
```

### Task Control Endpoints
```python
@app.route("/api/data/tasks/<task_id>/pause", methods=["POST"])      # line 3403
@app.route("/api/data/tasks/<task_id>/resume", methods=["POST"])     # line 3422
@app.route("/api/data/tasks/<task_id>/cancel", methods=["POST"])     # line 3441
@app.route("/api/data/tasks/<task_id>/logs")                         # line 3463
@app.route("/api/data/tasks/<task_id>", methods=["DELETE"])          # line 3479
```

### Task Serialization for API
```python
# app.py, line 2434-2470
def _serialize_task(
    task_id: str,
    task: dict,
    *,
    include_log: bool = False,
    max_log_bytes: int = 2048,
) -> dict[str, Any]:
    # Returns JSON-friendly dict with:
    # - id, name, description, kind, source
    # - status, progress, progress_detail
    # - supports_control, can_pause, can_resume, can_cancel
    # - created_at, updated_at
    # - logs (if include_log=True)
```

---

## 4. PATH CONSTANTS & DIRECTORY STRUCTURE

### Constants (shared/constants.py, lines 1-47)
```python
DATA_DIR = Path(os.environ.get("TRADING_DATA_DIR", "~/data")).expanduser().resolve()
LOG_DIR = Path(os.environ.get("LOG_DIR", "~/logs")).expanduser().resolve()

# Used in viewer/app.py:
BACKTESTS_DIR = DATA_DIR / "backtests"
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
LOGS_DIR = LOG_DIR
TASK_LOGS_DIR = LOGS_DIR / "tasks"
REACT_DIST = Path(__file__).parent.parent / "frontend" / "dist"
STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
```

### Expected Directory Tree
```
~/data/
  backtests/                    # Existing: backtest .trades.json files
    example_sma_20260309_143022.trades.json
    mean_reversion_rsi_bb_20260310_120000.trades.json
  ohlcv/                        # OHLCV data by symbol/timeframe
    AAPL/1h.parquet
    MSFT/1h.parquet
  portfolios/                   # Combined portfolio runs
    portfolio_combined_20260310.json
  optimizations/                # ** NEW: For Optuna optimization runs **
    {strategy_name}_{timestamp}/
      db.sqlite                 # Optuna study database
      config.json               # Optimization parameters (config, ranges, objectives)
      results.json              # Final results with best params & metrics
      trial_logs/
        trial_1.log
        trial_2.log
        ...

~/logs/
  tasks/                        # Task-specific logs
    task_{task_id}.log
  *.log                         # General application logs
```

### Directory Creation
```python
# Ensure paths exist before use:
BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)
TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)

# For optimizations:
optim_dir = DATA_DIR / "optimizations" / f"{strategy_name}_{timestamp}"
optim_dir.mkdir(parents=True, exist_ok=True)
```

---

## 5. EXISTING TESTS FOR RUNNER/VIEWER

### Most Relevant Tests

#### 1. `/home/ubuntu/Desktop/trading-platform/tests/test_runner_cli.py` (68 lines)
**Focus**: CLI invocation and parameter passing
```python
# Test: test_cmd_run_persists_canonical_stats
# Validates: BacktestConfig creation, engine.run() call, stats computation
# Key: Shows how engine.run() output is saved with computed stats
```

#### 2. `/home/ubuntu/Desktop/trading-platform/tests/test_viewer_features.py` (500+ lines)
**Focus**: API endpoints, task management, data downloads
```python
# test_data_download_rejects_dates_before_genesis
# test_trade_detail_includes_meta
# Tests task creation, progress tracking, log ingestion
# Uses: viewer_client fixture, tmp_path, monkeypatching
```

**Key fixture** (line 19-29):
```python
@pytest.fixture
def viewer_client(monkeypatch, tmp_path):
    import viewer.main as viewer_app_module
    
    monkeypatch.setattr(viewer_app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(viewer_app_module, "BACKTESTS_DIR", tmp_path / "backtests")
    monkeypatch.setattr(viewer_app_module, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(viewer_app_module, "TASK_LOGS_DIR", tmp_path / "logs" / "tasks")
    viewer_app_module.DATA_TASKS.clear()
    return TestClient(viewer_app_module.app)
```

#### 3. `/home/ubuntu/Desktop/trading-platform/tests/test_models.py` (200+ lines)
**Focus**: BacktestMeta, BacktestResult serialization
```python
# Validates to_dict(), from_dict(), JSON round-trips
# Critical for optimization result persistence
```

#### 4. `/home/ubuntu/Desktop/trading-platform/tests/test_stats.py` (200+ lines)
**Focus**: Stats computation (Sharpe, Sortino, CAGR, drawdown)
```python
# Validates compute_stats() and compute_stats_for_result()
# Used after each trial completes
```

### Test Pattern for Optimization
Would follow the same structure:
```python
def test_optimization_task_creation(viewer_client, tmp_path):
    # 1. POST /api/optimizations/start
    # 2. Check task created in DATA_TASKS
    # 3. Poll /api/data/tasks/<task_id>
    # 4. Verify progress logs and final status
```

---

## 6. GOTCHAS AND EDGE CASES

### A. Date Range Splitting
**Current Implementation** (cli.py, line 54-84):
```python
def _detect_date_range(symbols: list[str], timeframe: str) -> tuple[str, str]:
    # For multiple symbols: returns INTERSECTION (latest start, earliest end)
    # This is crucial for multi-symbol backtests
```

**For Optimization**:
- Optuna trials should respect these bounds
- If splitting train/test or walk-forward, ensure dates don't overlap and cover available data
- Parse dates as `datetime.strptime(date_str, "%Y-%m-%d")` before arithmetic

### B. Parameter Serialization
**Gotcha**: Non-JSON-serializable parameters
- `BacktestConfig.parameters` must be JSON-safe (dict[str, Any])
- If optimization trial includes numpy types, convert: `int(np_int64)`, `float(np_float)`
- Optuna trials save parameter values to JSON; ensure types are built-in Python

**Example**:
```python
# Bad:
params = {"threshold": np.float64(0.5)}  # Not JSON-serializable

# Good:
params = {"threshold": float(0.5)}
```

### C. Result Serialization
**BacktestResult.save()** persists full JSON. Large backtests (many trades) can be slow:
- Equity curve: one point per trade (manageable)
- Trades: full Trade objects with all fields (can be large)
- **For optimization**: Consider storing only best-trial results fully; save trial stats summary

### D. Multi-Symbol Handling
**ProcessPoolExecutor** limitation: trades from different symbols mixed into single list
- Trade IDs renumbered sequentially after merge (vectorbt_adapter.py, line 317-319)
- Each symbol's trades processed independently, then merged
- **For optimization**: Important if tuning multi-symbol strategy; parallel workers reused per trial

### E. Stop/Pause Signals
**Task control** uses `threading.Event()`:
```python
# To check if task should pause/stop:
if task["pause_event"].is_set():
    task["pause_event"].wait()  # Block until resumed
if task["stop_event"].is_set():
    raise CancelledError()
```

**For optimization**: Optuna callbacks can check these events to gracefully stop studies.

### F. API/Frontend Contract
**Backtest listing** returns quick stats from file:
```python
# viewer/app.py, line 595-602
quick = _quick_stats_and_equity(
    trades,
    initial_capital,
    start,
    end,
    equity_curve=data.get("equity_curve", []),
    cached_stats=data.get("stats", {}),
)
```

**Key fields required in BacktestResult.stats dict**:
- `sharpe`, `sortino`, `cagr`, `max_drawdown_pct`, `win_rate`, `profit_factor`, `psr`, `p_value`

**For optimization**: Ensure trial results include these metrics for frontend display.

### G. Intrabar Modeling
**VectorbtAdapter supports intrabar exits** (line 157-169):
```python
intrabar_exit = _resolve_intrabar_exit(
    intrabar_df=intrabar_df,
    intrabar_timeframe=intrabar_timeframe,
    base_timeframe=config.timeframe,
    ...
)
```

**For optimization**: If using intrabar resolution, load extra data on each trial (performance cost).

---

## 7. SUMMARY OF KEY FUNCTIONS/CLASSES TO MIRROR

### For Optimization Backend:

**Create these analogous to existing patterns**:

1. **Optuna Study Manager** (like VectorbtAdapter)
   - Input: `OptimizationConfig` (extends BacktestConfig with study_direction, objectives, parameter_ranges)
   - Output: `OptimizationResult` (study.best_trial, best_params, trial_history)

2. **Trial Executor** (like _process_symbol)
   - Input: trial params from Optuna
   - Output: BacktestResult + metrics for Optuna objective function
   - Use existing `engine.run(config, strategy_path)` internally

3. **Optimization Task** (like data download task)
   - Registers task in `DATA_TASKS`
   - Uses `_write_task_log_line()` for progress
   - Honors `stop_event` and `pause_event` for cancellation
   - Saves results to `DATA_DIR / "optimizations"`

4. **API Endpoints**
   - `POST /api/optimizations/start` — create task, spawn thread
   - `GET /api/data/tasks/<task_id>` — reuse existing endpoint
   - `POST /api/optimizations/<optim_id>/results` — fetch final results

5. **Result Persistence**
   - Follow BacktestResult pattern
   - Store in JSON under `DATA_DIR/optimizations/{strategy_name}_{timestamp}/`
   - Save study database to SQLite (`db.sqlite`)
   - Create `OptimizationResult` dataclass analogous to `BacktestResult`

### Files to Create/Modify:
- **New**: `/home/ubuntu/Desktop/trading-platform/runner/optimizers/optuna_optimizer.py`
  - Class: `OptunaOptimizer(EngineAdapter)`
  - Reuses `VectorbtAdapter.run()` internally
  
- **Modify**: `/home/ubuntu/Desktop/trading-platform/viewer/app.py`
  - New routes: `/api/optimizations/*`
  - Reuse: `_create_task_record()`, `_write_task_log_line()`, DATA_TASKS machinery
  
- **New**: `/home/ubuntu/Desktop/trading-platform/shared/optim_models.py`
  - `OptimizationConfig` (extends BacktestConfig)
  - `OptimizationResult` (analogous to BacktestResult)
  - `TrialMetrics` (individual trial metadata)

- **Modify/Test**: `/home/ubuntu/Desktop/trading-platform/tests/test_viewer_features.py`
  - Add tests for optimization task creation, progress tracking, cancellation

---

## 8. DIRECTORY SUMMARY

```
trading-platform/
├── shared/
│   ├── models.py                           # BacktestMeta, BacktestResult
│   ├── constants.py                        # DATA_DIR, LOG_DIR
│   └── optim_models.py                    # [NEW] OptimizationConfig, etc.
├── runner/
│   ├── engines/
│   │   ├── base.py                         # EngineAdapter ABC, BacktestConfig
│   │   └── vectorbt_adapter.py             # VectorbtAdapter.run()
│   ├── cli.py                              # Entry: cmd_run()
│   └── optimizers/                         # [NEW]
│       └── optuna_optimizer.py             # [NEW] OptunaOptimizer class
├── viewer/
│   ├── main.py                             # FastAPI app, task management
│   └── core.py                             # Shared helper utilities
└── tests/
    ├── test_runner_cli.py                  # Engine invocation tests
    └── test_viewer_features.py             # Task/API tests
```


## Result diagnostics (added 2026-09-25)

Every parameter-mode optimization now emits a `diagnostics` block. It does not
change which parameter is selected; it reports how much the selection should be
trusted.

```json
"diagnostics": {
  "split":      { "train": {}, "embargo": {}, "test": {}, "embargo_bars_equivalent": 12.0 },
  "walk_forward_folds": [ { "fold": 0, "train": {}, "test": {} } ],
  "walk_forward_note": "Fold boundaries only: no per-fold performance was computed, ...",
  "plateau":    { "parameter": "atr_stop_mult", "direction": "maximize", "is_plateau": false, "sensitivity": 0.011 },
  "multiple_testing": { "n_trials": 8, "bonferroni_threshold": 0.00625,
                        "expected_max_under_null": 1.397, "survives_selection_bias": false }
}
```

- `embargo_pct` (default `0.0`, max `0.4`) drops a gap between the train and test
  windows **and is applied to the windows that actually run**: `run_optimization`
  forwards it into `_split_backtest_period`, so no trial ever sees data across the
  gap. A trade opened just before the split can close inside the test window, so
  its label is partly test information; without the gap that leaks backwards into
  training. The gap is additionally reported in bars via the context timeframe.
- `walk_forward_splits` (default `3`) is **report-only**. The repeated sequential
  folds are computed and published as a robustness diagnostic, but the selected
  parameters still come from the single train/test split above; the backtest is
  not re-run per fold. Folds never overlap and never train on an earlier fold's
  test window. The block is published as `walk_forward_folds` (fold boundaries
  only, with a `walk_forward_note` saying so); it was called `walk_forward`,
  which read like a performance result.
- `plateau` is centred on the trial the study selects: the highest metric for a
  maximize target, the lowest for a minimize target (`p_value`); the direction is
  published in `plateau.direction`.
- `plateau.is_plateau` is false when only the winning parameter scores within
  `tolerance` of the best. A lone spike is the classic overfitting signature.
- `multiple_testing.survives_selection_bias` compares the observed metric with
  the expected maximum of N trials under the null. A best-of-N result below that
  expectation is consistent with pure selection noise, and says so.

Reading a result: `is_plateau: false` **and** `survives_selection_bias: false`
together mean the selected parameter should not be promoted to live trading.
