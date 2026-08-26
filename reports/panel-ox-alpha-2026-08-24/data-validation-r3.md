# Code Review — Area `data-validation` (reviewer r3 / panel ox-alpha, 2026-08-24)

Blind, independent review of `data_tools/` (market-data download/cache/resample) and
`validation/` (backtest-vs-broker validation framework). All 51 tracked code files in the
area list were read in full. Read-only review; nothing was edited.

## Method & repo-standard notes

- Lint baseline: ruff, `select = ["E","F","I","W"]`, line-length 100, target py312
  (`pyproject.toml`). Nothing stricter is enforced, so complexity / mutable-default /
  broad-except categories are *not* suppressed by tooling and are in scope.
- Docs consulted for repo standards: `docs/validation_architecture.md`,
  `docs/reference-artifact-contract.md` (provenance rules: record source timezone +
  UTC conversion, keep header vs recomputed metrics distinct),
  `docs/DATA_SOURCES.md` (canonical layout
  `$TRADING_DATA_DIR/ohlcv/{source}/{asset_class}/[{region}/]{symbol}/{tf}.parquet`).
- Baseline rules applied: Fowler ch.3 smells, judged case-by-case (repo override +
  judgement-call rules respected), plus the Python fixed checklist: mutable default args,
  bare except, races on shared state, SQL string building, subprocess without timeout,
  tz-naive datetimes, resource leaks.

Severity scale: **critical** (breaks a shipped code path outright), **major** (wrong
results/data, security exposure, or a structural smell that will bite), **minor**
(localized defect, robustness/perf nit, small duplication).

Findings are numbered globally (`R3-nnn`) for easy referencing.

---

# PART 1 — `data_tools/`

## `data_tools/__init__.py`
Empty package marker. Scanned — nothing found.

## `data_tools/downloaders/__init__.py`
Empty package marker. Scanned — nothing found.

## `data_tools/downloaders/dukascopy_native.py`

- **R3-001 · critical · bug** — `src:1016-1028`: the merged bid/ask tick path calls
  `_download_and_assemble_merged_ticks(instrument, start_dt, end_dt, batch_size, pause_ms,
  max_retries, pipeline_batches=pipeline_batches, ...)`, but the signature
  (`src:774-786`) is `(instrument, start_dt, end_dt, batch_size, pipeline_batches,
  pause_ms, max_retries, *, ...)`. `pause_ms` lands in `pipeline_batches`,
  `max_retries` in `pause_ms`, and `pipeline_batches` is then also passed as a keyword →
  `TypeError: multiple values for argument 'pipeline_batches'` on *every*
  `--merge-tick`, `--merge-s1`, or `offer_side=both` tick/s1 download. This path cannot
  have been exercised by any test.
- **R3-002 · major · bug/race** — `src:869-884` + `src:1182-1184`:
  `_write_source_meta_index` does an unlocked read-modify-write of the shared
  `source_meta.json` (load whole index → set one key → rewrite file), and the write is
  not atomic (no temp-file rename). Concurrent downloads — e.g. the process-pool in
  `download_grid_market_data.py --workers 6` — can lose each other's metadata entries or
  leave a truncated/invalid JSON behind, which then silently degrades to `{}` for all
  future catalog runs (`catalog_builder._load_source_meta` warns and continues).
- **R3-003 · major · bug (minor blast radius)** — `src:985-986`: any requested end whose
  time component is exactly `00:00` is silently rewritten to `23:59:59`. A user who
  explicitly passes `--end 2024-01-05T00:00:00` meaning "end at midnight" gets a full
  extra day of data with no warning.
- **R3-004 · minor · bug** — `src:245-256` `generate_tick_urls` iterates
  `while current < end_dt` (hour granularity) while `generate_minute_urls`
  (`src:259-276`) uses `current <= end_day` — the two generators treat range ends
  differently; an end at exactly `HH:00:00` drops the final tick hour.
- **R3-005 · minor · race (benign)** — `src:66-77` `_load_instrument_meta` lazily fills a
  module global without a lock; two threads can race the JSON parse. GIL makes this
  mostly harmless, but it's an unguarded shared-state pattern.
- **R3-006 · minor · robustness** — `src:400` retries only `(asyncio.TimeoutError,
  OSError)`; aiohttp errors such as `ClientPayloadError` / some `ClientConnectionError`s
  propagate out of `_fetch_one` and abort the whole `asyncio.gather` batch instead of
  being retried/skipped like the handled classes.
- **R3-007 · minor · UX** — `src:1309-1310` the CLI loop catches `Exception` and prints
  `❌`; a user-initiated `DownloadCancelled` (raised from the stop_event machinery) is
  swallowed as a normal failure and the script reports "Done: N downloads completed"
  afterwards.
- **R3-008 · smell · Long Method** — `_download_async` spans `src:922-1197` (~275 lines)
  handling arg normalization, path resolution, both strategies, both offer sides, meta
  persistence and result building. Prime extract-method target.
- **R3-009 · smell · Duplicated Code** — the bid/ask "both" candle branch
  (`src:1073-1132`) repeats the same side-download lambda-progress plumbing twice;
  `_normalize_offer_side` (`src:640-644`), the OHLCV prefix/rename helpers
  (`src:647-674`) are duplicated nearly verbatim in `jforex_dl.py`
  (`src:960-1000`). Extract into a shared dukascopy-common module.
- **R3-010 · smell · Data Clumps** — the
  `stop_event/pause_event/report_progress/log_callback` quartet is threaded through every
  async function (`src:677-712`, `src:774-786`, `src:411-424` …). Bundle into a
  `DownloadControl` object; today adding one control knob means touching ~8 signatures.
- **R3-011 · smell · Duplicated maps** — timeframe mappings exist here
  (`TIMEFRAME_MAP src:146-165`, `TF_RESAMPLE_RULE src:168-178`, inline
  `storage_tf_map src:938-949`), again in `resample.TIMEFRAME_MAP`, in
  `shared.constants.normalize_timeframe`, and in `auto_download.TIMEFRAME_ORDER`. Four+
  representations of one concept; classic Repeated Switch/Shotgun Surgery surface.

Maintainability hot spot: the file is the de-facto dukascopy core (URL scheme, binary
format, throttling policy, storage layout) yet also hosts CLI plumbing; the critical
arg-mismatch in R3-001 is symptomatic of the untested 275-line method.

## `data_tools/downloaders/jforex_dl.py`

- **R3-012 · major · bug** — `src:967-970`
  `_normalize_timeframe_for_file_suffix` chains `.replace()` in a destructive order:
  `"15m"` first loses `"5m"→"m5"` (yielding `"1m5"`), and the later
  `.replace("15m","m15")` never matches. Used by
  `_build_offer_side_output_path` (`src:1003-1013`), so an
  `offer_side=both` 15m download writes `EURUSD/1m5_bid.parquet` /
  `1m5_ask.parquet` instead of `15m_bid.parquet` — filenames the catalog's timeframe
  regex (`catalog_builder._TIMEFRAME_REGEX`) cannot parse, so those datasets vanish from
  the catalog or get misclassified. (`"30m"` survives only by luck of ordering.)
