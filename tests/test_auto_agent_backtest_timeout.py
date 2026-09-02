from __future__ import annotations

import subprocess

import pytest

import runner.auto_agent as auto_agent


class _HangReader:
    """A stdout stand-in whose readline blocks forever: the deadline in
    _run_backtest must fire while streaming, not only during wait()."""

    def readline(self):
        import time

        time.sleep(3600)
        return ""


def test_run_backtest_kills_hung_child_after_timeout(monkeypatch):
    created: dict = {}
    streaming_started = __import__("threading").Event()
    # A short positive deadline: readline blocks well past it, proving the
    # deadline bounds the streaming loop (not just the final wait()).
    monkeypatch.setattr(auto_agent, "_backtest_timeout", lambda: 0.05)

    class _HangReader:
        def readline(self):
            streaming_started.set()
            # Return "" (EOF) after the deadline has passed; the loop must
            # detect the expired deadline before trusting the EOF.
            return ""

    class FakeProc:
        def __init__(self):
            self.stdout = _HangReader()
            self.returncode = None
            created["proc"] = self

        def wait(self, timeout=None):
            if created.get("killed"):
                return -9
            raise subprocess.TimeoutExpired(cmd="backtest", timeout=timeout)

        def kill(self):
            created["killed"] = True

        def poll(self):
            return None if not created.get("killed") else -9

    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: FakeProc())

    with pytest.raises(RuntimeError, match="timed out"):
        auto_agent._run_backtest(["python", "-m", "runner.cli", "run"], log=None)

    assert streaming_started.is_set(), "readline must have begun streaming"
    assert created["killed"] is True


def test_run_backtest_returns_output_on_success(monkeypatch):
    class _EagerReader:
        def __init__(self, lines):
            self._lines = list(lines)

        def readline(self):
            return self._lines.pop(0) if self._lines else ""

    class FakeProc:
        def __init__(self):
            self.stdout = _EagerReader(["hello\n"])

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: FakeProc())

    returncode, output = auto_agent._run_backtest(["cmd"], log=None)

    assert returncode == 0
    assert "hello" in output
