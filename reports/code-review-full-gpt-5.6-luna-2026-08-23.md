# Full-Repository Code Review

- **Model:** gpt-5.6-luna
- **Date:** 2026-08-23
- **Scope:** 366 tracked code files (`.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.css`)

## Checklist

- [x] 1. runner
- [x] 2. live_runner
- [x] 3. frontend
- [x] 4. viewer
- [x] 5. data & validation
- [x] 6. misc backend
- [x] 7. tests — chunk 1
- [x] 8. tests — chunk 2
- [x] 9. tests — chunk 3

## Summary

| Area | Critical | Major | Minor | Reviewed code files |
|---|---:|---:|---:|---:|
| runner | 0 | 10 | 11 | 36 |
| live_runner | 0 | 6 | 9 | 18 |
| frontend | 0 | 9 | 20 | 59 |
| viewer | 0 | 12 | 16 | 4 |
| data & validation | 1 | 19 | 12 | 50 |
| misc backend | 0 | 8 | 33 | 71 |
| tests — chunk 1 | 0 | 4 | 16 | 61 |
| tests — chunk 2 | 0 | 1 | 4 | 39 |
| tests — chunk 3 | 0 | 4 | 14 | 27 |
| supplemental omitted strategy file | 0 | 3 | 3 | 1 |
| **Total** | **1** | **76** | **138** | **366** |

The fixed misc-backend map contains 71 tracked code files; the supplemental pass covered the only omitted tracked code file, `strategy-comparisons/risk_parity/risk_parity_strategy.py`. The three explicitly scoped root configuration files (`pyproject.toml`, `pytest.ini`, `live_deployments.yaml`) were also reviewed.

## Top 10 Most Important Findings

1. **D1 — Merged Dukascopy download is uncallable** — `data_tools/downloaders/dukascopy_native.py:1010-1013` — **critical**. The merged-download path raises before execution because arguments are passed in the wrong order.
2. **V2 — Backtest identifiers permit filesystem traversal** — `viewer/main.py:1077,2531,6923,9072,8742` — **major**. Crafted IDs can read, overwrite, or delete files outside intended roots.
3. **V3 — Strategy path resolution can execute arbitrary existing Python files** — `viewer/core.py:202-207`, `viewer/main.py:7752-7754` — **major**. A traversed path is passed to the backtest runner.
4. **V4 — CSV replay reads arbitrary server-side paths** — `viewer/main.py:8480-8485` — **major**. Client-controlled paths are parsed on the server without import-root containment.
5. **D4 — JNLP/JAR bootstrap trusts remote code** — `data_tools/downloaders/jforex_dl.py:238-299` — **major**. Configurable remote JARs are placed on the JVM classpath without host/hash/signature controls.
6. **F2 — API client lacks explicit mutation protection and encodes resource IDs unsafely** — `frontend/src/api/client.ts:91-112,249-280,433-434,456-516,551-604` — **major**. Cookie-authenticated mutations may be CSRF-exposed and interpolated IDs can alter paths.
7. **V1 — Wildcard CORS is combined with credentials** — `viewer/main.py:631-638` — **major**. The API exposes sensitive and mutating routes to arbitrary origins under an unsafe credential configuration.
8. **MISC1 — Explicit live quantities bypass risk controls** — `risk/live.py:22-57` — **major**. Signals can submit oversized or malformed quantities outside configured limits.
9. **MISC4 — Backtest regeneration can destroy prior results before replacement is valid** — `scripts/regenerate_backtests.py:181-207` — **major**. Partial failure can leave the repository without historical artifacts.
10. **T3-1 — Code validator is not a security sandbox** — `tests/validators/code_validator.py:97-121` — **major**. Submitted strategy code can access host resources and spawn processes despite isolated Python import.

**Coverage:** Every tracked code file was accounted for: **366/366** (`.py`, `.ts`, `.tsx`, `.js`, `.jsx`, `.css`). Final totals: **1 critical, 76 major, 138 minor** findings. All nine checklist areas are complete.

## 9. tests — chunk 3

**Coverage:** 27/27 tracked test/validator code files reviewed. 0 critical, 4 major, and 14 minor findings.

### Major findings

- **T3-1 — Code validator executes submitted strategy code without a sandbox** — `tests/validators/code_validator.py:97-121` — **major**, security/process isolation. `-I` is not a filesystem/network/resource sandbox; run validation in a dedicated restricted process/container with process-group cleanup.
- **T3-2 — Backtest validator silently skips invalid trade timestamps** — `tests/validators/backtest_validator.py:68-72` — **major**, correctness. Present but malformed timestamps can pass; record validation errors instead.
- **T3-3 — Backtest validator fails to validate all required numeric stats** — `tests/validators/backtest_validator.py:40-45` — **major**, correctness. Non-numeric cagr/drawdown/win-rate/PnL values can bypass finite checks.
- **T3-4 — Cross-platform metrics test depends on executable regeneration scripts and external artifacts** — `tests/validation/test_cross_platform_metrics.py:1-11` — **major**, test isolation/false confidence. Use self-contained temporary fixtures and test regeneration separately.

### Minor findings

- **t3-1** `tests/validation/test_trade_statistics.py:13-29,403-413` — **minor**, contract gap: inconsistent aggregate-vs-exit PnL is not tested.
- **t3-2** `tests/validation/test_trade_comparison.py:253-291` — **minor**, false-positive tolerance: OR logic accepts large relative errors near zero.
- **t3-3** `tests/validation/test_trade_matcher.py:205-236` — **minor**, contract gap: reordered duplicate-key matching is untested.
- **t3-4** `tests/validation/test_trade_matcher.py:257-272` — **minor**, timezone: naive timestamps are silently treated as UTC.
- **t3-5** `tests/validation/test_paper_readiness.py:510-525` — **minor**, readiness contract: all checks disabled can still report `READY`.
- **t3-6** `tests/validation/test_markdown_report.py:118-121,152-154,187-188,217-223` — **minor**, Duplicated Code/weak assertions: repeated setup and substring-only checks.
- **t3-7** `tests/validation/test_cli.py:50-57` — **minor**, subprocess timeout: CLI invocation has no timeout.
- **t3-8** `tests/validation/test_cli.py:144-190` — **minor**, resource cleanup: generated workbook is not explicitly closed.
- **t3-9** `tests/test_yfinance_downloader.py:24-38` — **minor**, contract gap: forwarding, timezone, ordering, duplicate, and empty behavior are not asserted.
- **t3-10** `tests/validation/test_mt5_html_importer.py:75-94` — **minor**, weak assertion: direction semantics are not checked.
- **t3-11** `tests/validation/test_mt5_xlsx_importer.py:187-196` — **minor**, false confidence: no-op branches and quantity-only assertion do not test direction.
- **t3-12** `tests/validation/test_mt5_html_importer.py:1-204` and `tests/validation/test_mt5_xlsx_importer.py:1-274` — **minor**, Duplicated Code: parallel importer suites should share parametrized acceptance tests.
- **t3-13** `tests/validation/test_quantconnect_result_importer.py:302-311` — **minor**, environment coupling: import-time reference-dir capture and external artifacts make setup brittle.
- **t3-14** `tests/validation/test_quantconnect_result_importer.py:382-456` — **minor**, brittle golden contract: huge exact snapshot obscures semantic invariants.

### Fixed checklist and Fowler smells

- No mutable defaults, bare except, shared-state race, or SQL finding.
- Subprocess timeout: `tests/validation/test_cli.py:50-57` (t3-7); resource cleanup: t3-8.
- TZ-naive datetime contract: t3-4.
- Baseline smells: **Duplicated Code** in Markdown and MT5 importer tests; **Primitive Obsession/Data Clumps** in raw report/trade dictionaries; **Shotgun Surgery** in duplicated comparison expectations; **Divergent Change** in QuantConnect importer tests.

### Per-file accounting

All 27 requested files were explicitly accounted for by the agent. Findings occurred in `tests/test_yfinance_downloader.py`, `tests/validation/test_cli.py`, `tests/validation/test_cross_platform_metrics.py`, `tests/validation/test_markdown_report.py`, `tests/validation/test_mt5_html_importer.py`, `tests/validation/test_mt5_xlsx_importer.py`, `tests/validation/test_paper_readiness.py`, `tests/validation/test_quantconnect_result_importer.py`, `tests/validation/test_trade_comparison.py`, `tests/validation/test_trade_matcher.py`, `tests/validation/test_trade_statistics.py`, `tests/validators/backtest_validator.py`, and `tests/validators/code_validator.py`; the remaining listed files were scanned with no additional finding.

