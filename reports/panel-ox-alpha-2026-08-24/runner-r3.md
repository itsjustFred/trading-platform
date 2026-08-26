# Code Review — `runner/` (Reviewer r3, blind & independent)

Scope: all 37 tracked files under `runner/`. Every file is accounted for below (findings or "scanned — nothing found").
Method: full read of every file; targeted greps and two runtime checks (`import runner.engines.lean_adapter`; key existence in `shared/models.BacktestStats`) to confirm suspicions. Read-only: nothing was edited.
Severity: **critical** = wrong results/crash on the main path; **major** = incorrect behavior in realistic scenarios or silent data loss; **minor** = robustness/style/smell.

---

## 1. Bugs / correctness risks

### runner/engines/lean_adapter.py
- **[CRITICAL]** `lean_adapter.py:29` — module-level `os.environ.get(...)` with **no `import os` anywhere in the file**. Importing the module raises `NameError: name 'os' is not defined` (verified by running `python -c "import runner.engines.lean_adapter"`). Because `runner/cli.py:get_engine` imports it lazily inside the function (`cli.py:67`), the crash only surfaces when a user selects the LEAN engine — `--engine lean` is completely broken. It also means the module can never be imported at startup to be caught by smoke tests.

### runner/grid_tester.py
- **[CRITICAL]** `grid_tester.py:74` — `engine.run(config)` omits the required positional argument `strategy_path` (`EngineAdapter.run(config, strategy_path, ...)`). Every grid cell raises `TypeError`, which line 87's per-cell `except Exception` converts into an `"error"` status string. The tool therefore always produces an all-error matrix with no visible traceback — a silent total failure of the feature.

### runner/backtest_grid.py
- **[CRITICAL]** `backtest_grid.py:184` — subprocess mode appends `--keep-indicators` to a `runner.cli **run**` command, but that flag is only defined on the `grid`/`matrix` subparsers (`cli.py:437`, `cli.py:462`), not on `run`. Every subprocess-mode cell exits with argparse error 2 ("unrecognized arguments"). The in-process path (`backtest_grid.py:344`) handles the same flag correctly, so this only bites the documented `--subprocess` mode.
- **[MAJOR]** `backtest_grid.py:113` — `net_profit_pct` is read from persisted stats, but `BacktestStats` has no such field (verified against `shared/models.py:567+`). The fallback chain ends in `total_return`, so the column silently holds `total_return` values under a wrong name; downstream consumers (`build_strategy_overview` in `backtest_matrix.py:240-241`) treat it as a distinct metric.

