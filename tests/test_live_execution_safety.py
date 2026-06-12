"""Safety-net tests for live execution, risk sizing boundaries, persistence and recovery.

These pin the behaviors that mutation testing showed were unprotected:
pip scaling, fallback-stop geometry, submit guards, fill persistence/idempotency,
and fail-closed recovery decisions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from live_runner.base_adapter import BaseLiveAdapter
from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import TradeSignal
from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal


# ── Risk engine boundaries ─────────────────────────────────────────────────────


def _engine(**overrides: Any) -> RiskEngine:
    config = RiskConfig(
        max_risk_per_trade=overrides.pop("max_risk_per_trade", 0.01),
        max_total_exposure=overrides.pop("max_total_exposure", 0.05),
        max_positions=overrides.pop("max_positions", 5),
        default_sl_atr_multiplier=overrides.pop("default_sl_atr_multiplier", 2.0),
        **overrides,
    )
    return RiskEngine(config)


def test_position_size_scales_inversely_with_pip_value():
    """Kills mutant: dropping `* pip` from the sizing formula."""
    signal_kwargs = dict(
        action="buy",
        symbol="EURUSD",
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=None,
        timeframe="1h",
    )
    sized_pip_1 = _engine().calculate_position_size(
        signal=TradeSignal(**signal_kwargs, metadata={}),
        account_balance=10_000.0,
        pip_value=1.0,
        current_exposure=0.0,
    )
    sized_pip_10 = _engine().calculate_position_size(
        signal=TradeSignal(**signal_kwargs, metadata={}),
        account_balance=10_000.0,
        pip_value=10.0,
        current_exposure=0.0,
    )

    assert sized_pip_1.position_size == pytest.approx(50.0)
    assert sized_pip_10.position_size == pytest.approx(5.0)


def test_fallback_stop_is_below_entry_for_buys_and_above_for_sells():
    """Kills mutant: fallback stop placed on the wrong side of entry."""
    buy = TradeSignal(
        action="buy", symbol="AAPL", entry_price=100.0, stop_loss=None,
        take_profit=None, timeframe="1h", metadata={"atr": 1.5},
    )
    sell = TradeSignal(
        action="sell", symbol="AAPL", entry_price=100.0, stop_loss=None,
        take_profit=None, timeframe="1h", metadata={"atr": 1.5},
    )
    _engine().calculate_position_size(
        signal=buy, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0
    )
    _engine().calculate_position_size(
        signal=sell, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0
    )

    assert buy.stop_loss is not None and buy.stop_loss < buy.entry_price
    assert sell.stop_loss is not None and sell.stop_loss > sell.entry_price


def test_fallback_stop_without_atr_is_percent_of_price():
    """No-ATR fallback must scale with price, never an absolute pip distance.

    With the old absolute $2 fallback, a 100k asset got a stop 0.002% away and an
    absurd position size. Percent-based: distance == entry * fallback_stop_pct.
    """
    signal = TradeSignal(
        action="buy", symbol="BTCUSD", entry_price=100_000.0, stop_loss=None,
        take_profit=None, timeframe="1h", metadata={},
    )
    sized = _engine().calculate_position_size(
        signal=signal, account_balance=10_000.0, pip_value=1.0, current_exposure=0.0
    )

    expected_distance = 100_000.0 * 0.02
    assert signal.stop_loss == pytest.approx(100_000.0 - expected_distance)
    # risk_amount (1% of 10k) / distance (2000) = 0.05 units, not hundreds of BTC.
    assert sized.position_size == pytest.approx(0.05)


def test_unknown_deployment_status_is_fail_closed():
    """Kills mutant: unknown status normalized to ACTIVE (trading a corrupt row)."""
    from live_runner.daemon import _normalize_deployment_status

    assert _normalize_deployment_status("ACTIVE") == "ACTIVE"
    assert _normalize_deployment_status("paused") == "PAUSED"
    assert _normalize_deployment_status("DISABLED") == "UNKNOWN"
    assert _normalize_deployment_status("") == "UNKNOWN"
    assert _normalize_deployment_status(None) == "UNKNOWN"


# ── Live submission guards ─────────────────────────────────────────────────────


@dataclass
class _PositionsAdapter(BaseLiveAdapter):
    positions: list[dict[str, Any]] = field(default_factory=list)
    submitted_signal: LiveSignal | None = None

    async def connect(self) -> None:
        return None

    async def submit_order(self, signal: LiveSignal) -> str:
        self.submitted_signal = signal
        return "order-1"

    async def listen_executions(self) -> None:
        return None

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)


def _deployment(**settings: Any) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid4()),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["EURUSD"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
        signal_config={},
        settings={"max_risk_per_trade": 0.01, "max_total_exposure": 0.05, **settings},
    )


def _live_signal(deployment: LiveDeploymentConfig) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="EURUSD",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0},
    )


def test_submit_order_with_risk_blocks_when_broker_positions_fill_max_positions():
    """max_positions must bind on broker truth, not caller-supplied metadata."""
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment(max_positions=2)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 10.0},
            {"symbol": "MSFT", "qty": 5.0},
        ]
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == 0.0


def test_submit_order_with_risk_blocks_when_broker_exposure_consumes_cap():
    """max_total_exposure must bind using estimated open risk per broker position."""
    from live_runner.daemon import submit_order_with_risk

    # 2 open positions * 1% risk == 2% == max_total_exposure -> nothing left.
    deployment = _deployment(max_total_exposure=0.02, max_positions=10)
    adapter = _PositionsAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 10.0},
            {"symbol": "MSFT", "qty": 5.0},
        ]
    )

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == 0.0


def test_submit_order_with_risk_sizes_normally_when_flat():
    from live_runner.daemon import submit_order_with_risk

    deployment = _deployment()
    adapter = _PositionsAdapter(positions=[])

    asyncio.run(submit_order_with_risk(adapter, deployment, _live_signal(deployment)))

    assert adapter.submitted_signal is not None
    assert adapter.submitted_signal.metadata["qty"] == pytest.approx(200.0)


def test_alpaca_submit_order_blocks_non_positive_quantity(monkeypatch):
    """Kills mutant: relaxing the qty <= 0 guard to qty < 0."""
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    adapter._connected = True
    adapter._trading_client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(cash="100000", equity="100000"),
        submit_order=lambda **_kwargs: pytest.fail("order must not reach the broker"),
    )
    monkeypatch.setattr(module, "save_live_signal", lambda _signal: None)

    # max positions reached -> risk engine returns qty 0 -> submit must refuse.
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "max_positions": 1,
            "current_positions": 1,
        },
    )

    with pytest.raises(ValueError, match="non-positive quantity"):
        asyncio.run(adapter.submit_order(signal))


def test_ctrader_volume_rejects_missing_and_below_minimum_qty():
    """Volume floor must not silently override risk sizing; no default size."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
    )

    def _signal(metadata: dict[str, Any]) -> LiveSignal:
        return LiveSignal(
            deployment_id="dep-1",
            symbol="EURUSD",
            action="buy",
            theoretical_price=1.1,
            timestamp=datetime.now(timezone.utc),
            metadata=metadata,
        )

    with pytest.raises(ValueError, match="no positive quantity"):
        adapter._resolve_order_volume(_signal({}))
    with pytest.raises(ValueError, match="below the broker minimum"):
        adapter._resolve_order_volume(_signal({"qty": 0.07}))
    assert adapter._resolve_order_volume(_signal({"qty": 5.0})) == 5000
    assert adapter._resolve_order_volume(_signal({"ctrader_volume": 2000})) == 2000