- **R3-013 · major · security (supply chain)** — `src:280-311` +
  `src:344-353`: on first use the module downloads SDK JARs from the (env-overridable)
  `DUKASCOPY_JNLP_URL` via plain `urlopen` and loads them into a JVM. There is integrity
  checking for the *JVM* path (`_trusted_java_home_roots`, `src:356-437` — nice), but **no
  checksum/pinning for the downloaded JARs**, and `JFOREX_CLASSPATH` is honored from the
  environment unconditionally. Anyone who can influence that env var or DNS gets arbitrary
  code execution inside the trading platform's Python process. At minimum pin hashes and
  log the classpath source loudly.
- **R3-014 · minor · bug** — `src:547-555`: inside the JVM-candidate loop, the first
  candidate failing with a Java exception hits
  `except _java_exception_types(): _raise_jforex_runtime_error(...)` and aborts — the
  remaining candidates and the JPype-default fallback (`src:557-570`) are never tried.
  The loop structure suggests fallthrough was intended.
- **R3-015 · minor · duplication of policy** — `src:1095` hardcodes
  `"2003-01-01"` as the default start while the native downloader resolves it from
  `dukascopy_genesis.json`; the two dukascopy frontends silently disagree about genesis.
- **R3-016 · minor · robustness** — `src:1075-1076` rejects `offer_side='both'` for
  `target_tf == "tick"` but the equivalent tick-derived outputs (`s1`) reach the second
  rejection at `src:1105-1109` only after auth/JVM startup — fail fast consistently.
- **R3-017 · smell · Divergent Change** — the module mixes four concerns: JVM discovery/
  lifecycle (`src:314-620`), JNLP/network bootstrap (`src:238-311`), JForex query
  protocol (`src:700-957`), and offer-side storage layout (`src:960-1040`). Each changes
  for a different reason; splitting (at least) the JVM bootstrap into its own module would
  shrink this dramatically.
- **R3-018 · smell · Duplicated Code** — `_bars_with_offer_prefix` /
  `_resample_offer_prefixed` (`src:973-1000`) duplicate
  `dukascopy_native._prefix_ohlcv_columns` / `_resample_side_prefixed_ohlcv`; the
  `deduped/seen` loop body appears four times (`src:216-225`, `src:270-277`,
  `src:304-311`, `src:490-502`).

Maintainability hot spot: 1,313 lines where the risky part (JVM bridge) and the mundane
part (file naming) live together; R3-012 shows the mundane part is the one rotting.

## `data_tools/downloaders/base.py`

- **R3-019 · major · durability/race** — `_merge_and_store_single_file`
  (`src:151-184`) implements read-existing → concat → dedupe → `to_parquet(final_path)`
  in place. A crash mid-write leaves a corrupt parquet at the canonical path, which
  cascades: resume logic then fails, catalog marks the file errored, and backtests that
  read it die. Every downloader in the area funnels through this one method. Write to a
  temp file + atomic rename; optionally hold a per-path lock.
- **R3-020 · minor · resource leak** — `src:192` `pq.ParquetFile(output_path)` is never
  closed (`read_latest_timestamp`).
- **R3-021 · minor · correctness edge** — `src:207-214` scans row groups backwards
  trusting `statistics.max`; for files whose row groups aren't time-ordered (possible
  after external tooling touches them) smart-resume computes a wrong resume point and
  silently re-downloads or skips ranges.
- **R3-022 · smell · duplication seed** — `_select_timestamp_column`
  (`src:225-231`) is copied verbatim into `catalog_builder.py:331-336`. One of them
  should import the other.

Otherwise a clean, well-documented ABC; the shared-shape design is the right call.

## `data_tools/catalog_builder.py`

- **R3-023 · minor · import side effect** — `src:26` runs `logging.basicConfig(...)`
  at module import. `cache_manager` imports this module, so importing the cache from the
  FastAPI server re-configures root logging and can override app log formatting.
- **R3-024 · minor · bug** — `src:97-102` `_detect_storage` uses
  `str(path).startswith(str(cache_dir))`: a sibling directory named `cache_backup` or
  `cache2` classifies as "cached". Compare with `os.path.commonpath` / `Path.is_relative_to`.
- **R3-025 · minor · resource leak** — `src:355` `pq.ParquetFile` in
  `read_parquet_summary` never closed (same as R3-020).
- **R3-026 · minor · perf** — `src:844` `scan_fundamentals` fully materializes every
  fundamentals parquet (`pd.read_parquet`) just to record row/column counts. Use the
  same metadata-only trick as `read_parquet_summary`.
- **R3-027 · minor · silent classification** — `src:251/264/274` filenames that don't
  look like a timeframe default to `"1d"`; combined with the loose
  `_TIMEFRAME_REGEX` (`src:35`, which happily matches `"e"` via `[smhdw]\d+`? no —
  `(?:\d+)?(?:s|sec|m|min|h|d|w|mo)` alone matches bare `"d"`, `"m"`, `"w"`…) a file named
  `d.parquet` or `min.parquet` becomes a "timeframe" and its parent folder a symbol.
  Worth tightening or warning.
- **R3-028 · smell · Long Function / Primitive Obsession** —
  `_group_ohlcv_entries` (`src:439-722`, ~280 lines) hand-aggregates raw dicts through
  ~20 ad-hoc keys with repeated `str(...).strip()` coercion (`src:542`, `src:552`,
  `src:556`, `src:593`…). A small typed aggregate dataclass would delete most of the
  defensive coercion and make the merge rules testable.
- **R3-029 · smell** — duplicated `_select_timestamp_column` (see R3-022) and a
  third copy of mtime/last_updated reconciliation logic that overlaps
  `cache_manager._should_reuse…`-style freshness checks.

## `data_tools/cache_manager.py`

- **R3-030 · major · concurrency** — `get_catalog` (`src:240-256`) holds the global
  `_CACHE_LOCK` (RLock) across the entire cold build, including
  `build_catalog`'s full filesystem walk (potentially minutes on large trees). During a
  rebuild, *every* `get_catalog`/`get_overview_metadata`/`invalidate_cache` call from
  server threads blocks. Build outside the lock, then publish under a short critical
  section (per-key slots or double-checked locking).
- **R3-031 · major · API hazard** — `get_catalog_ohlcv_entries(unsafe_no_copy=True)`
  (`src:271-279`) hands callers a live reference to the cached list of dicts. Any caller
  mutation (an innocent `entry.pop("columns")`) poisons the shared cache for all future
  readers, and the flag makes it one refactor away from accidental use. Return a shallow
  copy of the list (dict sharing is then still cheap) or remove the escape hatch.
