# Code Review — area `data-validation` (reviewer r2, blind/independent)

Scope: `data_tools/` (market-data download/cache/resample) and `validation/` (backtest-vs-broker validation framework). All 50 tracked code files reviewed. Findings cite `file:line` with severity critical/major/minor. Smells use Fowler, *Refactoring* ch.3 names; repo standards may suppress a smell (none found that do).

---

## data_tools/downloaders/dukascopy_native.py (1316 lines)

### (a) Bugs / correctness

- **Major — positional args silently swallowed by keyword-only params.** `download_bi5_files(urls, batch_size, max_retries, batch_size=batch_size, ...)` at `dukascopy_native.py:1036-1041` (same pattern `:792-798`, `:811-817`, `:699-704`): `batch_size` and `max_retries` are declared keyword-only (`*,` after them at `:415-416`), so the two positional values are discarded and the explicit kwargs re-supply them. Harmless today only because both spellings carry identical values; it is dead/duplicated argument plumbing that will break if one site is edited.
- **Minor — `_fetch_one` returns success-shaped result for non-retryable failures.** At `:406`, after exhausting retries on `TimeoutError/OSError`, the returned `FetchResult` has `cooldown_required=False` even when `throttle_signal=True`; contrast `:378-383` which propagates it. A throttled run that times out will not trigger the hard cooldown path (`:516`) and can hammer a rate-limited server.
- **Minor — weekday heuristic mislabels weekend bad payloads.** `_looks_like_invalid_bi5` retry logic at `:385-397`: an HTML error page served on Saturday/Sunday is treated as "no data" without any retry; a transient 502-with-HTML on a weekend is silently dropped.
- **Minor — `parse_ticks` truncates trailing partial records silently.** `range(0, len(raw) - size + 1, size)` at `:559` (and `:582`) drops a trailing partial record without logging; corrupt feeds lose data invisibly.
- **Minor — tick merge keeps bid rows whose timestamps have no ask counterpart.** Inner join at `:839-843` for merged ticks means half the ticks vanish whenever bid/ask timestamps differ by 1 ms; no warning is emitted.
- **Minor — `mn1` resampled as months starting at first bar, not month start.** `TF_RESAMPLE_RULE["mn1"]="1MS"` at `:177` is correct, but `_ticks_to_ohlcv` at `:1068-1070` passes arbitrary timeframes from `DOWNLOAD_STRATEGY` keys; `tick`→`_ticks_to_ohlcv(df,"tick",...)` would raise only if reached — currently unreachable but fragile.

### (b) Security
Nothing beyond network hygiene: URLs are built from whitelisted instrument strings (`:249-253`), no eval/subprocess/shell. No secrets logged.

### (c) Baseline smells
- **Long Function / duplicated orchestration.** `_download_async` spans `:922-1197` (~275 lines) with three near-identical progress-lambda blocks (`:799-803`, `:818-822`, `:1043-1047`, `:1084-1088`, `:1103-1111`, `:1144-1148`) — extract a `_wrap_progress(report_progress, base, scale, prefix)` helper (Extract Function).
- **Duplicated Code — bid/ask side download.** The `offer_side == "both"` minute branch (`:1074-1115`) duplicates `_download_minute_side_dataframe` call shape twice differing only in `side=` and the progress lambda; likewise `_download_and_assemble_merged_ticks` (`:774-843`) duplicates it for ticks.
- **Primitive Obsession.** Timeframe strings ("m1","h4","1h","d1"…) flow through four parallel maps (`TIMEFRAME_MAP :146`, `storage_tf_map :938`, `TF_RESAMPLE_RULE :168`, `DOWNLOAD_STRATEGY :188`) plus a fifth alias dict inside `_normalize_genesis_timeframe :204`. A `TimeFrame` value type with the mappings as methods would remove the clump.
- **Data Clumps.** `(pause_event, stop_event, log_callback, report_progress)` travel together through every function signature in the file (`:354-356`, `:421-423`, `:686-692`, `:782-786`) — bundle into a `DownloadControls` object.
- **Mysterious Name.** `GENESIS_PATH`/`_load_genesis_index` (`:62`, `:849`) — nothing in this file explains what "genesis" is (it is a per-symbol data-availability start-date index); rename to `availability_index`.
- **Speculative Generality.** `pipeline_batches` windowing (`:429-430`, `:1262-1265`) adds a second dimension of batching that duplicates `batch_size` semantics; default 1 makes it dead config in all internal callers.

### (d) Maintainability hot spots
The 1300-line module mixes URL generation, HTTP engine, binary codecs, pandas assembly, persistence metadata, and CLI in one file — Divergent Change (any new source format touches the same module as any throttle-policy change). Splitting codec/parsing (`parse_ticks`/`parse_candles`) from transport (`download_bi5_files`) would shrink review surface substantially.

---

## data_tools/downloaders/jforex_dl.py (1313 lines)

### (a) Bugs / correctness

- **Major — first failing JVM candidate aborts discovery instead of falling back.** In `_ensure_jvm_started` at `jforex_dl.py:551-552`, `except _java_exception_types() as exc: _raise_jforex_runtime_error(...)` re-raises immediately, so a candidate that exists but fails to boot with a Java exception skips the remaining candidates and the final default-JPype attempt (`:557-570`). The loop's `startup_errors` collection (`:555`) shows fallback was the intent.
- **Major — partial artifacts persisted before a failing merge.** With `offer_side="both"`, bid/ask files are written at `:1146-1147` *before* the inner merge at `:1148`; if the merge yields zero overlapping timestamps the function raises at `:1179-1180` with two freshly-written side files already on disk and no combined output — a corrupt-looking cache state for `_resolve_resume_start` on the next run.
- **Minor — reported `rows`/`path` disagree with what was stored for `both`.** `rows=len(combined)` counts the inner-joined frame (`:1206`) while the stored files hold the full bid and ask series; `path=output_path.parent` (`:1189`) is a directory, not a file, breaking the `DownloadResult` contract every other caller relies on.
- **Minor — silent drop of explicitly configured JVM.** `_discover_jvm_paths` at `:444-449`: when `JFOREX_JVM_PATH` is set but `JFOREX_ALLOW_CUSTOM_JVM != "1"` (or trust check fails), the override vanishes with no log — mysterious-failure debugging cost.
- **Minor — connect poll loop can spin past deadline mid-sleep** (`:858-864`) — bounded, cosmetic.
- **Minor — `int(start_dt.timestamp() * 1000)` float rounding** (`:1030-1031`, `:1158-1159`): for sub-second timestamps the truncation can shave a millisecond off `end`, dropping the final tick/bar boundary case.

### (b) Security

- **Major — unverified remote jar bootstrap into the JVM classpath.** `_download_jforex_jars_from_jnlp` (`:280-311`) fetches every `jar`/`nativelib` href from a network JNLP document and executes them via `startJVM(classpath=...)` (`:549`) with **no checksum, signature, or host allow-list**. `DUKASCOPY_JNLP_URL` defaults to the vendor URL but is env-overridable, so anyone who can set that env var gets arbitrary code execution inside the trading platform's process. The module goes to elaborate lengths to restrict *which libjvm binary* may load (`_trusted_java_home_roots :356-379`, `_is_trusted_custom_jvm_path :422-437`) yet applies zero integrity checking to the far larger attack surface — the downloaded jars themselves.
- **Minor — credentials handling is otherwise sound**: `DUKASCOPY_PASSWORD` is read from env (`:184`) and never logged; diagnostics formatter excludes auth (`:505-529`).

### (c) Baseline smells

