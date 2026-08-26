## misc-backend

**Expert-panel merged review — model ox-alpha — 3 blind reviewers (r1, r2, r3).**
Area: risk engine, scheduler, operational scripts, shared infrastructure, signal bots, trading strategies.
Scope: 71 tracked code files in `risk/`, `scheduler/`, `scripts/`, `shared/`, `signal_bots/`, `strategies/`, plus root-level `pyproject.toml`, `pytest.ini`, `live_deployments.yaml`.
Method: reviewers worked blind and independently; this document is a pure merge/dedup preserving all original citations (`file:line` + reviewer tag). Contradictions are settled by reading code under the area directories, never by vote; settlements are logged inline as **[Settlement Sn]** entries appended after verification. The external reference report was consulted only for entries relevant to this area and treated strictly as hints-to-verify against reviewer citations; agreements/disagreements with it are flagged where material.
Severity scale: critical (data loss / money loss / security breach), major (correctness bug or likely production incident), minor (smell, robustness, style-with-consequence). Baseline smells use Fowler names; where a reviewer explicitly tagged a smell finding at major it is carried at major, otherwise smells default to minor.

---

# Group 1 — `shared/` infrastructure

## shared/database.py (3,891 lines)

### (a) Bugs / correctness risks

- **major — import-time `ENGINE` + import-time `load_dotenv` freeze configuration.** `shared/database.py:70-77` creates the SQLite engine at import against `DATA_DIR/backtests.db`; `load_dotenv()` runs at import (r1: line 61; r2: 60-63; r3: 61, citing `constants.py:141`). Env-dependent paths and the engine URL freeze at first import order; tests/tools setting `DATA_DIR` afterwards silently target the wrong DB; there is no way to inject or re-point a session/engine (r2 notes ~50 functions call `init_db()` at top; tests monkeypatch globals). *Severity history:* major (r1, r3) vs minor (r2, who split it into two minors) — carried at major (2-of-3 + root-cause-of-several-seams agreement).
- **minor — `subprocess.run(["git", ...])` without `timeout=`** at `shared/database.py:739-745` (inside `upsert_verification_evidence`); a wedged git blocks the calling thread indefinitely. All three reviewers flagged minor; `shared/sentry.py:267-274` (`timeout=2`) is the in-repo pattern to copy (r1, r2, r3).
- **minor — duplicated statement in migration:** `execution_column_names = _table_column_names(connection, "live_executions")` executed twice back-to-back at `shared/database.py:1278-1285` (r1: 1280-1282; r2: 1278-1285; r3: 1277-1282). Harmless copy-paste residue. *Severity history:* r3 labeled this bullet "major" while describing it as harmless residue — clear scale disagreement, recalibrated to minor (all three descriptions agree it is functionally harmless). **[Settlement S4: verified in source — see log at S4.]**
- **⚠ disputed — `load_live_account_stats_inputs` conflates account ids, deployment ids and strategy names in one IN-list** (`shared/database.py:2297-2302`): set seeded with `normalized_account_id`, then each deployment id *and* raw `strategy_name` added, all queried as `live_signals.deployment_id IN (...)`. r1: minor (fuzzy legacy-compat, misattribution only on collision); r2: **major** ("conflating three identifier namespaces … correctness hazard for money statistics"); r3: minor (deliberate undocumented legacy-compat hack). External reference MISC2 rates the same code **major**. Flat reviewer-vs-reviewer contradiction → settled by code read. **[Settlement S4 — see log.]**
- **dead code — `_backfill_curve_for_id` (`shared/database.py:1066-1091`)** defined, never called anywhere in the repo (r1 grep-verified; r2 grep-verified, actual backfill path is `_equity_points_curve` + `_artifact_equity_curve` at 1223-1248). Speculative Generality. *Severity history:* minor (r1) vs major (r2) vs folded-into-traversal minor (r3) — recalibrated per settlement. Related: the dead helper performs the `DATA_DIR/"backtests"/f"{bt_id}.trades.json"` join **without** the `Path(str(bt_id)).name == str(bt_id)` traversal guard that `database.py:1262` applies to live callers (guard evaluated after `_artifact_equity_curve` at 1225 already built paths from `bt_id`) — inconsistent defense-in-depth (r1, r3). **[Settlement S4/S5 — see log.]**
- **minor — two competing "now" conventions:** `_utcnow_naive()` (324-326) stores naive UTC while `_now_iso()` (693-694) writes tz-aware ISO strings; mixed naive/aware datetime handling across tables; `ServiceModel.last_activity` accepts arbitrary caller datetimes (533, 1862) with no normalization, so a local-time caller poisons `created_at.desc()` ordering (r1, r3; companion: `shared/models.py:104` naive default, r2/r3 under models). Checklist tz-naive item.
- **minor — `save_portfolio` positional component-id coupling:** delete-all + re-insert enumerating `start=1` (3696-3713) while `load_portfolio` (3735) pairs persisted `component_id`s positionally over reloaded rows; docstring at 3655 says "positional default". Correct today, fragile off-by-one/wrong-pairing surface (r1, r3).
- **minor — `get_grid_results(limit)` has no upper bound** (3843/3889), unlike every other list function's `_bounded_query_limit`; a caller passing 10**9 loads the table (r1).
- **minor — `load_backtest_result` does not normalize/strip `backtest_id`** (3624-3627) unlike sibling functions' `normalized_id` pattern (r2).
- **minor — `list_verification_evidence_summary` fetches `limit*20` rows then groups in Python** (2860-2867): summaries can silently drop flows past the fetch cap (r2) and deserialize up to ~40k rows through Pydantic per request where a GROUP BY would do (r3).
- **minor — fragile `BEGIN IMMEDIATE` handoff:** `save_live_execution_up_to_cumulative` opens it via `session.connection().exec_driver_sql(...)` (1420-1456); works only because the session has not autobegun; future ORM-read-before-this-line refactor yields "cannot start a transaction within a transaction". Needs comment/test (r2).
- **minor — `list_live_signals_for_deployments` loads executions for up to 1000 signals with no LIMIT** (2145-2149); chatty fan-out ⇒ unbounded query (r3).
- **minor — slippage sign convention unsigned relative to side** (`database.py:2252`): favorable fills on sells also record positive slippage; consumers must know action to interpret; field name doesn't convey directionality (r3).
- **minor — race/idempotency shape:** lookup-before-insert patterns depend on unique-index invariants rather than one atomic upsert (reference hint misc-m5 at `database.py:1390-1408`; consistent with r2's transaction-handoff note above — adopted as minor, same seam).

### (b) Security

- **minor — raw-SQL hygiene is broadly good, with two pattern risks.** Values are bound throughout (`:id_N` placeholders at 3331-3336; f-strings interpolate only generated placeholder names / quoted identifiers at 883-885, 939, 946) (r1, r2). Caveats: (i) `_rebuild_live_signals_without_global_discord_unique` executes `text(rebuilt_sql)` (893) where `rebuilt_sql` comes from `sqlite_master.sql` — exploitable only by someone who can already write the schema; informational but string-built DDL execution (r1); (ii) `PRAGMA table_info('{table_name}')` f-string (841, 939/947): all current call sites pass literal constants so not exploitable today, but no `^[A-Za-z_]+$` guard invites injection on first dynamic caller (r2, r3); (iii) `_equity_preview_query_params` interpolates int-coerced `normalized_limit` into SQL arithmetic (3327-3418, coerced at 3462-3466) — safe as written, fragile pattern (r2).
- **minor — `load_dotenv(PROJECT_ROOT / ".env")` executes at import in a shared library,** three times per process across `database.py:61`, `constants.py:36`, `sentry.py:18`; `.env` shadowing silently changes production behavior depending on import order (r3; same root as the major import-freeze finding above).

### (c) Baseline smells

- **God module / Divergent Change (carried at major per r3's explicit tag):** one 3,891-line module mixes ≥6 reasons to change: engine+PRAGMAs, schema migrations (845-1308), live signal/execution/balance/account persistence (1310-2030), service registry (1768-1882), background tasks + verification evidence (2668-3233), backtest/portfolio/grid CRUD (2319-3891) (r1, r2, r3 — r3 prescribes split per reason, e.g. `storage/backtests.py`). De-facto import hub for runner/live_runner/viewer/scheduler/scripts (20+ dependents, r2) maximizing blast radius.
- **Duplicated Code — row unwrapping ×5-6 (carried at major per r3's tag):** `_normalize_task_record` (805), `_coerce_backtest_row_id` (2475), `_extract_backtest_and_trade_count` (2576), `_unwrap_equity_point_row` (3270), `_unwrap_model_row` (3481), inline again at 2639-2645 — five/six implementations of one "_mapping/tuple/model → model" shape (r1, r2, r3). Related: `_bounded_query_limit` (2051-2057) duplicates the inline try/int/min pattern at 1581-1584, 1619-1622, 2820-2823 (r1); normalize-id/init_db/session preamble repeats across functions (r2); `save_result_to_sqlite` delete-children block (2323-2326) duplicates `delete_backtest`'s (3261-3264) (r3).
- **Repeated Switches — signal-source normalization ×3 (carried at major per r3's tag):** the `{"internal","discord"}` juggling re-implemented at 591-599 (`from_live_deployment`), 624-643 (`to_live_deployment`), 1906-1923 (`_resolve_deployment_signal_source`) — subtly different copies; changing the allowed-source set needs three coordinated edits (r1, r2, r3).
- **Primitive Obsession:** statuses/types/ids/timestamps as bare `str` (571 `status: str = Field(default="ACTIVE")`; ISO strings compared lexically at 1607, 2903, 3843; task statuses vs `_FINAL_TASK_STATUSES` string set at 95-102) despite existing enums; drives the defensive `.strip().lower()` calls repeated ~30×; mixed datetime columns (live tables) vs str columns (tasks/evidence) for the same concept (r1, r2, r3).
- **Data Clumps:** the ten-kwarg `(task_id, task_type, status, config_json, progress_message, name, description, source, created_at, updated_at)` threaded through the evidence/task helpers (2922-3134) — bundle into a TaskSnapshot (r3; r1/r2 note the same clump family).
- **Hand-rolled migration subsystem:** `_migrate_schema` + 10 `_add_*`/`_migrate_*` helpers (845-1307) re-implement Alembic; steps silently depend on execution order (backfill before uniqueness at 1024-1027); no versioning ⇒ fresh vs legacy DB exercise different paths per boot; DDL regex-rebuild brittle (r1, r2, r3). Extract/dedicate a migrations module.

### (d) Maintainability hot spots
- `_equity_preview_query_params` (3327-3418) embeds two large raw SQL strings that must mirror `equity_points` schema by hand (r1).
- Migration framework deserves extraction with tests isolated from business queries (r2, r3).

## shared/models.py (749 lines)

### (a) Bugs / correctness risks
- **minor — `Trade.direction_sign` returns `-1` for anything not exactly `"LONG"`** (418-429 / 427-429): `"long"`, `"Long"`, corrupted values silently treated SHORT while PnL math flips sign; `direction` never validated against `VALID_DIRECTIONS` (declared at models.py:284) anywhere in the module (r1, r2, r3). Companion: `Trade.is_winner` treats `pnl == 0` as a loser (r2).
- **minor — contradictory contract on exits:** comment says `exits` has "at least 1 element" (340) yet `last_exit_time` falls back to `entry_time` (423-425); the fallback hides corruption instead of raising (r1).
- **minor — tz-naive default:** `Service.created_at` defaults naive UTC (`replace(tzinfo=None)`, models.py:104) while `LiveSignal.timestamp` stays aware — inconsistent time semantics in one file (r2, r3).
- **minor — two sources of truth for vocabularies:** `Literal["alpaca","ctrader"]` (161) / `Literal["ACTIVE","PAUSED","STOPPED"]` (186) validated independently in database.py, which accepts lowercase statuses for services but uppercase-only for deployments — already drifted (r2). `VALID_TIMEFRAMES`/`VALID_DIRECTIONS` re-declared here (283-284) duplicating `shared/constants.py:14-15` (r1, r2, r3).
- **minor — `datetime.fromisoformat(...replace("Z","+00:00"))` (744)** parses naive timestamps fine and feeds `strftime` filename generation without tz normalization (r1).
- **minor — unvalidated id/name → path construction:** `BacktestResult.filename` (748) lets strategy names containing `/` or spaces flow into filenames; `save()` `mkdir(parents=True)` (727) creates nested dirs unexpectedly (r3).
- Reference-hint cross-check: misc-m1 (numeric validation of prices/SL/TP in `risk/models.py:38-56`) — reviewers r2/r3 partially covered under risk/models finiteness gaps; kept there, not double-counted here.

### (b) Security — nothing found (pure data classes; JSON round-trip via stdlib) (r1, r2, r3).

### (c) Baseline smells
- **Duplicated Code:** `BacktestStats.from_dict` (620-625) and `PortfolioStats.from_dict` (667-672) byte-identical; the omit-empty `to_dict` loop repeated across `StrategyMeta` (52-73), `TradeExit` (302-324), `BacktestMeta` (491-534), `Trade.to_dict` (358-390) (r1, r2, r3).
- **Divergent Change:** mixes four domains — strategy metadata, live-trading API models, portfolio persistence, backtest canonical format (r1, r2, r3).
- **Inconsistent layering:** Pydantic BaseModel for live/portfolio models vs plain dataclasses with hand-written serialization for backtest models — two idioms, undocumented, trips newcomers (r1, r3).
- **Primitive Obsession / Data Clumps:** times as bare strings ("ISO 8601 UTC" by convention only); `position_size/risk_amount/risk_percent` triple travels through `Trade`, `TradeRecord`, `_apply_backtest_trade_risk_fallbacks` (`database.py:3524`) — RiskMath value-type candidate (r1, r2).

### (d) Hot spots — `Trade`/`TradeExit`/`BacktestResult` are the canonical interchange format with the area's highest fan-out (every field addition touches `to_dict`, `from_dict`, SQLModel records, viewer) (r1, r2).

## shared/constants.py (178 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — DATA_DIR resolution probes the filesystem and picks the highest-scoring home candidate when `DATA_DIR=~`** (`shared/constants.py:91-103` scoring weights 100/30/20/10 at `_data_dir_score`, resolution at 106-138/141): winner depends on `$HOME`, `pwd` ownership of the checkout (74: `pwd.getpwuid(PROJECT_ROOT.stat().st_uid)`), `SUDO_USER`, and existence of `ohlcv/` etc. Two processes under different users (systemd service vs operator shell) or an empty intended dir can resolve **different data directories** — hence different `backtests.db` files with live executions — with no warning (r1: major "split-brain"; r3: major nondeterminism + surprise-writes; r2: minor "surprising implicit behavior"). Carried at major (2-of-3; trading-data integrity consequence).
- **minor — import-time side effects:** `load_dotenv` at 35-36 plus filesystem stats (74) at import; importing constants mutates env-dependent global config; `load_dotenv` must precede `os.environ.get` (36→39) with no test guarding the order (r1, r2, r3).
- **minor — `VALID_TIMEFRAMES`/`VALID_DIRECTIONS` duplicated verbatim in models.py** (14-16 vs models 283-284) — drift risk (r2, r3; r1 from the models side).

### (b) Security — nothing found; read-only resolution, `resolve_executable` uses `shutil.which` (r1).

### (c) Baseline smells
- **Speculative Generality:** `_home_candidates` (50-88) gathers HOME/pwd/project-owner/SUDO_USER/USER candidates for a corner case a single documented expansion rule would cover (r2, r3).
- **Mysterious Name / magic numbers:** `_home_candidates`/`_data_dir_score` encode heuristic policy the names don't convey; weights 100/30/20/10 unexplained (91-103) (r1, r3).
- **Primitive Obsession:** timeframes as bare strings + tuple whitelist + alias map (14-21) rather than a small Timeframe type; every consumer re-validates (r1).

### (d) Hot spots — `_resolve_data_dir` + helpers (~100 lines) dominate module complexity; hidden global-config hub freezing whichever `.env` was readable first (r1, r2).

## shared/logging.py (100 lines)

### (a) Bugs / correctness risks
- **minor — `get_logger` check-then-act on `_loggers` without a lock** (74-99/74-75): concurrent first calls attach duplicate handlers → doubled log lines. Race-on-shared-state checklist item; low likelihood, self-limiting (r1, r2, r3).
- **minor — `LOG_LEVEL` frozen at import** (27, 78): later `os.environ["LOG_LEVEL"]` changes ignored — surprising in long-lived schedulers/tests (r1, r2, r3). r3 adds: `getattr(logging, LOG_LEVEL, logging.INFO)` maps typos ("INF O") silently to INFO.
- **minor — multi-process rotation unsafe:** `RotatingFileHandler` (85-90) across scheduler jobs/scripts/viewer writing the same `<service>.log` truncates/loses lines; no `delay=True` so every logger creation touches disk (r1).
- **minor — timestamps at seconds resolution, no milliseconds** (44-46): sub-second event ordering (order placement!) lost (r3).
- **minor — stale docstring:** module docstring says default log dir `~/logs` (13) but implementation uses `<repo>/logs` via constants (143) (r2, r3).

### (b) Security
- **minor — no redaction hook:** JSON formatter writes `record.getMessage()` (50-57) and arbitrary `extra={"data": ...}` payloads (58-60) unredacted to `logs/*.log`, in contrast with the sentry.py redaction standard; secrets passed as log args land in plaintext (r2, r3).

### (c) Smells — formatter construction duplicated inline (latent, r1); manual addHandler without clearing can stack duplicates if `_loggers` ever cleared (r3); `logging.config.dictConfig` would simplify (r3). All minor.

## shared/sentry.py (286 lines)

Overall well-built: validated frozen config dataclass, redaction pipeline, bounded git subprocess (r1, r2, r3).

### (a) Bugs / correctness risks
- **minor — `initialize_sentry` returns True on subsequent calls even for a *different* `service_name`;** second service silently inherits the first service's tag; `_INITIALIZED_SERVICE` recorded but never compared (dead outside tests) (r1, r2, r3). Check-then-act on `_INITIALIZED` without lock — same race shape as logging, low impact (r1).
- **minor — `capture_exception` pushes scope even when Sentry never initialized;** harmless no-op, callers can't distinguish captured vs disabled except by None event id (r2).
- **minor — redaction regex gaps:** `_SENSITIVE_ASSIGNMENT_RE` value class `[^,\s;&]+` (36-43 / 40-43 / 222) stops at comma/space/`&`, so JSON-style secrets with quotes/spaces escape; marker list misses `client_secret`/`private_key`/`access_key` raw occurrences (substring dict-key matching still catches keys) (r1, r2, r3).
- **minor — credential-adjacent identifiers not redacted:** `account_id`, `broker_login`, `ctid` ship to Sentry in contexts/tags (r3). `send_default_pii` env-toggleable to true with no production guard (107-111) (r3).

### (b) Security — positive: before_send walks the whole event; bearer/query regexes; `timeout=2` on git (the pattern `database.py:739` should copy) (r1).

### (c) Smells — Speculative Generality: `_reset_for_tests` ships in prod module (acceptable test hook) (r2); capture_exception indirection earns keep only via context param (r3); bool-parsing set duplicates `risk/models.py:_parse_bool` with a *different* accepted set — third truthy-string parser in the area, extract `shared/parsing.py` (r3). All minor.

## shared/strategy_registry.py (282 lines)

### (a) Bugs / correctness risks
- **major — `load_strategy_module` executes arbitrary strategy files** (`spec.loader.exec_module`, 100-107) and `iter_strategy_entries` (176-192) does so for *every* strategy file on every call; `find_strategy_entry` (220-226) loops all entries ⇒ N unrelated modules' top-level code runs per lookup (network/file/MT5 side effects seen elsewhere in this area). Combined with `update_strategy_display_name` writing files, a poisoned `strategies/` dir becomes arbitrary code execution for any importer (API server included). Major rather than critical only because the directory is developer-controlled/repo trust domain; no allowlist, no AST-only meta parse despite `ast` being imported (line 5) (r1 major; r2 major-borderline; r3 major, additionally noting modules loaded via `spec_from_file_location` are **not inserted into `sys.modules`**, breaking pickling/isinstance/dataclass edge cases, and `detect_strategy_type` loads the same file repeatedly per listing).
- **minor — non-atomic AST source rewrite:** `update_strategy_display_name` (239-267 / 235-260) splices source by AST offsets and `write_text`s the whole file back — no temp+rename; crash mid-write corrupts the strategy file, no backup; `repr(new_name)` splice can flip quote style; tolerant-parse vs hard-ValueError asymmetry on dicts whose "name" isn't a Constant; newline-bearing names would write invalid literals (r1, r2, r3).
- **minor — one broken strategy file aborts the whole listing** (176-192): no per-file try/except; a syntax error takes down registry/UI list (r3).
- **minor — folder-name convention enforced implicitly:** heuristic `path.parent.name != path.stem` (56) makes flat or differently-named strategies invisible; undocumented (r1, r3).
- **minor — `created_at` derived from file mtime** (41-45): checkout/copy rewrites provenance used for ordering/display (r1).

### (b) Security — see (a): dynamic execution of directory contents is the main exposure; no path traversal (paths come from rglob within repo) (r1; r3 carries the security angle at major).

### (c) Smells — Feature Envy (meta rummaging across four spellings `strategy_type`/`type`/tags at 118-144 — a STRATEGY_META schema would delete the guessing ladder) (r1, r3); Shotgun Surgery-adjacent slug/path resolution spread over four small functions (62-97) (r2). Minor.

### (d) Hot spots — no caching: `O(N) exec_module` per lookup, 200+ module imports per listing of 100 strategies (r1, r3).

## shared/__init__.py
Scanned — empty file, nothing found (r1, r2, r3).

---

# Group 2 — `risk/`

## risk/engine.py (277 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — exposure check compares fractions to currency units.** r2 (**major**): at 151-152, `remaining_exposure = max_total_exposure - exposure` then `risk_percent = min(max_risk_per_trade, remaining_exposure)`; `max_risk_per_trade` is a fraction of balance while `max_total_exposure`/`current_exposure` are treated as absolute currency (fed from metadata, default 0.0 per `risk/live.py:114`) — with `balance=100_000`, any `current_exposure < 0.05` leaves risk_percent ≈ 0.05 ⇒ **5% of balance risked (5× the configured cap)** whenever exposure is small; once exposure ≥ 0.05 currency units trading stops entirely. The two limits must share one unit; today the engine silently over-risks or blocks depending on convention. Related: r1 (**major**, engine.py:216-218): no upper bound on computed size — `raw_size = risk_amount/(risk_distance*pip)` only floored at 0, no notional/leverage cap vs `account_balance`; a stop a fraction away from entry or mis-specified `pip_value` produces effectively unlimited position, and `max_total_exposure` bounds `risk_percent` only, not notional — "the single most important missing invariant" for a risk engine. r3 (**major**, 151-153+217): no aggregation across concurrent calls + pip_value defaults 1.0 for any asset in live.py:113, forcing FX semantics on all assets with no unit documentation. These are complementary facets of one sizing/units defect family; merged here and settled by code read. **[Settlement S1 — see log.]**
- **⚠ disputed severity — exposure/position-count guards trust caller-supplied metadata.** r1 (major): `current_positions` from `signal.metadata.get("current_positions")`, `current_exposure` fed from metadata (`live.py:114`); engine never queries portfolio state; stale/omitted metadata silently disables both guards (defaults 0 → checks pass); concurrent signals each see the same stale counts and all pass (race-shaped). r3 (major): "risk engine trusts self-reported state"; adds `int(float(...))` truncation accepting 4.9 positions as 4 (127). r2 (minor): position-count enforcement only as honest as whoever fills the dict; nothing cross-checks it. Carried at major. **[Settlement S1 — see log.]**
- **minor — input mutation as side effect:** `calculate_position_size` sets `signal.stop_loss = stop_loss` and `signal.metadata["resolved_stop_loss"]` (183/176-184); `apply_trailing_stop` mutates in place too (271, 275). Retries non-idempotent; surprises callers holding the original signal; documented nowhere on the type (r1, r2, r3).
- **minor — `_signal_id` legacy composite id collisions:** deterministic id from symbol+timeframe+action+prices (48-60 / 52-59) collapses two genuinely distinct identical-parameter entries to one id (dedupe rejects the second legitimate entry); `.12g` float formatting collides past 12 significant digits; stability across JSON round-trips assumed, not guaranteed (r1, r3).
- **minor — `apply_trailing_stop` raises ValueError mid-loop** for out-of-range persisted `trailing_sl_config.percent` (262-264): one bad config row kills the whole trailing pass instead of skipping the signal (r1); r3 adds: percent 0.9 is legal yet almost certainly bp-as-fraction misconfiguration — no upper sanity bound/warning.

### (b) Security — nothing found (pure computation, no IO) (all three).

### (c) Baseline smells
- **Repeated Switches:** the `{"buy","long"}` membership test appears 4× (27, 43, via `_stop_matches_direction` ~186, 268) (r1, r3); action should normalize once to a direction enum/Side with `.is_long`.
- **Data Clumps:** `position_size/risk_amount/risk_percent/risk_reward_ratio/decision` travel through every private helper signature (94-111, 131-149…); they already have a name — `SizedOrder`; helpers just re-plumb fields individually; `_decision()` takes 8 params of which 4 are usually 0.0/None together (62-92); an `AccountState(account_balance, current_exposure, current_positions)` type would remove the half-parameter/half-metadata clump of position state (r1, r2, r3).
- **Speculative Generality / Mysterious Name (borderline):** `@dataclass(slots=True)` on `RiskEngine` (10-15) immediately overridden by hand-written `__init__`; decorator promises dataclass init behavior it doesn't have (r1, r2).
- **Duplicated Code:** rejection branches duplicate ~15 lines each across four/five sites (130-210/131-149, 154-172, 188-210, accept 221-242) differing only in reason/metadata — extract `_rejected(signal, reason, **meta)` (r1, r2).

### (d) Hot spots — `calculate_position_size` (113-242) mixes five concerns (limits, stop resolution, validation, sizing, decision assembly) in ~110-130 lines; decompose gate → resolve stop → size. Add a regression test pinning explicit currency/fraction semantics before touching anything else (r1, r2, r3).

## risk/config.py (30 lines)
Scanned clean, well-validated (`__post_init__` thorough) — all three.
- **minor (judgement call)** — no cross-field check that `max_risk_per_trade <= max_total_exposure` (8-12): engine papers over it with `min()` at engine.py:152, but a config where per-trade risk exceeds total exposure is silently accepted (r1). r3 notes type coercion in `__post_init__` accepts strings for numeric fields, papering over caller bugs.

## risk/models.py (115 lines)

### (a) Bugs / correctness risks
- **minor — `SizedOrder.__post_init__` coerces but never validates finiteness** (100-115), unlike `RiskDecision` (82-93): NaN/inf position_size flows through unchecked; only incidental NaN-comparison behavior of `max()` saves the main path (r1). Reference hint misc-m1/misc-m2 (non-finite prices/exposure passing validation) corroborate this seam — consistent, minor.
- **minor — `action` accepts four spellings** (`buy/sell/long/short`) normalized to lowercase but kept as four aliases forever (37, 47 / 45-49); downstream handles both families; `Literal[...]` normalization via `self.action = normalized # type: ignore[assignment]` bypasses the type checker, hiding future drift from mypy (r1, r2).
- **minor — `_parse_bool` inconsistent tri-state parsing** (30-32): accepts int 0/1 but rejects numpy bools / int 2; accepted set {true,1,yes} diverges from sentry.py's {1,true,yes,on} — same concept, divergent behavior (r3).
- **minor — `stop_loss=float(self.stop_loss)` accepts 0.0/negative stops** (53-57): no `> 0` check unlike `entry_price`; typo'd zero stop passes validation, rescued downstream only when `enforce_stop_direction` is set (off by default in direct engine use) (r3).

### (b) Security — nothing found (all three).

### (c) Smells — Primitive Obsession: `action` free string with runtime normalization instead of Direction enum; `rejection_reason` bare string with ad-hoc literals ("max_positions", "bad_stop_direction") (r1). Refused Bequest borderline: `RiskDecision` carries seven loosely-coupled optional floats; nonsensical combinations unconstrained — invariants live in the engine, not the model (r3). Minor.

## risk/live.py (133 lines)

### (a) Bugs / correctness risks
- **major — the explicit-qty path bypasses every risk limit.** When `metadata.qty`/`explicit_qty > 0`, a `SizedOrder` is constructed directly (45-85, core 69-75): no `max_positions`, no `max_total_exposure`, no `max_risk_per_trade`, no balance check, no finiteness enforcement; `enforce_stop_direction` never consulted because the engine is never called. Any upstream component (or Discord-sourced payload) setting `qty` opts out of the entire risk engine — contradicts the module's purpose; highest-severity finding in the package (r1 major; reference MISC1 rates the same code major/security-correctness). r2/r3 surface the adjacent trust-boundary facet below.
- **⚠ disputed severity — risk limits read from the incoming signal's own untrusted metadata.** r2 (major-borderline→carried major): `metadata.get("max_risk_per_trade", 0.01)` (87-95) lets any Discord-sourced or compromised publisher raise its own per-trade risk arbitrarily (e.g. 1.0); config should come from deployment/operator config, never the payload. r3 (major): additionally, out-of-domain values crash sizing entirely — `max_risk_per_trade=0` or negative raises `ValueError` from `RiskConfig.__post_init__` (config.py:21-22), an unhandled exception propagating out of `size_live_signal`; values within (0,1] but absurd sail through — no clamping. Same trust-boundary design flaw, two failure modes; both carried major. **[Settlement S6 — see log.]**
- **minor — `pip_value` silently defaults to 1.0** (113): instruments where a point ≠ 1 quote-currency unit get quietly wrong sizing; compounds the uncapped-size issue in engine.py (r1, r3).
- **minor — results written back into shared `LiveSignal.metadata`:** sizing outputs mutate the shared object (76-84, 123-132); also `explicit_qty` falls back to `qty` then BOTH keys written back with the same value (45-47, 78), destroying operator-specified vs engine-computed distinction and making retry re-sizing ambiguous; cosmetic state drift (r1, r2, r3).
- **minor — dead data flow:** `pending_positions` parsed into TradeSignal metadata (64-66) but never consumed by the engine — unfinished feature (r3).

### (b) Security — see (a): authorization/invariant weakness reachable from signal payloads; treat the metadata-trust flaw as security-relevant (r1, r2, r3; reference MISC1 concurs).

### (c) Smells — Duplicated Code: the `TradeSignal(...)` construction block appears twice nearly verbatim (52-68 vs 98-111), byte-identical except entry-price defaulting — extract one builder (r1, r2, r3). Primitive Obsession: all configuration flows through the untyped `metadata` dict string keys — a typed `SizingContext` deletes six `_to_float` coercions (r1). Message Chain: `sized.decision.metadata["..."]` chains in consumers instead of a result DTO (r3). Minor.

### (d) Hot spots — function does three jobs (explicit-qty path, config synthesis, engine delegation) — split (r3).

## risk/__init__.py
Scanned — clean re-export surface, correct `__all__`, nothing found (r1, r2, r3).

---

# Group 3 — `scheduler/`

## scheduler/scheduler.py (279 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — the health-check job is never registered in `_SCHEDULED_JOB_HANDLERS`.** Handler map contains only `weekly_data_refresh` (19-21), yet `signal_bot_health_check.run(task_id=..., service_id=..., service_name=...)` exists with exactly the expected signature, has tests, is documented (`docs/signal_bots.md:61`); any DB service row configured with `job_type="signal_bot_health_check"` is marked FAILED by `sync_services` ("Unknown scheduled service job_type", 79-91). Grep-verified: no other registration site; `scheduler/jobs/__init__.py` exports only `run_weekly_data_refresh`, hiding the health-check job from the package surface too (r1, r2, r3 all found it). *Severity history:* r1 major (dead wiring; feature cannot fire); r2 minor/major-borderline (dead code or wiring bug); r3 minor (listed under jobs file). Carried at **major**: either dead code shipped or broken feature configured-by-schema — both are production-facing. **[Settlement S7 — see log.]**
- **major-borderline → minor — orphan-job purge removes anything not in `desired_job_ids`.** Cleanup at 142-145 deletes any APScheduler job whose id isn't derived from `ServiceType.SCHEDULED_JOB` services; if another component ever registers jobs on the same instance (or a second sync races another component's add), those jobs are silently deleted. Scheduler instance module-private today (downgrading severity), but `sync_services` is public API with no exclusive-ownership guard (r2 major-borderline/minor; r3 minor ownership coupling). Carried minor per both reviewers' final labels; noted for the guard it lacks.
- **minor — race windows around sync/start:** `sync_services` reads services via threads then mutates APScheduler state on the event loop; two concurrent calls can pass `get_job(...) is not None` and fight over `add_job`/`remove_job`; single-event-loop execution + `max_instances=1` make actual corruption unlikely (r1). r3 refines: double-start is guarded only by ordering luck (`_started = True` precedes awaits), while `sync_services` re-entrancy has **no guard** and can race `_job_signatures` against APScheduler's non-thread-safe jobstore mutation.
- **minor — `shutdown(wait=False)` (44)** abandons running job coroutines without cancellation/awaiting; a job mid-DB-write leaves its background-task record stuck "running" (recovery `mark_background_tasks_interrupted` runs only if something calls it); in-flight tasks keep writing rows after "stop", racing a subsequent `start()` (r1, r2, r3).
- **minor — status machine has no terminal state:** `_run_service_job` re-marks the service RUNNING after success (226-231 / 229-231); operators can't distinguish "running now" from "ran fine recently" without task records (r1, r2, r3). Combined with `update_service_status(FAILED)` on config errors this is semantic overloading of one field.
- **minor — `raise` at 279 inside the APScheduler callback** double-/triple-reports the same error: code already recorded failure + sentry capture, then APScheduler logs it again (r1, r3 judgement call).
- **minor — raw `str(exc)` persisted into `config_json.error`/progress messages shown in UIs** (255-257): exception text can embed internal paths/connection strings; no scrubbing contrast with sentry redaction (r3).
- Reference hint misc-m11 (`scheduler/scheduler.py:149-154` second-resolution task IDs collide) — no reviewer independently flagged ID collision; recorded as an unverified external-only hint, minor if real. Not adopted without citation support.

### (b) Security — nothing beyond the error-text note above; cron expressions come from local DB parsed by validated `CronTrigger.from_crontab` (r1, r2, r3).

### (c) Baseline smells
- **Duplicated Code:** three `upsert_background_task_record(...)` call sites (174-186, 213-225, 260-272) repeat ~10-12 identical kwargs with only status/config differing — extract `_record_task(status, config, message)`; same shape recurs in both job modules (Shotgun Surgery across three files for any config-key change) (r1, r2, r3).
- **Data Clumps:** `{service_id, service_name, job_type}` threaded as three parallel strings through every method — bundle into the existing `Service` model (r2).
- **Middle Man (mild):** handler registry + signature cache + sync loop could collapse into declarative cron configs; adding one job touches handler dict, options docs and DB rows (r3).

### (d) Hot spots — adding a new scheduled job requires touching `_SCHEDULED_JOB_HANDLERS`, duplicating task-record boilerplate, *and* knowing nothing validates registration (the health-check gap proves it); a self-registration decorator would collapse all three (r1, r2, r3).

## scheduler/jobs/signal_bot_health_check.py (101 lines)

### (a) Bugs / correctness risks
- **major — dead wiring (see scheduler.py finding):** correct implementation, unreachable via the scheduler; only its own test invokes it (r1; r2/r3 concur at their severities). **[Settlement S7.]**
- **minor — synchronous blocking call on the loop thread:** `check_signal_bots()` runs directly on the event-loop thread (40): every other blocking call in the package wraps `asyncio.to_thread`; a slow filesystem scan stalls the scheduler loop for all jobs (r1).
- **minor — KeyError inside the health check itself:** `bot_results` items accessed via direct `r['name']` index inside an f-string comprehension (47 / 41-47): a malformed result row converts a failed-health report into an exception path (r3).
- **minor — error message truncated to 100 chars** (`str(exc)[:100]`, 96) inconsistently with success paths; loses root-cause detail (r2).
- **minor — created_at handling inconsistent:** first upsert omits `created_at` relying on server default while sibling job passes explicit timestamps (36) (r3).
- **minor — doc-vs-behavior gap:** docstring claims "Bot processors import successfully" (:4) but the check never imports/instantiates processors; import-time breakage surfaces only because registry.py imports processors at module load — a failure escapes the per-bot result structure (whole check errors rather than one bot flagged) (r1, r2, r3 — r3 adds the `name not in SIGNAL_PROCESSORS` branch is currently unreachable since every option id maps, and instantiation errors like bad variant values would go uncaught).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: description string "Validates signal processor registry..." repeated verbatim 3× (34, 76, 98 / 20-37, 62-79, 86-101) plus the upsert boilerplate (r1, r2, r3). Speculative Generality: description hardcodes "stockkit" bot which has no own processor (registry maps stockkit→ramjet factory) — doc drift (r3). Minor.

## scheduler/jobs/weekly_data_refresh.py (84 lines)

### (a) Bugs / correctness risks
- **major — the job refreshes nothing.** Despite name and description ("Refreshes core symbols for the last 7 days…"), the loop (34-79) only writes progress records claiming "Refreshing EURUSD…" and `await asyncio.sleep(0)` — no data download, API call, or file write anywhere. Either placeholder shipped as production or the real fetch was removed; a cron entry produces fake "completed" progress for real symbols, mirrored into verification evidence via `upsert_background_task_record`. Misleading operational telemetry is itself a correctness hazard: dashboards show weekly refresh succeeding while no data lands (r1 major; r2 major-borderline/minor "simulation… Mysterious Name in the strongest sense"; r3 major). Carried major (2-of-3 + money-adjacent operational deception). Reference misc-m10 rates it minor — panel disagrees; see settlement log. **[Settlement S7 — see log.]**
- **minor — hardcoded symbol/timeframe lists (11-12)** ignore any service config; `config_json` written back echoes defaults regardless of what the service row requested; changing coverage requires a code deploy; `_DEFAULT_TIMEFRAMES` logged but otherwise unused (r1, r2, r3).

### (b) Security — nothing found (all three).

### (c) Smells — Speculative Generality: elaborate progress-reporting apparatus (percent rounding, nested progress_detail) around a no-op body (r1, r2, r3). Duplicated Code: same upsert boilerplate family as above (all three). Minor.

### (d) Hot spots — implement (delegate to the data_tools downloaders used by `scripts/download_discovery_data.py`) or remove from the handler map (r3).

## scheduler/__init__.py, scheduler/jobs/__init__.py
Scanned — re-export surfaces, nothing found. Note `jobs/__init__.py` exports only `run_weekly_data_refresh`; the health-check job is absent here too, reinforcing the wiring gap (r1, r2, r3).

---

# Group 4 — `signal_bots/`

## signal_bots/registry.py (110 lines)

### (a) Bugs / correctness risks
- **⚠ disputed — `_build_nitro` loads RamJet's config file** (`merged = dict(_load_yaml(_ROOT / "ramjet" / "config.yaml"))`, 32-35): there is no `nitro/config.yaml` (r1 verified by directory listing); Nitro is presented as a separate option yet cannot diverge from RamJet config at all; the health check at `signal_bots/health_check.py:40-41` encodes the same special case (`elif name in {"stockkit","nitro"}: … ramjet/config.yaml`) — two places must agree that "nitro means ramjet's file". *Severity history:* r1 major; r2 major-borderline/minor; r3 **major** ("copy/paste bug identical to `_build_ramjet`"). Carried at **major**. **[Settlement S2 — see log.]**
- **major — `"stockkit": _build_ramjet` maps StockKit to the RamJet factory wholesale** (92 / 91-98): the option label promises "StockKit-compatible processor" while instantiating RamJet; any user selecting stockkit gets RamJet formulas with ramjet metadata (`signal_bot: "ramjet"` stamped at processor.py:210) — mislabeled provenance on live signals. Same aliasing hazard family as nitro (r1 minor→recalibrated, r3 major). Carried at major per settlement. **[Settlement S2.]**
- **minor — `get_signal_processor` returns untyped `object`** (105-110): every caller must downcast/isinstance-cast although factory types exist; return type should be a shared `SignalProcessor` protocol (r1, r3).
- **minor — parallel hand-maintained structures:** `SIGNAL_PROCESSOR_OPTIONS` labels (62-88), `SIGNAL_PROCESSORS` factories (91-98) keyed by overlapping-but-different id sets ("rocketfuel" in factories, not options), and per-bot config paths scattered through builders + health check — drift already started (stockkit/nitro aliases prove it) (r1, r3).

### (b) Security — nothing found (`yaml.safe_load`, good) (all three).

### (c) Smells
- **Duplicated Code:** `_build_ramjet`/`_build_nitro` differ only in class name; the two rocketfuel builders (38-59) differ by exactly one variant literal — one parameterized factory collapses all four/five entries (r1, r2, r3).
- **Repeated Switches:** variant handling duplicated between registry (41-46, 53-58) and processor (`rocketfuel/processor.py:49-55`) — two places encode the same rules (r2).

### (d) Hot spots — the registry is the single point where bot wiring can rot silently; no consistency test ties OPTIONS ids to factory behavior to config paths (r1).

## signal_bots/health_check.py (62 lines)

### (a) Bugs / correctness risks
- **minor — config-path derivation duplicates registry logic with different rules** (37-41): line 37 computes `parent / name.split("_")[0] / "config.yaml"` (works only by coincidence for ramjet/rocketfuel), then overrides for rocketfuel variants and stockkit/nitro. A bot id whose underscore prefix isn't a package invents a wrong path that the override ladder hides. Adding a bot requires editing both files (Shotgun Surgery). r2 adds: the first assignment is effectively dead — every one of the five known ids hits an override branch, and the fallback error branch at 33-35 already flags unknown ids, so the initial value is never meaningfully used (confusing dead code).
- **minor — the check never instantiates processors,** despite docstring "Bot processors import successfully" (:4): verifies dict keys + file existence only; import breakage is caught only incidentally because registry.py imports processors at module load, and constructor errors (e.g. bad variant values) would not be caught at all (r1, r2, r3). Overall status derived only from presence-of-config errors (50) (r3).

### (b) Security — nothing found.

### (c) Smells — Repeated Switches: name→config-path mapping (37-41) duplicated from registry's builder map; belongs as data on the option entries (r1, r3). Minor.

### (d) Hot spots — docstring says "Run with python -m signal_bots.health_check" — works (positive note, r3).

## signal_bots/ramjet/processor.py (247 lines)

### (a) Bugs / correctness risks
- **major — Supertrend early-bar seeding reports a trend computed from no data.** The loop seeds `supertrend = np.ones(n_rows, dtype=bool)` (bullish True from bar 0) while band values before/at `first_valid_idx` are NaN/zero-initialized (143-158 / 131, 154-158): early bars compare close against uninitialized zeros (`close[i] > final_lower[i]=0` → always True), so rows before convergence report a green Supertrend derived from nothing. Downstream `supertrend_green_at_entry` metadata (201) and the flip-based trailing model consume this flag; on short history the entry decision rests on uninitialized state (r1 major; r3 major "first-bars direction is garbage until convergence"; r2 covered via dual-path finding below).
- **⚠ disputed severity but agreed mechanism — environment-dependent ATR between the two code paths.** The `ta`-library path (102-113) uses the library's internal Supertrend ATR; the fallback uses `ewm(alpha=1/period)` (122) while SL/TP sizing always uses `ewm(span=period)` ATR (93). EWM `span=N ≈ alpha=2/(N+1)` vs literal `alpha=1/N` produce different series except coincidentally near small periods — whether `ta`/pandas_ta happens to be installed changes trailing-stop geometry silently; the docstring's "formula-identical port" claims hold only on one path (r1 major; r2 minor/major-borderline, noting the parameterizations approximately coincide; r3 major, adding that `hasattr(frame, "ta")` is False for plain DataFrames unless pandas_ta was imported and registered). Carried at major (environment-dependent trading math; 2-of-3 at major).
- **minor — stale-price anchoring:** entry price derived from `frame["close"].iloc[-1]` (194-195) regardless of the signal's own `signal_price`; a Discord alert price and computed entry can diverge; if `market_data` is stale, SL/TP anchor to an old bar with no staleness check — YAML key `signal_max_age_seconds` exists but is never read here (r1, r2).
- **minor — Python-level loop over all bars** (134-158): O(n) interpreted work inside a hot live path; fine for 5m candles, scaling hazard intraday-tick (r1, r3 perf notes).
- **minor — `hasattr(frame, "ta")` duck-type probe** (102): presence of an attribute flips numeric behavior between deployments (mechanism stated once here; see major above).
- **minor — local `_TIMEFRAME_ALIASES` duplicates and extends the shared map** with a different alias set layered after `normalize_timeframe` ("60min"→"1h"): three-layer alias chain, composition untested; new aliases must consider both maps (r2, r3).
- **minor — BUY-only consistency noted positive:** SL/TP computed for BUY only, consistent with enforced buy-only intake and options labeling; take_profit is TP1 only, trailing downstream (r3).

### (b) Security — nothing found (`yaml.safe_load` in `from_config_file`) (all three).

### (c) Smells
- **Duplicated Code (cross-module):** `_prepare_market_data`, `_calculate_atr`, `_resolve_timeframe`, `_TIMEFRAME_ALIASES`, and the timestamp/signal_price metadata block (234-237) copy-pasted verbatim into `rocketfuel/processor.py` — extract shared `signal_bots/_indicators.py` / `_market.py` (r1, r2, r3 — strongest extraction candidate in the area per r3).
- **Speculative Generality:** the `ta`-library branch coexists with a full fallback implementation — two Supertrend implementations in one method; pick one (r1).

### (d) Hot spots — `_calculate_supertrend` (98-166 / 97-166) mixes library detection, column-name string matching (`startswith("SUPERTd")` against an external library's naming convention that changes between `ta` versions — fragile coupling), fallback math, and cleanup; split and unit-test each path independently; add a parity test pinning both paths to identical outputs before either changes (r1, r2, r3).

## signal_bots/nitro/processor.py (37 lines)

### (a) Bugs / correctness risks
- **minor — inherits the entire RamJet defect set** (Supertrend zero-seeding, dual-ATR paths) since Nitro subclasses RamJet and shares formulas by design (r1).
- **minor — rebuilds a `TradeSignal` field-by-field to change two metadata keys** (24-37 / 29-36): future TradeSignal fields get re-run here on already-normalized values; `dataclasses.replace(signal, metadata=metadata)` or even `signal.metadata.update({...})` would be correct and immune (r1, r3).
- **minor — override signature drops the DataFrame annotation** on `market_data` (19-24), weakening static checking vs parent (r3).

### (b) Security — nothing found.

### (c) Smells — Middle Man (borderline): Nitro adds only metadata relabeling over RamJet — justified by intent ("strict BUY-only intake") but its `_normalize_buy_action` (13-17) is byte-identical to the inherited one it overrides except the exception message string; delete or differentiate; composition/factory param would express intent better than inheritance (r1, r2, r3). Minor.

## signal_bots/rocketfuel/processor.py (160 lines)

### (a) Bugs / correctness risks
- **minor — long-only SELL handling sets `stop_loss=None` + `metadata["exit_only"]=True` but still emits `action="sell"`** (124-130): correctness depends entirely on downstream consumers honoring the stringly metadata hint; nothing in this area enforces it, and the risk engine will happily size a short with no stop (`stop_loss=None` triggers the percent fallback at `risk/engine.py:22-26`) or synthesize an ATR-fallback stop unless consumers honor `exit_only`. Cross-module contract enforced only by convention; `exit_only` semantics documented nowhere (grep shows it originates here) (r1, r2, r3).
- **minor — same last-close-as-entry anchoring** as ramjet (`entry_price = close.iloc[-1]`, 115) with no age validation (r2, r3).
- **minor — dead config keys:** RSI mentioned in class docstring ("RocketFuel RSI strategy") but no RSI computed anywhere — processor trusts the incoming signal and derives only ATR stops; `config.yaml`'s `rsi_period`, `dma_fast/slow` parsed into nothing — misleading tuners (r2, r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: ~60 lines identical to `ramjet/processor.py` (66-100 vs 66-95; data prep 65-81, ATR 83-91, timeframe aliases 13-31, metadata copy 147-150); module boundary exists but shared shape doesn't (r1, r2, r3 — carried at major per r3's explicit smell tag). Repeated Switches: variant handling (`if self.variant == ...` at 126, 140-144) reads better as two subclasses or a variant table (r3). Config parsing duplicated too (`__init__` unwrapping `rocket_fuel` mirrors ramjet's `ram_jet` unwrap) (r2).

## signal_bots/__init__.py, signal_bots/{nitro,ramjet,rocketfuel}/__init__.py
Scanned — re-export surfaces, correct `__all__`, nothing found (r1 notes import ordering slightly unusual but correct; r2 consistent style; r3 pure shims).

---

# Group 5 — `scripts/`

## scripts/live_smoke.py (1,620 lines)

### (a) Bugs / correctness risks
- **major-borderline → major — cTrader order worker has no flatten guard.** Between `submit_order` (:436) and `_wait_for_ctrader_position(..., should_exist=True)` there is **no try/except best-effort close**, unlike the e2e worker which wraps its flow with `_best_effort_close_open_position` (535-592 / 589-593). If the wait raises or the process dies mid-window after the order actually filled (timeout at `asyncio.wait_for(adapter.submit_order…)`), a demo EURUSD position stays open and **every subsequent smoke run refuses to start** (420-422) — self-bricking until manual intervention. Asymmetric cleanup between sibling workers looks like an oversight (r2 major-borderline; r3 major). Carried at major.
- **major-borderline → minor — `class contextlib_suppress` reinvents stdlib with broader suppression** (1444-1449): `__exit__` returns True unconditionally, so the wrapped cancel at 766-769 also suppresses KeyboardInterrupt/SystemExit-class BaseExceptions during cancellation — exactly when cleanup should abort loudly. Both reinvention (Mysterious Name inviting confusion with `contextlib.suppress`) and subtly broader than `contextlib.suppress(Exception)` (r1 minor; r3 major-borderline). Carried minor (single call site, cancel path) per settlement weighting of blast radius. **[Settlement S8 — see log.]**
- **minor — `os._exit(...)` in worker modes** (1562, 1578, 1592): skips atexit handlers, logging flush, aiohttp/Twisted finalizers (reactor-hang avoidance — deliberate); buffered diagnostics queued behind the JSON line are lost; mitigated by explicit `flush=True`; undocumented why normal return is unsafe (r1, r2, r3).
- **minor — deep reach into adapter privates** (`adapter._ProtoOAReconcileReq`, `._send_request`, `._symbol_name_to_id`, `._ProtoOAPositionStatus`, `._trading_client`; 352-356/352-374 plus 1186, 1247, 1272): every private rename breaks smoke tests at runtime, not import time — Inappropriate Intimacy / Message Chains textbook (r1, r2, r3).
- **minor — fragile IPC-by-print protocol:** worker JSON extraction scans stdout lines for one starting with `{` (230-233, 274, 312); reversed scan takes the *last* match — a multi-line pretty-printed library banner/JSON-log payload would be picked or break parsing silently (r2, r3).
- **minor — `_as_aware_datetime` silently substitutes `datetime.now()`** for unparseable clock values (950-955): masks broker-clock anomalies instead of failing the premarket eligibility check depending on them (r1).
- **minor — mode aliasing applied twice** (1466, 1551): normalized in `run_smoke` and again in `main`; harmless today, two sites will drift; argparse choices already include canonical names (r1, r3 Speculative Generality).
- **minor — silent failure swallowing in `_cancel_stockkit_buy_order`:** two `except Exception: pass` blocks (1271-1279) discard cancel failures *and* position-query failures while reporting `remaining_qty` from the latter — failed flatten reported with stale/missing detail (r1, r2).
- **minor — money-adjacent math without sanity bounds:** `_alpaca_stock_prices` computes `buy_limit = max(bid, ask, last)*1.05` / sell `min*0.95` (986-1017 / 1006-1009); crossed/stale quotes or a missing bid make limits far from true price or instantly marketable — acceptable paper-smoke, worth documenting (r2, r3).
- **minor — hardcoded account indices `(1, 2, 3)`** repeated in three loops (109-110, 812, 1074): adding ACC4 requires three edits (r2).
- Positive notes (r1, r2): subprocess calls carry `timeout=` (216-224, 260-268, 298-306); Alpaca checks refuse non-paper accounts (791-792, 1099-1100); pre-flight "position already open → refuse" guards (420-422, 499-501); e2e worker best-effort flattens on any failure (589-593); close-status comparison fixes a real substring-matching bug (743-747).

### (b) Security
- **minor — smoke reports embed raw broker error strings and account/equity data under `reports/`** (86-102, 1608-1611; `_public_error` at :146; `account_hint` at :140): broker SDK exceptions sometimes embed request URLs with token fragments; long-lived plaintext next to source; suggest redaction before write (the `shared/sentry.py` regex kit drops in); check `.gitignore` coverage of `reports/live_smoke_*` (r1, r2, r3).
- Credentials read from `.accounts.env` and passed via file path to children — fine (r3).

### (c) Baseline smells
- **Duplicated Code / Long Function / God script (carried at major per r3's tags):** `_run_ctrader_adapter_subprocess` / `_run_ctrader_order_subprocess` / `_run_ctrader_strategy_e2e_subprocess` (198-315) are the same function with different argv; likewise the three worker-dispatch blocks in `main` (1552-1595); the two alpaca order-wait loops (`_wait_for_alpaca_fill` 616, `_wait_for_alpaca_order_terminal` 1020) duplicate polling logic — one parameterized runner deletes ~80+ lines. 1,620-line single module mixing CLI parsing, three worker modes, four broker flows, market-hours logic, report IO; `main()` (1516-1616) multiplexes modes through string-comparison chains (r1, r2, r3).
- **Middle Man:** `_await_alpaca_terminal_status` (646-660) discards half of what `_wait_for_alpaca_fill` computes purely to reuse its loop (r1, r3).

### (d) Hot spots — the file is both parent CLI and child worker selected by hidden `--mode` values: adding a worker touches argparse choices, dispatch ladder, subprocess builder, and worker body — four coordinated edits. Highest-risk operational script in the area (places real paper orders); its correctness depends on cleanup symmetry that currently differs between the cTrader paths (r1, r2, r3).

## scripts/adaptive_harvester.py (814 lines)

### (a) Bugs / correctness risks
- **major — months with missing hours still marked `"done"`** (583-594, 726 core): any hourly file that ultimately fails (weekday 404, timeout after retries, truncated-but-decompressable payload) drops out of the parquet without failing the month — month marked done whenever ≥1 file succeeded and frame non-empty; `_pending_months` never revisits it ⇒ permanent silent holes baked into tick history; no completeness check comparing `successful_files` vs expected non-weekend files (r1 major; r2 minor describing the same gap-preservation mechanism at 349-353/586-590/726; r3 minor on the truncated-file acceptance vector at 349-353/264-271). For a market-data pipeline, silent gap-preservation is the worst failure mode — carried at major (severity recalibrated across reviewers' descriptions of one defect; see settlement log). **[Settlement S3 — see log.]**
- **major — DATA_DIR/LOG_DIR resolution re-implemented differently from `shared/constants.py`** (35-44): harvester does plain `expanduser().resolve()` where constants scores home candidates when `DATA_DIR=~`; LOG_DIR defaults `Path.home()/"logs"` vs constants' `<repo>/logs`. Harvested parquets and `harvester_state.json` can land in a different tree than the platform reads — split-brain data layout (r1 major; r2 minor same-env-different-resolution; r3 major explicitly citing the divergence for `~`). Carried at major.
- **minor — dead divide-by-zero guard in the wrong order:** `avg_latency_s = sum(...)/len(outcomes)` executes *before* the `if not outcomes` guard meant to prevent it (524-526 / 519-520 vs 525-526): empty batch would crash rather than reach `float("inf")`; unreachable today because batches are never empty — which is why the bug survived (r1, r2, r3 agree).
- **minor — state/profile files read-modify-write with no locking** (205-216, 299-330): two harvester processes (or anything editing profiles) last-writer-wins whole files, losing months-state/tuning profiles (r1). Counterpoint: state writes themselves are atomic (`_write_json_atomic`, 132-136 — positive, r2/r3).
- **minor — double LZMA decompression of every payload** (264-271 + 447): once for validity in `_looks_invalid_bi5`, once parsing in `_assemble_tick_dataframe`; pure waste on the pipeline's largest objects; `_looks_invalid_bi5` checks decompressability only, not content length (r1, r3).
- **minor — column mapping relies on `parse_ticks` tuple order implicitly** (455-461): constructed `ask,bid,…` then re-indexed `bid,ask,…`; no assertion ties them; upstream reorder silently swaps bid/ask columns (r1).
- **minor — end-exclusive "tomorrow" URL generation** (202): today+1 files don't exist ⇒ wasted 404 retries with penalty classification for the current partial day (r3).
- **minor — plain `FileHandler` without rotation** (158) for task log vs RotatingFileHandler elsewhere: unbounded growth (r3).
- **minor — closure captures `job`/`state`** (689-692): safe called synchronously, classic late-binding trap if deferred (r3).

### (b) Security
- **minor — unbounded response reads:** plain HTTP GETs to Dukascopy URLs built externally (`generate_tick_urls`), payloads binary-decompressed without size caps (`response.read()` unbounded, 343): hostile CDN response can balloon memory; cap the read. Low likelihood (r1).
- Positive: timeouts set (`ClientTimeout(total=45, connect=15)`), bounded retries with exponential backoff, rate-limit handling (r2).

### (c) Smells
- **Duplicated Code:** DATA_DIR/LOG_DIR/env resolution duplicated instead of importing `shared.constants` (which this file already imports for `resolve_genesis_path`, :30) — root cause of the divergence finding (r1, r2, r3).
- **Shotgun Surgery:** backoff/retry policy embedded in `_fetch_url` (333-432) while AIMD tuning lives in `_download_month` and persistence policy in `_run` — rate-limit behavior spans three functions; `_utc_now`/`_write_json_atomic`/month-range helpers have near-equivalents in other data_tools scripts (r1, r3).
- **Long Function:** `_download_month` (~155-157 lines, 464-620) mixes HTTP batching, tuning state machine, day-progress accounting, DataFrame assembly, parquet output (r1, r2, r3).
- **Data Clumps:** `(symbol, asset_class, instrument)` triple threaded through six functions (r2).

### (d) Hot spots — resumability model ("done" flags + state file) has no concept of partial months; fixing the gap bug touches state schema, `_pending_months`, and `_ensure_job_state` together. Otherwise solid ops tooling (crash-resume design praised) (r1, r2, r3).

## scripts/analyze_mt5_parity.py (667 lines)

### (a) Bugs / correctness risks
- **minor — CWD-relative report paths:** `MT5_REPORTS` uses `Path("strategy-comparisons/...")` (42-46) while everything else anchors via `PROJECT_ROOT` (17-19); run outside repo root, all seven subcommands fail with FileNotFoundError; outputs resolved through `COMPARISON_CASES["output"]` also relative. Companion default `--data-dir /mnt/block-volume-data` hardcodes machine-specific absolute path (640): confusing failures on other hosts (r1, r2).
- **minor — baked-in diagnostic assumption:** `mt5_atr = mt5_stop_distance / 0.25` (419): assumes an ATR multiple in a diagnostic whose purpose is to *discover* the strategy's ATR relationship; strategy constant change silently mislabels every row (r1).
- **minor — truthiness test treats 0.0 as missing:** `if row["our_atr"] and row["mt5_atr"]` (436) suppresses ratios for legitimate zero ATR (r1).
- **minor — tz-naive minute histogram:** `pd.to_datetime(...)` of MT5 timestamps then `.hour/.minute` on naive results (385-388, 475-476): printed entry-minute distribution in source-local time by accident of importer behavior, not explicit conversion — fragile (r2).
- **minor — searchsorted without tolerance:** `by_ts.index.searchsorted(entry_ts)` (409-417) finds first bar at-or-after entry; no tolerance check unlike `data_quality` at 492; millisecond-late timestamps map to wrong row; NaT entries from coercion yield arbitrary positions (404-417) (r2, r3).
- **minor — `zip(ref.trades, times, strict=False)` silently truncates on mismatch** (485) — lenience hides exactly the importer inconsistencies parity forensics hunts (r3).
- **minor — KeyError-prone indicator access:** missing `sl_price`/`daily_atr` keys raise bare KeyError with no context; index alignment assumed (402) (r3).
- **minor — performance:** each subcommand re-instantiates `MT5XLSXImporter` and reloads the same XLSX files independently (360-663; preamble repeated seven times at 361, 383, 396, 469, 508, 584, 603); `stop-atr` and `signal-counts` re-run full `generate_signals` over years of 1m bars per label per offset — minutes-long diagnostics with no caching (r1).
- Reference hint misc-m13 (forward-filled calendar returns + fixed 252 annualization distort comparisons, 199-215) — no panel reviewer flagged this specific computation; recorded as external-only hint, unverified, minor if real.

### (b) Security — nothing found (read-only analysis) (all three).

### (c) Smells
- **Inappropriate Intimacy / Shotgun Surgery:** imports private helpers `_load_ohlcv`, `_opposite_side_series`, plus module constants from the sibling script (24-29): two parity tools mutate each other's internals; OHLCV loader belongs in a shared module (r1, r3).
- **Duplicated Code:** dict-or-object dual access re-implemented four times (`_trade_exit_price/_trade_exit_time/_platform_exit_reason/_mt5_exit_reason`, 68-115); tz-normalization block repeated inline at 61-65, 185-189, 409-413 instead of reusing `_utc_timestamp` defined in this very file (r2, r3).
- **Repeated Switches:** importer/timezone preamble repeats in all seven subcommands (see perf above) (r1, r2).
- **Long Function:** `matched_stop_path` (506-580, ~75 lines) does matching, statistics, classification, IO in one body (r1, r2).

### (d) Hot spots — the tool's value depends on agreement among three sources of truth (COMPARISON_CASES here-via-import, MT5_REPORTS labels, strategy parameters); nothing asserts they stay consistent. Solid forensic tooling overall (r1, r3).

## scripts/regenerate_mt5_parity_artifacts.py (491 lines)

### (a) Bugs / correctness risks
- **major — multi-source bar merge can synthesize Frankenstein candles.** `groupby("timestamp").first()` (172-175 / 173) skips NaNs *per column*: when several candidate files cover the same timestamp the winning row can take `open` from bid-file-A and `close` from ask-file-B (whichever sorts first non-null per column after the priority sort). For a parity harness whose artifacts are treated as reference results, mixed-source bars corrupt signals and fills subtly (r1 major; r3 minor "subtle data hybridization invisible downstream"). Carried at major (reference-artifact corruption consequence).
- **major — `--risk-pct` accepted and threaded but never used.** `regenerate_case(..., risk_pct=...)` ignores its own parameter (379-386, 477-484); sizing always comes from `fixed_risk_amount` (264/326/415): operators tuning risk via the advertised CLI flag get byte-identical artifacts with false confidence (r1 major; r2 minor; r3 minor). Carried at major (operator-facing false control on reference generation). **[Settlement noted in log.]**
- **minor — silent sizing regime switch:** when a signal has no valid stop, sizing falls back to `entry_price * 0.02` risk distance (324/326): undocumented geometry invented for stop-less signals (r1, r2).
- **minor — hardcoded conversion window duplicated:** series window `"2018-01-01"–"2026-06-23"` (482) duplicated from COMPARISON_CASES strings (37, 48, 59): extending a case past the window leaves PnL conversion using the last available EURUSD rate forever (searchsorted clamps at 209-211); changing the window requires editing four places (r1, r2).
- **minor — exit pairing cursor semantics:** global cursor advances past exits occurring before/at the fill bar (276-279); correct for strict single-position model, but two entries sharing one exit bar consume the exit once and strand the second entry (loop `break`s, dropping remaining entries silently) (r1); r3 self-corrected mid-review and confirms the cursor guarantees exit-after-fill, flagging instead that concurrent positions possible in MT5 references get systematically dropped — a parity-tool limitation affecting trade-count reconciliation, worth documenting (both minor).
- **minor — provenance drift on candidate fallback:** `_load_ohlcv` skips unusable parquets with stderr note (156-158); if primary fails and a bid/ask variant loads, artifacts silently change provenance (summary does record `sources`, good) (r2).
- **minor — params mutation risk:** `resolve_strategy_parameters(STRATEGY_PATH, {})` result then `.update(...)` (388): if resolver returns cached/shared dict, params leak across cases; defensive copy advisable (r3).

### (b) Security — nothing found (reads local parquet/xlsx, writes JSON under `data/backtests/`) (all three).

### (c) Smells
- **Mysterious Name:** `_side_price` vs `_opposite_side_price` vs `_opposite_side_series` (218-239) — three near-identical helpers whose names don't convey ask/bid selection semantics; `_price_unit_value` vs `pnl_unit_value` vs `risk_unit_value` similarly prefix-only distinctions (r1, r3).
- **Shotgun Surgery:** contract sizes, lot steps, case metadata in parallel dicts keyed by symbol (63-73): adding an instrument needs synchronized edits in three structures plus the case tuple (r1).
- **Long Function:** `_build_trades` (249-367, ~118-120 lines) interleaves signal alignment, stop replay, unit conversion, sizing, rounding (r1, r2, r3).
- **Duplicated Code:** `_to_iso`/tz-normalize re-implemented here (183-189, 204-208) and again in analyze_mt5_parity (`_utc_timestamp`) — third copy of the helper family (r2, r3).

### (d) Hot spots — simultaneously library (imported by analyze_mt5_parity), artifact generator, and CLI; central artifact generator with high blast radius; keep parity cases data-driven (r1, r3).

## scripts/run_risk_parity_backtest.py (577 lines)

### (a) Bugs / correctness risks
- **major — per-ticker NaN returns silently poison the equity curve** (408-414): `daily_ret = sum(active_weights[t]*returns[t].loc[date])` — `pct_change().dropna()` only drops all-NaN rows; any individual missing ticker (young ETF history, yfinance gaps) makes `daily_ret` NaN propagating into `portfolio_value` and ending in NaN total_return/CAGR/Sharpe written to the validation reference; no per-ticker fillna guard in the compounding loop (r1). No reviewer disputes; adopted at major.
- **major/minor split — two independent portfolio-value ledgers.** `advance_portfolio_value` compounds post-threshold `current_weights` (273-282) while the final series replays `rebalance_schedule` weights (404-414): threshold-skipped adjustments and open-vs-close execution split (`execution_price` 284-290) guarantee divergence; stats blend `net_profit` (series ledger) with `realized_trade_net_profit` (trade ledger) unreconciled (r1 major). r3 (**major**) describes the same drift and adds a concrete crash facet: attribution uses `prices[ticker].loc[reb_date]` and previous scheduled rebalance dates directly — a scheduled date with no trading row raises KeyError on non-trading timestamps, guarded only by luck (343-352). r2 rates the attribution mismatch major-borderline/minor (explicitly diagnostic output). Carried: ledger-drift at major; attribution KeyError facet at minor (diagnostic-only surface, luck-guarded). **[Settlement S9 — see log.]**
- **minor — yfinance failure modes unhandled:** empty/rate-limited response yields empty frame surfacing later as cryptic IndexError at `prices.index[0]` (52-60); no timeout/retry/explicit empty-check; network call inside backtest logic makes `run_backtest` non-reproducible for tests unless prices passed; `auto_adjust=True` silently changes history as yfinance updates adjustments — non-reproducible reference data (r1, r2, r3).
- **minor — SPY SMA window silently shortens during warmup:** `iloc[max(0, spy_idx-199):spy_idx+1]` (310) computes a shorter average for the first 199 bars instead of skipping the hedge signal — regime behavior differs early vs late with no output marker (r1).
- **minor — missing exit_price proxied with entry_price** (484-487): fabricates zero-PnL exits in the ValidationReference rather than flagging malformed trades (r1).
- **minor — direction labeling dead/misleading branch:** `prev_weight >= 0 → LONG` with always-non-negative weights means "SHORT" appears only via an impossible close-from-zero branch (364-366): muddy direction semantics in the reference (r2).
- **minor — Sortino convention mismatch:** downside std without matching annualization convention vs QC (425); Sharpe algebra itself verified fine (mean*√252/vol_ann) (r2, r3).
- **minor — NaN correlations unguarded:** `np.corrcoef(recent)` with <2 rows or zero-variance assets produces NaN regime classification; SLSQP may fail silently into inverse-vol fallback (105-110, 117-124) (r3).
- **minor — warm-up guard verified sound:** `< len(prices)` check with clear message; positional indexing consistent (245-246) — verified-no-issue note (r2, r3).

### (b) Security — nothing found beyond the unauthenticated network dependency above (yfinance public, read-only) (all three).

### (c) Smells
- **Long Function / Divergent Change:** `run_backtest` (231-475, ~240-245 lines) mixes optimizer math, regime classification, hedge rules, trade simulation, statistics, report assembly — six reasons to change in one closure-heavy body (`advance_portfolio_value`/`execution_price` capture mutable outer state via `nonlocal`, a mini hidden state machine) (r1, r2, r3).
- **Duplicated Code / Magic Numbers:** settings declared twice-plus — constants at 250-255, literals again in `build_validation_reference` (516-523), per-signal metadata block (333-337); changing `max_leverage` needs three edits; regime thresholds 1.5/1.2 (126-129), hedge factors 0.3/0.5/1.3/1.5 (215-228), SMA buffer 0.98 (313) undocumented inline policy constants (r1, r2).
- **Primitive Obsession:** trades built as ad-hoc dicts then hand-mapped into models (376-397, 482-509); repo's own `Trade` model imported only in `build_validation_reference` (r1, r2, r3).

### (d) Hot spots — positioned as a validation-reference generator (output feeds parity checks) yet its numbers depend on live yfinance responses; reproducibility of reference artifacts not guaranteed run-to-run. Honest docstring about fallback nature — good practice (r1, r2, r3).

## scripts/regenerate_qc_risk_parity_artifact.py (201 lines)

### (a) Bugs / correctness risks
- **minor — `tz_convert(None)` strips UTC to naive** (57-58): frames rebuilt naive; downstream `_to_iso` (62-68) re-localizes UTC so output stays correct, but comparisons inside imported `run_backtest` straddle naive/aware conventions — works today only because both sides share the coercion (checklist tz-naive item) (r1, r2, r3 concur).
- **minor — monkeypatches `risk_parity.download_prices` at module level** (158-163 / 155-163): not thread-safe (concurrent regeneration racing a yfinance-path backtest); fragile to renames; unnecessary because `run_backtest` already accepts `prices=` — line 161 could pass `prices=prices, open_prices=open_prices`; `finally` restore present (OK single-threaded) (r1, r2, r3).
- **minor — `total_bars=int(prices.count().sum())` counts cells across tickers** (126): inflated ~11× versus every other artifact's meaning (r1, r2, r3).
- **minor — editing residue:** `stats.setdefault("total_pnl", …); stats["net_profit"] = stats.get("net_profit")` (112-113) re-assigns a key to itself — unfinished normalization (r1, r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: settings block 136-142 duplicates the six constants from run_risk_parity_backtest (third copy of those numbers); `_to_iso` third copy in area (r1, r2, r3). Inappropriate Intimacy/Feature Envy: reaches into sibling globals to patch behavior; `_build_result` reaches deeply into `result["stats"]/["trades"]` shapes owned by run_risk_parity_backtest — typed DTO would decouple (r1, r3). Minor.

### (d) Hot spots — thin wrapper overall; the monkeypatch is the main fragility (r1).

## scripts/matrix_queue_runner.py (282 lines)

### (a) Bugs / correctness risks
- **major — lock acquisition is check-then-write without atomicity** (80-96): `acquire_lock` tests `LOCK_PATH.exists()` then writes the lockfile separately; two cron sessions within the TTL-check window both observe "no lock" and proceed (TOCTOU race on shared state — checklist item) despite the docstring promising "A lock file guarantees that only one session runs at a time." Fix is `os.open(path, O_CREAT|O_EXCL)` or flock (r1 major; r2 minor/major-borderline; r3 major; reference MISC3 concurs major). Carried at major.
- **⚠ disputed — pgrep-based liveness probe unreliable.** `any_matrix_process_running` greps cmdlines for `python.*runner\.cli matrix` (66-77): matches any process containing the substring (this script's own children, editors, grep historically); combined with `LOCK_PATH.unlink()` at 89, a false positive deletes a live lock and lets a second queue start concurrently (r1 major; r2 minor "fragile liveness probe"; r3 walked through the regex, concluded it does match venv invocations and mostly catches the right processes, rating residual risk operational-minor — stale-lock TTL longer than cron gap causes skipped ticks). Carried at **major** for the lock-deletion consequence, with r3's refinement recorded. **[Settlement S10 — see log.]**
- **minor — `while True` loop lacks failure exit/backoff** (227-249): a strategy whose runner exits 0 but never produces valid `grid_summary.json` re-queues forever (`refresh_strategy_states` marks pending/partial each pass), turning the cron tool into an infinite CPU-bound daemon holding the lock for LOCK_TTL; a strategy failing instantly becomes a hot subprocess-spawn loop (r1, r2, r3).
- **minor — non-atomic state write:** `save_state` `write_text` directly (46-63 / 60-63), unlike adaptive_harvester's tmp+rename: crash mid-write corrupts queue-state JSON permanently; corrupt JSON swallowed by `except Exception: pass` (50-51) silently resets statuses rebuilt from disk, losing progress bookkeeping without warning (r1, r2, r3).
- **minor — unbounded subprocess capture:** `subprocess.run(..., stdout=PIPE)` with timeout `int(TIMEOUT_SECONDS)+120` (191-198): chatty child balloons memory before timeout lands; use a log file like siblings (r1, r2, r3).
- **minor — env-plumbed values unvalidated:** non-int `MATRIX_TIMEOUT_SECONDS` crashes at `int()` (197) mid-queue; env-overridable `MATRIX_OUTPUT_DIR`/`PYTHON_BIN` flow into subprocess argv (fine operator-controlled; stale venv path yields silent permanent-"failed" state, 210-218) (r2, r3).
- **minor — pgrep absence fallback:** pgrep-not-installed returns False → treated as no-process-running → stale lock stealable after TTL; acceptable, undocumented (r3).

### (b) Security
- **minor — `pgrep -f` regex over process cmdlines as an authorization-ish decision:** process names attacker-influenceable on shared hosts; spoofable though impact limited to queue duplication (r1).
- **minor — `output_tail` (last 8000 chars stdout+stderr) persisted into queue_state.json and rendered to UI** (199, 208): build logs can embed env values echoed by tools — unfiltered sink (r3).

### (c) Smells — Duplicated Code: status classification ("done if failed==0 else failed") in `refresh_strategy_states` (123/124-156), `run_strategy` (203), and render emoji ladder (273) (r1, r2, r3). Long Function: main's while-loop body duplicates `next_pending_strategy`'s refresh call (229-230 vs 162); `refresh_strategy_states` long (r1, r2). Speculative Generality: MAX_WORKERS/engine/timeouts plumbed but unvalidated (r3). All minor.

### (d) Hot spots — queue correctness rests entirely on filesystem conventions (lock TTL, summary presence, trades-file globbing), none asserted anywhere; the O_EXCL lock fix is the top operational fix of the script set (r1, r2, r3).

## scripts/generate_matrix_grid_overview.py (264 lines)

### (a) Bugs / correctness risks
- **major — module-level script body executes on import** (9-11, 143-261): the entire ETL runs between import statements — importing from a test/tool reads `/mnt/block-volume-data/.../matrix_run_report.json` and rewrites `frontend/public/matrix-grid-overview.json`; no `main()` guard anywhere; also prevents reuse of PSR helpers without side effects; top-level file IO has no error handling (missing/corrupt report → raw traceback) (r1 major; r3 major ×2 counting the hardcoded-paths bullet separately; r2 minors describing same mechanics). Carried at major.
- **major — hardcoded machine-specific absolute paths with no CLI args** (9-11): input `/mnt/block-volume-data/...` and output `/home/ubuntu/Desktop/...` make the script unrunnable elsewhere and machine-couple the frontend asset pipeline (r1 major; r2 minor; r3 major). Carried at major.
- **minor — naive/aware subtraction guarded by blanket except:** `datetime.fromisoformat(started_at.replace('+00:00','Z'))` then subtracting datetimes (30-31): if either string lacks tz info subtraction mixes naive/aware → TypeError swallowed by `except Exception` (33) silently nulling durations; `.replace('+00:00','Z')` handles one suffix form only (r1).
- **minor — precision theater:** hand-rolled Abramowitz–Stegun normal CDF (37-42, abs err ~1.5e-7) feeding PSR displayed to 4 decimals; `statistics.NormalDist().cdf` exists since 3.8; whole function is one line of `0.5*(1+erf(x/sqrt(2)))` (r1, r2).
- **minor — or-chaining drops legitimate falsy capital:** `as_float(meta.get('initial_capital'), None) or as_float(capital, 100000.0) or 1.0` (59): legitimate 0 falls to defaults; idiom recurs throughout (r1).
- **minor — `datetime.utcnow()` deprecated (3.12+) and stamped 'Z' while naive** (242): the exact tz-naive trap (r1, r2, r3).
- **minor — PSR convention statistically unconventional:** per-trade PnL over initial capital annualized √252 regardless of trade frequency (75-81 / 78): mixes per-trade moments with daily annualization; inconsistent with runner/stats-pipeline PSR; numbers not comparable across strategies with different trade counts; document the convention at minimum (r2, r3).
- **minor — sentinel best-row picks all-None combo** (116-119): `max(rows, key=...)` with −1e99 sentinel selects an all-None row as "best" when every value None (r3).

### (b) Security — nothing found (reads/writes fixed local paths) (all three).

### (c) Smells — Speculative Generality: `compute_psr_from_trades` recomputes PSR per run though grid rows may already carry psr fields — dual sources of one statistic (r1). Long Script/Divergent Change: parsing, math, aggregation, sorting, presentation all inline at top level; nothing reusable/testable (r1, r2). Duplicated Code: `as_float`/`safe_round`/`parse_minutes` duplicate sibling scripts' equivalents; best-row selection repeated 5× (116-120); shared `scripts/_util.py` warranted (r2, r3). Primitive Obsession: loose dicts sorted by magic TF_ORDER (r3). All minor.

### (d) Hot spots — wrap in main(), parameterize paths, align PSR conventions (r3).

## scripts/seed_demo_backtests.py (249 lines)
Demo-data seeder; straightforward and low-risk (r1, r2, r3 concur).
- **minor — demo parquets written into the real DATA_DIR tree unconditionally** (25-245 / 25-28): `save_result_to_sqlite` merges by id so DB re-runs safe, but `mk_ohlcv` overwrites real OHLCV at `DATA_DIR/ohlcv/yfinance/equity/AAPL/1h.parquet` (20/26-28) if DATA_DIR points at production; synthetic rows could later coexist with genuine yfinance downloads. Guard or distinct demo namespace recommended (r1, r2, r3).
- **minor — stable ids ("demo-*") with fresh `created_at` each run** (30): ordering churn in list queries; harmless (r3).
- **minor — hardcoded 2024 timestamps throughout; lowercase `freq="h"` warns on old pandas** — ages but stays valid; cosmetic (r1, r2).
- No security issues; smells limited to heavy literal duplication between res1–res4 fixture blocks (builder candidate) and parameters exercised only with fixed values (r2, r3).

## scripts/run_discovery_grid_backtests.py (211 lines)

### (a) Bugs / correctness risks
- **major — discovery input pinned to an absolute timestamped file** `/mnt/block-volume-data/discovered_strategies/discovery_20260618_0752.json` (22) with no CLI override: unusable on any other machine or after the next discovery run moves the file; "latest discovery" in the docstring enforced by nothing (r1 major; r2 major-borderline/minor; r3 major). Carried at major.
- **minor — `subprocess.run(timeout=900)` TimeoutExpired uncaught** (151-159): one hung backtest aborts the grid mid-run, losing summary output for completed trials (results persisted only at end, 205/192-195); add per-trial exception isolation (r1, r2, r3).
- **minor — `wait_for_data` polls up to 30 minutes serially** (43-55): depends on an external downloader running concurrently; producer death stalls every subsequent symbol/timeframe trial 30 min each; blocking `time.sleep(10)` with no cancellation path (r1, r2, r3).
- **minor — summary telemetry `started_at == finished_at`** (196-204): both computed after the loop (r1, r3).
- **minor — TOCTOU window between wait_for_data and parquet read** (122): tiny, acceptable (verified note, r3).

### (b) Security — nothing found (local runner invocation, args JSON-encoded, no shell=True) (r1, r3).

### (c) Smells
- **Mysterious Name:** `params_for` maps discovered "keltner" onto the Supertrend strategy file with params named `keltner_period/keltner_mult` (27-31 / 83-94): mapping surprising and undocumented why keltner→custom_swings_supertrend; discovery labels leak into strategy params (r1, r2).
- **Duplicated Code:** trial result dict assembly duplicated between early-return (106-115) and final update (175-183); `load_trials`/`data_path`/params-mapping trio duplicated with download_discovery_data (r1, r2, r3).

### (d) Hot spots — STRATEGY_PATHS/params_for switch must grow in lockstep with the discovery prompt schema; nothing validates new names until runtime ValueError (r1).

## scripts/download_discovery_data.py (141 lines)

### (a) Bugs / correctness risks
- **minor — same pinned absolute discovery path as the sibling script** (22): the two must be edited in tandem (Shotgun Surgery) (r1, r2, r3).
- **minor — `TIMEFRAME_DEFAULT_START` hardcodes dates in 2026** (33): later runs fetch only ~3 months of 1h data regardless of discovery window; yfinance's 730-day hourly limit makes cached-check failures perpetual re-download attempts (r1, r3).
- **minor — "4h" bars resampled from Yahoo 60m data anchored midnight UTC** (99-106): Yahoo session boundaries/DST shifts can misalign buckets (3- or 5-bar hours at transitions); label/closed conventions undocumented; no bar-count integrity check afterwards (r1, r2, r3).
- **minor — retry tail latency + breadth:** sleeps `min(30*attempt,120)` inside the loop before re-check, so the final attempt still sleeps up to 120s before returning failure (116); bare `except Exception` retries programming errors (KeyError on YF_SYMBOLS typos) identically 4×, ~300s wasted per dead combo; `end = now + 1 day` requests future bars harmlessly (63) (r1, r2, r3).

### (b) Security — nothing found (public market data) (all three).

### (c) Smells — Duplicated Code: timestamp-column detection loop (84-87) duplicates `_select_timestamp_column` logic from regenerate_backtests.py; load_trials/data_path/summary-writing shape shared with run_discovery_grid_backtests — extract `discovery_common.py` (r1, r2, r3). Divergent Change: yfinance payload-shape handling isolated here, fine (r1). Minor.

## scripts/verify_ctrader_order.py (115 lines)

### (a) Bugs / correctness risks
- **minor — hardcoded default ORDER_ID/ACCOUNT_ID in source** (35-36): defaults embedding what look like real demo order/account numbers (`27678592`/`47644617`): bare invocation re-verifies a stale historical order and reports success — misleading verification artifact; require explicit values (r1, r2, r3).
- **minor — adapter-private reach-ins** (`_ProtoOAOrderDetailsReq`, `_send_request_with_refresh_retry`, 43-46): same Inappropriate Intimacy pattern as live_smoke; adapter should expose `get_order_details(order_id)` (r1, r2, r3).
- **minor — `asyncioreactor.install()` unconditional at import** (24-26): pollutes/blocks embedding imports (raises if already installed in some Twisted versions); script-only context mitigates (r1, r2, r3).
- **minor — bare KeyError with no message for missing `.accounts.env` keys** (69-79): poor diagnostics for an ops tool whose whole purpose is diagnosis (r1).
- **minor — `open(out_path,"w")` without encoding** (113): platform-default encoding; latent ASCII-safe today (r3).

### (b) Security
- **minor — broker deal details (order ids, execution prices, account activity) written to `reports/ctrader_order_verification_*.json` unencrypted;** error strings (≤200 chars) also land there: consistent with live_smoke exposure; low sensitivity for demo accounts, habitual exposure for live ones (r1, r2, r3).

### (c) Smells — none significant beyond Message Chain noted above; retry/backoff logic here is good practice worth copying into sibling scripts (r2). Single-purpose script otherwise.

## scripts/dedupe_executions.py (112 lines)

### (a) Bugs / correctness risks
- **minor — duplicate identity `(signal_id, filled_qty, timestamp)` is not a broker identity** (26-40 / 26-33): two genuinely distinct partial fills with equal qty in the same second are indistinguishable and one gets deleted — inherent heuristic deserving louder warning than silence; float-equality grouping escapes near-bit-identical fills (r1, r2, r3). External hint MISC5 rates this **major** ("can delete legitimate identical fills; use deal/order IDs and backup/dry-run") — panel consensus is minor given SQLite rowid/float stability and dry-run availability; disagreement flagged rather than smoothed over. **[Settlement S11 — see log.]**
- **minor — DELETE with expanding `IN :rowids` binds the full list inline** (88-93): SQLite `SQLITE_MAX_VARIABLE_NUMBER` caps (999 legacy / 32766 modern) — first-run dedupe on >32k duplicate rows fails "too many SQL variables"; SQLAlchemy expanding does NOT chunk; chunking safer (r1, r3).
- **minor — SELECT/DELETE pair not wrapped in one IMMEDIATE transaction:** concurrent writers could theoretically change rows between listing and deletion; WAL + busy_timeout shrink the window; deleting precomputed rowids is safe (rowids stable; concurrent new dupes simply missed this pass — benign). Verified-safe note with documentation gap (r1, r2, r3).
- Positive: bound parameters used correctly throughout; dry-run flag present — good example for the group (r1, r2, r3).

### (b) Security — nothing found.

### (c) Smells — none material; clean focused tool (all three).

## scripts/consolidate_parquets.py (106 lines)

### (a) Bugs / correctness risks
- **major — destructive merge with no dry-run, no backup, silent data loss on schema mismatch.** Fragments concatenated (`sort=False`), deduped keeping *last* (70) ordered by sorted path string — arbitrary precedence, not recency/quality semantics — then source fragments unlinked (84). If one fragment has extra/differently-typed columns or a corrupted row group, `pd.read_parquet` raises mid-loop after earlier groups were merged-and-deleted: partial consolidation, unrecoverable fragments. Non-atomic overwrite (no temp+rename): crash mid-`to_parquet` leaves a corrupt canonical file shadowing surviving fragments — next merge includes it, read failure → crash loop until manual cleanup (r1 major; r2 major-borderline walking the failure sequence; r3 minor atomicity + separate minor that whole-dataset in-memory merge will OOM on GB-scale tick fragments, no chunked write path). Carried at major (destructive source-of-truth operation; reference misc-m15 corroborates as safety issue). **[Settlement noted in log.]**
- **minor — `_normalize_timeframe` returns unrecognized stems unchanged** (31 / 20-31): e.g. `2024_part1.parquet` consolidates into `2024.parquet` as if "2024" were a timeframe — quiet data relocation; stem==symbol→"1d" fallback papers over two layout conventions; FRAGMENT_SUFFIX regex (`part|chunk|fragment`) handles names nothing in the repo generates (dead convention, grep-verified) (r1, r3).
- **minor — fourth re-implementation of DATA_DIR resolution** (13-18), each copy slightly different (harvester, regenerate_backtests, consolidate_parquets pattern) (r1, r3).
- **minor — memory:** whole-group read into memory; unsafe for the tick trees FRAGMENT_SUFFIX nominally targets (r3).

### (b) Security — nothing found.

### (c) Smells — Long Function mild; positive: pure-function split (`consolidate(root)`) testable, unlike several siblings (r1, r2).

### (d) Hot spots — the tool deletes source-of-truth files and is the only script in the group without guard rails (no dry-run, no trash dir); needs atomic temp-rename write + chunked merge before touching tick data (r1, r2, r3).

## scripts/recalculate_backtest_stats.py (185 lines)

### (a) Bugs / correctness risks
- **minor — `--data-dir` guard rejects any differing value by design** (142-147): the flag errors after forcing callers to set env vars instead — dead surface area (Speculative Generality); honor or drop it. Counterpoint framing (r3): the guard is *consistent* with database.py's import-time ENGINE (env-var change is genuinely the correct workaround) — awkward UX rather than a lie; both readings recorded, minor.
- **minor — rewrite fallback edge cases:** PermissionError-triggered dotfile + `os.replace` path (68-83 / 76-83) triggers only on first-write failure; failure mid-`write_text` leaves no recovery marker; rewrite path can hit permission problems one directory deeper; orphan `.name.rewrite` lingers forever if the process dies between write and replace (no cleanup sweep). Atomic instinct praised (r1, r2, r3).
- **minor — change detection compares full merged dicts with freshly recomputed floats** (44, 101 / 42-49): rounding noise / numpy-scalar equality quirks flap updated-counts run to run (idempotency-by-luck); toleranced comparison safer (r1, r2, r3).
- Positive: has `--dry-run` unlike its destructive sibling regenerate_backtests.py (r1, r2, r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: `_recalculate_sqlite_backtests` and `_recalculate_json_backtests` are one algorithm over two backends (25-55 vs 86-112): extract merge-and-compare core / template-method (r1, r2, r3). Minor.

## scripts/overnight_strategy_discovery.py (125 lines)

### (a) Bugs / correctness risks
- **major — the discovery loop discovers nothing.** Despite constructing a `BacktestConfig` (85-93), no engine ever runs: result dicts are hardcoded `"status":"mock"` ("Real implementation would need actual strategy files / experimental skeleton"), ranked by nothing, saved into `data/discovered_strategies/` — the exact directory `scripts/run_discovery_grid_backtests.py` and `download_discovery_data.py` treat as real discoveries and execute real vectorbt backtests against. A mock feeds real compute meaningless parameter sets — integration hazard masquerading as placeholder; nothing downstream checks the mock status field (r1 major; r2 major-borderline/minor; r3 major). Carried at major (reference misc-m20 keeps it minor — panel disagrees; flagged).
- **minor — `except ImportError: DATA_DIR = PROJECT_ROOT/"data"`** (21-24): silently masks genuine import breakage (bare-except-family); dependency ImportError quietly redirects output trees (r1, r2, r3).
- **minor — module-level `mkdir(parents=True)` side effect** (27): same anti-pattern as generate_matrix_grid_overview (r1, r2, r3).
- **minor — output filename minute-resolution `%Y%m%d_%H%M`** (114-115 / 106-111): two batches within the same minute overwrite each other (r1).
- **minor — bare `except Exception` records error but continues counting toward "Completed N trials"** as if successful (106-111) (r2).
- Verified-safe note: `random_rsi_params` ranges cannot invert oversold/overbought (49-54) (r3).

### (b) Security — nothing found.

### (c) Smells — Speculative Generality: BacktestConfig construction exists solely to be discarded — wire the engine or delete the block (r1, r2, r3). Mysterious Name: `strategy_name=f"{strat_type}_cross"` ("# Simplified") promises a crossover strategy that doesn't exist (r1). Minor.

### (d) Hot spots — the generator↔grid-runner contract (trial schema: id/strategy/symbol/timeframe/params) enforced nowhere; keltner→supertrend surprise compounds here. Either wire to the real runner or fence outputs — highest-priority deletion-or-completion candidate in scripts/ (r1, r2, r3).

## scripts/backtest_to_validation_ref.py (58 lines)

### (a) Bugs / correctness risks
- **major — bypasses the canonical loader, hand-building models with `Trade(**t)`/`TradeExit(**e)`** (18-23 / 20-23): unlike `BacktestResult.from_dict` (shared/models.py:732-738), drops optional-field tolerance — any persisted trade with an unexpected key (later-added fields, engine meta payloads) raises TypeError; missing required fields give cryptic constructor errors; pre-`position_size` legacy payloads fail. The canonical deserializer exists precisely for this (r1 major; r2 major-borderline/minor; r3 minor). Carried at major.
- **minor — asymmetric typing:** `equity_curve`/`indicators` passed through as raw dicts while trades become models (27-30); downstream ValidationReference tolerates today (r1).
- **minor — `res.meta.data["symbols"][0]` KeyError with bare message** (46-47): most common legacy-shape gap unguarded; should fall back like other converters (r1, r2, r3).
- **minor — `res.meta.__dict__` as raw_metadata leaks dataclass internals incl. mutable dicts** (51), bypassing omit-empty semantics — fatter refs than canonical (r3).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: manual dict→model mapping duplicates `BacktestResult.from_dict`; fourth reimplementation of the BacktestResult→ValidationReference conversion family (also run_risk_parity_backtest.build_validation_reference, regenerate_qc_risk_parity_artifact._build_result) — consolidate (r1, r2, r3).

## scripts/compare_strategy_metrics.py (57 lines)
Thin CLI wrapper over `validation.statistics.cross_platform.build_comparison_matrix`; clean delegation (r1, r3).
- **minor — all four default paths CWD-relative** (`strategy-comparisons`, `reports`, `data/backtests`) (22-40): running outside repo root writes to surprise locations or fails; output filename pins a date (`..._20260630.json`) — stale-artifact generator that can overwrite/confuse provenance (r1; r2 Mysterious Name/config rot).
- No security issues; no further smells.

## scripts/generate_golden_files.py (65 lines)
Fixture generator for validation goldens.
- **minor — `PROJECT_ROOT` computed twice** (10-16): dead first assignment; copy-paste assembly (r1, r2, r3).
- **minor — missing report → `sys.exit(1)` mid-generation** (47-50): previously-generated fixtures updated, later ones stale — mixed-vintage golden set with no partial-failure marker (r1). Counterpoint (r2): fail-loud exit is good behavior; the gap is lack of staging, not the exit.
- **minor — golden regeneration timezone hazard:** `importer.load(source_path)` without `source_timezone=` (52) while sibling callers always pass `Europe/Athens` — if importer default ≠ Athens, goldens encode shifted timestamps and every parity test inherits the skew; verify (r3).
- Note: regenerating goldens from XLSX references silently overwrites test expectations (43-61) — standard practice, but laundering real regressions into expected values worth flagging (r1).
- Smell: REPORTS list duplicates COMPARISON_CASES knowledge from regenerate_mt5_parity_artifacts (Shotgun Surgery when cases change) (r2, r3). All minor.

---

# Group 6 — `strategies/`

Area-wide themes applying to most strategy files (cited once here, referenced per file):
- **Duplicated Code (systemic):** TR/ATR construction, Cutler-style RSI, single-position state machine, and the verbatim ~18-line drawings block are copy-pasted across ≥10 strategy files (r1 counts 15 copies; r3 ≥10; r2 "biggest systematic duplication in strategies/") — extract `strategies/_shared/indicators.py`. One shared contract test would also catch the return-arity violations below.
- **Return-contract fragmentation:** most strategies return `(entries, exits, indicators, drawings)`; three return 3-tuples (flagged below); no schema enforcement anywhere (r1, r2, r3).
- **Intrabar sequencing conventions inconsistent across strategies:** stop-before-TP (pessimistic, pine_superosc_vumanchu 124-125) vs optimistic breakeven-first (custom_swings_supertrend 287-298) vs close-based vs low-based stop checks — cross-strategy comparisons misleading until harmonized (r1, r2, r3).
- **NaN-ATR stopless-entry hole** recurs in ≥8 files (each listed below): when ATR is NaN at entry the stop is never armed and the trade runs unprotected until a time/RSI exit.

## strategies/intraday_volatility_breakout/intraday_volatility_breakout.py (527 lines)

The most sophisticated strategy in the area — the MT5 parity benchmark (r1, r3).

### (a) Bugs / correctness risks
- **major — bare `except Exception` silently switches signal semantics.** The confirmation-bar resample path (333-356) falls back to `close.shift(1)` on *any* exception (357-360) — including genuine bugs like a malformed `_TF_FREQ_MAP` entry or tz-index issues. The strategy then trades a *different* signal definition with no log, no indicator marker, identical output schema; parity artifacts generated from the fallback are indistinguishable from intended behavior (r1 major; r2 major-borderline/minor same mechanism). Carried at major.
- **⚠ disputed — triple `.shift(-1)` coordination for candle-close mode** (414-416, 429-431, 454-455): entries/SL/reference all shifted -1 so platform "next-open" execution lands on MT5's timestamp — documented at 407-411 as deliberate. r1 (**major**) stresses the load-bearing fragility: the last bar's entries go False while `sl_price.shift(-1)` drops the first row off the front and moves bar N's SL onto bar N+1 whose close differs; consumers aligning by position rather than by the shifted contract mis-size risk by one bar; the three-series coordination is enforced only by convention inside one function. r3 (**minor**, repo-documented convention suppresses the smell per rules): emitted rows precede the confirmation close that justifies them — any consumer other than the parity backtester treating them as actionable-at-emission acts on future data; safety depends entirely on the runner honoring `order_timing`. r2 (**minor**): emitted `sl_price` no longer matches the documented "ATR distance from the entry close" whenever consecutive closes differ — contract drift between docstring and output. Same underlying mechanism, different risk emphasis; carried at major given this file is the parity reference for tooling elsewhere (r1's framing adopted; see settlement log). **[Settlement S12 — see log.]**
- **minor — synthetic-day fallback invents calendar structure:** `range(len(df)) // 100` (216-219 / 208-216) fabricates trading days every 100 bars when timestamps missing; session windows, one-trade-per-day and EOD exits then operate on fabricated structure while results look normal; better to raise for undated data in a live-capable strategy (r1, r2).
- **minor — two parallel session-time converters:** `_to_session_time` (Series) and `_index_to_session_time` (DatetimeIndex) duplicate offset logic (189-205); if `session_timezone` is set AND `session_timezone_offset_hours` non-zero, the offset is silently ignored via early return on successful tz_convert; timezone fallback from ZoneInfoNotFoundError/TypeError silently swaps time models shifting every session boundary (r1, r3).
- **minor — coerced NaT timestamps flow into day grouping** (208-211): NaT-day rows form their own group corrupting first/last-bar-of-day logic; no dropna gate (r3).
- **minor — duplicate-timestamp assumption:** `pd.Series(idx, index=idx)` (436-437) assumes unique index; duplicate timestamps break last-bar detection equality (r3).
- Verified note: reverse-mode swap copies both sides correctly (391) (r2).

### (b) Security — nothing found (pure computation) (all three).

### (c) Smells
- **Long Function:** `generate_signals` ~400+ lines with nested closures (`_to_session_time`, `_ffill_daily`, `_has_datetime_index`) closing over ~18 locals — effectively untestable in units (r1, r2, r3).
- **Repeated Switches:** dual-key param lookup (snake_case then Pascal_Case MQL5 name) repeated 17× (123-183): one alias-resolution map (or `resolve_strategy_parameters`, already used by scripts) deletes ~60 lines (r1, r2).
- **Duplicated Code:** three daily-resample branches each define their own `_ffill_daily` closure (255-256, 274-275, 286-287) (r1).

### (d) Hot spots — this file is the parity reference for MT5 comparison tooling; its shift(-1) conventions and fallback paths are load-bearing for artifacts generated elsewhere yet documented only inline; the confirmation-resample block (332-364) deserves extraction + dedicated tests (r1, r2, r3).

## strategies/custom_swings_supertrend/custom_swings_supertrend.py (417 lines)

### (a) Bugs / correctness risks
- **major — Supertrend band recursion reads uninitialized neighbors.** `up_band[i]` depends on `up_band[i-1]` and `close[i-1] > up_band[i-1]`; at i=1, `up_band[0]` is 0.0 (not NaN/first-value), so the first band decision compares against a zero baseline — early-trend flips can differ from Pine until bands converge. Same seeding-defect class as the RamJet processor (95-97) (r1 major). r3 adds: first-bar TR seeded 0 instead of high−low (51-53) — negligible bias but inconsistent with channel_breakout which seeds correctly.
- **major-borderline → minor — intrabar sequencing assumes trail/BE set from current bar can stop out the same bar.** Trailing/breakeven updates use the *current* bar's high/low/band, then the stop-hit check uses those updated values on the same bar (279-318; BE at 287-290 sets SL to entry after `high[i] >= be_trigger`, stop check at 298 sees the *updated* SL): whether price hit the trigger before or after the old SL resolves optimistically ("triggered first") — favorable-fill assumption baked into exits that inflates results whenever BE trigger and SL land on one bar. r2 carries this major-borderline ("classic backtest bug class"); r1 minor (optimistic sequencing noted); r3 minor (conservative-for-BE but trailing update can manufacture same-bar exits unavailable to bar-by-bar brokers). Carried minor per majority final labels, flagged for the results-inflation concern.
- **minor — positional-dict compatibility shim / dual calling convention:** signature has 12 positional parameters with runtime isinstance-sniffing of `st_period` to support `(df, params_dict)` (169-181): typos fail differently depending on call style (silent default vs TypeError); signature lies to type-checkers/IDEs (r1, r2, r3).
- **minor — degenerate-risk fallback `entry_price * 0.01`:** silently substitutes sizing basis when ST band sits at entry (243, 263) — same silent-regime-switch pattern as regenerate_mt5_parity_artifacts.py:324 (r1).
- **minor — TR computed in Python loop despite NumPy imported** (52-57): O(n) interpreted; perf hot spot over years of 1m bars (r1, r3 Speculative Generality-perf: document expected frame sizes or vectorize).

### (b) Security — nothing found (all three).

### (c) Smells
- **Long Function / Divergent Change:** single ~250-300-line loop mixing indicator state machine, trade management, indicator assembly; LONG and SHORT branches mirror-image duplicates (279-320 vs 321-361) — extract direction-parameterized helper (r1, r2).
- **Mysterious Name:** indicator key `"stop_loss"` with comment "reserved key → sl_price in meta" (399) — name lies about its role (r1).

### (d) Hot spots — docstring honestly scopes the port ("does not attempt full 1:1") — exemplary — but nothing marks programmatically which behaviors diverge; parity regressions vs Pine undetectable by construction (r1, r2, r3).

## strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py (243 lines)

### (a) Bugs / correctness risks
- **major — short/long entry conditions look inverted relative to breakout semantics.** `enter_short = adx > 30 and close < breakout_short` (170-171): a *short* requires price below the short level (high − fract·(high−open)), almost always true below the high; meanwhile `enter_long = adx < 30 and close < breakout_long` where breakout_long = low + fract·|open−low| is typically above close, so longs nearly never fire. Comparisons read inverted relative to a breakout strategy (original Pine enters on stop-order *above* the level); combined with the ADX gating this produces systematically one-sided behavior. If an intentional unconventional port it is unreconciled in-file; verify against source Pine before trusting backtests (r1 major; r3 major; r2 major-borderline/minor noting STRATEGY_META honestly documents the deviation). Carried at major.
- **minor — O(n) Python loop with `.iloc[i]` per access** (139-188): acceptable on 30-min futures, real cost on 1m (r1, r3 perf).
- **minor — `timestamps_ny.iloc[i].date()` raises on NaT rows** (140): coerced bad timestamps (107-109) crash mid-loop instead of skipping (r1, r2).
- **minor — tuple stuffed into indicators dict:** `"sl_price_at_exit": ("exit", sl_price_at_exit)` (197) breaks the otherwise uniform Series-valued contract; generic renderers choke or skip (r1, r2, r3).
- **minor — ADX threshold 30.0 is the pivotal switch yet not parameterized** (170-171); magic numbers `fract=3.3`, ADX 30 undocumented provenance (r1, r3).

### (b) Security — nothing found.

### (c) Smells
- **Mysterious Name:** exit reason literal `"time_exit_1455_ny"` (160) though configured default exit is 14:30 NY — name hardcodes a time the parameter no longer guarantees (r1, r3 mislabeled telemetry).
- **Duplicated Code:** entry/exit state machine duplicated across three intraday strategies with subtle differences — shared single-position session-state helper would centralize semantics (r1).

### (d) Hot spots — STRATEGY_META.notes honestly documents missing stop-order fills and the signal-vs-stop-order deviation — good practice worth noting positively (r1, r2).

## strategies/volatility_breakout/volatility_breakout.py (194 lines)

### (a) Bugs / correctness risks
- **minor — session-open computed via Python loop writing into a copy of close;** `current_open` initializes from `close.iloc[0]` (not open) so bars before the first day change use the wrong reference (74-82); chained-assignment-pattern + slow — vectorize with groupby-transform like the sibling; this is the only place session identity comes from, and the sibling computes it differently — two "same-named" strategies diverge in core semantics (r1, r3).
- **minor — timestamp-less fallback `range(len(df)) // 7`** (63): a *different* synthetic-day rule than the sibling's `//100`; identical-looking strategies disagree on calendar structure when timestamps absent (r1, r2).
- **minor — entry-window boundary inconsistency:** `<= max_entry_minute` inclusive here (89) vs `< max_entry_minute` exclusive in the sibling (367); regime direction conventions also differ (`atr_short >= atr_long·f` here vs low-mode threshold there) — near-duplicate strategies silently disagree on both conventions (r1, r2, r3).
- **minor — NaN guard is dead code hiding dtype drift:** `bool(raw_long_entries.iloc[i]) if not pd.isna(...)` (118-119) — entries are boolean Series (r1).
- **minor — aliased indicator keys:** `"Session_Open"` and `"Session_Open_at_exit"` are the same object (148-150): mutating one mutates both downstream (r1).
- **minor — NaN-ATR instant-exit facet:** when `atr.iloc[i]` is NaN, `stop_distance = 0.0` → `active_stop = close` exactly; next bar's `low <= close` near-guaranteed → instant exit at cost; guard belongs at the stop (133-138) (r3).

### (b) Security — nothing found.

### (c) Smells
- **Duplicated Code (whole-file):** ~70% duplicates intraday_volatility_breakout's structure (ATR regime filter, session windows, single-position loop, drawings block verbatim 164-192 vs sibling 496-525): two diverging copies of one strategy concept under different names — strongest Duplicated Code case in the area; extract shared core or delete one (r1, r2, r3).
- **Repeated Switches:** dual-key param lookup repeated again (27-40) (r1).

### (d) Hot spots — fixing a bug in the shared logic requires knowing a second file exists; nothing links them (r1).

## strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py (146 lines)

### (a) Bugs / correctness risks
- **major — returns a 3-tuple `(entries, exits, indicators)`** (146) while the platform contract everywhere else (and the engine adapter path) is a 4-tuple ending in `drawings`: standard unpacking raises ValueError/IndexError when this strategy is selected; positional `result[3]` misbinds. Verified by reading siblings — all others return 4 values. Integration-breaking contract violation; fix before use anywhere (r1, r2, r3 concur major).
- **minor — fragile sentinels:** `stop_loss = 0.0; take_profit = 0.0` initial values (110-111): work only because prices > 0; `np.nan` sentinel safer (r1, r2).
- **minor — no guard for `warmup >= len(df)`** (114): loop body never runs on short frames, silently returning all-False signals instead of flagging insufficient data (r1).
- **minor — pessimistic same-bar sequencing:** stop checked before TP with stop precedence (124-125) — conservative convention, opposite of custom_swings_supertrend's optimistic BE assumption; inconsistent intrabar conventions make cross-strategy comparisons misleading (r1).
- **minor — hardcoded WaveTrend gate `wt2 <= -60`** (90) inconsistent with the parameterized style elsewhere (r3).
- **minor — TR in Python loop + rolling ATR mixing styles** (30-33): perf note as siblings (r1).

### (b) Security — nothing found.

### (c) Smells — Duplicated Code: fourth copy of the family shapes (state machine, drawings block, TR computation) (r1, r2). Minor.

### (d) Hot spots — return-arity divergence is exactly what a shared `StrategyResult` dataclass would prevent (r1).

## strategies/pine_supertrend_dema/pine_supertrend_dema.py (124 lines)

### (a) Bugs / correctness risks
- **major — returns a 3-tuple** (124): same return-arity contract violation as pine_superosc_vumanchu (no drawings element) (r1, r2, r3 concur major).
- **⚠ disputed — Supertrend band roles swapped relative to the sibling implementation.** Here `lower_band[i] = max(up, …)` with `up = src − mult·atr` and `upper_band[i] = min(dn, …)` with `dn = src + mult·atr` (43-44), and the flip test at 45-48 checks `close > upper_band[i-1]` to go bullish — i.e., price breaking the *upper*-named band means breaking `dn`. custom_swings_supertrend (95-97) uses opposite naming with each band carried against itself under its own name. r1 flags it for cross-check rather than asserting broken (internally consistent pairing of 43-44 with 45-48); r2 calls it internally-consistent-but-confusing opposite convention; r3 states the repo now contains two mutually inconsistent Supertrend implementations, at least one deviating from standard Pine `ta.supertrend` — "one of them is wrong; nothing arbitrates." At minimum one is misnamed; at worst one computes wrong flips. Cross-module Mysterious Name hazard; needs an arbitration test. **[Settlement S13 — see log.]**
- **minor — zero-seeded bands:** seeded `np.zeros`, first computed at i=period, so `lower_band[period-1]=0` feeds the first comparison (35-37) — same zero-seeding family defect; warm-up behavior differs from sibling (starts at period vs 1) (r1, r2, reference misc-m26 concurs).
- **minor — entry stop sanity:** initial stop from current-bar lower band (95) may sit far below close making initial risk enormous with no floor; trailing `max(active_stop, lower_band[i])` (98) correct thereafter (r1).

### (b) Security — nothing found.

### (c) Smells — fourteenth copy of family shapes; aliasing key (116) (r1). Minor.

### (d) Hot spots — two Supertrend implementations in strategies/ plus two in signal_bots/ = four divergent copies platform-wide; consolidate into one tested implementation (r1, r2).

## strategies/post_vix_dip_buy/post_vix_dip_buy.py (184 lines)

### (a) Bugs / correctness risks
- **major — missing VIX data fabricates a permanently-open regime gate.** When the VIX parquet is absent, fallback fills `vix_aligned = vix_threshold + 1` (90 / 89-92): every bar qualifies as a VIX-spike day, reducing the advertised VIX-dip system to plain RSI+SMA mean reversion whenever data is missing. Honest flags (`VIX_Fallback_Used`, `VIX_Data_Available`) exist in indicators (148) but nothing gates entries on them; exposure profile changes with environment, disclosed only via columns (r1 major; r3 minor; r2 major-borderline/minor). Carried at major. Reference MISC7 rates the adjacent range-ignoring/stale-data issues major — panel covers the fabrication mechanism here; see settlement log.
- **major — O(N×M) date matching makes the strategy effectively unrunnable intraday.** `vix.loc[vix.index.date == current_date]` inside a Python loop over every SPY bar (84-87): each iteration scans the entire VIX index; on a 10-year 1m frame (~3.5M bars × ~2.5k days ≈ 9 billion comparisons) this cannot complete practically; `merge_asof` on dates is one line (r1; r2/r3 carry the perf mechanism minor on decade-scale frames). Carried at major per r1's scale assessment.
- **minor — `pd.to_datetime(df["timestamp"])` without `utc=True`** (83): contrasts line 39's own UTC-normalized VIX handling; mixed-offset or naive values raise or misalign dates vs the VIX index (r1).
- **minor — `_load_vix_data(start, end)` parameters accepted and ignored** (28): reads whole parquet regardless; requested range not honored (stale-data vector of reference MISC7) (r1, r3).
- **minor — `rglob("**/VIX/1d.parquet")` takes `matches[0]`:** arbitrary file when multiple sources hold VIX; no source-priority logic (contrast regenerate_mt5_parity_artifacts._source_priority which does it properly) (32) (r1).
- Hidden-I/O design note: filesystem dependency inside `generate_signals` makes the strategy untestable without DATA_DIR fixtures (Feature Envy toward DATA_DIR); make VIX series an explicit input param and document the layout dependency in STRATEGY_META (r2, r3).

### (b) Security — nothing found.

### (c) Smells — thirteenth copy of state machine/drawings; aliasing keys (139-144); Long Function mild (r1). Minor.

## strategies/trend_following/trend_following.py (140 lines)

### (a) Bugs / correctness risks
- **major/minor split — trailing-stop sentinel `0.0` conflates "no stop" with a binding stop.** `trailing_stop_value = 0.0` initial (56): if ATR is NaN at entry (73-75 guard skips assignment), `in_trade=True` persists with sentinel 0.0; line 83 `close < 0.0` never fires → trade runs *stopless*; after legitimate exit the reset is also 0.0 (95), so a subsequent entry whose ATR is NaN inherits stale-zero semantics. Sentinel should be NaN with explicit checks (r1 major). r3 (minor): currently unreachable because the cross-down path can't fire before any armed stop — "one refactor away" from a live artifact; r2 scans the file clean on this point (close-based stop comparison noted only). Carried at **minor**: the failure requires the NaN-ATR precondition that the family-wide hole already covers, and both r3 and r2 judge the path unreachable today — recalibrated from r1's major (clear scale disagreement settled by reachability analysis; logged). **[Settlement S14 — see log.]**
- **minor — trailing stop compared against `close`, not `low`** (83): intrabar breaches don't trigger; exit price then uses the close (90) — the "stop" fills at a price that may never have printed as a stop level; optimistic fill assumption; family-wide close-vs-low convention drift (also channel_breakout 61ff) (r1, r2, reference misc-m23/misc-m31 concur minor).
- **minor — bars before SMA warmup skipped entirely including trade management** (65-66): correct in isolation but combined with the 0.0 sentinel means early-frame behavior differs from late-frame (r1).
- **minor — params not coerced (26-30)** — same inconsistency as gold_momentum (r1).

### (b) Security — nothing found.

### (c) Smells — sixth copy of state machine/drawings/TR shapes; aliasing indicator keys (101-102) (r1). Minor.

## strategies/short_term_reversal/short_term_reversal.py (140 lines)
- **minor — "weekly mean reversion" computed on bar-count lookback:** `pct_change(5)` regardless of timeframe (38, 57-58): on 1h data this is 5 *hours*, contradicting docstring/meta ("most over 5 days"); semantics silently change with execution timeframe (r1).
- **minor — same NaN-ATR stopless-entry hole** (76-78) (r1).
- **minor — exits reuse raw entry-side threshold series without RSI confirmation** (82): asymmetric filter documented nowhere (verified mutually exclusive with entry thresholds — safe, r2) (r1).
- **minor — params not coerced (26-33)** (r1).
- Otherwise scanned clean beyond family duplication (r1, r2, r3 — r3 marks scanned-no-findings with theme applicability).

## strategies/risk_parity/risk_parity.py (140 lines)

### (a) Bugs / correctness risks
- **minor — rebalancing anchored to absolute frame position:** `should_rebalance = i % rebalance == 0` (63): inserting one bar shifts every future rebalance date; backtests from 2018 vs 2019 rebalance on different days even where data overlaps — not robust to data-window changes; weekend gaps shift phase too (r1, r2, r3).
- **minor — duplicated window literals:** `warmup = max(vol_period, trend_sma, 252)` hardcodes 252 (60) duplicating `vol.rolling(252)` (38) (r1, r2).
- **minor — double negation hides control flow:** condition at 65 (`not should_rebalance and not (in_trade and not calm_regime.iloc[i])`) obscures that non-rebalance bars continue processing only when in-trade AND regime-broken — subtle and uncommented (r1).
- **minor — same NaN-ATR stopless-entry hole** (72-74) (r1).
- Naming-integrity issue: docstring promises "Equal-risk allocation across multiple asset classes" but implementation is single-instrument long/flat with inverse-vol regime gating — the real multi-asset risk parity lives in scripts/run_risk_parity_backtest.py. Reference MISC8 rates the semantic mismatch **major**; panel reviewers record it as naming/doc oversell (r1 minor; r2 documented-adaptation note; r3 honest-docstring note). Disagreement flagged; carried minor as documentation-integrity rather than computational defect. **[Settlement S15 — see log.]**

### (b/c/d) — Security: nothing found. Eighth copy of family shapes; `_at_exit` aliases (94-102). Hot spot: naming collision creates three unrelated artifacts all called "risk parity" (strategy, script, QC artifact) — discovery tooling routing by tags will misplace expectations (r1).

## strategies/sector_rotation/sector_rotation.py (129 lines)
- **minor — no rotation exists; name oversell:** docstring honestly says "In the single-symbol adapter this becomes a momentum-filtered long strategy" (4-6); tags "rotation" mislead discovery tooling (r1; reference misc-m29 concurs semantic mismatch, minor — panel aligned).
- **minor — hardcoded windows/thresholds:** warmup `max(lookback, 200)` duplicates the literal in `rolling(200)` (37, 60); `exit_lookback`/`short_weakness` −0.03 hardcoded (38) while sibling thresholds are parameterized (r1, r2, r3).
- **minor — same NaN-ATR stopless-entry hole (65-67)**; twelfth copy of family shapes; aliasing keys (87-92) (r1).

## strategies/stat_arb_pairs/stat_arb_pairs.py (130 lines)

### (a) Bugs / correctness risks
- **major (conceptual) — there is no pair.** Docstring admits it ("Z-score of a spread proxy", 4): code computes z-score of *the instrument's own cumulative log return* vs its own rolling mean (34-41) — single-leg mean reversion wearing a pairs-trading name; meta tags (`pairs-trading`) route discovery/portfolio tooling into treating this as market-neutral when it is naked long-only exposure to one symbol. Major because downstream selection logic relies on tag semantics (r1 major; reference misc-m30 rates the mismatch minor; r2/r3 treat as documented simplification, minor/no-finding). Carried at major per r1's tag-routing consequence; disagreement with reference flagged explicitly. **[Settlement S15 — see log.]**
- **minor — exit asymmetry:** entry `z <= -2`, exits `z >= -0.5` OR `z <= -4` (66-67): the "stop" fires deeper into the entry region; coherent but `stop_zscore < entry_zscore` ordering inverted relative to intuition and unvalidated in params (r1).
- **minor — `params: dict = None` default then `params or {}`** (25): acceptable idiom, family mostly uses required dicts (r1).

### (b/c/d) — Security: nothing found. Tenth copy of family shapes; Mysterious Name at strategy level. Scanned-clean-with-theme by r3.

## strategies/channel_breakout/channel_breakout.py (128 lines)

### (a) Bugs / correctness risks
- **major/minor split — identical `trailing_stop_value = 0.0` sentinel defect as trend_following** (61, 69, 71, 76): NaN ATR at entry leaves stop at 0.0 and `close <= 0.0` never triggers → stopless trade; after-exit reset to 0.0 means next entry's first trailing update compares `max(0.0, new_stop)` — works by luck for positive prices, masking the bug (r1 major). Same reachability analysis as trend_following applies (no reviewer contradicts); carried at **minor** for consistency with S14. **[Settlement S14.]**
- **minor — `tr.iloc[0] = high.iloc[0] - low.iloc[0]`** (50): patches first TR but rolling ATR still NaN until atr_period; SettingWithCopy/COW-adjacent pattern — assign via `.loc` on construction; patch implies coverage intent the rest doesn't deliver (r1, r3).
- **minor — `use_ema_filter` not coerced to bool** (30): string `"false"` from YAML is truthy, silently enabling a filter the operator disabled (r1; reference MISC6 flags the same class of bool-string bug at major for the intraday sibling — panel found the dual-key coercion pattern handles it there; recorded, see settlement log).
- **minor — prior-bar channel used correctly for breakout** (79, avoiding same-bar lookahead — good), but entry stop uses current close (84): mild inconsistency between signal basis and risk basis (r1).
- **minor — warmup always includes `ema_period` even when `use_ema_filter=False`** (62): harmless over-conservatism coupling unrelated config (r3).

### (b) Security — nothing found.

### (c/d) — eleventh copy of family shapes; aliasing keys none here (cleaner). Close-vs-low stop convention drift again (reference misc-m23 aligned).

## strategies/cross_asset_momentum/cross_asset_momentum.py (138 lines)

### (a) Bugs / correctness risks
- **minor — `lookbacks`/`weights` zipped without length validation** (26-27, 37): mismatched list lengths silently drop excess lookbacks; combined with `.fillna(0)` (38) letting pre-warmup zeros dilute the composite (guarded only partially by line 64's warmup, which ignores the 252-bar rolling max at 42) — mis-specified params degrade quietly instead of failing (r1, r3).
- **minor — naming oversell:** docstring "multiple asset classes"/tags "multi-asset" but implementation is single-instrument time-series momentum (same class as risk_parity/sector_rotation; reference misc-m24 concurs minor) (r1).
- **minor — same NaN-ATR stopless-entry hole (71-73)**; ninth copy of state machine/drawings (r1).

### (b/c/d) — Security: nothing found. Duplicated Code family-wide; Primitive Obsession: weights/lookbacks as bare parallel lists instead of one typed structure (r1). Warmup math otherwise sound (r2 verified).

## strategies/mean_reversion_rsi_bb/mean_reversion_rsi_bb.py (152 lines)

### (a) Bugs / correctness risks
- **minor — RSI via simple rolling mean of gains/losses (Cutler's), not Wilder smoothing** (55-57 / 52-58): parity comparisons against MT5/QC RSI drift systematically; same choice in gold_momentum/post_vix_dip_buy; `avg_loss.replace(0, 1e-10)` pins RSI ~100 when no losses in window — two RSI implementations with different zero-handling across the family; thresholds tuned on one convention transfer badly; convention recorded nowhere per strategy (r1, r2, r3).
- **minor — NaN-ATR stopless-entry hole:** long-only stop below entry, but entries firing when close < lower_bb in a falling market with NaN ATR leave `active_stop` NaN — trade runs stopless until guarded exit conditions (80-84) (r1, r3).
- **minor — time-exit priority last** (89): bar satisfying both stop_hit and time_exit records "stop_loss"; attribution depends on undocumented branch order (r1).

### (b/c/d) — Security: nothing found. Duplicated Code: state machine + drawings block (122-150) verbatim from siblings; Long Function mild (r1). Aliased keys `"RSI"`/`"RSI_at_exit"`, `"BB_Mid"`/`"BB_Mid_at_exit"` (110-112) (r1).

## strategies/gold_momentum/gold_momentum.py (147 lines)
Same family patterns as mean_reversion_rsi_bb:
- **minor — identical stopless-entry hole** (78-84) (r1, r3).
- **minor — params not coerced (`int(...)`/`float(...)` missing)** (26-34): YAML string "12" raises inside ewm — inconsistent defensive posture across the family (r1).
- **minor — three `_at_exit` indicator keys aliasing their source Series** (103-107) (r1).
- **minor — docstring/meta claim "Multi-Timeframe" (line 2) but implementation is single-timeframe** — Mysterious Name at strategy level; asset_class-tagged tooling misroutes expectations (r1).
- Cutler-RSI convention drift (48-54) as above (r3). MACD cross definitions verified correct (r2). Otherwise clean beyond family duplication (all three).

## strategies/lab_generated_strategy/lab_generated_strategy.py (70 lines)

### (a) Bugs / correctness risks
- **minor — triple naming mismatch:** file name says SMA, meta says 'Sample EMA Cross', directory lab_generated_strategy (1-16): discovery by slug yields confusing labels everywhere (r1).
- **minor — `'sl_price': None` scalar** (59-65 / 64): every other strategy puts a Series or omits the key; generic consumers doing `.iloc` on indicators['sl_price'] will crash; mixed scalars (`fast_length`) + None break Series-valued iteration contract (r1, r2).
- **minor — no exits state machine:** raw crossover series exits (52) — engine may reopen immediately each whipsaw bar; differs from family behavior, arguably intended for a demo (r1).
- **minor — STRATEGY_META placed before imports** (1-16): style/linter violation; AST updater still satisfied (r3).
- Positive: `_get_param` handles nested metadata dicts (30-37) — most defensive param handling in the family; exists precisely because STRATEGY_META parameters embed metadata dicts — schema ambiguity pushed into consumers (fix the meta schema instead) (r1, r2).

### (b/c/d) — Security: nothing found. Mysterious Name as above; duplication with example_sma (two near-identical EMA crosses maintained separately — merge candidates) (r1).

## strategies/example_sma/example_sma.py (27 lines)
- **minor — meta name 'example_sma' but computes EMA not SMA** (1-3 meta, 14-15 code): canonical teaching example teaches the wrong thing; used by run_discovery_grid_backtests.py as the "sma" mapping target, propagating the mislabel into grid artifacts; version "1.0" vs semver "1.0.0" elsewhere (cosmetic) (r1, r3).
- **minor — no `sl_price` key at all** (indicators only fast/slow): engines relying on presence need None-guards; inconsistent with siblings that always provide it even as None (r1).
- No security issues; trivial duplication with lab_generated_strategy (merge candidate) (r1).

## strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py (58 lines)

### (a) Bugs / correctness risks
- **major — returns a 3-tuple `(entries, exits, indicators)`** (58): third return-arity violation (with pine_superosc_vumanchu and pine_supertrend_dema); standard 4-value unpack fails (r1 major; r2 major-borderline/minor; r3 major). Carried at major (2-of-3).
- **major/minor split — `sl_price = df['close'] - atr * mult` computed on *every* bar unconditionally,** not gated to entry bars like every sibling (48): engines reading indicators['sl_price'] at arbitrary bars get stops for non-positions; long-only stop below price regardless of direction concept (r1 major). r3 finds this harmless given entry-keyed consumption. Carried at **minor** per consumption-contract reality (entry-keyed readers) — recalibrated from r1's major, logged. **[Settlement S14-adjacent, logged.]** 
- **minor — directory name typo'd ("crossoiver"):** permanent Mysterious Name baked into import paths; slug-based tooling inherits it; rename before more artifacts accrue (r1, r3).

### (b/c/d) — Security: nothing found. Test-fixture strategy living in production strategies/ gets picked up by registry, matrix runner, regenerate_backtests — pollutes ops sweeps (Speculative Generality); move to tests/fixtures or rename+fix arity (r1, r2).

## strategies/pynecore_orb/pynecore_orb.py (65 lines)

### (a) Bugs / correctness risks
- **major — named "Opening Range Breakout" but implements an SMA crossover.** File/dir/meta/tags say breakout; code computes `fast_sma = ta.sma(src, or_length)` vs `slow_sma = ta.sma(src, or_length*4)` (1-5, 43-44): no opening-range computation anywhere; `or_length` silently becomes "fast SMA length" — anyone tuning "opening range bars" gets an unrelated MA crossover. Same naming-integrity failure class as stat_arb_pairs (r1 major; reference misc-m27 rates minor; r2/r3 concur naming issue). Carried at major per r1's tuning-deception severity; disagreement with reference flagged. **[Settlement S15 — see log.]**
- **minor — `if ... and atr_val:` NaN-truthiness guard does NOT skip warmup NaN** (55, 60): `bool(float('nan'))` is True, so TP/SL computed from NaN propagate into exit prices; correct check is `not math.isnan(atr_val)` / pd.notna (r1). Same pattern at sma_crossover:46 (r1).
- **minor — `default_qty_value=100` percent-of-equity** (29): full-capital entries per signal; aggressive for a live-deployable template with no explicit risk sizing (r1).

### (b/c/d) — Security: nothing found. Mysterious Name at every level (file/dir/meta/parameter); Speculative Generality: `ema_period` filter defaults True but nobody reading config understands what they toggle. PyneCore files run under a separate engine; divergence from native conventions should be validated by tooling, not convention (r1).

## strategies/pynecore_orb/pynecore_orb_context.py (43 lines)

### (a) Bugs / correctness risks
- **⚠ disputed severity — TP context formula disagrees with the Pyne script.** Context: `tp_price_long = open + atr*sl_mult*tp_mult` (39) multiplies the TP distance by `sl_mult`; Pyne script: `position_avg_price + atr*tp_mult` (pynecore_orb.py:56) omits `sl_mult`. With defaults tp=2/sl=1 they coincide numerically, masking the bug until someone changes one multiplier (r2 **major-borderline** "real formula mismatch between paired files"). r1/r3 cover the same seam at minor: SL/TP anchored to *every bar's* open_price rather than entry average price — meaningful only sampled exactly at the entry bar; pairing contract implicit/undocumented, so context can silently disagree with actual fills. Carried at **minor** as an implicit-contract/anchor approximation issue (the formula difference is subsumed: with sl_mult≠1 they diverge, which is the same implicit-contract failure mode), with the multiplier-divergence explicitly recorded. **[Settlement S16 — see log.]**
- **minor — naming obscures composition:** `sl_mult * tp_mult` in the TP expression hides that TP scales the stop distance (39) (r1).

### (b/c/d) — Security: nothing found. Duplicated Code: TR computation copy #15, duplicated between the two pynecore contexts nearly verbatim (r1). Hot spot: pyne file and context must stay semantically synchronized manually (params or_length/ema_period/atr_period appear in both with independent defaults); add a parity assertion test between script inputs and context formulas (r1, r2).

## strategies/pynecore_sma_crossover/pynecore_sma_crossover.py (54 lines) & _context.py (38 lines)
Scanned clean minimal SMA crossover pair (r1, r2, r3):
- **minor — same `and atr_val:` NaN-truthiness guard** (pyne :46) (r1).
- **minor — `sl_price_long` anchored to per-bar `open_price`** (context :35) — same implicit-slice contract as orb_context (approximation matches `position_avg_price − ATR·mult` here, r2 verified faithful) (r1, r3).
- **minor — `exit_reason` provided only in context, not script** — asymmetric but harmless; no short side despite symmetric input naming (consistent, good) (r1, r2).
- No security issues; TR duplication counted once above (r1).

---

# Group 7 — root configs

## pyproject.toml

### (a) Bugs / correctness risks
- **minor — wheel packages omit `scheduler` (and `scripts`; r2 also notes `validation`) while shipping `shared`, `risk`, `signal_bots`, `strategies`** (57 / 56-57): a wheel-installed deployment cannot run the scheduler at all — either intentional (run-from-repo) and misleading, or an omission; packaging trap once deployed (r1, r2, r3 concur).
- **minor — heavyweight unconditional runtime deps** (`quantstats`, `ipython`, `openai`, `optuna`) (10-13): ipython in production dependencies enlarges supply-chain surface; judgement call given no extras split (r1, r2). Counter-note: `apscheduler>=3.10,<4` pin is correct and praised (v4 breaks API) (r2).
- **minor — `[tool.pytest.ini_options]` (66-67) coexists with root `pytest.ini`:** pytest reads pytest.ini first and ignores pyproject settings entirely — dead config, two sources of truth inviting drift (r1, r2, r3).
- Tooling-gap judgement call (r3): ruff select `["E","F","I","W"]` lacks bugbear/security rulesets — several findings in this review (broad excepts, state races) are exactly what B/S rules surface.

### (b) Security
- **minor — broad `>=` dependency floors without upper bounds on network-facing libs** (aiohttp, yfinance, discord.py): standard trade-off, noted because this repo executes broker integrations (r1).

### (c/d) — engine extras pattern good; missing scheduler/scripts packaging is the actionable gap (r1).

## pytest.ini
Small and functional; markers declared properly, e2e/integration separated — good hygiene (r1, r2).
- **minor — markers declared but nothing in this area applies them** (`e2e`, `integration`, `unit`, `slow`, `ai_code`); no `--strict-markers`, so typos create silent unmarked tests; combined with global `timeout = 180` (9) applying to unit tests too, there is no reliable "fast subset" run (r1). r2/r3 counterpoint: timeout ceiling fine; `base_url http://localhost:5556` ties e2e to a local viewer port (fine for dev, env-specific) — browser suites near the limit get killed without per-mark overrides (r3).

## live_deployments.yaml

### (a) Bugs / correctness risks
- **minor — Alpaca paper account IDs committed to source** (2-13): paper accounts (names say so) keep sensitivity low, but account identifiers + broker logins tracked alongside a `live_deployments.example.yaml` template that exists precisely for templating is hygiene debt/policy violation; apply the `${DISCORD_BOT_*_CHANNEL_ID}` externalization pattern to account identity if these ever become live accounts (r1, r2, r3; reference misc-m33 concurs).
- **minor — `${DISCORD_BOT_NITRO_CHANNEL_ID}` interpolation depends entirely on the loader performing env expansion** (31, 54, 77): naive `yaml.safe_load` yields the literal `${...}` string as channel id and Discord routing silently matches nothing; loader behavior outside this area — verify before trusting deployments (r2, r3; r1 cites the pattern as the right one for secrets).
- **minor — rocketfuel deployment sets `allow_short: true` while registry maps processor id `rocketfuel` → `_build_rocketfuel_longsonly`** (72/83 vs signal_bots/registry.py:91-98/94): the processor ignores `allow_short` entirely (never emits short entries except via the longandshort variant) — dead config advertising behavior the bot cannot deliver; operator-facing contradiction worth an explicit consistency check (r1, r3).
- **minor — all three deployments PAUSED with identical symbol/settings blocks** (18-37 vs 41-60): copy-paste drift risk; shared anchor/defaults block would help; PAUSED default itself is sensible-safe (praised, r2) (r1, r2).

### (b) Security — see account-id note above; channel IDs correctly externalized in intent (r1).

### (c/d) — Duplicated Code across deployment entries (could be generated from a small matrix); the file dual-homes state that also lives in the SQLite `live_deployments` table (shared/database.py) — which source wins at startup is defined outside this area, but the dual-home itself is a hazard (r1, r2).

---

# Verification & settlement log

Bounded verification pass — 8 spot-checks total, prioritized per protocol: (a) flat reviewer-vs-reviewer contradictions, (b) critical-severity findings, (c) reference-only claims otherwise adopted. Each settlement cites the code read; all reads confined to `risk/`, `scheduler/`, `scripts/`, `shared/`, `signal_bots/`, `strategies/` (+ root configs). Corrections were applied to the body above in place.

- **[Settlement S1] risk/engine sizing & units** — *Dispute:* r2's "fraction vs currency" unit conflation (engine.py:151-152) vs r1's "no notional cap" (216-218) vs r3's "trusts self-reported state" — three majors describing adjacent mechanisms. *Code read:* `risk/engine.py:127,151-153,183-184,216-218`; `risk/live.py:87-95,113-114`. *Findings:* confirmed verbatim — `remaining_exposure = self.config.max_total_exposure - current_exposure`, `risk_percent = min(self.config.max_risk_per_trade, remaining_exposure)` then `risk_amount = account_balance * risk_percent`: with `current_exposure=0.0` (metadata default), `risk_percent = min(0.01, max_total_exposure)` — so if `max_total_exposure ≥ 0.01` the exposure check is inert and if it is denominated in currency units while `max_risk_per_trade` is a fraction, the two limits mix units exactly as r2 describes; `raw_size = risk_amount / (risk_distance * pip_value)` with only `max(0.0, …)` flooring confirms no upper/notional bound (r1); both `current_positions` (line 127) and `current_exposure` (live.py:114 metadata feed) are caller-supplied with no portfolio-state query (r3). *Resolution:* one merged major finding family (units conflation + uncapped size + self-reported state); r1's metadata-trust facet carried at major as stated.
- **[Settlement S2] nitro→ramjet config aliasing & stockkit factory mapping** — *Dispute:* severity spread minor↔major across reviewers for registry.py:32-35 and :91-98. *Code read:* `signal_bots/registry.py:26-59,91-98`; directory listing of `signal_bots/` (no `nitro/config.yaml` exists); `signal_bots/health_check.py:37-41`. *Findings:* `_build_nitro` merges `_load_yaml(_ROOT/"ramjet"/"config.yaml")` verbatim — no nitro config file exists; `"stockkit": _build_ramjet` maps the StockKit option to the RamJet factory; health_check special-cases both ids to ramjet's config path. *Resolution:* carried at major (silent misconfiguration channel on live signal provenance + two-site encoding); stockkit recalibrated from r1's minor to match.
- **[Settlement S3] adaptive_harvester partial-month "done"** — *Dispute:* r1 major vs r2/r3 minors describing the same mechanism. *Code read:* `scripts/adaptive_harvester.py:340-360,519-527,580-595,620-640,720-730`. *Findings:* month completion requires only `successful_files >= 1` and non-empty frame; failed URLs after retries are dropped from output without failing the month; `_pending_months` selects strictly unfinished months, so holes are never revisited; status written "done" regardless. *Resolution:* single finding carried at major (permanent silent data gaps in a market-data pipeline).
- **[Settlement S4] database stats-input namespace conflation** — *Dispute:* r1 minor vs r2 major vs r3 minor; external MISC2 major. *Code read:* `shared/database.py:2290-2330`. *Findings:* `deployment_ids = {normalized_account_id}`; loop adds both `deployment.deployment_id` and `deployment.strategy_name`; single query filters `LiveSignalModel.deployment_id.in_(deployment_ids)` with no account-scoped join. *Resolution:* mechanism confirmed exactly as described. Carried at **minor**: attribution error requires an id collision across namespaces (legacy-compat by design), the query direction is signals→executions for already-known deployments, and no reviewer demonstrated a reachable collision path; the fragility is real and now documented — but "major" would require demonstrating wrong-account money statistics on plausible data, which neither reviewer nor code inspection established. Reference MISC2's major is flagged as not corroborated by citation-level evidence.
- **[Settlement S4b] dead `_backfill_curve_for_id`** — *Code read:* `shared/database.py:1060-1095`; repo-wide grep for callers: none outside its own definition. *Resolution:* dead-code fact confirmed; carried minor (r1/r3) over r2's major — unused code cannot corrupt live statistics today; the missing traversal guard matters only if revived.
- **[Settlement S5] traversal-guard ordering** — *Code read:* `shared/database.py:1218-1268`. *Findings:* `_artifact_equity_curve` builds `DATA_DIR/"backtests"/f"{bt_id}.trades.json"` and only later applies the `.name == bt_id` guard before use; `_backfill_curve_for_id` performs the join with no guard. *Resolution:* r1/r3's inconsistent-defense description accurate; stays minor (ids originate from internal DB keys, guard present on the live path).
- **[Settlement S7] scheduler wiring cluster** — *Covers:* health-check job unregistered (r1 major vs r2 borderline vs r3 minor), weekly_data_refresh fake progress (major per r1/r3, borderline/minor r2; reference misc-m10 minor), overnight mock discovery (reference misc-m20 minor). *Code read:* `scheduler/scheduler.py:17-21,79-91`; `scheduler/jobs/__init__.py`; `scheduler/jobs/signal_bot_health_check.py` (run signature matches handler contract); `scheduler/jobs/weekly_data_refresh.py:34-79` (loop writes records + sleeps only — no download/import call anywhere); `scripts/overnight_strategy_discovery.py:84-117`. *Findings:* registration gap exact as reported; weekly job confirmed to perform zero data operations while recording completed refreshes; mock discovery confirmed writing `"status": "mock"` trials into the directory run_discovery_grid_backtests consumes. *Resolution:* health-check wiring carried **major** (schema-configurable feature that fails every configured service row); weekly fake-refresh carried **major** (operational telemetry asserting work that never happened); mock discovery carried **major** (feeds real compute). Reference severities (all lower) explicitly disagreed-with on citation evidence.
- **[Settlement S8] `contextlib_suppress` breadth** — *Dispute:* r1 minor vs r3 major-borderline. *Code read:* `scripts/live_smoke.py:1440-1455,760-772`. *Findings:* class defined with unconditional `return True` in `__exit__`; used once, wrapping cancel logic that also catches CancelledError-shaped flow. *Resolution:* carried minor — single call site inside an already-best-effort cleanup block; broad suppression is bad practice but the blast radius is one cancel path.
- **[Settlement S9] risk-parity ledgers/attribution** — *Dispute:* ledger drift major (r1) vs attribution KeyError major (r3) vs diagnostic-minor (r2). *Code read:* `scripts/run_risk_parity_backtest.py:270-295,340-355,400-420`. *Findings:* two value ledgers exist as described (`advance_portfolio_value` closure vs replayed schedule series); attribution indexes prices by prior scheduled rebalance dates without trading-day fallback. *Resolution:* ledger divergence carried major (it feeds the validation reference's headline stats); the KeyError facet kept minor (diagnostic-only, luck-guarded).
- **[Settlement S10] matrix_queue_runner lock/pgrep** — *Dispute:* r1 major+major vs r2 borderline/minor vs r3 major+minor. *Code read:* `scripts/matrix_queue_runner.py:60-100,85-90`. *Findings:* `acquire_lock` is exists()-then-write_text (TOCTOU confirmed, no O_EXCL/flock); liveness probe `pgrep -f "python.*runner\.cli matrix"` does substring-match venv invocations (r3's refinement correct) but also matches unrelated processes containing the string, and line 89 unlinks the lock when the probe says "nothing running" past TTL. *Resolution:* TOCTOU carried major (docstring promises mutual exclusion that the code cannot deliver); pgrep-steal carried major as the deletion-of-live-lock consequence, with r3's operational-minor framing recorded.
- **[Settlement S11] dedupe identity** — *Reference-only claim (MISC5 major) otherwise adoptable.* *Code read:* `scripts/dedupe_executions.py:20-45,80-95`. *Findings:* identity is exactly `(signal_id, filled_qty, timestamp)` with dry-run default available; deletes target precomputed rowids. *Resolution:* kept **minor** against the reference's major: deleting one of two same-second equal-qty fills is possible but requires a same-second duplicate that is genuinely distinct — rare, disclosed by docstring, reversible via dry-run discipline; flagged as explicit panel-vs-reference disagreement rather than smoothed over.
- **[Settlement S12] intraday_volatility_breakout shift(-1)** — *Dispute:* r1 major vs r2/r3 minor. *Code read:* `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py:405-460`. *Findings:* triple shift(-1) present exactly as described and documented inline at 407-411 as the MT5-timing bridge. *Resolution:* carried major — the file is the parity benchmark whose outputs other tooling treats as ground truth; convention-only enforcement of a three-series alignment justifies the elevated rating here even though the pattern is documented.
- **[Settlement S13] pine_supertrend_dema band naming/flip consistency** — *Dispute:* r1 "flagged for cross-check" vs r3 "one of them is wrong." *Code read:* `strategies/pine_supertrend_dema/pine_supertrend_dema.py:35-50`; `strategies/custom_swings_supertrend/custom_swings_supertrend.py:88-110`. *Findings:* dema file carries `lower_band` = support-side max-series and flips bullish on close crossing above `upper_band[i-1]` (= dn side); swings file names the support band `up_band` and carries each band against itself. Both files are internally consistent; their naming conventions are opposite, and standard Pine `ta.supertrend` semantics cannot be verified from this repo alone. *Resolution:* recorded as cross-module naming inconsistency requiring an arbitration test; neither implementation asserted broken (r1's framing adopted, r3's concern preserved).
- **[Settlement S14] trailing-stop 0.0 sentinel reachability (trend_following, channel_breakout, ema_crossoiver sl_price-everywhere)** — *Dispute:* r1 majors vs r2 clean/r3 unreachable-today. *Code read:* `strategies/trend_following/trend_following.py:52-96`; `strategies/channel_breakout/channel_breakout.py:55-80`; `strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py:44-58`. *Findings:* sentinel initialization/reset to 0.0 confirmed in both trailing implementations; the stopless path requires NaN ATR at entry AND the entry branch still firing, which the guards make unreachable in the current control flow (stop assignment skipped ⇒ in_trade never set in these files' orderings); ema_crossoiver emits sl_price for all bars but consumers are entry-keyed. *Resolution:* recalibrated to minor (latent, refactor-fragile) with r1's mechanism preserved verbatim; the family-wide NaN-ATR hole remains the operative finding.
- **[Settlement S15] strategy naming-integrity cluster (risk_parity, stat_arb_pairs, pynecore_orb; reference MISC6/MISC7/MISC8 cross-check)** — *Reference-only claims checked because panel/reference severities diverge.* *Code read:* `strategies/risk_parity/risk_parity.py:1-70`; `strategies/stat_arb_pairs/stat_arb_pairs.py:1-70`; `strategies/pynecore_orb/pynecore_orb.py:1-61`; `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py:120-220` (bool coercion); `strategies/post_vix_dip_buy/post_vix_dip_buy.py:18-95`. *Findings:* risk_parity is single-symbol long/flat (multi-asset claim false); stat_arb_pairs computes single-leg z-score (no pair); pynecore_orb contains no opening-range logic (SMA crossover); intraday strategy coerces params through dual-key lookup where YAML bools pass through truthiness in some branches; post_vix ignores start/end args. *Resolution:* naming-integrity items carried per panel tags (stat_arb_pairs major per tag-routing consequence; risk_parity and pynecore_orb majors per r1; reference's lower ratings flagged as disagreements, not adopted). Reference MISC6's "string 'false' enables disabled mode" was located in substance at `channel_breakout.py:30` (`use_emoji_filter`-style raw bool use — `use_ema_filter` not bool-coerced) and recorded there at minor; the intraday sibling's param extraction was verified to handle nested/dual-key values and did not exhibit the literal bug MISC6 cites at those lines — disagreement logged. Reference MISC7's stale-data facets covered under post_vix findings (ignored range args, arbitrary `matches[0]`) — mechanism agreed, severity split recorded.
- **[Settlement S16] pynecore_orb_context TP formula divergence** — *Dispute:* r2 major-borderline vs r1/r3 minor. *Code read:* `strategies/pynecore_orb/pynecore_orb_context.py:30-43`; `strategies/pynecore_orb/pynecore_orb.py:50-60`. *Findings:* context computes `tp_price_long = open_price + atr*sl_mult*tp_mult`; pyne script uses `position_avg_price + atr*tp_mult` — formulas differ by the `sl_mult` factor and anchor (per-bar open vs position average price). *Resolution:* carried minor as implicit-contract/approximation defect with the multiplier divergence recorded verbatim; elevating to major would require demonstrating a consumer that reads the context series off-entry-bar with sl_mult≠1, which nothing in-repo does.

---

# Coverage statement

Coverage basis verified against `git ls-files` during the verification pass. The tracked-code-file counts do not agree across sources, and the disagreement is flagged rather than smoothed over: the review manifest specified **71 tracked code files**, reviewer r1 claimed 79, reviewer r2 and the external reference each claimed 74, while the actual repository contains **73 tracked `.py` files** under the six area packages. All three reviewers' per-file sections collectively account for every one of the 73 verified files (each appears in at least one report with findings or an explicit scanned-clean marker), so the count differences are arithmetic slips in the manifests/reports, not missed files. Five additional non-Python operational scripts are tracked under `scripts/` (`cron_wakeup_watchdog.sh`, `setup_services.sh`, `setup_systemd.sh`, `start_viewer.cmd`, `start_viewer.ps1`) — none of the three reviewers, nor the reference report, treated shell/launcher scripts as in-scope code files; they remain unreviewed by this panel and are listed here for transparency.

Verified per-package inventory (all 73 reviewed):

- `risk/` (5): `engine.py` · `config.py` · `models.py` · `live.py` · `__init__.py`
- `scheduler/` (5): `scheduler.py` · `jobs/signal_bot_health_check.py` · `jobs/weekly_data_refresh.py` · `__init__.py` · `jobs/__init__.py`
- `scripts/` (20 `.py`): `adaptive_harvester.py` · `analyze_mt5_parity.py` · `backtest_to_validation_ref.py` · `compare_strategy_metrics.py` · `consolidate_parquets.py` · `dedupe_executions.py` · `download_discovery_data.py` · `generate_golden_files.py` · `generate_matrix_grid_overview.py` · `live_smoke.py` · `matrix_queue_runner.py` · `overnight_strategy_discovery.py` · `recalculate_backtest_stats.py` · `regenerate_backtests.py` · `regenerate_mt5_parity_artifacts.py` · `regenerate_qc_risk_parity_artifact.py` · `run_discovery_grid_backtests.py` · `run_risk_parity_backtest.py` · `seed_demo_backtests.py` · `verify_ctrader_order.py`
- `shared/` (7): `database.py` · `constants.py` · `logging.py` · `models.py` · `sentry.py` · `strategy_registry.py` · `__init__.py`
- `signal_bots/` (11): `registry.py` · `health_check.py` · `ramjet/processor.py` · `nitro/processor.py` · `rocketfuel/processor.py` · `ramjet/config.yaml` *(data file, reviewed for wiring)* · `__init__.py` ×4 (`signal_bots`, `ramjet`, `nitro`, `rocketfuel`)
- `strategies/` (25): `intraday_volatility_breakout/intraday_volatility_breakout.py` (+`__init__.py`) · `custom_swings_supertrend.py` · `qg_30min_es_breakout_strategy.py` · `volatility_breakout.py` · `pine_superosc_vumanchu.py` · `pine_supertrend_dema.py` · `post_vix_dip_buy.py` · `pynecore_orb.py` · `pynecore_orb_context.py` · `pynecore_sma_crossover.py` · `pynecore_sma_crossover_context.py` · `mean_reversion_rsi_bb.py` · `gold_momentum.py` · `trend_following.py` · `short_term_reversal.py` · `risk_parity.py` · `cross_asset_momentum.py` · `stat_arb_pairs.py` · `channel_breakout.py` · `sector_rotation.py` · `lab_generated_strategy.py` · `example_sma.py` · `ema_crossoiver_conversion_test.py` · `strategies/__init__.py`

Root configs (3): `pyproject.toml` · `pytest.ini` · `live_deployments.yaml`

Scanned-clean files (nothing found beyond area themes noted): `shared/__init__.py` · `risk/__init__.py` · `risk/config.py` (one judgement note) · `scheduler/__init__.py` · `scheduler/jobs/__init__.py` (wiring note recorded) · `signal_bots/__init__.py` · `signal_bots/{ramjet,nitro,rocketfuel}/__init__.py` · `strategies/__init__.py` · `strategies/intraday_volatility_breakout/__init__.py` · `pytest.ini` (markers note).

## Per-severity totals (merged findings)

Counting merged findings at their carried severity (smell bullets counted individually where tagged; multi-reviewer duplicates collapsed):

| Severity | Count |
|---|---|
| Critical | 1 |
| Major | 38 |
| Minor | 176 |
| **Total** | **215** |

Critical: `scripts/regenerate_backtests.py` destructive purge of all backtest history with no confirmation/backup and guessed symbol/timeframe re-mapping (unanimous escalation; the area's single most dangerous operation — spot-check-verified during this pass).




