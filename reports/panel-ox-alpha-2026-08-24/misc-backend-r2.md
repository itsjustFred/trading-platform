# Code Review — `misc-backend` — reviewer r2 (ox-alpha, blind, independent)

Scope: risk engine, scheduler, operational scripts, shared infrastructure, signal bots, trading strategies + root config files (`pyproject.toml`, `pytest.ini`, `live_deployments.yaml`). All 74 tracked files accounted for below.

Severity scale: **critical** (data loss / money loss / security breach), **major** (correctness bug or likely production incident), **minor** (smell, robustness, style-with-consequence).

Status: **COMPLETE** — all 74 tracked files reviewed blind and independently; findings grouped per package below, per-file coverage explicit (including scanned-with-nothing-found entries).

---

## Group 1 — shared/

### `shared/database.py` (3,891 lines)

**(a) Bugs / correctness risks**

- `shared/database.py:1066-1091` — **major** — `_backfill_curve_for_id` is dead code. It is defined and never called anywhere in the repo (verified by grep); the actual backfill path uses `_equity_points_curve` + `_artifact_equity_curve` (`database.py:1223-1248`). Speculative Generality; also silently misleads maintainers about which backfill runs.
- `shared/database.py:1278-1285` — **minor** — duplicated statement: `execution_column_names = _table_column_names(connection, "live_executions")` executed twice in a row; the first call's result is discarded. Duplicated Code / copy-paste remnant.
- `shared/database.py:2297-2302` — **major** — in `load_live_account_stats_inputs`, `deployment_ids` is seeded with `normalized_account_id` itself, then each deployment id *and* its raw `strategy_name` are added, and all of them are queried as if they were deployment ids (`WHERE live_signals.deployment_id IN (...)`). If any strategy name or account id collides with another deployment's id, executions from unrelated deployments are folded into this account's stats. Conflating three identifier namespaces into one IN-list is a correctness hazard for money statistics.
- `shared/database.py:60-63` vs `shared/constants.py:141` — **minor** — `load_dotenv(PROJECT_ROOT / ".env")` runs at import time in both `constants.py` and `database.py`; module-import side effects mean env-dependent paths (`DATA_DIR`, engine URL) freeze at first import order. Any test or script importing a different entry point first can bind the engine to a different data dir than the one the user configured.
- `shared/database.py:70-77` — **minor** — module-level `ENGINE = create_engine(...)` plus `init_db()` called at the top of ~50 functions: every caller pays an implicit global-init; there is no way to inject a session/engine (tests must monkeypatch globals, cf. `tests/test_database_migrations.py`). Shotgun-surgery-prone design; not a bug per se but the root cause of several awkward seams.
- `shared/database.py:739-745` — **minor** — `subprocess.run(["git", "rev-parse", "HEAD"])` has no `timeout=` (contrast with `shared/sentry.py:267-274` which does). A wedged git invocation blocks whatever thread calls `upsert_verification_evidence`.
- `shared/database.py:1420-1456` — **minor/major-borderline** — `save_live_execution_up_to_cumulative` opens `BEGIN IMMEDIATE` via `session.connection().exec_driver_sql(...)`. With SQLModel/SQLAlchemy 2.x this works only because the session has not yet autobegun a transaction; any future refactor that reads through the ORM before this line will get `cannot start a transaction within a transaction`. Fragile transaction handoff, worth an explicit comment/test.
- `shared/database.py:3624-3627` — **minor** — `load_backtest_result` does not normalize/strip `backtest_id` unlike every sibling function (`normalized_id` pattern used elsewhere). Inconsistent input handling.
- `shared/database.py:2860-2867` — **minor** — `list_verification_evidence_summary` fetches `limit*20` rows to build a grouped summary, so summaries can silently drop flows when total records exceed the fetch cap; the grouping happens post-fetch rather than in SQL. Correctness edge on large datasets.

**(b) Security**

- `shared/database.py:3327-3418` — **minor** — `_equity_preview_query_params` builds SQL text with f-string interpolation of placeholders only (`:id_N`) — values remain bound parameters, so no injection. However the function interpolates `normalized_limit` directly into SQL arithmetic (`CAST(... * (:limit - 1)) ...`) — safe today since it is int-coerced at `database.py:3462-3466`; flagged as fragile pattern to keep an eye on. Not exploitable as written.
- `shared/database.py:841, 881, 939` — **minor** — `PRAGMA table_info('{table_name}')` f-string: table names are all hardcoded constants inside this module, so no external input reaches these strings today; noted as a pattern risk only.

**(c) Baseline smells**

- **God module / Divergent Change**: `shared/database.py` is one 3,891-line module owning (i) SQLite engine+PRAGMAs, (ii) 15 table models, (iii) hand-rolled schema migrations, (iv) backtest persistence, (v) portfolio persistence, (vi) live signal/execution/balance/account/deployment persistence, (vii) background tasks, (viii) verification evidence, (ix) grid results. Any of those domains changing forces edits here (Divergent Change → split per reason).
- **Duplicated Code**: the "normalize id string, init_db(), open session, coerce row" preamble repeats in nearly every function (`_optional_trimmed` / `_normalize_optional_id` / inline `str(x or "").strip()` appear interchangeably at e.g. `database.py:329, 1482-1484, 1535-1537, 1823, 2352-2353, 2672-2684`); likewise the str/_mapping/tuple-row unwrapping helpers (`_normalize_task_record:805`, `_coerce_backtest_row_id:2475`, `_unwrap_equity_point_row:3270`, `_unwrap_model_row:3481`, `_extract_backtest_and_trade_count:2576`) are five near-clones of one shape.
- **Primitive Obsession**: ids/timestamps/statuses are plain `str` everywhere (`created_at: str`, ISO strings compared lexically at `database.py:1607, 2903, 3843`); mixed `datetime` columns (live tables) vs `str` columns (background tasks, evidence) for the same concept.
- **Repeated Switches**: source-type normalization logic duplicated between `from_live_deployment` (`database.py:591-599`), `to_live_deployment` (`database.py:623-643`) and `_resolve_deployment_signal_source` (`database.py:1906-1923`) — three hand-rolled copies of the same {"internal","discord"} mapping.

**(d) Maintainability hot spots**

- The migration subsystem (`database.py:845-1307`) re-parses SQLite DDL with regex (`_rebuild_live_signals_without_global_discord_unique`) — correct-looking but brittle against any future schema edit; deserves extraction into a dedicated `migrations` module with tests isolated from business queries.
- File is the de-facto import hub for runner/live_runner/viewer/scheduler/scripts (grep shows 20+ dependents), maximizing blast radius of any change.

### `shared/models.py` (749 lines)

**(a) Bugs / correctness risks**

- `shared/models.py:76-79` — **major-borderline/minor** — `StrategyType.MARKET_DATA`/`SIGNAL_PROCESSOR` enum lives in the canonical trade-format model file but describes *bot/service* classification; meanwhile `LiveAccount.broker` and `LiveDeploymentConfig.status` hardcode `Literal["alpaca","ctrader"]` / `Literal["ACTIVE","PAUSED","STOPPED"]` (`models.py:161,186`) while database.py validates the same sets independently — two sources of truth that can drift (already drifted: database.py accepts lowercase statuses for services but uppercase-only for deployments).
- `shared/models.py:104` — **minor** — `Service.created_at` default stores naive UTC (`replace(tzinfo=None)`); combined with tz-aware datetimes elsewhere in the same file (`LiveSignal.timestamp`) this makes the model layer's time semantics inconsistent (tz-naive datetimes checklist item).
- `shared/models.py:418-429` — **minor** — `Trade.is_winner` treats `pnl == 0` as a loser and `direction_sign` maps anything not exactly `"LONG"` to short; silent fallback instead of validation against `VALID_DIRECTIONS` declared at `models.py:284`.

**(b) Security** — nothing found (pure data models).

**(c) Baseline smells**

- **Data Clumps**: `position_size/risk_amount/risk_percent` triple travels together through `Trade`, `TradeRecord`, `_apply_backtest_trade_risk_fallbacks` (`database.py:3524`) — candidate for a small `RiskMath` value type.
- **Duplicated Code**: `BacktestStats.from_dict`/`PortfolioStats.from_dict` extras-splitting logic is copy-pasted twice (`models.py:620-625, 667-672`).
- **Primitive Obsession**: times as bare `str` ("ISO 8601 UTC" by convention only) across `TradeExit.time`, `EquityPoint.time`, `BacktestMeta.created_at` — no type guards anywhere.

**(d) Maintainability hot spots** — file mixes four vocabularies (strategy metadata, live trading services, portfolio, backtest results); splitting live-trading models out would shrink the blast radius of viewer-only changes.

### `shared/constants.py` (178 lines)

**(a) Bugs / correctness risks**

- `shared/constants.py:91-103` — **minor** — `_data_dir_score` silently picks among `~`-expansion candidates by heuristic scoring; two machines with different `$HOME`/owner states can resolve `DATA_DIR="~"` to different directories with no warning logged. Surprising implicit behavior for a "constant" module.
- `shared/constants.py:14-16` — **minor** — `VALID_TIMEFRAMES`/`VALID_DIRECTIONS` duplicated verbatim in `shared/models.py:283-284`. Duplicated Code; drift risk.

**(b) Security** — nothing found. `(c)` Smells: **Speculative Generality (mild)** — `_home_candidates()` gathers HOME, pwd, project-owner, SUDO_USER, USER candidates for a corner case that a single documented expansion rule would cover. **(d)** Import-time side effects (`load_dotenv` at `constants.py:36`) make this the hidden global-config hub; everything downstream freezes whichever `.env` was readable first.

