# Code Review — `data-validation` area — Reviewer r1

Repo: `/home/ubuntu/Desktop/trading-platform` — area: `data_tools/` + `validation/`
Reviewer: r1 (blind, independent). Baseline: Fowler, *Refactoring* ch.3. Two rules: the repo overrides (a documented repo standard suppresses a smell it endorses) and every smell finding is a judgement call, not a hard violation. Findings cite `file:line` with severity.

**Status: COMPLETE.** All 50 tracked code files in the area were read in full and are accounted for below (findings, or "scanned; nothing found").

---

## Group 1: downloader core + Dukascopy family

### data_tools/downloaders/base.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `base.py:207-213` — `_read_latest_timestamp` scans parquet row groups **backwards and returns the first row-group max it finds**. That is only correct if row-group statistics are ordered, which is not guaranteed after a merge that sorts by timestamp... actually here rows are sorted before write (`base.py:170-180`) so in practice the last row group holds the max — but if any writer ever stores unsorted data (e.g. a hand-edited or externally produced parquet), resume silently starts from a mid-history timestamp and **re-downloads nothing while believing the range complete**. Silent wrong-range risk on a trading data path.
- **minor** `base.py:215-223` — fallback reads only the *last row group's last value*; for unsorted input this is not the latest timestamp at all.
- **minor** `base.py:151-184` — `_merge_and_store_single_file` does read-modify-write of the parquet with no lock/temp-file+rename. Concurrent downloads to the same symbol/timeframe can interleave and lose rows (last writer wins, whole file). Race condition on shared state.
- **minor** `base.py:233-239` — `_normalize_timestamps` uses `errors="coerce"` then silently drops NaT rows: corrupt timestamps are discarded without warning/log, hiding upstream data corruption.

**Smells**

- Primitive Obsession (minor): `DownloadResult.start/end` are ISO strings (`base.py:35-36`); timestamps as strings flow through catalog/meta code. A `TimestampRange` value type would prevent format drift.

### data_tools/downloaders/dukascopy_native.py (scanned; findings below)

**Bugs / correctness risks**

- **critical** `dukascopy_native.py:790-791` — in `_download_and_assemble_merged_ticks`, `bid_urls = generate_tick_urls(...)` then `ask_urls = generate_tick_urls(...)`: both sides generate **identical bid-file URLs** (tick URLs have no side component), so "ask" download re-downloads the same bid files; the inner merge at `:839-842` on identical frames yields ask columns equal to bid. Merged tick output is silently mislabeled/wrong.
- **major** `dukascopy_native.py:1016-1028` vs `:774-786` — call site passes positional args `(instrument, start_dt, end_dt, batch_size, pause_ms, max_retries)` but the function signature is `(instrument, start_dt, end_dt, batch_size, pipeline_batches, pause_ms, max_retries, ...)` → **pause_ms lands in pipeline_batches and max_retries lands in pause_ms** (e.g. defaults: pipeline_batches=3, pause_ms=900). Wrong throttling parameters applied whenever `merge_tick`/`merge_s1` is used.
- **major** `dukascopy_native.py:983-984` — `datetime.fromisoformat(requested_start).replace(tzinfo=timezone.utc)` raises `ValueError` for full ISO timestamps like `"2024-01-01T13:45:00+00:00"` (naive-replace onto aware datetime). The sibling JForex downloader handles this via `_parse_datetime`; native path breaks on non-date-only inputs.
- **major** `dukascopy_native.py:636` — `_assemble_candle_dataframe` drops all zero-volume candles (`df[df["volume"] > 0]`). For FX pairs/indices with genuinely flat minutes this deletes legitimate bars, creating gaps that propagate into resampled h1/d1 bars (wrong OHLC windows).
- **minor** `dukascopy_native.py:385-397` — invalid-bi5 retry treats any weekday decompress failure as throttling (`throttle_signal=True`), conflating corrupt payload with rate-limit; weekend failures return empty without cooldown even if the server was actually limiting.
- **minor** `dukascopy_native.py:1062-1070` — when `normalized_tf` is neither `tick` nor `s1`, `_ticks_to_ohlcv(df, normalized_tf, ...)` is reached only if strategy=="tick", which per `DOWNLOAD_STRATEGY` (:188-199) can't happen — dead branch, but confusingly written as reachable (`else:` at :1069).
- **minor** `dukascopy_native.py:1193-1194` — `str(df["timestamp"].iloc[0])` guarded by `"timestamp" in df.columns and len(df) > 0`, fine — but `out_path.stat().st_size` at :1195 will raise if store failed earlier; acceptable since store raises first.

**Security**

- minor `dukascopy_native.py:869-871` — `_write_source_meta_index` writes JSON without atomic rename; concurrent downloads can truncate/corrupt `source_meta.json` (also lost-update race).

**Smells**

- Long Function / duplicated lambdas (minor): progress-scaling lambda `(lambda pct, msg: _report_progress(report_progress, pct * 0.45, f"Bid {msg}"))` appears at `:799-803`, `:818-822`, `:1084-1088`, `:1103-1111`, `:1144-1148`. Extract `make_progress_scaler(report_progress, offset, scale, prefix)`.
- Data Clumps (minor): `batch_size, pipeline_batches, pause_ms, max_retries, report_progress, stop_event, pause_event, log_callback` repeated across `download_bi5_files`, `_download_minute_side_dataframe`, `_download_and_assemble_merged_ticks`, `_download_async` — bundle into a `FetchOptions` dataclass.
- Duplicated Code (minor): `_normalize_offer_side` (`:640-644`) is byte-identical to the one in `jforex_dl.py:960-964`.

### data_tools/downloaders/jforex_dl.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `jforex_dl.py:551-552` — inside the JVM candidate loop, `except _java_exception_types() as exc: _raise_jforex_runtime_error(...)` aborts on the **first** candidate's Java exception instead of trying the next candidate, defeating the whole candidate list; a single broken candidate (e.g. stale JAVA_HOME jvm.dll) makes startup fail entirely.
- **major** `jforex_dl.py:1179-1193` — in single-side mode `result_df` is assigned in the else-branch; but if `offer_side == "both"` path taken, `result_df` from `:1148` merge is used and `combined = result_df` at `:1183` counts rows of the merged frame while `size_bytes` sums both files — consistent. Real issue: **`result_df` may be referenced before assignment** is not possible, but at `:1148` the merge of `bid_result_df.merge(ask_result_df)` happens AFTER each side was already stored separately — fine. Actual bug: at `:1183-1189`, for offer_side="both" `output_path = out_path.parent` (a directory) is returned in `DownloadResult.path` where the contract (base.py:33) is "where the parquet was saved". Consumers expecting a file will break on `.stat()`/open.
- **minor** `jforex_dl.py:873-874` — `state.completed.wait(timeout_seconds)` with timeout up to 900 s default: if strategy thread dies without setting completed (JVM crash), caller waits full timeout; no early abort on disconnect (`onDisconnect` at :741-742 deliberately ignores).
- **minor** `jforex_dl.py:293-299` — jar download writes directly to final path (no temp+rename): interrupted download leaves truncated jar that passes the `exists() and st_size != 0` check next run → corrupted classpath permanently.
- **minor** `jforex_dl.py:171-173` — date-only end gets `-1ms` end-of-day adjustment, good; but datetime inputs pass through unchanged, so `end="2024-01-02 00:00:00"` excludes the entire last day — inconsistent boundary semantics between equivalent inputs.

**Security**

- **major** `jforex_dl.py:245-247, 295-297` — `urlopen(jnlp_url/jar_url, timeout=20)` fetches and executes (loads into JVM) code from a URL whose host comes from env var/config; jar contents are never integrity-checked (no checksum/signature verification) before being loaded into the JVM process. Supply-chain exposure if `DUKASCOPY_JNLP_URL` points anywhere attacker-controlled. Mitigations exist (trusted-home checks at :408-437 apply to JVM binaries only, not jars).
- minor `jforex_dl.py:183-194` — credentials read from env into dataclass; password could leak via diagnostics? No — diagnostics don't include auth. OK. But `_format_jvm_startup_diagnostics` prints full classpath paths (:505-529) — information disclosure of local layout in logs; minor.

**Smells**

- Shotgun Surgery / Duplicated Code (major smell, minor severity): `_normalize_offer_side`, `_bars_with_offer_prefix`/`_prefix_ohlcv_columns`, `_resample_offer_prefixed`/`_resample_side_prefixed_ohlcv` duplicate `dukascopy_native.py` nearly line-for-line; the two Dukascopy front-ends should share one module.
- Message Chain (minor): `jpype_mod.JClass("java.lang.Thread").currentThread().getContextClassLoader()` chain at :584-588 — wrap in helper.

