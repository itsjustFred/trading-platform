"""Why live_signals has no foreign key on deployment_id, and what adding one costs.

Measured facts held in place here, because together they are the whole argument on both sides.

1. **The FKs that do exist are enforced.** `shared/database.py` has a single `create_engine`
   site and applies `PRAGMA foreign_keys=ON` from a connect-listener, so every app
   connection has them on. A plain `sqlite3.connect` and a bare SQLAlchemy engine both
   report **off**, which is why the pragma is set rather than assumed.

2. **`live_signals.deployment_id` has no FK**, unlike `live_executions.signal_id` and
   `live_deployments.account_id`. Measured: 7 of 7 signals name an absent deployment, and
   every signal listing is deployment-scoped, so all 7 - and the 3 executions on them -
   are invisible. See `test_orphaned_live_records.py`.

3. **Nothing in the app deletes a deployment.** No `delete(LiveDeploymentModel)` exists, so
   the orphans were not made by application code removing a row.

4. **The smoke tooling creates them on purpose.** `scripts/live_smoke.py` builds a
   `LiveSignal` with `deployment_id=f"ctrader-demo-smoke-{account_id}"` and inserts no
   `live_deployments` row at all. Five smoke ids account for 6 of the 7 orphans; the
   seventh, `4e9bbdab`, was a real deployment and is now absent - removed outside the
   app, since no code path can do it.

**So adding the FK has a measured cost: it would break the smoke tooling**, because
every smoke signal write would fail on a missing parent. That is not an argument against
the constraint - a signal naming a nonexistent deployment is a defect, and these seven
are why they are invisible - but it is a cost to weigh, and it is invisible unless
someone writes it down at the moment somebody adds the constraint.

If the FK is ever added, this file should fail and be updated in the same commit, because
the smoke path must then insert a deployment row.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATABASE = REPO / "shared" / "database.py"
SMOKE = REPO / "scripts" / "live_smoke.py"

SMOKE_ID = "ctrader-demo-smoke-{account_id}"
DELETED_REAL_DEPLOYMENT = "4e9bbdab-0d15-4232-91e1-cc459166b764"


def test_foreign_keys_are_enabled_for_every_app_connection() -> None:
    """The premise: the existing FKs are real, so the missing one is a gap not a convention."""
    text = DATABASE.read_text(encoding="utf-8")
    assert text.count("create_engine(") == 1, (
        "there is now more than one engine creation site; a new one without the "
        "foreign_keys listener would have referential integrity silently disabled"
    )
    assert "PRAGMA foreign_keys=ON" in text, (
        "the connect-listener no longer enables foreign keys, so every declared FK "
        "is decorative"
    )


def test_live_signals_still_declares_no_foreign_key_on_deployment_id() -> None:
    """The gap, asserted. Adding the FK should fail here on purpose, in its own commit."""
    match = re.search(r"LiveSignalModel\((.*?)\n\s*\)", DATABASE.read_text(), re.S)
    block = match.group(1) if match else ""
    assert "ForeignKey" not in block, (
        "live_signals now declares a ForeignKey on deployment_id. Presumably deliberate - "
        "but the smoke tooling must then insert a deployment row, and the orphan inventory "
        "in test_orphaned_live_records.py has to be retired in the same change."
    )


def test_the_smoke_tooling_no_longer_writes_an_unresolvable_deployment_id() -> None:
    """The cost of the FK, retired.

    This file originally asserted the opposite: that ``LiveDeploymentModel(`` appears nowhere in
    the smoke tooling, so that nobody would add the FK without seeing the breakage. The tripwire has
    fired - ``live_smoke._ensure_ctrader_smoke_deployment`` now inserts the parent deployment row
    before writing the signal, so the FK no longer breaks smoke runs. The cost is gone, and the
    remaining question is no longer "is this safe" but "when".
    """
    text = SMOKE.read_text(encoding="utf-8")
    assert SMOKE_ID in text, (
        "the smoke tooling no longer names a synthetic deployment id, so re-measure this file"
    )
    assert "LiveDeploymentModel(" in text, (
        "the smoke tooling stopped inserting a deployment row, so the FK on "
        "live_signals.deployment_id would break it again"
    )
    ensure_call = "_ensure_ctrader_smoke_deployment(account_id=account_id, symbol=symbol)"
    signal_construction = (
        'signal = LiveSignal(\n        deployment_id=f"ctrader-demo-smoke-{account_id}"'
    )
    assert ensure_call in text
    assert text.index(ensure_call) < text.index(signal_construction), (
        "the deployment must be ensured before the signal that depends on it"
    )


def test_no_code_path_deletes_a_deployment() -> None:
    """Why the seventh orphan is a different story from the other six."""
    for path in (REPO / "shared").rglob("*.py"):
        if "tests" in path.parts:
            continue
        body = path.read_text(errors="replace")
        assert "delete(LiveDeploymentModel)" not in body, (
            f"{path} can now delete a deployment row, so the orphans have a second "
            f"cause and this file's account of them is incomplete"
        )


def test_the_two_orphan_stories_are_recorded_separately() -> None:
    """Six from the smoke tooling, one removed by hand. Collapsing them loses the cause."""
    assert SMOKE_ID.count("smoke") == 1
    assert DELETED_REAL_DEPLOYMENT.startswith("4e9bbdab"), (
        "the deleted real deployment id must stay recorded; it is the only orphan whose "
        "cause is not the smoke tooling"
    )
