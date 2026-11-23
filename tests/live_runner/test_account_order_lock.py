"""#362: per-account serialization of broker position reads with order placement.

Maintainer decision (2026-11-23, option b): every path that reads broker
position/qty for sizing, pre-submit caps, leverage or exit qty and then submits
runs inside one per-account critical section, so another of this daemon's
signal submissions - or a STOPPED/kill-switch flatten - cannot interleave
between the read and the order. A bounded lock wait that expires refuses the
order - ``decision.refused`` on the leased path, a raised
``AccountOrderLockTimeoutError`` on ``submit_order_with_risk`` - and it never
submits on a read that may be stale. Broker-side fills can still move the book
inside the section; reconciliation is the backstop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

import live_runner.account_order_lock as account_order_lock
import live_runner.daemon as daemon
import live_runner.kill_switch as kill_switch
from live_runner.base_adapter import ControlOutcome, LivePipValue, LiveQuote
from live_runner.daemon import (
    SUBMIT_OUTCOME_REFUSED,
    AccountOrderLockTimeoutError,
    _account_order_lock,
    _account_order_placement,
    _prepare_claim_and_submit_signal,
    submit_order_with_risk,
)
from live_runner.kill_switch import KillSwitchMode, KillSwitchState
from live_runner.signal_archive import SignalDecisionRecorder
from risk import TradeSignal
from shared.models import LiveDeploymentConfig, LiveSignal


@pytest.fixture(autouse=True)
def _fresh_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_daemon`` leaves the process-wide gate closed; never leak that."""
    from live_runner.shutdown import ShutdownCoordinator

    fresh = ShutdownCoordinator(force_exit_fn=lambda code: pytest.fail("forced exit"))
    monkeypatch.setattr(daemon, "_SHUTDOWN", fresh)
    monkeypatch.setattr(daemon, "_ACTIVE_SHUTDOWN", fresh)


def _deployment(
    *, account_id: str = "acc-shared", status: str = "ACTIVE"
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid.uuid4()),
        strategy_name="discord::362",
        account_id=account_id,
        symbols=["AAPL", "MSFT", "EURUSD"],
        timeframe="1h",
        status=status,
        signal_source="discord",
        signal_source_type="discord",
        signal_config={"channel_id": "100000000000000362"},
        settings={"max_risk_per_trade": 0.01, "max_total_exposure": 0.05},
    )


def _entry_signal(deployment: LiveDeploymentConfig, *, symbol: str = "EURUSD") -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol=symbol,
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0, "take_profit": 110.0},
    )


def _exit_trade_signal(symbol: str) -> TradeSignal:
    return TradeSignal(
        action="sell",
        symbol=symbol,
        entry_price=100.0,
        stop_loss=None,
        take_profit=None,
        timeframe="1h",
        metadata={
            "source": "discord",
            "signal_intent": "exit",
            "exit_only": True,
            "risk_sizing_mode": "exit_only",
            "exit_fraction": 1.0,
            "exit_position_side": "long",
            "discord_channel_id": "100000000000000362",
            "discord_message_id": f"900000000000000362-{symbol}",
            "discord_author_id": "bot-362",
            "received_at": datetime.now(timezone.utc).isoformat(),
        },
    )



class _Archive:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class _FakeDecision:
    """Stand-in for SignalDecisionRecorder on the daemon-created path."""

    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def refused(self, reason: str, **_kwargs: Any) -> None:
        self.records.append(("refused", reason))

    def ignored(self, reason: str, **_kwargs: Any) -> None:
        self.records.append(("ignored", reason))

    def unknown(self, reason: str, **_kwargs: Any) -> None:
        self.records.append(("unknown", reason))


