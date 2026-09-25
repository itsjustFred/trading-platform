"""Tests for ctrader adapter edge-cases and helper behavior.

Covers: get_positions (sign convention, closed-position exclusion, symbol
mapping), broker_order_id roundtrip, token refresh persistence and the
dedupe script.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import ctrader_open_api.messages.OpenApiMessages_pb2 as _real
import ctrader_open_api.messages.OpenApiModelMessages_pb2 as _real_model

from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal

# The reconcile-payload helpers below build real protobuf messages; without
# the package every test in that block would fail on import rather than skip.
pytest.importorskip(
    "ctrader_open_api.messages.OpenApiModelMessages_pb2",
    reason="ctrader-open-api package is not installed in this environment",
)


# ─── cTrader token refresh: re-auth + serialize + atomic persistence ──────────


class _FakeRefreshReq:
    def __init__(self) -> None:
        self.refreshToken = ""


class _FakeRefreshRes:
    def __init__(self, access_token: str, refresh_token: str, expires_in: int = 3600) -> None:
        self.accessToken = access_token
        self.refreshToken = refresh_token
        self.expiresIn = expires_in


class _FakeAccountAuthReq:
    def __init__(self) -> None:
        self.ctidTraderAccountId = 0
        self.accessToken = ""


class _FakeAccountAuthRes:
    pass


def _make_refresh_ready_adapter(tmp_path: Path, **overrides: Any):
    """Adapter wired with fake protobuf types so refresh/auth can run offline."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    accounts_env = overrides.pop("accounts_env_path", tmp_path / ".accounts.env")
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old-access",
        refresh_token="old-refresh",
        account_id="123",
        accounts_env_path=accounts_env,
        **overrides,
    )
    adapter._ProtoOARefreshTokenReq = _FakeRefreshReq
    adapter._ProtoOARefreshTokenRes = _FakeRefreshRes
    adapter._ProtoOAAccountAuthReq = _FakeAccountAuthReq
    adapter._ProtoOAAccountAuthRes = _FakeAccountAuthRes
    return adapter


def _read_env_pair(path: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            pairs[key] = value
    return pairs


def test_refresh_reauthorizes_account_before_retry(tmp_path: Path) -> None:
    """A token-expired account-scoped request must re-run account auth before retry."""
    from live_runner.ctrader_adapter import CTraderApiError

    adapter = _make_refresh_ready_adapter(tmp_path)
    calls: list[str] = []

    async def fake_send_request(request, *, timeout_seconds: int = 10):
        if isinstance(request, _FakeRefreshReq):
            calls.append("refresh")
            return _FakeRefreshRes("new-access", "new-refresh")
        if isinstance(request, _FakeAccountAuthReq):
            calls.append("account_auth")
            return _FakeAccountAuthRes()
        # First business request fails with expired token, then succeeds.
        calls.append("business")
        if calls.count("business") == 1:
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "invalid token")
        return SimpleNamespace(ok=True)

    adapter._send_request = fake_send_request  # type: ignore[assignment]

    business_req = SimpleNamespace(name="symbols")
    result = asyncio.run(
        adapter._send_request_with_refresh_retry(business_req, timeout_seconds=15)
    )

    assert getattr(result, "ok", False) is True
    # Account auth must occur AFTER refresh and BEFORE the successful retry.
    assert "refresh" in calls
    assert "account_auth" in calls
    assert calls.index("account_auth") > calls.index("refresh")
    assert calls.index("account_auth") < len(calls) - 1  # auth before final business retry


def test_parallel_refreshes_persist_one_consistent_token_pair(tmp_path: Path) -> None:
    """Two concurrent refresh attempts must yield exactly one refresh + one consistent pair."""
    adapter = _make_refresh_ready_adapter(tmp_path)
    refresh_count = {"n": 0}

    async def fake_send_request(request, *, timeout_seconds: int = 10):
        if isinstance(request, _FakeRefreshReq):
            refresh_count["n"] += 1
            await asyncio.sleep(0.01)  # widen the race window
            return _FakeRefreshRes("new-access", "new-refresh")
        raise AssertionError("unexpected request")

    adapter._send_request = fake_send_request  # type: ignore[assignment]

    async def run_two() -> tuple[bool, bool]:
        return await asyncio.gather(
            adapter._refresh_access_token(),
            adapter._refresh_access_token(),
        )

    results = asyncio.run(run_two())
    assert results == [True, True]
    assert refresh_count["n"] == 1  # exactly one network refresh, not two
    assert adapter.access_token == "new-access"
    assert adapter.refresh_token == "new-refresh"
    pairs = _read_env_pair(tmp_path / ".accounts.env")
    assert pairs["CTRADER_ACCESS_TOKEN"] == "new-access"
    assert pairs["CTRADER_REFRESH_TOKEN"] == "new-refresh"


