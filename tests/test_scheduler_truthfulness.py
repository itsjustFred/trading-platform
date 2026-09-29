"""Truthfulness tests for scheduled job handlers (Cleanup 20-D).

A scheduled job must either perform its advertised operation or report a
skip/failure — never record a completed refresh that never happened.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import shared.constants as constants_module


def test_weekly_refresh_reports_skip_when_config_missing(tmp_path: Path) -> None:
    """Missing OHLCV data must surface as a skipped/failed run, not completed."""
    from scheduler.jobs import weekly_data_refresh

    data_dir = tmp_path / "data"
    constants_module.set_data_dir(data_dir)
    try:
        with patch.object(
            weekly_data_refresh, "upsert_background_task_record"
        ), patch.object(
            weekly_data_refresh,
            "has_ohlcv_bars_in_range",
            return_value=False,
        ), patch.object(
            weekly_data_refresh,
            "ensure_backtest_data",
            side_effect=RuntimeError("no route to host"),
        ) as mock_download:
            result = asyncio.run(
                weekly_data_refresh.run(
                    task_id="task-skip",
                    service_id="svc-1",
                    service_name="Weekly Data Refresh",
                )
            )
    finally:
        constants_module.reset_data_dir()

    assert result["status"] in {"skipped", "failed"}
    assert result["refreshed"] == []
    assert result["missing"], "expected missing symbols to be reported"
    assert mock_download.called, "the job must attempt the advertised refresh"


def test_weekly_refresh_reports_noop_when_everything_already_fresh(tmp_path: Path) -> None:
    """Full window coverage means a no-op: reported as skipped, never completed."""
    from scheduler.jobs import weekly_data_refresh

    data_dir = tmp_path / "data"
    for symbol in weekly_data_refresh.REFRESH_SYMBOLS:
        root = data_dir / "ohlcv" / "yfinance" / "equity" / "US" / symbol
        root.mkdir(parents=True, exist_ok=True)
        for tf in weekly_data_refresh.REFRESH_TIMEFRAMES:
            (root / f"{tf}.parquet").write_bytes(b"stub")

    constants_module.set_data_dir(data_dir)
    try:
        with patch.object(
            weekly_data_refresh,
            "has_ohlcv_bars_in_range",
            return_value=True,
        ), patch.object(
            weekly_data_refresh,
            "ensure_backtest_data",
            side_effect=AssertionError("nothing must be downloaded when all fresh"),
        ):
            result = asyncio.run(
                weekly_data_refresh.run(
                    task_id="task-ok",
                    service_id="svc-2",
                    service_name="Weekly Data Refresh",
                )
            )
    finally:
        constants_module.reset_data_dir()

    # Nothing was missing, so nothing was downloaded: the job must report a
    # truthful no-op, not a completed refresh.
    assert result["status"] == "skipped"
    assert result["refreshed"] == []
    assert sorted(result["already_fresh"]) == sorted(weekly_data_refresh.REFRESH_SYMBOLS)
    assert result["missing"] == []
    reported_start = datetime.fromisoformat(result["window"]["start"])
    reported_end = datetime.fromisoformat(result["window"]["end"])
    assert (reported_end - reported_start).days == 7


def test_weekly_refresh_download_failure_fails_the_run(tmp_path: Path) -> None:
    """A failed auto-download must fail the job, not record a completed refresh."""
    from scheduler.jobs import weekly_data_refresh

    data_dir = tmp_path / "data"
    constants_module.set_data_dir(data_dir)
    try:
        with patch.object(
            weekly_data_refresh,
            "has_ohlcv_bars_in_range",
            return_value=False,
        ), patch.object(
            weekly_data_refresh,
            "ensure_backtest_data",
            side_effect=RuntimeError("download failed"),
        ):
            result = asyncio.run(
                weekly_data_refresh.run(
                    task_id="task-fail",
                    service_id="svc-3",
                    service_name="Weekly Data Refresh",
                )
            )
    finally:
        constants_module.reset_data_dir()

    assert result["status"] == "failed"
    assert any("download failed" in error for error in result["errors"])


def test_weekly_refresh_writes_skipped_task_record(tmp_path: Path) -> None:
    """The background task record must reflect the truthful outcome."""
    from scheduler.jobs import weekly_data_refresh

    data_dir = tmp_path / "data"
    constants_module.set_data_dir(data_dir)
    try:
        with patch.object(
            weekly_data_refresh, "upsert_background_task_record"
        ) as upsert, patch.object(
            weekly_data_refresh,
            "has_ohlcv_bars_in_range",
            return_value=False,
        ), patch.object(
            weekly_data_refresh,
            "ensure_backtest_data",
            side_effect=RuntimeError("offline"),
        ):
            result = asyncio.run(
                weekly_data_refresh.run(
                    task_id="task-record",
                    service_id="svc-4",
                    service_name="Weekly Data Refresh",
                )
            )
    finally:
        constants_module.reset_data_dir()

    assert result["status"] == "failed"
    statuses = [call.kwargs.get("status") for call in upsert.call_args_list]
    assert statuses[-1] == "failed"
    messages = [str(call.kwargs.get("progress_message", "")) for call in upsert.call_args_list]
    assert any("offline" in message.lower() for message in messages)


def test_weekly_refresh_rejects_unsafe_config_before_download(tmp_path: Path) -> None:
    from scheduler.jobs import weekly_data_refresh

    with patch.object(weekly_data_refresh, "upsert_background_task_record"), patch.object(
        weekly_data_refresh,
        "ensure_backtest_data",
        side_effect=AssertionError("unsafe config must not download"),
    ) as download:
        result = asyncio.run(
            weekly_data_refresh.run(
                task_id="task-config",
                service_id="svc-config",
                service_name="Weekly Data Refresh",
                config={"symbols": ["../escape"], "timeframes": ["1h"]},
            )
        )

    assert result["status"] == "failed"
    assert not download.called


def test_weekly_refresh_continues_after_unexpected_symbol_error(tmp_path: Path) -> None:
    from scheduler.jobs import weekly_data_refresh

    calls: list[str] = []
    fresh = {"AAPL": False, "MSFT": False}

    def fake_download(*, symbols, **_kwargs):
        calls.append(symbols[0])
        if symbols[0] == "AAPL":
            raise KeyError("unexpected unit failure")
        fresh["MSFT"] = True
        return []

    with patch.object(weekly_data_refresh, "upsert_background_task_record"), patch.object(
        weekly_data_refresh,
        "has_ohlcv_bars_in_range",
        side_effect=lambda *, symbol, **_kwargs: fresh[symbol],
    ), patch.object(
        weekly_data_refresh,
        "ensure_backtest_data",
        side_effect=fake_download,
    ):
        result = asyncio.run(
            weekly_data_refresh.run(
                task_id="task-continue",
                service_id="svc-continue",
                service_name="Weekly Data Refresh",
                config={"symbols": ["AAPL", "MSFT"], "timeframes": ["1h"]},
            )
        )

    assert calls == ["AAPL", "MSFT"]
    assert result["refreshed"] == ["MSFT"]
    assert any("KeyError" in error for error in result["errors"])


def test_signal_bot_health_check_handler_is_registered() -> None:
    """The signal-bot health-check job must be registered with the scheduler."""
    from scheduler.scheduler import _SCHEDULED_JOB_HANDLERS

    assert "signal_bot_health_check" in _SCHEDULED_JOB_HANDLERS
    handler = _SCHEDULED_JOB_HANDLERS["signal_bot_health_check"]
    assert handler.__module__ == "scheduler.jobs.signal_bot_health_check"


def test_registered_handlers_accept_handler_contract() -> None:
    """Every registered handler must accept the documented keyword contract."""
    import inspect

    from scheduler.scheduler import _SCHEDULED_JOB_HANDLERS

    for job_type, handler in _SCHEDULED_JOB_HANDLERS.items():
        signature = inspect.signature(handler)
        for parameter in ("task_id", "service_id", "service_name"):
            assert parameter in signature.parameters, f"{job_type}: missing {parameter}"


def test_scheduler_sync_registers_health_check_service(tmp_path: Path) -> None:
    """A RUNNING service with job_type signal_bot_health_check must get a real job."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from scheduler.scheduler import ServiceScheduler
    from shared.models import Service, ServiceStatus, ServiceType

    service = Service(
        name="Signal Bot Health Check",
        type=ServiceType.SCHEDULED_JOB,
        status=ServiceStatus.RUNNING,
        config={"cron": "*/30 * * * *", "job_type": "signal_bot_health_check"},
    )

    job_ids: list[str] = []

    async def _scenario() -> None:
        scheduler = ServiceScheduler()
        scheduler._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        scheduler._scheduler.start()
        scheduler._started = True
        try:
            with patch(
                "scheduler.scheduler.update_service_status"
            ) as mock_status, patch("scheduler.scheduler.list_services") as mock_list:
                mock_list.side_effect = lambda **kwargs: [service]
                mock_status.return_value = None
                await scheduler.sync_services([service])
                job_ids.extend(str(job.id) for job in scheduler._scheduler.get_jobs())
        finally:
            await scheduler.stop()

    asyncio.run(_scenario())

    assert job_ids, "expected at least one registered job"


