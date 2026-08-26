# Code Review — misc-backend (Reviewer r1)

Area: risk engine, scheduler, operational scripts, shared infrastructure, signal bots, trading strategies.
Scope: all tracked code files listed for this area + root-level `pyproject.toml`, `pytest.ini`, `live_deployments.yaml`.
Method: blind, independent review of source only; findings cite `file:line` with severity critical/major/minor. Smells are Fowler baseline names; repo standards override where documented, and every smell is a judgement call.

Status: COMPLETE — all 79 tracked files reviewed (findings per file below, or explicit "Scanned — nothing found").

---

## Group 1 — shared/ infrastructure

### shared/database.py (3,891 lines)

(a) Bugs / correctness risks

- **major — `shared/database.py:70-77`: module-level `ENGINE` created at import time.** Importing `shared.database` immediately opens a SQLite engine pointed at `DATA_DIR/backtests.db`, and `load_dotenv()` runs at import (line 61). Any tool that imports this module for an unrelated helper (several scripts do) pays the cost and inherits the resolved data dir; test isolation depends on env vars being set *before* import. Side-effectful import; makes the module order-dependent to configure.
- **major — `shared/database.py:739-745`: `subprocess.run(["git", ...])` without `timeout=`** (checklist: subprocess without timeout). If git hangs (locked index, network-mounted FS), every caller of `upsert_verification_evidence` blocks indefinitely inside what looks like a DB write.
- **minor — `shared/database.py:324-326` vs `:693-694`: two competing "now" conventions.** `_utcnow_naive()` returns tz-naive UTC for DB rows while `_now_iso()` writes tz-aware ISO strings into text columns (`created_at`/`updated_at` on evidence/task tables are strings). Mixed naive/aware datetime handling is exactly the tz-naive-datetime trap; `ServiceModel.last_activity` accepts arbitrary caller datetimes (line 533, 1862) with no normalization, so a local-time caller poisons ordering queries like `list_services` (ordered by `created_at.desc()`).
- **minor — `shared/database.py:1280-1282`: duplicate statement.** `execution_column_names = _table_column_names(...)` executed twice back-to-back; harmless but shows copy-paste editing.
- **minor — `shared/database.py:2297-2302`: `deployment_ids = {normalized_account_id}` seeds the set with the *account id*, then also adds each deployment's `strategy_name`. The subsequent `IN (...)` query over `LiveSignalModel.deployment_id` therefore matches signals whose `deployment_id` equals an account id or a bare strategy name — a fuzzy legacy-compat match that can silently attribute another deployment's executions to this account's stats if ids ever collide.
- **minor — `shared/database.py:1066-1091` + grep verification:** `_backfill_curve_for_id` is defined but never called anywhere in the repo (dead code — Speculative Generality).
- **minor — `shared/database.py:3696-3713`: `save_portfolio` deletes all components then re-inserts them starting at `index=1` while `_portfolio_component_id` formats `component-{index:03d}` — the docstring at 3655 says "positional default" but callers see 1-based ids while `load_portfolio` (3735) enumerates `start=1` against 0-based list positions; correct today, but the off-by-one surface is fragile.
- **minor — `shared/database.py:3843` vs `:3889`: `get_grid_results(limit)` interpolates `limit` via SQLAlchemy param binding (safe), but no upper bound is applied here unlike every other list function's `_bounded_query_limit`; a caller passing 10**9 loads the table.

(b) Security

- **minor — SQL string building:** the raw-SQL sections are actually done well — every value is bound (`:id_0` placeholders built at 3331-3336; f-string interpolation only over *generated placeholder names* and quoted identifiers at 883-885, 939, 946). One caveat: `_rebuild_live_signals_without_global_discord_unique` executes `text(rebuilt_sql)` (line 893) where `rebuilt_sql` comes from `sqlite_master.sql` — i.e., DDL read out of the database itself. Exploitable only by someone who can already write the schema, so informational, but it is string-built SQL execution.
- **minor — `shared/database.py:1082, 1225`: paths built from DB ids (`DATA_DIR/"backtests"/f"{bt_id}.trades.json"`). Line 1262 guards new callers with `Path(str(bt_id)).name == str(bt_id)` (good traversal guard), but `_backfill_curve_for_id` (dead code) performs the same join *without* the guard — if it is ever revived it becomes a path-traversal read.

(c) Baseline smells

- **Divergent Change / God module:** one 3,891-line module mixes at least six reasons to change: SQLite engine+PRAGMAs, schema migrations (845-1308), live signal/execution/balance/account persistence (1310-2030), service registry (1768-1882), background tasks + verification evidence (2668-3233), backtest/portfolio/grid CRUD (2319-3891). Any new table drags edits through this single file (Shotgun Surgery risk for everyone else).
- **Duplicated Code:** the row-unwrapping helpers `_normalize_task_record` (805-817), `_coerce_backtest_row_id` (2475-2488), `_unwrap_equity_point_row` (3270-3283), `_unwrap_model_row` (3481-3490) and the inline unwrapping at 2640-2645 are five implementations of the same "_mapping/tuple/model → model" shape. Similarly `_bounded_query_limit` (2051-2057) duplicates the inline try/int/min pattern at 1581-1584, 1619-1622, 2820-2823.
- **Repeated Switches:** signal-source normalization logic (`{"internal","discord"}` membership juggling) is re-implemented three times: 593-599 (`from_live_deployment`), 624-640 (`to_live_deployment`), 1906-1923 (`_resolve_deployment_signal_source`). A change to the allowed-source set requires three coordinated edits.
- **Primitive Obsession:** statuses/types/ids carried as bare `str` throughout (`status: str = Field(default="ACTIVE")` line 571; task statuses compared against the `_FINAL_TASK_STATUSES` string set at 95-102) despite `ServiceStatus`/`VerificationEvidenceStatus` enums existing; the stringly-typed dual convention forces the defensive `.strip().lower()` calls repeated ~30 times.
- **Speculative Generality:** `_backfill_curve_for_id` (1066) unused (see above); `save_grid_result_to_sqlite` vs `save_grid_result` (3798 vs 3860) are parallel APIs for the same row shape.

(d) Maintainability hot spots

- `_migrate_schema` (1294-1308) is a hand-rolled migration runner whose steps silently depend on execution order (backfill before uniqueness at 1024-1027); there is no migration versioning, so a fresh DB and a legacy DB exercise different code paths on every boot.
- The equity-preview CTE builder `_equity_preview_query_params` (3327-3418) embeds two large raw SQL strings; any schema change to `equity_points` must be mirrored here by hand.

### shared/constants.py

(a) Bugs / correctness risks

- **major — `shared/constants.py:106-141`: `DATA_DIR` resolution probes the filesystem and picks the highest-scoring candidate when `DATA_DIR` is `~`/`~/...`.** Which directory wins depends on `$HOME`, `pwd` entries, `SUDO_USER`, and whether `ohlcv/` etc. exist at process start. Two processes running under different users (e.g., a systemd service vs. an operator shell) can resolve *different* data directories — and therefore different `backtests.db` files containing live executions — with no warning. For a trading platform this is a real split-brain correctness risk.
- **minor — `shared/constants.py:35-36`: import-time `load_dotenv`** plus filesystem stat calls (74) at import; importing constants mutates `os.environ`.

(b) Security — nothing found (path handling is read-only resolution; `resolve_executable` uses `shutil.which`, no injection surface).

(c) Baseline smells

- **Mysterious Name (borderline):** `_home_candidates`/`_data_dir_score` encode a heuristic policy that the names don't convey; the scoring weights (100/30/20/10) are magic numbers (91-103).
- **Primitive Obsession:** timeframes remain bare strings with a tuple whitelist + alias map (14-21) rather than a small `Timeframe` type; every consumer re-validates.

(d) Maintainability hot spots — `_resolve_data_dir` + helpers (~100 lines) dominate the module and are the only part with branching complexity; everything else is flat constants.

### shared/logging.py

(a) Bugs / correctness risks

- **minor — race condition on shared state:** `get_logger` (`shared/logging.py:74-99`) is check-then-act on the module-global `_loggers` dict with no lock; concurrent first calls for the same service name attach duplicate handlers → doubled log lines. Checklist item (race conditions on shared state); low likelihood since most calls happen at import.
- **minor — `shared/logging.py:27`: `LOG_LEVEL` frozen at import time** from the environment; later `os.environ["LOG_LEVEL"]` changes (tests, long-lived schedulers reconfiguring) are ignored, inconsistently with `LOG_DIR` which is also import-frozen via constants.
- **minor — multi-process rotation:** `RotatingFileHandler` (85-90) is unsafe across processes; scheduler jobs, scripts and the viewer all write `<service>.log` in the same `LOG_DIR`, and concurrent rotation truncates/loses lines. No `delay=True` either, so every logger creation touches the filesystem.

(b) Security — nothing found. JSON formatter escapes via `json.dumps`; `formatException` output is embedded safely.

(c) Baseline smells

- **Duplicated Code (latent):** formatter construction repeated inline in `get_logger`; trivial today.

(d) Maintainability hot spots — none beyond the above.

### shared/models.py

(a) Bugs / correctness risks

- **minor — `shared/models.py:427-429`: `Trade.direction_sign` returns `-1` for *any* value that isn't exactly `"LONG"`** — `"long"`, `"Long"`, or a corrupted value are all silently treated as SHORT while PnL math flips sign. `direction: str` is never validated against `VALID_DIRECTIONS` anywhere in this module (Primitive Obsession consequence). In a platform that computes position sizing from trade direction this deserves an enum.
- **minor — `shared/models.py:340` vs `:423-425`: contradictory contract.** Comment says `exits` has "at least 1 element", yet `last_exit_time` defensively falls back to `entry_time`. One of the two is wrong; downstream code relying on the invariant will meet a fallback that hides data corruption instead of raising.
- **minor — `shared/models.py:744`: `datetime.fromisoformat(...replace("Z", "+00:00"))`** handles UTC-Z strings, but naive timestamps parse fine too and then feed `strftime` for filenames — no tz normalization, consistent with the repo's mixed naive/aware habit (checklist item).

(b) Security — nothing found (pure data classes; JSON round-trip via stdlib).

(c) Baseline smells

- **Duplicated Code:** `BacktestStats.from_dict` (620-625) and `PortfolioStats.from_dict` (667-672) are byte-for-byte identical bodies; likewise the `to_dict` "include optional fields if set" loops (`Trade.to_dict` 358-390, `StrategyMeta.to_dict` 52-62, `BacktestMeta.to_dict` 491-515) repeat one shape five times across the file.
- **Duplicated Code (cross-module):** `VALID_TIMEFRAMES`/`VALID_DIRECTIONS` re-declared here (283-284) despite living in `shared/constants.py:14-15` — two sources of truth for the same tuple (Shotgun Surgery: adding a timeframe requires editing both, and consumers import inconsistently).
- **Divergent Change:** single module mixes four domains — strategy metadata, live-trading API models, portfolio persistence, backtest canonical format. Live models change for broker reasons; backtest models change for engine reasons.
- **Inconsistent layering (judgement call):** Pydantic `BaseModel` for live/portfolio models but plain dataclasses with hand-written `to_dict`/`from_dict` for backtest models — two serialization idioms in one file; the hand-written ones are where the duplication above lives.

(d) Maintainability hot spots

- `Trade`/`TradeExit`/`BacktestResult` are the canonical interchange format for every strategy, script and the viewer; any field addition fans out to `to_dict`, `from_dict`, SQLModel records in `database.py`, and viewer code — the highest fan-out type in the area.

### shared/sentry.py

(a) Bugs / correctness risks

- **minor — `shared/sentry.py:138-142`: check-then-act on `_INITIALIZED` without a lock** — same race shape as logging; two threads initializing concurrently can both pass the guard (sentry_sdk.init itself is idempotent-ish, so impact is low). `_INITIALIZED_SERVICE` is written but never read outside tests (Speculative Generality, judgement call given `_reset_for_tests`).

(b) Security

- Good hygiene overall: `before_send` redaction walks the whole event (199-222), sensitive-key markers (23-35), bearer/query/assignment regexes, and `_detect_release` correctly bounds its git subprocess with `timeout=2` (267-274) — note this is the pattern `database.py:739` should copy.
- **minor — `shared/sentry.py:36-43`: redaction regexes miss `client_secret`, `private_key`, `access_key`;** marker list is substring-based so dict keys containing those would still be caught, but raw-string occurrences of those names in messages would not be. Judgement-call gap, not a hole in what's listed.

(c) Baseline smells — nothing significant. Small focused module; frozen dataclass config is good practice.

(d) Maintainability hot spots — none.

### shared/strategy_registry.py

(a) Bugs / correctness risks

