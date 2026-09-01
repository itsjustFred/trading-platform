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


def test_weekly_refresh_refreshes_symbols_with_coverage(tmp_path: Path) -> None:
    """Symbols whose OHLCV coverage is sufficient are refreshed (no-op) and reported."""
    from scheduler.jobs import weekly_data_refresh

    data_dir = tmp_path / "data"
    for symbol in weekly_data_refresh.REFRESH_SYMBOLS:
        root = data_dir / "ohlcv" / "yfinance" / "equity" / "US" / symbol
        root.mkdir(parents=True, exist_ok=True)
        for tf in weekly_data_refresh.REFRESH_TIMEFRAMES:
            (root / f"{tf}.parquet").write_bytes(b"stub")

    constants_module.set_data_dir(data_dir)
    try:
        result = asyncio.run(
            weekly_data_refresh.run(
                task_id="task-ok",
                service_id="svc-2",
                service_name="Weekly Data Refresh",
            )
        )
    finally:
        constants_module.reset_data_dir()

    assert result["status"] == "completed"
    assert sorted(result["refreshed"]) == sorted(weekly_data_refresh.REFRESH_SYMBOLS)
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
