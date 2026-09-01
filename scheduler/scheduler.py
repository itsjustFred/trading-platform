from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from scheduler.jobs.signal_bot_health_check import run as run_signal_bot_health_check
from scheduler.jobs.weekly_data_refresh import run as run_weekly_data_refresh
from shared.database import list_services, update_service_status, upsert_background_task_record
from shared.logging import get_logger
from shared.models import Service, ServiceStatus, ServiceType
from shared.sentry import capture_exception

logger = get_logger("scheduler")

_SCHEDULED_JOB_HANDLERS: dict[str, Callable[..., Awaitable[None]]] = {
    "weekly_data_refresh": run_weekly_data_refresh,
    "signal_bot_health_check": run_signal_bot_health_check,
}


class ServiceScheduler:
    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._started = False
        self._job_signatures: dict[str, str] = {}

    async def start(self) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        services = await asyncio.to_thread(
            list_services,
            service_type=ServiceType.SCHEDULED_JOB,
        )
        await self.sync_services(services)

    async def stop(self) -> None:
        if not self._started:
            return
        self._scheduler.shutdown(wait=False)
        self._started = False
        self._job_signatures.clear()

    async def sync_services(self, services: list[Service] | None = None) -> None:
        if not self._started:
            return
        if services is None:
            services = await asyncio.to_thread(
                list_services,
                service_type=ServiceType.SCHEDULED_JOB,
            )

        scheduled_services = [service for service in services if service.type == ServiceType.SCHEDULED_JOB]
        desired_job_ids = {str(service.id) for service in scheduled_services}

        for service in scheduled_services:
            service_id = str(service.id)
            if service.status != ServiceStatus.RUNNING:
                if self._scheduler.get_job(service_id) is not None:
                    self._scheduler.remove_job(service_id)
                self._job_signatures.pop(service_id, None)
                continue

            config = service.config if isinstance(service.config, dict) else {}
            cron_expr = str(config.get("cron") or "").strip()
            job_type = str(config.get("job_type") or "").strip().lower()
            if not cron_expr or not job_type:
                await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)
                logger.warning(
                    "Scheduled service config missing cron/job_type",
                    extra={"data": {"service_id": service_id, "service_name": service.name}},
                )
                continue

            if job_type not in _SCHEDULED_JOB_HANDLERS:
                await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)
                logger.warning(
                    "Unknown scheduled service job_type",
                    extra={
                        "data": {
                            "service_id": service_id,
                            "service_name": service.name,
                            "job_type": job_type,
                        }
                    },
                )
                continue

            signature = f"{cron_expr}|{job_type}"
            if (
                self._scheduler.get_job(service_id) is not None
                and self._job_signatures.get(service_id) == signature
            ):
                continue

            try:
                trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone.utc)
            except ValueError:
                await asyncio.to_thread(update_service_status, service_id, ServiceStatus.FAILED)
                logger.warning(
                    "Invalid cron for scheduled service",
                    extra={
                        "data": {
                            "service_id": service_id,
                            "service_name": service.name,
                            "cron": cron_expr,
                        }
                    },
                )
                continue
            self._scheduler.add_job(
                self._run_service_job,
                trigger=trigger,
                id=service_id,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                kwargs={
                    "service_id": service_id,
                    "service_name": service.name,
                    "job_type": job_type,
                },
                misfire_grace_time=300,
            )
            self._job_signatures[service_id] = signature
            logger.info(
                "Scheduled service job registered",
                extra={
                    "data": {
                        "service_id": service_id,
                        "service_name": service.name,
                        "cron": cron_expr,
                        "job_type": job_type,
                    }
                },
            )

        current_job_ids = {str(job.id) for job in self._scheduler.get_jobs()}
        for orphan_job_id in sorted(current_job_ids - desired_job_ids):
            self._scheduler.remove_job(orphan_job_id)
            self._job_signatures.pop(orphan_job_id, None)

    async def _run_service_job(
        self,
        *,
        service_id: str,
        service_name: str,
        job_type: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        task_id = f"service-{service_id}-{now.strftime('%Y%m%d%H%M%S')}"
        base_config = {
            "service_id": service_id,
            "service_name": service_name,
            "job_type": job_type,
            "started_at": now.isoformat(),
            "total_items": 1,
            "progress_percent": 5.0,
            "progress_detail": {
                "completed": 0,
                "failed": 0,
                "total": 1,
                "percent": 5.0,
                "current": f"Starting {job_type}",
            },
            "supports_control": False,
            "control_mode": "thread",
            "category": "service",
        }
        await asyncio.to_thread(
            upsert_background_task_record,
            task_id=task_id,
            task_type="service",
            status="running",
            config_json=base_config,
            progress_message=f"Starting {job_type}",
            name=f"{service_name} · {job_type}",
            description=f"Scheduled service job '{job_type}'",
            source="scheduler",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        await asyncio.to_thread(
            update_service_status,
            service_id,
            ServiceStatus.RUNNING,
            last_activity=now,
        )

        try:
            handler = _SCHEDULED_JOB_HANDLERS[job_type]
            await handler(task_id=task_id, service_id=service_id, service_name=service_name)
            finished = datetime.now(timezone.utc)
            complete_config = dict(base_config)
            complete_config.update(
                {
                    "finished_at": finished.isoformat(),
                    "progress_percent": 100.0,
                    "progress_detail": {
                        "completed": 1,
                        "failed": 0,
                        "total": 1,
                        "percent": 100.0,
                        "current": "Completed",
                    },
                    "result": {"status": "completed"},
                }
            )
            await asyncio.to_thread(
                upsert_background_task_record,
                task_id=task_id,
                task_type="service",
                status="completed",
                config_json=complete_config,
                progress_message="Completed",
                name=f"{service_name} · {job_type}",
                description=f"Scheduled service job '{job_type}'",
                source="scheduler",
                created_at=now.isoformat(),
                updated_at=finished.isoformat(),
            )
            await asyncio.to_thread(
                update_service_status,
                service_id,
                ServiceStatus.RUNNING,
                last_activity=finished,
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                capture_exception(
                    exc,
                    service_name="scheduler",
                    context={
                        "service_id": service_id,
                        "service_name": service_name,
                        "job_type": job_type,
                        "task_id": task_id,
                    },
                )
            failed_at = datetime.now(timezone.utc)
            failed_config = dict(base_config)
            failed_config.update(
                {
                    "finished_at": failed_at.isoformat(),
                    "progress_percent": 100.0,
                    "progress_detail": {
                        "completed": 0,
                        "failed": 1,
                        "total": 1,
                        "percent": 100.0,
                        "current": str(exc),
                    },
                    "error": str(exc),
                }
            )
            await asyncio.to_thread(
                upsert_background_task_record,
                task_id=task_id,
                task_type="service",
                status="failed",
                config_json=failed_config,
                progress_message=str(exc),
                name=f"{service_name} · {job_type}",
                description=f"Scheduled service job '{job_type}'",
                source="scheduler",
                created_at=now.isoformat(),
                updated_at=failed_at.isoformat(),
            )
            await asyncio.to_thread(
                update_service_status,
                service_id,
                ServiceStatus.FAILED,
                last_activity=failed_at,
            )
            raise