### runner/cli.py
- **[MAJOR]** `cli.py:115` — intersection logic returns `max(starts), min(ends)` without checking that `max(starts) <= min(ends)`. With symbols whose ranges don't overlap (e.g. one downloaded 2020–2021, another 2024–2026), it returns start > end, which then flows into `BacktestConfig` / `ensure_backtest_data` and yields empty backtests instead of a clear error. Also, when a symbol's parquet read fails, that symbol contributes nothing and the range silently covers only the remaining ones.
- **[MINOR]** `cli.py:104` / `cli.py:98` — arbitrary `matches[0]` selection from `rglob` when several sources hold the same symbol/timeframe (the repo's `data/ohlcv` contains yfinance, dukascopy and huggingface trees). The date range may come from one source while the backtest loads another.
- **[MINOR]** `cli.py:116` — hardcoded fallback dates `("2023-01-01", "2025-12-31")` silently mask missing data (same constants duplicated at `backtest_grid.py:217,221`).

### runner/engine.py (native TradeEngine)
- **[MAJOR]** `engine.py:743` — `self.equity = initial_capital + realized_pnl + unrealized`, but position sizing at `engine.py:349` uses `self.equity / entry_price` for the non-risk path *and* `_calculate_position_size` reads `self.equity` while equity already includes unrealized PnL of currently open positions. Sizing therefore compounds open-trade noise into new trade sizes; also `equity` never reflects cash reduction from open notional, so `max_value = equity * max_leverage` (`engine.py:351`) overstates available buying power while positions are open.
- **[MAJOR]** `engine.py:874` — `merged_row = pd.concat([row, signal_row])` runs for **every bar** even though it is only consumed when `entries.iloc[i]` is true. Beyond the performance cost, if the strategy output frame carries columns whose names collide with OHLCV names, concat produces duplicate-index rows and `row.get(key)` semantics change silently.
- **[MINOR]** `engine.py:879-884` — ordering within a bar is check-stops → fill-orders → create-order → close-on-signal; an exit signal fires after the entry order was queued in the same bar, so a same-bar entry+exit creates a pending market order that fills next bar *after* the flat signal — trades can appear one bar late relative to intent. Documented as `same_bar_order` only for stop-vs-TP, not for signal-vs-signal.
- **[MINOR]** `engine.py:517` — `r_multiple` fallback assumes a LONG default stop (`entry_price * 0.98`); for SHORT positions the synthetic risk distance is computed on the wrong side, inflating/deflating R multiples.
- **[MINOR]** `engine.py:515-516` — `fees = 0.0` hardcoded; `net_pnl == pnl` everywhere in the native engine. If fee models land later, every consumer reading `net_pnl` today is already misled.
- **[MINOR]** `engine.py:60-62` — intrabar resolution is dropped when equal to `config.timeframe`, but a resolution *coarser* than the timeframe passes through and later silently finds no window data.

### runner/engines/vectorbt_adapter.py
- **[MAJOR]** `vectorbt_adapter.py:444` — `meta.total_bars = sum(1 for _ in all_trades)` sets `total_bars` to the **trade count**, while native (`native_adapter.py:109,178`) and pynecore (`pynecore_adapter.py:78,184`) set actual bar counts. Anything comparing bar coverage across engines is wrong for vectorbt.
- **[MAJOR]** `vectorbt_adapter.py:1069` — end-date filtering is inclusive-to-microsecond (`end + 1 day - 1 µs`) here, but `native_adapter.py:47` filters `timestamp <= Timestamp(end)` (midnight). The same config produces different last-bar inclusion per engine — off-by-one-day divergence in results.
- **[MAJOR]** `vectorbt_adapter.py:1032` — `matches[0]` again picks an arbitrary parquet among multiple sources (see cli.py finding).
- **[MINOR]** `vectorbt_adapter.py:37` — module-level mutable global `_RISK_WARNING_KEYS` grows unboundedly and is process-global; in the `ProcessPoolExecutor` path each worker keeps its own copy, so "warn once" is per-process, i.e. warnings reappear across workers.
- **[MINOR]** `vectorbt_adapter.py:155-332` — `_process_symbol` maintains ~10 local variables for sizing math (`risk_target_usd`, `cap_notional`, `leverage_notional_limit`, ...) — long-function smell; see §3.

### runner/engines/native_adapter.py
- **[MAJOR]** `native_adapter.py:45-47` — end-date filter excludes the whole end day (see vectorbt counterpart above); inconsistent cross-engine behavior.
- **[MINOR]** `native_adapter.py:128-129` — appends `config.modeling_resolution` verbatim to `used_timeframes` even when `_load_intrabar_data` normalized/rejected it (e.g. resolution coarser than base returns None earlier, fine — but a raw alias like `"M5"` would be recorded unnormalized).
- **[MINOR]** `native_adapter.py:163` — bare `except Exception: duration_days = 0` swallows real configuration errors (e.g. ISO datetimes) as "0 days".

### runner/engines/pynecore_adapter.py
- **[MAJOR]** `pynecore_adapter.py:99-106,137-139` — if the `pyne` binary is missing or a symbol's run fails, the symbol is skipped with only a log line; the adapter still returns a successful `BacktestResult` with zero trades. Callers cannot distinguish "strategy produced nothing" from "engine absent".
- **[MAJOR]** `pynecore_adapter.py:550-573` (`_normalize_trade_exits`) — rewrites historical exit prices to match meta SL/TP levels whenever the CSV exit price crosses the level by any amount, fabricating fills the underlying engine did not report. Combined with `_apply_risk_sizing` recomputing PnL from these rewritten exits, results are partially synthetic.
- **[MINOR]** `pynecore_adapter.py:32-36` — imports three private helpers (`_normalize_drawings`, `_resolve_max_leverage`, `_resolve_max_position_pct`) from `vectorbt_adapter`; private-cross-module coupling smell (see §3).
- **[MINOR]** `pynecore_adapter.py:189` — hardcodes `"source": "yfinance"` regardless of the actual parquet source found.
- **[MINOR]** `pynecore_adapter.py:272` — `exec()` of strategy-derived helper code (AST-filtered to imports + the two builder functions, so mitigated); note that `Import` nodes are allowed wholesale, so any import side effects execute in-process.

### runner/engines/signal_replay_adapter.py
- **[MAJOR]** `signal_replay_adapter.py:667-682` (`_load_ohlcv`) — merges **all** parquet files matching symbol/timeframe across sources via rglob, deduping with `keep="last"` over an unspecified sort order; whichever source sorts last wins arbitrarily. Silent data provenance issues (compare cli.py/backtest_grid.py which take `matches[0]` — three different strategies for the same problem).
- **[MINOR]** `signal_replay_adapter.py:470-474` — fills at the fill-bar's **close** while vectorbt executes next-bar open; conventions differ between engines without documentation.
- **[MINOR]** `signal_replay_adapter.py:651-652` — any signal whose timestamp has a nonzero hour/minute forces `1m` data for the whole replay; a single misparsed timestamp degrades the run to 1-minute bars.
- **[MINOR]** Neither `SignalReplayAdapter` nor `SignalReplayEngine` implements `EngineAdapter` (no `name`, different `run` signature), yet lives under `runner/engines/` — inconsistent with the package contract.

### runner/stats.py
- **[MAJOR]** `stats.py:120-149` vs `stats.py:150-166` — when QuantStats is installed but `_daily_returns_series_from_equity_curve` returns None (unparseable timestamps, <2 points after cleaning), `sharpe/sortino/calmar/max_drawdown/total_return` all remain 0.0 — the manual fallback block only executes in the `else` branch of `HAS_QUANTSTATS`. Metrics silently vanish depending on an optional dependency and data shape.
- **[MAJOR]** `stats.py:145` vs `stats.py:153` — `total_return` is compounded `(1+r).prod()-1` on the QuantStats path but simple `last/first - 1` on the fallback path. Same input, different published metric.
- **[MINOR]** `stats.py:487,494,501,511` — literal `252` repeated although `DEFAULT_PERIODS_PER_YEAR`/`DAILY_RETURN_PERIODS_PER_YEAR` exist at lines 25/29.
- **[MINOR]** `stats.py:216` — dead attribute probe `e.timestamp` on EquityPoint (only `time` exists); harmless but misleading.
- **[MINOR]** `stats.py:260-278` — `_attach_inferred_freq` is defined and never called anywhere in the package (grep over runner/ shows no call sites).

### runner/daily_equity.py
- **[MINOR]** `daily_equity.py:33-35` — takes the **last** non-empty exit time as the aggregation day; combined with `stockkit_converter.py:135` writing `TradeExit(time="")`, TP1 partial PnL is silently excluded from daily equity (the loop skips empty times, keeping only the final exit).
- **[MINOR]** `daily_equity.py:108` — "preserve existing curve if longer than daily curve" heuristic: a sub-daily engine curve with fewer points than trading days gets replaced by the trade-only daily curve, losing intraday drawdown information.

### runner/walkforward.py
- **[MAJOR]** `walkforward.py:267-310` — "training" folds run the **same fixed parameters** as test folds; no parameter selection happens on IS data, so `is_sharpe` vs `oos_sharpe` is just two periods of one backtest, not walk-forward optimization. The summary invites exactly the wrong conclusion.
- **[MINOR]** `walkforward.py:133` merges `{**compute_stats_for_result(result), **result.stats}` while `cli.py:216` and `backtest_grid.py:285/348` merge the opposite way — freshly computed stats lose to stale persisted stats in walkforward but win everywhere else.

### runner/backtest_matrix.py
- **[MAJOR]** `backtest_matrix.py:90` — `_load_daily_rows` reads `row.get("time")`, but `daily_equity.build_daily_equity_curve` emits rows keyed `"date"` (`daily_equity.py:62`). The `time` column of `matrix_daily_equity.csv` is therefore always empty — downstream BayesOpt/allocation consumers keyed on that column get blank timestamps.
- **[MINOR]** `backtest_matrix.py:333` — `strategy_worker_count` defaults to CPU count while each cell may also spawn up to `max_workers` threads; no global concurrency bound → thread explosion on large machines.
- **[MINOR]** `backtest_matrix.py:37` — very long single-line import list; cosmetic.

### runner/optimizer.py
- **[MAJOR]** `optimizer.py:634-654` (`_asset_returns_frame_from_base_result`) — constructs per-symbol "return series" from **trade-PnL equity curves** (cumsum of trade PnL sampled at entry times) and feeds them to covariance-based risk-parity universe selection. These are strategy-execution artifacts, not asset returns; correlations derived from them are meaningless as asset diversification measures. When this frame has ≥2 columns it *prevents* the correct market-data path (`_asset_returns_frame_from_market_data`) from running (`optimizer.py:544-552`).
- **[MAJOR]** `optimizer.py:298-299` — `study.optimize(objective, ...)` has **no `catch=`**: any exception inside a trial (e.g. `_metric_value` raising on a non-finite metric at `optimizer.py:720-722`, or a data gap) fails the entire study instead of pruning the trial.
- **[MINOR]** `optimizer.py:175` — `timeframe = str(timeframes[0] if timeframes else "1h")` silently guesses when metadata lacks timeframes.
- **[MINOR]** `optimizer.py:500-511` — candidate-universe enumeration truncates combinations at 256 in insertion order (by size then lexicographic), biasing the search toward early symbols; the warning notes it but the bias is silent in results.

### runner/portfolio_optimizer.py
- **[MAJOR]** `portfolio_optimizer.py:387` — `target_sharpe` parameter accepted, documented as "for constraint", and never used anywhere in the module.
- **[MINOR]** `portfolio_optimizer.py:82` — month length approximated as 30 days; a 3-month OOS window drifts ~a week vs calendar months used in `walkforward._build_windows`.
- **[MINOR]** `portfolio_optimizer.py:129-131` — broad `except Exception: return []` hides DB/load failures behind "no data" (also triggers the misleading "Need at least 2 valid..." error message).
- **[MINOR]** `portfolio_optimizer.py:249` — Sharpe computed on raw per-point returns without annualization and with `1e-10` variance floors; not comparable to `stats.py`'s Sharpe, though both are surfaced to users as "sharpe".

### runner/risk_parity_optimizer.py
- Scanned — nothing found beyond style. Math is defensive (PSD shrinkage, jitter, fallbacks). Only nit: `score_risk_parity_universe` recomputes `np.corrcoef` that `shrink_covariance` partly duplicates (minor duplication).

### runner/capm.py
- **[MINOR]** `capm.py:67` — alpha annualized by `* TRADING_DAYS_PER_YEAR` regardless of the series' actual frequency; passing intraday returns overstates annualized alpha by ~×252.
- **[MINOR]** `capm.py:41-54` — alignment drops non-overlapping observations silently; no minimum-overlap-ratio guard (only ≥2 points required).

### runner/auto_agent.py
- **[MAJOR]** `auto_agent.py:804-820` — `subprocess.Popen` with stdout piping but **no timeout**; an LLM-generated strategy that loops forever hangs the agent indefinitely (`proc.wait()` unbounded). Checklist item "subprocess without timeout".
- **[MINOR]** `auto_agent.py:26` — imports private helper `_detect_date_range` from `runner.cli`.
- **[MINOR]** `auto_agent.py:215-273` — retry loop regenerates code but validation failure context is capped to the last 8000 chars (`_truncate` keeps tail); the *first* exception line (usually the root cause in tracebacks printed first) can be cut off depending on interleaving.
- **[MINOR]** `auto_agent.py:591-597` — LLM-chosen titles become directory names after slugify; collision with an existing strategy folder silently overwrites its README.md/strategy file (`write_text` without existence check).

### runner/llm_providers/nim_provider.py
- Scanned — clean. API key handled via env var, timeouts delegated to OpenAI client, metadata applied on success/error paths. No findings.

### runner/llm_providers/copilot_cli_provider.py
- **[MINOR]** `copilot_cli_provider.py:56-63` — system prompt concatenated into the user prompt (prompt-injection surface is inherent to the design; acceptable but worth noting since auto_agent relies on strict output contracts).

### runner/llm_providers/copilot_sdk_provider.py
- **[MINOR]** `copilot_sdk_provider.py:150-175` — `_run_coroutine` spawns a daemon Thread running a *second* event loop when already inside a running loop; the outer `done.wait(timeout + 1.0)` races the inner `asyncio.wait_for`, so near-simultaneous timeouts can raise either error; daemon thread leaks if outer wait expires first.
- **[MINOR]** `copilot_sdk_provider.py:301` — `PermissionHandler.approve_all` auto-approves every permission request from generated code; appropriate for sandboxed conversion but worth flagging as a deliberate trust decision.

### runner/converters/lean_converter.py
- **[MAJOR]** `lean_converter.py:81-97` — pairing logic consumes orders pairwise *after the first recognized entry*: if a second entry follows before any exit (pyramiding, or tag heuristics failing), the second **entry order is recorded as the first trade's exit**, producing fabricated PnL from two entries. Tag sniffing (`"Entry" in tag or tag in ("LONG","SHORT")`, line 87) is fragile against LEAN tag variety.
- **[MAJOR]** `lean_converter.py:73-77,105-111` — per-trade PnL is looked up from LEAN's `profitLoss` map keyed by exit timestamp; LEAN's profitLoss is portfolio cumulative PnL, so each trade's `pnl` is actually the whole-portfolio value at that moment unless the map happens to be per-event. Fallback price math ignores quantity sign mismatches and fees.
- **[MINOR]** `lean_converter.py:150-170` — `_build_meta` discards almost everything: `initial_capital` hardcoded to 100000, empty start/end, empty symbols (partially patched afterwards in `converters/cli.py:61-62`).

### runner/converters/stockkit_converter.py
- **[MAJOR]** `stockkit_converter.py:135` — TP1 partial exit row written with `time=""` (comment admits exact time unknown); `TradeExit.time` is documented as ISO-8601 UTC and consumers (`daily_equity.py`, stats, viewers) rely on it. This silently drops TP1 PnL from daily aggregation and breaks timeline sorting for such trades.
- **[MINOR]** `stockkit_converter.py:109-111` — `int()` casts reject fractional-share rows entirely (row skipped with a warning).
- **[MINOR]** `stockkit_converter.py:171-172` — slippage stored as `fees`, conflating two distinct concepts.
- **[MINOR]** `stockkit_converter.py:184-190` — naive datetime parsed then stamped UTC without DST/exchange-timezone consideration (checklist: tz-naive datetimes).

### runner/converters/cli.py
- **[MINOR]** `converters/cli.py:53-58` — the lean and stockkit branches are character-identical and the trailing `else` is unreachable given the membership check at line 46.

### runner/engines/base.py
- Scanned — clean. `BacktestConfig` properly validated; `validate_config` documents optional behavior. Note `start_date`/`end_date` are typed `str` without emptiness validation while most call sites now treat them as optional — a latent contract mismatch, minor.

### runner/strategy_ir/builder.py
- **[MINOR]** `builder.py:172-184` — exit-kind inference by English/German keyword substring ("stop", "tp", "time") — misclassification likely for paraphrased docs; deterministic-but-crude.
- **[MINOR]** `builder.py:225-233` — confidence is a fixed 0.25-per-section score presented as a graded metric.
- **[MINOR]** `builder.py:45-65` — `_coerce_scalar` treats `"123"` inside quotes correctly but also coerces numeric-looking quoted strings inconsistently vs `normalizer._coerce_scalar` — two divergent copies of the same concept (see Duplicated Code).

### runner/strategy_ir/normalizer.py
- **[MINOR]** `normalizer.py:18-38` — third copy of `_coerce_scalar` (with `auto_agent` and `builder` variants). Behavior differs subtly between copies (e.g. quoted-number handling).
- Otherwise scanned — solid defensive coercion logic.

### runner/strategy_ir/models.py
- Scanned — clean, well-validated Pydantic schemas. Only observation: `StateMachine`, `RawExpression`, `Drawing`, `ConditionSide`, several enums have no producer/consumer in the package yet (speculative generality candidate; presumably staged for future phases).

### runner/strategy_ir/codegen_vectorbt.py
- **[MINOR]** `codegen_vectorbt.py:64-95` — rule inference maps prose to thresholds like `(primary > 70)` for any text containing "tp"/"take profit"; generated strategies can silently contradict the documented logic (heuristic acknowledged in docstring but consequences worth restating).
- **[MINOR]** `codegen_vectorbt.py:158-163` — primary/secondary indicator choice is "first two alphabetically", unrelated to the described crossover pairs.

### runner/strategy_ir/routing.py
- Scanned — clean. Small state machine, well-factored.

### runner/strategy_ir/__init__.py
- Scanned — re-export barrel only; consistent with contents.

### runner/__init__.py, runner/engines/__init__.py, runner/converters/__init__.py
- Scanned (all three verified intentionally empty, 0 bytes) — nothing found.

### runner/llm_providers/base.py
- Scanned — Protocol with correct signature; nothing found.

### runner/llm_providers/__init__.py
- Scanned — factory matches declared provider tuple; nothing found.

---

## 2. Security

No SQL string building anywhere in the area (SQLite access goes through `shared.database`). Command execution inventory:
- **[MAJOR]** `auto_agent.py:804-820` — unbounded `Popen` (hang DoS); command list built from internal args only (no shell injection), but the executed Python code itself is LLM-generated by design — the trust boundary is the strategy file, and the child inherits the parent environment (`os.environ.copy()`, line 799).
- **[MINOR]** `pynecore_adapter.py:272` — `exec()` over AST-filtered strategy helpers; filter permits arbitrary `Import` statements, so imported-module side effects run in-process. Acceptable only because strategies are trusted artifacts.
- **[MINOR]** `copilot_sdk_provider.py:301` — `approve_all` permission handler grants generated code full tool permissions during conversion sessions.
- **[MINOR]** `vectorbt_adapter.py:464-469` / `native_adapter.py:23-29` — `exec_module` of arbitrary strategy paths (inherent to the product; noted for completeness). Module name is always `"strategy"`, so two different strategy files loaded in one process share `sys.modules` identity quirks — minor.
- Secrets: `NIMProvider` requires `NVIDIA_API_KEY` from env (`nim_provider.py:24-26`) — good; no secrets logged. `copilot_sdk_provider.py` accepts `github_token` via constructor/env without logging — good.
- `lean_adapter.py:96-114` — fixed-arg subprocess with timeout=600; safe construction.

---

## 3. Baseline smells (Fowler ch.3)

All judgement calls; repo conventions don't document overrides for any of these.

- **Duplicated Code (systemic)** — the strongest theme of this area:
  - Date-boundary parser (`parse → utc → extend end-of-day`): `vectorbt_adapter.py:1050-1062`, `walkforward.py:26-36`, `optimizer.py:422-434` — three identical implementations.
  - Engine lookup: `cli.py:56-75` and `backtest_grid.py:190-208` — parallel `get_engine`s with slightly different errors (drift already visible).
  - `_load_ohlcv` + rglob + lowercase-columns: `vectorbt_adapter.py:1023-1047`, `native_adapter.py:32-48`, `pynecore_adapter.py:512-535`, `signal_replay_adapter.py:667-713`, `cli.py:85-116` (date-range variant), `backtest_grid.py:211-222` — six variants with **three different multi-match resolutions** (`matches[0]`, sorted-first, concat-all) and two different end-date semantics.
  - `_uses_kwargs_style` / `_call_generate_signals`: `engine.py:205-229` and `vectorbt_adapter.py:75-102` — copy-paste including comments.
  - Risk-sizing math (risk$ → qty, caps, leverage limit): `vectorbt_adapter.py:201-259`, `pynecore_adapter.py:604-648`, `engine.py:294-353` (+`risk/engine` outside area) — three hand-rolled variants with divergent cap semantics.
  - `_serialize_meta_value`: `vectorbt_adapter.py:1252-1282` and `pynecore_adapter.py:436-462` — identical.
  - `_coerce_series`: `vectorbt_adapter.py:518-537` and `pynecore_adapter.py:298-317` — identical modulo error text.
  - Indicator "when"-inference: `vectorbt_adapter.py:851-861` (`_infer_indicator_when`), `pynecore_adapter.py:276-286` (`_normalize_trade_context_timeframe`), plus meta-key mapping twins at `vectorbt_adapter.py:864-874` / `pynecore_adapter.py:289-295`.
  - `_apply_metadata`: `nim_provider.py:92-111`, `copilot_cli_provider.py:173-192`, `copilot_sdk_provider.py:111-136` (third variant extended).
  - `_extract_content`: `copilot_cli_provider.py:10-25` vs `copilot_sdk_provider.py:28-55`.
  - `_coerce_scalar` ×3: `auto_agent.py` (inline via ast/literal paths), `strategy_ir/builder.py:45-65`, `strategy_ir/normalizer.py:18-38`.
  - `_detect_date_range` fallback constants `"2023-01-01","2025-12-31"`: `cli.py:116`, `backtest_grid.py:217,221`.
- **Repeated Switches** — `cli.get_engine` / `backtest_grid.get_engine` if-chains; a shared name→class map (one module) would delete both and the drift between them.
- **Shotgun Surgery** — adding a timeframe alias or changing date-boundary semantics requires touching 3-6 files (see Duplicated Code list); adding an engine touches `cli.py`, `backtest_grid.py`, and the `cmd_engines` listing table (`cli.py:242-247`).
- **Long Function / low cohesion** — `_process_symbol` (`vectorbt_adapter.py:105-334`, ~230 lines mixing I/O, sizing, trade construction); `run_auto_agent` (`auto_agent.py:106-273`); `compute_stats` (`stats.py:40-189`); `SignalReplayAdapter.run` (`signal_replay_adapter.py:407-624`).
- **Data Clumps** — `(signals, indicators)` passed as a pair through ~20 `_find_*`/`_resolve_*` functions in `vectorbt_adapter.py:877-1000`; the sizing variable cluster in `_process_symbol` (10 locals, some `float|None`) begs for a `SizingContext` type; `(series, when)` tuples recur as a pseudo-type `IndicatorMap` instead of a small class.
- **Primitive Obsession** — dates as strings throughout (`start_date: str` in `BacktestConfig`, ISO strings in `Trade`), leading to per-file parsing utilities (see duplication); `ExecutionModelSettings` fields are strings where enums would prevent invalid states (`engine.py:41-47`).
- **Speculative Generality** — `stats._attach_inferred_freq` (`stats.py:260-278`, no callers); unused `target_sharpe` param (`portfolio_optimizer.py:387`); unused IR model types (`StateMachine`, `RawExpression`, `Drawing`, `ConditionSide` — defined, exported, never constructed in-package); `engine.ExecutionModelSettings` self-describes several fields as "metadata-only until a later branch wires them in" (`engine.py:36-39`) — documented staging, borderline acceptable.
- **Feature Envy** — `daily_equity.attach_daily_equity` mutates `result.meta.data`/`result.stats` dicts of `BacktestResult` from outside; same for `signal_replay_adapter.py:812-818` post-processing `result.meta.tags`. Belongs on `BacktestResult`.
- **Middle Man** — `converters/cli.py:53-58` dispatch adds nothing over the converter map; `backtest_matrix` mostly forwards kwargs to `run_backtest_grid` (acceptable as orchestrator, but the 17-parameter forwarding signature is fragile).
- **Refused Bequest** — none observed; inheritance use is minimal and appropriate (EngineAdapter ABC).
- **Mysterious Name** — minor: `_metric(stats,...)` (`walkforward.py:53`), `probe`/`entry` shadowing in `backtest_matrix.py:298-307`, `when` meaning "evaluation stage" in IndicatorMap tuples. Not rename-worthy blockers.

---

## 4. Maintainability hot spots

1. **`vectorbt_adapter.py` (1296 lines)** — the gravity well of the area: data loading, execution-model shifting, indicator display heuristics, supertrend cosmetics, drawings normalization, sizing math and trade extraction in one module. Splitting data-loading (shared!), execution shifting, and presentation would resolve most duplication listed above. Highest-value refactor target.
2. **Cross-engine consistency** — no single definition of "how a bar range is filtered" or "which parquet wins": three engines answer differently. Any cross-engine comparison feature inherits silent skew.
3. **Grid/matrix subsystem** — `backtest_grid.py` duplicates `runner.cli` orchestration in-process *and* shells out to it in subprocess mode; the two modes already disagree (`--keep-indicators` bug), and `grid_tester.py` is a third, broken parallel implementation of the same idea. Consolidation candidate: delete `grid_tester.py` (its SQLite persistence lives in `shared.database` and its only caller is a test).
4. **`engine.py` equity/sizing model** — equity includes unrealized PnL while sizing treats it as spendable cash; correctness work needed before fee models or multi-position margin arrive.
5. **Stats dual-path** — quantstats/manual divergence means every metric has two definitions; pinning one canonical implementation (or making the fallback structurally identical) removes a class of silent metric drift.

---

## Finding counts by severity

| Severity | Count |
|---|---|
| Critical | 3 |
| Major | 22 |
| Minor | 52 |

(Totals include bugs, security items and baseline smells as itemized above; files with no findings are recorded as scanned-clean in their sections.)
