# Comparison Matrix Change Notes

Before/after notes required by the
[reference artifact contract](reference-artifact-contract.md#protected-baseline-files)
whenever a protected baseline file changes parity-sensitive behavior. Each entry
records the old and new `deltas[]` values of the comparison matrix for the
committed comparison cases, plus the remaining limitations.

## #228 — MT5 importers store fees as a positive cost

- Changed behavior, protected files:
  - `validation/importers/mt5_xlsx.py`, `validation/importers/mt5_html.py`:
    `fees = -(Kommission + Swap)` (MT5 signs costs negative), so `fees` is a
    positive cost, a net swap credit a negative fee, and `net_pnl = pnl - fees`
    no longer adds costs to the net. `meta.commission` / `meta.swap` keep the raw
    signed MT5 values. An unparsable fee cell counts as 0 like an empty cell
    (pre-existing behaviour, maintainer decision: no fee-cell guard).
  - Tests: `tests/validation/test_mt5_xlsx_importer.py`,
    `tests/validation/test_mt5_html_importer.py`,
    `tests/validation/test_golden_reconciliation.py` (now also pins the goldens'
    money fields to a fresh import of the tracked reports).
- Why: before, every MT5 cost was added to the trade net instead of subtracted.
- Before/after: Σ trade `net_pnl` of the three committed goldens now equals MT5
  `Nettogewinn gesamt` exactly: 21868.81 / 43791.14 / 39255.92 (before, it was
  off by twice the total costs). Matrix rows: **unchanged** — the comparison
  matrix metrics come from the XLSX header stats, not from per-trade fees.
- Remaining limitations:
  - An unparsable Kommission/Swap cell counts as 0 (and a literal `nan` text
    cell yields a NaN net); there is no fee-cell guard by maintainer decision,
    new report formats are verified by reading the report.
  - Per-exit `fees` in the goldens are `null`, so per-exit cost attribution is
    not recorded; costs are carried on the trade.

## #259 — risk-parity 2% rebalance gate marks through the previous close

- Changed behavior, protected files:
  - `scripts/run_risk_parity_backtest.py`: the drifted-weight trade gate, the
    trade delta and the realized-PnL check now key off the holding weight
    marked through the PREVIOUS return bar — the
    `HoldingsValue`/`TotalPortfolioValue` snapshot QC reads at its 10:00
    Rebalance — instead of the rebalance day's own close. The carried weight
    book (`next_weights`) and the published portfolio value still mark through
    the rebalance-day close, the MOC fill bar. The mark-to-market math is
    shared via `_drift_weights`; only the end bar differs.
  - `tests/strategies/test_risk_parity.py`: new parametrized test
    `test_trade_gate_ignores_the_rebalance_day_close` — a same-day SPY jump
    does not trade, the previous-session jump does.
  - `strategy-comparisons/risk_parity/risk_parity_reference.json`:
    regenerated on yfinance data (see below); the platform artifact in
    `data/backtests/` is not committed.
- Why: maintainer decision on #259 (2026-11-18): the rebalance day's close is
  not known at the 10:00 decision, so gating on it is a one-day look-ahead.
  The other #188 no-look-ahead guarantees (covariance window, hedge SMA,
  start date) are unchanged.
