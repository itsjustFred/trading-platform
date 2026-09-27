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


def test_get_live_signal_metadata_roundtrip(isolated_db):
    database = isolated_db
    # ACTIVE deployments require a fresh broker demo verification in addition
    # to the is_demo flag, mirroring what account discovery records.
    database.ensure_live_account("acc-1", "ctrader", is_demo=True)
    database.record_live_account_demo_verification("acc-1", is_demo=True)
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


from live_runner.ctrader_adapter import CTraderLiveAdapter


class _FakeCTraderAdapter(CTraderLiveAdapter):
    """A real adapter with only the transport stubbed.

    It subclasses ``CTraderLiveAdapter`` on purpose. The cancel/flatten logic
    lives in several helpers, and a stand-in class that re-declared only
    ``cancel_open_orders`` silently dropped every helper it did not know about,
    so a change to those helpers would not have been exercised here at all.
    ``__init__`` is bypassed (it loads protobuf and dials nothing); the message
    classes it would have bound are attached below.
    """

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
    _ProtoOACancelOrderReq = _real.ProtoOACancelOrderReq
    _ProtoOAClosePositionReq = _real.ProtoOAClosePositionReq
    _ProtoOAOrderType = _real_model.ProtoOAOrderType
    _ProtoOAOrderStatus = _real_model.ProtoOAOrderStatus
    _ProtoOAReconcileReq = _real.ProtoOAReconcileReq
    _ProtoOAReconcileRes = _real.ProtoOAReconcileRes
    _ProtoOAPositionStatus = _real_model.ProtoOAPositionStatus

    async def connect(self):
        return None

    async def _send_request_with_refresh_retry(self, request, timeout_seconds=None):
        """Answer reconcile with the real repeated `order` field.

        The field is named `order`, not `orders`, and the response is built from
        the real ProtoOAReconcileRes so a wrong field name fails here.
        """
        self.sent.append(request)
        if isinstance(request, self._ProtoOAReconcileReq):
            response = self._ProtoOAReconcileRes()
            for order in self._orders:
                response.order.add().CopyFrom(order)
            for position in self._positions:
                response.position.add().CopyFrom(position)
            return response
        return object()


def _order(order_id: int, order_type: int, *, position_id: int = 0, status: int | None = None):
    """A real ProtoOAOrder: ids are int64, so a string id would be rejected."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    order = ProtoOAOrder()
    order.orderId = order_id
    order.orderType = order_type
    if position_id:
        order.positionId = position_id
    if status is not None:
        order.orderStatus = status
    return order


def _protection(order_id: int, position_id: int, *, status: int | None = None):
    """A STOP_LOSS_TAKE_PROFIT order: an open position's stop + target."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )

    return _order(
        order_id,
        ProtoOAOrderType.STOP_LOSS_TAKE_PROFIT,
        position_id=position_id,
        status=ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED
        if status is None
        else status,
    )


async def _cancel(orders):
    adapter = _FakeCTraderAdapter(orders, [])
    cancelled = await adapter.cancel_open_orders()
    return adapter, cancelled


def _run(coro):
    return asyncio.run(coro)


def _cancelled_ids(adapter) -> set[int]:
    """Order ids the adapter actually sent a cancel request for."""
    return {int(r.orderId) for r in adapter.sent if getattr(r, "orderId", 0)}


def test_cancel_skips_market_orders_and_cancels_resting_ones():
    adapter, cancelled = _run(
        _cancel(
            [
                _order(101, 1),  # MARKET
                _order(102, 2),  # LIMIT
                _order(103, 3),  # STOP
                _order(104, 6),  # STOP_LIMIT
            ]
        )
    )
    assert cancelled == 3, "market orders fill immediately and cannot be cancelled"
    # `orderId` is an int64 field, so the real request always reads back an
    # int. Coercing here would hide a string that reached the wire.
    ids = _cancelled_ids(adapter)
    assert 101 not in ids, "market orders fill immediately and cannot be cancelled"
    assert {102, 103, 104} <= ids


