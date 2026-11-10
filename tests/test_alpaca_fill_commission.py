"""#124: Alpaca fills must never invent a price or a commission.

Three defects, one rule - a number the broker did not state is not stored as if it had:

1. ``_reconcile_signal_fill`` persisted a backfilled fill at ``filled_avg_price`` defaulting to
   ``0.0`` when the broker omitted it, so a fill with no price became a fill at price zero.
2. ``filled_avg_price`` is the CUMULATIVE average over every fill of the order, but it was stored
   as the price of only the missing remainder. With part of the order already recorded at a
   different price, the remainder got the wrong price. The remainder price is
   ``(avg * broker_qty - sum(local price * qty)) / missing_qty``.
3. Both the reconcile path and the stream path stored ``commission=0.0``. Equities are
   commission-free on Alpaca, but crypto fills carry a fee (posted end of day as a ``CFEE``
   account activity, in the received asset). Maintainer decision: an unknown commission is
   ``NULL``, ``0.0`` is reserved for a known zero (equities), and a later backfill pulls the
   ``CFEE`` activities, converts them to USD and updates the executions. Consumers treat a
   ``NULL`` commission as "net PnL incomplete", never as zero.

No real Alpaca call is made: the fake client returns payloads shaped like the documented
``GET /v2/account/activities/{activity_type}`` responses (https://docs.alpaca.markets/docs/
crypto-fees and the Trading API account-activities reference).
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import text

from shared.models import (
    PRICE_ABSENT,
    PRICE_DERIVED,
    PRICE_DERIVED_UNAVAILABLE,
    PRICE_OBSERVED,
    PRICE_SENT_BAD,
    LiveExecution,
    LiveSignal,
)

ACCOUNT = "alpaca-acc-124"
DEPLOYMENT = "dep-alpaca-124"
OTHER_DEPLOYMENT = "dep-alpaca-124-b"


# ── fixtures ────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(isolated_db):
    database, engine, _data_dir = isolated_db
    database.init_db()
    with database.get_session() as session:
        session.add(database.LiveAccountModel(id=ACCOUNT, broker="alpaca", name=ACCOUNT))
        session.flush()
        for deployment_id in (DEPLOYMENT, OTHER_DEPLOYMENT):
            session.add(
                database.LiveDeploymentModel(
                    id=deployment_id,
                    strategy_name=f"strategy-{deployment_id}",
                    account_id=ACCOUNT,
                    symbols_json=[],
                    timeframe="1m",
                    status="ACTIVE",
                )
            )
        session.commit()
    return database


@pytest.fixture
def live_runner_logs(caplog):
    """The `live-runner` logger does not propagate, so attach caplog's handler to it directly."""
    target = logging.getLogger("trading.live-runner")
    target.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="trading.live-runner")
    try:
        yield caplog
    finally:
        target.removeHandler(caplog.handler)


def _order_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"alpaca-124:{label}"))


def _signal(database, *, symbol: str = "AAPL", action: str = "buy", deployment: str = DEPLOYMENT):
    signal = LiveSignal(
        deployment_id=deployment,
        symbol=symbol,
        action=action,
        theoretical_price=100.0,
        timestamp=datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)
    return signal


def _local_fill(
    database,
    signal: LiveSignal,
    *,
    price: float,
    qty: float,
    order_id: str,
    key: str,
    commission: float | None = 0.0,
    observed: str | None = PRICE_OBSERVED,
    timestamp: datetime | None = None,
) -> None:
    assert database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=price,
            filled_qty=qty,
            commission=commission,
            timestamp=timestamp or datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
            broker_order_id=order_id,
            dedupe_key=key,
            actual_price_observed=observed,
        )
    )


def _rows(database, signal: LiveSignal) -> list[Any]:
    with database.get_session() as session:
        return list(
            session.exec(
                database.select(database.LiveExecutionModel)
                .where(database.LiveExecutionModel.signal_id == str(signal.id))
                .order_by(database.LiveExecutionModel.timestamp, database.LiveExecutionModel.id)
            ).all()
        )


def _adapter(order: Any | None = None, *, client: Any | None = None):
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True, account_id=ACCOUNT)
    if client is None:
        client = SimpleNamespace(get_order_by_client_id=lambda _client_order_id: order)
    adapter._trading_client = client
    adapter._connected = True
    return adapter


def _order(
    signal: LiveSignal,
    *,
    filled_qty: str,
    filled_avg_price: Any,
    order_id: str,
    asset_class: Any = "us_equity",
    symbol: str = "AAPL",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        client_order_id=str(signal.id),
        symbol=symbol,
        asset_class=asset_class,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        filled_at=datetime(2026, 11, 3, 15, 5, tzinfo=timezone.utc),
    )


# ── reconcile: price ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_avg", [None, "", "0", "0.0", "-1.5", "nan", "inf", "garbage"])
def test_reconcile_missing_or_unusable_average_price_persists_nothing(
    db, live_runner_logs, bad_avg
):
    signal = _signal(db)
    order = _order(signal, filled_qty="5", filled_avg_price=bad_avg, order_id=_order_id("bad"))

    recovered = asyncio.run(_adapter(order).reconcile_executions())

    assert recovered == 0
    assert _rows(db, signal) == [], "a fill without a usable broker price must not be stored"
    assert any("filled_avg_price" in record.getMessage() for record in live_runner_logs.records), (
        "the refusal must be logged so an operator can see why the fill is not recorded"
    )


def test_reconcile_retries_on_the_next_pass_once_the_price_arrives(db):
    signal = _signal(db)
    order = _order(signal, filled_qty="5", filled_avg_price=None, order_id=_order_id("retry"))
    adapter = _adapter(order)

    assert asyncio.run(adapter.reconcile_executions()) == 0
    order.filled_avg_price = "101.5"
    assert asyncio.run(adapter.reconcile_executions()) == 1

    rows = _rows(db, signal)
    assert len(rows) == 1
    assert rows[0].filled_qty == pytest.approx(5.0)
    assert rows[0].actual_price == pytest.approx(101.5)
    # From the order's average, not stated for this fill: never `observed` (review of #143).
    assert rows[0].actual_price_observed == PRICE_DERIVED


def test_reconcile_remainder_gets_the_remainder_price_not_the_cumulative_average(db):
    signal = _signal(db)
    order_id = _order_id("remainder")
    _local_fill(db, signal, price=100.0, qty=2.0, order_id=order_id, key="alpaca:exec:r-1")
    # Broker: 5 filled at a cumulative average of 102 -> 510 total notional. 200 of that is the
    # local leg, so the missing 3 were filled at (510 - 200) / 3.
    order = _order(signal, filled_qty="5", filled_avg_price="102", order_id=order_id)

    assert asyncio.run(_adapter(order).reconcile_executions()) == 1

    rows = _rows(db, signal)
    assert [row.filled_qty for row in rows] == [pytest.approx(2.0), pytest.approx(3.0)]
    remainder = rows[1]
    assert remainder.actual_price == pytest.approx((5 * 102.0 - 2 * 100.0) / 3)
    assert remainder.actual_price != pytest.approx(102.0)
    assert remainder.actual_price_observed == PRICE_DERIVED
    total_notional = sum(row.actual_price * row.filled_qty for row in rows)
    assert total_notional == pytest.approx(5 * 102.0), "rows must add up to the broker's average"


def test_reconcile_remainder_sums_over_several_local_legs(db):
    signal = _signal(db)
    order_id = _order_id("remainder-legs")
    _local_fill(db, signal, price=100.0, qty=1.0, order_id=order_id, key="alpaca:exec:l-1")
    _local_fill(db, signal, price=101.0, qty=1.5, order_id=order_id, key="alpaca:exec:l-2")
    order = _order(signal, filled_qty="4", filled_avg_price="101.25", order_id=order_id)

    assert asyncio.run(_adapter(order).reconcile_executions()) == 1

    remainder = _rows(db, signal)[-1]
    assert remainder.filled_qty == pytest.approx(1.5)
    assert remainder.actual_price == pytest.approx((4 * 101.25 - 100.0 - 151.5) / 1.5)
    assert remainder.actual_price_observed == PRICE_DERIVED


@pytest.mark.parametrize(
    ("local_price", "local_qty", "broker_avg", "broker_qty"),
    [
        (130.0, 4.0, "100", "5"),  # (500 - 520) / 1 < 0
        (125.0, 4.0, "100", "5"),  # (500 - 500) / 1 == 0
    ],
)
def test_reconcile_rejects_a_non_positive_remainder_price(
    db, live_runner_logs, local_price, local_qty, broker_avg, broker_qty
):
    """The price is refused, the QUANTITY is not: the ledger needs it (review of #143)."""
    signal = _signal(db)
    order_id = _order_id(f"negative-{local_price}")
    _local_fill(
        db, signal, price=local_price, qty=local_qty, order_id=order_id, key="alpaca:exec:n"
    )
    order = _order(signal, filled_qty=broker_qty, filled_avg_price=broker_avg, order_id=order_id)

    assert asyncio.run(_adapter(order).reconcile_executions()) == 1

    rows = _rows(db, signal)
    assert len(rows) == 2
    assert rows[0].filled_qty == pytest.approx(local_qty)
    remainder = rows[1]
    assert remainder.filled_qty == pytest.approx(float(broker_qty) - local_qty)
    assert remainder.actual_price == 0.0, "an impossible remainder price must not be persisted"
    assert remainder.actual_price_observed == PRICE_DERIVED_UNAVAILABLE


