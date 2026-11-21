"""The live kill switch (#300) at both of the daemon's checkpoints.

Two checkpoints, both covered here:

* **Before every entry.** ``prepare_order_with_risk`` is the single chokepoint
  every entry order - Discord and internal alike - passes through, and it
  already routes exits away before any entry gate runs. A halt therefore refuses
  entries without ever blocking a position being closed.
* **On every tick.** The deployment-sync loop enforces the halt against the
  broker through the same adapter control plane STOPPED enforcement uses, so a
  ``flatten`` halt really does leave the account flat.

Plus the two properties that make a halt worth having: it survives a restart
(the state is read from the database, never cached in the process), and a state
that cannot be read halts rather than trades.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

import live_runner.kill_switch as kill_switch
from live_runner.base_adapter import ControlOutcome, LivePipValue, LiveQuote
from live_runner.kill_switch import (
    KILL_SWITCH_GLOBAL_SCOPE,
    KillSwitchMode,
    LiveHaltError,
    enforce_live_kill_switch,
    read_kill_switch_state,
    refuse_entry_while_halted,
    strictest_kill_switch_mode,
)
from shared.database import (
    KILL_SWITCH_MODES,
    LiveAccountModel,
    clear_live_kill_switch,
    set_live_kill_switch,
)
from shared.models import LiveDeploymentConfig, LiveSignal

ACCOUNT = "acc-halt"


# ── Adapter doubles ───────────────────────────────────────────────────────────


class _EntryAdapter:
    """Provably demo, so the promotion gate (#165) passes through to the halt."""

    demo = True

    def __init__(self, positions: list[dict[str, object]] | None = None) -> None:
        self.submitted: list[tuple[LiveSignal, float | None]] = []
        # Empty by default: an entry is sized against a flat account. The exit
        # test below hands it the position that exit closes, because an exit is
        # sized against the broker position and there is nothing to size an
        # exit from in a flat one.
        self.positions: list[dict[str, object]] = list(positions or [])

    async def submit_order(self, signal, *, qty=None):
        self.submitted.append((signal, qty))
        return "order-1"

    async def get_positions(self):
        return list(self.positions)

    async def get_balance(self):
        return 100_000.0, 100_000.0

    async def get_pip_value(self, symbol):
        return LivePipValue(
            value=1.0,
            units_per_qty=1.0,
            conversion_rate=1.0,
            quote_asset="USD",
            account_asset="USD",
            source="test",
        )

    async def get_quote(self, symbol):
        return LiveQuote(
            bid=100.0,
            ask=100.0,
            timestamp=datetime.now(timezone.utc),
            source="test",
            timestamp_source="broker",
        )


class _SweepAdapter:
    """Records the control-plane calls a halt makes, and reports what it wants.

    ``flatten_outcome`` is what ``close_all_positions`` returns, so a partial or
    failing sweep can be simulated without a broker.
    """

    demo = True
    supports_cancel_orders = True
    supports_flatten = True

    def __init__(self, *, flatten_outcome: ControlOutcome | None = None) -> None:
        self.calls: list[str] = []
        self.flatten_outcome = flatten_outcome

    async def cancel_open_orders(self) -> ControlOutcome:
        self.calls.append("cancel_open_orders")
        return ControlOutcome(completed=1)

    async def close_all_positions(self) -> ControlOutcome:
        self.calls.append("close_all_positions")
        return self.flatten_outcome or ControlOutcome(completed=1)

    async def cancel_orphaned_protections(self) -> ControlOutcome:
        self.calls.append("cancel_orphaned_protections")
        return ControlOutcome()


def test_the_stored_modes_and_the_runner_modes_are_the_same_ranking() -> None:
    """The two layers cannot drift on what the modes are or how strict they are.

    ``shared.database`` stores a mode and refuses one it cannot interpret, and
    ``live_runner.kill_switch`` ranks them to combine a global halt with a
    per-account one. If either list changed alone, a stored mode would either be
    unreadable (fail closed, so merely loud) or - worse - be ranked as less
    strict than the operator asked for.
    """
    assert tuple(mode.value for mode in KillSwitchMode) == KILL_SWITCH_MODES


def _deployment(
    account_id: str = ACCOUNT, status: str = "ACTIVE"
) -> LiveDeploymentConfig:
    return LiveDeploymentConfig(
        id=str(uuid.uuid4()),
        strategy_name="demo",
        account_id=account_id,
        symbols=["AAPL"],
        timeframe="1h",
        status=status,
        signal_source="discord",
        signal_source_type="discord",
        signal_config={"channel_id": "100000000000000121"},
        settings={},
    )


def _entry_signal(deployment: LiveDeploymentConfig) -> LiveSignal:
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={"stop_loss": 95.0, "take_profit": 110.0},
    )


def _exit_signal(deployment: LiveDeploymentConfig) -> LiveSignal:
    """An exit order: it reduces exposure, so no halt may block it."""
    return LiveSignal(
        deployment_id=str(deployment.id),
        symbol="AAPL",
        action="sell",
        theoretical_price=100.0,
        timestamp=datetime.now(timezone.utc),
        metadata={
            "exit_only": True,
            "signal_intent": "exit",
            "risk_sizing_mode": "exit_only",
            "exit_fraction": 1.0,
            "qty": 5.0,
            "explicit_qty": 5.0,
        },
    )


