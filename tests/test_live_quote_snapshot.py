"""#134: every live trade must carry the data a slippage model needs.

The #125 entry-deviation gate already fetches a broker quote for entries, but
until this change the quote, the timing chain and the order context lived only
in signal metadata (or nowhere): rejected signals were logged and dropped, and
a fill could not say which quote it was judged against or when the order left
the process.

Pinned here:

* ``live_signals`` projects the quote snapshot and the timing chain
  (``producer_at`` / ``message_received_at`` / ``order_submitted_at`` /
  ``broker_ack_at``) into real columns; ``live_executions`` carries the
  broker's ``order_type`` and ``requested_qty``.
* Refusals before the broker request land in ``live_signal_rejections`` -
  never in ``live_signals``, which asserts an order may exist - with the
  gate's quote record attached, deduped like signal rows, and persisted
  best-effort so bookkeeping can never reach the order path.
* ``list_live_signal_executions_for_deployments`` splits the legacy
  ``actual - theoretical`` slippage into ``signal_delay_bps`` (quote at
  submit vs signal price), ``execution_slippage_bps`` (fill vs that quote)
  and ``spread_bps``, all adverse-positive; a fill whose signal has no usable
  snapshot is flagged ``quote_snapshot_missing``, never silently zero.
* Both broker paths stamp submit/ack around the broker call and record the
  order context on stream and reconciliation fills.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

import live_runner.daemon as daemon
from live_runner.base_adapter import LiveQuote
from shared.models import (
    LiveDeploymentConfig,
    LiveExecution,
    LiveSignal,
)

DEPLOYMENT = "12345678-1234-4234-8234-123456789012"
ACCOUNT = "acct-quote-snapshot"

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

_QUOTE_METADATA = {
    "entry_quote_bid": 99.9,
    "entry_quote_ask": 100.1,
    "entry_quote_side": "ask",
    "entry_quote_price": 100.1,
    "entry_quote_time": "2026-11-10T11:59:59+00:00",
    "entry_quote_source": "AlpacaLiveAdapter",
    "entry_quote_time_source": "broker",
    "entry_quote_age_seconds": 1.0,
    "received_at": "2026-11-10T11:59:58+00:00",
    "received_at_local": "2026-11-10T11:59:58.500+00:00",
}


@pytest.fixture
def db(isolated_db):
    database, _engine, _data_dir = isolated_db
    database.init_db()
    with database.get_session() as session:
        session.add(database.LiveAccountModel(id=ACCOUNT, broker="fixture", name=ACCOUNT))
        session.flush()
        session.add(
            database.LiveDeploymentModel(
                id=DEPLOYMENT,
                strategy_name="quote-snapshot",
                account_id=ACCOUNT,
                symbols_json=["AAPL"],
                timeframe="1h",
                status="ACTIVE",
            )
        )
        session.commit()
    return database


def _signal(
    *,
    action: str = "buy",
    metadata: dict[str, Any] | None = None,
    theoretical_price: float = 100.0,
) -> LiveSignal:
    return LiveSignal(
        deployment_id=DEPLOYMENT,
        symbol="AAPL",
        action=action,
        theoretical_price=theoretical_price,
        timestamp=NOW,
        metadata=dict(metadata if metadata is not None else _QUOTE_METADATA),
    )


def _execution(
    signal: LiveSignal,
    *,
    price: float = 100.2,
    qty: float = 1.0,
    order_type: str | None = "market",
    requested_qty: float | None = 1.0,
    key: str | None = None,
) -> LiveExecution:
    return LiveExecution(
        signal_id=signal.id,
        actual_price=price,
        actual_price_observed="observed",
        filled_qty=qty,
        commission=0.0,
        timestamp=NOW + timedelta(seconds=30),
        broker_order_id="order-1",
        dedupe_key=key or f"test:{uuid4()}",
        order_type=order_type,
        requested_qty=requested_qty,
    )


def _signal_row(database, signal: LiveSignal):
    with database.get_session() as session:
        return session.get(database.LiveSignalModel, str(signal.id))


def _execution_rows(database, signal: LiveSignal):
    with database.get_session() as session:
        return list(
            session.exec(
                database.select(database.LiveExecutionModel).where(
                    database.LiveExecutionModel.signal_id == str(signal.id)
                )
            ).all()
        )


def _serialized(database, signal: LiveSignal) -> dict[str, Any]:
    rows = database.list_live_signal_executions_for_deployments([DEPLOYMENT])
    return next(row for row in rows if row["signal_id"] == str(signal.id))


# ── schema + projection ───────────────────────────────────────────────────────


def test_live_signal_projects_quote_snapshot_and_timing(db) -> None:
    signal = _signal()
    db.save_live_signal(signal)

    row = _signal_row(db, signal)
    assert row.entry_quote_bid == pytest.approx(99.9)
    assert row.entry_quote_ask == pytest.approx(100.1)
    assert row.entry_quote_side == "ask"
    assert row.entry_quote_price == pytest.approx(100.1)
    assert row.entry_quote_time.replace(tzinfo=timezone.utc) == datetime(
        2026, 11, 10, 11, 59, 59, tzinfo=timezone.utc
    )
    assert row.entry_quote_source == "AlpacaLiveAdapter"
    assert row.entry_quote_time_source == "broker"
    # producer_at prefers the producer stamp; message_received_at is the local receipt.
    assert row.producer_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 11, 10, 11, 59, 58, tzinfo=timezone.utc
    )
    assert row.message_received_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 11, 10, 11, 59, 58, 500000, tzinfo=timezone.utc
    )
    # Nothing was submitted yet: the stamps stay NULL, not fabricated.
    assert row.order_submitted_at is None
    assert row.broker_ack_at is None


def test_live_signal_without_quote_metadata_keeps_columns_null(db) -> None:
    signal = _signal(metadata={})
    db.save_live_signal(signal)

    row = _signal_row(db, signal)
    assert row.entry_quote_bid is None
    assert row.entry_quote_ask is None
    assert row.entry_quote_price is None
    assert row.entry_quote_time is None
    assert row.producer_at is None
    assert row.message_received_at is None


def test_producer_at_falls_back_to_signal_timestamp(db) -> None:
    signal = _signal(metadata={"signal_timestamp": "2026-11-10T11:59:57+00:00"})
    db.save_live_signal(signal)

    row = _signal_row(db, signal)
    assert row.producer_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 11, 10, 11, 59, 57, tzinfo=timezone.utc
    )


def test_malformed_quote_metadata_degrades_to_null_not_a_number(db) -> None:
    signal = _signal(
        metadata={
            "entry_quote_bid": "not-a-price",
            "entry_quote_ask": None,
            "entry_quote_price": float("nan"),
            "entry_quote_time": "garbage",
            "received_at": "also-garbage",
        }
    )
    db.save_live_signal(signal)

    row = _signal_row(db, signal)
    assert row.entry_quote_bid is None
    assert row.entry_quote_ask is None
    assert row.entry_quote_price is None
    assert row.entry_quote_time is None
    assert row.producer_at is None
    # The raw values still live in metadata_json for audit.
    assert row.metadata_json["entry_quote_bid"] == "not-a-price"


def test_set_broker_order_id_stamps_submit_and_ack(db) -> None:
    signal = _signal()
    db.save_live_signal(signal)
    submitted = NOW + timedelta(seconds=5)
    acked = NOW + timedelta(seconds=5, milliseconds=180)

    assert db.set_live_signal_broker_order_id(
        str(signal.id), "broker-1", submitted_at=submitted, acknowledged_at=acked
    )

    row = _signal_row(db, signal)
    assert row.broker_order_id == "broker-1"
    assert row.order_submitted_at.replace(tzinfo=timezone.utc) == submitted
    assert row.broker_ack_at.replace(tzinfo=timezone.utc) == acked
    assert row.metadata_json["order_submitted_at"] == submitted.isoformat()
    assert row.metadata_json["broker_ack_at"] == acked.isoformat()

    # A re-persist without times must not erase what is known.
    assert db.set_live_signal_broker_order_id(str(signal.id), "broker-1")
    row = _signal_row(db, signal)
    assert row.order_submitted_at.replace(tzinfo=timezone.utc) == submitted
    assert row.broker_ack_at.replace(tzinfo=timezone.utc) == acked


def test_execution_order_context_round_trips(db) -> None:
    signal = _signal()
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal, order_type="limit", requested_qty=2.5))

    row = _execution_rows(db, signal)[0]
    assert row.order_type == "limit"
    assert row.requested_qty == pytest.approx(2.5)
    restored = row.to_live_execution()
    assert restored.order_type == "limit"
    assert restored.requested_qty == pytest.approx(2.5)


def test_execution_without_order_context_stays_null(db) -> None:
    signal = _signal()
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal, order_type=None, requested_qty=None))

    row = _execution_rows(db, signal)[0]
    assert row.order_type is None
    assert row.requested_qty is None


# ── migration ─────────────────────────────────────────────────────────────────


def test_migration_adds_quote_snapshot_columns_to_legacy_signals(isolated_db_no_schema) -> None:
    database, engine, _data_dir = isolated_db_no_schema
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE live_accounts (id TEXT PRIMARY KEY, broker TEXT, name TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE live_deployments (id TEXT PRIMARY KEY, strategy_name TEXT, "
                "account_id TEXT, symbols TEXT, timeframe TEXT, status TEXT, "
                "created_at TEXT, updated_at TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO live_accounts (id, broker, name) "
                "VALUES ('acct-legacy', 'fixture', 'a')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO live_deployments (id, strategy_name, account_id, symbols, "
                "timeframe, status, created_at, updated_at) VALUES "
                "('dep-legacy', 'legacy', 'acct-legacy', '[]', '1m', 'PAUSED', '', '')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE live_signals ("
                "id TEXT PRIMARY KEY, deployment_id TEXT, symbol TEXT, action TEXT, "
                "theoretical_price FLOAT, timestamp TEXT, source TEXT DEFAULT 'internal', "
                "broker_order_id TEXT DEFAULT '', metadata TEXT)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO live_signals (id, deployment_id, symbol, action, "
                "theoretical_price, timestamp, source, broker_order_id, metadata) VALUES "
                "('s-legacy', 'dep-legacy', 'AAPL', 'buy', 1.0, '2024-01-01T00:00:00', "
                "'internal', '', '{}')"
            )
        )

    database.init_db()

    with engine.connect() as conn:
        columns = {
            str(row[1])
            for row in conn.execute(text("PRAGMA table_info('live_signals')")).fetchall()
        }
        rejections = conn.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='live_signal_rejections'"
            )
        ).first()
        execution_columns = {
            str(row[1])
            for row in conn.execute(text("PRAGMA table_info('live_executions')")).fetchall()
        }
    for name in (
        "entry_quote_bid",
        "entry_quote_ask",
        "entry_quote_side",
        "entry_quote_price",
        "entry_quote_time",
        "entry_quote_source",
        "entry_quote_time_source",
        "producer_at",
        "message_received_at",
        "order_submitted_at",
        "broker_ack_at",
    ):
        assert name in columns, f"missing live_signals.{name} after migration"
    assert rejections is not None, "live_signal_rejections table was not created"
    assert "order_type" in execution_columns
    assert "requested_qty" in execution_columns


# ── rejected signals ──────────────────────────────────────────────────────────


def _rejection_rows(database) -> list[Any]:
    with database.get_session() as session:
        return list(session.exec(database.select(database.LiveSignalRejectionModel)).all())


def test_gate_rejection_is_persisted_with_its_quote_snapshot(db) -> None:
    """The refusal carries the quote it was judged against into the table."""
    signal = _signal(metadata={"discord_message_id": "msg-1"})
    gate_record = {
        "entry_quote_bid": 99.9,
        "entry_quote_ask": 100.1,
        "entry_quote_side": "ask",
        "entry_quote_price": 100.1,
        "entry_quote_time": "2026-11-10T11:59:59+00:00",
        "entry_quote_source": "AlpacaLiveAdapter",
        "entry_quote_time_source": "broker",
        "entry_quote_age_seconds": 1.0,
        "entry_deviation_sl_fraction": 0.5,
        "entry_gate_rejection_rule": "deviation_toward_stop_loss",
    }

    written = db.save_live_signal_rejection(
        signal,
        stage="prepare",
        reason="entry deviation gate rejected (deviation_toward_stop_loss)",
        extra_metadata=gate_record,
    )

    assert written is True
    rows = _rejection_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.signal_id == str(signal.id)
    assert row.deployment_id == DEPLOYMENT
    assert row.stage == "prepare"
    assert row.gate_rejection_rule == "deviation_toward_stop_loss"
    assert row.discord_message_id == "msg-1"
    assert row.entry_quote_bid == pytest.approx(99.9)
    assert row.entry_quote_ask == pytest.approx(100.1)
    assert row.entry_quote_price == pytest.approx(100.1)
    assert row.entry_quote_time.replace(tzinfo=timezone.utc) == datetime(
        2026, 11, 10, 11, 59, 59, tzinfo=timezone.utc
    )
    # A refusal is not a live_signals row: no order may exist for it.
    assert _signal_row(db, signal) is None


def test_rejection_dedupe_skips_a_redelivery(db) -> None:
    signal = _signal(metadata={"discord_message_id": "msg-dup"})
    kwargs = dict(stage="prepare", reason="refused")

    assert db.save_live_signal_rejection(signal, **kwargs) is True
    assert db.save_live_signal_rejection(signal, **kwargs) is False
    assert len(_rejection_rows(db)) == 1


def test_rejection_without_stable_identity_records_every_refusal(db) -> None:
    """No Discord id, no idempotency key, no producer stamp: nothing can tell
    a redelivery apart, so each refusal is recorded honestly (NULL dedupe)."""
    signal = _signal(metadata={})
    kwargs = dict(stage="prepare", reason="refused")

    assert db.save_live_signal_rejection(signal, **kwargs) is True
    assert db.save_live_signal_rejection(signal, **kwargs) is True
    assert len(_rejection_rows(db)) == 2


def test_rejection_dedupe_separates_stages_and_rules(db) -> None:
    """The same signal refused for two different reasons is two data points."""
    signal = _signal(metadata={"discord_message_id": "msg-rules"})

    assert (
        db.save_live_signal_rejection(
            signal, stage="prepare", reason="r1",
            extra_metadata={"entry_gate_rejection_rule": "quote_stale"},
        )
        is True
    )
    assert (
        db.save_live_signal_rejection(
            signal, stage="prepare", reason="r2",
            extra_metadata={"entry_gate_rejection_rule": "quote_at_or_beyond_stop_loss"},
        )
        is True
    )
    assert len(_rejection_rows(db)) == 2


# ── daemon: best-effort rejection persistence ─────────────────────────────────


def _deployment_config() -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=DEPLOYMENT,
        strategy_name="quote-snapshot",
        account_id=ACCOUNT,
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={"max_notional_per_trade": 1.0},
    )


def test_persist_submission_rejection_records_the_gate_quote(db) -> None:
    """End-to-end: the gate's own rejection record lands on the rejection row."""

    class _StaleQuoteAdapter:
        async def get_quote(self, symbol: str):
            return LiveQuote(
                bid=99.9,
                ask=100.1,
                timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
                source="test",
                timestamp_source="broker",
            )

    deployment = _deployment_config()
    signal = _signal(metadata={"discord_message_id": "msg-gate"})

    with pytest.raises(ValueError, match="entry deviation gate") as raised:
        asyncio.run(
            daemon._check_entry_deviation(
                _StaleQuoteAdapter(),
                deployment,
                signal,
                entry_price=100.0,
                stop_loss=96.0,
                take_profit=112.0,
            )
        )

    asyncio.run(
        daemon._persist_submission_rejection(
            deployment, signal, stage="prepare", exc=raised.value
        )
    )

    rows = _rejection_rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.stage == "prepare"
    assert row.gate_rejection_rule == "quote_stale"
    assert row.entry_quote_bid == pytest.approx(99.9)
    assert row.entry_quote_ask == pytest.approx(100.1)
    assert row.entry_quote_price == pytest.approx(100.1)
    assert row.entry_quote_time is not None
    assert row.discord_message_id == "msg-gate"


