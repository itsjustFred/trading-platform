## data-validation

**Panel:** model ox-alpha; three blind, independent reviewers (r1, r2, r3). Synthesized by ox-alpha directly from the three partial reports after repeated sub-agent spawn failures; all reviewer-vs-reviewer disputes, all critical findings, and the adopted reference-only claims were settled by reading the cited source lines directly (log in §F).
**Reference report** (`code-review-full-gpt-5.6-luna-2026-08-23.md` §5 "data & validation", D1–D20/d1–d12) consulted as hints only; agreements and disagreements logged in §F.
**Scope:** 50 tracked files (`data_tools/**`, `validation/**`). Every file accounted for in §G.

**Merged totals: 🔴 Critical 2 · 🟠 Major 51 · 🟡 Minor 118**

---

### A. Critical

**DV-C1 · Merged bid/ask tick download is broken twice over: positional args misaligned + duplicate keyword → guaranteed `TypeError`.**
Call site `dukascopy_native.py:1016-1028` passes `(instrument, start_dt, end_dt, batch_size, pause_ms, max_retries, pipeline_batches=pipeline_batches, …)` into the signature `(instrument, start_dt, end_dt, batch_size, pipeline_batches, pause_ms, max_retries, *, …)` (`:774-786`): `pause_ms` lands in `pipeline_batches`, `max_retries` in `pause_ms`, and `pipeline_batches` is then passed again as a keyword → `TypeError: multiple values for argument 'pipeline_batches'` before any work. *(r3 R3-001 critical; r1 found the same misalignment at `:1016-1028` rated major; Luna D1 critical — severity settled critical: the whole `--merge-tick`/`--merge-s1`/`offer_side=both` path cannot execute and no test covers it.)*

**DV-C2 · "Ask" columns are bid data: both sides fetch identical URLs.**
In the same function, `bid_urls = generate_tick_urls(instrument, start_dt, end_dt)` and `ask_urls = generate_tick_urls(...)` are the *same call* (`dukascopy_native.py:790-791`) — tick URLs carry no side component, so the ask leg re-downloads bid files and the inner merge (`:839-842`) produces ask columns equal to bid. Silent data corruption for every merged-tick dataset. Currently masked only by DV-C1's TypeError firing first. *(r1 critical, verified against source during synthesis; r2/r3 missed it.)*

---

### B. Major — data_tools/

**Downloader core (`base.py`)**
- **DV-B1 · Non-atomic read-modify-write of canonical parquets — single point of failure for every downloader.** `_merge_and_store_single_file` (`base.py:151-184`) reads existing → concat → dedupe → in-place `to_parquet`; a crash mid-write leaves a corrupt parquet at the canonical path that resume logic then trusts, and concurrent downloads (grid `--workers 6`) interleave and lose rows. *(r2 major, r3 major, r1 minor — settled major: every downloader funnels through this method.)* Related non-atomic writers: `download_grid_market_data.normalize_ohlcv_columns` (`:96-126`), `_write_source_meta_index` (`dukascopy_native.py:869-884`, unlocked + non-atomic, corrupt JSON silently degrades catalog loads to `{}`). *(r1/r3)*
- **DV-B2 · Resume point trusts row-group max statistics.** `_read_latest_timestamp` scans backwards assuming timestamp-ordered row groups (`base.py:207-214`, fallback `:215-223`); externally produced/unsorted parquet yields a mid-history resume that silently skips ranges. *(r1 major, r2 minor, r3 R3-021 minor — settled major on silent-wrong-range impact.)*

**Dukascopy native** (besides DV-C1/C2)
- **DV-B3 · Zero-volume candles deleted.** `_assemble_candle_dataframe` filters `df["volume"] > 0` (`dukascopy_native.py:636`) — genuinely flat FX minutes become gaps propagating into resampled h1/d1 OHLC windows. *(r1)*
- **DV-B4 · Full ISO start/end datetimes crash the native path.** `datetime.fromisoformat(x).replace(tzinfo=utc)` (`:983-984`) raises on already-aware inputs like `"2024-01-01T13:45:00+00:00"`; only date-only strings survive. *(r1)*

