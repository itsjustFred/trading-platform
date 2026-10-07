"""The broker's per-fill id must be recorded, or a dedupe collision stays undecidable forever.

This is the "much smaller ask" the item-1 investigation recorded: **adding the broker's deal id to
the stored execution makes every future instance decidable, and it is a schema addition rather than
a behaviour change** - so it can be done now without pre-empting the dedupe policy, which is still
yours.

The problem it removes is specific and measured. `dedupe_key` is a string **this codebase
assembled**, so the question it exists to answer - "were these two rows the same broker fill?" -
is not answerable from the table afterwards without parsing that string back apart. And when two
equal-sized fills on one order collide, parsing is the only evidence there is, and it is ambiguous
by construction: `_build_dedupe_key` falls back to a key derived from order, timestamp and volume
when the broker sends no deal id, so a derived key and a broker key can describe the same fill.

Measured on production: one stored signal produced two execution rows 2 ms apart at an identical
price, both with `dedupe_key IS NULL`, and nothing on the table could distinguish two real partial
fills from one fill counted twice.

What this does **not** do: change any dedupe behaviour, decide anything about collisions, or alter
what is stored for existing rows. It records one more piece of evidence.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]

import shared.database as database
from shared.models import LiveExecution
from tests.test_live_execution_safety import _verified_demo_account  # noqa: I001


def _seed_signal(dep: Any) -> str:
    """A signal row that satisfies `live_executions`' foreign key, built through the model."""
    signal_id = str(uuid.uuid4())
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id=signal_id,
                deployment_id=dep,
                symbol="AAPL",
                action="BUY",
                theoretical_price=261.7,
                status="EXECUTED",
                source="internal",
                broker_order_id="450566f0",
                timestamp=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return signal_id


def _fill(signal_id: str, *, fill_id: int | None, dedupe_key: str | None) -> LiveExecution:
    return LiveExecution(
        signal_id=uuid.UUID(signal_id),
        actual_price=261.7,
        filled_qty=0.07822,
        commission=0.0,
        timestamp=datetime.now(timezone.utc),
        broker_order_id="450566f0",
        dedupe_key=dedupe_key,
        broker_fill_id=fill_id,
    )


@pytest.fixture
def signal_id(isolated_db: Any) -> str:
    _verified_demo_account(database, "acc-1", "ctrader")
    # "acc-1" is this fixture's synthetic deployment, so it needs a parent row for the
    # live_signals.deployment_id foreign key.
    from tests.conftest import seed_synthetic_deployment_parents

    seed_synthetic_deployment_parents(database, ("acc-1",))
    return _seed_signal("acc-1")


