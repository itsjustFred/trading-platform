"""Tests for ctrader adapter edge-cases and helper behavior.

Covers: get_positions (sign convention, closed-position exclusion, symbol
mapping), broker_order_id roundtrip, token refresh persistence and the
dedupe script.
"""

from __future__ import annotations

import asyncio
import io
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal
from tests.conftest import seed_live_deployments

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


def test_env_file_lock_uses_windows_primitives(monkeypatch) -> None:
    import live_runner.ctrader_adapter as module

    calls: list[tuple[int, int, int]] = []

    class _Msvcrt(ModuleType):
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(descriptor: int, mode: int, size: int) -> None:
            calls.append((descriptor, mode, size))

    class _Handle(io.StringIO):
        def fileno(self) -> int:
            return 17

    monkeypatch.setattr(module.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "msvcrt", _Msvcrt("msvcrt"))
    handle = _Handle()

    with module._exclusive_file_lock(handle):
        pass

    assert handle.getvalue() == "\0"
    assert calls == [(17, _Msvcrt.LK_NBLCK, 1), (17, _Msvcrt.LK_UNLCK, 1)]


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

    # Generous budget: the child pays a full cold import (sqlmodel chain,
    # several seconds on loaded boxes); the lock semantics above — blocked
    # while held, correct content after — are what this test verifies.
    writer.wait(timeout=30)
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
    # `live_signals.deployment_id` carries a foreign key, so the synthetic id these persistence
    # tests write needs a parent row like any other signal.
    seed_live_deployments(database, ["dep-1"])
    return database


# ─── cTrader get_positions helpers ────────────────────────────────────────────


def _make_reconcile_adapter(tmp_path: Path, response):
    """Adapter wired so get_positions can run fully offline.

    ``response`` is the ProtoOAReconcileRes payload the fake transport
    returns; build it with :func:`_reconcile_response`.
    """
    # The module-level importorskip guarantees the protobuf package is here.
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAReconcileReq,
        ProtoOAReconcileRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAPositionStatus,
        ProtoOATradeSide,
    )

    from live_runner.ctrader_adapter import CTraderLiveAdapter

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