class _BlockingAdapter:
    """Adapter double that records broker reads and submits and can park.

    ``submit_order`` waits on ``release`` so a test can prove what the second
    submission of the same account does while the first is still at the broker.
    """

    demo = True

    def __init__(self, positions: list[dict[str, Any]]) -> None:
        self.positions = positions
        self.events: list[str] = []
        self.release = asyncio.Event()
        self.submitted: list[tuple[Any, float | None]] = []

    async def get_positions(self) -> list[dict[str, Any]]:
        self.events.append("positions")
        return list(self.positions)

    async def get_balance(self) -> tuple[float, float]:
        return 100_000.0, 100_000.0

    async def get_equity(self) -> float:
        return 100_000.0

    async def get_quote(self, symbol: str) -> LiveQuote:
        return LiveQuote(
            bid=100.0,
            ask=100.0,
            timestamp=datetime.now(timezone.utc),
            source="test",
        )

    async def get_pip_value(self, symbol: str) -> LivePipValue:
        return LivePipValue(
            value=1.0,
            units_per_qty=1.0,
            conversion_rate=1.0,
            quote_asset="USD",
            account_asset="USD",
            source="test",
        )

    async def get_used_margin(self) -> float | None:
        return 0.0

    async def submit_order(self, signal: Any, *, qty: float | None = None) -> str:
        self.events.append("submit-begin")
        self.submitted.append((signal, qty))
        # Only the first submit parks: a test releases it once it has proven
        # what a concurrent submission did meanwhile. Later submits return at
        # once so a second account is never stuck behind the parked one.
        if len(self.submitted) == 1:
            await self.release.wait()
        self.events.append("submit-end")
        return f"order-{len(self.submitted)}"


class _FlattenAdapter:
    """Control-plane double for STOPPED and kill-switch flatten sweeps."""

    supports_cancel_orders = True
    supports_flatten = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def cancel_open_orders(self) -> ControlOutcome:
        self.calls.append("cancel_open_orders")
        return ControlOutcome(completed=1)

    async def close_all_positions(self) -> ControlOutcome:
        self.calls.append("close_all_positions")
        return ControlOutcome(completed=1)

    async def cancel_orphaned_protections(self) -> ControlOutcome:
        self.calls.append("cancel_orphaned_protections")
        return ControlOutcome()


def _patch_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        daemon, "claim_live_deployment_submission", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        daemon, "release_live_deployment_submission", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        daemon, "renew_live_deployment_submission", lambda *_a, **_k: True
    )
    monkeypatch.setattr(daemon, "_deployment_can_dispatch", lambda _d, **_k: True)


def _patch_exit_ledgers(
    monkeypatch: pytest.MonkeyPatch,
    account_id: str,
    ledgers_by_symbol: dict[str, dict[str, float]],
) -> None:
    """Per-symbol deployment ledgers and an empty unfilled-exit book."""

    def _account_ledgers(account_id_: str, symbol: str) -> dict[str, float]:
        return dict(ledgers_by_symbol.get(symbol.upper(), {}))

    monkeypatch.setattr(
        daemon,
        "get_account_net_filled_qty_by_deployment",
        _account_ledgers,
        raising=False,
    )
    monkeypatch.setattr(
        daemon,
        "list_live_deployment_unfilled_exit_orders",
        lambda *_a, **_k: [],
        raising=False,
    )
    monkeypatch.setattr(
        daemon,
        "list_account_unfilled_exit_orders",
        lambda *_a, **_k: [],
        raising=False,
    )


async def _wait_for_event(predicate, *, what: str) -> None:
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


# ── Serialization: no interleaved read/submit on one account ─────────────────


def test_concurrent_entry_submissions_on_one_account_are_serialized(
    monkeypatch,
) -> None:
    """Two entries of the same account: the second reads broker positions only
    after the first submit has returned - never between its read and its write."""
    first, second = _deployment(), _deployment()
    assert first.account_id == second.account_id
    adapter = _BlockingAdapter(positions=[])

    async def _both() -> None:
        tasks = [
            asyncio.create_task(
                submit_order_with_risk(adapter, deployment, _entry_signal(deployment))
            )
            for deployment in (first, second)
        ]
        await _wait_for_event(
            lambda: "submit-begin" in adapter.events, what="first submit"
        )
        # The first submission is parked inside submit_order; the second has
        # not even read the broker book.
        await asyncio.sleep(0.2)
        assert adapter.events.count("positions") == 1
        adapter.release.set()
        await asyncio.gather(*tasks)

    asyncio.run(_both())

    assert adapter.events == [
        "positions",
        "submit-begin",
        "submit-end",
        "positions",
        "submit-begin",
        "submit-end",
    ]
    assert len(adapter.submitted) == 2


