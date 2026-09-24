"""Tests for the autonomous strategy conversion worker."""

from __future__ import annotations

from pathlib import Path

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


def _install_fake_provider(monkeypatch, responses, captured_kwargs=None, captured_calls=None):
    queue = list(responses)
    recorded_kwargs = captured_kwargs if captured_kwargs is not None else {}
    recorded_calls = captured_calls if captured_calls is not None else []

    class FakeProvider:
        def generate(self, **kwargs):
            recorded_calls.append(kwargs)
            if not queue:
                raise AssertionError("No fake responses left for auto_agent call")
            return queue.pop(0)

    def _factory(provider_name: str, **kwargs):
        recorded_kwargs["provider_name"] = provider_name
        recorded_kwargs.update(kwargs)
        return FakeProvider()

    monkeypatch.setattr(auto_agent, "get_llm_provider", _factory)
    return recorded_calls


def test_run_auto_agent_writes_screened_draft_without_execution(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls = _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
    )
    backtest_calls: list[list[str]] = []
    monkeypatch.setattr(
        auto_agent,
        "_run_backtest",
        lambda cmd, *, log=None: backtest_calls.append(cmd) or (0, "ok"),
    )

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Demo Agent",
        log=lambda *args, **kwargs: None,
    )

    assert len(llm_calls) == 3
    assert result["status"] == "awaiting_approval"
    assert result["approval_required"] is True
    assert result["validation"] == "not_run"
    assert result["backtest_output"] == ""
    assert result["engine"] == "vectorbt"
    assert result["symbols"] == ["AAPL"]
    assert result["timeframe"] == "1h"
    assert result["attempts"] == 1
    assert backtest_calls == []

    strategy_file = Path(result["strategy_file"])
    documentation_file = Path(result["documentation_file"])
    assert strategy_file.exists()
    assert documentation_file.exists()
    assert strategy_file.parent.name == strategy_file.stem
    assert documentation_file.name == "README.md"

    strategy_text = strategy_file.read_text()
    assert 'STRATEGY_META = {' in strategy_text
    assert '"""Generated strategy."""' in strategy_text
    assert "def generate_signals" in strategy_text
    assert documentation_file.read_text().startswith("# Demo Agent")




def test_run_auto_agent_sets_timeout_and_logs_for_long_code(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    captured_provider_kwargs: dict[str, object] = {}
    _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
        captured_kwargs=captured_provider_kwargs,
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

    assert captured_provider_kwargs["provider_name"] == "nim"
    assert captured_provider_kwargs["timeout_seconds"] == 600.0
    assert any(
        "Code is very long (1400 lines). Expect longer generation time." in message
        for message in logged_messages
    )
    assert any("✅ Step 1/3: Markdown Documentation created." in message for message in logged_messages)
    assert any("✅ Step 2/3: Strategy Meta defined." in message for message in logged_messages)
    assert any("✅ Step 3/3: Python Code generated." in message for message in logged_messages)



def test_run_auto_agent_retries_only_step_three_after_screening_failure(tmp_path, monkeypatch):
    _setup_agent(monkeypatch, tmp_path)
    llm_calls: list[dict[str, object]] = []
    _install_fake_provider(
        monkeypatch,
        [
            STEP_ONE_MARKDOWN,
            STEP_TWO_META,
            "import subprocess\nsubprocess.run(['ls'])\n",
            FIXED_STEP_THREE_CODE,
        ],
        captured_calls=llm_calls,
    )
    backtest_calls: list[list[str]] = []
    monkeypatch.setattr(
        auto_agent,
        "_run_backtest",
        lambda cmd, *, log=None: backtest_calls.append(cmd) or (0, "ok"),
    )

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Retry Agent",
        log=lambda *args, **kwargs: None,
    )

    assert result["attempts"] == 2
    assert result["status"] == "awaiting_approval"
    assert backtest_calls == []
    assert len(llm_calls) == 4
    assert "The Analyst" in str(llm_calls[0]["system_prompt"])
    assert "The Architect" in str(llm_calls[1]["system_prompt"])
    assert "The Coder" in str(llm_calls[2]["system_prompt"])
    assert "The Coder" in str(llm_calls[3]["system_prompt"])
    assert "screening" in str(llm_calls[3]["prompt"])


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


def test_run_auto_agent_writes_artifacts_inside_patched_dirs(tmp_path, monkeypatch):
    """Regression (pre-mortem PM-1): generated strategies must land inside the
    patched directories, never in the live repo registry — even when the
    artifact/path helpers live in different modules than the patch seam."""
    _setup_agent(monkeypatch, tmp_path)
    _install_fake_provider(
        monkeypatch,
        [STEP_ONE_MARKDOWN, STEP_TWO_META, STEP_THREE_CODE],
    )

    def fake_run_backtest(cmd: list[str], *, log=None, project_root=None):
        output_path = Path(cmd[cmd.index("--output") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"status":"ok"}')
        return 0, "ok"

    monkeypatch.setattr(auto_agent, "_run_backtest", fake_run_backtest)

    result = auto_agent.run_auto_agent(
        "strategy.entry('Long', strategy.long)",
        strategy_name="Seam Agent",
        log=lambda *args, **kwargs: None,
    )

    strategy_file = Path(result["strategy_file"])
    documentation_file = Path(result["documentation_file"])
    assert result["backtest_output"] == ""
    assert strategy_file.is_relative_to(tmp_path), f"strategy leaked: {strategy_file}"
    assert documentation_file.is_relative_to(tmp_path), f"docs leaked: {documentation_file}"

    # The CLI command must be launched with the strategy path relative to the
    # SAME root the subprocess runs in (pre-mortem PM-1 masking defect).
    strategy_file.relative_to(tmp_path)


def test_normalize_markdown_doc_and_extract_engine_roundtrip():
    """Contract sanity for the parsing module split (16-D2): the helpers moved
    out of auto_agent must behave identically to their old behavior."""
    from runner.agent_response_parsing import extract_engine, normalize_markdown_doc

    doc = normalize_markdown_doc("```markdown\n# T\n\nbody\n```", strategy_name=None)
    assert doc.startswith("# T")
    assert extract_engine("## Framework-Empfehlung\npynecore", requested_engine=None) == "pynecore"
    assert extract_engine("no engine here", requested_engine=None) == "vectorbt"


def test_run_backtest_kills_child_on_timeout(tmp_path):
    """CodeRabbit round (16-D2): a wedged validation backtest must be killed,
    not hang the auto-agent loop forever."""
    import sys

    from runner.agent_process import run_backtest

    script = tmp_path / "sleepy.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(60)\n")
    cmd = [sys.executable, str(script)]

    returncode, output = run_backtest(
        cmd, log=lambda *a, **k: None, project_root=tmp_path, timeout_seconds=2
    )

    assert returncode == 124
    assert "started" in output
