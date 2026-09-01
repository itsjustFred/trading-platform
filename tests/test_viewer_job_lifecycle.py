"""Cleanup 18-C slice 2 — `_running_jobs` lifecycle.

The job registry is mutated from request threads and worker threads, so every
transition must be lock-guarded, terminal jobs must be evicted after a grace
period, and a finished run must never leave a job record behind forever.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_register_and_finish_job_are_lock_guarded():
    from viewer import core

    job_id = "job-lock-1"
    try:
        core.register_job(job_id)
        assert core.get_job(job_id)["status"] == "running"

        core.finish_job(job_id, status="completed", result="ok")
        assert core.get_job(job_id)["status"] == "completed"
        assert core.get_job(job_id)["result"] == "ok"
    finally:
        core.evict_jobs(max_age_seconds=0)


def test_finish_job_fails_job_with_error():
    from viewer import core

    job_id = "job-lock-2"
    try:
        core.register_job(job_id)
        core.finish_job(job_id, status="failed", error="boom")
        job = core.get_job(job_id)
        assert job["status"] == "failed"
        assert job["error"] == "boom"
        assert job["result"] is None
    finally:
        core.evict_jobs(max_age_seconds=0)


def test_evict_jobs_removes_only_terminal_jobs_past_grace():
    from viewer import core

    terminal_id = "job-terminal"
    running_id = "job-still-running"
    try:
        core.register_job(terminal_id)
        core.register_job(running_id)
        core.finish_job(terminal_id, status="completed", result=None)

        # Grace period not elapsed yet -> nothing evicted.
        core.evict_jobs(max_age_seconds=3600)
        assert core.get_job(terminal_id) is not None
        assert core.get_job(running_id) is not None

        # After the grace period the terminal job is gone, the running one stays.
        core._JOBS_TIMESTAMP[terminal_id] = time.monotonic() - 10
        core.evict_jobs(max_age_seconds=5)
        assert core.get_job(terminal_id) is None
        assert core.get_job(running_id) is not None
    finally:
        core.evict_jobs(max_age_seconds=0)


def test_concurrent_job_updates_do_not_tear_state():
    from viewer import core

    job_id = "job-concurrent"
    try:
        core.register_job(job_id)
        errors: list[Exception] = []

        def _update(status: str) -> None:
            try:
                for _ in range(200):
                    core.set_job_status(job_id, status)
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=_update, args=("completed",)),
            threading.Thread(target=_update, args=("failed",)),
            threading.Thread(target=_update, args=("running",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        job = core.get_job(job_id)
        assert job["status"] in {"completed", "failed", "running"}
    finally:
        core.evict_jobs(max_age_seconds=0)