def test_concurrent_exits_on_different_symbols_one_account_are_serialized(
    monkeypatch,
) -> None:
    """_exit_lock covers only one account+symbol pair; the account lock is
    what keeps two different symbols of one account from interleaving."""
    _patch_claims(monkeypatch)
    first, second = _deployment(), _deployment()
    _patch_exit_ledgers(
        monkeypatch,
        first.account_id,
        {
            "AAPL": {str(first.id): 10.0},
            "MSFT": {str(second.id): 8.0},
        },
    )
    adapter = _BlockingAdapter(
        positions=[
            {"symbol": "AAPL", "qty": 10.0},
            {"symbol": "MSFT", "qty": 8.0},
        ]
    )

    async def _both() -> None:
        tasks = [
            asyncio.create_task(
                daemon._submit_normalized_discord_signal(
                    deployment.account_id,
                    adapter,
                    deployment,
                    _exit_trade_signal(symbol),
                    signal_timeframe="1h",
                    wait_enabled=False,
                )
            )
            for deployment, symbol in ((first, "AAPL"), (second, "MSFT"))
        ]
        await _wait_for_event(
            lambda: "submit-begin" in adapter.events, what="first submit"
        )
        await asyncio.sleep(0.2)
        assert adapter.events.count("positions") == 1
        adapter.release.set()
        await asyncio.gather(*tasks)

    asyncio.run(_both())

    assert adapter.events == [
        "positions",
        "submit-begin",
        "submit-end",
        "positions",
        "submit-begin",
        "submit-end",
    ]
    assert len(adapter.submitted) == 2


def test_submissions_on_different_accounts_do_not_block_each_other(
    monkeypatch,
) -> None:
    """The lock is per account: a parked submit on acc-a must not stall acc-b.

    The timeout is deliberately tiny: if the lock were ever keyed globally this
    test fails in milliseconds instead of parking CI on the production 120 s
    bound.
    """
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.2)
    other_account = _deployment(account_id="acc-other")
    parked = _deployment(account_id="acc-parked")
    adapter = _BlockingAdapter(positions=[])

    async def _run() -> None:
        parked_task = asyncio.create_task(
            submit_order_with_risk(adapter, parked, _entry_signal(parked))
        )
        await _wait_for_event(
            lambda: "submit-begin" in adapter.events, what="parked submit"
        )
        # acc-other takes its own lock and submits while acc-parked is held.
        await submit_order_with_risk(adapter, other_account, _entry_signal(other_account))
        # The other account's submit already returned; the parked one has not.
        assert adapter.events.count("submit-begin") == 2
        assert adapter.events.count("submit-end") == 1
        adapter.release.set()
        await parked_task

    asyncio.run(_run())

    assert adapter.events.count("submit-end") == 2


# ── Bounded wait: refuse, never submit on a stale read ────────────────────────


def test_submit_order_with_risk_refuses_when_the_account_lock_times_out(
    monkeypatch,
) -> None:
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    deployment = _deployment()
    adapter = _BlockingAdapter(positions=[])

    async def _run() -> None:
        lock = _account_order_lock(deployment.account_id)
        await lock.acquire()
        try:
            with pytest.raises(AccountOrderLockTimeoutError):
                await submit_order_with_risk(
                    adapter, deployment, _entry_signal(deployment)
                )
        finally:
            lock.release()

    asyncio.run(_run())

    # The refusal happened before any broker read or submit.
    assert adapter.events == []
    assert adapter.submitted == []


