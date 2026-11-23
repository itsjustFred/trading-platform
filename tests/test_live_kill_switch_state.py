"""Persistence of the live kill switch (#300).

Throwaway SQLite sandboxes only; nothing here touches the production database.

The halt is an operator instruction, so the properties under test are the ones a
restart, a second process or a typo would break: it survives all three, its
scopes are independent, and a malformed mode is refused on the way in rather
than interpreted on the way out.
"""

from __future__ import annotations

import pytest

from shared.database import (
    KILL_SWITCH_GLOBAL_SCOPE,
    LiveAccountModel,
    clear_live_kill_switch,
    ensure_live_account,
    get_live_kill_switch_state,
    get_session,
    read_live_kill_switch_scopes,
    set_live_kill_switch,
)


@pytest.fixture
def account(isolated_db_sandbox) -> str:
    """One known live account: an account-scoped halt must name a real one."""
    database, _engine, _data_dir = isolated_db_sandbox
    database.init_db()
    with get_session() as session:
        session.add(LiveAccountModel(id="acc-1", broker="alpaca", name="acc-1"))
        session.commit()
    return "acc-1"



def test_a_halt_is_persisted_for_a_fresh_reader(isolated_db_sandbox) -> None:
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "flatten"}


def test_a_halt_survives_a_restart(isolated_db_sandbox) -> None:
    """Nothing about the halt may live in this process's memory.

    The engine is disposed so every pooled connection is closed: a flag cached
    in a session, a module global or a connection-local state would survive the
    first read and nothing else. The CLI tests cover the same property across
    real processes, which is the shape an operator and the daemon are in.
    """
    _database, engine, _data_dir = isolated_db_sandbox

    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "orders", actor="operator")
    engine.dispose()

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "orders"}


def test_clearing_a_halt_takes_it_out_of_the_active_set(
    isolated_db_sandbox, account: str
) -> None:
    set_live_kill_switch(account, "orders", actor="operator")

    assert clear_live_kill_switch(account, actor="operator") is True
    assert read_live_kill_switch_scopes() == {}
    # Clearing twice is a no-op, not a second clear that rewrites history.
    assert clear_live_kill_switch(account, actor="operator") is False


def test_clearing_leaves_the_history_of_the_halt_readable(
    isolated_db_sandbox, account: str
) -> None:
    """Who halted trading, when, and who lifted it stays answerable."""
    set_live_kill_switch(account, "flatten", actor="operator-a", reason="incident 42")
    clear_live_kill_switch(account, actor="operator-b")

    state = get_live_kill_switch_state(account)
    assert state is not None
    assert state.active is False
    assert state.mode == "flatten"
    assert state.halted_by == "operator-a"
    assert state.reason == "incident 42"
    assert state.cleared_by == "operator-b"
    assert state.cleared_at is not None


def test_global_and_account_scopes_are_independent(
    isolated_db_sandbox, account: str
) -> None:
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "entries", actor="operator")
    set_live_kill_switch(account, "flatten", actor="operator")

    assert read_live_kill_switch_scopes() == {
        KILL_SWITCH_GLOBAL_SCOPE: "entries",
        account: "flatten",
    }

    clear_live_kill_switch(account, actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "entries"}


def test_escalating_the_mode_keeps_the_original_halt_instant(
    isolated_db_sandbox,
) -> None:
    """Escalating entries -> flatten does not restart the halt's clock.

    The instant an account has been halted is the number an incident review
    asks for; overwriting it with the escalation time would claim the account
    kept trading (under the weaker mode) until the escalation.
    """
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "entries", actor="operator")
    first = get_live_kill_switch_state(KILL_SWITCH_GLOBAL_SCOPE)
    assert first is not None

    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator")
    second = get_live_kill_switch_state(KILL_SWITCH_GLOBAL_SCOPE)

    assert second is not None
    assert second.mode == "flatten"
    assert second.halted_at == first.halted_at


def test_re_arming_a_cleared_scope_records_a_new_halt(
    isolated_db_sandbox, account: str
) -> None:
    set_live_kill_switch(account, "entries", actor="operator")
    clear_live_kill_switch(account, actor="operator")

    set_live_kill_switch(account, "orders", actor="operator")
    state = get_live_kill_switch_state(account)

    assert state is not None
    assert state.active is True
    assert state.cleared_at is None
    assert state.cleared_by is None


