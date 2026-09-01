from __future__ import annotations

import pytest

from runner.strategy_ir.codegen_vectorbt import generate_vectorbt_strategy_code
from runner.strategy_ir.models import (
    Condition,
    ConditionWhen,
    EngineHint,
    ExitSignal,
    ExitSignalKind,
    StrategyIR,
)


def test_unmatched_entry_condition_fails_loudly():
    strategy_ir = StrategyIR(
        name="Prose Demo",
        indicators=[],
        entry_conditions=[
            Condition(expression="buy when the moon is full", when=ConditionWhen.ENTRY)
        ],
        exit_signals=[],
        confidence=1.0,
        engine_hint=EngineHint.VECTORBT,
    )

    with pytest.raises(ValueError, match="moon is full"):
        generate_vectorbt_strategy_code(strategy_ir)


def test_unmatched_exit_condition_fails_loudly():
    strategy_ir = StrategyIR(
        name="Prose Demo",
        indicators=[],
        entry_conditions=[],
        exit_signals=[
            ExitSignal(
                name="exit_1",
                condition="sell when the vibes are off",
                kind=ExitSignalKind.CUSTOM,
            )
        ],
        confidence=1.0,
        engine_hint=EngineHint.VECTORBT,
    )

    with pytest.raises(ValueError, match="vibes are off"):
        generate_vectorbt_strategy_code(strategy_ir)
