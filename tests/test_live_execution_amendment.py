"""A repeated dedupe key is either a redelivery or an amendment. Only one of them may be dropped.

`save_live_execution_up_to_cumulative` branches on "is there already a row with this dedupe key".
That single check conflated two situations which mean opposite things:

    redelivery   the broker sent the same deal again, unchanged. Dropping it is right - this is
                 the duplicate `ctrader:deal:<dealId>` exists to catch, and dropping it is what
                 stops the 43.07822 + 0.07822 double-count.
    amendment    the broker sent the same dealId with a LARGER quantity. `ProtoOADeal` carries
                 `filledVolume` alongside `volume`, so a deal can grow. Dropping that freezes the
                 stored quantity at its first-seen value and the remainder of the fill is never
                 recorded.

cTrader exposes no fill-level identifier - a deal entry has `dealId`, `orderId`, `positionId`
and `symbolId` and nothing finer, and there is no `fillId` anywhere in the installed protos. So
an order split into several fills arrives as several deals with distinct ids and each is stored
correctly. That case needs no code. The amendment case is the one that was being dropped, and it
is what this file pins.

The distinction is one-directional: only an increase amends. A smaller or equal quantity is
treated as redelivery, because shrinking a fill that the broker has already reported would be
worse than ignoring it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, create_engine, text

from shared.database import save_live_execution_up_to_cumulative
from shared.models import LiveExecution

ACCOUNT = "acct-amendment"


@pytest.fixture
def db(monkeypatch):
    from sqlmodel import SQLModel

    from shared.database import LiveAccountModel, LiveSignalModel

    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    signal_id = str(uuid.uuid4())
    with Session(engine) as session:
        session.add(LiveAccountModel(id=ACCOUNT, broker="ctrader", name="probe", ctid="1"))
        session.add(
            LiveSignalModel(
                id=signal_id,
                deployment_id="ctrader-demo-smoke-probe",
                symbol="XAUUSD",
                action="buy",
                theoretical_price=261.7,
                timestamp=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
                metadata_json="{}",
            )
        )
        session.commit()

    monkeypatch.setattr("shared.database.get_session", lambda: Session(engine))
    monkeypatch.setattr("shared.database.init_db", lambda: None)
    return engine, signal_id


def _execution(signal_id: str, qty: float) -> LiveExecution:
    return LiveExecution(
        signal_id=uuid.UUID(signal_id),
        actual_price=261.7,
        filled_qty=qty,
        commission=0.0,
        timestamp=datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
        broker_order_id="450566f0-6dd5-401e-8544-47da2895aa55",
        dedupe_key="ctrader:deal:26533115",
    )


def _rows(engine) -> list[tuple[str, float]]:
    with Session(engine) as session:
        return [
            (row[0], float(row[1]))
            for row in session.exec(
                text("select dedupe_key, filled_qty from live_executions")
            ).all()
        ]


def test_an_unchanged_redelivery_is_still_dropped(db):
    """The behaviour the dedupe key exists for must not have been weakened."""
    engine, signal_id = db
    first = save_live_execution_up_to_cumulative(_execution(signal_id, 1.0), 1.0)
    again = save_live_execution_up_to_cumulative(_execution(signal_id, 1.0), 1.0)

    assert first == pytest.approx(1.0)
    assert again == 0.0, "an unchanged redelivery must be dropped, not double-counted"
    assert _rows(engine) == [("ctrader:deal:26533115", pytest.approx(1.0))]


def test_a_larger_quantity_amends_instead_of_being_dropped(db):
    """The defect this fixes: a grown dealId lost the remainder of its fill."""
    engine, signal_id = db
    save_live_execution_up_to_cumulative(_execution(signal_id, 1.0), 3.0)

    # Same dealId, broker now says the deal is 3.0 filled.
    amended = save_live_execution_up_to_cumulative(_execution(signal_id, 3.0), 3.0)

    assert amended == pytest.approx(2.0), "the incremental slice should be reported as persisted"
    assert _rows(engine) == [("ctrader:deal:26533115", pytest.approx(3.0))], (
        "the amended quantity must accumulate on the one row rather than adding a second row or "
        "being silently capped at the first-seen value"
    )


def test_several_amendments_accumulate(db):
    engine, signal_id = db
    save_live_execution_up_to_cumulative(_execution(signal_id, 0.5), 0.5)
    save_live_execution_up_to_cumulative(_execution(signal_id, 1.0), 1.0)
    save_live_execution_up_to_cumulative(_execution(signal_id, 2.5), 2.5)

    assert _rows(engine) == [("ctrader:deal:26533115", pytest.approx(2.5))]


def test_a_smaller_quantity_does_not_shrink_a_reported_fill(db):
    """One-directional on purpose: shrinking a reported fill is worse than ignoring it."""
    engine, signal_id = db
    save_live_execution_up_to_cumulative(_execution(signal_id, 2.0), 2.0)

    returned = save_live_execution_up_to_cumulative(_execution(signal_id, 0.5), 2.0)

    assert returned == 0.0
    assert _rows(engine) == [("ctrader:deal:26533115", pytest.approx(2.0))]


def test_an_amendment_is_capped_by_the_brokers_cumulative(db):
    """Growing a deal must not push the signal past the quantity the broker reports as filled.

    The insert path already caps at ``cumulative - already persisted``. If the amendment path did
    not, a deal whose first insert was capped - because the broker's cumulative was lower than the
    deal's own ``filledVolume`` - would be "amended" on the very next redelivery back up to the
    number the cap rejected, which is the same over-count the dedupe key exists to prevent.
    """
    engine, signal_id = db
    inserted = save_live_execution_up_to_cumulative(_execution(signal_id, 2.0), 1.0)
    assert inserted == pytest.approx(1.0), "the insert path should have capped at the cumulative"

    # Same deal, same claimed quantity, same broker cumulative: a redelivery, not new information.
    again = save_live_execution_up_to_cumulative(_execution(signal_id, 2.0), 1.0)

    assert again == 0.0, "the redelivery was booked as an amendment and inflated the total"
    assert _rows(engine) == [("ctrader:deal:26533115", pytest.approx(1.0))]


def test_an_amendment_cannot_consume_quantity_another_deal_already_booked(db):
    """The cap is about the whole signal, not just this row.

    Two deals on one signal: A books 1.0, B books 0.5, and the broker reports 1.5 filled. A then
    re-sends with 1.2 - a real increase, but there is no headroom left, so nothing may be added.
    """
    engine, signal_id = db
    save_live_execution_up_to_cumulative(_execution(signal_id, 1.0), 1.0)
    other = _execution(signal_id, 0.5)
    other.dedupe_key = "ctrader:deal:26533116"
    save_live_execution_up_to_cumulative(other, 1.5)

    amended = save_live_execution_up_to_cumulative(_execution(signal_id, 1.2), 1.5)

    assert amended == 0.0, "the amendment exceeded the broker's own cumulative"
    with Session(engine) as session:
        total = session.exec(text("select sum(filled_qty) from live_executions")).scalar()
    assert float(total) == pytest.approx(1.5), (
        "the signal now claims more filled than the broker does"
    )


def test_an_amendment_within_the_headroom_is_booked(db):
    """The cap must not swallow a legitimate increase.

    The broker's cumulative grew to 1.7 and deal A grew by 0.2, which fits exactly in the 0.2 of
    headroom left after deal B. The guard is a ceiling, not a freeze.
    """
    engine, signal_id = db
    save_live_execution_up_to_cumulative(_execution(signal_id, 1.0), 1.0)
    other = _execution(signal_id, 0.5)
    other.dedupe_key = "ctrader:deal:26533116"
    save_live_execution_up_to_cumulative(other, 1.5)

    amended = save_live_execution_up_to_cumulative(_execution(signal_id, 1.2), 1.7)

    assert amended == pytest.approx(0.2)
    with Session(engine) as session:
        total = session.exec(text("select sum(filled_qty) from live_executions")).scalar()
    assert float(total) == pytest.approx(1.7)


def test_an_amendment_supplies_a_price_the_first_row_lacked(db):
    """The first report of a fill can carry no price; the amendment is the second chance.

    ``actual_price`` is NOT NULL with a default of ``0.0``, so "no price yet" is stored as zero
    rather than NULL. Filling it in on the amendment is the only way that row ever gains a price,
    and dropping it leaves the row permanently priced at zero.
    """
    engine, signal_id = db
    unpriced = _execution(signal_id, 1.0)
    unpriced.actual_price = 0.0
    save_live_execution_up_to_cumulative(unpriced, 1.0)

    save_live_execution_up_to_cumulative(_execution(signal_id, 2.0), 2.0)

    with Session(engine) as session:
        stored = session.exec(text("select actual_price from live_executions")).scalar()
    assert float(stored) == pytest.approx(261.7), (
        "the amendment left the execution priced at zero even though it carried a real price"
    )


def test_commission_is_added_only_for_the_incremental_slice(db):
    engine, signal_id = db
    first = _execution(signal_id, 1.0)
    first.commission = 2.0
    save_live_execution_up_to_cumulative(first, 1.0)

    second = _execution(signal_id, 4.0)
    second.commission = 8.0
    save_live_execution_up_to_cumulative(second, 4.0)

    with Session(engine) as session:
        stored = session.exec(text("select commission from live_executions")).first()
    assert float(stored[0]) == pytest.approx(2.0 + 8.0 * (3.0 / 4.0)), (
        "commission must scale with the incremental slice, not be re-added in full or discarded"
    )