def test_leased_path_records_decision_refused_on_lock_timeout(
    monkeypatch,
) -> None:
    """The decision.refused record the maintainer decision requires."""
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    _patch_claims(monkeypatch)
    deployment = _deployment()
    adapter = _BlockingAdapter(positions=[])
    normalized = _exit_trade_signal("AAPL")
    live_signal = LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata=dict(normalized.metadata),
    )

    archive = _Archive()
    decision = SignalDecisionRecorder(
        trade_signal=normalized,
        account_id=deployment.account_id,
        deployment=deployment,
        archive=archive,
    )

    async def _run() -> str:
        lock = _account_order_lock(deployment.account_id)
        await lock.acquire()
        try:
            return await _prepare_claim_and_submit_signal(
                deployment.account_id,
                adapter,
                deployment,
                normalized,
                live_signal,
                is_exit=True,
                decision=decision,
            )
        finally:
            lock.release()

    outcome = asyncio.run(_run())

    assert outcome == SUBMIT_OUTCOME_REFUSED
    assert adapter.events == []
    assert adapter.submitted == []
    assert len(archive.records) == 1
    record = archive.records[0]
    assert record["type"] == "discord_signal_decision"
    assert record["outcome"] == "refused"
    assert record["reason"] == "account_order_lock_timeout"


def test_the_lock_is_released_after_a_timeout_refusal(monkeypatch) -> None:
    """A timed-out waiter must not leak the lock: the next submission proceeds."""
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    deployment = _deployment()
    adapter = _BlockingAdapter(positions=[])

    async def _run() -> None:
        lock = _account_order_lock(deployment.account_id)
        await lock.acquire()
        try:
            with pytest.raises(AccountOrderLockTimeoutError):
                await submit_order_with_risk(
                    adapter, deployment, _entry_signal(deployment)
                )
        finally:
            lock.release()
        adapter.release.set()
        await submit_order_with_risk(adapter, deployment, _entry_signal(deployment))

    asyncio.run(_run())

    assert len(adapter.submitted) == 1


# ── Lock ordering: _exit_lock outer, account lock inner ──────────────────────


def test_exit_submit_holds_exit_lock_outside_the_account_lock(monkeypatch) -> None:
    """Pin the only safe ordering: ``_exit_lock`` is held while the account
    lock is held, and nothing takes ``_exit_lock`` inside the account section."""
    _patch_claims(monkeypatch)
    deployment = _deployment(account_id="acc-order")
    _patch_exit_ledgers(
        monkeypatch,
        deployment.account_id,
        {"AAPL": {str(deployment.id): 5.0}},
    )

    class _ProbeAdapter(_BlockingAdapter):
        def __init__(self) -> None:
            super().__init__(positions=[{"symbol": "AAPL", "qty": 5.0}])
            self.lock_states: list[tuple[bool, bool]] = []
            self.release.set()

        async def submit_order(self, signal: Any, *, qty: float | None = None) -> str:
            self.lock_states.append(
                (
                    daemon._exit_lock(deployment.account_id, signal.symbol).locked(),
                    _account_order_lock(deployment.account_id).locked(),
                )
            )
            return await super().submit_order(signal, qty=qty)

    adapter = _ProbeAdapter()

    async def _run() -> None:
        await daemon._submit_normalized_discord_signal(
            deployment.account_id,
            adapter,
            deployment,
            _exit_trade_signal("AAPL"),
            signal_timeframe="1h",
            wait_enabled=False,
        )
        assert not daemon._exit_lock(deployment.account_id, "AAPL").locked()

    asyncio.run(_run())

    assert adapter.lock_states == [(True, True)]
    assert len(adapter.submitted) == 1


# ── Exit-path lock timeout: the surviving-mutant coverage ────────────────────


