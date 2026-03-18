"""Tests for the autonomous NVIDIA NIM strategy conversion worker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import runner.auto_agent as auto_agent


STEP_ONE_MARKDOWN = """# Demo Agent

## Quelle
- Original: test

## Kernlogik
### Entry
Fast SMA crosses above slow SMA.

### Exit
Fast SMA crosses below slow SMA.

### Stop-Loss
No dedicated stop-loss.

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| SMA | fast | 10 |
| SMA | slow | 30 |

## Risk-Management
- Position Sizing: fixed risk per trade

## Framework-Empfehlung
vectorbt
"""

STEP_TWO_META = """STRATEGY_META = {
    "name": "Demo Agent",
    "version": "1.0.0",
    "parameters": {"fast": 10, "slow": 30},
    "tags": {
        "type": ["momentum"],
        "source": "test",
        "asset_class": ["equities"],
        "complexity": "simple",
    },
}
"""

STEP_THREE_CODE = """\"\"\"Generated strategy.\"\"\"

import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    fast_period = params.get("fast", 10)
    slow_period = params.get("slow", 30)
    fast = df["close"].rolling(fast_period).mean()
    slow = df["close"].rolling(slow_period).mean()
    entries = ((fast > slow) & (fast.shift(1) <= slow.shift(1))).fillna(False)
    exits = ((fast < slow) & (fast.shift(1) >= slow.shift(1))).fillna(False)
    indicators_dict = {"fast_sma": fast, "slow_sma": slow}
    return entries, exits, indicators_dict
"""

FIXED_STEP_THREE_CODE = """import pandas as pd


def generate_signals(df: pd.DataFrame, params: dict):
    fast = df["close"].rolling(params.get("fast", 10)).mean()
    slow = df["close"].rolling(params.get("slow", 30)).mean()
    entries = ((fast > slow) & (fast.shift(1) <= slow.shift(1))).fillna(False)
    exits = ((fast < slow) & (fast.shift(1) >= slow.shift(1))).fillna(False)
    indicators_dict = {"fast_sma": fast, "slow_sma": slow}
    return entries, exits, indicators_dict
"""


def _setup_agent(monkeypatch, tmp_path):
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


def _install_fake_client(monkeypatch, responses, captured_kwargs=None, captured_calls=None):
    queue = list(responses)
    recorded_kwargs = captured_kwargs if captured_kwargs is not None else {}
    recorded_calls = captured_calls if captured_calls is not None else []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            recorded_kwargs.update(kwargs)
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create),
            )

        def _create(self, **kwargs):
            recorded_calls.append(kwargs)
            if not queue:
                raise AssertionError("No fake responses left for auto_agent call")
            content = queue.pop(0)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    monkeypatch.setattr(auto_agent, "OpenAI", FakeClient)
    return recorded_calls


def test_run_auto_agent_writes_strategy_docs_and_backtest_output(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls = _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
    )

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

    assert len(llm_calls) == 3
    assert result["engine"] == "vectorbt"
    assert result["symbols"] == ["AAPL"]
    assert result["timeframe"] == "1h"
    assert result["attempts"] == 1

    strategy_file = Path(result["strategy_file"])
    documentation_file = Path(result["documentation_file"])
    assert strategy_file.exists()
    assert documentation_file.exists()
    assert strategy_file.parent.name == strategy_file.stem
    assert documentation_file.name == "README.md"
    assert Path(result["backtest_output"]).exists()

    strategy_text = strategy_file.read_text()
    assert 'STRATEGY_META = {' in strategy_text
    assert '"""Generated strategy."""' in strategy_text
    assert "def generate_signals" in strategy_text
    assert documentation_file.read_text().startswith("# Demo Agent")



def test_run_auto_agent_sets_timeout_and_logs_for_long_code(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    captured_client_kwargs: dict[str, object] = {}
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
        captured_kwargs=captured_client_kwargs,
    )

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

    assert captured_client_kwargs["timeout"] == 600.0
    assert any(
        "Code is very long (1400 lines). Expect longer generation time." in message
        for message in logged_messages
    )
    assert any("✅ Step 1/3: Markdown Documentation created." in message for message in logged_messages)
    assert any("✅ Step 2/3: Strategy Meta defined." in message for message in logged_messages)
    assert any("✅ Step 3/3: Python Code generated." in message for message in logged_messages)



def test_run_auto_agent_retries_only_step_three_on_validation_failure(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE, FIXED_STEP_THREE_CODE],
        captured_calls=llm_calls,
    )

    backtest_calls = 0

    def fake_run_backtest(cmd: list[str], *, log=None):
        nonlocal backtest_calls
        backtest_calls += 1
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if backtest_calls == 1:
            return 1, "Traceback: boom"
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Retry Agent",
        log=lambda *args, **kwargs: None,
    )

    assert result["attempts"] == 2
    assert backtest_calls == 2
    assert len(llm_calls) == 4
    assert "The Analyst" in llm_calls[0]["messages"][0]["content"]
    assert "The Architect" in llm_calls[1]["messages"][0]["content"]
    assert "The Coder" in llm_calls[2]["messages"][0]["content"]
    assert "The Coder" in llm_calls[3]["messages"][0]["content"]
    assert "Traceback: boom" in llm_calls[3]["messages"][1]["content"]


def test_normalize_strategy_meta_accepts_list_tags():
    normalized = auto_agent._normalize_strategy_meta(
        {
            "name": "Demo Agent",
            "version": "1.0.0",
            "parameters": {"fast": 10},
            "tags": ["momentum", "equities"],
        },
        requested_name="Demo Agent",
        markdown_doc="# Demo Agent",
    )

    assert normalized["tags"] == {"type": ["momentum", "equities"]}


def test_normalize_strategy_meta_coerces_tag_field_types():
    normalized = auto_agent._normalize_strategy_meta(
        {
            "name": "Demo Agent",
            "parameters": {"fast": 10},
            "tags": {
                "type": "trend",
                "asset_class": "forex",
                "complexity": 2,
            },
        },
        requested_name="Demo Agent",
        markdown_doc="# Demo Agent",
    )

    assert normalized["tags"]["type"] == ["trend"]
    assert normalized["tags"]["asset_class"] == ["forex"]
    assert normalized["tags"]["complexity"] == "2"