def test_persist_submission_rejection_never_raises(db, monkeypatch) -> None:
    """Bookkeeping failure is logged and swallowed: the refusal stands."""
    monkeypatch.setattr(
        daemon,
        "save_live_signal_rejection",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    deployment = _deployment_config()
    signal = _signal(metadata={})

    asyncio.run(
        daemon._persist_submission_rejection(
            deployment, signal, stage="prepare", exc=ValueError("refused")
        )
    )
    assert _rejection_rows(db) == []


def test_persist_submission_rejection_skips_an_already_persisted_signal(db) -> None:
    """A refusal after the adapter persisted the signal row must not double-write:
    the live_signals row IS the record, its NULL submit/ack stamps the flag."""
    deployment = _deployment_config()
    signal = _signal(metadata={})
    db.save_live_signal(signal)

    asyncio.run(
        daemon._persist_submission_rejection(
            deployment, signal, stage="submit", exc=ValueError("refused")
        )
    )

    assert _rejection_rows(db) == []
    row = _signal_row(db, signal)
    assert row.order_submitted_at is None
    assert row.broker_ack_at is None


# ── slippage decomposition ────────────────────────────────────────────────────


def test_buy_decomposition_splits_delay_execution_and_spread(db) -> None:
    """Buy @ theoretical 100, quote ask 100.1 (bid 99.9), fill 100.2:

    delay = (100.1 - 100) / 100 = 10 bps adverse; exec = (100.2 - 100.1) / 100
    = 10 bps; spread = (100.1 - 99.9) / 100 = 20 bps.
    """
    signal = _signal(action="buy")
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal, price=100.2))

    row = _serialized(db, signal)
    assert row["signal_delay_bps"] == pytest.approx(10.0)
    assert row["execution_slippage_bps"] == pytest.approx(10.0)
    assert row["spread_bps"] == pytest.approx(20.0)
    assert row["quote_snapshot_missing"] is False
    assert row["quote_bid"] == pytest.approx(99.9)
    assert row["quote_ask"] == pytest.approx(100.1)
    assert row["quote_price"] == pytest.approx(100.1)
    assert row["order_type"] == "market"
    assert row["requested_qty"] == pytest.approx(1.0)
    assert row["producer_at"] is not None
    assert row["message_received_at"] is not None


