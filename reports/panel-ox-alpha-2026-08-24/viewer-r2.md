# Panel Review — Area: `viewer` — Reviewer r2 (blind, independent)

Repo: `/home/ubuntu/Desktop/trading-platform`
Files reviewed (complete tracked list for the area):

| File | Lines | Status |
|---|---|---|
| `viewer/__init__.py` | 0 | Scanned — empty, nothing found |
| `viewer/comparison.py` | 197 | Fully read |
| `viewer/core.py` | 4,888 | Fully read (5 chunks) |
| `viewer/main.py` | 10,913 | Fully read (7 chunks) |

Method: every file was read end-to-end in chunks; cross-references verified with targeted greps
(`runner/stats.py`, `docs/ARCHITECTURE.md`, `tests/test_viewer_features.py`, usage of `viewer.core.app`).
Read-only review; nothing was edited.

## Repo-standard overrides applied

Per the ground rules I looked for documented repo standards that endorse a smell the baseline flags:

- `pyproject.toml [tool.ruff.lint] select = ["E","F","I","W"]` — basic lint only; no smell suppressions.
- `docs/ARCHITECTURE.md` documents that `viewer/main.py`, `viewer/core.py`,
  `frontend/src/api/client.ts`, `frontend/src/api/types.ts` "must stay synchronized" when fields
  change. This **documents** the cross-module coupling but does not *endorse* it as a pattern, so
  Shotgun-Surgery findings stand (the doc mitigates, not excuses).
- No standard anywhere endorses the two-file split of viewer logic or the dict-based task registry,
  so no baseline smell is suppressed.

Baseline rule noted up front: **every smell below is a judgement call**, not a hard violation.

---

# 1. `viewer/comparison.py`

Small pure-math module ported from a Lean integration. Generally clean, defensive about empty
inputs, no I/O. Findings:

## (a) Bugs / correctness risks

- **F-c1 · major — Comparison silently ignores everything past the first two strategies.**
  `comparison.py:139-197` (`compare_backtests`) documents "Compare multiple backtests … for 2+
  backtests", but the combined view is computed only from `strategies[0]` and `strategies[1]`
  (`comparison.py:163-164`). The live endpoint `GET /api/compare`
  (`main.py:7638-7678`) happily forwards N ids and the frontend renders a "combined" curve that in
  fact mixes only A and B. A 3-way comparison shows plausible-looking but wrong combined stats with
  no error. Either implement pairwise-N combination or reject >2.

- **F-c2 · major — `pearson_correlation` has no length-equality guard.**
  `comparison.py:38-50`: `n = len(x)` drives `mean_x`/`mean_y` and the covariance sum, while
  `std_x`/`std_y` are summed over their own lists. With `len(x) != len(y)` the result is not a
  Pearson coefficient (silently wrong), and with `x` non-empty / `y` empty it raises
  `ZeroDivisionError` (`sum(y)/n`). Today's call sites pass equal-length aligned series, so this is
  latent — but the function is public API of the module and one refactor away from a 500.
  Fix: raise `ValueError` on length mismatch.

- **F-c3 · minor — Alignment is exact-string timestamp intersection.**
  `comparison.py:53-60` (`_align_series`) intersects raw timestamp strings. `2024-01-01T00:00:00Z`
  vs `2024-01-01T00:00:00+00:00` vs numeric epochs (equity points are Unix floats elsewhere in the
  viewer, cf. `core.py:260-279`) do not intersect, yielding "No overlapping dates" for data that
  overlaps perfectly. Duplicate timestamps collapse silently last-wins via dict comprehension.

