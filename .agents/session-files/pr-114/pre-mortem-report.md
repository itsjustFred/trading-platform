# Pre-Mortem — PR #114 (`fix/perma-viewer`)

**Scope:** the PR's changed code against `stack/p5-p4`. Live-control endpoints
(`viewer/main.py`), the delete gate and its refusal path, the DB helpers behind
them (`shared/database.py`), the backtest info payload (`viewer/core.py`) and the
live hub UI (`frontend/src/pages/LiveHub.tsx`).
**Date:** 2026-11-04
**Question:** it is three weeks after merge and something here hurt production.
What was it?

## Failure stories, most likely first

### F1 — A refused delete disarms a running deployment and the UI keeps saying ACTIVE (low likelihood, medium impact)

`viewer/main.py:13082` writes PAUSED before draining, and `viewer/main.py:13114`
restores the previous status if the delete is then refused for having history.
That restore is a second write, and it can fail: the handler logs an error and
continues with `status_restored: null` (`viewer/main.py:13141`). The 409 message
the operator sees still says "nothing was deleted", which is true of the row but
not of the deployment's state — it is now PAUSED. `frontend/src/pages/LiveHub.tsx:791`
does not refresh on error, so the card keeps showing ACTIVE until the next poll.

Story: an operator deletes a deployment that has signal history, the restore write
fails, the deployment silently stops trading, and the dashboard disagrees with the
database for as long as the operator looks at it.

**Status:** not fixed in this pass. The safe direction (stopped, not armed) is why
this is "medium" and not "blocker". Recommended hardening: refresh the deployment
list in the catch block, or surface `status_restored === null` in the toast.

### F2 — An order lands at the broker after its deployment row is gone (low likelihood, high impact)

Deleting an ACTIVE deployment drains first, but a drain can end IN_FLIGHT or
INDETERMINATE and the delete proceeds anyway. The row and its claim rows are then
removed; the audit keeps counts only (`viewer/main.py:13144`), not the lease or
signal ids. The response carries a warning and the UI toasts it
(`LiveHub.tsx:572`), so the operator is told. The gap is the evidence: after the
delete there is nothing in the application that says *which* submission was in
flight, so reconciling the broker fill with a deployment is manual archaeology.

**Status:** the delete handler now reads the drain verdict and toasts a non-clean
one (dc5dcd4); it used to discard the response. The evidence gap remains: the
audit keeps counts only. Recommended follow-up: put the in-flight claim ids (or
signal ids) into the audit detail before the delete.

### F3 — Re-pointing an ACTIVE deployment leaves the listener on the old account (low likelihood, medium impact)

`LiveDeploymentAccountUpdateRequest.allow_active` (`shared/models.py:257`) lets an
API caller re-point a deployment that is still ACTIVE; the DB refuses only while a
submission is in flight (`shared/database.py:3349`). An idle-but-ACTIVE deployment
can therefore move accounts while the running listener still holds the old one
until the next sync. The frontend never sets the flag, so this is an API-only path.

**Status:** resolved after the review round. `allow_active` is gone: an ACTIVE
deployment is refused outright (the listener holds the old account, so pausing is
a precondition), and the target account is re-verified against the broker
immediately before the write, like arming. Tests:
`test_a_repoint_of_an_active_deployment_is_refused`,
`test_a_repoint_is_refused_when_the_broker_cannot_confirm_the_target`.

### F4 — A live control is issued twice because the first call is slow (low impact)

The delete drain can hold the request for the drain budget
(`LIVE_SUBMISSION_DRAIN_BUDGET_SECONDS`, twice the claim TTL). The UI disables the
card via `setDeploymentBusy` while the call is in flight, but a second tab or a
script has no such guard. The server side is idempotent enough — the second delete
finds the row gone and answers 404 — so the damage is a confusing error, not a
double action. The confirmation echo requirement also stops a replayed request
from naming a different row.

**Status:** acceptable. No change recommended.

## What already holds (checked, not assumed)

- An unconfirmed control is refused at the server (428), audited with a reason, and
  the audit entry is asserted by a test — mutation M1/M2 in the mutation report.
- A refused delete of a STOPPED deployment keeps `stopped_enforced_at`; the gate
  runs only for ACTIVE and only a gate write is undone — mutation M3.
- Non-finite and producer-undefined stats are published as absent, never as 0.0 or
  `Infinity` — mutation M4 and the new provenance test.
- The frontend renders a missing metric as the unavailable placeholder rather than
  `NaN`, and a structured 409 body is rendered through `describeErrorDetail`
  (`frontend/src/api/client.ts:152`) instead of `[object Object]`.

## Not covered by this pre-mortem

- The frontend mutation surface: there is no JS mutation runner configured, so
  `frontend/src/**` was read, not mutated.
- Concurrency between two operator sessions acting on the same deployment; the
  claim/lease machinery is the existing P3 contract, not something this PR changes.