def test_sell_decomposition_is_adverse_positive(db) -> None:
    """Sell @ theoretical 100, quote bid 99.9 (ask 100.1), fill 99.8:

    delay = -(99.9 - 100) / 100 = 10 bps; exec = -(99.8 - 99.9) / 100 = 10 bps.
    """
    metadata = dict(_QUOTE_METADATA)
    metadata.update(
        {
            "entry_quote_side": "bid",
            "entry_quote_price": 99.9,
        }
    )
    signal = _signal(action="sell", metadata=metadata)
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal, price=99.8))

    row = _serialized(db, signal)
    assert row["signal_delay_bps"] == pytest.approx(10.0)
    assert row["execution_slippage_bps"] == pytest.approx(10.0)
    assert row["spread_bps"] == pytest.approx(20.0)


def test_delay_plus_execution_reconciles_with_legacy_slippage(db) -> None:
    """delay + exec is the adverse-positive total: ``slippage_bps`` for a buy
    (the legacy field is signed ``actual - theoretical``)."""
    signal = _signal(action="buy")
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal, price=100.25))

    row = _serialized(db, signal)
    assert row["signal_delay_bps"] + row["execution_slippage_bps"] == pytest.approx(
        row["slippage_bps"]
    )


def test_fill_without_quote_snapshot_is_flagged_not_zeroed(db) -> None:
    """An exit fill (no gate, no snapshot) must be visibly incomplete."""
    signal = _signal(metadata={}, action="sell")
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal, price=99.8))

    row = _serialized(db, signal)
    assert row["quote_snapshot_missing"] is True
    assert row["signal_delay_bps"] is None
    assert row["execution_slippage_bps"] is None
    assert row["spread_bps"] is None
    # Legacy fields still report what they always did.
    assert row["slippage_bps"] is not None