def _add_account(database, account_id: str) -> str:
    """Register one live account, so an account-scoped halt may name it.

    A broker the model accepts (``LiveAccountConfig.broker`` is a literal), because
    the refuse-a-typo check in ``set_live_kill_switch`` can only answer for an
    ``live_accounts`` row it can actually read.
    """
    with database.get_session() as session:
        session.add(LiveAccountModel(id=account_id, broker="alpaca", name=account_id))
        session.commit()
    return account_id


@pytest.fixture
def account(isolated_db_sandbox):
    """One known live account: an account-scoped halt must name a real one."""
    database, _engine, _data_dir = isolated_db_sandbox
    database.init_db()
    return _add_account(database, ACCOUNT)


def _patch_exit_ledger(monkeypatch, deployment: LiveDeploymentConfig, qty: float) -> None:
    import live_runner.daemon as daemon

    def _net_filled_qty(*_args: Any, **_kwargs: Any) -> dict[str, float]:
        return {str(deployment.id): qty}

    monkeypatch.setattr(
        daemon, "get_account_net_filled_qty_by_deployment", _net_filled_qty, raising=False
    )
    monkeypatch.setattr(
        daemon, "list_live_deployment_unfilled_exit_orders", lambda *a, **k: [], raising=False
    )
    monkeypatch.setattr(
        daemon, "list_account_unfilled_exit_orders", lambda *a, **k: [], raising=False
    )


def _submit_entry(deployment: LiveDeploymentConfig, adapter: _EntryAdapter) -> None:
    import live_runner.daemon as daemon

    asyncio.run(
        daemon.submit_order_with_risk(adapter, deployment, _entry_signal(deployment))
    )


def _prepare(deployment, signal, adapter, monkeypatch):
    import live_runner.daemon as daemon

    return asyncio.run(daemon.prepare_order_with_risk(adapter, deployment, signal))


# ── Combining scopes: the strictest halt wins ─────────────────────────────────


def test_the_strictest_mode_wins_over_a_weaker_scope() -> None:
    assert (
        strictest_kill_switch_mode(["entries", "flatten"]) is KillSwitchMode.FLATTEN
    )
    assert strictest_kill_switch_mode(["orders", "entries"]) is KillSwitchMode.ORDERS
    assert strictest_kill_switch_mode([]) is None
    assert strictest_kill_switch_mode([None, "entries"]) is KillSwitchMode.ENTRIES


def test_a_global_halt_covers_an_account_with_no_scope_of_its_own(account) -> None:
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "orders", actor="operator")

    state = read_kill_switch_state()

    assert state.mode_for_account(account) is KillSwitchMode.ORDERS
    assert state.mode_for_account("some-other-account") is KillSwitchMode.ORDERS


def test_an_account_scope_cannot_relax_a_global_halt(account) -> None:
    """A per-account halt may only add restriction, never lift a global one.

    The other reading lets an account-specific write quietly resume trading on
    an account the operator halted platform-wide - the one direction in which
    this feature can lose money.
    """
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator")
    set_live_kill_switch(account, "entries", actor="operator")

    assert read_kill_switch_state().mode_for_account(account) is KillSwitchMode.FLATTEN

    clear_live_kill_switch(account, actor="operator")

    assert read_kill_switch_state().mode_for_account(account) is KillSwitchMode.FLATTEN


def test_a_global_scope_cannot_relax_a_stricter_account_halt(account) -> None:
    set_live_kill_switch(account, "flatten", actor="operator")
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "entries", actor="operator")

    assert read_kill_switch_state().mode_for_account(account) is KillSwitchMode.FLATTEN


def test_a_corrupt_stored_mode_fails_closed(isolated_db_sandbox, account) -> None:
    """A mode the daemon cannot interpret is a halt, not a green light.

    The write path refuses an unknown mode, so a bad row means hand-edited data
    or a bug elsewhere. Either way the safe reading is the strictest one that
    still refuses entries, and it must be loud.
    """
    import shared.database as database

    with database.get_session() as session:
        session.add(database.LiveKillSwitchModel(scope=KILL_SWITCH_GLOBAL_SCOPE, mode="nonsense"))
        session.commit()

    state = read_kill_switch_state()

    assert state.readable is False
    assert state.mode_for_account(account) is KillSwitchMode.ENTRIES


def test_an_unreadable_state_reads_as_a_halt_for_every_account(
    isolated_db_sandbox, account, monkeypatch, caplog
) -> None:
    """Fail closed: a database that cannot answer halts trading.

    An empty answer would mean "nothing is halted", and a read failure is
    indistinguishable from one - which is exactly the moment a kill switch must
    not be reported as off. It refuses entries rather than flattening, because
    liquidating positions the operator never asked to close is the more
    expensive mistake.
    """
    import live_runner.kill_switch as kill_switch

    def _unreadable() -> dict[str, str]:
        raise OSError("database is unavailable")

    monkeypatch.setattr(kill_switch, "read_live_kill_switch_scopes", _unreadable)

    with caplog.at_level(logging.ERROR):
        state = read_kill_switch_state()

    assert state.readable is False
    assert state.mode_for_account(account) is KillSwitchMode.ENTRIES
    assert state.mode_for_account("never-halted") is KillSwitchMode.ENTRIES
    assert "database is unavailable" in caplog.text