def test_cancel_never_disarms_an_open_positions_stop_or_target():
    """A STOP_LOSS_TAKE_PROFIT order is a position's stop and target, not an entry.

    Cancelling it in the blanket sweep left the position open with no stop and
    no target, on an account the sweep is not necessarily about to flatten (and
    it runs before the flatten, so a flatten that then fails leaves the position
    permanently naked). The type must be excluded from the cancellable set.
    """
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    limit = _order(102, ProtoOAOrderType.LIMIT)
    limit.orderStatus = ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED
    stop = _order(103, ProtoOAOrderType.STOP)
    stop.orderStatus = ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED
    stop_limit = _order(104, ProtoOAOrderType.STOP_LIMIT)
    stop_limit.orderStatus = ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED
    protection = _protection(105, position_id=900)

    adapter = _FakeCTraderAdapter([limit, stop, stop_limit, protection], [])
    cancelled = _run(adapter.cancel_open_orders())

    assert _cancelled_ids(adapter) == {102, 103, 104}, (
        "every detached working order must still be cancelled, and the "
        "attached protection must not be"
    )
    assert cancelled == 3, (
        "the protection is not a cancelled order, so it must not be counted as "
        "one either: a count that includes it reports orders cancelled while a "
        "live stop was disarmed"
    )


def test_orphan_sweep_cancels_only_protections_whose_position_is_gone():
    """The flatten detaches the protection; the sweep cleans up exactly those."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    live_protection = _protection(201, position_id=900)
    orphan = _protection(202, position_id=901)
    unattached = _protection(203, position_id=0)
    limit = _order(204, 2)

    adapter = _FakeCTraderAdapter(
        [live_protection, orphan, unattached, limit],
        [_real_position(900, 1000)],
    )
    cancelled = _run(adapter.cancel_orphaned_protections())

    assert _cancelled_ids(adapter) == {202}, (
        "only the protection whose position is gone may be cancelled: the live "
        "position's stop is the one thing this must never touch, and a "
        "protection with no positionId cannot be attributed to anything"
    )
    assert cancelled == 1


def test_orphan_sweep_leaves_a_live_protection_alone_when_nothing_closed():
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    protection = _protection(301, position_id=900)
    adapter = _FakeCTraderAdapter([protection], [_real_position(900, 1000)])

    assert _run(adapter.cancel_orphaned_protections()) == 0
    assert _cancelled_ids(adapter) == set()


def test_orphan_sweep_ignores_a_terminal_protection():
    """A filled or cancelled orphan is already gone; cTrader rejects the cancel."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderStatus
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    filled = _protection(
        401, position_id=901, status=ProtoOAOrderStatus.ORDER_STATUS_FILLED
    )
    adapter = _FakeCTraderAdapter([filled], [])

    assert _run(adapter.cancel_orphaned_protections()) == 0
    assert _cancelled_ids(adapter) == set()


def test_orphan_sweep_raises_instead_of_reporting_a_clean_zero():
    """An unreadable reconcile response is UNKNOWN, not "there was nothing to do"."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = _FakeCTraderAdapter([], [])

    async def _bad(request, timeout_seconds=None):
        return "not-a-reconcile-response"

    adapter._send_request_with_refresh_retry = _bad
    with pytest.raises(RuntimeError, match="Unexpected reconcile response"):
        _run(adapter.cancel_orphaned_protections())


def test_cancel_with_no_orders_is_a_no_op():
    adapter, cancelled = _run(_cancel([]))
    assert cancelled == 0
    # The order-list request is still sent; only the cancel requests are absent.
    assert all(not getattr(r, "orderId", "") for r in adapter.sent)


def test_cancel_rejects_an_unexpected_response_type():
    adapter = _FakeCTraderAdapter([], [])

    async def _bad(request, timeout_seconds=None):
        return "not-a-reconcile-response"

    adapter._send_request_with_refresh_retry = _bad
    with pytest.raises(RuntimeError, match="Unexpected reconcile response"):
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
    closed = _run(adapter.close_all_positions())
    assert closed == 2
    # `positionId`/`volume` are int64 fields, so the real request always reads
    # back ints. Coercing here would hide a string that reached the wire.
    closed_ids = [r.positionId for r in adapter.sent]
    assert closed_ids == [201, 202]
    # A full close must carry the position volume; cTrader rejects volume 0.
    assert all(r.volume > 0 for r in adapter.sent)


def test_flatten_skips_positions_without_an_id():
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    positions = [_real_position(0, 0)]
    adapter = _FakeCTraderAdapter([], positions)

    async def _positions():
        return positions

    adapter._fetch_open_positions = _positions
    assert _run(adapter.close_all_positions()) == 0
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
    """Adapter whose transport answers the reconcile, then records cancels."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOACancelOrderReq,
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
    adapter._ProtoOACancelOrderReq = ProtoOACancelOrderReq
    adapter._ProtoOAOrderType = ProtoOAOrderType
    adapter._ProtoOAOrderStatus = ProtoOAOrderStatus

    # Real ProtoOAReconcileRes, so the repeated `order` field is the real one.
    response = ProtoOAReconcileRes()
    for order in orders:
        response.order.add().CopyFrom(order)

    cancelled: list[Any] = []

    async def _fake_send(request, **_kwargs):
        if isinstance(request, ProtoOAReconcileReq):
            return response
        if isinstance(request, ProtoOACancelOrderReq):
            # Recorded verbatim: `int()` here would coerce away a string id
            # that reached the wire instead of failing the assertion.
            cancelled.append(request.orderId)
            return ProtoOAReconcileRes()
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


