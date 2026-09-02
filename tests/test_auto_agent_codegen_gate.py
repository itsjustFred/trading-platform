"""Auto-agent integration tests for the LLM codegen trust boundary (F25).

Seams under test (``runner.auto_agent.run_auto_agent``):
- generated code is statically screened before it is written or executed;
- generated files stay registry-inert until a SHA256-bound human approval
  exists (no backtest subprocess runs before approval);
- the last generated file is removed when validation fails so no broken,
  unreviewed code stays registry-runnable;
- slug collisions with an existing strategy are rejected instead of silently
  overwriting another strategy;
- the backtest child process runs with a filtered environment, not a copy of
  the parent environment incl. API keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import runner.auto_agent as auto_agent
from tests.test_auto_agent import (
    FIXED_STEP_THREE_CODE,
    STEP_ONE_MARKDOWN,
    STEP_THREE_CODE,
    STEP_TWO_META,
    _install_fake_provider,
    _setup_agent,
)


def _happy_backtest(cmd: list[str], *, log=None):
    output_path = Path(cmd[cmd.index("--output") + 1])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text('{"status":"ok"}')
    return 0, "ok"


def _run_conversion(
    monkeypatch,
    tmp_path,
    code: str = STEP_THREE_CODE,
    strategy_name: str = "Gate Agent",
    **kwargs,
):
    _setup_agent(monkeypatch, tmp_path)
    _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, code],
    )
    monkeypatch.setattr(auto_agent, "_run_backtest", _happy_backtest)
    return auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name=strategy_name,
        log=lambda *args, **kwargs: None,
        **kwargs,
    )


def test_generated_code_is_screened_before_execution(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    _install_fake_provider(
        monkeypatch,
        [
            STEP_ONE_MARKDOWN,
            STEP_TWO_META,
            "import subprocess\nsubprocess.run(['ls'])\n",
            # Retry attempts keep failing screening.
            "import subprocess\nsubprocess.run(['ls'])\n",
            "import subprocess\nsubprocess.run(['ls'])\n",
        ],
    )
    backtest_calls: list[list[str]] = []

    def spy_backtest(cmd: list[str], *, log=None):
        backtest_calls.append(cmd)
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", spy_backtest)

    with pytest.raises(RuntimeError, match="screening"):
        auto_agent.run_auto_agent(
            "strategy.entry('Long', strategy.long)",
            strategy_name="Unsafe Agent",
            log=lambda *args, **kwargs: None,
        )

    assert backtest_calls == []
    assert not (tmp_path / "strategies" / "unsafe_agent" / "unsafe_agent.py").exists()


def test_failed_backtest_removes_last_generated_file(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    _install_fake_provider(
        monkeypatch,
        [
            STEP_ONE_MARKDOWN,
            STEP_TWO_META,
            STEP_THREE_CODE,
            FIXED_STEP_THREE_CODE,
            FIXED_STEP_THREE_CODE,
        ],
    )
    monkeypatch.setattr(
        auto_agent, "_run_backtest", lambda cmd, *, log=None: (1, "Traceback: boom")
    )

    with pytest.raises(RuntimeError, match="Auto-agent failed"):
        auto_agent.run_auto_agent(
            "strategy.entry('Long', strategy.long)",
            strategy_name="Broken Agent",
            log=lambda *args, **kwargs: None,
        )

    strategy_dir = tmp_path / "strategies" / "broken_agent"
    assert not (strategy_dir / "broken_agent.py").exists()
    # The failed attempt leaves no runnable file and no orphaned slug directory
    # that would block a retry of the same slug.
    assert not strategy_dir.exists()


def test_retry_of_failed_slug_is_not_blocked_by_leftovers(tmp_path, monkeypatch):
    """A failed attempt removes its slug directory — a retry of the same slug
    is not permanently blocked by leftovers of the failed attempt."""
    _setup_agent(monkeypatch, tmp_path)
    _install_fake_provider(
        monkeypatch,
        [
            STEP_ONE_MARKDOWN,
            STEP_TWO_META,
            STEP_THREE_CODE,
            # Attempts 2 and 3 only re-ask for the code step (md/meta are reused).
            FIXED_STEP_THREE_CODE,
            FIXED_STEP_THREE_CODE,
        ],
    )
    monkeypatch.setattr(
        auto_agent, "_run_backtest", lambda cmd, *, log=None: (1, "Traceback: boom")
    )
    with pytest.raises(RuntimeError, match="Auto-agent failed"):
        auto_agent.run_auto_agent(
            "strategy.entry('Long', strategy.long)",
            strategy_name="Retry Agent",
            log=lambda *args, **kwargs: None,
        )

    _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, FIXED_STEP_THREE_CODE],
    )
    monkeypatch.setattr(auto_agent, "_run_backtest", _happy_backtest)
    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Retry Agent",
        log=lambda *args, **kwargs: None,
    )
    assert result["attempts"] == 1
    assert Path(result["strategy_file"]).exists()


def test_validated_strategy_is_not_approved_until_human_review(tmp_path, monkeypatch):
    result = _run_conversion(monkeypatch, tmp_path)
    strategy_file = Path(result["strategy_file"])
    backtest_cmd: list[str] = []

    def recorded_backtest(cmd: list[str], *, log=None):
        backtest_cmd.append(cmd)
        return _happy_backtest(cmd, log=log)

    monkeypatch.setattr(auto_agent, "_run_backtest", recorded_backtest)

    from runner.approval_gate import StrategyNotApprovedError, approve_strategy, is_strategy_approved

    assert not is_strategy_approved(strategy_file, strategies_root=tmp_path / "strategies")

    def _run_gate() -> None:
        auto_agent.run_validated_backtest(
            strategy_file,
            engine=result["engine"],
            target=auto_agent.BacktestTarget(
                symbols=["AAPL"], timeframe="1h", start="2024-01-01", end="2024-01-31"
            ),
            output_path=Path(result["backtest_output"]),
            log=lambda *args, **kwargs: None,
        )

    with pytest.raises(StrategyNotApprovedError):
        _run_gate()
    assert backtest_cmd == []

    approve_strategy(strategy_file, approved_by="reviewer", strategies_root=tmp_path / "strategies")
    _run_gate()
    assert len(backtest_cmd) == 1


def test_slug_collision_is_rejected_without_overwriting(tmp_path, monkeypatch):
    result = _run_conversion(monkeypatch, tmp_path)
    original = Path(result["strategy_file"]).read_text()

    with pytest.raises(ValueError, match="already exists"):
        _run_conversion(monkeypatch, tmp_path, strategy_name="Gate Agent")
    assert Path(result["strategy_file"]).read_text() == original


def test_backtest_child_gets_filtered_environment(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    _setup_agent(monkeypatch, tmp_path)
    _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
    )

    def spy_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        captured["cmd"] = cmd

        class FakeProc:
            def __init__(self):
                self.stdout = iter(["ok\n"])
                self.returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def communicate(self, timeout=None):
                return "ok\n", ""

            def kill(self):
                return None

        proc = FakeProc()
        output = Path(cmd[cmd.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"status":"ok"}')
        return proc

    monkeypatch.setattr(auto_agent.subprocess, "Popen", spy_popen)
    monkeypatch.setenv("NIM_API_KEY", "secret-key")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Env Agent",
        log=lambda *args, **kwargs: None,
    )

    assert result["attempts"] == 1
    env = captured["env"]
    assert "NIM_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PYTHONUNBUFFERED"] == "1"

