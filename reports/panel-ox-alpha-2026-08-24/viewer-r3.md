# Panel Review — viewer (reviewer r3, blind)

- **Area:** `viewer` (`viewer/__init__.py`, `viewer/comparison.py`, `viewer/core.py`, `viewer/main.py`)
- **Reviewer:** r3
- **Date:** 2026-08-24
- **Method:** every file read in full (core.py in 3 chunks, main.py in 6 chunks); targeted greps for the fixed Python checklist; repo docs (`docs/ARCHITECTURE.md`, `pyproject.toml`, `docs/codex-rewrite-notes.md`) checked for standards that could override baseline smells. Read-only; nothing edited.
- **Severity scale:** critical / major / minor. Baseline smells are judgement calls per the brief.

## Repo-standard context (binds the smell baseline)

- `docs/ARCHITECTURE.md`: "Backend API: FastAPI (`viewer/main.py`)". The Flask app in `viewer/core.py` is therefore not the served backend — its route functions are dead code kept only as an import surface for `main.py`. This does *not* suppress the smells below (nothing documents keeping a dead route layer), but it changes their severity: dead-route bugs are latent, live-route bugs are not.
- `pyproject.toml [tool.ruff.lint] select = ["E","F","I","W"]` with `target-version = "py312"`. Ruff's E/F/W rules catch unused imports and syntax-level issues but none of the Fowler smells or the checklist items below; nothing here suppresses any finding.
- No doc found endorsing mutable defaults, broad exception catching, wildcard CORS, or the legacy shim layer → no override applies to those.

---

## File: `viewer/__init__.py`

**Scanned — no findings.** Empty file (0 lines) serving as package marker. Correct as-is.

---

## File: `viewer/comparison.py`

### (a) Bugs / correctness risks

