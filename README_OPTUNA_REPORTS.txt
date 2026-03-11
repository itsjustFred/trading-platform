═══════════════════════════════════════════════════════════════════════════════════
    OPTUNA HYPERPARAMETER OPTIMIZATION BACKEND - IMPLEMENTATION REPORTS
═══════════════════════════════════════════════════════════════════════════════════

Two comprehensive reports have been generated to help implement Optuna-based 
hyperparameter optimization for the trading-platform codebase.

───────────────────────────────────────────────────────────────────────────────────
📄 REPORT 1: OPTUNA_QUICK_SUMMARY.txt  (13 KB)
───────────────────────────────────────────────────────────────────────────────────

BEST FOR: Quick reference while coding, understanding architecture, copy-paste code

CONTENTS:
  1. Backtest Storage & ID Mapping
     └─ File paths, id format, BacktestResult structure, loading function
  
  2. Vectorbt Engine Adapter
     └─ Invocation pattern, BacktestConfig, parameter handling, multi-symbol processing
  
  3. Background Task Patterns
     └─ Task registry, progress tracking, threading model, API endpoints
  
  4. Path Constants & Directories
     └─ DATA_DIR, LOG_DIR, existing structure, new optimizations directory
  
  5. Most Relevant Tests
     └─ test_viewer_features.py (viewer_client fixture), patterns to follow
  
  6. Critical Gotchas
     └─ Date ranges, parameter serialization, result storage, multi-symbol handling
  
  7. Files to Create/Modify
     └─ Exact list of new files and which existing files to modify
  
  8. Key Code Locations
     └─ Copy-paste code snippets for common operations

USAGE: Open in text editor, search by section number or keyword


───────────────────────────────────────────────────────────────────────────────────
📘 REPORT 2: OPTUNA_IMPLEMENTATION_GUIDE.md  (21 KB, 584 lines)
───────────────────────────────────────────────────────────────────────────────────

BEST FOR: Comprehensive reference, understanding design decisions, detailed examples

CONTENTS (8 MAJOR SECTIONS):

1. BACKTEST STORAGE & LOADING (lines 1-100)
   ├─ ID Mapping and File Structure
   ├─ Key Models: BacktestMeta, BacktestResult
   └─ Loading Function: _load_result()

2. VECTORBT ENGINE ADAPTER DETAILS (lines 101-250)
   ├─ Class: VectorbtAdapter, Method: run()
   ├─ BacktestConfig with full field descriptions
   ├─ Parameter Resolution & Strategy Invocation
   ├─ Parallel Processing with ProcessPoolExecutor
   ├─ Data Loading & Result Construction
   └─ Stats Computation Integration

3. BACKGROUND TASK PATTERNS (lines 251-450)
   ├─ Global Task Registry: DATA_TASKS, DATA_TASKS_LOCK
   ├─ Task Lifecycle Model: status transitions
   ├─ Task Record Creation with full schema
   ├─ Task Progress Tracking: ✅/❌ prefixes, percent calculation
   ├─ Task State Management: thread-safe operations
   ├─ Logging to Task: _write_task_log_line(), _ingest_task_log_line()
   ├─ Threading Pattern: complete example from download_data endpoint
   ├─ Task Control Endpoints: pause, resume, cancel, logs
   └─ Task Serialization: _serialize_task() format

4. PATH CONSTANTS & DIRECTORY STRUCTURE (lines 451-520)
   ├─ Constants: DATA_DIR, LOG_DIR with env var overrides
   ├─ Viewer Paths: BACKTESTS_DIR, TASK_LOGS_DIR
   ├─ Expected Directory Tree
   ├─ Directory Creation code
   └─ Directory structure for new optimizations feature

5. EXISTING TESTS FOR RUNNER/VIEWER (lines 521-570)
   ├─ test_runner_cli.py: CLI patterns
   ├─ test_viewer_features.py: Task management patterns (MOST RELEVANT)
   ├─ test_models.py: Serialization tests
   ├─ test_stats.py: Stats computation validation
   └─ Test Pattern for Optimization: template

6. GOTCHAS AND EDGE CASES (lines 571-680)
   ├─ A. Date Range Splitting: intersection for multi-symbol
   ├─ B. Parameter Serialization: JSON safety, numpy types
   ├─ C. Result Serialization: storage performance
   ├─ D. Multi-Symbol Handling: ProcessPoolExecutor behavior
   ├─ E. Stop/Pause Signals: threading.Event usage
   ├─ F. API/Frontend Contract: required stats fields
   └─ G. Intrabar Modeling: performance considerations