def test_crossed_quote_is_flagged_as_no_snapshot(db) -> None:
    metadata = dict(_QUOTE_METADATA)
    metadata.update({"entry_quote_bid": 100.2, "entry_quote_ask": 100.1})
    signal = _signal(metadata=metadata)
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal))

    row = _serialized(db, signal)
    assert row["quote_snapshot_missing"] is True
    assert row["signal_delay_bps"] is None
    assert row["spread_bps"] is None


def test_unusable_fill_price_hides_only_execution_slippage(db) -> None:
    signal = _signal()
    db.save_live_signal(signal)
    db.save_live_execution(_execution(signal, price=0.0))
    # Mark the stored price unusable the way the adapters do.
    with db.get_session() as session:
        model = session.exec(
            db.select(db.LiveExecutionModel).where(
                db.LiveExecutionModel.signal_id == str(signal.id)
            )
        ).one()
        model.actual_price_observed = "sent_bad"
        session.add(model)
        session.commit()

    row = _serialized(db, signal)
    assert row["quote_snapshot_missing"] is False
    assert row["signal_delay_bps"] is not None
    assert row["spread_bps"] is not None
    assert row["execution_slippage_bps"] is None


# ── broker paths ──────────────────────────────────────────────────────────────


def test_alpaca_submit_stamps_order_timing(db, monkeypatch) -> None:
    """The stamps bracket the broker call and persist with the order id."""
    from live_runner import alpaca_adapter as alpaca_module
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    order_id = str(uuid4())

    class _Client:
        def submit_order(self, order_data=None, **_kwargs):
            return SimpleNamespace(id=order_id, symbol="AAPL")

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_client = _Client()

    signal = _signal(metadata={**_QUOTE_METADATA, "stop_loss": 95.0, "take_profit": 110.0})
    result = asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert result == order_id
    row = _signal_row(db, signal)
    assert row.broker_order_id == order_id
    assert row.order_submitted_at is not None
    assert row.broker_ack_at is not None
    assert row.order_submitted_at <= row.broker_ack_at
    assert row.entry_quote_bid == pytest.approx(99.9)
    # Sanity: the module under test is the real one (no persistence monkeypatch).
    assert alpaca_module.set_live_signal_broker_order_id is db.set_live_signal_broker_order_id