def _columns(table: str) -> dict[str, tuple[Any, ...]]:
    """`{name: (type, notnull, default)}` for one table, read-only."""
    conn = sqlite3.connect(f"file:{Path(database.DB_PATH)}?mode=ro", uri=True)
    try:
        return {r[1]: (r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _rows() -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(f"file:{Path(database.DB_PATH)}?mode=ro", uri=True)
    try:
        return list(
            conn.execute("SELECT dedupe_key, broker_fill_id FROM live_executions ORDER BY rowid")
        )
    finally:
        conn.close()


def test_the_column_exists_and_is_nullable(isolated_db: Any) -> None:
    """The precondition, asserted on the schema rather than on a save.

    A behavioural test alone would pass with the column absent and every value arriving as `NULL`,
    which is exactly the state this change exists to end.
    """
    conn = sqlite3.connect(f"file:{Path(database.DB_PATH)}?mode=ro", uri=True)
    try:
        info = _columns("live_executions")
    finally:
        conn.close()
    assert "broker_fill_id" in info, (
        "live_executions has no broker_fill_id column, so the broker's per-fill id cannot be "
        "recorded and a dedupe collision stays undecidable"
    )
    _type, notnull, default = info["broker_fill_id"]
    assert notnull == 0, "the column must be nullable - most adapters have no such id"
    assert default is None, (
        "the column must have no database default; a default would fabricate a fill id for "
        "adapters that never set one, which is precisely the failure mode being fixed"
    )


def test_a_broker_fill_id_survives_the_round_trip(signal_id: str) -> None:
    """Written on the way in, readable on the way out."""
    assert database.save_live_execution(
        _fill(signal_id, fill_id=26533115, dedupe_key="ctrader:deal:26533115")
    )
    rows = _rows()
    assert rows == [("ctrader:deal:26533115", 26533115)], rows


def test_two_equal_fills_on_one_order_are_now_distinguishable(signal_id: str) -> None:
    """**The case this exists for.**

    Identical price, 2 ms apart, equal volumes - indistinguishable except by the broker's own
    identifier. Before the change a reader had only the dedupe key, which is our own string; now the
    raw id is there to compare.
    """
    assert database.save_live_execution(
        _fill(signal_id, fill_id=26533115, dedupe_key="ctrader:deal:26533115")
    )
    assert database.save_live_execution(
        _fill(signal_id, fill_id=26533116, dedupe_key="ctrader:deal:26533116")
    )
    stored = [row[1] for row in _rows()]
    assert stored == [26533115, 26533116], stored
    assert len(set(stored)) == 2, (
        "the two fills are not distinguishable by broker id, so the collision is still undecidable"
    )


def test_an_absent_broker_id_is_stored_as_null_not_zero(signal_id: str) -> None:
    """`None` and `0` must not be conflated.

    A missing fill id and a fill whose id is `0` are different facts, and `0` is also the sentinel
    the
    adapter tests for (`if deal_id <= 0`). Storing a fabricated `0` would make "the broker told us
    nothing" look like "the broker said zero".
    """
    assert database.save_live_execution(_fill(signal_id, fill_id=None, dedupe_key="ctrader:x:1"))
    rows = _rows()
    assert rows[-1][1] is None, rows
    assert 0 not in [row[1] for row in rows], rows


def test_dedupe_behaviour_is_unchanged(signal_id: str) -> None:
    """The load-bearing non-regression.

    Recording evidence must not alter what is stored or what is rejected. The same deal saved twice
    must still be refused, and a genuinely distinct deal must still be accepted.
    """
    first = _fill(signal_id, fill_id=26533115, dedupe_key="ctrader:deal:26533115")
    assert database.save_live_execution(first) is True
    assert database.save_live_execution(
        _fill(signal_id, fill_id=26533115, dedupe_key="ctrader:deal:26533115")
    ) is False, "the duplicate was accepted; dedupe behaviour changed"
    assert database.save_live_execution(
        _fill(signal_id, fill_id=26533116, dedupe_key="ctrader:deal:26533116")
    ) is True
    assert len(_rows()) == 2, _rows()


def test_the_migration_adds_the_column_and_is_idempotent(isolated_db: Any) -> None:
    """The wiring, and the no-op-on-repeat property `init_db` depends on.

    The table is rebuilt without the column so this exercises the `ALTER` rather than `create_all`,
    which emits it from the model and would mask a missing migration.
    """
    db_path = Path(database.DB_PATH)
    conn = sqlite3.connect(db_path)
    try:
        columns_before = {r[1] for r in conn.execute("PRAGMA table_info(live_executions)")}
        if "broker_fill_id" in columns_before:
            conn.execute("ALTER TABLE live_executions DROP COLUMN broker_fill_id")
            conn.commit()
        missing = "broker_fill_id" not in {
            r[1] for r in conn.execute("PRAGMA table_info(live_executions)")
        }
    finally:
        conn.close()
    assert missing, "could not stage a table without the column, so this proves nothing"

    with database.get_engine().connect() as writable:
        database._add_live_executions_broker_fill_id(writable)
        writable.commit()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        after = _columns("live_executions")
    finally:
        conn.close()
    assert "broker_fill_id" in after, "the migration did not add the column"
    assert after["broker_fill_id"][1] == 0, "the added column must be nullable"

    # Re-running must not raise: `init_db` runs on every process start.
    with database.get_engine().connect() as writable:
        database._add_live_executions_broker_fill_id(writable)
        writable.commit()

class TestTheFillAsymmetryBetweenAdaptersIsIntentional:
    """Alpaca and cTrader persist fills differently, and that difference is the design.

    Alpaca call sites use `save_live_execution_up_to_cumulative`; cTrader uses `save_live_execution`.
    That reads like an oversight - one adapter was hardened and the other was missed - and it was
    proposed as such twice in this backlog before being checked.

    It is not an oversight. The two brokers report fills differently:

      * Alpaca's broker quantities are **cumulative** for an order, while local execution rows are
        stored as event deltas, so reconciling them means persisting only the positive difference.
        That is what the cumulative variant does.
      * cTrader emits **discrete deals**, each carrying its own `dealId` and `filledVolume`. A deal is
        an independent fill, not a running total, so there is nothing to reconcile against and the
        dealId-keyed dedupe on the plain path is correct.

    Calling the cumulative variant with a per-deal volume would treat a single fill as if it were an
    order-level running total and under-record it. Harmonising the two would introduce a fill-loss bug
    to remove an inconsistency that was never one.

    The assertions below are source-level on purpose: the distinction is which persistence function each
    adapter calls, and that is not observable from the database once a row is written.
    """

    ADAPTERS = REPO / "live_runner"

    def _call_sites(self, filename: str, function: str) -> list:
        source = (self.ADAPTERS / filename).read_text(encoding="utf-8")
        return [
            line for line in source.splitlines()
            if function in line and "import" not in line and not line.strip().startswith("#")
        ]

    def test_alpaca_uses_the_cumulative_variant(self) -> None:
        sites = self._call_sites("alpaca_adapter.py", "save_live_execution_up_to_cumulative")
        assert sites, "alpaca no longer calls save_live_execution_up_to_cumulative"

    def test_ctrader_does_not(self) -> None:
        """The assertion that stops the 'fix'."""
        assert not self._call_sites("ctrader_adapter.py", "save_live_execution_up_to_cumulative"), (
            "ctrader_adapter now calls save_live_execution_up_to_cumulative. cTrader deals are discrete "
            "per-deal fills carrying their own filledVolume, not cumulative order-level quantities, so "
            "the cumulative path would under-record them. If the broker's reporting has genuinely "
            "changed, say so here rather than letting this fail be 'fixed' away."
        )

    def test_ctrader_still_dedupes_on_the_deal_id(self) -> None:
        """The property that makes the plain path safe here."""
        source = (self.ADAPTERS / "ctrader_adapter.py").read_text(encoding="utf-8")
        assert "ctrader:deal:" in source, (
            "cTrader's dedupe key no longer prefers the deal id, which is what distinguishes two "
            "discrete fills on one order from a redelivery of the same fill"
        )