def test_exit_path_lock_timeout_refuses_and_releases_the_exit_lock(
    monkeypatch, caplog
) -> None:
    """An exit that cannot take the account lock is a refused exit, not a
    submit: ``decision.refused``, an error log, a ``prepare_exit`` rejection
    record, and ``_exit_lock`` released afterwards."""
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    _patch_claims(monkeypatch)
    deployment = _deployment(account_id="acc-exit-timeout")
    adapter = _BlockingAdapter(positions=[])
    decision = _FakeDecision()
    monkeypatch.setattr(
        daemon, "SignalDecisionRecorder", lambda **_kwargs: decision
    )
    persisted_stages: list[str] = []

    async def _persist(
        _deployment: LiveDeploymentConfig,
        _signal: LiveSignal,
        *,
        stage: str,
        exc: BaseException,
    ) -> None:
        persisted_stages.append(stage)

    monkeypatch.setattr(daemon, "_persist_submission_rejection", _persist)

    async def _run() -> None:
        lock = _account_order_lock(deployment.account_id)
        await lock.acquire()
        try:
            await daemon._submit_normalized_discord_signal(
                deployment.account_id,
                adapter,
                deployment,
                _exit_trade_signal("AAPL"),
                signal_timeframe="1h",
                wait_enabled=False,
            )
            assert not daemon._exit_lock(deployment.account_id, "AAPL").locked()
        finally:
            lock.release()

    with caplog.at_level(logging.ERROR):
        asyncio.run(_run())

    assert adapter.events == []
    assert adapter.submitted == []
    assert persisted_stages == ["prepare_exit"]
    assert decision.records == [("refused", "account_order_lock_timeout")]
    assert "order-placement lock" in caplog.text


# ── Signal age is re-checked after the lock wait ─────────────────────────────


def test_signal_age_is_rechecked_after_acquiring_the_lock(monkeypatch) -> None:
    """A signal fresh at dispatch but stale after the lock wait is refused
    before any broker read or submit."""
    _patch_claims(monkeypatch)
    deployment = _deployment()
    deployment.signal_config = {
        **(deployment.signal_config or {}),
        "max_signal_age_seconds": 0.3,
    }
    adapter = _BlockingAdapter(positions=[])
    decision = _FakeDecision()
    monkeypatch.setattr(
        daemon, "SignalDecisionRecorder", lambda **_kwargs: decision
    )
    persisted_stages: list[str] = []

    async def _persist(
        _deployment: LiveDeploymentConfig,
        _signal: LiveSignal,
        *,
        stage: str,
        exc: BaseException,
    ) -> None:
        persisted_stages.append(stage)

    monkeypatch.setattr(daemon, "_persist_submission_rejection", _persist)
    signal = _exit_trade_signal("AAPL")

    async def _run() -> None:
        lock = _account_order_lock(deployment.account_id)
        await lock.acquire()
        task = asyncio.create_task(
            daemon._submit_normalized_discord_signal(
                deployment.account_id,
                adapter,
                deployment,
                signal,
                signal_timeframe="1h",
                wait_enabled=False,
            )
        )
        try:
            # The pre-lock age gate has already passed; the signal only expires
            # while this submission is queued on the account lock.
            await asyncio.sleep(0.45)
            assert adapter.events == []
        finally:
            lock.release()
        await task

    asyncio.run(_run())

    assert adapter.events == []
    assert adapter.submitted == []
    assert persisted_stages == ["prepare_exit"]
    assert decision.records == [("refused", "signal_aged_out_during_lock_wait")]


# ── STOPPED and kill-switch flattens share the account lock ──────────────────


def test_stopped_flatten_waits_for_the_account_lock(monkeypatch) -> None:
    deployment = _deployment(status="STOPPED")
    adapter = _FlattenAdapter()
    monkeypatch.setattr(
        daemon, "mark_live_deployment_stopped_enforced", lambda _deployment_id: True
    )

    async def _run() -> None:
        lock = _account_order_lock(deployment.account_id)
        await lock.acquire()
        task = asyncio.create_task(
            daemon._enforce_stopped_deployments(
                [deployment],
                {deployment.account_id: adapter},
                already_enforced=set(),
            )
        )
        try:
            await asyncio.sleep(0.1)
            assert adapter.calls == []
        finally:
            lock.release()
        await task

    asyncio.run(_run())

    assert adapter.calls == [
        "cancel_open_orders",
        "close_all_positions",
        "cancel_orphaned_protections",
    ]