def test_alpaca_stream_fill_records_order_context(monkeypatch) -> None:
    """A stream fill stores the order's own type and requested quantity."""
    from live_runner import alpaca_adapter as module
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    saved: list[LiveExecution] = []
    monkeypatch.setattr(
        module,
        "save_live_execution_up_to_cumulative",
        lambda execution, _cumulative, **_kwargs: saved.append(execution) or execution.filled_qty,
    )
    monkeypatch.setattr(
        module, "save_live_execution", lambda execution: saved.append(execution) or True
    )

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    signal_id = str(uuid4())
    order_id = str(uuid4())
    update = SimpleNamespace(
        event="fill",
        execution_id=str(uuid4()),
        price="100.2",
        qty="1",
        position_qty="1",
        timestamp=datetime(2026, 11, 10, 12, 0, 30, tzinfo=timezone.utc),
        order=SimpleNamespace(
            id=order_id,
            client_order_id=signal_id,
            symbol="AAPL",
            asset_class="us_equity",
            order_type="limit",
            qty="1",
            filled_qty="1",
            filled_avg_price="100.2",
        ),
    )

    asyncio.run(adapter._on_trade_update(update))

    assert len(saved) == 1
    assert saved[0].order_type == "limit"
    assert saved[0].requested_qty == pytest.approx(1.0)


