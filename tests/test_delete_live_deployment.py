"""Deleting a deployment that still has history must refuse, not raise from the driver.

The defect: `delete_live_deployment` attempted the delete unconditionally. Once
`live_signals.deployment_id` carries a foreign key, any deployment with signals raised a raw
`sqlite3.IntegrityError` out of the ORM. The viewer did not catch it either, so the operator got a
500 for what is a deliberate refusal.

The behaviour chosen here is a typed refusal with counts, not a cascade. A cascade would "work" and
would silently delete the execution record of real trades - the one thing this database exists to
keep. The caller decides; this module only refuses, and refuses before writing anything.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from shared.models import LiveExecution


def _deployment(database: Any, deployment_id: str = "dep-delete") -> str:
    with database.get_session() as session:
        if session.get(database.LiveAccountModel, "acct-delete") is None:
            session.add(
                database.LiveAccountModel(id="acct-delete", broker="fixture", name="acct-delete")
            )
        session.add(
            database.LiveDeploymentModel(
                id=deployment_id,
                strategy_name=f"fixture-{deployment_id}",
                account_id="acct-delete",
                symbols_json=["EURUSD"],
                timeframe="1m",
                status="PAUSED",
            )
        )
        session.commit()
    return deployment_id


def _signal(database: Any, deployment_id: str) -> uuid.UUID:
    signal_id = uuid.uuid4()
    with database.get_session() as session:
        session.add(
            database.LiveSignalModel(
                id=str(signal_id),
                deployment_id=deployment_id,
                symbol="EURUSD",
                action="buy",
                theoretical_price=1.1,
                timestamp=datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc),
                source="internal",
                broker_order_id="",
            )
        )
        session.commit()
    return signal_id


def _execution(database: Any, signal_id: uuid.UUID) -> None:
    assert database.save_live_execution(
        LiveExecution(
            signal_id=signal_id,
            actual_price=1.1,
            filled_qty=0.5,
            commission=0.0,
            timestamp=datetime(2026, 10, 1, 10, 0, 5, tzinfo=timezone.utc),
            broker_order_id="bo-delete",
            dedupe_key="ctrader:deal:1",
        )
    )


def _deployment_exists(database: Any, deployment_id: str) -> bool:
    with database.get_session() as session:
        return session.get(database.LiveDeploymentModel, deployment_id) is not None


def test_a_deployment_without_history_is_deleted(isolated_db) -> None:
    """The ordinary case must keep working, and keep reporting True."""
    database, _engine, _data_dir = isolated_db
    database.init_db()
    deployment_id = _deployment(database)

    assert database.delete_live_deployment(deployment_id) is True
    assert not _deployment_exists(database, deployment_id)


def test_an_absent_deployment_reports_false(isolated_db) -> None:
    database, _engine, _data_dir = isolated_db
    database.init_db()

    assert database.delete_live_deployment("never-existed") is False


def test_a_blank_id_is_refused_without_touching_the_database(isolated_db) -> None:
    """An empty or whitespace id is not a lookup, so it must not reach the session.

    `session.get(Model, "")` is a legal call that simply finds nothing, so the guard reads as
    cosmetic - but a blank id reaching the delete path would let a caller who lost the real id
    delete *something* if the normalization ever changed, and it means every "no id" caller still
    opens a transaction. The guard is asserted directly, with a real deployment present to prove the
    blank id is not resolved to it.
    """
    database, _engine, _data_dir = isolated_db
    database.init_db()
    deployment_id = _deployment(database)

    for blank in ("", "   ", None):
        assert database.delete_live_deployment(blank) is False, blank
    assert _deployment_exists(database, deployment_id), (
        "a blank id deleted a real deployment, so the normalization guard is not load-bearing"
    )


def test_a_deployment_with_signals_refuses_with_a_typed_error(isolated_db) -> None:
    """The regression: no `IntegrityError` escapes, and nothing is half-deleted."""
    database, _engine, _data_dir = isolated_db
    database.init_db()
    deployment_id = _deployment(database)
    _signal(database, deployment_id)

    with pytest.raises(database.LiveDeploymentInUseError) as caught:
        database.delete_live_deployment(deployment_id)

    assert caught.value.deployment_id == deployment_id
    assert caught.value.signal_count == 1
    assert caught.value.execution_count == 0
    assert _deployment_exists(database, deployment_id), (
        "the deployment was deleted even though the call refused"
    )
    assert not isinstance(caught.value, database.IntegrityError), (
        "a raw driver exception is still escaping; the caller cannot distinguish a refusal from a "
        "database fault"
    )


def test_the_execution_count_is_reported_too(isolated_db) -> None:
    """The message has to say what would be destroyed, not just that something would."""
    database, _engine, _data_dir = isolated_db
    database.init_db()
    deployment_id = _deployment(database)
    signal_id = _signal(database, deployment_id)
    _execution(database, signal_id)

    with pytest.raises(database.LiveDeploymentInUseError) as caught:
        database.delete_live_deployment(deployment_id)

    assert (caught.value.signal_count, caught.value.execution_count) == (1, 1)
    assert "1 execution(s)" in str(caught.value)
    assert _deployment_exists(database, deployment_id)


def test_the_refusal_does_not_depend_on_the_foreign_key_being_present(isolated_db) -> None:
    """The check runs before the delete, so an unmigrated database behaves the same way.

    A database whose `live_signals` predates the key would otherwise accept the delete and leave
    orphaned signals behind - the exact state the migration exists to remove.
    """
    database, engine, _data_dir = isolated_db
    database.init_db()
    deployment_id = _deployment(database)
    _signal(database, deployment_id)

    with engine.begin() as connection:
        database.event.remove(engine, "connect", database._configure_sqlite_connection)
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("ALTER TABLE live_signals RENAME TO live_signals__no_fk")
        connection.exec_driver_sql(
            "CREATE TABLE live_signals AS SELECT * FROM live_signals__no_fk"
        )
        connection.exec_driver_sql("DROP TABLE live_signals__no_fk")
    # The pooled connections were configured with enforcement on; drop them so the rest of this test
    # really runs without it. Otherwise the key would refuse the delete and this would pass for the
    # wrong reason.
    engine.dispose()
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0, (
                "enforcement is still on, so this test cannot tell the pre-check from the key"
            )
        with pytest.raises(database.LiveDeploymentInUseError):
            database.delete_live_deployment(deployment_id)
        assert _deployment_exists(database, deployment_id)
    finally:
        database.event.listen(engine, "connect", database._configure_sqlite_connection)


def test_the_endpoint_reports_a_refusal_as_a_conflict(monkeypatch) -> None:
    """The typed refusal has to survive the HTTP boundary.

    Raising `LiveDeploymentInUseError` in the storage layer only helps if the endpoint catches it.
    It did not, so FastAPI answered 500 and the operator saw a server fault for a deliberate,
    data-preserving refusal. The counts are part of the answer: they say what is holding the row.
    """
    from fastapi.testclient import TestClient

    import viewer.main as viewer_main_module

    def _refuse(deployment_id: str) -> bool:
        raise viewer_main_module.LiveDeploymentInUseError(
            deployment_id, signal_count=1, execution_count=2
        )

    monkeypatch.setattr(viewer_main_module, "delete_live_deployment", _refuse)
    # The endpoint reads the row (to remember its status and to know whether the claim gate
    # needs closing) before deleting. A STOPPED row needs no gate write, so this isolates the
    # refusal path from the drain machinery.
    monkeypatch.setattr(
        viewer_main_module,
        "get_live_deployment",
        lambda deployment_id: SimpleNamespace(status="STOPPED"),
    )
    monkeypatch.setattr(
        viewer_main_module, "list_live_deployment_submission_claims", lambda deployment_id: []
    )
    client = TestClient(viewer_main_module.app)

    # Delete is a confirmed live control, so the request names the deployment it removes.
    response = client.delete(
        "/api/live/deployments/dep-keep?confirm_deployment_id=dep-keep"
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    # The refusal is structured so the operator (and the UI) can act on the counts.
    assert detail["error"] == "deployment_in_use"
    assert detail["signal_count"] == 1
    assert detail["execution_count"] == 2
    assert detail["status_restored"] is None


def test_an_unconfirmed_delete_is_refused_before_any_delete_is_attempted(monkeypatch) -> None:
    """Without `confirm_deployment_id` the request is refused (428) and nothing is touched."""
    from fastapi.testclient import TestClient

    import viewer.main as viewer_main_module

    attempts: list[str] = []

    def _record(deployment_id: str) -> bool:
        attempts.append(deployment_id)
        return True

    monkeypatch.setattr(viewer_main_module, "delete_live_deployment", _record)
    client = TestClient(viewer_main_module.app)

    response = client.delete("/api/live/deployments/dep-keep")

    assert response.status_code == 428, response.text
    assert attempts == []
