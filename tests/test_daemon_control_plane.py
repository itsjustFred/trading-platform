"""Daemon control-plane regressions: STOPPED, claims, PAUSED floor, demo gate.

Every test uses the throwaway SQLite sandbox from ``tests/conftest.py`` and a
fake adapter; no broker, network, service or production database is touched.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import live_runner.daemon as daemon
from live_runner.alpaca_adapter import AlpacaLiveAdapter
from live_runner.base_adapter import ControlOutcome, LiveQuote, order_not_submitted
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
    orphan_error: Exception | None = None
    flatten_error: Exception | None = None
    submit_error: Exception | None = None
    pre_submit_error: Exception | None = None
    demo: bool = True
    supports_cancel_orders: bool = True
    supports_flatten: bool = True
    calls: list[str] = field(default_factory=list)
    submitted: list[tuple[object, float | None]] = field(default_factory=list)
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
        if self.orphan_error is not None:
            raise self.orphan_error
        return self.orphan

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_quote(self, symbol: str) -> LiveQuote:
        # #125 entry-deviation gate: a quote at the signal entry passes.
        return LiveQuote(bid=200.0, ask=200.0, timestamp=datetime.now(timezone.utc), source="test")

    async def submit_order(self, signal, *, qty: float | None = None) -> str:
        self.submitted.append((signal, qty))
        if self.pre_submit_error is not None:
            # Mirrors a real adapter: a refusal raised inside the pre-request
            # block is marked, so the caller knows nothing reached the broker.
            with order_not_submitted():
                raise self.pre_submit_error
        if self.submit_error is not None:
            raise self.submit_error
        return "order-1"

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
                ACCOUNT_ID: LiveAccountConfig(id=ACCOUNT_ID, broker="ctrader")
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


def test_partial_cancel_still_flattens_but_does_not_stamp(isolated_db_sandbox) -> None:
    """Closing positions outranks a clean cancel.

    Refusing to flatten because an entry survived its cancel would leave the
    exposure open - the exact thing STOPPED exists to prevent. The surviving
    order does keep the deployment unenforced, so the next tick retries.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(cancel=ControlOutcome(completed=1, failed=("entry-2",)))

    ledger = _enforce(database, adapter)

    assert adapter.calls == ["cancel", "flatten", "orphan"]
    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_adapter_that_cannot_cancel_still_flattens_but_does_not_stamp(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(supports_cancel_orders=False)

    ledger = _enforce(database, adapter)

    assert adapter.calls == ["flatten", "orphan"]
    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_incomplete_orphan_sweep_does_not_stamp(isolated_db_sandbox) -> None:
    """A stop that outlived its position can still trigger and open one.

    Stamping with it resting would mark the deployment enforced and never
    retry, so the sweep has to be complete too.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(
        orphan=ControlOutcome(completed=0, failed=("sl-orphan",))
    )

    ledger = _enforce(database, adapter)

    assert adapter.calls == ["cancel", "flatten", "orphan"]
    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_orphan_sweep_failure_does_not_stamp(isolated_db_sandbox) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(orphan_error=RuntimeError("broker refused the sweep"))

    ledger = _enforce(database, adapter)

    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_adapter_without_an_orphan_sweep_still_stamps(isolated_db_sandbox) -> None:
    """No sweep concept means nothing can be left behind; do not block forever."""
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter(orphan_error=NotImplementedError("not supported"))

    ledger = _enforce(database, adapter)

    assert DEPLOYMENT_ID in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is not None


def test_failed_cancel_still_flattens_but_does_not_stamp(isolated_db_sandbox) -> None:
    """A cancel that raises says nothing about whether positions can close."""
    database, _, _ = isolated_db_sandbox
    _seed(database)
    adapter = _ControlAdapter()

    async def _explode() -> ControlOutcome:
        adapter.calls.append("cancel")
        raise RuntimeError("order list unavailable")

    adapter.cancel_open_orders = _explode  # type: ignore[method-assign]

    ledger = _enforce(database, adapter)

    assert adapter.calls == ["cancel", "flatten", "orphan"]
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


def test_demo_gate_requires_a_literal_true_flag() -> None:
    """Fail closed on every uncertainty, not just on a False flag.

    A missing flag, a False flag and an unrecognised adapter all read as "not
    demo". A falsy-but-present value must not be enough either: `is not None`
    would accept `paper=False`, which is exactly the live account this gate
    exists to refuse.
    """
    assert daemon._adapter_is_demo(SimpleNamespace()) is False
    assert daemon._adapter_is_demo(SimpleNamespace(paper=False)) is False
    assert daemon._adapter_is_demo(SimpleNamespace(demo=False)) is False
    assert daemon._adapter_is_demo(SimpleNamespace(paper=None)) is False
    assert daemon._adapter_is_demo(SimpleNamespace(paper=True)) is True
    assert daemon._adapter_is_demo(SimpleNamespace(demo=True)) is True
    # The real adapter goes through its own branch, which a SimpleNamespace
    # double never reaches; `paper=False` must stay "not demo" there too.
    assert (
        daemon._adapter_is_demo(
            AlpacaLiveAdapter(api_key="k", secret_key="s", paper=False)
        )
        is False
    )
    assert (
        daemon._adapter_is_demo(
            AlpacaLiveAdapter(api_key="k", secret_key="s", paper=True)
        )
        is True
    )


def test_demo_gate_refuses_a_newly_built_non_demo_adapter(
    isolated_db_sandbox, monkeypatch
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    account = database.list_live_accounts()[0]

    def _factory(*args, **kwargs):
        return _ControlAdapter(demo=False)

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


def _submit(database, adapter: _ControlAdapter) -> None:
    asyncio.run(
        daemon._submit_normalized_discord_signal(
            ACCOUNT_ID,
            adapter,
            _deployment(database),
            _trade_signal(),
            signal_timeframe="1h",
            wait_enabled=False,
        )
    )


def test_submit_exception_keeps_the_claim_so_the_drain_is_not_drained(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database, status="ACTIVE")
    adapter = _ControlAdapter(
        submit_error=RuntimeError("broker timeout after the order was sent")
    )

    _submit(database, adapter)

    assert adapter.submitted, "the order reached the adapter"
    claims = list_live_deployment_submission_claims(DEPLOYMENT_ID)
    assert len(claims) == 1
    assert claims[0].expired is False


def test_pre_broker_refusal_releases_the_claim(isolated_db_sandbox) -> None:
    """A refusal the adapter proves happened before the request must not lock
    the deployment out for the lease TTL.

    The failure is still reported as a failed submission, but the next signal
    may try immediately: nothing reached the broker, so there is no duplicate
    risk to hold the lease for.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database, status="ACTIVE")
    adapter = _ControlAdapter(
        pre_submit_error=ValueError("volume below the broker minimum")
    )

    _submit(database, adapter)

    assert adapter.submitted, "the adapter was asked to submit"
    assert list_live_deployment_submission_claims(DEPLOYMENT_ID) == []


