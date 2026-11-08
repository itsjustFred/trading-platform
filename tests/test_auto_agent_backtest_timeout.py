"""Subprocess-timeout tests for generated-strategy execution (F25 facet).

Seam under test: ``runner.auto_agent._run_backtest`` and the backtest child
spawned by ``run_auto_agent`` must be bounded by a wall-clock timeout so a
hanging generated strategy cannot wedge a worker thread forever.
"""

from __future__ import annotations

import pytest

import runner.auto_agent as auto_agent


class FakeProc:
    def __init__(self, *, timeout_on_wait: bool = False, output: str = ""):
        self.stdout = iter([])
        self._timeout_on_wait = timeout_on_wait
        self._output = output
        self.killed = False
        self.returncode = 0

    def poll(self):
        return 0 if not self._timeout_on_wait or self.killed else None

    def wait(self, timeout=None):
        if self._timeout_on_wait and not self.killed:
            raise auto_agent.subprocess.TimeoutExpired(cmd=["x"], timeout=timeout)
        return 0

    def communicate(self, timeout=None):
        # Mirrors subprocess.communicate: raises TimeoutExpired while the child
        # is still running, and after kill()+wait() the buffered output is read.
        self.wait(timeout=timeout)
        return self._output, ""

    def kill(self):
        self.killed = True


def test_backtest_timeout_is_defined_and_reasonable():
    assert auto_agent.BACKTEST_TIMEOUT_SECONDS >= 60


def test_agent_process_backtest_child_uses_filtered_environment(monkeypatch):
    from runner.agent_process import build_backtest_child_env

    monkeypatch.setenv("NIM_API_KEY", "secret-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("TEMP", r"C:\Windows\Temp")

    env = build_backtest_child_env()

    assert "NIM_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["SystemRoot"] == r"C:\Windows"
    assert env["COMSPEC"] == r"C:\Windows\System32\cmd.exe"
    assert env["TEMP"] == r"C:\Windows\Temp"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_run_backtest_kills_child_on_timeout(monkeypatch):
    proc = FakeProc(timeout_on_wait=True)
    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: proc)

    with pytest.raises(RuntimeError, match="timed out"):
        auto_agent._run_backtest(["python", "-c", "hang"], log=None)

    assert proc.killed


def test_run_backtest_streaming_is_bounded_by_the_timeout(monkeypatch):
    """The timeout must cover output streaming, not just the final wait.

    A generated strategy that hangs while holding the pipe open would block a
    plain read loop forever; communicate() is what keeps the wall-clock bound
    across the whole child lifetime.
    """

    class HangingStreamProc(FakeProc):
        def __init__(self):
            super().__init__(timeout_on_wait=True)
            # stdout that would block forever if iterated directly
            self.stdout = (line for line in _infinite_lines())

        def communicate(self, timeout=None):
            # Simulate communicate(): never iterates stdout without a timeout
            self.wait(timeout=timeout)
            return "", ""

    def _infinite_lines():
        while True:
            yield "progress\n"

    proc = HangingStreamProc()
    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: proc)

    with pytest.raises(RuntimeError, match="timed out"):
        auto_agent._run_backtest(["python", "-c", "hang"], log=None)

    assert proc.killed


def test_run_backtest_returns_output_on_success(monkeypatch):
    proc = FakeProc(output="line one\nline two\n")
    monkeypatch.setattr(auto_agent.subprocess, "Popen", lambda *a, **k: proc)

    returncode, output = auto_agent._run_backtest(["python", "-c", "ok"], log=None)
    assert returncode == 0
    assert "line one" in output


def test_timeout_helper_raises_runtime_error_with_the_real_limit():
    """The kill-and-report path must not blow up with its own NameError.

    `_timeout` builds the message for the operator. Referencing an undefined
    name there replaced the intended timeout RuntimeError with a NameError, so
    the caller saw "name '_backtest_timeout' is not defined" instead of which
    command hung and for how long.
    """
    proc = FakeProc()
    with pytest.raises(RuntimeError, match="timed out after"):
        auto_agent._timeout(proc, ["python", "-c", "hang"])
    assert proc.killed
    with pytest.raises(RuntimeError, match="python -c hang"):
        auto_agent._timeout(FakeProc(), ["python", "-c", "hang"])