### data_tools/catalog_builder.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `catalog_builder.py:26-29` — `logging.basicConfig(...)` executes at **module import time**, before the module's own logger exists; importing this module from an application reconfigures the root logger and hijacks app logging configuration (import side effect). Should be inside `main()`.
- **major** `catalog_builder.py:352-393` — `read_parquet_summary` takes `start` = row-group 0 min and `end` = last row-group max. If any row-group statistics are missing (`:379-389` fallback reads first value of group 0 / last value of last group), or if a file is not sorted by timestamp (possible for externally produced parquet), catalog start/end are wrong silently. Combined with `_merge_catalog_dates` string comparisons (`:396-404`, ISO date strings compared lexicographically) this is tolerable only because format is normalized to date ISO; but entries cached from older catalogs with full timestamps compare incorrectly (`"2024-1..." vs "2024-01..."` is guarded by `_format_catalog_date` on write, yet cache-reuse path `:757-770` copies raw cached strings without re-normalizing).
- **minor** `catalog_builder.py:100` — `_detect_storage` compares `str(parquet_path).startswith(str(cache_dir))` — plain string prefix match: `/data/cache-evil/x.parquet` matches prefix `/data/cache`. Use `os.path.commonpath` / `Path.is_relative_to`.
- **minor** `catalog_builder.py:161-167` — `_extract_timeframe_and_segment`: for non-tick segmented names, `separator_index = len(timeframe)` assumes the stem begins with the *normalized* timeframe; for stems like `"5m_eurusd"` normalize→`"5m"`? Actually `normalize_timeframe("5m")` returns canonical `"5m"` per shared/constants mapping m5→5m, so length matches; but for input `"m5_x"` the normalized timeframe `"5m"` has same length — coincidental. For aliases of different length than the raw token (e.g. `"sec_x"` → `"1s"`), the slice cuts at the wrong offset, returning garbage segment.
- **minor** `catalog_builder.py:844` — `scan_fundamentals` loads each fundamentals parquet **fully into memory** just to count rows/columns; use parquet metadata like `read_parquet_summary` does. Hot spot for large files.
- **minor** `catalog_builder.py:890-891` — `total_size_bytes` sums `e.get("size_bytes", 0)` where grouped entries always set it, but fundamentals error entries set size before read — OK; however grouped OHLCV `size_bytes` counts duplicate paths when a file appears both as entry path and in `files` list (`:488-497`) → double counting possible when cache merges segment entries that share the top-level path.

**Security**

- minor `catalog_builder.py:915` — `Path(args.output).write_text(out)`: no atomic write; a crash mid-write corrupts the catalog that other components cache-read (`_load_catalog_cache`). Also follows symlinks blindly — acceptable for CLI tool but worth noting given DATA_DIR is user-writable.

**Smells**

- Long Function (major smell): `_group_ohlcv_entries` spans `:439-722` (~280 lines) with a 20-key aggregate dict inline (`:449-479`). Extract aggregate class + per-concern merge helpers.
- Duplicated Code (minor): `_select_timestamp_column` (`:331-336`) is byte-identical to `downloaders/base.py:225-231`; the "timestamp|datetime|date|time" candidate walk should live once in shared code.
- Primitive Obsession (minor): entries are untyped dicts with ~25 ad-hoc keys mutated across 300 lines; a `CatalogEntry` model would catch key typos statically.

### validation/importers/quantconnect_result.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `quantconnect_result.py:695` — `points.sort(key=lambda point: tuple(point))` sorts equity points as tuples where the first element is int timestamp — fine — but `_submission_clusters` groups orders purely by identical submission-time string (`:655-657`): two orders one second apart form separate clusters while simultaneous-but-different-resolution timestamps merge; cluster summary feeds rebalance analysis downstream, so clustering granularity is fragile (string-equality on rendered timestamps rather than time-window bucketing).
- **minor** `quantconnect_result.py:189-216` — `_canonical_time` parses naive `%m/%d/%Y` formats as UTC (`:212-213`); QC exports are UTC so acceptable, but ambiguous US-format dates (01/02/2024) are interpreted month-first — if any export locale differs, times shift silently. Also returns original text unparsed on failure (`:210-211`), which then participates in equality checks as an opaque string — two spellings of the same instant compare unequal.
- **minor** `quantconnect_result.py:849-853` — `reader.fieldnames` used after `with` block closes the file (`mapped_headers` built at `:858-859` from `reader.fieldnames` — attribute persists after close, OK), but `csv_rows` materializes entire CSV; large exports load fully into memory (acceptable scale).
- **minor** `quantconnect_result.py:903-914` — `_verify_csv` matching is O(n²): for each CSV row it linearly scans all remaining JSON rows. With thousands of orders/trades this is slow; multiset comparison is claimed (`csv_verification.comparison = "normalized_multiset"` at :382) but implemented as greedy first-match — a CSV row can match a JSON row that another CSV row needed (order-dependent greedy assignment), potentially rejecting valid exports or accepting mismatched pairings that happen to satisfy counts.
- **minor** `quantconnect_result.py:793` — derived fee `max(minimum_fee, abs(quantity) * rate_per_unit)` applies minimum fee per order even when quantity is zero (cancelled/unfilled orders still get min fee); reconciliation then hard-fails at `:803-809` for legit portfolios where unreferenced orders were never filled. Correctness risk: importer raises instead of classifying as unresolved.

**Security**

- Nothing found beyond standard JSON parsing (uses safe `json.loads` on bytes; no eval/pickle).

**Smells**

- Long Function (major smell): `_fee_reconciliation` `:704-839` (~135 lines) with three near-identical return payloads (`:744-764`, `:766-786`, `:810-839`) — extract a result builder.
- Speculative Generality (minor): `_ORDER_DIRECTIONS` includes `-1: "Hold"`, `_TRADE_DIRECTIONS` maps `0/1`; direction `-1` accepted for orders but trades reject anything else — fine, but `_int` coercion of `raw.get("direction", -1)` default at :470 encodes QC internals without comment.
- Duplicated Code (minor): `_decimal`/`_float`/`_int` validators repeat bool-guard + finite-guard pattern three times; could share one numeric coercion helper.

### validation/reference.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `reference.py:51` — `datetime.utcnow` is deprecated (Python ≥3.12) and produces **tz-naive** datetimes stored as `imported_at`; `to_backtest_result` (`:88-89`) then emits naive ISO strings into `BacktestMeta.created_at` while the rest of the platform stamps tz-aware UTC (cf. `dukascopy_native.py:1205`). Naive/aware mixing breaks sorting/comparison downstream. Checklist item: tz-naive datetime.
- minor `reference.py:88` — meta id embeds `strftime('%Y%m%d-%H%M%S')` of import time: two imports within the same second collide on id (no uuid despite the unused `uuid4` import at :12).

**Smells**

- Speculative Generality (minor): `from uuid import uuid4` imported but never used (`reference.py:12`) — dead import suggesting an abandoned uniqueness scheme.
- Data Clumps (minor): `symbol`/`timeframe` stored as comma-joined strings (`quantconnect_result.py:442` passes `",".join(symbols)`); the reference should carry lists.

### validation/reporting/json_report.py (scanned; findings below)

**Bugs / correctness risks**

- **critical** `json_report.py:297-298, 360-361` — `_build_trade_delta_report` and `generate_json_report` annotate `trade_comparison_config: TradeComparisonConfig` but the module **never imports `TradeComparisonConfig`** (imports at `:17-27` cover AnomalyDetector, MatchedTrade, TradeComparator, DeltaReportBuilder… but not it). With `from __future__ import annotations` (`:7`) evaluation is deferred so runtime works, but any runtime introspection — `typing.get_type_hints`, pydantic validation of callables, or `eval`'d annotations — raises `NameError`. More importantly this is an unresolved-name defect waiting to fire.
- **major** `json_report.py:137-163` — infinity handling only covers `+inf`: a stat of `-inf` (possible for e.g. profit_factor with zero gross loss) falls through to `_is_numeric` → `abs_diff = inf`, `rel_diff = inf`; then `passed = passed or (abs_diff <= abs_tol)` is False even when **both sides are `-inf` and thus identical** — a false mismatch. Asymmetric with the `+inf` branch above.
- **major** `json_report.py:180-183` — pass logic is `passed = passed or (...)`: a field passes if **either** absolute OR relative tolerance holds. For tiny denominators rel-tol explodes (rel diff 1.0 when denom==0) while for huge values abs-tol is trivially satisfied; the OR makes tolerance strictly looser than either threshold alone. If intent is "within combined tolerance" this silently passes fields that should fail (e.g. expected 0.0001 vs actual 9999 → rel_diff≈1.0 > rel_tol but abs check? no—abs_diff=9998.98 fails too). Real risk case: expected=1e6, actual=1e6+50 → abs 50 > 0.01 but rel ≤ 0.001 passes by design; acceptable — but combined with `_STATS_FIELDS` containing scale-free metrics like win_rate (abs_tol 0.01 = 1 percentage point on a 0–1 ratio), thresholds are inconsistent across fields.
- **minor** `json_report.py:402-417` — platform stats resolution: when `platform_bt.trades` is empty and `platform_raw_stats` falsy, falls back to `compute_trade_statistics(platform_bt.trades)` on an empty list — stats computed from zero trades compared against reference stats; produces guaranteed-mismatch report rather than flagging "no platform data".
- **minor** `json_report.py:443` — `passed=1 if trade_delta_report.passed else 0` stuffs a bool into an int summary field; same at :453. Cosmetic type confusion in report consumers.
- **minor** `json_report.py:515` — `_build_recommendation(data)` reads `data.get("risk_passed")` never — risk failures are not surfaced in recommendations (risk_passed checked only in overall passed).

**Security**

- Nothing found: JSON output sanitized via `_sanitize_for_json` (non-finite floats → None) — good practice (`:30-55`). Note `set` handling in sanitizer converts to list non-deterministically (`:48-49`) — report output ordering may vary run-to-run; minor.

