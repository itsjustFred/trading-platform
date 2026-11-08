"""Database primitives for the daemon control plane.

Throwaway SQLite sandboxes only; nothing here touches the production database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from shared.database import _prune_duplicate_submission_claims, update_service_status
from shared.models import Service, ServiceStatus, ServiceType


def test_paused_service_is_an_atomic_floor_against_a_failed_write(
    isolated_db_sandbox,
) -> None:
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(
            name="paused-bot",
            type=ServiceType.DISCORD_BOT,
            status=ServiceStatus.PAUSED,
        )
    )

    failed = update_service_status(
        str(service.id), ServiceStatus.FAILED, respect_operator_intent=True
    )
    running = update_service_status(
        str(service.id), ServiceStatus.RUNNING, respect_operator_intent=True
    )

    assert failed is not None and failed.status == ServiceStatus.PAUSED
    assert running is not None and running.status == ServiceStatus.PAUSED


def test_operator_can_resume_a_paused_service_by_default(isolated_db_sandbox) -> None:
    """The floor belongs to automated callers, not to the operator's own write.

    The API resume path calls this without opting in. If the floor were the
    default, `PUT /api/services/{id}/status {"status": "running"}` would answer
    200 and leave the row PAUSED - a paused bot that can never be restarted.
    """
    database, _, _ = isolated_db_sandbox
    service = database.create_service(
        Service(
            name="paused-bot",
            type=ServiceType.DISCORD_BOT,
            status=ServiceStatus.PAUSED,
        )
    )

    resumed = update_service_status(str(service.id), ServiceStatus.RUNNING)

    assert resumed is not None and resumed.status == ServiceStatus.RUNNING


def test_claim_refuses_a_deployment_that_is_not_active(isolated_db_sandbox) -> None:
    """The lease is granted by the database, not by a snapshot the caller holds.

    A PAUSED deployment must not be claimable: that is the race the claim exists
    to close, and dropping the status predicate from the INSERT..SELECT would
    reopen it while every other test still passed.
    """
    database, _, _ = isolated_db_sandbox
    database.init_db()
    with database.get_session() as session:
        session.add(
            database.LiveAccountModel(
                id="acc-1", broker="ctrader", name="demo", is_demo=True
            )
        )
        session.add(
            database.LiveDeploymentModel(
                id="dep-1",
                strategy_name="discord::ramjet",
                account_id="acc-1",
                symbols_json=["AAPL"],
                timeframe="1h",
                status="PAUSED",
            )
        )
        session.commit()

    assert database.claim_live_deployment_submission("dep-1", "token-1") is False
    assert database.list_live_deployment_submission_claims("dep-1") == []


def test_prune_keeps_the_in_flight_lease(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE live_deployment_submission_claims ("
                "token TEXT PRIMARY KEY, deployment_id TEXT, "
                "claimed_at DATETIME, expires_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO live_deployment_submission_claims VALUES "
                "('expired', 'dep', :old, :expired), "
                "('running', 'dep', :older, :in_flight)"
            ),
            {
                "old": now - timedelta(seconds=5),
                "expired": now - timedelta(seconds=1),
                "older": now - timedelta(seconds=20),
                "in_flight": now + timedelta(seconds=10),
            },
        )

        removed = _prune_duplicate_submission_claims(connection)
        remaining = [
            row[0]
            for row in connection.execute(
                text("SELECT token FROM live_deployment_submission_claims")
            ).fetchall()
        ]

    assert removed == 1
    assert remaining == ["running"]
