"""Tests for the autonomous NVIDIA NIM strategy conversion worker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import runner.auto_agent as auto_agent


def test_run_auto_agent_writes_strategy_docs_and_backtest_output(tmp_path, monkeypatch):
    prompt_path = tmp_path / "strategy-conversion.md"
    prompt_path.write_text("System prompt")

    strategies_dir = tmp_path / "strategies"
    backtests_dir = tmp_path / "backtests"

    monkeypatch.setattr(auto_agent, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(auto_agent, "PROMPT_PATH", prompt_path)
    monkeypatch.setattr(auto_agent, "STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(auto_agent, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(
        auto_agent,
        "_resolve_backtest_target",
        lambda **_: auto_agent.BacktestTarget(
            symbols=["AAPL"],
            timeframe="1h",
            start="2024-01-01",
            end="2024-01-31",
        ),
    )
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")

    response_text = """```python
STRATEGY_META = {
    "name": "Demo Agent",
    "version": "1.0.0",
    "tags": {"type": ["momentum"], "source": "test", "asset_class": ["equities"], "complexity": "simple"},
}

def generate_signals(df, params):
    entries = df["close"] > df["close"].rolling(2).mean()
    exits = df["close"] < df["close"].rolling(2).mean()
    indicators_dict = {"sma_2": df["close"].rolling(2).mean()}
    return entries.fillna(False), exits.fillna(False), indicators_dict
```
```markdown
# Demo Agent

## Framework-Empfehlung
vectorbt
```"""

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))]
                    )
                )
            )

    monkeypatch.setattr(auto_agent, "OpenAI", FakeClient)

    def fake_run_backtest(cmd: list[str], *, log=None):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Demo Agent",
        log=lambda *args, **kwargs: None,
    )

    assert result["engine"] == "vectorbt"
    assert result["symbols"] == ["AAPL"]
    assert result["timeframe"] == "1h"
    strategy_file = Path(result["strategy_file"])
    documentation_file = Path(result["documentation_file"])
    assert strategy_file.exists()
    assert documentation_file.exists()
    assert strategy_file.parent.name == strategy_file.stem
    assert documentation_file.name == "README.md"
    assert Path(result["backtest_output"]).exists()


def test_run_auto_agent_sets_timeout_and_logs_for_long_code(tmp_path, monkeypatch):
    prompt_path = tmp_path / "strategy-conversion.md"
    prompt_path.write_text("System prompt")

    strategies_dir = tmp_path / "strategies"
    backtests_dir = tmp_path / "backtests"

    monkeypatch.setattr(auto_agent, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(auto_agent, "PROMPT_PATH", prompt_path)
    monkeypatch.setattr(auto_agent, "STRATEGIES_DIR", strategies_dir)
    monkeypatch.setattr(auto_agent, "BACKTESTS_DIR", backtests_dir)
    monkeypatch.setattr(
        auto_agent,
        "_resolve_backtest_target",
        lambda **_: auto_agent.BacktestTarget(
            symbols=["AAPL"],
            timeframe="1h",
            start="2024-01-01",
            end="2024-01-31",
        ),
    )
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")

    response_text = """```python
STRATEGY_META = {"name": "Long Agent"}

def generate_signals(df, params):
    return df["close"] > df["close"], df["close"] < df["close"], {"demo": df["close"]}
```
```markdown
## Framework-Empfehlung
vectorbt
```"""

    captured_client_kwargs: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            captured_client_kwargs.update(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **_: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=response_text))]
                    )
                )
            )

    monkeypatch.setattr(auto_agent, "OpenAI", FakeClient)

    def fake_run_backtest(cmd: list[str], *, log=None):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    logged_messages: list[str] = []

    auto_agent.run_auto_agent(
        "\n".join(f"line {idx}" for idx in range(1400)),
        strategy_name="Long Agent",
        log=lambda message, **kwargs: logged_messages.append(message),
    )

    assert captured_client_kwargs["timeout"] == 300
    assert any(
        "Code is very long (1400 lines). Expect longer generation time." in message
        for message in logged_messages
    )