def test_crash_between_receipt_and_persist_leaves_no_mixed_tokens(tmp_path: Path) -> None:
    """If persistence fails after receiving new tokens, no mixed pair is left behind."""
    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text(
        "CTRADER_ACCESS_TOKEN=old-access\nCTRADER_REFRESH_TOKEN=old-refresh\n",
        encoding="utf-8",
    )
    adapter = _make_refresh_ready_adapter(tmp_path, accounts_env_path=accounts_env)

    async def fake_send_request(request, *, timeout_seconds: int = 10):
        if isinstance(request, _FakeRefreshReq):
            return _FakeRefreshRes("new-access", "new-refresh")
        raise AssertionError("unexpected request")

    adapter._send_request = fake_send_request  # type: ignore[assignment]

    import live_runner.ctrader_adapter as mod

    def boom(*args, **kwargs):
        raise OSError("simulated crash during persist")

    original = mod._write_env_keys
    mod._write_env_keys = boom  # type: ignore[assignment]
    try:
        ok = asyncio.run(adapter._refresh_access_token())
    finally:
        mod._write_env_keys = original  # type: ignore[assignment]

    assert ok is False
    # In-memory tokens must not have been partially applied.
    assert adapter.access_token == "old-access"
    assert adapter.refresh_token == "old-refresh"
    # File still holds a consistent original pair (never mixed).
    pairs = _read_env_pair(accounts_env)
    assert pairs["CTRADER_ACCESS_TOKEN"] == "old-access"
    assert pairs["CTRADER_REFRESH_TOKEN"] == "old-refresh"


def test_persisted_accounts_file_has_0600_permissions(tmp_path: Path) -> None:
    """Atomic token persistence must chmod the accounts file to 0600."""
    import stat

    from live_runner.ctrader_adapter import _write_env_keys

    accounts_env = tmp_path / ".accounts.env"
    _write_env_keys(
        accounts_env,
        {"CTRADER_ACCESS_TOKEN": "a", "CTRADER_REFRESH_TOKEN": "r"},
    )
    mode = stat.S_IMODE(accounts_env.stat().st_mode)
    assert mode == 0o600
    pairs = _read_env_pair(accounts_env)
    assert pairs == {"CTRADER_ACCESS_TOKEN": "a", "CTRADER_REFRESH_TOKEN": "r"}


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="cross-process flock locking is POSIX-only (fcntl)",
)
def test_env_key_persistence_serializes_concurrent_writers(tmp_path: Path) -> None:
    """The env read-modify-write path must wait on the cross-process file lock."""
    import fcntl
    import subprocess
    import time

    accounts_env = tmp_path / ".accounts.env"
    accounts_env.write_text("EXISTING=1\n", encoding="utf-8")
    lock_path = tmp_path / ".accounts.env.lock"

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "from pathlib import Path; "
                    "from live_runner.ctrader_adapter import _write_env_keys; "
                    "_write_env_keys(Path(sys.argv[1]), {'CTRADER_ACCESS_TOKEN': 'new-access'})"
                ),
                str(accounts_env),
            ]
        )
        time.sleep(0.2)
        assert writer.poll() is None
        assert _read_env_pair(accounts_env) == {"EXISTING": "1"}

        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    writer.wait(timeout=2)
    assert writer.returncode == 0
    assert _read_env_pair(accounts_env) == {
        "EXISTING": "1",
        "CTRADER_ACCESS_TOKEN": "new-access",
    }


@pytest.fixture
def isolated_db(isolated_db_sandbox):
    """Delegates to the shared sandbox fixture but yields the database module
    directly, so these persistence tests keep their ``database = isolated_db``
    style."""
    database, _engine, _data_dir = isolated_db_sandbox
    return database


