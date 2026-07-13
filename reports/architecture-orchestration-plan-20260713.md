# Architecture Orchestration Plan - 2026-07-13

Base branch: `codex/new-main`  
Base commit at plan start: `84bfeaf` (`Document final cross-platform parity status`)  
Worktree root: `/home/ubuntu/worktrees`

Commit `84bfeaf` includes the completed parity baseline and must be treated as
the protected starting point for all child branches. In particular it includes:

- Risk Parity corrections in `scripts/run_risk_parity_backtest.py`,
  `scripts/regenerate_qc_risk_parity_artifact.py`,
  `tests/strategies/test_risk_parity.py`, and
  `tests/test_qc_risk_parity_artifact_loader.py`.
- MT5 timezone and comparison metadata fixes in
  `validation/importers/mt5_xlsx.py`,
  `validation/statistics/cross_platform.py`, and
  `tests/validation/test_cross_platform_metrics.py`.
- MT5 artifact and diagnostic fixes in
  `scripts/regenerate_mt5_parity_artifacts.py`,
  `scripts/analyze_mt5_parity.py`,
  `tests/test_mt5_parity_artifact_loader.py`, and
  `tests/test_analyze_mt5_parity.py`.

Branches touching these protected files must rebase onto the latest
`codex/new-main`, preserve the existing parity semantics, and document every
conflict resolution in their branch status note.

## Objective

Move the platform from ad-hoc strategy/backtest parity fixes toward a
mergeable, testable architecture where:

1. strategies emit signal intent and levels,
2. risk sizing is handled by a dedicated risk layer,
3. execution/fill modelling is handled by a dedicated execution layer,
4. reporting/statistics consume canonical trade/event artifacts, and
5. MT5/QuantConnect parity gaps are pursued only where reference evidence is
   strong enough to support code changes.

## Operating Rules

- Every implementation task gets its own branch and worktree under
  `/home/ubuntu/worktrees`.
- Workers must own disjoint write scopes.
- Resource throttle:
  - run at most two subagents at the same time;
  - prefer one active worker plus one lightweight explorer;
  - split broad audits into small, bounded prompts to avoid CPU/RAM pressure and
    stream disconnects;
  - pause new agents when local tests, frontend builds, or CodeRabbit are
    running.
- The main worktree stays on `codex/new-main` and is used only for orchestration,
  review integration, and final merge validation.
- CodeRabbit is serialized by the orchestrator. Do not let worker branches run
  ad-hoc reviews without being scheduled.
- Target CodeRabbit rate: at most 3 reviews/hour, with one active review at a
  time.
- No worker may merge, reset, force-push, or touch unrelated branches.
- Each branch must finish with:
  - focused tests for its scope,
  - `git diff --check`,
  - Python compile checks for changed Python files,
  - CodeRabbit `findings:0` for changed files,
  - a concise status note in `reports/` or the relevant docs file.

## Ownership Matrix

Shared contracts and files have a single writer branch. Other branches may read
them, but must not edit them until the owning branch has merged and their
worktree has been rebased onto the updated `codex/new-main`.

| Area | Owning Branch | Writable Paths | Read-Only For |
|---|---|---|---|
| Signal/risk/execution contract docs and models | `codex/sre-contract-design` | `docs/signal-risk-execution-contract.md`, `runner/strategy_ir/models.py`, `runner/strategy_ir/normalizer.py`, `runner/strategy_ir/routing.py`, `tests/test_strategy_ir_builder.py`, `tests/test_strategy_ir_normalizer.py`, `tests/test_strategy_ir_routing.py`, `tests/test_strategy_ir_codegen_vectorbt.py`, `tests/fixtures/sre_contract/` | all other branches until merged |
| Intraday strategy signal adapter | `codex/intraday-signal-contract` | `strategies/intraday_volatility_breakout/`, `tests/strategies/test_intraday_volatility_breakout.py`, `tests/fixtures/intraday_signal_contract/` | risk and execution branches |
| Risk decision and sizing | `codex/risk-sizing-contract` | `risk/`, `runner/engines/signal_replay_adapter.py`, `tests/test_risk_engine.py`, `tests/test_risk_consistency.py`, `tests/test_live_risk_sizing.py`, `tests/test_signal_bots_risk.py` | intraday and execution branches |
| Native execution/fill model | `codex/execution-fill-model` | `runner/engine.py`, `runner/engines/native_adapter.py`, `tests/test_native_engine_execution.py`, `tests/test_trade_engine.py`, `tests/fixtures/execution_fill_model/` | signal and risk branches |
| Risk Parity target/rebalance model | `codex/risk-parity-target-contract` | `scripts/run_risk_parity_backtest.py`, `runner/risk_parity_optimizer.py`, `strategies/risk_parity/`, `tests/strategies/test_risk_parity.py`, `tests/test_qc_risk_parity_artifact_loader.py` | all non-Risk-Parity branches |
| External reference schemas/importers | `codex/reference-artifact-contract` | `docs/reference-artifact-contract.md`, `validation/importers/mt5_xlsx.py`, new dedicated importer modules under `validation/importers/`, `validation/statistics/cross_platform.py`, `tests/validation/test_mt5_xlsx_importer.py`, `tests/validation/test_cross_platform_metrics.py`, new dedicated importer tests | all runtime refactor branches |
| Comparison matrix and regenerated artifacts | orchestrator only | `reports/strategy_metric_comparison_matrix.json`, `data/backtests/*.json` | all worker branches unless explicitly assigned |

