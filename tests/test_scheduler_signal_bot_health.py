"""Tests for signal bot health check scheduler job."""
import asyncio
from unittest.mock import patch, MagicMock

import pytest

from scheduler.jobs.signal_bot_health_check import run


def test_signal_bot_health_check_job_imports() -> None:
    """Test that the scheduler job module imports correctly."""
    assert callable(run)


def test_signal_bot_health_check_job_runs_successfully() -> None:
    """Test that the scheduler job runs without error when signal bots are healthy."""
    async def _run_test():
        with patch(
            "scheduler.jobs.signal_bot_health_check.check_signal_bots"
        ) as mock_check:
            mock_check.return_value = {
                "status": "ok",
                "results": [
                    {"name": "ramjet", "status": "ok", "errors": []},
                    {"name": "nitro", "status": "ok", "errors": []},
                ],
            }

            with patch(
                "scheduler.jobs.signal_bot_health_check.upsert_background_task_record"
            ) as mock_upsert:
                mock_upsert.return_value = MagicMock()

                await run(task_id="test-task-1", service_id="svc-1", service_name="test-service")

                assert mock_check.called
                assert mock_upsert.call_count >= 2

    asyncio.run(_run_test())


def test_signal_bot_health_check_job_detects_errors() -> None:
    """Test that the scheduler job reports errors when signal bots are unhealthy."""
    async def _run_test():
        with patch(
            "scheduler.jobs.signal_bot_health_check.check_signal_bots"
        ) as mock_check:
            mock_check.return_value = {
                "status": "error",
                "results": [
                    {"name": "nitro", "status": "error", "errors": ["Config missing"]},
                ],
            }

            with patch(
                "scheduler.jobs.signal_bot_health_check.upsert_background_task_record"
            ) as mock_upsert:
                mock_upsert.return_value = MagicMock()

                await run(task_id="test-task-2", service_id="svc-2", service_name="test-service")

                assert mock_check.called
                assert mock_upsert.call_count >= 2

    asyncio.run(_run_test())