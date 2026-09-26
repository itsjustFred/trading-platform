# Pre-Mortem Report — PR #102 (issues batch 76–87)

**Scope:** PR-changed production code (`live_runner/ctrader_adapter.py`,
`live_runner/daemon.py`, `runner/fees.py` + engine fee plumbing,
`runner/portfolio_optimizer.py`, `validation/readiness/validator.py`,
`data_tools/auto_download.py`).
**Date:** 2026-09-22

## Summary

Five scenarios: three were verified as real weaknesses during the read and
fixed immediately in the PR (crypto misclassification, stale session state,
sibling starvation); two remain fictional but plausible and are recorded with
hardening suggestions. Dominant theme: **stringly-typed cross-file contracts**
(`exit_only`, `fees=zero`, OOS status strings) and **session-state lifecycle**
in the long-lived adapters.

## Real findings (fixed in the PR, not fictional)

- **R1 — crypto pairs classified as FX.** `_fallback_scale_class` mapped every
  6-letter alpha symbol to FX; `BTCUSD`/`ETHUSD` would have traded at up to
  100000x size (FX `volume_per_qty=1000` vs ~0.01). Fixed with a crypto-base
  blocklist returning unknown (fail closed) + regression test.
- **R2 — disconnect kept stale scales.** `disconnect()` cleared
  `_symbol_name_to_id` but not `_symbol_props`/`_order_id_to_symbol`: stale
  broker scales across reconnects + unbounded order-map growth. Fixed + test.
- **R3 — one bad Discord service starved siblings.** The new fail-closed
  `ValueError` in `_ensure_discord_bot_source` propagated out of the per-tick
  service loop, skipping all remaining services until the next interval.
  Fixed with per-service try/except + continue.

## Post-Mortems

### 1. The `"exit_only"` string drift

**Severity:** Critical
**Component:** `live_runner/daemon.py` (`_metadata_for_exit_only_order`,
`submit_order_with_risk`), `live_runner/ctrader_adapter.py`
(`_resolve_order_volume`)
**Fragility type:** Stringly-typed contracts

#### What happened

Exit-only flatten orders started raising `Explicit metadata volume ... only
allowed for exit-only flatten orders` on every close. Open shorts could not be
flattened; the desk noticed positions surviving their exit signals.

#### The change that caused it

A contributor renamed the daemon's marker to `"exit-only"` (kebab-case, to
match a new config style) and updated the daemon side plus its tests — but not
the adapter's comparison in `_resolve_order_volume`, which still checked for
`"exit_only"`. Code review passed: both sides had green tests, each pinning its
own spelling.

#### Why it broke

The contract between producer (daemon metadata) and consumer (adapter gate) is
a bare string compared in two files with no shared constant, enum, or type.
`grep exit_only` finds the daemon side; nothing points at the adapter check.

#### How it was caught

Only in production behavior (failed closes); unit tests on each side passed
because each side pinned only its own spelling.

#### Hardening suggestions

1. Introduce a shared constant (e.g. `EXIT_ONLY_RISK_SIZING_MODE = "exit_only"`
   in `live_runner` or `shared`) and use it on both sides.
2. Add one cross-boundary test that builds daemon exit-only metadata and feeds
   it through the real adapter `_resolve_order_volume`.

### 2. The negative correlation bonus becomes a penalty

**Severity:** Medium
**Component:** `runner/portfolio_optimizer.py`
(`_weighted_correlation_penalty`, `_build_objective`)
**Fragility type:** Coincidental correctness / invisible invariants

#### What happened

After a "cleanup" the optimizer started preferring concentrated correlated
bets; OOS performance of selected portfolios decayed over a quarter before
anyone bisected it.

#### The change that caused it

A contributor noticed `_weighted_correlation_penalty` can return negative
values (negatively correlated pairs) and "fixed" it with `abs()`/`max(0, …)`,
reasoning a penalty should never be negative. The sign carries the
diversification signal: negative correlation *should* increase the score.

#### Why it broke

Nothing documents that the penalty is signed by design; the name says
"penalty" and the objective subtracts it, so a non-negative reading is the
natural (wrong) interpretation.

#### How it was caught

`test_objective_penalizes_positive_correlation` still passed (it only checks
the positive side). A dedicated test asserting diversified-beats-concentrated
on anti-correlated fixtures would have caught it — `TestIssue87Guards`
currently only asserts concentrated==0 vs diversified>0 on positive
correlation.

#### Hardening suggestions

1. Rename to `_weighted_correlation_adjustment` with a docstring stating the
   sign convention, or split into explicit reward/penalty terms.
2. Add a test: anti-correlated pair scores above the correlated pair at equal
   Sharpe.

### 3. `fees="zero"` silently becomes a strategy parameter

**Severity:** Medium
**Component:** `runner/cli.py` (`_fee_params_from_args`), engine adapters,
strategy `STRATEGY_META` schemas
**Fragility type:** Semantic coupling through shared mutable state
(`parameters` dict)

#### What happened

A strategy added strict `STRATEGY_META` parameter validation (reject unknown
keys). Every CLI backtest with `--fees-zero` started failing validation
because `fees` now rides inside the strategy `parameters` dict.

#### The change that caused it

Merging fee declaration into strategy parameters was expedient (it flows to
engines for free), but it conflates two namespaces: strategy hyperparameters
vs run-cost declaration. A strict schema cannot tell them apart.

#### Why it broke

The `parameters` dict is shared mutable state between the strategy layer and
the cost layer with no namespacing (`fee_*` prefix convention is undocumented
and unenforced).

#### How it was caught

Loudly (validation errors) — fail-closed, but noisy across all CLI runs.

#### Hardening suggestions

1. Namespace fee keys explicitly and document the reservation (`fee_*`
   belongs to the cost layer; strategies must ignore/forward it).
2. Long-term: carry the fee model out-of-band (`BacktestConfig.fee_model`)
   instead of inside strategy params.

### 4. OOS status string consumers drift

**Severity:** Low
**Component:** `runner/portfolio_optimizer.py` (`oos_status`),
`frontend/src/lib/optimizationEvidence.ts`, `runner/walkforward.py`
**Fragility type:** Stringly-typed contracts

#### What happened

A new `oos_status` value (`partial_overlap`) was added backend-side for a
refined overlap state; the frontend evidence classifier treated it as generic
"available" green because it only branches on known states, and walkforward
aggregation silently dropped the new state into `None`.

#### The change that caused it

Extending a string union in one layer without updating all consumers — the
TypeScript `OptimizationOosEvidenceState` and the Python producers are not
linked by any shared schema or contract test.

#### Why it broke

Three spellings of the same lifecycle (`available`/`insufficient_overlap`/
`no_oos_requested`) exist as bare strings on both sides of the stack with no
exhaustive-match enforcement.

#### How it was caught

Visual (green badge on partial data) — no test failed.

#### Hardening suggestions

1. Frontend: make the classifier exhaustive (unknown states → `unavailable`,
   never green by default).
2. Add a contract test listing the backend's status vocabulary against the
   frontend's handled states.

## Themes and Recommendations

- **String contracts across boundaries** (PM 1, PM 4, and the `fees`/`exit_only`
  keys in PM 3) are the systemic pattern: producer and consumer each test their
  own spelling. Prefer shared constants + at least one cross-boundary test per
  contract.
- **Session state in adapters** (R2) deserves a uniform rule: `disconnect()`
  clears everything derived from the session; add an assertion-style test per
  new session cache.
- **Signed-by-design numerics** (PM 2) need names and docstrings that state the
  convention, plus two-sided tests (reward side AND penalty side).