def test_scheduler_wrapper_records_handler_reported_status(tmp_path: Path) -> None:
    """_run_service_job must not overwrite a handler's truthful non-success status."""
    from scheduler.scheduler import ServiceScheduler

    async def _scenario() -> list[dict]:
        scheduler = ServiceScheduler()
        scheduler._scheduler.start()
        scheduler._started = True
        records: list[dict] = []

        async def _handler(**kwargs):
            # Simulate a handler that reports "skipped" and returns without
            # writing its own final record (the wrapper must record "skipped").
            return {"status": "skipped", "refreshed": [], "missing": ["EURUSD"]}

        try:
            with patch(
                "scheduler.scheduler.update_service_status"
            ), patch("scheduler.scheduler.upsert_background_task_record") as upsert, patch.dict(
                "scheduler.scheduler._SCHEDULED_JOB_HANDLERS",
                {"weekly_data_refresh": _handler},
            ):
                upsert.side_effect = lambda **kwargs: records.append(dict(kwargs))
                await scheduler._run_service_job(
                    service_id="svc-wrapper",
                    service_name="Weekly Data Refresh",
                    job_type="weekly_data_refresh",
                )
        finally:
            await scheduler.stop()
        return records

    records = asyncio.run(_scenario())

    assert records, "wrapper must write a final task record"
    assert records[-1]["status"] == "skipped", (
        "the wrapper must not downgrade a handler-reported skip to completed"
    )
    assert records[-1]["config_json"]["result"]["status"] == "skipped"