def test_stopped_flatten_lock_timeout_leaves_the_deployment_unenforced(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    deployment = _deployment(status="STOPPED")
    adapter = _FlattenAdapter()
    already_enforced: set[str] = set()
    monkeypatch.setattr(
        daemon, "mark_live_deployment_stopped_enforced", lambda _deployment_id: True
    )

    async def _run() -> None:
        lock = _account_order_lock(deployment.account_id)
        await lock.acquire()
        try:
            await daemon._enforce_stopped_deployments(
                [deployment],
                {deployment.account_id: adapter},
                already_enforced=already_enforced,
            )
        finally:
            lock.release()

    with caplog.at_level(logging.ERROR):
        asyncio.run(_run())

    assert adapter.calls == []
    assert already_enforced == set()
    assert "STOPPED enforcement refused" in caplog.text


def test_kill_switch_flatten_waits_for_the_account_lock(monkeypatch) -> None:
    account_id = "acc-kill-wait"
    adapter = _FlattenAdapter()
    state = KillSwitchState(halts={account_id: KillSwitchMode.FLATTEN})

    async def _run() -> None:
        lock = _account_order_lock(account_id)
        await lock.acquire()
        task = asyncio.create_task(
            kill_switch.enforce_live_kill_switch(
                {account_id: adapter}, [account_id], state=state
            )
        )
        try:
            await asyncio.sleep(0.1)
            assert adapter.calls == []
        finally:
            lock.release()
        report = await task
        assert report.swept == (account_id,)
        assert report.unmet == ()

    asyncio.run(_run())

    assert adapter.calls == [
        "cancel_open_orders",
        "close_all_positions",
        "cancel_orphaned_protections",
    ]


def test_kill_switch_flatten_lock_timeout_is_unmet(monkeypatch, caplog) -> None:
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 0.05)
    account_id = "acc-kill-timeout"
    adapter = _FlattenAdapter()
    state = KillSwitchState(halts={account_id: KillSwitchMode.FLATTEN})

    async def _run():
        lock = _account_order_lock(account_id)
        await lock.acquire()
        try:
            return await kill_switch.enforce_live_kill_switch(
                {account_id: adapter}, [account_id], state=state
            )
        finally:
            lock.release()

    with caplog.at_level(logging.ERROR):
        report = asyncio.run(_run())

    assert adapter.calls == []
    assert report.swept == ()
    assert report.unmet == (account_id,)
    assert "Kill-switch flatten refused" in caplog.text


# ── Stall warnings ───────────────────────────────────────────────────────────


def test_lock_wait_past_the_warning_threshold_is_logged(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_WAIT_WARN_SECONDS", 0.01)
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_TIMEOUT_SECONDS", 1.0)

    async def _run() -> None:
        lock = account_order_lock.account_order_lock("acc-wait-warn")
        await lock.acquire()

        async def _waiter() -> None:
            async with account_order_lock.account_order_placement("acc-wait-warn"):
                pass

        task = asyncio.create_task(_waiter())
        try:
            await asyncio.sleep(0.05)
        finally:
            lock.release()
        await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())

    assert "wait exceeded" in caplog.text


def test_lock_hold_past_the_warning_threshold_is_logged(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_WAIT_WARN_SECONDS", 999.0)
    monkeypatch.setattr(account_order_lock, "ACCOUNT_ORDER_LOCK_HOLD_WARN_SECONDS", 0.01)

    async def _run() -> None:
        async with account_order_lock.account_order_placement("acc-hold-warn"):
            await asyncio.sleep(0.05)

    with caplog.at_level(logging.WARNING):
        asyncio.run(_run())

    assert "lock held for" in caplog.text


def test_account_order_placement_yields_and_releases() -> None:
    """Unit pin for the context manager itself."""

    async def _run() -> None:
        async with _account_order_placement("acc-unit"):
            assert _account_order_lock("acc-unit").locked()
        assert not _account_order_lock("acc-unit").locked()

    asyncio.run(_run())
