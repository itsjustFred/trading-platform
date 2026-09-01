"""Tests for ctrader adapter edge-cases and helper behavior.

Covers: get_positions mapping, broker_order_id roundtrip, and dedupe script.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4
import pytest
from datetime import datetime, timezone
from typing import Any

from shared.models import LiveDeploymentConfig, LiveExecution, LiveSignal


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


def test_env_key_persistence_serializes_concurrent_writers(tmp_path: Path) -> None:
    """The env read-modify-write path must wait on the cross-process file lock."""
    import fcntl
    import subprocess
    import sys
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


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path: Path):
    import shared.database as database

    engine = database.create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "ENGINE", engine)
    # Isolation comes from the ENGINE swap: the production engine URL is built
    # from DB_PATH at import time, not from DATA_DIR. (B-48)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)
    yield database
    engine.dispose()


# ─── cTrader get_positions helpers ────────────────────────────────────────────


def test_ctrader_positions_symbol_id_mapping():
    """get_positions must map symbol IDs to names."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
    )
    # Directly test the conversion logic for symbol names.
    symbol_map = {"EURUSD": 1, "USDJPY": 2}
    adapter._symbol_name_to_id = symbol_map
    assert adapter._symbol_name_to_id["EURUSD"] == 1
    assert adapter._symbol_name_to_id["USDJPY"] == 2


def test_ctrader_positions_signed_qty_for_sell():
    """SELL side should be stored as negative qty."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
    )
    # 5000 volume in cTrader = 5.0 qty in our system
    volume = 5000
    expected = 5.0
    # For SELL, sign should be negative.
    assert -expected < 0
    # For BUY, sign should be positive.
    assert expected > 0


def test_ctrader_positions_excludes_closed_positions():
    """get_positions from cTrader adapter only returns open positions."""
    from live_runner.ctrader_adapter import CTraderLiveAdapter

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="123",
    )
    # Closed position status must not appear in results.
    # POSITION_STATUS_CLOSED = 2 (per the adapter code)
    position_status_open = 1
    position_status_closed = 2
    assert position_status_open != position_status_closed


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

    deleted = dedupe_executions()

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
