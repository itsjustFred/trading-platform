"""Regression: viewer shutdown must not block on in-flight startup prewarms.

The startup prewarms (JSON→SQLite migration, equity/backtest metadata warm)
fan out into long blocking scans. ``asyncio.to_thread`` runs them on the event
loop's default executor, which is joined with ``wait=True`` on loop close, and
a plain ``ThreadPoolExecutor`` additionally registers an interpreter-exit hook
that joins its (non-daemon) workers. An in-flight scan therefore pinned both a
``TestClient`` lifespan exit and interpreter teardown — unbounded on a
populated database, which timed out the CI ``Python ubuntu-latest`` job at
exactly the 60-minute limit. The prewarms now run on a dedicated daemon-thread
pool, so neither teardown waits on the blocking work.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

fastapi_testclient = pytest.importorskip("fastapi.testclient")


async def _noop_async() -> None:
    return None


def test_lifespan_shutdown_does_not_block_on_inflight_prewarm(monkeypatch):
    """A prewarm blocked mid-scan must not pin ``TestClient`` shutdown."""
    from viewer import main as viewer_main

    release = threading.Event()
    entered = threading.Event()

    def _blocking_prewarm() -> None:
        entered.set()
        release.wait(timeout=30)

    # Replace the JSON→SQLite migration (the first prewarm scheduled in the
    # lifespan) with a scan that blocks until the test releases it.
    monkeypatch.setattr(viewer_main, "_run_json_to_sqlite_migration", _blocking_prewarm)
    # Keep the remaining prewarms cheap and deterministic.
    monkeypatch.setattr(viewer_main, "init_db", lambda: None)
    monkeypatch.setattr(viewer_main, "_recover_maintenance_deletes", lambda: None)
    monkeypatch.setattr(viewer_main, "mark_background_tasks_interrupted", lambda **_: None)
    for name in (
        "_prewarm_data_views_background",
        "_prewarm_live_accounts_background",
        "_prewarm_strategies_background",
        "_prewarm_backtest_metadata_background",
        "_prewarm_portfolio_metadata_background",
        "_prewarm_tasks_cache_background",
        "_prewarm_jforex_jvm_background",
        "_sync_live_market_data_stream_background",
    ):
        monkeypatch.setattr(viewer_main, name, _noop_async)
    monkeypatch.setattr(viewer_main, "install_legacy_task_hooks", lambda: None)
    monkeypatch.setattr(viewer_main, "_resume_interrupted_tasks", lambda: None)

    started = time.monotonic()
    with fastapi_testclient.TestClient(viewer_main.app):
        assert entered.wait(timeout=10), "prewarm never started"
    # Shutdown happened while the prewarm was still blocked; it must not wait.
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"shutdown blocked on in-flight prewarm ({elapsed:.1f}s)"

    release.set()


def test_prewarm_workers_are_daemon_threads():
    """The prewarm pool must use daemon workers so teardown never joins them."""
    from viewer import main as viewer_main

    loop_ran = threading.Event()

    def _probe() -> None:
        loop_ran.set()

    future = viewer_main._BACKGROUND_PREWARM_EXECUTOR.submit(_probe)
    assert loop_ran.wait(timeout=10), "prewarm worker never ran"
    future.result(timeout=10)

    workers = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("viewer-prewarm")
    ]
    assert workers, "no prewarm worker thread observed"
    assert all(thread.daemon for thread in workers), [t.name for t in workers]


def test_interpreter_exit_is_not_pinned_by_inflight_prewarm():
    """A blocking prewarm on the pool must not delay interpreter shutdown."""
    script = textwrap.dedent(
        """
        import asyncio, sys, time
        sys.path.insert(0, {repo!r})
        from viewer import main as viewer_main

        async def main():
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                viewer_main._BACKGROUND_PREWARM_EXECUTOR, lambda: time.sleep(60)
            )
            await asyncio.sleep(0.3)
            future.cancel()

        asyncio.run(main())
        print("EXIT_REACHED")
        """
    ).format(repo=str(Path(__file__).parent.parent))

    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert "EXIT_REACHED" in result.stdout
    assert elapsed < 20, f"interpreter exit waited on in-flight prewarm ({elapsed:.1f}s)"