@pytest.mark.parametrize("observed", [PRICE_SENT_BAD, PRICE_ABSENT])
def test_reconcile_refuses_to_derive_a_remainder_from_an_unpriced_local_leg(db, observed):
    """A local leg stored at 0.0 because its price was not observed poisons the formula.

    The remainder's price is therefore unavailable, but its quantity is still recorded: exit
    sizing and duplicate-order protection read filled quantity (review of #143).
    """
    signal = _signal(db)
    order_id = _order_id(f"unpriced-{observed}")
    _local_fill(
        db, signal, price=0.0, qty=2.0, order_id=order_id, key="alpaca:exec:u", observed=observed
    )
    order = _order(signal, filled_qty="5", filled_avg_price="102", order_id=order_id)

    assert asyncio.run(_adapter(order).reconcile_executions()) == 1
    rows = _rows(db, signal)
    assert [row.filled_qty for row in rows] == [pytest.approx(2.0), pytest.approx(3.0)]
    assert rows[1].actual_price == 0.0
    assert rows[1].actual_price_observed == PRICE_DERIVED_UNAVAILABLE


def test_remainder_price_is_derived_inside_the_save_transaction(db):
    """A stream fill landing between the adapter's read and the save must not skew the price.

    The adapter saw 0 local and asked for the whole 5; meanwhile 3 arrived at 100. The save may
    only persist the 2 that are still missing, and their price is what the broker's average says
    THOSE 2 cost - not the average itself.
    """
    signal = _signal(db)
    order_id = _order_id("race")
    _local_fill(db, signal, price=100.0, qty=3.0, order_id=order_id, key="alpaca:exec:race")
    stale = LiveExecution(
        signal_id=signal.id,
        actual_price=102.0,
        filled_qty=5.0,
        commission=0.0,
        timestamp=datetime(2026, 11, 3, 15, 5, tzinfo=timezone.utc),
        broker_order_id=order_id,
        dedupe_key=f"alpaca:order:{order_id}:reconciled:5.0",
    )

    persisted = db.save_live_execution_up_to_cumulative(stale, 5.0, cumulative_avg_price=102.0)

    assert persisted == pytest.approx(2.0)
    remainder = _rows(db, signal)[-1]
    assert remainder.filled_qty == pytest.approx(2.0)
    assert remainder.actual_price == pytest.approx((510.0 - 300.0) / 2.0)


def test_cumulative_save_without_an_average_keeps_the_callers_price(db):
    """The new derivation is opt-in: the stream path still stores its per-event price."""
    signal = _signal(db)
    order_id = _order_id("opt-in")
    _local_fill(db, signal, price=100.0, qty=3.0, order_id=order_id, key="alpaca:exec:opt-1")
    event = LiveExecution(
        signal_id=signal.id,
        actual_price=99.0,
        filled_qty=2.0,
        commission=0.0,
        timestamp=datetime(2026, 11, 3, 15, 5, tzinfo=timezone.utc),
        broker_order_id=order_id,
        dedupe_key="alpaca:exec:opt-2",
    )

    assert db.save_live_execution_up_to_cumulative(event, 5.0) == pytest.approx(2.0)
    assert _rows(db, signal)[-1].actual_price == pytest.approx(99.0)


# ── reconcile + stream: commission ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("asset_class", "symbol", "expected"),
    [
        ("us_equity", "AAPL", 0.0),
        (SimpleNamespace(value="us_equity"), "AAPL", 0.0),  # enum-shaped, as alpaca-py sends it
        ("crypto", "BTC/USD", None),
        (SimpleNamespace(value="crypto"), "ETH/USD", None),
        ("us_option", "AAPL261120C00200000", None),
        (None, "AAPL", None),  # unknown asset class: unknown commission, not zero
    ],
)
def test_reconciled_commission_is_zero_only_for_equities(db, asset_class, symbol, expected):
    signal = _signal(db, symbol=symbol)
    order = _order(
        signal,
        filled_qty="1",
        filled_avg_price="100",
        order_id=_order_id(f"commission-{symbol}-{asset_class!r}"),
        asset_class=asset_class,
        symbol=symbol,
    )

    assert asyncio.run(_adapter(order).reconcile_executions()) == 1

    rows = _rows(db, signal)
    assert len(rows) == 1
    if expected is None:
        assert rows[0].commission is None, "an unknown commission must be NULL, never 0.0"
    else:
        assert rows[0].commission == expected


def _trade_update(
    *,
    signal_id: str,
    order_id: str,
    asset_class: Any,
    symbol: str,
    price: Any = "60000",
    qty: str = "0.5",
    event: str = "fill",
) -> SimpleNamespace:
    """Shaped like alpaca-py 0.44 `TradeUpdate`: no commission field anywhere."""
    return SimpleNamespace(
        event=event,
        execution_id=str(uuid4()),
        price=price,
        qty=qty,
        position_qty=qty,
        timestamp=datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
        order=SimpleNamespace(
            id=order_id,
            client_order_id=signal_id,
            symbol=symbol,
            asset_class=asset_class,
            filled_qty=qty,
            filled_avg_price=price,
        ),
    )


def _stream_capture(monkeypatch):
    from live_runner import alpaca_adapter as module

    saved: list[LiveExecution] = []
    monkeypatch.setattr(
        module,
        "save_live_execution_up_to_cumulative",
        lambda execution, _cumulative, **_kwargs: saved.append(execution) or execution.filled_qty,
    )
    monkeypatch.setattr(
        module, "save_live_execution", lambda execution: saved.append(execution) or True
    )
    return saved


@pytest.mark.parametrize(
    ("asset_class", "symbol", "expected"),
    [
        ("crypto", "BTC/USD", None),
        (SimpleNamespace(value="crypto"), "BTC/USD", None),
        ("us_equity", "AAPL", 0.0),
        (None, "AAPL", None),
    ],
)
def test_stream_fill_commission_is_unknown_unless_known_zero(
    monkeypatch, asset_class, symbol, expected
):
    saved = _stream_capture(monkeypatch)
    update = _trade_update(
        signal_id=str(uuid4()), order_id=_order_id("stream"), asset_class=asset_class, symbol=symbol
    )

    asyncio.run(_adapter(None)._on_trade_update(update))

    assert len(saved) == 1
    assert saved[0].commission == expected


@pytest.mark.parametrize(
    ("price", "stored", "verdict"),
    [
        ("60000", 60000.0, PRICE_OBSERVED),
        (None, 0.0, PRICE_ABSENT),
        ("0", 0.0, PRICE_SENT_BAD),
        ("-3", 0.0, PRICE_SENT_BAD),
        ("nan", 0.0, PRICE_SENT_BAD),
        ("garbage", 0.0, PRICE_SENT_BAD),
    ],
)
def test_stream_fill_records_whether_its_price_was_observed(monkeypatch, price, stored, verdict):
    saved = _stream_capture(monkeypatch)
    update = _trade_update(
        signal_id=str(uuid4()),
        order_id=_order_id("stream-price"),
        asset_class="us_equity",
        symbol="AAPL",
        price=price,
    )
    # No fallback price on the order either, or the stream would read that.
    update.order.filled_avg_price = None

    asyncio.run(_adapter(None)._on_trade_update(update))

    assert len(saved) == 1, "the stream path still records the quantity"
    assert saved[0].actual_price == stored
    assert math.isfinite(saved[0].actual_price)
    assert saved[0].actual_price_observed == verdict


# ── schema, model, save paths ──────────────────────────────────────────────────────────────────


def test_execution_model_round_trips_an_unknown_commission(db):
    signal = _signal(db, symbol="BTC/USD")
    _local_fill(
        db,
        signal,
        price=60000.0,
        qty=0.5,
        order_id=_order_id("null"),
        key="k:null",
        commission=None,
    )

    row = _rows(db, signal)[0]
    assert row.commission is None
    assert row.to_live_execution().commission is None


def test_fresh_schema_commission_column_is_nullable(db):
    with db.get_engine().connect() as connection:
        columns = {
            row[1]: row for row in connection.execute(text("PRAGMA table_info(live_executions)"))
        }
    assert columns["commission"][3] == 0, "commission must accept NULL (unknown)"


def test_partial_persist_keeps_an_unknown_commission_unknown(db):
    signal = _signal(db, symbol="BTC/USD")
    order_id = _order_id("partial-null")
    _local_fill(db, signal, price=60000.0, qty=0.2, order_id=order_id, key="k:p1", commission=None)
    execution = LiveExecution(
        signal_id=signal.id,
        actual_price=60000.0,
        filled_qty=0.5,
        commission=None,
        timestamp=datetime(2026, 11, 3, 15, 1, tzinfo=timezone.utc),
        broker_order_id=order_id,
        dedupe_key="k:p2",
    )

    assert db.save_live_execution_up_to_cumulative(execution, 0.5) == pytest.approx(0.3)
    assert [row.commission for row in _rows(db, signal)] == [None, None]