def _reconcile_position(
    *, status: int, trade_side: int, symbol_id: int, volume: int, position_id: int = 0
):
    """Build a real ProtoOAPosition payload for the reconcile response."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAPosition, ProtoOATradeData

    trade_data = ProtoOATradeData()
    trade_data.symbolId = symbol_id
    trade_data.volume = volume
    trade_data.tradeSide = trade_side
    position = ProtoOAPosition()
    position.positionId = position_id
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


def test_get_positions_preserves_broker_position_id(tmp_path: Path):
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
                    position_id=77,
                )
            ]
        ),
    )
    adapter._symbol_name_to_id = {"EURUSD": 1}

    positions = _run_get_positions(adapter)

    assert positions[0]["position_id"] == 77
    assert adapter._position_ids_by_symbol["EURUSD"] == (77,)


def test_get_positions_falls_back_to_raw_id_for_unknown_symbol(tmp_path: Path):
    """#80: an unmapped symbol id fails closed — guessing FX would misstate
    XAU/index exposure by 10-1000x."""
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

    with pytest.raises(ValueError, match="not in the symbols cache"):
        _run_get_positions(adapter)


def test_position_scoped_close_order_recovery_uses_durable_position_id(tmp_path: Path) -> None:
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAOrderListByPositionIdReq,
        ProtoOAOrderListByPositionIdRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    adapter = _make_refresh_ready_adapter(tmp_path)
    adapter._ProtoOAOrderListByPositionIdReq = ProtoOAOrderListByPositionIdReq
    adapter._ProtoOAOrderListByPositionIdRes = ProtoOAOrderListByPositionIdRes
    response = ProtoOAOrderListByPositionIdRes(
        order=[
            ProtoOAOrder(
                orderId=901,
                positionId=77,
                closingOrder=True,
            )
        ],
        hasMore=False,
    )
    requests: list[ProtoOAOrderListByPositionIdReq] = []

    async def fake_send(request, *, timeout_seconds: int = 15):
        requests.append(request)
        return response

    adapter._send_request_with_refresh_retry = fake_send  # type: ignore[assignment]
    recovered = asyncio.run(
        adapter._recover_broker_order_id(
            str(uuid4()),
            datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),
            position_id=77,
        )
    )

    assert recovered == "901"
    assert len(requests) == 1
    assert requests[0].positionId == 77
    assert requests[0].fromTimestamp < requests[0].toTimestamp


# ─── Broker order ID roundtrip ────────────────────────────────────────────────


def test_get_live_signal_broker_order_id_roundtrip(isolated_db):
    """Saving and reading broker_order_id must be symmetric."""
    database = isolated_db
    database.ensure_live_account("acc-1", "ctrader")
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


def test_get_live_signal_metadata_roundtrip(isolated_db):
    database = isolated_db
    database.ensure_live_account("acc-1", "ctrader")
    deployment = LiveDeploymentConfig(
        id=uuid4(),
        strategy_name="demo",
        account_id="acc-1",
        symbols=["AAPL"],
        timeframe="1h",
        status="ACTIVE",
        signal_source="internal",
    )
    database.create_live_deployment(deployment)
    signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"risk_sizing_mode": "exit_only", "position_id": 77},
    )
    database.save_live_signal(signal)

    assert database.get_live_signal_metadata(str(signal.id)) == {
        "risk_sizing_mode": "exit_only",
        "position_id": 77,
    }


def test_get_live_signal_broker_order_id_missing(isolated_db):
    """Missing broker_order_id must return None or empty."""
    database = isolated_db
    database.ensure_live_account("acc-1", "ctrader")
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


# ─── STOPPED controls: cancel and flatten must report honestly ────────────────
#
# The adapter used to skip unreadable positions and orders and log the rest, so the caller saw
# a count and could not tell a complete sweep from a partial one. `ControlOutcome` carries the
# items it could not address and the items the broker refused; only `complete` may be read as
# "the account is disarmed".


def _control_adapter(*, orders=(), positions=()):
    """An adapter wired to real proto messages and a fixed reconcile snapshot."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOACancelOrderReq,
        ProtoOAClosePositionReq,
        ProtoOAOrderErrorEvent,
        ProtoOAReconcileRes,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
        ProtoOAPositionStatus,
    )

    from live_runner.ctrader_adapter import CTraderApiError, CTraderLiveAdapter

    adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
    adapter.ctid_trader_account_id = 46736452
    adapter._terminal_auth_failure_reason = ""
    adapter._terminal_auth_failure_code = ""
    adapter._ProtoOACancelOrderReq = ProtoOACancelOrderReq
    adapter._ProtoOAClosePositionReq = ProtoOAClosePositionReq
    adapter._ProtoOAOrderType = ProtoOAOrderType
    adapter._ProtoOAOrderStatus = ProtoOAOrderStatus
    adapter._ProtoOAPositionStatus = ProtoOAPositionStatus
    adapter._ProtoOAReconcileRes = ProtoOAReconcileRes
    adapter._ProtoOAOrderErrorEvent = ProtoOAOrderErrorEvent
    adapter.sent: list[Any] = []
    adapter.refuse: set[int] = set()
    # Targets answered with a real `ProtoOAOrderErrorEvent` (how cTrader reports a trading
    # refusal: a *successful* response, not a `ProtoOAErrorRes`).
    adapter.refuse_event: dict[int, str] = {}

    snapshot = ProtoOAReconcileRes()
    for order in orders:
        snapshot.order.append(order)
    for position in positions:
        snapshot.position.append(position)

    async def _reconcile_state():
        return snapshot

    async def _send(request, **_kwargs):
        adapter.sent.append(request)
        target = int(getattr(request, "orderId", 0) or getattr(request, "positionId", 0) or 0)
        if target in adapter.refuse:
            raise CTraderApiError("ORDER_REJECTED", f"refused {target}")
        if target in adapter.refuse_event:
            return ProtoOAOrderErrorEvent(
                ctidTraderAccountId=46736452,
                errorCode=adapter.refuse_event[target],
                description=f"refused {target}",
            )
        return request

    adapter._reconcile_state = _reconcile_state
    adapter._send_request_with_refresh_retry = _send
    return adapter