def test_re_arming_a_cleared_scope_stamps_a_new_halt_instant(
    isolated_db_sandbox, account: str
) -> None:
    """A re-armed halt is a new episode, not a continuation of the cleared one.

    There is one row per scope, so it has nowhere else to keep the earlier
    episode's instant: reporting that one would claim the account had been halted
    continuously since then, which is exactly what the clear denied - and the
    incident timeline would show a halt standing over a window in which trading
    was allowed.
    """
    set_live_kill_switch(account, "entries", actor="operator")
    first = get_live_kill_switch_state(account)
    assert first is not None
    clear_live_kill_switch(account, actor="operator")

    set_live_kill_switch(account, "entries", actor="operator")
    second = get_live_kill_switch_state(account)

    assert second is not None
    assert second.halted_at is not None
    assert first.halted_at is not None
    assert second.halted_at > first.halted_at


def test_an_unknown_scope_reads_as_no_halt(isolated_db_sandbox) -> None:
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "entries", actor="operator")

    assert get_live_kill_switch_state("acc-never-halted") is None
    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "entries"}


@pytest.mark.parametrize("mode", ["", "  ", "halt", "STOP", "flat", None])
def test_an_unrecognised_mode_is_refused_on_the_way_in(
    isolated_db_sandbox, mode: object
) -> None:
    """A mode the daemon cannot interpret never reaches the table.

    Stored and interpreted later, a misspelling would either halt nothing (and
    say so) or, worse, be read as the strictest mode. Refusing the write is the
    only answer that cannot be wrong in the dangerous direction.
    """
    with pytest.raises(ValueError, match="kill switch mode"):
        set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, mode, actor="operator")

    assert read_live_kill_switch_scopes() == {}


def test_a_mode_is_normalised_before_it_is_stored(isolated_db_sandbox) -> None:
    """Padding and case are an operator typing, not a different mode."""
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "  entries  ", actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "entries"}

    # Escalating, because weakening a standing halt is refused: a re-write may
    # only add restriction (see the test below).
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "FLATTEN\t", actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "flatten"}


def test_a_standing_halt_cannot_be_weakened_by_a_re_write(
    isolated_db_sandbox,
) -> None:
    """A step-down must not quietly stop the sweeps behind a halt that still reads armed.

    The row would still report a halt while the mode in force is the one that
    touches nothing, which is the one direction a kill switch can lose money in.
    Lifting is a separate, attributed action; weakening is not a quiet edit.
    """
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator")

    with pytest.raises(ValueError, match="weaken"):
        set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "entries", actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "flatten"}


def test_two_racing_halt_writes_cannot_downgrade_each_other(
    isolated_db_sandbox, account: str
) -> None:
    """A CLI ``flatten`` racing a viewer ``orders`` must not lose to it.

    Both writes are legal escalations of a standing ``entries`` halt, so both
    pass the weaken check if they read the same snapshot - and then the last
    commit wins, silently stepping the halt down. The write is serialized
    (BEGIN IMMEDIATE), so the second writer reads the first one's committed
    mode and is refused as a weakening.

    Pinned with a third connection holding the write lock, like
    ``test_a_resume_waits_for_the_write_lock_before_it_reads_the_row``: while
    it is held the row is escalated to ``flatten`` and the lock released. An
    orders write that read the row before the flip (no BEGIN IMMEDIATE) sees
    ``entries``, judges the write legal, and its commit lands after the
    release - silently stepping the halt down to ``orders``. One that waited
    on the lock reads ``flatten`` and is refused as a weakening, so the scope
    stays ``flatten`` either way the scheduler interleaves the threads.
    """
    import sqlite3
    import threading
    import time

    database, _engine, _data_dir = isolated_db_sandbox

    set_live_kill_switch(account, "entries", actor="operator")

    blocker = sqlite3.connect(str(database.DB_PATH), timeout=30)
    outcomes: dict[str, object] = {}
    try:
        blocker.execute("BEGIN IMMEDIATE")

        def _write() -> None:
            try:
                outcomes["orders"] = set_live_kill_switch(
                    account, "orders", actor="operator-orders"
                )
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                outcomes["orders"] = exc

        orders_thread = threading.Thread(target=_write, name="orders-writer")
        orders_thread.start()
        # Long enough for an unserialized write to read the row and queue on
        # the commit; the locked write is still waiting to begin.
        time.sleep(1.0)

        # Escalate the standing halt to flatten and release: what the orders
        # write observes next is this committed mode.
        blocker.execute(
            "UPDATE live_kill_switch SET mode = 'flatten' WHERE scope = ?",
            (account,),
        )
        blocker.commit()

        orders_thread.join(timeout=30)
    finally:
        blocker.close()

    assert not orders_thread.is_alive()
    assert isinstance(outcomes["orders"], ValueError)
    assert read_live_kill_switch_scopes() == {account: "flatten"}


