# Mutation Testing Report — PR #114 (`fix/perma-viewer`)

**Scope:** the PR's changed production code against its base branch `stack/p5-p4`:
`viewer/main.py` (live-control confirmation, delete gate and refusal restore),
`viewer/core.py` (backtest info payload), `shared/database.py`
(`_stat_number_or_undefined`, `update_live_deployment_account`).
**Date:** 2026-11-04
**Method:** one mutation applied at a time, targeted tests run, mutation reverted,
`git status` confirmed clean before the next. This is a follow-up cycle: the PR
already had a mutation pass on the 428/409 split (see commit `4d9180c`), so the
scope here is the code that pass did not cover.

## Result table

| # | File | Mutation | Result | Test that caught it |
|---|------|----------|--------|---------------------|
| M1 | `viewer/main.py` | `status_code=428` → `409` on an absent confirmation | **Killed** | `test_an_omitted_confirmation_reports_428_precondition_required` |
| M2 | `viewer/main.py` | refusal audit reason `"no confirmation supplied"` → `"refused"` | **Killed** | same test (it asserts the audited reason) |
| M3 | `viewer/main.py` | delete gate `status_gated = original_status == "ACTIVE"` → `True` | **Killed** | delete tests (STOPPED stamp is cleared by the gate write) |
| M4 | `shared/database.py` | non-finite stat returned instead of `None` | **Killed** | `test_non_finite_stats_are_not_published_as_measurements` |
| M5 | `shared/database.py` | `BEGIN IMMEDIATE` removed from `update_live_deployment_account` | **Survived** | — |
| M6 | `viewer/core.py` | `data_source` forced to `""` in the backtest info payload | **Survived → Killed** | new `test_the_backtest_info_payload_publishes_the_artifact_provenance` |

**Mutation score: 5 / 6 killed (83%).** M6 was killed by a test added in this
pass; without it the score was 4 / 6.

## Survived mutation

**M5 — the re-point's write lock is untested.** `update_live_deployment_account`
takes `BEGIN IMMEDIATE` before counting in-flight claims, so a dispatcher cannot
insert a claim between the count and the commit. Removing the lock leaves all 8
account tests green.

This is a genuine coverage gap, and it is accepted rather than papered over: the
only test that would observe it has to hold a write lock on a second connection
and assert that the re-point blocks, which tests SQLite's locking rather than this
function. The guard is a one-line, documented invariant (see the comment above it
and the matching `save_live_execution`); a future refactor that drops it will not
be caught by the suite. Flagged here so the reviewer sees it, not hidden.

## Notes

- M1 and M2 both die on the same test on purpose: the 428 branch and its audit
  entry are one contract ("you did not confirm this" is reported and recorded),
  and a test that only checked the status code would let the audit rot.
- The frontend changes in this PR (`frontend/src/**`, `frontend/tests/**`) are
  covered by vitest, not by this pass; there is no JS mutation runner configured
  in the repo, so mutating them would mean introducing one. Out of scope here and
  called out rather than silently skipped.