def test_scheduler_wrapper_defaults_to_completed_when_handler_returns_none() -> None:
    """Handlers that return nothing keep the plain completed record."""
    from scheduler.scheduler import ServiceScheduler

    async def _scenario() -> list[dict]:
        scheduler = ServiceScheduler()
        scheduler._scheduler.start()
        scheduler._started = True
        records: list[dict] = []

        async def _handler(**kwargs):
            return None

        try:
            with patch(
                "scheduler.scheduler.update_service_status"
            ), patch("scheduler.scheduler.upsert_background_task_record") as upsert, patch.dict(
                "scheduler.scheduler._SCHEDULED_JOB_HANDLERS",
                {"signal_bot_health_check": _handler},
            ):
                upsert.side_effect = lambda **kwargs: records.append(dict(kwargs))
                await scheduler._run_service_job(
                    service_id="svc-none",
                    service_name="Signal Bot Health Check",
                    job_type="signal_bot_health_check",
                )
        finally:
            await scheduler.stop()
        return records

    records = asyncio.run(_scenario())

    assert records, "wrapper must write a final task record"
    assert records[-1]["status"] == "completed"


def test_scheduler_task_ids_are_unique_within_a_second() -> None:
    """B2: microsecond suffix — same-second runs must not share a task id."""
    import inspect

    import scheduler.scheduler as scheduler_module

    source = inspect.getsource(scheduler_module.ServiceScheduler._run_service_job)
    assert "%Y%m%d%H%M%S%f" in source