- **R3-032 · minor** — `get_overview_metadata` (`src:295-306`) releases the lock between
  miss-check and `setdefault`; benign (worst case duplicate build), but the mixed locking
  discipline invites future bugs.

## `data_tools/auto_download.py`

- **R3-033 · minor · heuristic misfire** — `_is_forex_symbol` (`src:301-306`)
  classifies *any* 6-letter uppercase symbol as forex (plus the SYMBOL_CONFIG lookup).
  Six-character equity tickers/CUSIP-ish strings route to the dukascopy downloader and
  then fail late with "No candle data" instead of trying yfinance.
- **R3-034 · minor · swallowed config errors** — `src:308-316` and `src:319-326` wrap the
  SYMBOL_CONFIG import in `except Exception: return False`; a genuine packaging error
  masquerades as "not a dukascopy symbol" and changes the download source silently.
- **R3-035 · minor · data duplication by design** — `HF_INTRADAY_COVERAGE_END`
  /`ALPACA_INTRADAY_START` (`src:21-22`) create a July-2025 overlap window downloaded from
  *both* HuggingFace and Alpaca (`_auto_download_source_plan`, `src:374-393`); the merge
  in `base.py` keeps newest rows, but two sources can disagree on the same bars and the
  winner is download-order dependent. Documented nowhere.
- **R3-036 · smell · Repeated Switch** — `_download_missing_ohlcv_data`
  (`src:396-488`) is a five-way if-chain dispatching on source name; a
  `{source: downloader_factory}` map (the repo already has a Downloader ABC!) removes the
  chain and the lazy-import repetition.
- **R3-037 · minor** — coverage checks read the *timestamp column* of candidate parquets
  in full (`src:138`, `src:275`) — fine, but `except Exception` there converts a corrupt
  file into "no coverage" → guaranteed re-download attempt; acceptable, but a narrower
  except with a louder log would help diagnosis.

## `data_tools/validate.py`

- **R3-038 · major · bug** — `src:94`:
  `rel = path.relative_to(DATA_DIR / "ohlcv") if str(path).startswith(str(DATA_DIR)) …`.
  When invoked via `python -m data_tools.validate --data-dir <custom>` (supported at
  `src:286,290`), files under the custom root don't sit below `DATA_DIR`, so `rel` stays
  an absolute path and `parts[0]` is `/` — worse, if the custom dir *is* inside
  `$HOME/data`-like prefixes the guard passes/fails inconsistently. With a custom dir the
  function raises `ValueError: ... is not in the subpath of ...` out of
  `validate_parquet`, crashing `validate_all`. Use `try/except ValueError` around
  `relative_to` with a fallback, or take `ohlcv_root` as a parameter.
- **R3-039 · major · bug (misreported data)** — gap detection
  (`src:162-186`): `large_gaps.index[:20]` yields pandas *labels*, which are then used
  positionally (`ts.iloc[idx - 1]`, `ts.iloc[idx]`). After `ts.dropna()` (`src:137`) the
  retained labels no longer equal positions, so whenever any unparseable timestamp was
  dropped earlier, reported `gap_start`/`gap_end` pairs point at the wrong bars (and
  `weekday()` weekend-skips at `src:173-178` are evaluated against the wrong endpoints).
  Use `ts.reset_index(drop=True)` before the diff loop.
- **R3-040 · minor · metric inflation** — OHLC violations
  (`src:199-206`) sum three boolean masks; one pathological bar can be counted up to
  three times, overstating `ohlc_violations` versus "number of violating bars".
- **R3-041 · minor · heuristic noise** — weekend skip covers Fri→Mon, Fri→Sun,
  Sun→Mon (`src:173-178`) but not Fri→Sat; forex Saturday-morning tails are flagged as
  gaps. Holidays likewise. Fine for a lint-style tool, but the docstring promises
  "accounting for weekends/holidays" — holidays are not handled.
- **R3-042 · smell · Speculative Generality** — `EXPECTED_BARS_PER_DAY`
  (`src:35-43`) is dead (never referenced). Delete or wire it in.
- **R3-043 · minor** — `logging.basicConfig` at import (`src:29`), same issue as R3-023.

## `data_tools/resample.py`

- **R3-044 · minor · dead code** — `src:118`: `pattern = f"**/{from_tf}.parquet"` is
  assigned and never used (the next line rglobs without it).
- **R3-045 · smell · Duplicated Code** — the
  `resample().agg({open:first,…})` block appears here (`src:60-72`) and again in
  `histdata_dl._resample`, `ctrader_dl._resample`, `huggingface_dl._resample`, inline in
  `yfinance_dl` (4h), and in `dukascopy_native._resample_candles`. Six copies of one
  OHLCV rule; extract `data_tools.resample.resample_ohlcv` everywhere (it exists!).
- **R3-046 · minor** — `update_catalog` (`src:138-150`) swallows all exceptions with a
  warning; a stale catalog after a "successful" resample is exactly the kind of silent
  inconsistency the cache tries to prevent.
- Clean otherwise; `resample_ohlcv` itself is tidy and UTC-safe.

## `data_tools/signal_import.py`

- **R3-047 · major · bug (silent wrong side)** — `_parse_action`
  (`src:67-77`) defaults *any* unrecognized action to `"buy"`: `"Exit"`, `"Close"`,
  `"SL"`, typos, even empty cells become long entries in a replay. For a signal-replay
  loader this can invert a strategy's behavior with no error. Raise on unknown, or emit
  an `unknown` side the replay layer rejects.
- **R3-048 · minor · tz-naive datetimes** — timezone is inferred from whether the
  *column name* contains `"ny"` (`src:91-92`); everything else is localized to UTC.
  Deliberate (German exports with `Zeit_NY`), but undocumented at the call site and a
  trap for any other exchange timezone.
- **R3-049 · minor** — timeframe inferred as `"1m"` iff the timestamp has a time-of-day
  (`src:80-81`, `src:113`); a 00:00:00-stamped intraday bar or a non-midnight daily
  convention misclassifies. Acceptable heuristic; deserves a comment.
- **R3-050 · minor · perf** — `frame.iterrows()` (`src:151`) over large CSVs; vectorized
  parsing would be 50–100× faster for replay-sized files.

## `data_tools/dolthub_manager.py`

- **R3-051 · major · security/checklist (SQL string building)** — `src:133-144`: the
  per-symbol export interpolates `symbol` straight into SQL
  (`WHERE act_symbol='{symbol}'`). Symbols come from a prior `SELECT DISTINCT` on the
  same DB, so exploitation needs a poisoned repo — but any symbol containing `'` breaks
  the query, and the pattern normalizes injection-by-habit. Parameterize or escape.
