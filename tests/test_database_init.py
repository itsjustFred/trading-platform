from __future__ import annotations

from pathlib import Path


def test_init_db_runs_schema_setup_once(monkeypatch, tmp_path: Path) -> None:
    import shared.database as database

    calls = {"create_all": 0, "migrate": 0}

    def fake_create_all(*_args, **_kwargs):
        calls["create_all"] += 1

    def fake_migrate_schema() -> None:
        calls["migrate"] += 1

    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    monkeypatch.setattr(database.SQLModel.metadata, "create_all", fake_create_all)
    monkeypatch.setattr(database, "_migrate_schema", fake_migrate_schema)
    monkeypatch.setattr(database, "_DB_INITIALIZED", False)

    database.init_db()
    database.init_db()

    assert calls == {"create_all": 1, "migrate": 1}


def test_undefined_metrics_survive_the_metadata_payload() -> None:
    """runner/stats.py names what it could not compute; the DB must not fill it in.

    Substituting a default here turned an undefined PSR into 0.0, which is the
    *worst* attainable PSR, so a backtest with no trades at all was listed as
    "PSR (higher = better) 0.0%" and read as a measured result. p_value had the
    same shape, defaulting to 1.0 -- maximum confidence in the null.
    """
    import shared.database as database

    stats = {"sharpe_ratio": 1.5, "undefined_metrics": ["psr", "p_value", "sharpe_ratio"]}
    assert database._stat_number_or_undefined(stats, "psr", 0.0) is None
    assert database._stat_number_or_undefined(stats, "p_value", 1.0) is None
    # Named as undefined even though a stale value is present: the producer is the
    # only party that knows whether the number is missing or genuinely zero.
    assert database._stat_number_or_undefined(stats, "sharpe_ratio", 0.0) is None

    # A metric the producer did not name keeps its value, and a real zero is a
    # measurement, not an absence.
    measured = {"psr": 0.0, "undefined_metrics": ["p_value"]}
    assert database._stat_number_or_undefined(measured, "psr", 0.0) == 0.0
    assert database._stat_number_or_undefined(measured, "p_value", 1.0) is None

    # Artifacts written before the convention existed carry no list at all and
    # must keep their historical values.
    legacy = {"psr": 0.75}
    assert database._stat_number_or_undefined(legacy, "psr", 0.0) == 0.75
    assert database._stat_number_or_undefined(legacy, "p_value", 1.0) == 1.0
    assert database._stat_number_or_undefined({}, "psr", 0.0) == 0.0


def test_daemon_heartbeat_upserts_and_reports_liveness(isolated_db) -> None:
    """A paused platform is silent, so "no activity" is ambiguous.

    The daemon publishing itself into the services table is what makes "running
    but idle" separable from "dead" for anyone reading the UI. The upsert has to
    work on the first beat, because update_service_status needs an existing row.
    """
    import shared.database as database

    first = database.record_daemon_heartbeat(detail={"connected_adapters": 2})
    assert str(first.id) == database.TRADING_DAEMON_SERVICE_ID
    assert first.name == "trading-daemon"
    assert first.type.value == "trading_daemon"
    assert first.last_activity is not None
    assert first.config["connected_adapters"] == 2

    # A second beat updates the same row rather than creating a second one.
    second = database.record_daemon_heartbeat(detail={"connected_adapters": 0})
    assert second.id == first.id
    assert second.last_activity >= first.last_activity
    assert second.config["connected_adapters"] == 0

    from shared.database import list_services

    rows = [
        s for s in list_services() if str(s.id) == database.TRADING_DAEMON_SERVICE_ID
    ]
    assert len(rows) == 1, "the heartbeat must upsert, not accumulate rows"


def test_daemon_heartbeat_failure_cannot_stop_the_daemon(monkeypatch) -> None:
    """A heartbeat that can take the daemon down is worse than no heartbeat.

    The component reporting on liveness must not be able to fail because of it,
    so the loop swallows a database error and keeps going. Asserted by letting it
    survive several failing beats, not by inspecting cancellation semantics.
    """
    import asyncio

    import live_runner.daemon as daemon

    beats: list[int] = []

    # Plain sync, like the real record_daemon_heartbeat: it is called *through*
    # asyncio.to_thread, which invokes it synchronously in a worker. An async
    # double would return an un-awaited coroutine and never run at all.
    def _boom(**_kwargs):
        beats.append(1)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(daemon, "record_daemon_heartbeat", _boom)
    # A short interval so the loop actually iterates inside the test.
    monkeypatch.setattr(daemon, "DAEMON_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    async def _exercise() -> int:
        task = asyncio.ensure_future(daemon._daemon_heartbeat_loop({}))
        deadline = 0.0
        while len(beats) < 3 and deadline < 2.0:
            await asyncio.sleep(0.01)
            deadline += 0.01
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return len(beats)

    assert asyncio.run(_exercise()) >= 3, (
        "the loop must survive repeated database failures rather than exiting on "
        "the first one"
    )