def _trade_data(volume: int = 1000):
    """`ProtoOATradeData` with its required fields set (symbolId, volume, tradeSide)."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOATradeData,
        ProtoOATradeSide,
    )

    trade_data = ProtoOATradeData()
    trade_data.symbolId = 1
    trade_data.volume = volume
    trade_data.tradeSide = ProtoOATradeSide.BUY
    return trade_data


def _order(order_id: int, order_type: int, *, status: int | None = 1, position_id: int = 0):
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrder,
        ProtoOAOrderStatus,
    )

    order = ProtoOAOrder()
    order.orderId = order_id
    order.orderType = order_type
    order.tradeData.CopyFrom(_trade_data())
    # `orderStatus` is a proto2 required field: set it unless the test is exercising its
    # absence, and even then the message must be encodable.
    order.orderStatus = status if status is not None else ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED
    if position_id:
        order.positionId = position_id
    return order


def _position(position_id: int, volume: int, *, status: int = 1):
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAPosition

    position = ProtoOAPosition()
    position.positionId = position_id
    position.positionStatus = status
    position.tradeData.CopyFrom(_trade_data(volume))
    position.swap = 0
    return position


def _run(coro):
    return asyncio.run(coro)


def test_cancel_skips_market_orders_and_reports_the_rest_complete() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    adapter = _control_adapter(
        orders=[
            _order(101, ProtoOAOrderType.MARKET),
            _order(102, ProtoOAOrderType.LIMIT),
            _order(103, ProtoOAOrderType.STOP_LIMIT),
        ]
    )

    outcome = _run(adapter.cancel_open_orders())

    assert outcome.completed == 2
    assert outcome.complete is True
    assert sorted(int(request.orderId) for request in adapter.sent) == [102, 103], (
        "a market order fills immediately and cannot be cancelled"
    )


def test_cancel_never_disarms_a_live_position() -> None:
    """SL/TP is attached to a position; cancelling it leaves that position unprotected."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    adapter = _control_adapter(
        orders=[
            _order(201, ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT, position_id=900),
            _order(202, ProtoOAOrderType.LIMIT),
        ]
    )

    outcome = _run(adapter.cancel_open_orders())

    assert outcome.completed == 1
    assert [int(request.orderId) for request in adapter.sent] == [202], (
        "the blanket cancel disarmed a position that the flatten may not manage to close"
    )


def test_cancel_skips_terminal_orders() -> None:
    """A filled or cancelled order is not working; sending a cancel for it only earns a refusal."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )

    adapter = _control_adapter(
        orders=[
            _order(301, ProtoOAOrderType.LIMIT, status=ProtoOAOrderStatus.ORDER_STATUS_FILLED),
            _order(302, ProtoOAOrderType.LIMIT, status=ProtoOAOrderStatus.ORDER_STATUS_CANCELLED),
            _order(303, ProtoOAOrderType.LIMIT),
        ]
    )

    outcome = _run(adapter.cancel_open_orders())

    assert outcome.completed == 1
    assert [int(request.orderId) for request in adapter.sent] == [303]


def test_one_refused_cancel_does_not_strand_the_others() -> None:
    """A refusal is recorded, not raised, and the remaining orders are still attempted."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    adapter = _control_adapter(
        orders=[
            _order(401, ProtoOAOrderType.LIMIT),
            _order(402, ProtoOAOrderType.LIMIT),
            _order(403, ProtoOAOrderType.LIMIT),
        ]
    )
    adapter.refuse = {402}

    outcome = _run(adapter.cancel_open_orders())

    assert outcome.completed == 2
    assert outcome.failed == ("402",)
    assert outcome.complete is False, (
        "a sweep with a refused order is not complete, and a caller must not read it as one"
    )
    assert sorted(int(request.orderId) for request in adapter.sent) == [401, 402, 403]