def test_alpaca_reconciled_fill_records_order_context(db) -> None:
    """The reconciliation path reads the same fields off the order payload."""
    from live_runner.alpaca_adapter import AlpacaLiveAdapter

    signal = _signal(metadata={})
    db.save_live_signal(signal)
    order_id = str(uuid4())
    db.set_live_signal_broker_order_id(str(signal.id), order_id)

    adapter = AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True, account_id=ACCOUNT)
    adapter._connected = True
    order = SimpleNamespace(
        id=order_id,
        client_order_id=str(signal.id),
        symbol="AAPL",
        asset_class="us_equity",
        order_type="market",
        qty="1",
        filled_qty="1",
        filled_avg_price="100.2",
        filled_at=datetime(2026, 11, 10, 12, 0, 30, tzinfo=timezone.utc),
    )
    adapter._trading_client = SimpleNamespace(
        get_order_by_client_id=lambda _cid: order,
        get_order_by_id=lambda _oid: order,
    )

    assert asyncio.run(adapter.reconcile_executions()) >= 0
    rows = _execution_rows(db, signal)
    assert len(rows) == 1
    assert rows[0].order_type == "market"
    assert rows[0].requested_qty == pytest.approx(1.0)


def test_ctrader_stream_fill_records_order_context(isolated_db) -> None:
    database = isolated_db[0]
    database.init_db()
    with database.get_session() as session:
        session.add(database.LiveAccountModel(id="acc-1", broker="ctrader", name="acc-1"))
        session.flush()
        session.add(
            database.LiveDeploymentModel(
                id="acc-1",
                strategy_name="quote-snapshot",
                account_id="acc-1",
                symbols_json=["EURUSD"],
                timeframe="1m",
            )
        )
        session.commit()
    signal_id = "99999999-9999-4999-8999-999999999999"
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id=signal_id,
                deployment_id="acc-1",
                symbol="EURUSD",
                action="BUY",
                theoretical_price=1.2345,
                source="internal",
                broker_order_id="45678",
                timestamp=NOW,
            )
        )
        session.commit()

    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="46736452"
    )
    adapter.account_config_id = "acc-1"
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    adapter._ProtoOAOrderType = SimpleNamespace(
        Name=lambda value: {1: "MARKET", 2: "LIMIT"}.get(value)
    )
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._order_id_to_symbol[45678] = "EURUSD"

    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    stream_order = ProtoOAOrder()
    stream_order.orderId = 45678
    stream_order.clientOrderId = signal_id
    stream_order.orderType = 1
    stream_order.tradeData.volume = 500_000
    stream_order.executedVolume = 500_000
    stream_order.utcLastUpdateTimestamp = 1_700_000_000_000

    event = SimpleNamespace(
        executionType=1,
        order=stream_order,
        deal=SimpleNamespace(
            dealId=6001,
            orderId=45678,
            symbolId=1,
            filledVolume=500_000,
            executionPrice=1.2345,
            executionTimestamp=1_700_000_000_000,
            commission=0,
        ),
        HasField=lambda field: field in {"order", "deal"},
    )

    asyncio.run(adapter._persist_execution_event(event))

    with database.get_session() as session:
        row = session.exec(
            database.select(database.LiveExecutionModel).where(
                database.LiveExecutionModel.signal_id == signal_id
            )
        ).one()
    assert row.order_type == "MARKET"
    # EURUSD fallback scale: volume_per_qty = 100000/100 = 1000.
    assert row.requested_qty == pytest.approx(500.0)


