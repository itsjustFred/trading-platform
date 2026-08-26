# Code Review — `misc-backend` (reviewer r3, blind & independent)

Area: risk engine, scheduler, operational scripts, shared infrastructure, signal bots, trading strategies + root-level `pyproject.toml`, `pytest.ini`, `live_deployments.yaml`.

Findings cite `file:line` with severity critical/major/minor. Baseline smells use Fowler (_Refactoring_ ch.3) names; repo-documented standards suppress a smell it endorses.

Status legend per file: ✅ findings · ◻ scanned clean.

---

## Report body (appended incrementally below)

---

# Group 1 — `shared/` infrastructure

## shared/database.py ✅

3,891-line god-module: 15 SQLModel table classes + ~60 free functions spanning backtests, portfolios, trades, equity points, drawings, indicators, grid results, live signals/executions/balances/accounts/deployments, services, background tasks and verification evidence.

**(a) Bugs / correctness**

1. **major** — `database.py:70-77`: module-level `ENGINE = create_engine(...)` runs at import time and `DB_PATH` depends on `DATA_DIR`, which itself depends on env vars resolved at import time (`constants.py:141`). Any code that sets `DATA_DIR`/`TRADING_DATA_DIR` after importing this module (e.g. tests, tools) silently targets the wrong database; there is no way to re-point the engine. Import-time side effects + global singleton make multi-env use impossible without process isolation.
2. **major** — `database.py:1277-1282` (`_migrate_live_executions_dedupe_key`): duplicated statement `execution_column_names = _table_column_names(connection, "live_executions")` appears twice back-to-back (lines 1280 and 1282) — copy/paste residue; harmless functionally but shows the migration was never re-read after editing.
3. **minor** — `database.py:1262`: `Path(str(bt_id)).name == str(bt_id)` is an obfuscated way of checking "id has no path separators", evaluated *after* already constructing paths from `bt_id` in `_artifact_equity_curve` (line 1225). A backtest id containing `/` would have been used in a path join before this guard in other call sites (`_backfill_curve_for_id`, line 1082, has **no such guard**). Path traversal from DB ids is only defended in one of two callers — inconsistent.
4. **minor** — `database.py:2297` (`load_live_account_stats_inputs`): `deployment_ids = {normalized_account_id}` seeds the deployment-id set with the *account* id, then adds strategy names (line 2302). If a real deployment id ever equals another account's id or a strategy name collides with a deployment id, executions are attributed to the wrong deployment. Deliberate legacy-compat hack, undocumented why strategy names belong in a deployment-id filter.
5. **minor** — `database.py:2252`: slippage sign convention: `slippage = actual - theoretical` is unsigned relative to side; a buy filled above and a sell filled below both show positive slippage, but a sell filled above (favorable) also shows positive — consumers must know action to interpret. Field name doesn't convey directionality.
6. **minor** — `database.py:3696` (`save_portfolio`): components enumerate `start=1`, but `load_portfolio` (line 3735) enumerates `start=1` over the *reloaded* rows ordered by autoincrement `id`; if a component insert failed partially historically (or rows were reordered), positional pairing of `component_id` back onto components can attach the wrong persisted component id. Positional coupling instead of storing `component_id` on the model round-trip.
7. **minor** — `database.py:841` (`_table_column_names`) and `database.py:939`: f-string interpolation into SQL text (`PRAGMA table_info('{table_name}')`). All current call sites pass literal strings, so it is not exploitable today, but the helper invites injection the first time someone passes user input; no assertion/guard that `table_name` matches `^[A-Za-z_]+$`.
8. **minor** — `database.py:324-326` `_utcnow_naive` stores naive UTC while `_now_iso()` (line 693) stores tz-aware ISO strings in other tables — two timestamp conventions in one module; comparisons across tables require knowing which is which (the checklist item "tz-naive datetimes" applies; mitigated by docstring).
9. **minor** — `database.py:2862-2905` (`list_verification_evidence_summary`): fetches `limit*20` records then groups in Python; with `limit=2000` that's 40k rows deserialized through Pydantic per summary request — O(N) memory for what is a GROUP BY.
10. **minor** — `database.py:2145-2149` (`list_live_signals_for_deployments`): loads *all* executions for up-to-1000 signals with no LIMIT on the execution query; a chatty signal fan-out makes this query unbounded.

**(b) Security**

11. **minor** — `database.py:61`: `load_dotenv(PROJECT_ROOT / ".env")` executed at import time in a shared library; combined with `constants.py:36` and `sentry.py:18` this happens three times per process. Not a leak, but any `.env` shadowing (e.g. stale local overrides) silently changes production behavior depending on import order.

**(c) Baseline smells**

12. **major — Divergent Change / God Module**: `database.py` (3,891 lines) changes for every unrelated reason: schema migrations, live-trading reconciliation, verification evidence, portfolio math, grid results. Fowler's remedy: split per reason (e.g. `storage/backtests.py`, `storage/live.py`, `storage/evidence.py`).
13. **major — Duplicated Code**: the row-unwrapping dance (`hasattr(row, "_mapping")` → values[0] → tuple[0]) is reimplemented five times: `_normalize_task_record` (805), `_coerce_backtest_row_id` (2475), `_extract_backtest_and_trade_count` (2576), `_unwrap_equity_point_row` (3270), `_unwrap_model_row` (3481), plus inline again at 2639-2645. Extract one generic `unwrap_row()` helper.
14. **major — Repeated Switches / Shotgun Surgery**: signal-source normalization logic (`internal`/`discord` juggling) exists three times: `LiveDeploymentModel.from_live_deployment` (591-599), `to_live_deployment` (624-641), and `_resolve_deployment_signal_source` (1906-1923) — three subtly different implementations of the same rule. A single normalization function should be the one source of truth.
15. **minor — Duplicated Code**: `save_result_to_sqlite` delete-children block (2323-2326) duplicates `delete_backtest`'s (3261-3264); extract `_delete_backtest_children(session, id)`.
16. **minor — Data Clumps**: `(task_id, task_type, status, config_json, progress_message, name, description, source, created_at, updated_at)` is threaded through `_record_verification_evidence_for_task`, `_build_background_task_record`, `_update_background_task_record`, `_record_task_completion_evidence` as ten parallel kwargs (2922-3134). Bundle into a `TaskSnapshot` dataclass.
17. **minor — Primitive Obsession**: statuses/types/categories are raw strings validated ad hoc (`_normalize_service_status`, `_normalize_service_type`, `_FINAL_TASK_STATUSES`); the enums exist in `shared/models.py` but the task-status domain never got one.

**(d) Maintainability hot spots**

18. The hand-rolled migration framework (`_migrate_schema` + 10 `_add_*`/`_migrate_*` helpers, lines 845-1307) re-implements Alembic inside one function chain; every new column means touching `create_all` models *and* a migration helper *and* sometimes the model's `from_*` normalizer — classic Shotgun Surgery. Recommend adopting Alembic or at least a declarative column-diff loop.

---

## shared/models.py ✅

**(a) Bugs / correctness**

1. **minor** — `models.py:425-429`: `Trade.direction_sign` returns `-1` for anything not `"LONG"` — including typos like `"long "` normalized away upstream or garbage data; silent wrong-sign math downstream rather than validation. Direction validity (`VALID_DIRECTIONS`) is never enforced in the model.
2. **minor** — `models.py:104`: `Service.created_at` defaults to naive UTC (`replace(tzinfo=None)`) — same tz-naive convention issue as database.py; deliberate but inconsistent with `LiveSignal.timestamp` which stays aware.
3. **minor** — `models.py:748` (`BacktestResult.filename`): strategy names containing `/` or spaces flow straight into filenames; `save()` will create nested dirs unexpectedly (`mkdir(parents=True)` at 727). Unvalidated id/name → path construction.

**(b) Security** — nothing found (pure dataclasses/pydantic, no I/O beyond file save).

**(c) Baseline smells**

4. **minor — Duplicated Code**: `StrategyMeta.to_dict/from_dict` (52-73), `TradeExit` (302-324), `BacktestMeta` (491-534) all hand-roll identical omit-empty serialization loops; `BacktestStats.to_dict/from_dict` and `PortfolioStats.to_dict/from_dict` (592-672) are near-identical twins that differ only in field names — extract a shared mixin or use pydantic for all.
5. **minor — Divergent Change**: `models.py` mixes four domains (strategy metadata, live-trading service records, backtest result format, portfolio/stats). Each evolves independently; split per reason.
6. **minor — Primitive Obsession**: `direction: str`, `type: str` ("overlay"/"panel"), `engine: str` are stringly-typed despite `Literal`/Enum availability elsewhere in the same file.

**(d) Maintainability hot spots** — none beyond above; the dual pydantic/dataclass split (pydantic for API shapes, dataclasses for backtest format) is undocumented and trips newcomers.

---

## shared/constants.py ✅

**(a) Bugs / correctness**

1. **major** — `constants.py:106-138` (`_resolve_data_dir`): when `DATA_DIR` is configured with `~`, the resolver *scores candidate home directories by which contains more runtime data* (`ohlcv` +100 etc.) and picks the highest. Two consequences: (i) nondeterminism — pointing DATA_DIR at an empty intended dir silently redirects to whatever legacy home has old data; (ii) surprise writes — new data lands in a different tree than the operator configured. Config-explicitness should beat scoring.
2. **minor** — `constants.py:74`: `pwd.getpwuid(PROJECT_ROOT.stat().st_uid)` — resolving homes by *file ownership* of the checkout; running the repo from a container where the mount is root-owned picks `/root` as a data-dir candidate. Fragile heuristic stack.
3. **minor** — `constants.py:14-15`: `VALID_TIMEFRAMES`/`VALID_DIRECTIONS` are defined identically in `shared/models.py:283-284` — single-source violation waiting to drift.

**(b) Security** — nothing found.

**(c) Baseline smells**

4. **minor — Speculative Generality**: `_home_candidates` (50-88) builds an ordered candidate list from HOME, pwd, project-owner uid, SUDO_USER and USER envs — five fallbacks for choosing a directory; complexity without a documented scenario needing it.
5. **minor — Mysterious Name**: `_data_dir_score` weights (100/30/20/10) are magic numbers with no explanation of tuning intent.

**(d) Maintainability hot spots**: import-order sensitivity — `load_dotenv` at line 36 must run before `os.environ.get` at 39; any reorder breaks config silently (no test guards it).

---

## shared/logging.py ✅

**(a) Bugs / correctness**

1. **minor** — `logging.py:27`: `LOG_LEVEL` is captured at import time; changing `os.environ["LOG_LEVEL"]` later (common in tests/tools) has no effect. Also `getattr(logging, LOG_LEVEL, logging.INFO)` maps *any* typo ("INF O") silently to INFO instead of erroring.
2. **minor** — `logging.py:33,74-75`: `_loggers` dict accessed from multiple threads without a lock; two threads calling `get_logger("x")` simultaneously can each construct handlers and double-add them → duplicate log lines. Race condition on shared state (checklist item). CPython GIL makes corruption impossible but duplication real.
3. **minor** — `logging.py:44-46`: timestamps formatted with seconds resolution and no milliseconds — correlating logs with sub-second events (order placement!) loses ordering; also drops the `%f` precision logging usually relies on.

