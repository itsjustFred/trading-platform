"""Issue #127: the three fill/signal dedupe edges the perma audit left unverified.

* **C2** - compound cTrader positions: several deals on one order, scale-in/scale-out across
  orders, partial closes. Cumulative caps and `ctrader:deal:{dealId}` keys must neither
  double-count nor drop a deal, and a key that is already owned by a *different* signal must
  never be mistaken for a redelivery (nor amended).
* **C18** - `save_live_signal` idempotency keys: distinct signals must not share a key, and
  the same producer event must map to one key regardless of how its timestamp is spelled.
* **C19** - `_auto_execution_dedupe_key` and the Alpaca fallback key: genuine equal-sized
  partial fills must stay distinct, redeliveries with a different timestamp spelling must stay
  one, and the broker's own fill id wins over anything derived from content.

Tests marked SAFE pass on the unmodified code and pin behaviour that was verified correct; the
rest reproduce a confirmed defect.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from live_runner.ctrader_adapter import CTraderLiveAdapter
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal
from tests.conftest import seed_live_deployments

T0 = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
DEP_1 = "11111111-1111-4111-8111-aaaaaaaaaaaa"
DEP_2 = "22222222-2222-4222-8222-bbbbbbbbbbbb"


# ── fixtures / helpers ────────────────────────────────────────────────────────────────────────


@pytest.fixture
def database(isolated_db):
    db = isolated_db[0]
    seed_live_deployments(db, [DEP_1, DEP_2])
    return db


def _persist_signal(database, *, deployment_id: str = DEP_1, broker_order_id: str = "") -> str:
    signal = LiveSignal(
        deployment_id=deployment_id,
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.2345,
        timestamp=T0,
        metadata={},
        broker_order_id=broker_order_id or None,
    )
    signal_id, created = database.save_live_signal(signal)
    assert created
    return signal_id


def _execution(
    signal_id: str,
    *,
    qty: float = 5.0,
    price: float = 100.0,
    timestamp: datetime = T0,
    broker_order_id: str = "order-1",
    dedupe_key: str | None = None,
    broker_fill_id: int | None = None,
) -> LiveExecution:
    return LiveExecution(
        signal_id=UUID(signal_id),
        actual_price=price,
        filled_qty=qty,
        commission=0.0,
        timestamp=timestamp,
        broker_order_id=broker_order_id,
        dedupe_key=dedupe_key,
        broker_fill_id=broker_fill_id,
    )


def _rows(database) -> list[tuple[str | None, int | None, float]]:
    import sqlite3

    conn = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True)
    try:
        return list(
            conn.execute(
                "SELECT dedupe_key, broker_fill_id, filled_qty FROM live_executions ORDER BY rowid"
            )
        )
    finally:
        conn.close()


# ── C19: `_auto_execution_dedupe_key` ─────────────────────────────────────────────────────────


def test_c19a_equal_partial_fills_with_distinct_broker_fill_ids_are_both_stored(database) -> None:
    """Two genuine partial fills: same price, qty, timestamp unit and order, distinct fill ids.

    The caller supplied no `dedupe_key`, so the derived key is all that separates them. It was
    built from content only, so the second fill was dropped as a "duplicate". The broker's own
    fill id must win over content.
    """
    signal_id = _persist_signal(database)
    first = _execution(signal_id, broker_fill_id=9001)
    second = _execution(signal_id, broker_fill_id=9002)

    assert database.save_live_execution(first) is True
    assert database.save_live_execution(second) is True

    assert sorted(row[1] for row in _rows(database)) == [9001, 9002]
    assert database.get_live_signal_filled_qty(signal_id) == pytest.approx(10.0)


def test_c19a_same_broker_fill_id_is_one_fill_whatever_the_timestamp_spelling(database) -> None:
    """A redelivery of fill 9001 with a different timestamp precision is the same fill."""
    signal_id = _persist_signal(database)
    original = _execution(
        signal_id, broker_fill_id=9001, timestamp=T0 + timedelta(microseconds=123456)
    )
    redelivered_seconds = _execution(signal_id, broker_fill_id=9001, timestamp=T0)
    redelivered_naive = _execution(
        signal_id,
        broker_fill_id=9001,
        timestamp=(T0 + timedelta(milliseconds=123)).replace(tzinfo=None),
    )

    assert database.save_live_execution(original) is True
    assert database.save_live_execution(redelivered_seconds) is False
    assert database.save_live_execution(redelivered_naive) is False

    assert len(_rows(database)) == 1
    assert database.get_live_signal_filled_qty(signal_id) == pytest.approx(5.0)


def test_c19a_cumulative_path_also_prefers_the_broker_fill_id(database) -> None:
    signal_id = _persist_signal(database)

    assert database.save_live_execution_up_to_cumulative(
        _execution(signal_id, broker_fill_id=9001), 10.0
    ) == pytest.approx(5.0)
    assert database.save_live_execution_up_to_cumulative(
        _execution(signal_id, broker_fill_id=9002), 10.0
    ) == pytest.approx(5.0)
    # The redelivery of 9001 is still a duplicate, and the cap is now exhausted anyway.
    assert database.save_live_execution_up_to_cumulative(
        _execution(signal_id, broker_fill_id=9001, timestamp=T0 + timedelta(seconds=1)), 10.0
    ) == 0.0

    assert database.get_live_signal_filled_qty(signal_id) == pytest.approx(10.0)


def test_c19b_safe_same_instant_in_any_zone_or_awareness_is_one_key() -> None:
    """SAFE: a content-derived key already collapses naive-UTC, aware-UTC and offset spellings."""
    from shared.database import _auto_execution_dedupe_key

    signal_id = str(uuid4())
    instant = datetime(2026, 7, 15, 10, 0, 0, 123000, tzinfo=timezone.utc)
    spellings = [
        instant,
        instant.replace(tzinfo=None),
        instant.astimezone(timezone(timedelta(hours=2))),
        instant.astimezone(timezone(timedelta(hours=-5))),
    ]
    keys = {
        _auto_execution_dedupe_key(_execution(signal_id, timestamp=stamp)) for stamp in spellings
    }
    assert len(keys) == 1, keys


def test_c19b_distinct_instants_are_never_merged_by_normalisation() -> None:
    """Normalisation must not truncate: two fills a microsecond apart are two fills."""
    from shared.database import _auto_execution_dedupe_key

    signal_id = str(uuid4())
    a = _auto_execution_dedupe_key(_execution(signal_id, timestamp=T0))
    b = _auto_execution_dedupe_key(_execution(signal_id, timestamp=T0 + timedelta(microseconds=1)))
    c = _auto_execution_dedupe_key(_execution(signal_id, timestamp=T0 + timedelta(milliseconds=1)))
    d = _auto_execution_dedupe_key(_execution(signal_id, timestamp=T0 + timedelta(seconds=1)))
    assert len({a, b, c, d}) == 4


def test_c19b_distinct_content_fills_are_both_stored(database) -> None:
    """SAFE: with no fill id, fills that differ in any key field stay distinct rows."""
    signal_id = _persist_signal(database)
    assert database.save_live_execution(_execution(signal_id, timestamp=T0)) is True
    assert (
        database.save_live_execution(
            _execution(signal_id, timestamp=T0 + timedelta(microseconds=1))
        )
        is True
    )
    assert database.save_live_execution(_execution(signal_id, qty=6.0)) is True
    assert database.save_live_execution(_execution(signal_id, price=100.5)) is True
    assert len(_rows(database)) == 4


def test_c19b_content_redelivery_across_awareness_is_stored_once(database) -> None:
    """SAFE: the same content redelivered naive-vs-aware is one fill, not two."""
    signal_id = _persist_signal(database)
    assert database.save_live_execution(_execution(signal_id, timestamp=T0)) is True
    assert (
        database.save_live_execution(_execution(signal_id, timestamp=T0.replace(tzinfo=None)))
        is False
    )
    assert len(_rows(database)) == 1


# ── C19: the Alpaca fallback key (no execution_id on the trade update) ────────────────────────


def _alpaca_update(
    *,
    signal_id: str,
    timestamp: Any,
    qty: str = "100",
    cumulative: str | None = "100",
    event: str = "partial_fill",
    execution_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        event=event,
        qty=qty,
        price="50.0",
        timestamp=timestamp,
        execution_id=execution_id,
        order=SimpleNamespace(
            client_order_id=signal_id,
            id=str(uuid5(NAMESPACE_URL, "alpaca-edge-order")),
            filled_qty=cumulative,
            filled_avg_price="50.0",
            commission=None,
        ),
    )



def _alpaca_capture(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    saved: list[tuple[LiveExecution, float]] = []

    def _capture_cumulative(execution: LiveExecution, cumulative_qty: float) -> float:
        saved.append((execution, cumulative_qty))
        return execution.filled_qty

    def _capture_plain(execution: LiveExecution) -> bool:
        saved.append((execution, 0.0))
        return True

    monkeypatch.setattr(module, "save_live_execution_up_to_cumulative", _capture_cumulative)
    monkeypatch.setattr(module, "save_live_execution", _capture_plain)
    return adapter, saved


def test_c19a_alpaca_fallback_key_separates_equal_partial_fills_by_cumulative(monkeypatch) -> None:
    """No execution_id, equal qty, same timestamp: the order's cumulative strictly increases."""
    adapter, saved = _alpaca_capture(monkeypatch)
    signal_id = str(uuid4())
    stamp = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)

    asyncio.run(
        adapter._on_trade_update(
            _alpaca_update(signal_id=signal_id, timestamp=stamp, qty="100", cumulative="100")
        )
    )
    asyncio.run(
        adapter._on_trade_update(
            _alpaca_update(signal_id=signal_id, timestamp=stamp, qty="100", cumulative="200")
        )
    )

    keys = [execution.dedupe_key for execution, _ in saved]
    assert len(keys) == 2 and keys[0] != keys[1], keys


