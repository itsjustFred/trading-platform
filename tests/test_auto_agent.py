"""Tests for the autonomous NVIDIA NIM strategy conversion worker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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

BROKEN_PRECHECK_STEP_THREE_CODE = """import pandas as pd


def helper(df: pd.DataFrame, params: dict):
    return df
"""

INVALID_SYNTAX_STEP_THREE_CODE = """def generate_signals(df, params)
    return None
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

    assert len(llm_calls) == 2
    assert result["engine"] == "vectorbt"
    assert result["symbols"] == ["AAPL"]
    assert result["timeframe"] == "1h"
    assert result["attempts"] == 1
    assert result["rollout_mode"] == "ir-first"
    assert result["codegen_path"] == "ir:vectorbt"

    strategy_file = Path(result["strategy_file"])
    documentation_file = Path(result["documentation_file"])
    assert strategy_file.exists()
    assert documentation_file.exists()
    assert strategy_file.parent.name == strategy_file.stem
    assert documentation_file.name == "README.md"
    assert Path(result["backtest_output"]).exists()
    assert "ir_file" in result
    assert Path(result["ir_file"]).exists()

    strategy_text = strategy_file.read_text()
    assert 'STRATEGY_META = {' in strategy_text
    assert '"""Auto-generated deterministic vectorbt strategy from StrategyIR."""' in strategy_text
    assert "def generate_signals" in strategy_text
    assert documentation_file.read_text().startswith("# Demo Agent")

    ir_text = Path(result["ir_file"]).read_text(encoding="utf-8")
    assert '"name": "Demo Agent"' in ir_text
    assert '"engine_hint": "vectorbt"' in ir_text



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
    assert any(
        "✅ Step 3/3: Deterministic vectorbt code generated from IR." in message
        for message in logged_messages
    )



def test_run_auto_agent_retries_only_step_three_on_validation_failure(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, FIXED_STEP_THREE_CODE],
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
    assert len(llm_calls) == 3
    assert "The Analyst" in llm_calls[0]["messages"][0]["content"]
    assert "The Architect" in llm_calls[1]["messages"][0]["content"]
    assert "The Coder" in llm_calls[2]["messages"][0]["content"]
    assert "Traceback: boom" in llm_calls[2]["messages"][1]["content"]


def test_run_auto_agent_falls_back_when_deterministic_codegen_fails(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
        captured_calls=llm_calls,
    )

    def fake_run_backtest(cmd: list[str], *, log=None):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    def _raise_codegen(_strategy_ir):
        raise RuntimeError("deterministic codegen failed")

    monkeypatch.setattr(auto_agent, "generate_vectorbt_strategy_code", _raise_codegen)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Deterministic Fallback Agent",
        log=lambda *args, **kwargs: None,
    )

    assert Path(result["strategy_file"]).exists()
    assert Path(result["backtest_output"]).exists()
    assert result["rollout_mode"] == "ir-first"
    assert result["codegen_path"] == "legacy-llm"
    assert len(llm_calls) == 3
    assert "The Coder" in llm_calls[2]["messages"][0]["content"]


def test_run_auto_agent_rollout_legacy_mode_skips_ir_codegen(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_AGENT_IR_ROLLOUT", "legacy")
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
        captured_calls=llm_calls,
    )

    def _should_not_run_codegen(_strategy_ir):
        raise AssertionError("Deterministic generator must not run in legacy rollout mode")

    monkeypatch.setattr(auto_agent, "generate_vectorbt_strategy_code", _should_not_run_codegen)

    def fake_run_backtest(cmd: list[str], *, log=None):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Legacy Rollout Agent",
        log=lambda *args, **kwargs: None,
    )

    assert Path(result["strategy_file"]).exists()
    assert Path(result["backtest_output"]).exists()
    assert result["rollout_mode"] == "legacy"
    assert result["codegen_path"] == "legacy-llm"
    assert len(llm_calls) == 3


def test_run_auto_agent_rollout_ir_only_disables_legacy_fallback(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_AGENT_IR_ROLLOUT", "ir-only")
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META],
        captured_calls=llm_calls,
    )

    def _raise_codegen(_strategy_ir):
        raise RuntimeError("deterministic codegen failed")

    monkeypatch.setattr(auto_agent, "generate_vectorbt_strategy_code", _raise_codegen)

    backtest_calls = 0

    def fake_run_backtest(cmd: list[str], *, log=None):
        nonlocal backtest_calls
        backtest_calls += 1
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    with pytest.raises(RuntimeError, match=r"ir-only rollout mode"):
        auto_agent.run_auto_agent(
            "strategy.entry('Long', strategy.long)",
            strategy_name="IR Only Agent",
            log=lambda *args, **kwargs: None,
        )

    assert backtest_calls == 0
    assert len(llm_calls) == 2


def test_run_auto_agent_rollout_ir_only_requires_supported_ir_route(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    monkeypatch.setenv("AUTO_AGENT_IR_ROLLOUT", "ir-only")
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META],
        captured_calls=llm_calls,
    )

    backtest_calls = 0

    def fake_run_backtest(cmd: list[str], *, log=None):
        nonlocal backtest_calls
        backtest_calls += 1
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    with pytest.raises(RuntimeError, match=r"route is unavailable"):
        auto_agent.run_auto_agent(
            "strategy.entry('Long', strategy.long)",
            strategy_name="IR Only Unsupported Agent",
            requested_engine="pynecore",
            log=lambda *args, **kwargs: None,
        )

    assert backtest_calls == 0
    assert len(llm_calls) == 2


