from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from shared.database import upsert_background_task_record
from shared.logging import get_logger
from signal_bots.health_check import check_signal_bots

logger = get_logger("scheduler")


async def run(*, task_id: str, service_id: str, service_name: str) -> None:
    """Run signal bot health check and report status."""
    logger.info(
        "Signal bot health check started",
        extra={"data": {"service_id": service_id, "service_name": service_name}},
    )

    await asyncio.to_thread(
        upsert_background_task_record,
        task_id=task_id,
        task_type="service",
        status="running",
        config_json={
            "service_id": service_id,
            "service_name": service_name,
            "supports_control": False,
            "control_mode": "thread",
            "category": "service",
        },
        progress_message="Checking signal bot processors...",
        name=f"{service_name} · Signal Bot Health Check",
        description="Validates signal processor registry and config files for stockkit, ramjet, rocketfuel, and nitro bots.",
        source="scheduler",
        updated_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        results = check_signal_bots()
        overall_status = results.get("status", "error")
        bot_results = results.get("results", [])

        errors = []
        for r in bot_results:
            if r.get("errors"):
                errors.extend([f"{r['name']}: {e}" for e in r["errors"]])

        if overall_status == "ok":
            status = "completed"
            logger.info(
                "Signal bot health check passed",
                extra={"data": {"service_id": service_id, "bots_checked": len(bot_results)}},
            )
        else:
            status = "failed"
            logger.warning(
                "Signal bot health check failed",
                extra={"data": {"service_id": service_id, "errors": errors}},
            )

        await asyncio.to_thread(
            upsert_background_task_record,
            task_id=task_id,
            task_type="service",
            status=status,
            config_json={
                "service_id": service_id,
                "service_name": service_name,
                "bots_checked": len(bot_results),
                "overall_status": overall_status,
                "errors": errors,
            },
            progress_message=f"Signal bot health check: {overall_status}",
            name=f"{service_name} · Signal Bot Health Check",
            description="Validates signal processor registry and config files for stockkit, ramjet, rocketfuel, and nitro bots.",
            source="scheduler",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    except Exception as exc:
        logger.exception(
            "Signal bot health check error",
            extra={"data": {"service_id": service_id, "error": str(exc)}},
        )
        await asyncio.to_thread(
            upsert_background_task_record,
            task_id=task_id,
            task_type="service",
            status="failed",
            config_json={
                "service_id": service_id,
                "service_name": service_name,
                "error": str(exc),
            },
            progress_message=f"Signal bot health check error: {str(exc)[:100]}",
            name=f"{service_name} · Signal Bot Health Check",
            description="Validates signal processor registry and config files for stockkit, ramjet, rocketfuel, and nitro bots.",
            source="scheduler",
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        raise