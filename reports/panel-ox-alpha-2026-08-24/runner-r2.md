# Code Review — Area `runner` (Reviewer r2)

Blind, independent review of all 36 tracked code files under `runner/`.
Scope: (a) bugs/correctness risks, (b) security issues, (c) Fowler baseline smells
(_Refactoring_ ch.3, with the two binding rules: repo overrides suppress endorsed smells;
every smell is a judgement call), (d) maintainability hot spots.
Every finding cites `file:line` and a severity (critical / major / minor).
Files with no findings are recorded as scanned-clean.

---

## 1. `runner/engines/vectorbt_adapter.py` (1296 lines)

### Bugs / correctness
- **major** `vectorbt_adapter.py:37-44` — `_RISK_WARNING_KEYS` is an unbounded module-global mutable set with no thread/process safety. It is read/written by worker processes spawned at `:370` (each gets a copy, so dedup silently fails across processes) and, in-process, it grows without bound for unbounded symbol×timeframe keys (`(symbol, timeframe, "fallback_stop")`). Also a latent race if the module is ever used from threads.
- **major** `vectorbt_adapter.py:118-123` — when a symbol has no data, `_process_symbol` returns `(trades=[], extra_timeframes=[], …)` so `used_timeframes` never gains the intrabar timeframe for that symbol, but worse: the caller cannot distinguish "no data" from "engine produced nothing"; the run succeeds with zero trades and no error for that symbol while other symbols produce results (silent partial backtest).
- **major** `vectorbt_adapter.py:155-161` — position state machine: `entries.iloc[i]` while already in a trade is ignored entirely (no pyramiding, no re-entry on same bar), and an entry bar followed immediately by an exit signal on the *same* index works, but a second `entry` while `in_trade` is dropped without record. More importantly, `entry_idx = None` sentinel: if `exits.iloc[i]` were ever true while `in_trade=False`, the branch order protects it — OK — but after a trade closes on bar `i`, a new entry on the same bar `i` is skipped (`elif`), losing signals. Judgement: intentional single-position model, but undocumented.
- **major** `vectorbt_adapter.py:201-261` — risk-based sizing uses `equity` that only updates on trade close (`:332 equity += pnl`); concurrent open positions are impossible in this loop, fine — but the leverage branch logs "leverage used" (`:239`) yet still sets `final_notional = required_notional` even when `required_notional > cap_notional` but `< leverage_notional_limit`; when `max_leverage < max_position_pct` the code silently raises leverage to the position cap (`:207-208`), i.e. a user-specified conservative `max_leverage=1` is overridden to e.g. 20% position sizing semantics. Surprising override, undocumented.
- **minor** `vectorbt_adapter.py:264-269` — synthesized stop price uses `direction == "LONG"` only; a malformed direction value (anything not `"SHORT"`) defaults to LONG stop placement, consistent with `_normalize_direction_value` (`:943-946`) which maps *everything* unknown (including `0`, `None`, garbage strings) to LONG. A typo like `"Long "` or `"long"` in a strategy column becomes a silent LONG trade.
- **major** `vectorbt_adapter.py:408-410` — trades from multiple symbols are sorted by string `entry_time` and merged into one capital pool, but each symbol's trades were sized against its own per-symbol equity path (`_process_symbol` runs one independent compounding sequence per symbol, `:332`). The post-hoc global re-equity curve (`:413-417`) double-counts: N symbols each compounding from full `initial_capital` are summed, producing equity far above any real portfolio. Same defect pattern in `pynecore_adapter.run` (:162-165) and `native_adapter.run` (~:150s). For multi-symbol runs the reported PnL/equity is not economically meaningful.
- **minor** `vectorbt_adapter.py:444` — `total_bars=sum(1 for _ in all_trades)` reports the *trade count* as `total_bars`. Misleading metadata (compare pynecore which accumulates real bars).
- **minor** `vectorbt_adapter.py:549-556` — `_prepare_next_open_execution_data` forces the last bar's exit True if the original last-bar exit was True (`shift` would drop it). Reasonable lookahead guard, but it also shifts *indicators* used for meta sampling (`:558-563`) while `stop_loss_series` etc. sampled at `entry_idx` now refer to the previous bar's values — consistent next-open semantics, but the intrabar stop/TP levels resolved at `:192-193` therefore lag the actual entry bar by one period; stops set adaptively on the signal bar are applied to the execution bar unchanged.
- **major** `vectorbt_adapter.py:1181-1200` — `_match_intrabar_exit_event`: when both stop and TP are hit inside the same intrabar bar, **stop always wins** regardless of the configured `same_bar_order` execution-model setting honored by the native engine (`engine.py:613-618`). Cross-engine inconsistency: vectorbt results are systematically pessimistic vs. native `take_profit_first` configs.
- **minor** `vectorbt_adapter.py:1032` — `_load_ohlcv` takes `matches[0]` of an `rglob` without sorting; when several asset-class folders contain the same symbol/timeframe, an arbitrary parquet wins (rglob order is filesystem-dependent). Compare `signal_replay_adapter._load_ohlcv` (:681) which sorts. Non-deterministic data selection.
- **minor** `vectorbt_adapter.py:1060-1062` — date-bound end expansion adds a day minus 1µs only when neither `T` nor `:` present; a `YYYY-MM-DD HH:MM` (space-separated, no `T`) end timestamp misses this branch — inconsistent boundary semantics.
- **minor** `vectorbt_adapter.py:1155` — intrabar window filter `timestamps >= effective_start` includes the entry bar itself; combined with `low <= stop_price` a stop placed exactly at the entry bar's low triggers instantly at the same timestamp as entry (zero-holding-period trade).

### Security
- **minor** `vectorbt_adapter.py:464-469` — `_load_strategy_module` executes arbitrary Python from `strategy_path` via `exec_module`. Inherent to the product (users run their own strategies), but note the auto-agent pipeline (`auto_agent.py`) writes LLM-generated code here and executes it without sandboxing — see §3 of auto_agent.

### Smells
- **Duplicated Code** `vectorbt_adapter.py:1023-1047` vs `pynecore_adapter.py:512-535` vs `native_adapter.py:_load_ohlcv` vs `signal_replay_adapter.py:667-713` — four near-identical OHLCV loaders (`rglob(f"**/{symbol}/{timeframe}.parquet")`, lowercase columns, timestamp parse, date slice) with drifting details (sorted vs not; date parsing via `_parse_date_bound` vs raw string compare). Shotgun-surgery-prone: the date-boundary fix in `_parse_date_bound` exists only here.
- **Duplicated Code** `vectorbt_adapter.py:851-874` (`_infer_indicator_when`, `_indicator_meta_key`) near-verbatim copies in `pynecore_adapter.py:276-295`; `_coerce_series`, `_serialize_meta_value`, `_normalize_indicator_map`/`_normalize_trade_context` also duplicated between the two adapters.
- **Repeated Switches** `vectorbt_adapter.py:877-916` — `_find_indicator_series` is a kind-string dispatch over hard-coded name lists; adding a new pseudo-indicator means touching this chain plus a new `_find_*_series` wrapper (`:919-940`). A shared name→kind map would collapse six wrappers.
- **Data Clumps** `vectorbt_adapter.py:172-183` — eight sequential `float | None` locals describing one concept (position sizing result); belongs in a small `SizingDecision` value object.
- **Primitive Obsession** `vectorbt_adapter.py:49-60` — timeframes carried around as bare strings with parallel `TIMEFRAME_ORDER` list + `TIMEFRAME_TO_DURATION` dict; `shared.constants.normalize_timeframe` already exists elsewhere, third partial reimplementation (also `backtest_grid.TIMEFRAME_ALIASES`, `native_adapter.TIMEFRAME_ORDER`).
- **Long Function** `_process_symbol` (`:105-334`, ~230 lines) mixes data loading, signal normalization, the trade loop, risk sizing, meta assembly. Prime extract-method candidate.

### Maintainability hot spots
The adapter has accreted three subsystems (signal contract normalization, execution simulation, display/drawing plumbing) in one module; the risk-sizing block alone (`:201-303`) would be a self-contained module. Highest-change-magnet file in the area.

---

## 2. `runner/engine.py` (903 lines)

