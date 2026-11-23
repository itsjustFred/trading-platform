"""Direct tests for the internal runner's open-entry-count ledger (#360).

``list_live_deployment_open_entry_counts_by_symbol`` decides whether a
deployment's per-symbol entry cap has room. These tests pin the FIFO
attribution, the unfilled-order working-status filter, and the deployment
scoping against a real (sandboxed) database — the runner-level tests only
monkeypatch this function.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from shared.models import LiveExecution, LiveSignal

BASE_TS = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)


def _save_signal(
    database,
    *,
    deployment_id: str,
    symbol: str = "AAPL",
    action: str = "buy",
    filled_qty: float | None = None,
    broker_order_id: str | None = None,
    timestamp: datetime | None = None,
    metadata: dict | None = None,
) -> str:
    """Persist one signal plus an optional single fill; return its id."""
    merged = {"idempotency_key": uuid.uuid4().hex, **(metadata or {})}
    if broker_order_id:
        merged["broker_order_id"] = broker_order_id
    signal = LiveSignal(
        id=uuid.uuid4(),
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,  # type: ignore[arg-type]
        theoretical_price=100.0,
        timestamp=timestamp or BASE_TS,
        metadata=merged,
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    if filled_qty is not None:
        database.save_live_execution(
            LiveExecution(
                signal_id=uuid.UUID(signal_id),
                actual_price=100.0,
                filled_qty=filled_qty,
                commission=0.0,
                timestamp=(timestamp or BASE_TS) + timedelta(seconds=1),
                broker_order_id=broker_order_id or f"fill-{uuid.uuid4().hex[:8]}",
            )
        )
    return signal_id


def _entry(
    database, deployment_id: str, *, symbol="AAPL", action="buy",
    filled_qty=None, broker_order_id=None, timestamp=None,
) -> str:
    return _save_signal(
        database,
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        filled_qty=filled_qty,
        broker_order_id=broker_order_id,
        timestamp=timestamp,
    )


def _exit(
    database, deployment_id: str, *, symbol="AAPL", action="sell",
    filled_qty=None, broker_order_id=None, timestamp=None, source=None,
) -> str:
    metadata: dict = {"exit_only": True, "risk_sizing_mode": "exit_only"}
    if source:
        metadata["source"] = source
    return _save_signal(
        database,
        deployment_id=deployment_id,
        symbol=symbol,
        action=action,
        filled_qty=filled_qty,
        broker_order_id=broker_order_id,
        timestamp=timestamp,
        metadata=metadata,
    )


def _counts(database, deployment_id: str, working=None, held=None) -> dict[str, int]:
    return database.list_live_deployment_open_entry_counts_by_symbol(
        deployment_id, working, held
    )


def _refs(database, deployment_id: str) -> set[str]:
    return database.list_live_deployment_unfilled_entry_order_refs(deployment_id)


def test_filled_entry_counts_as_one_open_position(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, filled_qty=10.0)

    assert _counts(database, mine) == {"AAPL": 1}


def test_round_trip_ending_flat_allows_reentry(isolated_db) -> None:
    """A buy fully reduced by an exit leaves no open entry: the symbol is
    absent from the counts, so the cap does not block a fresh entry."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, filled_qty=10.0, timestamp=BASE_TS)
    _exit(database, mine, filled_qty=10.0, timestamp=BASE_TS + timedelta(minutes=5))

    assert _counts(database, mine) == {}


def test_broker_close_rows_reduce_entries(isolated_db) -> None:
    """A broker-side close (source='broker_close') is a reducing row even
    though it is not an exit-only signal."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, filled_qty=10.0, timestamp=BASE_TS)
    _exit(
        database, mine, filled_qty=10.0,
        timestamp=BASE_TS + timedelta(minutes=5), source="broker_close",
    )

    assert _counts(database, mine) == {}


def test_exit_only_fills_reduce_oldest_entry_first(isolated_db) -> None:
    """FIFO: reducing fills close the oldest open entry before touching
    newer ones, so a partially closed oldest entry keeps counting."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    older = BASE_TS
    newer = BASE_TS + timedelta(minutes=10)
    _entry(database, mine, filled_qty=10.0, timestamp=older)
    _entry(database, mine, filled_qty=5.0, timestamp=newer)
    # 12 reducing: wipes the 10-share oldest entry, takes 2 of the 5-share
    # newer one, leaving a 3-share residual -> exactly one open entry.
    _exit(database, mine, filled_qty=12.0, timestamp=newer + timedelta(minutes=1))

    assert _counts(database, mine) == {"AAPL": 1}


