# Code Review — `viewer` area (Reviewer r1, blind & independent)

Scope: every tracked code file in the viewer package.

- `viewer/__init__.py` (0 lines)
- `viewer/comparison.py` (197 lines)
- `viewer/core.py` (4,888 lines) — legacy Flask-era helper/route module, now imported by FastAPI app
- `viewer/main.py` (10,913 lines) — FastAPI application

Review method: each file read end-to-end in chunks (core.py 1–4888, main.py 1–10913 fully covered). Findings cite `file:line`. Severity: critical / major / minor. Baseline smells are Fowler ch.3 names; per instructions these are always judgement calls and repo conventions may override — where a smell appears endorsed by the surrounding architecture (e.g., the deliberate legacy-compat shim in core.py), it is flagged but noted as likely intentional.

---

## 1. `viewer/__init__.py`

**Scanned: empty file (0 lines). Nothing found.**

No bugs, no security issues, no smells. It exists solely to make `viewer` a package; note that it carries no re-exports, so all consumers import `viewer.core` / `viewer.main` directly (fine).

Findings count: 0.

---

## 2. `viewer/comparison.py`

Small, self-contained math helpers. Overall clean, but several numeric-correctness issues that matter for a trading platform.

### (a) Bugs / correctness risks

1. **[major] comparison.py:20 — division-by-zero guard returns 0.0 instead of NaN/skip.** `compute_returns` maps any period whose previous equity is exactly 0 to return `0.0`. A zero-equity bar is usually a data corruption or liquidation event; folding it in as "0% return" silently distorts Sharpe/Sortino/correlation computed downstream (`compute_combined_stats`, lines 91–124). A missing/invalid point should be excluded from the series, not treated as a flat period.
2. **[major] comparison.py:34 — drawdown formula wrong for non-positive peaks.** `(v - peak) / peak if peak > 0 else 0.0`: when the series starts at ≤ 0 (possible after catastrophic loss reconstruction from trades, see `_equity_curve_from_trades` in core.py which can produce negative running equity), drawdown is reported as 0.0 even though the curve may be far below its peak. Same class of bug as core.py:935 (`dd = (peak - value)/peak if peak > 0 else 0`) — duplicated logic with identical flaw.
3. **[minor] comparison.py:72–73 — normalization uses only `vals_a[0]` / `vals_b[0]`; no check that the first value is finite/non-NaN.** If either leading value is NaN or negative, the combined curve becomes NaN-poisoned or sign-flipped. `_align_series` does no numeric validation of values (line 57–60).
4. **[minor] comparison.py:96 — population std-dev (divide by n) used for Sharpe.** Sample std (n−1) is the convention for annualized Sharpe on daily returns; with short overlap windows this systematically overstates Sharpe. Judgement call, but worth documenting since the constant 252 implies a daily-frequency assumption that `_align_series` does not guarantee (it aligns to whatever timestamps exist — could be intraday bars).
5. **[minor] comparison.py:97,122 — Sortino uses `mean_r / downside_std` without subtracting a target/MAR, and `downside_std` divides by `len(downside)` (count of negative returns) rather than `len(combined_rets)`.** The standard Sortino denominator uses all periods (or a target). As written, Sortino is inflated whenever there are few losing days.
6. **[minor] comparison.py:116 — dead condition.** `net_return = ... if combined_vals else 0.0` — line 88 already returned early when `combined_vals` is empty, so the ternary can never take the false branch. Harmless but misleading.
7. **[minor] comparison.py:104 — `min(dd_series)` for max drawdown relies on drawdowns being ≤ 0 by construction; fine here, but inconsistent with core.py/main.py which report drawdown as a positive percentage. Consumers of `combined_stats["max_drawdown"]` get a negative number while `/api/backtests` returns positive `max_drawdown_pct` — a cross-endpoint unit inconsistency (see main.py:2263 vs comparison.py:129).

### (b) Security

Nothing found. Pure computation, no I/O, no eval/exec.

### (c) Baseline smells

- **Duplicated Code → extract shared shape**: `compute_drawdown_pct` (25–35) duplicates the drawdown loop reimplemented in core.py `_quick_stats_and_equity` (930–938) and again inline in main.py portfolio stats (main.py:1745–1753); three hand-rolled variants of "running max + relative drop" with subtly different semantics (sign, rounding, zero-guard). Extract one canonical helper into this module and reuse.
- **Primitive Obsession**: equity series travel as `list[list]` of `[time_str, value]` (53–77) and as parallel lists `dates/vals_a/vals_b` (63–67) — a Data Clump that forces callers to keep three arrays aligned by position. A tiny `EquitySeries` value type would remove the positional contract and the risk of passing mismatched lengths (nothing validates `len(dates)==len(vals_a)==len(vals_b)` before `compute_combined_equity` zips them at line 74–77; zip truncates silently).
- **Mysterious Name (minor)**: `compute_combined_equity(..., start_capital: float = 100000)` — the magic default 100000 recurs across modules (also core.py:1013, 2084, 2659; main.py many sites). Repo seems to standardize on $100k, but it should be one named constant, not literals everywhere (see Shotgun Surgery below).

### (d) Maintainability hot spots

The module is small and cohesive; the main risk is that it is now half-dead: `compare_backtests` is consumed only through `viewer.main`'s `/api/compare` endpoint (main.py:7678), while the richer two-backtest compare path (`/api/backtests/compare`, main.py:6832) bypasses this module entirely and computes deltas ad hoc. Two comparison implementations coexist — Divergent Change risk if metrics definitions evolve.

---

## 3. `viewer/core.py`

This is the legacy module: it still contains Flask-style route registrations against a fake `app` object (lines 97–113, and ~40 `@app.route` decorators), plus helpers imported by `viewer.main`. Several findings below are consequences of that hybrid state. The repo clearly *intends* the shim (docstrings at lines 2–5, `_RequestPlaceholder` raising with an explanatory message), so "Speculative Generality / dead code" style findings are tempered accordingly — but the route functions themselves are unreachable via HTTP and some contain real logic that will never run; that is flagged as correctness risk because readers/tests may believe the endpoints are live.

### (a) Bugs / correctness risks

