# Expert Panel Code Review — Area `runner` (Reviewer r1)

Repo: `/home/ubuntu/Desktop/trading-platform` · Date: 2026-08-24 · Blind & independent review.
Scope: all 37 tracked code files under `runner/` (listed in the brief). Read-only; nothing was edited.

## Method

Every file was read in full (12,060 LOC). Cross-reference greps were used only to verify suspected bugs
(call signatures, field existence in `shared/models.py`, callers of suspected-dead modules) — no other
review/report files under `reports/` were opened.

### Repo conventions consulted (for the "repo overrides" rule)

- `README.md` (usage contracts, universal trade format, engine list)
- `docs/ARCHITECTURE.md` (native engine is primary; layer responsibilities)
- `docs/codex-rewrite-notes.md` (documents intentional decisions: stable grid run-ids,
  risk-parity universe mode, auto-download layer, "⚠️ Review erforderlich" markers for money-path changes)

**Override assessment:** No documented repo standard endorses any Fowler smell found below. Two designs
are *documented* and therefore judged leniently: (1) `ExecutionModelSettings` metadata-only fields
(`engine.py:32-47`, self-documented as auditability placeholders), and (2) the `strategy_ir` package being an
explicitly "incremental seam" (`builder.py:1-5`). Neither suppresses the concrete bugs listed.

**Checklist coverage (Python):** mutable default args — none found (dataclass `default_factory` used correctly);
bare `except:` — none (several broad `except Exception`, flagged individually); race conditions on shared
state — see `vectorbt_adapter` warning-set and grid thread pools; SQL string building — none in area
(SQLite access lives in `shared/database.py`); subprocess timeouts — violations in `auto_agent.py`,
missing import crash in `lean_adapter.py`; tz-naive datetimes — `portfolio_optimizer.py`,
`stockkit_converter.py` (deliberate), `pynecore_adapter.py`/`native_adapter.py` (date-bound semantics);
resource leaks — copilot SDK bridge thread, otherwise clean.

Severity policy: **critical** = functionality broken outright or financial results silently wrong at scale;
**major** = wrong results under realistic conditions, security exposure, or expensive structural traps;
**minor** = limited-blast-radius defects and smells.

---

## Per-file findings

### `runner/__init__.py`
Empty package marker. Scanned — nothing found.

### `runner/auto_agent.py`

**(a) Bugs / correctness**
- **[major]** `auto_agent.py:798-822` — `_run_backtest` uses raw `subprocess.Popen` + `proc.wait()` with **no
  timeout**. A hung generated strategy (e.g. infinite loop in `generate_signals`, huge intrabar load) blocks
  the conversion worker forever; the checklist item "subprocess without timeout" is violated. Every other
  spawn site in the area (`pynecore_adapter.py:134`, `lean_adapter.py:112`, `backtest_grid.py:476`) sets one.
- **[major]** `auto_agent.py:241,248-255` — the agent writes LLM-generated Python into
  `strategies/<slug>/<slug>.py` and immediately executes it via `runner.cli`. Combined with
  `viewer`-triggered invocations this is an unauthenticated remote-code-execution surface if the lab endpoint
  is reachable (prompt content → file → `exec_module`). It is the documented purpose of the feature, but no
  sandboxing, allow-listing, or static gate (e.g. forbid `os.system`/`subprocess` imports in generated code)
  exists. Flagged as accepted-risk-by-design that deserves an explicit security decision.
- **[minor]** `auto_agent.py:662-699` — `_resolve_backtest_target`: when the caller supplies no symbols *and*
  no timeframe, `normalized_symbols`/`normalized_timeframe` are empty, so the early branch at line 672 is taken;
  the `filtered`-by-empty-list blocks at 680-691 are dead code (an empty `normalized_symbols` can never be
  non-empty there — the outer `if` guards it).
- **[minor]** `auto_agent.py:132-137` — model validation only applies to provider `nim`; `copilot_*` models are
  passed through unchecked, so typos surface late as provider errors. Inconsistent contract.
- **[minor]** `auto_agent.py:722-723` — `except Exception: pass` while reading `catalog.json` silently falls
  back to a directory scan; a corrupt catalog is indistinguishable from a missing one.
- **[minor]** `auto_agent.py:249-254,741-754` — `engine` extracted from LLM Markdown is forwarded to the CLI
  unvalidated; an LLM hallucination like "vectorbtpro" fails only deep inside `cmd_run`.
- **[minor]** `auto_agent.py:801` — logs the full command line including output paths; harmless, but combined
  with `_log(line)` of all child stdout this can leak environment-specific absolute paths into task logs shown
  in the web UI.

**(b) Security**
- See RCE-surface item above. Positive notes: `ast.literal_eval` (never `eval`) for STRATEGY_META (502-522),
  slugified directory names prevent traversal (`_slugify`, 757-759), secrets never logged.

**(c) Baseline smells**
- **[major] Divergent Change / God module** — 843 lines mixing: LLM orchestration, German prompt templates
  (295-410), Markdown/Python response parsing (456-528), STRATEGY_META normalization (531-584), artifact IO
  (282-292), OHLCV-catalog ranking heuristics (56-82, 727-738), and subprocess management (762-822). At least
  four reasons to change (prompts, parsers, target selection, process control) live in one module.
- **[major] Duplicated Code (with behavioral drift)** — `_normalize_strategy_meta`/`_normalize_meta_tags`
  (531-584) re-implements `runner/strategy_ir/normalizer.normalize_strategy_meta_payload` with **different tag
  semantics** (`tags` strings become `{"type": [...]}` here vs `{"labels": [...]}` in the normalizer) and
  `_extract_markdown_title` (600-604) is copied verbatim in `normalizer.py:11-15`. Two normalization layers for
  the same payload will drift further.
- **[minor] Mysterious Name** — `LONG_CODE_LINE_THRESHOLD` (54) actually means "warn threshold", and
  `TIMEFRAME_RANK` ordering (56-64) encodes a preference heuristic that only `_entry_rank` explains.

**(d) Maintainability hot spots**
Prompt text, parsing and process control should be three modules; the meta-normalization should delegate to
`strategy_ir.normalizer` (which already exists) instead of competing with it.

### `runner/backtest_grid.py`

**(a) Bugs / correctness**
- **[minor]** `backtest_grid.py:3-7` vs `:390-409` and `cli.py` defaults — the module docstring states every
  cell "intentionally delegates to `runner.cli run`", but the **default** path (`use_subprocess=False`) runs
  in-process, which bypasses the CLI's progress plumbing and error-shape. Documented invariant contradicted by
  default behavior.
- **[minor]** `backtest_grid.py:211-222` — local `_detect_date_range` duplicates `runner.cli._detect_date_range`
  with **drifted semantics**: it reads only `matches[0]` (first path found) and has no intersection logic for
  multiple sources; the CLI version intersects across symbols. Same operation, two behaviors (see Shotgun
  Surgery below).
- **[minor]** `backtest_grid.py:277-292` and `:425-440` — the "skip existing" block is copy-pasted twice
  (in-process and subprocess paths); a fix to the invalid-file handling must be applied twice.
- **[minor]** `backtest_grid.py:362` — failures embed `traceback.format_exc()[-4000:]` into the summary JSON;
  useful, but truncated tracebacks can cut the root-cause frame mid-line.
- **[minor]** `backtest_grid.py:103` — `read_backtest_stats` does unguarded `json.loads`; a partially-written
  cell file (crash between save and read) raises out of the happy path and marks the cell failed *after* the
  backtest succeeded.