- **R3-052 · major · checklist (subprocess without timeout)** — every subprocess call
  lacks `timeout=`: `run_cmd`/`subprocess.check_call` (`src:30-32`),
  `_list_tables` (`src:60-65`), the symbols query (`src:111-116`), and the export calls
  (`src:133-144`, `src:74-77`). A stalled `dolt pull` against DoltHub hangs `update`
  indefinitely (relevant since the scheduler can invoke this).
- **R3-053 · minor · unchecked result** — the `SELECT DISTINCT act_symbol` call
  (`src:111-118`) ignores `returncode`; on failure you get zero symbols and a cheerful
  "Found 0 symbols to export."
- **R3-054 · minor · leftover artifacts** — in the earnings/rates loops
  (`src:164-199`) the intermediate `<table>.csv` is only unlinked on the success path;
  a conversion failure leaves stale CSVs next to the parquet outputs.
- **R3-055 · minor · tz-naive** — `df["timestamp"] = pd.to_datetime(df["timestamp"])`
  (`src:148`) stores tz-naive daily stamps, unlike every downloader (UTC-aware). Catalog
  consumers comparing ranges must now handle both.
- **R3-056 · smell · Duplicated Code** — the earnings and rates export blocks
  (`src:157-199`) are the same 20 lines twice; extract `_export_tables(repo, out_dir)`.

## `data_tools/migrate_csv.py`

- **R3-057 · minor · environment coupling** — `src:30-32` hardcodes
  `Path.home()/Desktop/custom_lean_integration` etc. — the original author's desktop
  layout. One-shot migration tool, but env vars would make it reusable/testable.
- **R3-058 · minor · silent partial success** — `_convert_ohlcv` catches
  `Exception` and returns 0 (`src:200-202`); `main` then exits 0 regardless, so a CI
  cron running migration can lose half the files invisibly. Aggregate failures and exit
  non-zero.
- **R3-059 · minor** — the yfinance multi-header sniffing (`src:145-157`) mutates based
  on `first_val.isupper()` — any ticker that is all-uppercase in the first data cell
  (e.g. literal ticker column) triggers row-skipping heuristics; fragile but bounded to
  legacy inputs.
- **R3-060 · smell** — the five `migrate_*` functions share one shape
  (glob → derive symbol → dest path → convert); a table of
  `(pattern, dest_template, ts_col)` entries collapses them.

## `data_tools/download_grid_market_data.py`

- **R3-061 · minor · non-atomic rewrite** — `normalize_ohlcv_columns`
  (`src:96-126`) rewrites the parquet in place after a full load; crash mid-write hits
  the same durability issue as R3-019.
- **R3-062 · minor · correctness** — `ensure_alias_copy` (`src:155-161`) treats
  *equal file size* as "alias fresh"; a regenerated canonical file with coincidentally
  equal size (or any content fix) never propagates to the requested-name alias that
  backtests actually read.
- **R3-063 · minor · semantics** — the same function fills canonical `open..close` from
  whichever side exists (`src:104-121`): with `price_type=ask` data the canonical columns
  silently contain ask prices, with `bid+ask` files `close` is bid-filled-then-ask-NaN-
  filled. Downstream parity checks comparing "close" across sources inherit the ambiguity.
- **R3-064 · minor** — skip check at `src:232` reads each target parquet twice in full
  (`parquet_non_null_ohlcv` + `parquet_rows`) per run; fine for 6 symbols, wasteful for
  bigger grids.
- **R3-065 · minor** — summary JSON goes to `Path.cwd()/"logs"` (`src:347-350`): output
  location depends on invocation directory, unlike every other artifact which anchors to
  `DATA_DIR`/repo.
- **R3-066 · smell** — third copy of `TIMEFRAME_ORDER` + alias dicts
  (`src:36`, `src:50-64`); `OIL_ALIASES` contains self-mappings (`"USOIL":"USOIL"`,
  `src:43-49`) that add nothing (Speculative Generality, tiny).

## `data_tools/downloaders/yfinance_dl.py`

- **R3-067 · minor · dead code** — `src:233-235`: the `key.capitalize()` rename branch
  can never fire — all columns were lowercased at `src:218`/`src:221` two statements
  earlier, and `capitalize()` produces `"Open"`-style keys that no longer exist.
- **R3-068 · minor · convention drift** — `resolved_end` is date-truncated
  (`src:183`), and yfinance treats `end` as exclusive → the requested end day is dropped.
  Other downloaders in the area include the end day (dukascopy sets 23:59:59; histdata
  clips inclusively). Same `--end` argument, different data, depending on source.
- **R3-069 · minor · cross-source alignment** — 4h bars are resampled from Yahoo's 60m
  feed onto calendar boundaries (`src:253-269`); dukascopy 4h bars follow FX session
  alignment. Comparing the same symbol/timeframe across sources yields structurally
  different 4h candles — worth a warning in the file docstring.
- **R3-070 · minor** — resume path (`src:171-182`) restarts the download at the latest
  stored *date*, re-fetching that whole day; merged with `keep="last"` so fresh partial
  rows overwrite previously stored complete-day rows from the same source. Benign for
  yfinance (same data), but the pattern is fragile if reused elsewhere.
- Clean structure otherwise; clipping warnings for intraday windows are a nice touch.

## `data_tools/downloaders/alpaca_dl.py`

- **R3-071 · minor** — missing volume silently filled with `0`
  (`src:179-180`); zero-volume bars then depress `validate.zero_volume_pct` stats and
  any volume-filtered strategy. Prefer NaN + dropna, or a warning.
- **R3-072 · smell · copy-paste helpers** — `_to_utc_timestamp` and `_parse_csv_list`
  (`src:52-64`) are byte-identical to `huggingface_dl.py:68-93` and near-identical to
  yfinance's; move to `downloaders/base.py`.
- Otherwise solid: token-pagination loop with repeat-token guard (`src:256-263`) is the
  best-engineered fetch loop in the area.

## `data_tools/downloaders/huggingface_dl.py`

- **R3-073 · major · perf/robustness (borderline minor)** — `download`
  fetches full *monthly* parquet files over HTTPS for every symbol × timeframe request
  (`src:253-270`) with no local cache: requesting 5 symbols × 1d from the default start
  (`DEFAULT_START_DATE = 1994-10-01`, `src:30`) pulls ~370 whole-market monthly files
  per symbol, ~1,850 large HTTP payloads, to keep rows for one ticker. Add a
  month-file disk cache keyed by (year,month)+ETag, or filter server-side via
  partition predicates if the dataset supports it.
- **R3-074 · minor** — per-month `except Exception` (`src:258-261`) accumulates
  warnings and proceeds; a systemic failure (auth revoked, dataset renamed) surfaces
  only at the end as "No rows found" after downloading everything first. Fail fast on
  consecutive N failures.