## Proposed Branch Queue

### 1. Contract Design And Golden Fixtures

- Branch: `codex/sre-contract-design`
- Worktree: `/home/ubuntu/worktrees/sre-contract-design`
- Purpose:
  - Define the canonical signal/risk/execution contracts before broad refactor.
  - Add golden fixture expectations for the current intraday strategy pipeline.
- Primary write scope:
  - `docs/signal-risk-execution-contract.md`
  - `runner/strategy_ir/models.py`
  - `runner/strategy_ir/normalizer.py`
  - `runner/strategy_ir/routing.py`
  - `tests/fixtures/sre_contract/`
  - `tests/test_strategy_ir_builder.py`
  - `tests/test_strategy_ir_normalizer.py`
  - `tests/test_strategy_ir_routing.py`
  - `tests/test_strategy_ir_codegen_vectorbt.py`
- Must not modify:
  - production execution logic in `runner/engine.py`
  - regenerated artifacts

### 2. Intraday Signal-Only Pilot

- Branch: `codex/intraday-signal-contract`
- Worktree: `/home/ubuntu/worktrees/intraday-signal-contract`
- Purpose:
  - Convert `intraday_volatility_breakout` into a pilot that cleanly emits
    signal intent, stop distance/level, take-profit level if any, and exit
    signal metadata.
  - Preserve existing platform artifact behavior through compatibility adapters.
- Primary write scope:
  - `strategies/intraday_volatility_breakout/`
  - `tests/strategies/test_intraday_volatility_breakout.py`
  - `tests/fixtures/intraday_signal_contract/`
  - compatibility adapters only after `codex/sre-contract-design` is merged or
    this branch is rebased onto it
- Depends on:
  - Contract Design And Golden Fixtures merged into `codex/new-main`, or this
    branch explicitly rebased onto that branch before editing adapters

### 3. Risk And Sizing Unification

- Branch: `codex/risk-sizing-contract`
- Worktree: `/home/ubuntu/worktrees/risk-sizing-contract`
- Purpose:
  - Make native backtest, signal replay, and live sizing explicitly route
    through a single risk decision contract where possible.
  - Document intentional live/backtest strictness differences.
- Primary write scope:
  - `risk/`
  - `runner/engines/signal_replay_adapter.py`
  - `tests/test_risk_engine.py`
  - `tests/test_risk_consistency.py`
  - `tests/test_live_risk_sizing.py`
  - `tests/test_signal_bots_risk.py`
- Must coordinate with:
  - Intraday Signal-Only Pilot for field naming only. Branch 3 is
    coordination-only for shared strategy output fields and must not edit
    `strategies/intraday_volatility_breakout/` or shared fixtures owned by
    Branch 2.

### 4. Execution Model Adapter

- Branch: `codex/execution-fill-model`
- Worktree: `/home/ubuntu/worktrees/execution-fill-model`
- Purpose:
  - Introduce explicit execution/fill settings for market timing, bid/ask side,
    stop trigger side, same-bar ordering, and lower-timeframe replay.
  - Keep existing defaults behavior-compatible.
- Primary write scope:
  - `runner/engine.py`
  - `runner/engines/native_adapter.py`
  - `tests/test_native_engine_execution.py`
  - `tests/test_trade_engine.py`
  - `tests/fixtures/execution_fill_model/`
- Depends on:
  - Contract Design And Golden Fixtures

### 5. Risk Parity Portfolio Target Contract

- Branch: `codex/risk-parity-target-contract`
- Worktree: `/home/ubuntu/worktrees/risk-parity-target-contract`
- Purpose:
  - Treat Risk Parity as target-weight/portfolio-target generation rather than
    a normal entry/exit signal strategy.
  - Separate target generation, rebalance execution, and attribution reporting.