# ─── cTrader get_positions helpers ────────────────────────────────────────────


def _make_reconcile_adapter(tmp_path: Path, response):
    """Adapter wired so get_positions can run fully offline.

    ``response`` is the ProtoOAReconcileRes payload the fake transport
    returns; build it with :func:`_reconcile_response`.
    """
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    # The module-level importorskip guarantees the protobuf package is here.
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPositionStatus,
        ProtoOATradeSide,
    )
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAReconcileReq,
        ProtoOAReconcileRes,
    )

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
        accounts_env_path=tmp_path / ".accounts.env",
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._ProtoOAReconcileReq = ProtoOAReconcileReq
    adapter._ProtoOAReconcileRes = ProtoOAReconcileRes
    adapter._ProtoOAPositionStatus = ProtoOAPositionStatus
    adapter._ProtoOATradeSide = ProtoOATradeSide

    async def _fake_send_request_with_refresh_retry(request, **_kwargs):
        assert isinstance(request, ProtoOAReconcileReq)
        assert request.ctidTraderAccountId == adapter.ctid_trader_account_id
        return response

    # type: ignore[method-assign]: the real method is replaced by the stub.
    adapter._send_request_with_refresh_retry = _fake_send_request_with_refresh_retry
    return adapter


def _reconcile_response(positions: list) -> Any:
    """Build a ProtoOAReconcileRes carrying the given positions."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAReconcileRes

    response = ProtoOAReconcileRes()
    response.position.extend(positions)
    return response


def _reconcile_position(*, status: int, trade_side: int, symbol_id: int, volume: int):
    """Build a real ProtoOAPosition payload for the reconcile response."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAPosition, ProtoOATradeData

    trade_data = ProtoOATradeData()
    trade_data.symbolId = symbol_id
    trade_data.volume = volume
    trade_data.tradeSide = trade_side
    position = ProtoOAPosition()
    position.positionStatus = status
    position.tradeData.CopyFrom(trade_data)
    position.price = 0.0
    return position


def _run_get_positions(adapter) -> list[dict[str, Any]]:
    return asyncio.run(adapter.get_positions())


def test_get_positions_returns_negative_qty_for_sell(tmp_path: Path):
    """B-10: SELL positions are reported as negative qty in broker volume units."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPositionStatus,
        ProtoOATradeSide,
    )

    adapter = _make_reconcile_adapter(
        tmp_path,
        _reconcile_response(
            [
                _reconcile_position(
                    status=ProtoOAPositionStatus.POSITION_STATUS_OPEN,
                    trade_side=ProtoOATradeSide.SELL,
                    symbol_id=1,
                    volume=5000,
                )
            ],
        ),
    )
    adapter._symbol_name_to_id = {"EURUSD": 1}

    positions = _run_get_positions(adapter)

    assert len(positions) == 1, f"SELL position must be returned, got {positions}"
    assert positions[0]["symbol"] == "EURUSD"
    # 5000 broker volume units == 5.0 qty, signed negative for SELL.
    assert positions[0]["qty"] == -5.0


def test_get_positions_returns_positive_qty_for_buy(tmp_path: Path):
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPositionStatus,
        ProtoOATradeSide,
    )

    adapter = _make_reconcile_adapter(
        tmp_path,
        _reconcile_response(
            [
                _reconcile_position(
                    status=ProtoOAPositionStatus.POSITION_STATUS_OPEN,
                    trade_side=ProtoOATradeSide.BUY,
                    symbol_id=1,
                    volume=5000,
                )
            ],
        ),
    )
    adapter._symbol_name_to_id = {"EURUSD": 1}

    positions = _run_get_positions(adapter)

    assert len(positions) == 1
    assert positions[0]["qty"] == 5.0


def test_get_positions_excludes_closed_positions(tmp_path: Path):
    """B-11: only POSITION_STATUS_OPEN positions may reach the risk engine."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPositionStatus,
        ProtoOATradeSide,
    )

    adapter = _make_reconcile_adapter(
        tmp_path,
        _reconcile_response(
            [
                _reconcile_position(
                    status=ProtoOAPositionStatus.POSITION_STATUS_OPEN,
                    trade_side=ProtoOATradeSide.BUY,
                    symbol_id=1,
                    volume=5000,
                ),
                _reconcile_position(
                    status=ProtoOAPositionStatus.POSITION_STATUS_CLOSED,
                    trade_side=ProtoOATradeSide.BUY,
                    symbol_id=1,
                    volume=9999,
                ),
            ]
        ),
    )
    adapter._symbol_name_to_id = {"EURUSD": 1}

    positions = _run_get_positions(adapter)

    symbols_and_qty = [(p["symbol"], p["qty"]) for p in positions]
    assert ("EURUSD", 5.0) in symbols_and_qty, (
        f"Open BUY position (volume 5000) missing from get_positions: {positions}"
    )
    # The closed position carried volume 9999 (qty 9.999); if it leaked, the
    # result would hold two rows. Assert on the closed volume directly.
    assert all(qty != 9.999 for _sym, qty in symbols_and_qty), (
        f"Closed position (volume 9999) leaked into open positions: {positions}"
    )
    assert len(positions) == 1, f"Expected only the open position: {positions}"