def test_partially_closed_oldest_entry_still_counts(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, filled_qty=10.0)
    _exit(database, mine, filled_qty=4.0, timestamp=BASE_TS + timedelta(minutes=5))

    assert _counts(database, mine) == {"AAPL": 1}


def test_over_reduction_does_not_create_negative_entries(isolated_db) -> None:
    """More reducing quantity than entry quantity cannot push the count
    negative or resurrect closed entries."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, filled_qty=5.0)
    _exit(database, mine, filled_qty=50.0, timestamp=BASE_TS + timedelta(minutes=5))

    assert _counts(database, mine) == {}


def test_short_entries_count_and_reduce(isolated_db) -> None:
    """A sell entry opens a short; a buy-side exit-only fill reduces it."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, action="sell", filled_qty=8.0)
    assert _counts(database, mine) == {"AAPL": 1}

    _exit(database, mine, action="buy", filled_qty=8.0,
          timestamp=BASE_TS + timedelta(minutes=5))
    assert _counts(database, mine) == {}


def test_exit_fill_tolerance_boundary(isolated_db) -> None:
    """A residual at or below ``_EXIT_FILL_TOLERANCE`` does not count; one
    share above it does."""
    from shared.database import _EXIT_FILL_TOLERANCE
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    tol = _EXIT_FILL_TOLERANCE
    _entry(database, mine, filled_qty=10.0, timestamp=BASE_TS)
    _exit(
        database, mine, filled_qty=10.0 - tol / 2,
        timestamp=BASE_TS + timedelta(minutes=5),
    )
    assert _counts(database, mine) == {}

    _entry(database, mine, filled_qty=10.0, timestamp=BASE_TS + timedelta(minutes=10))
    _exit(
        database, mine, filled_qty=10.0 - tol * 2,
        timestamp=BASE_TS + timedelta(minutes=15),
    )
    # residual = 2*tol > tol -> still open
    assert _counts(database, mine) == {"AAPL": 1}


def test_equal_timestamps_order_deterministically_by_signal_id(isolated_db) -> None:
    """Rows sharing a timestamp (or lacking ordering granularity) fall back
    to the signal id, so FIFO attribution is stable run to run. The column
    is NOT NULL, so a truly absent timestamp cannot be stored."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    # Two entries at the same timestamp; reduction covers exactly one of
    # them whichever way the tiebreak lands, leaving one open entry.
    _entry(database, mine, filled_qty=4.0, timestamp=BASE_TS)
    _entry(database, mine, filled_qty=4.0, timestamp=BASE_TS)
    _exit(database, mine, filled_qty=4.0, timestamp=BASE_TS + timedelta(minutes=1))

    assert _counts(database, mine) == {"AAPL": 1}
    # Repeated reads agree.
    assert _counts(database, mine) == {"AAPL": 1}


def test_counts_are_scoped_to_the_deployment(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    other = str(uuid.uuid4())
    seed_live_deployments(database, [mine, other])

    _entry(database, other, filled_qty=10.0)
    assert _counts(database, mine) == {}
    assert _counts(database, other) == {"AAPL": 1}


def test_symbols_are_counted_independently(isolated_db) -> None:
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, symbol="AAPL", filled_qty=10.0)
    _entry(database, mine, symbol="MSFT", filled_qty=3.0, timestamp=BASE_TS)
    _entry(
        database, mine, symbol="MSFT", filled_qty=2.0,
        timestamp=BASE_TS + timedelta(minutes=10),
    )
    # FIFO: the 3.0 exit closes the oldest MSFT entry exactly; the newer
    # 2.0 entry stays open.
    _exit(
        database, mine, symbol="MSFT", filled_qty=3.0,
        timestamp=BASE_TS + timedelta(minutes=15),
    )

    assert _counts(database, mine) == {"AAPL": 1, "MSFT": 1}


def test_working_unfilled_entry_blocks(isolated_db) -> None:
    """An entry with no fills counts as open only while the broker still
    reports its order working — matched on broker order id or signal id."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, broker_order_id="bo-1")
    by_signal = _entry(database, mine, symbol="MSFT")

    assert _counts(database, mine, {"bo-1"}) == {"AAPL": 1}
    assert _counts(database, mine, {by_signal}, held=set()) == {"MSFT": 1}
    assert _counts(database, mine, {"bo-1", by_signal}) == {
        "AAPL": 1, "MSFT": 1,
    }
    # Neither reference working and the broker holds no position: nothing
    # blocks.
    assert _counts(database, mine, {"unrelated"}, held=set()) == {}
    # Fail-closed default: unknown working set -> a broker-referenced
    # unfilled entry still counts, a reference-less one does not.
    assert _counts(database, mine, None) == {"AAPL": 1}