- Primary write scope:
  - `scripts/run_risk_parity_backtest.py`
  - `runner/risk_parity_optimizer.py`
  - `strategies/risk_parity/`
  - `tests/strategies/test_risk_parity.py`
  - `tests/test_qc_risk_parity_artifact_loader.py`
- Constraint:
  - Do not change QC parity numbers unless supported by a real QC export or
    stronger reference extraction.
  - Must merge after `codex/sre-contract-design` and after any execution model
    changes that alter rebalance fill semantics. Rebase onto the latest
    `codex/new-main` before touching protected Risk Parity files.

### 6. Reference Artifact Contract

- Branch: `codex/reference-artifact-contract`
- Worktree: `/home/ubuntu/worktrees/reference-artifact-contract`
- Purpose:
  - Define richer MT5/QC reference import schemas for future external evidence:
    tick/M1 bid-ask windows, EA ATR/stop-distance exports, and QC rebalance
    fill exports.
- Primary write scope:
  - `docs/reference-artifact-contract.md`
  - `validation/importers/mt5_xlsx.py`
  - new dedicated importer modules under `validation/importers/`
  - `validation/statistics/cross_platform.py`
  - `tests/validation/test_mt5_xlsx_importer.py`
  - `tests/validation/test_cross_platform_metrics.py`
  - new importer-specific tests under `tests/validation/`
- Constraint:
  - Schema and importer scaffolding only unless real new external artifacts are
    provided.
  - Should merge immediately after `codex/sre-contract-design` and before
    runtime refactors that consume new reference metadata.

## Subagent Recovery

If the server restarts or CPU/RAM pressure kills active agents, all in-flight
subagent results are treated as lost unless their final message was already
captured in this thread. The orchestrator then continues locally from Git state,
restarts only the smallest necessary explorer, and never assumes unpublished
subagent work exists.

Low-resource default after a crash:

1. zero implementation workers until the plan is stable;
2. at most one lightweight explorer at a time;
3. no frontend build, full pytest, CodeRabbit, and subagent work concurrently;
4. create branches/worktrees before assigning workers, but do not run heavy
   validation in more than one worktree at once.

## CodeRabbit Queue

Initial priority:

1. `codex/sre-contract-design`
2. `codex/reference-artifact-contract`
3. `codex/risk-sizing-contract`
4. `codex/execution-fill-model`
5. `codex/intraday-signal-contract`
6. `codex/risk-parity-target-contract`

Review rules:

- Run CodeRabbit only after focused tests and `git diff --check` pass.
- Review one branch at a time.
- If rate-limited, queue the next run in tmux and do not commit unreviewed
  changes.
- If a branch changes more than ~150 files, split it before review.

## Merge Strategy

Recommended merge order:

1. Contract Design And Golden Fixtures
2. Reference Artifact Contract
3. Risk And Sizing Unification
4. Execution Model Adapter
5. Intraday Signal-Only Pilot
6. Risk Parity Portfolio Target Contract

After each merge to `codex/new-main`:

```bash
CHANGED_SCOPE_TESTS="${CHANGED_SCOPE_TESTS:-tests/validation/test_cross_platform_metrics.py}"
pytest -q $CHANGED_SCOPE_TESTS
pytest tests/validation/ tests/validators/ -q
python scripts/compare_strategy_metrics.py --output reports/strategy_metric_comparison_matrix.json
cd frontend && npm ci && npm run build
```

Changed-scope smoke tests must include the relevant files for the branch, for
example:

- Risk Parity: `tests/strategies/test_risk_parity.py`,
  `tests/test_qc_risk_parity_artifact_loader.py`.
- MT5 artifact/reference work: `tests/test_mt5_parity_artifact_loader.py`,
  `tests/test_analyze_mt5_parity.py`,
  `tests/validation/test_mt5_xlsx_importer.py`,
  `tests/validation/test_cross_platform_metrics.py`.
- Risk sizing: `tests/test_risk_engine.py`, `tests/test_risk_consistency.py`,
  `tests/test_live_risk_sizing.py`, `tests/test_signal_bots_risk.py`.
- Native execution: `tests/test_native_engine_execution.py`,
  `tests/test_trade_engine.py`.

Full `pytest tests/` should be run before a final PR or release branch, with
known ENV-dependent failures triaged separately. Any ENV-dependent exception
must be listed in an allowlist keyed by exact test id and failure signature,
with owner, expiry date, and triage evidence. Any new or changed failure blocks
the final PR/release gate.

## Open Questions For Explorer Agents

The initial explorer pass should answer:

1. Where exactly are signal, risk, and execution currently coupled?
2. Which tests form the minimum safe golden fixture set?
3. Which parity improvements are still possible locally without new MT5/QC
   artifacts?
4. Which branches above should be split or reordered before implementation?
