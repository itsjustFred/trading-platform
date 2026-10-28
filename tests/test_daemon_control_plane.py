"""Daemon control-plane regressions: STOPPED, claims, PAUSED floor, demo gate.

Every test uses the throwaway SQLite sandbox from ``tests/conftest.py`` and a
fake adapter; no broker, network, service or production database is touched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

import live_runner.daemon as daemon
from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.base_adapter import ControlOutcome
from shared.database import (
    clear_live_deployment_stopped_enforced,
    get_live_deployment,
    list_live_deployment_submission_claims,
    list_stopped_enforced_deployment_ids,
    update_live_deployment_status,
    update_service_status,
)
from shared.models import (
    LiveAccountConfig,
    LiveDeploymentConfig,
    Service,
    ServiceStatus,
    ServiceType,
)

DEPLOYMENT_ID = "11111111-1111-1111-1111-111111111111"
ACCOUNT_ID = "acc-1"


@dataclass
class _ControlAdapter:
    cancel: ControlOutcome = field(default_factory=ControlOutcome)
    flatten: ControlOutcome = field(default_factory=ControlOutcome)
    orphan: ControlOutcome = field(default_factory=ControlOutcome)
    flatten_error: Exception | None = None
    demo: bool = True
    supports_cancel_orders: bool = True
    supports_flatten: bool = True
    calls: list[str] = field(default_factory=list)
    disconnect_calls: int = 0

    async def cancel_open_orders(self) -> ControlOutcome:
        self.calls.append("cancel")
        return self.cancel

    async def close_all_positions(self) -> ControlOutcome:
        self.calls.append("flatten")
        if self.flatten_error is not None:
            raise self.flatten_error
        return self.flatten

    async def cancel_orphaned_protections(self) -> ControlOutcome:
        self.calls.append("orphan")
        return self.orphan

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def _seed(database, *, status: str = "STOPPED", is_demo: bool = True) -> None:
    database.init_db()
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(
                id=ACCOUNT_ID,
                broker="ctrader",
                name="demo",
                is_demo=is_demo,
            )
        )
        session.add(
            database.LiveDeploymentModel(
                id=DEPLOYMENT_ID,
                strategy_name="discord::ramjet",
                account_id=ACCOUNT_ID,
                symbols_json=["AAPL"],
                timeframe="1h",
                status=status,
            )
        )
        session.commit()


def _deployment(database) -> LiveDeploymentConfig:
    deployment = get_live_deployment(DEPLOYMENT_ID)
    assert deployment is not None
    return deployment


def _enforce(database, adapter: _ControlAdapter, already: set[str] | None = None) -> set[str]:
    ledger = set() if already is None else set(already)
    asyncio.run(
        daemon._enforce_stopped_deployments(
            [_deployment(database)],
            {ACCOUNT_ID: adapter},
            already_enforced=ledger,
            account_lookup={
                ACCOUNT_ID: LiveAccountConfig(
                    id=ACCOUNT_ID, broker="ctrader", is_demo=True
                )
            },
        )
    )
    return ledger


def test_complete_sweep_marks_the_deployment_enforced(isolated_db_sandbox) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter()

    ledger = _enforce(database, adapter)

    assert adapter.calls == ["cancel", "flatten", "orphan"]
    assert DEPLOYMENT_ID in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is not None


def test_partial_flatten_does_not_stamp_and_does_not_sweep_orphans(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(
        flatten=ControlOutcome(completed=1, unidentified=("position:2",))
    )

    ledger = _enforce(database, adapter)

    assert "flatten" in adapter.calls
    assert "orphan" not in adapter.calls
    assert DEPLOYMENT_ID not in ledger
    # The dashboard reads this stamp: no stamp means "not stopped", even though
    # the deployment's status column still says STOPPED.
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_partial_cancel_does_not_flatten_or_stamp(isolated_db_sandbox) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(cancel=ControlOutcome(completed=1, failed=("entry-2",)))

    ledger = _enforce(database, adapter)

    assert adapter.calls == ["cancel"]
    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_stopped_to_paused_clears_the_stamp_and_second_stop_reenforces(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    first = _ControlAdapter()
    _enforce(database, first)
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is not None

    update_live_deployment_status(DEPLOYMENT_ID, "PAUSED")

    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None
    assert list_stopped_enforced_deployment_ids() == set()

    update_live_deployment_status(DEPLOYMENT_ID, "STOPPED")
    second = _ControlAdapter()
    ledger = _enforce(database, second, already=list_stopped_enforced_deployment_ids())

    assert second.calls == ["cancel", "flatten", "orphan"]
    assert DEPLOYMENT_ID in ledger


def test_daemon_cleanup_clears_a_stamp_when_it_observes_a_pause(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database, status="PAUSED")
    assert clear_live_deployment_stopped_enforced(DEPLOYMENT_ID) is True
    with database.get_session() as session:
        record = session.get(database.LiveDeploymentModel, DEPLOYMENT_ID)
        record.stopped_enforced_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(record)
        session.commit()

    ledger = _enforce(database, _ControlAdapter(), already={DEPLOYMENT_ID})

    assert ledger == set()
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_shared_account_is_not_flattened_for_one_stopped_deployment(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    with database.get_session() as session:
        session.add(
            database.LiveDeploymentModel(
                id="22222222-2222-2222-2222-222222222222",
                strategy_name="other",
                account_id=ACCOUNT_ID,
                symbols_json=["MSFT"],
                timeframe="1h",
                status="ACTIVE",
            )
        )
        session.commit()
    adapter = _ControlAdapter()

    asyncio.run(
        daemon._enforce_stopped_deployments(
            database.list_live_deployments(),
            {ACCOUNT_ID: adapter},
            already_enforced=set(),
        )
    )

    assert adapter.calls == []


def test_demo_gate_refuses_to_build_an_adapter_for_a_non_demo_account(
    isolated_db_sandbox, monkeypatch
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database, is_demo=False)
    account = database.list_live_accounts()[0]
    calls: list[str] = []

    def _factory(*args, **kwargs):
        calls.append("factory")
        return _ControlAdapter()

    monkeypatch.setattr(daemon, "_create_adapter_for_account", _factory)
    active: dict[str, object] = {}

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            ACCOUNT_ID,
            {ACCOUNT_ID: account},
            active,
            accounts_env_values={},
        )
    )

    assert calls == []
    assert active == {}


def test_demo_gate_drops_an_existing_non_demo_adapter(
    isolated_db_sandbox, monkeypatch
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database, is_demo=True)
    account = database.list_live_accounts()[0]
    adapter = _ControlAdapter(demo=False)
    active = {ACCOUNT_ID: adapter}

    asyncio.run(
        daemon._ensure_adapter_for_active_account(
            ACCOUNT_ID,
            {ACCOUNT_ID: account},
            active,
            accounts_env_values={},
        )
    )

    assert active == {}
    assert adapter.disconnect_calls == 1


def test_recover_failed_services_does_not_resurrect_a_paused_bot(
    isolated_db_sandbox, monkeypatch
) -> None:
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(name="bot", type=ServiceType.DISCORD_BOT, status=ServiceStatus.FAILED)
    )
    update_service_status(str(service.id), ServiceStatus.PAUSED)
    stale_failed = service.model_copy(update={"status": ServiceStatus.FAILED})
    monkeypatch.setattr(daemon, "list_services", lambda: [stale_failed])

    daemon._recover_failed_services()

    assert database.get_service(str(service.id)).status == ServiceStatus.PAUSED
    assert str(service.id) not in daemon._DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE


def test_sync_running_discord_bot_rechecks_the_pause(
    isolated_db_sandbox, monkeypatch
) -> None:
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(name="bot", type=ServiceType.DISCORD_BOT, status=ServiceStatus.RUNNING)
    )
    update_service_status(str(service.id), ServiceStatus.PAUSED)
    started: list[str] = []

    async def _ensure(*args, **kwargs):
        started.append("started")

    monkeypatch.setattr(daemon, "_ensure_discord_bot_source", _ensure)
    monkeypatch.setattr(
        daemon,
        "_resolve_discord_bot_runtime",
        lambda *args, **kwargs: ("KEY", "token", "channel"),
    )
    monkeypatch.setattr(
        daemon, "_resolve_discord_service_trusted_author_ids", lambda *a, **k: {"1"}
    )

    asyncio.run(
        daemon._sync_running_discord_bot(
            service.model_copy(update={"status": ServiceStatus.RUNNING}),
            active_discord_bots={},
            active_discord_bot_tasks={},
            accounts_env_values={},
        )
    )

    assert started == []


def _trade_signal() -> object:
    from risk import TradeSignal

    return TradeSignal(
        action="buy",
        symbol="AAPL",
        entry_price=200.0,
        stop_loss=195.0,
        take_profit=210.0,
        timeframe="1h",
        metadata={
            "discord_channel_id": "channel-1",
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def test_submit_exception_keeps_the_claim_so_the_drain_is_not_drained(
    isolated_db_sandbox, monkeypatch
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database, status="ACTIVE")

    async def _explode(*args, **kwargs):
        raise RuntimeError("broker timeout after the order was sent")

    monkeypatch.setattr(daemon, "submit_order_with_risk", _explode)

    asyncio.run(
        daemon._submit_normalized_discord_signal(
            ACCOUNT_ID,
            _ControlAdapter(),
            _deployment(database),
            _trade_signal(),
            signal_timeframe="1h",
            wait_enabled=False,
        )
    )

    claims = list_live_deployment_submission_claims(DEPLOYMENT_ID)
    assert len(claims) == 1
    assert claims[0].expired is False


def test_claim_is_renewed_while_the_submit_is_in_flight(
    isolated_db_sandbox, monkeypatch
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database, status="ACTIVE")
    token = "renew-me"
    assert database.claim_live_deployment_submission(DEPLOYMENT_ID, token) is True
    before = list_live_deployment_submission_claims(DEPLOYMENT_ID)[0].expires_at

    renewals: list[str] = []
    real_renew = daemon.renew_live_deployment_submission

    def _counted(token_arg, **kwargs):
        renewals.append(token_arg)
        # Keep the real 30 s lease so the renewal visibly extends the expiry;
        # only the loop interval is shortened by the patched module constant.
        return real_renew(token_arg, ttl_seconds=30.0)

    monkeypatch.setattr(daemon, "renew_live_deployment_submission", _counted)
    monkeypatch.setattr(daemon, "LIVE_SUBMISSION_CLAIM_TTL_SECONDS", 0.06)

    async def _run() -> None:
        task = asyncio.create_task(
            daemon._renew_submission_claim_until_cancelled(token)
        )
        await asyncio.sleep(0.14)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())

    after = list_live_deployment_submission_claims(DEPLOYMENT_ID)[0].expires_at
    assert renewals
    assert after > before


def test_alpaca_stopped_keeps_sl_tp_when_the_flatten_fails(
    isolated_db_sandbox,
) -> None:
    from tests.test_alpaca_adapter import (
        _FakeOrder,
        _FakePosition,
        _FakeTradingClient,
    )

    database, _, _ = isolated_db_sandbox
    _seed(database, status="STOPPED")
    client = _FakeTradingClient(
        orders=[
            _FakeOrder("entry-1", "AAPL", "buy"),
            _FakeOrder("sl-1", "AAPL", "sell", order_type="stop"),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit"),
        ],
        positions=[_FakePosition("AAPL", 10.0)],
        close_error=RuntimeError("broker refused to flatten"),
    )
    adapter = AlpacaLiveAdapter(api_key="key", secret_key="secret", paper=True)
    adapter._connected = True
    adapter._trading_client = client

    ledger = _enforce(database, adapter)

    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None
    assert [order.id for order in client.orders] == ["sl-1", "tp-1"]