- **major — `shared/strategy_registry.py:100-107`: `load_strategy_module` executes arbitrary strategy files with `spec.loader.exec_module(module)`** and `iter_strategy_entries` (176-192) does this for *every* strategy file on every call — `find_strategy_entry` (220-226) loops over all entries, each triggering full module execution including module-level side effects (network calls, file writes, MT5 connections seen elsewhere in this area's strategies). Any code path that resolves a strategy by name can therefore run N unrelated strategy modules' top-level code. Combined with `update_strategy_display_name` writing files, a poisoned `strategies/` dir becomes arbitrary code execution for anything that imports this helper. Severity major rather than critical because the strategies directory is developer-controlled, but there is no allowlist, no AST-only meta parse (the module imports `ast` at line 5 but only uses it for display-name rewriting).
- **minor — `shared/strategy_registry.py:41-45`: `created_at` derived from file mtime** — filesystem-dependent provenance; a checkout or copy rewrites "creation" times used presumably for ordering/display.
- **minor — `shared/strategy_registry.py:239-267`: `update_strategy_display_name` splices source text by AST offsets and writes the whole file back non-atomically** (`write_text` directly, no temp+rename); a crash mid-write corrupts the strategy file. Also `zip(node.value.keys, node.value.values, strict=False)` tolerates `{**a, "name": ...}`-style dicts whose "name" key isn't a Constant and silently falls to `raise ValueError` — acceptable, but the mismatch between tolerant parse and hard failure is worth noting.
- **minor — `shared/strategy_registry.py:56`: heuristic `path.parent.name != path.stem`** means a strategy placed flat in `strategies/` root or in a differently-named subfolder is invisible to the registry — undocumented convention enforced implicitly.

(b) Security

- See (a): dynamic execution of directory contents is the main exposure; no path traversal risk since paths come from rglob within the repo.

(c) Baseline smells

- **Feature Envy (borderline):** `update_strategy_display_name` reaches into AST internals of another file to edit it; a small "STRATEGY_META reader/writer" owned by a dedicated module would gather this (currently split between here and whatever else parses STRATEGY_META).
- **Repeated Switches:** `StrategyType` detection (118-144) tries four metadata spellings (`strategy_type`, `type`, tags variants) with token normalization — a schema for STRATEGY_META would delete the guessing ladder.

(d) Maintainability hot spots — `load_strategy_module` is called repeatedly for the same files with no caching; registry scans execute all strategy modules each time (`O(N) exec_module` per lookup).

### shared/__init__.py
Scanned — empty file, nothing found.

---

## Group 2 — risk/

### risk/engine.py

(a) Bugs / correctness risks

- **major — `risk/engine.py:216-218`: no upper bound on computed position size.** `raw_size = risk_amount / (risk_distance * pip)` is only floored at 0 (`max(0.0, raw_size)`); there is no notional/leverage cap against `account_balance`. A signal whose stop sits a fraction away from entry (or a mis-specified `pip_value`) produces an effectively unlimited position, and `max_total_exposure` does *not* bound notional — it bounds `risk_percent` only. For a risk engine this is the single most important missing invariant.
- **major — `risk/engine.py:130, 151-152`: exposure and position-count limits trust caller-supplied metadata.** `current_positions` comes from `signal.metadata.get("current_positions")` and `current_exposure` is a parameter fed (in `live.py:114`) from `metadata.get("current_exposure")`. The engine never queries actual portfolio state, so stale/omitted metadata silently disables both the `max_positions` and `max_total_exposure` guards (defaults 0 → both checks pass). Race-condition-shaped: concurrent signals each see the same stale counts and all pass.
- **minor — `risk/engine.py:176-184`: mutates the input `TradeSignal` (`signal.stop_loss = stop_loss`, `signal.metadata["resolved_stop_loss"] = ...`)**, same in `apply_trailing_stop` (271, 275). Side-effectful input mutation makes retries non-idempotent and surprises callers holding the original signal.
- **minor — `risk/engine.py:48-60`: `_signal_id` builds a deterministic "legacy" id from symbol+timeframe+action+prices;** two genuinely distinct entries with identical parameters collapse to one id — any downstream dedupe keyed on this rejects the second legitimate entry.
- **minor — `risk/engine.py:262-264`: `apply_trailing_stop` raises `ValueError` mid-loop** if a persisted `trailing_sl_config.percent` is out of range; a single bad config row kills the whole trailing pass instead of skipping the signal.

(b) Security — nothing found (pure computation, no IO).

(c) Baseline smells

- **Repeated Switches:** the `{"buy", "long"}` membership test appears 4× (27, 43, 186-via-_stop_matches_direction, 268); action should be normalized once to a direction enum on `TradeSignal`.
- **Data Clumps:** `position_size/risk_amount/risk_percent/risk_reward_ratio/decision` travel together through every private helper signature (94-111, 131-149…); they already have a name — `SizedOrder` — the helpers just re-plumb the fields individually.
- **Speculative Generality (borderline):** `@dataclass(slots=True)` on `RiskEngine` (10-15) immediately overridden by a hand-written `__init__`; the decorator adds nothing but confusion about which `__init__` runs.

(d) Maintainability hot spots — `calculate_position_size` (113-242) mixes five concerns (limits, stop resolution, validation, sizing, decision assembly) in one 130-line method; the three rejection branches duplicate ~15 lines each.

### risk/config.py
Scanned — clean, well-validated module. One judgement-call note: **no cross-field check that `max_risk_per_trade <= max_total_exposure`** (`risk/config.py:8-12`); the engine papers over it with `min()` at `engine.py:152`, but a config where per-trade risk exceeds total exposure is silently accepted. Minor.

### risk/live.py

(a) Bugs / correctness risks

- **major — `risk/live.py:45-85`: the explicit-qty path bypasses every risk limit.** When `metadata.qty`/`explicit_qty > 0`, the function constructs a `SizedOrder` directly (69-75) — no `max_positions`, no `max_total_exposure`, no `max_risk_per_trade`, no balance check, and `enforce_stop_direction` never consulted because the engine is never called. Any upstream component (or Discord-sourced signal payload) that sets `qty` opts out of the entire risk engine. This contradicts the module's purpose and is the highest-severity finding in the package.
- **minor — `risk/live.py:113`: `pip_value` silently defaults to `1.0`.** For instruments where a point ≠ 1 unit of quote currency, missing metadata yields quietly wrong sizing (compounds the uncapped-size issue in `engine.py`).
- **minor — `risk/live.py:76-84, 123-132`: writes sizing results back into the shared `LiveSignal.metadata`** — same input-mutation concern as the engine; also means the persisted signal record changes depending on whether sizing ran.

(b) Security — see the (a) bypass above; treat as authorization/invariant weakness reachable from signal payloads.

(c) Baseline smells

- **Duplicated Code:** the `TradeSignal(...)` construction block appears twice nearly verbatim (52-68 and 98-111); extract one builder.
- **Primitive Obsession:** all configuration flows through the untyped `metadata` dict string keys (`max_risk_per_trade`, `pip_value`, …) — a typed `SizingContext` would delete the six `_to_float` defensive coercions.

(d) Maintainability hot spots — none beyond duplication above.

### risk/models.py

(a) Bugs / correctness risks

- **minor — `risk/models.py:100-115`: `SizedOrder.__post_init__` coerces but never validates finiteness,** unlike `RiskDecision` (82-93). A `NaN`/`inf` position_size flows through unchecked here; only the incidental NaN-comparison behavior of `max()` in the engine saves the main path.
- **minor — `risk/models.py:37, 47`: `action` accepts four spellings (`buy/sell/long/short`) normalized to lowercase but kept as four aliases** — downstream code must handle both families forever (see Repeated Switches in engine).

(b) Security — nothing found.

(c) Baseline smells

- **Primitive Obsession:** `action` as free string with runtime normalization instead of a `Direction` enum; `rejection_reason` likewise bare string with callers using ad-hoc literals ("max_positions", "bad_stop_direction").
- **Refused Bequest (borderline):** none serious — models are small and cohesive.

(d) Maintainability hot spots — none.

### risk/__init__.py
Scanned — re-export surface only, correct `__all__`, nothing found.

---

## Group 3 — scheduler/

### scheduler/scheduler.py

(a) Bugs / correctness risks

- **major — `scheduler/scheduler.py:19-21` vs `scheduler/jobs/signal_bot_health_check.py`: the health-check job is never registered in `_SCHEDULED_JOB_HANDLERS`.** The handler map contains only `weekly_data_refresh`, yet `signal_bot_health_check.run(task_id=..., service_id=..., service_name=...)` exists with exactly the expected signature, has tests, and is documented in `docs/signal_bots.md:61`. Any DB service row configured with `job_type="signal_bot_health_check"` is marked FAILED by `sync_services` (line 79-91, "Unknown scheduled service job_type"). Verified by grep across the repo — no other registration site. Dead wiring; the feature cannot fire from the scheduler.
- **minor — race window between check and act:** `sync_services` reads services via a thread (35-39/51-55), then mutates APScheduler state on the event loop; two concurrent `sync_services` calls (e.g., an API-triggered resync racing startup) can both pass the `get_job(...) is not None` checks and fight over `add_job`/`remove_job`. Single-event-loop execution makes actual corruption unlikely (`max_instances=1` helps), so minor.
- **minor — `scheduler/scheduler.py:44`: `shutdown(wait=False)`** abandons any currently-running job coroutine without cancellation or awaiting; a job mid-DB-write at shutdown can leave its background-task record stuck in "running" (the `mark_background_tasks_interrupted` recovery in database.py only runs if something calls it).
- **minor — `_run_service_job` re-marks the service RUNNING after success (226-231).** A completed one-shot job therefore leaves the service showing "running" forever unless something later flips it; combined with `update_service_status(FAILED)` on config errors this is a status machine with no terminal state.
- **minor — `raise` at 279 inside an APScheduler job callback**: the exception propagates into APScheduler's error handling, which logs it — but because the code already recorded failure + sentry capture above, this double-reports the same error through three channels.

(b) Security — nothing found (cron expressions come from the local DB, parsed by `CronTrigger.from_crontab` which validates).

(c) Baseline smells

- **Duplicated Code:** the three `upsert_background_task_record(...)` call sites in `_run_service_job` (174-186, 213-225, 260-272) repeat the same 10 kwargs with only status/config differing — extract one `_record_task(status, config, message)` helper. Same shape recurs in both job modules below.
- **Divergent Change (mild):** `ServiceScheduler` mixes three concerns — registry sync, job dispatch, and task-record bookkeeping.

(d) Maintainability hot spots — adding a new scheduled job requires touching `_SCHEDULED_JOB_HANDLERS`, creating a job module duplicating the task-record boilerplate, *and* knowing that nothing validates the registration (the signal-bot gap proves it); a per-module self-registration decorator would collapse all three.

### scheduler/jobs/signal_bot_health_check.py

(a) Bugs / correctness risks

- **major — dead wiring (see scheduler.py finding):** correct implementation, unreachable via the scheduler; only its own test invokes it.
- **minor — `scheduler/jobs/signal_bot_health_check.py:40`: `check_signal_bots()` runs synchronously on the event loop thread** — every other blocking call in this package is wrapped in `asyncio.to_thread`; this one is not, so a slow filesystem scan stalls the scheduler loop for all jobs.

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code:** the description string "Validates signal processor registry..." is repeated verbatim three times (34, 76, 98); the upsert kwargs blocks likewise.

(d) Maintainability hot spots — none beyond above.

### scheduler/jobs/weekly_data_refresh.py

(a) Bugs / correctness risks

- **major — `weekly_data_refresh.py:34-79`: the job refreshes nothing.** Despite the name and description ("Refreshes core symbols for the last 7 days..."), the loop only writes progress records to SQLite and `await asyncio.sleep(0)` — there is no data download, no API call, no file write. Either placeholder shipped as production or the real fetch was removed; either way a cron entry produces fake "completed" progress for real symbols. Misleading operational telemetry is itself a correctness hazard: dashboards will show weekly data refresh succeeding while no data lands.
- **minor — hardcoded symbol/timeframe lists (11-12)** ignore any service config; the `config_json` written back echoes the defaults regardless of what the service row requested.

(b) Security — nothing found.

(c) Baseline smells

- **Speculative Generality / premature scaffolding:** the elaborate progress-reporting apparatus (percent rounding, nested `progress_detail`) around a no-op body.
- **Duplicated Code:** same upsert boilerplate family as above.

(d) Maintainability hot spots — none beyond above.

### scheduler/__init__.py, scheduler/jobs/__init__.py
Scanned — re-export surfaces, nothing found. Note `scheduler/jobs/__init__.py` exports only `run_weekly_data_refresh`; the health-check job is absent here too, reinforcing the wiring gap.

---

## Group 4 — signal_bots/

### signal_bots/registry.py

(a) Bugs / correctness risks

- **major — `signal_bots/registry.py:32-35`: `_build_nitro` loads RamJet's config file.** `merged = dict(_load_yaml(_ROOT / "ramjet" / "config.yaml"))` — the Nitro factory merges `ramjet/config.yaml`, not a nitro config (there is no `nitro/` directory; verified by directory listing). Today it is formula-identical by design ("RamJet-equivalent"), but any future divergence between the bots silently reuses the wrong config, and the health check at `signal_bots/health_check.py:40-41` *encodes the same special case* (`elif name in {"stockkit", "nitro"}: ... ramjet/config.yaml`) — two places must now agree that "nitro means ramjet's file".
- **minor — `signal_bots/registry.py:92`: `"stockkit": _build_ramjet`.** The StockKit option builds a RamJet processor; the label promises "StockKit-compatible processor" while instantiating RamJet wholesale. Same family of aliasing hazard as above.
- **minor — `signal_bots/registry.py:105-110`: `get_signal_processor` returns untyped `object`;** every caller must downcast. The factories' declared return types are the concrete classes — return type should be a shared `SignalProcessor` protocol.

(b) Security — nothing found (YAML via `yaml.safe_load`, good).

(c) Baseline smells

- **Duplicated Code:** `_build_ramjet`/`_build_nitro` (26-35) and `_build_rocketfuel_longsonly`/`_build_rocketfuel_longandshort` (38-59) differ only in class and variant literal; one parameterized factory would collapse all four.
- **Data Clumps / Primitive Obsession:** bot identity is three parallel hand-maintained structures — `SIGNAL_PROCESSOR_OPTIONS` labels (62-88), `SIGNAL_PROCESSORS` factories (91-98), and per-bot config paths scattered through builders + health check. Adding a bot touches all of them (the stockkit/nitro aliases prove the drift already started).

(d) Maintainability hot spots — the registry is the single point where bot wiring can rot silently; there's no consistency test tying OPTIONS ids to factory behavior to config paths.

### signal_bots/health_check.py

(a) Bugs / correctness risks

- **minor — `signal_bots/health_check.py:37-41`: config-path derivation duplicates registry logic with different rules.** Line 37 computes `parent / name.split("_")[0] / "config.yaml"` (works only by coincidence for ramjet/rocketfuel), then immediately overrides for rocketfuel variants and stockkit/nitro. If a bot id ever contains an underscore whose prefix isn't a package (e.g., a hypothetical `ram jet_pro`), line 37 invents a wrong path; the override ladder then hides it.
- **minor — the check never instantiates processors,** so import-time breakage in a processor module is caught only because `registry.py` imports them at module load — a failure surfaces as an exception outside the per-bot result structure (whole check errors rather than one bot flagged).

(b) Security — nothing found.

(c) Baseline smells — **Repeated Switches:** the name→config-path mapping (37-41) is a switch duplicated from the registry's builder map; belongs as data on the option entries.

(d) Maintainability hot spots — none beyond above.

### signal_bots/ramjet/processor.py

(a) Bugs / correctness risks

- **major — `signal_bots/ramjet/processor.py:143-158`: hand-rolled Supertrend loop indexes NumPy arrays starting from `first_valid_idx` while `final_upper[i-1]` may still be NaN** when the first valid bar is index 0 and `basic_upper[0]` was NaN — guarded here by seeding at `first_valid_idx`, OK — but the real defect: `supertrend` is seeded `np.ones(...)` = True (bullish) at rows *before* `first_valid_idx`, so early bars report a green Supertrend computed from no data. Downstream `supertrend_green_at_entry` metadata (line 201) and the flip-based trailing model consume this flag; on a short history the entry decision can be based on uninitialized trend state.
- **major — ATR mismatch between the two code paths:** the `ta`-library path (102-113) uses whatever ATR the library computes internally for its Supertrend, but the fallback path uses `ewm(alpha=1/period)` (line 122) for Supertrend while SL/TP sizing always uses `ewm(span=period)` ATR (93). EWM `span` vs `alpha` parameterizations produce different series (`alpha=2/(span+1)` vs `alpha=1/period`). Whether the deployed environment has the `ta` package installed therefore changes trailing-stop geometry, silently. "Formula-identical port" claims (docstring 33-40) hold only on one path.
- **minor — Python-level loop over all bars (134-158)** makes this O(n) interpreted work per signal inside what looks like a hot live path; fine for 5m candles, a scaling hazard if reused intraday-tick.
- **minor — `signal_bots/ramjet/processor.py:102`: `hasattr(frame, "ta")` duck-type probe:** presence of a `ta` attribute flips numeric behavior between deployments (same finding as above, mechanism stated once here).
- **minor — `process_signal` derives entry price from `frame["close"].iloc[-1]` (194)** regardless of the signal's own `signal_price`; a Discord alert price and computed entry can diverge without warning.

(b) Security — nothing found.

(c) Baseline smells

- **Duplicated Code (cross-module):** `_prepare_market_data`, `_calculate_atr`, `_resolve_timeframe`, `_TIMEFRAME_ALIASES`, and the timestamp/signal_price metadata block (234-237) are copy-pasted verbatim into `rocketfuel/processor.py` — extract a shared `signal_bots/_indicators.py`.
- **Speculative Generality:** the `ta`-library branch exists alongside a full fallback implementation — two Supertrend implementations in one method; pick one.

(d) Maintainability hot spots — `_calculate_supertrend` (98-166): two implementations, manual array indexing, and column-name string matching (`startswith("SUPERTd")`) against an external library's naming convention that changes between `ta` versions — fragile coupling.

### signal_bots/nitro/processor.py

(a) Bugs / correctness risks

- **minor — `signal_bots/nitro/processor.py:24-37`: rebuilds a `TradeSignal` field-by-field to change two metadata keys;** any future field added to `TradeSignal` (e.g. new validation in `__post_init__`) gets re-run here on already-normalized values — harmless today, but `dataclasses.replace(signal, metadata=metadata)` would be correct and immune.
- **minor — inherits the entire RamJet defect set** (Supertrend seeding, dual-ATR paths) since Nitro subclasses RamJet and shares formulas by design.

(b) Security — nothing found.

(c) Baseline smells

- **Middle Man (borderline):** Nitro adds only metadata relabeling over RamJet — justified by intent ("strict BUY-only intake") but its `_normalize_buy_action` (13-17) is byte-identical to the inherited one it overrides; the override does nothing except exist. Delete or differentiate.
- **Duplicated Code:** see processor rebuild note.

(d) Maintainability hot spots — none beyond inheritance coupling noted above.

### signal_bots/rocketfuel/processor.py

(a) Bugs / correctness risks

- **minor — `signal_bots/rocketfuel/processor.py:124-130`: long-only SELL handling sets `stop_loss=None` + `metadata["exit_only"]=True` but still emits `action="sell"` as a fresh `TradeSignal`.** Correctness depends entirely on downstream consumers honoring the metadata hint; nothing in this area enforces it (risk engine will happily size a short with no stop — `stop_loss=None` triggers the percent fallback at `risk/engine.py:22-26` and opens an actual short position). Cross-module contract enforced only by convention.
- **minor — same verbatim duplication defects as RamJet** (data prep, ATR, timeframe aliases).

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code:** ~60 lines identical to `ramjet/processor.py` (66-100 vs ramjet 66-95); the module boundary exists but the shared shape doesn't.

(d) Maintainability hot spots — none beyond above.

### signal_bots/__init__.py
Scanned — re-export surface, ordering of imports slightly unusual (registry before rocketfuel) but correct; nothing found.

### signal_bots/{nitro,ramjet,rocketfuel}/__init__.py
Scanned — single-class re-export surfaces, correct `__all__`, nothing found.

---

## Group 5 — scripts/

### scripts/live_smoke.py

(a) Bugs / correctness risks

- **minor — `scripts/live_smoke.py:1444-1449`: hand-rolled `class contextlib_suppress` shadows the stdlib idiom and swallows *everything*.** Its `__exit__` returns `True` unconditionally, so the wrapped cancel at 767-769 also suppresses `KeyboardInterrupt`/`asyncio.CancelledError` raised during cancellation — exactly when you want cleanup to abort loudly. `contextlib.suppress(Exception)` exists; this class is both reinvention and subtly broader.
- **minor — `scripts/live_smoke.py:1562, 1578, 1592`: `os._exit(...)` in worker modes** skips atexit handlers, logging flush, and aiohttp/Twisted finalizers by design (reactor hang avoidance), but any buffered diagnostics queued behind the JSON line are lost. Acceptable trade-off, undocumented.
- **minor — `scripts/live_smoke.py:352-356, 451`: deep reach into adapter privates** (`adapter._ProtoOAReconcileReq`, `._send_request`, `._symbol_name_to_id`, `._ProtoOAPositionStatus`, `._trading_client`) from a *script*. Every private rename in `live_runner.ctrader_adapter` breaks smoke tests at runtime, not import time (Inappropriate Intimacy / Message Chains).
- **minor — `scripts/live_smoke.py:950-955`: `_as_aware_datetime` silently substitutes `datetime.now()` for any unparseable clock value,** masking broker-clock anomalies instead of failing the premarket eligibility check that depends on them.
- **minor — `scripts/live_smoke.py:1466` and `:1551`: mode aliasing applied twice** (in `run_smoke` and again in `main`) — harmless today; two normalization sites will drift.
- **minor — `scripts/live_smoke.py:1271-1279`: two `except Exception: pass` blocks** in `_cancel_stockkit_buy_order` discard cancel failures *and* position-query failures while reporting `remaining_qty` from the latter — a failed flatten can be reported with stale/missing detail.
- Positive notes: subprocess calls correctly carry `timeout=` (216-224, 260-268, 298-306); Alpaca checks refuse non-paper accounts (791-792, 1099-1100); the e2e worker best-effort-flattens on any failure (589-593); the close-status comparison fixes a real substring-matching bug (743-747).

(b) Security

- **minor — `scripts/live_smoke.py:86-102, 1608-1611`: smoke reports containing raw broker error strings and account balances/equity are written under `reports/` and repo root (`live_integration_report.json`).** Broker exceptions sometimes embed account/credential fragments; these artifacts are long-lived plaintext next to source. Suggest redaction before write (the `shared/sentry.py` regex kit would drop in).

(c) Baseline smells

- **Duplicated Code:** `_run_ctrader_adapter_subprocess` / `_run_ctrader_order_subprocess` / `_run_ctrader_strategy_e2e_subprocess` (198-315) are the same function with different argv; likewise the three worker-dispatch blocks in `main` (1552-1595) differ only in coroutine and args. One parameterized runner deletes ~80 lines.
- **Mysterious Name:** `contextlib_suppress` invites confusion with `contextlib.suppress`.
- **Long Function / Divergent Change:** 1,620-line single module mixing CLI parsing, three worker modes, four broker flows, market-hours logic, and report IO; each concern changes for a different reason.
- **Middle Man:** `_await_alpaca_terminal_status` (646-660) discards half of what `_wait_for_alpaca_fill` computes purely to reuse its loop.

(d) Maintainability hot spots — the worker-in-subprocess architecture means the same file is both parent CLI and child worker selected by hidden `--mode` values; adding a worker touches argparse choices, dispatch ladder, subprocess builder, and worker body — four coordinated edits.

### scripts/adaptive_harvester.py

(a) Bugs / correctness risks

- **major — `scripts/adaptive_harvester.py:583-594, 726`: months with missing hours are still marked `"done"`.** Any hourly file that ultimately fails (weekday 404, timeout after retries, connection drop) is dropped from the parquet without failing the month — the month is marked done whenever ≥1 file succeeded and the frame is non-empty. A transient network outage mid-month therefore bakes permanent holes into tick history that `_pending_months` (623-635) will never revisit. For a market-data pipeline, silent gap-preservation is the worst failure mode; there is no completeness check comparing `successful_files` against expected non-weekend files.
- **major — `scripts/adaptive_harvester.py:35-44`: DATA_DIR/LOG_DIR resolution re-implemented differently from `shared/constants.py`.** The constants module scores home-directory candidates when `DATA_DIR=~`; the harvester does a plain `expanduser().resolve()`, and defaults LOG_DIR to `Path.home()/"logs"` where constants default to `PROJECT_ROOT/"logs"`. Harvested tick parquets and `harvester_state.json` can land in a different tree than the platform reads — split-brain data layout, the same hazard flagged under shared/constants.py.
- **minor — `scripts/adaptive_harvester.py:524-526`: dead divide-by-zero guard in the wrong order.** `avg_latency_s = sum(...)/len(outcomes)` executes *before* the `if not outcomes` check that was clearly meant to prevent it; an empty batch would crash rather than reach `float("inf")`. Unreachable today because batches are never empty, which is itself why the bug survived.
- **minor — `scripts/adaptive_harvester.py:205-216, 299-330`: state and profile files are read-modify-write with no locking;** two harvester processes (or a harvester plus anything editing profiles) last-writer-wins whole files, losing months-state or tuning profiles.
- **minor — `scripts/adaptive_harvester.py:264-271` + `:447`: every payload is LZMA-decompressed twice** — once for validity in `_looks_invalid_bi5`, once for parsing in `_assemble_tick_dataframe`. Pure waste on the largest objects in the pipeline.
- **minor — `scripts/adaptive_harvester.py:455-461`: column mapping relies on `parse_ticks` tuple order implicitly** (constructed order `ask,bid,...` then re-indexed to `bid,ask,...`); no assertion ties the two, and a reorder upstream silently swaps bid/ask columns.

(b) Security

- **minor — fetches are plain HTTP GETs to Dukascopy URLs built by external code (`generate_tick_urls`) and the payloads are binary-decompressed without size caps** (`response.read()` unbounded, line 343). A compromised/hostile CDN response can balloon memory. Low likelihood, but `response.content.read(n)` with a cap would close it.

(c) Baseline smells

- **Duplicated Code:** DATA_DIR/LOG_DIR/env resolution (32-48) duplicates `shared/constants.py` instead of importing it (root cause of the divergence finding).
- **Shotgun Surgery:** backoff/retry policy embedded in `_fetch_url` (333-432) while AIMD tuning lives in `_download_month` and persistence policy in `_run` — rate-limit behavior spans three functions.
- **Long Function:** `_download_month` (~155 lines) mixes HTTP batching, tuning state machine, day-progress accounting, DataFrame assembly, and parquet output.

(d) Maintainability hot spots — the resumability model ("done" flags + state file) has no concept of partial months; fixing the gap-preservation bug requires touching state schema, `_pending_months`, and `_ensure_job_state` together.

### scripts/analyze_mt5_parity.py

(a) Bugs / correctness risks

- **minor — `scripts/analyze_mt5_parity.py:42-46`: `MT5_REPORTS` paths are CWD-relative** (`Path("strategy-comparisons/...")`). Every other location in the file is anchored via `PROJECT_ROOT` (17-19); run from any directory other than repo root, all seven subcommands fail with FileNotFoundError. Same class of issue: outputs resolved through `COMPARISON_CASES["output"]` (`data/backtests/...`) are also relative.
- **minor — `scripts/analyze_mt5_parity.py:419`: `mt5_atr = mt5_stop_distance / 0.25`** bakes an assumed ATR multiple (0.25) into a diagnostic whose entire purpose is to *discover* the strategy's ATR relationship; if the strategy constant changes, this diagnostic silently mislabels every row.
- **minor — `scripts/analyze_mt5_parity.py:436`: truthiness test `if row["our_atr"] and row["mt5_atr"]`** treats a legitimate `0.0` ATR as missing (ratio suppressed) — harmless here, but the same idiom elsewhere would drop real data.
- **minor — `scripts/analyze_mt5_parity.py:360-663`: each subcommand re-instantiates `MT5XLSXImporter` and reloads the same XLSX files** independently; `stop-atr` and `signal-counts` re-run full `generate_signals` over years of 1m bars per label per offset — minutes-long invocations for diagnostics with no caching.

(b) Security — nothing found (read-only analysis).

(c) Baseline smells

- **Inappropriate Intimacy / Duplicated Code:** imports private helpers `_load_ohlcv`, `_opposite_side_series`, plus module constants from the sibling script (24-29). The OHLCV loader belongs in a shared module; two parity tools now mutate each other's internals.
- **Long Function:** `matched_stop_path` (506-580) does matching, statistics, classification, and IO in one body.
- **Duplicated Code:** the importer/timezone preamble (`importer = MT5XLSXImporter()` + load with Athens tz) repeats in all seven subcommands (361, 383, 396, 469, 508, 584, 603).

(d) Maintainability hot spots — the tool's value depends on agreement among three sources of truth (COMPARISON_CASES here-via-import, MT5_REPORTS labels, the strategy's own parameters); nothing asserts they stay consistent.