def test_amending_a_known_commission_with_an_unknown_slice_makes_it_unknown(db):
    """2.0 known + an unknown slice is not 2.0: the total is unknown."""
    signal = _signal(db, symbol="BTC/USD")
    order_id = _order_id("amend-null")
    first = LiveExecution(
        signal_id=signal.id,
        actual_price=60000.0,
        filled_qty=1.0,
        commission=2.0,
        timestamp=datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
        broker_order_id=order_id,
        dedupe_key="k:amend",
    )
    assert db.save_live_execution_up_to_cumulative(first, 3.0) == pytest.approx(1.0)
    grown = first.model_copy(update={"filled_qty": 3.0, "commission": None})

    assert db.save_live_execution_up_to_cumulative(grown, 3.0) == pytest.approx(2.0)
    rows = _rows(db, signal)
    assert len(rows) == 1
    assert rows[0].filled_qty == pytest.approx(3.0)
    assert rows[0].commission is None


_LEGACY_LIVE_EXECUTIONS_DDL = """
CREATE TABLE live_executions (
	id VARCHAR NOT NULL,
	signal_id VARCHAR NOT NULL,
	actual_price FLOAT NOT NULL,
	filled_qty FLOAT NOT NULL,
	commission FLOAT NOT NULL,
	timestamp DATETIME NOT NULL,
	broker_order_id VARCHAR NOT NULL,
	dedupe_key VARCHAR,
	broker_fill_id INTEGER,
	actual_price_observed VARCHAR,
	PRIMARY KEY (id),
	FOREIGN KEY(signal_id) REFERENCES live_signals (id)
)
"""


def _build_legacy_database(isolated_db_no_schema):
    database, engine, _data_dir = isolated_db_no_schema
    database.SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE live_executions"))
        connection.execute(text(_LEGACY_LIVE_EXECUTIONS_DDL))
        connection.execute(
            text("CREATE INDEX ix_live_executions_signal_id ON live_executions (signal_id)")
        )
        connection.execute(
            text("CREATE INDEX ix_live_executions_timestamp ON live_executions (timestamp)")
        )
        connection.execute(
            text(
                "CREATE INDEX ix_live_executions_broker_order_id "
                "ON live_executions (broker_order_id)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX uq_live_executions_dedupe_key "
                "ON live_executions(dedupe_key) WHERE dedupe_key IS NOT NULL"
            )
        )
        for account_id, broker in (("acc-alp", "alpaca"), ("acc-ct", "ctrader")):
            connection.execute(
                text(
                    "INSERT INTO live_accounts (id, broker, name, is_demo) "
                    "VALUES (:id, :broker, :id, 0)"
                ),
                {"id": account_id, "broker": broker},
            )
            connection.execute(
                text(
                    "INSERT INTO live_deployments (id, strategy_name, account_id, symbols, "
                    "timeframe, status, created_at, updated_at) VALUES "
                    "(:id, :id, :account, '[]', '1m', 'ACTIVE', "
                    "'2026-11-01 00:00:00', '2026-11-01 00:00:00')"
                ),
                {"id": f"dep-{account_id}", "account": account_id},
            )
        signals = {
            "sig-crypto": ("dep-acc-alp", "BTC/USD"),
            "sig-equity": ("dep-acc-alp", "AAPL"),
            "sig-ctrader": ("dep-acc-ct", "XAU/USD"),
        }
        for signal_id, (deployment_id, symbol) in signals.items():
            connection.execute(
                text(
                    "INSERT INTO live_signals (id, deployment_id, symbol, action, "
                    "theoretical_price, timestamp, metadata, source) VALUES "
                    "(:id, :dep, :symbol, 'buy', 1.0, '2026-11-03 15:00:00', '{}', 'internal')"
                ),
                {"id": signal_id, "dep": deployment_id, "symbol": symbol},
            )
        executions = [
            ("ex-crypto", "sig-crypto", 0.0, "alpaca:exec:c"),
            ("ex-equity", "sig-equity", 0.0, "alpaca:exec:e"),
            ("ex-ctrader", "sig-ctrader", 1.5, "ctrader:deal:1"),
            ("ex-ctrader-zero", "sig-ctrader", 0.0, "ctrader:deal:2"),
        ]
        for execution_id, signal_id, commission, key in executions:
            connection.execute(
                text(
                    "INSERT INTO live_executions (id, signal_id, actual_price, filled_qty, "
                    "commission, timestamp, broker_order_id, dedupe_key) VALUES "
                    "(:id, :sig, 10.0, 1.0, :commission, '2026-11-03 15:00:00', 'bo', :key)"
                ),
                {"id": execution_id, "sig": signal_id, "commission": commission, "key": key},
            )
    return database, engine


def test_legacy_not_null_commission_column_is_migrated_to_nullable(isolated_db_no_schema):
    database, engine = _build_legacy_database(isolated_db_no_schema)

    database.init_db()

    with engine.connect() as connection:
        columns = {
            row[1]: row for row in connection.execute(text("PRAGMA table_info(live_executions)"))
        }
        assert columns["commission"][3] == 0
        assert columns["actual_price"][3] == 1, "only commission's constraint is relaxed"
        commissions = dict(
            connection.execute(text("SELECT id, commission FROM live_executions")).fetchall()
        )
        foreign_keys = connection.execute(
            text("PRAGMA foreign_key_list(live_executions)")
        ).fetchall()
        index_names = {
            row[1] for row in connection.execute(text("PRAGMA index_list(live_executions)"))
        }
    assert set(commissions) == {"ex-crypto", "ex-equity", "ex-ctrader", "ex-ctrader-zero"}
    # The 0.0 on an Alpaca crypto row was always the hard-coded fallback, never a stated fee.
    assert commissions["ex-crypto"] is None
    assert commissions["ex-equity"] == 0.0, "an equity 0.0 is a known zero and stays"
    assert commissions["ex-ctrader"] == 1.5
    assert commissions["ex-ctrader-zero"] == 0.0, "other brokers' rows are not touched"
    assert any(row[3] == "signal_id" and row[2] == "live_signals" for row in foreign_keys)
    assert {
        "uq_live_executions_dedupe_key",
        "ix_live_executions_signal_id",
        "ix_live_executions_timestamp",
        "ix_live_executions_broker_order_id",
    } <= index_names
    with pytest.raises(Exception):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO live_executions (id, signal_id, actual_price, filled_qty, "
                    "commission, timestamp, broker_order_id, dedupe_key) VALUES "
                    "('dup', 'sig-equity', 1, 1, NULL, '2026-11-03', 'bo', 'alpaca:exec:e')"
                )
            )


def test_commission_nullable_migration_is_idempotent(isolated_db_no_schema):
    database, engine = _build_legacy_database(isolated_db_no_schema)
    database.init_db()
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE live_executions SET commission = 0.0 WHERE id = 'ex-crypto'")
        )

    with engine.begin() as connection:
        assert database._make_live_executions_commission_nullable(connection) is False

    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT commission FROM live_executions WHERE id = 'ex-crypto'")
        ).scalar_one()
    assert value == 0.0, "a second run must not rewrite data again"


# ── consumers ──────────────────────────────────────────────────────────────────────────────────


def test_execution_listings_report_unknown_commission_as_none(db):
    signal = _signal(db, symbol="BTC/USD")
    _local_fill(
        db,
        signal,
        price=60000.0,
        qty=0.5,
        order_id=_order_id("list"),
        key="k:list",
        commission=None,
    )

    listed = db.list_live_signal_executions_for_deployments([DEPLOYMENT])
    _balances, stats_rows = db.load_live_account_stats_inputs(ACCOUNT)
    signals = db.list_live_signals_for_deployments([DEPLOYMENT])

    assert [row["commission"] for row in listed] == [None]
    assert [row["commission"] for row in stats_rows] == [None]
    assert [row["commission"] for row in signals] == [None]


def test_viewer_live_trades_keep_unknown_commission_unknown(monkeypatch):
    import viewer.main as viewer_main

    row = {
        "signal_id": "s",
        "execution_id": "e",
        "deployment_id": DEPLOYMENT,
        "symbol": "BTC/USD",
        "action": "buy",
        "theoretical_price": 1.0,
        "actual_price": 1.0,
        "slippage": 0.0,
        "slippage_bps": 0.0,
        "filled_qty": 1.0,
        "commission": None,
        "signal_timestamp": datetime(2026, 11, 3, tzinfo=timezone.utc),
        "execution_timestamp": datetime(2026, 11, 3, tzinfo=timezone.utc),
        "broker_order_id": "bo",
        "signal_metadata": {},
    }
    monkeypatch.setattr(viewer_main, "list_live_deployments_for_account", lambda _a: [])
    monkeypatch.setattr(
        viewer_main, "list_live_signal_executions_for_deployments", lambda _ids: [row]
    )

    payload = asyncio.run(viewer_main._live_trades_payload(ACCOUNT))

    assert payload["trades"][0]["commission"] is None


def _stats_row(action: str, price: float, qty: float, minute: int, commission: float | None):
    return {
        "symbol": "BTC/USD",
        "action": action,
        "actual_price": price,
        "filled_qty": qty,
        "commission": commission,
        "execution_timestamp": datetime(2026, 11, 3, 10, minute, tzinfo=timezone.utc),
    }


def test_viewer_stats_net_pnl_is_incomplete_when_any_commission_is_unknown(monkeypatch):
    import viewer.main as viewer_main

    rows = [_stats_row("buy", 100.0, 1.0, 0, 0.5), _stats_row("sell", 110.0, 1.0, 5, None)]
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["total_pnl"] == pytest.approx(10.0)
    assert payload["net_pnl_complete"] is False
    assert payload["net_pnl"] is None, "an unknown fee must not be netted as zero"
    assert payload["commission_unknown_count"] == 1
    assert payload["total_commission"] is None