**Smells**

- Duplicated Code (minor): `_rel_diff` (:87-91) duplicates inline denom logic at :170-174; `_compare_stats` re-implements instead of calling the helper.
- Long Function (minor): `generate_json_report` ~160 lines mixing stat computation, summaries, explanations; extract stat-delta builder.
- Feature Envy (minor): `_extract_risk_deltas` (:321-350) pokes at duck-typed `risk_delta_report.trade_results[].field_results` — move serialization onto RiskComparisonReport itself.

### validation/readiness/models.py (scanned; findings below)

**Bugs / correctness risks**

- **minor** `models.py:65-68` vs `:77-80` — `require_trade_report`, `require_regression`, `require_risk_report`, `require_stats` are each declared **twice** in `ReadinessConfig`. Pydantic keeps the last declaration; harmless today because values are identical, but editing one copy and not the other silently does nothing — maintenance trap.
- minor `models.py:70-74` — threshold field names say `_pct` but semantics are fractions (0.01 = 1%) while validator messages format them with `{:.4%}` consistently — OK internally, but naming invites callers passing `1` intending 1% (which becomes 100%). Mysterious Name risk.

**Smells**

- Nothing further: clean pydantic model file.

### validation/readiness/validator.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `validator.py:96-106` + `:86-94` — required-artifact absence produces **two overlapping sets of checks**: `_check_required_artifacts` emits `required_artifact_*` error checks AND the main body appends duplicate `required_<artifact>` error checks for the same condition. Each missing artifact double-counts errors; cosmetic but also indicates divergent duplication that will drift.
- **major** `validator.py:463-465` — `failed_results` filters `not r.passed and r.severity != "info"`; but if a risk result has severity "error"/"warning" yet `expected_trade`/`actual_trade` is None it's skipped at :470-471 **without recording any failure detail**, and position-size/stop-distance checks then compute max over an empty issue list → default 0.0 → both checks PASS despite failed risk results. A risk comparison that failed structurally (missing trade refs) reports "Max diff 0%" and readiness can go READY. Silent pass-through of failure evidence.
- minor `validator.py:244` — `all_deltas` counts errors across matched+missing+extra, but `max_error_count` semantic per message is "deviations in matched trades" (description at :269); missing/extra trades already have their own thresholds, so errors there are double-penalized.
- minor `validator.py:371-389` — warning-level mismatches force `READY_WITH_WARNINGS` forever (`passed=warning_mismatch_count == 0`); no config knob to waive reviewed warnings.

**Smells**

- Duplicated Code (minor): `_check_trades` builds four near-identical ReadinessCheck blocks (`:259-338`); a small factory taking (name, count, threshold, noun) would remove ~60 lines.
- Long Parameter List / Data Clumps (minor): `validate()` takes 5 optional artifacts + config; bundle into a `ValidationArtifacts` param object.
- Middle Man (minor): `_check_required_artifacts` duplicates logic the caller repeats at :87-94 — one of the two should go.

### validation/importers/mt5_xlsx.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `mt5_xlsx.py:459-560` — `_build_trades` pairs each "in" row with the next "out" row **positionally**; partial closes / multiple exits per position (MT5 emits several "out" rows for one "in") attach only the first exit and skip the rest, mispricing PnL. Also at :487-489 an unexpected second "in" silently abandons the current entry (`break`) and the outer loop advances via `i = j if j > i else i + 1`, potentially skipping trades without any warning.
- **major** `mt5_xlsx.py:509-511` — fees are taken only from the **exit** row's Kommission/Swap; MT5 books entry commission on the entry deal. Entry commissions are dropped → net_pnl overstated for every trade.
- **major** `mt5_xlsx.py:547` — `net_pnl=(pnl - fees) if (pnl is not None and fees is not None)`: `fees` is `0.0` when no commission columns exist (default=0.0), so net_pnl is computed — fine — but when `fees == 0.0` the `fees=fees if fees != 0.0 else None` at :546 sets `fees=None` while net_pnl was computed with fees=0. Inconsistent representation of "no fees" across fields; consumers checking `fees is None` vs 0 diverge.
- minor `mt5_xlsx.py:324-327` — `total_return` derivation requires `"initial_capital" in stats` but `_parse_stats` never writes that key (it lives in meta from `_parse_meta`); dead branch — total_return is never derived.
- minor `mt5_xlsx.py:417-418` — `row.get(col_sl, None)` on a pandas Series works, but if `_find_col` returned a column present in header yet absent from a given record dict... records come from the same df so keys always exist; OK. Real edge: duplicate column names after `_extract_table` (two "Preis" columns) collapse into one column losing data.
- minor `mt5_xlsx.py:583-599` — `_to_iso` returns raw unparsed text on failure ("tests will catch obvious issues" — comment admits it); downstream comparisons then mismatch silently rather than failing import.

**Security**

- Nothing found: reads local xlsx via pandas; no formula execution risk (openpyxl data-only default for values). No eval/exec.

**Smells**

- Duplicated Code (major smell): `_STAT_LABELS`, `_STAT_LABEL_MAP` construction (:24-69), `_parse_count_pct`, `_extract_first_percent`, `_extract_parenthesized_number`, `_coerce_stat`, `_infer_exit_reason`, `_coerce_setting` are duplicated nearly verbatim between this file and `mt5_html.py:24-68, 311-368, 573-582`. One shared MT5 parsing module should own them (Shotgun Surgery risk: label list changes must be made twice).
- Long Method (minor): `_build_trades` (~100 lines) mixes column discovery, pairing state machine, model construction.

### validation/importers/mt5_html.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `mt5_html.py:262-269` — `_parse_stats` iterates cells pairwise `for i in range(0, len(cells)-1, 2)` AND then re-applies pairs for exactly 4-column layouts at :267-269 — **double application**: for a 4-cell row, `(cells[0], cells[1])` and `(cells[2], cells[3])` are applied twice (once by the loop, once explicitly). Idempotent for plain values, but for `max_drawdown_*` handling it overwrites identically — harmless today but any non-idempotent stat breaks; more importantly the loop already covers 4-column layouts making :266-269 dead-but-executed code.
- **major** `mt5_html.py:398-399` — `_build_sltp_map` does bare `float(sl.replace(",", "."))`; an SL cell like `"1.2345 / 1.2350"` or thousands-separated values raise ValueError and abort the whole import (no try/except, unlike every other numeric helper here).
- **major** `mt5_html.py:457-462` — trade pairing keyed by `tid` string from the *Trade* column searched as `["Auftrag", "Trade"]` (:419): MT5 HTML reports use the same order number for multiple deals of a position; grouping purely by id collapses split fills into one pair where the LAST "in"/"out" row wins (`trade_rows[tid][direction] = row` at :451) — earlier rows silently discarded.
- minor `mt5_html.py:73-92` — indentation anomaly: entire docstring/body indented one extra level inside `load(` signature continuation — legal Python but a lint trap suggesting copy-paste; `kwargs.pop("features", ...)` mutates caller's kwargs (side effect visible to callers reusing the dict).
- minor `mt5_html.py:562` — `import zoneinfo` inside function while sibling module uses top-level `ZoneInfo`; also no `ZoneInfoNotFoundError` handling here (mt5_xlsx has it) → invalid tz name raises raw exception instead of ValueError.

**Security**

- minor `mt5_html.py:91-92` — parses untrusted HTML with BeautifulSoup/lxml: no script execution risk, but `lxml` parser on malformed input is C-accelerated and historically DoS-prone on entity bombs; reports are local files so exposure low. No `dangerouslySetInnerHTML` equivalent — output goes to typed models. OK.

**Smells**

- Duplicated Code (major smell): see mt5_xlsx entry — the two MT5 importers share ~200 lines of near-identical helpers; extract `validation/importers/mt5_common.py`.
- Inconsistent abstraction (minor): xlsx importer takes `tzinfo | str | None`, html only `str | None` — same concept, two signatures.

### validation/importers/base.py (scanned; nothing found)

Scanned fully. Small clean ABC; abstract `load` contract documented. No bugs/security issues/smells beyond the inherent Template-Pattern style which is appropriate here.

### validation/importers/__init__.py (scanned; findings below)

- **minor (bug)** `__init__.py:1-7` — package re-exports only 3 of 4 importers: `quantconnect_result.QuantConnectResultImporter` exists but is not exported here, unlike its siblings. Callers must deep-import; inconsistent public surface (likely oversight when QC importer was added).