def test_dead_unfilled_entry_does_not_block(isolated_db) -> None:
    """A canceled/expired unfilled entry — absent from the broker's working
    list — must not lock the symbol forever (#360 blocker)."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, broker_order_id="bo-dead")

    # The broker confirms the order is terminal AND holds no position in
    # the symbol: the entry is dead and must not lock it.
    assert _counts(database, mine, set(), held=set()) == {}
    # Once the dead order's row is all that remains, the symbol is free.
    assert _counts(database, mine, {"bo-other"}, held=set()) == {}


def test_filled_but_unbooked_entry_counts_via_broker_position(isolated_db) -> None:
    """Bundle-50 blocker: an entry whose order filled but whose fill has not
    reached ``live_executions`` is not working either — the working list
    cannot tell it from a dead unfilled order. A non-zero broker position
    in the symbol is the proof it is still open, so the N=1 cap holds."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, broker_order_id="bo-filled-unbooked")

    # Not working, but the broker holds AAPL: counted open.
    assert _counts(database, mine, set(), held={"AAPL"}) == {"AAPL": 1}
    # The position proof is per-symbol: a held MSFT position does not
    # rescue an AAPL entry.
    assert _counts(database, mine, set(), held={"MSFT"}) == {}
    # Unreadable position state fails closed: still counted open.
    assert _counts(database, mine, set(), held=None) == {"AAPL": 1}
    # Symbol matching is case-insensitive like the ledger's.
    assert _counts(database, mine, set(), held={"aapl"}) == {"AAPL": 1}


def test_held_position_matches_crypto_across_symbol_spellings(isolated_db) -> None:
    """Bundle-51 blocker: Alpaca reports crypto positions as ``BTCUSD`` while
    a stored signal may say ``BTC/USD`` — the held-position proof must match
    on the canonical pair key or the N=1 cap fails open."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, symbol="BTC/USD", broker_order_id="bo-crypto")

    # Broker spelling BTCUSD matches the stored BTC/USD entry.
    assert _counts(database, mine, set(), held={"BTCUSD"}) == {"BTC/USD": 1}
    # And the reverse spelling.
    _entry(database, mine, symbol="BTCUSD", broker_order_id="bo-crypto-2")
    assert _counts(database, mine, set(), held={"BTC/USD"}) == {
        "BTC/USD": 1, "BTCUSD": 1,
    }
    # A different pair does not rescue either spelling.
    assert _counts(database, mine, set(), held={"ETHUSD"}) == {}


def test_held_position_leaves_equity_symbols_untouched(isolated_db) -> None:
    """The canonical key must not merge distinct equity tickers."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    _entry(database, mine, symbol="BRK.B", broker_order_id="bo-brk")

    assert _counts(database, mine, set(), held={"BRK.B"}) == {"BRK.B": 1}
    assert _counts(database, mine, set(), held={"BRKB"}) == {}


def test_unfilled_entry_refs_cover_both_id_kinds(isolated_db) -> None:
    """The refs set carries signal ids and broker order ids of unfilled
    entries only — never exits, broker closes, or filled entries."""
    from tests.conftest import seed_live_deployments

    database, _engine, _data_dir = isolated_db
    mine = str(uuid.uuid4())
    seed_live_deployments(database, [mine])

    unfilled = _entry(database, mine, broker_order_id="bo-open")
    filled = _entry(database, mine, symbol="MSFT", filled_qty=1.0)
    _exit(database, mine, symbol="TSLA", filled_qty=None, broker_order_id="bo-exit")
    _exit(
        database, mine, symbol="NVDA", filled_qty=None,
        broker_order_id="bo-close", source="broker_close",
    )

    refs = _refs(database, mine)
    assert unfilled in refs
    assert "bo-open" in refs
    assert filled not in refs
    assert _refs(database, mine).isdisjoint({"bo-exit", "bo-close"})