**JForex**
- **DV-B5 · First failing JVM candidate aborts startup.** `_ensure_jvm_started` re-raises inside the candidate loop (`jforex_dl.py:551-552`), skipping remaining candidates and the JPype-default fallback the `startup_errors` collection shows was intended. *(all three reviewers)*
- **DV-B6 · Remote JARs loaded into the JVM with zero integrity checking.** `_download_jforex_jars_from_jnlp` (`:280-311`) fetches jar/nativelib hrefs from the env-overridable `DUKASCOPY_JNLP_URL` and puts them on the classpath (`startJVM :549`) — no checksum/signature/host allow-list — while the module meticulously trust-checks the much smaller libjvm surface (`:356-437`). Supply-chain RCE if env/DNS is hostile. *(all three; Luna D4)*
- **DV-B7 · Jar-bootstrap failures swallowed into a generic warning.** `except Exception: logger.warning(...); return [], f"unavailable"` (`:352-356`) collapses download/parse/security failures into a later generic startup error. *(Luna D5 only — verified against source and adopted.)*
- **DV-B8 · Partial artifacts persisted before a failing merge.** With `offer_side="both"`, side files are written (`:1146-1147`) before the inner join (`:1148`); a zero-overlap merge raises with two fresh side files on disk poisoning `_resolve_resume_start`. *(r2)*
- **DV-B9 · `DownloadResult.path` is a directory for merged downloads.** `output_path.parent` returned (`:1189`) violates the base contract ("where the parquet was saved"); consumers calling `.stat()`/open break. *(r1 major, r2 minor — settled major: contract break on a public result type.)*
- **DV-B10 · Timeframe-suffix string surgery mangles filenames.** Chained `.replace()` (`_normalize_timeframe_for_file_suffix :967-970`) turns `"15m"` into `"1m5"` (later `"15m"→"m15"` never matches); output files like `EURUSD_1m5_bid.parquet` are unparseable by the catalog timeframe regex and vanish/misclassify. *(r3 R3-012; r2 flagged the pattern as fragile)*

**Catalog / cache**
- **DV-B11 · Silent `"1d"` default poisons the catalog.** `parse_ohlcv_path` defaults unparseable stems to daily (`catalog_builder.py:251,264,274`, mostly unlogged); a mislabeled dataset is worse than a skipped one downstream. *(r2 major; r3 R3-027 minor — settled major on research-data impact.)*
- **DV-B12 · Global cache lock held across the entire cold build.** `get_catalog` runs `build_catalog`'s minutes-long filesystem scan inside `_CACHE_LOCK` (`cache_manager.py:240-255`), blocking every server cache reader/writer. *(all three)*
- **DV-B13 · `unsafe_no_copy=True` hands out live shared mutable state.** `get_catalog_ohlcv_entries` (`:271-279`) — one caller mutation poisons the cache for all future readers. *(all three)*
- **DV-B14 · Prefix-match storage classification.** `_detect_storage` uses `startswith` (`catalog_builder.py:97-102`) — sibling dirs like `cache_backup` classify as cached; same pattern in `validate.py:94`. *(r1, r2, r3)*