def test_processor_market_data_refuses_synthetic_fabrication():
    from live_runner.daemon import _build_processor_market_data

    signal = TradeSignal(
        action="buy", symbol="AAPL", entry_price=100.0, stop_loss=98.0,
        take_profit=None, timeframe="5m", metadata={},
    )

    with pytest.raises(ValueError, match="refusing to fabricate"):
        _build_processor_market_data(signal, timeframe="5m")


# ── Persistence durability / idempotency / recovery reads ─────────────────────


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path: Path):
    import shared.database as database

    engine = database.create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "ENGINE", engine)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    yield database
    engine.dispose()


def _persist_signal(database: Any) -> LiveSignal:
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)
    return signal


def test_save_live_execution_is_durable(isolated_db):
    """Kills mutant: removing the commit (fills silently rolled back)."""
    database = isolated_db
    signal = _persist_signal(database)
    execution = LiveExecution(
        signal_id=signal.id,
        actual_price=100.5,
        filled_qty=10.0,
        commission=0.1,
        timestamp=datetime.now(timezone.utc),
        broker_order_id="bo-1",
        dedupe_key="alpaca:exec:e-1",
    )

    assert database.save_live_execution(execution) is True

    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 1
    assert rows[0].filled_qty == 10.0
    assert rows[0].dedupe_key == "alpaca:exec:e-1"


def test_save_live_execution_skips_duplicate_dedupe_key(isolated_db):
    database = isolated_db
    signal = _persist_signal(database)

    def _execution() -> LiveExecution:
        return LiveExecution(
            signal_id=signal.id,
            actual_price=100.5,
            filled_qty=10.0,
            commission=0.0,
            timestamp=datetime.now(timezone.utc),
            broker_order_id="bo-1",
            dedupe_key="alpaca:exec:dup",
        )

    assert database.save_live_execution(_execution()) is True
    assert database.save_live_execution(_execution()) is False

    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 1