def test_ctrader_reconciled_fill_records_order_context(isolated_db) -> None:
    database = isolated_db[0]
    database.init_db()
    with database.get_session() as session:
        session.add(database.LiveAccountModel(id="acc-1", broker="ctrader", name="acc-1"))
        session.flush()
        session.add(
            database.LiveDeploymentModel(
                id="acc-1",
                strategy_name="quote-snapshot",
                account_id="acc-1",
                symbols_json=["EURUSD"],
                timeframe="1m",
            )
        )
        session.commit()
    signal_id = "88888888-8888-4888-8888-888888888888"
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id=signal_id,
                deployment_id="acc-1",
                symbol="EURUSD",
                action="BUY",
                theoretical_price=1.2345,
                source="internal",
                broker_order_id="45678",
                timestamp=NOW,
            )
        )
        session.commit()

    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="46736452"
    )
    adapter.account_config_id = "acc-1"
    adapter._ProtoOADealStatus = SimpleNamespace(FILLED=2, PARTIALLY_FILLED=3)
    adapter._ProtoOAOrderType = SimpleNamespace(
        Name=lambda value: {1: "MARKET", 2: "LIMIT"}.get(value)
    )
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._order_id_to_symbol[45678] = "EURUSD"

    deal = SimpleNamespace(
        dealId=7001,
        dealStatus=2,
        orderId=45678,
        symbolId=1,
        filledVolume=500_000,
        executionPrice=1.2345,
        executionTimestamp=1_700_000_000_000,
        utcLastUpdateTimestamp=0,
        commission=0,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    order = ProtoOAOrder()
    order.orderType = 1
    order.tradeData.volume = 500_000
    details = SimpleNamespace(order=[order], deal=[deal])
    from uuid import UUID

    recovered = asyncio.run(
        adapter._persist_recovered_deals(
            signal_id=signal_id,
            signal_uuid=UUID(signal_id),
            broker_order_id="45678",
            details_res=details,
        )
    )

    assert recovered == 1
    with database.get_session() as session:
        row = session.exec(
            database.select(database.LiveExecutionModel).where(
                database.LiveExecutionModel.signal_id == signal_id
            )
        ).one()
    assert row.order_type == "MARKET"
    assert row.requested_qty == pytest.approx(500.0)


def test_ctrader_submit_stamps_order_timing(db, monkeypatch) -> None:
    """The stamps bracket the broker call and persist with the order id."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="46736452"
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._ProtoOAExecutionEvent = type("_NoExecutionEvent", (), {})

    async def _symbol_id(_symbol):
        return 1

    async def _submit(_request):
        return SimpleNamespace(orderId=654321)

    monkeypatch.setattr(adapter, "_resolve_symbol_id", _symbol_id)
    monkeypatch.setattr(
        adapter,
        "_build_new_order_request",
        lambda signal, symbol_id: SimpleNamespace(volume=500_000),
    )
    monkeypatch.setattr(adapter, "_submit_new_order_request", _submit)

    signal = LiveSignal(
        deployment_id=DEPLOYMENT,
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.2345,
        timestamp=NOW,
        metadata={"stop_loss": 1.23, "take_profit": 1.24},
    )
    # The deployment row is seeded for account 'acct-quote-snapshot'; the
    # adapter only needs the signal's deployment_id to exist.
    result = asyncio.run(adapter.submit_order(signal, qty=1.0))

    assert result == "654321"
    row = _signal_row(db, signal)
    assert row.broker_order_id == "654321"
    assert row.order_submitted_at is not None
    assert row.broker_ack_at is not None
    assert row.order_submitted_at <= row.broker_ack_at


def test_ctrader_requested_qty_reads_trade_data_volume_real_proto(tmp_path) -> None:
    """``ProtoOAOrder`` has no root ``volume``: the requested volume lives in
    ``tradeData.volume``. A fake shaped like the old (wrong) field hid the
    NULL-in-production bug, so this test uses the real protobuf message."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="46736452",
        accounts_env_path=tmp_path / ".accounts.env",
    )

    order = ProtoOAOrder()
    order.orderId = 45678
    order.orderType = 1
    order.tradeData.volume = 500_000
    assert adapter._requested_order_qty(order, "EURUSD") == pytest.approx(500.0)

    empty = ProtoOAOrder()
    empty.orderId = 45679
    empty.orderType = 1
    assert adapter._requested_order_qty(empty, "EURUSD") is None
    assert adapter._requested_order_qty(None, "EURUSD") is None


