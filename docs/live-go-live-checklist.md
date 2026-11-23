# Go-Live Checklist: lifting the demo-only block

**Status: draft.** This is the written bar for the day real money is allowed
past `_adapter_is_demo` (`live_runner/daemon.py`). It answers issue #306;
the maintainer decision of 2026-11-23 settled the open question: **there is
no hard per-account capital limit in code**. "Start small" is an operator
step recorded in the evidence below, not a gate the code enforces.

Lifting the block is itself a deliberate code change (the check is a hard
`is True` test on the adapter's demo/paper flag, fail-closed by design),
reviewed like any other money-path change — never a config flip.

## 1. Safety features in place

All of the following are merged. Each must be verified working in the paper
environment before go-live, not just present in the tree.

- [ ] **Kill switch** — persisted halt on every trading path (#300; PR
  #316; runbook "Kill Switch"). Verify set/lift/CLI and that a halt
  survives a daemon restart.
- [ ] **Loss circuit breaker** — persisted per-scope drawdown latches for
  entries (#301; PR #351; runbook "Loss Circuit Breaker"). Verify latch,
  alert and the documented reset semantics.
- [ ] **Pre-submit hard caps** — independent caps on quantity, notional
  and price deviation judged before any order reaches the broker (#302;
  PR #329), with the leverage ceiling judged on free margin (#359; PR
  #369). Verify a deliberate breach is refused or clamped per cap before
  submission, not after.
- [ ] **Broker reconciliation** — periodic comparison of the platform's
  view against the broker's working book (#303; PR #330; runbook
  "Periodic Broker Reconciliation"). Verify mismatches alert **and** the
  halt-and-release behaviour: the default of 2 consecutive mismatches
  halts the account's deployments' entries, and a clean pass releases
  them. Confirm the documented open-decision items are resolved or
  accepted.
- [ ] **Ops alerting** — ERROR-and-above records page a webhook
  (`OPS_ALERT_*`, #304; PR #331; runbook "Ops Alert Channel"). Verify an
  induced error actually arrives.
- [ ] **Backup / restore / rollback** — SQLite live-DB backup, guarded
  restore and the release/rollback procedure (#305; PR #333; runbook
  "Release / rollback"). Verify a restore into a scratch DB once before
  go-live.
- [ ] **Promotion gate** — only promoted `production` code may touch a
  real-money account; paper runs `experimental` (#165; runbook "Strategy
  Promotion Gate"). Its evidence table already requires ≥ 60 days and
  ≥ 30 trades of paper plus ≤ 2 % parity deviation — at the default
  thresholds, and no `promotion_thresholds.json` may lower them for this
  checklist — section 2 below is the operational evidence on top of
  that.

## 2. Evidence period

- [ ] **≥ 60 days of paper trading** on the deployment shape intended for
  live — same strategy file hash, same bound parameters, same account
  type (paper), same signal source. The promotion gate enforces the
  duration/trade-count floor; this checklist additionally requires the
  daily artifacts:
  - a **daily PnL statement** per account per day (broker-reported,
    stored with the run's evidence), and
  - a **live-vs-backtest drift review**: compare the paper run's fills,
    slippage and PnL against the backtest/promotion evidence on the same
    window; the proposed review bar is the promotion gate's ≤ 2 %
    relative deviation on compared metrics (a proposal pending maintainer
    sign-off, not a recorded decision). A drift above the bar is a stop,
    not a talking point.
- [ ] No unresolved `CRITICAL`/high-severity audit findings on the locked
  dependency set attributable to the deployed extras (once #389 lands:
  see `docs/dependency-security.md` and the weekly `security-audit`
  workflow; until then, check the locked dependency set manually).

## 3. Staged capital ramp (operator steps, not code)

There is deliberately **no capital cap enforced in code**. The pre-submit
caps bound per-order risk; account-level exposure is an operator
responsibility, staged as follows:

- [ ] **One strategy, one account, minimal size.** The first real-money
  deployment is a single promoted strategy on a single account, started
  at a notional the operator chooses and records in the promotion
  evidence — small enough that a total-loss day is an inconvenience, not
  an event. "Minimal" is deliberately not a number in this document: it
  is the operator's call at signing time.
- [ ] **Soak before scale.** After the first real-money week, review the
  daily PnL statements and reconciliation output; scale size or add a
  second deployment only by an explicit operator decision recorded in
  the runbook, never by editing a threshold silently.
- [ ] **Kill-switch rehearsal on real money** — prove the halt path once
  deliberately (small size, market hours) before the capital is meaningful.

## 4. Go-live sign-off

- [ ] Every box in sections 1–3 is checked and the evidence files exist.
- [ ] A maintainer records the go-live decision (date, strategy, account,
  starting size) in this document's revision history or the issue thread.
- [ ] The demo-block lift ships as its own reviewed PR; this checklist is
  its acceptance criteria.

## Out of scope

- Per-account capital limits in code — explicitly rejected by the
  maintainer decision (2026-11-23).
- Automating the daily PnL statement or drift report — today they are
  operator-produced artifacts; automating them is a separate issue.
- Scope per the #184 maintainer decision (2026-11-23): Alpaca crypto is
  not traded live, and the Discord-token signal path is out of scope —
  real-money go-live covers cTrader plus Alpaca equities only.