1. **[major] comparison.py:162–174 — combined view silently ignores strategies beyond the first two.** `compare_backtests` is documented (line 145: "2+ backtests") and the endpoint feeds it N ids (`/api/compare`, main.py:7638–7678), but the combined equity/stats use only `strategies[0]` and `strategies[1]`. A 3-way compare shows a "50/50 combined portfolio" of two of three strategies with no error or marker. Either compute a true N-way blend or restrict the API contract to exactly 2.
2. **[minor] comparison.py:72–73 — normalization divides by the first aligned value only when positive; zero/negative starts silently become flat 1.0.** A strategy whose first overlapping equity value is ≤ 0 contributes a constant curve instead of failing loudly, skewing combined stats.
3. **[minor] comparison.py:20 — returns guard only exact-zero denominators.** A near-zero prior value produces astronomically large returns that dominate Sharpe/Sortino/correlation downstream (`compute_combined_stats`). No winsorization or floor.
4. **[minor] comparison.py:96–97 — Sharpe uses population std over *daily* returns scaled by √252 unconditionally.** For intraday equity series (the platform's common case, e.g. 1h bars) this mislabels period returns as daily and inflates annualization; there is no period detection.
5. **[minor] comparison.py:116 — net-return guard checks `combined_vals` after line 87 already returned early on emptiness**, so the trailing `if combined_vals else 0.0` is dead — harmless but confusing; suggests copy-paste lineage.
6. **[minor] comparison.py:133–135 — `overlapping_days` reports `len(dates)` even when the combined view errored out**, and `start_date`/`end_date` come from the raw date strings rather than parsed dates, so mixed formats sort/compare incorrectly.

### (b) Security

No direct security exposure (pure computation module). Note only that its output flows into `/api/compare` unauthenticated — see main.py findings for the transport-level issue.

### (c) Baseline smells

- **Data Clumps [minor→major]** — comparison.py:63–67, 80–85: `(dates, vals_a, vals_b)` travel together through three functions plus the derived `combined_equity`; one `AlignedSeries` type would remove the 4-parameter clump and the risk of transposing arguments.
- **Duplicated Code [minor]** — comparison.py:93–97 vs 118–122: mean/std/annualize logic repeated for Sharpe and Sortino; also drawdown math re-implemented here duplicates `_quick_stats_and_equity`'s inline peak/dd loop in core.py:930–938 and `_portfolio_stats_from_trades` in main.py:1745–1753 (three hand-rolled max-drawdown implementations across the area).
- **Primitive Obsession [minor]** — equity curves as bare `list[[time_str, float]]` (lines 53–60, 148): dict/tuple pairs with implicit positional meaning; a small record type would make `_align_series` self-documenting.
- **Mysterious Name [minor]** — `vals_a` / `vals_b` / `_align_series`'s `common` (line 59) are acceptable but `dd` (line 30) and `cov` (line 45) read terse against the domain's precision elsewhere.

### (d) Maintainability hot spots

The whole module is small and testable — good. The hot spot is the contract drift between docstring ("2+"), function shape (pairwise), and caller (N ids). Fix #1 there and most of this file's risk disappears.

---

## File: `viewer/core.py`

### (a) Bugs / correctness risks

1. **[critical] core.py:1505–1506 — orphaned code fragment / broken module structure.**
   ```python
       comparison = compare_backtests(strategies)
       return jsonify(comparison)
   ```
   Two indented lines sit at top level between `get_backtest_ohlcv` (ends 1502) and the next section comment. At import time this raises `NameError: name 'strategies' is not defined` — importing `viewer.core` fails outright unless something above defines `strategies`. Since `main.py` imports `viewer.core` at startup, this is a load-bearing landmine: if these lines were recently added/moved, the entire backend fails to boot. Even if currently shadowed by some global, they can never execute meaningfully — they are remnants of a deleted `compare_backtests` route and must be removed.
2. **[critical] core.py:4853–4865, 4884 — dead Flask runtime; `main()` cannot work.** Every endpoint is registered on `_NoopRouteApp` (core.py:97–113), whose `route()` is a no-op decorator and whose `run()` raises `RuntimeError("Flask runtime is removed...")`. `main()` at 4871–4884 calls `app.run(...)`, i.e. running `python -m viewer.core` always crashes. All ~40 `@app.route` functions (list_backtests 1003, rename_backtest 1074, get_trade_with_context 1369, run_backtest 1801, create_portfolio 2108, get_log 2558, data_download 4457, delete_symbol_data 4820, index/static_files 4853–4865, …) are unreachable code duplicating live FastAPI routes in main.py.
3. **[major] core.py:2108–2109 vs 2295–2296, 2304–2307 — path traversal via user-controlled id/name (dead but duplicated in live code).** `create_portfolio` writes `PORTFOLIOS_DIR / f"{portfolio_id}.json"` where `portfolio_id = f"{name.lower().replace(' ', '-')}-{timestamp}"` and `name` comes straight from the request body — `"../../x"` yields `..-…json` → escape attempt (dot preserved), and any `/` creates subdirectories. `get_portfolio`/`delete_portfolio`/`compare_portfolios` (2304–2340) read/delete `f"{portfolio_id}.json"` with **no validation whatsoever** (`../../backtests/foo.trades` deletes arbitrary files if extension-matching is bypassed by OS semantics). Contrast `_resolve_grid_result_path` (2365–2381), which does this correctly with a whitelist regex + resolve/parent check — the correct pattern exists in-file and was simply not applied. The live FastAPI versions have partial fixes (see main.py findings) but several remain unfixed.
4. **[major] core.py:1835–1892 — race conditions on `_running_jobs`.** `_running_jobs[job_id] = {...}` (1835) and nested mutations inside the worker thread (1880–1887) happen without `DATA_TASKS_LOCK` or any lock. Concurrent status reads (`get_backtest_run_status`) can observe torn transitions; more importantly entries are never evicted, so the dict grows unboundedly (memory leak) and job ids from `uuid4()[:8]` can collide across enough runs, silently hijacking another job's status slot. Same pattern in the live main.py runner (see below).
5. **[major] core.py:1128–1141 — wrong stat mapping: `best_trade ← avg_winner`, `worst_trade ← avg_loser`.** Best/worst single trade are not averages; a backtest with one huge winner shows a tame average instead. Duplicated verbatim into the live route (main.py:6982–6983), so the bug ships to users today.
6. **[major] core.py:4083–4096 — process-global stdout/stderr redirect inside a thread.** `redirect_stdout(handle), redirect_stderr(handle)` in `_launch_agent_task` swaps `sys.stdout/sys.stderr` **for the whole process** while the LLM agent runs. Any concurrent request logging to stdout during that window writes into this task's log file (or races the swap). This is a correctness and log-integrity hazard; the thread-bound handler class right above (2935–2959) exists precisely to avoid it.
7. **[major] core.py:3305–3418 / 3421–3649 / 3652–4019 — task-runner threads have no bottom-level `finally` for unexpected exceptions.** `_start_data_task.run_proc` catches only `(subprocess.SubprocessError, OSError)` (3401); `_start_dukascopy_task`/`_start_jforex_task` outer handlers are `except Exception` (good) but the inner download loops catch narrow tuples (3568, 3793–3802) — an unexpected exception type (e.g. `AttributeError` from a downloader refactor) escapes the inner loop, aborts remaining combinations, and is only saved by the outermost handler; in `_start_data_task` it would leave the task stuck at "running" forever because the handler list excludes it. Daemon threads also mean in-flight subprocesses are orphaned on shutdown.
8. **[minor] core.py:274–279 — duplicate-timestamp collapse keeps the later point via `collapsed[-1] = point` but relies on pre-sorting; fine, yet `_normalize_equity_points` silently drops points with `unix_time <= 0` (265), discarding legitimate pre-1970 or malformed-but-recoverable data with no warning.
9. **[minor] core.py:296 — `ordered_trades[0].entry_time or ordered_trades[0].last_exit_time` falls back to exit time for the initial-capital anchor while ordering uses `last_exit_time or entry_time` (290) — inconsistent key order can put the capital point after the first trade's point for same-timestamp trades.
10. **[minor] core.py:1161–1236 — regex-based indicator detection will double-count**: presence fallbacks (1225–1234) add SMA(20)/EMA(12)/RSI(14) etc. even when explicit matches already extracted real periods, because dedup keys include the period. Cosmetic duplication in charts, wasted pandas_ta computation.
11. **[minor] core.py:1441–1450 — indicator warm-up slice recomputed per request with `df.iloc[warmup_start:ohlcv_end_idx].reset_index(drop=True)`; `_indicator_to_points` then does a Python-level row scan per series (1360–1366) — O(bars × indicators) per trade-context request.
12. **[minor] core.py:2264 — portfolio id timestamp uses local-naive `datetime.now(timezone.utc).strftime(...)` — fine (tz-aware), but `now.strftime('%Y%m%d%H%M%S')` collides if two portfolios with the same name are created within the same second; the second silently overwrites the first's file (write_text truncates).

### (b) Security

1. **[critical] core.py:2560–2565 — arbitrary file deletion outside DATA_DIR (prefix-match bypass).** `delete_symbol_data` validates with `if not str(p).startswith(str(dd))`. `Path("/home/ubuntu/data_backup/secret").resolve()` startswith `/home/ubuntu/data` → passes; the check also accepts `/home/ubuntu/data-evil/...`. Correct form is `p.is_relative_to(dd)` (or `os.path.commonpath`). This exact flaw is copied into the live FastAPI route (main.py:10670) — see there for impact.
2. **[major] core.py:4820–4847 — DELETE endpoint deletes *any* file under DATA_DIR**, including `backtests/*.trades.json`, `portfolios/*.json`, the SQLite DB directory contents, and ohlcv parquets, with no auth and no path scoping to "data files"; combined with #1 this is remote unauthenticated arbitrary-ish file deletion.
3. **[major] core.py:2558–2593 — unguarded log path traversal (dead code, fixed in live twin).** `get_log` builds `LOGS_DIR / log_name` with no containment check; `GET /api/logs/..%2f..%2f.env`-style requests read arbitrary files. The live version (main.py:10721–10730) added `is_relative_to` — proof this was a real, patched vulnerability whose fix never propagated back.
4. **[major] core.py:1801–1866 — CLI argument injection surface.** `run_backtest` passes client JSON fields (`engine`, `symbols`, `timeframe`, `start`, `end`, `capital`, params) verbatim into a `subprocess.run([sys.executable, "-m", "runner.cli", ...])` argv. List-shaped `symbols` items beginning with `-` become flags to runner.cli; `engine` selects arbitrary engine modules. No allow-listing anywhere (live route inherits the same shape).
5. **[major] core.py:4673–4701 — `data_resample` spawns `data_tools.resample --source <user string>`**; the source path is unvalidated, letting a request point the resampler at any filesystem location (read/re-write side effects depend on that module).
6. **[minor] core.py:4611–4651 — `agent_convert` forwards arbitrary user-supplied `code` to an LLM conversion agent which executes/generated-writes strategy files; no sandboxing or size cap at this layer (risk accepted by product, but worth flagging given no auth on the route).**

### (c) Baseline smells

- **Divergent Change [major]** — core.py is one 4,888-line module serving ≥7 unrelated reasons to change: backtest serialization (119–470), OHLCV caching (379–441), indicator math (1161–1366), strategy registry (163–215, 1689–1767), portfolio simulation (2056–2320), task/job orchestration (2811–4159), data-download management (4388–4808), log viewing (2407–2603), frontend serving (4850–4865). Any one of these domains forces edits here.
- **Shotgun Surgery [major]** — every task-lifecycle change requires synchronized edits to core.py's `_update_task_fields/_set_task_status/_mark_task_finished/_serialize_task` AND main.py's wrappers (main.py:2788–3202) which monkey-patch them back together (main.py:3195–3202). Adding one field to a task touches both files.
- **Duplicated Code [major]** — (i) `_safe_float` core.py:822–829 ≡ main.py:1445–1452; (ii) `_downsample_numeric_minmax` core.py:961–1000 ≡ main.py:1670–1718 (same algorithm, diverging comments); (iii) optimization-run endpoint body core.py:1904–2012 ≈ main.py:8592–8695 (~110 lines each); (iv) `create_portfolio` core.py:2108–2298 ≈ main.py:8833–9068; (v) stats payload construction core.py:1113–1145 ≈ main.py:6961–6994 including the shared `best_trade` bug; (vi) benchmark resampling block core.py:2616–2646 ≈ main.py:10784–10812; (vii) dukascopy/jforex runners 3421–4019 share ~200 lines of identical scaffolding.
- **Speculative Generality [major]** — the whole Flask compatibility layer: `_RequestPlaceholder` (81–86), `jsonify` (89–90), `send_from_directory` (93–94), `_NoopRouteApp` (97–109), `request`/`app` globals (112–113), plus ~40 decorated dead routes and the `main()` entrypoint (4871–4888) that cannot run. Delete or reduce to plain functions actually imported by main.py.
- **Repeated Switches [minor]** — source-name dispatch repeated: `_missing_ohlcv_details` (480–492), `_download_task_labels` (4350–4378), `_task_cache_invalidation_reason` (4388–4403), `data_download` (4505–4596) all re-decide behavior from the same source string set; a single SOURCE_REGISTRY map would prevent the four lists drifting (they already disagree — `harvest` appears only in the invalidation map).
- **Long Function / middle-manager layers [minor]** — `_extract_ast_parameters` (1578–1686) mixes three parsing strategies in one 108-line function; `_strategy_payload` (1689–1716) eagerly scans all backtests and portfolios for detail payloads that `list_strategies` immediately pops off (1729–1731) — wasted work by design.
- **Message Chains [minor]** — `meta.get("data", {}).get("symbols", [])` chains recur ~15× (e.g. 232–237, 1040–1041, 1386–1389, 1489–1494); a meta accessor helper would localize model-shape changes.

### (d) Maintainability hot spots

1. The dual-module split itself: core.py should shrink to pure helpers consumed by main.py; today it is simultaneously library, dead framework, and duplicate API.
2. Task orchestration trio (`_start_data_task` / `_start_dukascopy_task` / `_start_jforex_task`) — extract the shared scaffold (record creation, pause/cancel polling, progress math, log writing) behind one parameterized runner.
3. In-memory registries (`_running_jobs` 69, `DATA_TASKS` 2811) as module globals mutated from many threads — centralize access in one small class with locking; today correctness depends on discipline spread over 2,000 lines.
4. The orphan fragment at 1505–1506 is evidence there is no import-time smoke test for `viewer.core` in CI; add `python -c "import viewer.main"` to CI.

---

## File: `viewer/main.py`

### (a) Bugs / correctness risks

1. **[critical] main.py:7737–8103 (`run_backtest`) & 8106–8455 (`run_backtest_signals`) — worker threads can strand tasks and leak `_running_jobs`.** The `_run()` closures catch `(subprocess.SubprocessError, OSError, RuntimeError, ValueError)` (8082) / `(RuntimeError, ValueError, OSError)` (8436) only. An unexpected exception (KeyError from `_running_jobs[task_id]` if the entry was concurrently deleted, AttributeError, pandas internals) propagates out of the thread: `_mark_task_finished` never runs, the task stays "running" in UI forever, and `_running_jobs[task_id]` leaks. Also `report_stage(handle, ...)` at 7930 references `handle` before the `with log_file.open(...)` binding in one closure arrangement — safe as written only because `report_download_phase` is defined after `handle` exists lexically; any reorder breaks it silently.
2. **[major] main.py:7786–8101 — `_running_jobs` mutated from event-loop thread + worker threads with no lock** (writes at 7790, 7935, 8059–8061, 8079–8081, 8094–8096; reads at 8586). Same lost-update/torn-state risks as core.py; additionally `get_backtest_run_status` prefers the DB-backed task snapshot but falls back to this unsynchronized dict.
3. **[major] main.py:6921–6936 — `rename_backtest` writes to `BACKTESTS_DIR / f"{backtest_id}.trades.json"` where `backtest_id` comes from the URL with no whitelist.** Unlike grid-results (core.py:2365–2381) there is no `[A-Za-z0-9_-]+` check; a crafted id with separators targets other directories. It also round-trips the *entire* result through `result.save()` to change one notes field — a corrupting failure mid-write destroys the artifact (no temp-file + rename).
4. **[major] main.py:9096–9131 — `get_portfolio`/`rename_portfolio` build `PORTFOLIOS_DIR / f"{portfolio_id}.json"` from raw path input** (also 8741–8744 in `compare_portfolios`). `create_portfolio` now sanitizes names via `_portfolio_identifier` (8993, good), but these read/write endpoints still accept `../` ids; rename writes attacker-chosen paths. Apply the `_GRID_RESULT_ID_RE` pattern.
5. **[major] main.py:632–638 — CORS misconfiguration: `allow_origins=["*"]` together with `allow_credentials=True`.** Per CORS spec the combination disables the credential grant (browsers reject `*` with credentials), so either intent is unmet: if credentials matter, origins must be enumerated; if not, drop `allow_credentials`. As written it invites CSRF-style abuse of the unauthenticated destructive endpoints below from any origin in non-browser clients.
6. **[major] main.py:6770–6829, 8779–8830 — destructive bulk endpoints with zero confirmation/auth:** `deduplicate_backtests` deletes every fingerprint-duplicate except the newest (fingerprint = strategy/symbols/timeframe/dates/engine — equal fingerprints can still be *different* runs the user wants, e.g. differing parameters not in the fingerprint), `delete-bulk` deletes arbitrary id lists, `remove-empty_backtests` deletes anything with `trade_count == 0` (an in-flight run persisted early looks empty). One accidental click is unrecoverable — no soft-delete.
7. **[major] main.py:9953–9973, 9976–10017, 10767–10773 — async routes doing heavy blocking I/O directly on the event loop.** `data_overview`, `data_category_details`, `list_symbols` call `_get_data_views()` synchronously (parquet metadata scans, potentially thousands of files — see `_scan_single_file_ohlcv_entries` 5573–5611). Likewise `get_benchmark` (10776–10834) does full DataFrame loads/resamples inline, as do `get_trade_with_context`/replay/compare (7463–7678). During any of these the whole server — including websockets — stalls. Most sibling routes correctly use `asyncio.to_thread`; these were missed.
8. **[major] main.py:3381, 1148, 410 — `asyncio.run()` inside sync helpers reachable from the running loop's thread.** `_discover_live_accounts_payload` calls `asyncio.run(discover_configured_accounts())`; `_restart_interrupted_task` (run via `asyncio.to_thread` at 10569 — OK) but `_resume_interrupted_tasks` (357–570) runs inside lifespan startup on the main thread *before* the loop serves, calling `asyncio.run(run_backtest(...))` (410) — creating and destroying a fresh loop while the app's own loop exists is fragile; if call ordering ever shifts this raises "cannot be called from a running event loop".
9. **[major] main.py:3195–3202 — module-level monkey-patching of another module's privates.** Rebinding `legacy_viewer._create_task_record = _create_task_record` etc. is the only thing keeping the two halves coherent; any second importer of `viewer.core` (tests, scripts) sees patched behavior depending on import order. Fragile hidden coupling — should be explicit composition.
10. **[major] main.py:154–264 — ~110 alias imports of `legacy_viewer` internals, then a second redundant alias block `_LEGACY_CREATE_TASK_RECORD = _create_task_record` … (257–264)** — two names for every concept; readers must track which is which. This is Middle Man at module scale.
11. **[minor] main.py:10288–10332 — `agent_convert` accepts unlimited-size `code` strings into memory and persists them into task config JSON; no length cap (DoS-ish) and provider allow-list differs from core.py's (`SUPPORTED_LLM_PROVIDER_NAMES` vs local `AGENT_CONVERT_PROVIDER_NAMES`) — same endpoint, different validation depending on which file you read.
12. **[minor] main.py:1455–1471 — `_parse_iso_datetime_cached` is `@lru_cache(maxsize=8192)` keyed by raw text; timestamps with microsecond variety evict quickly, and cached `None`s from transient garbage are sticky — minor, but note cache poisoning by user-controlled strings.
13. **[minor] main.py:3519–3520 — `_seed_default_live_accounts` is `def …: return` — dead placeholder kept and called nowhere visible; delete.
14. **[minor] main.py:2408–2422 — `maybe_fraction` and `maybe_win_rate` are byte-identical nested functions**; one helper suffices.
15. **[minor] main.py:2570–2585 vs 2719–2733 — the 11-key recovery-metadata key list is duplicated twice**; adding a key requires editing both loops plus `_apply_recovery_data_metadata`.
16. **[minor] main.py:8913–8916 — component win counting uses `> 0` on weighted pnl while `_calculate_realized_live_pnl` (3720–3723) treats 0-pnl fills separately — inconsistent win definitions across pages.
17. **[minor] main.py:9632 — `recent_fills.sort(key=… execution_timestamp …)` sorts by ISO string; mixed offsets/precision would misorder (should sort by the unix field already present).**
18. **[minor] main.py:10541–10542 — task list includes logs only for non-final tasks (`include_log = status not in FINAL_TASK_STATUSES`) but `_serialize_task` still computes `progress_payload` from log content for final tasks via `_task_progress_payload(task, log_content="")` — completed tasks created before persistence show 0% until `_mark_task_finished`'s percent bump; cosmetic staleness.

### (b) Security

1. **[critical] main.py:10661–10687 — `DELETE /api/data/symbol?path=…` retains the prefix-check bypass.** `str(file_path).startswith(str(data_root))` (10670) accepts `/home/ubuntu/data_backup/x` and any sibling sharing the prefix; the endpoint then `unlink()`s it and rebuilds the catalog. Combined with no authentication anywhere in the app, this is remote arbitrary file deletion within (and, via the prefix flaw, adjacent to) the data root. Fix: `file_path.is_relative_to(data_root)` + optional extension whitelist + confirm-token.
2. **[major] main.py:6911–6936, 9096–9131, 8741–8744 — path-keyed read/write endpoints without id whitelists (see bugs #3/#4).** Rename endpoints additionally write whole JSON blobs to computed paths — arbitrary file overwrite within traversal reach.
3. **[major] main.py:8458–8568 — `run-from-signals` reads **any** `filepath` from the request** (`load_signals_from_csv(file_path)`, 8485): arbitrary file *parse* oracle (error messages distinguish found/not-found/invalid), and `metadata["signal_filepath"] = expanduser()` lets callers probe home-relative paths. Constrain to a signals directory.
4. **[major] main.py:632–638 — wildcard-CORS + credentials (see bug #5) amplifies every finding in this section for browser-based attackers.**
5. **[major] No authentication/authorization on any mutating endpoint** (deletes, bulk deletes, downloads spawning subprocesses, live deployment creation, symbol deletion). If the intended boundary is "LAN tool", nothing enforces it — default bind for uvicorn invocations isn't restricted here, and core.py's own `--host 0.0.0.0` default (4874) shows the posture.
6. **[minor] main.py:7860–7894 — argument-injection surface inherited from core.py:** user strings flow into `runner.cli` argv unvalidated (flags like `--help`-style tokens possible via symbols/timeframe). Low likelihood, but cheap to close with per-field validators.
7. **[minor] main.py:9479–9498 — service-log search greps **all** log files and returns raw lines; log lines may embed broker/account identifiers — information exposure depends entirely on logging hygiene elsewhere; flagging because the endpoint is unauthenticated.

### (c) Baseline smells

- **Middle Man [major]** — the alias wall (154–264) plus wrapper functions that only forward (`rename_backtest_name` 6939–6941, `rename_portfolio_name` 9129–9131, `list_tasks` 10554–10556 forwarding `list_data_tasks`) — the PUT variants exist purely as aliases; FastAPI supports multiple decorators on one function (used elsewhere, e.g. 6875–6876).
- **Duplicated Code [major]** — (i) `_downsample_equity_curve_minmax` (7017–7111) third variant of min/max downsampling in the area (cf. core.py:961, main.py:1670); (ii) `_resolve_component_backtest_id` (3996–4099) vs `_resolve_strategy_backtest_id` (4102–4169) — ~80% shared matching logic; (iii) optimization/portfolio/stat endpoints re-implemented from core.py (see core findings); (iv) `maybe_fraction`/`maybe_win_rate` (2408–2422); (v) the 11-key loops (2570/2719); (vi) benchmark fallback-resample copied from core.py.
- **Shotgun Surgery [major]** — task lifecycle fields require edits in `_task_config_payload` (2542–2585), `_build_task_from_record` (2671–2735), `_apply_recovery_data_metadata` (2738–2785), `_decorate_task_from_record` (2611–2668) — four parallel key lists describing one record shape.
- **Divergent Change [major]** — main.py hosts backtests CRUD, matrix/grid, signal replay, CAPM, portfolios, live-trading accounts/deployments/websockets, services, verification evidence, data management, downloads, logs, static SPA middleware — 12+ domains in one 10,913-line module. Routers-per-domain (FastAPI `APIRouter`) is the obvious split.
- **Primitive Obsession [major]** — the Task is a `dict[str, Any]` carrying events, Popen handles, threads, floats, and strings, passed everywhere and defensively `.get()`-ed at ~120 sites; a `TaskRecord` dataclass would delete hundreds of `int(_safe_float(task.get(...)))` coercions and the four duplicated key lists.
- **Long Function [major]** — `create_portfolio` (8833–9068, ~235 lines), `run_backtest._run` (7860–8098, ~240), `_normalize_portfolio_payload` (4851–5086, ~235), `_compute_portfolio_contributions` (4538–4776, ~240), `get_live_dashboard` (9580–9643). Each mixes fetch/compute/serialize phases.
- **Feature Envy [minor]** — `_matrix_grid_row_with_trades` (6578–6603) and `_backtest_compare_payload` (7681–7733) reach deep into `BacktestResult.meta.data[…][…]` chains repeatedly; methods on the model (or the existing `_backtest_info_payload`) would hide the shape.
- **Refused Bequest [minor]** — `_LiveStreamBroker` topic states all share subscribe/poll/broadcast mechanics but positions/signals/executions differ only in `fetch_payload`/poll seconds; fine as composed, noted only because the four websocket endpoints (10837–10874) are copy-shaped and a fifth stream will be tempted to copy again.
- **Data Clumps [minor]** — `(symbols, timeframes, start, end, offer_side, merge_tick, merge_s1, batch_size, pause_ms, query_timeout_seconds)` appear as kwargs in `_start_jforex_task`, `_apply_recovery_data_metadata`, `_download_request_key_for_source`, and the config payload loops — solved partially by `_PreparedDownloadRequest` (10031–10045); extend that dataclass to cover the task side too.

### (d) Maintainability hot spots

1. Module size/domain count (see Divergent Change) — highest-leverage refactor in the area: split into `routers/backtests.py`, `routers/portfolios.py`, `routers/live.py`, `routers/data.py`, `services/tasks.py`, etc.
2. The core↔main bridge (alias wall + monkey-patch) — replace with an explicit `services/task_store.py` owning `DATA_TASKS`, locking, and persistence; both modules then import it normally.
3. Cache constellation — seven module-global caches with paired locks (1239–1277) each implementing warm/check/invalidate by hand; one small TTL-cache utility would remove ~300 lines and several subtle lock-scope questions (e.g. `_ensure_portfolio_meta_cache` releases the lock before refreshing, allowing stampedes).
4. Startup sequence (573–628 + `_resume_interrupted_tasks`) restarts persisted tasks with `asyncio.run` inside lifespan — fragile ordering; document or move to a dedicated supervisor task.
5. Error-handling inconsistency: some routes wrap everything in try/except → HTTPException(500, str(exc)) leaking internals (e.g. 6493, 6997, 9123), others rely on the generic handler returning a sanitized body (1056–1069). Standardize on the latter; `detail=str(exc)` can disclose filesystem paths.

---

## Fixed checklist sweep (Python) — summary of verification

| Item | Result |
|---|---|
| Mutable default args | **None found** (grep `= []`/`={}` in signatures: clean; `PydanticField(default_factory=…)` used correctly throughout main.py models) |
| Bare `except:` | **None found** (61 broad `except Exception` occurrences exist — flagged individually above where they swallow task lifecycle errors or leak internals) |
| Race conditions on shared state | **Found** — `_running_jobs` unlocked mutation (core.py:1835/1880, main.py:7790/7935/8059); `DATA_TASKS` handled correctly under `DATA_TASKS_LOCK` except read-modify-write of nested `pause_event` objects outside the lock (core.py:4742–4745, main.py:10582–10586) |
| SQL string building | **None** — no SQL constructed in area; DB access goes through `shared.database` helpers |
| Subprocess without timeout | **Found (partial)** — core.py:1870 has timeout=300 ✓, core.py:1787 timeout=30 ✓, core.py:3370 `Popen` **without timeout** (streaming design; mitigated by stop_event but a hung child blocks the task forever), main.py:7967 `Popen` mitigated by `proc.wait(timeout=300)` at 8019 ✓ |
| tz-naive datetimes | **None found** — `datetime.now()` naive form absent; all uses are `datetime.now(timezone.utc)`; pandas parses pinned to `utc=True` throughout (spot-checked ~30 sites) |
| Resource leaks | **Found** — unbounded `_running_jobs` growth (never pruned); `DATA_TASKS` grows until manual DELETE; `_OHLCV_DF_CACHE` bounded by signature replacement but holds full DataFrames per symbol/timeframe indefinitely; `_parse_iso_datetime_cached` lru_cache bounded ✓; log-file handles opened in `with` blocks consistently ✓ |

## Coverage statement

All four tracked files fully accounted for: `__init__.py` (scanned, empty), `comparison.py` (197/197 lines), `core.py` (4,888/4,888 lines in 3 reads), `main.py` (10,913/10,913 lines in 6 reads). No files under `reports/` were read; review conducted blind per instructions.