- **Duplicated Code — dedupe helper rewritten four times.** Identical "preserve-order set dedupe" loops at `:218-225`, `:270-277`, `:304-311`, `:491-502` — extract one `unique(iterable)` util.
- **Duplicated Code — cross-module copies from dukascopy_native.** `_normalize_offer_side` (`:960-964`) is byte-identical to `dukascopy_native.py:640-644`; `_bars_with_offer_prefix`/`_resample_offer_prefixed` (`:973-1000`) mirror `_prefix_ohlcv_columns`/`_resample_side_prefixed_ohlcv` (`dukascopy_native.py:647-674`). Offer-side logic belongs in one shared module.
- **Primitive Obsession / Data Clumps.** Timeframe aliasing appears again here (`TIMEFRAME_ALIASES :60`, `BASE_TIMEFRAME_MAP :80`, `WINDOW_DAYS_BY_BASE_TF :92`, `BAR_INTERVAL_MS :99`, `JFOREX_PERIOD_ATTR :107`, plus `_normalize_timeframe_for_file_suffix :967` doing string surgery) — the fifth independent timeframe mapping in the area.
- **Mysterious Name.** `_normalize_timeframe_for_file_suffix` performs sequential blind `.replace()` calls (`:967-970`) whose correctness depends on replacement order ("1m" before "15m" is only safe because "15m" doesn't contain "1m") — a map lookup would be self-documenting and safe.
- **Inappropriate Intimacy.** Imports sibling privates `SYMBOL_CONFIG, _resolve_symbol_and_instrument` from `dukascopy_native` (`:40`) — the underscore names are another module's implementation details; promote them to a public shared symbols module.

### (d) Maintainability hot spots
~450 lines of JVM/classpath/trust-discovery plumbing (`:204-609`) ahead of the actual data query makes the file two modules wearing one name. A `jforex_runtime.py` (JVM lifecycle + jar bootstrap) split from `jforex_dl.py` (history queries) would isolate the risky bootstrap code and let the downloader be reviewed without it.

---

## validation/importers/quantconnect_result.py (995 lines)

### (a) Bugs / correctness

- **Major — tz-naive fallback timestamps silently pinned to UTC.** `_canonical_time` at `quantconnect_result.py:200-213`: when ISO parsing fails, `datetime.strptime` with `"%m/%d/%Y %H:%M:%S"` (and the `%I:%M:%S %p` variant) yields a **naive** datetime that `:212-213` stamps as UTC. Those legacy formats exist precisely because such exports are *not* UTC-labelled; a local-time export has every entry/exit time shifted by the zone offset, corrupting downstream backtest-vs-broker time matching with no warning. (The checklist item "tz-naive datetimes" applies literally.)
- **Minor — `_submission_clusters` crashes on empty input.** `min(sizes)`/`max(sizes)` at `:670-671` raise `ValueError` if `orders` is empty. Unreachable today because `load` rejects empty orders at `:347-349`, but the function is written as if independently callable — a latent trap.
- **Minor — full equity curve embedded in metadata.** `_strategy_equity_evidence` returns every point (`"points": points`, `:701`) which lands inside `raw_metadata` (`:429`) and therefore inside every emitted report/hash. A multi-year backtest's equity series (10⁵–10⁶ points) is copied into each artifact; first/last point plus count would carry the same evidentiary weight.
- **Minor — rigid equity point schema.** `:684-685` requires *exactly* 5-element `[t,o,h,l,c]` points; LEAN also emits 2-element `[t,value]` series for "Strategy Equity" depending on framework version — such exports become unusable rather than degrading to close-only evidence.
- **Minor — float money math in Trade construction.** `net_pnl=pnl - fees` (`:597`) and float conversions at `:555-559` do binary-float arithmetic on monetary values even though the module deliberately uses `Decimal` everywhere else (e.g., `:479-482`). Penny-level drift possible in comparisons against broker statements.

### (b) Security
Nothing found: file reads only, `json.loads`/`csv.DictReader` used safely, no dynamic execution, path comes from caller.

### (c) Baseline smells

- **Duplicated Code — triplicated reconciliation payloads.** `_fee_reconciliation` returns three ~20-key dict literals (`:744-764`, `:766-786`, `:810-838`) sharing eight identical keys; extract one payload builder parameterized by resolution status.
- **Long Function.** `load` spans `:324-451` (~130 lines) orchestrating nine concerns; `_fee_reconciliation` spans `:704-839`; `_normalize_orders` `:453-533`. Each exceeds any reasonable extraction unit boundary — Extract Function/Extract Class.
- **Repeated Switches.** `_csv_field_matches` (`:972-986`) is an if-chain dispatching on field name to comparators; a `{field: comparator}` map (the module already keeps `_CSV_NUMERIC_FIELDS`) removes the switch and makes new field types additive.
- **Primitive Obsession.** Currency amounts oscillate between `str`, `Decimal`, and `float` within single functions (`format(quantity,"f")` vs `float(portfolio_total)`); a `Money` type would stop the conversion ping-pong.

### (d) Maintainability hot spots
Strongest file in the area for defensive validation (refuses to invent evidence, checks duplicate ids/order references) — but its size is driven by evidence-plumbing prose (deeply nested dict shapes) interleaved with logic. Moving the reconciliation-report shape into a small dataclass layer would cut ~150 lines and make the three resolution paths diffable.

---

## data_tools/catalog_builder.py (922 lines)

### (a) Bugs / correctness

- **Major — silent "1d" guess poisons the catalog.** `parse_ohlcv_path` defaults `timeframe_value = "1d"` whenever a stem can't be parsed (`catalog_builder.py:251`, `:264`, `:274`) with no log in two of the three branches. A file named `SPX_quarterly.parquet` (or any unmatched convention) is published as a daily dataset in `catalog.json`; downstream selection tools trust that label. Misclassified data feeding strategy research is worse than a skipped file.
- **Major — prefix-match storage detection.** `_detect_storage` at `:97-102` uses `str(parquet_path).startswith(str(cache_dir))`; sibling directories like `<data>/cache-old/x.parquet` match `<data>/cache` and get labelled `"cached"`. Needs `os.path.commonpath` or a trailing-separator check.
- **Minor — parquet reader never closed.** `read_parquet_summary` opens `pq.ParquetFile(parquet_path)` at `:355` and relies on GC to release the handle; under a large tree scan (thousands of files) plus any exception path, fd exhaustion is possible. Use the context-manager form. (Resource-leak checklist.)
- **Minor — TOCTOU between stat and open.** `parquet_path.stat()` at `:354` then open/read at `:355/:380/:384`: a concurrently rewritten file yields mismatched size/mtime metadata vs content — relevant because downloads run while catalogs rebuild.
- **Minor — full-table load for a row count.** `scan_fundamentals` at `:843-846` runs `pd.read_parquet(parquet_file)` materializing entire tables in memory just to record `len(df)`; `pq.ParquetFile(...).metadata.num_rows` (as used in `read_parquet_summary`) avoids it.
- **Minor — non-atomic catalog write.** `main` writes `catalog.json` via plain `write_text(out)` at `:914-915`. The same file is the reuse cache (`_load_catalog_cache :63`), so an interrupted run leaves a torn file; handled gracefully today, but atomic tmp+rename is cheap insurance.

### (b) Security
Nothing found: local filesystem scanning only; regexes are bounded; JSON parsing safe.

### (c) Baseline smells

- **Long Function / Feature Envy.** `_group_ohlcv_entries` spans `:439-722` (~285 lines) mutating a 24-key aggregate dict literal (`:449-479`); it is entirely envious of entry-dict internals. Extract a `DatasetAggregate` class owning the merge rules (Extract Class/Move Method).
- **Primitive Obsession.** Dataset records are stringly-keyed dicts with alias pairs — `row_count` *or* `rows` (`:508`), `start` *or* `start_date` (`:520-521`), `source` *or* `sources` (`:481-486`) — every consumer must know both spellings. One typed record kills the aliases.
- **Duplicated Code — source-meta key shape.** `meta_key = f"{source}/{asset_class}/{symbol}/{timeframe}"` (`:786`) re-implements the key format written by `dukascopy_native.py:1171`. Two modules owning one serialization format is Shotgun Surgery waiting for a third writer; extract `source_meta_key(...)`.
- **Duplicated Code — scan scaffolding.** `scan_ohlcv`/`scan_universes`/`scan_fundamentals` (`:732`, `:801`, `:821`) repeat the exists→iterate→stat→error-decorate skeleton three times.
- **Mysterious Name.** `_TIMEFRAME_REGEX` alternation `(?:\d+)?(?:s|sec|m|min|h|d|w|mo)|[smhdw]\d+` at `:35` accepts both `5m` and `m5` forms with no comment on which producers emit which; the double-form acceptance is why downstream needs `normalize_timeframe` everywhere.
- **Import-time side effect.** Module-level `logging.basicConfig(...)` at `:26` configures the root logger on import — surprising for a library module imported by `validate.py` and others; move into `main()`.

### (d) Maintainability hot spots
The filename-parsing heuristics (`:35-45`, `:141-190`, `:234-287`, plus the re-parse patch `_normalize_grouping_entry_from_path :297-328`) encode the repo's entire implicit file-naming grammar in one place — yet each downloader invents its own suffix conventions (see jforex `_build_offer_side_output_path`). A single shared naming module (writer + parser tested together) would remove the most bug-prone code in the area.

---

## validation/reporting/json_report.py (685 lines)

### (a) Bugs / correctness

- **Major — undefined name referenced in public signatures.** `TradeComparisonConfig` appears in the parameter annotations of `_build_trade_delta_report` (`json_report.py:297`), `generate_json_report` (`:360`) and `generate_markdown_report` (`:561`) but is **never imported** in this module (imports at `:17-27` cover `MatchedTrade`, `TradeComparator`, etc.). It works only because `from __future__ import annotations` (`:7`) makes annotations lazy strings; any runtime annotation consumer (`typing.get_type_hints`, doc tooling, pydantic signature introspection) raises `NameError`. Add the import.
- **Minor — dead helper.** `_rel_diff` (`:87-91`) is defined but never called; `_compare_stats` re-implements the identical denominator logic inline at `:170-174`.
- **Minor — markdown path round-trips through JSON.** `generate_markdown_report` serializes the full report to a JSON *string* and immediately `json.loads` it back (`:578-589`) just to get dicts — doubles work on large reports and silently applies the NaN→None sanitizer to numbers displayed in Markdown without noting it.
- **Minor — `stat_summary.passed` semantics.** `passed=1 if all(d.passed ...)` at `:453` counts an **empty** stat_deltas list as passed=1 (vacuous truth); combined with `:485` an empty comparison silently passes stats. Defensible but worth an explicit comment/guard.

### (b) Security
Nothing found: output-only module, `ensure_ascii=False` JSON encoding safe.

### (c) Baseline smells

- **Duplicated Code — relative-diff formula in three modules.** Identical `max(|a|,|b|)` guarded division appears here twice (`_rel_diff :87-91` and inline `:170-174`) and as `_abs_pct_diff` in `readiness/validator.py:23-27`; one shared `numeric_diffs()` util belongs next to the tolerance config.
- **Duplicated Code — stats filtering.** The `allowed_fields = set(TradeStatistics.model_fields.keys()); filtered = {...}` idiom appears at `:60-62` (`_stats_from_reference`) and again verbatim at `:413-415`.
- **Long Function.** `generate_json_report` spans `:353-512` with four interleaved sections; the platform-stats fallback chain (`:402-417`) alone deserves extraction.
- **Repeated Switches / Speculative Generality.** `_extract_risk_deltas` (`:321-350`) duck-types its input via `hasattr(risk_delta_report, "trade_results")` and accepts `Any | None` — the parameter should be typed as the real union; likewise `platform_result`'s three-way isinstance ladder at `:406-411` including an impossible `dict` case (signature says `ValidationReference | list[Trade]`).
- **Primitive Obsession.** `severity` is a free-text `str` on `StatDelta` (`:219`) with magic values "info"/"warning"/"error" scattered across modules — an enum would let the summarizers (`:444-445`) be exhaustive.

### (d) Maintainability hot spots
This module both computes deltas (fallback paths) and renders them — Divergent Change: tolerance-policy edits and report-schema edits land in the same file. Moving the fallback computation into `validation/comparison/` and keeping pure rendering here halves the surface.

---

## validation/readiness/validator.py (631 lines)

### (a) Bugs / correctness

- **Major — required-artifact failure double-counted.** When a required artifact is missing, `validate()` first collects checks via `_check_required_artifacts` (`validator.py:75-79`, producing e.g. `required_artifact_regression_result`, severity error, failed) and then appends a *second* failed error check for the same fact named `required_regression_result` (`:96-106`). Every missing artifact yields two error checks, doubling the reported `errors` count (`:108`) and confusing consumers that key off check names.
- **Minor — non-numeric field failures silently ignored.** In `_check_risk` (`:469-492`), a *failed* `position_size`/`stop_distance` field result whose expected/actual aren't numeric contributes neither an issue nor a check failure — the readiness gate can pass while the underlying comparison flagged a problem.
- **Minor — risk-report-level verdict ignored.** `_check_risk` derives everything from `trade_results`; if the report carries an aggregate pass/fail or summary counts they are never consulted, so an empty `trade_results` list passes both risk checks vacuously (`max(..., default=0.0)` at `:494-495`).

### (b) Security
Nothing found: pure computation over in-memory artifacts.

### (c) Baseline smells

- **Duplicated Code — four copies of one threshold-check shape.** The `sev = "error" if n > cfg.X else ("warning" if n else "info"); passed = n <= cfg.X; append(check); if not passed: recommendations.append(f"...")` block is repeated for trade_errors (`:260-284`), missing (`:287-311`), extra (`:314-338`), critical mismatches (`:341-368`) and again for position size vs stop distance (`:500-544`) and win rate vs net profit (`:583-629`) — nine instances of one shape. Extract `cls._threshold_check(name, description, count, threshold, message_noun, recommendation_fmt)`.
- **Shotgun Surgery (risk).** Adding one new readiness dimension requires touching `validate()`, `_check_*`, and `ReadinessConfig` in lockstep — a list-of-(checker, config-flag) registry would collapse it.
- **Mysterious Name.** Comments say "# Check b" for both missing trades (`:286`) and extra trades (`:313`) — stale lettering from an earlier draft.

### (d) Maintainability hot spots
The class is 630 lines of classmethods with a mutable `recommendations: list[str]` threaded through every private method as an out-parameter (`:76-83`, `:156`, `:200`, …) — Message-chain-ish coupling where each check mutates shared state. Returning `(checks, recommendations)` tuples or collecting checks and deriving recommendations from failed checks afterwards removes the threading entirely.

---

## validation/importers/mt5_xlsx.py (619 lines)

### (a) Bugs / correctness

- **Major — dead branch guarantees `total_return` is never derived.** `_parse_stats` at `mt5_xlsx.py:324` gates on `"initial_capital" in stats`, but `initial_capital` is only ever written to **meta**, never to stats (`_parse_meta :217-219`; `_parse_stats` has no such key). The condition is always false, so `total_return` can never be produced — the feature silently doesn't exist.
- **Major — unpaired entry rows are skipped without record.** In `_build_trades`, an "in" row whose scan finds no "out" (or hits a following "in") leaves via `exit_row is None` at `:492-494` and advances with no log, warning, or count. Open positions and malformed reports lose trades invisibly — for a validation importer, silence here undermines the whole artifact.
- **Major — fees taken only from the exit row.** Commission/swap are read from `exit_row` alone (`:509-510`). MT5 posts entry commission on the *in* row; entry commissions are systematically dropped, understating fees and overstating net PnL in every comparison against a reference that includes them.
- **Minor — direction inferred from `Typ` but `Richtung` required.** `direction_str = "LONG" if typ_entry == "buy"` at `:507`: a sell-to-open row yields SHORT correctly, but any unexpected `Typ` value ("sell limit", localized variant) still maps to SHORT rather than raising; combined with the `direction in ("in","out")` filter at `:465` this can mislabel pending-order fills.
- **Minor — `_to_iso` returns unparsed garbage as timestamp.** At `:598-599` an unrecognized format returns the raw text as the trade's `entry_time`. A malformed cell poisons time-sorted matching downstream instead of failing loudly.
- **Minor — SL/TP map keyed on raw cell objects.** `mapping[oid] = ...` at `:419` keys on whatever pandas parsed (float vs int vs str); lookups at `:524` with `sltp_map.get(order_id)` miss when the two tables type the same order number differently. Normalize to `str/int` first.
- **Minor — `float(val)` may raise on non-numeric meta.** `meta["initial_capital"] = float(val)` at `:219` isn't guarded like `_to_float`; a localized "10 000,00" crashes the import.

### (b) Security
Nothing found: reads local xlsx via pandas; regexes bounded; no external input beyond the workbook itself.

### (c) Baseline smells

- **Primitive Obsession / Repeated Switches.** `_STAT_LABELS` → `_STAT_LABEL_MAP` (`:25-69`) then a chain of `if key == ...` special cases in `_parse_stats` (`:293-317`) dispatching per canonical key; each new stat label needs edits in two places plus a third if it needs custom parsing — fold the parser choice into the table (`(label, key, parser_fn)` triples).
- **Duplicated Code.** The `row[3] if len(row) > 3 and pd.notna(row[3]) else None` extraction repeats five times in `_parse_meta` (`:204`, `:208`, `:212`, `:218`, `:222`) — extract `cell(row, 3)`.
- **Long Function.** `_build_trades` spans `:428-562` mixing column resolution, row pairing state machine, fee math and object construction; extract pairing from assembly.
- **Mysterious Name.** `_extract_table(... end_idx=None)`'s sentinel-based slicing and `__col_N` placeholder columns (`:162-168`) encode sheet-layout assumptions with no comment about which MT5 report versions they hold for.

### (d) Maintainability hot spots
Hardcoded German labels throughout (`Einstellungen`, `Ersteinlage`, `Hebel`, column names) mean one MT5 UI-language change breaks parsing everywhere at once — Shotgun Surgery across three methods. Centralize the label→field vocabulary at module top (as done for stats) or accept a label pack parameter.

---

## validation/importers/mt5_html.py (582 lines)

### (a) Bugs / correctness

- **Major — trade pairing by ID collapses partial closes and re-entries.** `_build_trades` groups all rows by trade id (`mt5_html.py:436-451`), keeping only the **last** "in" and last "out" seen per tid. A position scaled in twice (two "in" rows, same tid) or closed in two parts (two "out") yields one fabricated Trade with the wrong entry/exit pair and silently discards the other fills; the xlsx importer's sequential pairing handles these differently, so the two importers of the *same* report disagree.
- **Major — incomplete pairs dropped silently.** `:461-462` `continue  # Incomplete trade pair` — no log/count, same evidence-loss concern as mt5_xlsx.
- **Major — fees from exit row only.** `:476-477` identical to the xlsx bug: entry-row commission ignored.
- **Minor — `_to_iso` raises on unknown timezone.** `:562-563` `ZoneInfo(source_tz)` unguarded inside the try that catches only `ValueError`; an invalid IANA name raises `ZoneInfoNotFoundError` out of a loop over every cell — while the sibling xlsx module carefully maps it to a `ValueError` (`mt5_xlsx.py:77-80`). Also re-imports `zoneinfo` per call.
- **Minor — settings scan swallows arbitrary "=" cells.** `_parse_meta :224-229` accepts any table cell containing "=" anywhere in the sheet, including order comments like `sl=1.0850`, polluting `settings`.
- **Minor — `_parse_stats` double-parses 4-column layouts.** The stride-2 loop at `:262-265` already covers columns 0-1 and 2-3; the extra `len(cells)==4` block at `:267-269` re-applies the same pairs. Idempotent today only because `_apply_stat` overwrites equal values; any counter-style stat would double.
- **Minor — direction inference mirrors the xlsx flaw** (`:473-474`): non-"buy" `Typ` → SHORT without validation.

### (b) Security

- **Minor — HTML parsed with BeautifulSoup only; good**: no raw regex-on-HTML for structure, text extracted via `get_text`. `errors="replace"` at `:91` prevents decode crashes. No injection surface since output is dataclasses, not HTML.

### (c) Baseline smells

- **Duplicated Code — near-total copy of mt5_xlsx.** `_STAT_LABELS`/`_STAT_LABEL_MAP` (`:24-67`) are byte-identical to `mt5_xlsx.py:25-69`; so are `_coerce_setting`, `_parse_count_pct`, `_extract_first_percent`, `_extract_parenthesized_number`, `_coerce_stat`, `_infer_exit_reason` (`:233-368`, `:573-582`). Two importers = two places to fix every MT5 format change — extract a shared `mt5_common.py` (labels + coercion + exit-reason) and keep only layout parsing per format.
- **Duplicated Code — stat special-casing repeated.** The `winners_with_pct`/`losers_with_pct`/`max_drawdown_*` if-chain (`:283-305`) duplicates `mt5_xlsx.py:293-317` with slight drift already visible (xlsx treats drawdown keys separately).
- **Primitive Obsession.** Rows as `list[str]` with positional access guarded by `col >= len(row)` checks everywhere (`:390-396`, `:537-553`) — a small `Row` wrapper with typed getters would delete the bounds noise.

### (d) Maintainability hot spots
Same label-vocabulary Shotgun Surgery risk as mt5_xlsx, made worse by being a fork rather than shared code. Any third MT5 importer (e.g., English UI) should force the extraction.

---

## data_tools/dukascopy_discovery.py (591 lines)

### (a) Bugs / correctness

- **Major — network failure misread as "no data" corrupts the genesis index.** `probe_single_day` catches broad `Exception` at `dukascopy_discovery.py:145-147`, logs at debug level, and returns False. A timeout/5xx during discovery records a false "miss", and because `_find_start_year/_month/_day` assume availability is monotonic, one flaky probe shifts the discovered genesis date permanently into the saved index (`save_genesis_index`). Should distinguish transport failure (retry/raise) from empty payload.
- **Major — Saturday-only weekend guard.** `_year_probe_date`/`_month_probe_date` (`:150-159`) step back one day only when `candidate.weekday() == 5`. A period ending on **Sunday** (weekday 6) — when FX feeds are empty — is probed as-is, producing a false miss; same for holidays. Genesis dates discovered over such boundaries are wrong by days.
- **Minor — dead probe helpers.** `_looks_like_empty_response` (`:49-51`) and `_is_valid_probe_csv` (`:60-77`) are never called anywhere in the module — leftovers of an earlier CSV-probe implementation (Speculative Generality / dead code).
- **Minor — new event loop + session per probe.** `asyncio.run(download_bi5_files(...))` inside `probe_single_day` (`:141-143`) rebuilds an aiohttp session for every single-file probe; discovery performs dozens-to-hundreds of probes per symbol/timeframe. Batch or reuse sessions.
- **Minor — post-binary-search linear re-scan always runs.** `_find_start_month :294-298` and `_find_start_day :341-350` walk every month/day below the candidate even when the binary search already converged — doubles probe count against a rate-limited server.
- **Minor — `--timeframe` silently overrides `--timeframes`** (`:543`) without warning about the deprecation.

### (b) Security
Nothing found: read-only network probes to the vendor feed, JSON output local.

### (c) Baseline smells

- **Duplicated Code — three logged-probe wrappers.** `_year_has_data_logged`/`_month_has_data_logged`/`_day_has_data_logged` (`:171-176`, `:195-208`, `:211-228`) repeat probe+progress-print with only label/date differing; parameterize.
- **Mysterious Name / data-in-code.** `REFERENCE_GENESIS` (`:39-46`) hard-codes expected values for exactly two symbols in module source — rots immediately and silently stops covering anything else; belongs in a fixture/config file.
- **Global mutable state.** Module-global `VERBOSE_LOGS` flipped in `main()` (`:37`, `:536-537`) — thread-unsafe and untestable; pass a logger/flag down.

### (d) Maintainability hot spots
Imports sibling privates again (`_resolve_symbol_and_instrument` from `dukascopy_native`, `:25-32`) — third module coupled to that file's internals, reinforcing the case for a shared symbols/instruments module.

---

## data_tools/auto_download.py (497 lines)

### (a) Bugs / correctness

- **Major — coverage check reads entire timestamp columns into memory.** `_coverage_for` at `auto_download.py:275` and `_parquet_has_bar_in_range` at `:138` do full `pd.read_parquet(columns=["timestamp"])` per matching file on every call. For tick datasets (10⁷–10⁹ rows) this is minutes of IO and GB of RAM *per symbol per timeframe*, and it runs twice per requirement (`:61-65`, again at `:103`). The catalog already persists min/max timestamps (`catalog_builder.read_parquet_summary`) — consult it instead.
- **Major — coverage satisfied by a single bar.** `_has_required_coverage :247-262` compares only the min/max dates of available data against the required window; a file holding exactly two bars (first/last day) plus a multi-year gap in between passes as "covered", so backtests run on hole-filled data with no download. Gap-awareness (or reuse of `data_tools.validate.gaps`) is needed.
- **Minor — bare-ish excepts around imports/classification.** `_is_forex_symbol` and `_is_dukascopy_symbol` wrap in `except Exception: return False` (`:315-316`, `:325-326`): an import error inside dukascopy_native silently makes every 6-letter symbol "not forex" and reroutes routing to yfinance — wrong data source chosen without any signal.
- **Minor — Alpaca fallback keyed on message text.** At `:432-434` fallback triggers when `"Missing Alpaca credentials" in str(exc)`; brittle string-coupling across modules (rename the message, lose the fallback). Use a typed exception.
- **Minor — HF→Alpaca seam can duplicate or skip bars.** Plan builder `:374-393`: huggingface gets `[start, min(end, 2025-07-31)]`, alpaca gets `[max(start, 2025-07-01), end]`; the overlapping July window (`:22-23`) means both sources contribute the same days, merged later by whatever concatenation order the storage merge uses — duplicated timestamps unless downstream dedupes.
- **Minor — `1w` missing from intraday guard.** `TIMEFRAME_ORDER` includes `1w` (`:19`) but `US_INTRADAY_TIMEFRAMES` doesn't; fine, yet `_resolve_intrabar_timeframe("1d","1-lower")` returns `4h` for US stocks which no US intraday source plan covers beyond yfinance's limits.

### (b) Security
Nothing found: no shell/subprocess, credentials handled inside downloader modules.

### (c) Baseline smells

- **Repeated Switches.** `_download_missing_ohlcv_data` is a five-branch if-chain over source names (`:409-488`), each branch identical except the class instantiated — a `{source: downloader_cls}` registry removes the chain and the repeated kwargs block (five near-identical call shapes).
- **Shotgun Surgery.** Source-selection knowledge (`_is_forex_symbol`, `_is_dukascopy_symbol`, `_is_us_intraday_stock`, date-window tables) duplicates what each downloader module knows about itself; adding a source touches selection logic here *and* the new module.
- **Primitive Obsession / Data Clumps.** `(symbol, timeframe, start, end)` quintuple passed through six functions; `DataRequirement` exists but stops being used below `ensure_backtest_data`.
- **Duplicated Code.** Timestamp-parsing + min/max coverage logic appears twice (`_coverage_for :281-285` vs `has_ohlcv_bars_in_range`/`_parquet_has_bar_in_range :144-147`).

### (d) Maintainability hot spots
Lazy imports scattered through the dispatcher (`:410`, `:422`, `:450`, `:462`, `:474`) hide the dependency graph and defeat static analysis; a top-level source registry keeps circular-import avoidance while restoring discoverability.

---

## data_tools/cache_manager.py (378 lines)

### (a) Bugs / correctness

- **Major — `get_catalog` builds the entire catalog while holding the global lock.** `cache_manager.py:240-255`: `build_catalog(root, ...)` (a full filesystem scan + optional validation runs) executes inside `with _CACHE_LOCK`, so one cold request blocks *every* cache reader/writer process-wide for minutes on large data trees. Compute outside the lock and re-check under it (double-checked locking), or use a per-key building lock.
- **Major — stale-cache race with concurrent downloads.** Reads populate from `catalog.json`/scan (`:248-250`) with no mtime/fingerprint check; a download finishing between a caller's read and use is invisible until someone calls `invalidate_*`. `auto_download._invalidate_catalog_cache` covers its own path, but any other writer (resample CLI run manually) leaves FastAPI endpoints serving pre-mutation catalogs indefinitely.
- **Minor — `unsafe_no_copy=True` returns shared mutable state.** `get_catalog_ohlcv_entries :271-279` hands callers the live list; any caller mutation (e.g., `entry["rows"] = ...`) corrupts the cache for all subsequent readers. The name admits the hazard but nothing enforces read-only use.
- **Minor — stats double-count misses.** In `get_overview_metadata`, an overview miss also triggers `get_catalog` (`:301`) which counts its own miss/hit; `_CACHE_STATS` mixes layers so hit-rate math over-credits.

### (b) Security
Nothing found. (Shared-state discipline is otherwise good: single RLock, deepcopies at the boundary.)

### (c) Baseline smells

- **Duplicated Code — three caches in two dicts with parallel logic.** Catalog vs overview population/invalidation logic mirrors itself across `get_catalog`/`get_overview_metadata`; a small `CacheSlot` class (get/set/invalidate/stats) removes the twin dicts `_CATALOG_CACHE`/`_OVERVIEW_CACHE` (`:27-28`).
- **Primitive Obsession.** `CacheKey = tuple[str, bool]` unpacked by position everywhere (`:223`, `:364-365`); a frozen dataclass key is self-documenting.
- **Middle Man (mild).** `invalidate_after_download`/`invalidate_after_resample` are one-line reason wrappers — fine as API sugar, but they're the only reason values used anywhere; fold reasons into the enum when the third arrives.

### (d) Maintainability hot spots
Stats bookkeeping interleaved into every function makes each change touch all of them — extract a `_record(event)` helper. Otherwise this is the tidiest module in `data_tools`.

---

## validation/risk/comparison.py (425 lines)

### (a) Bugs / correctness

- **Major — extra trades marked `passed=True`.** In `RiskComparator.compare`, unmatched *actual* trades produce `passed=True, severity="info"` (`comparison.py:144-159`). An extra platform trade the reference never took is a real divergence for a validation gate, yet `_build_report`'s `all(r.passed ...)` (`:338`) counts it as passing — a strategy trading symbols/timings absent from the reference can be certified risk-consistent.
- **Major — silent profile-computation failures.** `_compute_profile` swallows every exception and returns None at `:191-192`; the pair then fails with generic "Could not compute risk profile" (`:204-214`) hiding whether it was bad stop data, a sizing-model bug, or NaN prices. Log/record the exception per trade.
- **Minor — `getattr(trade, "stop_loss", None)` shadows meta inconsistently.** `:165-171`: attribute wins over meta even when attribute is stale; more importantly only `stop_loss is None` gates profile computation (`:173-174`) — a trade with stop but no TP proceeds with `take_profit=None`, which percent-risk models may treat differently than fixed-risk (asymmetry between sizing models).
- **Minor — empty comparison passes.** `all(r.passed for r in results)` on zero matched trades (`:338`) yields `passed=True` with "All 0 trade risk profile(s) match" — vacuous pass again; readiness gating downstream should require ≥1 pair.

### (b) Security
Nothing found: pure computation.

### (c) Baseline smells

- **Duplicated Code — relative-diff helper #3.** `_pct_diff :27-32` duplicates `json_report._rel_diff/_abs_pct_diff` in validator.py verbatim; third copy of one formula.
- **Data Clumps / Config sprawl.** `RiskComparisonConfig` mixes tolerance fields, matching config (`time_tolerance_seconds`, `match_symbol`), and sizing params (`account_balance`, `sizing_model`, `model_config`) — three concepts in one bag; matching belongs to `TradeMatcher`'s config type.
- **Middle Man.** `RiskReportBuilder.to_delta_report` exists to re-shape results into `TradeDeltaReport`; reasonable adapter, but `_to_trade_delta` reaching into `DeltaReportBuilder._field_result_to_delta` (another class's private, `:400-401`) is Inappropriate Intimacy — promote that converter to a shared function.

### (d) Maintainability hot spots
Severity strings again ("info"/"warning") as free text across four modules; when readiness logic keys off them (validator does), an enum shared by comparison/risk/readiness would prevent drift.

---

## validation/regression/runner.py (375 lines)

### (a) Bugs / correctness

- **Major — `run_regression` ignores configured time tolerance for matching.** `runner.py:315` calls `TradeMatcher.match(golden_ref.trades, current_ref.trades)` with **no** `time_tolerance_seconds`, while `RegressionConfig.trade_config` may carry `entry_time_seconds`/`exit_time_seconds` (`comparison.py:44` holds those fields). A user tightening/loosening time tolerance via config silently has no effect in the regression path (unlike `json_report._build_trade_delta_report:300-303`, which passes it). Config-vs-behavior divergence.
- **Minor — `TradeComparator` re-instantiated per trade.** `:320-321` inside the loop; harmless but wasteful, and inconsistent with hoisting elsewhere.
- **Minor — golden stats recomputed rather than stored.** `:332-333`: if the golden file's recorded statistics differ from what recomputation yields over its trades, the regression compares recomputed-vs-recomputed and can never detect corruption of the stored stats block. Consider comparing against `golden_ref.stats` when present.
- **Minor — vacuous pass on empty inputs** again at `:340-341` (`all([]) == True`) — same family as risk/comparison.

### (b) Security
Nothing found: reads local JSON; `model_validate_json` on trusted-ish files only. Note `source_path` resolution at `:304-306` will happily read any absolute path embedded in a golden file — acceptable for a local dev tool, worth noting if goldens are ever shared.

### (c) Baseline smells

- **Duplicated Code — `_compare_stats` is json_report's twin.** `:78-157` duplicates `json_report.py:_compare_stats :124-204` nearly line-for-line (same field list copy-pasted at `:22-33` vs `json_report.py:65-76`), differing only in tolerance source and delta type. One parameterized comparator serving both would remove ~120 lines and guarantee consistent pass semantics.
- **Duplicated Code — relative-diff formula 4th copy** inline at `:125-129`.
- **Long Function.** `run_regression` spans `:262-375` mixing loading, matching, comparison, aggregation and explanation assembly.
- **Primitive Obsession.** `deviations: list[Any]` (`:184`) — typed as the union it actually holds (`TradeDelta | FieldDelta`).

### (d) Maintainability hot spots
The runner/reporting pair (`RegressionReporter`) plus duplicated stat-compare logic means a tolerance-semantics fix must land here *and* in `json_report.py` — Shotgun Surgery already realized (see drift between `passed = passed or ...` OR-semantics in both).

---

## validation/comparison/delta_report.py (332 lines)

### (a) Bugs / correctness

- **Minor — mismatched-trade deltas counted twice in `total_matched`.** `build` appends every comparison result to `matched_trades` (`delta_report.py:254-256`) *and* anomalies of type MISMATCHED_TRADE are converted but not bucketed (`:261-268`: only missing/extra/duplicate_match handled), so a mismatched pair appears once as a matched delta — while `json_report` counts `mismatched` from `anomaly_type is not None`. Consistent, but the `duplicate_match` special-case appended into `matched_trades` (`:267-268`) inflates `total_matched` for duplicated matches.
- **Minor — `_rel_diff`/`_abs_diff` recomputed here though `FieldComparisonResult` already carries them.** `:127-131` re-derives diffs instead of trusting the comparator's values; if the comparator's tolerance logic and this recomputation ever disagree (rounding), the explanation text contradicts the pass flag.
- **Minor — severity hardcodes "direction"/"pnl" as error fields.** `:161-163`: the error/warning split lives in a local set; adding an error-level field elsewhere requires editing here too (Shotgun Surgery with `comparison.py`'s field list).

### (b) Security
Nothing found: pure model construction.

### (c) Baseline smells

- **Duplicated Code — relative-diff formula 5th copy.** `_rel_diff :31-35` identical to json_report/risk/validator copies.
- **Repeated Switches.** `_anomaly_to_delta` is a four-way if-chain over `AnomalyType` (`:183-237`) where each branch builds nearly the same TradeDelta; a dispatch map `{AnomalyType: extractor}` collapses it, and the fall-through at `:234-237` returns a delta with no ids/type — silent swallow of unknown anomaly types.
- **Long Function.** `build` (`:240-332`) mixes bucketing, pass computation, summary stats, and prose generation — four reasons to change.

### (d) Maintainability hot spots
This module is the schema hub every other validation module imports (`TradeDeltaReport`, `FieldDelta`) — good — but the builder also owns policy (what counts as error vs warning, what fails the report). Splitting models from `DeltaReportBuilder` would let importers depend on shapes without importing policy.

---

## data_tools/download_grid_market_data.py (357 lines)

### (a) Bugs / correctness

- **Major — `normalize_symbol` applied twice, corrupting the plan for aliased inputs.** `main` builds `planned` with `requested_symbol=requested` and `canonical_symbol=normalize_symbol(requested, ...)` (`download_grid_market_data.py:280-287`) — correct — but then `summary["requested_symbols"] = symbols` (`:336`) reports already-canonicalized names as "requested", and if a user passes `SP500` twice under different aliases (e.g. `sp500,US500`) both planned entries canonicalize to `US500` and run concurrently in the process pool (`:294-311`), racing on the same `canonical_dir/1m.parquet` write (`:209-226`). No dedupe of canonical targets before fan-out.
- **Major — alias copies can serve stale/divergent data.** `ensure_alias_copy :155-161` skips the copy when file sizes match; two different datasets can share a size (same row count, different content after a re-download), silently keeping an outdated alias. Compare mtime+size or hash.
- **Minor — full-file parquet rewrite in worker loop.** `normalize_ohlcv_columns :96-126` reads and rewrites the entire 1m dataset per symbol; combined with `--workers 6`, six concurrent rewrites of multi-GB files spike memory. Also not atomic — a crash mid-`to_parquet` corrupts the cached 1m source that `source_path.exists()` trusts.
- **Minor — resample cache check loads two full columns.** `:232-234` calls `parquet_non_null_ohlcv` + `parquet_rows` (two full reads) just to decide validity; one metadata read would do.
- **Minor — `rows_1m` counted from the alias copy but errors swallowed upstream.** `parquet_non_null_ohlcv` returns 0 on *any* exception (`:140-141`), so a corrupt file reads as "zero valid rows" and triggers needless re-resample rather than surfacing corruption.

### (b) Security
Nothing found: local orchestration over the existing downloader.

### (c) Baseline smells

- **Duplicated Code — timeframe vocabulary copy #4.** Local `TIMEFRAME_ALIASES` (`:50-64`), `TIMEFRAME_ORDER` (`:36`), and `VALID_TIMEFRAMES` (`:164`) duplicate `shared.constants.normalize_timeframe`'s job plus auto_download's `TIMEFRAME_ORDER`; the comment at `:34-35` even admits manual sync is required — classic Shotgun Surgery, already bitten once per the `_timeframe_sort_key` docstring (`:84-89`).
- **Duplicated Code — executor branch duplication.** The ProcessPool vs serial paths repeat the identical 11-kwarg call (`:295-309` vs `:315-330`); build the kwargs dict once.
- **Primitive Obsession.** `result["paths"]` mixes path entries with `"rows_1m"` integer entries keyed into the same dict (`:227-228`) — two value types in one map; separate `paths`/`row_counts`.

### (d) Maintainability hot spots
Symbol canonicalization logic here overlaps `_resolve_symbol_and_instrument` in dukascopy_native (a third spelling of "normalize this symbol"); unify in shared constants/symbols module.

---

## data_tools/downloaders/alpaca_dl.py (352 lines)

### (a) Bugs / correctness

- **Minor — resume-start can shrink the requested window.** `alpaca_dl.py:218`: `self._resolve_resume_start(out_path, start) or start` — when `start` is None but a file exists, resume uses the stored last timestamp (good); but when an explicit earlier `start` is given than what's on disk, `_resolve_resume_start` presumably returns the later stored timestamp, silently *ignoring* the caller's explicit start. Verify semantics; either way the `or start` fallback makes intent ambiguous.
- **Minor — pagination loop unbounded on server token churn.** The `seen_tokens` guard (`:259-262`) stops repeated tokens, but a server returning always-new tokens for the same page loops forever; add a max-page cap.
- **Minor — CLI catches broad Exception per pair** (`:342-345`) and continues; acceptable for batch UX but hides auth errors among symbol errors in exit-code accounting.
- **Minor — empty pages silently tolerated; "no data" only detected after full pagination** (`:265-274`) — fine, but with `limit=10_000` a delisted symbol burns many empty requests before failing.

### (b) Security
Credentials read from env with two vendor spellings (`:74-85`), never logged; client built over TLS SDK. Nothing found.

### (c) Baseline smells

- **Duplicated Code — TIMEFRAME_MAP copy #5** (`:24-37`), same alias table as every other downloader.
- **Repeated Switches.** `_to_alpaca_timeframe` if-chain (`:109-121`) mapping canonical tf → vendor enum is exactly the shape a dict `{tf: TimeFrame}` removes.
- **Primitive Obsession.** Timestamp-column sniffing tuple (`"timestamp","time","datetime","t"` `:159`) duplicates catalog_builder's `_select_timestamp_column` list — one shared helper.

### (d) Maintainability hot spots
Clean module overall; main debt is again the per-source timeframe/column vocabulary duplication.

---

## data_tools/downloaders/yfinance_dl.py (349 lines)

### (a) Bugs / correctness

- **Major — `4h` built by resampling 60m bars, misaligning session boundaries.** `yfinance_dl.py:53` maps both `1h` and `4h` to interval `"60m"`, then resamples to `4h` at `:253-269`. Yahoo's 60m bars align to exchange sessions (09:30 ET start), so pandas' default `4h` origin (midnight UTC) produces buckets split 09:30/13:30/17:30/21:30 rather than exchange-aligned 4-hour bars — every stored `4h` candle for yfinance-sourced symbols is offset versus the dukascopy/jforex `4h` files the platform compares against. Should anchor the resample (`origin="start"` or offset) or fetch native 1h and aggregate deliberately.
- **Minor — intraday clip silently truncates requested history.** `_clip_intraday_window :84-112`: a request for 2020→today at `5m` is clipped to ~60 days with only a warning string; combined with auto_download's coverage check (min/max based), the truncated file then *satisfies* nothing, but the DownloadResult carries `warnings` that CLI prints while library callers (`auto_download._download_missing_ohlcv_data`) drop on the floor — no propagation into logs there.
- **Minor — resume path double-clipped.** `:169-183`: resume start goes through `_clip_intraday_window` twice (once via bounded_start logic, once explicitly); harmless but convoluted — the second call re-derives an identical bound.
- **Minor — MultiIndex flattening takes `column[0]`.** `:213-217`: when yfinance returns `(field, ticker)` columns this keeps the field; if grouped by ticker instead it silently keeps tickers as column names and the OHLCV check at `:239-240` fails with a misleading "missing required OHLCV columns".

### (b) Security
Nothing found: public API, no credentials.

### (c) Baseline smells

- **Duplicated Code — TIMEFRAME_MAP copy #6** (`:31-49`) plus its own `YF_INTERVAL_MAP`.
- **Duplicated Code — `_to_utc_timestamp` byte-identical to alpaca_dl's** (`:73-81` vs `alpaca_dl.py:52-60`); also `_parse_csv_list`, output-path builder, CLI skeleton. The three "simple downloader" modules (alpaca/yfinance/huggingface) share ~40% scaffolding — extract a base class helper set.
- **Primitive Obsession.** Timestamp-column candidate tuple again (`:223`).

### (d) Maintainability hot spots
The 4h resample special case embedded mid-download is the kind of source-specific quirk that belongs beside the other timeframe mappings in shared constants, not inline in a method.

---

## data_tools/downloaders/huggingface_dl.py (370 lines)

### (a) Bugs / correctness

- **Major — failed months silently skipped, producing gappy datasets that pass coverage checks.** `huggingface_dl.py:257-261`: any exception reading a monthly parquet (transient 5xx, rate limit) becomes a warning string and the loop continues; the merged file then has month-shaped holes while `auto_download._has_required_coverage` (min/max based) still considers the range covered. A validation-grade downloader should fail or retry on transport errors and reserve skip for genuine 404s.
- **Minor — full remote parquet downloaded then filtered in memory.** `_normalize_month_frame` filters per symbol *after* `pd.read_parquet(url)` (`:258`, filter at `:152`); monthly all-symbol 1m files are large — pyarrow's `filters=` parameter would push the predicate down.
- **Minor — resample before dedupe across month seams.** `:277-283`: concat/dedupe happens first, good, but bars at month boundaries from different vintages keep="last" arbitrarily by sort order rather than source precedence.
- **Minor — `RESAMPLE_RULES["1d"]="1d"`** (`:54`) resamples daily→daily no-op; harmless but suggests copy-paste.

### (b) Security
- **Minor — URL built from f-string template with fixed constants only** (`:27-29`) — safe. `pd.read_parquet(url)` fetches over HTTPS without integrity verification; consistent with other sources here but worth noting since this is third-party-published data entering the research pipeline unverified.

### (c) Baseline smells

- **Duplicated Code — TIMEFRAME_MAP copy #7**, `_to_utc_timestamp` third byte-identical copy (`:68-76`), same CLI skeleton as alpaca/yfinance.
- **Primitive Obsession.** Column-candidate tuples again (`:140-149`).

### (d) Maintainability hot spots
Same scaffolding-sharing opportunity as alpaca/yfinance; nothing structural beyond that.

---

## data_tools/validate.py (318 lines)

### (a) Bugs / correctness

- **Major — gap detector mislabels index positions after `dropna`.** At `validate.py:132-137` `ts` is built then `dropna()`'d; the gap loop at `:166-170` uses `ts.iloc[idx - 1]` where `idx` is a **label** from `diffs.index` — for a default RangeIndex these coincide, but if the parquet's index is non-contiguous (e.g. previously filtered file), `iloc[idx-1]` fetches the wrong row and reports shifted gap boundaries. Use positional `.loc`/`get_indexer` deliberately.
- **Major — OHLC violations counted but never escalate status.** `:206-208`: violations append only a *warning*, so a file with 10k crossed candles still reports status "warning" and catalog validation shows non-error; combined with `_run_validation` in catalog_builder treating any non-"error" as passable, corrupt price data flows through.
- **Minor — weekend-skip logic misses Friday→Tuesday holidays and intraday Friday gaps.** `:173-178`: Fri(4)→Mon(0) skipped wholesale, so a *missing Monday* (holiday with no bars) is never flagged as suspicious even though the gap spans 3+ days; also equity half-days produce false positives under fixed thresholds.
- **Minor — duplicate detection on dropped-NaT series** (`:150`) counts duplicates of parseable timestamps only; NaT rows vanish silently rather than being reported as unparseable duplicates.
- **Minor — module-level `logging.basicConfig` again** (`:29`) — same import-side-effect issue as catalog_builder.

### (b) Security
Nothing found: local files only, JSON report write is caller-controlled path.

### (c) Baseline smells

- **Primitive Obsession.** Timeframe→threshold knowledge duplicated here (`GAP_THRESHOLD_MINUTES :46-54`, `EXPECTED_BARS_PER_DAY :35-43`) vs every downloader's maps; note `EXPECTED_BARS_PER_DAY` is defined but **never used** — dead config (Speculative Generality).
- **Mysterious Name.** The "XAUUSD issue" comment at `:143` references tribal history without explaining the failure mode (epoch seconds stored as ms?) — encode it as a named check instead.

### (d) Maintainability hot spots
`validate_parquet` is a well-shaped single-responsibility function — one of the cleaner modules; its main risk is that its verdicts feed the catalog cache with no severity policy shared with readiness gating.

---

## data_tools/downloaders/histdata_dl.py (294 lines)

### (a) Bugs / correctness

- **Major — year failures silently skipped; partial history stored as complete.** `_download_year` returns None on any error (`histdata_dl.py:224-226`) and `download` proceeds with whatever years succeeded (`:124-131`). A 2020-2025 request where 2022 fails yields a file with a permanent hole; resume logic keys off the *last* timestamp so the hole is never back-filled. Track failed years in `DownloadResult.warnings` at minimum.
- **Minor — resume-start filter applied after resample decision but before merge** — fine — but `combined["timestamp"] <= end` compares Timestamp against a raw string (`:144`) relying on pandas coercion; with mixed types this can raise or silently compare lexicographically in future pandas versions.
- **Minor — `zf.namelist()[0]` assumes first entry is the CSV** (`:208`) — a directory entry would crash the read; filter by `.csv`.
- **Minor — HTML-scraped form token regex** (`:184`) breaks silently on vendor page redesign (already returns None → silent skip per above).
- **Minor — `import re` inside the function** (`:182`) per-year loop; trivial but odd.

### (b) Security
- **Minor — scraping flow posts tokens from fetched HTML** (`:192-199`) — standard for this vendor, no eval/injection risk; timeouts present (`:174`, `:200`) satisfying the subprocess/timeout checklist analog for requests. Nothing else found.

### (c) Baseline smells

- **Duplicated Code — RESAMPLE_MAP + `_resample` copy #8.** Identical aggregation dict appears in dukascopy_native, jforex (via resample_ohlcv), yfinance, huggingface, histdata — one shared `resample.py` helper exists (`data_tools.resample.resample_ohlcv`) that this module ignores.
- **Speculative Generality (mild).** `AVAILABLE_PAIRS` includes 27 pairs while docstring says "subset: 12"; list drift unverified against vendor.

### (d) Maintainability hot spots
The scraping steps (GET token → POST → unzip → parse) inline in one method make retry/backoff policy impossible to add without rewriting; extract per-step methods when touching next.

---

## data_tools/downloaders/ctrader_dl.py (298 lines)

### (a) Bugs / correctness

- **Major — tick data silently resampled by minute-bucket rules.** `ctrader_dl.py:156-157`: when `timeframe == "tick"` no resample occurs, but the loaded CSV's rows are stored as-is; however for `timeframe == "1m"` no resample happens either (`:156` excludes it) even if source CSV is *tick* granularity — a tick CSV imported with default `--timeframe 1h` gets resampled, but with `--timeframe 1m` raw ticks are stored under a `1m.parquet` name. Data labelled 1m that is actually ticks corrupts every downstream bar-based consumer.
- **Minor — separator sniffing accepts first parse with ≥5 columns.** `_load_csv :181-189`: a semicolon file whose first comma-parse yields one wide column plus junk can still pass if pandas splits ≥5; no header validation that columns look like OHLCV.
- **Minor — string date comparisons again** (`:151-153`) — same pandas-coercion fragility as histdata.
- **Minor — `matches[0]` picks arbitrary CSV** when several match a symbol glob (`:128-131`) — e.g. `EURUSD_tick.csv` vs `EURUSD_2024.csv`; no mtime/recency preference and no error on ambiguity.

### (b) Security
Nothing found: local CSV import only.

### (c) Baseline smells

- **Duplicated Code — RESAMPLE_MAP + `_resample` copy #9**, byte-identical to histdata's.
- **Speculative Generality.** The class advertises cTrader Open API support in its docstring (`:2`, `:66-71`) but `download` raises "not implemented" unless CSV paths given (`:133-136`) — the API half doesn't exist.
- **Primitive Obsession.** Timestamp-candidate list copy #4 (`:196`).

### (d) Maintainability hot spots
Small module; main issue is being a near-duplicate of histdata/yfinance scaffolding with an unimplemented core path.

---

## validation/comparison/comparison.py (292 lines)

### (a) Bugs / correctness

- **Major — PnL passes on *either* tolerance (OR semantics).** `comparison.py:282`: `passed = passed_pct or passed_abs` — a $10,000 PnL difference between reference and platform passes because it's within 0.1% relative; conversely a 0.001% diff of a huge PnL fails the absolute check but passes relative. For a validation gate, "either" is the wrong default for money fields (the stat comparators use OR too but across abs *or* rel of the same magnitude — here the two tolerances are conceptually different currencies). At minimum this deserves an explicit config flag.
- **Minor — `_normalize_timestamp` assumes ISO strings.** `:17-23`: `datetime.fromisoformat` on any non-ISO entry_time raises ValueError out of the comparator, aborting the whole comparison run rather than failing that pair. Importers mostly normalize, so exposure is limited to hand-built references.
- **Minor — exit price compares only the last exit** (`:127-128`) — multi-exit trades compare final legs only, silently ignoring earlier partial exits.
- **Minor — direction compared as raw strings** (`:175-185`): "LONG"/"long"/"Buy" variants mismatch though semantically equal; importers normalize today, but nothing enforces it.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Duplicated Code — `_compare_optional_pct` and `_compare_optional_price` are identical** (`:217-242` vs `:244-269`). Delete one.
- **Duplicated Code — `_pct_diff`/`_abs_diff` copy #6/#7** of the shared formula.
- **Long Parameter List / Data Clumps.** Every `_compare_*` threads `(expected, actual, tolerance, field_name)`; a tiny `FieldSpec(name, tolerance)` bundle would halve signatures.
- **Repeated Switches (mild).** `compare()` is a fixed 9-field script (`:108-163`); adding a compared field means editing this method, delta_report's error-field set, and readiness checks — three places.

### (d) Maintainability hot spots
The tolerance defaults here (e.g. `entry_time_seconds=60`) silently govern every validation run that doesn't pass explicit config — including `json_report`'s fallback path where `trade_comparison_config=None`. Centralizing defaults in one documented place matters more than usual.

---

## validation/cli.py (259 lines)

### (a) Bugs / correctness

- **Major — markdown path runs the entire validation pipeline twice.** `cli.py:237-250`: for the default format the report is generated, then `generate_json_report` re-runs matching/comparison/anomaly/stat pipelines from scratch just to read the `passed` flag (the comment admits it). Doubles runtime on large trade sets and can diverge if generation is nondeterministic.
- **Minor — `pnl_pct` bound to `--stats-tolerance`.** `:190`: the CLI maps "statistics relative tolerance" onto *per-trade* PnL relative tolerance (`TradeComparisonConfig.pnl_pct`), conflating portfolio-level stat tolerance with trade-level PnL tolerance; a user tightening stats to 0.0001 unknowingly tightens every trade's PnL gate.
- **Minor — importer applies to both files with no per-file override.** `_load_validation_reference(platform_path, args.importer)` (`:174`): a JSON reference + MT5 xlsx platform requires `--importer auto` to work only because extension sniffing rescues it; explicit `--importer mt5_html` then mis-parses the JSON side (falls through to `mt5_html` loader on `.json` suffix → BeautifulSoup on JSON text → confusing error).
- **Minor — exit-code semantics conflate load failure (2) with validation failure (1) correctly**, good — but report-generation failure also returns 2 while writing output failure returns 2, losing the distinction for automation.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Duplicated Code — the four-argument `generate_*_report(...)` call repeated three times** (`:197-204`, `:206-213`, `:241-248`). Build kwargs once.
- **Duplicated Code — importer dispatch if-chain** (`:58-63`) will grow with each new importer; an entry-point registry is the usual fix.

### (d) Maintainability hot spots
Exit-status derivation by re-running the pipeline is the hot spot; have `generate_markdown_report` return `(text, passed)` or accept a pre-built `ValidationReport`.

---

## validation/statistics/cross_platform.py (316 lines)

### (a) Bugs / correctness

- **Major — hard-coded case table defeats the "auditable matrix" purpose.** `cross_platform.py:68-98`: the three MT5 cases and one QC case are literal paths inside the function. Any new strategy requires editing this module (Shotgun Surgery), and a missing artifact is only *reported* (`:167-171`), never distinguished from a stale row — the matrix silently ships rows whose inputs vanished.
- **Major — `_build_mt5_row` crashes on missing reference instead of emitting a row.** `:114`: `MT5XLSXImporter().load(...)` raises FileNotFoundError for a missing xlsx, aborting `build_comparison_matrix` entirely — yet `_row_payload` carefully supports `missing_artifacts`, which can then never be populated for MT5 cases. Dead codepath / inconsistent contract.
- **Minor — win-rate heuristic misfires on 0–100 scale edge.** `:218-220`: `win_rate > 1.0 → /100` treats a legitimate 1.0 (100% wins, already fractional) correctly but would mangle a 2-win-2-trade count stored under `win_rate`; acceptable, undocumented.
- **Minor — relative delta denominator convention differs** from every other module: here `relative = absolute / max(|ref|,|plat|)` (`:194-195`) — same as elsewhere actually; but no zero-denominator special-case when both are 0 and absolute ≠ 0 (impossible since absolute=0 then) — fine; noting checked.
- **Minor — lazy intra-function import** `from runner.stats import compute_stats_for_result` (`:241`) couples validation to runner package at call time; failure surfaces mid-matrix-build.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Divergent Change.** The module owns metric normalization, platform-stat policy (`_prefers_stored_platform_stats :262-265` tag-based switches), MT5 period scraping, and report assembly — four reasons to change.
- **Repeated Switches.** Tag/engine-based policy via string matching (`"qc-parity" in tags`, `"python-reimplementation"` `:263-265`, `:273-277`) — magic strings duplicated across two functions; extract constants or config.
- **Primitive Obsession.** `stats: dict[str, float | None]` with per-key fallback ladders (`_first_metric`) — a typed Metrics dataclass would remove the key-string guessing.

### (d) Maintainability hot spots
Case definitions belong in a YAML/JSON fixture next to the artifacts; code should iterate.

---

## validation/comparison/anomalies.py (244 lines)

### (a) Bugs / correctness

- **Minor — extra trades severity "info" contradicts risk module's treatment.** `anomalies.py:146`: EXTRA_TRADE is "info" here while `risk/comparison.py` treats unmatched actual trades as passed and `delta_report.build` fails the whole report on *any* extra (`:273-275`). Three modules, three opinions on the same event — a gate-consistency hazard.
- **Minor — `match_kwargs` re-matching trap.** `detect_missing`/`detect_extra` each accept their own `**match_kwargs`; callers passing different kwargs to `detect()` than to individual detectors get inconsistent anomaly sets. The `match_result=` escape hatch exists (`:71-72`) but nothing enforces its use.
- **Minor — duplicate detection keyed on `trade.id`.** `:192-197`: ids are importer-assigned sequence numbers (mt5 importers number 1..N); two *different* trades legitimately share id spaces across reference/platform, which is handled (separate dicts), but within one side an id collision from a bad importer masks as "matched N times".

### (b) Security
Nothing found.

### (c) Baseline smells

- **Duplicated Code.** `_normalize_timestamp` third copy (`comparison.py:17`, `matcher.py:19`) — this one is the canonical location; the others should import it.
- **Duplicated Code — error_fields set duplicated** (`:240` vs `delta_report.py:161`): `{"direction","pnl"}` appears twice; drift here means delta severity and anomaly severity disagree for the same mismatch.
- **Repeated Switches (mild).** The four detect_* methods repeat match→iterate→append shape with only message/severity differing.

### (d) Maintainability hot spots
Severity policy ("which fields are error-level") is spread across anomalies.py, delta_report.py, comparison.py and readiness/validator.py — single-source it.

---

## validation/comparison/matcher.py (164 lines)

### (a) Bugs / correctness

- **Major — greedy first-match pairing mispairs simultaneous trades.** `matcher.py:126-158`: for each reference trade the first unmatched actual satisfying tolerances wins. With two identical entries at the same minute but different exits (scale-in/scale-out), reference #1 can consume actual #2 whose exit differs, producing both a false mismatch *and* cascading missing/extra. Sorted best-match (or Hungarian on time distance) would be stable; at minimum prefer minimal exit-time distance among candidates.
- **Minor — O(N·M) scan.** `:128-145` linear inner loop per reference trade; 10k×10k trade sets = 10⁸ comparisons. Sort-by-entry + bisect window makes it near-linear.
- **Minor — tolerance applies to exit even when exit is NaT-ish empty string.** `_normalize_timestamp("")` raises (`:28`), aborting matching for trades without exits rather than treating them as open positions.
- **Minor — MatchKey records *reference* times only** (`:150-155`) — the key claims both sides matched at ref's timestamps; fine for display, misleading for audit ("key" implies shared values).

### (b) Security
Nothing found.

### (c) Baseline smells

- **Primitive Obsession.** Dicts-as-structs in `ref_data`/`act_data` (`:104-120`) with string keys `"trade"/"entry"/"exit"/"matched"` — a small dataclass removes typo risk.
- **Mysterious Name.** `options.get("time_tolerance_seconds", 0)` — untyped `**options` hides the two real knobs from signature; make them keyword params with defaults.

### (d) Maintainability hot spots
The greedy matcher is the correctness core of the entire validation area and is also the least defensive file in it; worth hardening before any performance work.

---

## data_tools/downloaders/dolthub_dl.py (248 lines)

### (a) Bugs / correctness

- **Minor — SQL built by f-string interpolation.** `dolthub_dl.py:96-102`: `act_symbol = '{symbol}'`, dates interpolated raw; `table` interpolated into backticks. Symbol comes from the caller/CLI, so it's an internal tool — but a symbol like `AAPL' OR '1'='1` silently rewrites the query, and any future exposure (web endpoint calling this) makes it injection. Parameterize or at minimum escape quotes.
- **Major-ish correctness — pagination ignored.** DoltHub's API caps rows per response; this reads `data.get("rows")` once (`:145`) with no `next_page_cursor`/offset handling, so large histories come back truncated yet are stored as complete datasets and merged into canonical files. Silent truncation is the worst failure mode for market data.
- **Minor — `data` may be unbound.** If all retries exhaust via the `RequestException` path (`:135-136`) the raise happens there, fine — but if `max_retries` were 0 the loop never assigns `data`; latent NameError shape.
- **Minor — custom `--query` results stored under symbol "UNKNOWN"** (`:236`) with equity asset class regardless of repo (`:91`, `:189`) — mislabeled artifacts.

### (b) Security
The SQL string building above is the finding; otherwise requests with timeouts (`:113`) — good.

### (c) Baseline smells

- **Primitive Obsession / Repeated Switches.** Column-name mapping if-chain (`:157-164`) repeats the candidate-list pattern from four other modules.
- **Speculative Generality.** `KNOWN_REPOS` includes an options repo whose schema doesn't fit the OHLCV path at all (`:45-53`) — unreachable via the current pipeline.

### (d) Maintainability hot spots
Pagination + parameterization are the two structural gaps; everything else matches sibling downloaders.

---

## data_tools/dolthub_manager.py (217 lines)

### (a) Bugs / correctness

- **Critical — subprocess without timeout.** `dolthub_manager.py:32` `subprocess.check_call(cmd, cwd=cwd)` and `:60-65`, `:77`, `:111-116`, `:133-144`: **no `timeout=` on any subprocess call** — a hung `dolt` clone/pull/export blocks forever (checklist item literally violated). A stuck network mount or dolt lock wait hangs CI silently.
- **Major — SQL built by f-string with symbol interpolation.** `:112`, `:138`: symbol strings from the repo's own table feed the query; a symbol containing a quote breaks or rewrites the query. Same family as dolthub_dl; here symbols come from `SELECT DISTINCT act_symbol` output, so data-driven injection into a shell-executed CLI.
- **Minor — temp CSV left behind on failure between write and finally** (`:131-155`): the `finally` cleans `{symbol}.csv`, good, but `_export_table_csv` (`:76-77`) has no cleanup on exception for earnings/rates paths when `pd.read_csv` fails after export — actually `csv_path.unlink()` at `:175/:197` is inside try after read; a read failure skips unlink, leaking CSVs.
- **Minor — tz-naive timestamps stored.** `:148` `pd.to_datetime(df["timestamp"])` without `utc=True` writes naive local-ish stamps while every other module normalizes to UTC — inconsistent canonical files.
- **Minor — `resolve_executable` imported but unused; commands hardcoded as `"dolt"`.** If dolt isn't on PATH the failure is a raw FileNotFoundError per call.

### (b) Security
Subprocess calls use list form (no shell) — good; the risk is absence of timeouts plus data-driven query text passed to that subprocess's SQL layer.

### (c) Baseline smells

- **Duplicated Code.** Earnings and rates export blocks are copy-paste twins (`:158-177` vs `:179-199`) differing only in repo/dir names — one loop over a spec table.
- **Long Function / Divergent Change.** `export_data` spans `:93-201` handling three repos with three layouts.

### (d) Maintainability hot spots
Add `timeout=` to every subprocess call before anything else; then unify the three export branches.

---

## data_tools/migrate_csv.py (229 lines)

### (a) Bugs / correctness

- **Minor — sibling-project hard-coded absolute paths.** `migrate_csv.py:30-32`: reads `~/Desktop/custom_lean_integration/...` and `~/Desktop/stockkit_ai_bot/...` — outside this repo, undocumented assumptions about machine layout; fails opaquely elsewhere.
- **Minor — yfinance metadata-row sniffing heuristic fragile.** `_convert_ohlcv :145-157`: treats any first-row value where `first_val.isupper()` as metadata (so a ticker-like first *data* row is dropped); loop caps skip at 3 rows regardless of actual metadata depth.
- **Minor — dest-exists check races with partial writes.** `:129-131`: skip-if-exists means an interrupted earlier run leaves a truncated parquet that permanently blocks re-migration until manually deleted.
- **Minor — rows lacking timestamp column are still written** (`:178-180`, `:189-193`): `keep` omits timestamp gracefully but produces OHLC-only parquet indistinguishable from proper ones in catalog (timeframe detection then defaults to "1d" per catalog_builder).

### (b) Security
Nothing found beyond reading out-of-repo paths.

### (c) Baseline smells

- **Duplicated Code.** Five `migrate_*` functions repeat glob→derive-symbol→dest→convert; a table of `(pattern, source_kind, dest_template)` would collapse them.
- **Mysterious Name.** `datetime_col` vs `timestamp_col` parameters (`:124-126`) differ only in precedence — non-obvious.

### (d) Maintainability hot spots
One-shot migration tool; acceptable debt, but its dest-collision behavior interacts badly with the "1d default" labeling bug in catalog_builder (two bugs compounding).

---

## data_tools/signal_import.py (201 lines)

### (a) Bugs / correctness

- **Major — `_parse_action` defaults every unrecognized action to "buy".** `signal_import.py:67-77`: a typo'd, empty, or localized side ("kauf", "cover", "exit_long") silently becomes a long entry. For signal replay into a trading pipeline this is the most dangerous default possible; unknown values should be rejected or at least logged.
- **Minor — timeframe inferred from presence of time-of-day.** `:113`: a daily bar stamped 00:00:00 → "1d", but an actual midnight intraday signal also maps to "1d"; conversely daily bars with 16:00 stamps become "1m". Inference without a source-format hint is guesswork — accept explicit timeframe or document the heuristic.
- **Minor — NY-time detection by substring.** `:91`: `"ny" in header` matches "ny" anywhere (e.g. "many") though normalized headers make it unlikely; brittle locale coupling regardless.
- **Minor — separator detection on line one only** (`:33-37`): quoted CSVs with commas inside fields can out-count semicolons; acceptable heuristic, unflagged.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Primitive Obsession.** Signals are loose dicts (`:188-198`) consumed via string keys downstream; a TypedDict/dataclass would lock the contract with `ReplaySignal.from_payload`.
- **Mysterious Name.** `_TIME_KEYS` mixes languages and semantics ("zeit_ny" encodes timezone in a column-name candidate).

### (d) Maintainability hot spots
Small and focused; main risk is the buy-default above.

---

## data_tools/resample.py (198 lines)

### (a) Bugs / correctness

- **Major — no guard against resampling to a *lower or equal* timeframe.** `resample.py:41-75`: `resample_ohlcv` happily "resamples" 1h→1m (producing sparse rows) or 1d→1d; `TIMEFRAME_ORDER :38` exists but is never consulted. Combined with grid tooling that trusts file names, a wrong-order call silently overwrites e.g. `5m.parquet` with data derived from 1h.
- **Minor — output written next to source by default.** `resample_file :100-104`: writes `{tf}.parquet` into the source directory, so running with `--from 1m --timeframes 1m` (or a caller passing the source tf in the list) **overwrites the source file**. No self-target check.
- **Minor — non-atomic parquet write again** (`:106`) — crash mid-write corrupts a canonical timeframe file.
- **Minor — `update_catalog` swallows all exceptions as warnings** (`:149-150`), leaving stale catalog after mutation while reporting success of the resample.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Duplicated Code — TIMEFRAME_MAP copy #10 and agg-dict copy #11.** This is *the* module other downloaders should have imported; instead each vendored its own copy (histdata, ctrader, yfinance, huggingface, dukascopy_native). The shared-helper fix is literally to delete their copies and import from here.
- **Dead Code.** `pattern` variable assigned and unused at `:118`.

### (d) Maintainability hot spots
This is the natural home for the area's resampling vocabulary; promoting it (and adding order validation) fixes ~8 duplication findings at once.

---

## validation/stats/calculator.py (167 lines)

### (a) Bugs / correctness

- **Minor — drawdown computed on trade-close sequence, not equity curve.** `_compute_drawdowns :59-78`: peak/drawdown over *realized* per-trade PnL only; intra-trade adverse excursion is invisible, so `max_drawdown` here will never match a platform stat computed from an equity series — systematic divergence in every stats comparison that mixes the two conventions. The docstring honestly declares the convention (`:14-16`), but cross-platform comparisons (cross_platform.py) don't reconcile it.
- **Minor — Sortino denominator convention.** `:103`: downside variance divides by count of negative returns, not by n (or by sqrt-frequency scaling) — declared at `:11-13`, but this is the least standard of the published variants; any reference-platform comparison inherits the mismatch.
- **Minor — breakeven trades counted in total but neither winners nor losers** (`:118-120`) — win_rate denominators consistent, fine; noting verified.
- **Minor — infinite ratios serialized into reports.** `profit_factor = inf` / `recovery_factor = inf` (`:129`, `:139`) rely on json_report's NaN/Inf sanitizer to become null downstream; any consumer bypassing that encoder writes invalid JSON.

### (b) Security
Nothing found.

### (c) Baseline smells
Clean module — explicit conventions, pure functions, no smells worth flagging beyond the convention risks above.

### (d) Maintainability hot spots
The docstring contract (`:6-17`) is exactly what the duplicated comparators should cite when unified.

---

## validation/reference.py (110 lines)

### (a) Bugs / correctness

- **Minor — deprecated `datetime.utcnow()` as default factory.** `reference.py:51`: `datetime.utcnow` is deprecated (3.12+) and produces a **naive** UTC datetime; every other module uses `datetime.now(timezone.utc)`. Downstream `.strftime`/`.isoformat` consumers can't tell it's UTC. (Checklist: tz-naive datetimes.)
- **Minor — `ALLOWED_FRAMEWORKS` omits `"quantconnect"`.** `:18-28` allows "lean" — which is what quantconnect_result.py emits (`source_framework="lean"`), consistent — but the QC importer's `raw_metadata["source_framework"]="quantconnect"` while the canonical field says "lean"; a future framework value ("mt5_new") fails validation at import time with no registration path beyond editing this frozenset.
- **Minor — empty-trades guard bypass documented but unlogged.** `allow_empty=True` (`:77-78`) silently accepts zero trades; readiness gating elsewhere assumes non-empty.

### (b) Security
Nothing found.

### (c) Baseline smells
- **Primitive Obsession.** `source_framework`/`validation_status` validated against frozensets then stored as str; the `str, Enum` pattern used elsewhere (risk/models.py) is more consistent.
Otherwise clean, well-documented model.

### (d) Maintainability hot spots
None significant.

---

## validation/readiness/models.py (80 lines)

### (a) Bugs / correctness

- **Major — duplicate field definitions in `ReadinessConfig`.** `require_trade_report`, `require_regression`, `require_risk_report`, `require_stats` are each declared **twice** (`models.py:65-68` and again `:77-80`). Pydantic v2 raises `PydanticUserError: field duplicated` on model creation... in practice with `from __future__ import annotations` and dict-based namespace this surfaces at first import of the module. If this file imports cleanly today it's because pydantic tolerates redefinition with last-wins — either way it's a latent breakage and certainly unintended. Delete the second block.
- **Minor — naming confusion.** `stats_win_rate_tolerance_pct` holds a *fraction* (0.01) not a percent (`:73-74`) despite the `_pct` suffix — validator compares raw fractions (`validator.py:581-582`). Mysterious-name hazard that invites 100× config errors.

### (b) Security / (c) Smells
Nothing further; the duplication above is the finding.

---

## validation/risk/models.py (67 lines)

### (a) Bugs / correctness

- **Minor — `direction: str = "LONG"` free-text.** `risk/models.py:38`: sizing math presumably branches on direction; a lowercase "long" from any importer would take the default LONG branch or mis-size. Enum would pin it.
- **Minor — `stop_distance` required non-optional** (`:56`) yet profiles are built for trades whose TP may be None; construction sites must fabricate a distance — worth verifying fixed_risk/percent_risk behavior for stop==entry (zero distance → division risk).

### (b) Security
Nothing found.

### (c) Baseline smells
- **Speculative Generality (mild).** `RiskModelConfig` (`:20-27`) isn't referenced by comparison.py's config path (it passes `FixedRiskConfig | PercentRiskConfig` directly) — check usage; possibly dead abstraction.

---

## data_tools/downloaders/base.py (239 lines)

### (a) Bugs / correctness

- **Major — non-atomic read-modify-write of canonical parquet.** `_merge_and_store_single_file :151-184`: read existing → concat → dedupe → `to_parquet` overwrite. Two concurrent downloaders targeting the same file (grid tool runs workers; auto_download + manual CLI) interleave read/write and lose rows or corrupt the file (no tmp+rename, no lock). Every downloader inherits this.
- **Minor — resume granularity is one day.** `_resolve_resume_start :138-149` resumes from the stored *date*, re-downloading that whole day and relying on keep="last" dedupe — correct but wasteful for tick data (a full day per resume); also silently *skips* nothing if the source clock differs.
- **Minor — `_read_latest_timestamp` trusts row-group max stats.** `:207-213`: if the file isn't sorted by timestamp within/across row groups (true after any unsorted merge), the last row group's max is not the global max — the fallback at `:215-223` only reads the final row group too. Resume start can be earlier than actual data → benign duplicate work thanks to dedupe, but the "latest" claim in the docstring is not guaranteed.
- **Minor — parquet reader unclosed** (`:192`) — same fd-hygiene note as catalog_builder.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Template Method done right** — the ABC is clean; `_merge_and_store_single_file` is the shared write path where atomicity belongs once implemented.
- **Duplicated Code (cross-module).** `_select_timestamp_column :225-231` is the canonical copy; catalog_builder and three downloaders still carry their own.

### (d) Maintainability hot spots
This base class is the right place to centralize: atomic writes, timeframe validation, and column normalization — three fixes that would cascade through all nine downloaders.

---

## Package `__init__.py` files (10 files)

*Scanned together — see final accounting.*

---

## Final accounting — every tracked file

| # | File | Status |
|---|------|--------|
| 1 | data_tools/\_\_init\_\_.py | Scanned — empty (0 lines), nothing to review. |
| 2 | data_tools/auto_download.py | Reported above. |
| 3 | data_tools/cache_manager.py | Reported above. |
| 4 | data_tools/catalog_builder.py | Reported above. |
| 5 | data_tools/dolthub_manager.py | Reported above. |
| 6 | data_tools/download_grid_market_data.py | Reported above. |
| 7 | data_tools/downloaders/\_\_init\_\_.py | Scanned — empty (0 lines), nothing to review. |
| 8 | data_tools/downloaders/alpaca_dl.py | Reported above. |
| 9 | data_tools/downloaders/base.py | Reported above. |
| 10 | data_tools/downloaders/ctrader_dl.py | Reported above. |
| 11 | data_tools/downloaders/dolthub_dl.py | Reported above. |
| 12 | data_tools/downloaders/dukascopy_native.py | Reported above. |
| 13 | data_tools/downloaders/histdata_dl.py | Reported above. |
| 14 | data_tools/downloaders/huggingface_dl.py | Reported above. |
| 15 | data_tools/downloaders/jforex_dl.py | Reported above. |
| 16 | data_tools/downloaders/yfinance_dl.py | Reported above. |
| 17 | data_tools/dukascopy_discovery.py | Reported above. |
| 18 | data_tools/migrate_csv.py | Reported above. |
| 19 | data_tools/resample.py | Reported above. |
| 20 | data_tools/signal_import.py | Reported above. |
| 21 | data_tools/validate.py | Reported above. |
| 22 | validation/\_\_init\_\_.py | Scanned (5 lines) — re-exports only, no findings. |
| 23 | validation/cli.py | Reported above. |
| 24 | validation/comparison/\_\_init\_\_.py | Scanned (33 lines) — re-exports only, no findings. |
| 25 | validation/comparison/anomalies.py | Reported above. |
| 26 | validation/comparison/comparison.py | Reported above. |
| 27 | validation/comparison/delta_report.py | Reported above. |
| 28 | validation/comparison/matcher.py | Reported above. |
| 29 | validation/importers/\_\_init\_\_.py | Scanned (7 lines) — re-exports only, no findings. |
| 30 | validation/importers/base.py | Scanned (35 lines) — abstract interface only (`load` signature + docstring), no findings. |
| 31 | validation/importers/mt5_html.py | Reported above. |
| 32 | validation/importers/mt5_xlsx.py | Reported above. |
| 33 | validation/importers/quantconnect_result.py | Reported above. |
| 34 | validation/readiness/\_\_init\_\_.py | Scanned (22 lines) — re-exports only, no findings. |
| 35 | validation/readiness/models.py | Reported above. |
| 36 | validation/readiness/validator.py | Reported above. |
| 37 | validation/reference.py | Reported above. |
| 38 | validation/regression/\_\_init\_\_.py | Scanned (16 lines) — re-exports only, no findings. |
| 39 | validation/regression/runner.py | Reported above. |
| 40 | validation/reporting/\_\_init\_\_.py | Scanned (18 lines) — re-exports only, no findings. |
| 41 | validation/reporting/json_report.py | Reported above. |
| 42 | validation/risk/\_\_init\_\_.py | Scanned (20 lines) — re-exports only, no findings. |
| 43 | validation/risk/adapter.py | See below (risk sizing adapters). |
| 44 | validation/risk/comparison.py | Reported above. |
| 45 | validation/risk/fixed_risk.py | See below. |
| 46 | validation/risk/models.py | Reported above. |
| 47 | validation/risk/percent_risk.py | See below. |
| 48 | validation/statistics/cross_platform.py | Reported above. |
| 49 | validation/stats/\_\_init\_\_.py | Scanned (7 lines) — re-exports only, no findings. |
| 50 | validation/stats/calculator.py | Reported above. |

---

## validation/risk/fixed_risk.py (132 lines) & percent_risk.py (142 lines)

*(Reviewed together — near-identical modules.)*

### (a) Bugs / correctness

- **Major — `min_position_size` semantics inverted.** Both models (`fixed_risk.py:92-93`, `percent_risk.py:102-103`): if the computed size is *below* `min_position_size`, position size is set to **0.0** rather than clamped up to the minimum. A config meaning "never trade less than 100 units" instead becomes "skip the trade" — and because comparison then sees position_size 0 vs platform's real size, every undersized trade reads as a catastrophic mismatch rather than a floor adjustment.
- **Major — lot rounding floors to zero silently.** `math.floor(size/lot)*lot` (`fixed_risk.py:87-90`, `percent_risk.py:97-100`) yields 0 whenever risk_amount < stop_distance×lot_size; combined with the above, tiny accounts produce all-zero sizing with no warning anywhere in the result object.
- **Minor — direction never used in math.** Both compute `stop_distance = abs(entry - stop)` (`fixed_risk.py:79`, `percent_risk.py:88`) — a stop on the wrong side of entry for the stated direction produces a positive distance and a plausible-looking profile instead of an error. Direction is carried into the output only.
- **Minor — `validate()` mutates its input** (`input.stop_loss = stop_loss`, `fixed_risk.py:61`, `percent_risk.py:60`) — surprising side effect on a "validate" method; documented but easy to misuse.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Duplicated Code — the two models are ~85% identical** (validation, rounding, RR ratio, profile assembly). One base class with an abstract `_risk_budget(input)` would leave each file ~20 lines.
- **Refused Bequest (mild).** `RiskSizingResult` with `profiles=[one]` per call — the aggregate shape serves exactly one profile everywhere it's used.

---

## validation/risk/adapter.py (235 lines)

### (a) Bugs / correctness

- **Major — two disjoint sizing paths with different fallback behavior.** `calculate_trade_risk :112-170` delegates to `risk.RiskEngine`, which *synthesizes* a fallback stop when none exists (docstring `:7-12`), while `size() :172-202` raises on missing stop. `risk/comparison.py::_compute_profile` uses `size()`; anything using `calculate_trade_risk` compares against invented stops — the same validation framework both forbids and fabricates missing evidence depending on entry point.
- **Minor — `action = "buy" if trade.direction == "LONG"` else sell** (`:41`): any non-"LONG" string (lowercase, "Buy") becomes "sell".
- **Minor — `total_risk_percent` summed as percentages of different balances** (`:229`): summing per-trade fractions is dimensionally odd (1% + 1% = 2%?) — fine as an exposure proxy, undocumented.
- **Minor — dead parameters.** `current_exposure` forwarded (`:136`) but its effect depends entirely on engine internals; `pip_value=1.0` default silently mis-sizes pip-denominated instruments unless callers know to override.

### (b) Security
Nothing found.

### (c) Baseline smells

- **Middle Man.** `trade_to_signal` wraps the module function one-for-one (`:79-90`) — pick one form.
- **Repeated Switches.** `size()`'s model dispatch (`:189-202`) is the polymorphism-by-if pattern; the models share an interface, so a registry dict completes it.

### (d) Maintainability hot spots
The dual-path fallback discrepancy is the item to resolve before any live-trading readiness decision relies on these numbers.

---

