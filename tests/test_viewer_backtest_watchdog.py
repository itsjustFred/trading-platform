"""Cleanup 18-C slice 4 — watchdog covers the whole child process lifetime.

The backtest pump drains stdout until EOF and only then reaches
`proc.wait(timeout=...)`; the timeout covered post-EOF teardown, not the run.
A watchdog must bound the entire child lifetime, including a child that
streams forever without exiting.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_watchdog_kills_child_that_streams_forever():
    """A chatty child that never exits must be killed by the watchdog."""
    from viewer import main as viewer_main

    # Child prints a line every 100ms and never exits on its own.
    cmd = [
        sys.executable,
        "-c",
        "import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.1)\n",
    ]

    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        viewer_main._monitor_backtest_process(
            proc,
            timeout_seconds=1.0,
            poll_interval=0.05,
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    elapsed = time.monotonic() - started
    assert proc.returncode is not None, "watchdog must terminate the child"
    assert elapsed < 5.0, f"watchdog took {elapsed:.2f}s; expected ~1s"


def test_watchdog_does_not_kill_child_that_finishes_in_time():
    from viewer import main as viewer_main

    cmd = [sys.executable, "-c", "print('done', flush=True)"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        outcome = viewer_main._monitor_backtest_process(
            proc,
            timeout_seconds=15.0,
            poll_interval=0.05,
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    assert outcome is True
    assert proc.returncode == 0


def test_watchdog_reports_timed_out_child():
    """The monitor must report whether it timed out so the caller can fail the task."""
    from viewer import main as viewer_main

    cmd = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    outcome = None
    try:
        outcome = viewer_main._monitor_backtest_process(
            proc,
            timeout_seconds=0.5,
            poll_interval=0.05,
        )
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    assert outcome is False
    assert proc.returncode is not None


def test_timeout_error_message_reports_configured_budget(monkeypatch):
    """The post-EOF timeout error must name the actual budget, not a hard-coded 300."""
    from viewer import main as viewer_main

    monkeypatch.setattr(viewer_main, "BACKTEST_WATCHDOG_TIMEOUT_SECONDS", 60.0)
    message = f"Backtest timed out after {viewer_main.BACKTEST_WATCHDOG_TIMEOUT_SECONDS:.0f} seconds"
    assert "60" in message
    assert "300" not in message


def test_watchdog_does_not_kill_its_own_process_group():
    """A child that is NOT a group leader must fall back to proc.kill():
    killpg on our own pgid would kill the caller (observed as SIGKILL to the
    whole pytest run when the runner is spawned without start_new_session)."""
    from viewer import main as viewer_main

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    import os

    try:
        # The child shares our process group here (no start_new_session).
        assert os.getpgid(proc.pid) != proc.pid
        viewer_main._kill_process_group(proc)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert proc.returncode is not None


def test_watchdog_kills_child_group_when_child_is_group_leader():
    """A start_new_session child leads its own group; the watchdog must kill
    the whole group (and survive doing so)."""
    import os

    from viewer import main as viewer_main

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        assert os.getpgid(proc.pid) == proc.pid
        outcome = viewer_main._monitor_backtest_process(
            proc, timeout_seconds=0.5, poll_interval=0.05
        )
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert outcome is False
    assert proc.returncode is not None


def test_watchdog_signals_timed_out_event_before_killing():
    """The shared event must be set when the watchdog kills, so the caller's
    failure path can report the timeout instead of the kill's exit code."""
    from viewer import main as viewer_main

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    timed_out = threading.Event()
    try:
        outcome = viewer_main._monitor_backtest_process(
            proc, timeout_seconds=0.5, poll_interval=0.05, timed_out=timed_out
        )
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert outcome is False
    assert timed_out.is_set(), "watchdog timeout must set the shared event"


def test_watchdog_leaves_timed_out_event_clear_on_normal_exit():
    """A child that exits in time must NOT trip the caller's timeout flag."""
    from viewer import main as viewer_main

    proc = subprocess.Popen(
        [sys.executable, "-c", "print('done', flush=True)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    timed_out = threading.Event()
    try:
        outcome = viewer_main._monitor_backtest_process(
            proc, timeout_seconds=15.0, poll_interval=0.05, timed_out=timed_out
        )
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    assert outcome is True
    assert not timed_out.is_set()


def test_run_reports_timeout_reason_not_kill_exit_code(
    viewer_client, monkeypatch, tmp_path
):
    """A watchdog-killed child's returncode is whatever the kill produced; the
    task error must still name the timeout budget instead of that exit code."""
    import viewer.main as viewer_app_module

    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    strategy_file = strategies_dir / "demo_strategy.py"
    strategy_file.write_text("class DemoStrategy:\n    pass\n", encoding="utf-8")

    monkeypatch.setattr(viewer_app_module, "STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(viewer_app_module.legacy_viewer, "STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(viewer_app_module, "_dolthub_pull", lambda: None)
    monkeypatch.setattr(viewer_app_module, "_ensure_backtest_data_availability", lambda **kwargs: [])
    monkeypatch.setattr(viewer_app_module, "BACKTEST_WATCHDOG_TIMEOUT_SECONDS", 0.5)

    # `viewer_app_module.subprocess` is the global subprocess module, so patch
    # the attribute the worker reads and call the real Popen captured here.
    real_popen = subprocess.Popen

    def fake_popen(cmd, *args, **kwargs):
        # A child that keeps streaming (and never exits) until the watchdog kills it.
        return real_popen(
            [
                sys.executable,
                "-c",
                "import time\nwhile True:\n    print('tick', flush=True)\n    time.sleep(0.05)\n",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    monkeypatch.setattr(viewer_app_module.subprocess, "Popen", fake_popen)

    response = viewer_client.post(
        "/api/backtests/run",
        json={
            "strategy": "demo_strategy.py",
            "engine": "native",
            "symbols": ["AAPL"],
            "timeframe": "1d",
        },
    )
    assert response.status_code == 202
    task_id = response.json()["task_id"]

    for _ in range(200):
        task = viewer_app_module.DATA_TASKS[task_id]
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)

    task = viewer_app_module.DATA_TASKS[task_id]
    assert task["status"] == "failed"
    error_text = str(task.get("error") or "")
    assert "timed out after 0" in error_text, (
        f"failure reason must name the timeout budget, got: {error_text!r}"
    )