def test_c19b_alpaca_fallback_key_ignores_timestamp_spelling(monkeypatch) -> None:
    """A redelivery with a naive / offset / string timestamp is the same fill, same key."""
    adapter, saved = _alpaca_capture(monkeypatch)
    signal_id = str(uuid4())
    instant = datetime(2026, 7, 15, 10, 0, 0, 123456, tzinfo=timezone.utc)
    for stamp in (
        instant,
        instant.replace(tzinfo=None),
        instant.astimezone(timezone(timedelta(hours=2))),
        "2026-07-15T10:00:00.123456Z",
        "2026-07-15T10:00:00.123456+00:00",
    ):
        asyncio.run(adapter._on_trade_update(_alpaca_update(signal_id=signal_id, timestamp=stamp)))

    keys = {execution.dedupe_key for execution, _ in saved}
    assert len(saved) == 5 and len(keys) == 1, keys


def test_c19b_alpaca_without_identity_or_timestamp_is_not_persisted(monkeypatch) -> None:
    """No execution id, no usable cumulative, no timestamp: a key cannot be derived honestly.

    The old code substituted `now()` for the missing timestamp, so every redelivery got a fresh
    key and was stored again. Refuse instead; reconcile_executions backfills from the order.
    """
    adapter, saved = _alpaca_capture(monkeypatch)
    signal_id = str(uuid4())

    asyncio.run(
        adapter._on_trade_update(
            _alpaca_update(signal_id=signal_id, timestamp=None, cumulative=None)
        )
    )

    assert saved == []