### data_tools/dukascopy_discovery.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `dukascopy_discovery.py:150-159` — `_year_probe_date`/`_month_probe_date` avoid Saturday (`weekday() == 5`) by stepping back one day, but do **not** handle Sunday (`weekday() == 6`): FX markets closed Sunday → probe misses valid months whose month-end lands on Sunday only, binary search then misclassifies the month as dataless and returns a later genesis date. Also `candidate - timedelta(days=1)` can cross into the previous month/year (Dec 31 → Dec 30 fine, but Jan month probes stepping to prior year-end day probe wrong bucket).
- **major** `dukascopy_discovery.py:294-298` & `:341-350` — after binary search finds earliest candidate month/day, the linear "walk down" loops probe **every** lower index unconditionally without early exit and, worse, update `earliest` even if an intermediate probe is a false hit (probe noise flips result); combined with single-day probes at fixed 10:00 UTC (holiday half-days!), discovered genesis can be off by days. Probing holidays (no data) inside the walk resets nothing — logic assumes monotonic availability, violated by holidays.
- minor `dukascopy_discovery.py:141-143` — `asyncio.run(...)` per probe: each probe spins up and tears down a new event loop + aiohttp session (TCPConnector) — very slow for hundreds of probes; connection reuse abandoned.
- minor `dukascopy_discovery.py:145-147` — `except Exception:` around probe swallows all errors incl. KeyboardInterrupt? No — KeyboardInterrupt is BaseException, fine; but broad except hides DNS/auth failures as "miss", corrupting search results silently (only debug log).
- minor `dukascopy_discovery.py:536-537` — module-global `VERBOSE_LOGS` mutated from CLI main: not thread-safe, and library callers importing the module get print-based progress regardless of logging config (mixing `print` with logger).

**Smells**

- Duplicated Code (minor): `_find_start_month` and `_find_start_day` are structurally identical binary-search + fallback-walk implementations; parameterize by probe function.
- Feature Envy (minor): module reaches into `dukascopy_native` privates (`_resolve_symbol_and_instrument`, `download_bi5_files`) — these are de-facto public API; move to explicit exports.

### data_tools/auto_download.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `auto_download.py:103-108` — coverage re-check happens **after all plan steps run**, raising RuntimeError if still uncovered; but step failures inside `_download_missing_ohlcv_data` propagate first, aborting remaining sources in the plan (e.g. huggingface fails → alpaca step never runs despite being planned as fallback). Plan steps should be tried/continued individually.
- **major** `auto_download.py:377-381` — HF coverage window: if `hf_start > HF_INTRADAY_COVERAGE_END`, both bounds nulled and HF skipped — correct; but when only `end` is provided and end < ALPACA_INTRADAY_START (e.g. backtest ending 2024), Alpaca leg is skipped (`alpaca_end < START` fails :387) and HF leg gets `hf_start = normalized_start or normalized_end` = end date → single-day HF request for a range whose start was None; requirement.start was empty meaning "from beginning" — coverage check will then fail with RuntimeError instead of downloading full history.
- minor `auto_download.py:139-141, 275-277` — `except Exception` around parquet reads logs warning and treats file as missing coverage → triggers re-download of possibly huge files on transient read error; acceptable-ish but no retry/distinguish corruption vs lock contention.
- minor `auto_download.py:284-285` — coverage start/end computed by reading **entire timestamp column** of every matching parquet (`read_parquet(columns=["timestamp"])` loads all rows); catalog metadata (row-group stats, cf. base.py `_read_latest_timestamp`) would avoid full scans. Performance hot spot on big stores.
- minor `auto_download.py:308, 322` — `except Exception` around SYMBOL_CONFIG import lookups returns False silently — a broken install routes US stocks to yfinance without notice; narrow to ImportError.
- minor `auto_download.py:432-434` — fallback on `"Missing Alpaca credentials" not in str(exc)` string-matching exceptions is brittle (message refactor breaks fallback silently).

**Smells**

- Repeated Switches (minor): `_download_missing_ohlcv_data` is an if-chain over source names (`:409-488`) mapping to downloader constructors — replace with a registry dict {source: factory}; adding a source requires editing this chain (Shotgun Surgery with download_grid_market_data.py which likely has its own copy).
- Duplicated Code (minor): symbol-classification helpers (`_is_forex_symbol`, `_is_dukascopy_symbol`) rebuild SYMBOL_CONFIG sets on every call — cache them.

### validation/risk/comparison.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `comparison.py:191-192` — `_compute_profile` wraps sizing in bare `except Exception: return None`; a profile-less pair then yields severity "warning" and the readiness gate (`validator.py:463-465` filters on severity != info) may still pass overall. Sizing bugs (TypeError from bad meta types) are silenced rather than surfaced as errors — systematic risk-model failures look identical to "trade had no stop".
- **major** `comparison.py:338` + `:144-159` — report `passed = all(r.passed for r in results)`, but extra trades are constructed with `passed=True, severity="info"` (:153-154): platform-only trades never fail the risk comparison. Deliberate asymmetry vs missing trades (`passed=False`, warning) — defensible, but note extra trades also bypass position-size checks entirely, so a platform that doubles its trade count passes risk cleanly.
- minor `comparison.py:27-32` — third reimplementation of the max-denominator relative-diff helper in this area (cf. json_report.py:87, validator.py:23); all should share one util.
- minor `comparison.py:407` — composite `trade_id = f"{exp_id}_{act_id}"` uses raw ids; underscore join is ambiguous when ids contain underscores.

**Smells**

- Duplicated Code (minor): `_compare_numeric` mirrors FieldComparisonResult-building in delta_report; tolerance tuple `(pct, abs)` as positional pair — Primitive Obsession, give it a small Tolerance type.
- Feature Envy (minor): `RiskReportBuilder.to_delta_report` reaches into `DeltaReportBuilder._field_result_to_delta` (private of another class, `:400-403`) — leaking encapsulation across builders.

### validation/risk/adapter.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `adapter.py:172-202` — `size()` dispatch: if `model == PERCENT_RISK` handled, else falls through to FixedRisk for **any other enum value**; fine today with 2 models but adding one silently routes to fixed-risk (Replaced-enum-with-dispatch smell). More concretely: default `config=None` for FIXED path constructs `FixedRiskConfig(risk_amount=100.0)` — a hard-coded $100 risk assumption invisible at call sites that pass no config.
- minor `adapter.py:41` — direction mapping `"buy" if LONG else "sell"`: any unexpected direction string (e.g. lowercase "long") silently becomes "sell" instead of erroring.
- minor `adapter.py:131-137` — `calculate_trade_risk` mutates shared engine state? No — engine call looks pure; OK. But `signal.stop_loss` write-back contract ("engine fills fallback stop") depends on RiskEngine mutating the passed signal object — hidden side effect across module boundary (temporal coupling).

**Smells**

- Middle Man (minor): `RiskAdapter.trade_to_signal` just delegates to module-level `_trade_to_signal` — two names for one behavior.
- Data Clumps (minor): (entry_price, stop_loss, take_profit, direction, account_balance) travel together through adapter→model; already modeled by RiskSizingInput but adapter methods still take them piecemeal.

### validation/risk/models.py (scanned; findings below)

- **minor** `models.py:20-27` — `RiskModelConfig` defined but referenced nowhere in the package (checked imports across validation/) — Speculative Generality; delete or wire it in.
- minor `models.py:38` — `direction: str = "LONG"` free-text field where an Enum exists conceptually (Trade.direction elsewhere); Primitive Obsession.

### validation/risk/fixed_risk.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `fixed_risk.py:85-93` — position size after lot rounding can hit exactly 0.0 (floor to lot), then `if position_size < min_position_size: position_size = 0.0` keeps 0; downstream `risk_per_trade = 0 * stop_distance = 0`, risk_percent 0 → comparisons of two zero profiles pass trivially. A trade whose stop distance exceeds risk_amount/lot_size reports "risk matches" with zero exposure — masking data problems. At minimum should flag zero-sized results.
- minor `fixed_risk.py:61` — `validate()` mutates its input argument (`input.stop_loss = stop_loss`) — surprising side effect on a pydantic model passed by callers; document or return resolved values.
- minor `fixed_risk.py:92` — `< min_position_size` sets size to 0 rather than clamping up to min; semantics of "min" inverted (min usually means floor).

**Smells**

- Duplicated Code (major smell): `FixedRiskModel.size` and `PercentRiskModel.size` share ~80% (lot rounding, min/max clamp, RR ratio, profile assembly); extract base class or shared function with a risk-amount strategy hook.

### validation/risk/percent_risk.py (scanned; findings below)

