# Comparison Matrix Change Notes

Before/after notes required by the
[reference artifact contract](reference-artifact-contract.md#protected-baseline-files)
whenever a protected baseline file changes parity-sensitive behavior. Each entry
records the old and new `deltas[]` values of the comparison matrix for the
committed comparison cases, plus the remaining limitations.

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
    day boundaries in the matrix, not the trade counts. Follow-up.
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