def test_alpaca_execution_id_is_still_the_preferred_key(monkeypatch) -> None:
    """SAFE: the broker-native id outranks every derived key."""
    adapter, saved = _alpaca_capture(monkeypatch)
    signal_id = str(uuid4())
    asyncio.run(
        adapter._on_trade_update(
            _alpaca_update(signal_id=signal_id, timestamp=T0, execution_id="exec-1")
        )
    )
    assert saved[0][0].dedupe_key == "alpaca:exec:exec-1"


# ── C18: idempotency keys ─────────────────────────────────────────────────────────────────────


def _deployment(deployment_id: str = DEP_1) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=deployment_id,
        strategy_name="demo",
        account_id="fixture-account",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={},
    )


def _live_signal(
    deployment_id: str = DEP_1,
    *,
    action: str = "buy",
    entry: float = 1.1,
    stamp: Any = "2026-07-15T10:00:00+00:00",
) -> LiveSignal:
    from live_runner.daemon import _trade_signal_to_live_signal

    trade = TradeSignal(
        action=action,
        symbol="EURUSD",
        entry_price=entry,
        stop_loss=None,
        take_profit=None,
        timeframe="1h",
        metadata={"signal_timestamp": stamp},
    )
    return _trade_signal_to_live_signal(trade, _deployment(deployment_id))