Same zero-size clamp issue as fixed_risk.py:102-103 (`position_size < min_position_size → 0.0`). Additionally:
- minor `percent_risk.py:94-95` — `risk_amount = balance * risk_percent` then `position_size = risk_amount / stop_distance`: no notional/value cap; for tiny stop distances (0.00001) produces absurd leverage silently — only bounded by optional max_position_size which defaults None. Validation framework itself should sanity-bound this.
- minor `percent_risk.py:121-123` — recomputes `risk_percent_of_balance` after rounding/clamping (good), but `total_risk_percent=risk_percent_of_balance` sums per-trade percentages across a portfolio meaninglessly (percentages don't add) — see adapter.py:229 summing them.

### validation/risk/__init__.py (scanned; findings below)

- minor `__init__.py:9-17` — exports private `_trade_to_signal` in `__all__` (private name in public API); meanwhile `FixedRiskConfig/FixedRiskModel/RiskSizingInput/SizingModel/RiskModelConfig` are importable only via deep paths despite being part of the public config surface used by `RiskComparisonConfig.model_config`. Export list inconsistent.

### validation/comparison/delta_report.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `delta_report.py:270-275` — `passed = not has_error and not has_missing and not has_extra`: **any** single extra platform trade fails the entire validation, contradicting every threshold-based consumer (`ReadinessConfig.max_extra_trades` defaults 0 but json_report's trade_summary counts extra separately; readiness treats extras as warning-tolerable). One spurious fill → whole run failed. Inconsistent severity semantics across the pipeline.
- minor `delta_report.py:287-289` — `total_matched` counts matched deltas excluding duplicate_match, but duplicate_match deltas were *appended* to matched_trades at :268 — so a duplicated match both inflates matched list and is excluded from the count; summary arithmetic fragile.
- minor `delta_report.py:143` — explanation assigned by mutating pydantic model after construction instead of passing in constructor; works but fights the model's immutability posture used elsewhere.

**Smells**

- Duplicated Code (minor): `_abs_diff/_rel_diff/_is_numeric` trio duplicated again here (also in json_report.py, comparison.py) — fourth copy of `_rel_diff` logic counting risk/comparison.py and validator.py.
- Long Method (minor): `_anomaly_to_delta` (:181-237) is an if-chain over AnomalyType — Repeated Switches; dispatch dict or polymorphic anomaly would remove it (and silently falls through to bare TradeDelta for unknown types :234-237).

### validation/comparison/matcher.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `matcher.py:126-158` — greedy first-match pairing: reference trades walk in list order and take the first compatible unmatched actual. With duplicate timestamps (common on m1 bars with tolerance>0), two reference trades can pair to actual trades in an order that maximizes exit-time mismatches or steals a partner (cf. quantconnect greedy-match issue). Optimal assignment (min-cost bipartite) not required, but sorting both sides by entry_time before matching would fix most instability cheaply.
- minor `matcher.py:19-31` — `_normalize_timestamp` assumes naive strings are UTC ("Naive strings are assumed UTC") — documented decision, but combined with MT5 importer converting properly this is fine; QC importer emits "Z" suffixes handled. OK as repo-endorsed convention.
- minor `matcher.py:99-100` — options parsed from **kwargs with stringly defaults; typos in caller kwargs silently ignored (`time_tolerance` vs `time_tolerance_seconds`). Mysterious Name / no validation of unknown keys.

**Smells**

- O(n·m) nested loop (minor): fine at validation scale; noted only because detect_duplicates exists precisely because matcher cannot produce duplicates yet still runs (defensive dead code unless matcher changes).
- Data Clumps (minor): ref_data/act_data dicts with string keys — small internal scope, acceptable.

### validation/comparison/comparison.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `comparison.py:280-282` — PnL pass rule is `passed_pct or passed_abs`: with defaults pnl_pct=0.001 and pnl_abs=0.01, any pair where one side is ~0 passes via abs (diff 0.009 ≤ 0.01) regardless of relative magnitude; conversely huge PnLs pass on 0.1% relative even when absolute error is thousands. The OR makes PnL tolerance the loosest possible interpretation — for a trading validation gate this materially weakens the check. Same pattern criticized at json_report.py:180-183.
- major `comparison.py:127-128` — exit price compared using only `exits[-1]`; multi-exit trades compare last leg only while volume compares full quantity → structurally guaranteed mismatch for scaled exits (or silent pass when quantities differ per leg).
- minor `comparison.py:152-153, 159-160` — SL/TP read from `.meta.get("stop_loss")` but adapter/risk code also accepts direct attributes (`getattr(trade, "stop_loss", None)`); if a Trade model carries direct attrs and empty meta, comparison reports SL/TP mismatch that risk comparison doesn't see — two different field-resolution conventions in the same area.

**Smells**

- Duplicated Code (minor): `_normalize_timestamp`, `_pct_diff`, `_abs_diff` duplicated between matcher.py and comparison.py in the same package (both exported through `__init__`) — consolidate into comparison/util.
- Duplicated Code (minor): `_compare_optional_price` and `_compare_optional_pct` are byte-identical bodies (:244-269 vs :217-242).

### validation/comparison/anomalies.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `anomalies.py:71-79` — when `match_result is None`, `detect()` re-matches; but `detect_missing`/`detect_extra` each accept their own optional match_result — callers passing precomputed results to `detect()` are fine, yet direct calls with different tolerance kwargs re-run matching inconsistently. More concretely: `detect()` passes `**match_kwargs` only in the `match_result is None` branch; if a caller passes `match_result` computed with different tolerances than the comparator used, mismatch detection operates on a different pairing — no consistency guard.
- minor `anomalies.py:84-88` — duplicates detected over matches built from `match_result.matched`; since TradeMatcher never emits duplicate pairs (each actual consumed once), `detect_duplicates` can only fire when callers hand-craft results — dead path in the standard pipeline (see delta_report duplicate handling though, which suggests some caller does).
- minor `anomalies.py:240` — third copy of the `error_fields = {"direction", "pnl"}` severity mapping (cf. delta_report.py:161) — must stay in sync manually.

**Smells**

- Shotgun Surgery (minor): severity classification for direction/pnl errors duplicated across anomalies.py:240 and delta_report.py:161-163; extract shared SEVERITY_ERROR_FIELDS constant.
- Speculative Generality (minor): `match_kwargs` plumbing through three detect_* methods mostly unused in practice.

### validation/comparison/__init__.py (scanned; nothing found beyond context)

Clean facade re-exporting the package's public names; `__all__` complete and consistent with imports. No findings.

### validation/regression/runner.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `runner.py:315` — `TradeMatcher.match(golden_ref.trades, current_ref.trades)` is called **without tolerance kwargs**, then `AnomalyDetector.detect(golden_ref, current_ref.trades, comparison_results)` at :323-325 also re-matches with default tolerance — while the comparator config `cfg.trade_config.entry_time_seconds=60` implies matched pairs were expected within 60s. Net effect: regression matching uses exact-second equality regardless of configured trade tolerances; a 1-second timestamp drift fails every match as missing/extra even though TradeComparisonConfig says 60s is acceptable. Config plumbing incomplete.
- **major** `runner.py:336-341` — `trade_passed = trade_delta_report.passed and not has_trade_mismatch`: combined with delta_report's any-extra-fails rule, a single warning-level field mismatch (e.g. price within 0.02% but over 0.01% tol) flips `has_trade_mismatch` → regression failed, while readiness would call it READY_WITH_WARNINGS. Inconsistent gate strictness between regression and readiness paths for identical artifacts.
- minor `runner.py:90-111` — same +inf-only infinity handling as json_report.py (`-inf` falls through to numeric diff producing inf diffs and guaranteed failure when both sides are -inf).
- minor `runner.py:294` — golden file loaded via `ValidationReference.model_validate_json`; pydantic will coerce but any schema drift in stored goldens raises raw ValidationError without a "re-baseline your golden" hint.
- minor `runner.py:344-348` — `deviations` mixes TradeDelta and FieldDelta objects in one untyped list (`list[Any]`); consumers must isinstance-check — Primitive Obsession.

**Smells**

- Duplicated Code (major smell): `_STATS_FIELDS`, `_is_numeric`, `_build_stat_explanation`, `_compare_stats`, and the +inf branch are near-copies of json_report.py:65-204 — one stats-comparison module should serve both (they already drift: runner uses per-field tolerance dicts, json_report fixed tolerances).
- Long Function (minor): `run_regression` mixes loading, matching, reporting; extract steps.

### validation/stats/calculator.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `calculator.py:52-56` — `_realized_pnl` sums exit PnLs and ignores `trade.fees`; trades whose `pnl` field is gross and fees carried separately (as MT5/QC importers produce: `net_pnl = pnl - fees`) get Sharpe/Sortino/drawdown computed on gross PnL — systematic bias in every risk-adjusted metric whenever fees material. The canonical `Trade.pnl` semantics vs exits' pnl ambiguity makes this a silent correctness hazard.
- major `calculator.py:118-120` — breakeven trades (pnl == 0) counted in neither winners nor losers but **are** in total_trades → win_rate denominator includes them (win_rate < winners/(winners+losers)). Defensible convention but undocumented in the header list of conventions (:6-16); cross-platform comparisons against MT5 (which excludes breakeven from its win%) will diverge silently.
- minor `calculator.py:103` — Sortino downside deviation divides by count of negative returns only (documented at :11-13) — non-standard (usual formulation divides by N total), inflating Sortino vs other tools; documented so repo-endorsed, flagged for cross-platform parity awareness.
- minor `calculator.py:129,139` — `profit_factor`/`recovery_factor` set to `float("inf")` — propagates into JSON reports (sanitized to null there) and into `_compare_stats` where only +inf==+inf passes (see json_report finding).

**Smells**

- Nothing significant: clean, well-documented pure module.

### validation/stats/__init__.py (scanned; nothing found)

Clean facade; exports complete. No findings.

### validation/statistics/cross_platform.py (scanned; findings below)

Note: this module lives outside the package facade (`validation/__init__`, `validation/stats/__init__` export nothing from it; consumers reach it only by deep path — `scripts/compare_strategy_metrics.py:14`). Meanwhile `stats/calculator.py:4` claims the parallel `validation/statistics/` module was removed — **it wasn't**; the docstring contradicts a tracked, still-imported module. Docstring/comment vs reality drift.

**Bugs / correctness risks**

- **major** `cross_platform.py:241` — `from runner.stats import compute_stats_for_result` — imports from top-level `runner` package inside validation framework code; tight hidden coupling to an application-layer module, breaks if validation is used standalone (import error mid-function, not at module load). Also creates circular-import risk if runner ever imports validation.statistics.
- major `cross_platform.py:208-213` — MT5 CAGR derived as `(ending/initial)^(365.25/days)-1` using **calendar days between report dates**, but MT5 report period covers trading days only... actually calendar-based annualization is correct here; real issue: `period` extracted by regex from first 12 rows of the XLSX (`:281-297`) — if layout changes or the date range cell sits below row 12, CAGR silently omitted (None) rather than flagged.
- major `cross_platform.py:114` — `_build_mt5_row` calls `MT5XLSXImporter().load(...)` with **no FileNotFoundError guard** (unlike `_load_platform_stats` which checks exists): a missing reference XLSX aborts the entire matrix build instead of recording `missing_artifacts` like platform files get.
- minor `cross_platform.py:191-196` — `_metric_delta` relative delta has no sign convention doc and returns 0.0 when both zero — fine — but `denominator else 0.0` masks divide-by-zero for opposite-sign values (reference=-100, platform=100 → denom 100, relative 2.0 OK).
- minor `cross_platform.py:219-220` — win_rate >1 heuristic (`/=100`) misfires for win_rate expressed 0–1 but stored >1 due to percent-of-percent data errors; acceptable normalization, worth a comment.

**Smells**

- Divergent Change (minor): module hardcodes three MT5 case paths + one QC path (`:68-98`) — adding cases edits this function (data-as-code); move case definitions to config/JSON.
- Feature Envy (minor): reaches into `result.meta.tags`/engine strings to pick stat sources (:262-278) — stringly-typed dispatch on magic tag `"qc-parity"`.

### validation/cli.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `cli.py:237-250` — for markdown output the CLI **re-runs the whole pipeline** via `generate_json_report` just to learn pass/fail — doubling runtime and risking divergence if markdown/json paths ever differ; also comment admits it. Return value of the markdown branch should come from a structured result object.
- major `cli.py:190` — `pnl_pct=args.stats_tolerance`: reuses the *statistics* tolerance flag as the PnL relative tolerance — conflating two concepts under one flag (`--stats-tolerance` help text says "Statistics relative tolerance" yet feeds trade-level pnl_pct). Users tuning stats tolerance silently change trade matching.
- minor `cli.py:183-192` — `entry_time_seconds=args.time_tolerance` float assigned to config typed float — fine; but `time_tolerance_seconds=int(args.time_tolerance)` truncates fractional seconds passed via `-t 0.5`.
- minor `cli.py:168-170, 178-180` — broad `except Exception` around loads converts programming errors (NameError etc.) into "Failed to load" exit-2 — hides bugs during development.

**Smells**

- Nothing further: CLI structure otherwise clean.

### data_tools/validate.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `validate.py:29` — `logging.basicConfig` again executed at import time (same defect as catalog_builder.py:26).
- **major** `validate.py:132-140` — after `errors="coerce"` + `dropna()`, `date_range_start/end` read `ts.iloc[0]/[-1]` of the *dropped* series — correct order preserved, but duplicates check at :150 runs on the dropped series while OHLC checks at :199 run on `df` rows aligned to the ORIGINAL index: coerced-NaT rows remain in df and their NaN prices count as null violations twice-over (once as unparseable timestamps warning, once as nulls) — cosmetic double-reporting; more importantly `large_gaps.index[:20]` uses positional index into ts AFTER dropna — `ts.iloc[idx-1]` assumes idx is positional; `diff()` preserves original index labels, so if row 5 was dropped, idx labels shift and `ts.iloc[idx]` points at the WRONG row (label≠position). Gap boundaries reported incorrectly for files with any unparseable timestamps.
- major `validate.py:173-178` — weekend-skip logic checks only weekday pair of gap endpoints: Friday→Tuesday holidays (e.g. Easter Monday) flagged; Sunday-open symbols (crypto!) always flagged Friday→Sunday→Monday patterns skipped but intra-Sunday gaps not; crypto/timeframe "1d" threshold 4320 min = 3 days flags every long weekend for equities despite "non-forex" comment claiming weekend handling. Gap detection quality is weak for anything but 24h forex.
- minor `validate.py:94` — `path.relative_to(DATA_DIR / "ohlcv") if str(path).startswith(str(DATA_DIR))` — startswith prefix bug family again (`/data2` matches `/data`); use Path.is_relative_to.
- minor `validate.py:143` — corrupt-epoch check `year < 1990` hard-coded; ms-vs-s confusion yields years 1970–2262 range issues not caught (e.g. timestamps in year 50000 raise OverflowError inside pd.to_datetime? no—coerce → NaT, counted as unparseable; acceptable).
- minor `validate.py:312` — report written non-atomically (same pattern as catalog_builder).

**Smells**

- Long Function (minor): `validate_parquet` ~150 lines mixing concerns; split into check functions returning warnings/errors lists.
- Mysterious Name (minor): `EXPECTED_BARS_PER_DAY` defined at :35-43 but never used anywhere in the module — dead configuration table (Speculative Generality).

### data_tools/cache_manager.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `cache_manager.py:271-279` — `unsafe_no_copy=True` returns the **cached list object itself** (no deepcopy): any caller mutating the returned entries (sorting in place, appending keys) corrupts the shared cache for all subsequent readers, and does so under a lock released before use. The flag name admits the hazard but nothing prevents misuse; at minimum document or mark entries read-only.
- major `cache_manager.py:240-262` — `get_catalog` holds `_CACHE_LOCK` (RLock) across `build_catalog(root)` / disk load — a full filesystem scan + parquet metadata reads that can take minutes on large stores. Every other caller of `get_catalog*`, `invalidate_cache`, `get_cache_stats` blocks for the entire build. Lock-per-key or double-checked locking with a per-key build lock needed. Throughput cliff under concurrent API requests.
- minor `cache_manager.py:301-306` — `get_overview_metadata` builds overview from catalog outside the lock then `setdefault`; two threads may both build (benign duplication) — fine, but the second `deepcopy(_OVERVIEW_CACHE[key])` may return the OTHER thread's overview — consistent anyway.
- minor `cache_manager.py:144-146` — total size double-counts when an OHLCV entry lists files whose sizes are already aggregated into the entry's own `size_bytes`? Grouped entries aggregate file sizes once; fundamentals counted separately; OK. Noting only that `_safe_int(entry.get("size_bytes"))` silently coerces None→0 masking missing data.

**Smells**

- Module-level mutable globals guarded by one RLock — acceptable pattern here (documented thread-safe intent), not flagged as smell.
- Duplicated Code (minor): source-collection logic `_collect_sources` (:98-109) mirrors catalog_builder's summary building (`catalog_builder.py:866-877`) — same "sources vs mixed" convention implemented twice.

### data_tools/resample.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `resample.py:104-106` — `resample_file` writes `{tf}.parquet` **into the source directory by default**, overwriting any existing higher-timeframe parquet without merging (unlike base.py's merge-store). A re-run after the 1m source gained new history produces truncated higer-TF files (only the resampled window), silently *losing* older bars if source 1m was itself clipped (yfinance 60-day limit!). Combined with auto_download flows this can regress stored data.
- major `resample.py:57` — `pd.to_datetime(df["timestamp"], utc=True)` without `errors=`: one bad timestamp aborts whole resample with raw exception (CLI catches nothing at :181).
- minor `resample.py:33` — `"1w": "1W"` alias exists in map but TIMEFRAME_ORDER includes it; Dukascopy native downloader writes `w1` filenames — naming mismatch (`1w.parquet` vs `w1`) means resampled weekly files land under a name other consumers may not find (cf. jforex `_normalize_timeframe_for_file_suffix` mapping 1w→w1).
- minor `resample.py:22` — import-time `logging.basicConfig` again.
- minor `resample.py:118` — dead variable: `pattern = f"**/{from_tf}.parquet"` computed and never used (rglob uses different pattern at :119).

**Smells**

- Duplicated Code (major smell): `_resample` implementations duplicated across resample.py:41-75, ctrader_dl.py:229-246, histdata_dl.py:228-246, huggingface_dl.py:191-212, yfinance_dl.py:253-269, dukascopy_native.py:747-768 — six copies of the identical OHLCV agg dict; consolidate on this module (it IS the canonical tool per docstrings).

### data_tools/signal_import.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `signal_import.py:67-77` — `_parse_action` defaults **unknown/missing actions to "buy"**: any malformed side value ("B", "close", empty) silently becomes a long entry. For signal replay this fabricates trades; should skip row or raise.
- major `signal_import.py:92` — timezone inferred from column NAME containing "ny" (`zeit_ny`, `time_ny`); a column like "company" containing time would... no—only checked on chosen time_column. But `source_timezone = "America/New_York"` string passed to `tz_localize` — DST ambiguity: naive timestamps during fall-back hour raise `AmbiguousTimeError` uncaught (no `ambiguous="infer"` / nonexistent handling).
- minor `signal_import.py:113` — timeframe inferred from presence of time-of-day: a daily signal stamped 00:00:00 → "1d" fine; but intraday signals at exactly midnight misclassified as daily.
- minor `signal_import.py:174-186` — every CSV cell copied into metadata verbatim; a wide export balloons payload memory; also `row.get(symbol_column)` etc. use pandas Series.get — fine.

**Smells**

- Primitive Obsession (minor): signals as raw dicts with magic keys ("timestamp","symbol","action","entry_price") consumed by ReplaySignal.from_payload elsewhere — a Signal TypedDict/pydantic model would pin the contract.

### data_tools/dolthub_manager.py (scanned; findings below)

**Bugs / correctness risks**

- **critical (security)** `dolthub_manager.py:96-102, 111-117, 132-144` — **SQL built by f-string interpolation**: `SELECT ... WHERE act_symbol='{symbol}'` and table name interpolated unquoted. Symbols come from `dolt sql` output (repo-controlled), so exploitability is bounded, but any symbol containing a quote breaks/injects the query; table names from `SHOW TABLES` interpolated directly. Checklist item "SQL string building" violated verbatim. Use parameterized queries (`dolt sql -q ... --param`?) or strict identifier validation.
- **major** `dolthub_manager.py:30-32` — `subprocess.check_call(cmd, cwd=cwd)` with **no timeout** anywhere in the module (`run_cmd`, `_list_tables` via subprocess.run without timeout, exports). A hung dolt process blocks forever. Checklist item: subprocess without timeout.
- major `dolthub_manager.py:148` — `pd.to_datetime(df["timestamp"])` **without utc=True** → tz-naive timestamps written to parquet, inconsistent with every other writer in the area (checklist item tz-naive datetimes); downstream merge with UTC-normalized data shifts/sorts incorrectly.
- minor `dolthub_manager.py:76-77` — `open(output_csv, "w")` without encoding/close-on-error context is used correctly (with-block), fine; but `_export_table_csv` writes CSV next to final parquet with fixed name — concurrent runs collide on temp csv path (race).
- minor `dolthub_manager.py:12-15` — another import-time `logging.basicConfig`.

**Smells**

- Divergent Change (minor): stocks/earnings/rates export blocks near-duplicated (:93-199); parameterize per-repo config.

### data_tools/migrate_csv.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `migrate_csv.py:30-32` — hardcoded absolute sibling-project paths (`~/Desktop/custom_lean_integration`, `~/Desktop/stockkit_ai_bot`) outside this repo; migration silently migrates nothing when layout differs, and reading sibling projects violates repo boundaries — brittle one-shot script promoted to tracked tool.
- major `migrate_csv.py:129-131` — `dest.exists() → SKIP`: existing partial/corrupt parquets are never refreshed; combined with non-atomic `to_parquet` write (:196) an interrupted run leaves a truncated dest that all future migrations skip. No temp-file+rename.
- minor `migrate_csv.py:145-157` — yfinance multi-header sniffing heuristics (`first_val.isupper()`) are fragile: symbol names are uppercase so legitimate first data rows whose price column contains ticker-like strings get dropped.
- minor `migrate_csv.py:200-202` — broad `except Exception` logs and continues — acceptable for batch migrate, but converts KeyboardInterrupt? No (BaseException). OK.

**Security**

- Nothing found (local file reads/writes only; no shell, no SQL).

**Smells**

- Duplicated Code (minor): five `migrate_*` functions share glob→convert→count shape; a table-driven loop over (glob, tf, timestamp_col) would collapse them.

### data_tools/download_grid_market_data.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `download_grid_market_data.py:155-161` — `ensure_alias_copy` treats equal file SIZE as proof of content equality (`alias_path.stat().st_size == canonical_path.stat().st_size`) — different data with same byte length (extremely likely across download windows) skips refresh; stale alias copies persist silently. Compare mtime+size or hash.
- major `download_grid_market_data.py:294-313` — ProcessPoolExecutor workers each run async event loops + aiohttp against the same host with combined concurrency `workers × batch_size × pipeline_batches` (6×20×2 default = 240 simultaneous requests) — guaranteed rate-limit/ban territory; no global throttle across processes.
- major `download_grid_market_data.py:225-236` — resampling reads `source_path` (canonical dir) but writes to `requested_dir` via `resample_file(..., output_dir=requested_dir)`; if `requested_symbol == canonical_symbol`, requested_dir == canonical_dir and re-runs overwrite TFs without merge (see resample.py finding) — grid refresh loses history when source 1m was range-clipped.
- minor `download_grid_market_data.py:96-126` — `normalize_ohlcv_columns` rewrites parquet non-atomically (read-modify-write, crash → corrupt file that later passes `path.stat().st_size != 0` checks).
- minor `download_grid_market_data.py:347-350` — summary written to `Path.cwd()/logs` — depends on invocation directory, not repo/log root (LOG_DIR exists in shared.constants but unused here).

**Smells**

- Duplicated Code (minor): TIMEFRAME_ORDER/TIMEFRAME_ALIASES/VALID_TIMEFRAMES re-declared here despite shared.constants.normalize_timeframe existing; comment even says "must stay in sync" — Shotgun Surgery acknowledged in-code.
- Speculative Generality (minor): `OIL_ALIASES` maps aliases to themselves plus WTI/CL/BRENT variants — only reachable via --oil-type flag; fine but half the mappings are identity.

### data_tools/downloaders/alpaca_dl.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `alpaca_dl.py:235-243` — paginated request loop sends `limit=10_000` with `page_token`; correct, but `parsed_start.to_pydatetime()` passes tz-aware datetime while Alpaca API historically requires US/Eastern-naive or ISO-with-offset — alpaca-py handles conversion; real bug: when BOTH parsed_start and parsed_end are None the request has no bounds and pages through entire history with 10k pages and no cap/progress — runaway download risk on first run (resume start mitigates only if file exists).
- minor `alpaca_dl.py:180` — missing volume filled with int `0` (not float) — dtype inconsistency in parquet schema across symbols.
- minor `alpaca_dl.py:259` — repeated-token guard compares only consecutive tokens via seen set — fine; but no max-page ceiling: a buggy server echoing unique tokens forever loops unbounded.
- minor `alpaca_dl.py:218` — `_resolve_resume_start(out_path, start) or start` — resume start (a date string) wins over explicit start? No: `_resolve_resume_start` returns requested_start unchanged when provided; falls back to latest stored ts only when start falsy. Correct but subtle; OK.

**Security**

- minor `alpaca_dl.py:74-85` — credentials read from env into instance attributes; not logged anywhere (checked). OK.

**Smells**

- Nothing significant; cleanest downloader alongside yfinance.

### data_tools/downloaders/yfinance_dl.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `yfinance_dl.py:53-59` — `YF_INTERVAL_MAP["4h"] = "60m"`: 4h downloads fetch 1h bars and resample at :253-269 — but `_clip_intraday_window` applies the **60-day window to the 4h request based on 4h classification** while actually pulling 60m data (also 60-day limited — coincidentally consistent). Fine. Real issue: resample `.dropna(subset=["open","high","low","close"])` keeps bars where volume summed to 0 across partial sessions; and 4h buckets align to clock (0/4/8...) not exchange session — 4h bars mix pre/post market differently than MT5 references; parity hazard documented nowhere.
- major `yfinance_dl.py:169-183` — resume-start resolution: when `bounded_start is None` (no explicit start), `resolved_start = self._resolve_resume_start(out_path, None)` — returns date-only string of last ts; then clipped again by 60-day window which **silently discards** resume dates older than 60 days for intraday (warning appended) — expected due to Yahoo limits, but the resulting file then has a gap between stored history and new data with no error surfaced to callers checking coverage (auto_download._has_required_coverage will loop re-downloading the same 60-day window forever for ranges > 60 days back). Interaction bug between auto_download expectations and Yahoo constraints.
- minor `yfinance_dl.py:214-217` — MultiIndex flatten takes `column[0]` — with group_by="column" and single ticker this drops price-field level? columns are (field, ticker) so [0]=field — correct; fragile if yfinance changes order.
- minor `yfinance_dl.py:242` — coerce errors → NaT rows dropped silently (no warning count).

**Smells**

- Duplicated Code (minor): `_parse_csv_list`, `_to_utc_timestamp`, `_normalize_supported_timeframe` duplicated nearly identically in alpaca_dl.py/huggingface_dl.py — move to base.py.

### data_tools/downloaders/ctrader_dl.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `ctrader_dl.py:149-153` — date filtering compares `df["timestamp"] >= effective_start` where effective_start comes from `_resolve_resume_start` (a DATE string "2024-01-01"); pandas compares tz-aware timestamps against naive date string — comparison raises or coerces depending on pandas version; on versions allowing coercion the string is interpreted midnight UTC — OK-ish, but `end` filter compares raw string too; inconsistent typing between branches.
- major `ctrader_dl.py:120` — timeframe validation accepts anything in RESAMPLE_MAP or "tick", but tick CSVs then flow into `_merge_and_store_single_file` writing `{tf}.parquet` named "tick" while asset-class guess at :143 misses XAUUSD/XAGUSD (listed metals in AVAILABLE_PAIRS) → gold/silver stored under asset_class "forex".
- minor `ctrader_dl.py:181-189` — separator sniffing tries sep until ≥5 columns parse; a semicolon file with exactly 5 comma-parseable garbage columns stops early with wrong parse (no header sniff).
- minor `ctrader_dl.py:207` — `pd.to_datetime(..., utc=True)` without errors= → raw exception aborts import on malformed row (inconsistent with other loaders' coerce+drop).

**Smells**

- Speculative Generality (minor): class advertises cTrader Open API support but raises "not implemented" (:133-136) — dead API surface; docstring says simplified port.
- Duplicated Code (minor): `_resample` copy #2 (see resample.py smell).

### data_tools/downloaders/histdata_dl.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `histdata_dl.py:171-204` — screen-scrapes histdata.com HTML with regex token extraction (`id="tk"\s+value="..."`) and POSTs form data; any site layout change breaks silently (warning per year, then "No data downloaded"). Fragile by design; no contract/tests. Also `requests.Session()` created per year — cookies re-fetched yearly (fine), but session never closed (resource leak, minor).
- major `histdata_dl.py:116-119` — year derivation `int(effective_start[:4])` slices resume-date strings assuming ISO format; `_resolve_resume_start` guarantees iso date — OK — but CLI `--years 2020-2025` parse at :286-287 splits on "-" producing ["2020","2025"]; a single-year arg "2021" yields parts len 1 → IndexError crash (unhandled).
- minor `histdata_dl.py:202` — success heuristic `len(resp.content) < 100` — small ZIP could be valid empty-year; misclassified failure (benign).
- minor `histdata_dl.py:218-220` — `format="%Y%m%d %H%M%S"` strict parse: histdata occasionally emits trailing whitespace handled by .strip(); OK. Volume column may be absent in some exports → pd.read_csv with names=6 raises on 5-col rows (ParserError) → caught broadly at :224 returning None (silent year loss).

**Security**

- minor `histdata_dl.py:184` — regex over remote HTML (ReDoS-resistant here, bounded pattern); POST includes only static fields. No secrets sent. OK.

**Smells**

- Duplicated Code (minor): `_resample` copy #3.
- Feature Envy (minor): download logic interleaves HTTP mechanics with parsing; separate transport from parsing for testability.

### data_tools/downloaders/huggingface_dl.py (scanned; findings below)

**Bugs / correctness risks**

- **major** `huggingface_dl.py:250-270` — monthly parquet download loops over ALL months from DEFAULT_START_DATE (1994!) when start omitted via resume default — each month pulls the ENTIRE dataset shard (all symbols) via `pd.read_parquet(url)` and filters to one symbol client-side: hundreds of full-shard downloads (~GBs each) for one ticker. No shard-level predicate pushdown (pyarrow filters=) — severe bandwidth/time cost, plus HF rate limits make long loops fail mid-way leaving partial merges (which then look complete after _merge_and_store).
- major `huggingface_dl.py:258-261` — `except Exception: warnings.append(continue)` swallows auth/network errors per month; systematic failure (bad URL template, expired dataset) yields months-long warning list then RuntimeError only if ALL fail — partial silent data otherwise.
- minor `huggingface_dl.py:31 vs auto_download.HF_INTRADAY_COVERAGE_END` — coverage constant duplicated ("2025-07-31") in two modules — drift risk.
- minor `huggingface_dl.py:233` — end defaults to coverage end rather than today; requesting recent data silently capped with only a warnings list consumers may ignore.

**Smells**

- Duplicated Code (minor): `_parse_csv_list/_to_utc_timestamp/_normalize_supported_timeframe` trio again (copy #3).
- Long Function (minor): download() ~90 lines mixing month iteration, normalization, storage.

### data_tools/downloaders/dolthub_dl.py (scanned; findings below)

**Bugs / correctness risks**

- **critical (security)** `dolthub_dl.py:94-102` — SQL query built by f-string with `symbol`, `effective_start`, `end` interpolated: `act_symbol = '{symbol}'`. Symbol originates from CLI/user input → classic injection vector executed server-side at DoltHub (leaks/exports arbitrary repo data the token can read, or errors). Also identifier `table` interpolated unquoted. Parameterize via API params or escape quotes. Checklist item: SQL string building.
- **major** `dolthub_dl.py:139` — `data` referenced after retry loop: if all retries raise inside except branch they re-raise (OK), but if `resp.json()` raises JSONDecodeError (HTTP 200 HTML page) the `break` never executes and `data` unbound → UnboundLocalError instead of clear error.
- major `dolthub_dl.py:111-134` — retry loop retries only Timeout/ConnectionError; `raise_for_status` HTTP 429/5xx raises RequestException → immediate abort (no retry/backoff for server throttling, inconsistent with other downloaders).
- minor `dolthub_dl.py:167-171` — post-query date filtering duplicates the WHERE clause already built into SQL (redundant but harmless; masks SQL-injection impact of start/end too).
- minor `dolthub_dl.py:89` — `repo.split("/", 1)` without validation: repo without slash → owner==whole, repo_name missing → nonsense URL; ValueError-free but confusing 404.

**Smells**

- Repeated Switches (minor): retry logic re-implemented per downloader (dukascopy backoff, dolthub linear, histdata none) — extract shared retry helper.

### data_tools/__init__.py (scanned; nothing found)

Empty package marker. Nothing to review.

### data_tools/downloaders/__init__.py (scanned; nothing found)

Empty package marker; note asymmetry: `validation/importers/__init__.py` re-exports its classes while this one exports nothing — every consumer deep-imports downloaders. Minor consistency point only.

### validation/__init__.py (scanned; findings below)

- minor `__init__.py:1-5` — exports only ValidationReference though subpackages (comparison, reporting, readiness, regression) form the public API; importing `validation.reporting` etc. still works via submodule paths. Cosmetic incompleteness.

### validation/readiness/__init__.py (scanned; nothing found)

Clean facade: exports models + validator, `__all__` complete and matching imports. No findings.

### validation/regression/__init__.py (scanned; nothing found)

Clean facade: re-exports RegressionConfig/RegressionReporter/RegressionResult/run_regression. No findings.

### validation/reporting/__init__.py (scanned; nothing found)

Clean facade: re-exports ValidationReport + both report generators. No findings.

---

## Cross-cutting summary

**Top correctness risks (would fix first):**
1. `dukascopy_native.py:790-791` — merged bid/ask tick download fetches bid URLs twice → ask data is bid data (silent data corruption).
2. `dukascopy_native.py:1016-1028` — positional-argument misalignment into `_download_and_assemble_merged_ticks` swaps pause_ms/max_retries.
3. `dolthub_dl.py:94-102` / `dolthub_manager.py:96-102` — SQL built by string interpolation with user-controlled symbol (injection).
4. `json_report.py` — missing `TradeComparisonConfig` import (NameError under annotation introspection) plus `-inf` asymmetry and OR-tolerance pass logic weakening the gate.
5. `stats/calculator.py:52-56` — all validation statistics computed on gross PnL ignoring fees.
6. `resample.py:104-106` + `download_grid_market_data.py` — resampled TF files overwritten without merge; grid refresh can silently truncate stored history.

**Checklist sweep (Python):**
- mutable default args: none found (dataclasses use `field(default_factory=...)` throughout) ✔
- bare except: no true bare `except:`; many broad `except Exception` flagged per-file above (auto_download.py:315/325, dukascopy_discovery.py:145, risk/comparison.py:191, cli.py:168/178/214, migrate_csv.py:200, huggingface_dl.py:259, histdata_dl.py:224).
- race conditions on shared state: base.py parquet read-modify-write; dukascopy_native source_meta write; cache_manager unsafe_no_copy; catalog.json non-atomic writes; download_grid alias-copy check.
- SQL string building: dolthub_dl.py:94-102, dolthub_manager.py:112/138 (critical/major above).
- subprocess without timeout: dolthub_manager.py:30-32, 60-65, 77, 111-116, 132-144 (jforex urlopen calls have timeouts; jpype JVM start unbounded by design).
- tz-naive datetimes: reference.py:51 (`datetime.utcnow`, naive); dolthub_manager.py:148 (`pd.to_datetime` without utc); matcher/comparison assume-naive-is-UTC (documented convention); mt5 importers handle via source_timezone param (good).
- resource leaks: histdata_dl.py Session never closed; dukascopy_discovery new event loop+connector per probe (inefficiency more than leak); jforex client disconnect handled in finally ✔.
- unhandled promise rejections / N+1 fetches (TS items): N/A — Python area; nearest analogues are the per-month full-shard HF downloads (N+1-shaped bandwidth problem, flagged) and CLI re-running the pipeline to learn pass/fail (cli.py:237-250).

**Dominant smells across the area:** Duplicated Code (six resample copies; four `_rel_diff`s; two MT5 importers ~200 shared lines; stats-comparison duplicated between runner and json_report; `_normalize_offer_side` twins), Repeated Switches / if-chain source dispatch in auto_download, Primitive Obsession (untyped catalog/signal/deviation dicts), Long Function (catalog_builder._group_ohlcv_entries, quantconnect _fee_reconciliation, json_report.generate_json_report), Speculative Generality (RiskModelConfig, EXPECTED_BARS_PER_DAY, uuid4 import, ctrader API surface).

---

## Finding counts by severity

Bulleted findings recorded above (smell-only observations are labeled as Smells and not double-counted):

- **critical: 4** (2 correctness — dukascopy_native bid/ask merge fetches bid twice, json_report missing `TradeComparisonConfig` import; 2 security — SQL string building in dolthub_dl.py and dolthub_manager.py)
- **major: 52**
- **minor: 100**

Total findings: **156** across 50 files — 42 files with findings, 8 files scanned clean (validation/importers/base.py, validation/comparison/__init__.py, validation/stats/__init__.py, validation/readiness/__init__.py, validation/regression/__init__.py, validation/reporting/__init__.py, data_tools/__init__.py, data_tools/downloaders/__init__.py).