def test_viewer_stats_net_pnl_subtracts_known_commissions(monkeypatch):
    import viewer.main as viewer_main

    rows = [_stats_row("buy", 100.0, 1.0, 0, 0.5), _stats_row("sell", 110.0, 1.0, 5, 0.25)]
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["net_pnl_complete"] is True
    assert payload["commission_unknown_count"] == 0
    assert payload["total_commission"] == pytest.approx(0.75)
    assert payload["net_pnl"] == pytest.approx(10.0 - 0.75)


# ── CFEE backfill ──────────────────────────────────────────────────────────────────────────────

TRADE_DAY = "2026-11-03"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _parse_stamp(value: str) -> datetime:
    if len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeActivitiesClient:
    """`GET /v2/account/activities/{activity_type}`, as documented.

    Server-side: ``after``/``until`` filter on the activity's creation time (a trade's
    ``transaction_time``; a non-trade activity's ``created_at``, else its ``date``),
    ``direction`` orders by id, ``page_size`` caps at 100 and ``page_token`` is the id of the
    last item of the previous page.
    """

    def __init__(self, *, fills=(), cfees=(), fees=(), fail: bool = False):
        self.items = {"FILL": list(fills), "CFEE": list(cfees), "FEE": list(fees)}
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_order_by_client_id(self, _client_order_id):  # pragma: no cover - unused here
        raise AssertionError("the backfill must not look orders up one by one")

    def get(self, path: str, data: dict[str, Any] | None = None):
        params = dict(data or {})
        self.calls.append((path, params))
        if self.fail:
            raise RuntimeError("503 Service Unavailable")
        prefix = "/account/activities/"
        assert path.startswith(prefix), path
        items = self.items[path[len(prefix) :]]

        def stamp(item):
            return _parse_stamp(
                item.get("transaction_time") or item.get("created_at") or item["date"]
            )

        selected = [
            item
            for item in items
            if ("after" not in params or stamp(item) > _parse_stamp(params["after"]))
            and ("until" not in params or stamp(item) < _parse_stamp(params["until"]))
        ]
        selected.sort(key=lambda item: item["id"], reverse=params.get("direction") != "asc")
        token = params.get("page_token")
        if token:
            ids = [item["id"] for item in selected]
            selected = selected[ids.index(token) + 1 :]
        page_size = min(int(params.get("page_size", 100)), 100)
        return [dict(item) for item in selected[:page_size]]


def _et_id(moment: datetime) -> str:
    """Alpaca activity ids carry the America/New_York wall-clock time (see the docs' examples)."""
    from zoneinfo import ZoneInfo

    local = moment.astimezone(ZoneInfo("America/New_York"))
    return f"{local:%Y%m%d%H%M%S}{local.microsecond // 1000:03d}::{uuid4()}"


def _fill_activity(
    *, order_id: str, side: str, qty: str, price: str, at: datetime, symbol: str = "BTC/USD"
) -> dict[str, Any]:
    return {
        "activity_type": "FILL",
        "cum_qty": qty,
        "id": _et_id(at),
        "leaves_qty": "0",
        "order_id": order_id,
        "order_status": "filled",
        "price": price,
        "qty": qty,
        "side": side,
        "symbol": symbol,
        "transaction_time": at.isoformat().replace("+00:00", "Z"),
        "type": "fill",
    }


def _cfee(
    *,
    qty: str = "0",
    net_amount: str = "0",
    day: str = TRADE_DAY,
    symbol: str = "BTCUSD",
    status: str = "executed",
    with_created_at: bool = True,
    description: str = "Coin Pair Transaction Fee (Non USD)",
) -> dict[str, Any]:
    entry = {
        "id": f"{day.replace('-', '')}000000000::{uuid4()}",
        "activity_type": "CFEE",
        "date": day,
        "net_amount": net_amount,
        "description": description,
        "symbol": symbol,
        "qty": qty,
        "price": "60100",
        "status": status,
    }
    if with_created_at:
        entry["created_at"] = (
            f"{(datetime.fromisoformat(day) + timedelta(days=1)):%Y-%m-%d}T05:00:00Z"
        )
    return entry


FILL_AT = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)  # 10:00 ET, same day either way


def _crypto_fill(
    database,
    *,
    action: str = "buy",
    qty: float = 0.5,
    price: float = 60000.0,
    label: str = "btc",
    at: datetime = FILL_AT,
    symbol: str = "BTC/USD",
    deployment: str = DEPLOYMENT,
    commission: float | None = None,
) -> tuple[LiveSignal, str]:
    signal = _signal(database, symbol=symbol, action=action, deployment=deployment)
    order_id = _order_id(label)
    _local_fill(
        database,
        signal,
        price=price,
        qty=qty,
        order_id=order_id,
        key=f"alpaca:exec:{label}",
        commission=commission,
        timestamp=at,
    )
    return signal, order_id


def _commission(database, signal: LiveSignal) -> list[float | None]:
    return [row.commission for row in _rows(database, signal)]


def _backfill(client, now: datetime = NOW) -> int:
    return asyncio.run(_adapter(client=client).backfill_commissions(now=now))


def test_backfill_converts_a_base_asset_fee_on_a_buy_to_usd(db):
    signal, order_id = _crypto_fill(db)
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 1

    # 0.00125 BTC received-asset fee, valued at the price the BTC was bought at.
    assert _commission(db, signal) == [pytest.approx(0.00125 * 60000.0)]
    paths = {path for path, _params in client.calls}
    assert {"/account/activities/FILL", "/account/activities/CFEE"} <= paths
    for _path, params in client.calls:
        assert params.get("direction") == "asc"
        assert int(params.get("page_size")) == 100


def test_backfill_uses_the_usd_fee_on_a_sell(db):
    signal, order_id = _crypto_fill(db, action="sell", label="btc-sell")
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=order_id, side="sell", qty="0.5", price="60000", at=FILL_AT)
        ],
        cfees=[_cfee(net_amount="-75.0", description="Coin Pair Transaction Fee (USD)")],
    )

    assert _backfill(client) == 1
    assert _commission(db, signal) == [pytest.approx(75.0)]


def test_backfill_allocates_an_aggregated_day_fee_by_received_quantity(db):
    """One CFEE for the day and pair, two orders: each order carries its share of the BTC fee."""
    first, first_order = _crypto_fill(db, qty=0.2, price=60000.0, label="agg-1")
    second_at = FILL_AT + timedelta(hours=2)
    second, second_order = _crypto_fill(db, qty=0.3, price=61000.0, label="agg-2", at=second_at)
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=first_order, side="buy", qty="0.2", price="60000", at=FILL_AT),
            _fill_activity(
                order_id=second_order, side="buy", qty="0.3", price="61000", at=second_at
            ),
        ],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 2

    assert _commission(db, first) == [pytest.approx(0.00125 * 0.2 / 0.5 * 60000.0)]
    assert _commission(db, second) == [pytest.approx(0.00125 * 0.3 / 0.5 * 61000.0)]


def test_backfill_sums_per_trade_fee_entries_for_the_day(db):
    """Whether Alpaca posts one CFEE per trade or one per day, the day's total is the same."""
    first, first_order = _crypto_fill(db, qty=0.2, label="per-1")
    second_at = FILL_AT + timedelta(hours=1)
    second, second_order = _crypto_fill(db, qty=0.3, label="per-2", at=second_at)
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=first_order, side="buy", qty="0.2", price="60000", at=FILL_AT),
            _fill_activity(
                order_id=second_order, side="buy", qty="0.3", price="60000", at=second_at
            ),
        ],
        cfees=[_cfee(qty="-0.0005"), _cfee(qty="-0.00075", with_created_at=False)],
    )

    assert _backfill(client) == 2
    assert _commission(db, first) == [pytest.approx(0.0005 * 60000.0)]
    assert _commission(db, second) == [pytest.approx(0.00075 * 60000.0)]


def test_backfill_splits_an_order_across_its_local_legs(db):
    signal = _signal(db, symbol="BTC/USD")
    order_id = _order_id("legs")
    _local_fill(
        db, signal, price=60000.0, qty=0.1, order_id=order_id, key="k:leg1", commission=None
    )
    _local_fill(
        db,
        signal,
        price=60000.0,
        qty=0.4,
        order_id=order_id,
        key="k:leg2",
        commission=None,
        timestamp=FILL_AT + timedelta(minutes=1),
    )
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=order_id, side="buy", qty="0.1", price="60000", at=FILL_AT),
            _fill_activity(
                order_id=order_id,
                side="buy",
                qty="0.4",
                price="60000",
                at=FILL_AT + timedelta(minutes=1),
            ),
        ],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 2
    assert _commission(db, signal) == [
        pytest.approx(0.00125 * 0.1 / 0.5 * 60000.0),
        pytest.approx(0.00125 * 0.4 / 0.5 * 60000.0),
    ]


def test_backfill_follows_pagination(db):
    signal, order_id = _crypto_fill(db, label="paged", at=FILL_AT + timedelta(hours=3))
    foreign = [
        _fill_activity(
            order_id=_order_id(f"eth-{index}"),
            side="buy",
            qty="1",
            price="3000",
            at=FILL_AT + timedelta(seconds=index),
            symbol="ETH/USD",
        )
        for index in range(120)
    ]
    ours = _fill_activity(
        order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT + timedelta(hours=3)
    )
    client = FakeActivitiesClient(fills=[*foreign, ours], cfees=[_cfee(qty="-0.00125")])

    assert _backfill(client) == 1
    assert _commission(db, signal) == [pytest.approx(75.0)]
    fill_calls = [params for path, params in client.calls if path.endswith("/FILL")]
    assert len(fill_calls) >= 2
    first_page = sorted(foreign, key=lambda item: item["id"])[:100]
    assert fill_calls[1]["page_token"] == first_page[-1]["id"]