# ── Checkpoint 1: before every entry ──────────────────────────────────────────


@pytest.mark.parametrize("mode", [mode.value for mode in KillSwitchMode])
def test_an_entry_is_refused_while_the_account_is_halted(
    isolated_db_sandbox, account, mode: str
) -> None:
    deployment = _deployment()
    adapter = _EntryAdapter()
    set_live_kill_switch(account, mode, actor="operator")

    with pytest.raises(LiveHaltError, match=mode):
        _submit_entry(deployment, adapter)

    assert adapter.submitted == []


def test_a_global_halt_refuses_an_entry_for_any_account(
    isolated_db_sandbox, account
) -> None:
    deployment = _deployment(account_id="acc-not-named")
    adapter = _EntryAdapter()
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator")

    with pytest.raises(LiveHaltError, match="flatten"):
        _submit_entry(deployment, adapter)

    assert adapter.submitted == []


def test_a_halt_on_another_account_does_not_block_this_account(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    database, _engine, _data_dir = isolated_db_sandbox
    _add_account(database, "acc-halted-elsewhere")
    deployment = _deployment(account_id="acc-free")
    adapter = _EntryAdapter()
    set_live_kill_switch("acc-halted-elsewhere", "flatten", actor="operator")

    _submit_entry(deployment, adapter)

    assert len(adapter.submitted) == 1


def test_a_halt_never_blocks_an_exit(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """Closing a position is the one thing a halt must never prevent.

    A halt that trapped an operator in an open position would be the opposite of
    its purpose: it is meant to stop new risk, not to freeze the existing one.
    """
    deployment = _deployment()
    # The broker position this exit closes: an exit is sized against it, so a
    # flat account has nothing to close and the halt could not be told apart
    # from a refusal to close it.
    adapter = _EntryAdapter(positions=[{"symbol": "AAPL", "qty": 5.0}])
    _patch_exit_ledger(monkeypatch, deployment, 5.0)
    set_live_kill_switch(account, "flatten", actor="operator")

    prepared, qty = _prepare(deployment, _exit_signal(deployment), adapter, monkeypatch)

    assert qty == 5.0
    assert prepared.metadata["exit_only"] is True


def test_an_unreadable_halt_state_refuses_the_entry(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    deployment = _deployment()
    adapter = _EntryAdapter()

    import live_runner.kill_switch as kill_switch

    def _unreadable() -> dict[str, str]:
        raise OSError("database is unavailable")

    monkeypatch.setattr(kill_switch, "read_live_kill_switch_scopes", _unreadable)

    with pytest.raises(LiveHaltError, match="unreadable"):
        _submit_entry(deployment, adapter)

    assert adapter.submitted == []


def test_the_refusal_names_the_account_and_the_mode(
    isolated_db_sandbox, account
) -> None:
    deployment = _deployment()
    set_live_kill_switch(account, "orders", actor="operator")

    with pytest.raises(LiveHaltError) as excinfo:
        asyncio.run(
            refuse_entry_while_halted(account, deployment_id=str(deployment.id))
        )

    assert "orders" in str(excinfo.value)
    assert account in str(excinfo.value)
    assert str(deployment.id) in str(excinfo.value)


# ── Checkpoint 2: on every tick ───────────────────────────────────────────────


def test_flatten_cancels_orders_and_closes_positions(
    isolated_db_sandbox, account
) -> None:
    set_live_kill_switch(account, "flatten", actor="operator")
    adapter = _SweepAdapter()

    report = asyncio.run(
        enforce_live_kill_switch(
            {account: adapter}, [account], state=read_kill_switch_state()
        )
    )

    assert adapter.calls == [
        "cancel_open_orders",
        "close_all_positions",
        "cancel_orphaned_protections",
    ]
    assert report.swept == (account,)
    assert report.unmet == ()
    assert report.without_adapter == ()


def test_orders_mode_cancels_but_leaves_positions_open(
    isolated_db_sandbox, account
) -> None:
    set_live_kill_switch(account, "orders", actor="operator")
    adapter = _SweepAdapter()

    report = asyncio.run(
        enforce_live_kill_switch(
            {account: adapter}, [account], state=read_kill_switch_state()
        )
    )

    assert adapter.calls == ["cancel_open_orders"]
    assert report.unmet == ()


def test_entries_mode_touches_the_broker_not_at_all(
    isolated_db_sandbox, account
) -> None:
    """'entries' is the mode that only refuses: no broker call is made at all."""
    set_live_kill_switch(account, "entries", actor="operator")
    adapter = _SweepAdapter()

    report = asyncio.run(
        enforce_live_kill_switch(
            {account: adapter}, [account], state=read_kill_switch_state()
        )
    )

    assert adapter.calls == []
    assert report.swept == ()
    assert report.unmet == ()


def test_no_halt_touches_nothing(isolated_db_sandbox, account) -> None:
    adapter = _SweepAdapter()

    report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account]))

    assert adapter.calls == []
    assert report.swept == ()
    assert report.unmet == ()
    assert report.without_adapter == ()


