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

import shared.database as database
from shared.models import LiveExecution


def _seed_deployment(deployment_id: str) -> None:
    """An account and a deployment, so a signal has a parent row to point at.

    Written here rather than imported from another suite: this file needs a *parent row*, not a
    verified demo account, and borrowing another module's fixture helper would couple two files that
    are testing different things.
    """
    with database.get_session() as session:
        session.add(database.LiveAccountModel(id="acc-1", broker="ctrader", name="acc-1"))
        session.add(
            database.LiveDeploymentModel(
                id=deployment_id,
                strategy_name="fill-id-probe",
                account_id="acc-1",
                symbols_json=["AAPL"],
                timeframe="1m",
            )
        )
        session.commit()


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
    _seed_deployment("acc-1")
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
    """Written on the way in, readable on the way out.

    Both directions are asserted: the write-only half would pass with the column populated and the
    model still dropping it on read, which leaves the evidence unusable to every caller.
    """
    from sqlmodel import select

    assert database.save_live_execution(
        _fill(signal_id, fill_id=26533115, dedupe_key="ctrader:deal:26533115")
    )
    rows = _rows()
    assert rows == [("ctrader:deal:26533115", 26533115)], rows

    with database.get_session() as session:
        record = session.exec(select(database.LiveExecutionModel)).one()
    assert record.to_live_execution().broker_fill_id == 26533115, (
        "the column is populated but the model drops the value on read, so no caller can use it"
    )


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


def test_the_adapter_records_the_deal_id_it_keys_the_row_by() -> None:
    """The whole point is that the *adapter* stores it, not that the column accepts a value.

    The storage tests above would all pass with the adapter still passing nothing, which is the
    state this change exists to end - so this drives the adapter's own construction path.
    """
    pytest.importorskip(
        "ctrader_open_api.messages.OpenApiModelMessages_pb2",
        reason="ctrader-open-api package is not installed in this environment",
    )
    from types import SimpleNamespace

    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=2, PARTIALLY_FILLED=3)
    adapter._resolve_filled_volume = lambda **_: 1000.0
    adapter._symbol_name_for_id = lambda _symbol_id: "XAUUSD"
    adapter._scale_for = lambda _symbol: SimpleNamespace(volume_per_qty=1000.0)
    deal = SimpleNamespace(
        dealStatus=2,
        dealId=26533115,
        executionPrice=261.7,
        commission=0.0,
        executionTimestamp=1_700_000_000_000,
        utcLastUpdateTimestamp=0,
        symbolId=41,
    )

    execution = adapter._build_reconciled_execution(
        deal, signal_uuid=uuid.uuid4(), broker_order_id="450566f0"
    )

    assert execution is not None
    assert execution.dedupe_key == "ctrader:deal:26533115"
    assert execution.broker_fill_id == 26533115, (
        "the adapter keyed the row by the deal id but did not record it, so a later collision "
        "between two equal-sized fills is still undecidable"
    )


def test_a_missing_deal_id_is_none_not_zero() -> None:
    """``0`` is the protobuf default and the adapter's own sentinel, so it must not be stored."""
    pytest.importorskip(
        "ctrader_open_api.messages.OpenApiModelMessages_pb2",
        reason="ctrader-open-api package is not installed in this environment",
    )
    from types import SimpleNamespace

    from live_runner.ctrader_adapter import _deal_id_or_none

    assert _deal_id_or_none(None) is None
    assert _deal_id_or_none(SimpleNamespace()) is None
    assert _deal_id_or_none(SimpleNamespace(dealId=0)) is None
    assert _deal_id_or_none(SimpleNamespace(dealId=26533115)) == 26533115
    # A negative id is not a real deal either. cTrader's dealIds are positive longs, so this is
    # defensive, but `or None` alone would have let a negative through - and the whole point of
    # this helper is that only a genuine id is ever stored.
    assert _deal_id_or_none(SimpleNamespace(dealId=-1)) is None, (
        "a negative dealId was stored as if it were real"
    )


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