def test_backfill_waits_until_the_day_has_been_posted(db):
    signal, order_id = _crypto_fill(db, label="early")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client, now=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)) == 0
    assert _commission(db, signal) == [None]
    assert client.calls == [], "fees are posted end of day; a same-day lookup is not attempted"


def test_backfill_leaves_null_when_no_fee_is_posted(db, live_runner_logs):
    signal, order_id = _crypto_fill(db, label="nofee")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)]
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None], "no posted fee is not a zero fee"


def test_backfill_fails_closed_when_the_pair_traded_outside_this_account_view(db, live_runner_logs):
    """A manual trade (or another system's) on the same pair shares the day's CFEE."""
    signal, order_id = _crypto_fill(db, label="ours")
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT),
            _fill_activity(
                order_id=_order_id("manual"),
                side="buy",
                qty="0.1",
                price="60000",
                at=FILL_AT + timedelta(hours=1),
            ),
        ],
        cfees=[_cfee(qty="-0.0015")],
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]
    assert any("CFEE" in record.getMessage() for record in live_runner_logs.records)


def test_backfill_fails_closed_when_the_day_boundary_is_ambiguous(db):
    """02:00Z is Nov 4 in UTC but Nov 3 in New York; which day's CFEE it is is not documented."""
    late = datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)
    signal, order_id = _crypto_fill(db, label="boundary", at=late)
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=late)],
        cfees=[_cfee(qty="-0.00125", day="2026-11-03"), _cfee(qty="-0.00125", day="2026-11-04")],
    )

    assert _backfill(client, now=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)) == 0
    assert _commission(db, signal) == [None]


@pytest.mark.parametrize(
    "entry",
    [
        _cfee(qty="-0.00125", status="correct"),
        _cfee(qty="-0.00125", status="canceled"),
        _cfee(qty="-0.00125", net_amount="-75"),  # both an asset and a USD amount
        _cfee(qty="0.00125"),  # a positive fee
        _cfee(qty="0", net_amount="0"),  # a zero entry: which side is it?
        _cfee(qty="not-a-number"),
    ],
)
def test_backfill_fails_closed_on_an_unclassifiable_fee_entry(db, entry):
    signal, order_id = _crypto_fill(db, label="weird")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.00125"), entry],
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


def test_backfill_fails_closed_when_a_usd_fee_entry_names_the_pair(db):
    signal, order_id = _crypto_fill(db, label="fee-entry")
    fee = {
        "activity_type": "FEE",
        "activity_sub_type": "COM",
        "created_at": "2026-11-04T05:00:00Z",
        "currency": "USD",
        "date": TRADE_DAY,
        "id": f"20261103000000000::{uuid4()}",
        "net_amount": "-1.00",
        "qty": "0",
        "status": "executed",
        "symbol": "BTCUSD",
    }
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.00125")],
        fees=[fee],
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


def test_backfill_fails_closed_when_local_and_broker_quantities_disagree(db):
    signal, order_id = _crypto_fill(db, qty=0.4, label="short")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


def test_backfill_fails_closed_on_an_implausibly_large_fee(db):
    signal, order_id = _crypto_fill(db, label="huge")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.01")],  # 2% of the received BTC
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


def test_backfill_fails_closed_for_a_non_usd_quoted_pair(db):
    signal, order_id = _crypto_fill(db, label="ethbtc", symbol="ETH/BTC", price=0.05, qty=1.0)
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(
                order_id=order_id, side="buy", qty="1", price="0.05", at=FILL_AT, symbol="ETH/BTC"
            )
        ],
        cfees=[_cfee(qty="-0.0025", symbol="ETHBTC")],
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


def test_backfill_fails_closed_when_part_of_the_group_is_already_set(db, live_runner_logs):
    first, first_order = _crypto_fill(db, qty=0.2, label="mixed-1", commission=1.0)
    second_at = FILL_AT + timedelta(hours=1)
    second, second_order = _crypto_fill(db, qty=0.3, label="mixed-2", at=second_at)
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=first_order, side="buy", qty="0.2", price="60000", at=FILL_AT),
            _fill_activity(
                order_id=second_order, side="buy", qty="0.3", price="60000", at=second_at
            ),
        ],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 0
    assert _commission(db, first) == [1.0]
    assert _commission(db, second) == [None]


def test_backfill_is_idempotent(db):
    signal, order_id = _crypto_fill(db, label="idem")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 1
    assert _backfill(client) == 0
    assert _commission(db, signal) == [pytest.approx(75.0)]


def test_backfill_spans_deployments_of_the_same_account(db):
    """The CFEE is per account: two deployments' orders on one pair share it."""
    first, first_order = _crypto_fill(db, qty=0.2, label="dep-a")
    second_at = FILL_AT + timedelta(hours=1)
    second, second_order = _crypto_fill(
        db, qty=0.3, label="dep-b", at=second_at, deployment=OTHER_DEPLOYMENT
    )
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=first_order, side="buy", qty="0.2", price="60000", at=FILL_AT),
            _fill_activity(
                order_id=second_order, side="buy", qty="0.3", price="60000", at=second_at
            ),
        ],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 2
    assert _commission(db, first) == [pytest.approx(30.0)]
    assert _commission(db, second) == [pytest.approx(45.0)]


def test_backfill_survives_a_broker_error(db, live_runner_logs):
    signal, _order = _crypto_fill(db, label="error")

    assert _backfill(FakeActivitiesClient(fail=True)) == 0
    assert _commission(db, signal) == [None]
    assert any(
        "503" in record.getMessage() or "503" in str(getattr(record, "data", ""))
        for record in live_runner_logs.records
    )


def test_backfill_never_touches_a_known_commission(db):
    signal = _signal(db, symbol="AAPL")
    _local_fill(db, signal, price=100.0, qty=1.0, order_id=_order_id("eq"), key="k:eq")
    client = FakeActivitiesClient()

    assert _backfill(client) == 0
    assert _commission(db, signal) == [0.0]
    assert client.calls == [], "nothing is unknown, so nothing is fetched"


def test_apply_commissions_refuses_rows_that_are_no_longer_unknown(db):
    signal, _order = _crypto_fill(db, label="apply", commission=2.0)
    execution_id = str(_rows(db, signal)[0].id)

    assert db.apply_live_execution_commissions(ACCOUNT, {execution_id: 9.0}) == 0
    assert _commission(db, signal) == [2.0]


def test_apply_commissions_refuses_rows_of_another_account(db):
    signal, _order = _crypto_fill(db, label="apply-other")
    execution_id = str(_rows(db, signal)[0].id)

    assert db.apply_live_execution_commissions("some-other-account", {execution_id: 9.0}) == 0
    assert _commission(db, signal) == [None]


# ── daemon wiring ──────────────────────────────────────────────────────────────────────────────


def test_daemon_backfill_pass_calls_ready_adapters_and_survives_failures():
    from live_runner import daemon

    calls: list[str] = []

    class _Adapter:
        def __init__(self, name: str, *, connected: bool = True, fail: bool = False):
            self.name = name
            self._connected = connected
            self.fail = fail

        async def backfill_commissions(self) -> int:
            calls.append(self.name)
            if self.fail:
                raise RuntimeError("boom")
            return 1

    adapters = {
        "a": _Adapter("a", fail=True),
        "b": _Adapter("b"),
        "c": _Adapter("c", connected=False),
    }

    asyncio.run(daemon._backfill_commissions_once(adapters))

    assert sorted(calls) == ["a", "b"], "not-ready adapters are skipped, a failure is contained"


def test_base_adapter_backfill_is_a_no_op():
    from live_runner.base_adapter import BaseLiveAdapter

    class _Minimal(BaseLiveAdapter):
        async def connect(self):  # pragma: no cover
            return None

        async def submit_order(self, signal, *, qty=None):  # pragma: no cover
            return ""

        async def listen_executions(self):  # pragma: no cover
            return None

        async def get_balance(self):  # pragma: no cover
            return 0.0, 0.0

    assert asyncio.run(_Minimal().backfill_commissions()) == 0


# ── additional fail-closed edges ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_qty", ["nan", "inf"])
def test_reconcile_refuses_a_non_finite_broker_quantity(db, bad_qty):
    signal = _signal(db)
    order = _order(signal, filled_qty=bad_qty, filled_avg_price="100", order_id=_order_id(bad_qty))

    assert asyncio.run(_adapter(order).reconcile_executions()) == 0
    assert _rows(db, signal) == []


def test_amending_an_unknown_commission_with_a_known_slice_stays_unknown(db):
    """An unknown fee plus a known one is still unknown: the slice cannot complete it."""
    signal = _signal(db, symbol="BTC/USD")
    order_id = _order_id("amend-unknown-first")
    first = LiveExecution(
        signal_id=signal.id,
        actual_price=60000.0,
        filled_qty=1.0,
        commission=None,
        timestamp=datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc),
        broker_order_id=order_id,
        dedupe_key="k:amend-unknown",
    )
    assert db.save_live_execution_up_to_cumulative(first, 3.0) == pytest.approx(1.0)
    grown = first.model_copy(update={"filled_qty": 3.0, "commission": 4.0})

    assert db.save_live_execution_up_to_cumulative(grown, 3.0) == pytest.approx(2.0)
    assert _commission(db, signal) == [None]


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0])
def test_apply_commissions_rejects_an_unusable_value(db, value):
    signal, _order = _crypto_fill(db, label=f"apply-bad-{value}")
    execution_id = str(_rows(db, signal)[0].id)

    with pytest.raises(ValueError):
        db.apply_live_execution_commissions(ACCOUNT, {execution_id: value})
    assert _commission(db, signal) == [None]