def test_c18_safe_entry_exit_reentry_with_new_producer_stamps_are_three_signals(database) -> None:
    """SAFE: a re-entry after an exit is a distinct signal when the producer stamp moved."""
    entry = database.save_live_signal(_live_signal(action="buy", stamp="2026-07-15T10:00:00+00:00"))
    exit_ = database.save_live_signal(
        _live_signal(action="sell", stamp="2026-07-15T11:00:00+00:00")
    )
    reentry = database.save_live_signal(
        _live_signal(action="buy", stamp="2026-07-15T12:00:00+00:00")
    )

    assert [entry[1], exit_[1], reentry[1]] == [True, True, True]
    assert len({entry[0], exit_[0], reentry[0]}) == 3


def test_c18_safe_same_event_in_two_deployments_is_two_signals(database) -> None:
    """SAFE: the key is deployment-scoped, so a fan-out is not swallowed."""
    a = database.save_live_signal(_live_signal(DEP_1))
    b = database.save_live_signal(_live_signal(DEP_2))
    assert a[1] is True and b[1] is True and a[0] != b[0]


def test_c18_safe_every_keyed_field_separates_signals() -> None:
    """SAFE: price, action, SL/TP, timeframe and stamp each change the key."""
    from live_runner.daemon import _trade_signal_to_live_signal

    def _key(**override: Any) -> str:
        fields: dict[str, Any] = dict(
            action="buy",
            symbol="EURUSD",
            entry_price=1.1,
            stop_loss=1.0,
            take_profit=1.3,
            timeframe="1h",
            metadata={"signal_timestamp": "2026-07-15T10:00:00+00:00"},
        )
        fields.update(override)
        live = _trade_signal_to_live_signal(TradeSignal(**fields), _deployment())
        return str(live.metadata["idempotency_key"])

    keys = {
        _key(),
        _key(action="sell"),
        _key(entry_price=1.1001),
        _key(stop_loss=0.99),
        _key(take_profit=1.31),
        _key(timeframe="4h"),
        _key(metadata={"signal_timestamp": "2026-07-15T10:00:01+00:00"}),
        _key(symbol="GBPUSD"),
    }
    assert len(keys) == 8


def test_c18_same_instant_spelled_differently_is_one_key() -> None:
    """The same producer event redelivered with another timestamp spelling must dedupe.

    `str(stamp)` was hashed verbatim, so `...Z`, `...+00:00`, a datetime object and a naive
    datetime produced four keys for one event: four broker orders on a replay.
    """
    instant = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    spellings = [
        "2026-07-15T10:00:00+00:00",
        "2026-07-15T10:00:00Z",
        "2026-07-15T12:00:00+02:00",
        "2026-07-15 10:00:00+00:00",
        instant,
        instant.replace(tzinfo=None),
        instant.astimezone(timezone(timedelta(hours=-5))),
    ]
    keys = {_live_signal(stamp=stamp).metadata["idempotency_key"] for stamp in spellings}
    assert len(keys) == 1, keys


def test_c18_distinct_instants_are_not_merged_by_stamp_normalisation() -> None:
    """Normalising the stamp must not widen the window: 1 s / 1 us apart still differ."""
    base = datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc)
    keys = {
        _live_signal(stamp=base).metadata["idempotency_key"],
        _live_signal(stamp=base + timedelta(microseconds=1)).metadata["idempotency_key"],
        _live_signal(stamp=base + timedelta(seconds=1)).metadata["idempotency_key"],
        _live_signal(stamp=base + timedelta(days=1)).metadata["idempotency_key"],
    }
    assert len(keys) == 4