7. SUMMARY OF KEY FUNCTIONS/CLASSES (lines 681-730)
   ├─ What to create (OptunaOptimizer, OptimizationConfig, OptimizationResult)
   ├─ What to mirror (VectorbtAdapter pattern)
   ├─ Files to Create/Modify with exact paths
   └─ Example: How to structure new optimization backend

8. DIRECTORY SUMMARY (lines 731-760)
   └─ Complete file tree with annotations on what to create/modify

USAGE: Read sequentially for understanding, or jump to sections by line numbers


───────────────────────────────────────────────────────────────────────────────────
🎯 QUICK START - WHAT TO DO NEXT
───────────────────────────────────────────────────────────────────────────────────

1. READ QUICK SUMMARY
   File: OPTUNA_QUICK_SUMMARY.txt
   Time: 10-15 minutes
   Goal: Understand architecture, identify key entry points

2. CREATE NEW FILES
   a) shared/optim_models.py
      └─ Define OptimizationConfig, OptimizationResult, TrialMetrics
   
   b) runner/optimizers/optuna_optimizer.py
      └─ Implement OptunaOptimizer(EngineAdapter) with run() method
      └─ Internally use VectorbtAdapter for each trial

3. MODIFY VIEWER/APP.PY
   a) Add new routes: POST /api/optimizations/start, GET /api/optimizations/{id}
   b) Reuse existing task patterns: _create_task_record(), DATA_TASKS
   c) Reuse logging: _write_task_log_line(), _ingest_task_log_line()

4. WRITE TESTS
   Add to tests/test_viewer_features.py:
   └─ test_optimization_task_creation()
   └─ test_optimization_progress_tracking()
   └─ Use viewer_client fixture pattern

5. REFERENCE THE GUIDE
   File: OPTUNA_IMPLEMENTATION_GUIDE.md
   Use: For detailed function signatures, line numbers, code examples


───────────────────────────────────────────────────────────────────────────────────
📍 KEY FILE LOCATIONS (for ctrl+click or direct navigation)
───────────────────────────────────────────────────────────────────────────────────

CORE BACKTEST HANDLING:
  /home/ubuntu/Desktop/trading-platform/shared/models.py              (line 215-510)
    └─ BacktestMeta, BacktestResult, Trade, EquityPoint models
  
  /home/ubuntu/Desktop/trading-platform/runner/engines/vectorbt_adapter.py
    └─ VectorbtAdapter class (line 260), run() method, _process_symbol()

TASK INFRASTRUCTURE (to reuse):
  /home/ubuntu/Desktop/trading-platform/viewer/app.py                (3560 lines)
    └─ _create_task_record()        (line 2281)
    └─ DATA_TASKS                   (line 2177)
    └─ DATA_TASKS_LOCK              (line 2178)
    └─ _write_task_log_line()       (line 2215)
    └─ _ingest_task_log_line()      (line 2371)
    └─ _serialize_task()            (line 2434)
    └─ download_data endpoint       (line 2641) [threading pattern example]

CONSTANTS:
  /home/ubuntu/Desktop/trading-platform/shared/constants.py          (line 1-47)
    └─ DATA_DIR, LOG_DIR definitions

EXISTING TESTS (to learn from):
  /home/ubuntu/Desktop/trading-platform/tests/test_viewer_features.py (★)
    └─ viewer_client fixture        (line 19-29)
  
  /home/ubuntu/Desktop/trading-platform/tests/test_runner_cli.py
    └─ test_cmd_run_persists_canonical_stats (line 39-67)


───────────────────────────────────────────────────────────────────────────────────
💡 DESIGN PATTERN TO FOLLOW
───────────────────────────────────────────────────────────────────────────────────