### scripts/regenerate_mt5_parity_artifacts.py

(a) Bugs / correctness risks

- **major — `scripts/regenerate_mt5_parity_artifacts.py:172-175`: multi-source bar merge can synthesize Frankenstein candles.** `groupby("timestamp").first()` skips NaNs *per column*, so when several candidate files cover the same timestamp, the winning row can take `open` from bid-file-A and `close` from ask-file-B (whichever sorts first non-null per column after the priority sort). For a parity harness whose output artifacts are treated as reference results, mixed-source bars corrupt both signals and fills subtly.
- **major — `scripts/regenerate_mt5_parity_artifacts.py:379-386, 477-484`: `--risk-pct` is accepted and passed but never used.** `regenerate_case(..., risk_pct=...)` ignores its own parameter; sizing always comes from `fixed_risk_amount` (264, 326). Operators tuning risk via the advertised CLI flag get byte-identical artifacts with a false sense of control.
- **minor — `scripts/regenerate_mt5_parity_artifacts.py:324`: when a signal has no valid stop, sizing falls back to `entry_price * 0.02` as risk distance** — a silent, undocumented sizing regime switch inside the trade builder.
- **minor — `scripts/regenerate_mt5_parity_artifacts.py:482`: conversion series window hardcoded to `"2018-01-01"–"2026-06-23"`, duplicated from COMPARISON_CASES strings (37, 48, 59)** — extending a case past 2026-06-23 leaves PnL conversion using the last available EURUSD rate forever (searchsorted clamps at 209-211).
- **minor — `scripts/regenerate_mt5_parity_artifacts.py:276-279`: exit pairing advances a global cursor past exits that occur before/at the fill bar;** correct for a strict single-position model, but two entries sharing one exit bar consume the exit once and strand the second entry without warning (it just ends the loop via `break` at 279, dropping remaining entries silently).