**(b) Security**
- `subprocess.run(..., timeout=timeout_seconds)` present (476) ✓; commands built from argument lists, no shell
  interpolation ✓.

**(c) Baseline smells**
- **[minor] Duplicated Code / Shotgun Surgery** — `get_engine` (190-208) duplicates `cli.get_engine`
  (56-75); `write_matrix_csv` (496-520) duplicates `backtest_matrix.write_matrix_csv` (178-203); the
  `TIMEFRAME_ALIASES` map (41-55) overlaps `shared.constants.normalize_timeframe`. Engine addition =
  three-file change.
- **[minor] Data Clumps** — `run_one_backtest` / `run_one_backtest_in_process` / `build_cli_command` thread the
  same 14-argument bundle (strategy, symbol, timeframe, start, end, engine, capital, risk/max-position/max-
  leverage, resolution, params, dirs…); a `GridCellSpec` value object would collapse it.
- **[minor] Duplicated Code (intra-file)** — the result-dict scaffolding (`started_at`, `pending`, …) is built
  independently in both run paths (266-275 vs 414-423).

**(d) Maintainability hot spots**
The cell-runner trio (`build_cli_command`, `run_one_backtest*`) plus the duplicated skip-existing block is the
highest-churn area; extracting a `GridCell` dataclass and one `rehydrate_existing()` helper removes ~80 lines
and one drift trap.

### `runner/backtest_matrix.py`

**(a) Bugs / correctness**
- **[minor]** `backtest_matrix.py:333,367-375` — strategy-level `ThreadPoolExecutor` (default workers =
  CPU count) wraps grid-level pools; `--strategy-workers 4 --max-workers 4` spawns up to 16 concurrent
  backtests with no combined cap → oversubscription and skewed cell timings.
- **[minor]** `backtest_matrix.py:72-95` — `_load_daily_rows` trusts `stats.daily_returns` written by
  `attach_daily_equity`; rows carry `run_id` rebuilt from `build_run_id` — if grid naming ever changes, the
  join key silently mismatches (undocumented coupling).
- **[minor]** `backtest_matrix.py:242,260` — `max(ok_rows, key=lambda row: _safe_float(row.get("sharpe")) or
  float("-inf"))`: a legitimate `0.0` Sharpe is falsy and becomes `-inf`; harmless today, wrong tomorrow.
- **[minor]** `backtest_matrix.py:298-307` — builds a throwaway `StrategyEntry` "probe" with
  `documentation_path=None, meta={}` solely to reuse `_strategy_matches`; identifier matching therefore cannot
  consult display names loaded later (alias set differs between probe and final entry).

**(b) Security** — nothing found (no subprocess, no network, writes confined to output dirs).

**(c) Baseline smells**
- **[minor] Duplicated Code** — `write_matrix_csv` (178-203) and the CSV-fieldname list duplicate
  `backtest_grid.py:496-520` (fields differ by one column — drift already visible).
- **[minor] Long Parameter List** — `_run_strategy_matrix_cell` takes 19 keyword parameters (98-120), all
  forwarded verbatim; a config object shared with `run_backtest_grid` would halve it.

**(d) Maintainability hot spots** — the strategy-discovery/probe block (296-324) belongs in
`shared.strategy_registry` where the same matching already half-lives.

### `runner/capm.py`

**(a) Bugs / correctness**
- **[minor]** `capm.py:66-67` — `alpha` is annualized by `* TRADING_DAYS_PER_YEAR` unconditionally; passing
  weekly/monthly returns silently misannualizes. Precondition (daily observations) is undocumented.

**(b) Security** — nothing found.

**(c) Baseline smells** — clean; small focused module. Verified used by `viewer/main.py:140,7193` (not dead code).

**(d) Maintainability** — nothing found.

### `runner/cli.py`

**(a) Bugs / correctness**
- **[minor]** `cli.py:104-111` — `_detect_date_range` reads the **first** parquet match per symbol
  (`matches[0]`) when the exact timeframe file is missing; with several sources present (e.g. `forex/dukascopy`
  vs `forex/histdata`) the detected range depends on filesystem enumeration order — nondeterministic runs.
- **[minor]** `cli.py:107` — `pd.to_datetime(df["timestamp"])` without `utc=True` (unlike every adapter); a
  tz-aware parquet column mixes offsets in min/max computation. Works today because min/max are order-only,
  but fragile.
- **[minor]** `cli.py:185-195` — `sys.exit(1)` inside `cmd_run` on config errors makes the function unusable as
  a library API (grid/matrix deliberately avoid importing `cli`, partly because of this).
- **[minor]** `cli.py:39` — `initialize_sentry("runner")` at import time: importing the module (e.g. from
  `optimizer`, `auto_agent`, tests) activates Sentry as a side effect.
