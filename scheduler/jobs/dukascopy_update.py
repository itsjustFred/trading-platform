"""Weekly Dukascopy dataset refresh scheduled job.

Runs the incremental pipeline in ``data_tools.dukascopy.pipeline`` against
the configured dataset root and optionally mirrors the changed files to R2.
Any failure or incomplete fetch is logged as an ERROR ``data.alert`` record —
under the daemon that reaches the Discord ops webhook through
``shared.alerts`` — and recorded as a failed task.

Config (service ``config`` mapping, ``dukascopy`` section)::

    {
      "dukascopy": {
        "root": "/mnt/block-volume-data/dukascopy",
        "symbols": ["EURUSD", "..."],        # optional, default all 16
        "upload": true,                       # optional, default false
        "raw_dir": "/path/_raw"               # optional, default <root>/_raw
      }
    }
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from shared.database import upsert_background_task_record
from shared.logging import get_logger

logger = get_logger("scheduler")

_DESCRIPTION = "Incremental Dukascopy dataset refresh with optional R2 upload."


def _dukascopy_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    source: Mapping[str, Any] = {} if config is None else config
    if not isinstance(source, Mapping):
        raise ValueError("dukascopy_update config must be a mapping")
    section = source.get("dukascopy", source.get("dukascopy_update", source))
    if not isinstance(section, Mapping):
        raise ValueError("dukascopy_update config section must be a mapping")
    return dict(section)


async def run(
    *,
    task_id: str,
    service_id: str,
    service_name: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the incremental update; returns a status dict for the scheduler."""
    from data_tools.dukascopy import pipeline, upload

    cfg = _dukascopy_config(config)
    root = str(cfg.get("root") or "").strip()
    if not root:
        result: dict[str, Any] = {
            "status": "failed",
            "errors": ["dukascopy.root is not configured"],
        }
        await _record(
            task_id, service_id, service_name, "failed", result,
            "dukascopy.root is not configured",
        )
        return result

    symbols = cfg.get("symbols") or None
    do_upload = bool(cfg.get("upload"))
    raw_dir = cfg.get("raw_dir") or None

    logger.info(
        "Dukascopy update started",
        extra={"data": {"service_id": service_id, "root": root, "upload": do_upload}},
    )
    await _record(
        task_id, service_id, service_name, "running",
        {"root": root, "upload": do_upload}, "Dukascopy update running",
    )

    try:
        report = await asyncio.to_thread(
            pipeline.update_dataset,
            root,
            symbols=symbols,
            raw_dir=raw_dir,
        )
        uploaded: list[str] = []
        if do_upload and report.published:
            uploaded = await asyncio.to_thread(
                upload.upload_dataset, root, report.published
            )
        problems = await asyncio.to_thread(pipeline.verify_manifest, root)
        statuses = {s.symbol: s.status for s in report.symbols}
        bad = {k: v for k, v in statuses.items() if v not in {"updated", "up-to-date"}}
        if bad or problems:
            error = "; ".join(
                [f"{k}: {v}" for k, v in bad.items()]
                + [f"manifest: {p}" for p in problems[:5]]
            )
            result = {
                "status": "failed" if bad else "partial",
                "symbols": statuses,
                "uploaded": len(uploaded),
                "errors": [error] if error else [],
            }
            await _record(
                task_id, service_id, service_name, result["status"], result,
                error or "Dukascopy update finished with warnings",
            )
            logger.error(
                "Dukascopy update finished with problems: %s",
                error,
                extra={
                    "data": {
                        "alert": "dukascopy_update_incomplete",
                        "errors": result["errors"],
                    }
                },
            )
            return result
        result = {
            "status": "completed",
            "symbols": statuses,
            "uploaded": len(uploaded),
            "errors": [],
        }
        await _record(
            task_id, service_id, service_name, "completed", result,
            "Dukascopy update completed",
        )
        return result
    except Exception as exc:
        logger.exception("Dukascopy update failed")
        logger.error(
            "Dukascopy update failed: %s",
            exc,
            extra={
                "data": {
                    "alert": "dukascopy_update_failed",
                    "error": str(exc),
                }
            },
        )
        result = {"status": "failed", "errors": [f"{type(exc).__name__}: {exc}"]}
        await _record(
            task_id, service_id, service_name, "failed", result,
            f"Dukascopy update failed: {exc}",
        )
        return result


async def _record(
    task_id: str,
    service_id: str,
    service_name: str,
    status: str,
    result: dict[str, Any],
    message: str,
) -> None:
    await asyncio.to_thread(
        upsert_background_task_record,
        task_id=task_id,
        task_type="service",
        status=status,
        config_json={
            "service_id": service_id,
            "service_name": service_name,
            "result": result,
        },
        progress_message=message,
        name=f"{service_name} · Dukascopy Update",
        description=_DESCRIPTION,
        source="scheduler",
    )