**Coverage:** 27/27 files explicitly accounted for; 0 critical, 4 major, and 14 minor findings.

## 7. tests — chunk 1

**Coverage:** 61/61 tracked test code files reviewed. 0 critical, 4 major, and 16 minor findings.

### Major findings

- **T1 — Conversion test falls back to a valid fixture when extraction fails** — `tests/e2e/lab/test_conversion.py:67-70` — **major**, false positive. Broken/empty conversion can pass; fail when extraction fails.
- **T2 — Intentional broken fixture is included in positive AI-quality discovery** — `tests/integration/test_ai_code_quality.py:10-18` — **major**, test correctness. Exclude it from positive tests and assert separately that it fails validation.
- **T3 — Test orchestrator launches layers without timeout** — `tests/orchestrator.py:55-61` — **major**, harness reliability. Hung pytest can hang the full orchestration; bound and clean up child processes.
- **T4 — cTrader live-fix test leaves timed-out child alive** — `tests/test_ctrader_live_fix.py:209-223` — **major**, resource leak. Handle timeout and terminate/kill in cleanup.

### Minor findings

- **t1** `tests/e2e_frontend.js:140-148` — **minor**, false positive: console-error test logs but never fails.
- **t2** `tests/integration/test_strategy_pipeline.py:44-62` — **minor**, isolation: discovery/setup failures become module-wide skips.
- **t3** `tests/integration/test_strategy_pipeline.py:89-96` — **minor**, timing: fixed sleep polling lacks deadline/classification.
- **t4** `tests/conftest.py:42-57` — **minor**, global cache can stale across environment changes.
- **t5** `tests/strategies/conftest.py:84-96` — **minor**, min/max coverage can hide internal data gaps.
- **t6** `tests/test_api.py:58-64,72-100,108-115,121-126` — **minor**, conditional assertions let empty DB pass.
- **t7** `tests/test_api.py:136-143` — **minor**, benchmark test accepts both 200 and 404.
- **t8** `tests/test_grid_tester_sqlite.py:148-180` — **minor**, newest-first test never asserts order.
- **t9** `tests/test_live_daemon.py:77-80,202-215,231-244,370-371,395-396,421-422,438-460` — **minor**, manual global-state cleanup is not fixture-based.
- **t10** `tests/test_ctrader_live_fix.py:315-367` — **minor**, SQLite engine is not disposed.
- **t11** `tests/test_auto_agent.py:133-218` — **minor**, stale failed-attempt artifacts are not asserted absent.
- **t12** `tests/test_cache_manager.py:121-132` — **minor**, shared result/error lists lack synchronization.
- **t13** `tests/test_live_api.py:125-153` — **minor**, duplicate monkeypatch silently replaces the first.
- **t14** `tests/strategies/test_custom_swings_supertrend.py:77-102` — **minor**, synthetic OHLC violates market invariants.
- **t15** `tests/strategies/test_custom_swings_supertrend.py:113-124` — **minor**, parameter test checks only default count.
- **t16** `tests/strategies/test_*` common risk checks — **minor**, risk assertions omit point value/multiplier/currency and may not exercise canonical sizing.

### Fixed checklist and Fowler smells

- No mutable defaults, bare `except`, material SQL defect, or material timezone defect found.
- Subprocess timeout/cleanup: T3/T4; shared-state races: t9/t12; resource cleanup: T4/t10.
- JS: top-level E2E async IIFE lacks final catch (minor concern covered by t1 context); no XSS/missing-boundary/N+1 finding.
- Baseline smells: **Duplicated Code** across strategy regression tests; **Feature Envy/Data Clumps/Primitive Obsession** in nested response and fixture setup; **Shotgun Surgery** across live routing tests; **Divergent Change** in `test_live_api.py`; **Message Chains** in nested payload assertions. No material Repeated Switches, Speculative Generality, Middle Man, or Refused Bequest.

### Per-file accounting

All 61 files were explicitly accounted for by the agent. Scanned with findings: `tests/conftest.py`, `tests/e2e/lab/test_conversion.py`, `tests/e2e_frontend.js`, `tests/integration/test_ai_code_quality.py`, `tests/integration/test_strategy_pipeline.py`, `tests/orchestrator.py`, `tests/strategies/conftest.py`, all listed strategy test files with the common risk concern where applicable, `tests/test_api.py`, `tests/test_auto_agent.py`, `tests/test_cache_manager.py`, `tests/test_ctrader_live_fix.py`, `tests/test_grid_tester_sqlite.py`, `tests/test_live_api.py`, and `tests/test_live_daemon.py`. The remaining listed files were scanned with no distinct finding, including intentional negative/Pine fixtures.

**Coverage:** 61/61 files explicitly accounted for; 0 critical, 4 major, and 16 minor findings.

## 8. tests — chunk 2

**Coverage:** 39/39 tracked test code files reviewed. 0 critical, 1 major, and 4 minor findings. The requested test run passed 419 tests with 12 warnings.

### Major findings

- **T2-1 — OOS test accepts ignored `train_months` contract** — `tests/test_portfolio_optimizer_oos.py:214-224` — **major**, false confidence. The test characterizes an implementation that ignores the documented walk-forward training window; implement and assert the intended train/test/gap boundaries.

### Minor findings

- **T2-1** `tests/test_risk_consistency.py:55,94` — **minor**, timezone: tests use naive `datetime.now()` instead of the production UTC-aware contract.
- **T2-2** `tests/test_trade_engine.py:324-368` — **minor**, false confidence: unavailable fixtures skip parity and zero-trade cases assert only `True`.
- **T2-3** `tests/test_scheduler_signal_bot_health.py:34-39,60-64` — **minor**, missing assertions: health status/error/task payloads are not checked.
- **T2-4** `tests/test_signal_bot_health_check.py:6-25` — **minor**, isolation: production registry/filesystem is required and layout changes cannot be distinguished from defects.

### Fixed checklist and Fowler smells

- Mutable defaults, bare except, shared-state race, SQL construction, unbounded subprocess, and resource leaks: no distinct finding in this chunk.
- TZ-naive test datetimes: T2-1.
- Baseline: broad `test_viewer_features.py` is a **Divergent Change** hotspot; fixture repetition is judgment-based **Duplicated Code/Data Clumps**.

### Per-file accounting

All 39 requested files were explicitly accounted for by the agent; findings occurred in `tests/test_portfolio_optimizer_oos.py`, `tests/test_risk_consistency.py`, `tests/test_scheduler_signal_bot_health.py`, `tests/test_signal_bot_health_check.py`, and `tests/test_trade_engine.py`. The remaining listed files were scanned with no additional finding.

**Coverage:** 39/39 files explicitly accounted for; 0 critical, 1 major, and 4 minor findings.

## Supplemental — omitted tracked strategy file

**Coverage:** 1/1 tracked code file omitted from the fixed area map was reviewed: `strategy-comparisons/risk_parity/risk_parity_strategy.py`. 0 critical, 3 major, and 3 minor findings.

### Major findings

- **S1 — Per-asset returns are aligned by array length, not dates** — `strategy-comparisons/risk_parity/risk_parity_strategy.py:132-138` — **major**, correctness. Symbol-specific gaps or stale prices can correlate mismatched periods; align a date-indexed price table before calculating returns.
- **S2 — Risk-parity target is dimensionally incorrect** — `strategy-comparisons/risk_parity/risk_parity_strategy.py:181-184` — **major**, numerical correctness. Risk contributions sum to portfolio volatility, but the objective targets `1/n`; target normalized contributions or `portfolio_volatility/n`.
- **S3 — Inverse-volatility fallback bypasses configured weight bounds** — `strategy-comparisons/risk_parity/risk_parity_strategy.py:205-211` — **major**, risk correctness. Failed optimization can return weights above the configured maximum and later capping does not restore minimums/normalization.

### Minor findings

