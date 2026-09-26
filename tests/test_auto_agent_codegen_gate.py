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


def test_failed_screening_removes_generated_artifacts(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    unsafe = "import subprocess\nsubprocess.run(['ls'])\n"
    _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, unsafe, unsafe, unsafe],
    )
    backtest_calls: list[list[str]] = []
    monkeypatch.setattr(
        auto_agent,
        "_run_backtest",
        lambda cmd, *, log=None: backtest_calls.append(cmd) or (0, "ok"),
    )

    with pytest.raises(RuntimeError, match="screening"):
        auto_agent.run_auto_agent(
            "strategy.entry('Long', strategy.long)",
            strategy_name="Broken Agent",
            log=lambda *args, **kwargs: None,
        )

    strategy_dir = tmp_path / "strategies" / "broken_agent"
    assert not (strategy_dir / "broken_agent.py").exists()
    assert not (strategy_dir / "broken_agent.py.generated.json").exists()
    assert backtest_calls == []
    assert not strategy_dir.exists()


def test_screening_retry_keeps_only_an_unapproved_draft(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    unsafe = "import subprocess\nsubprocess.run(['ls'])\n"
    _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, unsafe, FIXED_STEP_THREE_CODE],
    )
    backtest_calls: list[list[str]] = []
    monkeypatch.setattr(
        auto_agent,
        "_run_backtest",
        lambda cmd, *, log=None: backtest_calls.append(cmd) or (0, "ok"),
    )

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Mid Agent",
        log=lambda *args, **kwargs: None,
    )

    strategy_dir = tmp_path / "strategies" / "mid_agent"
    assert result["attempts"] == 2
    assert result["status"] == "awaiting_approval"
    assert backtest_calls == []
    assert (strategy_dir / "mid_agent.py").exists()
    assert (strategy_dir / "mid_agent.py.generated.json").exists()


def test_generated_draft_cannot_be_imported_before_approval(tmp_path, monkeypatch):
    result = _run_conversion(monkeypatch, tmp_path, strategy_name="Import Gate")
    strategy_file = Path(result["strategy_file"])

    from runner.approval_gate import StrategyNotApprovedError
    from shared.strategy_registry import load_strategy_module

    with pytest.raises(StrategyNotApprovedError):
        load_strategy_module(strategy_file, tmp_path / "strategies")


def test_approved_route_rescreens_before_execution(tmp_path, monkeypatch):
    result = _run_conversion(monkeypatch, tmp_path, strategy_name="Screen Gate")
    strategy_file = Path(result["strategy_file"])
    strategy_file.write_text("import subprocess\nsubprocess.run(['ls'])\n")

    from runner.approval_gate import StrategyNotApprovedError, approve_strategy

    approve_strategy(strategy_file, approved_by="reviewer", strategies_root=tmp_path / "strategies")
    backtest_calls: list[list[str]] = []
    monkeypatch.setattr(
        auto_agent,
        "_run_backtest",
        lambda cmd, *, log=None: backtest_calls.append(cmd) or (0, "ok"),
    )

    with pytest.raises(StrategyNotApprovedError, match="screening"):
        auto_agent.run_validated_backtest(
            strategy_file,
            engine=result["engine"],
            target=auto_agent.BacktestTarget(
                symbols=["AAPL"], timeframe="1h", start="2024-01-01", end="2024-01-31"
            ),
            output_path=tmp_path / "screened.trades.json",
            log=lambda *args, **kwargs: None,
        )

    assert backtest_calls == []


def test_validated_strategy_is_not_approved_until_human_review(tmp_path, monkeypatch):
    result = _run_conversion(monkeypatch, tmp_path)
    strategy_file = Path(result["strategy_file"])
    backtest_cmd: list[list[str]] = []
    output_path = tmp_path / "approved.trades.json"

    def recorded_backtest(cmd: list[str], *, log=None):
        backtest_cmd.append(cmd)
        return _happy_backtest(cmd, log=log)

    monkeypatch.setattr(auto_agent, "_run_backtest", recorded_backtest)

    from runner.approval_gate import (
        StrategyNotApprovedError,
        approve_strategy,
        is_strategy_approved,
    )

    assert not is_strategy_approved(strategy_file, strategies_root=tmp_path / "strategies")

    def _run_gate() -> None:
        auto_agent.run_validated_backtest(
            strategy_file,
            engine=result["engine"],
            target=auto_agent.BacktestTarget(
                symbols=["AAPL"], timeframe="1h", start="2024-01-01", end="2024-01-31"
            ),
            output_path=output_path,
            log=lambda *args, **kwargs: None,
        )

    with pytest.raises(StrategyNotApprovedError):
        _run_gate()
    assert backtest_cmd == []

    approve_strategy(strategy_file, approved_by="reviewer", strategies_root=tmp_path / "strategies")
    _run_gate()
    assert len(backtest_cmd) == 1
    assert output_path.exists()


def test_approved_validation_route_preserves_retry_behavior(tmp_path, monkeypatch):
    result = _run_conversion(monkeypatch, tmp_path, strategy_name="Retry Route")
    strategy_file = Path(result["strategy_file"])
    from runner.approval_gate import approve_strategy

    approve_strategy(strategy_file, approved_by="reviewer", strategies_root=tmp_path / "strategies")
    output_path = tmp_path / "retry.trades.json"
    calls = 0

    def fake_run(cmd: list[str], *, log=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            output_path.write_text('{"status":"ok"}', encoding="utf-8")
        return (1, "validation failed") if calls == 1 else (0, "ok")

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run)
    returncode, output = auto_agent.run_validated_backtest(
        strategy_file,
        engine=result["engine"],
        target=auto_agent.BacktestTarget(
            symbols=["AAPL"], timeframe="1h", start="2024-01-01", end="2024-01-31"
        ),
        output_path=output_path,
        max_attempts=2,
        retry=lambda error: None,
        log=lambda *args, **kwargs: None,
    )

    assert returncode == 0
    assert output == "ok"
    assert calls == 2


def test_slug_collision_is_rejected_without_overwriting(tmp_path, monkeypatch):
    result = _run_conversion(monkeypatch, tmp_path)
    original = Path(result["strategy_file"]).read_text()

    with pytest.raises(ValueError, match="already exists"):
        _run_conversion(monkeypatch, tmp_path, strategy_name="Gate Agent")
    assert Path(result["strategy_file"]).read_text() == original


def test_backtest_child_gets_filtered_environment(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    real_run_backtest = auto_agent._run_backtest
    result = _run_conversion(monkeypatch, tmp_path, strategy_name="Env Agent")
    strategy_file = Path(result["strategy_file"])
    monkeypatch.setattr(auto_agent, "_run_backtest", real_run_backtest)
    from runner.approval_gate import approve_strategy

    approve_strategy(strategy_file, approved_by="reviewer", strategies_root=tmp_path / "strategies")

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

    auto_agent.run_validated_backtest(
        strategy_file,
        engine=result["engine"],
        target=auto_agent.BacktestTarget(
            symbols=["AAPL"], timeframe="1h", start="2024-01-01", end="2024-01-31"
        ),
        output_path=tmp_path / "approved.trades.json",
        log=lambda *args, **kwargs: None,
    )

    env = captured["env"]
    assert "NIM_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["PYTHONUNBUFFERED"] == "1"