- **R3-075** — duplicated helpers, see R3-072.

## `data_tools/downloaders/histdata_dl.py`

- **R3-076 · minor · resource leak** — a fresh `requests.Session()` per year
  (`src:171`) is never closed; years×symbols runs churn TLS handshakes. Hoist one
  session (or `with requests.Session()`) per download call.
- **R3-077 · minor · end-date semantics** — `combined["timestamp"] <= end`
  (`src:143-144`) compares tz-aware stamps to a `YYYY-MM-DD` string; pandas parses the
  string to midnight, so the whole end day is excluded — inconsistent with dukascopy's
  inclusive end handling (see R3-068).
- **R3-078 · minor · tz-naive** — `datetime.now().year` (`src:118`) uses local time; on
  Dec 31 evening in TZ≥UTC+1 the last wanted year is off by one. Use
  `datetime.now(timezone.utc)`.
- **R3-079 · minor** — `import re` inside `_download_year` (`src:182`) and
  `import argparse` inside `main` (`src:262`) — hoist to module top (style only).

## `data_tools/downloaders/ctrader_dl.py`

- **R3-080 · minor · taxonomy drift** — indices get `asset_class = "index"`
  (`src:142-145`) while the repo canon (`SYMBOL_CONFIG`, docs, catalog examples) is
  `"indices"`. Files land under `ohlcv/ctrader/index/…`, fragmenting catalog grouping.
- **R3-081 · minor** — `pd.to_datetime(df["timestamp"], utc=True)` without
  `errors="coerce"` (`src:207`): one malformed row aborts the entire import, unlike
  every sibling loader which coerces+drops.
- **R3-082 · minor · mislabel risk** — with `timeframe="1m"` and a tick CSV, raw ticks
  are stored under `1m.parquet` un-resampled (`src:156-157` only resamples ≠1m);
  downstream consumers trust the filename.
- **R3-083 · smell** — `_resample` + `RESAMPLE_MAP` again duplicated
  (see R3-045); separator-sniffing loader (`src:181-189`) is reasonable.

## `data_tools/downloaders/dolthub_dl.py`

- **R3-084 · major · security/checklist (SQL string building)** — `src:94-102`: default
  query interpolates `symbol`, `table`, `branch`, and dates directly into SQL sent to the
  DoltHub API. CLI-supplied values make this self-inflicted-injection territory, but the
  same builder is reachable from `auto_download` paths; quote/parameterize.
- **R3-085 · major · silent truncation** — the response's `rows` are consumed with **no
  pagination** (`src:145-154`); the free DoltHub API caps result sizes, so a long history
  comes back truncated and the partial frame is merged into the canonical store as if
  complete. Resume logic will then believe coverage exists. At minimum assert a
  row-count below the API cap or honor a pagination cursor if offered.
- **R3-086 · minor** — `owner, repo_name = repo.split("/", 1)` (`src:89`) raises a bare
  `ValueError: not enough values to unpack` for a repo without slash; wrap with a clear
  message.
- **R3-087 · minor** — `resp.json()` failures (HTML error page with HTTP 200 behind a
  proxy) raise an unhandled `json.JSONDecodeError` outside the retry umbrella
  (`src:113-115` only retries Timeout/ConnectionError).
- Retry/backoff on timeouts (`src:111-134`) is good.

## `data_tools/dukascopy_discovery.py`

- **R3-088 · minor · heuristic edge** — probe-date helpers dodge only Saturdays
  (`candidate.weekday() == 5`, `src:150-159`); a Sunday probe (no FX data) returns
  "miss" and can push the discovered genesis a few days late. Probe Tue/Wed instead of
  "last non-Saturday".
- **R3-089 · minor · perf** — `asyncio.run(...)` per single-file probe
  (`src:141-147`): hundreds of event-loop startups per discovery run. Reuse one loop or
  a plain `urllib` GET for probes.
- **R3-090 · minor · assumption** — binary search presumes data availability is
  monotonic from genesis onward; delisted symbols or mid-history outages send the search
  to odd answers (mitigated somewhat by the linear sweeps at `src:284-298`,
  `src:328-350`, which themselves multiply probe counts).
- **R3-091 · minor** — `REFERENCE_GENESIS` hardcoded for two symbols
  (`src:39-46`), config-as-code; move to the genesis JSON.
- Incremental save-per-discovery (`src:570`) is a genuinely good resilience touch.

---

# PART 2 — `validation/`

## `validation/__init__.py`, `comparison/__init__.py`, `regression/__init__.py`,
## `reporting/__init__.py`, `readiness/__init__.py`, `stats/__init__.py`
Scanned — nothing found. Re-exports only; `__all__` lists match.

## `validation/importers/__init__.py`
Scanned — nothing found (re-exports the three importers).

## `validation/reference.py`

- **R3-092 · minor · tz-naive/deprecated** — `imported_at: datetime =
  Field(default_factory=datetime.utcnow)` (`src:51`): deprecated in 3.12 and produces a
  naive datetime; the rest of the area uses `datetime.now(timezone.utc)`. The naive value
  then flows into `BacktestMeta.created_at` via `isoformat()` with no offset suffix.
- **R3-093 · minor** — generated ids use second-granularity timestamps
  (`src:87-89`): two imports of the same reference within one second collide on
  `BacktestMeta.id`.
- Model validators (`src:55-79`) are exemplary — allow_empty escape hatch documented.

## `validation/cli.py`

- **R3-094 · major · wasted work / divergence risk** — for `--format markdown`, pass/fail
  is determined by *re-running the entire pipeline* via `generate_json_report`
  (`src:237-250`) after `generate_markdown_report` already ran it internally
  (`json_report.generate_markdown_report → generate_json_report`, `src:578-589`).
  Double wall-clock on every markdown invocation and two chances for the two runs to
  diverge (they currently can't, but any nondeterminism — dict ordering fixes aside —
  would make the printed report disagree with the exit code). Return the verdict with
  the report.
- **R3-095 · minor · precision loss** — `--time-tolerance` is parsed as float and
  validated (`src:103-105`, `src:146-157`), then truncated via
  `int(args.time_tolerance)` when handed to the reporters (`src:200`, `src:209`,
  `src:244`): `--time-tolerance 0.5` gives the comparator 0.5 s but the report metadata
  0 s. Plumb the float through.
- **R3-096 · minor · naming mismatch** — `pnl_pct=args.stats_tolerance`
  (`src:190`): the stats tolerance flag secretly sets the per-trade PnL relative
  tolerance. Works today by intent; confusing contract.

## `validation/comparison/comparison.py`

- **R3-097 · smell · Duplicated Code** — `_compare_optional_price`
  (`src:244-269`) and `_compare_optional_pct` (`src:217-242`) are character-identical
  apart from the delegated call. Collapse to one.
