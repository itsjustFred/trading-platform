# Architecture Orchestration Status - 2026-07-13

## Scope

This status records the post-restart recovery and merge validation for the architecture-alignment branch queue on `codex/new-main`. The external worktrees under `/home/ubuntu/worktrees` were not used after the restart because sandbox access was unreliable; recovery branches were created in the main writable workspace instead.

## Resource Policy Used

- No concurrent subagents were used during CodeRabbit, pytest, or compile gates.
- System load was checked before heavy steps; observed load stayed below `0.30` with about `20.7-21.0 GB` memory available.
- CodeRabbit was run serially in tmux sessions.

## Completed Slices

### Intraday Signal Contract

- Recovery branch: `codex/intraday-signal-contract-recovery`
- Merged commit: `56d5d44 Add intraday signal contract metadata`
- Files changed:
  - `strategies/intraday_volatility_breakout/intraday_volatility_breakout.py`
  - `tests/strategies/test_intraday_volatility_breakout.py`
- Result:
  - Added metadata-only signal contract fields: `signal_id`, `strategy_id`, `entry_reference_price`, `order_timing`, `order_type`, and `exit_reason`.
  - Preserved existing entry/exit mask behavior.
  - `entry_reference_price` records the side-specific signal price and preserves candle-close shifting semantics.
  - `exit_reason` preserves `time_exit` precedence over EOD fallback.
- Validation:
  - `python -m py_compile strategies/intraday_volatility_breakout/intraday_volatility_breakout.py`
  - `pytest tests/strategies/test_intraday_volatility_breakout.py -q`: `15 passed`
  - CodeRabbit uncommitted review with base `codex/new-main`: `findings:0`

### Risk Parity Portfolio Target Contract

- Recovery branch: `codex/risk-parity-target-contract-recovery`
- Merged commit: `031027d Add risk parity portfolio target metadata`
- Files changed:
  - `scripts/run_risk_parity_backtest.py`
  - `tests/strategies/test_risk_parity.py`
- Result:
  - Added `portfolio_target_signals` to `run_backtest()` output.
  - Added stable `risk_parity:<timestamp>` target signal IDs.
  - Linked materialized rebalance trades to `portfolio_target_signal_id` in trade metadata.
  - Preserved the documented `rebalance_threshold=0.02`, open-price fill preference, trade counts, PnL semantics, and attribution metric bases.
- Validation:
  - `python -m py_compile scripts/run_risk_parity_backtest.py`
  - `pytest tests/strategies/test_risk_parity.py -q`: `11 passed`
  - CodeRabbit uncommitted review with base `codex/new-main`: `findings:0`

## Combined Post-Merge Gate

Command:

```bash
python -m py_compile strategies/intraday_volatility_breakout/intraday_volatility_breakout.py scripts/run_risk_parity_backtest.py && \
pytest tests/strategies/test_intraday_volatility_breakout.py tests/strategies/test_risk_parity.py -q && \
git diff --check
```

Result: `26 passed`, compile checks passed, whitespace check passed.

## Current Repository State

- Branch: `codex/new-main`
- Latest merged architecture commit: `031027d Add risk parity portfolio target metadata`
- Branch is ahead of `origin/codex/new-main` by 7 commits.
- Remaining uncommitted pre-existing change: `reports/strategy_metric_comparison_matrix.json`, timestamp-only matrix regeneration diff. It was intentionally not included in either slice commit.

## Broad Post-Merge Gates

- `pytest tests/validation/ tests/validators/ -q`: `467 passed`
- `cd frontend && npm ci && npm run build`: passed. NPM reported 4 existing audit findings; no frontend source or lockfile changes were produced.

## Remaining Work

- External worktree branches `codex/intraday-signal-contract` and `codex/risk-parity-target-contract` still point at the pre-recovery commit `699ecc3`; the merged recovery branches contain the authoritative completed work in `codex/new-main`.
- Remaining uncommitted pre-existing change: `reports/strategy_metric_comparison_matrix.json`, timestamp-only matrix regeneration diff.
- No new external MT5/QC evidence was introduced in this architecture slice; parity-sensitive metric semantics were not changed.
