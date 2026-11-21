"""The viewer's kill-switch endpoints (#300).

Throwaway SQLite sandboxes only; nothing here touches the production database.

The properties under test are the ones that make a dashboard button safe to ship:
a halt is one click (an operator in an incident must not fight a dialog), the modes
that move money are confirmed on the server rather than trusted to the UI, a
refusal never writes a halt that reads as armed, and every attempt - refused ones
included - is attributed in the control audit trail.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import viewer.main as viewer_main_module
from shared.database import (
    list_live_control_audit,
    read_live_kill_switch_scopes,
)

ACCOUNT = "acc-api"
HALT_PATH = "/api/live/kill-switch/halt"
RESUME_PATH = "/api/live/kill-switch/resume"


@pytest.fixture
def client(isolated_db_sandbox):
    database, _engine, _data_dir = isolated_db_sandbox
    database.init_db()
    database.ensure_live_account(ACCOUNT, "alpaca", is_demo=True)
    with TestClient(viewer_main_module.app) as test_client:
        yield test_client


def _halt(client: TestClient, **payload: object) -> object:
    return client.post(HALT_PATH, json=payload, headers={"X-Operator": "jane"})


def test_nothing_halted_reads_as_nothing_halted(client) -> None:
    response = client.get("/api/live/kill-switch")

    assert response.status_code == 200
    payload = response.json()
    assert payload["readable"] is True
    assert payload["halts"] == []
    assert payload["global_halted"] is False
    assert payload["halt_details"] == []


def test_a_halt_is_set_from_the_dashboard_and_read_back(client) -> None:
    response = _halt(
        client, scope=ACCOUNT, mode="orders", reason="incident 42", confirmed_scope=ACCOUNT
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["halted"] is True
    assert payload["scope"] == ACCOUNT
    assert payload["mode"] == "orders"
    assert payload["reason"] == "incident 42"
    # The returned state is what the daemon enforces, not an echo of the write.
    assert payload["state"]["halts"] == [{"scope": ACCOUNT, "mode": "orders"}]
    assert payload["state"]["halted_accounts"] == [ACCOUNT]
    detail = payload["state"]["halt_details"]
    assert len(detail) == 1
    assert detail[0]["scope"] == ACCOUNT
    # The detail's mode is the one the state captured, so the payload's two views
    # cannot describe two different observations of an escalation.
    assert detail[0]["mode"] == "orders"
    assert detail[0]["history_available"] is True
    assert detail[0]["halted_by"]


def test_a_money_moving_mode_is_refused_without_a_confirmation(client) -> None:
    """'entries' is one click; 'flatten' closes positions, so it is confirmed.

    The confirmation lives here, on the server, because the API is directly
    callable by a script or a stale tab - a dialog in the UI constrains nothing
    but the UI's own politeness.
    """
    response = _halt(client, scope=ACCOUNT, mode="flatten")

    assert response.status_code == 428
    assert read_live_kill_switch_scopes() == {}


def test_a_confirmation_naming_a_different_scope_is_refused(client) -> None:
    response = _halt(client, scope=ACCOUNT, mode="flatten", confirmed_scope="global")

    assert response.status_code == 409
    assert read_live_kill_switch_scopes() == {}


def test_a_confirmed_flatten_halt_is_set(client) -> None:
    response = _halt(client, scope=ACCOUNT, mode="flatten", confirmed_scope=ACCOUNT)

    assert response.status_code == 200
    assert read_live_kill_switch_scopes() == {ACCOUNT: "flatten"}


def test_a_scope_that_names_no_account_is_refused_and_writes_nothing(client) -> None:
    """A halt that covers nothing while the dashboard reads it as armed."""
    response = _halt(client, scope="acc-typo", mode="entries")

    assert response.status_code == 400
    assert read_live_kill_switch_scopes() == {}


def test_an_unknown_mode_is_refused_and_writes_nothing(client) -> None:
    response = _halt(client, scope=ACCOUNT, mode="liquidate-everything")

    assert response.status_code == 400
    assert read_live_kill_switch_scopes() == {}


def test_a_halt_and_its_refusal_are_both_audited(client) -> None:
    _halt(client, scope=ACCOUNT, mode="entries")
    _halt(client, scope="acc-typo", mode="entries")

    entries = list_live_control_audit()
    halts = [entry for entry in entries if entry["action"] == "kill_switch.halt"]
    written = [entry for entry in halts if entry["succeeded"] is True]
    refusals = [entry for entry in halts if entry["succeeded"] is False]

    assert written, "the halt was not written to live_control_audit"
    assert written[0]["actor"] == "jane"
    assert written[0]["target_type"] == "live_kill_switch"
    assert written[0]["target_id"] == ACCOUNT
    assert written[0]["detail"]["mode"] == "entries"
    assert refusals, "a refused halt was not recorded as an attempt"
    assert refusals[-1]["target_id"] == "acc-typo"


def test_resuming_a_halt_is_confirmed_before_trading_may_resume(client) -> None:
    _halt(client, scope=ACCOUNT, mode="entries")

    response = client.post(RESUME_PATH, json={"scope": ACCOUNT})

    assert response.status_code == 428
    assert read_live_kill_switch_scopes() == {ACCOUNT: "entries"}


def test_a_confirmed_resume_lifts_the_halt(client) -> None:
    _halt(client, scope=ACCOUNT, mode="entries")

    response = client.post(
        RESUME_PATH,
        json={"scope": ACCOUNT, "confirmed_scope": ACCOUNT},
        headers={"X-Operator": "jane"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["lifted"] is True
    assert payload["halted"] is False
    assert read_live_kill_switch_scopes() == {}
    resumes = [
        entry
        for entry in list_live_control_audit()
        if entry["action"] == "kill_switch.resume"
    ]
    assert resumes[-1]["succeeded"] is True
    assert resumes[-1]["actor"] == "jane"


def test_resuming_a_scope_that_is_not_halted_is_idempotent(client) -> None:
    response = client.post(
        RESUME_PATH, json={"scope": ACCOUNT, "confirmed_scope": ACCOUNT}
    )

    assert response.status_code == 200
    assert response.json()["lifted"] is False


def test_resuming_a_malformed_scope_is_a_refusal_not_a_crash(client) -> None:
    """A scope the write path refuses must not become a 500.

    ``clear_live_kill_switch`` raises for an empty or mis-cased global scope; that
    is a client error with its own audited refusal, exactly like a halt on a
    scope that names no account.
    """
    response = client.post(
        RESUME_PATH, json={"scope": "", "confirmed_scope": ""}
    )

    assert response.status_code == 400
    refusals = [
        entry
        for entry in list_live_control_audit()
        if entry["action"] == "kill_switch.resume" and entry["succeeded"] is False
    ]
    assert refusals, "a refused resume was not recorded as an attempt"