- **s1** `strategy-comparisons/risk_parity/risk_parity_strategy.py:226-243` — **minor**, model correctness: historical correlation baseline overlaps the recent regime window.
- **s2** `strategy-comparisons/risk_parity/risk_parity_strategy.py:230-235` — **minor**, semantic clarity: average absolute correlation differs from ordinary average correlation and should be explicit.
- **s3** `strategy-comparisons/risk_parity/risk_parity_strategy.py:247-278` — **minor**, maintainability: `cov_matrix` is accepted but unused.

### Fixed checklist and baseline smells

- No mutable defaults, bare except, shared-state race, SQL, subprocess-timeout, timezone, resource-leak, or security finding.
- No high-confidence Fowler smell beyond Primitive Obsession in raw arrays/weights and the unused interface parameter.

### Per-file accounting

`strategy-comparisons/risk_parity/risk_parity_strategy.py` — S1-S3, s1-s3; fully scanned and syntax-checked.

**Coverage:** 1/1 supplemental file accounted for; 0 critical, 3 major, and 3 minor findings.

## 6. misc backend

**Coverage:** 74/74 explicitly scoped misc-backend paths reviewed (71 tracked code files plus `pyproject.toml`, `pytest.ini`, and `live_deployments.yaml`). 0 critical, 8 major, and 33 minor findings.

### Major findings

- **MISC1 — Explicit live quantity bypasses risk controls** — `risk/live.py:22-57` — **major**, security/correctness. Positive `explicit_qty`/`qty` is returned without max-position, exposure, stop-distance, balance, or finite-value enforcement.
- **MISC2 — Live account statistics query mixes account and deployment identifiers** — `shared/database.py:2285-2310` — **major**, data isolation/correctness. Signals can be incorrectly included by identifier collision and execution queries lack an explicit account-scoped join.
- **MISC3 — Matrix queue lock acquisition is non-atomic** — `scripts/matrix_queue_runner.py:89-108` — **major**, race condition. Check-then-write allows concurrent workers to proceed simultaneously; use exclusive-create/advisory locking.
- **MISC4 — Backtest regeneration destructively purges prior artifacts before replacement is valid** — `scripts/regenerate_backtests.py:181-207` — **major**, data safety. Partial failures can leave the repository without prior results; stage and atomically swap output.
- **MISC5 — Execution deduplication can delete legitimate identical fills** — `scripts/dedupe_executions.py:20-27,49-57` — **major**, correctness. `(signal_id, filled_qty, timestamp)` is not a broker identity; use deal/order IDs and backup/dry-run.
- **MISC6 — Intraday volatility strategy treats string `"false"` as true** — `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py:150-167,208-216` — **major**, correctness. `bool(value)` silently enables disabled modes from JSON/CLI config.
- **MISC7 — VIX strategy ignores requested range and forward-fills stale data indefinitely** — `strategies/post_vix_dip_buy/post_vix_dip_buy.py:20-31,72-83` — **major**, correctness. Wrong/stale VIX source can generate signals outside requested coverage.
- **MISC8 — Risk-parity strategy is actually single-symbol long-only** — `strategies/risk_parity/risk_parity.py:3-12,20-31` — **major**, semantic correctness. It does not perform multi-asset risk-parity allocation despite its declared strategy semantics.

### Minor findings

- **misc-m1** `risk/models.py:38-56` — **minor**, numeric validation: prices/SL/TP do not reject non-finite/invalid directional values.
- **misc-m2** `risk/engine.py:91-104` — **minor**, numeric validation: non-finite exposure can pass through sizing decisions.
- **misc-m3** `shared/database.py:739-752` — **minor**, subprocess: Git SHA detection lacks timeout.
- **misc-m4** `shared/database.py:841,947` — **minor**, SQL identifier interpolation is fragile; validate/quote identifiers.
- **misc-m5** `shared/database.py:1390-1408` — **minor**, race/idempotency: lookup-before-insert depends on unique-index invariants rather than one atomic upsert.
- **misc-m6** `shared/database.py:1418-1464` — **minor**, data integrity: cumulative fills are not scoped by broker order/deal identity.
- **misc-m7** `signal_bots/health_check.py:34-48` — **minor**, correctness: stockkit/nitro config paths map to ramjet and generic name splitting is unreliable.
- **misc-m8** `signal_bots/ramjet/processor.py:34-46` — **minor**, parameter validation: ATR/Supertrend periods and multipliers lack positive constraints.
- **misc-m9** `signal_bots/rocketfuel/processor.py:31-44` — **minor**, parameter validation: ATR period/multiplier accepts zero/negative values.
- **misc-m10** `scheduler/jobs/weekly_data_refresh.py:13-79` — **minor**, correctness: no-op job reports successful refresh.
- **misc-m11** `scheduler/scheduler.py:149-154` — **minor**, task identity: second-resolution IDs collide.
- **misc-m12** `scripts/adaptive_harvester.py:350-421,533-538` — **minor**, resource handling: pending tasks are not explicitly cancelled/gathered on failure and retry-after is ignored.
- **misc-m13** `scripts/analyze_mt5_parity.py:199-215` — **minor**, statistics: forward-filled calendar returns and fixed 252 annualization can distort comparisons.
- **misc-m14** `scripts/compare_strategy_metrics.py:17-31` — **minor**, Mysterious Name/config drift: hard-coded dated output filename misstates provenance and can overwrite artifacts.
- **misc-m15** `scripts/consolidate_parquets.py:42-78` — **minor**, destructive resource operation: source fragments are removed without staged atomic replacement/validation.
- **misc-m16** `scripts/download_discovery_data.py:99-119` — **minor**, retry handling: final attempt still sleeps and broad errors are treated as transient.
- **misc-m17** `scripts/generate_matrix_grid_overview.py:242` — **minor**, timezone: `utcnow()` is labeled with `Z` despite being naive.
- **misc-m18** `scripts/live_smoke.py:387-410,499-519` — **minor**, resource lifecycle: cTrader connections are not guaranteed to disconnect.
- **misc-m19** `scripts/live_smoke.py:1030-1048` — **minor**, operational safety: failed cleanup can leave an open paper position without independent reconciliation.
- **misc-m20** `scripts/overnight_strategy_discovery.py:69-102` — **minor**, Speculative Generality/correctness: all trials are mock rows, not evaluated discoveries.
- **misc-m21** `scripts/run_discovery_grid_backtests.py:151-171` — **minor**, provenance: command logging via join is ambiguous for special paths/JSON.
- **misc-m22** `scripts/verify_ctrader_order.py:38-57` — **minor**, resource lifecycle: disconnect is success-path only.
- **misc-m23** `strategies/channel_breakout/channel_breakout.py:36-44,60-67` — **minor**, strategy correctness: trailing stop checks close instead of intrabar low.
- **misc-m24** `strategies/cross_asset_momentum/cross_asset_momentum.py:20-31` — **minor**, semantic mismatch: “cross-asset” operates on one close series.
- **misc-m25** `strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py:1-58` — **minor**, interface: returns three values instead of common four-value strategy contract.
- **misc-m26** `strategies/pine_supertrend_dema/pine_supertrend_dema.py:21-59` — **minor**, indicator correctness: zero-filled early bands can cause non-equivalent state/flips.
- **misc-m27** `strategies/pynecore_orb/pynecore_orb.py:37-61` — **minor**, semantic mismatch: named ORB but uses SMA crossover.
- **misc-m28** `strategies/qg_30min_es_breakout_strategy/qg_30min_es_breakout_strategy.py:112-121` — **minor**, strategy correctness: `fract=3.3` thresholds can sit beyond candle range; verify formula.
- **misc-m29** `strategies/sector_rotation/sector_rotation.py:3-12,22-30` — **minor**, semantic mismatch: single-symbol positive momentum is not sector rotation.
- **misc-m30** `strategies/stat_arb_pairs/stat_arb_pairs.py:3-12,27-35` — **minor**, semantic mismatch: one-asset cumulative log return is not a pair spread.
- **misc-m31** `strategies/trend_following/trend_following.py:69-75` — **minor**, strategy correctness: trailing stop ignores intrabar low.
- **misc-m32** `strategies/volatility_breakout/volatility_breakout.py:47-54,84-91` — **minor**, configuration: permissive numeric direction mapping can enable shorts unexpectedly.
- **misc-m33** `live_deployments.yaml:1-83` — **minor**, configuration security: committed account metadata and unresolved channel placeholders need activation validation.

### Fixed checklist and baseline smells

