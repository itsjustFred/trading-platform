"""Broker order id conflict health read and halt enforcement (#126).

A duplicate live signal carrying a different non-empty ``broker_order_id`` than the
stored one means a broker order may exist that the ledger does not track, so the
affected deployment is halted. These tests pin the two facts that keep it fail closed:

* the conflict is *reported* (health payload), not only logged; and
* the halt *stays* applied - a restart, or a status write that undid it, must not
  leave a deployment with an untracked broker order trading again.
"""

from __future__ import annotations

import logging

import pytest

from live_runner.broker_order_conflicts import (
    check_broker_order_conflicts,
    enforce_broker_order_conflict_halts,
)
from shared.database import (
    get_live_deployment,
    save_live_signal,
    update_live_deployment_status,
)
from shared.models import LiveSignal
from tests.conftest import seed_live_deployments

#: A UUID, because a persisted deployment row is rehydrated into a pydantic
#: ``LiveDeploymentConfig`` on every read and a synthetic id cannot round-trip.
DEPLOYMENT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_DEPLOYMENT_ID = "22222222-2222-2222-2222-222222222222"


def _signal(*, discord_message_id: str, broker_order_id: str) -> LiveSignal:
    return LiveSignal(
        deployment_id=DEPLOYMENT_ID,
        symbol="AAPL",
        action="buy",
        theoretical_price=100.0,
        timestamp="2026-11-21T00:00:00+00:00",
        metadata={
            "discord_message_id": discord_message_id,
            "broker_order_id": broker_order_id,
        },
    )


@pytest.fixture
def conflict(isolated_db) -> str:
    """One persisted conflicting id on ``DEPLOYMENT_ID``, which starts out halted."""
    database, _engine, _data_dir = isolated_db
    seed_live_deployments(database, [DEPLOYMENT_ID, OTHER_DEPLOYMENT_ID])
    save_live_signal(_signal(discord_message_id="ord-1", broker_order_id="BRK-AAA"))
    save_live_signal(_signal(discord_message_id="ord-1", broker_order_id="BRK-BBB"))
    return DEPLOYMENT_ID


@pytest.fixture
def active_deployment(isolated_db) -> str:
    """One deployment of the account, seeded ACTIVE so a conflict can halt it."""
    database, _engine, _data_dir = isolated_db
    seed_live_deployments(database, [DEPLOYMENT_ID])
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")
    return DEPLOYMENT_ID


def test_check_reports_the_conflict_as_unhealthy(conflict) -> None:
    health = check_broker_order_conflicts()

    assert health["status"] == "error"
    assert health["deployment_ids"] == [DEPLOYMENT_ID]
    conflict_row = health["conflicts"][0]
    assert conflict_row["deployment_id"] == DEPLOYMENT_ID
    assert conflict_row["symbol"] == "AAPL"
    assert conflict_row["broker_order_id"] == "BRK-AAA"
    assert conflict_row["conflicting_broker_order_ids"] == ["BRK-BBB"]


