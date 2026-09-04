"""Deadline-bounded streaming for the generated-strategy backtest child.

The streaming loop must enforce BACKTEST_TIMEOUT_SECONDS while the child
produces nothing: a blocking readline() only re-checks the deadline when a
line arrives, so a silent (or silent-after-partial-line) child hung forever.
The loop therefore select()s the raw fd and reads bounded chunks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

import runner.auto_agent as auto_agent

PROJECT_ROOT = auto_agent.PROJECT_ROOT


def _fake_proc(stdout, returncode=0):
    class _FakeProc:
        def __init__(self):
            self.stdout = stdout
            self.returncode = returncode
            self.killed = False

        def wait(self, timeout=None):
            if self.killed:
                return -9
            return self.returncode

        def kill(self):
            self.killed = True

        def poll(self):
            return -9 if self.killed else None

    return _FakeProc()


def test_run_backtest_kills_hung_child_after_timeout(monkeypatch):
    """A silent child (never a line) must be killed by the deadline."""
    created: dict = {}
    streaming_started = threading.Event()
    monkeypatch.setattr(auto_agent, "_backtest_timeout", lambda: 0.05)

    class _HangReader:
        # A real pipe the fake child never writes to and never closes: select()
        # wakes on nothing and the loop must keep re-checking the deadline.
        def __init__(self):
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            self._fd = read_fd
            self._write_fd = write_fd
            streaming_started.set()

        def fileno(self):
            return self._fd

        def close(self):
            os.close(self._fd)
            os.close(self._write_fd)

    proc = _fake_proc(_HangReader())
    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: proc)

    with pytest.raises(RuntimeError, match="timed out"):
        auto_agent._run_backtest(["python", "-m", "runner.cli", "run"], log=None)

    assert streaming_started.is_set(), "read loop must have started"
    assert proc.killed is True, "a silent child must be killed at the deadline"


def test_run_backtest_returns_output_on_success(monkeypatch):
    class _EagerReader:
        def __init__(self, data: bytes):
            read_fd, write_fd = os.pipe()
            os.write(write_fd, data)
            os.close(write_fd)
            os.set_blocking(read_fd, False)
            self._fd = read_fd

        def fileno(self):
            return self._fd

        def close(self):
            os.close(self._fd)

    proc = _fake_proc(_EagerReader(b"hello\nworld\n"))
    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: proc)

    returncode, output = auto_agent._run_backtest(["cmd"], log=None)

    assert returncode == 0
    assert output == "hello\nworld\n"


def test_run_backtest_raises_after_deadline_for_real_silent_subprocess(monkeypatch):
    """End-to-end: a real child that never writes is killed, not waited on."""
    monkeypatch.setattr(auto_agent, "_backtest_timeout", lambda: 2.0)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
    )

    def _popen_side_effect(*args, **kwargs):
        return proc

    monkeypatch.setattr(auto_agent.subprocess, "Popen", _popen_side_effect)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        auto_agent._run_backtest(["python", "-m", "runner.cli", "run"], log=None)
    elapsed = time.monotonic() - started

    assert elapsed < 30, f"a silent child must be killed by the deadline, took {elapsed:.1f}s"
    assert proc.poll() is not None, "child must be reaped"


def test_run_backtest_raises_after_deadline_for_partial_line_child(monkeypatch):
    """A child that writes an unterminated line and hangs must not wedge the
    read in a blocking readline() — the deadline still fires."""
    monkeypatch.setattr(auto_agent, "_backtest_timeout", lambda: 2.0)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(600)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
    )

    def _popen_side_effect(*args, **kwargs):
        return proc

    monkeypatch.setattr(auto_agent.subprocess, "Popen", _popen_side_effect)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        auto_agent._run_backtest(["python", "-m", "runner.cli", "run"], log=None)
    elapsed = time.monotonic() - started

    assert elapsed < 30, f"a partial-line child must not wedge the reader, took {elapsed:.1f}s"
    assert proc.poll() is not None, "child must be reaped"


def test_run_backtest_returns_completed_output_for_fast_real_subprocess(monkeypatch):
    """A normal child's lines (and a trailing fragment without a newline) are
    returned verbatim."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('line-a'); print('line-b'); sys.stdout.write('tail')",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(PROJECT_ROOT),
    )
    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: proc)

    returncode, output = auto_agent._run_backtest(["cmd"], log=None)

    assert returncode == 0
    assert "line-a" in output
    assert "line-b" in output
    assert output.endswith("tail")
