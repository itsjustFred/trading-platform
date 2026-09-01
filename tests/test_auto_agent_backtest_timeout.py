from __future__ import annotations

import subprocess

import pytest

import runner.auto_agent as auto_agent


def test_run_backtest_kills_hung_child_after_timeout(monkeypatch):
    created: dict = {}

    class FakeProc:
        def __init__(self):
            self.stdout = iter([])
            self.returncode = None
            created["proc"] = self

        def wait(self, timeout=None):
            if created.get("killed"):
                return -9
            if timeout is None:
                raise AssertionError("wait() must be called with a timeout")
            raise subprocess.TimeoutExpired(cmd="backtest", timeout=timeout)

        def kill(self):
            created["killed"] = True

    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: FakeProc())

    with pytest.raises(RuntimeError, match="timed out"):
        auto_agent._run_backtest(["python", "-m", "runner.cli", "run"], log=None)

    assert created["killed"] is True


def test_run_backtest_returns_output_on_success(monkeypatch):
    class FakeProc:
        def __init__(self):
            self.stdout = iter(["hello\n"])

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: FakeProc())

    returncode, output = auto_agent._run_backtest(["cmd"], log=None)

    assert returncode == 0
    assert "hello" in output
