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

    failed = update_service_status(str(service.id), ServiceStatus.FAILED)
    running = update_service_status(str(service.id), ServiceStatus.RUNNING)

    assert failed is not None and failed.status == ServiceStatus.PAUSED
    assert running is not None and running.status == ServiceStatus.PAUSED


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