def test_run_auto_agent_uses_legacy_codegen_when_engine_has_no_ir_generator(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
        captured_calls=llm_calls,
    )

    def fake_run_backtest(cmd: list[str], *, log=None):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Pynecore Legacy Route Agent",
        requested_engine="pynecore",
        log=lambda *args, **kwargs: None,
    )

    assert result["engine"] == "pynecore"
    assert Path(result["strategy_file"]).exists()
    assert Path(result["backtest_output"]).exists()
    assert len(llm_calls) == 3
    assert "The Coder" in llm_calls[2]["messages"][0]["content"]


def test_run_auto_agent_preflight_retries_before_backtest(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [
            STEP_ONE_MARKDOWN,
            STEP_TWO_META,
            BROKEN_PRECHECK_STEP_THREE_CODE,
            FIXED_STEP_THREE_CODE,
        ],
        captured_calls=llm_calls,
    )

    def _raise_codegen(_strategy_ir):
        raise RuntimeError("deterministic codegen failed")

    monkeypatch.setattr(auto_agent, "generate_vectorbt_strategy_code", _raise_codegen)

    backtest_calls = 0

    def fake_run_backtest(cmd: list[str], *, log=None):
        nonlocal backtest_calls
        backtest_calls += 1
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Preflight Retry Agent",
        max_iterations=4,
        log=lambda *args, **kwargs: None,
    )

    assert result["attempts"] == 3
    assert backtest_calls == 1
    assert len(llm_calls) == 4
    assert "PREFLIGHT[CONTRACT]" in llm_calls[3]["messages"][1]["content"]


def test_run_auto_agent_preflight_syntax_failure_never_launches_backtest(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls: list[dict[str, object]] = []
    _install_fake_client(
        monkeypatch,
        [
            STEP_ONE_MARKDOWN,
            STEP_TWO_META,
            INVALID_SYNTAX_STEP_THREE_CODE,
            INVALID_SYNTAX_STEP_THREE_CODE,
        ],
        captured_calls=llm_calls,
    )

    def _raise_codegen(_strategy_ir):
        raise RuntimeError("deterministic codegen failed")

    monkeypatch.setattr(auto_agent, "generate_vectorbt_strategy_code", _raise_codegen)

    backtest_calls = 0

    def fake_run_backtest(cmd: list[str], *, log=None):
        nonlocal backtest_calls
        backtest_calls += 1
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    with pytest.raises(RuntimeError, match=r"PREFLIGHT\[SYNTAX\]"):
        auto_agent.run_auto_agent(
            "strategy.entry('Long', strategy.long)",
            strategy_name="Preflight Syntax Agent",
            max_iterations=3,
            log=lambda *args, **kwargs: None,
        )

    assert backtest_calls == 0
    assert len(llm_calls) == 4
    assert "PREFLIGHT[SYNTAX]" in llm_calls[3]["messages"][1]["content"]


def test_run_auto_agent_continues_when_ir_seam_builder_fails(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    _install_fake_client(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
    )

    def fake_run_backtest(cmd: list[str], *, log=None):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    def _raise_ir_builder(**kwargs):
        raise RuntimeError("IR build failed")

    monkeypatch.setattr(auto_agent, "build_strategy_ir", _raise_ir_builder)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Legacy Fallback Agent",
        log=lambda *args, **kwargs: None,
    )

    assert Path(result["strategy_file"]).exists()
    assert Path(result["backtest_output"]).exists()
    assert "ir_file" not in result


def test_normalize_strategy_meta_coerces_list_shapes():
    normalized = auto_agent._normalize_strategy_meta(
        {
            "name": "List Shape Strategy",
            "parameters": [
                {"name": "fast", "default": 10},
                {"slow": 30},
            ],
            "tags": ["momentum", "forex"],
        },
        requested_name=None,
        markdown_doc="# Fallback Name",
    )

    assert normalized["name"] == "List Shape Strategy"
    assert normalized["parameters"] == {"fast": 10, "slow": 30}
    assert normalized["tags"] == {"labels": ["momentum", "forex"]}


def test_normalize_strategy_meta_parses_mapping_strings():
    normalized = auto_agent._normalize_strategy_meta(
        {
            "name": "String Shape Strategy",
            "parameters": "{'fast': 10, 'slow': 30}",
            "tags": '{"source": "unit-test", "type": ["trend"]}',
        },
        requested_name=None,
        markdown_doc="# Fallback Name",
    )

    assert normalized["parameters"] == {"fast": 10, "slow": 30}
    assert normalized["tags"] == {"source": "unit-test", "type": ["trend"]}


def test_normalize_strategy_meta_rejects_invalid_tags_type():
    with pytest.raises(TypeError, match=r"STRATEGY_META\['tags'\]"):
        auto_agent._normalize_strategy_meta(
            {
                "name": "Invalid Tags Strategy",
                "parameters": {"fast": 10},
                "tags": 123,
            },
            requested_name=None,
            markdown_doc="# Fallback Name",
        )