def test_apply_commissions_is_all_or_nothing(db):
    """One row that is no longer unknown refuses the whole group, not just itself."""
    first, _ = _crypto_fill(db, label="atomic-1")
    second, _ = _crypto_fill(db, label="atomic-2", commission=1.0)
    updates = {str(_rows(db, first)[0].id): 3.0, str(_rows(db, second)[0].id): 4.0}

    assert db.apply_live_execution_commissions(ACCOUNT, updates) == 0
    assert _commission(db, first) == [None]
    assert _commission(db, second) == [1.0]


def test_backfill_fails_closed_when_the_broker_side_disagrees_with_the_signal(db):
    """A fill reported as a sell for a buy signal is not something to price a fee from."""
    signal, order_id = _crypto_fill(db, label="wrong-side")
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=order_id, side="sell", qty="0.5", price="60000", at=FILL_AT)
        ],
        cfees=[_cfee(net_amount="-75.0", description="Coin Pair Transaction Fee (USD)")],
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


def test_backfill_fails_closed_when_a_side_has_no_fee_of_its_kind(db):
    """Buys receive the base asset; a USD-only fee entry cannot be theirs."""
    signal, order_id = _crypto_fill(db, label="usd-for-buy")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(net_amount="-75.0", description="Coin Pair Transaction Fee (USD)")],
    )

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


def test_backfill_fails_closed_when_an_order_spans_two_fee_days(db):
    signal = _signal(db, symbol="BTC/USD")
    order_id = _order_id("two-days")
    second_day = FILL_AT + timedelta(days=1)
    _local_fill(
        db, signal, price=60000.0, qty=0.5, order_id=order_id, key="k:two-days", commission=None
    )
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=order_id, side="buy", qty="0.2", price="60000", at=FILL_AT),
            _fill_activity(order_id=order_id, side="buy", qty="0.3", price="60000", at=second_day),
        ],
        cfees=[_cfee(qty="-0.0005"), _cfee(qty="-0.00075", day="2026-11-04")],
    )

    assert _backfill(client, now=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)) == 0
    assert _commission(db, signal) == [None]


def test_backfill_ignores_unknown_commissions_outside_the_lookback(db):
    """A row far older than the lookback window is left alone and costs no broker call."""
    signal, _order = _crypto_fill(db, label="ancient", at=FILL_AT - timedelta(days=400))
    client = FakeActivitiesClient()

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]
    assert client.calls == []


def test_backfill_fails_closed_on_a_fill_activity_without_a_symbol(db):
    """A fill that names no pair could be any pair's; it must not be silently skipped."""
    signal, order_id = _crypto_fill(db, label="no-symbol")
    ours = _fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)
    nameless = _fill_activity(
        order_id=_order_id("nameless"),
        side="buy",
        qty="0.1",
        price="60000",
        at=FILL_AT + timedelta(minutes=5),
    )
    nameless["symbol"] = ""
    client = FakeActivitiesClient(fills=[ours, nameless], cfees=[_cfee(qty="-0.0015")])

    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]


# ── review of PR #143 ──────────────────────────────────────────────────────────────────────────
#
# 1. A stream event whose own price is unusable must not borrow the order's CUMULATIVE average.
# 2. Crypto is decided from the symbol's data, not from a slash.
# 3. A derived remainder price is plausibility-checked and labelled `derived`.
# 4. An unpriced remainder still records its QUANTITY; every PnL reader treats it as incomplete.


@pytest.mark.parametrize(("event_price", "verdict"), [("0", PRICE_SENT_BAD), (None, PRICE_ABSENT)])
def test_stream_partial_fill_does_not_borrow_the_cumulative_average(
    monkeypatch, event_price, verdict
):
    """Fill 1@100 then 1@104: the second update's average is 102, which is no fill's price."""
    saved = _stream_capture(monkeypatch)
    update = _trade_update(
        signal_id=str(uuid4()),
        order_id=_order_id(f"partial-avg-{event_price}"),
        asset_class="us_equity",
        symbol="AAPL",
        price=event_price,
        qty="1",
        event="partial_fill",
    )
    update.order.filled_qty = "2"
    update.order.filled_avg_price = "102"

    asyncio.run(_adapter(None)._on_trade_update(update))

    assert len(saved) == 1
    assert saved[0].filled_qty == pytest.approx(1.0)
    assert saved[0].actual_price == 0.0
    assert saved[0].actual_price_observed == verdict


def test_stream_single_fill_order_may_use_the_average_but_labels_it_derived(monkeypatch):
    """When this event IS the whole order, the order's average is this fill's price."""
    saved = _stream_capture(monkeypatch)
    update = _trade_update(
        signal_id=str(uuid4()),
        order_id=_order_id("single-fill-avg"),
        asset_class="us_equity",
        symbol="AAPL",
        price=None,
        qty="2",
    )
    update.order.filled_qty = "2"
    update.order.filled_avg_price = "101.5"

    asyncio.run(_adapter(None)._on_trade_update(update))

    assert saved[0].actual_price == pytest.approx(101.5)
    assert saved[0].actual_price_observed == PRICE_DERIVED


def test_stream_fill_without_its_own_quantity_does_not_use_the_average(monkeypatch):
    """A `fill` event without its own qty falls back to the cumulative qty; that proves nothing."""
    saved = _stream_capture(monkeypatch)
    update = _trade_update(
        signal_id=str(uuid4()),
        order_id=_order_id("no-event-qty"),
        asset_class="us_equity",
        symbol="AAPL",
        price=None,
        qty="2",
    )
    update.qty = None
    update.order.filled_avg_price = "101.5"

    asyncio.run(_adapter(None)._on_trade_update(update))

    assert saved[0].actual_price == 0.0
    assert saved[0].actual_price_observed == PRICE_ABSENT


def test_stream_then_reconcile_never_trusts_a_borrowed_average(db):
    """The reviewer's sequence end to end: 1@100 observed, 1@? (price "0"), then 1 missed."""
    signal = _signal(db)
    order_id = _order_id("stream-then-reconcile")
    first = _trade_update(
        signal_id=str(signal.id),
        order_id=order_id,
        asset_class="us_equity",
        symbol="AAPL",
        price="100",
        qty="1",
        event="partial_fill",
    )
    second = _trade_update(
        signal_id=str(signal.id),
        order_id=order_id,
        asset_class="us_equity",
        symbol="AAPL",
        price="0",
        qty="1",
        event="partial_fill",
    )
    second.timestamp = datetime(2026, 11, 3, 15, 1, tzinfo=timezone.utc)
    second.order.filled_qty = "2"
    second.order.filled_avg_price = "102"
    adapter = _adapter(_order(signal, filled_qty="3", filled_avg_price="103", order_id=order_id))

    asyncio.run(adapter._on_trade_update(first))
    asyncio.run(adapter._on_trade_update(second))
    assert asyncio.run(adapter.reconcile_executions()) == 1

    rows = _rows(db, signal)
    assert [row.filled_qty for row in rows] == [
        pytest.approx(1.0),
        pytest.approx(1.0),
        pytest.approx(1.0),
    ]
    assert [row.actual_price_observed for row in rows] == [
        PRICE_OBSERVED,
        PRICE_SENT_BAD,
        PRICE_DERIVED_UNAVAILABLE,
    ]
    assert [row.actual_price for row in rows] == [pytest.approx(100.0), 0.0, 0.0]


def test_reconcile_trusts_a_derived_local_leg(db):
    signal = _signal(db)
    order_id = _order_id("derived-leg")
    _local_fill(
        db,
        signal,
        price=100.0,
        qty=2.0,
        order_id=order_id,
        key="alpaca:exec:derived-leg",
        observed=PRICE_DERIVED,
    )
    order = _order(signal, filled_qty="5", filled_avg_price="102", order_id=order_id)

    assert asyncio.run(_adapter(order).reconcile_executions()) == 1
    remainder = _rows(db, signal)[-1]
    assert remainder.actual_price == pytest.approx((510.0 - 200.0) / 3.0)
    assert remainder.actual_price_observed == PRICE_DERIVED