- Mutable defaults: no mutable default arguments found; `None` defaults are safe.
- Bare except: none found; broad exceptions are covered above where they hide failures.
- Shared-state races: MISC3 and database idempotency concerns.
- SQL string building: identifier interpolation at `shared/database.py:841,947` (minor).
- Subprocess timeout: `shared/database.py:739-752` (minor).
- TZ-naive datetime: `scripts/generate_matrix_grid_overview.py:242` (minor); documented legacy naive UTC elsewhere.
- Resource leaks: cTrader cleanup gaps in `scripts/live_smoke.py` and `scripts/verify_ctrader_order.py`.
- Baseline smells: **Mysterious Name**, **Duplicated Code**, **Feature Envy**, **Data Clumps**, **Primitive Obsession**, **Repeated Switches**, **Shotgun Surgery**, **Divergent Change**, **Speculative Generality**, **Message Chains**, **Middle Man**, and **Refused Bequest** were considered; actionable examples include repeated strategy/bot branching, shared parameter clumps, `shared/database.py` divergent responsibilities, mock discovery abstraction, and thin inheritance in Nitro/RamJet.

### Per-file accounting

All 74 explicitly listed paths were accounted for. The agent’s full coverage list confirms: risk files (5), scheduler files (5), scripts (21), shared files (8), signal bot files (11), strategy files (22), plus `pyproject.toml`, `pytest.ini`, and `live_deployments.yaml`; each was either assigned a finding above or recorded as scanned with no additional high-confidence defect.

**Coverage:** 74/74 explicitly scoped paths accounted for; 0 critical, 8 major, and 33 minor findings.

## 5. data & validation

**Coverage:** 50/50 tracked data/validation code files reviewed. 1 critical, 19 major, and 12 minor findings.

### Critical

- **D1 — Merged Dukascopy download passes positional arguments in the wrong order** — `data_tools/downloaders/dukascopy_native.py:1010-1013` — **critical**, correctness. The `offer_side="both"` path passes `pipeline_batches` twice and raises `TypeError` before merged download execution; correct the call and add a regression test.

### Major

- **D2 — DoltHub SQL interpolates table and symbol values** — `data_tools/dolthub_manager.py:112-140` — **major**, security. Crafted identifiers can alter SQL; validate/escape or parameterize.
- **D3 — DoltHub downloader accepts/interpolates custom SQL and identifiers** — `data_tools/downloaders/dolthub_dl.py:96-102` — **major**, security. Treat custom SQL as privileged and strictly validate repository/table/symbol inputs.
- **D4 — JNLP/JAR bootstrap trusts configurable remote code** — `data_tools/downloaders/jforex_dl.py:238-299` — **major**, security/resource. Restrict approved HTTPS hosts, validate redirects, pin hashes/signatures, and bound response sizes.
- **D5 — JAR bootstrap swallows all failures** — `data_tools/downloaders/jforex_dl.py:351-353` — **major**, correctness/observability. Remote download/parsing/security failures become a later generic startup error; fail closed with root cause.
- **D6 — HistData ZIP response and first member lack resource validation** — `data_tools/downloaders/histdata_dl.py:202-210` — **major**, resource/security. Hostile archives can exhaust memory/CPU; stream with bounded size and validate archive structure.
- **D7 — HuggingFace Parquet URLs are loaded wholesale without bounds/integrity** — `data_tools/downloaders/huggingface_dl.py:250-260` — **major**, resource/security. Use bounded downloads, timeouts, caching, and size/hash validation.
- **D8 — Gap detection uses retained non-contiguous indices** — `data_tools/validate.py:162-170` — **major**, correctness. Dropping invalid timestamps without resetting the index can select incorrect rows or raise; use positional enumeration.
- **D9 — Weekend-gap suppression ignores asset class** — `data_tools/validate.py:172-178` — **major**, time-series correctness. The non-forex rule is applied regardless of asset class, hiding genuine gaps; use explicit session calendars.
- **D10 — Resampling does not sort/deduplicate before OHLC aggregation** — `data_tools/resample.py:56-71` — **major**, time-series correctness. Unordered/duplicate bars can alter first/last values; define and apply preprocessing first.
- **D11 — cTrader date filtering relies on raw-string coercion** — `data_tools/downloaders/ctrader_dl.py:148-157` — **major**, time-series correctness. Parse boundaries to validated UTC timestamps.
- **D12 — HistData date filtering relies on raw-string coercion** — `data_tools/downloaders/histdata_dl.py:140-148` — **major**, time-series correctness. Normalize and validate UTC boundaries.
- **D13 — Yahoo 4-hour resampling uses UTC calendar buckets** — `data_tools/downloaders/yfinance_dl.py:253-269` — **major**, time-series correctness. Exchange-session and DST behavior can be wrong; anchor to intended session timezone and handle incomplete buckets.
- **D14 — Dukascopy candle windows lack explicit instrument calendar policy** — `data_tools/downloaders/dukascopy_native.py:754-808` — **major**, time-series correctness. Fixed UTC boundaries can mis-handle sessions, weekends, DST, and month ends.
- **D15 — Risk-profile exceptions are swallowed** — `validation/risk/comparison.py:163-192` — **major**, correctness/safety. Invalid inputs or adapter defects become generic warnings instead of failing readiness with cause.
- **D16 — Readiness can certify incomplete validation as warning-only** — `validation/readiness/validator.py:85-118` — **major**, safety. Skipped required checks or risk/trade mismatches can still produce `READY_WITH_WARNINGS`; require complete evidence.
- **D17 — Aggregate per-trade risk percent is not portfolio exposure** — `validation/risk/adapter.py:204-229` — **major**, risk correctness. Summing risks ignores overlap and shared capital; compute concurrent timeline exposure.
- **D18 — Drawdown starts from zero PnL rather than account equity** — `validation/stats/calculator.py:59-78` — **major**, numerical correctness. Percentage drawdown is misleading across initial balances and loss-before-gain paths.
- **D19 — Sharpe/Sortino annualize irregular trade PnL as daily** — `validation/stats/calculator.py:81-107` — **major**, numerical correctness. Use a regular time series or explicit observation frequency.
- **D20 — MT5 reconstruction pairs each entry with the next out row** — `validation/importers/mt5_xlsx.py:459-495` — **major**, correctness. Interleaving, hedging, partial fills, and symbols can be mismatched; use stable IDs and quantity matching.

### Minor

- **d1** `data_tools/downloaders/alpaca_dl.py:211-216` — **minor**, caller-controlled symbol in output path.
- **d2** `data_tools/downloaders/yfinance_dl.py:151-157` — **minor**, symbol path lacks strict validation.
- **d3** `data_tools/downloaders/ctrader_dl.py:118-145` — **minor**, symbol path permits separators/traversal.
- **d4** `data_tools/downloaders/dolthub_dl.py:89-91` — **minor**, malformed repo and unvalidated output symbol.
- **d5** `data_tools/catalog_builder.py:828-846` — **minor**, large fundamentals loaded solely for metadata.
- **d6** `data_tools/download_grid_market_data.py:293-313` — **minor**, concurrent workers can overlap writes.
- **d7** `validation/comparison/matcher.py:64-145` — **minor**, Duplicated Code; greedy first-fit matching can mispair ambiguous trades.
- **d8** `validation/reporting/json_report.py:124-204` — **minor**, tolerance policy can accept unintended deviations.
- **d9** `validation/statistics/cross_platform.py:217-228` — **minor**, heuristic `win_rate > 1` unit conversion.
- **d10** `validation/importers/quantconnect_result.py:189-216` — **minor**, invalid timestamps pass through.
- **d11** `data_tools/signal_import.py:100-114` — **minor**, naive signal timestamps depend on implicit timezone assumptions.
- **d12** `data_tools/downloaders/base.py:150-182` — **minor**, concurrent in-memory merge/write lacks visible locking and atomic replacement.

### Fixed checklist and baseline smells