def test_an_order_with_an_unreadable_id_is_reported_not_skipped() -> None:
    """`orderId` is an int64; 0 cannot be addressed, and silently skipping hides the order."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    adapter = _control_adapter(orders=[_order(0, ProtoOAOrderType.LIMIT)])

    outcome = _run(adapter.cancel_open_orders())

    assert outcome.completed == 0
    assert outcome.unidentified, "an unaddressable order was reported as nothing to do"
    assert outcome.complete is False


def test_flatten_closes_every_open_position_with_its_volume() -> None:
    adapter = _control_adapter(positions=[_position(901, 1000), _position(902, 2500)])

    outcome = _run(adapter.close_all_positions())

    assert outcome.completed == 2
    assert outcome.complete is True
    sent = [(int(r.positionId), int(r.volume)) for r in adapter.sent]
    assert sent == [(901, 1000), (902, 2500)], (
        "an unset volume serializes as 0, which cTrader rejects instead of closing"
    )


def test_a_position_without_readable_volume_is_a_failure_not_a_skip() -> None:
    """The blocker: partial flatten reported as success leaves open exposure behind."""
    position = _position(903, 0)

    adapter = _control_adapter(positions=[_position(901, 1000), position])

    outcome = _run(adapter.close_all_positions())

    assert outcome.completed == 1
    assert outcome.unidentified == ("position:903:volume",)
    assert outcome.complete is False, (
        "a position that could not be read is still open; reporting the sweep as complete is "
        "how a stopped deployment keeps its exposure"
    )


def test_a_position_without_trade_data_is_a_failure_not_a_skip() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAPosition

    position = ProtoOAPosition()
    position.positionId = 904
    position.positionStatus = 1
    position.tradeData.symbolId = 1
    position.tradeData.volume = 0
    position.tradeData.tradeSide = 1
    position.swap = 0

    adapter = _control_adapter(positions=[position])

    outcome = _run(adapter.close_all_positions())

    assert outcome.completed == 0
    assert outcome.unidentified == ("position:904:volume",)
    assert outcome.complete is False


def test_one_refused_close_does_not_stop_the_others() -> None:
    adapter = _control_adapter(positions=[_position(911, 1000), _position(912, 1000)])
    adapter.refuse = {911}

    outcome = _run(adapter.close_all_positions())

    assert outcome.completed == 1
    assert outcome.failed == ("911",)
    assert outcome.complete is False
    assert sorted(int(request.positionId) for request in adapter.sent) == [911, 912]


def test_a_close_refused_with_an_order_error_event_is_a_failure() -> None:
    """The blocker: cTrader answers a refused close with `ProtoOAOrderErrorEvent`.

    That is a successful response as far as the transport is concerned, so only an explicit check
    keeps `MARKET_CLOSED` from being counted as a completed close and the position from being
    reported as flat.
    """
    adapter = _control_adapter(positions=[_position(921, 1000), _position(922, 1000)])
    adapter.refuse_event = {921: "MARKET_CLOSED"}

    outcome = _run(adapter.close_all_positions())

    assert outcome.completed == 1
    assert outcome.failed == ("921",)
    assert outcome.complete is False
    assert sorted(int(r.positionId) for r in adapter.sent) == [921, 922]


def test_a_cancel_refused_with_an_order_error_event_is_a_failure() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    adapter = _control_adapter(
        orders=[
            _order(601, ProtoOAOrderType.LIMIT),
            _order(602, ProtoOAOrderType.LIMIT),
        ]
    )
    adapter.refuse_event = {601: "ORDER_NOT_FOUND"}

    outcome = _run(adapter.cancel_open_orders())

    assert outcome.completed == 1
    assert outcome.failed == ("601",)
    assert outcome.complete is False


def test_orphaned_protection_cancel_refused_with_an_order_error_event_is_a_failure() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    adapter = _control_adapter(
        orders=[_order(701, ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT, position_id=999)],
    )
    adapter.refuse_event = {701: "ORDER_NOT_FOUND"}

    outcome = _run(adapter.cancel_orphaned_protections())

    assert outcome.completed == 0
    assert outcome.failed == ("701",)
    assert outcome.complete is False


def test_an_empty_sweep_is_complete() -> None:
    """Nothing to do is success; that is why the flag is not `completed > 0`."""
    adapter = _control_adapter()

    outcome = _run(adapter.close_all_positions())

    assert outcome.completed == 0
    assert outcome.complete is True


def test_orphaned_protections_are_cancelled_but_live_ones_are_kept() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    adapter = _control_adapter(
        orders=[
            _order(501, ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT, position_id=900),
            _order(502, ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT, position_id=999),
        ],
        positions=[_position(900, 1000)],
    )

    outcome = _run(adapter.cancel_orphaned_protections())

    assert outcome.completed == 1
    assert outcome.complete is True
    assert [int(request.orderId) for request in adapter.sent] == [502], (
        "the protection of a live position was cancelled, leaving it naked"
    )


def test_an_unreadable_order_status_is_detected_from_the_wire() -> None:
    """An enum value this build has no name for is kept in the unknown fields, not dropped.

    Without this the two cases - "the broker sent nothing" and "the broker sent a status this
    client cannot name" - would be the same observation, and a future terminal status would read
    as ACCEPTED and be cancelled.
    """
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    wire = ProtoOAOrder()
    wire.orderId = 601
    wire.orderType = 2
    wire.orderStatus = 1
    wire.tradeData.CopyFrom(_trade_data())
    raw = bytearray(wire.SerializeToString())
    # Append an unknown varint field with field number 4 (orderStatus) and value 9.
    raw += bytes([4 << 3 | 0, 9])
    parsed = ProtoOAOrder()
    parsed.ParseFromString(bytes(raw))

    adapter = _control_adapter()

    assert adapter._unrecognised_status_wire_value(parsed) == 9
    assert adapter._unrecognised_status_wire_value(wire) is None, (
        "an order that never carried a status must not look like an unrecognised one"
    )


def test_the_base_adapter_does_not_claim_the_controls() -> None:
    from live_runner.base_adapter import BaseLiveAdapter

    class _Minimal(BaseLiveAdapter):
        async def connect(self) -> None:
            return None

        async def submit_order(self, signal, *, qty=None) -> str:
            return ""

        async def listen_executions(self) -> None:
            return None

        async def get_balance(self) -> tuple[float, float]:
            return 0.0, 0.0

    adapter = _Minimal()

    assert adapter.supports_cancel_orders is False
    assert adapter.supports_flatten is False
    cancel_call = adapter.cancel_open_orders()
    with pytest.raises(NotImplementedError):
        _run(cancel_call)
    close_call = adapter.close_all_positions()
    with pytest.raises(NotImplementedError):
        _run(close_call)


def test_describe_reports_a_clean_sweep_differently_from_a_partial_one() -> None:
    """`describe` is the operator-facing sentence, so its two branches must not be interchangeable.

    The complete branch says nothing is outstanding; the partial branch must name every
    unidentifiable and refused item, because that line is how an operator learns that a stopped
    deployment still holds exposure. Swapping the predicate would have a partial sweep read as
    clean - the exact failure `ControlOutcome` exists to prevent.
    """
    from live_runner.base_adapter import ControlOutcome

    clean = ControlOutcome(completed=3)
    assert clean.complete is True
    assert clean.describe() == "3 completed, nothing outstanding"

    empty = ControlOutcome()
    assert empty.complete is True
    assert empty.describe() == "0 completed, nothing outstanding"

    partial = ControlOutcome(completed=1, unidentified=("order:None",), failed=("7",))
    assert partial.complete is False
    description = partial.describe()
    assert "nothing outstanding" not in description, description
    assert "1 completed" in description, description
    assert "1 unidentifiable" in description, description
    assert "order:None" in description, description
    assert "1 refused" in description, description
    assert "7" in description, description

    unidentified_only = ControlOutcome(completed=0, unidentified=("position:None:volume",))
    assert unidentified_only.complete is False
    assert "nothing outstanding" not in unidentified_only.describe()

    failed_only = ControlOutcome(completed=0, failed=("42",))
    assert failed_only.complete is False
    assert "nothing outstanding" not in failed_only.describe()


# ─── #121 review: what an exit needs to know before it is sized ──────────────


def test_ctrader_working_order_ids_lists_only_working_orders() -> None:
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )

    working = _order(11, ProtoOAOrderType.MARKET, status=ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED)
    working.clientOrderId = "sig-11"
    filled = _order(12, ProtoOAOrderType.MARKET, status=ProtoOAOrderStatus.ORDER_STATUS_FILLED)
    filled.clientOrderId = "sig-12"
    cancelled = _order(
        13, ProtoOAOrderType.LIMIT, status=ProtoOAOrderStatus.ORDER_STATUS_CANCELLED
    )
    adapter = _control_adapter(orders=[working, filled, cancelled])

    ids = _run(adapter.working_order_ids())

    assert set(ids) == {"11", "sig-11"}


def test_ctrader_position_qty_tolerance_is_half_a_volume_step() -> None:
    from live_runner.ctrader_adapter import CTraderLiveAdapter, SymbolScale

    adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
    adapter._symbol_props = {
        "EURUSD": SymbolScale(
            volume_per_qty=1000.0, price_digits=5, min_volume=1000, lot_size=100_000.0
        ),
        "XAUUSD": SymbolScale(volume_per_qty=1.0, price_digits=2, min_volume=1, lot_size=100.0),
    }

    assert adapter.position_qty_tolerance("EURUSD") == pytest.approx(0.0005)
    assert adapter.position_qty_tolerance("xauusd") == pytest.approx(0.5)


# ─── #121 review concern 6: exit volumes round down to the step ──────────────


def _volume_adapter(*, volume_per_qty: float = 1000.0, min_volume: int = 1, step_volume: int = 1):
    from live_runner.ctrader_adapter import CTraderLiveAdapter, SymbolScale

    adapter = CTraderLiveAdapter.__new__(CTraderLiveAdapter)
    adapter._symbol_props = {
        "EURUSD": SymbolScale(
            volume_per_qty=volume_per_qty,
            price_digits=5,
            min_volume=min_volume,
            lot_size=volume_per_qty * 100.0,
            step_volume=step_volume,
        )
    }
    return adapter


def _volume_signal(qty: float, *, exit_only: bool) -> LiveSignal:
    metadata: dict[str, Any] = {"qty": qty, "explicit_qty": qty}
    if exit_only:
        metadata.update({"exit_only": True, "risk_sizing_mode": "exit_only"})
    else:
        metadata.update({"stop_loss": 1.0, "take_profit": 1.2})
    return LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata=metadata,
    )


def test_ctrader_exit_volume_rounds_down_entry_volume_keeps_rounding() -> None:
    """0.3336 qty = 333.6 volume units: an exit closes 333, never 334."""
    adapter = _volume_adapter()

    assert adapter._resolve_order_volume(_volume_signal(0.3336, exit_only=True)) == 333
    assert adapter._resolve_order_volume(_volume_signal(0.3336, exit_only=False)) == 334


def test_ctrader_exit_volume_rounds_down_to_the_step_volume() -> None:
    adapter = _volume_adapter(min_volume=100, step_volume=100)

    assert adapter._resolve_order_volume(_volume_signal(0.3336, exit_only=True)) == 300


def test_ctrader_exit_volume_ignores_float_noise_of_a_whole_volume() -> None:
    adapter = _volume_adapter(min_volume=1000, step_volume=1000)

    assert adapter._resolve_order_volume(
        _volume_signal(2.9999999999999996, exit_only=True)
    ) == 3000


def test_ctrader_exit_volume_that_rounds_to_nothing_is_refused_as_below_step() -> None:
    from live_runner.exit_signals import ExitBelowStepError

    adapter = _volume_adapter(min_volume=1000, step_volume=1000)

    with pytest.raises(ExitBelowStepError) as excinfo:
        adapter._resolve_order_volume(_volume_signal(0.5, exit_only=True))
    assert excinfo.value.requested_qty == pytest.approx(0.5)
    assert excinfo.value.qty_step == pytest.approx(1.0)
    assert "0.5" in str(excinfo.value)


def test_ctrader_exit_qty_step_is_the_step_volume_in_qty_units() -> None:
    adapter = _volume_adapter(volume_per_qty=1000.0, min_volume=1000, step_volume=1000)
    assert adapter.exit_qty_step("eurusd") == pytest.approx(1.0)
    adapter = _volume_adapter(volume_per_qty=1000.0, step_volume=1)
    assert adapter.exit_qty_step("EURUSD") == pytest.approx(0.001)


def test_ctrader_step_volume_comes_from_broker_props() -> None:
    from live_runner.ctrader_adapter import _symbol_scale_from_broker_props

    scale = _symbol_scale_from_broker_props(
        digits=5, lot_size=10_000_000, min_volume=1000, step_volume=1000, fallback=None
    )
    assert scale is not None
    assert scale.step_volume == 1000
    # Unknown or unusable: one broker volume unit, so a full close of the real
    # position volume is never cut short.
    for raw in (None, 0, -5, "x"):
        scale = _symbol_scale_from_broker_props(
            digits=5, lot_size=10_000_000, min_volume=1000, step_volume=raw, fallback=None
        )
        assert scale is not None
        assert scale.step_volume == 1