The Optuna backend should mirror the existing architecture:

  1. Model Layer (shared/optim_models.py)
     └─ OptimizationConfig(BacktestConfig) + OptimizationResult dataclass

  2. Engine Layer (runner/optimizers/optuna_optimizer.py)
     └─ OptunaOptimizer(EngineAdapter) with run(config, strategy_path) method
     └─ Internally: For each trial → call VectorbtAdapter.run()

  3. Task Management (viewer/app.py - add new routes)
     └─ POST /api/optimizations/start → create task, spawn thread
     └─ Reuse: _create_task_record(), DATA_TASKS, progress logging
     └─ GET /api/data/tasks/<task_id> → existing endpoint works for optimization tasks

  4. Result Persistence
     └─ Save to ~/data/optimizations/{strategy_name}_{timestamp}/
     └─ Store: db.sqlite (Optuna), config.json, results.json

  5. Testing (tests/test_viewer_features.py)
     └─ Use viewer_client fixture pattern
     └─ Test task creation, progress tracking, API responses


───────────────────────────────────────────────────────────────────────────────────
❗ CRITICAL DETAILS NOT TO MISS
───────────────────────────────────────────────────────────────────────────────────

1. Parameter Serialization
   • Must be JSON-safe: convert numpy types to Python built-ins
   • BacktestConfig.parameters: dict[str, Any] stored in BacktestMeta

2. Stats Computation
   • After each trial: call compute_stats_for_result(backtest_result)
   • Required fields for frontend: sharpe, cagr, max_drawdown_pct, win_rate, etc.

3. Task Progress Logging
   • Use ✅ prefix for successes, ❌ for failures
   • Auto-calculation: percent = completed / (completed + failed) * 100

4. Thread-Safe Task Updates
   • Always use DATA_TASKS_LOCK when reading/writing DATA_TASKS
   • Check stop_event and pause_event in Optuna callbacks

5. Date Range Handling
   • Multi-symbol: intersection (latest start, earliest end)
   • Validate before passing to engine.run()

6. Result Storage
   • Optuna study database: SQLite backend
   • Best trial results: full BacktestResult JSON
   • Trial summaries: lightweight metrics dict


───────────────────────────────────────────────────────────────────────────────────
📊 COMPARISON TABLE: EXISTING PATTERNS VS. WHAT YOU'LL BUILD
───────────────────────────────────────────────────────────────────────────────────

┌─────────────────────┬──────────────────────────┬──────────────────────────────┐
│ Aspect              │ Data Download Task       │ Optuna Optimization Task     │
├─────────────────────┼──────────────────────────┼──────────────────────────────┤
│ Task Kind           │ "download"               │ "optimization"               │
│ Entry Point         │ POST /api/data/download  │ POST /api/optimizations/start│
│ Result Location     │ ~/data/ohlcv/            │ ~/data/optimizations/        │
│ Progress Units      │ num_symbols              │ num_trials                   │
│ Sub-tasks           │ ✅ symbol downloaded     │ ✅ trial N: sharpe=1.5       │
│ Stats Stored        │ N/A                      │ All metrics (in results.json)│
│ Main Thread Job     │ HTTP downloader          │ Call engine.run() per trial  │
│ Control Actions     │ pause, resume, cancel    │ pause, resume, cancel (same) │
│ Database            │ File-based (parquet)     │ Optuna SQLite                │
└─────────────────────┴──────────────────────────┴──────────────────────────────┘


───────────────────────────────────────────────────────────────────────────────────
✅ VERIFICATION CHECKLIST
───────────────────────────────────────────────────────────────────────────────────

Before starting implementation, verify you have:

  □ Read OPTUNA_QUICK_SUMMARY.txt (all 8 sections)
  □ Identified all key file paths in the codebase
  □ Understand BacktestConfig and BacktestResult structures
  □ Familiar with DATA_TASKS task registry pattern
  □ Understand _create_task_record() and threading pattern
  □ Know how to serialize results to JSON
  □ Can read/write to TASK_LOGS_DIR
  □ Understand parameter passing through config.parameters
  □ Know how stats are computed via compute_stats_for_result()

When ready to code:

  □ Create shared/optim_models.py
  □ Create runner/optimizers/optuna_optimizer.py
  □ Add routes to viewer/app.py
  □ Add tests to tests/test_viewer_features.py
  □ Test parameter serialization
  □ Test task creation and progress tracking
  □ Verify results saved to ~/data/optimizations/


═══════════════════════════════════════════════════════════════════════════════════

Generated: 2026-03-11
Codebase: /home/ubuntu/Desktop/trading-platform

For questions, refer to the detailed guide (OPTUNA_IMPLEMENTATION_GUIDE.md)
or examine the code locations listed above.

═══════════════════════════════════════════════════════════════════════════════════