### Bugs / correctness
- **critical** `engine.py:874` — `merged_row = pd.concat([row, signal_row])` concatenates two Series *vertically*, not element-wise. The result is a Series of length `len(row)+len(signal_row)` where duplicate labels form a repeated index; `row.get("stop_loss")`/`_extract_levels(merged_row)` then read from this stacked Series. When the signal frame contains a column named like a price column (e.g. strategy emits an indicator called `open` or `close`), values collide ambiguously; and `pd.concat` on mismatched dtypes produces an object-dtype Series, making every subsequent numeric comparison fragile. The evident intent was `pd.combine_first` / a horizontal merge (`pd.concat([row, signal_row], axis=0)` is what happens; axis join semantics of Series concat are vertical by design). Every direction/level lookup downstream operates on corrupted data whenever `signal_row` shares labels with `row` — and `_signals_frame` deliberately contains `entries`, `exits` plus indicator columns, so label overlap with `row`'s OHLCV columns is common for indicator names like "high"/"low". At minimum this doubles row length and breaks `.get()` expectations.
- **major** `engine.py:156-167` — `RiskConfig(max_risk_per_trade=configured_risk_pct if configured_risk_pct > 0 else 0.01, ...)` — when the user did *not* enable risk sizing, a hidden default 1% risk engine is still constructed (unused unless `_risk_enabled`, OK) but `max_total_exposure` defaults to `config.max_position_pct or 0.05` — conflating "max position notional %" with "total portfolio exposure %". Two different risk concepts fed from one knob.
- **major** `engine.py:159` — `int(float(self.config.parameters.get("max_positions", 5) or 5))` — a strategy parameter named `max_positions` silently controls engine-level risk caps; user strategy params leak into risk policy (surprising cross-talk, undocumented).
- **major** `engine.py:302-353` — `_calculate_position_size` reads `fixed_risk_amount` / `Fixed_Risk_Amount` / `tick_value` / `tick_size` / `lot_step` / `min_lot` / `max_lot` straight out of `parameters` with per-call defaults — a hidden parameter protocol discoverable only by reading engine internals.
- **major** `engine.py:466-473` — MARKET orders get re-sized at fill using fill-price-derived stop (`order.quantity = self._calculate_position_size(...)`), but LIMIT/STOP/STOP_LIMIT orders keep the quantity sized at creation price — inconsistent sizing semantics across order types, undocumented.
- **major** `engine.py:600` — `for _, row in window.iterrows():` — O(n) row iteration per bar over the intrabar window makes intrabar replay quadratic in practice for dense windows; `_intrabar_window` re-filters the entire intrabar frame per bar (`:576-583`) instead of using searchsorted on a pre-sorted frame (it *is* pre-sorted in `_prepare_intrabar`).
- **minor** `engine.py:205-219` — `_uses_kwargs_style` imports `inspect` inside function twice (`:208`, `:224`); trivial but noise.
- **minor** `engine.py:233-235` — `stop_loss = float(stop_loss) if pd.notna(stop_loss) else None` — if both `stop_loss` and `sl_price` are NaN, `float(nan)` guard is right, but if `row.get("sl_price")` is a non-numeric string, `float()` raises ValueError mid-backtest rather than being coerced/tolerated.
- **minor** `engine.py:243` — `lower.startswith("tp") and lower[2:].isdigit()` accepts `tp0`… but also `tp00`; fraction keys derived as `f"{name}_pct"` require exact `tp1_pct` naming — asymmetric contract, easy to get wrong.
- **minor** `engine.py:517` — `risk_per_share = abs(entry - (stop_loss or entry*0.98))` — `or` treats a legit stop_loss of `0.0` as falsy (edge case, but `0` stop is nonsense anyway); however for SHORT positions whose stop > entry, `abs()` masks sign, and R-multiples for shorts use the same formula — OK due to abs, but subtle.
- **minor** `engine.py:888-896` — progress emit condition `i >= next_emit or i == total_bars - 1` with `next_emit = i + step_bars` — first emit happens at bar 0 (`next_emit=0`), fine; cosmetic only.
- **minor** `engine.py:743` — `self.equity` includes unrealized PnL, and `_calculate_position_size` sizes new positions off mark-to-market equity — margin modeling is implicit; combined with `max_leverage` cap this is plausible but nowhere documented.

### Security
- Nothing beyond strategy-execution-by-design (module exec happens in the adapters, not here).

### Smells
- **Speculative Generality** `engine.py:25-29` + `:96-107` — `OrderType.LIMIT/STOP/STOP_LIMIT` and `Order.limit_price/stop_price` are fully implemented in `_fill_pending_orders` but no code path ever creates such orders (`_create_order` only builds MARKET, `:381`). Dead capability surface kept alive at maintenance cost (the checklist item "delete/inline" applies until limit orders ship).
- **Duplicated Code** `engine.py:187-203` (`_coerce_bool_series`/`_coerce_float_series`) duplicates `vectorbt_adapter._coerce_series` family; `_to_iso` (`:355-360`) duplicates `signal_replay_adapter._to_iso` verbatim; `_uses_kwargs_style`/`_call_strategy` (`:206-229`) duplicates `vectorbt_adapter._uses_kwargs_style`/`_call_generate_signals`.
- **Divergent Change** — `TradeEngine` changes both when execution semantics change (fills/stops/intrabar) and when the strategy-output contract changes (normalization/meta extraction). Two reasons to change in one class.
- **Long Method** `run` (`:843-903`) plus `_check_stops` (`:626-724`) — the TP ladder + stop interaction logic nested five deep.
- **Primitive Obsession** — directions as `"LONG"/"SHORT"` strings, `same_bar_order` normalized strings, `market_order_timing` free-form strings recorded into metadata without enum validation (`ExecutionModelSettings.from_config` lowercases `same_bar_order` but accepts any string for the other four fields, `:72-77` — a typo like `next_bar_opne` is persisted as-is).

---

## 3. `runner/auto_agent.py` (843 lines)