### `shared/logging.py` (100 lines)

**(a) Bugs / correctness risks**

- `shared/logging.py:27,78` — **minor** — `LOG_LEVEL` captured once at import; `get_logger` later reads the frozen constant, so setting `os.environ["LOG_LEVEL"]` after first import has no effect (surprising in long-lived scheduler processes).
- `shared/logging.py:74-75` — **minor** — `_loggers` cache is read without a lock; two threads calling `get_logger` concurrently for the same new service can attach duplicate handlers (duplicate log lines). Race condition on shared state (checklist item); low likelihood, self-limiting impact.
- `shared/logging.py:13` — **docstring says default log dir `~/logs`** but implementation defaults to `<repo>/logs` (`shared/constants.py:143`). Mysterious Name/stale doc.

**(b) Security**

- `shared/logging.py:50-57` — **minor** — JSON formatter writes `record.getMessage()` unredacted; unlike `shared/sentry.py` there is no secret scrubbing, so tokens passed as log args go to disk in plaintext. Consistency gap with the Sentry redaction standard.

**(c) Smells** — nothing beyond above. **(d)** Fine otherwise.

### `shared/sentry.py` (286 lines)

**(a) Bugs / correctness risks**

- `shared/sentry.py:139-140` — **minor** — `initialize_sentry` returns `True` on any subsequent call once initialized, even for a *different* `service_name`; second service silently inherits the first service's tag. `_INITIALIZED_SERVICE` is recorded but never checked.
- `shared/sentry.py:185-191` — **minor** — `capture_exception` calls `sentry_sdk.push_scope()` even when Sentry was never initialized; harmless no-op but callers cannot distinguish "captured" from "disabled" except by `None` event id.

**(b) Security** — good news: explicit redaction pipeline (`_before_send`, `_SENSITIVE_*`, bearer-token regex) is a repo strength. One gap:

- `shared/sentry.py:40-43` — **minor** — `_SENSITIVE_ASSIGNMENT_RE` requires the value to be a contiguous token (`[^,\s;&]+`); JSON-style secrets containing quotes/spaces (`"password": "hunter 2"`) escape redaction.

**(c) Smells** — **Speculative Generality (borderline)**: `_reset_for_tests` (`sentry.py:283-286`) ships in production module; acceptable test hook, note only. **(d)** Nothing further.

### `shared/strategy_registry.py` (282 lines)

**(a) Bugs / correctness risks**

- `shared/strategy_registry.py:100-107` — **major-borderline** — `load_strategy_module` executes arbitrary strategy files at import time (`spec.loader.exec_module`) with no sandboxing; combined with `update_strategy_display_name` (`registry.py:235-260`) writing source back to disk after AST round-trip, a malformed strategy file becomes both a code-execution vector and a file-corruption vector (write is not atomic, unlike other repo writes).
- `shared/strategy_registry.py:238-266` — **minor** — `update_strategy_display_name` replaces the AST span with `repr(new_name)`; if the dict literal uses single quotes elsewhere the file style flips; more importantly the early `break` at `registry.py:266` only fires after the inner loop completes without matching, so a `STRATEGY_META` dict *without* a `"name"` key falls through to `raise ValueError` — correct but subtle; a `continue`/`break` asymmetry that took effort to verify.
- `shared/strategy_registry.py:176-192` + `220-226` — **minor** — `find_strategy_entry` re-executes every strategy module on each lookup (module load per file per call); O(N) imports per identifier resolution, repeated for each API request. Performance smell, and repeated exec of modules with import-time side effects multiplies them.

**(b) Security** — see `load_strategy_module` above: **major** if strategies dir is ever writable by non-trusted parties (it is part of the repo, same trust domain — hence borderline).

**(c) Smells** — **Shotgun Surgery (adjacent)**: slug/relative-path/doc-path resolution spread over four small functions (`registry.py:62-97`) that all take `(path, strategies_dir)` — a StrategyRef value object would bundle the clump. **(d)** Nothing further.

### `shared/__init__.py`

Empty package marker — scanned, nothing found.

---

## Group 2 — risk/

### `risk/engine.py` (277 lines)

**(a) Bugs / correctness risks**