1. **[critical] core.py:1505–1506 — orphaned code after `return` inside `get_backtest_ohlcv`.**
   ```python
       return jsonify(ohlcv_data)

       comparison = compare_backtests(strategies)
       return jsonify(comparison)
   ```
   Lines 1505–1506 are dead statements referencing undefined names (`strategies`, `compare_backtests` was imported at line 52 so only `strategies` is undefined). This looks like the remains of a deleted `/api/compare` handler; harmless at runtime (never executes) but it proves an incomplete refactor and will trip linters/readers. Delete.

2. **[major] core.py:1074–1092 — `rename_backtest` writes JSON non-atomically and trusts client-controlled `backtest_id` for path construction.** `path = BACKTESTS_DIR / f"{backtest_id}.trades.json"` (1077): a request id containing `/` or `..` is not neutralized (unlike `_resolve_grid_result_path` at 2368–2381 which does whitelist). On FastAPI this exact handler is superseded (main.py:6911 re-registers PATCH), but the function is still importable and the same unguarded pattern is what `_load_result` (119–123) also uses. Path-traversal containment should be centralized once (Shotgun Surgery — see §c).
   Additionally `json.loads(path.read_text())` → mutate → `write_text` (1086–1089) is a lost-update race if a backtest process saves concurrently; no temp-file+rename.

3. **[major] core.py:1834–1892 (`run_backtest`) — job registry race + unbounded growth.** `_running_jobs[job_id]["status"]` is mutated from the worker thread while `get_backtest_run_status` reads it from another thread without a lock (dict ops are GIL-atomic individually, but read-modify-write sequences like `_running_jobs[id]["status"]="completed"; _running_jobs[id]["result"]=output` at 1880–1881 can interleave with a reader seeing status=completed with result=None). Entries are never removed, so long-running servers leak memory per run. The newer task framework (DATA_TASKS + lock) supersedes this, yet `run_backtest` here still populates it — and main.py aliases `_running_jobs = legacy_viewer._running_jobs` (main.py:209) so both modules share the leaky dict.

4. **[major] core.py:1870–1876 — subprocess timeout kills nothing about children but is fine; however `timeout=300` raises inside the thread and is caught at 1885 — OK — but `proc.stdout+proc.stderr` (1879) can be enormous (full runner logs) and is stored verbatim in the in-memory job record and returned wholesale by the status endpoint.** Unbounded response size / memory growth; minor DoS surface on a local tool.

5. **[major] core.py:3370–3378 (`_start_data_task.run_proc`) — pause/resume of subprocesses uses SIGSTOP/SIGCONT on the direct child only; the spawned `data_tools.* -m` processes may spawn their own children (dolt, java) which won't be stopped.** Also `process.kill()` on cancel (cancel_data_task 4783–4785) doesn't kill the process group — orphaned downloaders can keep writing parquet files after "cancelled". Use `start_new_session=True` + `os.killpg`.

6. **[major] core.py:3413 / 3646–3648 / 4016–4018 — daemon threads run downloads; process exit discards them mid-write.** Combined with `_mark_task_finished` persistence, a shutdown during parquet write can leave truncated files that later pass mtime-based cache checks (`_OHLCV_DF_CACHE` signature is mtime+size, core.py:390–397 — a same-size partial write would be served as valid).