(b) Security — nothing found (reads local parquet/xlsx, writes JSON under `data/backtests/`).

(c) Baseline smells

- **Mysterious Name:** `_side_price` vs `_opposite_side_price` vs `_opposite_side_series` — three near-identical helpers whose names don't convey ask/bid selection semantics (218-239).
- **Shotgun Surgery:** contract sizes, lot steps, and case metadata live in parallel dicts keyed by symbol (63-73); adding an instrument requires synchronized edits in three structures plus the case tuple.
- **Long Function:** `_build_trades` (249-367, ~120 lines) interleaves signal alignment, stop replay, unit conversion, sizing, and rounding.

(d) Maintainability hot spots — this module is simultaneously library (imported by analyze_mt5_parity), artifact generator, and CLI; changes for any one consumer ripple into the parity reference artifacts.

### scripts/run_risk_parity_backtest.py

(a) Bugs / correctness risks

- **major — `scripts/run_risk_parity_backtest.py:408-414`: per-ticker NaN returns silently poison the equity curve.** `daily_ret = sum(active_weights[t] * returns[t].loc[date])` — `pct_change().dropna()` only drops rows where *all* tickers are NaN; any individual missing ticker (common in young ETF history or yfinance gaps) makes `daily_ret` NaN, and the NaN propagates through every subsequent `portfolio_value.loc[date]`, ending in NaN total_return/CAGR/Sharpe written to the validation reference. No per-ticker fillna guard anywhere in the compounding loop.
- **minor — `scripts/run_risk_parity_backtest.py:273-282` vs `:404-414`: two independent portfolio-value ledgers.** `advance_portfolio_value` compounds using *post-threshold* `current_weights`, while the final series replays `rebalance_schedule` weights; threshold-skipped adjustments and the open-vs-close execution split (`execution_price` 284-290) guarantee the two ledgers diverge, yet stats blend `net_profit` (series ledger) with `realized_trade_net_profit` (trade ledger) without reconciling the gap.
- **minor — `scripts/run_risk_parity_backtest.py:52-60`: `yf.download` failure modes unhandled** — an empty/rate-limited response yields an empty frame whose failure surfaces later as a cryptic IndexError at `prices.index[0]`; no timeout, retry, or explicit empty-check (network call inside backtest logic also makes `run_backtest` non-reproducible for tests unless prices passed).
- **minor — `scripts/run_risk_parity_backtest.py:310`: SPY SMA window `iloc[max(0, spy_idx - 199):spy_idx + 1]`** silently computes a *shorter* average during the first 199 bars instead of skipping the hedge signal — hedge regime behavior differs between early and late periods with no marker in output metadata.
- **minor — `scripts/run_risk_parity_backtest.py:484-487`: missing exit_price proxied with entry_price** ("use same-day close") fabricates a zero-PnL exit in the ValidationReference rather than flagging the trade as malformed.

(b) Security — nothing found beyond the unauthenticated-network dependency above (yfinance).

(c) Baseline smells

- **Long Function / Divergent Change:** `run_backtest` (231-475, ~245 lines) mixes optimizer math, regime classification, hedge rules, trade simulation, statistics, and report assembly — six reasons to change in one closure-heavy body (`advance_portfolio_value`/`execution_price` capture mutable outer state via `nonlocal`, a mini state machine hidden in nesting).
- **Duplicated Code:** strategy settings declared twice — constants at 250-255 and again as literals in `build_validation_reference` (516-523); changing `max_leverage` requires editing both plus the per-signal metadata block (333-337).
- **Magic Numbers:** regime thresholds 1.5/1.2 (126-129), hedge factors 0.3/0.5/1.3/1.5 (215-228), SMA buffer 0.98 (313) — undocumented policy constants inline.
- **Primitive Obsession:** trades built as ad-hoc dicts then hand-mapped into models (376-397, 482-509); a small dataclass would delete the mapping layer.

(d) Maintainability hot spots — positioned as a *validation reference generator* (output feeds parity checks), yet its numbers depend on live yfinance responses; reproducibility of reference artifacts is not guaranteed run-to-run.

### scripts/regenerate_qc_risk_parity_artifact.py

(a) Bugs / correctness risks

- **major — `scripts/regenerate_qc_risk_parity_artifact.py:57-58`: `tz_convert(None)` strips UTC and yields tz-naive timestamps.** The local price frames are built with UTC-aware indexes, then converted to naive; downstream `_to_iso` (62-68) re-localizes as UTC so output stays correct, but any comparison against tz-aware frames inside `run_backtest` (e.g., `rebalance_dates > prices.index[warm_up_days]` comparisons in the imported module) now straddles naive/aware conventions — checklist tz-naive-datetime item. Works today only because both sides go through the same coercion.
- **minor — `scripts/regenerate_qc_risk_parity_artifact.py:158-163`: monkeypatches `risk_parity.download_prices` at module level.** Not thread-safe (concurrent regeneration + a yfinance-path backtest would race on the swapped function) and fragile to renames — a decorator/dependency-injection parameter on `run_backtest(prices=...)` already exists (it accepts `prices`!) making the patch unnecessary: line 161 could simply pass `prices=prices, open_prices=open_prices`.
- **minor — `scripts/regenerate_qc_risk_parity_artifact.py:126`: `total_bars=int(prices.count().sum())`** counts cells across all tickers (11 × rows), not bars — metadata field is inflated ~11× versus every other artifact's meaning.
- **minor — `scripts/regenerate_qc_risk_parity_artifact.py:112-113`: `stats.setdefault("total_pnl", ...); stats["net_profit"] = stats.get("net_profit")`** — the second statement re-assigns a key to itself (no-op), suggesting an unfinished normalization.

(b) Security — nothing found.

(c) Baseline smells

- **Duplicated Code:** settings block 136-142 duplicates the same six constants from `run_risk_parity_backtest.py` (third copy in the repo for these numbers).
- **Inappropriate Intimacy:** reaches into sibling module's globals to patch behavior (see above).

(d) Maintainability hot spots — thin wrapper overall; the monkeypatch is the main fragility.

### scripts/matrix_queue_runner.py

(a) Bugs / correctness risks