def test_a_halt_set_before_the_daemon_started_is_enforced(
    isolated_db_sandbox, account
) -> None:
    """The restart case: the halt was written while nothing was running."""
    set_live_kill_switch(account, "flatten", actor="operator")
    adapter = _SweepAdapter()

    # No injected state: the enforcement reads the database itself, exactly as
    # the daemon's first tick does after a restart.
    report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account]))

    assert adapter.calls == [
        "cancel_open_orders",
        "close_all_positions",
        "cancel_orphaned_protections",
    ]
    assert report.swept == (account,)


def test_a_cleared_halt_stops_the_enforcement(
    isolated_db_sandbox, account
) -> None:
    set_live_kill_switch(account, "flatten", actor="operator")
    clear_live_kill_switch(account, actor="operator")
    adapter = _SweepAdapter()

    report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account]))

    assert adapter.calls == []
    assert report.swept == ()


def test_a_halted_account_without_an_adapter_is_reported(
    isolated_db_sandbox, account, caplog
) -> None:
    """A halt that cannot be enforced must say so, loudly, every tick.

    The account is retained for adapter connection while it is halted (the sync
    loop connects it), so this is the state where that connect has not landed
    yet - or has failed. Silence here would read as "halted and flat".
    """
    set_live_kill_switch(account, "flatten", actor="operator")

    with caplog.at_level(logging.ERROR):
        report = asyncio.run(
            enforce_live_kill_switch({}, [account], state=read_kill_switch_state())
        )

    assert report.swept == ()
    assert report.without_adapter == (account,)
    assert "no connected adapter" in caplog.text


def test_an_adapter_that_cannot_flatten_is_reported_not_swallowed(
    isolated_db_sandbox, account, caplog
) -> None:
    """An unsupported flatten is an unmet halt, not a clean sweep.

    The adapter control plane raises NotImplementedError for a capability it
    does not have, and reading that as "nothing was open" is how a halted
    account keeps its exposure behind an accurate-looking report.
    """
    set_live_kill_switch(account, "flatten", actor="operator")

    class _NoFlatten(_SweepAdapter):
        supports_flatten = False

        async def close_all_positions(self) -> ControlOutcome:
            raise NotImplementedError("cannot flatten")

    adapter = _NoFlatten()

    with caplog.at_level(logging.ERROR):
        report = asyncio.run(
            enforce_live_kill_switch(
                {account: adapter}, [account], state=read_kill_switch_state()
            )
        )

    assert "cancel_open_orders" in adapter.calls
    assert report.unmet == (account,)
    assert report.swept == ()
    assert "cannot flatten" in caplog.text


def test_an_incomplete_flatten_is_reported_and_retried(
    isolated_db_sandbox, account, caplog
) -> None:
    """A partial sweep is never recorded as a completed one (#274's rule)."""
    set_live_kill_switch(account, "flatten", actor="operator")
    adapter = _SweepAdapter(
        flatten_outcome=ControlOutcome(completed=1, failed=("AAPL",))
    )

    with caplog.at_level(logging.ERROR):
        report = asyncio.run(
            enforce_live_kill_switch(
                {account: adapter}, [account], state=read_kill_switch_state()
            )
        )

    assert report.unmet == (account,)
    assert report.swept == ()
    assert "AAPL" in caplog.text


def test_a_pending_cancel_is_not_a_completed_sweep(isolated_db_sandbox, account) -> None:
    """A cancel the broker accepted but has not settled can still fill.

    ``ControlOutcome.complete`` ignores ``pending_cancel``, so reading it as a
    clean sweep would let an order fill after the account was reported flat. The
    STOPPED machinery holds its stamp for exactly the same reason.
    """
    set_live_kill_switch(account, "flatten", actor="operator")

    class _PendingCancel(_SweepAdapter):
        async def cancel_open_orders(self) -> ControlOutcome:
            self.calls.append("cancel_open_orders")
            return ControlOutcome(completed=1, pending_cancel=("order-9",))

    adapter = _PendingCancel()
    report = asyncio.run(
        enforce_live_kill_switch(
            {account: adapter}, [account], state=read_kill_switch_state()
        )
    )

    assert report.unmet == (account,)
    assert report.swept == ()
    # The flatten still runs: closing the positions is the money-safety action.
    assert "close_all_positions" in adapter.calls


def test_a_pending_cancel_warns_first_and_escalates_like_stopped(
    isolated_db_sandbox, account, caplog
) -> None:
    """A routine broker cancel must not log ERROR on every tick.

    ``pending_cancel`` normally settles within seconds, so the sweep borrows
    the STOPPED machinery's rule: WARNING while the passes are young, ERROR
    only once ``PENDING_CANCEL_ESCALATION_PASSES`` consecutive sweeps still see
    the ids outstanding.
    """
    from live_runner.kill_switch import PENDING_CANCEL_ESCALATION_PASSES

    set_live_kill_switch(account, "orders", actor="operator")

    class _PendingCancel(_SweepAdapter):
        async def cancel_open_orders(self) -> ControlOutcome:
            self.calls.append("cancel_open_orders")
            return ControlOutcome(completed=1, pending_cancel=("order-9",))

    adapter = _PendingCancel()
    state = read_kill_switch_state()

    with caplog.at_level(logging.WARNING):
        for _ in range(PENDING_CANCEL_ESCALATION_PASSES - 1):
            asyncio.run(
                enforce_live_kill_switch({account: adapter}, [account], state=state)
            )

    assert "pending_cancel" in caplog.text
    # The per-tick "halt not fully enforced" summary is ERROR by design for any
    # unmet sweep; what must not fire early is the pending-cancel escalation.
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "pending_cancel" in record.getMessage()
    ]

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            enforce_live_kill_switch({account: adapter}, [account], state=state)
        )

    errors = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "pending_cancel" in record.getMessage()
    ]
    assert errors, "a stuck pending_cancel never escalated to ERROR"