def test_preparation_failure_never_takes_the_claim(
    isolated_db_sandbox, monkeypatch
) -> None:
    """Sizing/validation runs before the claim, so it cannot hold a lease."""
    database, _, _ = isolated_db_sandbox
    _seed(database, status="ACTIVE")
    adapter = _ControlAdapter()

    async def _reject(*args, **kwargs):
        raise ValueError("Refusing to submit live order without stop loss")

    monkeypatch.setattr(daemon, "prepare_order_with_risk", _reject)

    _submit(database, adapter)

    assert adapter.submitted == []
    assert list_live_deployment_submission_claims(DEPLOYMENT_ID) == []


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


def test_alpaca_stopped_never_leaves_a_naked_position_when_the_flatten_fails(
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
            _FakeOrder(
                "sl-1", "AAPL", "sell", order_type="stop", qty=10.0, stop_price=190.0
            ),
            _FakeOrder("tp-1", "AAPL", "sell", order_type="limit", qty=10.0),
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
    # The entry is gone, the position survived, and it is protected again by a
    # stop at the price the cancelled protection carried: no naked position.
    assert "entry-1" not in [order.id for order in client.orders]
    stops = [order for order in client.orders if order.order_type == "stop"]
    assert len(stops) == 1
    assert stops[0].side == "sell"
    assert stops[0].qty == 10.0
    assert stops[0].stop_price == 190.0


# ─── Enforcement stamp: leases, status guard, persisted-stamp authority ──────


def test_stamp_is_refused_while_a_live_submission_lease_exists(isolated_db_sandbox) -> None:
    """An in-flight submit can still fill after the flatten; stamping would stop the retry."""
    database, _, _ = isolated_db_sandbox
    _seed(database, status="ACTIVE")
    assert database.claim_live_deployment_submission(DEPLOYMENT_ID, "tok-live") is True
    update_live_deployment_status(DEPLOYMENT_ID, "STOPPED")
    adapter = _ControlAdapter()

    ledger = _enforce(database, adapter)

    assert adapter.calls == ["cancel", "flatten", "orphan"], "the flatten must still run"
    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None

    # Once the submission outcome is known the lease is released and the next tick stamps.
    assert database.release_live_deployment_submission("tok-live") is True
    retry = _ControlAdapter()
    ledger = _enforce(database, retry)
    assert retry.calls == ["cancel", "flatten", "orphan"]
    assert DEPLOYMENT_ID in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is not None


def test_stamp_is_refused_while_an_expired_but_unreleased_lease_exists(
    isolated_db_sandbox,
) -> None:
    """An expired lease means an outcome was never observed - not that nothing happened."""
    database, _, _ = isolated_db_sandbox
    _seed(database, status="ACTIVE")
    assert (
        database.claim_live_deployment_submission(DEPLOYMENT_ID, "tok-old", ttl_seconds=0.01)
        is True
    )
    update_live_deployment_status(DEPLOYMENT_ID, "STOPPED")
    import time

    time.sleep(0.05)
    assert list_live_deployment_submission_claims(DEPLOYMENT_ID)[0].expired is True

    ledger = _enforce(database, _ControlAdapter())

    assert DEPLOYMENT_ID not in ledger
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_mark_enforced_only_writes_while_the_deployment_is_stopped(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database, status="PAUSED")

    assert database.mark_live_deployment_stopped_enforced(DEPLOYMENT_ID) is False
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None

    update_live_deployment_status(DEPLOYMENT_ID, "STOPPED")
    assert database.mark_live_deployment_stopped_enforced(DEPLOYMENT_ID) is True
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is not None


def test_resume_and_restop_between_ticks_is_reenforced_despite_the_ledger(
    isolated_db_sandbox,
) -> None:
    """The in-memory ledger never saw the deployment leave STOPPED; the stamp did."""
    database, _, _ = isolated_db_sandbox
    _seed(database)
    ledger = _enforce(database, _ControlAdapter())
    assert DEPLOYMENT_ID in ledger

    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")
    update_live_deployment_status(DEPLOYMENT_ID, "STOPPED")
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None

    second = _ControlAdapter()
    _enforce(database, second, already=ledger)

    assert second.calls == ["cancel", "flatten", "orphan"], (
        "a stale in-memory ledger skipped enforcement of a freshly re-stopped deployment"
    )
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is not None


def test_a_persisted_stamp_skips_enforcement_even_with_an_empty_ledger(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    _enforce(database, _ControlAdapter())

    again = _ControlAdapter()
    _enforce(database, again, already=set())

    assert again.calls == []


# ─── Operator intent on service status ───────────────────────────────────────


@pytest.mark.parametrize("operator_status", [ServiceStatus.PAUSED, ServiceStatus.STOPPED])
@pytest.mark.parametrize("automated_status", [ServiceStatus.FAILED, ServiceStatus.RUNNING])
def test_automated_writers_never_overwrite_operator_intent(
    isolated_db_sandbox, operator_status, automated_status
) -> None:
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(name="bot", type=ServiceType.DISCORD_BOT, status=ServiceStatus.RUNNING)
    )
    update_service_status(
        str(service.id), operator_status, respect_operator_intent=False
    )  # the operator establishes the pause/stop

    update_service_status(str(service.id), automated_status, respect_operator_intent=True)

    assert database.get_service(str(service.id)).status == operator_status
    # The operator (respect_operator_intent=False) can still change it.
    update_service_status(
        str(service.id), ServiceStatus.RUNNING, respect_operator_intent=False
    )
    assert database.get_service(str(service.id)).status == ServiceStatus.RUNNING


@pytest.mark.parametrize("operator_status", [ServiceStatus.PAUSED, ServiceStatus.STOPPED])
def test_discord_reconnect_does_not_mark_an_operator_paused_or_stopped_bot_running(
    isolated_db_sandbox, operator_status
) -> None:
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(name="bot", type=ServiceType.DISCORD_BOT, status=ServiceStatus.FAILED)
    )
    update_service_status(
        str(service.id), operator_status, respect_operator_intent=False
    )  # the operator establishes the pause/stop
    service_id = str(service.id)

    class _Source:
        is_connected = False

        async def reconnect(self) -> None:
            return None

    daemon._DISCORD_SERVICE_RECONNECT_RETRY_NOT_BEFORE.pop(service_id, None)
    asyncio.run(
        daemon._reconnect_discord_bot_source(
            service, _Source(), service_id, running_task=None
        )
    )

    assert database.get_service(service_id).status == operator_status