def test_get_positions_maps_symbol_ids_to_names(tmp_path: Path):
    """B-12: the reconcile payload's symbolId must resolve through the map."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPositionStatus,
        ProtoOATradeSide,
    )

    adapter = _make_reconcile_adapter(
        tmp_path,
        _reconcile_response(
            [
                _reconcile_position(
                    status=ProtoOAPositionStatus.POSITION_STATUS_OPEN,
                    trade_side=ProtoOATradeSide.BUY,
                    symbol_id=2,
                    volume=4000,
                )
            ],
        ),
    )
    adapter._symbol_name_to_id = {"EURUSD": 1, "USDJPY": 2}

    positions = _run_get_positions(adapter)

    assert len(positions) == 1
    assert positions[0]["symbol"] == "USDJPY"
    assert positions[0]["qty"] == 4.0


def test_get_positions_falls_back_to_raw_id_for_unknown_symbol(tmp_path: Path):
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPositionStatus,
        ProtoOATradeSide,
    )

    adapter = _make_reconcile_adapter(
        tmp_path,
        _reconcile_response(
            [
                _reconcile_position(
                    status=ProtoOAPositionStatus.POSITION_STATUS_OPEN,
                    trade_side=ProtoOATradeSide.BUY,
                    symbol_id=42,
                    volume=1000,
                )
            ],
        ),
    )
    adapter._symbol_name_to_id = {}

    positions = _run_get_positions(adapter)

    assert positions[0]["symbol"] == "42"
    assert positions[0]["qty"] == 1.0


# ─── Broker order ID roundtrip ────────────────────────────────────────────────


def test_get_live_signal_broker_order_id_roundtrip(isolated_db):
    """Saving and reading broker_order_id must be symmetric."""
    database = isolated_db
    # ACTIVE deployments require a fresh broker demo verification in addition
    # to the is_demo flag, mirroring what account discovery records.
    database.ensure_live_account("acc-1", "ctrader", is_demo=True)
    database.record_live_account_demo_verification("acc-1", is_demo=True)
    deployment = LiveDeploymentConfig(
        id=uuid4(), strategy_name="demo", account_id="acc-1",
        symbols=["AAPL"], timeframe="1h", status="ACTIVE", signal_source="internal",
    )
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)

    # Store
    database.set_live_signal_broker_order_id(str(signal.id), "bo-123")

    # Retrieve
    assert database.get_live_signal_broker_order_id(str(signal.id)) == "bo-123"
    assert database.find_live_signal_id_by_broker_order_id("acc-1", "bo-123") == str(signal.id)


def test_get_live_signal_broker_order_id_missing(isolated_db):
    """Missing broker_order_id must return None or empty."""
    database = isolated_db
    # ACTIVE deployments require a fresh broker demo verification in addition
    # to the is_demo flag, mirroring what account discovery records.
    database.ensure_live_account("acc-1", "ctrader", is_demo=True)
    database.record_live_account_demo_verification("acc-1", is_demo=True)
    deployment = LiveDeploymentConfig(
        id=uuid4(), strategy_name="demo", account_id="acc-1",
        symbols=["AAPL"], timeframe="1h", status="ACTIVE", signal_source="internal",
    )
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)

    assert not database.get_live_signal_broker_order_id(str(signal.id))
    assert database.find_live_signal_id_by_broker_order_id("acc-1", "missing") is None


# ─── dedupe_executions script ─────────────────────────────────────────────────


def test_dedupe_executions_keeps_oldest_row(isolated_db):
    """When duplicates exist, the oldest row (lowest rowid) is kept."""
    database = isolated_db
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)

    ts = datetime.now(timezone.utc)
    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=10.0,
            commission=0.0,
            timestamp=ts,
            broker_order_id="bo-1",
            dedupe_key="dup:1",
        )
    )
    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=10.0,
            commission=0.0,
            timestamp=ts,
            broker_order_id="bo-1",
            dedupe_key="dup:2",
        )
    )

    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 2

    from scripts.dedupe_executions import dedupe_executions

    deleted = dedupe_executions(confirm=True)

    assert deleted == 1
    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 1
    assert rows[0].dedupe_key == "dup:1"  # oldest (first inserted) kept


def test_dedupe_executions_dry_run_does_not_delete(isolated_db):
    """dry_run mode must report duplicates without deleting."""
    database = isolated_db
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)

    ts = datetime.now(timezone.utc)
    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=10.0,
            commission=0.0,
            timestamp=ts,
            broker_order_id="bo-1",
            dedupe_key="dup:1",
        )
    )
    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=10.0,
            commission=0.0,
            timestamp=ts,
            broker_order_id="bo-1",
            dedupe_key="dup:2",
        )
    )

    from scripts.dedupe_executions import dedupe_executions

    would_delete = dedupe_executions(dry_run=True)

    assert would_delete == 1
    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 2  # unchanged


def test_dedupe_executions_idempotent_on_clean_table(isolated_db):
    """Running dedupe on a clean table must return 0."""
    database = isolated_db
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={},
    )
    database.save_live_signal(signal)

    ts = datetime.now(timezone.utc)
    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=10.0,
            commission=0.0,
            timestamp=ts,
            broker_order_id="bo-1",
            dedupe_key="unique:1",
        )
    )
    database.save_live_execution(
        LiveExecution(
            signal_id=signal.id,
            actual_price=100.0,
            filled_qty=20.0,
            commission=0.0,
            timestamp=ts,
            broker_order_id="bo-1",
            dedupe_key="unique:2",
        )
    )

    from scripts.dedupe_executions import dedupe_executions

    deleted = dedupe_executions()

    assert deleted == 0
    with database.get_session() as session:
        rows = session.exec(database.select(database.LiveExecutionModel)).all()
    assert len(rows) == 2


# ─── cTrader cancel / flatten (STOPPED semantics) ───────────────────────────


class _FakeCTraderAdapter:
    """Exercises the cTrader cancel/flatten logic without a broker connection."""

    def __init__(self, orders, positions):
        self._orders = orders
        self._positions = positions
        self.sent = []
        self.ctid_trader_account_id = 10105151
        self._connected = True
        self._authenticated = True

    def _raise_if_terminal_auth_failed(self):
        return None

    def _raise_terminal_auth_from_api_error(self, exc):
        return None

    # Use the REAL protobuf message classes. Hand-rolled stand-ins accepted
    # whatever the adapter assigned to them, so a str being assigned to an
    # int64 field (which raises TypeError on the wire) passed these tests.
    _ProtoOAOrderListReq = _real.ProtoOAOrderListReq
    _ProtoOACancelOrderReq = _real.ProtoOACancelOrderReq
    _ProtoOAClosePositionReq = _real.ProtoOAClosePositionReq
    _ProtoOAOrderType = _real_model.ProtoOAOrderType

    class _OrderListRes:
        """Real response shape: the repeated field is named `order`, not `orders`."""

        def __init__(self, orders):
            self.order = list(orders)

    _ProtoOAOrderListRes = _OrderListRes

    async def connect(self):
        return None

    async def _send_request_with_refresh_retry(self, request, timeout_seconds=None):
        self.sent.append(request)
        if hasattr(request, "fromTimestamp") and hasattr(request, "toTimestamp"):
            return self._OrderListRes(self._orders)
        return object()

    async def cancel_open_orders(self):
        from live_runner.ctrader_adapter import CTraderLiveAdapter

        source = CTraderLiveAdapter.cancel_open_orders
        return await source(self)


def _order(order_id: int, order_type: int):
    """A real ProtoOAOrder: ids are int64, so a string id would be rejected."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    order = ProtoOAOrder()
    order.orderId = order_id
    order.orderType = order_type
    return order