def test_c18_unparseable_stamp_still_keys_on_its_text() -> None:
    """A stamp we cannot parse keeps its verbatim text: no silent merge of different strings."""
    a = _live_signal(stamp="bar-17").metadata["idempotency_key"]
    b = _live_signal(stamp="bar-18").metadata["idempotency_key"]
    c = _live_signal(stamp="bar-17").metadata["idempotency_key"]
    assert a != b and a == c


def test_c18_identical_event_replayed_is_swallowed_loudly(database, caplog) -> None:
    """By design: an identical producer event (same stamp, same fields) is a redelivery.

    A re-entry that is byte-identical to an earlier signal cannot be told from a replay, and
    trading it again on a guess is the worse failure for a live account, so it stays swallowed.
    What was wrong is that it was swallowed *silently* and reported against a Discord id that
    does not exist for these signals. It must now name the idempotency key and both signal ids.
    """
    first = database.save_live_signal(_live_signal(action="buy"))
    database.save_live_signal(_live_signal(action="sell", stamp="2026-07-15T11:00:00+00:00"))

    with caplog.at_level(logging.WARNING, logger="shared.database"):
        replay = database.save_live_signal(_live_signal(action="buy"))

    assert replay == (first[0], False)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "idempotency_key" in message and first[0] in message for message in messages
    ), messages


# ── C2: compound cTrader positions ────────────────────────────────────────────────────────────

ENTRY_ORDER = 45678
CLOSE_ORDER = 45679


def _ctrader_adapter() -> CTraderLiveAdapter:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="46736452"
    )
    adapter.account_config_id = "acc-1"
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=2, PARTIALLY_FILLED=3)
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._order_id_to_symbol[ENTRY_ORDER] = "EURUSD"
    adapter._order_id_to_symbol[CLOSE_ORDER] = "EURUSD"
    return adapter


def _stream_event(
    *,
    signal_id: str,
    order_id: int,
    deal_id: int,
    volume: int,
    cumulative: int,
    execution_type: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        executionType=execution_type,
        order=SimpleNamespace(
            orderId=order_id,
            clientOrderId=signal_id,
            executedVolume=cumulative,
            utcLastUpdateTimestamp=1_700_000_000_000,
        ),
        deal=SimpleNamespace(
            dealId=deal_id,
            orderId=order_id,
            positionId=777,
            symbolId=1,
            filledVolume=volume,
            executionPrice=1.2345,
            executionTimestamp=1_700_000_000_000,  # every deal in the same millisecond
            commission=0.0,
        ),
        HasField=lambda field: field in {"deal", "order"},
    )


def _recovered_deal(deal_id: int, order_id: int, volume: int) -> SimpleNamespace:
    return SimpleNamespace(
        dealId=deal_id,
        dealStatus=2,
        orderId=order_id,
        positionId=777,
        symbolId=1,
        filledVolume=volume,
        executionPrice=1.2345,
        executionTimestamp=1_700_000_000_000,
        utcLastUpdateTimestamp=0,
        commission=0,
    )


def _reconcile(adapter, signal_id: str, order_id: int, deals, cumulative: int) -> int:
    details = SimpleNamespace(order=[SimpleNamespace(executedVolume=cumulative)], deal=list(deals))
    return asyncio.run(
        adapter._persist_recovered_deals(
            signal_id=signal_id,
            signal_uuid=UUID(signal_id),
            broker_order_id=str(order_id),
            details_res=details,
        )
    )


def _seed_ctrader_signal(database, order_id: int, *, deployment_id: str = DEP_1) -> str:
    signal_id = _persist_signal(database, deployment_id=deployment_id)
    assert database.set_live_signal_broker_order_id(signal_id, str(order_id))
    return signal_id