7. **[major] core.py:2660 — benchmark equity crashes on zero/negative first close:** `shares = initial / first_close` — no guard; a corrupt row (close=0 passes `_df_to_ohlcv_records`' finiteness mask? No — 0 is finite, so close=0 rows survive filtering at 562–569) yields ZeroDivisionError → 500. Same pattern at main.py:10825–10826.

8. **[major] core.py:2650–2653 — benchmark date filter compares tz-aware Timestamp column against naive strings:** `df[df["timestamp"] >= start]` where `start` is a raw query-param string. pandas compares a datetime64[ns, UTC] column to string fine, but if `start` carries a timezone offset different from UTC the comparison is still correct (pandas coerces), whereas mixed-naive columns (if any parquet stored naive stamps that survived line 417's `utc=True`... they don't) — actually safe due to line 417; downgraded to minor: the filter is applied AFTER resampling in the fallback branch (2620–2646) but BEFORE in the primary branch, so `start/end` semantics differ between branches (resample fills gaps with dropped NaN rows then filters — boundary days can differ).

9. **[minor] core.py:247–257 `_iso_to_unix` swallows all ValueError/TypeError → 0.0**, and callers treat `unix_time <= 0` as "skip" (e.g. `_normalize_equity_points` 264–266). Legitimate pre-1970 timestamps impossible here, but epoch-0 exactly is dropped silently. Acceptable; noting the sentinel conflation of "invalid" and "epoch".

10. **[minor] core.py:296 — `ordered_trades[0].entry_time or ordered_trades[0].last_exit_time`**: if both are None/empty, `_iso_to_unix("")→0.0`, `first_trade_time>0` false → skip seed point; fine. But trade ordering key at 288–293 mixes `last_exit_time or entry_time` for the primary sort while the seed point uses entry_time — the initial-capital point can land after the first trade point if the first trade exits before a second enters earlier than its own exit; `_normalize_equity_points` sorts anyway, so the seed can be silently collapsed/dropped by the dedupe at 273–278 (keeps the LAST point at equal time — i.e., a trade PnL overwrites the initial capital marker at the same second).

11. **[minor] core.py:541–551 — manual int64→float timestamp conversion:** `timestamp_series.array.asi8` on an all-NaT series raises? `asi8` on NaT gives iNaT (int64 min) which is handled at 542–543 — good. But `asi8` fails for dtype `datetime64[ns, tz]`? For tz-aware Arrow-backed arrays `.array` may not expose asi8 (ArrowExtensionArray lacks asi8 pre-2.x → AttributeError, uncaught → 500). Wrapped nowhere. Low likelihood given line 417 normalizes, but `_df_to_ohlcv_records` is also called on frames built elsewhere (replay path passes frames straight from `_load_ohlcv_df`, so normalized — OK).

12. **[minor] core.py:1176–1184 `_add` closure + regex-based indicator detection (1187–1234)**: heuristic parsing of strategy source with `re.finditer(r"vbt\.MA\.run\(([^)]*)\)"...)` breaks on nested parens/multi-line calls; defaults injected (SMA 20, EMA 12 …) can display indicators the strategy doesn't use — cosmetic, but misleads traders reviewing charts.

13. **[minor] core.py:1360–1366 `_indicator_to_points` iterates `series.items()` and indexes `df.iloc[i]` assuming positional alignment; if `df_slice.reset_index(drop=True)` was forgotten anywhere the mapping silently mismatches (call site at 1449 does reset; replay call site in main.py:7589 passes `replay_df` NOT reset — `df.iloc[np.flatnonzero(keep_mask)]` preserves original indices, so `df.iloc[i]` inside `_indicator_to_points` uses the series' positional i against a frame whose labels differ — wait, `iloc` is positional on the frame too, so alignment holds; but `ta.supertrend` etc. compute on the sliced frame whose index is non-contiguous — pandas_ta output index follows input, `series.items()` yields those labels, and `df.iloc[i]` treats them as positions → WRONG ROW when the mask keeps non-contiguous slices.** In main.py:7587–7589, `replay_df` comes from `_slice_replay_ohlcv` (main.py:7419–7425) which returns `df.iloc[np.flatnonzero(keep_mask)]` — positional indexing preserved (iloc returns positional rows but KEEPS original labels). Then `_indicator_to_points(df=replay_df, series=computed_on_replay_df)`: `series.items()` yields (label, value); label == positional only if labels are 0..n-1. After boolean-row selection labels are the ORIGINAL df labels (e.g., 0, 37, 38, 120…), so `df.iloc[label]` fetches the wrong bars → indicator points mapped to wrong timestamps. This is a genuine [major] bug in the replay indicator path (manifests in main.py:7589 via this shared helper). Fix: `reset_index(drop=True)` before computing, or use `df.loc`.

14. **[minor] core.py:2062–2076 `list_portfolios` builds mini-equity with fixed 50-point step sampling including an appended last point → 51 points; inconsistent with `_downsample_numeric_minmax` used elsewhere. Cosmetic inconsistency.

15. **[minor] core.py:2127 — local `import pandas as pd` shadowing the module-level import (line 30). Redundant; delete.

16. **[minor] core.py:2264 — `portfolio_id = f"{name.lower().replace(' ', '-')}-..."`: name with slashes/path chars lands in filename at 2295 (`PORTFOLIOS_DIR / f"{portfolio_id}.json"`). A name like `../../x` produces `../../x-20250101....json` → traversal WRITE outside PORTFOLIOS_DIR. main.py's replacement `_portfolio_identifier` (main.py:5089–5092) sanitizes properly, so the live endpoint is safe — this legacy one is only dangerous if called directly. Marked minor for reachability, but the pattern difference is instructive.

17. **[minor] core.py:4532–4339 `_task_symbol_summary`: `"all symbols"` vs `f"{symbols[0]} +{len(symbols)-1}"` says "+N-1" where N = total, i.e. "+4" means 5 symbols — off-by-one in the human-readable label ("A +4" should be "+4 more" = 5 total; it renders total-1 correctly as "more", so label is right; withdrawing).

18. **[minor] core.py:4829–4836 `delete_symbol_data` prefix check `str(p).startswith(str(dd))`** — classic prefix-match bypass: `/data/evil` matches `/data` prefix for e.g. DATA_DIR=/home/u/data vs /home/u/data2 (no slash separator). `Path.is_relative_to` (used correctly in main.py:10670? — main.py:10666–10671 repeats the SAME string-prefix check) — both copies share the flaw; resolve() mitigates `..` but sibling-directory confusion remains. Should be `p.relative_to(dd)` / `is_relative_to`.

19. **[minor] core.py:4853–4865 `index`/`static_files` call `send_from_directory`, which at line 93–94 is defined to RAISE RuntimeError("Flask static serving is unavailable")**. These routes are dead under FastAPI (middleware serves statics, main.py:992–1014) but if anything ever invokes them they raise. Consistent with the intentional shim — noting only that `index()` is registered on the fake app and unreachable; fine.

20. **[minor] core.py:2532 — `total_size = sum(path.stat().st_size ...)`: TOCTOU with rotating log files can raise FileNotFoundError mid-listing (stat after glob). Caught nowhere in `list_logs` → occasional 500. Wrap stat in try.

21. **[minor] core.py:4445–4454 `_signal_process` ignores PermissionError** from os.kill (ESRCH handled); a zombie/reparented pid raises ProcessLookupError — handled; EPERM would propagate → 500 on pause/resume.

### (b) Security

1. **[major] core.py:4827–4836 (and its twin main.py:10662–10675) — arbitrary file deletion with weak containment.** `DELETE /api/data/symbol?path=...` deletes ANY file whose resolved path string starts with DATA_DIR's string. Combined with symlink resolution it is mostly OK, but (i) the prefix test lacks a trailing-separator check (sibling dirs like `/data-prod` pass if DATA_DIR=`/data`), (ii) there is no authentication anywhere in the app (CORS `allow_origins=["*"]` with `allow_credentials=True`, main.py:632–638 — see main.py section) so any web page the operator visits can drive these endpoints (DNS rebinding / CSRF-style local exfil/deletion). Severity major given destructive capability.
2. **[minor] core.py:2558–2565 `get_log` — `LOGS_DIR / log_name` without containment (the main.py version at main.py:10721–10730 DOES resolve+is_relative_to).** Legacy copy allows `GET /api/logs/../../etc/passwd`-style reads IF this handler were mounted; it isn't under FastAPI. Flagged as latent hazard in shared helper code.
3. **[minor] core.py:137 json.loads on optimization files, 1010/1519/2063/2307 etc. — JSON files loaded from disk without size limits; a huge file blocks the event loop thread pool. Local-tool acceptable.
4. Nothing found re: SQL string building (no SQL in this module — DB access is behind `shared.database`), eval/exec, or secrets logging. Task logs do echo full commands (3335, 3457, 3692) — commands include only user-supplied symbol/timeframe/date strings passed through argparse lists (no shell=True anywhere — good; verified all `subprocess.run/Popen` calls use list argv).

### (c) Baseline smells

1. **Divergent Change (major, structural)**: core.py is four modules wearing one trenchcoat: (i) backtest/portfolio read APIs, (ii) strategy registry + AST parameter extraction (`_extract_ast_parameters`, 1578–1686), (iii) the entire background-task/download engine (2811–4651), (iv) data catalog/scanning. Any change to downloader lifecycle touches the same file as chart payload shaping. Split per reason-to-change.
2. **Shotgun Surgery (major)**: the same micro-logic exists in 2–4 places and must be edited together:
   - drawdown computation: comparison.py:25 / core.py:930 / main.py:1745;
   - `_safe_float`: core.py:822 AND main.py:1445 (verbatim duplicate);
   - `_downsample_numeric_minmax`: core.py:961 AND main.py:1670 (verbatim duplicate);
   - stats payload shaping: core.py:1113–1145 AND main.py:6962–6994 (near-verbatim);
   - benchmark resampling block: core.py:2618–2646 AND main.py:10785–10812 (copy-paste);
   - dukascopy genesis validation: core.py:4482–4499 AND main.py:10076–10097;
   - path-containment checks: core.py:4829 vs main.py:10667 (both flawed the same way — fixing one fixes nothing).
   The duplication between core.py and main.py is partially *by design* (main.py wraps legacy functions), but the wrappers re-implement rather than delegate in several of the above cases.
3. **Middle Man / dead delegation layer (judgement call, likely endorsed)**: main.py:205–264 rebinds ~50 underscore-private names from core.py, then main.py:3194–3202 monkey-patches core.py with wrapper versions — a circular shim so legacy calls route through persistence-aware wrappers. It works, but it makes control flow very hard to trace (two live implementations of `_get_task`, `_serialize_task`, etc., swapped at import time). Document or invert (move the task engine out of core.py entirely).
4. **Refused Bequest-ish / Speculative Generality (minor)**: `_RequestPlaceholder` (81–86), `send_from_directory` stub (93–94), `_NoopRouteApp` (97–113) exist only so that ~40 dead `@app.route` decorators don't crash at import. If those routes are truly retired (FastAPI equivalents live in main.py), deleting the routes + shims removes ~700 lines of misleading "endpoints". Kept intentionally per docstring — but every dead route body is still maintained as if alive (they appear in greps, tests target them?). Recommend deletion with a git history breadcrumb.
5. **Repeated Switches (minor)**: `_missing_ohlcv_details` (467–509), `_download_task_labels` (4350–4378), `_task_cache_invalidation_reason` (4388–4403) and main.py's `_auto_download_source_plan` all encode source/category knowledge in if-chains keyed on strings. A single SOURCE registry (name → labels, invalidation reason, fallback chain) would collapse them.
6. **Data Clumps (minor)**: `(symbol, timeframe)` travels as separate params through ~15 functions (`_load_ohlcv_df`, `_missing_ohlcv_details`, `_infer_symbol_data_category`, genesis collectors…); download identity dicts (main.py:3005–3034 mirrors core.py:2817–2845) re-pack the same 10 fields repeatedly. Bundle into a `DownloadRequest`/`SymbolTimeframe` type.
7. **Long Function (minor, adjacent to baseline list)**: `_start_jforex_task.run_download` (3691–4014) is ~320 lines with a nested fallback ladder; `_extract_ast_parameters` (1578–1686) similarly dense. Both are prime extraction targets.
8. **Mysterious Name (minor)**: `BARS_BEFORE=60/BARS_AFTER=25` fine; but `_LEGACY_*` aliases in main.py referencing core.py underscore names make grep ambiguous (which `_create_task_record` runs?). Naming the module-level indirection explicitly would help.

### (d) Maintainability hot spots

- The dual-module split (core.py legacy ↔ main.py modern) is THE hot spot: every fix must ask "which copy?" (see Shotgun Surgery items). 
- Global mutable state: `_running_jobs` (69), `_OHLCV_DF_CACHE` (74–78), `DATA_TASKS` (2811), plus module-level `STATIC_DIR` chosen at import time from filesystem existence (60–61). Testability suffers; cache has no eviction policy (grows with distinct symbol/timeframe pairs — bounded in practice by universe size, but unbounded in principle; deep copies returned via `.copy(deep=False)` mitigate mutation).
- `_quick_stats_and_equity` (853–958) silently recomputes full stats when cache keys are missing — O(trades) work on every listing request for legacy JSON files (list_backtests 1003–1058 loops ALL backtest files and parses FULL JSON incl. trades/equity just to render cards). With hundreds of large result files this endpoint is O(repo size) per call — the SQLite meta-cache in main.py exists precisely to avoid this; the legacy path remains as a footgun.

---

## 4. `viewer/main.py`

The active FastAPI app. Generally higher quality than core.py, with real attention to caching, recovery and single-flight coordination — but it is 10.9k lines doing everything, and several concrete defects follow.

### (a) Bugs / correctness risks

1. **[critical] CORS misconfiguration — main.py:632–638.** `allow_origins=["*"]` TOGETHER WITH `allow_credentials=True`. Starlette's CORSMiddleware will not echo `*` with credentials in practice (spec forbids it), but Starlette ≥0.22 responds `access-control-allow-origin: <request origin>` when credentials are enabled and origins is `*`?? — precisely: with `allow_credentials=True` and wildcard, Starlette reflects ANY origin while allowing cookies/Authorization headers. That turns every state-changing local endpoint (delete backtest, start downloads, deploy LIVE TRADING deployments at 9693–9807!) into CSRF-reachable surface from any website the user visits. For a platform that can create live-money deployments, this is the top finding. Fix: explicit origin allowlist (e.g. localhost dev ports) or drop credentials.
2. **[critical→major] Live deployment creation has no authentication/authorization at all (whole app).** Endpoints `POST /api/live/deployments` (9693), `POST /api/live/deployments/ctrader/top5` with `activate=True` (9646–9690) create ACTIVE live trading deployments binding broker accounts. Combined with #1, any webpage can trigger real-money deployment creation. Even without CORS abuse, binding to `0.0.0.0` (core.py main() default host — main.py has no uvicorn bind shown, but core.py:4874 shows the historical intent) exposes this LAN-wide. Rate: **critical** for a trading platform.
3. **[major] main.py:1148 & 410 — `asyncio.run(run_backtest(request_payload))` called from sync context (`_resume_interrupted_tasks` runs in `asyncio.to_thread` from lifespan; `_restart_interrupted_task` is invoked via `asyncio.to_thread` from the async endpoint at 10567–10569).** `asyncio.run` inside a worker THREAD creates a fresh loop — legal — but `run_backtest` itself spawns threads and touches `DATA_TASKS` etc.: OK. However `_restart_interrupted_task` ALSO mutates task state and awaits nothing; the subtle hazard is nested-loop policy: if anyone ever calls it from the event-loop thread directly, `asyncio.run` raises. Fragile pattern; also `run_backtest` is `async def` performing blocking `subprocess.Popen` setup synchronously before returning (7860–8103 run in a thread via `threading.Thread` — fine). Downgraded to minor-major: works today, one refactor from breaking.
4. **[major] main.py:8019 — `proc.wait(timeout=300)` after fully draining stdout: the streaming loop at 7978–7997 reads until EOF, so `wait(timeout=300)` measures only post-EOF teardown — the intended "backtest timed out after 300 seconds" almost never triggers while a hung child keeps the pipe open forever.** The timeout is ineffective for the actual hang scenario (child alive, no output). Need a watchdog timer around the whole read loop.
5. **[major] main.py:7929–7930 — `report_download_phase` closes over `handle` before it is bound.** `def report_download_phase(percent, message): report_stage(handle, ...)` references `handle` from enclosing scope; it is CALLED inside `_ensure_backtest_data_availability` (7938–7952) which executes INSIDE `with log_file.open(...) as handle:` — binding exists by then. OK. But `_ensure_backtest_data_availability` raising propagates to except-clause at 8082 writing to `handle` — still in scope. Fine; withdrawing. (Verified carefully — closure is sound.)
6. **[major] main.py:8338–8359 — signal replay filters OHLCV to `[start_dt, end_dt]` inclusive of end-of-day microsecond, then adapter replays; but `_resolve_signal_replay_timeframe` may pick "1m" (3627–3628) while the data was downloaded at requested timeframe — mismatch handled upstream by auto-download using the SAME timeframe; consistent. Real issue: `frame.columns = [str(col).lower()...]` mutates the DataFrame RETURNED BY `_load_ohlcv_df` — which returns `merged.copy(deep=False)` (core.py:403/441): shallow copy shares underlying blocks; assigning `frame["timestamp"] = pd.to_datetime(...)` at 8350 replaces a column (CoW off → may emit SettingWithCopy warning or write into shared cache object under pandas<2 with `copy(deep=False)`). Under Copy-on-Write (pandas≥2) safe; under pandas 1.x this can poison the process-wide OHLCV cache for that symbol/timeframe.** Given `requirements` unknown, flag as [major-if-pandas<2]/minor otherwise.
7. **[major] main.py:1740 & 5047 & 8910 — sorting trades by `_component_trade_time` which returns ISO STRINGS; mixed-format timestamps ("2024-01-02T..." vs "...+00:00" vs "...Z" absent) sort lexicographically — mostly monotonic for ISO-8601-UTC, but entries lacking timezone vs carrying `+00:00` interleave incorrectly ("2024-01-02T10:00:00" > "2024-01-02T10:00:00+00:00"? '+' (43) < '0'? Actually shorter string is prefix → sorts first; consistent-ish). Edge cases exist where naive-vs-aware mix flips order. Minor.
8. **[major] main.py:3703–3733 `_calculate_realized_live_pnl` — win_rate denominator bug:** `total_trades = len(execution_rows)` counts RAW EXECUTION ROWS (each partial fill!), while `wins/losses` count round-trip CLOSES. `win_rate = wins / total_trades` therefore dramatically understates win rate when orders fill in parts. Also rows with direction==0 (unknown action) are skipped from PnL but STILL counted in total_trades (skip happens via `continue` BEFORE incrementing anything — wait, `total_trades` is computed after the loop from len(rows), so skipped/zero-qty rows inflate the denominator). Correct metric needs closed-trade count. [major] — displayed on live dashboard (9595–9604).
9. **[major] main.py:3750–3759 — max drawdown from BALANCE series only, ignoring equity spikes; and `peak_balance` starts at 0.0 so the first record's drawdown contribution requires balance>0 — fine — but balances ordered as returned (assume chronological from DB; no sort). If `list_live_balances`/`load_live_account_stats_inputs` returns DESC order (common for "latest-first" queries), peak tracking runs newest→oldest producing garbage drawdown. Verify ordering; defensive sort needed.
10. **[major] main.py:9842–9862 `get_live_balances` returns whatever order the DB gives; frontend draws equity curve — same ordering concern as #9 (shared root cause: no explicit ORDER BY at this layer).
11. **[major] main.py:1356–1577 — `_LiveStreamBroker._run_topic` polls `fetch_payload` every 1s per topic per connection-set; `_live_accounts_stream_payload` calls `_cached_live_accounts_payload(force_refresh=True)` (3450) which performs FULL account discovery (broker API round-trips) every second while any dashboard websocket is open; `_live_positions_payload` constructs a NEW AlpacaLiveAdapter per poll (9333) — 1 req/s/stream of REST calls; N browser tabs multiply. No backoff on error (broadcasts error each second, continues). Add jitter/cache or event-driven refresh. [major] operational risk (broker rate-limits → IP bans).
12. **[major] main.py:1455–1471 — `@lru_cache(maxsize=8192)` on `_parse_iso_datetime_cached` caches `None` results too; adversarial/varied garbage timestamps evict useful entries — fine — but the REAL issue: cached datetimes are returned SHARED; callers treat them as immutable (they are) — OK. Withdrawn; no bug. Instead: **[minor]** unbounded-ish cache of user-controlled strings is a mild memory vector (bounded 8192).
13. **[major] main.py:2788–2795 `_create_task_record` wrapper persists EVERY task creation to SQLite synchronously; `_update_task_fields`/`_set_task_status`/`_update_task_progress` (2908–2969) persist on EVERY progress tick — progress callbacks fire per downloaded symbol AND per intra-download percent (report_progress in core.py:3524–3531 fires per percent chunk from the downloader). Each tick = `upsert_background_task_record` = SQLite write (with lock contention on DATA_TASKS_LOCK held during `_LEGACY_UPDATE_TASK_PROGRESS` then ANOTHER lock acquisition inside `_persist_task_snapshot`). High-frequency writes → WAL churn and lock contention with the 1s WS pollers reading the same table. [major] performance/robustness hot spot; throttle ticks (e.g. ≥1% delta or ≥500ms).
14. **[major] main.py:2822–2855 `_get_task` — deadlock risk analysis: acquires DATA_TASKS_LOCK briefly (pop pending), releases, then `_retry_coalesced_cleanup` (DB op) outside lock — good. Then `_decorate_task_from_record(task_id, task)` mutates the SHARED task dict WITHOUT the lock (2611–2668) while other threads read/write it (progress updates hold lock). Concurrent dict mutation during iteration elsewhere? `list_data_tasks` snapshots under lock (10536–10537) — but decoration mutates live objects that snapshot already copied (shallow `dict(task)`) — torn reads of progress_detail possible (readers see new percent with old current message). Cosmetic tearing, not crash. [minor].
15. **[major] main.py:3318–3323 `_consume_live_ws_messages` catches broad Exception and pushes `_disconnect` — including `asyncio.CancelledError`? No: CancelledError is BaseException in py3.8+, not caught by `except Exception` — good. But the OUTER `_stream_live_payload` loop `await websocket.send_json(outbound)` (3323) with a slow client: queue overflow policy drops OLDEST messages silently (3229–3232) — acceptable design; heartbeat pings fill queue if client stalled → eventually drops updates. Fine. Withdrawn.
16. **[major] main.py:594–595 lifespan: `await asyncio.to_thread(_resume_interrupted_tasks)` BLOCKS SERVER STARTUP on auto-restarting potentially long operations — worse, `_resume_interrupted_tasks` calls `asyncio.run(run_backtest(...))` which STARTS the backtest and waits only for the 202-return (fast). But `_start_jforex_task` etc. return immediately too. However `run_backtest` awaited via asyncio.run includes `_ensure_backtest_data_availability`? No — that runs inside the worker thread. Startup-blocking portion = DB reads + thread starts: bounded. OK — [minor]: startup latency depends on interrupted-task count; consider backgrounding.
17. **[major] main.py:656–657, 758–759 — mutable default `config: dict[str, Any] = {}` and `parameters: dict[str, Any] = {}`, `signals: list[...] = []` (759), `strategies/components: list = []` (808–809). Pydantic/SQLModel models DO deep-copy field defaults per instance (unlike plain Python), so the classic mutable-default bug is neutralized by the framework — flagging as checked-and-safe per checklist, NOT a bug. (Same for `ServiceCreateRequest.config`.)
18. **[major] main.py:10290–10333 `agent_convert` — sends ARBITRARY USER CODE STRING to an LLM agent (`run_auto_agent(payload["code"], ...)` via `_launch_agent_task`) which presumably writes/executes generated strategy files. No auth (see #2), no size limit on `code` (a 500MB body would be queued in memory + persisted to task config via `_persist_task_snapshot` → SQLite blob). Add request-size limits and treat as privileged action.
19. **[major] main.py:8480–8489 `run_backtest_from_signals` accepts `filepath` and loads CSV from ANY path (`load_signals_from_csv(file_path)`) — arbitrary file READ probe (error messages distinguish FileNotFound vs parse errors → oracle), plus content flows into signals metadata (8519 stores expanded path). Contained to local tool threat model; [minor→major] depending on exposure; combined with CORS #1 it is remotely probeable.
20. **[major] main.py:10063–10073 — `query_timeout_seconds` validated 30..7200 for dukascopy/jforex, but `_jforex_download_command` (10126–10138) passes it to CLI; recovery path (`_resume_interrupted_tasks` 484) re-reads persisted `query_timeout_seconds` WITHOUT re-validating (only `_safe_int` default 900) → poisoned DB record can inject out-of-range value post-restart. Low impact (argparse downstream likely clamps), [minor].
21. **[major] main.py:2104–2116 `_upsert_backtest_meta_cache(backtest_id or "")` at 8043 — called with possibly "" → guarded (normalized_id empty → return) — fine.
22. **[minor] main.py:6703–6704 — duplicate query params `drawdown_min/max` AND `max_drawdown_min/max` with silent precedence — documented behavior but easy to misuse; alias without deprecation warning.
23. **[minor] main.py:2408–2422 — `maybe_fraction`/`maybe_win_rate` heuristically divide by 100 when |value|>1: a user filtering sharpe_min=1.5 is untouched (sharpe not passed through maybe_fraction — correct), but `total_return_max=150` meaning 15000%? becomes 1.5 (150%). Ambiguity inherent; document.
24. **[minor] main.py:2286–2294 `_backtest_summary_from_meta` hardcodes `"net_profit": 0.0` — list view always shows net profit 0 unless another path fills it. If intentional (not in DB), name it (`net_profit: None`) — 0 is a lie that survives arithmetic.
25. **[minor] main.py:3558–3560 `_parse_request_date(end_of_day=True)`: adds a day minus 1µs ONLY for pure-date strings — good; but comparisons elsewhere use `signal_dt > end_dt` with µs precision; a signal at 23:59:59.9999995 rounds oddly — negligible.
26. **[minor] main.py:3657–3664 `_waiting_countdown` uses `time.time()` wall clock vs candle-close math based on floor of signal time; DST irrelevant in UTC — fine; but `unit_seconds.get(unit, 0)` silently treats unknown units as never-wait → returns None → status stays "Waiting" forever with countdown None. Cosmetic.
27. **[minor] main.py:5089–5092 `_portfolio_identifier` slug collision: two portfolios named identically within the same second collide on ID → second overwrites first file (9065–9066 write_text). Add uuid suffix.
28. **[minor] main.py:7478 — `symbol = str(trade.symbol or "").strip() or "SPY"` — silent fallback to SPY when trade lacks symbol: chart shows wrong instrument rather than an error. Prefer 422.
29. **[minor] main.py:7631 — `for symbol in symbols[:10]` magic cap silently drops symbols 11+ from OHLCV payload; document or paginate.
30. **[minor] main.py:9059–9061 — portfolio save writes BOTH SQLite record AND JSON file non-atomically (write_text direct); crash between → divergent sources of truth reconciled by `_snapshot_portfolio_payloads` preferring FILE (1892–1902 loads files first, DB only fills gaps) — so stale file shadows newer DB row. Ordering choice inverted for rename path (rename writes both). Reconcile strategy documented nowhere.
31. **[minor] main.py:10714–10764 `get_log` reads ENTIRE log files into memory (`_collect_log_entries` reads full text, splits all lines) then slices last N — with `lines≤100000` allowed and multi-hundred-MB logs, one request can allocate GBs. Stream tail instead (like `_read_task_log` does with max_bytes — reuse it).
32. **[minor] main.py:9419–9465 `_service_log_tokens`/matching scans EVERY line of EVERY log with substring matching per service — O(all logs) per request; same hot spot as #31.

### (b) Security

1. **[critical] CORS wildcard + credentials + zero authN/authZ on destructive & live-trading endpoints** — see (a)#1/#2. This is the umbrella finding: `DELETE /api/data/symbol` (10661), bulk deletes (6818, 9082), deduplicate (destructive, 6770/8779), downloads spawning subprocesses (10253), agent convert executing model-generated code (10288), live deployment activation (9646/9693) — ALL callable by any origin with credentials. Even without CORS reflection, the app assumes localhost-only but binds widely and ships Sentry + GZip like a production service. Minimum fix set: explicit CORS origins; token auth for POST/DELETE; refuse non-loopback bind unless explicitly configured.
2. **[major] main.py:10288–10333 LLM code-execution pipeline reachable unauthenticated** (as above).
3. **[major] main.py:8480 arbitrary-path CSV read probe** (as above #19).
4. **[minor] main.py:10721–10730 log path containment done RIGHT here (resolve + is_relative_to) — good; contrast legacy core.py:2564. Positive note.
5. **[minor] main.py:6521–6525 `_matrix_result_path` blocks `..` and leading `/` but allows absolute Windows paths (`C:\...` contains no leading `/`)? `Path("C:\\evil").is_absolute()` is True on POSIX False! On Linux, `"C:\\..\\x"` is a relative filename — harmless locally. Fine.
6. **[minor] Error responses echo internal exception text (`detail=str(exc)`) in MANY handlers (6493, 6518, 6901, 6936, 6997, 8123, 9123, 9501, 10764…) — info disclosure of filesystem paths/library internals. The global handler at 1056–1069 correctly masks unhandled ones; the explicit str(exc) sites leak deliberately. Local tool: minor.
7. **Checked per language checklist:** no SQL string building (all DB via sqlmodel layer); no bare `except:` (all scoped); subprocess always list-argv (no shell injection found); subprocess timeouts present in core.py run paths (1874, 8019-though-ineffective) but MISSING for `subprocess.Popen` streamed tasks by design (user-cancellable) — acceptable; tz handling consistently UTC-aware (`datetime.now(timezone.utc)` throughout, `pd.to_datetime(..., utc=True)`) — GOOD, no tz-naive datetimes found except legacy `_iso_to_unix` treating naive as UTC explicitly (core.py:253–255 — correct choice); resource leaks: file handles all via context managers EXCEPT `pq.ParquetFile(parquet_path)` at main.py:5458 — ParquetFile holds an open file handle; never closed (no context manager / .close()) in `_read_parquet_summary` — repeated catalog scans leak fds until GC. [minor→major] on filesystems with low fd limits: add `with pq.ParquetFile(...) as pf` (pyarrow supports context manager).

### (c) Baseline smells

1. **Divergent Change (critical, structural)**: main.py hosts ≥9 reasons to change: app bootstrap/lifespan, middleware/CORS, backtest listing/filtering/sorting, portfolio engine + contributions math, signal replay, live trading dashboards/websockets, data catalog views, download orchestration/single-flight, task persistence/recovery, verification evidence, services registry. Any one domain change edits this 11k-line file. Highest-value refactor in the area: split routers per domain (FastAPI APIRouter makes this mechanical).
2. **Duplicated Code (major)**: within main.py itself —
   - restart-config reconstruction duplicated VERBATIM between `_resume_interrupted_tasks` (387–409) and `_restart_interrupted_task` (1125–1147): extract `backtest_request_from_config(config)->BacktestRunRequest`.
   - backtest-resolution loops duplicated between `_resolve_component_backtest_id` (4013–4099) and `_resolve_strategy_backtest_id` (4114–4169) (~80% same code).
   - pause/resume/cancel handlers (10572–10630) duplicate core.py:4731–4788 nearly line-for-line (both registered; FastAPI wins).
   - `maybe_fraction` vs `maybe_win_rate` (2408–2422) identical bodies.
   - `_deployment_ids_for_account` + trades/signals payloads share enrichment scaffolding.
   - across files: `_downsample_numeric_minmax`, `_safe_float`, stats shaping, benchmark resampling (see core.py §c2).
3. **Repeated Switches (minor)**: `_auto_download_source_plan` (5992–6054) and `_download_missing_ohlcv_data` (6057–6161) switch on source strings with per-source download calls — a SourceStrategy registry would unify with core.py's parallel chains.
4. **Primitive Obsession (major)**: tasks are `dict[str, Any]` with ~25 loosely-typed keys threaded through 30+ functions (pause_event/stop_event/process/thread stored IN the dict alongside serializable fields, requiring careful stripping in `_task_config_payload` 2542–2585 and `_build_task_from_record`). One wrong key silently persists junk to SQLite. A `TaskRecord` dataclass separating runtime handles (excluded from serialization) from payload would kill a whole bug class.
5. **Data Clumps (minor)**: `(strategy_name, symbol, timeframe)` triple passed through component resolution/contributions/filters (3996–4099, 4444+, 4779+); `(start,end)` date pairs; `(symbol,timeframes,start,end,offer_side,merge_tick,...)` download identity (already identified). Introduce value types.
6. **Message Chains (minor)**: `payload.get("components") if isinstance(payload.get("components"), list) else []` pattern repeated ~15× (1772, 1787, 1948, 4543–4547, 4786–4789, 5050…) — hide behind `def _list_of_dicts(payload,key)`.
7. **Middle Man (minor, judgement)**: `_portfolio_record_to_payload`/`_portfolio_payload_to_model` thin converters are fine; but `_snapshot_task` → `_get_task` → `_LEGACY_GET_TASK` + decorate + hydrate (2858–2860 wrapping 2822–2855) is a 4-layer delegation stack for a dict lookup — symptom of the core/main shim.
8. **Speculative Generality (minor)**: `_seed_default_live_accounts` (3519–3520) — function body is bare `return`; delete. `include_all` handling for `"*"` grid ids (6634) supports syntax nothing in the frontend documents. `_QUIET_EXACT_PATHS` machinery (911–947) plus disabled uvicorn access logger (950–957) double-implements "quiet paths".
9. **Long Function / Large Class (adjacent)**: `create_portfolio` (8833–9068, ~235 lines), `run_backtest._run` (7860–8098, ~240 lines), `_compute_portfolio_contributions` (4538–4776, ~240 lines) — extraction targets.
10. **Inappropriate Intimacy (minor)**: main.py reaches into ~50 private (`_`-prefixed) attributes of `legacy_viewer` (205–264) — the modules are effectively one program with two namespaces; formalize by moving shared code to a third module both import.

### (d) Maintainability hot spots

1. **Task lifecycle spread across three layers** (in-dict runtime state in core.py, persistence wrappers in main.py, SQLite records in shared/database) with monkey-patched indirection (3194–3202) — hardest place to reason about failures; every bug report about "stuck task" requires tracing 4 hops.
2. **Caching lattice**: 7 independent caches with distinct invalidation rules (DATA_VIEWS, LIVE_ACCOUNTS, STRATEGIES, BACKTEST_META, BACKTEST_EQUITY_PREVIEW, PORTFOLIO_META, TASK_HYDRATION) + core.py's OHLCV DF cache. Invalidation triggers are scattered (`_invalidate_data_views_cache` called in 3 places; backtest cache refreshed in run/rename/delete paths). A missed call site = stale UI with no error. Centralize invalidation events.
3. **Dual storage reconciliation** (JSON files ↔ SQLite) for backtests and portfolios, with different precedence per entity (files win for portfolios at 1888–1920; DB-first for backtests via `_load_result` 1072–1087 with file fallback that WRITES BACK to DB). Deduplicate/delete paths must touch both (`_delete_backtest_with_artifacts` 2524–2539, `_delete_portfolio_with_artifacts` 2019–2042) — every new artifact type must remember both stores (Shotgun Surgery).
4. **Progress-reporting write amplification** (see (a)#13) will degrade under production load; throttling belongs in ONE place (`_update_task_progress`).
5. **Testing surface**: pure functions (coverage math `_check_entry_range_coverage` 5811–5859, `_auto_download_source_plan`, `_calculate_realized_live_pnl`) are well-isolated and testable — good; but endpoint handlers embed business logic inline (create_portfolio), making regression coverage expensive until routers split.

---

## Cross-cutting summary (highest-priority fixes)

| # | Finding | Location | Severity |
|---|---------|----------|----------|
| 1 | Wildcard CORS with credentials + zero auth on destructive/live-trading endpoints | main.py:632–638 (+9646, 9693, 10661, 10288) | critical |
| 2 | Dead/orphaned code proving unfinished refactor (`strategies` undefined) | core.py:1505–1506 | critical (hygiene) |
| 3 | Backtest run watchdog ineffective (post-EOF wait only) | main.py:7978–8023 | major |
| 4 | Win-rate counts execution rows, not closed trades | main.py:3741–3743 | major |
| 5 | Indicator point mapping misaligned for non-contiguous replay slices (positional iloc vs preserved labels) | core.py:1357–1366 via main.py:7587–7589 | major |
| 6 | Progress persistence writes to SQLite on every tick | main.py:2908–2969 | major |
| 7 | Weak path-containment (prefix match, duplicated) for file DELETE | core.py:4829–4836, main.py:10666–10675 | major |
| 8 | Legacy rename/write paths non-atomic + unsanitized id path join | core.py:1074–1092, 2167+ | major |
| 9 | Per-second broker polling per websocket; new adapter per poll | main.py:3450, 9333, 1356–1390 | major |
| 10 | Three divergent drawdown implementations; duplicated helpers across core/main | comparison.py:25, core.py:930, main.py:1745; core.py:822≈main.py:1445; core.py:961≈main.py:1670 | major |

## Checklist sweeps (per language checklist)

- Mutable default args: pydantic models neutralize them (main.py:698, 743, 759, 808–809) — verified safe; NO plain-function mutable defaults found.
- Bare except: none found (all excepts typed; two `except Exception` broad-catch in middleware/error-handler — appropriate).
- Race conditions on shared state: `_running_jobs` unsynchronized (core.py:69, 1835–1892; main.py:209) [major]; DATA_TASKS lock discipline mostly correct with decoration-window tears [minor]; JSON↔SQLite dual-write races [minor].
- SQL string building: none — parameterized ORM layer throughout.
- Subprocess without timeout: interactive/task subprocesses intentionally unbounded-but-cancellable (documented); batch `run` paths have timeouts, though main.py's is ineffective (#3).
- tz-naive datetimes: none found; UTC enforced consistently (positive).
- Resource leaks: `pq.ParquetFile` handle not closed (main.py:5458) [minor]; daemon threads abandoned on shutdown (core.py) [minor]; `_running_jobs`/cache growth (above).
- (TS/React items N/A — Python-only area.)

## File accounting

| File | Status | Findings |
|---|---|---|
| viewer/__init__.py | Scanned, nothing found | 0 |
| viewer/comparison.py | Reviewed | 7 findings |
| viewer/core.py | Reviewed | 32 findings |
| viewer/main.py | Reviewed | 40 findings |

All four tracked files accounted for.