- Mutable defaults: no issue found.
- Bare `except`: no bare statement; broad exception concerns are covered by D5/D15.
- Shared-state races/resource handling: d6/d12 and D6/D7; Dolt subprocesses at `data_tools/dolthub_manager.py:30-77` lack timeouts.
- SQL string building: D2/D3.
- TZ-naive datetimes: `data_tools/dolthub_manager.py:145-149`, `data_tools/signal_import.py:100-114`, D11/D12.
- Baseline smells: **Duplicated Code** in downloader normalization; **Primitive Obsession** for dates/timeframes/risk tolerances; **Repeated Switches** for source/timeframe branching; **Divergent Change** in `catalog_builder.py`; **Shotgun Surgery** across timeframe aliases/resampling; **Message Chains** in validation/report traversal.

### Per-file accounting

`data_tools/__init__.py` — scanned. `data_tools/auto_download.py` — scanned. `data_tools/cache_manager.py` — scanned. `data_tools/catalog_builder.py` — d5. `data_tools/dolthub_manager.py` — D2 and timeout/timezone checklist findings. `data_tools/download_grid_market_data.py` — d6. `data_tools/downloaders/__init__.py` — scanned. `data_tools/downloaders/alpaca_dl.py` — d1. `data_tools/downloaders/base.py` — d12. `data_tools/downloaders/ctrader_dl.py` — d3,D11. `data_tools/downloaders/dolthub_dl.py` — D3,d4. `data_tools/downloaders/dukascopy_native.py` — D1,D14. `data_tools/downloaders/histdata_dl.py` — D6,D12. `data_tools/downloaders/huggingface_dl.py` — D7. `data_tools/downloaders/jforex_dl.py` — D4,D5. `data_tools/downloaders/yfinance_dl.py` — d2,D13. `data_tools/dukascopy_discovery.py` — scanned. `data_tools/migrate_csv.py` — scanned. `data_tools/resample.py` — D10. `data_tools/signal_import.py` — d11. `data_tools/validate.py` — D8,D9. `validation/__init__.py` — scanned. `validation/cli.py` — scanned. `validation/comparison/__init__.py` — scanned. `validation/comparison/anomalies.py` — scanned. `validation/comparison/comparison.py` — scanned. `validation/comparison/delta_report.py` — scanned. `validation/comparison/matcher.py` — d7. `validation/importers/__init__.py` — scanned. `validation/importers/base.py` — scanned. `validation/importers/mt5_html.py` — scanned. `validation/importers/mt5_xlsx.py` — D20. `validation/importers/quantconnect_result.py` — d10. `validation/readiness/__init__.py` — scanned. `validation/readiness/models.py` — scanned. `validation/readiness/validator.py` — D16. `validation/reference.py` — scanned. `validation/regression/__init__.py` — scanned. `validation/regression/runner.py` — scanned. `validation/reporting/__init__.py` — scanned. `validation/reporting/json_report.py` — d8. `validation/risk/__init__.py` — scanned. `validation/risk/adapter.py` — D17. `validation/risk/comparison.py` — D15. `validation/risk/fixed_risk.py` — scanned. `validation/risk/models.py` — scanned. `validation/risk/percent_risk.py` — scanned. `validation/statistics/cross_platform.py` — d9. `validation/stats/__init__.py` — scanned. `validation/stats/calculator.py` — D18,D19.

**Coverage:** 50/50 files explicitly accounted for; 1 critical, 19 major, and 12 minor findings.

## 4. viewer

**Coverage:** 4/4 tracked viewer code files reviewed. 0 critical, 12 major, and 16 minor findings.

### Major findings

- **V1 — Wildcard CORS is combined with credentials** — `viewer/main.py:631-638` — **major**, security. `allow_origins=["*"]` with `allow_credentials=True` is unsafe for an API exposing account data and mutations; use an explicit trusted-origin allowlist.
- **V2 — Backtest identifiers are used in filesystem paths without containment validation** — `viewer/main.py:1077,2531,6923,9072,8742` — **major**, security. Traversal identifiers can read, overwrite, or delete files outside intended directories; whitelist IDs or resolve and enforce containment.
- **V3 — Strategy resolver can return an arbitrary existing path** — `viewer/core.py:202-207`, `viewer/main.py:7752-7754` — **major**, security. A traversed path can be passed to `runner.cli` for execution; require containment and registered strategy IDs.
- **V4 — CSV replay accepts an arbitrary server-side filepath** — `viewer/main.py:8480-8485` — **major**, security. Client input controls which server file is parsed; accept uploads/server-issued references or restrict to an import directory.
- **V5 — Resampling accepts an arbitrary source path** — `viewer/main.py:10381-10409` — **major**, security. Validate source under `DATA_DIR` and require a supported regular file.
- **V6 — Portfolio identifiers can collide and overwrite existing portfolios** — `viewer/main.py:5089-5092,8993,9064-9067` — **major**, correctness. Second-resolution IDs can collide under concurrent same-name creation.
- **V7 — Data subprocesses in the legacy task launcher can run indefinitely** — `viewer/core.py:3370-3385` — **major**, resource/security. Unbounded `wait()` permits hung tasks; supervise process groups with deadlines and cleanup.
- **V8 — Full-size data payloads have weak resource controls** — `viewer/main.py:6875-6901,7218-7233,7618-7635` — **major**, availability. Large trades/bars/indicators/equity payloads can consume memory, CPU, bandwidth, and event-loop capacity.
- **V9 — Shared mutable task/job state is not consistently synchronized** — `viewer/main.py:209,7790-7796,7934-7935,8059-8061,8275-8281,8586-8589` — **major**, race condition. `_running_jobs` is accessed across requests/background threads without a lock.
- **V10 — Data file deletion containment uses prefix matching** — `viewer/core.py:4827-4834` and `viewer/main.py:10666-10671` — **major**, security. String prefixes do not guarantee directory containment and can permit sibling-prefix escapes.
- **V11 — Legacy delete endpoint can delete files outside the data directory** — `viewer/core.py:4820-4847` — **major**, security. Client-controlled paths are unlinked after the flawed containment check.
- **V12 — Legacy strategy path fallback is not restricted to `STRATEGIES_DIR`** — `viewer/core.py:202-207` — **major**, security. Existing paths outside the strategy root can be executed by the runner.

### Minor findings

- **v1** `viewer/main.py:698,743,759,808-809` — **minor**, mutable collection defaults in request models; use `default_factory`.
- **v2** `viewer/main.py:6489-6493,6517-6518,6900-6901,6934-6936,8724-8727,9499-9501,10762-10764` — **minor**, error details expose internal exception strings.
- **v3** `viewer/main.py:7638-7678` — **minor**, missing backtests are silently omitted from comparisons.
- **v4** `viewer/main.py:6854-6870,7717-7725` — **minor**, comparison metric units are inconsistent across endpoints.
- **v5** `viewer/main.py:154-260,2788-3203` plus `viewer/core.py:1003-4887` — **minor**, Duplicated Code/Shotgun Surgery/Divergent Change/Middle Man from parallel implementation and monkey-patching.
- **v6** `viewer/core.py:1357-1365` — **minor**, indicator conversion assumes positional integer index and can misalign timestamps.
- **v7** `viewer/core.py:1061-1069,1151-1158,1482-1502` — **minor**, legacy endpoints serialize full datasets synchronously without bounds.
- **v8** `viewer/core.py:1548-1549,1573-1574,2337-2340,2601-2603` — **minor**, broad exception handling hides malformed data.
- **v9** `viewer/core.py:2056-2298` and `viewer/main.py:8833-9068` — **minor**, duplicated/divergent portfolio behavior.
- **v10** `viewer/core.py:4858-4865` — **minor**, legacy static route checks unresolved paths before delegating.
- **v11** `viewer/comparison.py:69-76` — **minor**, non-positive initial equity silently becomes a flat normalized series.
- **v12** `viewer/comparison.py:38-50,106-113` — **minor**, correlation inputs lack length/finiteness guards.
- **v13** `viewer/comparison.py:93-98,118-122` — **minor**, Sharpe/Sortino annualization assumes all observations are trading days.
- **v14** `viewer/comparison.py:162-173` — **minor**, two-strategy combination ignores additional supplied strategies.
- **v15** `viewer/comparison.py:53-60` — **minor**, raw timestamp keys prevent equivalent timestamps from aligning and overwrite duplicates.
- **v16** `viewer/comparison.py:63-76` — **minor**, synthetic 50/50 portfolio normalization at first overlap is undocumented.

### Fixed checklist