@pytest.mark.parametrize("operator_status", [ServiceStatus.PAUSED, ServiceStatus.STOPPED])
def test_discord_terminal_listener_failure_does_not_overwrite_operator_intent(
    isolated_db_sandbox, operator_status
) -> None:
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(name="bot", type=ServiceType.DISCORD_BOT, status=ServiceStatus.RUNNING)
    )
    update_service_status(
        str(service.id), operator_status, respect_operator_intent=False
    )  # the operator establishes the pause/stop

    class _Source:
        is_paused = False

        async def listen_signals(self):
            raise RuntimeError("401 Unauthorized: improper token")
            yield  # pragma: no cover

    assert daemon._is_terminal_discord_runtime_error(RuntimeError("401 Unauthorized"))
    asyncio.run(daemon._run_discord_bot_service_listener(service, _Source()))

    assert database.get_service(str(service.id)).status == operator_status


def test_resumed_and_restopped_deployment_with_a_stale_ledger_still_needs_enforcement(
    isolated_db_sandbox,
) -> None:
    """Resume + re-stop between ticks, sole deployment on its account, stale ledger.

    The resume cleared the persisted stamp, but the daemon never observed the deployment leave
    STOPPED, so its in-memory ledger still lists it. Two things must not trust that ledger:

    * account selection, otherwise the account gets no adapter and the stop is never enforced;
    * the ledger itself, which must be cleaned so a failed sweep this tick does not leave the
      stale entry behind to hide the deployment on later ticks.
    """
    database, _, _ = isolated_db_sandbox
    _seed(database)
    ledger = _enforce(database, _ControlAdapter())
    assert DEPLOYMENT_ID in ledger
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")
    update_live_deployment_status(DEPLOYMENT_ID, "STOPPED")
    deployment = _deployment(database)
    assert deployment.stopped_enforced_at is None

    needing = daemon._account_ids_needing_stopped_enforcement([deployment], set(ledger))
    assert needing == {ACCOUNT_ID}, "a stale ledger kept the account from getting an adapter"

    # A sweep that fails this tick must still leave the ledger without the stale entry.
    failing = _ControlAdapter(flatten=ControlOutcome(completed=0, failed=("pos-1",)))
    ledger = _enforce(database, failing, already=ledger)
    assert failing.calls[:2] == ["cancel", "flatten"]
    assert DEPLOYMENT_ID not in ledger, "the stale ledger entry survived a re-stop"
    assert get_live_deployment(DEPLOYMENT_ID).stopped_enforced_at is None


def test_a_stamped_deployment_in_the_ledger_needs_no_adapter(isolated_db_sandbox) -> None:
    database, _, _ = isolated_db_sandbox
    _seed(database)
    ledger = _enforce(database, _ControlAdapter())

    assert (
        daemon._account_ids_needing_stopped_enforcement([_deployment(database)], ledger) == set()
    )