- `risk/engine.py:151-152` — **major** — exposure check compares apples to oranges: `remaining_exposure = max_total_exposure - exposure` then `risk_percent = min(max_risk_per_trade, remaining_exposure)`. `max_risk_per_trade` is a fraction of *balance* (1%) while `max_total_exposure` is treated here as an absolute currency amount subtracted from `current_exposure`, yet at `engine.py:216-218` the same `risk_percent` is multiplied by balance again (`risk_amount = balance * risk_percent`). If `current_exposure` is passed in currency units (as the parameter name and callers like `risk/live.py:114` suggest — default `0.0`, fed from metadata), then with `balance=100_000` any `current_exposure < 0.05` leaves risk_percent ≈ 0.05 → 5% of balance risked, i.e. **5× the configured per-trade cap** whenever exposure is small; conversely once exposure ≥ 0.05 currency units trading stops entirely. The two limits must share one unit; today the engine silently over-risks or blocks depending on unit convention. (Severity major: direct money-sizing impact.)
- `risk/engine.py:183-184` — **minor** — mutating the input signal inside sizing (`signal.stop_loss = stop_loss`) is a side effect callers don't expect from a "calculate" method; `apply_trailing_stop` also mutates in place (`engine.py:271,275`). Documented nowhere on the type; surprise-mutation bug class.
- `risk/engine.py:127` — **minor** — `current_positions` read from *signal metadata* rather than engine/portfolio state means position-count enforcement is only as honest as whoever fills the dict; nothing cross-checks it. Design gap worth flagging for a risk component.
- `risk/engine.py:14-16` — **minor** — `@dataclass(slots=True)` combined with a hand-written `__init__` that shadows the generated one: works, but redefines constructor semantics of a dataclass (Mysterious Name territory — the decorator promises dataclass init behavior it doesn't have).

**(b) Security** — nothing found (pure computation, no I/O).

**(c) Baseline smells**

- **Long Parameter List / Data Clumps**: `_decision()` takes 8 parameters of which 4 are usually 0.0/None together (`engine.py:62-92`); a `Rejection` value object would collapse the four rejection call sites (~40 lines each) into one.
- **Duplicated Code**: the five rejection branches (`engine.py:130-210`) are near-identical `_sized_order(...)` + `_decision(accepted=False, ...)` blocks differing only in reason/metadata.

**(d) Maintainability hot spots** — the unit-conflation above is the hot spot; add a regression test pinning `calculate_position_size` to explicit currency/fraction semantics before touching anything else.

### `risk/config.py` (30 lines)

Scanned. Validation in `__post_init__` is solid (all bounds checked). Nothing found.

### `risk/models.py` (115 lines)

**(a)** `risk/models.py:45-49` — **minor** — `TradeSignal.action` is typed `Literal[...]` but normalization via `self.action = normalized  # type: ignore[assignment]` bypasses the type checker; a typo upstream still raises at runtime, so this is fine, but the ignore hides future drift from mypy. Otherwise validation quality is high.
**(b) Security** — nothing found. **(c)** Smells: none material. **(d)** Good file.

### `risk/live.py` (133 lines)

**(a) Bugs / correctness risks**

- `risk/live.py:87-95` — **major-borderline/minor** — risk limits are read from *the incoming signal's own metadata* (`metadata.get("max_risk_per_trade", 0.01)`): any Discord-sourced or compromised signal publisher can raise its own per-trade risk to arbitrary values (e.g. `max_risk_per_trade=1.0`). Risk config should come from deployment/operator config, never from the untrusted payload. For a risk module this is a trust-boundary design flaw (severity major if Discord signals reach this path with attacker-controlled metadata).
- `risk/live.py:45-47` — **minor** — `explicit_qty` falls back from `explicit_qty` to `qty`; both then written back into metadata (`live.py:76-84`) so the two keys can disagree with what was actually used if `qty` was present but ≤0. Cosmetic state drift.

**(b) Security** — see trust-boundary note above.

**(c) Smells** — **Duplicated Code**: the explicit-qty path (`live.py:52-85`) and the sized path (`live.py:98-132`) duplicate the whole TradeSignal construction + metadata write-back block; extract one builder.

**(d)** The `signal.metadata = {...}` write-backs mutate the shared LiveSignal object — same surprise-mutation pattern as engine.py.

### `risk/__init__.py`

Clean re-export surface, `__all__` correct — scanned, nothing found.

---

## Group 3 — scheduler/

### `scheduler/scheduler.py` (279 lines)

**(a) Bugs / correctness risks**

- `scheduler/scheduler.py:142-144` — **major** — orphan cleanup removes any APScheduler job whose id is not in `desired_job_ids`, but `desired_job_ids` is computed only from `ServiceType.SCHEDULED_JOB` services. If anything else ever registers jobs on the same `AsyncIOScheduler` instance (or a second `ServiceScheduler` sync runs while another component added a job), those jobs are silently deleted. The scheduler instance is module-private today, which downgrades this to major-borderline — but the code has no guard asserting exclusive ownership, and `sync_services` is public API.
- `scheduler/scheduler.py:226-231` — **minor** — on job *success* the service status is set back to `RUNNING` rather than recording last-run success distinctly; combined with the FAILED path (`scheduler.py:273-278`) an operator cannot distinguish "running now" from "ran fine recently" without inspecting task records. Semantic overloading of one status field.
- `scheduler/scheduler.py:44` — **minor** — `shutdown(wait=False)` abandons running job coroutines without cancellation; in-flight `_run_service_job` tasks keep writing background-task rows after "stop", racing a subsequent `start()`.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code**: the three `upsert_background_task_record` blocks (`scheduler.py:174-186, 213-225, 260-272`) repeat ~12 identical kwargs; a small `_write_task(status, config, message)` helper would cut the file by a third. Same duplication repeats in both job files.
- **Data Clumps**: `{service_id, service_name, job_type}` passed around as three parallel strings through every method — bundle into the existing `Service` model instead of unpacking into kwargs at `scheduler.py:122-126`.

**(d) Maintainability hot spots** — handler registry `_SCHEDULED_JOB_HANDLERS` (`scheduler.py:19-21`) currently maps exactly one job; `signal_bot_health_check.run` exists with the right signature but is not registered — either dead or unwired (see next file).

### `scheduler/jobs/signal_bot_health_check.py` (101 lines)

**(a) Bugs / correctness risks**

- `signal_bot_health_check.py:13-14` vs `scheduler/scheduler.py:19-21` — **minor/major-borderline** — this job is never registered in `_SCHEDULED_JOB_HANDLERS`, so scheduling it through the services table always fails with "Unknown scheduled service job_type" and marks the service FAILED (`scheduler.py:79-91`). Either dead code (Speculative Generality) or a wiring bug that breaks anyone configuring a health-check service. Also referenced nowhere else in repo except tests/docs per grep.
- `signal_bot_health_check.py:96` — **minor** — error message truncated to 100 chars (`str(exc)[:100]`) inconsistently with the success paths; loses root-cause detail for long tracebacks.

**(b) Security** — nothing found.

**(c) Smells** — **Duplicated Code**: the same 10-line description string and upsert boilerplate repeated 3×(`:20-37, 62-79, 86-101`).

**(d)** Fine otherwise.

### `scheduler/jobs/weekly_data_refresh.py` (84 lines)

**(a) Bugs / correctness risks**

- `weekly_data_refresh.py:34-79` — **major-borderline/minor** — the entire "refresh" is a simulation: it loops hardcoded symbols, writes progress records, `await asyncio.sleep(0)` and logs "finished" — no data is actually downloaded or refreshed anywhere in the file. As a scheduled job named `weekly_data_refresh` this misleads operators into believing market data is being maintained (Mysterious Name in the strongest sense). If real refresh exists elsewhere (`scripts/download_discovery_data.py`), it is not wired here.
- `weekly_data_refresh.py:11-12` — **minor** — module-level constant symbol/timeframe lists duplicate what should come from service config; changing coverage requires a code deploy (config keys ignored).

**(b) Security** — nothing found. **(c)** Smells: **Speculative Generality** (job does nothing but report progress), **Duplicated Code** (upsert block). **(d)** Low.

### `scheduler/__init__.py`, `scheduler/jobs/__init__.py`

Re-export markers — scanned, nothing found beyond the note that `jobs/__init__.py` exports only `weekly_data_refresh`, hiding the existence of `signal_bot_health_check` from the package surface (consistent with the wiring bug above).

---

## Group 4 — signal_bots/

### `signal_bots/registry.py` (110 lines)

**(a) Bugs / correctness risks**

- `signal_bots/registry.py:32-35` — **major-borderline/minor** — `_build_nitro` loads **ramjet's** `config.yaml` (`_ROOT / "ramjet" / "config.yaml"`), not a nitro config. If a nitro-specific config file is ever added it will be silently ignored; today nitro intentionally shares ramjet parameters, but the code reads like a copy-paste slip and there is no comment asserting intent. Same latent confusion in `signal_bots/health_check.py:40-41`, which maps both `stockkit` *and* `nitro` to ramjet's file.
- `signal_bots/registry.py:91-98` — **minor** — `"stockkit"` maps to `_build_ramjet`; the label promises "StockKit-compatible processor (SL/TP + trailing metadata)" but the implementation is byte-for-byte RamJet. If StockKit semantics ever diverge this alias silently produces RamJet behavior. Mysterious Name.
- `signal_bots/registry.py:38-59` — **minor** — the two rocketfuel builders differ by exactly one literal (`"longsonly"` vs `"longandshort"`); classic parameterize-the-duplicate case.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Duplicated Code**: `_build_ramjet`/`_build_nitro` are identical except the constructor name; the two rocketfuel builders likewise. One parameterized factory covers all five entries.
- **Repeated Switches**: variant handling duplicated between registry (`:41-46, :53-58`) and processor (`rocketfuel/processor.py:49-55`) — two places encode the same variant rules.

**(d)** The registry is also the only place documenting bot semantics (labels/descriptions); fine.

### `signal_bots/health_check.py` (62 lines)

**(a) Bugs / correctness risks**

- `signal_bots/health_check.py:37-41` — **minor** — dead first assignment: `config_path = Path(__file__).parent / name.split("_")[0] / "config.yaml"` is immediately overwritten for every one of the five known ids (all ids hit the `if`/`elif`). Only reachable if OPTIONS gains an id not in SIGNAL_PROCESSORS — but then line 33 already flags it as error, so the initial value can never be meaningfully used. Confusing dead code; delete or restructure.
- `signal_bots/health_check.py:20-52` — **minor** — the check never actually imports/instantiates processors despite the docstring claiming "Bot processors import successfully" (`:4`); it only verifies dict keys and file existence. Doc-vs-behavior gap: import breakage would pass the health check until runtime.

**(b) Security** — nothing found. **(c)** Smells: none beyond above. **(d)** Fine.

### `signal_bots/ramjet/processor.py` (247 lines)

**(a) Bugs / correctness risks**

- `signal_bots/ramjet/processor.py:102-113` — **minor/major-borderline** — duck-typed `hasattr(frame, "ta")` branch: if the optional `ta` extension is installed on the DataFrame, Supertrend comes from the `ta` library; otherwise a hand-rolled loop runs. The two implementations use *different ATR smoothing* (`ewm(span=period)` at `:93` vs `ewm(alpha=1/period)` at `:122` — these coincide only when period==1... actually span N ≈ alpha 1/N so they match approximately) and potentially different flip conventions; SL/TP prices computed from `atr` (`:199-200`) can therefore shift depending on whether `ta` happens to be installed. Environment-dependent trading math is a correctness hazard.
- `signal_bots/ramjet/processor.py:194-200` — **minor** — entry price taken from the *latest close in the supplied frame* regardless of signal timestamp; if `market_data` is stale the SL/TP anchor to an old bar with no staleness check (config key `signal_max_age_seconds` exists in YAML but is never read here).
- `signal_bots/ramjet/processor.py:13-29` vs `shared/constants.py:17-21` — **minor** — local `_TIMEFRAME_ALIASES` duplicates and extends the shared map ("60min"→"1h" etc.) rather than extending the central table. Duplicated Code / Shotgun Surgery risk when a new alias is needed.

**(b) Security** — nothing found (yaml.safe_load everywhere; good).

**(c) Baseline smells**

- **Duplicated Code**: `_prepare_market_data` + `_calculate_atr` + `_resolve_timeframe` are copy-pasted between ramjet and rocketfuel processors (`ramjet/processor.py:66-95` ≈ `rocketfuel/processor.py:65-91`); extract a shared OHLCV utility module.
- **Long Method (borderline)**: `_calculate_supertrend` (`:97-166`) mixes three concerns (library probe, fallback math, column cleanup) in one function.

**(d)** Hot spot: the dual ATR/Supertrend paths need a parity test pinning both to identical outputs before either changes.

### `signal_bots/nitro/processor.py` (37 lines)

**(a)** `nitro/processor.py:12-17` — **minor** — override exists solely to change the exception message ("Nitro..." vs "RamJet..."); logic identical. Harmless but pure duplication. Also `process_signal` rebuilds the TradeSignal field-by-field instead of `dataclasses.replace(signal, metadata=...)`. **(b)** Nothing. **(c)** Smell: **Middle Man** class — Nitro adds only metadata relabeling over RamJet; inheritance chosen where composition/config would do (though not a Refused Bequest since no behavior is rejected). **(d)** Fine.

### `signal_bots/rocketfuel/processor.py` (160 lines)

**(a) Bugs / correctness risks**

- `signal_bots/rocketfuel/processor.py:124-130` — **minor** — long-only SELL yields `stop_loss=None` + `metadata["exit_only"]=True`; downstream consumers must remember to honor `exit_only` — nothing in this package enforces it, and `risk/engine.py` will happily fall back to a synthetic stop if such a signal reaches sizing with `enforce_stop_direction` unset. Cross-module contract enforced only by convention.
- `rocketfuel/processor.py:115` — same stale-frame entry-price issue as ramjet (`entry_price = close.iloc[-1]` with no age validation).

**(b) Security** — nothing found.

**(c) Baseline smells** — **Duplicated Code** (see ramjet: `_prepare_market_data`, `_calculate_atr`, `_resolve_timeframe` clones). Config parsing duplicated too (`__init__` unwrapping `rocket_fuel` sub-dict mirrors ramjet's `ram_jet` unwrap).

**(d)** RSI/DMA settings in `config.yaml` (`rsi_period`, `dma_fast/slow`) are parsed into nothing — the processor uses only ATR; dead config keys mislead tuners (Speculative Generality / config rot).

### `signal_bots/__init__.py`, `signal_bots/{ramjet,rocketfuel,nitro}/__init__.py`

Re-export markers, consistent style — scanned, nothing found.

---

## Group 5 — scripts/ (part 1: the two largest)

### `scripts/live_smoke.py` (1,620 lines)

**(a) Bugs / correctness risks**

- `scripts/live_smoke.py:436-459` — **major-borderline** — the cTrader order worker opens a real position then closes it; but between `submit_order` (`:436`) and `_wait_for_ctrader_position(..., should_exist=True)` there is **no try/except flatten guard** (unlike the e2e worker which has `_best_effort_close_open_position`, `:535-592`). If `_wait_for_ctrader_position` raises or the process is killed mid-window (timeout at `asyncio.wait_for(adapter.submit_order...)` where the order actually filled), a demo EURUSD position is left open and *every subsequent smoke run refuses to start* (`:420-422`). The asymmetry between the two workers looks like an oversight.
- `scripts/live_smoke.py:1444-1449` — **minor** — local `class contextlib_suppress` reimplements `contextlib.suppress(Exception)` with silent-all semantics; used once at `:768`. Reimplemented stdlib (Mysterious Name + Duplicated Code); also swallows *all* exceptions including `KeyboardInterrupt`-adjacent errors in the cancel path.
- `scripts/live_smoke.py:1271-1279` — **minor** — `_cancel_stockkit_buy_order` uses bare `except Exception: pass` twice; cancel failures are invisible in details except indirectly via remaining_qty. Checklist item: silent failure of an order-cancel path.
- `scripts/live_smoke.py:230, 274, 312` — **minor** — worker JSON extraction scans stdout lines for one starting with `{`; any earlier JSON-looking line (e.g. library banner) would be picked only if it's the last such line — reversed scan mitigates but a multi-line pretty-printed JSON payload from a library would break parsing silently. Fragile IPC-by-print protocol.
- `scripts/live_smoke.py:1561-1595` — **minor** — worker modes call `os._exit()` immediately after print; buffered output could theoretically be lost on some platforms since `os._exit` skips flushing (they do pass `flush=True`, so mitigated — noting because `os._exit` also skips atexit/Sentry flush by design).
- `scripts/live_smoke.py:986-1017` — **minor** — `_alpaca_stock_prices` computes `buy_limit = max(bid, ask, last) * 1.05`; with a crossed/stale quote this can be far from true price. Acceptable for paper smoke; flagged as money-adjacent math without sanity bounds.
- `scripts/live_smoke.py:109-110` — **minor** — Alpaca account checks hardcode `for index in (1, 2, 3)`; adding ACC4 requires code edit in three separate loops (`:109, :812, :1074`). Data Clump/magic numbers.

**(b) Security**

- `scripts/live_smoke.py:67-68` — **minor** — `_public_error` embeds raw exception text into report JSON files written under `reports/`; broker SDK exceptions can embed request URLs containing tokens (cTrader refresh flow). No scrubbing analogous to `shared/sentry.py`. Low likelihood, but these reports are committed to the repo directory.
- Positive: paper-mode enforcement before any order (`:791-792, :1099-1100`) and pre-flight "position already open → refuse" guards (`:420-422, :499-501`) are good safety rails.

**(c) Baseline smells**

- **Long Function / God script**: `main()` (`:1516-1616`) multiplexes three subprocess-worker modes plus four top-level modes through string comparison chains; the file mixes CLI parsing, worker entrypoints, broker logic, market-hours logic, and reporting (Divergent Change).
- **Duplicated Code**: the three `_run_ctrader_*_subprocess` functions (`:198-233, :236-277, :280-315`) differ only in mode name/extra args — one parameterized builder removes ~80 lines.
- **Feature Envy (mild)**: `_find_ctrader_open_position` reaches five levels into adapter internals (`adapter._ProtoOAReconcileReq()`, `._send_request`, `.ctid_trader_account_id`, `._symbol_name_to_id`, `._ProtoOAPositionStatus...`) at `:352-374` instead of the adapter exposing `find_open_position(symbol)`. Message Chains smell, textbook.

**(d) Maintainability hot spots** — private-API coupling to `CTraderLiveAdapter`/`AlpacaLiveAdapter` internals means adapter refactors break the smoke tool silently; promote those operations to public adapter methods.

### `scripts/adaptive_harvester.py` (814 lines)

**(a) Bugs / correctness risks**

- `scripts/adaptive_harvester.py:519-520` vs `:525-526` — **minor** — `avg_latency_s = sum(...) / len(outcomes)` computed *before* the `if not outcomes:` guard that then sets it to infinity; dead branch (batch is never empty since `urls` non-empty and slice non-empty), but the ordering shows the guard was bolted on. Harmless today.
- `scripts/adaptive_harvester.py:349-353` — **minor** — empty-payload retry treats weekend emptiness as terminal but weekday emptiness as retryable; after exhausting retries a weekday-empty file counts toward neither success nor failure stats — month can complete with silently missing hours (successful_files < total_files yet status "done", `:586-590` only fails if *zero* succeeded). Data-completeness gap: partial months are marked `done` (`:726`) with no warning.
- `scripts/adaptive_harvester.py:43-44` — **minor** — `LOG_DIR` here defaults to `Path.home()/logs` while `shared/constants.py:143` defaults to `<repo>/logs`; same env var resolved differently across the repo. Inconsistent config resolution.
- `scripts/adaptive_harvester.py:695-707` — **minor** — `asyncio.run()` called inside a per-month loop; fine standalone, but if ever imported and called from an async context it will raise. Also each `asyncio.run` recreates connectors — acceptable for CLI.

**(b) Security**

- `scripts/adaptive_harvester.py:333-432` — positive: timeouts set (`ClientTimeout(total=45, connect=15)`), bounded retries with exponential backoff, rate-limit handling. Nothing found.

**(c) Baseline smells**

- **Duplicated Code**: DATA_DIR/LOG_DIR resolution re-implemented locally (`:35-44`) instead of importing `shared.constants` (which the file already imports for `resolve_genesis_path` at `:30`) — two sources of truth in one file.
- **Long Function**: `_download_month` (~157 lines, `:464-620`) handles HTTP batching, AIMD tuning, progress logging, assembly, and persistence.
- **Data Clumps**: `(symbol, asset_class, instrument)` triple threaded through six functions.

**(d)** State/profile writes are atomic (`_write_json_atomic`, good). Overall solid ops tooling; main risk is the silent-partial-month issue above.

### `scripts/analyze_mt5_parity.py` (667 lines)

**(a) Bugs / correctness risks**

- `scripts/analyze_mt5_parity.py:409-417` — **minor** — `stop_atr`: `by_ts.index.searchsorted(entry_ts)` finds the *first bar at or after* entry, but there is no tolerance check (unlike `data_quality` at `:492`); an entry timestamp a millisecond after the last matching bar silently maps to the *next* row or beyond. Diagnostic-only impact.
- `analyze_mt5_parity.py:640` — **minor** — default `--data-dir /mnt/block-volume-data` hardcodes a machine-specific absolute path; on any other host every subcommand fails with a confusing FileNotFoundError until overridden. Config-by-convention smell.
- `analyze_mt5_parity.py:385-388, 475-476` — **minor** — `pd.to_datetime(...)` of MT5 timestamps then `.hour/.minute` on tz-*naive* results in `entry_minutes`; tz handling is done properly elsewhere (`_utc_timestamp`) but not here — the printed minute histogram is in source-local time by accident of importer behavior rather than by explicit conversion. Fragile.

**(b) Security** — nothing found (local file analysis only).

**(c) Baseline smells**

- **Long Function**: `matched_stop_path` (~75 lines, `:506-580`) interleaves matching, diff collection, and reporting.
- **Duplicated Code**: `_trade_exit_price/_trade_exit_time/_platform_exit_reason/_mt5_exit_reason` re-implement dict-or-object dual access four times (`:68-115`); one pair of adapter helpers would do.
- **Repeated Switches**: `summary`, `entry_minutes`, `stop_atr`, ... all repeat the same `importer = MT5XLSXImporter(); for label, mt5_path in MT5_REPORTS.items(): ref = importer.load(...)` preamble seven times.

**(d)** Fine for a diagnostics tool; no runtime risk.

### `scripts/run_risk_parity_backtest.py` (577 lines)

**(a) Bugs / correctness risks**

- `scripts/run_risk_parity_backtest.py:343-352` — **major-borderline/minor** — attribution PnL uses `prices[ticker].loc[reb_date]` vs previous rebalance date close-to-close, but multiplies by `interval_start_portfolio_value` captured *before* mark-to-market; combined with per-ticker weight deltas this double-counts when weights changed at prior rebalances (attribution ≠ realized PnL; the stats block even reports both `net_profit` and `rebalance_attribution_net_profit` which will disagree). It is explicitly diagnostic output, hence borderline.
- `run_risk_parity_backtest.py:364-366` — **minor** — `direction` logic: `prev_weight >= 0 → LONG`, but weights here are always ≥ 0 (long-only strategy), so `"SHORT"` can only appear via the `delta < 0 and not prev_weight` branch — i.e. opening a "SHORT"-labelled trade when actually *closing to zero from zero*, which cannot happen. Dead/misleading branch; direction semantics muddy in the validation reference.
- `run_risk_parity_backtest.py:423` — **minor** — Sharpe computed as `(mean*252)/(std*sqrt(252))` = mean/std*sqrt(252) — correct — but uses `std()` with pandas default ddof=1 while `_annualized_sharpe` in analyze_mt5_parity uses ddof=1 too; consistent. However `sortino` (`:425`) divides by downside std without annualizing the mean the same way QC does — flagged as convention mismatch only.
- `run_risk_parity_backtest.py:245-246` — **minor** — `warm_up_days < len(prices)` guard raises with clear message — good; but `rebalance_dates > prices.index[warm_up_days]` uses positional index on a date index that may contain gaps (fine) — no issue, noting verified.
- `run_risk_parity_backtest.py:54-57` — **minor** — yfinance schema drift: with newer yfinance versions `data["Close"]` may be a Series when a single ticker resolves; guarded by `TICKERS` length 11 today. Also `auto_adjust=True` silently changes history as yfinance updates adjustments — non-reproducible reference data for validation artifacts.

**(b) Security** — nothing found (no secrets; network read-only).

**(c) Baseline smells**

- **Long Function**: `run_backtest` spans ~245 lines (`:231-475`) with nested closures mutating `nonlocal` state (`advance_portfolio_value`, `:273-282`) — hard to test in isolation.
- **Magic numbers**: lookbacks/thresholds inline (`:250-255`) instead of module constants or CLI args; `settings=` dict at `:516-523` duplicates them a third time (Duplicated Code).
- **Primitive Obsession**: trades built as raw dicts then manually converted to `Trade` models (`:482-509`) instead of building models once.

**(d)** The docstring's honest fallback disclaimer is good practice.

### `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py` (527 lines)

**(a) Bugs / correctness risks**

- `intraday_volatility_breakout.py:357-360` — **major-borderline/minor** — the confirmation-resample block is wrapped in a bare `except Exception:` that silently falls back to `close.shift(1)`; any pandas error (bad `_TF_FREQ_MAP` value, malformed index) degrades signal semantics *without any warning*, producing trades that look valid but follow a different rule than configured. Silent-fallback in trading logic.
- `:415-416, :429-431, :455` — **minor** — `.shift(-1)` "emit one bar early" alignment trick: if the last bar of the frame is an entry candidate it shifts onto NaN/False and is dropped — acceptable — but `sl_price.shift(-1)` can move an SL computed from bar N's close onto bar N+1 whose close differs, so the emitted `sl_price` no longer matches the documented "ATR-based distance from the entry close" (`:15`) whenever closes differ. Contract drift between docstring and indicator output.
- `:216-219` — **minor** — synthetic-day fallback (`range(len(df)) // 100`) silently fabricates calendar structure for non-datetime frames; combined with `minute_of_day = range(len(df))`, entry windows become arbitrary. Better to raise for undated data in a live-trading-capable strategy.
- `:391` — **minor** — reverse-mode swap uses `.copy()` on one side only (`long_candidates, short_candidates = short_candidates.copy(), long_candidates.copy()` — both copied, fine) — verified OK; noting checked.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Long Function**: `generate_signals` is ~400 lines (`:121-527`) doing parameter parsing, resampling, regime logic, entries, exits, metadata contract, and drawings — at least five reasons to change (Divergent Change within one function).
- **Duplicated Code**: the triple `params.get(x, params.get(X, default))` snake/camel dual-key pattern repeats 17 times (`:123-183`); a small parameter-resolution helper (or `resolve_strategy_parameters` from runner, already used by scripts) would delete ~60 lines.
- **Data Clumps**: `entries/exits/indicators/drawings` 4-tuple return contract is positional and undocumented at the signature; consumers (scripts/analyze_mt5_parity.py:399) rely on tuple order.

**(d)** This is the parity-benchmark strategy — highest-value file to keep under tight test; the bare-except fallback above directly undermines that role.

### `scripts/regenerate_mt5_parity_artifacts.py` (491 lines)

**(a) Bugs / correctness risks**

- `scripts/regenerate_mt5_parity_artifacts.py:276-279` — **minor** — exit-cursor semantics: an entry whose fill index precedes all remaining exits consumes one exit per trade strictly in order; if two entries map to overlapping exit windows, trades can pair with the *wrong* exit bar (documented as fast-path parity, but worth a comment).
- `:156-158` — **minor** — `_load_ohlcv` skips unusable candidate parquets with only a stderr note; if the *primary* source fails and only a bid/ask variant loads, artifacts silently change provenance (the summary does record `sources`, good).
- `:326` — **minor** — `qty = fixed_risk / (risk_stop_distance * risk_unit_value)`; when `stop_loss` is NaN the fallback `risk_stop_distance = entry_price * 0.02` (`:324`) invents risk geometry not present anywhere else — silently different sizing for stop-less signals.
- `:479` / `:384` — **minor** — `--risk-pct` argument parsed and threaded through `regenerate_case(...)` but never used inside (`fixed_risk_amount` comes from strategy params). Dead parameter misleading operators into thinking they changed risk.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Long Function**: `_build_trades` (~118 lines, `:249-367`) mixes matching, stop replay, FX conversion, lot rounding, and model construction.
- **Duplicated Code**: `_to_iso`/tz-normalize logic re-implemented here (`:183-189`, `:204-208`) and again in analyze_mt5_parity (`_utc_timestamp`) — third copy of the same helper family.
- **Magic numbers**: hard-coded date range `"2018-01-01"–"2026-06-23"` duplicated across COMPARISON_CASES *and* `main()` (`:482`); changing the window requires editing four places.

**(d)** Fine as an artifact generator; keep parity cases data-driven.

### `strategies/custom_swings_supertrend/custom_swings_supertrend.py` (417 lines)

**(a) Bugs / correctness risks**

- `custom_swings_supertrend.py:298-309` — **major-borderline/minor** — intra-bar sequencing assumes stop is checked before TP on the same bar (`if low[i] <= sl: ... elif high[i] >= tp:`); on bars that touch both, the loss side always wins regardless of bar open/close order. Conservative, defensible — but the opposite convention exists in some engines, so cross-engine comparisons will differ. Also breakeven (`:287-290`) is applied using *current* bar high before the stop check uses it on the same bar — look-ahead within the bar (BE move triggered by this bar's high then surviving this bar's low check). Same-bar state mutation ordering is the classic backtest bug class; flagging as major-borderline because it inflates results whenever BE trigger and SL are hit on one bar.
- `:45-66` — **minor** — Python-loop ATR over full frame each call: O(n) pure-Python loop will be slow on large 1m datasets (perf, not correctness).
- `:169-181` — **minor** — dual calling convention (positional args *or* dict-as-first-arg) handled by runtime type sniffing of `st_period`; works but makes signatures untypeable and invites silent mis-binding when callers pass `(df, params_dict, ema_fast)`. Primitive Obsession / awkward API.

**(b) Security** — nothing found.

**(c) Baseline smells**

- **Long Method**: `generate_signals` ~250 lines including the LONG/SHORT mirrored management blocks.
- **Duplicated Code**: the entire LONG branch (`:279-320`) mirrors the SHORT branch (`:321-361`) line-for-line with sign flips; a sign-vector approach halves it.

**(d)** The honest "what was not ported" docstring is exemplary.

### `scripts/regenerate_backtests.py` (322 lines)

**(a) Bugs / correctness risks**

- `scripts/regenerate_backtests.py:201-208, 211-218` — **major** — `main()` unconditionally purges **all** SQLite backtest rows and deletes every `*.trades.json` before regenerating; if any strategy run then fails (`:297-306`, failures are collected and tolerated), the historical backtest corpus is gone with no backup. A destructive bulk-delete as an unconditional first step of a regeneration script is a data-loss footgun (no `--yes`/dry-run gate). Severity major for an operational script.
- `regenerate_backtests.py:126-147` — **minor** — `_select_symbol_timeframe` silently assigns whatever symbol/timeframe is alphabetically first when meta gives no hints; regenerated results for the same strategy can change instruments between runs depending on which parquets exist. Non-deterministic artifact generation.
- `:38-43` — **minor** — DATA_DIR re-resolution duplicated locally (third copy in scripts; cf. adaptive_harvester) instead of importing `shared.constants.DATA_DIR`.
- `:242` — **minor** — skipped signal-processors are recorded in `failures` list though semantically they're skips — summary conflates failures and skips (misleading ops signal).

**(b) Security** — nothing found beyond the destructive-default above (which is safety rather than security).

**(c) Baseline smells**

- **Long Function**: `main()` ~100 lines doing purge, index build, iteration, engine dispatch, reporting.
- **Shotgun Surgery (adjacent)**: timeframe priority list `:45` duplicates ordering knowledge that also exists in runner engines.

**(d)** The per-failure tolerance pattern is good; just don't pair it with upfront deletion.

### `scripts/matrix_queue_runner.py` (282 lines)

**(a) Bugs / correctness risks**

- `scripts/matrix_queue_runner.py:80-96` — **minor/major-borderline** — lock protocol is check-then-write without atomicity: two cron sessions can both pass `acquire_lock()` between `LOCK_PATH.exists()` and `write_text` (TOCTOU race on shared state — checklist item). Consequence is bounded (both run the queue; pgrep guard helps), but the file-lock intent is not actually guaranteed; use `O_CREAT|O_EXCL` or `fcntl.flock`.
- `matrix_queue_runner.py:66-77` — **minor** — `any_matrix_process_running` greps process list with regex `python.*runner\.cli matrix`; matches *this script's own* subprocess only by luck of timing, and would also match unrelated user processes containing that substring. Fragile liveness probe.
- `:191-198` — **minor** — `subprocess.run(..., timeout=int(TIMEOUT_SECONDS)+120)` has a timeout (good), but stdout is unbounded in memory until completion (`capture via PIPE`); a chatty matrix run could balloon RSS.
- `:227-249` — **minor** — `while True:` loop with `continue` comment claims cron coexistence but nothing sleeps or exits on repeated failure: a strategy that always fails instantly becomes a hot loop hammering subprocess spawns until lock TTL. Missing failure backoff/exit condition.

**(b) Security**

- `matrix_queue_runner.py:18-26` — **minor** — env-overridable paths (`MATRIX_OUTPUT_DIR`, `PYTHON_BIN`) flow straight into subprocess argv; fine for operator-controlled env, noted only because cron env differs from shell env (stale venv path → silent failure mode at `:210-218` returning "failed" forever).

**(c) Baseline smells** — **Duplicated Code**: status-row dicts built three times in `refresh_strategy_states` (`:124-156`). **Long Function**: `refresh_strategy_states`. **(d)** State JSON written non-atomically (`save_state`, `:60-63`) unlike adaptive_harvester's atomic writer — crash mid-write corrupts queue state.

### `scripts/generate_matrix_grid_overview.py` (264 lines)

**(a) Bugs / correctness risks**

- `scripts/generate_matrix_grid_overview.py:242` — **minor** — `datetime.utcnow()` deprecated since 3.12 and produces tz-naive timestamp labeled "Z" — checklist tz-naive item; use `datetime.now(timezone.utc)`.
- `generate_matrix_grid_overview.py:75-81` — **minor** — PSR computed from *per-trade PnL over initial capital* as the return series with `sqrt(252)` annualization regardless of trade frequency; statistically unconventional (mixes per-trade Sharpe with daily annualization) and inconsistent with runner's PSR. Overview-only, but the number shown to users is not comparable across strategies with different trade counts.
- `generate_matrix_grid_overview.py:9-11` — **minor** — hardcoded machine paths for both input (`/mnt/block-volume-data/...`) and output (absolute repo path); no CLI args at all — script is unrunnable elsewhere.
- `generate_matrix_grid_overview.py:143-144, 181-182` — **minor** — top-level script body does file IO with no error handling; a missing/corrupt report raises a raw traceback (acceptable for cron but noisy).

**(b) Security** — nothing found.

**(c) Baseline smells** — **Speculative Generality**: hand-rolled `normal_cdf` Abramowitz-Stegun approximation (`:37-42`) when `math.erf` exists in stdlib — the whole function is one line of `0.5*(1+erf(x/sqrt(2)))`. **Duplicated Code**: best-row selection repeated 5×(`:116-120`). **(d)** Module executes everything at import; zero functions — refactor into `main()` for testability.

### `scripts/seed_demo_backtests.py` (249 lines)

**(a) Bugs / correctness risks**

- `scripts/seed_demo_backtests.py:25-27` — **minor** — writes demo OHLCV parquets into the *real* DATA_DIR tree alongside production data (`data/ohlcv/yfinance/equity/AAPL/1h.parquet`); if real yfinance data later lands there it's silently coexisting with synthetic rows or vice versa. Demo data should live under an obviously-fake source name.
- `seed_demo_backtests.py:11` — **minor** — `pd.date_range(..., freq="h")` lowercase 'h' fine on modern pandas; on older pandas this warns. Cosmetic.

**(b) Security** — nothing found. **(c)** Smells: heavy literal duplication between res1–res4 blocks (Data Clump candidate: a small builder). **(d)** Fine as demo fixture.

### `scripts/run_discovery_grid_backtests.py` (211 lines)

**(a) Bugs / correctness risks**

- `scripts/run_discovery_grid_backtests.py:22` — **major-borderline/minor** — `DISCOVERY_PATH` hardcodes a dated absolute path `/mnt/block-volume-data/discovered_strategies/discovery_20260618_0752.json`; script is single-shot by construction and fails outright when that file rotates. Should be a CLI arg.
- `run_discovery_grid_backtests.py:151-159` — **minor** — `subprocess.run(timeout=900)` present (good) but a timeout raises `subprocess.TimeoutExpired` which is *not* caught in `run_trial`; one slow trial aborts the whole grid run before the summary is written.
- `:43-55` — **minor** — `wait_for_data` polls every 10 s for up to 30 min inside a serial loop; a permanently-missing symbol stalls all subsequent trials for 30 minutes each. No early exit when data will never arrive.
- `:83-94` — **minor** — `"keltner"` maps to `custom_swings_supertrend` strategy with Supertrend params named `keltner_period/keltner_mult` — Mysterious Name: discovery labels leak into strategy params, confusing future readers about which indicator actually runs.

**(b) Security** — nothing found.

**(c) Baseline smells** — **Duplicated Code**: result-dict assembly duplicated with matrix_queue_runner conventions. **(d)** Fine for a one-shot ops script otherwise.

### `scripts/regenerate_qc_risk_parity_artifact.py` (201 lines)

**(a) Bugs / correctness risks**

- `scripts/regenerate_qc_risk_parity_artifact.py:158-163` — **major-borderline/minor** — monkeypatches `risk_parity.download_prices` at module level to inject local prices; not thread-safe and, if `run_backtest` raises between patch and restore, the `finally` restores — OK — but any *other* thread importing/using the module concurrently sees swapped behavior. Acceptable in a single-threaded script; flagged as fragile seam (dependency injection via parameter would be cleaner).
- `regenerate_qc_risk_parity_artifact.py:57-58` — **minor** — `prices.index.tz_convert(None)` converts UTC→naive-UTC silently; combined with `run_risk_parity_backtest`'s date-string indexing this works, but naive datetimes re-enter the pipeline (checklist tz-naive item).
- `:126` — **minor** — `total_bars=int(prices.count().sum())` counts non-NaN cells across all tickers, not bars — mislabeled stat (`total_bars` should be row count).

**(b) Security** — nothing found.

**(c) Baseline smells** — **Duplicated Code**: `_to_iso` is now the fourth copy of the same helper family across scripts. Parameters dict duplicated from `run_risk_parity_backtest.py:516-523` verbatim. **(d)** Fine otherwise.

### `scripts/recalculate_backtest_stats.py` (185 lines)

**(a) Bugs / correctness risks**

- `scripts/recalculate_backtest_stats.py:143-147` — **minor/major-borderline** — hard-fails when `--data-dir` differs from resolved `DATA_DIR`; well-intentioned guard but makes the flag useless (it can never legitimately differ), so the CLI lies about its own capability. Either honor the flag or drop it.
- `recalculate_backtest_stats.py:44, 101` — **minor** — equality check `merged_stats == existing_stats` compares dicts with float values freshly recomputed; float repr instability can cause spurious "updated" writes each run (idempotency-by-luck). Toleranced comparison would be safer.

**(b) Security** — nothing found. The dry-run default-off plus explicit flags design is good; `_save_json_payload` has a PermissionError fallback rewrite path that could surprise (writes dotfile then replaces) but is atomic-ish.

**(c) Baseline smells** — **Duplicated Code**: SQLite and JSON recalc loops are structurally identical (`:25-55` vs `:86-112`) — one generic pass over a (loader, saver) pair would do. **(d)** Fine.

### `scripts/download_discovery_data.py` (141 lines)

**(a) Bugs / correctness risks**

- `scripts/download_discovery_data.py:22` — **minor** — same hardcoded dated discovery path as run_discovery_grid_backtests (both must be edited in lockstep — Shotgun Surgery).
- `download_discovery_data.py:63` — **minor** — `end = now + 1 day` for intraday yfinance downloads; harmless but requests future bars.
- `download_discovery_data.py:99-106` — **minor** — "4h" built by resampling 60m data; pandas `'4h'` resample aligns to midnight boundaries while Yahoo 4h candles may align differently — parity nuance only.
- `download_discovery_data.py:114-117` — **minor** — retry loop catches bare `Exception` and sleeps up to 120 s per attempt ×4 — no distinction between fatal (bad symbol) and transient errors; worst case ~8 min per dead symbol.

**(b) Security** — nothing found (public data API).

**(c) Baseline smells** — minor duplication of trial-loading with sibling script. **(d)** Fine.

### `scripts/verify_ctrader_order.py` (115 lines)

**(a) Bugs / correctness risks**

- `scripts/verify_ctrader_order.py:35-36` — **major-borderline/minor** — hardcoded demo order/account ids (`27678592`, `47644617`) as defaults; running the script without env vars verifies a stale historical order and reports success — misleading verification artifact. Defaults should be required args.
- `verify_ctrader_order.py:24-26` — **minor** — `asyncioreactor.install()` at import without checking an existing reactor; re-import in an embedding process raises. Script-only context mitigates.
- `:43-46` — **minor** — reaches into adapter privates (`_ProtoOAOrderDetailsReq`, `_send_request_with_refresh_retry`) — same Message Chains smell as live_smoke.

**(b) Security**

- `:113-115` — **minor** — report JSON includes deal details; fine, but written into repo `reports/` which gets committed (same note as live_smoke).

**(c)** Smells: none further. **(d)** Retry/backoff logic here is actually good practice worth copying into sibling scripts.

### `scripts/dedupe_executions.py` (112 lines)

**(a) Bugs / correctness risks**

- `scripts/dedupe_executions.py:88-93` — **minor/major-borderline** — the SELECT identifying duplicates and the DELETE are two separate statements without a transaction wrapper around both; a new duplicate execution committed by the live daemon between the two statements is harmless (delete targets precomputed rowids), but conversely a *concurrent legitimate write* shifting rowids is impossible in SQLite (rowids stable), so risk is low. However there's also no re-check that rows still exist before delete — safe due to `WHERE rowid IN`. Noting verified-safe, borderline documentation gap only.
- `dedupe_executions.py:26-33` — **minor** — dedupe identity `(signal_id, filled_qty, timestamp)` treats two *legitimate* partial fills of equal size at the same timestamp as duplicates; given millisecond timestamps unlikely, but the docstring acknowledges the heuristic. Keep as documented caveat.

**(b) Security** — nothing found. Dry-run flag + parameterized expanding bind list = good practice.

**(c)** Smells: none material. **(d)** Good maintenance script.

### `scripts/consolidate_parquets.py` (106 lines)

**(a) Bugs / correctness risks**

- `scripts/consolidate_parquets.py:77-84` — **major-borderline/minor** — writes merged parquet to target path then deletes fragments; if the process dies *between* write and unlink, next run re-merges (fine). But if it dies *during* `to_parquet`, the canonical target file can be truncated/corrupt while fragments were not yet deleted... actually fragments survive (unlink after), so recovery is possible — but the corrupt canonical file now shadows good fragments on next run since `_canonical_target(fragment) == target` and merge would include the corrupt file's read failure → crash loop until manual cleanup. Non-atomic replace (no temp+rename like adaptive_harvester) is the root issue.
- `consolidate_parquets.py:70` — **minor** — `drop_duplicates(subset=["timestamp"], keep="last")`: "last" depends on fragment iteration order (`sorted(set(members))` by path string) — deterministic but arbitrary precedence between overlapping sources.
- `:13-18` — **minor** — fourth local copy of DATA_DIR resolution.

**(b) Security** — nothing found.

**(c)** Smells: **Long Function** mild. **(d)** Use atomic temp-file rename as adaptive_harvester does.

### `scripts/backtest_to_validation_ref.py` (58 lines)

**(a) Bugs / correctness risks**

- `scripts/backtest_to_validation_ref.py:20-23` — **major-borderline/minor** — reconstructs `Trade(**t)` from raw dict; `Trade.from_dict` exists in shared/models and handles missing optionals, while this direct constructor raises `TypeError` on any legacy payload missing a required field (e.g. pre-`position_size` files). Also bypasses the canonical parser's exit handling.
- `backtest_to_validation_ref.py:46-47` — **minor** — `res.meta.data["symbols"][0]` / `["timeframes"][0]` KeyError on results with empty data dicts; no fallback.

**(b) Security** — nothing found. **(c)** Duplicates `run_risk_parity_backtest.build_validation_reference` conversion logic (Duplicated Code). **(d)** Fine otherwise.

### `scripts/compare_strategy_metrics.py` (57 lines)

Thin CLI wrapper over `validation.statistics.cross_platform.build_comparison_matrix`; args sane, output written atomically-enough for reports. Default output filename embeds a date (`..._20260630.json`) that will silently go stale (Mysterious Name / config rot) — **minor**. Scanned, nothing else found.

### `scripts/generate_golden_files.py` (65 lines)

**(a)** `generate_golden_files.py:10,16` — **minor** — `PROJECT_ROOT` computed twice (dead first assignment). Fixture regeneration exits 1 when a source XLSX is missing — good fail-loud behavior. Nothing else found. **(b)** Nothing. **(c)** Hardcoded report list duplicates COMPARISON_CASES knowledge in regenerate_mt5_parity_artifacts.py (Shotgun Surgery when cases change). **(d)** Fine.

### `scripts/overnight_strategy_discovery.py` (125 lines)

**(a) Bugs / correctness risks**

- `scripts/overnight_strategy_discovery.py:94-104` — **major-borderline/minor** — despite the docstring promising "auto-discover trading strategies", the trial loop builds a config it never runs (`# Note: Real implementation would need actual strategy files`) and writes `"status": "mock"` results. Downstream consumers (`scripts/run_discovery_grid_backtests.py`, `download_discovery_data.py`) read these JSONs as if they were real discoveries. The pipeline's head is a stub wearing production clothing — either finish or gate it.
- `overnight_strategy_discovery.py:21-24` — **minor** — `except ImportError: DATA_DIR = ...` fallback silently diverges path resolution between environments.
- `:27` — **minor** — module-level `DISCOVERED_DIR.mkdir(...)` side effect at import time.
- `:106-111` — **minor** — bare `except Exception` records error but continues counting toward "Completed N trials" as if successful.

**(b) Security** — nothing found. **(c)** Speculative Generality (the mock scaffold itself). **(d)** Low value as-is; highest-priority candidate for deletion-or-completion in scripts/.

---

## Group 6 — strategies/ (remainder) + root config files

### `strategies/volatility_breakout/volatility_breakout.py` (194 lines)

**(a)** `volatility_breakout.py:74-82` — **minor** — session-open computed with a Python loop over all bars (slow); `:63-64` synthetic-day fallback (`//7`) fabricates sessions for undated frames like its sibling strategy. `:89` entry window uses `<=` max_entry_minute inclusive while sibling uses `<` — two "identical" strategies with subtly different windows (Duplicated Code divergence). **(b)** Nothing. **(c)** Near-duplicate of `strategies/intraday_volatility_breakout` with drifted semantics — extract shared core or delete one (strongest Duplicated Code case in the repo area). **(d)** Same parity-criticality note as sibling.

### `strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py` (243 lines)

**(a)**
- `qg_30min_es_breakout_strategy.py:170-171` — **major-borderline/minor** — both entry conditions test `close < breakout_*`: `enter_long` requires ADX<30 AND close below a long-breakout level computed as `low + 3.3*|open-low|`; given `(open-low)` ≥ 0, `breakout_long ≥ low`, so "close < breakout_long" fires on ordinary down bars — combined with the Pine notes admitting stop-order semantics were replaced by signal logic, longs here are effectively "buy weakness in low ADX", plausibly inverted from the original intent (original enters on stop-order *above* the level). The meta honestly documents the deviation; still flagging because an inverted entry sign silently flips the strategy.
- `:140` — **minor** — `timestamps_ny.iloc[i].date()` on NaT rows raises; `errors="coerce"` upstream can produce NaT → crash mid-loop on dirty data.
- `:197` — **minor** — `"sl_price_at_exit": ("exit", sl_price_at_exit)` stores a tuple where every other indicator key holds a Series — contract anomaly that will break generic indicator iteration downstream.

**(b)** Nothing. **(c)** Long-ish stateful loop fine; honest deviation notes in STRATEGY_META are good. **(d)** Verify intended direction against source Pine before trusting backtests.

### `strategies/mean_reversion_rsi_bb/mean_reversion_rsi_bb.py` (152 lines)

**(a)** `mean_reversion_rsi_bb.py:57` — **minor** — `avg_loss.replace(0, 1e-10)` makes RSI pin at ~100 when no losses in window; sibling `_calc_rsi` in post_vix_dip_buy uses NaN — two RSI implementations with different zero-handling (Duplicated Code with behavioral divergence). Entry/exit same-bar semantics standard. **(b)** Nothing. **(c)** RSI/ATR/TR block duplicated across ≥8 strategy files — extract shared indicators module (biggest systematic duplication in strategies/). **(d)** Fine.

### `strategies/gold_momentum/gold_momentum.py` (147 lines)

Same family as mean_reversion_rsi_bb: duplicated RSI/TR/ATR code, identical drawings block. Long-only by construction (no short branch) though MACD cross-down exits exist — consistent. Scanned; nothing beyond family-level duplication. Nothing found at file-specific level beyond above.

### `strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py` (146 lines)

**(a)**
- `pine_superosc_vumanchu.py:146` — **major** — returns a **3-tuple** (`entries, exits, indicators_dict`) while the platform contract everywhere else (and the engine adapter) expects 4 (`entries, exits, indicators, drawings`). Any consumer unpacking four values crashes; any consumer using positional `result[3]` gets IndexError. Contract violation.
- `:114-125` — **minor** — stop checked before TP on the same bar (conservative convention), and `stop_loss=0.0/take_profit=0.0` initial sentinels work only because prices > 0 — fragile sentinel use.

**(b)** Nothing. **(c)** Hand-rolled TR loops again (perf). **(d)** Fix the return arity before this strategy is used anywhere.

### `strategies/pine_supertrend_dema/pine_supertrend_dema.py` (124 lines)

**(a)**
- `pine_supertrend_dema.py:124` — **major** — same 3-tuple contract violation as pine_superosc_vumanchu (`return entries, exits, indicators_dict`, no drawings).
- `:43-44` — **minor** — band naming swapped relative to custom_swings_supertrend (`lower_band[i] = max(up, ...)` where `up = src - mult*atr`): internally consistent (lower band tracks support) but opposite naming convention to the sibling file — confusing cross-reading.

**(b)** Nothing. **(c)** Third copy of Supertrend implementation with slightly different warm-up behavior (starts at `period`, sibling at 1). **(d)** Consolidate Supertrend variants into one tested implementation.

### `strategies/post_vix_dip_buy/post_vix_dip_buy.py` (184 lines)

**(a)**
- `post_vix_dip_buy.py:89-90` — **major-borderline/minor** — when VIX parquet is missing, `vix_aligned` is filled with `vix_threshold + 1`, i.e. the regime gate is forced permanently *open*, so the "VIX spike" strategy silently becomes plain RSI+SMA dip-buying. It does emit `VIX_Fallback_Used`/`VIX_Data_Available` indicators (good), but trade results are produced under assumptions opposite to the strategy thesis with no error/warning.
- `post_vix_dip_buy.py:83-87` — **minor** — O(n×m) per-row `vix.index.date == current_date` scan over full VIX history per SPY row; on multi-year 1h data this is minutes of wasted compute (and `df["timestamp"]` tz-naive vs UTC-aware index comparison relies on both being dates).
- `:28, 21-22` — **minor** — unused `start`/`end` params and unused `os` import (dead params).

**(b)** Nothing. **(c)** Family duplication as above; hidden filesystem dependency inside generate_signals makes the strategy untestable without DATA_DIR fixtures (Feature Envy toward DATA_DIR / hidden I/O in a pure function contract). **(d)** Make VIX series an explicit input param.

### `strategies/trend_following/trend_following.py` (140 lines)

Scanned. Standard family pattern (SMA cross + ATR trailing). `trend_following.py:64` loop starts at 0 but guards NaN slow SMA — fine; trailing stop compares close < stop (close-based) while others compare low ≤ stop (intrabar-based) — cross-strategy inconsistency worth harmonizing, **minor**. Nothing else found.

### `strategies/short_term_reversal/short_term_reversal.py` (140 lines)

Scanned. Same family template. Note `short_term_reversal.py:57-58`: exit condition `ret_5d > exit_threshold` can be true on the *entry* bar itself (entry requires ret<-5%, exit >+3%, so mutually exclusive — verified safe). Nothing found beyond family duplication.

### `strategies/risk_parity/risk_parity.py` (140 lines)

**(a)** `risk_parity.py:62-63` — **minor** — `should_rebalance = i % rebalance == 0` keys off *bar index*, so rebalance cadence depends on where the data window starts and gaps (weekends) shift it — calendar drift vs the docstring's "rebalanced periodically" claim; also single-symbol adaptation of a portfolio strategy (documented). **(b)** Nothing. **(c)** `warmup = max(vol_period, trend_sma, 252)` hardcodes 252 regardless of params — magic number. **(d)** Fine.

### `strategies/sector_rotation/sector_rotation.py` (129 lines)

Scanned. `sector_rotation.py:60` hardcodes `max(lookback, 200)` — the 200 EMA period isn't parameterized although sma_200 is central to entry (magic number, minor). Otherwise family-standard. Nothing else found.

### `strategies/stat_arb_pairs/stat_arb_pairs.py` (130 lines)

**(a)** `stat_arb_pairs.py:37-41` — **minor** — pairs-trading reduced to z-score of *own* cumulative log returns ("spread proxy"); the pairs concept is gone entirely while META advertises pairs-trading (Mysterious Name / honest-simplification tension; docstring does say simplified). `:66-67` mean-reversion exit `z >= -exit_zscore` correct for long-from-below entries. **(b)** Nothing. **(c)** Family duplication. **(d)** Fine.

### `strategies/channel_breakout/channel_breakout.py` (128 lines)

**(a)** `channel_breakout.py:50` — **minor** — mutates `tr.iloc[0]` after building from concat (safe here — new object — but SettingWithCopy-adjacent pattern). `:79` uses prior-bar channel (`upper_channel.iloc[i-1]`) correctly avoiding self-inclusion. Trailing stop close-based vs low-based inconsistency again (**minor**, family-wide). **(b)** Nothing. **(c)** Family duplication. **(d)** Fine.

### `strategies/cross_asset_momentum/cross_asset_momentum.py` (138 lines)

Scanned. `cross_asset_momentum.py:37-38` — zip(lookbacks, weights) silently truncates to shorter list if user passes mismatched lengths (**minor**: validate lengths). Warmup math sound. Family duplication otherwise. Nothing else found.

### `strategies/pynecore_orb/pynecore_orb.py` (65 lines) & `pynecore_orb_context.py` (43 lines)

**(a)**
- `pynecore_orb_context.py:39` — **major-borderline/minor** — TP context formula `open + atr * sl_mult * tp_mult` disagrees with the Pyne script's `position_avg_price + atr * tp_mult` (`pynecore_orb.py:56`): the context multiplies TP distance by `sl_mult` too. With defaults (tp=2, sl=1) they coincide numerically, masking the bug until someone changes one multiplier. Real formula mismatch between paired files.
- `pynecore_orb_context.py:37-38` — **minor** — SL anchored to current bar `open_price` rather than entry average price; approximation documented nowhere.
- Naming: neither file implements an actual opening-range breakout (it's an SMA(or_length) × 4 crossover); "ORB" is inherited branding (Mysterious Name).

**(b)** Nothing. **(c)** The `_context.py` twin-file pattern is a clean way to give pynecore strategies a pandas-testable shadow — good design, undermined only by the formula drift above. **(d)** Add a parity assertion test between script inputs and context formulas.

### `strategies/pynecore_sma_crossover/pynecore_sma_crossover.py` (54) & `_context.py` (38)

Scanned. Context mirrors script faithfully here (SL = open − ATR·mult matches `position_avg_price − ATR·mult` approximation). `exit_reason` provided only in context, not script — asymmetric but harmless. Nothing found.

### `strategies/lab_generated_strategy/lab_generated_strategy.py` (70 lines)

**(a)** `lab_generated_strategy.py:59-65` — **minor** — indicators dict mixes Series with scalars (`fast_length`) and `None` (`sl_price`); consumers iterating Series-valued entries will trip on scalars. Contract looseness. **(b)** Nothing. **(c)** `_get_param` nested-dict handling exists precisely because STRATEGY_META parameters embed metadata dicts — schema ambiguity pushed into every consumer (Primitive Obsession; fix the meta schema instead). **(d)** Fine.

### `strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py` (58 lines)

**(a)**
- `ema_crossoiver_conversion_test.py:58` — **major-borderline/minor** — third file returning a **3-tuple** (`return entries, exits, indicators_dict`), violating the 4-value contract used by engines/scripts.
- Directory/file name misspells "crossover" as "crossoiver" (Mysterious Name, literally).

**(b)** Nothing. **(c)** Test-fixture strategy living in production strategies/ dir — gets picked up by registry, matrix runner, and regenerate_backtests (pollutes ops sweeps; Speculative Generality). **(d)** Move to tests/fixtures or rename+fix arity.

### `strategies/example_sma/example_sma.py` (27 lines)

Minimal reference strategy; version string "1.0" vs "1.0.0" elsewhere (cosmetic inconsistency). Scanned, nothing found.

### `strategies/__init__.py`, `strategies/intraday_volatility_breakout/__init__.py`

Empty package markers — scanned, nothing found.

---

## Root config files

### `pyproject.toml`

**(a/b)**
- `pyproject.toml:57` — **minor** — `[tool.hatch.build.targets.wheel] packages` omits `scheduler` (and `validation`, `scripts`): a wheel install cannot run the scheduler at all even though it ships `shared`/`risk`. Packaging drift.
- `pyproject.toml:66-67` vs `pytest.ini:2` — **minor** — duplicate `[tool.pytest.ini_options] testpaths`; pytest.ini wins, making the pyproject section dead config (two sources of truth).
- Dependencies pin `apscheduler>=3.10,<4` (good, v4 breaks API) and include heavyweight always-on deps (openai, optuna, ipython) for a runtime image — supply-chain surface remark only (**minor**).

**(c/d)** Fine otherwise.

### `pytest.ini`

Markers declared properly, timeout set, e2e/integration separated — good hygiene. `base_url http://localhost:5556` ties e2e to a local viewer port (fine for dev). Scanned, nothing found.

### `live_deployments.yaml`

**(a/b Security)**
- `live_deployments.yaml:1-13` — **minor** — real (paper) Alpaca account ids + broker_logins are committed; there is a `live_deployments.example.yaml` template, so the tracked live file should arguably be gitignored. Paper accounts lower the impact, but account identifiers in VCS is still hygiene debt.
- `live_deployments.yaml:31,54,77` — **minor** — `${DISCORD_BOT_NITRO_CHANNEL_ID}` style interpolation depends entirely on the loader performing env expansion; naive `yaml.safe_load` yields the literal string as channel id. Verify the loader expands before trusting these deployments.

**(c)** The three deployment blocks differ only in bot name/account/channel (Data Clumps → could be generated from a small matrix). **(d)** All deployments ship PAUSED by default — sensible safe default.

---

## Cross-cutting observations (area-level)

1. **Contract drift is the systemic risk**: 3 files violate the 4-tuple signal contract; SL/TP formulas drift between twin files; two "same-name" volatility breakout strategies diverge. One shared contract test would catch all of these.
2. **Indicator-code duplication** (RSI/ATR/TR/Supertrend) spans ≥10 files with behavioral differences (zero-handling, warm-up, band naming) — extract `strategies/_shared/indicators.py`.
3. **tz handling** is mostly exemplary (UTC-aware throughout) except the noted naive spots (`generate_matrix_grid_overview.py:242`, `regenerate_qc_risk_parity_artifact.py:57`, `shared/models.py:104`, analyze_mt5_parity entry-minutes).
4. **Destructive-by-default ops scripts** (`regenerate_backtests.py`) and **mock-pipeline heads** (`overnight_strategy_discovery.py`) are the two highest-leverage fixes outside risk/engine unit conflation.

## Files scanned with no findings (explicit list)

- `risk/config.py`, `risk/__init__.py`
- `scheduler/__init__.py`, `scheduler/jobs/__init__.py` (wiring note recorded under jobs)
- `signal_bots/__init__.py`, `signal_bots/{ramjet,rocketfuel,nitro}/__init__.py`
- `shared/__init__.py`, `strategies/__init__.py`, `strategies/intraday_volatility_breakout/__init__.py`
- `pytest.ini`
- File-specific-nothing-found beyond family-level duplication: `gold_momentum.py`, `trend_following.py`, `short_term_reversal.py`, `sector_rotation.py`, `stat_arb_pairs.py` (one naming note), `channel_breakout.py`, `cross_asset_momentum.py`, `pynecore_sma_crossover*.py`, `example_sma.py`, `compare_strategy_metrics.py` (stale-date note)

## Finding counts by severity

Counted per bullet (borderline items counted at the stated severity):

| Severity | Count |
|---|---|
| Critical | 0 |
| Major | 9 |
| Minor | 78 |

Major items: database dead-backfill + stats namespace conflation (2), risk engine exposure-unit conflation (1), risk/live trust-boundary config (1), scheduler orphan-job purge (1), live_smoke cTrader worker flatten guard (1), regenerate_backtests destructive purge (1), pine_superosc 3-tuple (1), pine_supertrend_dema 3-tuple (1). Items explicitly labeled "major-borderline/minor" in text are counted under Minor per their final label; the nine above are the ones carried at major.