def test_c2_safe_scale_in_then_partial_close_stream_redelivery_and_reconcile(database) -> None:
    """SAFE: a position scaled in over three deals and partially closed over two.

    Entry order: deals 1001/1002/1003 of 300_000/300_000/400_000 (two equal-sized, one
    millisecond). Close order (a second signal, same position): deals 2001/2002 of
    250_000/250_000. Every deal is streamed, every deal is redelivered with a stale and with the
    final order snapshot, and reconciliation replays them all. Each signal must end with exactly
    its order's cumulative volume and one row per deal.
    """
    entry_signal = _seed_ctrader_signal(database, ENTRY_ORDER)
    close_signal = _seed_ctrader_signal(database, CLOSE_ORDER, deployment_id=DEP_1)
    adapter = _ctrader_adapter()

    entry_deals = [(1001, 300_000), (1002, 300_000), (1003, 400_000)]
    cumulative = 0
    for index, (deal_id, volume) in enumerate(entry_deals):
        cumulative += volume
        event = _stream_event(
            signal_id=entry_signal,
            order_id=ENTRY_ORDER,
            deal_id=deal_id,
            volume=volume,
            cumulative=cumulative,
            execution_type=1 if index == len(entry_deals) - 1 else 2,
        )
        asyncio.run(adapter._persist_execution_event(event))
        asyncio.run(adapter._persist_execution_event(event))  # immediate redelivery

    close_deals = [(2001, 250_000), (2002, 250_000)]
    cumulative = 0
    for deal_id, volume in close_deals:
        cumulative += volume
        asyncio.run(
            adapter._persist_execution_event(
                _stream_event(
                    signal_id=close_signal,
                    order_id=CLOSE_ORDER,
                    deal_id=deal_id,
                    volume=volume,
                    cumulative=cumulative,
                    execution_type=2,
                )
            )
        )

    # A stale redelivery of the first entry deal, carrying the FINAL order snapshot.
    asyncio.run(
        adapter._persist_execution_event(
            _stream_event(
                signal_id=entry_signal,
                order_id=ENTRY_ORDER,
                deal_id=1001,
                volume=300_000,
                cumulative=1_000_000,
                execution_type=2,
            )
        )
    )

    # Reconciliation replays every deal with the order's final cumulative: nothing new.
    assert (
        _reconcile(
            adapter,
            entry_signal,
            ENTRY_ORDER,
            [_recovered_deal(d, ENTRY_ORDER, v) for d, v in entry_deals],
            1_000_000,
        )
        == 0
    )
    assert (
        _reconcile(
            adapter,
            close_signal,
            CLOSE_ORDER,
            [_recovered_deal(d, CLOSE_ORDER, v) for d, v in close_deals],
            500_000,
        )
        == 0
    )

    rows = _rows(database)
    assert sorted(row[0] for row in rows) == [
        "ctrader:deal:1001",
        "ctrader:deal:1002",
        "ctrader:deal:1003",
        "ctrader:deal:2001",
        "ctrader:deal:2002",
    ]
    assert database.get_live_signal_filled_qty(entry_signal) == pytest.approx(1000.0)
    assert database.get_live_signal_filled_qty(close_signal) == pytest.approx(500.0)


def test_c2_safe_a_capped_out_deal_is_healed_by_reconciliation_not_doubled(database) -> None:
    """SAFE: deal 2 arrives first (cumulative 600k), deal 1 is capped out; reconcile restores it."""
    signal = _seed_ctrader_signal(database, ENTRY_ORDER)
    adapter = _ctrader_adapter()

    asyncio.run(
        adapter._persist_execution_event(
            _stream_event(
                signal_id=signal, order_id=ENTRY_ORDER, deal_id=1002, volume=300_000,
                cumulative=600_000, execution_type=1,
            )
        )
    )
    asyncio.run(
        adapter._persist_execution_event(
            _stream_event(
                signal_id=signal, order_id=ENTRY_ORDER, deal_id=1001, volume=300_000,
                cumulative=300_000, execution_type=2,
            )
        )
    )
    assert database.get_live_signal_filled_qty(signal) == pytest.approx(300.0)

    recovered = _reconcile(
        adapter,
        signal,
        ENTRY_ORDER,
        [_recovered_deal(1001, ENTRY_ORDER, 300_000), _recovered_deal(1002, ENTRY_ORDER, 300_000)],
        600_000,
    )
    assert recovered == 1
    assert database.get_live_signal_filled_qty(signal) == pytest.approx(600.0)
    assert sorted(row[0] for row in _rows(database)) == ["ctrader:deal:1001", "ctrader:deal:1002"]