def test_extreme_producer_timestamps_degrade_to_null_never_raise(db) -> None:
    """#337 blocker: producer-controlled stamps run through
    ``_lift_optional_datetime`` inside ``save_live_signal``, which the
    adapters call from ``order_not_submitted`` - a raise there refuses the
    order, exits included. Entries and exits must persist with NULL stamps."""
    bad_values = [
        "0001-01-01T00:00:00+05:00",  # parses, then OverflowError in astimezone
        12345,                        # non-string, non-datetime
        {"not": "a date"},
        "not-a-date-at-all",
    ]
    for action in ("buy", "sell"):
        for bad in bad_values:
            signal = _signal(
                action=action,
                metadata={
                    "signal_timestamp": bad,
                    "received_at": bad,
                    "received_at_local": bad,
                    "entry_quote_time": bad,
                },
            )
            db.save_live_signal(signal)  # must not raise
            row = _signal_row(db, signal)
            assert row.producer_at is None
            assert row.message_received_at is None
            assert row.entry_quote_time is None


def test_conversion_overwrites_payload_received_at_local() -> None:
    """``received_at_local`` is a local receipt stamp: a payload value must
    never reach persistence (``setdefault`` used to let it win)."""
    from live_runner.daemon import _trade_signal_to_live_signal
    from risk.models import TradeSignal
    from tests.test_live_execution_safety import _deployment

    deployment = _deployment()
    for action in ("buy", "sell"):
        signal = TradeSignal(
            action=action,
            symbol="AAPL",
            entry_price=100.0,
            stop_loss=98.0 if action == "buy" else None,
            take_profit=104.0 if action == "buy" else None,
            timeframe="1h",
            metadata={
                "received_at_local": "1999-01-01T00:00:00+00:00",
                "signal_timestamp": "0001-01-01T00:00:00+05:00",
            },
        )
        converted = _trade_signal_to_live_signal(signal, deployment)
        stamped = datetime.fromisoformat(converted.metadata["received_at_local"])
        assert stamped.year >= 2026
        # The hostile producer stamp is preserved as raw metadata for audit but
        # must not break the projection: it lifts to NULL, never raises.
        fields = db_live_signal_quote_snapshot_fields_for_test(converted)
        assert fields["producer_at"] is None
        assert fields["message_received_at"] is not None


def db_live_signal_quote_snapshot_fields_for_test(signal):
    from shared.database import _live_signal_quote_snapshot_fields

    return _live_signal_quote_snapshot_fields(signal.metadata)


def test_refusal_log_precedes_rejection_persist(monkeypatch) -> None:
    """#337 should-fix: the operator-visible log must come before the
    rejection write, which is also bounded - a contended database can never
    hold the refusal path (or the per-symbol exit lock) hostage."""
    import logging

    import live_runner.daemon as daemon_mod
    from live_runner.exit_signals import ExitBelowStepError
    from tests.test_live_execution_safety import _deployment

    deployment = _deployment()
    live_signal = _signal(action="sell")
    events: list[str] = []

    class _OrderProbe(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append("log")

    daemon_logger = logging.getLogger("trading.live-runner")
    probe = _OrderProbe()
    daemon_logger.addHandler(probe)

    async def _below_step(*_args, **_kwargs):
        raise ExitBelowStepError(
            "below step", requested_qty=0.001, qty_step=0.01
        )

    def _metadata(*_args, **_kwargs):
        events.append("persist")
        return None

    def _save_rejection(*_args, **_kwargs):
        events.append("persist-write")
        return True

    monkeypatch.setattr(daemon_mod, "prepare_order_with_risk", _below_step)
    monkeypatch.setattr(daemon_mod, "get_live_signal_metadata", _metadata)
    monkeypatch.setattr(daemon_mod, "save_live_signal_rejection", _save_rejection)

    try:
        asyncio.run(
            daemon_mod._prepare_claim_and_submit_signal(
                "acct-quote-snapshot",
                None,
                deployment,
                SimpleNamespace(),
                live_signal,
                is_exit=True,
            )
        )
    finally:
        daemon_logger.removeHandler(probe)

    assert events.index("log") < events.index("persist")
    assert events.index("persist") < events.index("persist-write")