# ─── exit-only is decided by the stamp, not by the order side ───────────────
#
# `risk_sizing_mode="exit_only"` addresses a close request at an existing broker
# position, which carries no SL/TP because the position already has them. The
# decision rests on that system-stamped metadata and never on `signal.action`:
# the close side is DERIVED from the broker book by `daemon._exit_only_close_action`,
# and flattening a SHORT is a BUY. Gating on the action therefore refused
# precisely the orders that matter -- every stop-loss and take-profit on a short
# -- while long exits passed, and CodeRabbit flagged it critical.
#
# The protective intent that survives is real but different: an exit-only order
# must never become an unbounded entry. That is refused where the position is
# resolved (`_position_id_for_exit` finding no broker position), not by
# string-matching the side. The Alpaca twin of this is
# test_alpaca_exit_only_is_decided_by_metadata_not_by_the_order_side.


def _make_submit_adapter(tmp_path: Path, *, open_position_ids=(77,)):
    """Adapter whose transport records the request type it is handed.

    `open_position_ids` seeds the broker positions the exit-only path resolves
    against, so the close branch is genuinely reachable: a buy must not reach
    it, and a sell must.
    """
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAClosePositionReq,
        ProtoOAExecutionEvent,
        ProtoOANewOrderReq,
        ProtoOAOrderErrorEvent,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderType,
        ProtoOATimeInForce,
        ProtoOATradeSide,
    )

    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
        accounts_env_path=tmp_path / ".accounts.env",
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._position_ids_by_symbol = {"EURUSD": tuple(open_position_ids)}
    adapter._ProtoOANewOrderReq = ProtoOANewOrderReq
    adapter._ProtoOAClosePositionReq = ProtoOAClosePositionReq
    adapter._ProtoOAOrderErrorEvent = ProtoOAOrderErrorEvent
    adapter._ProtoOAExecutionEvent = ProtoOAExecutionEvent
    adapter._ProtoOAOrderType = ProtoOAOrderType
    adapter._ProtoOATradeSide = ProtoOATradeSide
    adapter._ProtoOATimeInForce = ProtoOATimeInForce

    close_requests: list[Any] = []
    new_order_requests: list[Any] = []
    persisted: list[tuple[str, str]] = []

    async def _fake_send(request, **_kwargs):
        if isinstance(request, ProtoOAClosePositionReq):
            close_requests.append(request)
        elif isinstance(request, ProtoOANewOrderReq):
            new_order_requests.append(request)
        else:
            raise AssertionError(f"unexpected request {type(request).__name__}")
        return SimpleNamespace(orderId=555)

    adapter._send_request_with_refresh_retry = _fake_send
    adapter.close_requests = close_requests
    adapter.new_order_requests = new_order_requests
    adapter.persisted_order_ids = persisted
    return adapter


def _offline_signal_persistence(monkeypatch, module, adapter, signal) -> None:
    """Keep the database out of a submit_order, recording what it persists."""
    persisted: list[tuple[str, str]] = adapter.persisted_order_ids
    monkeypatch.setattr(module, "get_live_signal_broker_order_id", lambda _sid: "")
    monkeypatch.setattr(module, "get_live_signal_timestamp", lambda _sid: None)
    monkeypatch.setattr(
        module, "save_live_signal", lambda _signal: (str(signal.id), True)
    )
    monkeypatch.setattr(
        module,
        "set_live_signal_broker_order_id",
        lambda sid, oid: persisted.append((sid, oid)) or True,
    )


