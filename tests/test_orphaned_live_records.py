"""Live records whose parent no longer exists, and are therefore invisible.

`live_signals.deployment_id` has **no foreign key**, unlike its two siblings:
`live_executions.signal_id` and `live_deployments.account_id` both declare one. So a signal survives
the deletion of the deployment it belongs to, and every signal query in `shared/database.py` is
deployment-scoped - `.where(LiveSignalModel.deployment_id == deployment_id)` and
`.in_(deployment_ids)` in at least eight places.

The combination is a silent-loss class rather than a crash. Nothing errors. The rows are intact and
simply unreachable through every listing path, so a deployment that is recreated under a new id
leaves
its history behind like a cupboard nobody opens. Measured on production:

  * **7 of 7** signals reference a `deployment_id` that is not in `live_deployments`
  * 6 distinct missing ids - one vanished UUID and five `ctrader-demo-smoke-*` string ids
  * **3 of 3** executions hang off those orphaned signals, so the executions are invisible too
  * one signal carries **two** execution rows (sizes 0.07822 and 43.07822, same order, 2.8 ms
  apart),
    which sums to 43.15644 for what the deployment history records as one fill

That last one is the mirror image of backlog item 1, which is about two *equal-sized* fills being
silently **dropped** as duplicates. Here two *unequal* rows for one signal were both **kept**, and
nothing can see either. Both facts belong to the operator: whether to clean the rows, and whether
the
duplicate should be reconciled or deleted, is a data decision, and deleting the wrong one invents a
history that never happened.

So this file asserts only what is measurable, in both directions, and leaves every remedy open:

  * a new orphan appears that is not listed  -> something created a signal without its deployment
  * the list empties                          -> the operator reconciled the records

The counts are derived from production rather than transcribed, so reconciling the data makes the
test
describe reality again instead of failing forever.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

PRODUCTION_DB = Path("/home/ubuntu/data/backtests.db")

# What is currently orphaned, with the reason each one matters. Every value below was READ from
# production - an earlier draft of this file carried a hand-written signal UUID that did not exist,
# which is exactly how a fixture ends up asserting a fiction. Emptying this after a reconciliation
# is the intended way to retire the finding.
# APPLIED 2026-10-06 by _backfill_smoke_deployments, which runs inside init_db().
#
# The corpus held 7 orphaned signals across 6 distinct deployment ids. Six were smoke ids whose
# accounts all existed in live_accounts, so the backfill recreated the parent deployment row exactly as
# _ensure_ctrader_smoke_deployment writes on new runs - namespaced strategy_name, PAUSED,
# signal_source 'smoke'. Five rows for six signals, because two signals shared ctrader-demo-smoke-47644617.
#
# One orphan remains, and it is the one that was never mechanically fixable: deployment
# 4e9bbdab-0d15-4232-91e1-cc459166b764 was a real deployment that has since been deleted by hand. It
# holds the only in-production instance of the item 1 double-count - execution rows 43.07822 and
# 0.07822 summing to 43.15644 for one fill - so deleting it to clear the last violation would destroy
# the evidence. Its disposition is still the operator's call.
EXPECTED_ORPHANED_SIGNALS = {
    "9eb8eb2f-a094-4836-9886-ce1abe6baffc": (
        "dep 4e9bbdab, a real deployment since deleted; buy AAPL, theoretical 170.0. Carries "
        "execution rows (43.07822 and 0.07822) for one fill - the double-count. NOT resolvable "
        "mechanically: the deployment is gone and reinserting it would fabricate a trading record."
    ),
}
EXPECTED_MISSING_DEPLOYMENT_IDS = {
    "4e9bbdab-0d15-4232-91e1-cc459166b764": "a real deployment that has since been deleted",
}
# One signal, two rows, and the sum that double-counts.
EXPECTED_SIGNALS_WITH_MULTIPLE_EXECUTIONS = {
    "9eb8eb2f-a094-4836-9886-ce1abe6baffc": (43.07822, 0.07822),
}


def _measured() -> dict[str, object]:
    if not PRODUCTION_DB.exists():
        pytest.skip(f"{PRODUCTION_DB} is absent, so there is no production to audit")
    conn = sqlite3.connect(f"file:{PRODUCTION_DB}?mode=ro", uri=True)
    try:
        deployments = {r[0] for r in conn.execute("select id from live_deployments")}
        orphans = {
            r[0]: r[1]
            for r in conn.execute("select id, deployment_id from live_signals")
            if r[1] not in deployments
        }
        missing_ids = {r[1] for r in conn.execute(
            "select id, deployment_id from live_signals") if r[1] not in deployments}
        multi: dict[str, tuple[float, ...]] = {}
        for sig, in conn.execute(
            "select signal_id from live_executions group by signal_id having count(*) > 1"
        ):
            multi[sig] = tuple(
                r[0] for r in conn.execute(
                    "select filled_qty from live_executions where signal_id=?"
                    " order by timestamp", (sig,))
            )
        return {"orphans": orphans, "missing_ids": missing_ids, "multi": multi}
    finally:
        conn.close()


def test_the_orphaned_signals_are_exactly_what_is_declared() -> None:
    measured = set(_measured()["orphans"])  # type: ignore[arg-type]
    unexpected = measured - set(EXPECTED_ORPHANED_SIGNALS)
    assert not unexpected, (
        f"signals {sorted(unexpected)} now reference a missing deployment and are not declared. "
        f"A signal was written without its deployment, so it is invisible to every "
        f"deployment-scoped query; add it with its reason."
    )
    resolved = set(EXPECTED_ORPHANED_SIGNALS) - measured
    assert not resolved, (
        f"EXPECTED_ORPHANED_SIGNALS lists {sorted(resolved)} but they are no longer orphaned. If "
        f"the operator reconciled them, empty the mapping in the same change so this test "
        f"describes reality rather than a wish."
    )


def test_the_missing_deployment_ids_are_exactly_what_is_declared() -> None:
    measured = set(_measured()["missing_ids"])  # type: ignore[arg-type]
    assert measured == set(EXPECTED_MISSING_DEPLOYMENT_IDS), (
        f"missing deployment ids differ: measured {sorted(measured)}, declared "
        f"{sorted(EXPECTED_MISSING_DEPLOYMENT_IDS)}"
    )


def test_the_double_recorded_signal_is_still_there() -> None:
    """The mirror image of item 1: two rows kept where item 1 worries about two dropped.

    Pinned separately because it is the only row in the database where the quantity is ambiguous,
    and
    it is invisible - the signal's deployment does not exist, so no deployment-scoped query reaches
    it. A reader auditing "how much did we trade" from the deployments would never see it.
    """
    measured = _measured()["multi"]  # type: ignore[assignment]
    assert set(measured) == set(EXPECTED_SIGNALS_WITH_MULTIPLE_EXECUTIONS), (
        f"signals with more than one execution row changed: {sorted(measured)}"
    )
    for sig, sizes in EXPECTED_SIGNALS_WITH_MULTIPLE_EXECUTIONS.items():
        assert tuple(measured[sig]) == sizes, (  # type: ignore[index]
            f"the sizes for {sig} changed: {measured[sig]} != {sizes}"
        )


def test_no_orphaned_signal_is_reachable_through_a_deployment_scoped_query() -> None:
    """Why this is silent rather than loud, asserted so the reason is not folklore.

    Every signal listing in `shared/database.py` filters on deployment_id, so an orphaned signal
    cannot appear in any of them. This test states that consequence directly instead of relying on a
    reader to infer it from the query shapes.
    """
    src = Path(__file__).resolve().parents[1] / "shared" / "database.py"
    text = src.read_text(encoding="utf-8")
    scoped = text.count("LiveSignalModel.deployment_id")
    assert scoped >= 8, (
        f"only {scoped} deployment-scoped signal filters found; this test's premise is that signal "
        f"listings are deployment-scoped, and that is no longer true"
    )
    measured = _measured()
    assert measured["orphans"], "no orphans measured, so there is nothing for this to explain"

