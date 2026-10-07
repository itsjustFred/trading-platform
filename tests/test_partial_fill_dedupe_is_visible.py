"""A skipped fill whose quantity differs is a lost fill, not a redelivery - and must say so.

Measured, on an isolated database, no broker call: cTrader's dedupe key is
`ctrader:deal:<dealId>`, which identifies a **deal**, and it is used against a unique index meant
to identify a **fill**. Three partial fills reported under one deal id store the first and drop the
other two:

    qty=1.0   -> saved=True
    qty=0.5   -> saved=False     INFO: Skipping duplicate live execution
    qty=0.25  -> saved=False     INFO: Skipping duplicate live execution
    rows actually stored: 1 -> [(1.0, 'ctrader:deal:9001')]

So if the broker ever reports a partial-fill sequence as a single deal, the stored position size
is short by 0.75 and the only record of it is an INFO line. An operator watching for problems
sees nothing, and the row looks complete.

**Whether cTrader reports one deal id per fill or one per deal cannot be answered from here.**
That is a broker question - item 11's shape - and this file does not guess it. What is decidable
is the distinction in the log, and that is what changed: a skip whose quantity matches the stored
row is a redelivery and stays INFO; a skip whose quantity differs is a *second fill discarded*
and is now a WARNING naming both quantities and the gap.

Nothing about what gets stored changed. Whether the second fill should have been stored at all
is item 1, untouched.

Isolation uses the repository's own `isolated_db` fixture, so these tests cannot reach production.
`live_executions.signal_id` carries a real foreign key, so the fixture creates the parent signal -
which is incidentally the evidence that the enforced FKs of L74 are genuinely enforced.
"""

from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, timezone

import pytest

DEAL = 9001
OTHER_DEAL = 9002


@pytest.fixture()
def wired_db(isolated_db):
    """The sandbox plus one signal, since `live_executions.signal_id` is a real foreign key."""
    database = isolated_db[0]

    # This file's signals point at a synthetic deployment; the FK under construction needs the
    # parent to exist, and it is only three lines here rather than at each call site.
    from tests.conftest import seed_synthetic_deployment_parents

    seed_synthetic_deployment_parents(
        database, ("ctrader-demo-smoke-dedupe-test",)
    )
    signal_id = str(uuid.uuid4())
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id=signal_id,
                deployment_id="ctrader-demo-smoke-dedupe-test",
                symbol="XAUUSD",
                action="buy",
                theoretical_price=261.7,
                timestamp=datetime.now(timezone.utc),
                source="internal",
                broker_order_id="",
                idempotency_key=str(uuid.uuid4()),
            )
        )
        session.commit()
    return database, signal_id


def _fill(signal_id: str, deal: int, qty: float, ts: str):
    from shared.models import LiveExecution

    return LiveExecution(
        id=str(uuid.uuid4()),
        signal_id=signal_id,
        actual_price=261.7,
        filled_qty=qty,
        commission=0.0,
        timestamp=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc),
        broker_order_id=f"ord-{deal}",
        dedupe_key=f"ctrader:deal:{deal}",
        broker_fill_id=deal,
    )


def _skip_logs(database, action) -> str:
    """Run one save and return what the database module logged while doing it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = database.logger
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        action()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
    return stream.getvalue()


def test_a_partial_fill_sequence_loses_every_fill_after_the_first(wired_db) -> None:
    """The measured behaviour, pinned so a change to the key or the index is visible.

    This is the finding, not an endorsement: three fills under one deal id produce one stored row.
    """
    database, signal = wired_db
    assert database.save_live_execution(_fill(signal, DEAL, 1.0, "2026-10-05T10:00:00")) is True
    assert database.save_live_execution(_fill(signal, DEAL, 0.5, "2026-10-05T10:00:03")) is False
    assert database.save_live_execution(_fill(signal, DEAL, 0.25, "2026-10-05T10:00:06")) is False
    with database.get_session() as session:
        rows = session.exec(
            database.select(database.LiveExecutionModel).where(
                database.LiveExecutionModel.dedupe_key == f"ctrader:deal:{DEAL}"
            )
        ).all()
    assert [r.filled_qty for r in rows] == [1.0], (
        "expected one stored row of 1.0 for a three-fill sequence under one deal id; got "
        f"{[r.filled_qty for r in rows]}. If this now stores all three, the dedupe key is per-fill "
        f"and item 1's premise has changed - re-measure rather than inherit."
    )


def test_an_exact_redelivery_stays_info(wired_db) -> None:
    """The common case must not cry wolf, or the warning below stops being read."""
    database, signal = wired_db
    fill = _fill(signal, DEAL, 1.0, "2026-10-05T10:00:00")
    assert database.save_live_execution(fill) is True
    text = _skip_logs(
        database, lambda: database.save_live_execution(fill)
    )
    assert "Skipping duplicate" in text
    assert "NOT a redelivery" not in text, (
        "an identical redelivery was reported as a lost fill; if the tolerance in "
        "_same_quantity is too tight this fires on every ordinary redelivery"
    )


def test_a_second_fill_on_one_deal_is_a_warning_naming_the_gap(wired_db) -> None:
    """The decidable half: make the loss visible without changing what is stored."""
    database, signal = wired_db
    assert database.save_live_execution(_fill(signal, DEAL, 1.0, "2026-10-05T10:00:00")) is True
    text = _skip_logs(
        database,
        lambda: database.save_live_execution(_fill(signal, DEAL, 0.5, "2026-10-05T10:00:03")),
    )
    assert "NOT a redelivery" in text, (
        "a fill whose quantity differs from the stored row was skipped as if it were a redelivery"
    )
    assert "0.50000000" in text, "the warning must name the quantity it discarded"
    assert "short by" in text, (
        "the warning must say the stored position size is short, and by how much"
    )


def test_a_different_deal_is_unaffected(wired_db) -> None:
    """So the warning cannot be mistaken for the key being globally broken."""
    database, signal = wired_db
    assert database.save_live_execution(_fill(signal, DEAL, 1.0, "2026-10-05T10:00:00")) is True
    assert database.save_live_execution(_fill(signal, OTHER_DEAL, 1.0, "2026-10-05T10:01:00")) is True


def test_quantity_comparison_survives_a_decimal_round_trip() -> None:
    """Why the comparison is a tolerance and not equality.

    A redelivery arrives through a decimal round-trip, so `0.1 + 0.2 != 0.3`. With equality the
    ordinary redelivery would be classified as a different fill and warn on every one - which is
    how a warning gets muted.
    """
    from shared.database import _quantity_gap, _same_quantity

    assert _same_quantity(0.30000000000000004, 0.3) is True
    assert _same_quantity(1.0, 0.5) is False
    assert _quantity_gap(1.0, 0.5) == "0.50000000"