**(b) Security**

4. **minor** — `logging.py:58-60`: arbitrary `extra={"data": ...}` payloads are JSON-dumped verbatim with `default=str` — no redaction hook. Contrast with sentry.py which carefully filters secrets; secrets logged via `logger.info(..., extra={"data": {...}})` land in plaintext in `logs/*.log`.

**(c) Baseline smells**

5. **minor — Middle Man**: module docstring says default log dir is `~/logs` (line 13) but implementation uses repo `./logs` via constants — stale doc lying about behavior.
6. **minor — Duplicated Code**: handler construction would be simpler via `logging.config.dictConfig`; manual addHandler without clearing existing handlers means re-import under different names can stack duplicates if `_loggers` were ever cleared.

**(d) Maintainability hot spots**: none significant.

---

## shared/sentry.py ✅

Well-built module overall: validated config dataclass, redaction hooks, bounded subprocess timeout.

**(a) Bugs / correctness**

1. **minor** — `sentry.py:139-140`: `initialize_sentry` returns True immediately if `_INITIALIZED` even when the first init *failed* mid-way (exception between `sentry_sdk.init` and flag set leaves flag False — actually safe), but a second call with a *different* service name silently keeps the first service tag; `_INITIALIZED_SERVICE` is recorded yet never compared — dead field (only test reset uses it).
2. **minor** — `sentry.py:222`: `_SENSITIVE_ASSIGNMENT_RE` value class `[^,\s;&]+` stops at `,`/space/`&` — tokens embedded in base64url with padding are caught, but values quoted in JSON (`"token": "abc def"`) with spaces inside won't match; best-effort redaction, acceptable, worth a comment.

**(b) Security**

3. **minor** — `sentry.py:23-35`: sensitive-key markers include the very common substring `"secret"`/`"key"` — good — but `account_id`, `broker_login`, `ctid` (real credential-adjacent identifiers used elsewhere in this repo) are NOT redacted and will ship to Sentry in event contexts/tags.
4. **minor** — `sentry.py:107-111`: `send_default_pii` defaults False (good) but is env-toggleable with no guard preventing true in production environments — rely on operator discipline.

**(c) Baseline smells**

5. **minor — Speculative Generality**: `capture_exception` wrapper (177-191) merely adds a scope tag; most services could call `sentry_sdk.capture_exception` directly — the indirection earns its keep only via context param.
6. **minor — Duplicated Code**: bool parsing (`{"1","true","yes","on"}`) duplicates the same set in `risk/models.py:_parse_bool` — third copy of truthy-string parsing in the area (also database.py patterns). Extract `shared/parsing.py`.

**(d) Maintainability hot spots**: none.

---

## shared/strategy_registry.py ✅

**(a) Bugs / correctness**

1. **major** — `strategy_registry.py:100-107` (`load_strategy_module`): `exec_module` executes arbitrary strategy Python *without any isolation*, and modules are loaded via `spec_from_file_location` **not inserted into `sys.modules`** — so dataclasses/classes defined with `from __future__ annotations` or pickling/cross-module isinstance checks break subtly; worse, repeated calls (via `iter_strategy_entries` → `load_strategy_meta` per file, then `detect_strategy_type` loads the *same file again*) execute strategy files multiple times per listing — side-effectful strategy files (logging, network) fire repeatedly.
2. **minor** — `strategy_registry.py:176-192` (`iter_strategy_entries`): one broken strategy file raising during import aborts the whole listing — no per-file try/except; one syntax error in any strategy takes down the registry/UI list.
3. **minor** — `strategy_registry.py:239-260` (`update_strategy_display_name`): AST-based source rewrite splices `repr(new_name)` into source; `repr` of a string containing non-ASCII produces escaped form (py3 repr keeps unicode, ok) but a name containing newline characters writes invalid single-line string literal → corrupted strategy file written with **no backup**.
4. **minor** — `strategy_registry.py:56`: discovery requires `path.parent.name == path.stem` — strategies not following the folder-name convention are silently invisible; undocumented convention enforced implicitly.

**(b) Security**

5. **major** — same as (a)1: registry-driven listing imports every strategy file found under `strategies/`; anything dropped into that directory (or a symlinked path) gets arbitrary code execution in whichever process lists strategies (API server included). At minimum document the trust boundary; ideally AST-parse meta instead of executing.

**(c) Baseline smells**

6. **minor — Feature Envy**: `StrategyEntry.engine`/`display_name` reach into `meta` dict repeatedly; fine, but `_strategy_type_from_meta` (118-144) rummages through four meta spellings (`strategy_type`, `type`, tags…) — normalize once at load.
7. **minor — Repeated Switches**: identifier matching normalizes via whitespace-collapsing (`_identifier_key`); alias set built ad hoc — adding a new alias form means editing `strategy_identifier_aliases` (Repeated Switches-ish map growth).

**(d) Maintainability hot spots**: `find_strategy_entry` is O(N·files) with import per entry — listing 100 strategies imports 200+ modules per lookup.

---

## shared/__init__.py ◻ scanned — empty file, nothing found.

---

# Group 2 — `risk/`

## risk/engine.py ✅

**(a) Bugs / correctness**

1. **major** — `engine.py:151-153` + `216`: exposure accounting treats `max_total_exposure` as a fraction of *balance* and caps per-trade risk at the remainder, but there is no aggregation across concurrent calls: `current_exposure` is caller-supplied via signal metadata (`current_positions` likewise, line 127). The "risk engine" trusts self-reported state — two simultaneous signals sized with the same metadata both pass. In-process race + cross-process trust gap; acceptable only if a single serialized caller owns it (documented nowhere).
2. **major** — `engine.py:217`: `position_size = risk_amount / (risk_distance * pip)` — units conflation: `pip_value` defaults to 1.0 in `live.py:113` for *any* asset. For equities/crypto where "pip" is meaningless, size = risk_amount/risk_distance (correct for per-unit contracts), but for FX pairs quoted with pip_value≠1 passed wrongly, positions scale incorrectly. The parameter name forces FX semantics onto all assets with no unit documentation.
3. **minor** — `engine.py:183`: `calculate_position_size` mutates the input `signal.stop_loss` and metadata as a side effect of sizing — surprising action-at-a-distance; callers reusing the signal get the fallback stop silently baked in (partially mitigated by `resolved_stop_loss` metadata).
4. **minor** — `engine.py:127`: `int(float(signal.metadata.get("current_positions", 0) or 0))` — `float→int` truncation accepts 4.9 positions as 4; lenient coercion of a safety-critical counter.
5. **minor** — `engine.py:262-264` (`apply_trailing_stop`): `percent >= 1` raises, but `percent` between e.g. 0.9 gives a 90% trail — legal yet almost certainly a misconfigured basis-points-as-fraction; no upper sanity bound/warning.
6. **minor** — `engine.py:52-59` (`_signal_id`): legacy composite id formats floats with `.12g` — two distinct prices differing past 12 significant digits collide; negligible for prices, but id-stability across serialization boundaries (JSON round-trip float repr) is assumed, not guaranteed.

**(b) Security** — nothing found (no I/O).

**(c) Baseline smells**

7. **minor — Long Parameter List / Data Clump**: `calculate_position_size(signal, account_balance, pip_value, current_exposure)` plus `current_positions` smuggled inside `signal.metadata` — position-state is half a parameter, half metadata. An `AccountState(account_balance, current_exposure, current_positions)` type would remove the clump.
8. **minor — Duplicated Code**: the reject-with-zero-sized-order block is repeated four times nearly verbatim (131-149, 154-172, 188-210, final accept 221-242); extract `_rejected(signal, reason, **meta)` builder.
9. **minor — Primitive Obsession**: `action: Literal["buy","sell","long","short"]` with `in {"buy","long"}` checks scattered (27, 43, 268) — a `Side` enum with `.is_long` would centralize.

**(d) Maintainability hot spots**: `calculate_position_size` is 110 lines mixing gating, stop resolution, sizing and decision assembly — decompose into gate → resolve stop → size steps.

---

## risk/config.py ✅

Clean small module. Validation in `__post_init__` is thorough.
**(a)** Nothing found. **(b)** Nothing found.
**(c) smells**
1. **minor — Speculative Generality**: `__post_init__` coerces types (`float(self.max_risk_per_trade)`) — accepting strings for numeric fields papers over caller bugs instead of rejecting them; with `slots=True` dataclasses the coercion also breaks frozen-style hashability expectations. Judgment call; arguably convenience.
**(d)** None.

---

## risk/models.py ✅

**(a) Bugs / correctness**

1. **minor** — `models.py:30-32` (`_parse_bool`): accepts int 0/1 but rejects numpy bools / other ints (2 raises) — inconsistent tri-state parsing duplicated from sentry.py with a *different accepted set* ({true,1,yes} here vs {1,true,yes,on} in sentry.py) — same concept, divergent behavior.
2. **minor** — `models.py:53-57`: `stop_loss=float(self.stop_loss)` accepts 0.0/negative stops (no `> 0` check unlike `entry_price`) — a typo'd `stop_loss=0` passes validation and is only rescued downstream by direction checks when `enforce_stop_direction` is set (off by default in direct engine use).

**(b) Security** — nothing found.

**(c) Baseline smells**

3. **minor — Refused Bequest (borderline)/Data Clump**: `RiskDecision` carries seven loosely-coupled optional floats; several combinations are nonsensical (accepted=False with nonzero quantity) but unconstrained — invariants live in the engine, not the model.
**(d)** None.

---

## risk/live.py ✅

**(a) Bugs / correctness**

1. **major** — `live.py:87-95`: `RiskConfig` is rebuilt from **caller-supplied metadata** on every sizing call; metadata keys like `max_risk_per_trade=0` or negative raise `ValueError` from `RiskConfig.__post_init__` (config.py:21-22) and crash sizing entirely — a hostile/corrupt Discord signal payload can take down the live sizing path (unhandled exception propagates out of `size_live_signal`). Values within (0,1] but absurd (e.g. 0.9 risk/trade) sail through — no clamping to platform limits.
2. **minor** — `live.py:45-47`: explicit qty falls back `explicit_qty` → `qty`; then line 78 writes BOTH keys back with the same value — the distinction between "operator-specified" and "engine-computed" is destroyed after first sizing, making re-sizing decisions ambiguous on retry.
3. **minor** — `live.py:64-66`: `pending_positions` is parsed and passed into TradeSignal metadata but never consumed by `RiskEngine.calculate_position_size` — dead data flow suggesting an unfinished feature (Speculative Generality).

**(b) Security** — covered by (a)1 (untrusted metadata reaching risk limits).

**(c) Baseline smells**

4. **minor — Duplicated Code**: the two `TradeSignal(...)` construction blocks (52-68 vs 98-111) are byte-identical except entry_price defaulting — extract `_trade_signal_from(metadata, signal, *, default_price)`.
5. **minor — Message Chain**: `sized.decision.metadata["..."]` chains appear in consumers; the adapter exposes raw internals rather than projecting a result DTO.