- **F-c4 · minor — Degenerate normalization produces a silently flat combined curve.**
  `comparison.py:72-73`: if `vals_a[0] <= 0` (or B's), *every* point of that leg is replaced by
  `1.0` — the combined equity becomes a constant `start_capital` line and all derived stats
  (Sharpe 0, drawdown 0) look legitimate. A NaN/error marker would be honest.

- **F-c5 · minor — Sharpe/Sortino conventions diverge from the canonical runner stats.**
  `comparison.py:93-97` annualizes with `√252` assuming *daily* observations, but the series fed in
  by `/api/compare` are per-equity-point returns (often hourly bars) — Sharpe is inflated ~√24.
  Std is population (÷n), Sortino downside-deviation ÷ `len(downside)` (`comparison.py:121`).
  `runner/stats.py` deliberately handles period counts differently (see its intraday comment at
  `runner/stats.py:236` and annualized forms at `runner/stats.py:494,511`). Two Sharpe definitions
  in one product = numbers that disagree page-to-page.

- **F-c6 · trivial — dead guard.** `comparison.py:116` guards `if combined_vals` but line 88-89
  already returned when empty.

## (b) Security

Nothing found. The module touches no user input, filesystem, or network.

## (c) Baseline smells

- **Duplicated Code (major)** — Sharpe/Sortino/max-drawdown/correlation math re-implemented here
  while `runner/stats.py` already owns canonical implementations (`runner/stats.py:494,511`),
  and drawdown is *also* re-implemented inline twice more (`core.py:927-938`,
  `main.py:_portfolio_stats_from_trades` at `main.py:1732-1766`). Four drawdown implementations.
  Extract one shared metrics module.
- **Primitive Obsession / Data Clumps (minor)** — equity curves travel as `list[list]`
  `[time_str, value]` pairs and alignment/normalization rules live in callers. A tiny
  `EquitySeries` value type (timestamps parsed once) would eliminate F-c3/F-c4 class bugs.
- **Speculative Generality (minor)** — the plural "2+ strategies" surface
  (`comparison.py:139-160`) promises generality the implementation doesn't have (see F-c1);
  either delete the pretense or implement it.

## (d) Maintainability hot spots

The file itself is a pleasant size; the hot spot is **divergence from `runner/stats.py`** — every
fix to Sharpe/Sortino semantics must now be made twice (and today they disagree).

---

# 2. `viewer/__init__.py`

Empty package marker (0 lines). **Scanned — nothing found.** Appropriate as-is.

---

# 3. `viewer/core.py`

Self-described "shared helper logic" imported by `main.py` as `legacy_viewer`. Contains three
layers interleaved in one 4,888-line module: (1) live-used helpers, (2) a complete Flask-era
HTTP route layer that is **dead code**, (3) a background task engine.

## (a) Bugs / correctness risks

- **F-co1 · major — Orphaned statements prove mid-surgery breakage.**
  `core.py:1505-1506`, directly after `get_backtest_ohlcv` returns at `core.py:1502`:
  ```python
      comparison = compare_backtests(strategies)
      return jsonify(comparison)
  ```
  These lines are unreachable and reference an undefined `strategies` (`NameError` if reached).
  They are the remains of a compare endpoint that lost its head during a copy/paste edit — strong
  evidence the file is edited without execution coverage.

- **F-co2 · major — Dolthub pull and Dolthub status scan different directories.**
  `_dolthub_pull` operates on `_DOLTHUB_REPOS = Path.home() / "data" / "dolthub"`
  (`core.py:1772-1795`) while `_scan_dolthub_repos` (used by the data UI) scans
  `DATA_DIR / "dolthub"` (`core.py:4170-4172`). When `DATA_DIR ≠ ~/data/dolthub` the app pulls
  repos it never reports on and reports on repos it never pulls. One of the two roots is wrong;
  `DATA_DIR` is the repo-canonical location (`docs/ARCHITECTURE.md`).

- **F-co3 · major — Stats payload mislabels aggregates as extremes.**
  `core.py:1132-1133`: `"best_trade": raw.get("avg_winner", 0)` and
  `"worst_trade": raw.get("avg_loser", 0)`. The UI field names promise best/worst single trades;
  users get averages. Duplicated verbatim in the live endpoint (`main.py:6982-6983`), so fixing
  one fixes nothing.

- **F-co4 · minor — Race conditions on shared job/task state.**
  `_running_jobs` is a plain module dict written from request threads and worker threads without a
  lock (`core.py:69`, `1835`, `1880-1887`; continued in `main.py:7790`, `8059-8096`). Individual
  dict ops are GIL-atomic but the multi-key update sequences (`status`/`result`/`error`) are not,
  so `/status` readers observe torn transitions. The newer `DATA_TASKS` registry *does* use a lock
  (`core.py:2811-2812`) — the old dict is the leftover.

- **F-co5 · minor — Unbounded in-memory caches (resource leak).**
  `_OHLCV_DF_CACHE` (`core.py:74-78`) grows without eviction for the life of the process; entries
  pin whole OHLCV DataFrames. Entries are removed only when the underlying file disappears
  (`core.py:386-389`). A viewer browsing many symbols/timeframes walks memory upward
  monotonically. Add LRU/TTL bounding.

- **F-co6 · minor — User-controlled symbol interpolated into a glob pattern.**
  `core.py:385`: `ohlcv_root.rglob(f"**/{normalized_symbol}/{normalized_timeframe}.parquet")`.
  A symbol containing `*`, `?`, or `[]` alters the match set (wrong data shown, pathological scans
  on large trees). Escape or validate against `^[A-Z0-9.\-]+$`.

- **F-co7 · minor — Misleading subprocess timeout on the live backtest runner.**
  The Popen stdout-pump loop (`main.py:7978-7999`, launched from the shared code path) only
  reaches `proc.wait(timeout=300)` at `main.py:8019` *after* stdout hits EOF. A chatty child that
  never exits keeps the loop alive indefinitely — the 300 s bound covers the tail, not the run.
  (Listed under core because `_run` mirrors `_start_data_task`'s unbounded pump at
  `core.py:3381-3385`, which has no timeout at all and relies solely on the cancel event.)

- **F-co8 · minor — Inconsistent exit/entry time fallback ordering.**
  `core.py:296` prefers `entry_time` for the anchor point while `core.py:290,303` prefer
  `last_exit_time` for ordering/stepping — synthetic first point can precede/out-of-order the
  first trade point for same-timestamp data.

- **F-co9 · minor — Dead variable in the Dukascopy task loop.**
  `core.py:3522` assigns `used_native_backend = False` which is never read in that function (its
  counterpart at `core.py:3763` *is* read at `core.py:3956`). Copy-paste residue from the JForex
  twin.

- Checked and clean: no mutable default arguments anywhere in the module; no bare `except:` (all
  handlers name exception types); timezone handling is exemplary (`_iso_to_unix` assumes UTC for
  naive stamps, `core.py:252-255`; all `now()` calls tz-aware); SQL access absent here.

## (b) Security

- **F-co10 · critical — `_strategy_path` accepts arbitrary filesystem paths and feeds them to a
  subprocess (arbitrary code execution).**
  `core.py:202-207`:
  ```python
  candidate = (STRATEGIES_DIR / identifier).resolve()
  return candidate if candidate.exists() else None
  ```
  Any *existing* path — including outside the repo — is accepted. This function is **live**:
  `main.py:7752` resolves the user-supplied `strategy` field of `POST /api/backtests/run` through
  it and the result becomes `--strategy <path>` of `python -m runner.cli run`
  (`main.py:7863-7880`). Posting `{"strategy": "/tmp/evil.py", ...}` executes attacker-chosen
  Python in-process with the server's privileges. There is no authentication middleware anywhere
  in `main.py`, CORS is `allow_origins=["*"]` (`main.py:632-638`), and the README instructs binding
  `0.0.0.0`. Fix: require the identifier to resolve *through the strategy registry only*
  (`_strategy_entry`) and drop the filesystem fallback, or verify
  `candidate.is_relative_to(STRATEGIES_DIR.resolve())`.

- **F-co11 · major — Sibling-prefix bypass in the delete-symbol containment check.**
  `core.py:2827-2836` (mirrored live at `main.py:10666-10675`):
  `if not str(p).startswith(str(dd)): 403`. `"/data/evil/x".startswith("/data")` is True — any
  directory whose name merely *begins with* the DATA_DIR string passes. Use
  `p.is_relative_to(dd)` (already used correctly elsewhere, e.g. `main.py:1005`, `main.py:10726`)
  or `os.path.commonpath`. Combined with `unlink()` this permits deletion of files under
  `DATA_DIR*-named siblings`.

- **F-co12 · major — ID-to-path concatenation without validation (dead routes, live twins).**
  `backtest_id`, `optimization_id`, `portfolio_id`, `log_name` are formatted directly into paths:
  `core.py:119-123` (`_load_result`), `126-130` (`_optimization_path`), `1077` (rename write),
  `2304/2315/2333` (portfolio read/delete/compare), `2564` (`LOGS_DIR / log_name`). An id
  containing `/` or `..` escapes the intended directory. The Flask routes carrying most of these
  are dead (see F-co15), but the helpers are exported and reused by `main.py`
  (`_load_optimization_payload` at `main.py:8724`, `_optimization_summary_payload`), and the
  identical pattern is *live* in `main.py` (see F-m3). The repo's own `grid-results` resolver
  (`core.py:2365-2381`) demonstrates the correct pattern (regex whitelist + resolved-parent
  equality) — it just isn't applied anywhere else.

- **F-co13 · minor — Glob-pattern injection** (see F-co6) — information-level exposure only
  (read-only matching), listed here for completeness.

## (c) Baseline smells

- **Dead Code + Speculative Generality (critical as a maintenance finding, F-co15).**
  The entire Flask-compat layer is unreachable: `_NoopRouteApp.route` returns an identity
  decorator (`core.py:102-106`), `app.run()` raises by contract (`core.py:108-109`),
  `request` is a raising placeholder (`core.py:81-112`), and nothing imports `viewer.core.app`
  (verified by grep; tests exercise only helpers). Every `@app.route` handler below it —
  roughly `core.py:1003-2680`, `4201-4330`, `4457-4865`, i.e. **~2,300 lines** — is dead,
  duplicating live FastAPI endpoints in `main.py` path-for-path (`/api/backtests/run`,
  `/api/optimizations/run`, `/api/data/download`, `/api/logs/{name}`, `/api/benchmark/{symbol}`,
  `/api/backtest/{id}/stats`, …). Readers and editors must constantly ask "which one runs?"
  Delete the layer, keep the helpers.

- **Duplicated Code (major).**
  - `_start_dukascopy_task` (`core.py:3421-3649`) vs `_start_jforex_task`
    (`core.py:3652-4019`): ~200 near-identical lines (cancel-before-launch block, resume gate,
    per-combination loop, ✅/❌ logging, traceback writer, catalog refresh, final-status rollup).
    Parameterize the downloader callable; the fallback branch is the only real difference.
  - `_downsample_numeric_minmax` `core.py:961-1000` ≡ `main.py:1670-1718` (verbatim).
  - `_safe_float` `core.py:822-829` ≡ `main.py:1445-1452`.
  - Optimization job body `core.py:1904-2012` ≈ `main.py:8592-8695`.
  - Backtest-stats mapping `core.py:1110-1145` ≈ `main.py:6955-6997` (including the F-co3 bug,
    copied).
  - Benchmark buy&hold block `core.py:2609-2680` ≈ `main.py:10776-10834`.
  - Indicator payload normalization `core.py:1413-1431` ≈ `main.py:7236-7288`.

- **Shotgun Surgery (major).** Adding/renaming one API field requires synchronized edits in
  `viewer/main.py`, `viewer/core.py`, `frontend/src/api/client.ts`, `frontend/src/api/types.ts`
  (per `docs/ARCHITECTURE.md:112-118`), plus — in practice — the dead core twin. The F-co3 bug
  surviving in two places is the smell made flesh.

- **Feature Envy (minor).** The task-lifecycle functions exist to manipulate `DATA_TASKS` dict
  entries (`_update_task_fields`, `_update_task_progress`, `_ingest_task_log_line`,
  `_mark_task_finished`, `core.py:3080-3152`): dozens of `task.get(...)`/`task["..."]` pokes on
  another module's data structure. A small `TaskRecord` class owning its own mutation + snapshot
  logic would collapse them.

- **Primitive Obsession (major).** Task records are `dict[str, Any]` with stringly statuses
  (`"starting"/"running"/"paused"/…`), `threading.Event` objects stored *inside* JSON-bound dicts,
  and ad-hoc key conventions (`restart_config`, `download_request_key`). Statuses are compared as
  bare strings in ≥12 sites across both files. One enum + dataclass would remove a whole class of
  typo bugs.

- **Mysterious Name (minor).** `legacy_viewer` (it's the *helper* layer, not legacy-only — it
  hosts the live task engine), `jsonify` (identity function, `core.py:89-90`), the `_LEGACY_*`
  aliases (`main.py:212-264`), `ALL_LOGS_NAME`. Rename on next touch.

- **Middle Man (minor).** `_RequestPlaceholder.__getattr__`, no-op `jsonify`,
  `send_from_directory` shim (`core.py:81-113`) exist purely to make dead code importable.

## (d) Maintainability hot spots

1. The dead Flask layer (F-co15) — largest single cleanup win in the area.
2. The downloader-task twins (F-Dup) — highest bug-propagation risk in active code.
3. `core.py` as god-module: strategy registry access, OHLCV loading, drawings normalization,
   task engine, dukascopy genesis, catalog scanning — six reasons to change live interleaved
   (mild Divergent Change); splitting task-engine into its own module would decouple it from the
   Flask corpse.

---

# 4. `viewer/main.py`

The real FastAPI app: 10,913 lines, ~60 endpoints, WebSockets, background-job orchestration,
portfolios, live-trading dashboards, data management.

## (a) Bugs / correctness risks

- **F-m1 · major — `asyncio.run()` inside code reachable from the running loop.**
  `main.py:1148` (`_restart_interrupted_task`) and `main.py:3381`
  (`_discover_live_accounts_payload`), also `main.py:410` (`_resume_interrupted_tasks`). All
  current call chains happen to run in worker threads via `asyncio.to_thread(...)`
  (`main.py:10569`, `main.py:9713`, lifespan startup), where a fresh loop is legal. Any future
  caller that invokes these from async context gets
  `RuntimeError: asyncio.run() cannot be called from a running event loop`. Worse,
  `asyncio.run(run_backtest(...))` spins a brand-new loop per restart — fire-and-forget tasks
  created inside (none today, by luck) would die with the loop. Replace with a thread-based
  invocation or make these endpoints native-async.

- **F-m2 · major — Import-time monkey-patching of the legacy module.**
  `main.py:3195-3202` rebinds eight private functions on the already-imported `viewer.core`
  (`legacy_viewer._create_task_record = _create_task_record`, etc.) so that core's task engine
  gains DB persistence. Consequences: behavior differs depending on whether `viewer.main` was
  imported; `viewer.core` used standalone (tools, notebooks, some tests) silently skips
  persistence; the wrapper chain (`main._update_task_fields` → `core._update_task_fields` →
  `_persist_task_snapshot` → `upsert_background_task_record`) is invisible to static analysis.
  Inject the persistence hook explicitly (core already accepts callbacks elsewhere).

- **F-m3 · major — Bulk-delete endpoints concatenate raw IDs into unlink paths.**
  `_delete_backtest_with_artifacts` (`main.py:2524-2539`, `BACKTESTS_DIR / f"{normalized_id}.trades.json"`
  at `main.py:2531`) and `_delete_portfolio_with_artifacts` (`main.py:2019-2042`, path at
  `main.py:2030`) are fed by `POST /api/backtests/delete-bulk` (`main.py:6818-6829`),
  `POST /api/portfolios/delete-bulk` (`main.py:9082-9093`), `DELETE /api/portfolios/{id}`
  (`main.py:9159-9163`), and the dedupe/removal endpoints. Body-supplied ids are **not** URL-
  constrained, so `{"ids": ["../../../../tmp/victim"]}` deletes `…/victim.trades.json` /
  `…/victim.json` anywhere the process can write. Same class as F-co12; fix with the grid-results
  pattern (`core.py:2365-2381`). Rename endpoints that *write* (`main.py:6923`, `9119-9120`) are
  constrained by URL routing to slash-free segments, hence lower risk.

- **F-m4 · major — Hardcoded vendor coverage windows are already stale.**
  `main.py:5931-5933`: `HF_INTRADAY_COVERAGE_END = "2025-07-31"`,
  `ALPACA_INTRADAY_START = "2025-07-01"`. `_auto_download_source_plan` (`main.py:5992-6054`) uses
  them to split ranges between HuggingFace and Alpaca; for any range extending past 2025-07-31
  (i.e., everything today) the HF leg is dropped silently and intraday equities fall entirely to
  Alpaca — or, without Alpaca creds, auto-download degrades to yfinance daily-grade data with only
  a log line. Move to configuration with an explicit "coverage unknown" behavior.

- **F-m5 · major — N+1 database access in hot paths.**
  - `_live_accounts_stream_payload` (`main.py:3449-3469`) runs `get_latest_live_balance(account_id)`
    per account, and this payload is refetched **every 1 second** per open WebSocket
    (`_LIVE_WS_ACCOUNTS_POLL_SECONDS = 1.0`, `main.py:1255`; broker loop `main.py:1356-1390`).
    Accounts × 1 Hz × SQLite round-trips on the event-loop's thread pool.
  - `GET /api/live/dashboard` (`main.py:9579-9643`) serially awaits `_live_account_stats_payload`
    (full balances+executions scan) *and* `_live_trades_payload` per account — quadratic in
    accounts, all sequential (`main.py:9594-9608`). Batch the queries or gather concurrently.

- **F-m6 · major — Divergent duplicate of the agent-conversion endpoint.**
  Live `agent_convert` (`main.py:10288-10333`) validates providers against
  `AGENT_CONVERT_PROVIDER_NAMES = ("nim", "copilot_sdk")` (`main.py:164`), while the dead core twin
  (`core.py:4611-4626`) validates against `SUPPORTED_LLM_PROVIDER_NAMES`. Same route, two different
  contracts; anyone grepping core to answer "which providers work?" gets the wrong answer.

- **F-m7 · minor — 8-character task IDs.** `str(uuid.uuid4())[:8]` at `main.py:7768`, `8238`,
  `8602` (32 bits). Collisions overwrite `DATA_TASKS`/`_running_jobs` entries and cross-wire status
  polling between unrelated jobs. The data-task endpoints already use full UUIDs
  (`main.py:10259`); unify.

- **F-m8 · minor — Shared task-dict mutation during serialization.**
  `_snapshot_task` returns a *shallow* copy (`main.py:2858-2860`); nested structures
  (`progress_detail`, `progress_history`) are then mutated by `_ensure_task_runtime_metadata`
  (`main.py:1522-1534`) while a worker thread may be appending. Symptom would be occasional
  doubled/lost history samples; benign but real. Deep-copy in the snapshot or move history
  appends under the lock.

- **F-m9 · minor — `_safe_float(value, value)` default trick can leak non-numbers.**
  `main.py:2408-2422` (`maybe_fraction`/`maybe_win_rate`): on unparsable input the function returns
  the *original object* (e.g. a string), which later flows into `<` comparisons in `in_range`
  (`main.py:2401-2406`) and would 500. Currently shielded by pydantic-typed query params; fragile
  to reuse.

- **F-m10 · minor — `_normalize_win_rate` heuristics misclassify edge values.**
  `main.py:2233-2243`: any value in `(1.0, 100]` is divided by 100, so a legitimately-stored
  `win_rate = 1.5` (%-scale, 1.5%) silently becomes 0.015 (0.15%). Acceptable given the dual-scale
  legacy data, but worth a comment; it's a guess, not a spec.

- **F-m11 · minor — Blocking file I/O on the event loop in the WS log tailer.**
  `task_log_socket` (`main.py:10877-10913`) opens/reads/seeks the log synchronously every 0.5 s
  inside the async handler. Small chunks, but under many simultaneous WS viewers this serializes on
  the loop; `asyncio.to_thread` or a watcher thread would isolate it.

- **F-m12 · trivial — `_seed_default_live_accounts` is an empty stub** (`main.py:3519-3520`);
  delete or implement.

- Checked and clean: no mutable-default pitfalls in practice — the `= {}` / `= []` class attributes
  on SQLModel request models (`main.py:698`, `743`, `759`, `808-809`) are deep-copied per-instance
  by pydantic, so the classic bug cannot manifest (noted, not counted); no bare except; datetimes
  consistently UTC (`_parse_iso_datetime_cached` `main.py:1455-1471` assumes UTC for naive input);
  WebSocket broadcast queue overflow drops oldest rather than blocking (`main.py:3225-3232` — nice);
  lifespan shutdown cancels and awaits all background tasks (`main.py:596-628`); SPA static
  middleware uses a correct `is_relative_to` containment check (`main.py:1004-1005`).

## (b) Security

- **F-m13 · major — CORS `allow_origins=["*"]` together with `allow_credentials=True`.**
  `main.py:632-638`. Per the Fetch/CORS spec the combination is incoherent (browsers refuse
  credentialed requests when the header is a literal `*`), so today it mainly produces confusing
  failures; but it signals intent to open the API to any origin, and there is **no authentication
  or authorization anywhere** in the module — every mutating endpoint (delete backtests/portfolios/
  symbols/files, start subprocesses, create live deployments, execute the LLM agent) is callable by
  any same-network host per the README's `--host 0.0.0.0` deployment. At minimum: drop
  `allow_credentials`, restrict origins to the Vite dev origin, and document the
  trusted-LAN assumption prominently.

- **F-m14 · major — Arbitrary local file read via CSV signal filepath.**
  `run_backtest_from_signals` (`main.py:8458-8524`) loads whatever path the client supplies:
  `load_signals_from_csv(file_path)` at `main.py:8485`, echoing parse errors back to the caller
  (`main.py:8487-8489`) and embedding the expanded path into persisted metadata
  (`main.py:8519`). Restrict to an uploads/import directory and reject escapes.

- **F-m15 · major — Sibling-prefix bypass in `DELETE /api/data/symbol`** — the live twin of
  F-co11 (`main.py:10666-10675`), permitting deletion under any `DATA_DIR*`-prefixed sibling
  directory. Same fix (`is_relative_to`).

- **F-m16 · major — Path-traversal deletion via bulk IDs** — see F-m3 (counted once there; the
  security substance is the same finding).

- Positive controls worth noting (so they aren't "fixed" away): log reader enforces
  `is_relative_to(LOG_DIR)` (`main.py:10724-10729`); matrix filename guard rejects `..`/leading `/`
  (`main.py:6521-6525`); static middleware containment (`main.py:1004-1005`); grid-results
  whitelist lives in core (`core.py:2365-2381`). The pattern clearly exists in-repo — it's just
  inconsistently applied (Shotgun Surgery in security controls).

## (c) Baseline smells

- **Divergent Change (critical-as-smell).** One 10,913-line module changes for at least ten
  unrelated reasons: backtest listing/detail, strategy registry, portfolio construction/analytics,
  signal replay, live-trading dashboard + deployments, market-data streaming, data downloads,
  background-task framework, log viewing, services/verification, CAPM analytics, matrix results.
  Any of these teams must re-read the whole file and risk the import-order-sensitive monkey-patch
  block (F-m2). Split per reason (routers + services packages); FastAPI's `APIRouter` exists for
  exactly this.

- **Duplicated Code (major).**
  - vs `core.py`: optimization job (`main.py:8592-8695` ≈ `core.py:1904-2012`), backtest stats
    mapping (`main.py:6955-6997` ≈ `core.py:1110-1145`), benchmark resampling
    (`main.py:10776-10834` ≈ `core.py:2609-2680`), `_downsample_numeric_minmax`
    (`main.py:1670-1718` ≡ `core.py:961-1000`), `_safe_float` (`main.py:1445-1452` ≡
    `core.py:822-829`), indicator normalization (`main.py:7236-7288` ≈ `core.py:1413-1431`),
    dukascopy genesis endpoint (`main.py:10336-10352` ≡ `core.py:4654-4670`).
  - internal: `_resolve_component_backtest_id` (`main.py:3996-4099`) vs
    `_resolve_strategy_backtest_id` (`main.py:4102-4169`) — ~70 lines differing only in
    symbol/timeframe filtering; `_group_duplicate_backtests` vs `_group_duplicate_portfolios`
    (`main.py:2472-2521` vs `1967-2016`); pause/resume/cancel endpoint triples
    (`main.py:10572-10630`) repeat the same guard sequence; the stage-reporting closures in
    `run_backtest` and `run_backtest_signals` (`main.py:7902-7930` vs `8286-8311`).
  - the ~60-line legacy alias block (`main.py:205-264`) re-exports core privates one by one — a
    Middle Man layer that must be updated on every core change.

- **Long Function (major).** `run_backtest` `main.py:7736-8103` (~370 lines: request shaping,
    task bookkeeping, subprocess orchestration, output parsing, state machine); 
    `create_portfolio` `main.py:8833-9068` (~235); `_normalize_portfolio_payload`
    `main.py:4851-5086` (~235); `run_backtest_signals` `main.py:8106-8455` (~350);
    `_compute_portfolio_contributions` `main.py:4538-4776` (~240).

- **Primitive Obsession (major).** Same dict-and-string disease as core: `dict[str, Any]`
  payloads throughout, stringly statuses, hand-rolled fingerprint strings
  (`main.py:2341-2350`, `1948-1964`), magic weight sentinel `0.000001`
  (`main.py:4679`). Partial credit: `_PreparedDownloadRequest` (`main.py:10031-10045`) shows the
  team knows the cure — extend it.

- **Repeated Switches (minor).** Source-dispatch if-chains recur: `_download_missing_ohlcv_data`
  (`main.py:6057-6161`, five-source ladder), `_generic_download_command` (`main.py:10196-10221`),
  `_task_cache_invalidation_reason` (`core.py:4388-4403`), `_download_task_labels`
  (`core.py:4350-4378`). A source → {module, labels, invalidation} map would collapse four sites.

- **Message Chains (minor).** `payload.get("combined_stats") or {}` → `.get(...)` ladders and
  `result.meta.data.get("timeframes", ["1h"])[0] if result.meta.data.get("timeframes") else "1h"`
  repeated at `main.py:7474-7476`, `7546-7548`, `7624-7627`, `7698-7704` — hide behind one
  `primary_timeframe(result)` helper (one exists in spirit in `_backtest_info_payload`).

- **Speculative Generality (minor).** `_seed_default_live_accounts` no-op (`main.py:3519-3520`);
  the `include_validation: bool | None` pass-through (`main.py:10020-10028`); unused
  `_QUIET_EXACT_PATHS` member overlap with prefixes is fine, but `_UVICORN_ACCESS_PATH_RE`
  (`main.py:924`) parses uvicorn's human log format even though the access logger is disabled
  five lines later (`main.py:950-956`) — one of the two mechanisms is dead.

- **Mysterious Name (minor).** `AGENT_CONVERT_PROVIDER_NAMES` implies conversion-specific providers
  but gates the only conversion endpoint; `legacy_risk_per_trade_pct` fields that are actually
  *weights* (`main.py:4486`); `_cached_backtest_equity_preview_only` vs
  `_cached_backtest_equity_preview` differ in fallback semantics — names don't say so.

## (d) Maintainability hot spots

1. Module size/divergence (see smell above) — the defining problem of the area.
2. The core↔main monkey-patch bridge (`main.py:3195-3202` + alias block `205-264`) — most likely
   place for the next "works in dev, not in prod" incident.
3. Task lifecycle spread over both files (create/update/persist/hydrate/recover/restart in
   `main.py:2788-3202` operating on core-owned dicts).
4. Portfolio normalization pipeline (`_normalize_portfolio_payload` + enrichment + contributions)
   — high cyclomatic density, deeply nested fallbacks, no unit-testable seams.

---

# Cross-cutting observations

1. **The area's central defect is the unresolved core/main split.** Dead Flask routes (~2,300
   lines), verbatim-duplicated helpers, divergent duplicates with behavioral differences
   (providers, stats labels), and an import-order-dependent patch layer all trace to it. A
   single deliberate migration (helpers into a service module, delete the route layer, kill the
   aliases) would remove the majority of findings in this report.
2. **Security controls are applied inconsistently** — three correct containment implementations
   (`is_relative_to` in static middleware, logs, grid-results) coexist with two vulnerable
   patterns (`startswith` prefix checks; raw id→path concatenation). Standardize on one
   `safe_resolve(base, name)` helper.
3. **Checklist sweep (Python items):** mutable default args — none harmful (pydantic shields);
   bare except — none; race conditions on shared state — present but minor (`_running_jobs`,
   shallow snapshots, F-co4/F-m8); SQL string building — none in area (parameterized via
   `shared.database` calls); subprocess timeouts — present on `subprocess.run` calls
   (`core.py:1792`, `1874`) but ineffective/misleading on the long-running Popen pumps
   (F-co7); tz-naive datetimes — none found, UTC discipline is good; resource leaks — unbounded
   DF cache (F-co5) and unbounded in-memory task/log accumulation are the two real ones.

# Finding counts (by file)

| File | Critical | Major | Minor | Trivial |
|---|---|---|---|---|
| `viewer/__init__.py` | 0 | 0 | 0 | 0 |
| `viewer/comparison.py` | 0 | 2 | 4 | 1 |
| `viewer/core.py` | 1 | 5 | 7 | 0 |
| `viewer/main.py` | 0 | 9 | 7 | 1 |

**Totals: 1 critical · 16 major · 18 minor · 2 trivial — 37 findings.**
(Security: 6 substantive findings — 1 critical RCE-enabling, 5 major. Smells named per Fowler
baseline with the repo-overrides rule applied; nothing tooling-enforced was counted.)
