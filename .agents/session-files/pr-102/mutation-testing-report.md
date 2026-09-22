# Mutation testing report — PR #102 (issues batch 76–87)

Scope: highest-risk new logic in the PR (first review cycle).
Runner: `uv run pytest` on targeted subsets. Every mutation reverted after its run (`git checkout -- <file>` verified clean).

| # | File | Mutation | Result | Diagnostic | Catching test(s) |
|---|------|----------|--------|------------|------------------|
| M2 | `runner/fees.py` | Missing fee config returns `ZeroFeeModel()` instead of raising | Killed | Clear | `test_missing_config_raises`, `test_trading_engine_run_without_fee_config_raises` |
| M-A | `validation/readiness/validator.py` | `_abs_pct_diff` denominator back to `max(\|expected\|,\|actual\|)` | Killed | Clear | `test_halved_denominator_fixed` |
| M-B | `live_runner/ctrader_adapter.py` | JPY fallback class removed (JPY falls through to FX) | Killed | Clear | `test_ctrader_fx_and_jpy_volume_scales_match_lots`, `test_ctrader_jpy_relative_sl_tp_uses_three_digits` |
| M-C | `runner/optimizer.py` | `p_value` study direction forced to `maximize` | Killed | Clear | `test_study_direction_matches_metric_semantics`, `test_run_optimization_creates_minimize_study_for_p_value` |

## Mutation score

4/4 sampled mutants killed (100% on sample).

## Gaps found and closed

- `runner/fees.py` (new module) had **zero** direct unit tests: no test asserted the missing-config hard error or the model math. Added `tests/test_fees.py` (11 tests: resolve precedence/conflicts, `NotionalFeeModel` math + guards, `ZeroFeeModel`, engine-level hard-error run). M2 verified killed by the new tests before revert.

## Diagnostic quality

All kills were Clear: test names name the behavior (`halved_denominator`, `jpy_relative`, `study_direction_matches_metric_semantics`) and assertion messages point at the root value.

## Notes

- Full-file mutation across all 76 changed files was not run (suite cost); sampling targeted the money paths (#80 scales), gates (#83 denominator/Sharpe), selection bias (#82 direction), and the new fee interface (#84).
- Pre-existing suites already killed the engine-integration equivalents (e.g. the 27-failure fallout when fee plumbing landed without fixture declarations).