def test_a_resume_racing_a_re_halt_cannot_interleave_into_a_lost_update(
    isolated_db_sandbox, account: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clear_live_kill_switch`` holds the same write lock as the halt write.

    A resume that read the row before a re-halt committed must not land its
    ``cleared_at`` over the new episode: without BEGIN IMMEDIATE the clear's
    stale snapshot can commit last and silently lift a halt that was just
    re-armed. Serialized, the re-halt waits for the clear and then re-arms the
    scope as a new episode - the halt the operator asked for is the halt that
    stands.

    The interleaving is pinned with events, like the halt-vs-halt race above:
    the clear writer signals from inside the write lock (``_utcnow_naive``
    runs after ``BEGIN IMMEDIATE`` and the row read), the re-halt starts only
    then and blocks on the lock, and the clear releases once the re-halt is
    queued.
    """
    import threading

    import shared.database as database

    set_live_kill_switch(account, "entries", actor="operator")

    real_utcnow = database._utcnow_naive
    clear_holds_lock = threading.Event()
    halt_queued = threading.Event()

    def _slow_utcnow() -> object:
        # Only the clear writer rendezvous here: its _utcnow_naive call is the
        # one inside BEGIN IMMEDIATE after the row read. The re-halt's own
        # call happens once it holds the lock and must not wait.
        if threading.current_thread().name == "clear-writer":
            clear_holds_lock.set()
            halt_queued.wait(timeout=30)
        return real_utcnow()

    monkeypatch.setattr(database, "_utcnow_naive", _slow_utcnow)

    outcomes: dict[str, object] = {}

    def _clear() -> None:
        try:
            outcomes["clear"] = clear_live_kill_switch(
                account, actor="operator-resume"
            )
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcomes["clear"] = exc

    def _halt() -> None:
        # Start only once the clear writer provably holds the write lock;
        # this call then blocks on BEGIN IMMEDIATE until the clear commits.
        clear_holds_lock.wait(timeout=30)
        halt_queued.set()
        try:
            outcomes["halt"] = set_live_kill_switch(
                account, "flatten", actor="operator-halt"
            )
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            outcomes["halt"] = exc

    clear_thread = threading.Thread(target=_clear, name="clear-writer")
    halt_thread = threading.Thread(target=_halt, name="halt-writer")
    clear_thread.start()
    halt_thread.start()
    clear_thread.join(timeout=30)
    halt_thread.join(timeout=30)

    assert not clear_thread.is_alive() and not halt_thread.is_alive()
    assert outcomes["clear"] is True
    assert outcomes["halt"].mode == "flatten"  # type: ignore[union-attr]
    # The re-halt re-armed the scope after the clear committed: the halt the
    # operator asked for is the halt that stands, not one the resume erased.
    assert read_live_kill_switch_scopes() == {account: "flatten"}


def test_a_resume_waits_for_the_write_lock_before_it_reads_the_row(
    isolated_db_sandbox, account: str
) -> None:
    """``clear_live_kill_switch`` takes BEGIN IMMEDIATE like the halt write.

    A resume whose row read ran *before* a concurrent write committed would
    act on a stale snapshot and could stamp ``cleared_at`` over a halt that
    was re-armed in between - the resume erasing a halt the operator asked
    for. BEGIN IMMEDIATE makes the read itself wait for the writer, so the
    clear always observes the last committed state.

    Pinned with a third connection holding the write lock: while it is held
    the row is flipped to cleared and the lock released. A clear that read
    the row before the flip (no BEGIN IMMEDIATE) reports ``True`` and stamps
    its own ``cleared_by`` over the committed clear; one that waited reports
    ``False`` - there was nothing left to clear.
    """
    import sqlite3
    import threading
    import time

    database, _engine, _data_dir = isolated_db_sandbox

    set_live_kill_switch(account, "entries", actor="operator")

    blocker = sqlite3.connect(str(database.DB_PATH), timeout=30)
    outcomes: dict[str, object] = {}
    try:
        blocker.execute("BEGIN IMMEDIATE")

        def _clear() -> None:
            try:
                outcomes["clear"] = clear_live_kill_switch(
                    account, actor="operator-resume"
                )
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion
                outcomes["clear"] = exc

        clear_thread = threading.Thread(target=_clear, name="clear-writer")
        clear_thread.start()
        # Long enough for an unserialized clear to finish its row read and
        # queue on the commit; the locked clear is still waiting to begin.
        time.sleep(1.0)

        # Commit a clear of our own while the lock is held, then release:
        # what the resume observes next is this committed state.
        blocker.execute(
            "UPDATE live_kill_switch SET cleared_at = ?, cleared_by = 'raw-sql' "
            "WHERE scope = ?",
            (database._utcnow_naive().isoformat(sep=" "), account),
        )
        blocker.commit()

        clear_thread.join(timeout=30)
    finally:
        blocker.close()

    assert not clear_thread.is_alive()
    assert outcomes["clear"] is False
    assert read_live_kill_switch_scopes() == {}


def test_an_escalation_is_not_attributed_to_whoever_escalated(
    isolated_db_sandbox,
) -> None:
    """The original halt's actor survives an escalation.

    Otherwise the record attributes the whole halt - a continuous one - to the
    last person who touched it, and "who halted this account" gets the wrong
    answer exactly when it matters. The escalation is attributed in
    ``live_control_audit``, which records the actor of every write.
    """
    set_live_kill_switch(
        KILL_SWITCH_GLOBAL_SCOPE, "entries", actor="operator-a", reason="incident 42"
    )

    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator-b")
    state = get_live_kill_switch_state(KILL_SWITCH_GLOBAL_SCOPE)

    assert state is not None
    assert state.mode == "flatten"
    assert state.halted_by == "operator-a"
    assert state.reason == "incident 42"


def test_the_reserved_global_scope_cannot_be_created_as_an_account(
    isolated_db_sandbox,
) -> None:
    """The two scopes share one column, so the collision must never exist."""
    with pytest.raises(ValueError, match="reserved kill switch scope"):
        ensure_live_account(KILL_SWITCH_GLOBAL_SCOPE, "alpaca")

    assert read_live_kill_switch_scopes() == {}


def test_a_halt_for_an_unknown_account_is_refused(isolated_db_sandbox) -> None:
    """A typo'd account id would look armed while halting nothing."""
    with pytest.raises(ValueError, match="unknown live account"):
        set_live_kill_switch("acc-typo", "flatten", actor="operator")

    assert read_live_kill_switch_scopes() == {}


def test_the_reserved_global_scope_cannot_be_used_as_an_account_scope(
    isolated_db_sandbox,
) -> None:
    """An account literally named 'global' would collide with the global halt."""
    with pytest.raises(ValueError, match="reserved"):
        set_live_kill_switch(" Global ", "flatten", actor="operator")

    assert read_live_kill_switch_scopes() == {}


def test_a_halt_for_an_unknown_account_is_allowed_when_accounts_are_unreadable(
    isolated_db_sandbox, monkeypatch: pytest.MonkeyPatch, account: str
) -> None:
    """Never block an operator's halt on a read failure.

    The typo check above refuses only what it can *prove* is wrong. A database
    that cannot answer "does this account exist" proves nothing, and refusing
    the halt would make a broken read the reason trading kept running.
    """
    _database, _engine, _data_dir = isolated_db_sandbox

    def _unreadable() -> list:
        raise OSError("database is unavailable")

    monkeypatch.setattr("shared.database.list_live_accounts", _unreadable)

    # 'acc-unverified' names no account either, but nothing can prove that now.
    set_live_kill_switch("acc-unverified", "flatten", actor="operator")

    assert read_live_kill_switch_scopes() == {"acc-unverified": "flatten"}