- **major — `scripts/matrix_queue_runner.py:80-96`: lock acquisition is check-then-write without atomicity (race condition on shared state).** `acquire_lock` tests `LOCK_PATH.exists()` then writes the lockfile in a separate step; two cron sessions firing within the TTL-check window can both observe "no lock" and both proceed. The standard fix is `os.open(path, O_CREAT|O_EXCL)` or an flock — neither used despite the docstring promising "A lock file guarantees that only one session runs at a time."
- **major — `scripts/matrix_queue_runner.py:66-77`: stale-lock liveness probe via `pgrep -f "python.*runner\.cli matrix"` is unreliable** — it matches *any* process whose full cmdline contains that substring (including this script's own children, editors with the file open, grep itself historically). Combined with `LOCK_PATH.unlink()` at line 89, a false positive deletes a live lock and lets a second queue start concurrently.
- **minor — `scripts/matrix_queue_runner.py:227-249`: `while True` loop with no exit condition other than queue exhaustion** — a strategy whose runner always exits 0 but never produces a valid `grid_summary.json` re-queues forever (`refresh_strategy_states` marks it pending/partial each pass), turning the cron-invoked tool into an infinite CPU-bound daemon holding the lock for LOCK_TTL.
- **minor — `scripts/matrix_queue_runner.py:46-63`: state file read-modify-write, non-atomic write (`write_text` directly)** — unlike adaptive_harvester's tmp+rename, a crash mid-write corrupts queue state JSON permanently (load_state then silently resets it, losing progress bookkeeping).
- **minor — `scripts/matrix_queue_runner.py:191-198`: subprocess timeout is `int(TIMEOUT_SECONDS)+120` but `subprocess.run(..., stdout=PIPE)` with no output size bound;** a chatty child can balloon memory before the timeout lands.

(b) Security

- **minor — `scripts/matrix_queue_runner.py:68-74`: `pgrep -f` with a regex over process cmdlines** — process names are attacker-influenceable on shared hosts; matching by substring for an authorization decision ("is another runner alive") is spoofable, though impact is limited to queue duplication.

(c) Baseline smells

- **Duplicated Code:** status classification logic ("done if failed==0 else failed") appears in `refresh_strategy_states` (123), `run_strategy` (203), and `render_state` emoji ladder (273).
- **Long Function:** `main`'s while-loop body duplicates `next_pending_strategy`'s refresh call (229-230 vs 162).

(d) Maintainability hot spots — the queue's correctness rests entirely on filesystem conventions (lock TTL, summary presence, trades-file globbing); none of them are asserted anywhere.

### scripts/generate_matrix_grid_overview.py

(a) Bugs / correctness risks

- **major — `scripts/generate_matrix_grid_overview.py:9-11, 143`: module-level script body executes on import.** The entire ETL pipeline runs between import statements — importing this module (e.g., from a test or another tool) reads `/mnt/block-volume-data/.../matrix_run_report.json` and rewrites `frontend/public/matrix-grid-overview.json`. No `if __name__ == "__main__"` guard exists anywhere in the file.
- **minor — `generate_matrix_grid_overview.py:30-31`: `datetime.fromisoformat(started_at.replace('+00:00', 'Z'))` then subtracting two datetimes:** if either string lacks timezone info the subtraction mixes naive/aware and raises TypeError — caught by the blanket `except Exception` (33) which silently nulls durations. Also note `.replace('+00:00','Z')` only handles one suffix form.
- **minor — `generate_matrix_grid_overview.py:37-42`: hand-rolled Abramowitz–Stegun normal CDF** with absolute error ~1.5e-7 feeding PSR values displayed to 4 decimals — precision theater; `statistics.NormalDist().cdf` exists in stdlib since 3.8.
- **minor — `generate_matrix_grid_overview.py:59`: `as_float(meta.get('initial_capital'), None) or as_float(capital, 100000.0) or 1.0`** — `or`-chaining means a legitimate initial capital of 0 falls through to defaults; minor because zero capital is nonsensical, but the idiom recurs throughout the file.
- **minor — `generate_matrix_grid_overview.py:242`: `datetime.utcnow()`** — deprecated since Python 3.12, naive datetime mislabeled with a 'Z' suffix; the exact tz-naive trap the checklist names.

(b) Security — nothing found (reads/writes fixed local paths).

(c) Baseline smells

- **Speculative Generality:** `compute_psr_from_trades` recomputes PSR per run from raw trades even though grid rows may already carry psr fields from the runner — dual sources of the same statistic.
- **Long Script / Divergent Change:** parsing, math, aggregation, sorting, and presentation all inline at module top level; nothing reusable, nothing testable.

(d) Maintainability hot spots — hardcoded absolute paths (`/mnt/block-volume-data`, `/home/ubuntu/Desktop/...`) make the script machine-specific; no parameterization at all.

### scripts/seed_demo_backtests.py
Scanned — demo-data seeder; straightforward and low-risk. Findings:
- **minor — `scripts/seed_demo_backtests.py:25-245`: `main()` writes demo parquets *and* SQLite rows unconditionally with no `--force`/idempotency note;** `save_result_to_sqlite` merges by id so re-runs are safe, but `mk_ohlcv` silently overwrites real OHLCV files at `DATA_DIR/ohlcv/yfinance/equity/AAPL/1h.parquet` (line 20) if a developer points DATA_DIR at production data. A guard or distinct `demo/` path segment would remove the foot-gun.
- **minor — hardcoded 2024 timestamps throughout** — demo data ages but stays valid; acceptable for fixtures.
No security issues, no baseline smells beyond the above.

### scripts/run_discovery_grid_backtests.py

(a) Bugs / correctness risks

- **major — `scripts/run_discovery_grid_backtests.py:22`: discovery input pinned to an absolute, timestamped file** `/mnt/block-volume-data/discovered_strategies/discovery_20260618_0752.json` with no CLI override. The script is unusable on any other machine or after the next discovery run overwrites/moves that file; "latest discovery" in the docstring is enforced by nothing.
- **minor — `run_discovery_grid_backtests.py:43-55`: `wait_for_data` polls up to 30 minutes for a data file that nothing in this script produces** — it depends on an external downloader running concurrently; combined with `time.sleep(10)` blocking loop there's no cancellation path.
- **minor — `run_discovery_grid_backtests.py:151-159`: `subprocess.run(..., timeout=900)` will raise TimeoutExpired which is *uncaught*** — one hung backtest aborts the whole grid mid-run, losing summary output for completed trials (results list is only persisted at the end, line 205).
- **minor — `run_discovery_grid_backtests.py:196-199`: summary `started_at == finished_at`** (both computed after the loop) — cosmetic telemetry bug.

(b) Security — nothing found (local runner invocation, args JSON-encoded).

(c) Baseline smells

- **Mysterious Name:** `params_for` maps discovered strategy names ("keltner") onto a Supertrend strategy file (27-31) — the mapping is surprising and undocumented why keltner→custom_swings_supertrend.
- **Duplicated Code:** trial result dict assembly duplicated between early-return (106-115) and final update (175-183).

(d) Maintainability hot spots — the STRATEGY_PATHS/params_for switch (27-31, 67-95) must grow in lockstep with the discovery prompt schema; nothing validates new discovered strategy names until runtime ValueError.

### scripts/regenerate_backtests.py

(a) Bugs / correctness risks

- **major — `scripts/regenerate_backtests.py:201-227`: `main()` destroys all backtest data with no confirmation or dry-run.** `_purge_sqlite_backtests()` deletes every row of five tables and `_purge_json_backtests` unlinks every `*.trades.json` before regenerating; a typo'd DATA_DIR (or the demo-seeder's habit of pointing DATA_DIR at shared trees) wipes production backtest history irrecoverably. Destructive bulk delete + no `--yes` flag is the riskiest operational shape in the scripts group.
- **minor — `regenerate_backtests.py:38-43`: third independent re-implementation of DATA_DIR resolution** (same divergence-from-shared/constants pattern as adaptive_harvester).
- **minor — `regenerate_backtests.py:91-123`: `_candidate_symbols` maps asset-class tags to hardcoded symbol lists** — a strategy tagged "crypto" silently gets EURUSD/XAUUSD/SPY/AAPL (the fallback at 113-114) because no crypto branch exists; regenerated backtests then run on instruments unrelated to the strategy's intent.
- **minor — `regenerate_backtests.py:237-242`: skipped signal-processors are appended to `failures`** — telemetry conflates intentional skips with errors; summary counts "failures=len(failures)" inflated.
- **minor — `regenerate_backtests.py:297-305`: exception tuple omits `pynecore` engine errors that derive from BaseException subclasses** (e.g., custom compile errors) — an unexpected exception type aborts the whole regeneration loop after data was already purged, leaving an empty DB.

(b) Security — nothing found.

(c) Baseline smells

- **Divergent Change:** symbol/timeframe heuristics (49-147) are discovery policy embedded in a regeneration script — changes to data layout force edits here.
- **Long Function:** main() (~100 lines) mixes purge, index build, per-strategy dispatch, error taxonomy.

(d) Maintainability hot spots — purge-then-regenerate has no checkpointing: any crash between purge and completion leaves the platform with partial/no backtest history.

### scripts/recalculate_backtest_stats.py

(a) Bugs / correctness risks

- **minor — `scripts/recalculate_backtest_stats.py:142-147`: the `--data-dir` guard rejects any non-default value by design** — a user passing `--data-dir` that resolves equal to DATA_DIR works, but the flag is otherwise a trap: it errors after forcing callers to set env vars instead. The flag should not exist or should be honored; as written it's dead surface area (Speculative Generality).
- **minor — `recalculate_backtest_stats.py:68-83`: `_save_json_payload`'s PermissionError fallback writes a hidden `.name.rewrite` file then `os.replace`s** — good atomic-replace instinct, but the fallback triggers only on PermissionError on the *first* write; a failure mid-`write_text` leaves no recovery marker, and the rewrite path itself can hit the same permission problem one directory deeper.
- **minor — `recalculate_backtest_stats.py:44, 101`: change detection compares full merged dict equality** — correct, but stats containing float rounding noise from `compute_stats_for_result` will flap updated-counts run to run.

(b) Security — nothing found.

(c) Baseline smells

- **Duplicated Code:** `_recalculate_sqlite_backtests` and `_recalculate_json_backtests` are the same algorithm over two backends (25-55 vs 86-112); extract the merge-and-compare core.
- Positive note: has `--dry-run`, unlike its destructive sibling regenerate_backtests.py.

(d) Maintainability hot spots — none beyond duplication.

### scripts/overnight_strategy_discovery.py

(a) Bugs / correctness risks

- **major — `scripts/overnight_strategy_discovery.py:64-117`: the discovery loop discovers nothing.** Despite constructing a `BacktestConfig` (85-93), no engine ever runs — the result dict is hardcoded `"status": "mock"` with a comment "Real implementation would need actual strategy files / This is experimental skeleton". Downstream consumers exist: `run_discovery_grid_backtests.py` reads files from `data/discovered_strategies/` and executes real vectorbt backtests against them. A mock discovery file therefore seeds real compute with meaningless parameter sets — an integration hazard masquerading as a placeholder.
- **minor — `overnight_strategy_discovery.py:21-24**: `except ImportError: DATA_DIR = PROJECT_ROOT / "data"`** silently masks genuine import breakage in shared.constants (bare-except-family pattern); if shared.constants raises ImportError for a dependency reason, the script quietly writes discovery output to the wrong tree.
- **minor — `overnight_strategy_discovery.py:27`: module-level `mkdir(parents=True)`** side effect at import time.
- **minor — `overnight_strategy_discovery.py:114-115`: output filename uses minute-resolution `%Y%m%d_%H%M`;** two batches within the same minute overwrite each other.

(b) Security — nothing found.

(c) Baseline smells

- **Speculative Generality:** the entire BacktestConfig construction exists only to be discarded; either wire the engine or delete the block.
- **Mysterious Name:** `strategy_name=f"{strat_type}_cross"` with comment "# Simplified" — name promises a crossover strategy that doesn't exist.

(d) Maintainability hot spots — the contract between this generator and run_discovery_grid_backtests.py (trial schema: id/strategy/symbol/timeframe/params) is enforced nowhere; the "keltner"→supertrend surprise compounds here.

### scripts/download_discovery_data.py

(a) Bugs / correctness risks

- **minor — `scripts/download_discovery_data.py:22`: same pinned absolute discovery path as run_discovery_grid_backtests** (`/mnt/block-volume-data/discovered_strategies/discovery_20260618_0752.json`) — the two scripts must be edited in tandem (Shotgun Surgery).
- **minor — `download_discovery_data.py:33`: `TIMEFRAME_DEFAULT_START` hardcodes dates in 2026;** a later run silently fetches only ~3 months of 1h data regardless of discovery window.
- **minor — `download_discovery_data.py:99-106`: "4h" bars are resampled from Yahoo 60m data with `.resample("4h")`** anchored to midnight UTC — Yahoo's session boundaries and DST shifts can produce misaligned 4h candles (a 3-bar or 5-bar hour bucket at DST transitions); no bar-count integrity check afterwards.
- **minor — `download_discovery_data.py:116`: retry sleeps `min(30*attempt, 120)` even after the final attempt's failure** — actually the sleep happens inside the loop *before* re-check, so the last iteration still sleeps up to 120s before returning failure; wasted tail latency.

(b) Security — nothing found (public market data).

(c) Baseline smells

- **Duplicated Code:** timestamp-column detection loop (84-87) duplicates `_select_timestamp_column` logic from regenerate_backtests.py; column normalization block duplicated across three data scripts.
- **Divergent Change:** yfinance payload shape handling will need edits whenever Yahoo changes schema — isolated here, fine.

(d) Maintainability hot spots — none beyond above.

### scripts/verify_ctrader_order.py

(a) Bugs / correctness risks

- **minor — `scripts/verify_ctrader_order.py:35-36`: hardcoded default ORDER_ID/ACCOUNT_ID in source.** Defaults embedding what look like real demo account/order numbers invite accidental verification of the wrong order when env vars are unset; should require explicit values.
- **minor — `verify_ctrader_order.py:43-46`: reaches into adapter privates (`_ProtoOAOrderDetailsReq`, `_send_request_with_refresh_retry`)** — same Inappropriate Intimacy pattern as live_smoke.py.
- **minor — `verify_ctrader_order.py:24-26`: `asyncioreactor.install()` runs unconditionally at import**, before any guard; importing this module from tests pollutes the global reactor (install raises if already installed in some Twisted versions).
- **minor — `verify_ctrader_order.py:69-79`: `env["CTRADER_APP_ID"]` etc. raise bare KeyError with no message** if `.accounts.env` lacks a key — poor diagnostics for an ops tool whose whole purpose is diagnosis.

(b) Security

- **minor — writes broker deal details (order ids, execution prices, account activity) to `reports/ctrader_order_verification_*.json`** unencrypted alongside other reports; consistent with live_smoke finding, low sensitivity for demo accounts but habitual exposure for live ones.

(c) Baseline smells

- **Speculative Generality:** none; script is single-purpose.
- **Long Function:** acceptable size.

(d) Maintainability hot spots — none beyond privates coupling.

### scripts/dedupe_executions.py

(a) Bugs / correctness risks

- **minor — `scripts/dedupe_executions.py:88-93`: DELETE with expanding `IN :rowids` binds the full rowid list inline;** SQLite's default `SQLITE_MAX_VARIABLE_NUMBER` (999 on older builds, 32766 modern) caps this — a first-run dedupe on a legacy table with >32k duplicate rows fails with "too many SQL variables". Chunking would be safer.
- **minor — `dedupe_executions.py:26-40`: duplicate identity is `(signal_id, filled_qty, timestamp)`;** two genuinely distinct partial fills with identical qty within the same second (fast markets) are indistinguishable and one is deleted — inherent to the heuristic, worth a warning in output rather than silent confidence.
- **minor — non-dry-run delete is not wrapped in an explicit transaction with the group listing** — rows could theoretically change between listing and deletion under concurrent writers; WAL + busy_timeout make the window tiny but the SELECT/DELETE pair isn't in one IMMEDIATE transaction.

(b) Security — nothing found (bound parameters used correctly throughout — good example for the group).

(c) Baseline smells — none significant; clean focused tool with dry-run.

(d) Maintainability hot spots — none.

### scripts/consolidate_parquets.py

(a) Bugs / correctness risks

- **major — `scripts/consolidate_parquets.py:48-85`: destructive merge with no dry-run, no backup, and silent data loss on schema mismatch.** Fragments are concatenated (`sort=False`), deduped by timestamp keeping the *last* file read (line 70 — ordering by sorted path string, not by data quality/recency semantics), then **source fragments are unlinked** (84). If one fragment has extra/differently-typed columns or a corrupted row group, `pd.read_parquet` raises mid-loop after earlier groups were already merged-and-deleted — partial consolidation, unrecoverable fragments. Also `keep="last"` combined with `sorted(set(members))` means alphabetical file order decides which duplicate rows win — arbitrary.
- **minor — `consolidate_parquets.py:20-31`: `_normalize_timeframe` returns unrecognized stems unchanged** (line 31), so e.g. `2024_part1.parquet` consolidates into `2024.parquet` as if "2024" were a timeframe; the function's fallbacks ("1d" when stem==symbol) paper over two layout conventions.
- **minor — `consolidate_parquets.py:13-18`: fourth re-implementation of DATA_DIR resolution** (same pattern as harvester/regenerate_backtests).

(b) Security — nothing found.

(c) Baseline smells

- **Speculative Generality:** FRAGMENT_SUFFIX regex handles `part|chunk|fragment` suffixes but nothing in this repo generates those names (grep shows no producer) — dead convention handling.
- Positive note: pure-function split (`consolidate(root)`) is testable, unlike several siblings.

(d) Maintainability hot spots — the tool deletes source-of-truth files; it is the only script in the group without any guard rail (no dry-run, no trash dir).

### scripts/compare_strategy_metrics.py
Scanned — thin CLI wrapper over `validation.statistics.cross_platform.build_comparison_matrix`. Findings:
- **minor — `scripts/compare_strategy_metrics.py:22-40`: all four default paths are CWD-relative** (`strategy-comparisons`, `reports`, `data/backtests`); running outside repo root writes output to surprise locations or fails. Output filename also pins a date (`..._20260630.json`) as the default — stale-artifact generator.
No security issues; no baseline smells beyond the above (clean delegation to library code).

### scripts/generate_golden_files.py
Scanned — fixture generator for validation goldens. Findings:
- **minor — `scripts/generate_golden_files.py:10-16`: `PROJECT_ROOT` computed twice** (lines 10-11 and 16); harmless duplication signaling copy-paste assembly.
- **minor — `generate_golden_files.py:47-50`: missing report → `sys.exit(1)` mid-generation** leaves previously-generated fixtures updated and later ones stale — mixed-vintage golden set with no marker of partial failure.
- Note: golden fixtures regenerated from XLSX references silently overwrite test expectations (43-61); standard practice but worth flagging that regenerating goldens can launder real regressions into expected values.
No security issues; no smells beyond above.

### scripts/backtest_to_validation_ref.py

(a) Bugs / correctness risks

- **major — `scripts/backtest_to_validation_ref.py:18-23`: bypasses the canonical loader and hand-builds models with `Trade(**t)` / `TradeExit(**e)`.** Unlike `BacktestResult.from_dict`, this drops every optional-field tolerance: a persisted trade containing an unexpected key (any field added later, or `meta` payloads from engines) raises TypeError instead of converting; conversely required fields absent produce cryptic constructor errors. The canonical `BacktestResult.from_json` exists precisely for this (shared/models.py:732-738) and is not used.
- **minor — `backtest_to_validation_ref.py:27-30`: `equity_curve`/`indicators` passed through as raw dicts** rather than model instances while `trades` become models — asymmetric typing inside one result object; downstream `ValidationReference` tolerates it today.
- **minor — `backtest_to_validation_ref.py:46-47`: `res.meta.data["symbols"][0]` KeyError with bare message** when meta.data lacks symbols — the most common legacy-shape gap, unguarded.

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code:** the manual dict→model mapping duplicates `BacktestResult.from_dict`; delete 12 lines by using it.

(d) Maintainability hot spots — every new field on Trade/BacktestMeta must be manually remembered here; using the canonical deserializer removes the class of bug.

---

## Group 6 — strategies/

### strategies/intraday_volatility_breakout/intraday_volatility_breakout.py

(a) Bugs / correctness risks

- **major — `intraday_volatility_breakout.py:357-360`: bare `except Exception` silently switches signal semantics.** The confirmation-bar resample path (333-356) falls back to `close.shift(1)` on *any* exception — including genuine bugs like a malformed `_TF_FREQ_MAP` entry or a tz-aware index issue. The strategy then trades a *different* signal definition with no log, no marker in indicators, and identical output schema; parity artifacts generated from the fallback are indistinguishable from intended behavior.
- **major — `intraday_volatility_breakout.py:414-416, 429-431, 454-455`: entries/SL/reference shifted `-1` bars for candle-close mode** — the last bar of the frame gets `entries.shift(-1)` = False but `sl_price.shift(-1)` shifts the final row's stop into NaN and *drops* the first row's value off the front; any consumer aligning `sl_price` to the entry bar by position rather than by the shifted contract mis-sizes risk by one bar. The triple-shift coordination across three series is enforced only by convention inside one function.
- **minor — `intraday_volatility_breakout.py:216-219`: synthetic-day fallback (`range(len(df)) // 100`)** invents trading days every 100 bars when timestamps are missing — session windows, one-trade-per-day, and EOD exits then operate on fabricated calendar structure while results look normal.
- **minor — `intraday_volatility_breakout.py:189-205`: two parallel session-time converters** (`_to_session_time` for Series, `_index_to_session_time` for DatetimeIndex) duplicating the offset logic; if `session_timezone` is set AND `session_timezone_offset_hours` non-zero, the offset is silently ignored (early return via tz_convert success).
- **minor — `intraday_volatility_breakout.py:467-471`: Python loop building `signal_id` per entry bar** — O(entries) interpreted work plus `.loc[...]` per row; slow on grids over years of 1m data.