@pytest.mark.parametrize(
    ("broker_avg", "expected_price", "verdict"),
    [
        ("102", (510.0 - 400.0) / 1.0, PRICE_DERIVED),  # 110: +7.8% of the average, inside
        ("104", 0.0, PRICE_DERIVED_UNAVAILABLE),  # 120: +15.4%, outside the 10% band
        ("96", 0.0, PRICE_DERIVED_UNAVAILABLE),  # 80: -16.7%, outside the band
    ],
)
def test_reconcile_remainder_price_must_be_plausible(db, broker_avg, expected_price, verdict):
    signal = _signal(db)
    order_id = _order_id(f"band-{broker_avg}")
    _local_fill(db, signal, price=100.0, qty=4.0, order_id=order_id, key="alpaca:exec:band")
    order = _order(signal, filled_qty="5", filled_avg_price=broker_avg, order_id=order_id)

    assert asyncio.run(_adapter(order).reconcile_executions()) == 1

    remainder = _rows(db, signal)[-1]
    assert remainder.filled_qty == pytest.approx(1.0), "the quantity is always recorded"
    if verdict == PRICE_DERIVED:
        assert remainder.actual_price == pytest.approx(5 * float(broker_avg) - 400.0)
    else:
        assert remainder.actual_price == expected_price
    assert remainder.actual_price_observed == verdict


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("BTC/USD", "crypto"),
        ("BTCUSD", "crypto"),
        ("btcusd", "crypto"),
        ("ETH/BTC", "crypto"),
        ("ETHBTC", "crypto"),
        ("SOLUSDT", "crypto"),
        ("USDCUSD", "crypto"),
        ("DOGEUSDC", "crypto"),
        ("AAPL", "us_equity"),
        ("USD", "us_equity"),  # ProShares Ultra Semiconductors: an equity named like a quote
        ("BRK.B", "us_equity"),
        ("AAPL261120C00200000", "unknown"),  # an option: neither, so unknown
        ("", "unknown"),
        ("BTC/", "unknown"),
        ("XYZABC", "unknown"),  # six letters, no known quote suffix
    ],
)
def test_alpaca_symbol_classification(symbol, expected):
    from shared.alpaca_symbols import classify_alpaca_symbol

    assert classify_alpaca_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "pair"),
    [
        ("BTC/USD", ("BTC", "USD")),
        ("BTCUSD", ("BTC", "USD")),
        ("SOLUSDT", ("SOL", "USDT")),
        ("ETHBTC", ("ETH", "BTC")),
        ("AAPL", None),
        ("AAPL261120C00200000", None),
    ],
)
def test_split_crypto_pair_accepts_both_spellings(symbol, pair):
    from shared.alpaca_symbols import split_crypto_pair

    assert split_crypto_pair(symbol) == pair


def test_backfill_prices_a_crypto_signal_stored_without_a_slash(db):
    signal, order_id = _crypto_fill(db, label="no-slash", symbol="BTCUSD")
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=FILL_AT)],
        cfees=[_cfee(qty="-0.00125")],
    )

    assert _backfill(client) == 1
    assert _commission(db, signal) == [pytest.approx(75.0)]


def test_backfill_logs_every_candidate_it_skips(db, live_runner_logs):
    option = _signal(db, symbol="AAPL261120C00200000")
    _local_fill(
        db,
        option,
        price=2.5,
        qty=1.0,
        order_id=_order_id("option"),
        key="k:option",
        commission=None,
    )
    early, _ = _crypto_fill(db, label="too-early", at=NOW - timedelta(hours=6))
    option_id = str(_rows(db, option)[0].id)
    early_id = str(_rows(db, early)[0].id)

    assert _backfill(FakeActivitiesClient()) == 0

    text_ = " ".join(
        record.getMessage() + " " + str(getattr(record, "data", ""))
        for record in live_runner_logs.records
    )
    assert option_id in text_ and "not a crypto pair" in text_
    assert early_id in text_ and "not posted yet" in text_


def test_legacy_migration_classifies_symbols_from_data_not_the_slash(isolated_db_no_schema):
    database, engine = _build_legacy_database(isolated_db_no_schema)
    extra = {
        "sig-nosl": ("BTCUSD", "ex-nosl"),
        "sig-opt": ("AAPL261120C00200000", "ex-opt"),
        "sig-brk": ("BRK.B", "ex-brk"),
    }
    with engine.begin() as connection:
        for signal_id, (symbol, execution_id) in extra.items():
            connection.execute(
                text(
                    "INSERT INTO live_signals (id, deployment_id, symbol, action, "
                    "theoretical_price, timestamp, metadata, source) VALUES "
                    "(:id, 'dep-acc-alp', :symbol, 'buy', 1.0, '2026-11-03 15:00:00', '{}', "
                    "'internal')"
                ),
                {"id": signal_id, "symbol": symbol},
            )
            connection.execute(
                text(
                    "INSERT INTO live_executions (id, signal_id, actual_price, filled_qty, "
                    "commission, timestamp, broker_order_id, dedupe_key) VALUES "
                    "(:id, :sig, 10.0, 1.0, 0.0, '2026-11-03 15:00:00', 'bo', :key)"
                ),
                {"id": execution_id, "sig": signal_id, "key": f"k:{execution_id}"},
            )

    database.init_db()

    with engine.connect() as connection:
        commissions = dict(
            connection.execute(text("SELECT id, commission FROM live_executions")).fetchall()
        )
    assert commissions["ex-nosl"] is None, "crypto without a slash is still crypto"
    assert commissions["ex-opt"] is None, "an undeterminable class fails closed to unknown"
    assert commissions["ex-brk"] == 0.0, "an equity zero is a known zero"
    assert commissions["ex-equity"] == 0.0
    assert commissions["ex-ctrader-zero"] == 0.0


def test_execution_listings_do_not_present_an_unusable_price_as_zero(db):
    signal = _signal(db)
    _local_fill(
        db,
        signal,
        price=0.0,
        qty=1.0,
        order_id=_order_id("unpriced-listing"),
        key="k:unpriced-listing",
        observed=PRICE_DERIVED_UNAVAILABLE,
    )

    listed = db.list_live_signal_executions_for_deployments([DEPLOYMENT])
    _balances, stats_rows = db.load_live_account_stats_inputs(ACCOUNT)
    signals = db.list_live_signals_for_deployments([DEPLOYMENT])

    for row in (listed[0], stats_rows[0]):
        assert row["actual_price"] is None
        assert row["slippage"] is None
        assert row["slippage_bps"] is None
        assert row["actual_price_observed"] == PRICE_DERIVED_UNAVAILABLE
        assert row["filled_qty"] == pytest.approx(1.0), "the quantity is real"
    assert signals[0]["actual_price"] is None


def test_viewer_stats_pnl_is_incomplete_when_any_fill_price_is_unusable(monkeypatch):
    import viewer.main as viewer_main

    rows = [
        _stats_row("buy", 100.0, 1.0, 0, 0.0),
        {**_stats_row("sell", 0.0, 1.0, 5, 0.0), "actual_price": None},
    ]
    rows[1]["actual_price_observed"] = PRICE_DERIVED_UNAVAILABLE
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["pnl_complete"] is False
    assert payload["price_unknown_count"] == 1
    assert payload["total_pnl"] is None, "an unknown price must not be realized as zero"
    assert payload["net_pnl"] is None
    assert payload["net_pnl_complete"] is False


def test_viewer_stats_pnl_is_complete_for_derived_prices(monkeypatch):
    import viewer.main as viewer_main

    rows = [_stats_row("buy", 100.0, 1.0, 0, 0.0), _stats_row("sell", 110.0, 1.0, 5, 0.0)]
    rows[1]["actual_price_observed"] = PRICE_DERIVED
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["pnl_complete"] is True
    assert payload["price_unknown_count"] == 0
    assert payload["total_pnl"] == pytest.approx(10.0)
    assert payload["net_pnl"] == pytest.approx(10.0)


def test_viewer_live_trades_keep_an_unusable_price_unknown(monkeypatch):
    import viewer.main as viewer_main

    row = {
        "signal_id": "s",
        "execution_id": "e",
        "deployment_id": DEPLOYMENT,
        "symbol": "AAPL",
        "action": "buy",
        "theoretical_price": 100.0,
        "actual_price": None,
        "actual_price_observed": PRICE_SENT_BAD,
        "slippage": None,
        "slippage_bps": None,
        "filled_qty": 1.0,
        "commission": 0.0,
        "signal_timestamp": datetime(2026, 11, 3, tzinfo=timezone.utc),
        "execution_timestamp": datetime(2026, 11, 3, tzinfo=timezone.utc),
        "broker_order_id": "bo",
        "signal_metadata": {},
    }
    monkeypatch.setattr(viewer_main, "list_live_deployments_for_account", lambda _a: [])
    monkeypatch.setattr(
        viewer_main, "list_live_signal_executions_for_deployments", lambda _ids: [row]
    )

    trade = asyncio.run(viewer_main._live_trades_payload(ACCOUNT))["trades"][0]

    assert trade["actual_price"] is None
    assert trade["slippage"] is None
    assert trade["slippage_bps"] is None
    assert trade["actual_price_observed"] == PRICE_SENT_BAD
    assert trade["filled_qty"] == pytest.approx(1.0)