def test_ctrader_exit_only_buy_closes_a_short_and_an_unbacked_exit_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """The exit-only contract, asserted through the real cTrader submit path.

    A close's side is DERIVED from the broker book: flattening a SHORT is a
    BUY. An action-based rule therefore refuses exactly the exits that matter
    -- every stop-loss and take-profit on a short -- while letting long exits
    through, and it would also skip the new-order path's mandatory SL/TP entry
    guard for nothing. The decision rests on the system-stamped metadata; what
    must still be refused is exit-only metadata with no broker position, which
    would OPEN a position instead of closing one.
    """
    from live_runner import ctrader_adapter as module

    # (a) closing a short: action "buy" + exit-only metadata is a legitimate
    # exit. It must reach the position-close path and a close request must
    # actually be sent -- this is the regression test.
    adapter = _make_submit_adapter(tmp_path)
    short_close = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="buy",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 3.0, "risk_sizing_mode": "exit_only", "position_id": 77},
    )
    _offline_signal_persistence(monkeypatch, module, adapter, short_close)

    assert asyncio.run(adapter.submit_order(short_close, qty=3.0)) == "555"

    assert [request.positionId for request in adapter.close_requests] == [77], (
        "a short close carries action 'buy' and must still be routed to the "
        "position-close request"
    )
    # Resolved to a real broker volume (>0), not dropped or zeroed.
    assert all(request.volume > 0 for request in adapter.close_requests)
    assert adapter.new_order_requests == [], (
        "the close must not fall through to a new order, which would open a "
        "second position and skip the mandatory SL/TP entry guard"
    )
    assert adapter.persisted_order_ids == [(str(short_close.id), "555")]

    # (b) exit-only metadata with no broker position has nothing to close, and
    # the close path would have skipped the entry guard: it would OPEN an
    # unbounded position. Refused for BOTH sides, with nothing sent at all.
    for action in ("buy", "sell"):
        flat_adapter = _make_submit_adapter(tmp_path, open_position_ids=())
        unbacked = LiveSignal(
            deployment_id="dep-1",
            symbol="EURUSD",
            action=action,
            theoretical_price=1.1,
            timestamp=datetime.now(timezone.utc),
            metadata={"qty": 3.0, "risk_sizing_mode": "exit_only", "position_id": 77},
        )
        _offline_signal_persistence(monkeypatch, module, flat_adapter, unbacked)

        with pytest.raises(ValueError, match="no broker position id"):
            asyncio.run(flat_adapter.submit_order(unbacked, qty=3.0))

        assert flat_adapter.close_requests == [], "nothing may be closed"
        assert flat_adapter.new_order_requests == [], "nothing may reach the broker"
        assert flat_adapter.persisted_order_ids == [], (
            "no broker order id to persist: a persisted signal row without one "
            "wedges every redelivery on the duplicate-recovery path"
        )


def test_ctrader_sell_with_exit_only_mode_still_closes_the_position(
    tmp_path: Path, monkeypatch
) -> None:
    """The control for the guard above: a sell may take the close path.

    Without this, "never close" would satisfy the buy test while silently
    disabling exit-only exits altogether.
    """
    from live_runner import ctrader_adapter as module

    adapter = _make_submit_adapter(tmp_path)
    signal = LiveSignal(
        deployment_id="dep-1",
        symbol="EURUSD",
        action="sell",
        theoretical_price=1.1,
        timestamp=datetime.now(timezone.utc),
        metadata={"qty": 3.0, "risk_sizing_mode": "exit_only", "position_id": 77},
    )
    _offline_signal_persistence(monkeypatch, module, adapter, signal)

    assert asyncio.run(adapter.submit_order(signal, qty=3.0)) == "555"

    assert [request.positionId for request in adapter.close_requests] == [77]
    assert adapter.new_order_requests == []
    assert adapter.persisted_order_ids == [(str(signal.id), "555")]


# ─── int64 ids: a shape the wire cannot carry must be skipped ────────────────
#
# `orderId`/`positionId` are protobuf int64. The string form cannot be assigned
# (TypeError on the wire) and the coerced comparison is what keeps the loop
# going: a single unusable id must not abort the loop and leave every other
# order resting or position open.


class _ListedOrder:
    """A reconcile order row whose id is not a protobuf int64 value.

    A real ProtoOAOrder cannot hold a string in an int64 field, so this shape is
    only representable off the wire; the cancel *request* stays the real
    protobuf type, so a leaked string still raises TypeError on assignment.
    """

    def __init__(self, order_id: Any, *, order_type: int, order_status: int) -> None:
        self.orderId = order_id
        self.orderType = order_type
        self.orderStatus = order_status


class _ReconcileListing:
    """A reconcile response carrying off-wire order rows.

    `cancel_open_orders` only needs `isinstance(response, _ProtoOAReconcileRes)`
    plus the repeated `order` rows, so this stand-in keeps the id shapes a
    protobuf message cannot represent.
    """

    def __init__(self, orders: list[Any]) -> None:
        self.order = list(orders)