**(d) Maintainability hot spots**: the function does three jobs (explicit-qty path, config synthesis, engine delegation) — split.

---

# Group 3 — `scheduler/`

## scheduler/scheduler.py ✅

**(a) Bugs / correctness**

1. **major** — `scheduler.py:30-39` (`start`): no lock/guard around the async start sequence — two concurrent `start()` coroutines both pass the `if self._started` check (it flips only at line 34 after a synchronous `start()`; but between line 31 check and 34 set there is an `await asyncio.to_thread(list_services...)`? No — `_started = True` at line 34 precedes awaits, so double-start is guarded *only if* no await occurs between check and set — here safe by luck of ordering, yet `sync_services` re-entrancy (line 48) has **no such guard**: concurrent invocations race on `_job_signatures` and can add/remove jobs concurrently against APScheduler's non-thread-safe jobstore mutation path. Race condition on shared state (checklist item).
2. **minor** — `scheduler.py:44`: `shutdown(wait=False)` abandons running job threads/tasks without awaiting completion; combined with `asyncio.to_thread` DB writes inside jobs (line 174), process teardown can kill mid-write leaving background task rows stuck in "running" (they are later swept by `mark_background_tasks_interrupted`, but only if that is called on next boot).
3. **minor** — `scheduler.py:142-145`: orphan sweep removes *any* job in the scheduler not in desired ids — if another component ever registers its own jobs on the same scheduler instance they get silently deleted; ownership coupling.
4. **minor** — `scheduler.py:229-231`: on success, service status is set to RUNNING again (already RUNNING from 187-192); intent was presumably an idle/OK state — as written, last_activity updates but status vocabulary never distinguishes "ran fine" vs "running now".
5. **minor** — `scheduler.py:279`: `_run_service_job` re-raises after recording failure — correct for visibility, but APScheduler will also log it and count an error per misfire; with `coalesce=True` this is fine, just noisy. Judgment call.

**(b) Security**

6. **minor** — `scheduler.py:255-257`: raw `str(exc)` persisted into `config_json.error`/progress messages shown in UIs — exception text can embed internal paths/connection strings; no scrubbing (contrast with sentry redaction).

**(c) Baseline smells**

7. **minor — Duplicated Code**: the three near-identical upsert payloads (`base_config`/`complete_config`/`failed_config`, lines 156-225 & 244-272) differ by a handful of keys — extract a `_record_task(status, **overrides)` builder. Same shape repeats in both job modules below (Shotgun Surgery across three files for any config-key change).
8. **minor — Middle Man**: `_SCHEDULED_JOB_HANDLERS` registry + signature cache + sync loop could collapse into declarative cron configs; today adding one job touches handler dict, options docs and DB rows.

**(d) Maintainability hot spots**: none beyond above.

---

## scheduler/jobs/signal_bot_health_check.py ✅

**(a) Bugs / correctness**

1. **minor** — `signal_bot_health_check.py:41-47`: `results.get("status", "error")` defaults to error on missing key (good), but `bot_results` items are accessed with `r['name']` (line 47) via direct index inside f-string list comprehension — a malformed result row without "name" raises KeyError *inside* the health check itself, converting a failed-health report into an exception path.
2. **minor** — `signal_bot_health_check.py:36`: first `upsert_background_task_record` omits `created_at`; relies on server default (`now_iso`) — fine — but subsequent calls also omit it, so the created_at comes from row creation while every other job passes explicit timestamps; inconsistent with sibling module.

**(b) Security** — nothing found.

**(c) Baseline smells**

3. **minor — Duplicated Code**: the three `upsert_background_task_record(...)` call blocks (20-37, 62-79, 86-101) repeat name/description/source verbatim — extract locals or a partial.
4. **minor — Speculative Generality**: description string hardcodes "stockkit" bot which doesn't exist as its own processor (registry maps stockkit→ramjet factory) — doc drift.

**(d)** None significant.

---

## scheduler/jobs/weekly_data_refresh.py ✅

**(a) Bugs / correctness**

1. **major** — `weekly_data_refresh.py:34-79`: the entire job **fakes progress without refreshing any data**. It loops over hardcoded symbols, writes progress records claiming "Refreshing EURUSD…", sleeps 0, and logs "finished". No data download/import happens anywhere in the module. As a scheduled service registered in `_SCHEDULED_JOB_HANDLERS` (scheduler.py:19-21), it reports completed refreshes that never occurred — misleading operational evidence recorded into verification evidence via `upsert_background_task_record`'s final-status mirror. This is either a placeholder shipped to production or dead code wired live; both are wrong.
2. **minor** — `weekly_data_refresh.py:11-12`: hardcoded symbol/timeframe lists ignore the service's own `config_json` (service.config is available via service_id lookup) — configuration channel exists but unused.

**(b) Security** — nothing found.

**(c) Baseline smells**

3. **minor — Duplicated Code**: per-symbol upsert payload construction duplicates scheduler.py's base_config shape again (see cross-file smell above).
4. **minor — Speculative Generality**: `_DEFAULT_TIMEFRAMES` logged but never used beyond log lines.

**(d)** The module should either be implemented (delegate to the data_tools downloaders used by `scripts/download_discovery_data.py`) or removed from the handler map.

---

## scheduler/__init__.py ◻ scanned — 3-line re-export, nothing found.
## scheduler/jobs/__init__.py ◻ scanned — 3-line re-export, nothing found.

---

# Group 4 — `signal_bots/`

## signal_bots/registry.py ✅

**(a) Bugs / correctness**

1. **major** — `registry.py:32-35` (`_build_nitro`): loads config from **`ramjet/config.yaml`** — copy/paste bug identical to `_build_ramjet`. If nitro ever gets its own `nitro/config.yaml`, it will silently never be read; today it means Nitro cannot diverge from RamJet config at all despite being presented as a separate option in `SIGNAL_PROCESSOR_OPTIONS`.
2. **major** — `registry.py:91-98`: `"stockkit": _build_ramjet` maps StockKit to the RamJet factory, but the option text (line 66) promises "StockKit-compatible processor (SL/TP + trailing metadata)" sourced from a different codebase (per ramjet docstring, the port origin). Any user selecting stockkit gets RamJet formulas with ramjet metadata (`signal_bot: "ramjet"` stamped by processor.py:210) — mislabeled provenance on live signals.
3. **minor** — `registry.py:38-59`: `_build_rocketfuel_longsonly/_longandshort` are the same function modulo the variant literal — Duplicated Code; a `functools.partial`/closure over variant would do.
4. **minor** — `registry.py:105-110` (`get_signal_processor`) returns untyped `object` — callers must isinstance-cast; the factory type alias exists but the return type ignores it.

**(b) Security**

5. **minor** — `registry.py:20`: `yaml.safe_load` — good (not unsafe load). Nothing else.

**(c) Baseline smells**

6. **minor — Duplicated Code**: config-merge boilerplate repeated in all four builders; extract `_merged_config(subdir)` helper.
7. **minor — Repeated Switches**: `SIGNAL_PROCESSOR_OPTIONS` (UI metadata) parallel to `SIGNAL_PROCESSORS` (factories) keyed by overlapping-but-different id sets ("rocketfuel" exists in factories but not options) — drift already visible; single source of truth recommended.

**(d)** Config file location coupling: builders assume `<bot>/config.yaml` relative to package root — same assumption duplicated in health_check.py:37-41 (see below).

---

## signal_bots/health_check.py ✅

**(a) Bugs / correctness**

1. **minor** — `health_check.py:37-41`: config-path resolution logic (`name.split("_")[0]` then special-case overrides for rocketfuel*/stockkit/nitro) **reimplements** registry's config locations a second time; add a new bot and you must update both files. Also `name.split("_")[0]` for hypothetical "rocket_fuel_x" style ids would produce wrong subdir — fragile heuristic plus overrides.
2. **minor** — `health_check.py:33-35`: checks `name not in SIGNAL_PROCESSORS` — but since registry maps every option id, this branch is currently unreachable; meanwhile it does NOT verify processors actually instantiate (`get_signal_processor(name)` would catch constructor errors like bad variant values). Health check is shallower than its docstring claims ("Bot processors import successfully").
3. **minor** — `health_check.py:50`: overall status derived only from presence-of-config errors; instantiation/behavior not exercised.

**(b) Security** — nothing found.

**(c) Baseline smells**

4. **minor — Shotgun Surgery**: as noted — registry.py and health_check.py must change in lockstep for any new bot; move config-path knowledge onto the registry entries.
5. **minor — Duplicated Code**: the per-option result-dict scaffold is fine; no further duplication.

**(d)** Docstring says "Run with python -m signal_bots.health_check" — works, good.

---

## signal_bots/ramjet/processor.py ✅

**(a) Bugs / correctness**

1. **major** — `processor.py:102-113` (`_calculate_supertrend` duck-typing branch): `hasattr(frame, "ta")` is always False for plain DataFrames unless the `pandas_ta` accessor package has been imported *and* registered — even then `hasattr(df, "ta")` tests attribute existence, which pandas_ta installs as an accessor making it True only when installed. The fallback then uses `ewm(alpha=1/period)` ATR (Wilder) while the class docstring and `_calculate_atr` use EMA-span ATR (line 93) — the two supertrend codepaths use **different ATR definitions**, so signals differ depending on whether pandas_ta is installed. Silent environment-dependent trading behavior.
2. **major** — `processor.py:131,154-158`: supertrend direction initialization `supertrend = np.ones(n_rows, dtype=bool)` assumes bullish start from bar 0 regardless of price vs bands; with NaN final_upper before first_valid_idx, early bars compare close against uninitialized zeros (`close[i] > final_lower[i]=0` → always True) — first-bars direction is garbage until convergence. Standard implementations seed direction from close vs initial bands; here early signals can flip-flop spuriously.
3. **minor** — `processor.py:194-195`: `entry_price = float(frame["close"].iloc[-1])` — uses the *last closed candle* as entry; combined with live submission latency the SL/TP are anchored to stale prices; acceptable design choice but undocumented slippage assumption.
4. **minor** — `processor.py:199-200`: SL/TP computed for BUY only — `_normalize_buy_action` enforces buy-only so consistent, but the class is advertised in options as "BUY-only setup" — consistent; note take_profit is TP1 only, trailing handled downstream — fine.
5. **minor** — `processor.py:13-29`: local `_TIMEFRAME_ALIASES` duplicates `shared/constants.TIMEFRAME_ALIASES` concept with a *different* alias set ("60min" etc.) layered after `normalize_timeframe` — three-layer alias chain whose composition is untested; e.g. "H1" → normalize_timeframe gives "1h"; "60MIN" → normalized "60min" → mapped "1h" — works, but any new alias must consider both maps.

**(b) Security** — nothing found (yaml.safe_load in from_config_file).

**(c) Baseline smells**

6. **minor — Duplicated Code**: `_prepare_market_data`, `_calculate_atr`, `_resolve_timeframe`, `_TIMEFRAME_ALIASES` duplicated nearly byte-for-byte in rocketfuel/processor.py (13-31, 65-91) — extract shared `signal_bots/_market.py`.
7. **minor — Refused Bequest (mild)**: `NitroSignalProcessor` subclasses RamJet solely to override action validation and restamp metadata — composition (factory param) would express intent better; see nitro notes.
8. **minor — Primitive Obsession**: `raw_signal: dict[str, Any]` untyped bag; a TypedDict/pydantic RawSignal would document required keys.

**(d) Maintainability hot spots**: `_calculate_supertrend` mixes library detection, column-name guessing (105-110) and a 50-line manual implementation — split and unit-test each path independently.

---

## signal_bots/rocketfuel/processor.py ✅

**(a) Bugs / correctness**

1. **minor** — `rocketfuel/processor.py:124-130`: SELL in longsonly variant sets `stop_loss=None, exit_only=True` — downstream risk engine (`risk/live.py`) sizes via ATR fallback stop when stop_loss is None *unless* consumers honor exit_only; nothing in this repo's shared models documents `exit_only` semantics (grep shows it originates here) — contract lives only in a stringly metadata key.
2. **minor** — `rocketfuel/processor.py:115`: same last-close-as-entry anchoring note as ramjet.
3. **minor** — RSI mentioned in class docstring ("RocketFuel RSI strategy") but no RSI computed anywhere — the processor trusts the incoming signal entirely and only derives ATR stops; docstring misleads about signal generation responsibility.

**(b) Security** — nothing found.

**(c) Baseline smells**

4. **major — Duplicated Code**: `_prepare_market_data` (65-81), `_calculate_atr` (83-91), `_resolve_timeframe`/aliases (13-31), metadata timestamp/price copying (147-150) are copies of ramjet/processor.py — the strongest extraction candidate in the area.
5. **minor — Repeated Switches**: variant handling (`if self.variant == ...` at 126, 140-144) would read better as two strategy subclasses or a small variant table.

**(d)** None beyond above.

---

## signal_bots/nitro/processor.py ✅

**(a) Bugs / correctness**

1. **minor** — `nitro/processor.py:19-24`: `process_signal(self, raw_signal: dict[str,Any], market_data)` drops the type annotation on market_data and forwards positionally-named args — works, but the override signature diverges from parent's typed signature (no DataFrame annotation) weakening static checking.
2. **minor** — metadata restamping rebuilds TradeSignal field-by-field (29-36); `TradeSignal` is mutable dataclass — `signal.metadata.update({...})` would suffice; extra construction risks dropping future fields added to the parent model (Shotgun Surgery trigger).

**(b) Security** — nothing found.

**(c) Baseline smells**

3. **minor — Refused Bequest**: inheritance used purely to specialize validation + stamping; the parent's `process_signal` runs fully then results are rewritten. Composition over inheritance applies cleanly.
4. **minor — Duplicated Code**: `_normalize_buy_action` override duplicates the parent's method except the error message string.

**(d)** None.

---

## signal_bots/__init__.py, ramjet/__init__.py, rocketfuel/__init__.py, nitro/__init__.py ◻ scanned — pure re-export shims, nothing found.

---

# Group 5 — `scripts/`

## scripts/live_smoke.py ✅

**(a) Bugs / correctness**

1. **major** — `live_smoke.py:396-459` (`_ctrader_order_worker`): unlike `_ctrader_strategy_e2e_worker` (which wraps its flow in try/BaseException + `_best_effort_close_open_position`, lines 589-593), the order-worker roundtrip has **no best-effort close** — if `_wait_for_ctrader_position(should_exist=False)` times out or any exception fires between open and successful close (lines 444-459), the demo position stays open and every subsequent run refuses to start ("Refusing smoke order because … already has an open position", line 422). Asymmetric cleanup = self-bricking tool until manual intervention.
2. **major** — `live_smoke.py:1444-1449`: local `class contextlib_suppress` reimplements `contextlib.suppress` (stdlib) — but worse, it swallows *everything including KeyboardInterrupt/SystemExit-ish BaseExceptions*? No — `__exit__` returns True for all exceptions incl. `BaseException` subclasses passed to it (contextlib passes BaseException too), so the cancel-failure handler at line 766-769 can swallow a Ctrl-C during cancellation. Reinventing stdlib with subtly broader suppression.
3. **minor** — `live_smoke.py:216-224,260-268,298-306`: three near-identical subprocess-runner helpers (`_run_ctrader_adapter_subprocess` / `_order_subprocess` / `_strategy_e2e_subprocess`) differing only in mode name and args — textbook Duplicated Code (see smells).
4. **minor** — `live_smoke.py:230-233` (+274, 312): JSON payload extracted by scanning stdout lines for one starting with `{` — any broker/library log line that happens to start with `{` (JSON logs!) earlier than the payload wins because search is `reversed(lines)` (last match); fragile contract between parent and worker, would break silently if worker prints a trailing JSON log.
5. **minor** — `live_smoke.py:352-356`: reaches deep into adapter internals (`adapter._ProtoOAReconcileReq`, `adapter._send_request`, `adapter._symbol_name_to_id`, `adapter._trading_client` at 1186, 1247, 1272) — Message Chains into private API; any adapter refactor breaks smoke tooling.
6. **minor** — `live_smoke.py:1562,1578,1592`: `os._exit()` skips atexit handlers/stdio buffers other than the explicit flushed prints — acceptable in workers to dodge Twisted reactor teardown hangs, but undocumented why normal `return` is unsafe here.
7. **minor** — `live_smoke.py:1006-1009`: buy limit = max(price)*1.05, sell limit = min*0.95 — for a stock quote with bid=0 missing, min could be last trade far below ask; sell_limit computed off min positive price may cross the book and fill instantly at market — fine for flatten intent, but the "limit" semantics are approximations worth documenting.
8. **minor** — `live_smoke.py:1436`: `zip(..., strict=True)` over a 2-tuple names list vs `asyncio.gather(return_exceptions=True)` results — correct today; brittle only if a third check is added without updating names.

**(b) Security**

9. **minor** — `live_smoke.py:146`: `_public_error(exc)` puts raw exception strings (may embed API key fragments in URLs from SDK errors) into reports written under `reports/`; no redaction pass before writing (contrast sentry.py hygiene).
10. **minor** — `live_smoke.py:55-61` + subprocess env passing: credentials read from `.accounts.env` and passed implicitly via file path to child processes — fine — but the smoke report records account numbers (`account_hint`, line 140) into committed-report directories; check `.gitignore` coverage of `reports/live_smoke_*`.

**(c) Baseline smells**

11. **major — Duplicated Code / Long Function**: 1,620-line single-file CLI mixing 10 modes incl. 3 worker submodes; the three ctrader subprocess helpers (198-315) and the two alpaca order-wait loops (`_wait_for_alpaca_fill` 616, `_wait_for_alpaca_order_terminal` 1020) duplicate each other's polling logic. Split per mode into modules.
12. **minor — Speculative Generality**: `_SMOKE_MODE_ALIASES` maps both directions of naming; `main` re-normalizes args.mode (line 1551) though argparse choices already include canonical names — double normalization.
13. **minor — Middle Man**: `_DiscordSmokeAdapter` fine; `_await_alpaca_terminal_status` (646-660) discards filled_qty just to return status — thin wrapper adding indirection.

**(d) Maintainability hot spots**: this is the highest-risk operational script in the area (it places *real* paper orders); its correctness depends on cleanup symmetry that currently differs between the cTrader paths (finding 1).

---

## scripts/adaptive_harvester.py ✅

**(a) Bugs / correctness**

1. **major** — `adaptive_harvester.py:36-43`: DATA_DIR resolution duplicated from shared/constants but **without the home-candidate scoring logic** — so harvester and platform can resolve different DATA_DIRs for the same env value when `~` is used (harvester: literal expanduser; platform: scored candidates). State file (`harvester_state.json`) and downloaded parquets could land in a different tree than the app reads. Copy-paste divergence of critical config resolution.
2. **minor** — `adaptive_harvester.py:524-526`: `avg_latency_s` computed as `sum/len(outcomes)` guarded *after* use pattern — actually computed before the `if not outcomes` guard which sets inf afterwards (lines 525-527 compute then overwrite) — dead assignment ordering confusion; outcomes can never be empty here (batch slice non-empty while offset<total), so the guard is dead code.
3. **minor** — `adaptive_harvester.py:349-353`: weekend empty files treated as success=False but not penalized; weekday empties retry with backoff then fail the URL — a mis-detected LZMA-valid-but-truncated file (passes decompress, zero ticks) would be accepted as success with empty parse output; `_looks_invalid_bi5` checks decompressability only, not content length (line 264-271).
4. **minor** — `adaptive_harvester.py:689-692`: closure `on_batch_complete` captures loop variables `job`/`state` — safe here (called synchronously within same iteration) but classic late-binding trap if ever deferred.
5. **minor** — `adaptive_harvester.py:202` (`datetime.now(timezone.utc).date() + timedelta(days=1)`): end-exclusive "tomorrow" generates URLs for today+1 whose files don't exist yet → wasted 404 retries with penalty classification for the current partial day; minor inefficiency each run.
6. **minor** — `adaptive_harvester.py:158`: plain `FileHandler` (no rotation) for task log — long-running harvest grows `task_harvester.log` unbounded (shared/logging.py uses RotatingFileHandler; inconsistent).

**(b) Security** — nothing found (public Dukascopy endpoints, no secrets).

**(c) Baseline smells**

7. **minor — Duplicated Code**: `_utc_now`, `_write_json_atomic`, month-range helpers exist in near-equivalent form in other data_tools scripts (Shotgun Surgery across downloader family).
8. **minor — Long Function**: `_download_month` (~155 lines) mixes HTTP session mgmt, AIMD tuning, progress logging and parquet assembly; extract tuning controller.

**(d)** Good: atomic state writes (line 132-136), exponential backoff, crash-resume design.

---

## scripts/analyze_mt5_parity.py ✅

**(a) Bugs / correctness**

1. **minor** — `analyze_mt5_parity.py:402`: `our_stop_distance = abs(pd.to_numeric(indicators["sl_price"]...) - df["close"])` — if `indicators` lacks "sl_price" or "daily_atr" keys, KeyError with no context; also assumes index alignment between indicators series and df.
2. **minor** — `analyze_mt5_parity.py:404-417`: builds `by_ts = pd.Series(df.index, index=ts)` then `by_ts.index.searchsorted(entry_ts)` — if `ts` contains NaT (coerced, line 403), searchsorted with NaT entries yields arbitrary positions; NaN timestamps not dropped first (unlike other scripts' dropna discipline).
3. **minor** — `analyze_mt5_parity.py:485`: `zip(ref.trades, times, strict=False)` silently truncates on mismatch — deliberate lenience, but hides importer inconsistencies during parity forensics where truncation is exactly what you're hunting.
4. **minor** — `analyze_mt5_parity.py:114`: tolerance `max(1e-5, abs(stop)*1e-7)` — heuristic stop-vs-exit matching can misclassify near-stop signal exits; acknowledged approximation in a diagnostics tool, acceptable.

**(b) Security** — nothing found.

**(c) Baseline smells**

5. **minor — Duplicated Code**: tz-normalization block `if ts.tzinfo is None: tz_localize("UTC") else tz_convert("UTC")` appears at 61-65, 185-189 (imported sibling), 409-413 — repeated inline instead of reusing `_utc_timestamp` defined in this very file (line 61).
6. **minor — Shotgun Surgery**: imports private helpers (`_load_ohlcv`, `_opposite_side_series`, `COMPARISON_CASES`) from `regenerate_mt5_parity_artifacts` (lines 24-29) — the two scripts must evolve together; shared module warranted.

**(d)** Solid forensic tooling overall.

---

## scripts/run_risk_parity_backtest.py ✅

**(a) Bugs / correctness**

1. **major** — `run_risk_parity_backtest.py:343-352`: rebalance-attribution PnL uses `prices[ticker].loc[reb_date]` and previous rebalance date directly — but `attribution_prev_price` uses the *previous scheduled rebalance date*, while `interval_start_portfolio_value` marks value through *current* rebalance via `advance_portfolio_value`. If a scheduled date had no trading row (holiday mapping via `first_available_trading_dates`), `.loc[rebalance_dates[i-1]]` raises KeyError on non-trading timestamps. Guarded only by luck that MS dates usually map to trading days.
2. **major** — `run_risk_parity_backtest.py:405-414`: portfolio_value series built by compounding daily returns from weights snapshot dict `rebalance_schedule[date]`, but trades were sized against `current_portfolio_value` advanced separately (line 273-282) — two parallel equity ledgers (one per-day, one per-rebalance) that can drift apart; final stats mix them (`net_profit` from series, realized pnl from trades). Documented nowhere which is authoritative; PSR-style comparisons against QC will diverge.
3. **minor** — `run_risk_parity_backtest.py:117-124`: `np.corrcoef(recent)` with <2 rows or zero-variance asset produces NaN correlations → regime classification garbage-in; no guards (compare covariance fn which also lacks PSD guarantees after shrinkage — SLSQP may then fail silently into inverse-vol fallback, line 105-110).
4. **minor** — `run_risk_parity_backtest.py:56,63-70`: yfinance download with `auto_adjust=True` then separate opens reindexed — adjusted open/close consistency assumed; yfinance occasionally returns misaligned partial frames → silent look-ahead-free but wrong-history backtests.
5. **minor** — `run_risk_parity_backtest.py:245-246`: validation message says "less than the number of price rows" but code enforces `< len(prices)` while returns (dropna) is one shorter — off-by-one edge if warm_up == len(prices)-1 → `returns.index[warm_up_days]`… actually uses prices.index — consistent; borderline.
6. **minor** — `run_risk_parity_backtest.py:423`: Sharpe computed as mean*252 / vol_ann where vol_ann already includes √252 — algebra fine; but `if vol_ans` truthiness treats 0-vol as sharpe 0 rather than undefined — cosmetic.

**(b) Security** — nothing found (yfinance public).

**(c) Baseline smells**

7. **minor — Long Function**: `run_backtest` is ~240 lines with nested closures mutating nonlocal state (`advance_portfolio_value`); split simulation from stats.
8. **minor — Primitive Obsession**: trades as ad-hoc dicts with stringly meta keys; the repo's own `Trade` model imported only in `build_validation_reference`.

**(d)** Honest docstring about fallback nature — good.

---

## scripts/regenerate_mt5_parity_artifacts.py ✅

**(a) Bugs / correctness**

1. **major** — `regenerate_mt5_parity_artifacts.py:272-279` (`_build_trades` exit pairing): `exit_cursor` advances monotonically; for an entry whose matched exit index ≤ current fill_index, cursor skips forward — but when multiple entries occur before any exit, later entries pair with *earliest remaining* exit even if that exit precedes their own fill bar? No: line 276 `while exit_indices[exit_cursor] <= fill_index: exit_cursor += 1` ensures exit after fill. However overlapping positions are impossible in this model while MT5 reference allows simultaneous exposure — parity artifacts systematically drop concurrent-position trades (cursor consumed). Parity tool limitation worth documenting; affects trade-count reconciliation.
2. **minor** — `regenerate_mt5_parity_artifacts.py:173`: `groupby("timestamp").first()` — takes first *non-null per column* across sources ordered by priority; mixed bid/ask sources can splice OHLC from different feeds at the same timestamp (open from feed A, close from feed B) — subtle data hybridization invisible downstream.
3. **minor** — `regenerate_mt5_parity_artifacts.py:160-161,174-175`: start/end filter applied twice (pre-concat and post-groupby) with inclusive end — duplicated work, harmless.
4. **minor** — `regenerate_mt5_parity_artifacts.py:388`: `resolve_strategy_parameters(STRATEGY_PATH, {})` then `params.update(...)` mutates returned dict — if resolver returns a cached/shared dict, params leak across cases (depends on runner implementation; defensive copy advisable).
5. **minor** — `regenerate_mt5_parity_artifacts.py:479`: `--risk-pct` arg parsed but never used (fixed_risk_amount comes from strategy params, line 415) — dead parameter misleading operators.

**(b) Security** — nothing found.

**(c) Baseline smells**

6. **minor — Long Function**: `_build_trades` ~120 lines of cursor state machine — extract stop-scan and sizing steps.
7. **minor — Mysterious Name**: `_price_unit_value` vs `pnl_unit_value` vs `risk_unit_value` — three near-identical unit concepts distinguished by prefix only.

**(d)** Central artifact generator for MT5 parity — high blast radius; see finding 1.

---

## scripts/matrix_queue_runner.py ✅

**(a) Bugs / correctness**

1. **major** — `matrix_queue_runner.py:80-96` (`acquire_lock`): lock acquisition is check-then-write (`LOCK_PATH.exists()` → `write_text`) with no `O_EXCL` — two cron sessions racing the same tick can both observe "no lock" and both proceed (classic TOCTOU race on shared state; checklist item). Use `open(LOCK_PATH, "x")`.
2. **major** — `matrix_queue_runner.py:227-249`: `while True:` queue-drain loop runs *all pending strategies* in one invocation (comment says intentional), but each `run_strategy` can take TIMEOUT+120 s; with cron sessions every N minutes the second cron blocks harmlessly on lock — however if the machine reboots mid-run, LOCK_TTL (7200 s default) plus `any_matrix_process_running` pgrep gate (line 87) is the only recovery — pgrep pattern `python.*runner\.cli matrix` won't match a venv-python invocation whose cmdline is `/path/.venv/bin/python -m runner.cli matrix`? It will (regex matches substring), ok — but it also matches *this* queue runner's own child only; fine. Main residual risk: stale-lock TTL longer than typical cron gap causes skipped ticks — operational, minor.
3. **minor** — `matrix_queue_runner.py:191-197`: `subprocess.run(..., timeout=int(TIMEOUT_SECONDS)+120)` without capture limit — `stdout=PIPE` accumulates full matrix logs in memory (only tail kept); a chatty run can OOM the queue runner itself. Use a log file like sibling scripts do.
4. **minor** — `matrix_queue_runner.py:50-51`: corrupt state JSON silently swallowed (`except Exception: pass`) → fresh state resets all progress markers while old grid outputs remain — statuses rebuilt from disk anyway (refresh_strategy_states), so benign but silent.
5. **minor** — `matrix_queue_runner.py:66-77`: `pgrep` failure (not installed) returns False → treated as "no process running" → stale lock eligible for steal after TTL — acceptable fallback, undocumented.

**(b) Security**

6. **minor** — `matrix_queue_runner.py:199,208`: `output_tail` (last 8000 chars of combined stdout+stderr) persisted into queue_state.json and rendered to chat/UI (`render_state`) — build logs can embed env values echoed by tools; unfiltered sink.

**(c) Baseline smells**

7. **minor — Duplicated Code**: status-row construction triplicated in `refresh_strategy_states` (124-156) with per-branch field defaults — table-driven builder would collapse it.
8. **minor — Speculative Generality**: `MAX_WORKERS`/engine/timeouts env-plumbed but never validated (non-int MATRIX_TIMEOUT_SECONDS crashes at line 197 int() inside run — actually int() at line 197 raises at runtime mid-queue).

**(d)** Lock design needs O_EXCL fix (finding 1) — top operational risk of the script set.

---

## scripts/regenerate_backtests.py ✅

**(a) Bugs / correctness**

1. **critical** — `regenerate_backtests.py:201-208` + `221-227`: `main()` **unconditionally purges ALL backtests** (SQLite tables truncated via unconditional deletes; every `*.trades.json` unlinked) before regenerating — with no `--yes/--dry-run` guard and no backup. Running this against a DATA_DIR where any strategy fails (network-less engine import, missing engine binary) leaves the platform with a wiped history: `_purge_sqlite_backtests` deletes portfolio-independent evidence including live-comparison baselines. Destructive-by-default with zero confirmation is the single most dangerous script in the area.
2. **major** — `regenerate_backtests.py:91-123` (`_candidate_symbols`): symbol selection guesses from strategy tags ("forex"→XAUUSD/EURUSD…; else default list) — regenerated "same" backtest can silently run on a *different symbol/timeframe* than the original result it replaced (original id/data discarded in purge). Reproducibility of the research corpus is broken by design here.
3. **minor** — `regenerate_backtests.py:53-73` (`_parse_symbol_timeframe`): depth-based heuristics (`len(parts)>=5` vs `>=4`) with silent "1d" fallbacks — mislabeled timeframes propagate into backtest configs quietly.
4. **minor** — `regenerate_backtests.py:215-217`: JSON purge `path.unlink()` without error handling — read-only file raises mid-purge leaving half-purged state (SQLite already truncated at that point — see 1).
5. **minor** — `regenerate_backtests.py:281-284`: `engine.validate_config(config)` outside try — a validator raising (vs returning errors) aborts the whole regeneration loop after purge completed.

**(b) Security** — nothing found beyond destructive-I/O concern above.

**(c) Baseline smells**

6. **minor — Shotgun Surgery**: duplicates DATA_DIR resolution again (lines 38-43) instead of importing `shared.constants.DATA_DIR` (three scripts do this: adaptive_harvester, regenerate_backtests, consolidate_parquets — each slightly different).
7. **minor — Long Function**: `main()` 100 lines orchestrating purge/index/run/report.

**(d)** Recommend: refuse to run without explicit `--purge-existing`, write to staging dir, and preserve original backtest ids/parameters for reproducibility.

---

## scripts/generate_matrix_grid_overview.py ✅

**(a) Bugs / correctness**

1. **major** — `generate_matrix_grid_overview.py:9-11`: hardcodes absolute machine-specific paths (`/mnt/block-volume-data/...`, `/home/ubuntu/Desktop/trading-platform/frontend/public/...`) with **no CLI arguments at all** — script is unrunnable on any other checkout/volume and silently overwrites frontend asset when paths happen to exist.
2. **major** — `generate_matrix_grid_overview.py:143-261`: entire program is **module-level script code** (no main() guard) — importing it (tests, tooling) executes file I/O and overwrites the overview JSON. Also prevents reuse of its PSR helpers without side effects.
3. **minor** — `generate_matrix_grid_overview.py:78`: PSR variance formula `(1 - skew*sharpe + ((kurt-1)*sharpe²)/4)/n` uses *population* skew/kurt of per-trade returns scaled to daily-Sharpe (√252) — mixing trade-frequency moments with daily annualization understates/overstates PSR depending on trade frequency; convention mismatch vs `recalculate/stats` pipeline (which computes psr elsewhere). At minimum document the convention.
4. **minor** — `generate_matrix_grid_overview.py:242`: `datetime.utcnow()` deprecated (3.12+) and produces naive timestamp stamped 'Z' — tz-naive datetime checklist item.
5. **minor** — `generate_matrix_grid_overview.py:116-119`: `max(rows, key=...)` with sentinel −1e99 picks an all-None row as "best" when every value is None — summary reports a combo of Nones rather than null.

**(b) Security** — nothing found.

**(c) Baseline smells**

6. **minor — Duplicated Code**: `as_float`/`safe_round`/`parse_minutes` duplicate equivalents in sibling scripts (recalculate, matrix_queue_runner) — shared `scripts/_util.py` warranted.
7. **minor — Primitive Obsession**: rows as loose dicts with stringly keys sorted by magic TF_ORDER — fine for a report generator.

**(d)** Wrap in main(), parameterize paths, align PSR conventions.

---

## scripts/seed_demo_backtests.py ✅

**(a) Bugs / correctness**

1. **minor** — `seed_demo_backtests.py:26-28`: writes synthetic parquet OHLCV into the real DATA_DIR tree (`data/ohlcv/yfinance/equity/AAPL/1h.parquet`) — overwrites any genuine AAPL 1h parquet with fabricated zig-zag data if operator points DATA_DIR at production. Demo seeder should quarantine under a demo namespace.
2. **minor** — `seed_demo_backtests.py:30` + four `BacktestMeta(created_at=now)`: same timestamp reused — fine; but ids are stable ("demo-*") while created_at changes each run → `list_backtest_metadata_rows` ordering churn; harmless.
3. **minor** — trade1 quantity 10 vs exits 5+5 — consistent; trade1 pnl_pct 2.03 on 100k capital w/ qty 10 — cosmetic fixture math, fine.

**(b) Security** — nothing found.

**(c) Baseline smells**

4. **minor — Speculative Generality**: `mk_ohlcv(source/asset_class)` parameters exercised only with fixed values; drawings fixtures duplicate viewer schema knowledge.

**(d)** Fine as a fixture utility given (1) is heeded.

---

## scripts/run_discovery_grid_backtests.py ✅

**(a) Bugs / correctness**

1. **major** — `run_discovery_grid_backtests.py:22`: discovery input pinned to an absolute dated file (`discovery_20260618_0752.json`) on `/mnt/block-volume-data` — same hardwiring smell as generate_matrix_grid_overview; script silently operates on stale trial sets.
2. **minor** — `run_discovery_grid_backtests.py:150-159`: `subprocess.run(..., timeout=900)` — TimeoutExpired propagates uncaught out of `run_trial`, aborting the whole batch loop and losing results of prior trials (no try around run_trial in main loop, line 192-195).
3. **minor** — `run_discovery_grid_backtests.py:43-55` (`wait_for_data`): polls up to 30 min for a file another process should produce — coupling by filesystem with no signaling; if producer died, caller waits full timeout per symbol/timeframe (serial → hours).
4. **minor** — `run_discovery_grid_backtests.py:122`: `date_bounds` reads parquet without the existence re-check after wait_for_data — TOCTOU window tiny; acceptable.
5. **minor** — `run_discovery_grid_backtests.py:196-204`: summary `started_at` recorded *after* all trials ran (equals finished_at) — misleading telemetry field.

**(b) Security** — nothing found (subprocess args constructed locally, no shell=True).

**(c) Baseline smells**

6. **minor — Duplicated Code**: `load_trials`, `data_path`, params-mapping trio duplicated with `download_discovery_data.py` (see below) — the pair is one logical tool split in two.

**(d)** Add per-trial exception isolation.

---

## scripts/download_discovery_data.py ✅

**(a) Bugs / correctness**

1. **minor** — `download_discovery_data.py:33`: `TIMEFRAME_DEFAULT_START` dates ("2026-04-19") hardcoded — future-dated relative to some environments; yfinance hourly limited to 730 days window — silently fewer rows than MIN_ROWS → perpetual re-download attempts each run (cached check fails, retry burns quota).
2. **minor** — `download_discovery_data.py:99-106`: 4h resample from 60m bars — label/closed conventions default (left-labeled) — consistent with most vendors but undocumented; boundary hour alignment vs exchange session can shift bars.
3. **minor** — `download_discovery_data.py:114-116`: catches all exceptions per attempt with sleep(min(30*attempt,120)) — total worst-case 30+60+90+120=300 s per combo serially; fine, but `except Exception` breadth hides programming errors (KeyError on YF_SYMBOLS typos retried identically 4×).
4. **minor** — same absolute DISCOVERY_PATH pin as sibling script (line 22).

**(b) Security** — nothing found.

**(c) Baseline smells**

5. **minor — Duplicated Code**: shares load_trials/data_path/summary-writing shape with run_discovery_grid_backtests.py; extract `discovery_common.py`.

**(d)** None further.

---

## scripts/regenerate_qc_risk_parity_artifact.py ✅

**(a) Bugs / correctness**

1. **minor** — `regenerate_qc_risk_parity_artifact.py:57-58`: `prices.index.tz_convert(None)` strips tz keeping UTC wall time — correct conversion, but downstream `pd.Timestamp(end)` naive comparisons (line 125 duration calc) mix naive/aware casually; works because strip happened, fragile pattern.
2. **minor** — `regenerate_qc_risk_parity_artifact.py:126`: `total_bars=int(prices.count().sum())` — counts *cells* (tickers×days), not bars; mislabeled metric in artifact metadata.
3. **minor** — `regenerate_qc_risk_parity_artifact.py:155-163`: monkeypatches `risk_parity.download_prices` global — not thread-safe and silently alters module behavior for anything importing concurrently; acceptable for a CLI, flagged for pattern proliferation.
4. **minor** — `regenerate_qc_risk_parity_artifact.py:113`: `stats["net_profit"] = stats.get("net_profit")` — no-op statement; leftover editing residue.

**(b) Security** — nothing found.

**(c) Baseline smells**

5. **minor — Duplicated Code**: `_to_iso` third copy in area (also analyze_mt5_parity inline, database.py variants).
6. **minor — Feature Envy**: `_build_result` reaches deeply into `result["stats"]/["trades"]` dict shapes owned by run_risk_parity_backtest — a typed result DTO would decouple.

**(d)** None.

---

## scripts/recalculate_backtest_stats.py ✅

Well-behaved maintenance script: dry-run support, skip flags, atomic-ish JSON rewrite fallback.

**(a) Bugs / correctness**

1. **minor** — `recalculate_backtest_stats.py:142-147`: refuses `--data-dir` different from resolved DATA_DIR — good guard, but the error instructs env var change; combined with database.py's import-time ENGINE this is the correct workaround — consistent, just awkward UX.
2. **minor** — `recalculate_backtest_stats.py:76-83`: rewrite fallback writes dotfile then os.replace — if process dies between write and replace, orphan `.name.rewrite` lingers forever (no cleanup sweep).
3. **minor** — `recalculate_backtest_stats.py:42-49`: merged-stats equality check compares dicts pre/post — `compute_stats_for_result` may return numpy scalars failing == equality spuriously → perpetual "updated" counts; depends on runner.stats serialization discipline.

**(b) Security** — nothing found.

**(c) Baseline smells**

4. **minor — Duplicated Code**: sqlite/json recalc twins differ only in persistence calls — template-method candidate.

**(d)** None.

---

## scripts/overnight_strategy_discovery.py ✅

**(a) Bugs / correctness**

1. **major** — `overnight_strategy_discovery.py:84-104`: the "discovery" is **mock**: it constructs a `BacktestConfig` it never runs (comment admits "experimental skeleton"), emits `status: "mock"` trials ranked by nothing, and saves them into `data/discovered_strategies/` — the very directory `scripts/run_discovery_grid_backtests.py` and `download_discovery_data.py` treat as real discovered strategies. A mock feeding a real pipeline without a gate: downstream grid backtests will happily "validate" randomly generated parameter noise. Status field does say "mock", but nothing downstream checks it.
2. **minor** — `overnight_strategy_discovery.py:21-24`: `try: from shared.constants import DATA_DIR except ImportError:` fallback — masks real import breakage (e.g. dotenv missing) with silent path divergence.
3. **minor** — `overnight_strategy_discovery.py:27`: module-level `mkdir` side effect on import (same anti-pattern as generate_matrix_grid_overview).
4. **minor** — `random_rsi_params` (49-54) can generate oversold ≥ overbought (e.g. 35/65 boundary inversion is impossible given ranges 10-35 vs 65-90 — actually safe; noting verified-safe).

**(b) Security** — nothing found.

**(c) Baseline smells**

5. **minor — Speculative Generality**: BacktestConfig construction exists solely to be discarded; delete until real.

**(d)** Either wire to the real vectorbt runner (like run_discovery_grid_backtests does) or clearly fence outputs.

---

## scripts/verify_ctrader_order.py ✅

**(a) Bugs / correctness**

1. **minor** — `verify_ctrader_order.py:35-36`: hardcoded demo ORDER_ID `27678592` / ACCOUNT_ID `47644617` as *defaults* — bare invocation re-verifies an ancient order and reports "VERIFIED" — misleading artifact generation from stale constants (env override documented, defaults still wrong-footing).
2. **minor** — `verify_ctrader_order.py:24-26`: `asyncioreactor.install()` at import without checking `asyncio.get_event_loop` state — double-install raises if another reactor was installed (fine standalone; breaks if imported as library — it's a script, acceptable).
3. **minor** — `verify_ctrader_order.py:113`: `open(out_path, "w")` without encoding — platform-dependent default encoding for JSON containing only ASCII today; latent.

**(b) Security**

4. **minor** — `verify_ctrader_order.py:89`: error strings (≤200 chars of exception text) written to reports dir — same credential-leak-in-exceptions caveat as live_smoke.

**(c) Baseline smells**

5. **minor — Message Chain**: pokes adapter privates (`_ProtoOAOrderDetailsReq`, `_send_request_with_refresh_retry`) — same Demeter violation family as live_smoke; adapter should expose `get_order_details(order_id)`.

**(d)** None.

---

## scripts/dedupe_executions.py ✅

**(a) Bugs / correctness**

1. **minor** — `dedupe_executions.py:26-33`: duplicate definition `(signal_id, filled_qty, timestamp)` — float `filled_qty` grouping is float-equality sensitive; near-duplicate fills differing in last bit escape dedupe (and the docstring's premise). Acceptable given SQLite stores floats verbatim from same source.
2. **minor** — `dedupe_executions.py:88-93`: DELETE ... WHERE rowid IN :rowids with expanding bindparam — unbounded list size; millions of dupes → giant SQL statement (SQLite var limit historically 999/32766) — SQLAlchemy expanding handles chunking? It does NOT chunk; oversized lists raise. Practical dupes volumes small; flagging for completeness.
3. **minor** — no transaction around groups-read + delete (two separate execute calls) — a concurrent insert between them isn't in rowid list — fine (only removes known dupes), benign.

**(b) Security** — nothing found.

**(c) Baseline smells** — none significant; clean focused script.

**(d)** None.

---

## scripts/consolidate_parquets.py ✅

**(a) Bugs / correctness**

1. **minor** — `consolidate_parquets.py:57-85`: merge keeps `keep="last"` after sort by timestamp only — but sort stability across members depends on member iteration order (sorted by path); "last" is whichever fragment sorted later by *path name*, not by recency — deterministic but arbitrary precedence; a misnamed fragment can shadow newer data.
2. **minor** — `consolidate_parquets.py:64-78`: whole-dataset read into memory per group; tick fragments (GBs) will OOM — no chunked write path; script unsafe for the tick trees it nominally targets (FRAGMENT_SUFFIX suggests harvester output).
3. **minor** — non-atomic: `to_parquet(target)` overwrite in place; crash mid-write corrupts the canonical file with no temp-file rename (contrast adaptive_harvester's `_write_json_atomic`).
4. **minor** — `_normalize_timeframe` unknown stems pass through (line 31) creating `{garbage}.parquet` canonical targets — quiet data relocation.

**(b) Security** — nothing found.

**(c) Baseline smells**

5. **minor — Shotgun Surgery**: DATA_DIR resolution copy #3 (lines 13-18).

**(d)** Needs atomic write + chunked merge before touching tick data.

---

## scripts/generate_golden_files.py ✅

**(a) Bugs / correctness**
1. **minor** — `generate_golden_files.py:16`: `PROJECT_ROOT` assigned twice (lines 10-11 via sys.path block, again line 16) — residue.
2. **minor** — `generate_golden_files.py:52`: `importer.load(source_path)` without `source_timezone=` — sibling callers pass `Europe/Athens`; golden fixtures generated with importer default timezone — if default ≠ Athens, goldens encode shifted timestamps and every parity test inherits the skew. Verify importer default matches; inconsistency with analyze_mt5_parity (which always passes Athens) is itself a hazard.

**(b) Security** — nothing found.
**(c) Baseline smells**
3. **minor — Duplicated Code**: REPORTS list duplicates COMPARISON_CASES paths from regenerate_mt5_parity_artifacts — single source recommended.

**(d)** None.

---

## scripts/backtest_to_validation_ref.py ✅

**(a) Bugs / correctness**

1. **minor** — `backtest_to_validation_ref.py:20-23`: reconstructs models via `Trade(**t)` — legacy payloads containing extra/renamed keys raise TypeError with poor context; sibling loaders use `Trade.from_dict` (tolerant) — inconsistent parsing path for the same file format.
2. **minor** — `backtest_to_validation_ref.py:46-47`: `res.meta.data["symbols"][0]` KeyError when data lacks symbols (common for older artifacts) — should fall back like other converters.
3. **minor** — `backtest_to_validation_ref.py:51`: `res.meta.__dict__` as raw_metadata leaks dataclass internals incl. mutable dicts into pydantic model — works, but bypasses `to_dict()` omit-empty semantics producing fatter refs than canonical.

**(b) Security** — nothing found.
**(c) Baseline smells**
4. **minor — Duplicated Code**: fourth reimplementation of BacktestResult→ValidationReference conversion (also run_risk_parity_backtest.build_validation_reference, regenerate_qc_risk_parity_artifact._build_result) — consolidate.

**(d)** None.

---

## scripts/compare_strategy_metrics.py ◻ scanned — thin CLI wrapper delegating to validation.statistics.cross_platform; argument handling and output writing are sound. Nothing found beyond the project-wide hardcoded relative-path defaults (acceptable for a dev tool).

---

# Group 6 — `strategies/`

Area-wide themes applying to most strategy files (cited once here, referenced per file):
- **Duplicated Code**: TR/ATR construction, Cutler-style RSI, and the verbatim 18-line drawings block are copy-pasted across ≥10 strategy files. Extract `strategies/_shared/indicators.py`.
- **Repeated Switches**: the stop→rsi→reversion→time exit-priority chain repeats nearly identically in mean_reversion_rsi_bb, gold_momentum, short_term_reversal, sector_rotation, cross_asset_momentum, risk_parity.
- **Return-contract fragmentation**: most strategies return `(entries, exits, indicators, drawings)`; two return 3-tuples (flagged below) — no schema enforcement anywhere.

## strategies/intraday_volatility_breakout/intraday_volatility_breakout.py ✅

The most sophisticated strategy in the area (MT5 parity port, heavily commented).

**(a) Bugs / correctness**
1. **minor** — `intraday_volatility_breakout.py:414-416,430-431,454-455`: candle-close entries/stops are shifted `-1` so platform "next-open" execution lands on MT5's timestamp. Documented (lines 407-411) and deliberate — but it means emitted entry rows precede the information (confirmation close) that justifies them; any consumer other than the parity backtester treating these as actionable-at-emission acts on future data. Repo-documented convention suppresses the smell per review rules; flagged because the safety of the trick depends entirely on the runner honoring `order_timing`.
2. **minor** — `intraday_volatility_breakout.py:189-205`: timezone resolution silently falls back from `session_timezone` to fixed-offset hours on `ZoneInfoNotFoundError`/`TypeError` — two different time models produce different session days with no warning; a typo'd tz name shifts every session boundary invisibly.
3. **minor** — `intraday_volatility_breakout.py:208-211`: coerced NaT timestamps become NaT `.dt.date` values flowing into `day` grouping — NaT-day rows form their own group and corrupt "first bar of day"/"last bar of day" logic; no dropna gate.
4. **minor** — `intraday_volatility_breakout.py:436-437`: `pd.Series(idx, index=idx)` assumes a unique index; duplicate timestamps (possible after multi-source merges upstream) break the last-bar detection equality.

**(b) Security** — nothing found.

**(c) Baseline smells**
5. **minor — Duplicated Code / Mysterious Name**: near-twin of `volatility_breakout.py` (same meta lineage, same parameter spellings) with *divergent* semantics (window inclusivity, regime direction) — readers cannot tell which is canonical.

**(d)** Hot spot: 527-line function; the confirmation-resample block (332-364) with its triple fallback deserves extraction + dedicated tests.

---

## strategies/custom_swings_supertrend/custom_swings_supertrend.py ✅

**(a) Bugs / correctness**
1. **minor** — `custom_swings_supertrend.py:279-318`: trailing/breakeven stop updates use the *current bar's* high/low and Supertrend band, then the stop is checked against the *same bar's* low/high — an intrabar ordering assumption (trail set from this bar's extreme can stop this bar out). Conservative for longs' BE (raises stop only after trigger touched) but the trailing update can manufacture same-bar exits unavailable to a bar-by-bar broker.
2. **minor** — `custom_swings_supertrend.py:51-53`: first bar TR seeded 0 instead of high−low — negligible bias, inconsistent with channel_breakout which seeds correctly.
3. **minor** — `custom_swings_supertrend.py:169-181`: hidden overload — `generate_signals(df, params_dict)` supported by runtime isinstance sniffing of the first positional arg; signature lies to type-checkers and IDE users.

**(b) Security** — nothing found.

**(c) Baseline smells**
4. **minor — Speculative Generality (perf)**: pure-Python per-bar loops for ATR/Supertrend/trade management over potentially millions of 1m bars — the hot loop the vectorbt engine was supposed to avoid; document expected frame sizes or vectorize.

**(d)** Honest docstring about intentionally-not-ported Pine features — good.

---

## strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py ✅

**(a) Bugs / correctness**
1. **major** — `qg_30min_es_breakout_strategy.py:170-171`: entry comparisons look inverted for a breakout strategy: `enter_long = adx < 30 AND close < breakout_long` (long entered when close is *below* the upper breakout level `low + fract·|open−low|`), and `enter_short = adx > 30 AND close < breakout_short` — the short side also requires close *below* its level and has no above-level counterpart. Either a faithful port of unconventional logic or a sign/comparison error introduced in conversion; nothing in-file reconciles it with the Pine source. Verify before trusting any produced backtest.
2. **minor** — `qg_30min_es_breakout_strategy.py:160`: exit reason hardcoded `"time_exit_1455_ny"` while the actual forced-exit default is 14:30 — mislabeled telemetry.
3. **minor** — `qg_30min_es_breakout_strategy.py:197`: `indicators_dict["sl_price_at_exit"] = ("exit", sl_price_at_exit)` — a **tuple**, breaking the otherwise uniform "indicator value is a Series" contract every consumer and sibling strategy follows.
4. **minor** — `qg_30min_es_breakout_strategy.py:170-171`: ADX threshold `30.0` is the strategy's pivotal switch yet is not exposed as a parameter.

**(b) Security** — nothing found.

**(c) Baseline smells**
5. **minor — Magic Number**: `fract=3.3`, ADX 30 — undocumented tuning provenance.

**(d)** Per-bar Python loop again (perf hot spot on 1m ES data).

---

## strategies/volatility_breakout/volatility_breakout.py ✅

**(a) Bugs / correctness**
1. **minor** — `volatility_breakout.py:133-138`: when `atr.iloc[i]` is NaN, `stop_distance = 0.0` → `active_stop = close` exactly; the very next bar's `low <= close` is near-guaranteed → instant exit at cost. Regime gating makes this rare (NaN regime compares False), but the guard belongs at the stop, not incidentally in the filter.
2. **minor** — `volatility_breakout.py:74-82`: `session_open` computed via O(n) `.iloc` writes into a Series copy (chained-assignment pattern + slow); vectorize with groupby-transform like the sibling does.
3. **minor** — `volatility_breakout.py:89` vs sibling `intraday_volatility_breakout.py:367`: entry-window end is inclusive here (`<=`) and exclusive there (`<`); regime direction is `atr_short >= atr_long·f` here vs `< threshold` ("low" mode) there — the twin strategies silently disagree on both conventions.

**(b) Security** — nothing found.

**(c) Baseline smells**
4. **minor — Duplicated Code**: see area theme; this file is the derivative twin.

**(d)** None further.

---

## strategies/mean_reversion_rsi_bb/mean_reversion_rsi_bb.py ✅
1. **minor** — `mean_reversion_rsi_bb.py:52-58`: Cutler RSI (SMA of gains) — different smoothing than Wilder RSI used in `post_vix_dip_buy._calc_rsi` (also Cutler) vs Pine `ta.rsi` (Wilder) conversions elsewhere; thresholds tuned on one convention transfer badly to the other. Convention is nowhere recorded per strategy.
2. **minor** — `mean_reversion_rsi_bb.py:78-81`: when ATR is NaN at entry, no stop is armed (`active_stop` stays NaN) — the trade runs with time/RSI exits only; silent unbounded-risk path rather than skip-or-fallback.
**(b)** Nothing found. **(c)** Duplication theme. **(d)** None.

---

## strategies/gold_momentum/gold_momentum.py ✅
Same two patterns as mean_reversion_rsi_bb: Cutler-RSI convention drift (48-54) and no-stop-when-ATR-NaN entry (81-84). 2 minor. Otherwise clean; MACD cross definitions correct.

---

## strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py ✅
1. **major** — `pine_superosc_vumanchu.py:146`: returns a **3-tuple** `(entries, exits, indicators_dict)` — violates the 4-value `(entries, exits, indicators, drawings)` contract every other native strategy and the adapters follow; any generic caller unpacking four names raises ValueError at runtime, discovered only when this strategy is selected.
2. **minor** — `pine_superosc_vumanchu.py:90`: WaveTrend oversold gate `wt2 <= -60` hardcoded, inconsistent with the parameterized style of the rest of the file.
**(c)** Supertrend-oscillator loop duplicates the family's third TR/numpy-loop implementation. **(d)** None.

---

## strategies/pine_supertrend_dema/pine_supertrend_dema.py ✅
1. **major** — `pine_supertrend_dema.py:124`: returns a **3-tuple** (no drawings) — same contract violation as pine_superosc_vumanchu.
2. **minor** — `pine_supertrend_dema.py:43-44`: band-carry conditions reference the *opposite* band (`lower_band[i] = max(up, lower[i-1]) if close[i-1] > upper_band[i-1]`) whereas `custom_swings_supertrend` carries each band against itself — the repo now contains two mutually inconsistent Supertrend implementations, at least one deviating from standard Pine `ta.supertrend`. One of them is wrong; nothing arbitrates.
**(c)** Naming collision hazard: `lower_band` here holds the support band while `up_band` does in custom_swings (Mysterious Name across modules). **(d)** None.

---

## strategies/post_vix_dip_buy/post_vix_dip_buy.py ✅
1. **minor** — `post_vix_dip_buy.py:89-92`: when VIX data is absent the filter is *disablingly satisfied* (`vix_aligned = vix_threshold + 1`) — the strategy silently becomes plain RSI-dip-buying depending on whatever parquet happens to exist under DATA_DIR; exposure profile changes with environment (disclosed only via indicator columns).
2. **minor** — `post_vix_dip_buy.py:83-88`: row-wise `vix.loc[vix.index.date == current_date]` scan — O(n·m); `merge_asof` on dates is the fix. Noticeable on decade-scale 1m SPY frames.
3. **minor** — `post_vix_dip_buy.py:28`: `_load_vix_data(start, end)` parameters accepted and ignored.
**(b)** Nothing found. **(c)** Duplication theme. **(d)** Filesystem-coupled strategy (hidden data dependency) worth documenting in STRATEGY_META.

---

## strategies/trend_following/trend_following.py ✅
1. **minor** — `trend_following.py:56,95`: `trailing_stop_value` uses `0.0` as both "no stop" sentinel and a legitimate possible value (never for real prices, but the sentinel conflates state); exit-price attribution would record 0.0 if the cross-down path fired before any ATR-armed stop — currently unreachable, one refactor away from a divide-by-zero-looking artifact.
Otherwise scanned clean (beyond area-wide duplication).

---

## strategies/short_term_reversal/short_term_reversal.py ◻ scanned — standard template, correct RSI/time-exit logic, no file-specific defects found (area-wide duplication theme applies).

## strategies/risk_parity/risk_parity.py ✅
1. **minor** — `risk_parity.py:63`: `should_rebalance = i % rebalance == 0` phases off the *absolute frame index* — identical parameters rebalance on different calendar days depending on how much warmup history the caller passes; results not reproducible across data windows.
Honest docstring notes it's a simplified single-symbol stand-in. Nothing else.

## strategies/sector_rotation/sector_rotation.py ✅
1. **minor** — `sector_rotation.py:37,38,60`: SMA window 200 and −3% weakness threshold hardcoded (not in params) while the loop warmup also hardcodes 200 — parameter/config drift between metadata promises and implementation.
Otherwise clean.

## strategies/cross_asset_momentum/cross_asset_momentum.py ✅
1. **minor** — `cross_asset_momentum.py:37-38`: `zip(lookbacks, weights)` silently truncates on unequal list lengths, and `fillna(0)` lets pre-warmup zeros dilute the composite — combined effect: mis-specified params degrade quietly instead of failing.
Otherwise clean.

## strategies/stat_arb_pairs/stat_arb_pairs.py ◻ scanned — z-score-of-cumulative-log-return proxy is documented as simplified; params=None handled correctly; no file-specific defects (duplication theme applies).

## strategies/channel_breakout/channel_breakout.py ✅
1. **minor** — `channel_breakout.py:50`: `tr.iloc[0] = ...` positional chained write — pandas SettingWithCopy/COW warning territory; assign via `.loc` on construction.
2. **minor** — `channel_breakout.py:62`: warmup always includes `ema_period` even when `use_ema_filter=False` — harmless over-conservatism, but couples unrelated config.
Otherwise clean.

## strategies/lab_generated_strategy/lab_generated_strategy.py ✅
1. **minor** — `lab_generated_strategy.py:1-16`: `STRATEGY_META` placed *before* imports (module executes assignments pre-import block) — style-only, but breaks every linter convention and the AST updater in strategy_registry expects top-level assigns (still satisfied).
Explicit `sl_price: None` contract noted and fine. Otherwise clean.

## strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py ✅
1. **major** — `ema_crossoiver_conversion_test.py:58`: returns a **3-tuple** — same contract violation as the two pine_* strategies (third instance; generic runners crash on selection).
2. **minor — Mysterious Name**: directory/display name carry the typo "crossoiver"; slug-based tooling (registry aliases, matrix dirs) inherits it permanently — rename before more artifacts accrue.
`sl_price` emitted for all bars (not just entries) — harmless given entry-keyed consumption.

## strategies/example_sma/example_sma.py ✅
1. **minor — Mysterious Name**: named `example_sma` / tagged "moving_average" but computes **EMA** (`ewm(span=...)`) — the canonical teaching example teaches the wrong thing.
2. **minor** — `example_sma.py:2`: version `"1.0"` vs semver `"1.0.0"` used everywhere else.
Otherwise clean.

## strategies/pynecore_orb/pynecore_orb.py ◻ scanned — PyneCore-annotated source; can only be executed by the pynecore engine; meta and inputs well-formed. Nothing found.
## strategies/pynecore_orb/pynecore_orb_context.py ✅
1. **minor** — `pynecore_orb_context.py:37-42`: SL/TP context series are anchored to *every bar's open* (`open_price ± atr·mult`) — meaningful only when sampled exactly at the entry bar; the pairing contract with the @pyne strategy (which anchors to `position_avg_price`) is implicit and undocumented, so the context can silently disagree with the strategy's actual fills.
## strategies/pynecore_sma_crossover/pynecore_sma_crossover.py ◻ scanned — clean.
## strategies/pynecore_sma_crossover/pynecore_sma_crossover_context.py ◻ scanned — clean; consistent open-anchored caveat as above applies but is single-consumer here.

## strategies/__init__.py ◻ scanned — empty, nothing found.
## strategies/intraday_volatility_breakout/__init__.py ◻ scanned — whitespace-only, nothing found.

---

# Group 7 — Root config files

## pyproject.toml ✅
1. **minor** — `pyproject.toml:56-57`: wheel packages ship `shared, runner, viewer, data_tools, strategies, risk, signal_bots` but **omit `scheduler`** — a scheduler service installed from the wheel cannot import its own package; fine while everything runs from the checkout, a packaging trap once deployed.
2. **minor** — `pyproject.toml:64`: ruff select `["E","F","I","W"]` — no flake8-bugbear (B), no security-oriented rulesets; several issues found in this review (bare-broad excepts, mutable-state races) are exactly what B/S rules surface. Tooling-gap judgment call, not a violation.
3. **minor** — `pyproject.toml:66-67` + `pytest.ini:2`: **two competing pytest configurations** (both set testpaths); pytest.ini silently wins — dual source of truth invites drift.

## pytest.ini ✅
1. **minor** — `pytest.ini:9`: global `timeout = 180` applies to e2e/integration marks too; browser-suite runs near the limit get killed without per-mark overrides. Also `base_url` hardcodes port 5556 (env-specific). Covered jointly with pyproject finding 3 (config duplication).

## live_deployments.yaml ✅
1. **minor (security)** — `live_deployments.yaml:2-13`: real (paper) Alpaca account IDs committed in the tracked file while `live_deployments.example.yaml` exists precisely for templating — account numbers aren't secrets, but the tracked/example split exists and is being violated; policy question for the repo owner.
2. **minor** — `live_deployments.yaml:31,54,77`: `channel_id: "${DISCORD_BOT_NITRO_CHANNEL_ID}"` — YAML has no native env interpolation; unless the loader performs manual substitution, the literal `${...}` string becomes the channel id and Discord routing silently matches nothing. Loader behavior unverifiable from this area; flagging the undocumented dependency.
3. **minor** — `live_deployments.yaml:83` vs `signal_bots/registry.py:94`: the rocketfuel deployment sets `allow_short: true`, but registry maps processor id `rocketfuel` → the **longsonly** factory (SELL = exit-only). The processor ignores `allow_short` entirely, so the setting is dead config promising behavior the bot cannot deliver — operator-facing contradiction.

---

# Summary

Every tracked file in the area is accounted for above (findings reported, or explicitly marked scanned-clean). Headline items:

- **critical (1)**: `scripts/regenerate_backtests.py` destructively purges all backtest storage with no confirmation/backup and reconstructs on guessed symbol/timeframe mappings.
- **Top majors**: fake "weekly data refresh" scheduled job reporting completed work (scheduler/jobs/weekly_data_refresh.py); mock discovery feeding the real pipeline (overnight_strategy_discovery.py); three strategy files violating the 4-value return contract; TOCTOU lock race in matrix_queue_runner.py; nitro/stockkit registry mis-mapping; environment-dependent Supertrend ATR in ramjet processor; asymmetric position cleanup in live_smoke cTrader worker; risk limits rebuilt from untrusted signal metadata (risk/live.py); DATA_DIR resolution divergence across five copies.
- **Dominant systemic smell**: Duplicated Code — DATA_DIR/env resolution (5×), row-unwrapping (6×), TZ-normalization (4×), BacktestResult↔ValidationReference conversion (4×), signal-bot market-data helpers (2× full files), RSI/TR/drawings blocks (10+ strategies). A `shared/` consolidation pass would remove the majority of the drift-class bugs above at the root.

## Finding counts by severity

| Severity | Count |
|---|---|
| Critical | 1 |
| Major | 36 |
| Minor | 233 |
| **Total** | **270** |

Scanned-clean files (nothing found): shared/__init__.py · scheduler/__init__.py · scheduler/jobs/__init__.py · signal_bots/__init__.py · signal_bots/ramjet/__init__.py · signal_bots/rocketfuel/__init__.py · signal_bots/nitro/__init__.py · strategies/__init__.py · strategies/intraday_volatility_breakout/__init__.py · strategies/short_term_reversal.py · strategies/stat_arb_pairs.py · strategies/pynecore_orb.py · strategies/pynecore_sma_crossover.py · strategies/pynecore_sma_crossover_context.py · scripts/compare_strategy_metrics.py