def test_two_service_runs_in_the_same_second_get_different_task_ids(monkeypatch) -> None:
    """The behavioural half of the truthfulness pin, and the half that matters.

    `test_scheduler_task_ids_are_unique_within_a_second` asserts the format string
    `"%Y%m%d%H%M%S%f"` appears in the source. That is the weakest form of check
    there is: it says nothing about what the code does with the value, and any
    refactor to a different correct-looking construction would fail it.

    Measured: dropping the microseconds so two runs in one second collide fails
    that test and **nothing else**. Five other files mention `task_id`; all pass.
    So the property had a single line of coverage and it was a line of text.

    What a collision costs is concrete. The id is the key of
    `upsert_background_task_record`, so a second run in the same second
    *overwrites* the first run's record rather than queueing beside it. One of two
    service executions is not rendered oddly - it is simply gone from the task list.

    The clock here *advances*, in quarter-second steps, rather than being frozen.
    That is the whole design: freezing it would give every call an identical
    instant and the correct implementation would fail too, because the
    microsecond suffix comes from the clock. Stepping inside one second models the
    real case - two runs a fraction of a second apart - where a second-resolution
    id collides and a microsecond one does not.
    """
    import asyncio
    from datetime import datetime, timedelta, timezone

    import scheduler.scheduler as scheduler_module

    origin = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    # One millisecond per call. Small enough that a whole run - which calls
    # `now()` several times, twice of them feeding one task id - still lands
    # inside the same wall-clock second, so both runs really are "in the same
    # second" and a second-resolution id would collide.
    ticks = iter(origin + timedelta(milliseconds=n) for n in range(200))

    class _SteppingDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001, ANN206 - mirrors datetime's signature
            return next(ticks)

    seen: list[str] = []

    # Plain `def`, not `async def`: the code reaches these through
    # `asyncio.to_thread`, which calls them synchronously in a worker. An async
    # fake returns a coroutine that is never awaited, so nothing is recorded and
    # the test fails for a reason unrelated to task ids.
    def _fake_upsert(**kwargs):  # noqa: ANN003
        seen.append(str(kwargs["task_id"]))

    def _fake_status(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    async def _fake_handler(**_kwargs):  # noqa: ANN003
        return {"status": "completed"}

    monkeypatch.setattr(scheduler_module, "datetime", _SteppingDatetime)
    monkeypatch.setattr(scheduler_module, "upsert_background_task_record", _fake_upsert)
    monkeypatch.setattr(scheduler_module, "update_service_status", _fake_status)
    monkeypatch.setitem(scheduler_module._SCHEDULED_JOB_HANDLERS, "probe", _fake_handler)

    scheduler = scheduler_module.ServiceScheduler()

    # One id per run, read from the run's *first* write. The two writes inside a
    # run deliberately share an id - that is what makes the start record and the
    # completion record one task rather than two - so the thing to compare is the
    # id of run 1 against the id of run 2.
    first_of_each_run: list[str] = []
    for _ in range(2):
        seen.clear()
        asyncio.run(
            scheduler._run_service_job(
                service_id="svc-1",
                service_name="Probe",
                job_type="probe",
            )
        )
        assert seen, "precondition: the job wrote no task record at all"
        first_of_each_run.append(seen[0])

    assert first_of_each_run[0] != first_of_each_run[1], (
        f"both runs produced the task id {first_of_each_run[0]!r} within the same "
        "second. The id is an upsert key, so the first run's record is overwritten "
        "and that service execution disappears from the task list entirely."
    )