(b) Security — nothing found (pure computation).

(c) Baseline smells

- **Long Function:** `generate_signals` is 400+ lines with nested closures (`_to_session_time`, `_ffill_daily`, `_has_datetime_index`) closing over ~18 locals — effectively untestable in units.
- **Repeated Switches:** parameter extraction does dual-key lookup (`snake_case`, then `Pascal_Case` MQL5 name) 17 times (123-183); a single alias-resolution map would collapse it.
- **Duplicated Code:** three daily-resample branches each define their own `_ffill_daily` closure (255-256, 274-275, 286-287).

(d) Maintainability hot spots — this file is the parity reference for MT5 comparison tooling; its shift(-1) conventions and fallback paths are load-bearing for artifacts generated elsewhere, yet documented only inline.

### strategies/custom_swings_supertrend/custom_swings_supertrend.py

(a) Bugs / correctness risks

- **major — `custom_swings_supertrend.py:95-97`: Supertrend band recursion reads uninitialized neighbors.** `up_band[i]` depends on `up_band[i-1]` and `close[i-1] > up_band[i-1]`; at i=1, `up_band[0]` is 0.0 (not NaN/first-value), so the first band decision compares against a zero baseline — early-trend flips can differ from Pine until the bands converge. Same class of seeding defect as the RamJet processor.
- **major — `custom_swings_supertrend.py:169-181`: positional-dict compatibility shim breaks keyword callers.** If a caller passes `generate_signals(df, params_dict)` the dict is consumed, but a caller passing *keyword* `st_period={"...": ...}` would also be swallowed; more importantly the signature has 12 positional parameters that `resolve_strategy_parameters` must map by exact name — the dual calling convention (dict-positional vs kwargs) means typos fail differently depending on call style (silent default vs TypeError).
- **minor — `custom_swings_supertrend.py:52-57`: TR computed in a Python loop** despite NumPy being imported; O(n) interpreted — same perf note as other ports.
- **minor — `custom_swings_supertrend.py:287-290`: breakeven checks `high[i] >= be_trigger` then sets SL to entry, but the stop-hit check at 298 uses the *updated* same-bar SL:** intrabar sequencing (did price hit BE trigger before or after hitting old SL?) is resolved optimistically as "triggered first" — favorable-fill assumption baked into exits.
- **minor — `custom_swings_supertrend.py:243, 263`: degenerate-risk fallback `entry_price * 0.01`** silently substitutes sizing basis when ST band sits at entry — same silent-regime-switch pattern as regenerate_mt5_parity_artifacts.py:324.

(b) Security — nothing found.

(c) Baseline smells

- **Long Function / Divergent Change:** single 300-line loop mixing indicator state machine, trade management, and indicator assembly; LONG and SHORT branches are mirror-image duplicates (279-320 vs 321-361) — extract direction-parameterized helper.
- **Mysterious Name:** indicator key `"stop_loss"` with comment "reserved key → sl_price in meta" (399) — name lies about its role.

(d) Maintainability hot spots — the docstring honestly scopes the port ("does not attempt full 1:1") but nothing marks which behaviors diverge programmatically; parity regressions vs Pine are undetectable by construction.

### strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py

(a) Bugs / correctness risks

- **major — `qg_30min_es_breakout_strategy.py:170-171`: the short-entry condition ignores its own breakout level.** `enter_short = adx > 30 and close < breakout_short` — a *short* entry requires price below the short breakout level (high − fract·(high−open)), which is almost always true whenever price is below the high; meanwhile `enter_long` uses `close < breakout_long` where breakout_long = low + fract·|open−low| is typically *above* close, so longs nearly never fire. The comparison operators look inverted relative to a breakout strategy (long should require close ≥ upper breakout). Combined with the ADX gating (`adx < 30` for long vs `> 30` for short) this produces a systematically one-sided strategy; if intentional it's undocumented.
- **minor — `qg_30min_es_breakout_strategy.py:139-188`: O(n) Python loop with `.iloc[i]` per access** on 30-min futures data — acceptable here, but the same pattern in 1m strategies is a real cost.
- **minor — `qg_30min_es_breakout_strategy.py:140`: `timestamps_ny.iloc[i].date()`** raises on NaT rows (coerced bad timestamps from line 107-109) instead of skipping them.
- **minor — `qg_30min_es_breakout_strategy.py:197`: `"sl_price_at_exit": ("exit", sl_price_at_exit)`** — a *tuple* stuffed into an indicators dict whose every other value is a Series; any generic indicator renderer will choke or silently skip this key. Contract violation by shape.

(b) Security — nothing found.

(c) Baseline smells

- **Mysterious Name:** exit reason literal `"time_exit_1455_ny"` (160) though the configured default exit is 14:30 NY — name hardcodes a time the parameter no longer guarantees.
- **Duplicated Code:** entry/exit state machine duplicated across three intraday strategies (this, volatility_breakout, custom_swings_supertrend) with subtle differences — a shared single-position session-state helper would centralize the semantics.

(d) Maintainability hot spots — STRATEGY_META.notes honestly documents the missing stop-order fills; good practice worth noting positively.

### strategies/volatility_breakout/volatility_breakout.py

(a) Bugs / correctness risks

- **major — `volatility_breakout.py:74-82`: session-open computed via Python loop writing into a copy of `close`** — `session_open = close.copy()` then overwriting element-by-element works, but `current_open` initializes from `close.iloc[0]` (not open), so bars before the first day change use the wrong reference; more importantly this is the *only* place session identity comes from, and the sibling strategy (intraday_volatility_breakout) computes it differently — two "same-named" strategies diverge in core semantics.
- **minor — `volatility_breakout.py:63`: timestamp-less fallback `day = range(len(df)) // 7`** — a different synthetic-day rule than the sibling's `//100`; identical-looking strategies disagree on calendar structure when timestamps are absent.
- **minor — `volatility_breakout.py:89` vs sibling line 367:** entry window uses `<= max_entry_minute` (inclusive) here but `< max_entry_minute` (exclusive) there — off-by-one boundary inconsistency between near-duplicate strategies.
- **minor — `volatility_breakout.py:118-119`: `bool(raw_long_entries.iloc[i]) if not pd.isna(...)`** — entries are boolean Series; NaN check is dead code that hides upstream dtype drift.
- **minor — `volatility_breakout.py:148-150`: `"Session_Open"` and `"Session_Open_at_exit"` are the same object** — two indicator keys aliasing one Series; mutating one mutates both downstream.

(b) Security — nothing found.

(c) Baseline smells

- **Duplicated Code (whole-file):** ~70% of this file duplicates intraday_volatility_breakout's structure (ATR regime filter, session windows, single-position loop, drawings block verbatim at 164-192 vs sibling 496-525). The repo carries two diverging copies of one strategy concept under different names.
- **Repeated Switches:** dual-key param lookup repeated again (27-40).

(d) Maintainability hot spots — fixing a bug in the shared logic requires knowing a second file exists; nothing links them.

### strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py

(a) Bugs / correctness risks

