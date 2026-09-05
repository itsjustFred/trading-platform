from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import runner.cli as runner_cli
from shared.models import StrategyType
from shared.strategy_registry import detect_strategy_type


def test_detect_strategy_type_from_meta_field(tmp_path: Path):
    strategy_path = tmp_path / "meta_signal_processor.py"
    strategy_path.write_text(
        "STRATEGY_META = {'name': 'Meta Signal', 'strategy_type': 'signal_processor'}\n",
        encoding="utf-8",
    )

    detected = detect_strategy_type(strategy_path, tmp_path)
    assert detected == StrategyType.SIGNAL_PROCESSOR


def test_detect_strategy_type_detects_signal_processor_class_statically(tmp_path: Path):
    """A *SignalProcessor class with process_signal is detected from the AST —
    the file is never executed, so dynamic/dangerous module code stays inert."""
    strategy_path = tmp_path / "class_signal_processor.py"
    strategy_path.write_text(
        "\n".join(
            [
                "class ExampleSignalProcessor:",
                "    def process_signal(self, raw_signal, market_data):",
                "        return raw_signal",
                "",
            ]
        ),
        encoding="utf-8",
    )

    detected = detect_strategy_type(strategy_path, tmp_path)
    assert detected == StrategyType.SIGNAL_PROCESSOR


def test_detect_strategy_type_static_detection_never_executes(tmp_path: Path):
    """Detection must not execute the strategy file (no import side effects)."""
    strategy_path = tmp_path / "side_effect_signal_processor.py"
    strategy_path.write_text(
        "\n".join(
            [
                "raise RuntimeError('strategy file must never be executed on lookup')",
                "class ExampleSignalProcessor:",
                "    def process_signal(self, raw_signal, market_data):",
                "        return raw_signal",
                "",
            ]
        ),
        encoding="utf-8",
    )

    detected = detect_strategy_type(strategy_path, tmp_path)
    assert detected == StrategyType.SIGNAL_PROCESSOR


def test_detect_strategy_type_defaults_to_market_data(tmp_path: Path):
    strategy_path = tmp_path / "market_data_strategy.py"
    strategy_path.write_text(
        "\n".join(
            [
                "def generate_signals(df, params):",
                "    return df['close'] > 0, df['close'] < 0, {'example': df['close']}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    detected = detect_strategy_type(strategy_path, tmp_path)
    assert detected == StrategyType.MARKET_DATA


def test_cmd_run_rejects_signal_processor_strategies(tmp_path: Path):
    strategy_path = tmp_path / "ramjet_processor.py"
    strategy_path.write_text(
        "\n".join(
            [
                (
                    "STRATEGY_META = {'name': 'RamJet Processor', "
                    "'strategy_type': 'signal_processor'}"
                ),
                "class RamJetSignalProcessor:",
                "    def process_signal(self, raw_signal, market_data):",
                "        return raw_signal",
                "",
            ]
        ),
        encoding="utf-8",
    )

    args = SimpleNamespace(
        engine="vectorbt",
        strategy=str(strategy_path),
        symbols=["AAPL"],
        timeframe="1d",
        start="2024-01-01",
        end="2024-02-01",
        capital=100000.0,
        risk_pct=None,
        max_position_pct=None,
        max_leverage=None,
        data_dir="",
        params="{}",
        name="ramjet",
        output=str(tmp_path / "out.trades.json"),
    )

    with pytest.raises(ValueError, match="signal_processor"):
        runner_cli.cmd_run(args)