def test_a_clean_sweep_resets_the_pending_cancel_escalation(
    isolated_db_sandbox, account, caplog, monkeypatch
) -> None:
    """The pass count belongs to one unsettled episode, not to the account."""
    from live_runner.kill_switch import PENDING_CANCEL_ESCALATION_PASSES

    # A completed sweep is throttled for KILL_SWITCH_SWEEP_INTERVAL_SECONDS;
    # this test needs the very next tick to sweep again.
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_SWEEP_INTERVAL_SECONDS", 0.0)
    set_live_kill_switch(account, "orders", actor="operator")
    state = read_kill_switch_state()

    class _FlakyCancel(_SweepAdapter):
        pending = True

        async def cancel_open_orders(self) -> ControlOutcome:
            self.calls.append("cancel_open_orders")
            if self.pending:
                return ControlOutcome(completed=1, pending_cancel=("order-9",))
            return ControlOutcome(completed=1)

    adapter = _FlakyCancel()
    with caplog.at_level(logging.WARNING):
        for _ in range(PENDING_CANCEL_ESCALATION_PASSES):
            asyncio.run(
                enforce_live_kill_switch({account: adapter}, [account], state=state)
            )

    adapter.pending = False
    report = asyncio.run(
        enforce_live_kill_switch({account: adapter}, [account], state=state)
    )
    assert report.swept == (account,)

    # A later pending_cancel starts the count over: the first pass warns again
    # instead of resuming at the escalated ERROR.
    adapter.pending = True
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            enforce_live_kill_switch({account: adapter}, [account], state=state)
        )

    assert "pending_cancel" in caplog.text
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "pending_cancel" in record.getMessage()
    ]


def test_a_completed_sweep_is_not_repeated_before_the_interval(
    isolated_db_sandbox, account
) -> None:
    """The sweep is broker I/O; a flat account is not re-flattened every tick."""
    set_live_kill_switch(account, "flatten", actor="operator")
    adapter = _SweepAdapter()
    state = read_kill_switch_state()

    first = asyncio.run(enforce_live_kill_switch({account: adapter}, [account], state=state))
    second = asyncio.run(
        enforce_live_kill_switch({account: adapter}, [account], state=state)
    )

    assert first.swept == (account,)
    assert second.swept == ()
    assert adapter.calls.count("close_all_positions") == 1


def test_a_completed_sweep_is_repeated_once_the_interval_has_passed(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """The interval bounds how long a hand-placed broker order survives.

    Refusing entries stops the daemon opening risk, not a human at the broker, so
    a standing halt re-sweeps rather than declaring the account flat forever.
    """
    monkeypatch.setattr(kill_switch, "KILL_SWITCH_SWEEP_INTERVAL_SECONDS", 0.0)
    set_live_kill_switch(account, "flatten", actor="operator")
    adapter = _SweepAdapter()
    state = read_kill_switch_state()

    first = asyncio.run(enforce_live_kill_switch({account: adapter}, [account], state=state))
    second = asyncio.run(
        enforce_live_kill_switch({account: adapter}, [account], state=state)
    )

    assert first.swept == (account,)
    assert second.swept == (account,)
    assert adapter.calls.count("close_all_positions") == 2


def test_a_halt_that_needs_the_broker_retains_an_adapter_for_its_account(
    isolated_db_sandbox, account
) -> None:
    """A PAUSED account is otherwise never connected, so its halt is unenforceable.

    The same lesson STOPPED enforcement learned: cancel and flatten can only run
    through an adapter, and an account whose deployments are all PAUSED never gets
    one. Retention is not permission - only the runnable set gets listener tasks -
    so this cannot make an account tradeable.
    """
    import live_runner.daemon as daemon

    paused = _deployment(status="PAUSED")
    set_live_kill_switch(account, "flatten", actor="operator")

    retained = daemon._accounts_needing_halt_enforcement(
        read_kill_switch_state(), [paused]
    )

    assert account in retained


def test_an_entries_halt_needs_no_adapter(isolated_db_sandbox, account) -> None:
    """'entries' is enforced at the entry gate, so it connects nothing extra."""
    import live_runner.daemon as daemon

    paused = _deployment(status="PAUSED")
    set_live_kill_switch(account, "entries", actor="operator")

    retained = daemon._accounts_needing_halt_enforcement(
        read_kill_switch_state(), [paused]
    )

    assert retained == set()


def test_an_unreadable_halt_state_retains_nothing(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """Fail closed means refusing entries, not connecting every adapter."""
    import live_runner.daemon as daemon
    import live_runner.kill_switch as kill_switch

    paused = _deployment(status="PAUSED")

    def _unreadable() -> dict[str, str]:
        raise OSError("database is unavailable")

    monkeypatch.setattr(kill_switch, "read_live_kill_switch_scopes", _unreadable)

    retained = daemon._accounts_needing_halt_enforcement(
        read_kill_switch_state(), [paused]
    )

    assert retained == set()


def test_a_re_halt_after_a_clear_sweeps_at_once(isolated_db_sandbox, account) -> None:
    """A lifted halt leaves no sweep behind to inherit.

    Otherwise a clear followed by a re-halt of the same mode would wait out the
    interval of the halt that was just lifted - and the account is not known to
    be flat at that point, because it was allowed to trade in between. The clear
    has to be observed by a refresh for it to be seen as an episode, which is why
    this goes through one; a clear and a re-halt inside a single tick are
    indistinguishable at this level and wait out the interval, never longer.
    """
    set_live_kill_switch(account, "flatten", actor="operator")
    adapter = _SweepAdapter()
    state = read_kill_switch_state()
    asyncio.run(enforce_live_kill_switch({account: adapter}, [account], state=state))

    clear_live_kill_switch(account, actor="operator")
    state.refresh()
    set_live_kill_switch(account, "flatten", actor="operator")
    state.refresh()

    report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account], state=state))

    assert report.swept == (account,)
    assert adapter.calls.count("close_all_positions") == 2