- Before/after matrix: reference stats before -> after (QC run "Sleepy Tan
  Coyote" truth in parentheses): total return 0.9296 -> 0.9500 (0.9378);
  CAGR 7.58% -> 7.70% (7.62%); max drawdown -17.85% -> -17.66% (-17.70%);
  Sharpe (rf 2.5%) 0.351 -> 0.361 (0.372); annual vol 8.76% -> 8.76% (8.7%).
  Regenerated 2026-11-21 on the same yfinance `auto_adjust=True` download
  convention as the 2026-11-13 artifact (`download_prices`,
  `lean_statistics=True`, `risk_free_rate=0.025`); the platform artifact in
  `data/backtests/` is not committed. yfinance revised the adjusted series
  between the two downloads, so the before/after numbers above mix the code
  change with a data revision: re-running the PRE-change code on the
  2026-11-21 download gives 0.9354 return / 0.3535 Sharpe / 265 trades, i.e.
  the code change alone is 0.9354 -> 0.9500 and 0.3535 -> 0.3606 on identical
  data. Order-leg parity vs the QC "Sleepy Tan Coyote" orders is 250/264
  identical in (date, symbol, sign), up from 249/264 on 2026-11-13.
- Remaining limitations:
  - Order sizing (`position_size`) and the realized-PnL factor still mark
    the portfolio through the rebalance-day close; QC's `SetHoldings` sizes
    off the same 10:00 snapshot. The gate — the decision #259 scoped — is
    previous-close; sizing parity remains a documented residual.
  - No fee model, fractional shares, QC sizing MOC orders at the previous
    close, and yfinance-vs-QC adjusted-price differences (median fill gap
    0.1%).

## #203 — comparison matrix reads MT5 reports on the broker clock

- Changed behavior, protected files:
  - `validation/importers/mt5_xlsx.py`: `load(..., broker_clock=...)` converts
    report timestamps through the named broker clock (`BrokerClock.to_utc`),
    mutually exclusive with `source_timezone`; `raw_metadata` records
    `broker_clock` (`source_timezone` is `null` then). A report time the clock
    skipped (2024-04-06 02:00-03:00 server) raises; it is no longer returned as
    the raw string.
  - `validation/statistics/cross_platform.py`: each MT5 case names the clock its
    platform run used (`dukascopy_mt5_forex` for EURUSD, `dukascopy_mt5_index`
    for NAS100/SP500, the same as `COMPARISON_CASES`); the row publishes
    `reference_broker_clock` in place of `reference_source_timezone`.
  - `scripts/analyze_mt5_parity.py`: reads each report on its case clock and
    buckets sessions and years on that clock's server time.
  - Tests: `tests/test_broker_clock.py`, `tests/validation/test_mt5_xlsx_importer.py`,
    `tests/validation/test_cross_platform_metrics.py`, `tests/test_analyze_mt5_parity.py`.
- Why: MT5 prints `TimeCurrent()`, the broker's server wall clock. That clock has
  three offset epochs; `Europe/Athens` matches only one of them. Evidence and
  examples: `strategy-comparisons/intraday_volatility_breakout/audit/README.md`.
  Example: SP500 server 2024-04-01 17:15 is 15:15Z on the broker clock, 14:15Z
  under Athens.
- Before/after matrix: **not produced in this change**, for the same reason as
  #186 (the platform artifacts in `data/backtests/` are not committed). Matrix
  metrics come from the XLSX header stats and are unchanged; what moves is the UTC
  instant of each reference trade timestamp (1 h for the affected epochs).
- Remaining limitations:
  - `scripts/generate_golden_files.py` still calls `importer.load(path)` with no
    clock, so the committed goldens in `tests/fixtures/validation/goldens/` carry
    server wall time labelled as UTC. The goldens are checked for shape only, not
    timestamps. Re-generating them on the broker clock is a separate change.
  - `scripts/analyze_mt5_parity.py` hardcoded `MT5_SOURCE_TIMEZONE = "Europe/Athens"`
    before this change; that constant is removed.

## #188 — risk-parity QC reproduction (no look-ahead, start date, drifting weights)

- Changed behavior, protected files:
  - `scripts/run_risk_parity_backtest.py`: covariance/regime/SMA200 windows end at
    the previous close (QC decides at 10:00); rebalancing starts at `start_date`
    and the equity curve starts at the last bar before it; positions are
    buy-and-hold and the 2% trade gate compares against the drifted weight;
    history download starts 200 calendar days earlier (`price_history_start`).
  - `scripts/regenerate_qc_risk_parity_artifact.py`: loads local history from
    `price_history_start(...)` so offline regeneration keeps the margin.
  - `tests/validation/test_cross_platform_metrics.py`: `risk_parity_quantconnect`
    platform Sharpe pin 0.213296 -> 0.350569 (regenerated reference).
  - Focused tests: `tests/strategies/test_risk_parity.py`,
    `tests/test_qc_risk_parity_artifact_loader.py`.
- Before/after matrix (`risk_parity_quantconnect`, reference = QC run): total
  return 0.725 -> 0.9296 (QC 0.9378); CAGR 6.24% -> 7.58% (7.62%); max drawdown
  25.5% -> 17.85% (17.70%); Sharpe (rf 2.5%) 0.229 -> 0.351 (0.372); annual vol
  10.0% -> 8.76% (8.7%). Figures are from the PR description's regenerated run
  (yfinance, pulled 2026-11-13); the artifacts in `data/backtests/` are not
  committed, so the matrix payload was not re-diffed in this change.
- Remaining limitations:
  - The 2% trade gate uses the drifted weight marked through the rebalance-day
    close, which is also the MOC fill bar. QC evaluates
    `HoldingsValue/TotalPortfolioValue` at 10:00, i.e. through the previous
    close. Legs within one day's drift of the gate can therefore flip; the 15+16
    unmatched QC legs sit within ~0.3pp of it. Moving the gate to the previous
    bar needs a regenerated reference and re-pinned stats (local data is not in
    the repo), so it is left as a follow-up decision — resolved by #259 (see
    the #259 entry above).
  - No fee model, fractional shares, QC sizing MOC orders at the previous close,
    and yfinance-vs-QC adjusted-price differences (median fill gap 0.1%).

## #186 — MT5 parity: server-clock D1 bars and explicit broker clocks

- Changed behavior, protected files:
  - `scripts/regenerate_mt5_parity_artifacts.py`: every comparison case now runs
    on an explicit `broker_clock` (`dukascopy_mt5_forex` for EURUSD,
    `dukascopy_mt5_index` for NAS100/SP500). EURUSD builds its D1 bars from the
    M1 bars on the broker's server clock (`use_native_daily_ohlcv: False`) instead
    of the UTC-day `1d` parquet.
  - `tests/validation/test_cross_platform_metrics.py`: the MT5 first-entry
    timestamp assertion is skipped with a reason when the locally generated
    artifact is absent (the artifacts are not committed; see the reference
    artifact contract).
  - `tests/test_mt5_parity_artifact_loader.py`: new tests for the regenerate
    script's case selection, EUR-account conversion gate and clock plumbing.
- Why: the MT5 tester history builds D1 bars and the weekend/holiday rows on the
  broker's server clock; the UTC-day bars put the day boundaries in other places.
  Evidence: `strategy-comparisons/intraday_volatility_breakout/audit/README.md`.
- Before/after matrix: **not produced in this change.** The platform artifacts in
  `data/backtests/` are generated locally (multi-GB data run) and are not
  committed, so the `build_comparison_matrix()` deltas cannot be recomputed from
  the repository alone. The reported before/after numbers (for example SP500 net
  PnL 71,758.82 -> 32,633.26 for the strategy itself) are in the audit README.
  Recomputing the matrix deltas needs the local regenerated artifacts.
- Remaining limitations:
  - The matrix still reads MT5 XLSX times as `Europe/Athens`
    (`validation/statistics/cross_platform.py`). That is 1 h off for the index
    history in summer 2018–2023 and in US-only DST weeks from 2024-04; it affects
    day boundaries in the matrix, not the trade counts. **Resolved in #203**
    (see above).
  - The artifact-dependent guards skip, with a reason, when the artifacts are
    absent. This is by design: no CI download of multi-GB data.

## #128 / PR #136 — relative deviation measured against the reference

- Changed behavior: `validation/statistics/cross_platform.py::_metric_delta`.
- Before: `relative_delta = absolute_delta / max(|reference|, |platform|)`,
  reported as `0.0` whenever that denominator was zero.
- After: `relative_delta = absolute_delta / |reference|`. A zero reference with
  a non-zero platform now yields `relative_delta: null` plus a note; zero on
  both sides stays `0.0`.
- Why: the symmetric denominator halved real drift — a reference of 100 against
  a platform of 200 read as 50%, not 100% (#128). Readiness already measured
  against the reference; the same PR moves the risk-comparison gate
  (`validation/risk/comparison.py`) from the symmetric base to the reference.
- Focused tests: `tests/test_relative_deviation_reference.py`.

### Before/after matrix

Method: `build_comparison_matrix()` over the committed references in
`strategy-comparisons/` and the locally generated platform artifacts in `data/backtests/` (not committed), run
once on the pre-change `cross_platform.py` and once on the change. Every key of
the payload except `generated_at` and the values below is identical between the
two runs: reference/platform stats, `absolute_delta`, paths, notes, missing
artifacts and the metric list do not change. 7 of the 20 metric deltas change;
the other 13 are unchanged because `|platform| <= |reference|` for them, where
the old denominator already equalled `|reference|`.

To reproduce, call `build_comparison_matrix` with those two directories, swap
`validation/statistics/cross_platform.py` for its pre-change revision for the
second call, and diff the two payloads with `generated_at` removed.

| case_id | metric | reference | platform | absolute_delta | relative_delta before | relative_delta after |
|---|---|---:|---:|---:|---:|---:|
| `intraday_volatility_breakout_sp500_m1` | `trade_count` | 512 | 576 | 64 | 0.111111 | 0.125000 |
| `intraday_volatility_breakout_sp500_m1` | `net_profit` | 39255.92 | 55717.15 | 16461.23 | 0.295443 | 0.419331 |
| `intraday_volatility_breakout_sp500_m1` | `cagr` | 0.03985281381 | 0.053637 | 0.01378418619 | 0.256990 | 0.345877 |
| `intraday_volatility_breakout_eurusd_h1` | `trade_count` | 492 | 498 | 6 | 0.012048 | 0.012195 |
| `intraday_volatility_breakout_eurusd_h1` | `net_profit` | 21868.81 | 28083.36 | 6214.55 | 0.221289 | 0.284174 |
| `intraday_volatility_breakout_eurusd_h1` | `cagr` | 0.02361450976 | 0.029631 | 0.006016490239 | 0.203047 | 0.254779 |
| `risk_parity_quantconnect` | `sharpe_ratio` | 0.213296 | 0.3509 | 0.137604 | 0.392146 | 0.645132 |

No committed comparison case has a zero reference, so the `null` relative-delta
shape is covered by unit tests instead of by this matrix.

### Remaining limitations

- Both runs use the current platform artifacts. No new backtest was produced for
  this change, so this is a before/after of the reported deltas, not new parity
  evidence.
- For a non-zero reference the new denominator is never larger than the old one,
  so a tolerance gate can only become stricter or stay the same; it can never
  accept a drift the old rule rejected.
- `relative_delta: null` is a new shape in the serialized matrix. No in-repo
  consumer reads `deltas[].relative_delta` for gating — `_row_payload` only
  serializes it — and the matrix carries no schema version, so external readers
  must treat `null` as "no proportion exists for a zero reference".
- The regenerated matrix artifact is not committed: the generator's default
  output directory (`reports/`) was removed in #101. This note replaces the
  artifact for the purpose of the contract.
- `validation/comparison/matcher.py` keeps its symmetric
  `max(|ref|, |act|, 1e-12)` denominator on purpose: it ranks candidate trade
  pairings rather than gating a tolerance.
- The denominator guard test matches the literal `max(abs(a), abs(b))` spelling
  (single- or multi-line). A rewritten symmetric denominator, such as the
  matcher's, is a review question, not something the regex can detect.