def _make_listing_cancel_adapter(tmp_path: Path, orders: list[Any]):
    """Cancel adapter answering with an off-wire order listing.

    Unlike `_make_cancel_adapter`, the recorded ids are stored exactly as the
    adapter produced them: a leaked string must show up as a string, not be
    coerced away by the recorder.
    """
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOACancelOrderReq,
        ProtoOAReconcileReq,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )

    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
        accounts_env_path=tmp_path / ".accounts.env",
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._ProtoOAReconcileReq = ProtoOAReconcileReq
    adapter._ProtoOAReconcileRes = _ReconcileListing
    adapter._ProtoOACancelOrderReq = ProtoOACancelOrderReq
    adapter._ProtoOAOrderType = ProtoOAOrderType
    adapter._ProtoOAOrderStatus = ProtoOAOrderStatus

    response = _ReconcileListing(orders)
    cancelled: list[Any] = []

    async def _fake_send(request, **_kwargs):
        if isinstance(request, ProtoOAReconcileReq):
            return response
        if isinstance(request, ProtoOACancelOrderReq):
            cancelled.append(request.orderId)
            return ProtoOACancelOrderReq()
        raise AssertionError(f"unexpected request {type(request).__name__}")

    adapter._send_request_with_refresh_retry = _fake_send
    adapter.cancelled_ids = cancelled
    return adapter


def _listed_order(order_id: Any, order_type: int | None = None) -> "_ListedOrder":
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOAOrderStatus,
        ProtoOAOrderType,
    )

    return _ListedOrder(
        order_id,
        order_type=ProtoOAOrderType.LIMIT if order_type is None else order_type,
        order_status=ProtoOAOrderStatus.ORDER_STATUS_ACCEPTED,
    )


def test_cancel_open_orders_skips_an_order_whose_id_is_not_an_int(tmp_path: Path):
    """An id the wire cannot carry is skipped, not raised.

    The cancel request assigns `orderId` outside any error handling, so a
    string reaching it raises TypeError out of the whole call and every other
    order stays resting after an operator asked to stop.
    """
    adapter = _make_listing_cancel_adapter(tmp_path, [_listed_order("order-abc")])

    count = asyncio.run(adapter.cancel_open_orders())

    assert adapter.cancelled_ids == [], (
        f"an unusable id must not be sent, got {adapter.cancelled_ids!r}"
    )
    assert count == 0


def test_cancel_open_orders_keeps_cancelling_after_an_unusable_id(tmp_path: Path):
    """A skipped order must not abort the loop over the orders behind it."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrderType

    orders = [
        _listed_order("order-abc"),
        _listed_order(55),
        _listed_order(66, ProtoOAOrderType.STOP),
    ]
    adapter = _make_listing_cancel_adapter(tmp_path, orders)

    count = asyncio.run(adapter.cancel_open_orders())

    assert adapter.cancelled_ids == [55, 66], (
        "the working orders must still be cancelled, got "
        f"{adapter.cancelled_ids!r}"
    )
    assert count == 2


def test_flatten_skips_a_position_whose_id_is_not_an_int():
    """An unparseable positionId is skipped instead of aborting the flatten.

    Without the coercion the loop raises out of `close_all_positions` and the
    remaining positions stay open.
    """
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    positions = [
        SimpleNamespace(
            positionId="position-abc",
            tradeData=SimpleNamespace(volume=1000),
        )
    ]
    adapter = _FakeCTraderAdapter([], [])

    async def _positions():
        return positions

    adapter._fetch_open_positions = _positions

    assert _run(adapter.close_all_positions()) == 0
    assert adapter.sent == []


def test_flatten_skips_a_string_zero_position_id():
    """"0" is not a position: cTrader rejects it instead of closing anything."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    positions = [
        SimpleNamespace(positionId="0", tradeData=SimpleNamespace(volume=1000))
    ]
    adapter = _FakeCTraderAdapter([], [])

    async def _positions():
        return positions

    adapter._fetch_open_positions = _positions

    assert _run(adapter.close_all_positions()) == 0
    assert adapter.sent == []


def test_flatten_closes_the_other_positions_after_an_unusable_id():
    """A skipped position must not abort the loop over the positions behind it."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    positions = [
        SimpleNamespace(
            positionId="position-abc",
            tradeData=SimpleNamespace(volume=1000),
        ),
        _real_position(201, 1000),
        _real_position(202, 2000),
    ]
    adapter = _FakeCTraderAdapter([], [])

    async def _positions():
        return positions

    adapter._fetch_open_positions = _positions

    assert _run(adapter.close_all_positions()) == 2
    assert [request.positionId for request in adapter.sent] == [201, 202]