- Mutable defaults: `viewer/main.py:698,743,759,808-809` (v1).
- Race condition: unprotected `_running_jobs` (V9).
- Subprocess timeout/resource lifecycle: `viewer/core.py:3370-3385` (V7).
- Bare except and SQL string building: no findings.
- Timezone-naive datetime defect: no clear active defect found.

### Per-file accounting

`viewer/__init__.py` — scanned with nothing found. `viewer/comparison.py` — v11-v16. `viewer/core.py` — V3,V7,V10,V11,V12,v6-v10. `viewer/main.py` — V1,V2,V4-V6,V8,V9,V10,v1-v5,v9.

**Coverage:** 4/4 viewer files explicitly accounted for; 0 critical, 12 major, and 16 minor findings.

## 3. frontend

**Coverage:** 59/59 tracked frontend code files reviewed. 0 critical, 9 major, and 20 minor findings. Production build passed; no frontend `test` script is defined.

### Major findings

- **F1 — No application error boundary** — `frontend/src/App.tsx:75-123` — **major**, resilience. Lazy route/render failures can blank the whole app.
- **F2 — Destructive requests lack explicit auth/CSRF handling and IDs are not encoded** — `frontend/src/api/client.ts:91-112,249-280,433-434,456-516,551-604` — **major**, security. Cookie-authenticated mutations may be CSRF-exposed and interpolated IDs can alter paths; encode IDs and enforce server authorization.
- **F3 — Data-management async loads reject or commit stale state** — `frontend/src/pages/DataManagementPage.tsx:456-469,501-540` — **major**, correctness. Add catches plus abort/request-generation guards.
- **F4 — Live Hub has overlapping refreshes and N+1 account requests** — `frontend/src/pages/LiveHub.tsx:225-299,350-391` — **major**, correctness/performance. Older refreshes can overwrite newer state; serialize/cancel and batch/cache.
- **F5 — Live P&L treats each execution as an independent trade** — `frontend/src/pages/LiveViewer.tsx:946-967` — **major**, financial correctness. It ignores opening/closing matching, partial fills, and fees.
- **F6 — Exit markers use signal time instead of execution time** — `frontend/src/pages/LiveViewer.tsx:620-631` — **major**, chart correctness. Exits appear at the signal candle rather than actual fill.
- **F7 — Benchmark requests can overwrite newer selection state** — `frontend/src/pages/BacktestDetailPage.tsx:134-145` — **major**, race condition. Cancel or guard superseded requests.
- **F8 — Live terminal retains unbounded logs and interpolates job ID into WebSocket URL** — `frontend/src/components/LiveTerminal.tsx:30-67` — **major**, resource/security. Encode the ID and cap/virtualize retained lines.
- **F9 — Backtest modal accepts NaN/out-of-range numeric values** — `frontend/src/components/CreateBacktestModal.tsx:205-235,270-299` — **major**, correctness. Validate finite capital/risk and date ordering before submission.

### Minor findings

- **f1** `frontend/src/components/Modal.tsx:20-68` — **minor**, accessibility: missing dialog semantics, Escape handling, focus trap/restoration.
- **f2** `frontend/src/components/DataTable.tsx:27-40` — **minor**, correctness: mixed/null sort comparator is unstable.
- **f3** `frontend/src/pages/BacktestDetailPage.tsx:505-516,690-707` — **minor**, promise handling: fullscreen rejection is uncaught.
- **f4** `frontend/src/pages/LiveViewer.tsx:579-600` — **minor**, correctness: OHLC values lack structural invariant validation.
- **f5** `frontend/src/components/TaskManager.tsx:26-38` — **minor**, polling: continues while idle and hides all network errors.
- **f6** `frontend/src/api/types.ts:14,125,222,278,341,427,525` — **minor**, Primitive Obsession/type safety: public models use `any`/broad records.
- **f7** `frontend/src/api/client.ts:143-153,208-239,247-260,538,551-554` — **minor**, Duplicated Code/Shotgun Surgery: endpoint wrappers repeat untyped responses and ID construction.
- **f8** `frontend/src/pages/BacktestsPage.tsx:229-247,631-690` — **minor**, Duplicated Code: metric normalization is repeated and can drift.
- **f9** `frontend/src/pages/LiveHub.tsx:1529-1593` — **minor**, Feature Envy: local P&L ledger mishandles completed trades, fees, partial/duplicate fills.
- **f10** `frontend/src/components/ToastProvider.tsx:57-70` — **minor**, lifecycle: timers are not tracked/cleared and IDs can theoretically collide.
- **f11** `frontend/src/pages/BacktestDetailPage.tsx:84-85,142-143` — **minor**, type safety: benchmark `any` bypasses chart contracts.
- **f12** `frontend/src/components/TradingViewChart.tsx:106-112,123-130` — **minor**, XSS-adjacent maintainability: preserve text-only rendering for backend metadata.
- **f13** `frontend/src/pages/BacktestDetailPage.tsx:219-251` — **minor**, correctness: fallback aggregate metrics may describe only the loaded page.
- **f14** `frontend/src/components/BacktestFromSignalsModal.tsx:45-82` — **minor**, stale async state: loading can overwrite reopened/current modal state.
- **f15** `frontend/src/components/ResampleModal.tsx:91-99` and `frontend/src/components/SmartHarvestModal.tsx:101-109` — **minor**, Duplicated Code/type safety: duplicate task-submit patterns and `catch (any)`.
- **f16** `frontend/src/components/FilterBar.tsx:62-78` — **minor**, accessibility: missing button type and expanded-controls semantics.
- **f17** `frontend/src/components/MultiSelectDropdown.tsx:83-226` — **minor**, accessibility: lacks listbox semantics and keyboard/Escape behavior.
- **f18** `frontend/src/pages/TradeChartPage.tsx:505-516` — **minor**, promise handling: fullscreen API rejection is unguarded.
- **f19** `frontend/vite.config.ts:1-20` — **minor**, performance: production chunks exceed 500 KB.
- **f20** `frontend/src/pages/PortfoliosPage.tsx:233-291` and `frontend/src/pages/StrategyLabPage.tsx:1-100` — **minor**, Primitive Obsession: loosely parsed JSON/config lacks schema validation.

### Per-file accounting

`frontend/eslint.config.js` — scanned; no distinct finding. `frontend/src/App.tsx` — F1. `frontend/src/Sidebar.tsx` — scanned. `frontend/src/api/client.ts` — F2,f7. `frontend/src/api/queries.ts` — scanned. `frontend/src/api/types.ts` — f6. `frontend/src/components/BacktestFromSignalsModal.tsx` — f14. `frontend/src/components/Badge.tsx` — scanned. `frontend/src/components/CreateBacktestModal.tsx` — F9. `frontend/src/components/DataTable.tsx` — f2. `frontend/src/components/DownloadModal.tsx` — scanned. `frontend/src/components/DualRangeSlider.tsx` — scanned. `frontend/src/components/FilterBar.tsx` — f16. `frontend/src/components/LiveTerminal.tsx` — F8. `frontend/src/components/LoadingSpinner.tsx` — scanned. `frontend/src/components/Modal.tsx` — f1. `frontend/src/components/MultiSelectDropdown.tsx` — f17. `frontend/src/components/OptimizeModal.tsx` — scanned. `frontend/src/components/RangeFilterControl.tsx` — scanned. `frontend/src/components/RenameModal.tsx` — scanned. `frontend/src/components/ResampleModal.tsx` — f15. `frontend/src/components/SmartHarvestModal.tsx` — f15. `frontend/src/components/Sparkline.tsx` — scanned. `frontend/src/components/StatCard.tsx` — scanned. `frontend/src/components/StrategiesProvider.tsx` — scanned. `frontend/src/components/SymbolInput.tsx` — scanned. `frontend/src/components/TaskManager.tsx` — f5. `frontend/src/components/ToastProvider.tsx` — f10. `frontend/src/components/TradeTable.tsx` — scanned. `frontend/src/components/TradingViewChart.tsx` — f12. `frontend/src/components/index.ts` — scanned. `frontend/src/hooks/liveStreams.ts` — scanned. `frontend/src/hooks/useWebSocket.ts` — scanned. `frontend/src/index.css` — scanned. `frontend/src/lib/optimizationEvidence.ts` — scanned. `frontend/src/lib/queryClient.ts` — scanned. `frontend/src/main.tsx` — scanned. `frontend/src/pages/BacktestDetailPage.tsx` — F7,f3,f11,f13. `frontend/src/pages/BacktestsPage.tsx` — f8. `frontend/src/pages/ComparePage.tsx` — scanned. `frontend/src/pages/DataManagementPage.tsx` — F3. `frontend/src/pages/LiveDetail.tsx` — scanned. `frontend/src/pages/LiveHub.tsx` — F4,f9. `frontend/src/pages/LiveViewer.tsx` — F5,F6,f4. `frontend/src/pages/LoggingPage.tsx` — scanned. `frontend/src/pages/MatrixGridPage.tsx` — scanned. `frontend/src/pages/OptimizationResultsPage.tsx` — scanned. `frontend/src/pages/OptimizationsPage.tsx` — scanned. `frontend/src/pages/PortfolioDetailPage.tsx` — scanned. `frontend/src/pages/PortfoliosPage.tsx` — f20. `frontend/src/pages/Services.tsx` — scanned. `frontend/src/pages/StrategiesPage.tsx` — scanned. `frontend/src/pages/StrategyDetailPage.tsx` — scanned. `frontend/src/pages/StrategyLabPage.tsx` — f20. `frontend/src/pages/TaskHub.tsx` — scanned. `frontend/src/pages/TradeChartPage.tsx` — f18. `frontend/src/utils/stats.ts` — scanned. `frontend/tests/optimizationEvidence.test.ts` — scanned. `frontend/vite.config.ts` — f19.

