from __future__ import annotations

from runner.strategy_ir.builder import build_strategy_ir
from runner.strategy_ir.models import EngineHint


def test_build_strategy_ir_extracts_markdown_sections_and_reaches_full_confidence():
    markdown = """# Builder Demo

## Kernlogik
### Entry
- Fast EMA crossover Slow EMA

### Exit
- Fast EMA crossunder Slow EMA

## Indikator-Einstellungen
| Indikator | Parameter | Wert |
|-----------|-----------|------|
| Fast EMA | fast | 10 |
| Slow EMA | slow | 30 |
"""

    strategy_ir = build_strategy_ir(
        strategy_slug="builder_demo",
        engine="vectorbt",
        source_code="fast = ta.ema(close, 10)\nslow = ta.ema(close, 30)",
        markdown_doc=markdown,
        strategy_meta={
            "name": "Builder Demo",
            "version": "1.0.0",
            "parameters": {"fast": 10, "slow": 30},
            "tags": {"type": ["momentum"]},
        },
    )

    assert strategy_ir.name == "Builder Demo"
    assert strategy_ir.engine_hint == EngineHint.VECTORBT
    assert strategy_ir.confidence == 1.0
    assert [parameter.name for parameter in strategy_ir.parameters] == ["fast", "slow"]
    assert {indicator.name for indicator in strategy_ir.indicators} == {"Fast EMA", "Slow EMA"}
    assert len(strategy_ir.entry_conditions) == 1
    assert len(strategy_ir.exit_signals) == 1


def test_build_strategy_ir_falls_back_to_source_indicator_tokens():
    strategy_ir = build_strategy_ir(
        strategy_slug="source_fallback_demo",
        engine="vectorbt",
        source_code="trend = ta.ema(close, 20)\nosc = vbt.RSI.run(close, 14)",
        markdown_doc="# Source Fallback\n\n### Entry\n- Entry condition\n\n### Exit\n- Exit condition",
        strategy_meta={
            "name": "Source Fallback",
            "version": "1.0.0",
            "parameters": {},
            "tags": {},
        },
    )

    indicator_names = {indicator.name for indicator in strategy_ir.indicators}
    assert "ema" in indicator_names
    assert "RSI" in indicator_names


def test_build_strategy_ir_marks_unknown_engine_hint():
    strategy_ir = build_strategy_ir(
        strategy_slug="unknown_engine_demo",
        engine="custom_engine",
        source_code="pass",
        markdown_doc="# Unknown Engine\n\n### Entry\n- Entry\n\n### Exit\n- Exit",
        strategy_meta={
            "name": "Unknown Engine",
            "version": "1.0.0",
            "parameters": {},
            "tags": {},
        },
    )

    assert strategy_ir.engine_hint == EngineHint.UNKNOWN


def test_exit_kind_ignores_substring_matches() -> None:
    """C6: "laptop" is not a take-profit; word boundaries decide."""
    from runner.strategy_ir.builder import _infer_exit_kind
    from runner.strategy_ir.models import ExitSignalKind

    assert _infer_exit_kind("close the laptop lid") == ExitSignalKind.CLOSE
    assert _infer_exit_kind("take profit at 110") == ExitSignalKind.TAKE_PROFIT
    assert _infer_exit_kind("exit on tps cascade") == ExitSignalKind.CLOSE