async def _cancel(orders):
    adapter = _FakeCTraderAdapter(orders, [])
    cancelled = await adapter.cancel_open_orders()
    return adapter, cancelled


def _run(coro):
    return asyncio.run(coro)


def test_cancel_skips_market_orders_and_cancels_resting_ones():
    adapter, cancelled = _run(
        _cancel(
            [
                _order(101, 1),  # MARKET
                _order(102, 2),  # LIMIT
                _order(103, 4),  # STOP_LOSS_TAKE_PROFIT
                _order(104, 6),  # STOP_LIMIT
            ]
        )
    )
    assert cancelled == 3, "market orders fill immediately and cannot be cancelled"
    ids = [int(getattr(r, "orderId", 0) or 0) for r in adapter.sent if getattr(r, "orderId", 0)]
    assert 101 not in ids, "market orders fill immediately and cannot be cancelled"
    assert {102, 103, 104} <= set(ids)


def test_cancel_with_no_orders_is_a_no_op():
    adapter, cancelled = _run(_cancel([]))
    assert cancelled == 0
    # The order-list request is still sent; only the cancel requests are absent.
    assert all(not getattr(r, "orderId", "") for r in adapter.sent)


def test_cancel_rejects_an_unexpected_response_type():
    adapter = _FakeCTraderAdapter([], [])

    async def _bad(request, timeout_seconds=None):
        return "not-an-order-list-response"

    adapter._send_request_with_refresh_retry = _bad
    with pytest.raises(RuntimeError, match="Unexpected order-list response"):
        _run(adapter.cancel_open_orders())