## 2. live_runner

**Coverage:** 18/18 tracked live_runner code files reviewed. 0 critical, 6 major, and 9 minor findings were recorded below.

### Major findings

- **L1 — cTrader explicit volume metadata can bypass risk sizing** — `live_runner/ctrader_adapter.py:757-779` — **major**, correctness/security. Explicit `ctrader_volume` or `volume` metadata takes precedence over the risk-sized quantity, potentially bypassing configured risk and exposure limits.
- **L2 — Persisting a signal before validating/building the order can permanently block retries** — `live_runner/ctrader_adapter.py:1346-1355` and `live_runner/alpaca_adapter.py:496-511` — **major**, correctness/reliability. Local validation or request construction failures can leave an orphaned canonical signal that later retries treat as an existing submission.
- **L3 — Missing service-specific Discord credentials silently fall back to a generic token** — `live_runner/daemon.py:728-746` — **major**, security/account isolation. A deployment may authenticate with a token belonging to another service or account.
- **L4 — Preflight treats unsupported broker types as valid** — `live_runner/preflight.py:250-309` — **major**, operational safety. Only Alpaca and cTrader are checked; unknown brokers can produce `ok=True`.
- **L5 — cTrader token refresh writes shared global credentials for all accounts** — `live_runner/ctrader_adapter.py:463-487` — **major**, security/correctness. Global keys and environment mutation allow one account’s refresh to replace another’s credentials.
- **L6 — Alpaca datetime conversion preserves timezone-naive values** — `live_runner/alpaca_adapter.py:50-58` — **major**, correctness. Naive SDK datetimes can pass through into live execution records and break aware/naive comparisons.

### Minor findings

- **l1 — Background market-data worker threads are not joined during shutdown** — `live_runner/market_data_stream.py:175-182` — **minor**, resource lifecycle. Shutdown can return while worker threads still use streams or emit callbacks.
- **l2 — Market-data stream replacement and subscription updates are only partially synchronized** — `live_runner/market_data_stream.py:112-173` — **minor**, race condition. Reconnects can race with subscription updates and leave a new stream with stale subscriptions.
- **l3 — Discord polling assumes message IDs are numeric** — `live_runner/signal_sources/discord_source.py:451-488` — **minor**, robustness. Malformed IDs can skip an entire channel iteration.
- **l4 — Discord parser can infer trades from weak textual matches** — `live_runner/signal_sources/discord_source.py:216-241` — **minor**, live-trading safety. Generic or negated prose can be interpreted as an actionable signal.
- **l5 — Debug logging may expose more account payload data than necessary** — `live_runner/account_discovery.py:304-331` — **minor**, security/privacy. Full broker-derived account fields can be persisted in logs.
- **l6 — Temporary files can remain after failed cTrader credential writes** — `live_runner/ctrader_adapter.py:162-169` — **minor**, resource leak/security. Token-containing temporary files may remain after write or replace failures.
- **l7 — Manual live trigger does not disconnect its adapter** — `live_runner/test_signal_trigger.py:30-50` — **minor**, resource leak. SDK resources may remain open until process termination.
- **l8 — `--accounts-env` does not reliably control manual-trigger credentials** — `live_runner/test_signal_trigger.py:62-66` — **minor**, correctness/security. Existing environment variables can override the selected env file, causing an order to use an unintended account.
- **l9 — cTrader deployment selection forces one result when `limit <= 0`** — `live_runner/ctrader_deployment.py:56` — **minor**, correctness. `limit=0` unexpectedly returns one deployment.

### Baseline smells and maintainability hot spots

- **Primitive Obsession:** `live_runner/daemon.py:728-746` uses derived environment-variable names and raw strings as Discord service identity; a typed credential/service reference would reduce cross-service substitution risk — **minor**.
- **Data Clumps:** `live_runner/ctrader_adapter.py:223-251` carries related credential and account-routing values independently; an account-scoped credential object would clarify ownership — **minor**.
- **Duplicated Code:** credential and boolean parsing are repeated across `account_discovery.py`, `alpaca_adapter.py`, `ctrader_adapter.py`, `daemon.py`, and `preflight.py` — **minor**.
- **Divergent Change:** `live_runner/daemon.py` handles adapter lifecycle, Discord lifecycle, deployment synchronization, balance polling, routing, risk preparation, and cleanup — **minor**.
- **Repeated Switches:** broker-specific branching is repeated in `preflight.py`, `daemon.py`, and credential resolution paths — **minor**.
- **Message Chains:** `live_runner/daemon.py:728-746` and `1108-1120` walk through deployment/service/config/environment values for credential resolution — **minor**.
- **Feature Envy:** `live_runner/daemon.py` extensively interprets `LiveDeploymentConfig` internals; this may belong in deployment value-object methods — **minor**.
- **Shotgun Surgery:** credential format changes span `account_discovery.py`, `alpaca_adapter.py`, `ctrader_adapter.py`, `daemon.py`, and `preflight.py` — **minor**.

### Per-file accounting

- `live_runner/__init__.py` — scanned with nothing found.
- `live_runner/account_discovery.py` — l5.
- `live_runner/account_routing.py` — scanned with nothing found.
- `live_runner/alpaca_adapter.py` — L2, L6.
- `live_runner/base_adapter.py` — scanned with nothing found.
- `live_runner/config_parser.py` — scanned with nothing found.
- `live_runner/ctrader_adapter.py` — L1, L2, L5, l6.
- `live_runner/ctrader_deployment.py` — l9.
- `live_runner/daemon.py` — L3; baseline smells.
- `live_runner/market_data_stream.py` — l1, l2.
- `live_runner/preflight.py` — L4; duplicated parsing smell.
- `live_runner/service_discovery.py` — scanned with nothing found.
- `live_runner/signal_sources/__init__.py` — scanned with nothing found.
- `live_runner/signal_sources/base_source.py` — scanned with nothing found.
- `live_runner/signal_sources/discord_source.py` — l3, l4.
- `live_runner/signal_sources/execution_timing.py` — scanned with nothing found.
- `live_runner/stockkit_routing.py` — scanned with nothing found.
- `live_runner/test_signal_trigger.py` — l7, l8.

**Coverage:** 18/18 live_runner files explicitly accounted for; 0 critical, 6 major, and 9 minor findings.

## 1. runner

**Coverage:** 36/36 tracked runner code files reviewed. 0 critical, 10 major, and 11 minor findings were recorded below. The review also identified baseline maintainability smells.

### Major findings