### Bugs / correctness
- **major** `auto_agent.py:798-822` — `_run_backtest` spawns `subprocess.Popen` reading stdout line-by-line with **no timeout**. A hung child backtest hangs the whole agent forever (the fixed checklist explicitly flags subprocess-without-timeout). `timeout_seconds` parameter exists (`:119`, `OPENAI_TIMEOUT_SECONDS = 600`) but is only forwarded to the LLM provider, never to the backtest process.
- **major** `auto_agent.py:215-273` — retry loop writes each regenerated strategy over the same `artifacts.strategy_path`; if all iterations fail, the last (broken) generated file remains on disk inside `strategies/`, where the strategy registry will pick it up as a runnable strategy. No cleanup/quarantine on failure.
- **major** `auto_agent.py:282-292` — `_build_strategy_artifacts` derives directory names from LLM-produced titles via `_slugify` (`:757-759`) — sanitized enough for traversal (`[^a-zA-Z0-9]+` → `_`, strips leading/trailing dots is *not* done: a title of `....` slugifies to `auto_strategy`, OK; but a requested_name like `..%2f..` can't produce slashes since `/` is replaced — acceptable). Residual risk: collision/overwrite — two different strategies with the same slug silently overwrite each other's files (`strategy_dir.mkdir(exist_ok=True)`, `write_text` clobbers).
- **minor** `auto_agent.py:139-143` — `source_code.count("\n") + 1` counts final-newline-terminated files correctly-ish; cosmetic.
- **minor** `auto_agent.py:509-522` — fallback brace-scrape `candidate[start:end+1]` + `ast.literal_eval` will happily parse `{...}` inside a *string literal* of unrelated prose; low probability given upstream extraction, but the failure mode is a confusing TypeError far from cause.
- **minor** `auto_agent.py:662-711` — `_resolve_backtest_target`: when caller passes symbols *and* timeframe, catalog scan is skipped (fine); but when only symbols given, `min(filtered, key=_entry_rank)` picks e.g. equities/1h deterministically — implicit target selection is surprising but logged.
- **minor** `auto_agent.py:701-704` — imports private `_detect_date_range` from `runner.cli` — layering inversion (agent → CLI → grid), couples the pipeline to CLI internals (see Shotgun Surgery below).

### Security
- **major** `auto_agent.py:225-255` — the pipeline persists LLM output to `strategies/<slug>/<slug>.py` and immediately executes it via a subprocess backtest. There is no static screening of generated code (no AST allow-list, no import restrictions) before `python -m runner.cli run` executes it with the repo's full privileges (including `.env` credentials loaded elsewhere in the app). Prompt-injected or hallucinated code runs unrestricted. Given the threat model (user-supplied source code → LLM → exec), this deserves explicit acknowledgment/mitigation (e.g., subprocess sandboxing, import allow-list validation before write).
- **minor** `auto_agent.py:799` — `env = os.environ.copy()` forwards the full parent environment (including API keys) to the child process; harmless locally, but worth minimizing for a worker that may be wired to remote job runners.

### Smells
- **Shotgun Surgery** `auto_agent.py:26` + `optimizer.py:16` both import `_detect_date_range` from `runner.cli`; the detection logic actually lives in three places (cli, backtest_grid with a *different* signature returning different fallback dates, walkforward's `_parse_boundary`).
- **Long Module / Divergent Change** — prompts (German copy, `:295-410`), LLM response parsing (`:456-528`), artifact/file management (`:276-292`), orchestration (`:106-273`), and target selection heuristics (`:662-759`) — five change reasons in one file.
- **Mysterious Name** `TIMEFRAME_RANK`/`ASSET_CLASS_RANK`/`SYMBOL_RANK` (`:56-82`) encode "preferred default dataset" ordering but the name says only "rank"; a comment or `PREFERRED_*` prefix would help.
- **Duplicated Code** `_normalize_markdown_doc`/`_extract_python_response` (`:456-490`) vs `strategy_ir/normalizer.py` helpers — the repo has two parallel STRATEGY_META normalizers (`auto_agent._normalize_strategy_meta` vs `strategy_ir.normalizer.normalize_strategy_meta_payload`) with subtly different tag coercion rules (`tags` string → `{"type": [...]}` here vs `{"labels": [...]}` there). Drift bug waiting.

---

## 4. `runner/engines/signal_replay_adapter.py` (819 lines)

### Bugs / correctness
- **major** `signal_replay_adapter.py:346-349` — protective-exit priority: within a bar where both SL and TP hit, **stop wins unconditionally**, again ignoring any `same_bar_order` setting (inconsistent with native engine's configurable behavior; same inconsistency noted in vectorbt intrabar path).
- **major** `signal_replay_adapter.py:348,351` — `exit_price = float(position.stop_loss or float(bar["close"]))` — `or` treats a legitimate stop price of `0.0` as falsy; unreachable for sane prices but the idiom is wrong; same for take_profit.
- **major** `signal_replay_adapter.py:573-585` — closed trades sorted by `(last_exit_time, entry_time, symbol)`, then equity curve built from running sum of PnL — but `realized_pnl` during the replay loop (used for account_balance/exposure sizing, `:501`) accumulated in *signal order*, not exit order; final equity curve ordering differs from sizing-time equity. Minor distortion inherent to the design; worth documenting.
- **major** `signal_replay_adapter.py:451-546` — one position per symbol (`dict[str, _OpenPosition]`); a second signal for the same symbol while flat opens a new position, but a signal for symbol B never checks total-portfolio exposure beyond `current_positions` count passed into the risk engine — OK — yet `account_balance` uses `initial_capital + realized_pnl` ignoring unrealized drawdown of open positions; sizing drifts optimistic during drawdown.
- **minor** `signal_replay_adapter.py:374` — `context = frame[frame["timestamp"] <= signal.timestamp]` — full-frame boolean scan per signal (O(signals × bars)); use searchsorted.
- **minor** `signal_replay_adapter.py:386-389` — `except ValueError: return None` swallows processor ValueErrors silently — the signal disappears with no log; debugging signal drops is needlessly hard.
- **minor** `signal_replay_adapter.py:651` — `_infer_replay_timeframe` returns `"1m"` for ANY signal whose normalized timeframe is in the intraday set — meaning an hourly signal stream forces 1-minute data download (potentially huge); heuristic is aggressive and undocumented.
- **minor** `signal_replay_adapter.py:703-710` — start/end filters applied AFTER merge/dedup; correct, but `end` is inclusive raw compare while other modules expand end-of-day — inconsistent boundary handling across the runner (see vectorbt `_parse_date_bound`).

### Security
- Nothing notable; yfinance auto-download (`:715-732`) performs network I/O implicitly during backtests — flagging as operational surprise rather than vulnerability (**minor**: `auto_download=True` default can silently hit network in offline CI).

### Smells
- **Middle Man** `SignalReplayEngine` (`:627-819`) mostly delegates to `SignalReplayAdapter` after loading data — justified as façade (adds download+resolution inference), borderline.
- **Duplicated Code** — `_apply_trailing_stop` synthetic-signal construction (`:214-226`) and risk-signal construction (`:503-520`) mirror the TradeEngine/risk-engine call shape in `engine.py:328-347`; `_position_risk_amount` 2%/98% default-stop magic mirrors `engine.py:278,287` and `_finalize_position` `:517`.
- **Data Clump** — `_OpenPosition` (`:108-129`) carries `stop_loss/initial_stop_loss`, `take_profit/initial_take_profit`, `risk_amount/risk_percent` pairs; a nested `RiskContext` would clarify which fields mutate.

---

## 5. `runner/optimizer.py` (797 lines)

### Bugs / correctness
- **major** `optimizer.py:247-284` — objective returns `0.0` penalty for no-trade trials; with `direction="maximize"`, a strategy whose genuine metrics are negative Sharpe makes "no trades" (0.0) *better* than honest negative trials — Optuna will steer toward degenerate parameter regions that avoid trading. Penalty should be strongly negative or trial-pruned.
- **major** `optimizer.py:301` — `study.best_trial` raises ValueError if ALL trials failed/pruned; unlike `portfolio_optimizer` (`:444-453` guards), no guard here — one bad window crashes the whole optimization after N trials of compute.
- **major** `optimizer.py:386-419` — `_split_backtest_period` splits on wall-clock proportion of the ISO strings; `train_period["end"] == split_ts` and test starts 1µs later, but engines filter data by date boundaries that snap to day granularity in some paths (`_parse_date_bound` expands bare dates) — train/test leakage of up to one bar/day depending on caller formatting. Not fatal but undermines OOS claims.
- **major** `optimizer.py:216-243` — pre-flight requires OHLCV bars for EVERY required symbol in BOTH segments, raising for the whole optimization if one symbol lacks bars in the tiny test segment — brittle all-or-nothing validation.
- **minor** `optimizer.py:175` — timeframe taken as `timeframes[0]` silently ignores additional timeframes of the base backtest.
- **minor** `optimizer.py:634-654` — `_asset_returns_frame_from_base_result` derives per-symbol "returns" from *trade-entry-time* PnL cumsum — these are not asset returns (they're strategy-per-symbol equity ticks); covariance on them is methodologically dubious as a fallback before trying market data. Ordering is inverted: market data should be primary.
- **minor** `optimizer.py:679` — same unsorted `rglob(...)[0]` arbitrary-parquet-selection issue as vectorbt loader.
- **minor** `optimizer.py:398` — `split_ts = start_ts + ((end_ts - start_ts) * train_split_pct)` — Timedelta × float is fine in pandas, OK.

### Security
- Nothing notable.

### Smells
- **Duplicated Code** `optimizer.py:422-434` `_parse_period_boundary` ≡ `walkforward._parse_boundary` (`:26-36`) ≡ `vectorbt_adapter._parse_date_bound` (`:1050-1062`) — three copies of the same end-of-day expansion rule.
- **Long Function / Divergent Change** `run_optimization` (`:159-363`) handles config resolution, data validation, Optuna wiring, payload persistence; `_run_risk_parity_covariance_optimization` (`:527-631`) repeats the payload-shape boilerplate nearly verbatim (payload dicts differ in a handful of keys) — extract a shared result-builder.
- **Primitive Obsession** — `train_period/test_period` as `dict[str,str]` clumps passed through many signatures; a `Period` dataclass would prevent key-typo bugs.
- **Repeated Switches** — `TARGET_METRIC_ALIASES` (`:38-53`) is fine (shared map, endorsed), but `_suggest_parameter` (`:437-458`) is an if-chain over `spec["type"]` that belongs beside the validator `_validate_parameter_ranges` (`:115-144`) — spec schema knowledge spread across two functions.

---

## 6. `runner/engines/pynecore_adapter.py` (735 lines)

### Bugs / correctness
- **critical** `pynecore_adapter.py:137-139` — PyneCore failures are swallowed: `logger.error(...); continue`. A systematically broken `pyne` install yields a BacktestResult with zero trades and success status — the worst failure mode for a trading tool (silently empty results get persisted to SQLite and ranked by optimizers). Should raise after all symbols fail (compare vectorbt's `RuntimeError` on symbol errors, `vectorbt_adapter.py:390-394`).
- **major** `pynecore_adapter.py:99-106` — if the `pyne` binary isn't found: `logger.error(...); continue` — same silent-empty-result problem for every symbol.
- **major** `pynecore_adapter.py:129-135` — `subprocess.run(..., timeout=300)` good, BUT `subprocess.TimeoutExpired` is not caught — one slow symbol aborts the entire multi-symbol run with a raw traceback instead of skipping/logging.
- **major** `pynecore_adapter.py:162-165` — multi-symbol equity curve sums per-symbol PnL sequences each compounded from full initial capital (same double-count defect as vectorbt adapter §1).
- **major** `pynecore_adapter.py:604-648` — `_apply_risk_sizing` retroactively rewrites `trade.quantity`/`pnl`/`exits[-1].quantity/.pnl` AFTER the fact based on a recomputed equity path — but `trade.pnl` was parsed from PyneCore's own accounting (`Profit USD`); mixing the engine's fills with homegrown sizing produces internally inconsistent trades (e.g., percentage stats from CSV no longer match rewritten quantities). Also `equity += pnl` uses the NEW pnl but skips equity update when sl missing (`:616`) — mixed accounting.
- **minor** `pynecore_adapter.py:91-94` — `df[[...]].to_csv(csv_path)` — timestamp written as ISO string; PyneCore round-trip precision depends on its parser; no assertion the CSV row count matches df.
- **minor** `pynecore_adapter.py:151` — `all_stats = _parse_stats_csv(stats_csv)` — overwritten per symbol; only the LAST symbol's stats survive into the shared result.
- **minor** `pynecore_adapter.py:550-572` — `_normalize_trade_exits` mutates exit prices to match SL/TP meta ("if exit price crossed the level, pretend it filled at the level") — rewrites historical fills toward theoretical levels; optimistic-fill bias (assumes level was tradable through).
- **minor** `pynecore_adapter.py:88` — strategy copied into tmpdir loses sibling `_context.py` helper discovery? No — helpers load from original path (`:235-240`), but the EXECUTED copy at `strategy_copy_path` is the one PyneCore runs; if the strategy imports its own sibling modules, they're not copied → ImportError inside PyneCore (swallowed by `:137` continue!). Compounding failure-hiding.
- **minor** `pynecore_adapter.py:431` — nearest-bar tolerance `<= pd.Timedelta(seconds=1)` for matching trade timestamps to bars; daily data with exchange-local timestamps may exceed tolerance → context enrichment silently skipped.

### Security
- **major** `pynecore_adapter.py:251-273` — `_compile_helper_namespace` `exec()`s arbitrary extracted functions from the strategy file (and its `_context.py` sibling). By design strategies are code, but this exec path bypasses even the "run through PyneCore" isolation story — helper functions execute in-process with full privileges. Combined with auto_agent writing LLM code to disk, this is a second unguarded exec surface.

### Smells
- **Duplicated Code** — `_serialize_meta_value` (`:436-462`) ≡ vectorbt's `:1252-1282`; `_coerce_series` (`:298-317`) ≡ vectorbt's `:518-537`; `_infer_asset_class` (`:538-547`) ≡ vectorbt's `_infer_asset_class_from_path` (`:1203-1211`); `_load_ohlcv` (`:512-535`) ≡ vectorbt's `:1023-1047`. Whole-file duplication tax between the two adapters.
- **Feature Envy** — `_apply_risk_sizing` envies Trade internals (mutates five fields across two objects); the sizing math belongs beside the vectorbt adapter's equivalent (`vectorbt_adapter.py:201-261`) in one shared sizer.
- **Long Function** `PyneCoreAdapter.run` (`:52-201`) — data loop + subprocess orchestration + parsing + equity/meta building.

---

## 7. `runner/backtest_grid.py` (679 lines)

### Bugs / correctness
- **major** `backtest_grid.py:211-222` — `_detect_date_range` fallback returns hardcoded `("2023-01-01", "2025-12-31")` when no parquet found; the subsequent run then fails confusingly (data download may fix it, but if the symbol simply doesn't exist, users get a 2023–2025 backtest of nothing). Silent fabricated range.
- **major** `backtest_grid.py:277-292` and `:425-440` — the skip-existing "rehydrate" block is duplicated verbatim in `run_one_backtest_in_process` and `run_one_backtest` (Duplicated Code smell, but also a bug vector: the subprocess variant rehydrates BEFORE resolving whether the existing output matches current parameters — resume ignores changed `--params`).
- **major** `backtest_grid.py:606-613` — ThreadPoolExecutor with `max_workers>1` runs `run_one_backtest` in-process by default; each cell calls `ensure_backtest_data` concurrently → concurrent downloads/parquet writes to the same cache directories; races on shared filesystem state (checklist: race conditions on shared state).
- **minor** `backtest_grid.py:36` — GRID_SYMBOLS includes instrument names (`SP500`, `NAS100`, `OIL`) that are not yfinance tickers; default grid likely fails cells out of the box.
- **minor** `backtest_grid.py:190-208` — local `get_engine` duplicates `runner.cli.get_engine` minus signal-replay and error text; two engine registries drift (cli raises listing available engines; grid doesn't).
- **minor** `backtest_grid.py:341-342` — `engine_adapter.run(config, strategy)` passes `strategy` (path string) — fine — but `config.strategy_name=Path(strategy).stem` while cli uses `args.name or stem`; naming inconsistency between grid outputs and manual runs affects registry joins.

### Security
- **minor** `backtest_grid.py:466` — `result["cmd"] = " ".join(cmd)` records full command including `--params` JSON into summary JSON — params may embed secrets users paste into CLI; low risk, worth noting.

### Smells
- **Duplicated Code** — skip-existing blocks (`:277-292` vs `:425-440`), result-dict initialization (`:266-275` vs `:414-423`) — the two variants of `run_one_backtest` share ~80% shape; extract common wrapper with a pluggable executor.
- **Long Parameter List** — `build_cli_command` (`:127-146`, 15 params), `run_one_backtest_in_process` (`:243-262`, 16 params), `run_backtest_grid` (`:523-545`, 19 params). A `GridCellRequest` dataclass is overdue (Data Clumps).
- **Shotgun Surgery** — engine factory duplicated here and in cli; timeframe alias tables here vs `shared.constants.normalize_timeframe`.

---

## 8. `runner/stats.py` (563 lines)

### Bugs / correctness
- **major** `stats.py:145` — `total_return = float((1 + returns_series).prod() - 1)` compounds *daily-resampled* returns — correct — but at `:152-153` the manual fallback computes `total_return` from equity endpoints; the two definitions disagree when the equity curve has gaps (ffill days have 0 return; endpoint method unaffected). Metric definition silently depends on quantstats availability.
- **major** `stats.py:113-117` — `daily_returns = _daily_returns_from_equity_curve(equity_curve)` is computed ONLY under `if equity_curve and len(equity_curve) > 1:` — but `cagr` uses trade timestamps (`:320-333`) while sharpe uses daily resampling; for curves where all points share one timestamp (single-day backtests), `resample("D").last().ffill()` yields 1 row → returns None → sharpe stays 0.0 while p_value comes from `_one_sided_p_value([])` = 1.0. Consistent zeros, fine — but PSR needs n≥10 (`:523`) and returns None → rounded into `psr=None` — OK.
- **major** `stats.py:95` — no-trades branch: `profit_factor = 0.0` but winners branch sets inf→None (`:173`); consumers distinguishing 0.0 vs None must handle both conventions.
- **minor** `stats.py:216` — `ts = e.time if hasattr(e, "time") else e.timestamp ...` — EquityPoint always has `.time` (verified in shared/models.py:436); dead defensive branch.
- **minor** `stats.py:242-249` — `resample("D").last().ffill()` — ffill extends the LAST known equity forward to today? No — resample bounded by index range, OK; but `.ffill()` after `.last()` fills weekends/holidays with prior equity creating zero-return days that dilute annualized metrics (defensible, but changes Sharpe vs. trading-day convention; DAILY_RETURN_PERIODS_PER_YEAR=252 with calendar-day observations mismatches).
- **minor** `stats.py:487-494` — `_sharpe_manual` divides variance by `len(excess)` (population) then annualizes √252 while inputs are *daily* only if the fallback path was reached with truly daily data — but the fallback feeds `_daily_returns(equities)` which are PERIOD returns of arbitrary cadence (`:463-465`); annualizing bar-level returns of hourly curves at √252 understates volatility scaling. The quantstats path resamples properly; the fallback doesn't (`_infer_periods_per_year` is computed at `:154-155` for Calmar but NOT used for sharpe/sortino).
- **minor** `stats.py:514-516` — `_sharpe_unused` parameters accepted and ignored in both PSR helpers — Mysterious Name + dead param (kept presumably for signature compatibility with removed callers).
- **minor** `stats.py:336-367` — `_resolve_backtest_period_bounds` falls back to `pd.Timestamp.now(tz="UTC")` twice → CAGR 0 via `days<=0` guard; OK but "now()" inside a stats function is a hidden nondeterminism.
- **minor** `stats.py:31-37` — module-level try-import of quantstats toggling algorithm paths (HAS_QUANTSTATS) — environment-dependent numerics; see major above.

### Security
- Nothing notable.

### Smells
- **Duplicated Code** — `_probabilistic_sharpe` vs `_probabilistic_sharpe_manual` (`:514-554`) implement the same math twice (series vs list) instead of converting once.
- **Long Function** `compute_stats` (`:40-189`) — 150-line metric assembler with interleaved branches; the HAS_QUANTSTATS if/else at `:120-166` duplicates the MaxDD/Calmar tail.
- **Speculative Generality** — `_attach_inferred_freq` (`:260-278`) is defined but never called anywhere in the package (grep confirms no call sites in runner/) — delete or wire it.

---

## 9. `runner/backtest_matrix.py` (507 lines)

### Bugs / correctness
- **major** `backtest_matrix.py:147-150` — writes `grid_summary.json` into the per-strategy output dir, but `run_backtest_grid` ALREADY wrote the identical file (`backtest_grid.py:624-625`) — redundant double write racing with the inner function under `strategy_workers>1`? Not racing (different dirs), just redundant.
- **major** `backtest_matrix.py:72-95` — `_load_daily_rows` reads `payload["stats"]["daily_returns"]` — populated only by `attach_daily_equity`; rows keyed by `row.get("return", "")` — matches producer. OK. But `_field` formats floats with `%.10g` while leaving ints/strings raw — CSV consumers get mixed-type columns for equity (string "" vs number).
- **minor** `backtest_matrix.py:242` — `best_row = max(ok_rows, key=lambda row: _safe_float(row.get("sharpe")) or float("-inf"), default=None)` — `or` maps sharpe==0.0 (a valid value) to `-inf`; a strategy whose best cell genuinely has Sharpe 0.0 is deprioritized versus negative-Sharpe cells equally. Use explicit None check.
- **minor** `backtest_matrix.py:287` — `strategies_dir: Path = Path("strategies")` — CWD-relative default; matrix behaves differently depending on launch directory (cli passes args.strategies_dir, also relative). Fragile default for a data-pipeline entrypoint.
- **minor** `backtest_matrix.py:300-307` — constructs a probe `StrategyEntry` with `meta={}` and `documentation_path=None` purely for matching — fine, but `_strategy_matches` compares `display_name` built from empty meta; identifier matching on display name silently differs pre/post meta load.

### Security
- Nothing notable.

### Smells
- **Long Parameter List** — `_run_strategy_matrix_cell` keyword args (17) and `run_backtest_matrix` (21 params, `:264-288`); same Data Clumps remedy as grid.
- **Duplicated Code** — failed-row dicts appended in two exception handlers (`:378-387`, `:396-405`) identical shape.
- **Inappropriate Intimacy** — reaches into grid internals (`build_run_id`, `output_path_for`, `read via json`) instead of asking grid for structured results.

---

## 10. `runner/portfolio_optimizer.py` (506 lines)

### Bugs / correctness
- **major** `portfolio_optimizer.py:29-45` — `_parse_time_to_ts` parses ISO strings with `datetime.fromisoformat(time_str.replace("Z", "+00:00"))`; tz-naive strings (e.g. `"2024-01-01T00:00:00"`) interpret as LOCAL time via `.timestamp()` — timezone-dependent alignment across machines (checklist: tz-naive datetimes). All producers in this repo emit `+00:00` ISO so it works today, but any naive input silently shifts epochs.
- **major** `portfolio_optimizer.py:244` — `returns = np.diff(portfolio_eq) / (portfolio_eq[:-1] + 1e-10)` — epsilon in denominator distorts returns when equity near/below zero (negative equity flips signs); then `sharpe = mean/std` is a RAW (non-annualized, non-risk-adjusted-by-rf) ratio labeled `sharpe` — inconsistent with `stats.py`'s annualized, rf-adjusted definition; IS/OOS and cross-module comparisons mislead.
- **major** `portfolio_optimizer.py:274-279` — objective maximizes `sharpe - 5·corr_penalty - 3·dd_excess` but `trial.report(sharpe, trial.number)` reports the UNpenalized sharpe for pruning — pruning decisions contradict the returned objective; with the default median pruner absent it's moot, but the hook invites future pruners to prune good-weight candidates.
- **major** `portfolio_optimizer.py:86-92` — train window capped at test_start, but points BETWEEN train_end and test_start are dropped ("gap", `:106`) — deliberate, yet when `train_months` is None the train set extends TO test_start (no embargo) while weights are evaluated on data contiguous with test — standard leakage concern, documented choice at best.
- **minor** `portfolio_optimizer.py:157` — `require_overlap=False` branch: `prior_times = [t for t in equity_values if t <= sorted_times[0]]` — if a curve begins AFTER the window start (window_start = max of mins, so shouldn't happen) `prior_times` could be empty → `max([])` ValueError. Guarded by window construction today.
- **minor** `portfolio_optimizer.py:369` — `OPTIMIZATIONS_DIR` defined at MODULE BOTTOM, after functions that reference it (`_build_optimization_result` at `:336` references it at call time — works, but definition-after-use is a readability trap).
- **minor** `portfolio_optimizer.py:385-392` — `target_sharpe` parameter accepted and never used (docstring claims "for constraint"). Dead parameter.

### Security
- **minor (positive note)** `portfolio_optimizer.py:372-382` — `_persist_optimization_result` validates output path stays inside OPTIMIZATIONS_DIR — good containment, rare in this area; similar hardening is absent in `walkforward._write_payload` and `lean_adapter._write_config`.

### Smells
- **Speculative Generality** — `StrategyEquityPoint.time: int` typed as Unix seconds but every caller feeds ISO strings converted at the boundary; the int-typed model forces `_parse_time_to_ts` gymnastics (Primitive Obsession inverse: the model should carry the ISO string or a Timestamp).
- **Mysterious Name** — `require_overlap` toggles two fundamentally different alignment algorithms (`:171-213`); boolean-flag polymorphism (Fowler's Flag Arguments).
- **Duplicated Code** — correlation/off-diagonal math here overlaps `risk_parity_optimizer.avg_abs_corr` (`risk_parity_optimizer.py` score fn) with different denominators.

---

## 11. `runner/cli.py` (474 lines)

### Bugs / correctness
- **major** `cli.py:116` — `_detect_date_range` falls back to `("2023-01-01","2025-12-31")` — fabricated range (as in grid); combined with `ensure_backtest_data` this triggers downloads for a made-up window.
- **major** `cli.py:107` — `ts = pd.to_datetime(df["timestamp"])` WITHOUT `utc=True` — tz-aware parquet timestamps parse fine, but tz-naive ones stay naive; `.strftime` output then reflects naive wall time while engines compare UTC — off-by-timezone date ranges possible depending on data provenance (checklist: tz-naive datetimes).
- **major** `cli.py:185-188` — on `ValidationError` the CLI logs and `sys.exit(1)` — fine for CLI, but `cmd_run` is ALSO imported/called programmatically by tests/tools; `sys.exit` in library-ish code is harsh (minor-major judgement).
- **minor** `cli.py:204-209` — `engine.run(config, args.strategy)` vs `engine.run(config, args.strategy, progress_callback=...)` — branches only because some adapters historically lacked the kwarg; all current adapters accept it (base ABC defines it) — simplify.
- **minor** `cli.py:137-139` — `json.loads(args.params)` unguarded: malformed JSON raises raw JSONDecodeError traceback instead of argparse-style error.
- **minor** `cli.py:85-116` — reads only `matches[0]` parquet per symbol (unsorted rglob, same arbitrary-selection issue).

### Security
- **minor** `initialize_sentry("runner")` at import (`:39`) — side-effectful telemetry init at module import; importing `runner.cli` in tests/offline environments hits network config. Operational, minor.

### Smells
- **Duplicated Code** — `get_engine` (`:56-75`) vs `backtest_grid.get_engine` (`:190-208`); `_emit_progress` (`:45-53`) duplicated in optimizer (`:757-764`) and native_adapter's local `emit_progress`.
- **Shotgun Surgery** — adding an engine requires touching: cli.get_engine, backtest_grid.get_engine, help text (`:350`, `:242-247`), and the engine list in cmd_engines. Registry pattern (llm_providers does this correctly, `llm_providers/__init__.py:21-31`) should be adopted.

---

## 12. `runner/llm_providers/copilot_sdk_provider.py` (369 lines)

### Bugs / correctness
- **major** `copilot_sdk_provider.py:160-173` — bridge thread runs `asyncio.run(asyncio.wait_for(coro, timeout))`; if the outer `done.wait(timeout+1.0)` expires, the function raises TimeoutError but the daemon thread keeps running the coroutine with NO cancellation — leaked event loop/client session continues consuming resources and may later mutate `result`/`error` dicts harmlessly, but SDK subprocesses spawned by CopilotClient outlive the request. Resource leak on timeout path.
- **minor** `copilot_sdk_provider.py:169` — `timeout_seconds + 1.0` grace race: coroutine timeout fires at T, thread teardown at T..T+1; borderline benign.
- **minor** `copilot_sdk_provider.py:310-312` — `except TypeError:` around `send_and_wait(prompt, timeout=...)` retries WITHOUT timeout — older SDK signatures lose the timeout entirely (unbounded wait inside an already-bounded asyncio.wait_for — actually outer wait_for still bounds it; net effect OK but misleading).
- **minor** `copilot_sdk_provider.py:34-42` — recursive `_extract_content` walks dicts/lists with no cycle protection — a cyclic payload (SDK objects with back-references converted to dicts?) would RecursionError; unlikely but cheap to guard.
- **minor** `copilot_sdk_provider.py:295-299` — case-insensitive membership check but returns `resolved_model` (original casing) — consistent; OK.

### Security
- **minor** `copilot_sdk_provider.py:301-308` — `PermissionHandler.approve_all` grants every tool permission the SDK session requests (file edits, shell) for an LLM call that only needs text generation. Least-privilege violation: a prompt-injected model response could trigger approved tools. Prefer a deny/no-tools handler for pure completion calls.

### Smells
- **Long Method** `generate` (`:198-282`) — status/error bookkeeping woven through six except blocks; a small state machine or context manager would compress it.
- **Duplicated Code** — `_compose_prompt` here (`:139-148`) ≡ inline composition in copilot_cli_provider (`:52-57`); `_apply_metadata` triplicated across all three providers with slightly different key sets.

---

## 13. `runner/walkforward.py` (365 lines)

### Bugs / correctness
- **major** `walkforward.py:133` — `result.stats = {**compute_stats_for_result(result), **result.stats}` — computed stats are OVERWRITTEN by whatever the engine left in `result.stats` (which for vectorbt is empty, for pynecore is PyneCore's CSV stats). Every other caller (`backtest_grid.py:348`, `cli.py:216`, `optimizer._canonical_stats`) does the opposite merge order. Fold IS/OOS sharpes therefore come from heterogeneous sources depending on engine — direct comparability bug.
- **major** `walkforward.py:130-132` — `attach_daily_equity(result, start_date=start, end_date=end)` — WRONG KEYWORD NAMES: the function signature is `attach_daily_equity(result, start=None, end=None)` (`daily_equity.py:71-73`). Calling with `start_date=`/`end_date=` raises `TypeError: unexpected keyword argument` the first time a fold result lacks an equity curve. Verified against daily_equity.py — this is a live crash on the no-equity-curve path.
- **major** `walkforward.py:306` — `"oos_max_drawdown": _metric(test_stats, "max_drawdown_pct")` — stats store max_drawdown_pct NEGATIVE (`stats.py:143` negates); `_summary_from_folds` abs()es it (`:144-147`) — OK — but fold payloads expose the signed value while summary exposes magnitude; consumers comparing them see sign confusion.
- **minor** `walkforward.py:177-182` — portfolio mode ignores `step_months` (accepted in config echo but unused — optimize_portfolio_allocation has no stepping); config payload pretends it applied.
- **minor** `walkforward.py:252-254` — lazy `from runner.cli import get_engine` inside function — circular-import workaround duplicating the engine factory a THIRD time.

### Security
- Nothing notable.

### Smells
- **Duplicated Code** — `_parse_boundary` ≡ optimizer's `_parse_period_boundary` ≡ vectorbt's `_parse_date_bound` (third copy); `_float_or_none` ≡ `_safe_float_or_none` in optimizer ≡ `_safe_float` in backtest_matrix.
- **Data Clumps** — the 14-kwarg `_run_fold_backtest` signature (`:96-112`) mirrors BacktestConfig field-for-field; construct the config at the caller.

---

## 14. `runner/strategy_ir/models.py` (344 lines)

### Bugs / correctness
- Nothing functional found. Validators are thorough (unique states, bounds, transitions).
- **minor** `models.py:ParameterValueType.CHOICE` + `ParameterSpec.choices` — `_validate_bounds` enforces choices non-empty for CHOICE but doesn't validate `default ∈ choices` — a default outside the declared options passes validation and surprises codegen.

### Security
- Nothing notable.

### Smells
- Clean overall; the enum-heavy typed IR is the best-designed file in the area. Minor: **Speculative Generality** — `StateMachine`/`StateTransition`, `RawExpressionLanguage.CSHARP/PSEUDO`, `DrawingKind.BOX/HISTOGRAM/SCATTER` have zero consumers in the repo today (codegen supports none of them) — kept presumably for LLM-schema forward-compat, which is a defensible repo-standard exception; flagged as judgement only.

---

## 15. `runner/strategy_ir/codegen_vectorbt.py` (322 lines)

### Bugs / correctness
- **critical** `codegen_vectorbt.py:81-105` — `_rule_from_text` translates natural-language condition text into comparisons by keyword sniffing: "overbought"/"tp" → `(signal > 70)`, "oversold"/"stop"/"risk-off" → `(signal < 30)`. A condition mentioning "stop" in an ENTRY context generates `(rsi < 30)` entries. Worse, the generic branch maps ANY condition containing ">" or "above" to `(primary > secondary)` where primary/secondary are merely the first two alphabetically-sorted indicators — generated logic routinely has no relation to the documented strategy. Deterministic ≠ correct: silently generating wrong trading logic for an automated money-adjacent pipeline is the highest-severity issue in strategy_ir. At minimum, unmatched/unmappable conditions must fail loudly (raise) rather than fall through to `default_rule`.
- **major** `codegen_vectorbt.py:63-69` — `_indicator_defaults` picks the first positive NUMERIC param as "window": an RSI with params `{lower: 30, upper: 70}` yields window=30 → EMA span 30 or rolling mean 30 — indicator type inferred from substring (`is_rsi` → RSI with Wilder smoothing via `ewm(alpha=1/window)` — reasonable) but the window semantic is guesswork; thresholds are discarded entirely.
- **major** `codegen_vectorbt.py:110-124` — primary/secondary chosen by alphabetical indicator order; entry rules reference `indicator_{slug}` names built with `_safe_identifier` de-dup suffixes, but `STRATEGY_META.parameters` exposes RAW param names (e.g. `period`) while generated code reads `params.get('window_key')` where window_key is the FIRST sorted numeric param's NAME (e.g. `'length'`) — overrides only apply if user guesses the right key; other params silently ignored.
- **minor** `codegen_vectorbt.py:generated drawings` — markers emitted for first 25 entry/exit ROWS regardless of whether they begin a simulated trade (signal≠fill after next-open shift); viewer drawings then disagree with trades.
- **minor** `codegen_vectorbt.py:_calc_rsi` — `avg_loss.replace(0, pd.NA)` then `.fillna(50.0)`: RSI undefined when avg_loss==0 (pure uptrend) collapses to neutral 50 instead of 100 — mildly wrong edge behavior.

### Security
- Generated code is written to disk and executed by the adapters — same trust-chain caveat as auto_agent (§3 security). Codegen itself injects only IR-derived strings into f-compiled templates; `indicator.name` flows into identifiers via `_safe_identifier` (sanitized) and into `indicators_dict[{label!r}]` via repr — injection-safe.

### Smells
- **Mysterious Name** — `_rule_from_text(primary_signal=..., secondary_signal=...)` — "primary/secondary" hide that these are positional indicator picks.
- **Repeated Switches** — `is_ema`/`is_rsi` substring dispatch (`:141-160` template assembly); a registry of indicator emitters would extend cleanly.

---

## 16. `runner/risk_parity_optimizer.py` (252 lines)

### Bugs / correctness
- **minor** `risk_parity_optimizer.py:41-44` — `returns_from_equity_matrix` divides by `np.maximum(equity_matrix[:-1], 1e-12)` — negative equity floors to 1e-12 → gigantic spurious positive returns; nan_to_num cleans inf but sign-distorted finite values pass through.
- **minor** `risk_parity_optimizer.py:110-125` — SLSQP failure falls back to inverse-vol weights WITHOUT renormalizing to bounds — inv-vol weights can exceed `max_weight` clip then renormalize (`:126-130` clips then normalizes, fine) — actually clip+normalize happens for both paths; OK.
- **minor** `risk_parity_optimizer.py:158` — `np.corrcoef(returns, rowvar=False)` with constant series → NaN row → nan_to_num zeroes correlations → diversification_ratio inflated for dead assets; acceptable given shrinkage.
- **minor** score weights (`0.35`, `-0.5`, `-2.0`, `:176`) are magic constants with no rationale comment — tuning invisible.

### Security
- Nothing notable.

### Smells
- Well-scoped module. **Primitive Obsession**: selection returned as plain dict (`select_best_risk_parity_universe` return) despite `RiskParitySelection` dataclass existing — the dataclass dissolves into a dict at the boundary, losing typing for consumers (`optimizer.py:559-566` indexes by string key).

---

## 17. `runner/converters/lean_converter.py` (206 lines)

### Bugs / correctness
- **critical** `lean_converter.py:64-67` — pairing assumes strict Entry→Next-order-exit alternation per ticker: `is_entry = "Entry" in tag or tag in ("LONG","SHORT")`; partial fills, scale-ins, or LEAN tags not containing those tokens cause the loop to either skip entries (orphaned exits ignored at `:78-79`) or pair an exit with the WRONG entry (`orders[i+1]` blindly). Multi-lot sequences produce fabricated trades/pnl. No validation that exit follows entry logically (exit time ≥ entry time is implied but quantity match unchecked — `qty = abs(entry["quantity"])` ignores the EXIT order's quantity, `:97`).
- **major** `lean_converter.py:84-93` — pnl lookup keyed on exact timestamp string equality of the exit order time vs profitLoss keys; any serialization drift (ms truncation) silently falls back to price-diff pnl IGNORING fees/leverage/multipliers — understated error rates.
- **major** `lean_converter.py:131-140` — `_build_meta` fabricates `initial_capital=100000.0` and empty start/end regardless of the LEAN data — downstream stats (returns, CAGR) computed off a fictional capital base.
- **minor** `lean_converter.py:59` — `all_orders.sort(key=lambda x: (x["time"], x["id"] or 0))` — mixed str/int id types would raise; LEAN ids numeric-ok.
- **minor** `lean_converter.py:174-181` — `datetime.fromtimestamp(ts)` for chart x-values assumes seconds; LEAN charts often use milliseconds → equity timestamps in 1970; no magnitude sanity check.

### Security
- **minor** `json.loads(path.read_text())` on untrusted converter input — fine locally; no size guard (a huge LEAN export loads fully into memory).

### Smells
- **Long Function** `_build_trades` (`:44-123`) — flattening, grouping, pairing, pricing, tagging in one body.
- **Primitive Obsession** — orders as ad-hoc dicts (`all_orders.append({...})`).

---

## 18. `runner/engines/native_adapter.py` (194 lines)

### Bugs / correctness
- **major** `native_adapter.py` (equity rebuild block, ~lines 150-160) — same multi-symbol equity double-count as vectorbt/pynecore: per-symbol TradeEngines each compound from full initial_capital, then `running = config.initial_capital; running += trade.pnl` across all symbols — portfolio equity overstated for multi-symbol runs.
- **minor** `native_adapter.py:_load_intrabar_data` — `selected in {"", "main", "main_timeframe", "open_close", base_timeframe.lower()}` omits aliases accepted by `ExecutionModelSettings.from_config` (e.g. "1-lower" resolves to a concrete tf in vectorbt but is rejected here as not-a-timeframe via `_is_finer` ValueError-guard → returns None silently). Native silently degrades to open_close where vectorbt would use 1-lower replay — cross-engine parity gap.
- **minor** `native_adapter.py:duration_days` — bare `except Exception: duration_days = 0` — broad except swallowing even KeyboardInterrupt-adjacent errors? No, Exception excludes those; still overly broad (checklist: bare except style).
- **minor** — symbols with no data are silently skipped (`continue`) with no aggregate warning; empty universe → empty result success.

### Security
- Strategy module exec — same by-design caveat as vectorbt.

### Smells
- **Duplicated Code** — TIMEFRAME_ORDER, `_load_strategy_module`, `_load_ohlcv` — third/fourth copies.
- **Middle Man** — NativeAdapter thin over TradeEngine (justified: adds multi-symbol orchestration).

---

## 19. `runner/llm_providers/copilot_cli_provider.py` (192 lines)

### Bugs / correctness
- **minor** `copilot_cli_provider.py:47-49` — command built with `-p composed_prompt` BEFORE checking executable exists — fine (FileNotFoundError handled at `:61`).
- **minor** `copilot_cli_provider.py:96-99` — JSON decode failure silently falls back to raw stdout as content: if the CLI prints a JSON error object with exit code 0, the raw JSON becomes the "LLM response" and flows into strategy-code extraction — garbage-in propagation. Should fail loudly on undecodable JSON.
- **minor** — no handling of `stderr` warnings on success path (ignored) — fine.
- Timeout handled (`subprocess.run(timeout=...)`) ✓ — this provider does the checklist right, unlike auto_agent.

### Security
- **minor** — prompt passed as argv (`-p`, composed_prompt): very long prompts exceed OS arg limits (E2BIG) on Linux (~2MB) for large strategy sources; also prompt content visible in `ps` output on multi-user hosts — minor confidentiality note.

### Smells
- **Duplicated Code** — `_apply_metadata` (third copy), `_extract_content` (near-copy of sdk provider's, fewer keys), system-prompt composition (duplicate).

---

## 20. `runner/converters/stockkit_converter.py` (190 lines)

### Bugs / correctness
- **major** `stockkit_converter.py:118-133` — TP1 partial-exit split invents `tp1_pnl` from prices and assigns `runner_pnl` as residual-by-price, but `trade.pnl` remains the CSV `total_pnl`; sum(exit pnls) ≠ trade.pnl whenever slippage/fees differ per leg — internal consistency break that corrupts per-exit analytics while totals look right.
- **minor** `stockkit_converter.py:124` — `TradeExit(time="", ...)` for TP1 leg — empty-string timestamps violate the ISO-8601 invariant asserted elsewhere (`shared/models.py:337` comment; stats parsers `pd.to_datetime("" )` → NaT filtered); daily-equity builder skips these legs (`daily_equity.py:47-56` loops exits, last non-empty wins — actually picks the LAST exit with truthy time, so runner leg — OK by luck).
- **minor** `stockkit_converter.py:186-192` — `_parse_datetime` failure returns raw `f"{date}T{time}"` unparsed — invalid ISO propagates.
- **minor** `stockkit_converter.py:69-71` — `backtest_end=trades[-1].entry_time[:10]` — trades NOT sorted; "last row" ≠ latest date; start/end meta wrong for unordered exports.
- **minor** — EXPECTED_HEADERS defined but never validated against the actual CSV header — a renamed column surfaces as KeyError-skipped rows (logged per-row) and possibly an EMPTY trade list with success status.

### Security
- Nothing notable (local CSV ingestion).

### Smells
- **Duplicated Code** — meta-building boilerplate duplicated across all three converters/adapters.
- **Data Clump** — 20-column row unpacked into 20 locals per iteration.

---

## 21. `runner/grid_tester.py` (146 lines)

### Bugs / correctness
- **critical** `grid_tester.py:87` — `result = engine.run(config)` — `EngineAdapter.run` requires `(config, strategy_path)` (base ABC `engines/base.py:31-38`; every concrete adapter too). Missing positional argument → `TypeError` caught by the broad `except Exception` at `:101` and recorded as a per-cell "error" — i.e., **every cell of every grid run through this module fails**, and the module reports a completed matrix of uniform errors. Dead/broken feature that still writes "success-shaped" artifacts to SQLite.
- **major** `grid_tester.py:101` — broad `except Exception` per cell converts programming errors (like the above TypeError) into data — masking the crash indefinitely.
- **minor** `grid_tester.py:83-85` — `stats.get("sharpe", 0)` — stats keys are `sharpe_ratio` per `stats.py`/BacktestStats.to_dict; grid matrix would read zeros even if run worked (key-name drift; backtest_grid.read_backtest_stats knows both spellings, this file doesn't).
- **minor** `grid_tester.py:60` — `find_strategy_entry(strategy_slug)` result `entry` unused beyond existence check — strategy PATH never passed to engine (part of bug #1).

### Security
- Nothing notable.

### Smells
- **Speculative Generality / dead feature** — superseded by backtest_grid (docstring even says results feed portfolio optimizer, which now consumes matrix CSVs instead). Candidate for deletion or repair; current state is worst-of-both.

---

## 22. `runner/daily_equity.py` (126 lines)

### Bugs / correctness
- **major** `daily_equity.py:71-76` — `attach_daily_equity(result, start=None, end=None)` — the `start`/`end` parameters are NEVER USED in the function body. Five call sites (backtest_grid ×2, cli ×1, walkforward ×1 [with wrong names]) pass date filters expecting them to matter; they are silently ignored. Dead parameters that mislead callers (and caused walkforward's TypeError).
- **minor** `daily_equity.py:33-41` — picks the LAST exit with truthy `time` as the trade's day; for multi-leg exits the day attributed is the final leg's — defensible, undocumented.
- **minor** `daily_equity.py:62-66` — `prev_equity = running_equity - equity_by_day[day]; if prev_equity > 0` — negative prior equity → return forced 0.0 silently (division guard) — acceptable but hides blowups.
- **minor** `daily_equity.py:96-104` — "preserve existing equity_curve if longer than daily" — a bar-level curve with 2 points beats a 500-day daily curve? Condition `len(result.equity_curve) > len(daily_points)` — yes, a 3-point intraday curve suppresses a 200-point daily rebuild. Threshold semantics questionable.

### Security
- Nothing notable.

### Smells
- **Speculative Generality** — the unused start/end params (above).
- **Long Conditional** — attach_daily_equity's four early-return branches repeat the same `meta.data/stats` mutation triple; extract `_set_frequency_metadata`.

---

## 23. `runner/capm.py` (71 lines)

### Bugs / correctness
- **minor** `capm.py:52-56` — alpha annualized by ×252 (simple scaling of the daily intercept) — standard quick-and-dirty; geometric ((1+a)^252−1) would differ materially for large alphas; document choice.
- **minor** `capm.py:31-33` — DatetimeIndex localized/converted to UTC, but non-DatetimeIndex goes through `pd.to_datetime(index, utc=True, errors="coerce")` — NaT indices then dropped by the `~normalized.index.isna()` filter ✓. Solid little module.
- No other findings.

### Security
- Nothing notable.

### Smells
- Scanned clean — well-factored, validated inputs, clear errors. Exemplary for this area.

---

## 24. `runner/converters/cli.py` (111 lines)

### Bugs / correctness
- **minor** `converters/cli.py:41-51` — the `if engine == "lean" / elif engine == "stockkit" / else` dispatch calls both converters identically (`converter["convert"](input_path, strategy_name=name)`) — the branch chain is redundant (Repeated Switches with identical arms); the else-arm is dead.
- **minor** `converters/cli.py:54-56` — `result.meta.data.get("symbols")` — converters set symbols already; harmless redundancy.
- **minor** — no handling of conversion exceptions: malformed input raises raw traceback (acceptable for dev CLI).

### Security
- Nothing notable.

### Smells
- **Repeated Switches** — the identical-arm if/elif above; a registry with uniform signatures removes it.

---

## 25. `runner/llm_providers/nim_provider.py` (111 lines)

### Bugs / correctness
- **minor** `nim_provider.py:16-22` — OpenAI import fallback defines a stub class that raises on instantiation — good error message deferred to construction ✓.
- **minor** `nim_provider.py:60-62` — raises ValueError("NIM provider requires a model.") AFTER applying status="running" metadata — finally-free design relies on except-block overwrite ✓ (handled).
- **minor** — no retry/backoff on transient 429/5xx from NIM — auto_agent's retry loop covers it coarsely (regenerates whole step).
- **minor** — `response.choices[0].message.content` unguarded against empty choices array (IndexError → caught by generic except → re-raised with metadata ✓).

### Security
- **minor** `nim_provider.py:31` — api_key resolved from env and held on instance; never logged ✓. Base URL overridable via constructor only — no SSRF surface from user input in-repo.

### Smells
- **Duplicated Code** — `_apply_metadata` (second/third copy across providers).

---

## 26. `runner/engines/base.py` (134 lines)

### Bugs / correctness
- **minor** `base.py:52-56` — `model_config = ConfigDict(frozen=False)` — mutable config dataclass passed into threads/processes (vectorbt pickles it to workers ✓ fine; but grid ThreadPoolExecutor shares ONE config object across cells? No — grid constructs per cell ✓).
- **minor** `base.py:64-69` — `modeling_resolution` validator accepts any VALID_TIMEFRAMES value including COARSER-than-base timeframes (e.g. base 1h, resolution 1d) — engines then disagree (vectorbt rejects finer-check, native `_is_finer` rejects, ExecutionModelSettings accepts anything not equal to main/open_close/base) → silent divergence.
- **minor** `base.py:84-93` — `resolve_strategy_parameters` swallows ALL exceptions from meta load (`except Exception: meta={}`) — a syntactically broken STRATEGY_META silently yields defaults-only params; overrides still apply, masking authoring errors.

### Security
- Nothing notable.

### Smells
- **Duplicated Code** — VALID_RESOLUTIONS tuple partially duplicates VALID_TIMEFRAMES + engine-local tables.
- Scanned otherwise clean; good use of pydantic constraints.

---

## 27. `runner/engines/lean_adapter.py` (136 lines)

### Bugs / correctness
- **critical** `lean_adapter.py:26-31` — `LEAN_PROJECT_DIR = Path(os.environ.get("LEAN_PROJECT_DIR", ...))` but `os` is NEVER imported (imports are json/logging/subprocess/tempfile/datetime/pathlib — verified by grep). Importing this module raises `NameError: name 'os' is not defined` at line 26 → `runner.cli --engine lean` and `backtest_grid.get_engine("lean")` crash at import time. The lean engine is entirely broken.
- **major** `lean_adapter.py:18` — `tempfile` imported, never used (dead import; hints at removed docker-run scaffolding).
- **major** `lean_adapter.py:_run_lean` — invokes `[sys.executable?]` NO — plain `"python"` (not `sys.executable`), assuming a `lean` pip package importable as module `lean` with `-m lean backtest` accepting a config JSON + `--output` — speculative invocation that almost certainly doesn't match the real lean CLI (which is `lean backtest` without `-m`, project-scoped). Combined with the NameError above, the "run LEAN" path has likely never executed successfully.
- **minor** `lean_adapter.py:timezone` imported unused; `datetime.now()` (naive, local tz) used for output dir naming (`:87`) — checklist tz-naive datetime.
- **minor** `lean_adapter.py:_find_result` — globs `*.json` then `**/*.json` (superset duplicate work) and picks first parseable file containing "orders"/"trades" — arbitrary selection among candidates.
- **minor** — `progress_callback` accepted and ignored.

### Security
- **minor** `lean_adapter.py:_write_config` — writes `backtest_config.json` into a FIXED path in `$HOME/Desktop/custom_lean_integration` — symlink/jacking on shared hosts aside, concurrent runs clobber each other's config (race on shared file, checklist item).

### Smells
- **Speculative Generality** — Phase-1 stub kept in the engine registry though non-functional; either fix or remove from registries/help.
- **Dead Code** — unused imports (`tempfile`, `timezone`).

---

## 28. `runner/strategy_ir/builder.py` (250 lines)

### Bugs / correctness
- **major** `builder.py:170-178` — `_infer_exit_kind`: `"tp" in lowered` matches words CONTAINING "tp" (e.g. "ftp", "https"); `"time" in lowered` similarly loose; kinds feed downstream semantics — mild but real misclassification vector.
- **minor** `builder.py:120-127` — `_coerce_scalar` treats any text containing "." as float candidate: "1.2.3" → ValueError → returns text ✓; but version-like "1.5" in a *description* column would coerce — table parser is position-based (col0=indicator, col1=param, col2=value), extra prose columns ignored — OK.
- **minor** `builder.py:196-206` — confidence = 0.25 × count(non-empty sections) — a fake-confidence heuristic consumed nowhere yet (routing.py doesn't read `confidence`); placeholder.
- **minor** `builder.py:214-218` — `source_summary` = first markdown line — often the H1 title, duplicated with `name`.

### Security
- Nothing notable (pure parsing).

### Smells
- **Duplicated Code** — `_coerce_scalar` duplicated verbatim in `strategy_ir/normalizer.py:_coerce_scalar` (`:17-33`); `_extract_markdown_title` duplicated between normalizer and auto_agent.
- **Primitive Obsession** — markdown-table parsing by regex/line-splitting; a tiny table model would tame `_indicator_rows_from_markdown`.

---

## 29. `runner/strategy_ir/normalizer.py` (218 lines)

### Bugs / correctness
- **minor** `normalizer.py:36-52` — `_parse_mapping_string` tries `json.loads` catching `(ValueError, SyntaxError, TypeError, json.JSONDecodeError)` — `json.JSONDecodeError` subclasses ValueError (redundant member; harmless).
- **minor** `normalizer.py:96-116` — list-form parameters: entries with `"name"` take `default`/`value`/`initial`/`start` — but if NONE present, `parameter_value = item.get("default")` = None silently — parameter materializes with None default, later crashing `int(None)` paths in codegen `_resolve_window`? Codegen guards with try/except → default used ✓. Still: silent None.
- **minor** — tag coercion here yields `{"labels": [...]}` for string tags while `auto_agent._normalize_meta_tags` yields `{"type": [...]}` — SAME input, DIFFERENT schemas depending on which normalizer ran (cross-module inconsistency, see §3 smells).
- No other functional findings; coercion coverage is genuinely broad.

### Security
- `ast.literal_eval` used (not eval) ✓ safe.

### Smells
- **Divergent Change (latent)** — this file exists as "the better" meta-normalizer while auto_agent still carries its own; until one is deleted, every schema tweak is shotgun surgery across both.
- Scanned clean otherwise.

---

## 30. `runner/strategy_ir/routing.py` (96 lines)

### Bugs / correctness
- **minor** `routing.py:11-27` — `_normalize_rollout_mode` accepts a wide alias set; unknown values silently fall back to `ir-first` (default) rather than erroring — a typo'd rollout mode quietly changes codegen strategy. Prefer strict validation for an ops toggle.
- **minor** `routing.py:74-79` — `first_llm_attempt=2` when IR generator selected encodes "skip LLM attempt 1" as an integer protocol understood only by the (absent-from-runner) consumer — magic-number coupling; no in-repo consumer validates it.
- No other findings; small, clear seam module.

### Security
- Nothing notable.

### Smells
- **Speculative Generality (light)** — `generator_id`, `allow_legacy_fallback`, alias modes: more configuration surface than current consumers exercise. Defensible as a rollout seam; flagged as judgement.

---

## 31. `runner/llm_providers/__init__.py` (37 lines)

Scanned. **No findings.** Clean registry factory with explicit supported-names tuple and helpful error. The model other factories in the area (engines) should follow.

## 32. `runner/llm_providers/base.py` (16 lines)

Scanned. **No findings.** Protocol with keyword-only signature; appropriate.

## 33. `runner/strategy_ir/__init__.py` (52 lines)

Scanned. **No findings.** Re-export barrel; `__all__` consistent with imports.

## 34. `runner/__init__.py` (0 lines)

Scanned. Empty package marker. **No findings.**

## 35. `runner/engines/__init__.py` (0 lines)

Scanned. Empty package marker. **No findings.**
Note: an engine registry here (like llm_providers') would eliminate the duplicated `get_engine` factories (cli, backtest_grid, walkforward).

## 36. `runner/converters/__init__.py` (0 lines)

Scanned. Empty package marker. **No findings.**

---

# Cross-cutting themes (summary)

1. **Silent failure dominance (most dangerous theme).** Engines convert hard failures into empty-but-successful results: pynecore `continue` on binary-missing/child-failure, grid_tester catching its own TypeError per-cell, converters emitting zero-trade successes, `_detect_date_range` fabricating date ranges. For a trading platform, "ran fine, zero trades" must be distinguishable from "broken."
2. **Multi-symbol portfolio accounting is fictitious.** vectorbt/native/pynecore each compound per-symbol equity from full capital and then sum — reported equity/PnL for multi-symbol runs is structurally overstated.
3. **Cross-engine execution-model inconsistency.** `same_bar_order` is configurable in native but hardcoded stop-first in vectorbt intrabar and signal-replay; intrabar resolution support diverges (native rejects "1-lower", vectorbt honors it).
4. **Copy-paste stratification.** OHCV loaders ×4, date-boundary parsers ×3, float-coercers ×4, `_apply_metadata` ×3, `_serialize_meta_value`/`_coerce_series` ×2, `get_engine` ×3, STRATEGY_META normalizers ×2, `_to_iso` ×2, `_detect_date_range` ×2(+variants). The single highest-leverage refactor in the area is a `runner/data_access.py` + shared meta-normalizer.
5. **Exec trust chain.** LLM-generated code is written to `strategies/` and executed both via subprocess (auto_agent) and in-process `exec` (pynecore helpers) with `PermissionHandler.approve_all`; no screening layer.
6. **Checklist sweep (Python):** mutable default args — none found (dataclasses use `default_factory` consistently ✓); bare except — none literally bare, but several overbroad `except Exception` (auto_agent catalog, base.resolve_strategy_parameters, grid_tester, stats helpers) flagged; race conditions on shared state — grid ThreadPool concurrent downloads, vectorbt warning-set, lean fixed config path; SQL string building — none (SQLite access behind shared.database ✓); subprocess without timeout — auto_agent `_run_backtest` (violates), pynecore TimeoutExpired uncaught, cli/grid/lean/copilot providers ✓ compliant; tz-naive datetimes — cli._detect_date_range, portfolio_optimizer ISO parse, lean_adapter output dir naming; resource leaks — copilot_sdk timeout-path thread/loop leak.

# Finding counts

Severity totals across all per-file findings:

- critical: 5
- major: 62
- minor: 89

Per-area breakdown of criticals: lean_adapter missing `os` import (1), grid_tester `engine.run(config)` missing argument (1), engine.py `pd.concat` row-merge semantics (1), pynecore silent swallow of engine failures (1), codegen_vectorbt keyword-sniffed rule fabrication (1).