**Auto-download / coverage gating**
- **DV-B15 · Coverage satisfied by min/max alone — multi-year holes pass.** `_has_required_coverage` compares only first/last available dates (`auto_download.py:247-262`); two bars bracketing a gap certify the range, so backtests run hole-filled with no download. *(r2; corroborated by r3's gate-integrity theme.)*
- **DV-B16 · Failed month/year downloads are silently skipped, then look covered.** HuggingFace per-month `except Exception: warnings.append(continue)` (`huggingface_dl.py:257-261`) and HistData per-year None returns (`histdata_dl.py:224-226`, holes never back-filled since resume keys off last timestamp) produce gappy datasets that DV-B15's check then certifies. *(r2 major; r1 corroborated.)*
- **DV-B17 · Coverage checks read entire timestamp columns, repeatedly.** `pd.read_parquet(columns=["timestamp"])` full scans per file per requirement, twice per run (`auto_download.py:138, 275-277`) — minutes/GB on tick stores while the catalog already persists min/max metadata. *(r2 major; r1 minor — settled major.)*
- **DV-B18 · One failed plan step aborts the remaining fallback sources.** Exceptions propagate out of `_download_missing_ohlcv_data` steps instead of continue-on-failure (`auto_download.py:103-108, 396-488`), so HF failing prevents the Alpaca fallback that was planned. Also: end-only requirements collapse to a single-day HF request (`:377-381`). *(r1)*
- **DV-B19 · HuggingFace pulls whole-market monthly shards per symbol with no pushdown/cache.** `pd.read_parquet(url)` then client-side symbol filter (`huggingface_dl.py:253-270`); a long history means hundreds of large payloads per ticker plus rate-limit mid-loop failures leaving partial merges that look complete. *(r1, r2; Luna D7 overlaps)*

**yfinance / ctrader / histdata specifics**
- **DV-B20 · yfinance `4h` bars use UTC calendar buckets, misaligned vs FX sources.** 60m fetch resampled on midnight-UTC origin (`yfinance_dl.py:53, 253-269`) produces 09:30/13:30-style splits that never match dukascopy/jforex session-aligned `4h` files in comparisons. *(r2 major; Luna D13; r1 alignment note.)*
- **DV-B21 · Intraday clip silently truncates requested history and defeats coverage loops.** 60-day Yahoo window discards older resume points with only an in-result warning that `auto_download` drops on the floor (`yfinance_dl.py:84-112, 169-183`) — ranges >60 days back re-download the same window forever. *(r1)*
- **DV-B22 · Tick CSVs stored under `1m.parquet`.** `ctrader_dl.py:156-157` resamples only ≠1m, so raw ticks imported with `--timeframe 1m` keep the name while bar-based consumers trust it; metals (XAUUSD/XAGUSD) also land under asset_class `forex` (`:143`). *(r2 major; r3 minor — settled major on mislabel impact.)*

**Resample / validate**
- **DV-B23 · Resample overwrites without merging — stored history can regress.** `resample_file` writes `{tf}.parquet` beside the source (`resample.py:104-106`); a re-run after the 1m source gained new history replaces higher-TF files with only the resampled window, and grid refresh paths hit exactly this (`download_grid_market_data.py:225-236`). *(r1 major, r3 history-loss risk.)*
- **DV-B24 · Gap detector uses pandas labels positionally after `dropna()`.** `large_gaps.index[:20]` labels fed to `ts.iloc[idx±…]` (`validate.py:162-186`) report wrong gap boundaries whenever any timestamp was coerced/dropped. *(all three; Luna D8)*
- **DV-B25 · `--data-dir` mode crashes under its own flag.** `path.relative_to(DATA_DIR/"ohlcv")` guarded by a `startswith` on a different root (`validate.py:94`) raises `ValueError … not in the subpath` for custom roots, killing `validate_all`. *(r3)*

**Signal import / dolthub**
- **DV-B26 · Unknown signal actions default to `"buy"`.** `_parse_action` (`signal_import.py:67-77`) maps typos, "Exit", "Close", empty cells to long entries — replay fabricates trades with no error. *(all three)*
- **DV-B27 · SQL built by f-string in both dolthub modules.** `dolthub_manager.py:138` interpolates `symbol`/table names into `dolt sql -q`; `dolthub_dl.py:94-102` interpolates symbol/table/branch/dates into the API query. Checklist item violated verbatim; exploitability bounded (repo-derived/internal input) but quote-bearing symbols break queries and the pattern normalizes injection-by-habit. *(r1 critical×2, r2 major/minor, r3 major×2, Luna D2/D3 major — settled major per bounded reachability; r1's criticals noted as dissent.)*
- **DV-B28 · Every `dolt` subprocess runs without timeout.** `subprocess.check_call/run` at `dolthub_manager.py:32, 60-65, 77, 111-116, 133-144` — a hung clone/pull blocks forever; scheduler-invocable. *(r2 critical, r1 major, r3 major — settled major-high: availability failure, visible not silent; r2's critical noted as dissent.)*
- **DV-B29 · DoltHub API pagination ignored — silent truncation stored as complete.** Single `rows` read with no cursor (`dolthub_dl.py:145-154`); capped responses merge into canonical store as if complete and resume then believes coverage exists. *(r2, r3)*

**Grid tooling**
- **DV-B30 · Alias freshness = equal file size.** `ensure_alias_copy` skips copies when sizes match (`download_grid_market_data.py:155-161`) — regenerated canonical data never propagates to the alias backtests actually read. *(r1, r2; r3 minor — settled major.)*
- **DV-B31 · Grid fan-out races and rate-limit storms.** Duplicate aliases canonicalize to the same target and run concurrently in the process pool racing one parquet write (`:280-311`, no dedupe); combined concurrency `workers × batch_size × pipeline_batches` ≈ 240 simultaneous requests with no cross-process throttle. *(r2, r1; Luna d6 overlap)*

**Discovery**
- **DV-B32 · Genesis discovery corrupted by Sunday/holiday probes and transport failures-as-misses.** Probe helpers dodge only Saturday (`dukascopy_discovery.py:150-159`); broad `except Exception → False` (`:145-147`) records network errors as permanent "misses" saved into the genesis index by monotonic binary search. Discovered genesis shifts by days, permanently. *(r1, r2)*

---

### B (cont.) Major — validation/

**Reporting / gates**
- **DV-B33 · Undefined name in public signatures.** `TradeComparisonConfig` annotated at `json_report.py:297,360,561` but never imported (imports `:8-27` lack it). Latent-only today thanks to `from __future__ import annotations`; any runtime annotation consumer raises `NameError`. *(r1 critical, r2 major — settled major: no reachable runtime failure today; r1's critical noted as dissent. Verified by direct read.)*
- **DV-B34 · `-inf` asymmetry produces false mismatches.** Infinity handling covers only `+inf` (`json_report.py:137-163`; twin in `regression/runner.py:90-110`): two identically `-inf` stats (e.g. profit_factor with zero losses) compare as inf-diff and fail. *(r1 major, r3 minor — settled major: validation gate integrity.)*
- **DV-B35 · OR-tolerance pass logic weakens every numeric gate.** `passed = passed or (abs_ok or rel_ok)` (`json_report.py:180-183`; same OR shape in `comparison.py:280-282` for PnL) lets fields pass whichever tolerance is looser — inconsistent thresholds across scale-free fields like win_rate. *(r1 major; r2/Luna d8 minor — settled major.)*
- **DV-B36 · Required-artifact failures double-counted.** `_check_required_artifacts` emits `required_artifact_*` errors AND the main body appends parallel `required_<artifact>` errors for the same facts (`readiness/validator.py:75-106`): doubled error counts, drift-prone duplication. *(all three)*
- **DV-B37 · Risk gate can pass on structurally failed comparisons.** Failed risk results with non-numeric fields contribute neither issue nor check failure (`validator.py:463-492`), empty `trade_results` passes vacuously via `max(default=0.0)` (`:494-495`), and report-level verdicts are never consulted. *(r1 major; r2 minors — settled major.)*
- **DV-B38 · Extra platform trades pass the risk comparison.** Unmatched actuals constructed `passed=True, severity="info"` (`risk/comparison.py:144-159`) feed `all(r.passed …)` (`:338`) — a strategy inventing trades certifies as risk-consistent; meanwhile `delta_report.build` fails the whole report on *any* extra (`:270-275`) and anomalies rates EXTRA_TRADE "info" (`anomalies.py:146`): three modules, three opinions on one event. *(r1, r2; gate-inconsistency folded here.)*
- **DV-B39 · Profile-computation exceptions swallowed.** `_compute_profile` returns None on anything (`risk/comparison.py:191-192`), degrading sizing bugs into generic warnings. *(all three; Luna D15)*
- **DV-B40 · Markdown CLI runs the entire pipeline twice.** Pass/fail derived by re-running `generate_json_report` after `generate_markdown_report` already computed it (`cli.py:237-250`) — double wall-clock plus divergence risk. *(all three)*

**MT5 importers**
- **DV-B41 · Entry-side commissions dropped — net PnL overstated.** Both importers read Kommission/Swap from the exit row only (`mt5_xlsx.py:509-511`, `mt5_html.py:476-477`); MT5 books entry commission on the entry deal. *(r1, r2)*
- **DV-B42 · Direction inferred from an unrequired column — everything becomes SHORT.** `direction = LONG if typ == "buy" else SHORT` (`mt5_xlsx.py:506-507`; html `:473-474`): missing/locale-variant `Typ` silently relabels every trade; mismatches then flood reports obscuring the real fault. *(r3 major; r1/r2 partial.)*
- **DV-B43 · Pairing loses split fills.** xlsx walks positionally attaching only the first exit and abandoning entries on a second "in" (`mt5_xlsx.py:459-560`); html groups by order id keeping last-in/last-out (`mt5_html.py:436-451`) — the two importers disagree on the same report, and incomplete pairs vanish without log (`html :461-462`). *(r1, r2 majors; Luna D20)*
- **DV-B44 · German-formatted SL/TP parsing corrupts or aborts.** `float(sl.replace(",", "."))` (`mt5_html.py:398-399`): `"2.500"` silently becomes `2.5`; `"1.234,56"` becomes `"1.234.56"` and raises ValueError aborting the import (r3 reported pure silent corruption; r1 reported pure abort — both mechanisms real, settled as stated here after arithmetic check). *(r3 major, r1 major.)*
- **DV-B45 · `total_return` derivation unreachable.** Gates on `"initial_capital" in stats` but that key is only ever written to meta (`mt5_xlsx.py:324-327` vs `:217-219`). *(r1, r2, r3)*

**Stats / regression**
- **DV-B46 · Validation statistics computed on gross PnL — fees ignored.** `_realized_pnl` sums exit PnLs (`stats/calculator.py:52-56`); Sharpe/Sortino/drawdown systematically biased whenever fees matter. Breakeven-trade denominator divergence from MT5 adds silent skew (`:118-120`). *(r1 major; r3 scanned this file clean — omission noted.)*
- **DV-B47 · Regression matching ignores configured time tolerances.** `TradeMatcher.match(...)` called bare (`regression/runner.py:315`) while config carries entry/exit seconds — exact-second equality decides matches regardless of settings; combined with DV-B38's strictness, regression and readiness disagree on identical artifacts (`:336-341`). *(r1, r2)*

**Risk sizing**
- **DV-B48 · Sub-minimum sizes floored to zero, silently.** Lot rounding floors to 0 and `< min_position_size → 0.0` (`fixed_risk.py:85-93`, `percent_risk.py:97-103`): undersized trades produce all-zero profiles that trivially "match", and min-size semantics are inverted. *(r1, r2 majors; r3 footgun.)*
- **DV-B49 · Dual sizing paths disagree on missing stops.** `calculate_trade_risk` delegates to RiskEngine which *fabricates* a fallback stop, while `size()` raises (`risk/adapter.py:112-170` vs `:172-202`) — the validation framework both forbids and invents missing evidence depending on entry point. *(r2; r1 temporal-coupling note.)*
- **DV-B50 · Hardcoded `$100` / `1%` defaults inside a validation tool.** `size()` invents `FixedRiskConfig(risk_amount=100.0)` or `PercentRiskConfig(risk_percent=0.01)` when config omitted (`adapter.py:172-202`) — silent assumptions in the tool whose purpose is exposing assumptions. *(r1, r2.)*

**Cross-platform matrix**
- **DV-B51 · Zombie statistics module with case-paths as code.** `statistics/cross_platform.py` still exists while `stats/calculator.py:4` declares the package removed; three MT5 cases + one QC row hardcoded (`:68-98`, Shotgun Surgery), and a missing MT5 xlsx crashes the whole matrix build instead of recording `missing_artifacts` (`:114`). *(r3, r2, r1.)*

---

### C. Minor findings (selected; full detail in the three partials)

**data_tools/**
- `dukascopy_native.py` — end `00:00:00` silently rewritten to 23:59:59 (`:985-986`, r3); tick vs minute URL generators treat range ends differently (`:245-276`, r3); retry tuple misses aiohttp payload errors so one aborts whole batches (`:400`, r3); weekend HTML errors treated as no-data without retry (`:385-397`, r1/r2); trailing partial tick records dropped silently (`:559,582`, r2); inner join drops bid rows lacking ask counterparts at 1 ms offsets without warning (`:839-843`, r2); keyword-only params silently swallow duplicate positional args (`:1036-1041` et al., r2 — fragile plumbing); `_fetch_one` returns success-shaped result losing cooldown signal on timeout exhaustion (`:406`, r2).
- `jforex_dl.py` — interrupted jar download leaves truncated jar passing the size check next run (no temp+rename, `:293-299`, r1); date-only end gets −1 ms but datetime end excludes last day — boundary inconsistency (`:171-173`, r1/r2); float-ms truncation can shave final boundary (`:1030-1031`, r2); configured-but-untrusted JVM override vanishes silently (`:444-449`, r2); connect poll can spin past deadline (`:858-864`, r2); hardcoded `"2003-01-01"` default start disagrees with genesis JSON (`:1095`, r3); offer_side='both' s1 rejection fires only after auth/JVM startup (`:1105-1109`, r3).
- `base.py` — corrupt timestamps coerced+dropped without warning (`:233-239`, r1); parquet reader never closed (`:192`, all); `DownloadResult.start/end` as ISO strings (Primitive Obsession, r1).
- `catalog_builder.py` — import-time `logging.basicConfig` hijacks app logging (`:26`, all three; same in `validate.py:29`, `resample.py:22`, `dolthub_manager.py:12-15`); non-atomic catalog write (`:914-915`, r1/r2); timeframe-segment slice assumes normalized length (`:161-167`, r1); fundamentals fully materialized for row counts (`:843-846`, all); OHLCV size double-counting when cache merges share paths (`:488-497`, r1); loose `_TIMEFRAME_REGEX` accepts bare letters as timeframes (`:35`, r3); TOCTOU between stat and open under concurrent downloads (`:354-355`, r2).
- `cache_manager.py` — mixed locking discipline invites bugs (`get_overview_metadata` releases between check and setdefault, `:295-306`, r3/r1); stats double-count misses across cache layers (`:301`, r2).
- `auto_download.py` — six-letter-uppercase heuristic routes equities to dukascopy failing late (`:301-306`, r3); SYMBOL_CONFIG import errors swallowed into "not forex" rerouting (`:308-326`, r1/r2/r3); Alpaca fallback keyed on exception message substring (`:432-434`, all); HF/Alpaca July overlap window downloaded from both sources, winner by merge order, documented nowhere (`:21-22,374-393`, r1/r3/r2); timestamp-column full scans (perf, folded in DV-B17).
- `validate.py` — OHLC violations counted but never escalate status beyond warning (`:199-208`, r2); one pathological bar triple-counted across three masks (`:199-206`, r3); weekend/holiday skip logic misses Fri→Sat tails, holidays, crypto (`:173-178`, r3/r1/r2 — Luna D9 major dissent noted, panel holds minor: docstring overpromise not silent corruption); dead `EXPECTED_BARS_PER_DAY` (`:35-43`, r1/r3).
- `resample.py` — no guard against downsampling/equal-TF resample; `--from 1m --timeframes 1m` overwrites source (`:41-75,100-104`, r2 — related to DV-B23); `update_catalog` swallows failures leaving stale catalog (`:138-150`, r3/r1); dead pattern variable (`:118`, r3).
- `signal_import.py` — tz inferred from column-name substring "ny" (`:91-92`, r3); midnight-stamped intraday misclassified as daily (`:80-81,113`, all); DST fall-back naive localization raises uncaught AmbiguousTimeError (`:92+`, r1); iterrows perf (`:151`, r3).
- `dolthub_manager.py` / `dolthub_dl.py` — tz-naive timestamps written (`manager :148`, all); unchecked SELECT DISTINCT returncode yields cheerful zero symbols (`manager :111-118`, r3); stale intermediate CSVs on failure (`manager :164-199`, r3); JSONDecodeError outside retry umbrella (`dl :113-115`, r1/r3); repo-without-slash unpack error (`dl :89`, all); custom-query results stored under symbol "UNKNOWN" (`dl :236`, r2); earnings/rates export twins (duplication, both).
- `download_grid_market_data.py` — summary JSON to `Path.cwd()`/logs unlike every other artifact (`:347-350`, r1/r3); skip-check reads target parquets twice per run (`:232-236`, r3/r2); canonical open..close filled from whichever side exists — cross-source parity ambiguity (`:104-121`, r3); identity OIL_ALIASES mappings (r3).
- `yfinance_dl.py` — end day exclusive vs inclusive elsewhere (`:183`, r3/r1); resume restarts whole stored-day re-fetching (`:171-182`, r3); MultiIndex flatten takes `column[0]` fragility (`:213-217`, r1/r2); coerce-drop silent (`:242`, r1); dead capitalize-rename branch (`:233-235`, r3).
- `alpaca_dl.py` — missing volume filled int 0 dtype drift (`:179-180`, r1/r3); unbounded pagination on token churn / runaway first-run downloads with no bounds (`:256-263, 235-243`, r1/r2).
- `histdata_dl.py` — single-year `--years 2021` IndexError crash (`:286-287`, r1); Session-per-year churn (`:171`, r1/r3); tz-local year derivation off-by-one near New Year (`:118`, r3); string-vs-timestamp end comparison (`:143-144`, r3/r2); ZIP member assumed first entry + small-response heuristic misclassification (`:202-220`, r2/r1 — Luna D6 resource-hardening dissent noted, panel holds minor for a trusted-vendor scrape); imports inside functions (`:182,262`, r3).
- `huggingface_dl.py` — end defaults to coverage cap rather than today (`:233`, r1/r3); coverage constant duplicated across two modules (`:31`, r1); resample-before-dedupe month-seam vintage arbitrariness (`:277-283`, r2); no-op `1d→1d` resample rule (`:54`, r2).
- `ctrader_dl.py` — separator sniffing stops at first ≥5-column parse (`:181-189`, all); `matches[0]` arbitrary CSV pick on glob ambiguity (`:128-131`, r2); malformed-row to_datetime aborts import unlike siblings' coerce+drop (`:207`, r1/r3); asset-class taxonomy "index" vs canon "indices" (`:142-145`, r3); advertised Open API path raises not-implemented (Speculative Generality, r2/r3).
- `dukascopy_discovery.py` — asyncio.run per probe rebuilds session each probe (`:141-147`, all); linear re-scan always runs post-binary-search doubling probes against rate limits (`:294-350`, r1/r2); dead probe helpers (`:49-77`, r2); hardcoded REFERENCE_GENESIS for two symbols (`:39-46`, r3); global VERBOSE_LOGS mutation (`:536-537`, r1/r2).
- `migrate_csv.py` — hardcoded author-desktop sibling paths (`:30-32`, all); dest-exists skip permanently blocks re-migration after truncated write (`:129-131`, r1/r2); uppercase-sniffing metadata heuristic fragility (`:145-157`, all); rows lacking timestamps written anyway then catalog-defaults to 1d (compounding with DV-B11, r2).

**validation/**
- `reference.py` — deprecated tz-naive `datetime.utcnow` flows into `BacktestMeta.created_at` (`:51`; r1 major → settled minor: cosmetic provenance skew, sorting unaffected within area conventions; r2/r3 minor); second-granularity id collisions (`:87-89`, r1/r3); unused uuid4 import (r1).
- `cli.py` — `pnl_pct=args.stats_tolerance` flag conflation (`:190`, all); fractional seconds truncated by int() (`:200,209`, r3/r1); broad excepts convert programming errors to load failures (`:168-180`, r1); importer dispatch if-chain (r2).
- `comparison/comparison.py` — exit compares `exits[-1]` only while PnL sums all legs (`:127-130`, r1/r3); direction compared as raw strings (`:175-185`, r2); SL/TP meta-vs-attribute resolution differs from risk module's (`:152-160`, r1); byte-identical `_compare_optional_price/_pct` twins (all); `_normalize_timestamp` non-ISO raises abort whole run (`:17-23`, r2); alignment is exact-string intersection (r3).
- `matcher.py` — greedy first-fit mispairs simultaneous trades (major per r2, minor per r1/r3 — settled **minor-plus**: real but bounded by tolerance windows; characterization tests recommended before touching); O(n·m) scan (`:128-145`, r2/r3); empty-string exits raise via `_normalize_timestamp("")` treating open positions as errors (`:28`, r2); MatchKey records reference times only (`:150-155`, r2); stringly `**options` (r1/r2).
- `anomalies.py` — EXTRA_TRADE severity "info" contradicts delta_report's fail-on-any-extra (folded into DV-B38); `match_kwargs` consistency trap (`:53-90`, r1/r2); unreachable `detect_duplicates` given matcher consumption semantics (~50 lines Speculative Generality, r3; r1 notes defensive value); error-fields set duplicated (`:240-243`, all).
- `delta_report.py` — `total_matched` excludes duplicates via 1-tuple membership test reading like a bug (`:287-289`, r3); diffs re-derived instead of trusting comparator values risking explanation/pass contradiction (`:127-131`, r2); `_rel_diff` fifth copy (all); `_anomaly_to_delta` Repeated Switch with silent unknown-type fallthrough (`:183-237`, r2/r1).
- `importers/__init__.py` — exports 3 of 4 importers, QC missing (r1).
- `mt5_xlsx.py` / `mt5_html.py` — `_parse_stats` double-applies 4-column rows (idempotent today, `html :262-269`, r1/r2/r3); `_to_iso` returns raw garbage timestamps on parse failure (`xlsx :583-599`, r1/r2); SL/TP header variants silently yield None (`xlsx :407-408`, r3); ZoneInfoNotFoundError raw vs ValueError inconsistency + zoneinfo import in hot loop (`html :561-564`, all); extra-indentation load() body lint trap (`html :74-121`, r1/r3); settings scan swallows any "=" cell (`html :224-229`, r2); SLTP map keyed on raw cell objects type-mismatched lookups (`xlsx :419`, r2); unguarded `float()` on localized meta (`xlsx :219`, r2); net_pnl None-vs-0 inconsistent null semantics (`xlsx :546-547`, r1/r3); ~200 lines of shared German stat-label machinery duplicated wholesale (largest duplication in area — extract mt5_common, all three).
- `quantconnect_result.py` — legacy naive formats stamped UTC regardless of export locale (`:189-216`, r1 major → panel minor per QC-exports-are-UTC reality; r2 major dissent noted; r3 concurs minor); O(n²) CSV verification greedy match (`:902-916`, r1/r3); min-fee applied to zero-quantity orders hard-fails legit portfolios (`:793`, r1); rigid 5-element equity schema rejects LEAN 2-element series (`:684-685`, r2); full equity curve embedded in every artifact/hash (`:701`, r2); `_submission_clusters` latent empty-input crash (`:670-672`, r2/r3); triplicated reconciliation payloads (r1/r2).
- `readiness/models.py` — four fields declared twice; verified harmless today (last-wins, module imports fine) but a shadow-edit trap (r1/r3 minor; r2 suspected import-breaking — refuted by direct execution during synthesis).
- `readiness/validator.py` — placeholder checks recorded passed=True/"info" when artifacts absent — correct only while require_* flags hold (`:205-242` etc., r3); warning-level mismatches force READY_WITH_WARNINGS forever with no waive knob (`:371-389`, r1); nine instances of one threshold-check shape (r2/r1); recommendations threaded as out-param through every method (r2).
- `regression/runner.py` — TradeComparator constructed per trade (`:318-321`, r3); recomputed-vs-recomputed golden stats can't detect stored-stats corruption (`:332-333`, r2); deviations list[Any] (r1/r2); vacuous pass on empty inputs (`:340-341`, r2).
- `json_report.py` — dead `_rel_diff` helper while inline twin used (`:87-91`, r2); markdown round-trips through JSON string (`:578-589`, r2); empty stat_deltas counts as passed=1 vacuous truth (`:453`, r2); platform-stats fallback computes stats from zero trades guaranteeing mismatch instead of flagging no-data (`:402-417`, r1); bool stuffed into int summary (`:443,453`, r1); risk_passed never surfaced in recommendations (`:515`, r1); impossible dict branch in isinstance ladder (r2/r3).
- `risk/models.py` — explicit `frozen=False` configs suggest past migration (r3); free-text direction field (r1/r2); unreferenced `RiskModelConfig` (Speculative Generality, r1/r2).
- `risk/fixed_risk.py`/`percent_risk.py` — `validate()` mutates caller's model (`:60-61`, all); no notional sanity bound — tiny stop distances produce absurd leverage silently (`percent_risk :94-95`, r1); direction unused in math — wrong-side stop yields plausible profile (`:79/:88`, r2); negative fixed risk_amount unvalidated producing absurd negative sizes floored by rounding (r3); total_risk_percent summation dimensionally odd (`adapter :229`, r1/r2); ~85-90% identical models — extract base StopDistanceSizer (all three).
- `risk/adapter.py` — binary LONG-else-sell mapping (`:41`, all); Middle Man trade_to_signal wrapper (r1/r2); if-chain model dispatch (r2/r1).
- `risk/comparison.py` — attribute-over-meta stop resolution asymmetry vs comparison module (`:165-174`, r2/r1); composite trade_id underscore-join ambiguity (`:407`, r1); Inappropriate Intimacy reaching DeltaReportBuilder private (`:400-402`, all).
- `statistics/cross_platform.py` — period extraction reads only first 12 rows swallowing failures disabling CAGR silently (`:281-297,208-213`, r3/r1); deferred runner.stats import mid-function circular-dependency paper-over (`:241-242`, r3/r1/r2); win_rate>1 heuristic edge (r1/r2/r3; Luna d9).
- `stats/calculator.py` — drawdown from trade-close sequence not equity curve (documented convention, cross-platform comparisons diverge silently; `:59-78`, r2 minor, Luna D18 major dissent — panel holds minor per documented convention); Sortino divides by negative-count (declared; least-standard variant; `:103`, all; Luna D19 annualization dissent similarly held minor); infinite ratios rely on downstream sanitizer (`:129,139`, r1/r2).

---

### D. Cross-cutting themes

1. **One OHLCV resample rule, ~11 implementations** (agg-dict copies in resample, histdata, ctrader, huggingface, yfinance, dukascopy_native, jforex-via-shared…): `data_tools.resample.resample_ohlcv` already exists — adopt it everywhere. Timeframe vocabularies are independently re-declared in ≥10 modules (TIMEFRAME_MAP/ALIASES/ORDER families) with an in-code "must stay in sync" confession.
2. **Durability**: canonical parquet writes are non-atomic in base.merge, grid normalize, dukascopy source-meta, catalog.json, migrate dest — one shared `atomic_write_parquet` (temp+rename, optional lock) fixes the class (DV-B1 family).
3. **Two-and-a-half stats engines**: `regression/runner._compare_stats`, `reporting/json_report._compare_stats`, plus zombie `statistics/cross_platform.py` — consolidate on `stats/calculator.py` conventions.
4. **MT5 importer twins**: ~200 shared lines (labels, coercers, exit-inference, assembly) — extract `mt5_common.py`.
5. **Gate-consistency**: extra-trade severity and tolerance OR/AND semantics disagree across anomalies/delta_report/risk-comparison/readiness — single-source severity policy.
6. **Fixed-checklist verdicts**: SQL string building (both dolthub modules) and subprocess-without-timeout (dolthub_manager) are the two literal violations; mutable defaults clean everywhere; tz-naive spots confined to reference.utcnow, dolthub stamps, and QC legacy fallbacks; resource leaks are repeated unclosed ParquetFile/Session handles.

---

### E. Reference-report adjudication summary

Adopted (verified): D1 (=DV-C1), D4/D5 (jar bootstrap), D8 (=DV-B24). Corroborated-by-panel: D2/D3 (=DV-B27, severity held at major), D13 (=DV-B20), D15 (=DV-B39), D20 (=DV-B43). Refuted/not adopted: **D10** (resample sorts via `sort_index()` before agg — verified in source), **D14** (fixed UTC boundaries are the documented repo convention for FX data), **D18/D19** (documented calculator conventions; flagged for cross-platform parity awareness instead), **D16/D17** (substance covered under DV-B37/B50 at calibrated severity), D6/D7 (held minor/trusted-vendor; substance partially in DV-B19), D9 (minor per docstring-overpromise reading), D11/D12 (string-coercion fragility held minor; pandas currently coerces deterministically), d1–d12 (either merged above at matching or lower severity, or subsumed: d7→matcher note, d8→DV-B35, d12→DV-B1).

---

### F. Verification & settlement log (synthesizer code reads)

1. **dukascopy_native call site/signature** (`:1005-1028`, `:774-800`): confirmed both DV-C1 (TypeError via duplicate `pipeline_batches`) and DV-C2 (identical bid/ask URL calls at `:790-791`). r3's claim that the misalignment *silently* swaps pause_ms/max_retries was corrected: the duplicate keyword raises before that matters; r1's identical finding rated major — raised to critical jointly with DV-C2.
2. **r3's "silent 1000× SL/TP corruption"** (`float("1.234,56".replace(",", "."))`): arithmetic checked — `"1.234,56".replace(",", ".")` = `"1.234.56"` which raises ValueError, it does not yield 1.23456. Merged defect recorded accurately in DV-B44 (silent corruption for dot-thousands strings, abort for comma-decimals).
3. **TradeComparisonConfig import** (`json_report.py:1-30` + grep): confirmed missing; `from __future__ import annotations` present at top → deferred evaluation, no runtime failure today. Settled major (r1 critical dissent noted).
4. **ReadinessConfig duplicate fields**: executed import — module loads fine, pydantic last-wins. r2's "PydanticUserError on import" refuted; kept as minor maintenance trap (r1/r3 position).
5. **Dolthub SQL + subprocess** (`dolthub_manager.py:32,138`, `dolthub_dl.py:94-102`): confirmed f-string SQL interpolation and timeout-less subprocesses. Severity settled major/major (r1's two security-criticals and r2's availability-critical recorded as dissents; reachability bounded).
6. **signal_import `_parse_action`** (`:65-80`): confirmed `return "buy"` fallthrough for unknowns.
7. **Luna D5 jar-bootstrap swallow** (`jforex_dl.py:344-356`): confirmed `except Exception → warning → return []`; adopted as DV-B7.
8. **Luna D10 resample ordering** (`resample.py:55-71`): refuted — `set_index().sort_index()` precedes `.resample().agg()`.

---

### G. Coverage

All 50 tracked files accounted for across the three partials (each reviewer read the area in full): 42 files carry findings above or in the partials; scanned-clean: `data_tools/__init__.py`, `data_tools/downloaders/__init__.py`, `validation/__init__.py`, `validation/comparison/__init__.py`, `validation/importers/base.py` (design note folded into MT5 sections), `validation/readiness/__init__.py`, `validation/regression/__init__.py`, `validation/reporting/__init__.py`, `validation/stats/__init__.py`. Per-file mapping tables appear in each partial report.

---

**Final counts: Critical 2 · Major 51 · Minor 118 · Total 171**