- **[minor]** `cli.py:216` — `result.stats = {**result.stats, **compute_stats_for_result(result)}` silently
  *overwrites* engine-provided stats (e.g. PyneCore's own `stats.csv` values) with the canonical ones where
  keys collide; `walkforward.py:133` does the opposite merge order. Merge order is inconsistent across the area.

**(b) Security** — nothing found (paths come from CLI args; SQLite write delegated).

**(c) Baseline smells**
- **[minor] Repeated Switches / Duplicated Code** — `get_engine` if-chain (56-75) duplicated in
  `backtest_grid.get_engine`; a registry dict would remove the second site.
- **[minor] Shotgun Surgery** — adding a new engine requires edits here, in `backtest_grid.get_engine`,
  `cmd_engines` (242-247), and each adapter's `validate_config` messaging.
- **[minor] Primitive Obsession** — dates remain strings through config/validation/stats; the same
  string-vs-Timestamp boundary bugs seen in the adapters originate here.

**(d) Maintainability hot spots** — the four `cmd_*` functions repeat the same 15-argument forwarding blocks
(254-337); an args→config-object builder would shrink the file by a third.

### `runner/converters/__init__.py`
Empty. Scanned — nothing found.

### `runner/converters/cli.py`

**(a) Bugs / correctness**
- **[minor]** `converters/cli.py:53-58` — the `elif engine == "stockkit"` and `else` branches: both converters
  accept `(path, strategy_name=name)` identically, so the branch pair is redundant and the `else` is
  unreachable (engine membership is checked at line 46).

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Middle Man / Speculative Generality** — the `CONVERTERS` registry carries `"extensions"` metadata
  that nothing consumes, and `cmd_convert` re-dispatches manually instead of using the registry uniformly.
- **[minor] Import-time side effect** — `logging.basicConfig` at module import (line 20) reconfigures the root
  logger for any importer.

**(d) Maintainability** — fine overall; unify dispatch through the registry.

### `runner/converters/lean_converter.py`

**(a) Bugs / correctness**
- **[major]** `lean_converter.py:81-97` — trade pairing assumes strict alternating Entry→Exit per ticker in
  time order. Real LEAN outputs contain partial fills, scaling in/out, and orders tagged neither `Entry` nor
  `LONG/SHORT`; those orders are skipped (88-90) and an entry followed by another entry pairs the second entry
  as the "exit" (93-96), producing fabricated trades with wrong PnL. Silent corruption of converted results.
- **[minor]** `lean_converter.py:104-111` — PnL lookup uses the exact exit-order timestamp string; on miss it
  falls back to price-delta PnL *ignoring quantity differences between entry and exit legs* (`qty` is the
  entry's absolute size, 102).
- **[minor]** `lean_converter.py:150-170` — `_build_meta` hardcodes `initial_capital=100000.0` and leaves
  `backtest_start/end` empty regardless of the actual LEAN run; downstream period-based metrics (CAGR in
  `stats.py` falls back through equity bounds) operate on wrong premises.
- **[minor]** `lean_converter.py:156` — `from datetime import datetime, timezone` re-imported inside
  `_build_meta` although already imported at module level (line 18).

**(b) Security** — nothing found (parses local JSON; no deserialization of untrusted types beyond `json.loads`).

**(c) Baseline smells**
- **[minor] Primitive Obsession** — timestamps juggled as strings with Z/+00:00 dual-key hacks (73-77);
  a normalized timestamp type would delete the lookup triplication.
- **[minor] Mysterious Name** — `MR_` tag prefix parsing (116-119) is undocumented domain shorthand.

**(d) Maintainability** — the stateful pairing loop (81-147) deserves extraction into a small per-ticker
state machine once partial fills are supported.

### `runner/converters/stockkit_converter.py`

**(a) Bugs / correctness**
- **[minor]** `stockkit_converter.py:133-141` — TP1 `TradeExit.time=""` (empty string) violates the universal
  format's expectation of an ISO timestamp; `daily_equity.build_daily_equity_curve` skips such exits when
  aggregating (it selects the last exit *with* a time), so TP1 PnL lands on the runner's exit day — daily
  attribution is shifted.
- **[minor]** `stockkit_converter.py:123-149` — TP1/runner PnL is *estimated* from prices while `trade.pnl`
  keeps the CSV's authoritative `PnL ($)`; the parts won't necessarily sum to the total (slippage/fees
  distribution unaccounted).
- **[minor]** `stockkit_converter.py:172` — slippage is stored in `fees`; conflates two concepts that the
  trade schema separates.
- **[minor]** `stockkit_converter.py:184-190` — `_parse_datetime` assumes `%Y-%m-%d %H:%M:%S` and stamps UTC;
  on format deviation it returns the raw `f"{date}T{time}"` string, which downstream `pd.to_datetime` calls
  may or may not parse (silent format drift).
- **[minor]** `stockkit_converter.py:99` — `id=i+1` uses the raw row index; skipped rows (177-179) leave gaps
  in trade ids.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Primitive Obsession** — direction kept as raw uppercase CSV string (102) without validation
  against {LONG, SHORT}; a typo becomes a phantom direction in the dataset.

**(d) Maintainability** — acceptable size; the header contract (`EXPECTED_HEADERS`, 24-45) is declared but
never enforced against actual files — either validate or delete it (Speculative Generality, minor).

### `runner/daily_equity.py`

**(a) Bugs / correctness**
- **[minor]** `daily_equity.py:57-61` — `prev_equity > 0` guard zeroes the return when equity has gone
  negative/bankrupt instead of flagging ruin; a wiped-out account renders as a calm 0% day.
- **[minor]** `daily_equity.py:106-116` — when the engine curve has more points than the daily rebuild, the
  original curve is kept but `stats["daily_returns"]` (trade-derived) is attached alongside; consumers get
  two subtly different "daily" stories for one backtest (curve-based vs trade-attributed).

**(b) Security** — nothing found.

**(c) Baseline smells** — clean, well-scoped module.

**(d) Maintainability** — nothing found.

### `runner/engine.py` (native TradeEngine)

**(a) Bugs / correctness**
- **[major]** `engine.py:780-786` — indicator dict entries are copied into `_signals_frame` by bare name:
  an indicator returned as `{"entries": ...}` or `{"exits": ...}` (common names!) **overwrites the signal
  columns**, and afterwards lines 785-786 return the (now replaced) frame columns as entries/exits. The
  vectorbt adapter guards this by keeping indicators in a separate map (`vectorbt_adapter.py:503-510`); the
  native engine corrupts them silently.
- **[major]** `engine.py:898-901` — after the loop, end-of-data liquidation appends a *second* equity point for
  the final timestamp (`_update_equity` already recorded bar `total_bars-1` at line 886). The duplicated
  terminal point flows into `_daily_returns_series_from_equity_curve` (groupby-last saves the equity level,
  but the extra point skews drawdown-duration and any per-point consumer).
- **[major]** `engine.py:158` — `max_total_exposure = float(config.max_position_pct or 0.05)`: the native
  engine interprets `--max-position-pct` as a *portfolio-total exposure* fed to `RiskConfig`, while
  `vectorbt_adapter._resolve_max_position_pct` (1285-1290) treats the same flag as a *per-position notional
  cap* with asset-class defaults of 100%/20%, and the CLI help (`cli.py:363-365`) documents it as per-position
  notional. One flag, three meanings; native additionally defaults to 5% when unset — a silent 20× sizing
  divergence between the flagship engine and vectorbt.
- **[major]** `engine.py:63-69,613-618,656-670` — same-bar stop+TP resolution defaults to `stop_first`, while
  `signal_replay_adapter._process_protective_exits` (346-352) resolves the identical ambiguity **stop-first
  only when the stop level is touched per sub-bar but prioritizes whatever hit** — actually it checks `stop_hit`
  first too (347), but `engine._intrabar_first_trigger` prefers TP when `same_bar_order="take_profit_first"`
  whereas signal-replay has no equivalent knob and always favors stop. Conservative bias differs across engines
  for identical setups; cross-engine comparisons (the platform's selling point) are not apples-to-apples.
- **[minor]** `engine.py:362-389` + `460-473` — MARKET orders are position-sized twice: once optimistically at
  creation on signal-bar close, then re-sized at fill on next-bar open; the first computation is discarded
  work and the interim `order.quantity` briefly reflects a stale price.
- **[minor]** `engine.py:874` — `merged_row = pd.concat([row, signal_row])` executed **per bar**; for 100k+
  bars this dominates runtime. Build the merged frame once, or read the few needed fields directly.
- **[minor]** `engine.py:781-783` — indicator values inserted via `value.values` ignore index alignment
  (guarded only by length equality); an indicator built on a filtered/reindexed frame lands misaligned.
- **[minor]** `engine.py:517` — `r_multiple` denominates by `stop_loss or entry_price*0.98`; trades sized by
  `fixed_risk_amount` with wide synthetic stops get distorted R multiples.
- **[minor]** `engine.py:206-229` — `_uses_kwargs_style`/`_call_strategy` duplicates
  `vectorbt_adapter._uses_kwargs_style`/`_call_generate_signals` (75-102) with identical logic.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[major] Divergent Change** — `TradeEngine` (128-903) mixes order lifecycle, risk sizing (via `risk.*`),
  execution-model documentation, progress reporting, indicator/drawing presentation, and meta serialization.
  Execution-model changes and presentation changes both rewrite this file.
- **[minor] Speculative Generality (judgement)** — `OrderType.LIMIT/STOP/STOP_LIMIT` (25-29, 424-446) have
  complete fill logic but nothing in the area ever creates such orders (`_create_order` only builds MARKET).
  Either wire them up or defer the branches.
- **[minor] Primitive Obsession** — `take_profit_levels` as `list[dict]` with stringly `"filled"` flags
  (108, 647-709) wants to be a small dataclass.
- *(Documented)* `ExecutionModelSettings` metadata-only fields are self-declared placeholders (32-47) — noted,
  not charged.

**(d) Maintainability hot spots**
The bar loop (`run`, 843-901) plus `_check_stops` (626-724) concentrate execution semantics, presentation and
progress in one place; extracting `RiskSizer` and `ExecutionModel` collaborators would isolate the money-path
logic that the rewrite notes already flag for extra review.

### `runner/engines/__init__.py`
Empty. Scanned — nothing found.

### `runner/engines/base.py`

**(a) Bugs / correctness** — nothing found (validators correct).

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Speculative Generality** — `validate_config` (123-134) re-checks `symbols`/`timeframe` emptiness
  that `BacktestConfig`'s pydantic constraints (`Field(min_length=1)`, required `timeframe`) already enforce.
- **[minor] Broad exception swallow** — `resolve_strategy_parameters` (80-83) `except Exception: meta = {}`
  hides malformed `STRATEGY_META` files; a syntax error in a strategy silently yields no parameter defaults.
- **[minor] Primitive Obsession** — `start_date`/`end_date` as free strings (46-47); every adapter re-parses
  and re-validates them (see the date-bound divergence findings under the adapters).

**(d) Maintainability** — good core abstraction; the duplicated kwargs-style detection (noted under
`engine.py`/`vectorbt_adapter.py`) belongs here as shared machinery.

### `runner/engines/lean_adapter.py`

**(a) Bugs / correctness**
- **[critical]** `lean_adapter.py:29` — module-level `LEAN_PROJECT_DIR = Path(os.environ.get(...))` but `import
  os` is absent (imports at 13-24: json, logging, subprocess, tempfile, datetime, pathlib). Any use of the lean
  engine raises `NameError: name 'os' is not defined` **at import time** — `LeanAdapter` is unusable, and
  `cli.get_engine("lean")` / `backtest_grid.get_engine("lean")` crash before returning. Nothing in the test
  suite imports this module eagerly enough to catch it.
- **[major]** `lean_adapter.py:96-104` — invokes `python -m lean backtest ...`; the pip-installed QuantConnect
  CLI exposes a `lean` console script, not a `lean` module — even with the `os` bug fixed, the subprocess path
  would fail with "No module named lean" on standard installs.
- **[minor]** `lean_adapter.py:18` — `import tempfile` unused.
- **[minor]** `lean_adapter.py:126-135` — `_find_result` scans `*.json` recursively and accepts the first file
  containing `"orders"` or `"trades"`; output dirs containing unrelated JSON with those keys pick the wrong
  artifact.

**(b) Security** — nothing beyond executing a local subprocess with a bounded timeout (112) ✓.

**(c) Baseline smells**
- **[minor] Speculative Generality (documented)** — Phase-1 wrapper status is documented in the docstring
  (8-10); the dead execution path is acknowledged tech debt rather than an accidental stub.

**(d) Maintainability** — the module needs its import fixed and its execution story decided (either implement
or reduce to pure result-conversion).

### `runner/engines/native_adapter.py`

**(a) Bugs / correctness**
- **[major]** `native_adapter.py:44-47` — date filtering `df["timestamp"] <= pd.Timestamp(end, tz="UTC")`
  treats a `YYYY-MM-DD` end as midnight **exclusive-of-day**: the entire final requested day is dropped.
  `vectorbt_adapter._parse_date_bound` (1050-1062) deliberately extends date-only ends to `23:59:59.999999`.
  Identical CLI invocations produce different datasets per engine — directly affects walk-forward fold
  boundaries and grid cells.
- **[minor]** `native_adapter.py:148-150` — trades sorted by `(entry_time, id)` then renumbered; `id` at sort
  time is per-symbol sequential, so ties between symbols resolve by symbol processing order — fine, but the
  subsequent equity curve (152-156) orders by `last_exit_time`, so trade ids are not monotonic in equity-curve
  order (cosmetic inconsistency for consumers assuming id≈chronology).
- **[minor]** `native_adapter.py:162-164` — `except Exception: duration_days = 0` swallows malformed config
  dates that `BacktestConfig` users assume are valid.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `TIMEFRAME_ORDER` (20) duplicates `vectorbt_adapter.TIMEFRAME_ORDER` (49);
  `_load_ohlcv` (32-48) is a third variant of the same loader family (with the divergent date semantics above);
  the `BacktestMeta` assembly block (170-187) repeats the vectorbt/pyne template including `"universe": ""`.

**(d) Maintainability** — thin and readable; main debt is the loader/date-handling duplication.

### `runner/engines/pynecore_adapter.py`

**(a) Bugs / correctness**
- **[major]** `pynecore_adapter.py:604-648` — `_apply_risk_sizing` **rewrites historical trades after the fact**:
  quantities and PnL from the PyneCore run are replaced with risk-model math using an equity path approximated
  by iterating `sorted(trades)` and adding recomputed PnL. Trades without `sl_price` keep their original
  quantity/PnL but still advance equity (615-617), so compounded sizing mixes two accounting regimes within one
  run. Reported results are a hybrid of engine output and post-hoc simulation.
- **[major]** `pynecore_adapter.py:530-533` — `df["timestamp"] >= start_date` / `<= end_date` compare tz-aware
  datetimes against raw `YYYY-MM-DD` strings; besides relying on pandas' string-coercion behavior, the end
  bound is midnight-exclusive → final day dropped (same cross-engine divergence as `native_adapter.py:44-47`;
  vectorbt handles it inclusively).
- **[minor]** `pynecore_adapter.py:129-139` — `subprocess.run(..., timeout=300)` ✓ has a timeout, but
  `subprocess.TimeoutExpired` is not caught: one slow/hung `pyne` invocation aborts the whole multi-symbol
  backtest with a traceback, while every other per-symbol failure path merely logs and `continue`s (137-139).
- **[minor]** `pynecore_adapter.py:150-151` — `all_stats = _parse_stats_csv(stats_csv)` overwrites per symbol;
  multi-symbol runs keep only the last symbol's stats.
- **[minor]** `pynecore_adapter.py:416-433` — `_locate_trade_bar` falls back to nearest bar within 1 second;
  DST-shifted or rounded CSV timestamps can bind a trade to the wrong bar and sample wrong indicator meta.
- **[minor]** `pynecore_adapter.py:88` — strategy file copied into temp dir by reading full text; symlinks and
  encoding attributes lost — acceptable, but `Path(strategy_path).read_text()` uses locale default encoding
  pre-3.15 behaviors (no `encoding=` arg) while writers elsewhere specify UTF-8.
- **[minor]** `pynecore_adapter.py:162-165` — equity curve built from `entry_time` ordering (matches vectorbt),
  while native uses `last_exit_time` and signal-replay uses `last_exit_time` — three conventions for the same
  artifact.

**(b) Security**
- **[minor]** `pynecore_adapter.py:251-273` — `_compile_helper_namespace` `exec()`s code extracted from
  strategy files (`<name>_context.py`). Running user strategies is the product's purpose, but helper files are
  loaded *in the runner process* (not the sandboxed `pyne` subprocess) and execute arbitrary code with full
  privileges; worth an explicit trust-boundary note in docs.

**(c) Baseline smells**
- **[major] Duplicated Code** — `_serialize_meta_value` (436-462) is byte-for-byte
  `vectorbt_adapter._serialize_meta_value` (1252-1283); `_coerce_series` (298-317) duplicates
  `vectorbt_adapter._coerce_series` (518-537); `_normalize_trade_context`/`_normalize_trade_context_timeframe`/
  `_trade_context_meta_key` (276-346) re-implement `vectorbt_adapter._normalize_indicator_map`/
  `_infer_indicator_when`/`_indicator_meta_key` with cosmetic renames; `_infer_asset_class` (538-547) duplicates
  `vectorbt_adapter._infer_asset_class_from_path` (1203-1211). The private cross-module import
  (`from runner.engines.vectorbt_adapter import _normalize_drawings, _resolve_max_leverage, ...`, lines 32-36)
  confirms the coupling — these helpers belong in a shared `runner/engines/common.py`.
- **[minor] Middle Man (partial)** — the adapter delegates drawing normalization to vectorbt's private function
  while reimplementing everything adjacent to it — worst of both worlds.

**(d) Maintainability hot spots**
Extract the shared serialization/coercion layer first; it removes ~120 lines and eliminates the
private-import coupling that currently makes vectorbt_adapter changes risky for PyneCore.

### `runner/engines/signal_replay_adapter.py`

**(a) Bugs / correctness**
- **[major]** `signal_replay_adapter.py:386-389` — `except ValueError: return None` around
  `signal_processor.process_signal` silently discards signals the processor rejects; a misconfigured processor
  (e.g. rejecting all entries for missing ATR context) yields an empty-but-successful backtest. All other
  rejection paths at least keep the signal observable.
- **[major]** `signal_replay_adapter.py:643-665` — `_infer_replay_timeframe` hardcodes `return "1m"` whenever
  any signal has an intraday timestamp or a 1m–4h timeframe; a handful of 1h signals forces a 1-minute OHLCV
  download/resample of the full range (via `SignalReplayEngine._download_yfinance`, 776-791) — massive data
  volume and runtime for no benefit when protective exits are absent. The choice is neither configurable nor
  proportional to need.
- **[minor]** `signal_replay_adapter.py:470-475` — fills occur at the *current* bar's close (`searchsorted`
  finds the bar at/after the signal timestamp, then uses its close). With intra-bar-stamped signals this uses
  post-signal price information (mild look-ahead); the event engines fill next-bar open instead — third timing
  model in the area (see also `engine.py`, `vectorbt_adapter.py`).
- **[minor]** `signal_replay_adapter.py:346-352` — when stop and TP are hit in the same bar, stop always wins;
  `engine.py` makes this configurable (`same_bar_order`) and defaults identically, but the replay engine offers
  no override and no intrabar refinement — divergence documented only by reading both implementations.
- **[minor]** `signal_replay_adapter.py:573-575` — `closed_trades.sort(key=lambda t: (t.last_exit_time, ...))`
  sorts ISO strings lexicographically; mixed offset representations (`+00:00` vs `Z`) from upstream converters
  would misorder.
- **[minor]** `signal_replay_adapter.py:501` — `account_balance = max(initial + realized, 1.0)` floors at $1 to
  dodge division-by-zero; a blown account keeps sizing at absurd leverage ratios instead of halting.
- **[minor]** `signal_replay_adapter.py:681-683,689` — `except (ValueError, FileNotFoundError, OSError):
  continue` while reading parquets silently drops unreadable sources; combined with the raise at 790 the user
  sees "Missing OHLCV" even when the file exists but is corrupt.

**(b) Security** — nothing found (yfinance download delegated to data_tools).

**(c) Baseline smells**
- **[minor] Duplicated Code** — `_to_iso` (21-27) duplicates `engine.py:_to_iso` (355-360); the
  `emit(10.0 + ((index / total_signals) * 80.0), "Replaying signals...")` expression is pasted six times
  (455, 482, 495, 498, 523, 546); tag plumbing is done twice (adapter sets tags at 594, engine wrapper
  re-merges them at 812-818).
- **[minor] Data Clumps** — `_OpenPosition` (107-129) carries `stop_loss/take_profit` *and*
  `initial_stop_loss/initial_take_profit` pairs that only differ by trailing mutation; a small trail-history
  object would clarify.

**(d) Maintainability hot spots**
The signal loop in `run` (451-546) interleaves progression bookkeeping, protective-exit polling and flip logic;
a per-symbol position manager extraction would clarify ownership of `last_checked_at` (the most bug-prone
field here).

### `runner/engines/vectorbt_adapter.py`

**(a) Bugs / correctness**
- **[major]** `vectorbt_adapter.py:155-334` vs `:407-417` — position sizing compounds on a **per-symbol**
  `equity` accumulator, but the persisted equity curve restarts at `initial_capital` and replays *globally
  sorted* trades. With >1 symbol, trade N's size was computed from symbol-A's local equity while the curve
  credits it in interleaved chronological order; realized PnL sequence in the curve ≠ sequence used for
  sizing. Reported drawdowns/Sharpe describe an equity path the simulator never traded.
- **[major]** `vectorbt_adapter.py:444` — `total_bars=sum(1 for _ in all_trades)` sets `BacktestMeta.total_bars`
  to the **number of trades**, not bars (PyneCore and native set actual bar counts). Any consumer using
  total_bars for activity/turnover metrics gets nonsense; field semantics corrupted in persisted JSON.
- **[minor]** `vectorbt_adapter.py:201-262` — when `required_notional` exceeds the position cap but fits under
  leverage limit, `final_notional = required_notional` (236-238) — identical in the `if` and first `elif`
  branches; the branch structure suggests a cap that isn't applied (only logging differs). Dead conditional
  complexity around leverage.
- **[minor]** `vectorbt_adapter.py:37` — `_RISK_WARNING_KEYS` is a module-level mutable set used for warn-once
  dedup, but `_process_symbol` runs in `ProcessPoolExecutor` children (370-376): each process gets its own
  copy, so dedup never works across symbols (its exact purpose), and the parent's set stays empty.
- **[minor]** `vectorbt_adapter.py:549-556` — next-bar-open shift force-fills a signal on the very last bar
  (keeps `original_exits.iloc[-1] = True`), so an exit signal on the final bar executes at that same bar's
  open — earlier than the entry convention allows; boundary inconsistency.
- **[minor]** `vectorbt_adapter.py:1159-1176` — intrabar scan checks stop before take-profit within a bar
  (1189-1199) regardless of which was hit first inside the bar; conservative but undocumented (ties into the
  same-bar divergence theme).
- **[minor]** `vectorbt_adapter.py:449` — `"source": "yfinance"` hardcoded in meta regardless of the actual
  parquet origin ( dukascopy/histdata layouts are equally loadable); provenance metadata is wrong for most FX
  runs (same constant in `pynecore_adapter.py:189`).

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[major] Divergent Change / god-functions** — `_process_symbol` spans lines 105-334 (~230 lines) mixing data
  loading, signal normalization, trade simulation, risk sizing, and meta assembly; the module overall (1,296
  lines) adds indicator *presentation* logic (`_supertrend_component`, pane/color styling, hidden-hint lists,
  596-761) that belongs to the viewer layer — chart-taste changes and fill-model changes edit the same file.
- **[minor] Repeated Switches** — `_find_indicator_series` (877-916) chains kind-keyword string comparisons;
  a `{kind: matcher}` map would flatten it and absorb the six thin `_find_*_series` wrappers (919-940).
- **[minor] Primitive Obsession** — `IndicatorMap = dict[str, tuple[pd.Series, str]]` with magic `when`
  strings ("entry"/"exit") recurring through four modules.
- **[minor] Duplicated Code** — `_parse_date_bound` (1050-1062) ≈ `optimizer._parse_period_boundary` (422-434)
  ≈ `walkforward._parse_boundary` (26-36); `TIMEFRAME_ORDER`/`TIMEFRAME_TO_DURATION` duplicated in
  `native_adapter`; `_uses_kwargs_style` duplicated in `engine.py`.

**(d) Maintainability hot spots**
This is the largest file in the area and the drift epicenter (three engines copy fragments of it). Extract:
(a) `runner/engines/ohlcv.py` for loading/date bounds, (b) `runner/engines/serialization.py` for meta
value coercion, (c) move indicator styling to the viewer boundary.

### `runner/grid_tester.py`

**(a) Bugs / correctness**
- **[critical]** `grid_tester.py:74` — `engine.run(config)` violates the `EngineAdapter.run` signature
  (`run(config, strategy_path, progress_callback=None)`, `engines/base.py:104-109`); every cell raises
  `TypeError`, is caught by the broad handler (87-91), and the grid reports `"status": "error"` for all
  combinations. The module cannot have worked in its current form.
- **[major]** `grid_tester.py:77-86` — even absent the crash, `result.stats` is empty for freshly-run engines
  (stats are computed by `cli.cmd_run`, not adapters), so every metric defaults to `float(stats.get(..., 0))`
  → the persisted grid/SQLite rows advertise `status: success` with all-zero Sharpe/CAGR/win-rate. Misleading
  data reaches the portfolio-optimizer consumption path (`shared.database.get_grid_results`).
- **[minor]** `grid_tester.py:110` — `output_path.write_text(json.dumps(payload))` without `encoding=` and
  without `ensure_ascii=False` consistency vs sibling modules; cosmetic.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Duplicated Code** — third implementation of "run strategy over symbol×timeframe grid" alongside
  `backtest_grid.run_backtest_grid` (which is complete) and `backtest_matrix`; README documents
  `runner.grid_tester` as *the* grid tester, so the broken module sits on the documented golden path.

**(d) Maintainability hot spots**
Decide its fate: fix the call and delegate stats to `compute_stats_for_result`, or delete it in favor of
`backtest_grid` and update `README.md:82`.

### `runner/llm_providers/__init__.py`

**(a)/(b)** — nothing found.

**(c) Baseline smells**
- **[minor] Repeated Switches** — `get_llm_provider` if-chain (23-29); a `{name: cls}` registry matches the
  `SUPPORTED_LLM_PROVIDER_NAMES` tuple it mirrors (two structures to update per provider today).

**(d)** — fine otherwise.

### `runner/llm_providers/base.py`

Protocol definition, correct and minimal. Scanned — nothing found.

### `runner/llm_providers/copilot_cli_provider.py`

**(a) Bugs / correctness**
- **[minor]** `copilot_cli_provider.py:65` — the full composed prompt (which contains the entire source code
  and system prompt from `auto_agent`) is passed as an **argv element** (`-p <prompt>`); on Windows' ~32k-char
  command-line limit large conversions fail outright, and on Unix the prompt is world-readable via `ps`
  while the subprocess runs.
- **[minor]** `copilot_cli_provider.py:143-149` — JSON parse failure falls back to treating raw stdout as
  content; a truncated JSON response is silently accepted as prose.

**(b) Security** — prompt visible in process listing (above); executable resolved from env var
(`COPILOT_CLI_EXECUTABLE`) — expected for a dev tool; timeout present ✓.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `_extract_content` (10-25) duplicates `copilot_sdk_provider._extract_content`
  with a *different* key list (`message` position differs, `data/result` missing); `_apply_metadata` (173-192)
  is the third copy of the same helper across providers (also `nim_provider.py:92-111`,
  `copilot_sdk_provider.py:111-137`) — belongs in `base.py`.

**(d)** — otherwise clean and defensively written.

### `runner/llm_providers/copilot_sdk_provider.py`

**(a) Bugs / correctness**
- **[minor]** `copilot_sdk_provider.py:150-175` — `_run_coroutine` bridge: when an event loop is already
  running, work is moved to a daemon thread with an outer `done.wait(timeout+1.0)`; if the inner
  `asyncio.wait_for` deadline passes *between* the outer timeout firing and thread teardown, the daemon thread
  (and its Copilot session) leaks. Also `if "value" in error:` (171) reads identically to the result-dict
  check — the two dicts deserve distinct keys (`error["exc"]`).
- **[minor]** `copilot_sdk_provider.py:43-48` — `_extract_content` iterates `payload.values()` and returns the
  first non-empty string; dict ordering determines whether `content` or an unrelated field wins for unknown
  payloads — nondeterministic content selection for unexpected schemas (sets at 43, 68 compound this).
- **[minor]** `copilot_sdk_provider.py:301-312` — `PermissionHandler.approve_all` auto-approves *all* permission
  requests from the Copilot session; combined with generated-code execution this widens the blast radius of a
  prompt-injected response. Deliberate for automation, but undocumented as a security posture.

**(b) Security** — see approve_all above; `github_token` handled without logging ✓.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `MODEL_ALIASES` (14-19) and the `gpt5_fallbacks` tuple (359-365) encode the
  same mapping knowledge twice; `_compose_prompt` (139-147) duplicates the CLI provider's inline composition
  (57-63).
- **[minor] Long Function** — `generate` (198-282) is five exception taxonomies deep; the metadata triple-write
  pattern (before/except/finally) repeats per provider.

**(d)** — solid error taxonomy overall; centralize `_apply_metadata`/`_extract_content` in `base.py`.

### `runner/llm_providers/nim_provider.py`

**(a) Bugs / correctness**
- **[minor]** `nim_provider.py:9-12` — the OpenAI fallback stub raises only on instantiation; constructing
  `NIMProvider` without the package succeeds and fails later with a less obvious error. Acceptable, but
  validating eagerly in `__init__` (like the API-key check) would fail fast.

**(b) Security** — API key from env, never logged ✓; HTTPS default base_url ✓.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `_apply_metadata` third copy (92-111); the try/except-metadata sandwich in
  `generate` (51-89) repeats the copilot providers' shape.

**(d)** — cleanest of the three providers.

### `runner/optimizer.py`

**(a) Bugs / correctness**
- **[major]** `optimizer.py:544-552` + `634-654` — `risk_parity_covariance` mode prefers returns derived from
  **per-symbol trade-PnL pseudo-equity** (`_asset_returns_frame_from_base_result`) over actual asset prices;
  correlations then measure strategy-trade overlap, not asset co-movement, yet drive covariance risk-parity
  weights and universe selection. Market data is used only when fewer than two symbols have ≥2 trades — the
  methodologically weaker input is the primary one.
- **[minor]** `optimizer.py:398-412` — train/test split at `start + span*pct` lands mid-day for intraday
  ranges; the train period's last bar and the test period's first bar can belong to the same calendar day, and
  the test window starts at an arbitrary intraday timestamp (microsecond nudges at 400-408). Walk-forward
  (`walkforward.py`) aligns on month boundaries — the two OOS methodologies disagree.
- **[minor]** `optimizer.py:274-284` — no-trade trials return penalty `0.0`, which **beats** every trial with a
  negative Sharpe; Optuna is pushed toward parameter regions that avoid trading rather than regions with small
  losses — a biased surrogate.
- **[minor]** `optimizer.py:223` — `data_dir = str(base_result.meta.data.get("data_dir") or "")` reads a key
  that `BacktestMeta.data` producers never set (`vectorbt_adapter` writes timeframes/symbols/universe/source
  only) → auto-download always resolves to the default DATA_DIR even when the base backtest ran elsewhere.
- **[minor]** `optimizer.py:298-301` — `study.best_trial` raises `ValueError` if *all* trials failed (e.g.
  metric missing); unhandled, it escapes as a raw Optuna error instead of the module's usual message-rich
  ValueErrors.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `_parse_period_boundary` (422-434) third copy of the date-bound helper;
  `_json_safe`/`_safe_float_or_none`/`_emit_progress` re-implementations exist in `walkforward.py` and
  `cli.py` under other names.
- **[minor] Long Parameter List** — `_run_risk_parity_covariance_optimization` takes 13 keyword parameters
  (527-542), all unpacked from the caller's locals; a context object would track the growing surface.
- **[minor] Divergent Change (early)** — parameter-mode Optuna flow and risk-parity covariance flow share a
  file with disjoint payloads (329-358 vs 588-624); the two result schemas already diverge
  (`best_value` semantics differ).

**(d) Maintainability hot spots**
The payload-building blocks (326-363, 585-631) are 80% identical — extract a result-schema builder before the
modes multiply further.

### `runner/portfolio_optimizer.py`

**(a) Bugs / correctness**
- **[minor]** `portfolio_optimizer.py:29-45` — `_parse_time_to_ts` calls `datetime.fromisoformat(...).timestamp()`
  on tz-**naive** strings, interpreting them in the *machine-local* timezone; equity points produced by
  `daily_equity` are offset-aware (`+00:00`) so the common path is safe, but any naive-stamped curve (external
  imports, older artifacts) shifts alignment windows by the UTC offset — checklist tz item.
- **[minor]** `portfolio_optimizer.py:265-276` — `correlation_penalty` is computed once outside the objective
  from the *equal-weight-implied* returns and is therefore **constant across trials**; it does not optimize
  diversification, it merely offsets the Sharpe scale (weights don't influence it).
- **[minor]** `portfolio_optimizer.py:302-317` — OOS metrics require strict timestamp intersection
  (`require_overlap=True`); daily curves built from different trade schedules rarely intersect exactly, so
  `oos_sharpe` silently disappears (`_compute_oos_metrics` returns None) even when ample overlapping history
  exists. The forward-fill path used for training (171-187) is not offered to OOS.
- **[minor]** `portfolio_optimizer.py:387` — `target_sharpe` parameter is accepted, documented "for
  constraint", and never used (Speculative Generality).
- **[minor]** `portfolio_optimizer.py:113-131` — `_load_strategy_equity` swallows *all* exceptions
  (`except Exception: return []`), so a database failure is reported as "need at least 2 valid curves".
- **[minor]** `portfolio_optimizer.py:82` — month = fixed 30×24×3600 s; over long ranges the OOS window drifts
  versus calendar months (walkforward uses `DateOffset(months=...)`).

**(b) Security** — `_persist_optimization_result` (372-382) validates the path stays inside
OPTIMIZATIONS_DIR ✓ (nice containment).

**(c) Baseline smells**
- **[minor] Primitive Obsession** — `StrategyEquityPoint.time: int` unix-seconds with parsing at every
  boundary (25, 72, 101); a timestamp-typed point would delete `_parse_time_to_ts` round-trips.
- **[minor] Module-level placement oddity** — `OPTIMIZATIONS_DIR` defined at line 369, *after* half the module
  that references it conceptually; harmless but a readability trap (works only because `_persist_*` runs late).

**(d)** — the alignment utilities (`_align_equity_curves` family) are the reusable core; consider promoting
them next to `risk_parity_optimizer` as a small `runner/allocation` shared module.

### `runner/risk_parity_optimizer.py`

**(a) Bugs / correctness**
- **[minor]** `risk_parity_optimizer.py:117-121` — after SLSQP, `np.clip(weights, lower, upper)` can violate the
  equality constraint before the renormalizing division; if clipping zeroes a weight the renormalized solution
  is no longer the solver's optimum (edge case, but the fallback inverse-vol path doesn't clip at all —
  asymmetric post-processing).
- **[minor]** `risk_parity_optimizer.py:145` — `np.corrcoef(returns, rowvar=False)` on constant columns
  (flat series surviving `fillna(0)`) yields NaN rows already mapped to 0 by nan_to_num — average |corr|
  silently deflates for illiquid universes.
- **[minor]** `risk_parity_optimizer.py:153` — score = `sharpe + 0.35·DR − 0.5·|ρ| − 2·rc_error` mixes
  per-period Sharpe with dimensionless penalties; Sharpe scale depends on return frequency (daily vs hourly
  inputs reorder universes). Frequency sensitivity undocumented.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `candidate_universes` (168-186) duplicates the combination logic of
  `optimizer._candidate_symbol_universes` (476-511, including the cap concept) and the avg-|corr| arithmetic
  appears in both this module (148) and `optimizer`/`portfolio_optimizer` variants.

**(d)** — well-factored numerics overall; the cleanest optimizer module in the area.

### `runner/stats.py`

**(a) Bugs / correctness**
- **[major]** `stats.py:120-166` — metric definitions depend on whether the optional `quantstats` package is
  installed: QS path annualizes via `periods=DAILY_RETURN_PERIODS_PER_YEAR` on resampled daily returns and
  computes `total_return` as the compounded daily-return product (145), while the fallback annualizes via
  inferred observation frequency and computes `total_return` from equity endpoints (153). The same backtest
  JSON yields **different Sharpe/Calmar/total_return on different machines**. For a results-comparison
  platform this is an environment-dependent metric drift, not a rounding quirk.
- **[minor]** `stats.py:247,130-137` — daily resample `ffill()` injects zero-return weekend/holiday days, then
  Sharpe annualizes with √252 over ~365 observations; systematic downward bias vs trading-day calendars
  (and PSR inherits n inflated with zero-variance days).
- **[minor]** `stats.py:173` — infinite `profit_factor` collapses to `None` (JSON-friendly), but callers like
  `backtest_grid.read_backtest_stats` then read `None` and treat it as missing rather than "no losing trades"
  — information loss at the serialization boundary.
- **[minor]** `stats.py:366-367` — degenerate-period fallback returns `now, now` making CAGR 0 silently; a
  warning-level log would preserve diagnosability.
- **[minor]** `stats.py:483-494` — manual Sharpe divides variance by `len(excess)` (population), while pandas'
  `.std()` in the QS path is sample (n−1); tiny-sample PSR/Sharpe discrepancies between paths again.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Duplicated Code** — the manual/QS dual paths (126-166) implement every metric twice; `_sharpe_manual`
  hardcodes 252 while module constants exist (`DEFAULT_PERIODS_PER_YEAR`) — the constant is imported nowhere in
  the manual path.
- **[minor] Long Function** — `compute_stats` (40-189) nests four levels and mixes trade-aggregation and
  curve-aggregation concerns.

**(d) Maintainability hot spots**
Pin the metric definitions: either make quantstats mandatory or port the daily-resample path to the fallback
so both install profiles agree.

### `runner/strategy_ir/__init__.py`
Re-export barrel, consistent `__all__`. Scanned — nothing found.

### `runner/strategy_ir/models.py`

**(a)/(b)** — nothing found (validators thorough, `extra="forbid"` everywhere — good).

**(c) Baseline smells**
- **[minor] Speculative Generality (judgement — documented seam)** — `StateMachine`, `StateTransition`,
  `RawExpression*`, `Drawing*` models have no producer/consumer outside tests today; the package docstring
  declares the incremental-seam intent, so this is accepted scaffolding — noted for future pruning if the IR
  rollout stalls.

**(d)** — exemplary pydantic modeling; no action.

### `runner/strategy_ir/builder.py`

**(a) Bugs / correctness**
- **[minor]** `builder.py:163-169` — entry conditions become one `Condition` per Markdown bullet line; prose
  bullets ("wirde bei Close über SMA") are stored verbatim as `expression` — downstream `codegen` must guess
  semantics from prose (see codegen finding). Confidence metric (225-233) rewards presence, not parsability,
  so unparsable docs score 1.0.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `_coerce_scalar` (45-65) duplicates `normalizer._coerce_scalar` (18-38)
  verbatim; `_extract_section` heading regex overlaps `auto_agent` section parsing.

**(d)** — small, honest module; the risk lives downstream in codegen.

### `runner/strategy_ir/codegen_vectorbt.py`

**(a) Bugs / correctness**
- **[major]** `codegen_vectorbt.py:64-95` — `_rule_from_text` maps *prose* to trading logic by keyword
  sniffing: any condition mentioning "stop" becomes `(primary < 30)` (RSI oversold!), "overbought"/"tp" becomes
  `(primary > 70)`, otherwise `primary > secondary`. Generated strategies can execute logic categorically
  unrelated to the documented strategy (e.g. a stop-loss sentence becomes an RSI threshold) — and because the
  generator is labeled "deterministic", the output inspires unwarranted trust. At minimum the emitted code
  should carry a prominent "heuristic approximation" marker in `STRATEGY_META.tags`.
- **[minor]** `codegen_vectorbt.py:51-61` — `_indicator_defaults` picks the first positive numeric param as
  "window"; an indicator whose only numeric param is a multiplier (e.g. BB std=2) silently becomes a 2-period
  window.
- **[minor]** `codegen_vectorbt.py:155-163,248-249` — with ≥2 indicators the secondary signal is the second
  indicator regardless of role (a volume indicator becomes a price comparator); with 0 indicators the default
  rule compares SMA(14) to close — arbitrary seeds presented as the strategy.

**(b) Security** — generated code contains only literal expressions constructed from identifiers sanitized via
`_safe_identifier` ✓ (no injection path from IR strings into code, aside from `label` strings embedded via
`{spec['label']!r}` which repr-escapes ✓).

**(c) Baseline smells**
- **[minor] Mysterious Name** — `primary_signal`/`secondary_signal` mean "first/second indicator" — the names
  suggest price-axis semantics.

**(d)** — the emission skeleton (176-321) is readable; the heuristic layer is the part needing a honesty pass.

### `runner/strategy_ir/normalizer.py`

**(a) Bugs / correctness** — nothing found (coercion ladder is defensive and tested).

**(b) Security** — `ast.literal_eval` before `json.loads` fallback order is reversed vs typical advice but
safe (both are no-exec parsers) ✓.

**(c) Baseline smells**
- **[minor] Duplicated Code (competing implementation)** — `normalize_strategy_meta_payload` (194-218)
  duplicates `auto_agent._normalize_strategy_meta` with different tag semantics (`labels` vs `type` keys);
  `_extract_markdown_title` (11-15) and `_coerce_scalar` (18-38) are verbatim copies. One of the two
  normalizations should win.

**(d)** — good candidate to become *the* STRATEGY_META normalizer for both auto_agent and the IR builder.

### `runner/strategy_ir/routing.py`

**(a)/(b)** — nothing found.

**(c) Baseline smells**
- **[minor] Mysterious Name / hidden coupling** — `first_llm_attempt=2` (90) encodes "IR attempt happens
  first, legacy LLM becomes attempt 2" — an implicit protocol with `auto_agent.MAX_ITERATIONS` bookkeeping
  that no type or comment pins down; changing either side silently breaks retry accounting.
- **[minor] Speculative Generality (light)** — alias normalization accepts nine spelling variants
  (15-27) for three effective modes.

**(d)** — clean, well-tested decision module.

### `runner/walkforward.py`

**(a) Bugs / correctness**
- **[minor]** `walkforward.py:267-310` — walk-forward folds reuse one fixed parameter set across all windows;
  genuine walk-forward re-optimizes per training window (the docstring's "rolling train/test" is accurate, but
  the README sells this as anti-overfitting validation — it validates *stability*, not selection robustness).
  Expectation gap, not a code defect.
- **[minor]** `walkforward.py:168-203` — `_run_portfolio_walkforward` accepts `step_months` and never uses it
  (single fold by construction); the CLI exposes `--step-months` for both modes (`cli.py:406`) implying
  behavior that doesn't exist in portfolio mode.
- **[minor]** `walkforward.py:133` — merge order `{**compute_stats_for_result(result), **result.stats}`
  prefers pre-existing engine stats, opposite to `cli.py:216` — same data, different precedence rules across
  entry points.
- **[minor]** `walkforward.py:143-147` — `oos_max_drawdown` folded through `abs()`; `stats.max_drawdown_pct` is
  already negative-signed by convention — the summary stores positives while folds store negatives; consumers
  must know which layer they're reading.

**(b) Security** — nothing found.

**(c) Baseline smells**
- **[minor] Duplicated Code** — `_float_or_none` (43-50) ≈ `optimizer._safe_float_or_none` (767-774) ≈
  `backtest_matrix._safe_float` (217-222); `_parse_boundary` (26-36) third date-bound copy;
  `_metric`/`_date_text` micro-helpers fine but add to the pile.

**(d)** — structure is clear; consolidate the numeric coercions area-wide.

---

## Cross-cutting themes

1. **Three execution timing models.** Next-bar-open (vectorbt, native), same-bar-close (signal replay),
   post-hoc restatement (pynecore risk sizing). Plus divergent same-bar stop/TP priority and divergent
   `max_position_pct` semantics. Cross-engine comparisons — the platform's core promise — are currently not
   apples-to-apples. Recommend an explicit `execution_contract` doc (extending
   `docs/signal-risk-execution-contract.md`) and conformance tests with one canonical synthetic dataset.
2. **Copy-drift duplication clusters.** OHLCV loaders + date bounds (3 loaders, 3 boundary parsers, 2
   semantics), meta-value serializers (vectorbt ↔ pynecore byte-identical), STRATEGY_META normalizers
   (auto_agent ↔ strategy_ir.normalizer, behaviorally different), `get_engine` (×2), `write_matrix_csv` (×2),
   `_apply_metadata`/`_extract_content` (LLM providers ×3), `_detect_date_range` (×2, drifted). This is the
   single largest maintainability liability in the area.
3. **Stats-key chaos.** `sharpe` vs `sharpe_ratio`, `net_profit_pct` vs `total_return`, `total_pnl` vs
   `net_profit`, resolved ad hoc via alias maps (`optimizer.TARGET_METRIC_ALIASES`,
   `backtest_grid.read_backtest_stats`) and `or`-fallbacks — each new consumer re-solves the problem.
   Canonicalize in `shared.models.BacktestStats`.
4. **Environment-dependent numbers.** Optional quantstats changes metric definitions (`stats.py`); hardcoded
   `"source": "yfinance"` mislabels provenance; `meta.total_bars` means trades in one engine and bars in two
   others. Persisted-artifact comparability requires contract tests over the JSON schema.
5. **Subprocess discipline.** Timeouts everywhere except `auto_agent._run_backtest`; `lean_adapter` cannot even
   import. A single `runner/process.py` helper (streaming Popen + timeout + log pump) would eliminate the
   class of bugs found here.

## Coverage statement

All 37 files in the area list were read and are accounted for above, including the three empty `__init__.py`
files (recorded as scanned, nothing found) and `llm_providers/base.py` (scanned, nothing found).

## Finding counts by severity

Counts verified against the inline tags above: 2 critical + 19 major + 87 minor tagged on
bugs/correctness/security bullets, plus 5 major + 48 minor tagged on baseline-smell bullets.

| Severity | Bugs/correctness/security | Baseline smells | Total |
|---|---|---|---|
| Critical | 2 | 0 | **2** |
| Major | 19 | 5 | **24** |
| Minor | 87 | 48 | **135** |
| **All** | 108 | 53 | **161** |