def test_frontend_live_trade_commission_type_admits_null():
    """`commission: null` is "fee not known yet"; the type must say so (review of #143)."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "frontend/src/api/types.ts").read_text(
        encoding="utf-8"
    )
    block = source.split("export interface LiveTradeSlippage {", 1)[1].split("}", 1)[0]
    fields = {
        line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip().rstrip(";")
        for line in block.splitlines()
        if ":" in line
    }
    assert fields["commission"] == "number | null"
    assert fields["actual_price"] == "number | null"
    assert fields["slippage"] == "number | null"
    assert fields["slippage_bps"] == "number | null"


# ── Tests to kill surviving mutants from mutation testing ───────────────────────────────────────


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        # MIN_CRYPTO_BASE=3: 2-letter base should be equity, not crypto
        ("ABUSD", "us_equity"),  # 2-letter base + USD
        ("XYBTC", "us_equity"),  # 2-letter base + BTC
        # CRYPTO_QUOTES order: ambiguous symbols should match longest quote first
        ("BTCUSDT", "crypto"),  # Should be BTC/USDT (USDT checked before USD)
        ("BTCUSDC", "crypto"),  # Should be BTC/USDC (USDC checked before USD)
        ("BTCUSD", "crypto"),   # Should be BTC/USD
    ],
)
def test_alpaca_symbol_classification_edge_cases(symbol, expected):
    """Edge cases that would survive MIN_CRYPTO_BASE or CRYPTO_QUOTES mutations."""
    from shared.alpaca_symbols import classify_alpaca_symbol

    assert classify_alpaca_symbol(symbol) == expected


@pytest.mark.parametrize(
    ("symbol", "pair"),
    [
        # CRYPTO_QUOTES order: ambiguous symbols should match longest quote first
        ("BTCUSDT", ("BTC", "USDT")),
        ("BTCUSDC", ("BTC", "USDC")),
        ("BTCUSD", ("BTC", "USD")),
        ("ETHUSDT", ("ETH", "USDT")),
    ],
)
def test_split_crypto_pair_quote_priority(symbol, pair):
    """Quote matching should prioritize longest match (USDT > USDC > USD > BTC)."""
    from shared.alpaca_symbols import split_crypto_pair

    assert split_crypto_pair(symbol) == pair


def test_backfill_fails_closed_when_buys_have_no_asset_fee_but_sells_have_usd_fee(db):
    """Buys with no asset fee must fail closed even when sells have a valid USD fee."""
    # Two orders: one buy, one sell on the same day/pair
    buy_signal, buy_order = _crypto_fill(db, action="buy", qty=0.5, price=60000.0, label="buy-only")
    sell_at = FILL_AT + timedelta(hours=1)
    sell_signal, sell_order = _crypto_fill(db, action="sell", qty=0.5, price=61000.0, label="sell-only", at=sell_at)
    
    # Only a USD fee for the sell side, NO asset fee for the buy side
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=buy_order, side="buy", qty="0.5", price="60000", at=FILL_AT),
            _fill_activity(order_id=sell_order, side="sell", qty="0.5", price="61000", at=sell_at),
        ],
        cfees=[_cfee(net_amount="-75.0", description="Coin Pair Transaction Fee (USD)")],  # USD fee only
    )

    assert _backfill(client) == 0, "Should fail closed: buys have no asset fee"
    assert _commission(db, buy_signal) == [None]
    assert _commission(db, sell_signal) == [None]


def test_backfill_respects_lookback_window(db):
    """Commissions older than COMMISSION_BACKFILL_LOOKBACK must be ignored."""
    # Create an old fill (40 days ago, outside the 30-day lookback)
    ancient_at = FILL_AT - timedelta(days=40)
    signal, order_id = _crypto_fill(db, label="ancient", at=ancient_at)
    
    client = FakeActivitiesClient(
        fills=[_fill_activity(order_id=order_id, side="buy", qty="0.5", price="60000", at=ancient_at)],
        cfees=[_cfee(qty="-0.00125", day=(ancient_at.date()).isoformat())],
    )

    # Should NOT backfill - too old
    assert _backfill(client) == 0
    assert _commission(db, signal) == [None]
    assert client.calls == [], "No broker calls for executions outside lookback"


def test_backfill_ignores_unknown_commissions_outside_the_lookback_explicit(db):
    """Explicit test for the lookback boundary: 31 days old should be ignored, 5 days should be processed."""
    # NOW = 2026-11-06 12:00:00, lookback starts at 2026-10-07 12:00:00
    # 31 days before FILL_AT (Nov 3) = Oct 3 - outside lookback
    old_at = FILL_AT - timedelta(days=31)
    old_signal, old_order = _crypto_fill(db, label="old", at=old_at)
    
    # 5 days before FILL_AT = Oct 29 - inside lookback (after Oct 7)
    new_at = FILL_AT - timedelta(days=5)
    new_signal, new_order = _crypto_fill(db, label="new", at=new_at)
    
    client = FakeActivitiesClient(
        fills=[
            _fill_activity(order_id=old_order, side="buy", qty="0.5", price="60000", at=old_at),
            _fill_activity(order_id=new_order, side="buy", qty="0.5", price="60000", at=new_at),
        ],
        cfees=[
            _cfee(qty="-0.00125", day=(old_at.date()).isoformat()),
            _cfee(qty="-0.00125", day=(new_at.date()).isoformat()),
        ],
    )

    assert _backfill(client) == 1, "Only the new fill should be backfilled"
    assert _commission(db, old_signal) == [None], "Old fill should be ignored"
    assert _commission(db, new_signal) == [pytest.approx(75.0)], "New fill should be priced"


def test_viewer_stats_net_pnl_requires_both_pnl_complete_and_known_commission(monkeypatch):
    """net_pnl is None when pnl_complete=False even if all commissions are known."""
    import viewer.main as viewer_main

    # One fill with unusable price (pnl_complete=False) but known commission
    rows = [
        _stats_row("buy", 100.0, 1.0, 0, 0.5),
        {**_stats_row("sell", 0.0, 1.0, 5, 0.5), "actual_price": None},
    ]
    rows[1]["actual_price_observed"] = PRICE_DERIVED_UNAVAILABLE
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["pnl_complete"] is False
    assert payload["commission_unknown_count"] == 0
    assert payload["total_commission"] == pytest.approx(1.0)
    assert payload["net_pnl"] is None, "net_pnl must be None when pnl_complete=False"
    assert payload["net_pnl_complete"] is False


def test_viewer_stats_net_pnl_is_none_when_commission_unknown_even_if_pnl_complete(monkeypatch):
    """net_pnl is None when commission_unknown > 0 even if pnl_complete=True."""
    import viewer.main as viewer_main

    # All prices known (pnl_complete=True) but one commission unknown
    rows = [
        _stats_row("buy", 100.0, 1.0, 0, None),  # unknown commission
        _stats_row("sell", 110.0, 1.0, 5, 0.5),
    ]
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["pnl_complete"] is True
    assert payload["commission_unknown_count"] == 1
    assert payload["total_commission"] is None
    assert payload["net_pnl"] is None, "net_pnl must be None when commission unknown"
    assert payload["net_pnl_complete"] is False




# ── Additional tests for remaining surviving mutants ────────────────────────────────────────────


def test_viewer_stats_net_pnl_unknown_commission_treated_as_unknown_not_zero(monkeypatch):
    """An unknown (NULL) commission must make total_commission None, not a smaller number."""
    import viewer.main as viewer_main

    # One known commission, one unknown
    rows = [
        _stats_row("buy", 100.0, 1.0, 0, 0.5),
        _stats_row("sell", 110.0, 1.0, 5, None),  # unknown commission
    ]
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["commission_unknown_count"] == 1
    assert payload["total_commission"] is None, "Unknown commission must make total None, not 0.5"
    assert payload["net_pnl"] is None
    assert payload["net_pnl_complete"] is False


def test_viewer_stats_unpriced_fill_counted_even_when_actual_price_nonzero(monkeypatch):
    """A fill with actual_price=0.0 but observed=sent_bad/absent/derived_unavailable is unpriced."""
    import viewer.main as viewer_main

    # Fill has price 0.0 but it's because the price was bad/absent, not a real $0 fill
    rows = [
        _stats_row("buy", 100.0, 1.0, 0, 0.0),
        {**_stats_row("sell", 0.0, 1.0, 5, 0.0), "actual_price": 0.0},
    ]
    rows[1]["actual_price_observed"] = PRICE_SENT_BAD  # Price was sent but unusable
    monkeypatch.setattr(viewer_main, "load_live_account_stats_inputs", lambda _a: ([], rows))

    payload = viewer_main._live_account_stats_payload(ACCOUNT)

    assert payload["pnl_complete"] is False
    assert payload["price_unknown_count"] == 1
    assert payload["total_pnl"] is None, "Unusable price must not be realized as zero"


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        # Unknown symbols (options, malformed) must NOT be classified as crypto
        ("AAPL261120C00200000", "unknown"),  # OCC option
        ("", "unknown"),
        ("BTC/", "unknown"),
        ("XYZABC", "unknown"),  # six letters, no known quote suffix
        ("BT", "us_equity"),  # too short for crypto
        ("BTC", "us_equity"),  # 3 letters but no quote suffix
    ],
)
def test_alpaca_symbol_classification_unknown_not_crypto(symbol, expected):
    """Unknown symbols must remain unknown, not become crypto (equivalent mutant check)."""
    from shared.alpaca_symbols import classify_alpaca_symbol

    assert classify_alpaca_symbol(symbol) == expected




@pytest.mark.parametrize("verdict", ["observd", "OBSERVED", "unknown", "", 0, True])
def test_an_unrecognised_price_verdict_is_not_a_usable_price(verdict) -> None:
    """Allowlist, not denylist: a typo or a future verdict must fail closed.

    Only a missing verdict (rows from before the column existed), `observed`
    and `derived` vouch for a price (platform-pr-verify finding on PR #143).
    """
    from shared.models import execution_price_is_usable

    assert execution_price_is_usable(101.5, verdict) is False


@pytest.mark.parametrize("verdict", [None, "observed", "derived"])
def test_known_good_price_verdicts_stay_usable(verdict) -> None:
    from shared.models import execution_price_is_usable

    assert execution_price_is_usable(101.5, verdict) is True
    assert execution_price_is_usable(0.0, verdict) is False