def test_flatten_closes_every_open_position():
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    positions = [_real_position(201, 1000), _real_position(202, 2000)]
    adapter = _FakeCTraderAdapter([], positions)

    class _ReconcileRes:
        pass

    async def _reconcile(request, timeout_seconds=None):
        return _ReconcileRes()

    async def _positions():
        return positions

    adapter._fetch_open_positions = _positions
    closed = _run(CTraderLiveAdapter.close_all_positions(adapter))
    assert closed == 2
    closed_ids = [int(r.positionId) for r in adapter.sent]
    assert closed_ids == [201, 202]
    # A full close must carry the position volume; cTrader rejects volume 0.
    assert all(int(r.volume) > 0 for r in adapter.sent)


def test_flatten_skips_positions_without_an_id():
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    positions = [_real_position(0, 0)]
    adapter = _FakeCTraderAdapter([], positions)

    async def _positions():
        return positions

    adapter._fetch_open_positions = _positions
    assert _run(CTraderLiveAdapter.close_all_positions(adapter)) == 0
    assert adapter.sent == []


def test_both_brokers_now_advertise_the_controls():
    from live_runner.alpaca_adapter import AlpacaLiveAdapter
    from live_runner.base_adapter import BaseLiveAdapter
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    for adapter_cls in (AlpacaLiveAdapter, CTraderLiveAdapter):
        assert adapter_cls.supports_cancel_orders.fget(object()) is True
        assert adapter_cls.supports_flatten.fget(object()) is True

    # A base adapter that implements neither must not claim support.
    class _Bare(BaseLiveAdapter):
        async def connect(self): ...
        async def submit_order(self, signal, *, qty=None): return ""
        async def listen_executions(self): ...
        async def get_balance(self): return (0.0, 0.0)

    bare = _Bare()
    assert bare.supports_cancel_orders is False
    assert bare.supports_flatten is False