def test_c2_a_dedupe_key_owned_by_another_signal_is_never_amended(database, caplog) -> None:
    """A repeated key under a DIFFERENT signal is not a redelivery and must not be amended.

    `ctrader:deal:{id}` is globally unique in the table but carries no account. If the same key
    ever reaches a second signal, the amendment branch compared the second signal's cumulative
    against the first signal's row and grew the first signal's quantity. Refuse, keep the row
    untouched, and say so loudly because the second signal's fill has just been lost.
    """
    first = _seed_ctrader_signal(database, ENTRY_ORDER)
    second = _persist_signal(database, deployment_id=DEP_2)
    key = "ctrader:deal:5150"

    assert database.save_live_execution_up_to_cumulative(
        _execution(first, qty=1.0, dedupe_key=key, broker_fill_id=5150), 1.0
    ) == pytest.approx(1.0)

    with caplog.at_level(logging.ERROR, logger="shared.database"):
        grown = database.save_live_execution_up_to_cumulative(
            _execution(second, qty=3.0, dedupe_key=key, broker_fill_id=5150), 3.0
        )

    assert grown == 0.0
    assert _rows(database) == [(key, 5150, pytest.approx(1.0))]
    assert database.get_live_signal_filled_qty(first) == pytest.approx(1.0)
    assert database.get_live_signal_filled_qty(second) == 0.0
    assert any(
        key in record.getMessage() and record.levelno >= logging.ERROR for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_c2_plain_insert_with_a_key_owned_by_another_signal_is_reported(database, caplog) -> None:
    """Same collision through `save_live_execution`: skipped, but not silently."""
    first = _persist_signal(database)
    second = _persist_signal(database, deployment_id=DEP_2)
    key = "ctrader:deal:5151"

    assert database.save_live_execution(_execution(first, dedupe_key=key)) is True
    with caplog.at_level(logging.ERROR, logger="shared.database"):
        assert database.save_live_execution(_execution(second, dedupe_key=key)) is False

    assert len(_rows(database)) == 1
    assert any(
        key in record.getMessage() and record.levelno >= logging.ERROR for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_c2_safe_same_key_same_signal_is_still_a_plain_redelivery(database, caplog) -> None:
    """SAFE: the cross-signal guard must not turn ordinary redeliveries into errors."""
    signal_id = _persist_signal(database)
    key = "ctrader:deal:5152"
    assert database.save_live_execution(_execution(signal_id, dedupe_key=key)) is True
    with caplog.at_level(logging.ERROR, logger="shared.database"):
        assert database.save_live_execution(_execution(signal_id, dedupe_key=key)) is False
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_c18_replay_across_the_upgrade_dedupes_against_a_pre_canonical_key(database) -> None:
    """A row keyed on the verbatim `...Z` spelling (pre-#127) must still catch its replay.

    Producers replay recent signals on restart, and a deploy is a restart, so the first replay
    after the upgrade is the likeliest one. Without the legacy fallback the canonical key would
    miss the old row and the replay would place a second broker order.
    """
    stamp = "2026-07-15T10:00:00Z"
    replay = _live_signal(stamp=stamp)
    legacy_key = replay.metadata["legacy_idempotency_key"]
    assert legacy_key != replay.metadata["idempotency_key"]

    # Simulate the row a pre-#127 daemon stored: keyed on the verbatim spelling only.
    pre_upgrade = _live_signal(stamp=stamp)
    pre_upgrade.metadata["idempotency_key"] = legacy_key
    pre_upgrade.metadata.pop("legacy_idempotency_key")
    stored_id, created = database.save_live_signal(pre_upgrade)
    assert created is True

    replay_id, replay_created = database.save_live_signal(replay)

    assert replay_created is False
    assert replay_id == stored_id


def test_c18_canonical_stamp_carries_no_legacy_key() -> None:
    """An already-canonical stamp keyed the same before and after; no fallback is needed."""
    live = _live_signal(stamp="2026-07-15T10:00:00+00:00")
    assert "legacy_idempotency_key" not in live.metadata