def test_broker_order_id_roundtrip_and_missing_execution_lookup(isolated_db):
    database = isolated_db
    signal = _persist_signal(database)

    assert database.set_live_signal_broker_order_id(str(signal.id), "bo-42") is True
    assert database.find_live_signal_id_by_broker_order_id("bo-42") == str(signal.id)
    assert database.find_live_signal_id_by_broker_order_id("missing") is None

    # Signal has no execution yet -> reported for reconciliation.
    assert database.list_live_signal_ids_missing_executions() == [str(signal.id)]

    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=1.0,
            commission=0.0,
            timestamp=datetime.now(timezone.utc),
            broker_order_id="bo-42",
            dedupe_key=None,
        )
    )
    assert database.list_live_signal_ids_missing_executions() == []


def test_alpaca_partial_fill_persists_event_qty_not_cumulative(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    saved: list[LiveExecution] = []

    def _capture(execution: LiveExecution) -> bool:
        saved.append(execution)
        return True

    monkeypatch.setattr(module, "save_live_execution", _capture)

    signal_id = str(uuid4())
    update = SimpleNamespace(
        event="partial_fill",
        qty="100",  # per-event quantity
        price="50.0",
        timestamp=datetime.now(timezone.utc),
        execution_id="exec-9",
        order=SimpleNamespace(
            client_order_id=signal_id,
            id="bo-9",
            filled_qty="3500",  # cumulative - must NOT be persisted
            filled_avg_price="49.9",
            commission=None,
        ),
    )

    asyncio.run(adapter._on_trade_update(update))

    assert len(saved) == 1
    assert saved[0].filled_qty == 100.0
    assert saved[0].dedupe_key == "alpaca:exec:exec-9"
    assert saved[0].broker_order_id == "bo-9"


def test_alpaca_partial_fill_without_event_qty_is_skipped(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    update = SimpleNamespace(
        event="partial_fill",
        timestamp=datetime.now(timezone.utc),
        order=SimpleNamespace(
            client_order_id=str(uuid4()),
            id="bo-10",
            filled_qty="3500",
            filled_avg_price="49.9",
            commission=None,
        ),
    )

    asyncio.run(adapter._on_trade_update(update))

    assert saved == []


def test_alpaca_reconcile_backfills_missing_fill(monkeypatch):
    from live_runner import alpaca_adapter as module

    adapter = module.AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
    signal_id = str(uuid4())

    monkeypatch.setattr(
        module, "list_live_signal_ids_missing_executions", lambda _limit: [signal_id]
    )
    saved: list[LiveExecution] = []
    monkeypatch.setattr(module, "save_live_execution", lambda e: saved.append(e) or True)

    adapter._trading_client = SimpleNamespace(
        get_order_by_client_id=lambda _cid: SimpleNamespace(
            id="bo-77",
            filled_qty="25",
            filled_avg_price="101.5",
            filled_at=datetime.now(timezone.utc),
        )
    )

    recovered = asyncio.run(adapter.reconcile_executions())

    assert recovered == 1
    assert saved[0].filled_qty == 25.0
    assert saved[0].broker_order_id == "bo-77"
    assert saved[0].dedupe_key == "alpaca:order:bo-77:reconciled"
    assert str(saved[0].signal_id) == signal_id


# ── Service restart semantics ──────────────────────────────────────────────────


def test_recover_failed_services_respects_paused_and_stopped(monkeypatch):
    """Restart must not resurrect services an operator paused or stopped."""
    from live_runner import daemon as module
    from shared.models import Service, ServiceStatus, ServiceType

    def _service(name: str, status: ServiceStatus) -> Service:
        return Service(
            name=name,
            type=ServiceType.DISCORD_BOT,
            status=status,
            config={},
            created_at=datetime.now(timezone.utc),
        )

    services = [
        _service("paused-bot", ServiceStatus.PAUSED),
        _service("stopped-bot", ServiceStatus.STOPPED),
        _service("failed-bot", ServiceStatus.FAILED),
        _service("running-bot", ServiceStatus.RUNNING),
    ]
    updated: list[tuple[str, ServiceStatus]] = []

    monkeypatch.setattr(module, "list_services", lambda: services)
    monkeypatch.setattr(
        module,
        "update_service_status",
        lambda service_id, status: updated.append((service_id, status)),
    )

    module._recover_failed_services()

    assert len(updated) == 1
    assert updated[0][0] == str(services[2].id)
    assert updated[0][1] == ServiceStatus.RUNNING