- **M1 — LEAN adapter fails during module import** — `runner/engines/lean_adapter.py:27` — **major**, correctness. `LEAN_PROJECT_DIR` accesses `os.environ` without importing `os`, so importing the adapter raises `NameError`.
- **M2 — Grid tester invokes the engine with a missing required argument** — `runner/grid_tester.py:74` — **major**, correctness. `engine.run(config)` omits the required `strategy_path`, so grid cells fail with `TypeError`.
- **M3 — Walk-forward fallback calls `attach_daily_equity` with invalid keywords** — `runner/walkforward.py:121` — **major**, correctness. The call uses `start_date`/`end_date`, while the function accepts `start`/`end`, causing `TypeError` when no equity curve is returned.
- **M4 — Native engine uses close price for market entries despite next-open execution semantics** — `runner/engine.py:362-364` — **major**, correctness. Sizing and risk calculations begin from the signal-bar close while fills occur at the next bar open, producing inconsistent execution and risk calculations.
- **M5 — Vectorbt equity curve timestamps realized P&L at entry** — `runner/engines/vectorbt_adapter.py:412-417` — **major**, correctness. Closed-trade P&L is attached to `entry_time`, creating look-ahead timing in drawdown and return metrics.
- **M6 — PyneCore helper execution is not sandboxed** — `runner/engines/pynecore_adapter.py:272` — **major**, security. `exec(compile(...))` executes repository/user-provided helper code with filesystem, process, network, and environment access.
- **M7 — Auto-agent subprocess timeout is configured but not enforced** — `runner/auto_agent.py:798-820` — **major**, correctness/resource management. `_run_backtest()` streams a `Popen` child without a timeout, so hung generated strategies can block indefinitely.
- **M8 — Portfolio optimization combines absolute equity levels** — `runner/portfolio_optimizer.py:223-233` — **major**, correctness. Curves are weighted before normalization, so starting capital differences distort allocation and portfolio returns.

### Major security findings

- **S1 — Copilot SDK grants all permission requests** — `runner/llm_providers/copilot_sdk_provider.py:301-307` — **major**, security. Passing `PermissionHandler.approve_all` unconditionally authorizes SDK permission requests.
- **S2 — Generated strategy modules execute arbitrary Python** — `runner/auto_agent.py:239-241` — **major**, security. LLM-generated source is written and launched without robust static policy enforcement, sandboxing, or filesystem/network restrictions.

### Minor findings

- **m1 — Daily equity attachment ignores date arguments** — `runner/daily_equity.py:51-58,73-75` — **minor**, correctness. `start` and `end` are accepted but not applied, so out-of-range trades are included.
- **m2 — Daily equity timestamps are normalized to midnight** — `runner/daily_equity.py:77-79` — **minor**, correctness. Daily P&L is timestamped at midnight instead of the last exit time, which can create ordering/boundary issues with intraday points.
- **m3 — LEAN result metadata discards source period and symbols** — `runner/converters/lean_converter.py:96-112` — **minor**, correctness. Converted metadata always emits empty dates and symbols.
- **m4 — Stockkit fallback timestamps may remain timezone-naive or malformed** — `runner/converters/stockkit_converter.py:150-153` — **minor**, correctness. Parse failures return unvalidated, timezone-free timestamp strings.
- **m5 — `target_sharpe` is unused** — `runner/portfolio_optimizer.py:274-287` — **minor**, correctness/maintainability. The documented parameter is never used as a constraint or objective input.
- **m6 — Portfolio objective uses raw rather than annualized Sharpe** — `runner/portfolio_optimizer.py:223-240` — **minor**, correctness. The ratio lacks annualization and risk-free-rate treatment despite being labeled Sharpe.
- **m7 — Risk-parity covariance selection lacks stability guards** — `runner/optimizer.py:543-563` — **minor**, model risk. Insufficient history, unstable covariance, and OOS symbol availability are not specifically guarded.
- **m8 — Risk-parity weight constraints can become inconsistent** — `runner/risk_parity_optimizer.py:76-82` — **minor**, correctness. An infeasible upper bound is silently changed to `1.0` rather than rejected or reported.
- **m9 — Vectorbt parallel execution can partially process before failing** — `runner/engines/vectorbt_adapter.py:364-394` — **minor**, correctness/maintainability. Successful work is accumulated and then discarded when any future failure causes the overall run to raise.
- **m10 — Broad exception suppression hides invalid strategy metadata** — `runner/engines/base.py:74-84` — **minor**, correctness/maintainability. All metadata-loading exceptions silently become empty metadata.
- **m11 — Grid tester converts missing metrics to plausible zeroes** — `runner/grid_tester.py:77-84` — **minor**, correctness. Missing statistics become indistinguishable from measured zero performance.

### Baseline smells and maintainability hot spots

- **B1 — Duplicated Code / Repeated Switches:** engine selection is independently mapped in `runner/cli.py:42-65`, `runner/backtest_grid.py:190-208`, and `runner/walkforward.py:319-322` — **minor**. A shared registry/factory would reduce drift.
- **B2 — Duplicated Code:** timestamp parsing and date-boundary policy are repeated in `runner/engines/vectorbt_adapter.py:1050-1062`, `runner/engines/signal_replay_adapter.py:21-35`, `runner/walkforward.py:21-35`, `runner/stats.py:219-229`, and `runner/converters/stockkit_converter.py:150-153` — **minor**.
- **B3 — Duplicated Code / Data Clumps:** indicator/context normalization is repeated in `runner/engines/vectorbt_adapter.py:567-593`, `runner/engines/pynecore_adapter.py:365-405`, `runner/strategy_ir/builder.py:117-144`, and `runner/strategy_ir/normalizer.py:73-154` — **minor**.
- **B4 — Divergent Change:** `runner/engine.py:128-903` combines models, sizing, strategy invocation, fills, stops/TP, replay, accounting, serialization, and result construction — **minor**.
- **B5 — Divergent Change / Shotgun Surgery:** `runner/auto_agent.py:106-843` combines provider orchestration, prompting, parsing, filesystem writes, artifact handling, and validation — **minor**.
- **B6 — Primitive Obsession / Data Clumps:** execution/risk policies are passed as loose strings, floats, and nested dictionaries across `runner/engine.py:32-46,156-166`, `runner/engines/vectorbt_adapter.py:279-303`, and `runner/engines/pynecore_adapter.py:569-631` — **minor**.
- **B7 — Data Clumps / Mysterious Name:** strategy identity, slug, source, documentation, and output paths travel as separate values across `runner/auto_agent.py:96-104`, `runner/engines/base.py:104-122`, `runner/optimizer.py:373-383`, and `runner/engines/vectorbt_adapter.py:344-355` — **minor**.
- **B8 — Repeated Switches:** engine/provider selection is repeated in `runner/cli.py:42-65`, `runner/backtest_grid.py:190-208`, `runner/converters/cli.py:29-52`, `runner/auto_agent.py:354-372`, and `runner/strategy_ir/routing.py:46-88` — **minor**.

### Per-file accounting

**Findings produced:** `runner/auto_agent.py` (M7, S2, B5); `runner/backtest_grid.py` (M2, m11, B1); `runner/backtest_matrix.py` (scanned; no distinct finding); `runner/cli.py` (B1, B8); `runner/converters/cli.py` (B8); `runner/converters/lean_converter.py` (m3); `runner/converters/stockkit_converter.py` (m4, B2); `runner/daily_equity.py` (m1, m2); `runner/engine.py` (M4, B4, B6); `runner/engines/base.py` (m10, B7); `runner/engines/lean_adapter.py` (M1); `runner/engines/native_adapter.py` (M4); `runner/engines/pynecore_adapter.py` (M6, B3); `runner/engines/signal_replay_adapter.py` (B2); `runner/engines/vectorbt_adapter.py` (M5, m9, B2); `runner/grid_tester.py` (M2, m11); `runner/optimizer.py` (m7, B8); `runner/portfolio_optimizer.py` (M8, m5, m6); `runner/risk_parity_optimizer.py` (m8); `runner/stats.py` (B2); `runner/strategy_ir/builder.py` (B3); `runner/strategy_ir/normalizer.py` (B3); `runner/strategy_ir/routing.py` (B8); `runner/walkforward.py` (M3, B1, B2).

**Scanned with nothing distinct found:** `runner/__init__.py`; `runner/capm.py`; `runner/converters/__init__.py`; `runner/engines/__init__.py`; `runner/llm_providers/__init__.py`; `runner/llm_providers/base.py`; `runner/llm_providers/copilot_cli_provider.py`; `runner/llm_providers/nim_provider.py`; `runner/strategy_ir/__init__.py`; `runner/strategy_ir/codegen_vectorbt.py`; `runner/strategy_ir/models.py`.