def test_check_is_ok_without_conflicts(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    seed_live_deployments(database, [DEPLOYMENT_ID])
    save_live_signal(_signal(discord_message_id="ord-1", broker_order_id="BRK-AAA"))
    save_live_signal(_signal(discord_message_id="ord-1", broker_order_id="BRK-AAA"))

    health = check_broker_order_conflicts()

    assert health == {"status": "ok", "deployment_ids": [], "conflicts": []}


def test_enforce_reasserts_the_halt_after_an_operator_resumes(conflict, caplog) -> None:
    """The core fail-closed case: the halt cannot be written away by a status change.

    Writing ACTIVE is exactly what an operator does when they are finished, so a
    deployment that still carries a conflict must come straight back to PAUSED.
    """
    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")
    assert get_live_deployment(DEPLOYMENT_ID).status == "ACTIVE"

    with caplog.at_level(logging.ERROR):
        halted = enforce_broker_order_conflict_halts()

    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"
    assert [row["deployment_id"] for row in halted] == [DEPLOYMENT_ID]
    assert halted[0]["conflicting_broker_order_ids"] == ["BRK-BBB"]
    alert = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(alert) == 1
    assert alert[0].data["alert"] == "live_broker_order_id_conflict"
    assert alert[0].data["deployment_id"] == DEPLOYMENT_ID


def test_enforce_leaves_an_already_halted_deployment_alone(conflict) -> None:
    """A re-halt of a halted deployment is a no-op, not a second stamp."""
    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"

    assert enforce_broker_order_conflict_halts() == []


def test_enforce_halts_only_the_deployments_that_carry_a_conflict(conflict) -> None:
    """The halt is narrow: another deployment on the same account keeps running."""
    assert get_live_deployment(OTHER_DEPLOYMENT_ID).status == "PAUSED"
    update_live_deployment_status(OTHER_DEPLOYMENT_ID, "ACTIVE")
    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"

    halted = enforce_broker_order_conflict_halts()

    assert halted == []
    assert get_live_deployment(OTHER_DEPLOYMENT_ID).status == "ACTIVE"


def test_enforce_reports_a_status_write_it_cannot_make(conflict, monkeypatch, caplog) -> None:
    """A halt that cannot be written must be loud, and must not stop the others."""
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")

    def _refuse(_deployment_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(
        "live_runner.broker_order_conflicts.pause_live_deployment_for_order_conflict",
        _refuse,
    )

    with caplog.at_level(logging.ERROR):
        halted = enforce_broker_order_conflict_halts()

    assert halted == []
    assert get_live_deployment(DEPLOYMENT_ID).status == "ACTIVE"
    alerts = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.data["alert"] for r in alerts] == ["live_broker_order_id_conflict_halt_failed"]
    assert alerts[0].data["deployment_id"] == DEPLOYMENT_ID


def test_resolve_clears_the_recordings_so_the_halt_can_be_lifted(
    conflict, caplog
) -> None:
    """The way out: clear the recorded ids, and the halt no longer re-asserts.

    Without this the halt is a dead end - every ACTIVE write would be undone at the
    next service sync tick and no operator action could recover the deployment.
    """
    from live_runner.broker_order_conflicts import resolve_broker_order_conflicts

    signal_id = check_broker_order_conflicts()["conflicts"][0]["signal_id"]
    cleared = resolve_broker_order_conflicts(DEPLOYMENT_ID)

    assert [row["signal_id"] for row in cleared] == [signal_id]
    assert cleared[0]["conflicting_broker_order_ids"] == ["BRK-BBB"]
    assert check_broker_order_conflicts()["status"] == "ok"
    assert check_broker_order_conflicts()["deployment_ids"] == []
    # With nothing recorded there is nothing left to enforce, and the halt is the
    # operator's to lift.
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")
    assert enforce_broker_order_conflict_halts() == []
    assert get_live_deployment(DEPLOYMENT_ID).status == "ACTIVE"
    assert [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_resolve_clears_every_conflict_of_the_deployment(active_deployment) -> None:
    """A partial clear would still halt the deployment, and would not say why."""
    from live_runner.broker_order_conflicts import resolve_broker_order_conflicts

    save_live_signal(
        _signal(discord_message_id="ord-two-1", broker_order_id="BRK-AAA")
    )
    save_live_signal(
        _signal(discord_message_id="ord-two-1", broker_order_id="BRK-BBB")
    )
    save_live_signal(
        _signal(discord_message_id="ord-two-2", broker_order_id="BRK-CCC")
    )
    save_live_signal(
        _signal(discord_message_id="ord-two-2", broker_order_id="BRK-DDD")
    )
    assert len(check_broker_order_conflicts()["conflicts"]) == 2

    cleared = resolve_broker_order_conflicts(DEPLOYMENT_ID)

    assert sorted(row["conflicting_broker_order_ids"][0] for row in cleared) == [
        "BRK-BBB",
        "BRK-DDD",
    ]
    assert check_broker_order_conflicts() == {
        "status": "ok",
        "deployment_ids": [],
        "conflicts": [],
    }


def test_resolve_reports_nothing_when_there_is_no_conflict(isolated_db) -> None:
    """A resolve on a clean deployment is an empty answer, not a partial one."""
    from live_runner.broker_order_conflicts import resolve_broker_order_conflicts

    database, _engine, _data_dir = isolated_db
    seed_live_deployments(database, [DEPLOYMENT_ID])
    save_live_signal(_signal(discord_message_id="ord-1", broker_order_id="BRK-AAA"))

    assert resolve_broker_order_conflicts(DEPLOYMENT_ID) == []
    assert check_broker_order_conflicts()["status"] == "ok"


def test_the_pause_by_conflict_survives_a_daemon_restart(conflict, caplog) -> None:
    """#126 decision: a paused-by-conflict deployment stays paused across a restart.

    PAUSED is a persisted status, so the daemon startup path (`_recover_failed_services`,
    which only ever retries FAILED services) cannot resurrect it, and the conflict
    enforcement on the restarted daemon still finds it halted.
    """
    import live_runner.daemon as daemon

    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"
    daemon._recover_failed_services()
    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"

    # A restart that still holds a stale in-memory ACTIVE snapshot re-halts here.
    update_live_deployment_status(DEPLOYMENT_ID, "ACTIVE")
    with caplog.at_level(logging.ERROR):
        daemon._recover_failed_services()
        enforce_broker_order_conflict_halts()

    assert get_live_deployment(DEPLOYMENT_ID).status == "PAUSED"
    assert check_broker_order_conflicts()["status"] == "error"