- **R3-098 · smell · duplicated helper** — `_normalize_timestamp` /
  `_time_diff_seconds` (`src:17-40`) duplicated verbatim in `matcher.py:19-36` (and a
  third variant family lives in delta_report/json_report). One `timestamps.py` util.
- **R3-099 · minor** — exit price uses `exits[-1]` only (`src:127-130`); multi-exit
  trades compare solely the last leg while PnL sums all legs — asymmetric but documented
  nowhere.
- Tolerance math (`_pct_diff` zero-denominator handling, `src:26-31`) is correct and
  consistently reused.

## `validation/comparison/matcher.py`

- **R3-100 · minor · algorithmic** — greedy O(n²) first-fit matching
  (`src:126-158`): with duplicate trades inside the tolerance window, pairing follows
  array order rather than proximity; worst case (few thousand trades × thousands) is
  noticeable inside the CLI. Sort candidates by entry-time distance or bucket by
  rounded timestamp before the linear scan.
- Docstring honestly declares the greedy semantics (`src:64-71`) — good.
- **R3-101 · minor** — `MatchResult.matched` tuples of pydantic models serialize fine
  but `matched: list[tuple[Trade, Trade, MatchKey]]` forces positional unpacking at all
  three call sites; a tiny named tuple would prevent swap bugs.

## `validation/comparison/anomalies.py`

- **R3-102 · smell · Speculative Generality** — `detect_duplicates`
  (`src:185-233`) can never fire: `TradeMatcher` marks actuals as consumed and never
  reuses a reference. ~50 lines of unreachable defense presented as a detection layer.
  Either keep as a cheap assertion with a comment, or delete.
- **R3-103 · minor · duplicated constant** — the error-fields set
  `{"direction", "pnl"}` (`src:240-243`) is repeated in `delta_report.py:161-162`;
  drift between them would make anomaly severity disagree with delta-report severity.
  Extract `CRITICAL_TRADE_FIELDS`.
- **R3-104 · minor** — `match_result: Any` + `**match_kwargs` forwarding
  (`src:53-90`): the same options must be passed identically to `detect_missing` /
  `detect_extra` when callers bypass `detect()`; typing is loose enough to allow
  silent inconsistency.

## `validation/comparison/delta_report.py`

- **R3-105 · minor** — see R3-103 (severity-set duplication, `src:161-162`).
- **R3-106 · minor · clarity** — `total_matched` excludes duplicates via
  `not in ("duplicate_match",)` — a 1-tuple membership test reading like a bug
  (`src:287-289`); use `!= "duplicate_match"`.
- Explanation builders are thorough and well-tested-looking; no defects found.

## `validation/importers/base.py`
Scanned — nothing found beyond a design note: the ABC's `load(source, **kwargs)`
doesn't formalize `source_timezone`, so `MT5XLSXImporter.load` accepts
`str | tzinfo` while `MT5HTMLImporter.load` accepts only `str` (interface drift,
see R3-114).

## `validation/importers/mt5_xlsx.py`

- **R3-107 · major · bug (silent direction corruption)** — direction is derived solely
  from the Typ cell: `direction_str = "LONG" if typ_entry == "buy" else "SHORT"`
  (`src:506-507`). The only hard requirements are Zeit/Richtung columns
  (`src:449-450`); if `Typ` is absent (locale variant, renamed column) or its values
  aren't exactly `buy`/`sell` lowercased, **every trade silently becomes SHORT** —
  direction mismatches then flood the comparison report, obscuring the real problem.
  Require the Typ column like Zeit/Richtung, or carry the raw value forward.
- **R3-108 · minor · dead code** — `total_return` derivation
  (`src:323-327`) requires `"initial_capital" in stats`, but initial capital is parsed
  into `meta`, never `stats` (`src:217-219` vs `src:118-120`) — unreachable as written.
  Either inject initial_capital into stats first or drop the block.
- **R3-109 · minor** — SL/TP columns matched only against exact `"S / L"`, `"S/L"`
  (`src:407-408`); MT5 locale variants (`S\L`, spaced headers) silently yield no SL/TP
  for all trades (meta None) rather than an error — weakens the risk-comparison stage
  invisibly.
- **R3-110 · minor** — pairing walk (`src:459-560`): partial closes (one `in`, several
  `out`s) attach only the first `out`; a stray `out` before its `in` is silently
  skipped. Acceptable simplification, but multi-partial positions under-report exits
  with no trace.
- **R3-111 · minor** — `net_pnl` computed only when `fees != 0`
  (`src:546-547`): a zero-fee trade leaves `net_pnl=None` while `pnl` is known —
  inconsistent null semantics for consumers (stats calculators fall back to `pnl`).
- **R3-112 · smell · God class** — 619 lines doing sheet-scanning, meta parsing,
  stat-label translation, SLTP mapping, trade reconstruction and coercion. The
  stat-label table + parsers (`src:24-69`, `src:331-395`) are duplicated wholesale in
  `mt5_html.py` (**the largest Duplicated Code instance in the area**) — extract a
  shared `mt5_stats.py`.

## `validation/importers/mt5_html.py`

- **R3-113 · major · bug (numeric parsing)** — `_build_sltp_map` parses HTML strings
  with `float(sl.replace(",", "."))` (`src:398-399`): German-formatted thousands
  separators (`"1.234,56"`) silently become `1.234` (and `"2.500,0"` → `2.5`). Wrong
  SL/TP values then flow into `trade.meta` and the risk comparator. The xlsx twin
  doesn't have this problem because Excel supplies floats. Use the same
  strip-thousands-then-decimal logic used elsewhere (`_coerce_stat`, `src:366`).
- **R3-114 · minor · inconsistent tz validation** — invalid IANA names raise raw
  `ZoneInfoNotFoundError` (a `KeyError` subclass) from deep inside `_to_iso`
  (`src:561-564`), while the xlsx importer raises a clean
  `ValueError("Unknown source timezone: …")` (`mt5_xlsx.py:79-80`). Also `import
  zoneinfo` sits inside the per-timestamp hot loop.
- **R3-115 · minor · provenance gap** — `raw_metadata` omits `source_timezone`
  (`src:97`) although the xlsx importer records it (`mt5_xlsx.py:124`); this directly
  violates the reference-artifact contract's principle 3 ("treat timestamps as
  source-local until an importer explicitly records the source timezone").
- **R3-116 · minor · formatting hazard** — the entire `load()` body is indented one
  extra level (`src:74-121`); valid Python, but any future edit at the wrong indent
  level silently changes control flow. Re-indent.
- **R3-117 · minor · dead weight** — `_parse_stats` applies 4-column rows twice:
  the stride-2 loop already consumes pairs, then the `if len(cells) == 4` block
  re-applies the same two pairs (`src:262-269`). Idempotent today, pure confusion.
- **R3-118 · smell** — see R3-112: share the stat-label/coercion machinery with
  mt5_xlsx instead of the current ~200-line copy.