- **major — `pine_superosc_vumanchu.py:146`: returns a 3-tuple `(entries, exits, indicators)`** while the platform contract everywhere else is a 4-tuple ending in `drawings`. Any caller unpacking four values (the standard engine path) raises ValueError on this strategy; any caller unpacking three misbinds `indicators` to `drawings`. Verified by reading the sibling strategies — all others return 4 values. This is an integration-breaking contract violation.
- **minor — `pine_superosc_vumanchu.py:110-111`: `stop_loss = 0.0; take_profit = 0.0` initial values** — if the first loop iteration somehow evaluates `in_trade=True` before assignment (not possible today), stops at 0.0 would trigger instantly; harmless but the sentinel choice invites future bugs vs `np.nan`.
- **minor — `pine_superosc_vumanchu.py:114`: no guard for `warmup >= len(df)`** on short frames — loop body never runs, silently returning all-False signals instead of flagging insufficient data.
- **minor — `pine_superosc_vumanchu.py:30-33`: TR in Python loop + rolling ATR mixing styles; same perf note as siblings.
- **minor — `pine_superosc_vumanchu.py:124-125`: stop and TP checked on the same bar with stop taking precedence** — pessimistic sequencing (opposite of custom_swings_supertrend's optimistic BE assumption) — inconsistent intrabar conventions across strategies make cross-strategy comparisons misleading.

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code:** single-position state machine, drawings block, TR computation — fourth copy of the same shapes.

(d) Maintainability hot spots — return-arity divergence is exactly the kind of thing a shared `StrategyResult` dataclass would prevent.

### strategies/mean_reversion_rsi_bb/mean_reversion_rsi_bb.py

(a) Bugs / correctness risks

- **minor — `mean_reversion_rsi_bb.py:55-57`: RSI via simple rolling mean of gains/losses** (Cutler's RSI), not Wilder's smoothing used by most reference platforms — parity comparisons against MT5/QC RSI will drift systematically; same choice in gold_momentum.
- **minor — `mean_reversion_rsi_bb.py:80`: long-only stop below entry, but entries can also fire when close < lower_bb in a falling market where ATR is NaN → `active_stop` stays NaN and the trade runs stopless** until an exit condition (84 guards with pd.notna) — silent unprotected positions.
- **minor — `mean_reversion_rsi_bb.py:89`: time-exit priority last** — a bar satisfying both stop_hit and time_exit records "stop_loss"; consistent with siblings but note exit-reason attribution depends on branch order, undocumented.
- **minor — `mean_reversion_rsi_bb.py:110-112`: `"RSI"`/`"RSI_at_exit"`, `"BB_Mid"`/`"BB_Mid_at_exit"` alias the same Series objects** (same as volatility_breakout finding).

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code:** state machine + drawings block (122-150) verbatim from siblings; **Long Function** mild.

(d) Maintainability hot spots — none beyond family-wide duplication.

### strategies/gold_momentum/gold_momentum.py

(a) Bugs / correctness risks

- **minor — `gold_momentum.py:78-84`: identical stopless-entry hole as mean_reversion_rsi_bb** (ATR NaN at entry ⇒ active_stop NaN ⇒ trade runs without stop).
- **minor — `gold_momentum.py:26-34`: params not coerced (`int(...)`/`float(...)` missing)** unlike every sibling — a YAML-provided `"12"` string makes `span="12"` raise inside ewm; inconsistent defensive posture across the family.
- **minor — `gold_momentum.py:103-107`: three `_at_exit` indicator keys aliasing their source Series** — same aliasing pattern.
- **minor — docstring/meta claim "Multi-Timeframe" (line 2) but implementation is single-timeframe** — Mysterious Name at strategy level; discovery/regeneration tooling selecting by asset_class tags will misroute expectations.

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code:** fifth copy of state machine/drawings/TR block.

(d) Maintainability hot spots — none beyond above.

### strategies/trend_following/trend_following.py

(a) Bugs / correctness risks

- **major — `trend_following.py:56, 80, 83`: trailing-stop sentinel `0.0` makes an unentered stop immediately binding.** `trailing_stop_value = 0.0` initial; if ATR is NaN at entry (73-75 guard skips assignment), `in_trade=True` persists with `trailing_stop_value=0.0`, and line 83 `close < 0.0` never fires — the trade runs *stopless* (opposite failure of firing instantly, but same sentinel root). Worse: after a legitimate exit the reset is also `0.0` (95) — a subsequent entry whose ATR is NaN inherits the previous regime's stale-zero semantics. Sentinel should be NaN with explicit checks.
- **minor — `trend_following.py:83`: trailing stop compared against `close`, not `low`** — intrabar breaches don't trigger; exit price then uses the close (90), so the "stop" fills at a price that may never have printed as a stop level. Optimistic fill assumption.
- **minor — `trend_following.py:65-66`: bars before SMA warmup are skipped entirely** including trade management — correct here, but combined with the 0.0 sentinel means early-frame behavior differs from late-frame.
- **minor — params not coerced (26-30)** — same inconsistency as gold_momentum.

(b) Security — nothing found.

(c) Baseline smells — sixth copy of state machine/drawings/TR shapes; aliasing indicator keys again (101-102).

(d) Maintainability hot spots — none beyond family-wide duplication.

### strategies/short_term_reversal/short_term_reversal.py

(a) Bugs / correctness risks

- **minor — `short_term_reversal.py:38, 57-58`: "weekly mean reversion" computed on bar-count lookback (`pct_change(5)`)** regardless of timeframe — on 1h data this is 5 *hours*, contradicting the docstring and meta ("dropped the most over 5 days"); strategy semantics silently change with execution timeframe.
- **minor — same NaN-ATR stopless-entry hole** (76-78 guard).
- **minor — `short_term_reversal.py:82`: reuses raw entry-side threshold series as exits** (`ret_5d > exit_threshold`) — fine, but no RSI confirmation on exits while entries require it; asymmetric filter documented nowhere.
- **minor — params not coerced (26-33).**

(b) Security — nothing found.

(c) Baseline smells — seventh copy of state machine/drawings/TR shapes; aliasing keys (100-101).

(d) Maintainability hot spots — none beyond above.

### strategies/risk_parity/risk_parity.py

(a) Bugs / correctness risks

- **minor — `risk_parity.py:63`: `should_rebalance = i % rebalance == 0`** anchors rebalancing to absolute frame position, not calendar or first-bar alignment — inserting one bar shifts every future rebalance date; results are not robust to data-window changes (a backtest from 2018 vs 2019 rebalances on different days even where data overlaps).
- **minor — `risk_parity.py:60`: `warmup = max(vol_period, trend_sma, 252)` hardcodes 252** rather than deriving from `vol.rolling(252)` at line 38 — two literals for one window.
- **minor — docstring promises "Equal-risk allocation across multiple asset classes" but implementation is single-instrument long/flat with inverse-vol regime gating** — the name oversells massively (Mysterious Name); the real multi-asset risk parity lives in scripts/run_risk_parity_backtest.py. Discovery tooling routing by tags will misplace this strategy.
- **minor — `risk_parity.py:65`: condition `not should_rebalance and not (in_trade and not calm_regime.iloc[i])`** — double negation over a compound state; equivalent readable form exists and the current form hides that non-rebalance bars only continue processing when in-trade AND regime-broken (i.e., exit checks run only on those bars) — subtle and uncommented.
- **minor — same NaN-ATR stopless-entry hole (72-74).**

(b) Security — nothing found.

(c) Baseline smells — eighth copy of family shapes; `_at_exit` aliases (94-102).

(d) Maintainability hot spots — naming collision with scripts/run_risk_parity_backtest.py + regenerate_qc_risk_parity_artifact.py creates three unrelated artifacts all called "risk parity".

### strategies/cross_asset_momentum/cross_asset_momentum.py

(a) Bugs / correctness risks

- **minor — `cross_asset_momentum.py:26-27`: `lookbacks`/`weights` zipped without length validation (37)** — a params file with mismatched list lengths silently drops the excess lookbacks; no error.
- **minor — `cross_asset_momentum.py:38`: `.fillna(0)` on each momentum leg** — early-frame missing data reads as zero momentum rather than NaN, so composite can cross the entry threshold on partial history inside warmup (guarded only by line 64's warmup, which uses max(lookbacks)+vol_window but not the 252-bar rolling max at 42).
- **minor — docstring says "multiple asset classes" / tags say "multi-asset" but implementation is single-instrument time-series momentum** — same naming oversell as risk_parity.
- **minor — same NaN-ATR stopless-entry hole (71-73); ninth copy of state machine/drawings.**

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code** family-wide; **Primitive Obsession:** weights/lookbacks as bare parallel lists instead of one typed structure.

(d) Maintainability hot spots — none beyond above.

### strategies/stat_arb_pairs/stat_arb_pairs.py

(a) Bugs / correctness risks

- **major (conceptual) — `stat_arb_pairs.py:4, 34-41`: there is no pair.** The docstring admits it ("Z-score of a spread proxy") but the code computes z-score of *the instrument's own cumulative log return* vs its own rolling mean — single-leg mean reversion wearing a pairs-trading name. Meta tags (`pairs-trading`) will route discovery/portfolio tooling into treating this as market-neutral when it is naked long-only exposure to one symbol. Severity major because downstream selection logic relies on tag semantics.
- **minor — `stat_arb_pairs.py:66-67`: exit conditions asymmetric with entry:** entry requires `z <= -2`, exits at `z >= -0.5` OR `z <= -4` — the "stop" fires when z goes *more negative*, i.e., deeper into the entry condition region; combined with the ATR stop below entry, both stops fire in the loss direction while profit-taking requires reversion. Coherent, but `stop_zscore < entry_zscore` ordering is inverted relative to intuition and unvalidated in params.
- **minor — `stat_arb_pairs.py:25`: `params: dict = None` default then `params or {}`** — acceptable idiom, though the family mostly uses required dicts.

(b) Security — nothing found.

(c) Baseline smells — tenth copy of family shapes; **Mysterious Name** at strategy level as above.

(d) Maintainability hot spots — none beyond above.

### strategies/channel_breakout/channel_breakout.py

(a) Bugs / correctness risks

- **major — `channel_breakout.py:61, 69, 71, 76`: identical `trailing_stop_value = 0.0` sentinel defect as trend_following** — NaN ATR at entry leaves stop at 0.0 and `close <= 0.0` never triggers (stopless trade); after-exit reset to 0.0 also means the *next* entry's first trailing update compares `max(0.0, new_stop)` which works by luck for positive prices, masking the bug.
- **minor — `channel_breakout.py:50`: `tr.iloc[0] = high.iloc[0] - low.iloc[0]`** patches the first TR but rolling ATR still yields NaN until bar atr_period — fine, but the patch implies intent to have full coverage that the rest doesn't deliver.
- **minor — `channel_breakout.py:30`: `use_ema_filter` not coerced to bool** — string `"false"` from YAML is truthy, silently enabling a filter the operator disabled.
- **minor — breakout compared to `upper_channel.iloc[i-1]` (79)** — previous-bar channel is the correct choice to avoid same-bar lookahead, good; but entry stop uses current close (84) creating mild inconsistency between signal basis and risk basis.

(b) Security — nothing found.

(c) Baseline smells — eleventh copy of family shapes; aliasing keys none here (cleaner).

(d) Maintainability hot spots — none beyond above.

### strategies/sector_rotation/sector_rotation.py

(a) Bugs / correctness risks

- **minor — `sector_rotation.py:4-6`: docstring honestly says "In the single-symbol adapter this becomes a momentum-filtered long strategy"** — i.e., no rotation exists; name oversell pattern again (tags "rotation" mislead discovery tooling).
- **minor — `sector_rotation.py:60`: warmup `max(lookback, 200)` hardcodes 200** duplicating the literal in `rolling(200)` (37) — two sources for one window.
- **minor — same NaN-ATR stopless-entry hole (65-67); twelfth copy of family shapes.**
- **minor — `exit_lookback`/`short_weakness` threshold `-0.03` hardcoded (38)** while sibling thresholds are parameterized.

(b) Security — nothing found.

(c) Baseline smells — Duplicated Code family-wide; aliasing keys (87-92).

(d) Maintainability hot spots — none beyond above.

### strategies/post_vix_dip_buy/post_vix_dip_buy.py

(a) Bugs / correctness risks

- **major — `post_vix_dip_buy.py:84-87`: O(N×M) date matching via `vix.loc[vix.index.date == current_date]` inside a Python loop over every SPY bar.** Each iteration scans the entire VIX index (creating a boolean array of len(VIX)); on a 10-year 1m SPY frame (~3.5M bars) against ~2.5k VIX days this is ~9 billion comparisons — this strategy effectively cannot run on intraday data. A merge_asof/join is one line.
- **major — `post_vix_dip_buy.py:90`: when VIX data is missing, fallback fabricates `vix_threshold + 1` for every bar.** The strategy then treats *every* bar as a VIX-spike day, reducing the strategy to pure RSI+SMA mean reversion — silently trading a different system than advertised whenever the data file is absent. The honest flags (`VIX_Fallback_Used`) exist in indicators (148) but nothing gates entries on them.
- **minor — `post_vix_dip_buy.py:83`: `pd.to_datetime(df["timestamp"])` without `utc=True`** (contrast line 39's own VIX handling) — if timestamps carry mixed offsets or naive values this raises or misaligns dates vs the UTC-normalized VIX index.
- **minor — `post_vix_dip_buy.py:28`: `_load_vix_data(start, end)` parameters accepted and ignored** — reads whole parquet regardless.
- **minor — `post_vix_dip_buy.py:32`: `rglob("**/VIX/1d.parquet")` takes `matches[0]`** — arbitrary file when multiple sources hold VIX; no source-priority logic (compare regenerate_mt5_parity_artifacts._source_priority which does it properly).

(b) Security — nothing found.

(c) Baseline smells — thirteenth copy of state machine/drawings; aliasing keys (139-144); **Long Function** mild.

(d) Maintainability hot spots — the external-data dependency (VIX parquet layout) is implicit; no schema validation of the loaded frame beyond "close" existing.

### strategies/pine_supertrend_dema/pine_supertrend_dema.py

(a) Bugs / correctness risks

- **major — `pine_supertrend_dema.py:124`: returns a 3-tuple** `(entries, exits, indicators)` — same return-arity contract violation as pine_superosc_vumanchu (no `drawings` element). Standard 4-value unpacking fails.
- **major — `pine_supertrend_dema.py:43-44`: Supertrend band roles are swapped relative to every sibling implementation.** Here `lower_band[i] = max(up, ...)` uses `up = src - mult*atr` and `upper_band[i] = min(dn, ...)` with `dn = src + mult*atr`; compare custom_swings_supertrend.py:95-97 where up_band (support) is `src − mult*atr` maxed and dn_band is `src + mult*atr` minimized. The *flip logic* at 45-48 then tests `close > upper_band[i-1]` to go bullish — i.e., price breaking the *upper* band in this file's naming, which given the swap means it breaks `dn = src + mult*atr`. Whether this nets out correct depends on reading both lines together; the two ports of "the same indicator" in one repo use opposite band naming with opposite comparisons — at minimum one is misnamed, at worst one is wrong. Flagged for cross-check rather than asserted-broken because the pairing of lines 43-44 with 45-48 is internally consistent.
- **minor — `pine_supertrend_dema.py:35-37`: bands seeded `np.zeros`, first computed at i=period; loop starts at `i=period` so `lower_band[period-1]=0` feeds the first comparison** — same zero-seeding family defect.
- **minor — entry stop from current-bar lower band (95), then trailing via `max(active_stop, lower_band[i])` (98)** — if the band gaps down after entry, `max` keeps the old stop (correct), but on entry bar the band may sit far below close making initial risk enormous with no sanity floor.

(b) Security — nothing found.

(c) Baseline smells — fourteenth copy of state machine shapes; aliasing key (116).

(d) Maintainability hot spots — two Supertrend implementations in strategies/ plus two in signal_bots/ = four divergent copies platform-wide.

### strategies/lab_generated_strategy/lab_generated_strategy.py

(a) Bugs / correctness risks

- **minor — `lab_generated_strategy.py:1`: file name says SMA, meta says 'Sample EMA Cross', directory is lab_generated_strategy** — triple naming mismatch; discovery by slug yields confusing labels everywhere.
- **minor — `lab_generated_strategy.py:64`: `'sl_price': None`** — scalar None where every other strategy puts a Series or omits the key; generic consumers doing `.iloc` on indicators['sl_price'] will crash. Contract deviation by type.
- **minor — no exits state machine:** exits are raw crossover series (52) — engine may open a new position immediately after closing one each whipsaw bar; differs from family behavior but arguably intended for an EMA demo.
- Positive note: `_get_param` handles nested metadata dicts (30-37) — the most defensive param handling in the family.

(b) Security — nothing found.

(c) Baseline smells — Mysterious Name as above; Duplicated Code with example_sma (below).

(d) Maintainability hot spots — none beyond above.

### strategies/example_sma/example_sma.py
Scanned — minimal EMA-crossover example. Findings:
- **minor — `example_sma.py:1-3`: meta name 'example_sma' but computes EMA not SMA** (14-15) — Mysterious Name; also used by run_discovery_grid_backtests.py as the "sma" mapping target, propagating the mislabel into grid artifacts.
- **minor — no `sl_price` key at all** (indicators only fast/slow) — engines relying on sl_price presence need None-guards; inconsistent with siblings that always provide it (even as None).
No security issues; smells: trivial duplication with lab_generated_strategy (two near-identical EMA crosses maintained separately — merge candidates).

### strategies/pynecore_orb/pynecore_orb.py

(a) Bugs / correctness risks

- **major — `pynecore_orb.py:1-5, 43-44`: the file is named "Opening Range Breakout" and meta tags say breakout, but the implementation is a *SMA crossover* strategy** (`fast_sma = ta.sma(src, or_length)` vs `slow_sma = ta.sma(src, or_length*4)`). There is no opening-range computation anywhere. The `or_length` parameter silently becomes "fast SMA length" — anyone tuning "opening range bars" gets a moving-average crossover with unrelated semantics. Same naming-integrity failure class as stat_arb_pairs.
- **minor — `pynecore_orb.py:55, 60`: truthiness test `if ... and atr_val:`** on a Series element — NaN is truthy in Python (`bool(float('nan'))` is True), so this guard does NOT skip warmup-NaN ATR values; TP/SL computed from NaN propagate NaN into strategy.exit prices. Correct check is `not math.isnan(atr_val)` / pd.notna.
- **minor — `pynecore_orb.py:29`: `default_qty_value=100` percent-of-equity** — full-capital entries per signal; combined with no explicit risk sizing this is aggressive for a live-deployable template.

(b) Security — nothing found.

(c) Baseline smells

- **Mysterious Name** at every level (file/dir/meta/parameter).
- **Speculative Generality:** `ema_period` filter defaults True but the ORB pretense means nobody reading the config understands what they're toggling.

(d) Maintainability hot spots — PyneCore files run under a separate engine; their divergence from native conventions (no STRATEGY parameters block consumable by resolve_strategy_parameters?) should be validated by tooling, not convention.

### strategies/pynecore_orb/pynecore_orb_context.py

(a) Bugs / correctness risks

- **minor — `pynecore_orb_context.py:37-40`: stop/take-profit levels anchored to `open_price` of *every* bar** rather than entry-bar open — the context provides per-bar series that downstream code must slice to entry bars; if consumed naively, SL for a long is recomputed each bar from that bar's open, drifting the stop. Contract is implicit.
- **minor — `pynecore_orb_context.py:39`: `tp_price_long = open + atr*sl_mult*tp_mult`** — TP scales the *stop distance*, correct, but naming (`sl_mult * tp_mult`) obscures the composition.

(b) Security — nothing found.

(c) Baseline smells — **Duplicated Code:** TR computation copy #15; duplicated between the two pynecore contexts nearly verbatim.

(d) Maintainability hot spots — the pyne file and its context file must stay semantically synchronized manually (params or_length/ema_period/atr_period appear in both with independent defaults); nothing ties them.

### strategies/pynecore_sma_crossover/pynecore_sma_crossover.py
Scanned — clean minimal SMA crossover in PyneCore format. Findings:
- **minor — same `and atr_val:` NaN-truthiness guard pattern (46)** as orb.
- **minor — `strategy.close("Long")` on crossunder but re-entry requires a fresh crossover — fine; no short side despite symmetric naming of inputs.**
Nothing else; no security issues; no significant smells beyond family duplication.

### strategies/pynecore_sma_crossover/pynecore_sma_crossover_context.py
Scanned — pandas mirror of the pyne strategy. Findings:
- **minor — `pynecore_sma_crossover_context.py:35`: `sl_price_long` anchored to per-bar `open_price`** — same implicit-slice contract as orb_context.
- **minor — exit_reason only marks crossunder bars (26-28); no sl_price_short even though the pyne file has no shorts either — consistent, good.**
No security issues; TR duplication noted once above.

### strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py

(a) Bugs / correctness risks

- **major — `ema_crossoiver_conversion_test.py:58`: returns a 3-tuple `(entries, exits, indicators)`** — third return-arity violation (with pine_superosc_vumanchu and pine_supertrend_dema). Standard 4-value unpack fails.
- **major — `ema_crossoiver_conversion_test.py:48`: `sl_price = df['close'] - atr * mult` computed on *every* bar unconditionally,** not gated to entry bars like every sibling; engines reading indicators['sl_price'] at arbitrary bars get stops for non-positions. Also long-only stop below price regardless of any direction concept.
- **minor — directory name typo'd ("crossoiver")** — permanent Mysterious Name baked into import paths.

(b) Security — nothing found.

(c) Baseline smells — Mysterious Name; Duplicated Code (EMA cross copy).

(d) Maintainability hot spots — "conversion_test" name suggests scratch status; shipping scratch files among deployable strategies pollutes discovery surfaces (iter_strategy_files picks it up).

### strategies/__init__.py
Scanned — empty file, nothing found.

---

## Group 7 — root configs

### pyproject.toml

(a) Bugs / correctness risks

- **minor — `pyproject.toml:57`: wheel packages omit `scheduler` and `scripts`** while shipping `shared`, `risk`, `signal_bots`, `strategies`. The scheduler package (a runtime service per its own design) is absent from distributions — a wheel-installed deployment cannot run the scheduler at all. Either intentional (run-from-repo) and then misleading, or an omission.
- **minor — `pyproject.toml:10-13`: heavyweight runtime deps** (`quantstats`, `ipython`, `openai`, `optuna`) are unconditional; ipython in production dependencies of every install is unusual and enlarges supply-chain surface. Judgement call given no extras split for them.
- **minor — `[tool.pytest.ini_options]` (66-67) coexists with root `pytest.ini`** — pytest reads `pytest.ini` first and ignores pyproject settings entirely; two config sources for one tool, one silently dead.

(b) Security — **minor:** broad dependency floor pins (`>=`) without upper bounds on network-facing libs (aiohttp, yfinance, discord.py) — standard trade-off, noted only because this repo executes broker integrations.

(c) Baseline smells — **Duplicated Code (config):** pytest config duplicated across two files (see above).

(d) Maintainability hot spots — engine extras pattern is good; missing scheduler/scripts packaging is the actionable gap.

### pytest.ini
Scanned — small and functional. Findings:
- **minor — `pytest.ini:5-8`: markers declared but nothing in this area applies them** (`e2e`, `integration`, `unit`, `slow`, `ai_code`); marker taxonomy exists without enforcement (no `--strict-markers`), so typos create silent unmarked tests.
- **minor — global `timeout = 180` (line 9)** applies to unit tests too; fine as ceiling, but combined with no strict-markers there's no way to run "fast" subset reliably.
No security issues.

### live_deployments.yaml

(a) Bugs / correctness risks

- **minor — `live_deployments.yaml:2-13`: Alpaca paper account IDs committed to source.** These are paper accounts (names say so) so sensitivity is low, but account identifiers + broker logins in a tracked file set precedent; the `${DISCORD_BOT_*_CHANNEL_ID}` interpolation (31, 54, 77) shows the right pattern for secrets — apply it to account identity if these ever become live accounts.
- **minor — `live_deployments.yaml:72`: rocketfuel deployment sets `allow_short: true`** — cross-checks against signal_bots/registry.py where the plain `"rocketfuel"` id maps to `_build_rocketfuel_longsonly` (long-only!). If this deployment resolves processor by `signal_processor: rocketfuel`, the config advertises shorting while the processor never emits short entries except via `rocketfuel_longandshort`; the two configs disagree about strategy behavior. Worth an explicit consistency check.
- **minor — all three deployments PAUSED with identical symbol lists/settings blocks** (18-37 vs 41-60) — copy-paste drift risk; a shared anchor or defaults block would help.

(b) Security — see account-id note above; channel IDs correctly externalized.

(c) Baseline smells — **Duplicated Code** across deployment entries.

(d) Maintainability hot spots — the file duplicates state that also lives in the SQLite `live_deployments` table (shared/database.py); which source wins at startup is defined outside this area, but the dual-home itself is a hazard.

---

## Summary counts

Every tracked file in the area list is accounted for above (findings or explicit "Scanned — nothing found"). Severity tags on findings:

| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 44 |
| Minor | 189 |

Major findings recap by theme (full details in per-file sections):

**Risk engine bypasses its own limits**
1. risk/live.py explicit-qty path constructs SizedOrder directly — no max_positions/exposure/risk checks.
2. risk/engine.py position size uncapped against balance; exposure/position-count guards trust caller metadata.

**Dead or fake operational wiring**
3. scheduler health-check job never registered in `_SCHEDULED_JOB_HANDLERS` (unreachable; verified by grep).
4. weekly_data_refresh writes progress records but refreshes no data.
5. overnight_strategy_discovery is a mock generator feeding real downstream backtests.
6. run_discovery_grid_backtests + download_discovery_data pinned to one absolute timestamped file.

**Destructive data operations without guard rails**
7. regenerate_backtests purges all backtest tables/JSON with no confirmation.
8. consolidate_parquets merges then unlinks source fragments, no dry-run, partial-failure loss.
9. adaptive_harvester marks months "done" with missing hours → permanent silent tick-data gaps.
10. matrix_queue_runner non-atomic lock + pgrep-based liveness probe can delete a live lock.

**Data-correctness in artifact/reference generators**
11. regenerate_mt5_parity_artifacts multi-source merge can synthesize mixed-source candles; --risk-pct ignored.
12. run_risk_parity_backtest single-ticker NaN gaps poison equity curve into validation reference.
13. regenerate_qc_risk_parity_artifact tz-naive index conversion; unnecessary module monkeypatch.
14. backtest_to_validation_ref hand-builds models instead of canonical from_dict.
15. generate_matrix_grid_overview executes its whole ETL at import time.

**Shared infrastructure**
16. shared/constants DATA_DIR resolution can pick different directories per process (split-brain DB/data).
17. shared/strategy_registry exec_module over all strategy files on every lookup.
18. shared/database subprocess-without-timeout; six-reason god module.
19. pyproject wheel omits the scheduler package.

**Signal bots**
20. registry nitro→ramjet config-file aliasing encoded in two places; stockkit→ramjet wholesale.
21. ramjet Supertrend zero-seeded early bars + ATR parameterization differs between `ta`-library and fallback paths.

**Strategy contract & semantics**
22. Three strategies return 3-tuples breaking the platform-wide 4-value unpack contract (pine_superosc_vumanchu, pine_supertrend_dema, ema_crossoiver_conversion_test).
23. Name-vs-behavior integrity failures: stat_arb_pairs has no pair, pynecore_orb has no opening range, example_sma computes EMA, risk_parity/sector_rotation/cross_asset_momentum/gold_momentum oversell multi-asset scope.
24. Silent regime switches inside strategies: intraday_volatility_breakout bare-except fallback changes signal definition; post_vix_dip_buy fabricates VIX-spike-everywhere when data missing; trend_following/channel_breakout trailing-stop 0.0 sentinel → stopless trades; qg_30min_es_breakout comparison directions produce one-sided trading; custom_swings_supertrend/pine_supertrend_dema zero-seeded Supertrend bands; volatility_breakout ~70% duplicate of intraday sibling with diverging boundaries.

