def _real_position(position_id: int, volume: int):
    """A real ProtoOAPosition with the fields the adapter reads."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAPosition

    position = ProtoOAPosition()
    position.positionId = position_id
    position.positionStatus = 1  # POSITION_STATUS_OPEN
    trade_data = position.tradeData
    trade_data.symbolId = 1
    trade_data.volume = volume
    return position


# ─── only working orders may be cancelled ───────────────────────────────────


def _make_cancel_adapter(tmp_path: Path, orders):
    """Adapter whose transport answers the order list, then records cancels."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOACancelOrderReq,
        ProtoOAOrderListReq,
        ProtoOAOrderListRes,
    )

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
        accounts_env_path=tmp_path / ".accounts.env",
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._ProtoOAOrderListReq = ProtoOAOrderListReq
    adapter._ProtoOAOrderListRes = ProtoOAOrderListRes
    adapter._ProtoOACancelOrderReq = ProtoOACancelOrderReq
    adapter._ProtoOAOrderType = ProtoOAOrderType
    adapter._ProtoOAOrderStatus = ProtoOAOrderStatus

    response = ProtoOAOrderListRes()
    for order in orders:
        response.order.add().CopyFrom(order)

    cancelled: list[int] = []
    state = {"listed": False}

    async def _fake_send(request, **_kwargs):
        if isinstance(request, ProtoOAOrderListReq):
            state["listed"] = True
            return response
        if isinstance(request, ProtoOACancelOrderReq):
            cancelled.append(int(request.orderId))
            return ProtoOAOrderListRes()
        raise AssertionError(f"unexpected request {type(request).__name__}")

    adapter._send_request_with_refresh_retry = _fake_send
    adapter.cancelled_ids = cancelled
    return adapter


def test_cancel_open_orders_skips_filled_and_cancelled_orders(tmp_path):
    """A terminal order must not be sent for cancellation.

    cTrader rejects it, and the rejection propagates out of the loop, so the
    remaining working orders would stay resting after a STOP.
    """
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )

    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    def _order(order_id: int, status: int) -> "ProtoOAOrder":
        order = ProtoOAOrder()
        order.orderId = order_id
        order.orderType = ProtoOAOrderType.LIMIT
        order.orderStatus = status
        return order

    adapter = _make_cancel_adapter(
        tmp_path,
        [
            _order(11, ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED),
            _order(22, ProtoOAOrderStatus.ORDER_STATUS_FILLED),
            _order(33, ProtoOAOrderStatus.ORDER_STATUS_CANCELLED),
            _order(44, ProtoOAOrderStatus.ORDER_STATUS_REJECTED),
            _order(55, ProtoOAOrderStatus.ORDER_STATUS_EXPIRED),
        ],
    )
    count = asyncio.run(adapter.cancel_open_orders())

    assert adapter.cancelled_ids == [11], (
        f"only the accepted order may be cancelled, got {adapter.cancelled_ids}"
    )
    assert count == 1


def test_cancel_open_orders_still_cancels_working_orders_of_every_type(tmp_path):
    """Status filtering must not narrow the cancellable-type set."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrder,
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )

    orders = []
    for index, order_type in enumerate(
        (
            ProtoOAOrderType.LIMIT,
            ProtoOAOrderType.STOP,
            ProtoOAOrderType.STOP_LIMIT,
        ),
        start=1,
    ):
        order = ProtoOAOrder()
        order.orderId = index
        order.orderType = order_type
        order.orderStatus = ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED
        orders.append(order)

    # MARKET orders fill immediately and cannot be cancelled.
    market = ProtoOAOrder()
    market.orderId = 99
    market.orderType = ProtoOAOrderType.MARKET
    market.orderStatus = ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED
    orders.append(market)

    adapter = _make_cancel_adapter(tmp_path, orders)
    count = asyncio.run(adapter.cancel_open_orders())

    assert adapter.cancelled_ids == [1, 2, 3]
    assert count == 3


def test_cancel_open_orders_treats_a_missing_status_as_working(tmp_path):
    """An unrecognised response shape must not silently leave orders resting."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrder,
        ProtoOAOrderType,
    )

    order = ProtoOAOrder()  # orderStatus left at its default (unset)
    order.orderId = 77
    order.orderType = ProtoOAOrderType.LIMIT

    adapter = _make_cancel_adapter(tmp_path, [order])
    count = asyncio.run(adapter.cancel_open_orders())

    assert adapter.cancelled_ids == [77], (
        f"a missing status must still be cancelled, got {adapter.cancelled_ids}"
    )
    assert count == 1