def test_a_halt_on_a_registered_account_with_no_deployment_is_enforced(
    isolated_db_sandbox,
) -> None:
    """A registered account is the operator's account, with or without a row here.

    The halt is an instruction about the account, not about a deployment: an
    account the operator registered but has not deployed anything on can still
    hold a broker-side position, and a global halt has to reach it rather than
    only the accounts that happen to have a deployment row.
    """
    import live_runner.daemon as daemon
    from shared.database import ensure_live_account

    ensure_live_account("acc-idle", "alpaca", is_demo=True)
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator")

    retained = daemon._accounts_needing_halt_enforcement(
        read_kill_switch_state(), [], ["acc-idle"]
    )

    assert retained == {"acc-idle"}


def test_a_halt_set_while_the_entry_is_being_prepared_stops_the_submit(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """The gate runs again at the last moment before the broker sees anything.

    `prepare_order_with_risk` reads broker state, so an operator who halts while
    this entry is being sized must not have it land anyway - that is the window
    the halt is being used in. Nothing has been claimed yet, so the refusal costs
    no submission lease and the deployment is not locked out.
    """
    import live_runner.daemon as daemon
    from live_runner.kill_switch import KillSwitchMode, LiveHaltError
    from risk import TradeSignal

    deployment = _deployment()
    adapter = _EntryAdapter()
    claims: list[str] = []
    real_refuse = daemon.refuse_entry_while_halted
    reads = {"count": 0}

    async def _refuse(*args: Any, **kwargs: Any) -> None:
        reads["count"] += 1
        if reads["count"] > 1:
            raise LiveHaltError(
                "halted", account_id=account, mode=KillSwitchMode.FLATTEN
            )
        return await real_refuse(*args, **kwargs)

    monkeypatch.setattr(daemon, "refuse_entry_while_halted", _refuse)
    monkeypatch.setattr(
        daemon,
        "claim_live_deployment_submission",
        lambda *a, **k: claims.append("claimed") or "token",
        raising=False,
    )

    asyncio.run(
        daemon._prepare_claim_and_submit_signal(
            account,
            adapter,
            deployment,
            TradeSignal(
                action="buy",
                symbol="AAPL",
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=110.0,
                timeframe="1h",
            ),
            _entry_signal(deployment),
            is_exit=False,
        )
    )

    assert adapter.submitted == []
    assert claims == []


def _run_submit_path(
    account: str,
    adapter: _EntryAdapter,
    deployment: LiveDeploymentConfig,
    live_signal: LiveSignal,
    *,
    is_exit: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Run one signal through ``_prepare_claim_and_submit_signal``.

    Returns the lease events in order, so a test can tell "the halt gate
    refused before the claim" from "the order reached the broker".
    """
    import live_runner.daemon as daemon
    from risk import TradeSignal

    events: list[str] = []
    monkeypatch.setattr(
        daemon,
        "claim_live_deployment_submission",
        lambda *a, **k: events.append("claimed") or "token",
        raising=False,
    )
    monkeypatch.setattr(
        daemon,
        "release_live_deployment_submission",
        lambda *a, **k: events.append("released") or True,
        raising=False,
    )
    asyncio.run(
        daemon._prepare_claim_and_submit_signal(
            account,
            adapter,
            deployment,
            TradeSignal(
                action=live_signal.action,
                symbol=live_signal.symbol,
                entry_price=float(live_signal.theoretical_price),
                stop_loss=95.0,
                take_profit=110.0,
                timeframe="1h",
            ),
            live_signal,
            is_exit=is_exit,
        )
    )
    return events


@pytest.mark.parametrize("mode", [mode.value for mode in KillSwitchMode])
def test_a_halt_never_blocks_an_exit_on_the_submit_path(
    isolated_db_sandbox, account, monkeypatch, mode: str
) -> None:
    """The last-moment gate is entry-only: an exit must still reach the broker.

    ``prepare_order_with_risk`` routes exits away before its own halt gate, but
    the submission path runs a second gate after preparation. That gate must
    skip exits too, or an operator who sets even the harmless ``entries`` mode -
    or hits the fail-closed unreadable-state case - is locked into every open
    position, which is the opposite of what a halt is for.
    """
    deployment = _deployment()
    adapter = _EntryAdapter(positions=[{"symbol": "AAPL", "qty": 5.0}])
    _patch_exit_ledger(monkeypatch, deployment, 5.0)
    set_live_kill_switch(account, mode, actor="operator")

    events = _run_submit_path(
        account, adapter, deployment, _exit_signal(deployment),
        is_exit=True, monkeypatch=monkeypatch,
    )

    assert events == ["claimed", "released"]
    assert len(adapter.submitted) == 1
    assert adapter.submitted[0][1] == 5.0


def test_an_unreadable_halt_state_never_blocks_an_exit_on_the_submit_path(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """Fail closed refuses entries; it must not trap open positions either."""
    import live_runner.kill_switch as kill_switch

    deployment = _deployment()
    adapter = _EntryAdapter(positions=[{"symbol": "AAPL", "qty": 5.0}])
    _patch_exit_ledger(monkeypatch, deployment, 5.0)

    def _unreadable() -> dict[str, str]:
        raise OSError("database is unavailable")

    monkeypatch.setattr(kill_switch, "read_live_kill_switch_scopes", _unreadable)

    events = _run_submit_path(
        account, adapter, deployment, _exit_signal(deployment),
        is_exit=True, monkeypatch=monkeypatch,
    )

    assert events == ["claimed", "released"]
    assert len(adapter.submitted) == 1


@pytest.mark.parametrize("mode", [mode.value for mode in KillSwitchMode])
def test_an_entry_is_refused_on_the_submit_path_while_halted(
    isolated_db_sandbox, account, monkeypatch, mode: str
) -> None:
    """The counterpart: every halt mode still refuses the entry end to end."""
    deployment = _deployment()
    adapter = _EntryAdapter()
    set_live_kill_switch(account, mode, actor="operator")

    events = _run_submit_path(
        account, adapter, deployment, _entry_signal(deployment),
        is_exit=False, monkeypatch=monkeypatch,
    )

    assert adapter.submitted == []
    assert events == []


def test_an_unreadable_halt_state_refuses_the_entry_on_the_submit_path(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    import live_runner.kill_switch as kill_switch

    deployment = _deployment()
    adapter = _EntryAdapter()

    def _unreadable() -> dict[str, str]:
        raise OSError("database is unavailable")

    monkeypatch.setattr(kill_switch, "read_live_kill_switch_scopes", _unreadable)

    events = _run_submit_path(
        account, adapter, deployment, _entry_signal(deployment),
        is_exit=False, monkeypatch=monkeypatch,
    )

    assert adapter.submitted == []
    assert events == []


def test_a_halt_written_during_the_claim_still_stops_the_entry(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """The claim awaited the database, so the halt is read once more after it.

    A halt written in that window is invisible to the pre-claim gate; the
    post-claim read is the last checkpoint before ``adapter.submit_order``. The
    refusal releases the lease it just took - the order never reached the
    broker, so nothing is in flight.
    """
    import live_runner.daemon as daemon
    from risk import TradeSignal

    deployment = _deployment()
    adapter = _EntryAdapter()
    events: list[str] = []

    def _claim(*_args: Any, **_kwargs: Any) -> str:
        # The halt lands while the claim is in flight - after every gate that
        # ran before it.
        set_live_kill_switch(account, "entries", actor="operator")
        events.append("claimed")
        return "token"

    monkeypatch.setattr(
        daemon, "claim_live_deployment_submission", _claim, raising=False
    )
    monkeypatch.setattr(
        daemon,
        "release_live_deployment_submission",
        lambda *a, **k: events.append("released") or True,
        raising=False,
    )

    asyncio.run(
        daemon._prepare_claim_and_submit_signal(
            account,
            adapter,
            deployment,
            TradeSignal(
                action="buy",
                symbol="AAPL",
                entry_price=100.0,
                stop_loss=95.0,
                take_profit=110.0,
                timeframe="1h",
            ),
            _entry_signal(deployment),
            is_exit=False,
        )
    )

    assert adapter.submitted == []
    assert events == ["claimed", "released"]


def test_submit_order_with_risk_rechecks_the_halt_before_submitting(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """The lease-less path gets the same last-moment gate as the leased one.

    ``live_smoke`` submits through ``submit_order_with_risk``, which has no
    claim to protect - but a halt written while the order was being sized must
    still stop it. The first read (inside ``prepare_order_with_risk``) passes;
    the recheck refuses.
    """
    import live_runner.daemon as daemon
    from live_runner.kill_switch import KillSwitchMode, LiveHaltError

    deployment = _deployment()
    adapter = _EntryAdapter()
    real_refuse = daemon.refuse_entry_while_halted
    reads = {"count": 0}

    async def _refuse(*args: Any, **kwargs: Any) -> None:
        reads["count"] += 1
        if reads["count"] > 1:
            raise LiveHaltError(
                "halted", account_id=account, mode=KillSwitchMode.ENTRIES
            )
        return await real_refuse(*args, **kwargs)

    monkeypatch.setattr(daemon, "refuse_entry_while_halted", _refuse)

    with pytest.raises(LiveHaltError):
        _submit_entry(deployment, adapter)

    assert adapter.submitted == []


def test_submit_order_with_risk_never_blocks_an_exit(
    isolated_db_sandbox, account, monkeypatch
) -> None:
    """The recheck is entry-only here too: closing is never halted."""
    import live_runner.daemon as daemon

    deployment = _deployment()
    adapter = _EntryAdapter(positions=[{"symbol": "AAPL", "qty": 5.0}])
    _patch_exit_ledger(monkeypatch, deployment, 5.0)
    set_live_kill_switch(account, "flatten", actor="operator")

    asyncio.run(
        daemon.submit_order_with_risk(
            adapter, deployment, _exit_signal(deployment)
        )
    )

    assert len(adapter.submitted) == 1
    assert adapter.submitted[0][1] == 5.0


def test_a_halt_written_while_the_daemon_is_running_is_enforced(
    isolated_db_sandbox, account
) -> None:
    """The state the daemon holds is refreshed, never cached across ticks.

    This is the production shape: one state object, refreshed in place, so the
    sweep ledger survives a tick while the halts do not. A halt written between
    two ticks - or by another process, which is every CLI and viewer write - has
    to be enforced by the next one.
    """
    adapter = _SweepAdapter()
    state = read_kill_switch_state()

    set_live_kill_switch(account, "flatten", actor="operator")
    state.refresh()

    report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account], state=state))

    assert report.swept == (account,)
    assert "close_all_positions" in adapter.calls


def test_an_escalated_mode_re_sweeps_a_state_that_is_refreshed_in_place(
    isolated_db_sandbox, account
) -> None:
    """Escalating entries -> flatten must not wait out the sweep interval.

    The ledger is keyed on the mode as well as the account, so the escalation
    sweeps at once - and the re-read state sees the escalated mode at all,
    because the daemon refreshes rather than caching.
    """
    set_live_kill_switch(account, "orders", actor="operator")
    adapter = _SweepAdapter()
    state = read_kill_switch_state()
    asyncio.run(enforce_live_kill_switch({account: adapter}, [account], state=state))
    calls_before = list(adapter.calls)

    set_live_kill_switch(account, "flatten", actor="operator")
    state.refresh()

    report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account], state=state))

    assert adapter.calls[: len(calls_before)] == calls_before
    assert "close_all_positions" in adapter.calls
    assert report.swept == (account,)


def test_an_escalated_mode_re_sweeps_immediately(
    isolated_db_sandbox, account
) -> None:
    """Escalating entries -> flatten must not wait out the sweep interval."""
    set_live_kill_switch(account, "orders", actor="operator")
    adapter = _SweepAdapter()
    asyncio.run(
        enforce_live_kill_switch(
            {account: adapter}, [account], state=read_kill_switch_state()
        )
    )
    calls_before = list(adapter.calls)

    set_live_kill_switch(account, "flatten", actor="operator")

    report = asyncio.run(
        enforce_live_kill_switch(
            {account: adapter}, [account], state=read_kill_switch_state()
        )
    )

    assert adapter.calls[: len(calls_before)] == calls_before
    assert "close_all_positions" in adapter.calls
    assert report.swept == (account,)


def test_an_unreadable_state_never_sweeps_the_broker(
    isolated_db_sandbox, account, monkeypatch, caplog
) -> None:
    """Unreadable means entries are refused; it does not license a flatten.

    Liquidating an account on a database error would be a real money event the
    operator never asked for. The halt is enforced as soon as the read recovers,
    because the state it cannot read is still persisted.
    """
    import live_runner.kill_switch as kill_switch

    def _unreadable() -> dict[str, str]:
        raise OSError("database is unavailable")

    monkeypatch.setattr(kill_switch, "read_live_kill_switch_scopes", _unreadable)
    adapter = _SweepAdapter()

    with caplog.at_level(logging.ERROR):
        report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account]))

    assert adapter.calls == []
    assert report.swept == ()
    assert report.unmet == ()
    assert "database is unavailable" in caplog.text


def test_a_mode_that_cannot_be_parsed_halts_entries_and_sweeps_nothing(
    isolated_db_sandbox, account
) -> None:
    """A corrupt row: fail closed on entries, never guess a destructive mode."""
    import shared.database as database

    with database.get_session() as session:
        session.add(
            database.LiveKillSwitchModel(scope=account, mode="liquidate-everything")
        )
        session.commit()

    adapter = _SweepAdapter()
    report = asyncio.run(enforce_live_kill_switch({account: adapter}, [account]))

    assert adapter.calls == []
    assert report.swept == ()
    assert report.unmet == ()

    with pytest.raises(LiveHaltError):
        asyncio.run(refuse_entry_while_halted(account))