## `validation/readiness/models.py`

- **R3-119 · minor · leftover** — `require_trade_report/regression/risk_report/stats`
  are declared twice (`src:65-68` and again `src:76-80`, with a stale comment above the
  second block). Identical values today, but a future edit to one copy shadows the
  other. Delete the duplicate block.

## `validation/readiness/validator.py`

- **R3-120 · major · duplicated gate logic** — missing-required-artifact handling is
  implemented twice: `_check_required_artifacts` (`src:148-193`) emits
  `required_artifact_*` error checks, and a second inline block
  (`src:85-106`) appends `required_<artifact>` error checks for the same conditions.
  Consequences: `errors` double-counted (two error checks per missing artifact →
  `ReadinessReport.errors=2`), recommendations duplicated, and any future threshold
  change must be made twice. Remove one implementation.
- **R3-121 · minor · placeholder semantics** — when an artifact is absent the
  corresponding substantive checks are recorded as `passed=True, severity="info",
  "skipping"` (`src:205-242`, `src:403-413`, `src:451-461`, `src:558-577`). Correct
  only because the required-artifact errors fire; if someone sets
  `require_trade_report=False` the trade gates vanish silently from the report. A
  `skipped` severity would express this honestly.
- **R3-122 · smell** — the four threshold checks (missing/extra/critical/warning,
  `src:259-391`) are one pattern four times; a small spec-table helper halves the class.

## `validation/regression/runner.py`

- **R3-123 · minor** — `TradeComparator(cfg.trade_config)` constructed inside the
  per-trade loop (`src:318-321`); hoist.
- **R3-124 · minor** — infinity special-casing handles only `+inf`
  (`src:90-110`); a `-inf` (possible from degenerate stats) falls into numeric
  comparison and produces `rel_diff=1.0`-style nonsense instead of the intended
  "both infinite" / "differs" branches.
- **R3-125 · smell · Duplicated Code (cross-file)** — `_STATS_FIELDS`,
  `_is_numeric`, the abs/rel diff math and `_build_stat_explanation`
  (`src:22-75`, `src:78-157`) are duplicated almost verbatim in
  `reporting/json_report.py:65-204`. Two tolerance engines that must stay in lockstep —
  extract one `stat_deltas.py`.

## `validation/reporting/json_report.py`

- **R3-126 · smell** — see R3-125 (owns the second copy of the stats-compare engine);
  additionally `StatDelta` vs `FieldDelta` are two shapes for the same concept with a
  converter (`src:261-272`) bridging them.
- **R3-127 · minor · dead branch** — in the act-stats fallback
  (`src:404-411`), `elif hasattr(platform_result, 'stats')` and
  `elif isinstance(platform_result, dict)` are unreachable given the parameter is
  `ValidationReference | list[Trade]` (a list has no `.stats`; dicts aren't in the
  union). Speculative Generality.
- **R3-128 · minor · clarity** — ref-stats fallback ladder
  (`src:395-401`): the final `else` duplicates the first condition's call; collapse.
- `_SafeJSONEncoder` + recursive sanitize (`src:30-55`) correctly kills NaN/Inf for
  strict JSON — good defensive piece.

## `validation/statistics/cross_platform.py`

- **R3-129 · major · stale architecture** — this parallel statistics module still
  exists while `stats/calculator.py`'s docstring declares "`validation/statistics/` was
  removed" (`calculator.py:4`). Either the doc lies or this module is a zombie. It also
  hardcodes three concrete MT5 cases + one QC row as code (`src:68-98`) — every new
  parity case edits this file (Shotgun Surgery). Move case definitions to a data file
  and reconcile the module's status.
- **R3-130 · minor** — `_extract_mt5_period` reads only the first 12 rows
  (`src:283`) and swallows all exceptions; period located lower in the sheet silently
  disables the CAGR derivation (`src:208-213`) with no note in the row payload.
- **R3-131 · minor** — deferred `from runner.stats import compute_stats_for_result`
  inside `_load_platform_stats` (`src:241-242`) papers over a circular dependency;
  document or break the cycle properly.
- Win-rate >1 normalization and finite-guard helpers (`src:217-228`, `src:300-309`)
  are sound.

## `validation/risk/models.py`
Scanned with one note: every model opts out of immutability (`model_config =
ConfigDict(frozen=False)` at `src:23,33,46,63`) which is the default anyway — the
explicit declarations suggest a past frozen→unfrozen migration; and they enable the
input-mutation smell fixed in R3-133. Nothing else found.

## `validation/risk/fixed_risk.py` + `validation/risk/percent_risk.py`

- **R3-132 · smell · Duplicated Code (major)** — the two sizing models are ~90%
  identical: same validate-fallback-stop logic, same lot rounding, same clamps, same RR
  computation (`fixed_risk.py:34-131` vs `percent_risk.py:34-141`). Only the
  risk-amount derivation differs (constant vs balance×pct). Extract a base
  `StopDistanceSizer`; divergence has already begun (percent validates
  `account_balance`/`risk_percent>0`, fixed never validates `risk_amount` sign — a
  negative config produces absurd negative sizes floored by lot rounding).
- **R3-133 · minor · hidden mutation** — both `validate()` methods write the resolved
  stop back into the caller's model (`fixed_risk.py:60-61`, `percent_risk.py:60`);
  action-at-a-distance on a supposedly passive input object. Return the resolved stop
  instead.
- **R3-134 · minor · semantic footgun** — sizes below `min_position_size` are silently
  zeroed (`fixed_risk.py:92-93`, `percent_risk.py:102-103`), making `risk_amount`=0;
  downstream comparisons can then "pass" on two zeroed profiles. At least tag the
  profile (e.g. `details.clamped=True`).

## `validation/risk/adapter.py`

- **R3-135 · minor** — direction mapping is binary: `action = "buy" if direction ==
  "LONG" else "sell"` (`src:41`); any unexpected direction string becomes a short.
- **R3-136 · minor** — `size()` invents a default `PercentRiskConfig(risk_percent=0.01)`
  when config is omitted (`src:190-191`): a silent 1%-of-balance assumption inside a
  *validation* tool whose purpose is to expose assumptions. Require explicit configs.
- **R3-137 · smell** — stop-loss/take-profit extraction from attributes-then-meta
  (`_trade_to_signal`, `src:44-51`) is duplicated a third time in
  `risk/comparison._compute_profile` (`src:165-171`) and again inside both sizing
  models' validate(); one resolver function, please.

## `validation/risk/comparison.py`

- **R3-138 · minor · checklist (over-broad except)** — `_compute_profile`
  swallows everything with `except Exception: return None` (`src:191-192`); genuine bugs
  (TypeError on bad meta values) degrade into the vague "Could not compute risk profile
  for one or both trades" warning. Catch the expected (ValueError) and let the rest
  propagate, or log exc_info.
