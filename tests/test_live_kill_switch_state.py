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
        session.add(LiveAccountModel(id="acc-1", broker="fixture", name="acc-1"))
        session.commit()
    return "acc-1"



def test_a_halt_is_persisted_for_a_fresh_reader(isolated_db_sandbox) -> None:
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "flatten", actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "flatten"}


def test_a_halt_survives_a_restart(isolated_db_sandbox) -> None:
    """Nothing about the halt may live in this process's memory.

    The engine is disposed so every pooled connection is closed: a flag cached
    in a session, a module global or a connection-local state would survive the
    first read and nothing else. ``scripts/live_smoke.py`` and the CLI cover the
    same property across real processes.
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
    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "  FLATTEN  ", actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "flatten"}

    set_live_kill_switch(KILL_SWITCH_GLOBAL_SCOPE, "entries\t", actor="operator")

    assert read_live_kill_switch_scopes() == {KILL_SWITCH_GLOBAL_SCOPE: "entries"}


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
