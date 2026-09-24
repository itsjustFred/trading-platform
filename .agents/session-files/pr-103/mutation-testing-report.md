# Mutation testing report — PR #103 (mega-batch, stacked on #102)

Scope: sampled high-risk new logic (first cycle). Each mutant applied, tested
on a targeted subset, reverted (`git checkout -- <file>`, verified clean).

| # | File | Mutation | Result | Diagnostic | Catching test(s) |
|---|------|----------|--------|------------|------------------|
| M1 | `live_runner/credential_env.py` | `resolve_env_value` falls back on empty (the `or` hole) | Killed (2 tests) | Clear | `test_alpaca_adapter_empty_file_value_not_shadowed` + file-wins test |
| M2 | `live_runner/daemon.py` | Synthetic TP reintroduced (`* 1.01`) | Killed | Clear | `test_submit_order_with_risk_fails_closed_without_take_profit` |
| M4 | `validation/comparison/matcher.py` | Min-cost → first-fit | Killed (10, cascading) | Clear (dedicated) | `test_closer_price_wins_over_earlier_index` |
| M5 | `runner/optimizer.py` | Haircut call-site disabled (`if False`) | SURVIVED → test added, then killed | — | NEW `test_sparse_trial_returns_adjusted_value` (end-to-end wiring) |
| M8 | `runner/converters/lean_converter.py` | Everything scales in, nothing closes | Killed (2) | Clear | partial-fill + fee tests |
| M9 | `runner/strategy_ir/routing.py` | Unknown mode falls back to default | Killed | Clear | `test_unknown_rollout_mode_fails_closed` |

## Mutation score

6/6 sampled mutants killed after closing the M5 gap (5/6 on first pass).

## Gaps found and closed

- M5: unit tests covered `_risk_adjusted_metric_value` but nothing covered
  the objective wiring (return value + `penalty_terms` attrs). Added
  `TestRiskAdjustedObjectiveWiring::test_sparse_trial_returns_adjusted_value`
  (mocked 3-trade backtest through a real 1-trial study) and re-verified the
  kill by construction (mutant returns 300, test expects 30).