- **R3-139 · minor · inconsistent tolerance semantics** — trade comparator passes if
  pct OR abs (`comparison.py:280-282`); risk comparator requires pct AND abs
  (`src:297-301`). Same vocabulary, opposite logic, no documentation of why. At minimum
  a comment; better, unify.
- **R3-140 · minor · asymmetric gating** — missing expected trades fail
  (`passed=False`, `src:127-142`) while extra actual trades pass (`passed=True`,
  `src:144-159`); defensible, but the report's `passed=all(...)` then lets a platform
  that invented trades clear the risk gate. Mirror the delta-report treatment of extras.
- **R3-141 · smell · layering** — `RiskReportBuilder._to_trade_delta` reaches into
  `DeltaReportBuilder._field_result_to_delta` — a private staticmethod of another
  class (`src:400-402`). Promote it to a public helper.

## `validation/importers/quantconnect_result.py`

The most defensively-built file in the area (Decimal money math, evidence
classification, hash pinning, explicit limitations block). Findings are nits:

- **R3-142 · minor · perf** — `_verify_csv` does an O(n²) multiset match
  (`src:902-916`): for each CSV row it linearly rescans the remaining JSON rows. Large
  order exports (10⁴ rows) → 10⁸ field comparisons. Index JSON rows by a canonical key
  (id + entry/exit times) and pop from a dict.
- **R3-143 · minor · tz-naive assumption** — legacy format fallbacks
  (`"%Y-%m-%d %H:%M:%S"`, `"%m/%d/%Y %H:%M:%S"`, `src:200-208`) are stamped UTC
  (`src:212-213`) and `raw_metadata.source_timezone` then claims `"UTC"`
  (`src:418`) even when the export was exchange-local. Conflicts with the contract's
  provenance principle for these legacy shapes.
- **R3-144 · minor** — `_submission_clusters` calls `min(sizes)`/`max(sizes)`
  (`src:670-672`); safe only because `orders` non-emptiness is enforced upstream
  (`src:347-349`) — an invariant worth a comment or an assert.
- **R3-145 · minor** — strictness choices (fee remainder must be exactly zero,
  `src:737-743`, `src:802-809`) are clearly deliberate and documented in the contract
  doc; noting for the panel that this importer intentionally rejects borderline-real
  exports — fine, but expect support traffic.

---

# Cross-cutting themes (for the fix-plan discussion)

1. **One OHLCV resample rule, six implementations** (R3-045, R3-011, R3-066):
   `data_tools.resample.resample_ohlcv` already exists — adopt it everywhere.
2. **Two-and-a-half stats engines** (R3-125/R3-126, R3-129): consolidate
   `stats/calculator.py` as the single source and delete/absorb
   `statistics/cross_platform.py`'s duplicated math.
3. **MT5 importer twins** (R3-112/R3-118): extract the shared German-label stat table,
   coercers, exit-reason inference and trade assembly.
4. **Durability of canonical parquets**: every writer (base.merge, grid
   normalize, dukascopy source-meta) does non-atomic in-place writes (R3-002, R3-019,
   R3-061). One shared `atomic_write_parquet` + temp-rename fixes the class.
5. **End-date semantics** vary by downloader (inclusive dukascopy vs exclusive
   yfinance/histdata) — R3-068/R3-077; standardize and document.
6. **SQL string building** in both dolthub modules (R3-051, R3-084) and **timeoutless
   subprocesses** (R3-052) are the two clearest fixed-checklist violations in the area.

# File accounting

| File | Status |
|---|---|
| data_tools/__init__.py | scanned, nothing found |
| data_tools/auto_download.py | R3-033…037 |
| data_tools/cache_manager.py | R3-030…032 |
| data_tools/catalog_builder.py | R3-023…029 |
| data_tools/dolthub_manager.py | R3-051…056 |
| data_tools/download_grid_market_data.py | R3-061…066 |
| data_tools/downloaders/__init__.py | scanned, nothing found |
| data_tools/downloaders/alpaca_dl.py | R3-071…072 |
| data_tools/downloaders/base.py | R3-019…022 |
| data_tools/downloaders/ctrader_dl.py | R3-080…083 |
| data_tools/downloaders/dolthub_dl.py | R3-084…087 |
| data_tools/downloaders/dukascopy_native.py | R3-001…011 |
| data_tools/downloaders/histdata_dl.py | R3-076…079 |
| data_tools/downloaders/huggingface_dl.py | R3-073…075 |
| data_tools/downloaders/jforex_dl.py | R3-012…018 |
| data_tools/downloaders/yfinance_dl.py | R3-067…070 |
| data_tools/dukascopy_discovery.py | R3-088…091 |
| data_tools/migrate_csv.py | R3-057…060 |
| data_tools/resample.py | R3-044…046 |
| data_tools/signal_import.py | R3-047…050 |
| data_tools/validate.py | R3-038…043 |
| validation/__init__.py | scanned, nothing found |
| validation/cli.py | R3-094…096 |
| validation/comparison/__init__.py | scanned, nothing found |
| validation/comparison/anomalies.py | R3-102…104 |
| validation/comparison/comparison.py | R3-097…099 |
| validation/comparison/delta_report.py | R3-105…106 |
| validation/comparison/matcher.py | R3-100…101 |
| validation/importers/__init__.py | scanned, nothing found |
| validation/importers/base.py | scanned (note in R3-114 context) |
| validation/importers/mt5_html.py | R3-113…118 |
| validation/importers/mt5_xlsx.py | R3-107…112 |
| validation/importers/quantconnect_result.py | R3-142…145 |
| validation/readiness/__init__.py | scanned, nothing found |
| validation/readiness/models.py | R3-119 |
| validation/readiness/validator.py | R3-120…122 |
| validation/reference.py | R3-092…093 |
| validation/regression/__init__.py | scanned, nothing found |
| validation/regression/runner.py | R3-123…125 |
| validation/reporting/__init__.py | scanned, nothing found |
| validation/reporting/json_report.py | R3-126…128 |
| validation/risk/__init__.py | scanned, nothing found (exporting a private name `_trade_to_signal` in `__all__` is odd but harmless) |
| validation/risk/adapter.py | R3-135…137 |
| validation/risk/comparison.py | R3-138…141 |
| validation/risk/fixed_risk.py | R3-132…134 |
| validation/risk/models.py | scanned (note, R3-132 context) |
| validation/risk/percent_risk.py | R3-132…134 |
| validation/statistics/cross_platform.py | R3-129…131 |
| validation/stats/__init__.py | scanned, nothing found |
| validation/stats/calculator.py | scanned, nothing found (cleanest file in the area; docstring contradiction covered by R3-129) |

# Finding counts

- critical: 1
- major: 19
- minor: 62
- total: 82
