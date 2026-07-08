from __future__ import annotations

import pandas as pd

from runner.strategy_ir.codegen_vectorbt import generate_vectorbt_strategy_code
from runner.strategy_ir.models import (
    Condition,
    ConditionWhen,
    EngineHint,
    ExitSignal,
    ExitSignalKind,
    IndicatorRef,
    ParameterSpec,
    ParameterValueType,
    StrategyIR,
)


def _sample_ir() -> StrategyIR:
    return StrategyIR(
        name="Deterministic Demo",
        parameters=[
            ParameterSpec(name="fast", value_type=ParameterValueType.INT, default=10),
            ParameterSpec(name="slow", value_type=ParameterValueType.INT, default=30),
        ],
        indicators=[
            IndicatorRef(name="Fast EMA", params={"fast": 10}),
            IndicatorRef(name="Slow EMA", params={"slow": 30}),
        ],
        entry_conditions=[
            Condition(
                expression="Fast EMA crossover Slow EMA",
                when=ConditionWhen.ENTRY,
            )
        ],
        exit_signals=[
            ExitSignal(
                name="exit_1",
                condition="Fast EMA crossunder Slow EMA",
                kind=ExitSignalKind.CLOSE,
            )
        ],
        confidence=1.0,
        engine_hint=EngineHint.VECTORBT,
        metadata={"version": "1.0.0", "tags": {"type": ["momentum"]}},
    )


def test_generate_vectorbt_strategy_code_is_deterministic():
    strategy_ir = _sample_ir()

    code_a = generate_vectorbt_strategy_code(strategy_ir)
    code_b = generate_vectorbt_strategy_code(strategy_ir)

    assert code_a == code_b


def test_generate_vectorbt_strategy_code_emits_valid_contract():
    strategy_ir = _sample_ir()
    code = generate_vectorbt_strategy_code(strategy_ir)

    namespace: dict[str, object] = {}
    exec(code, namespace)

    assert "STRATEGY_META" in namespace
    assert namespace["STRATEGY_META"]["name"] == "Deterministic Demo"
    assert "generate_signals" in namespace

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC"),
            "close": [100 + idx * 0.2 for idx in range(60)],
        }
    )

    output = namespace["generate_signals"](df, {"fast": 5, "slow": 12})
    assert isinstance(output, tuple)
    assert len(output) in {3, 4}

    entries, exits, indicators = output[:3]
    assert len(pd.Series(entries)) == len(df)
    assert len(pd.Series(exits)) == len(df)
    assert isinstance(indicators, dict)
    assert set(indicators) >= {"Fast EMA", "Slow EMA"}

    if len(output) == 4:
        assert isinstance(output[3], list)


def test_generate_vectorbt_strategy_code_handles_ir_without_indicators():
    strategy_ir = StrategyIR(
        name="No Indicator Demo",
        parameters=[
            ParameterSpec(name="window", value_type=ParameterValueType.INT, default=15),
        ],
        indicators=[],
        entry_conditions=[],
        exit_signals=[],
        confidence=0.0,
        engine_hint=EngineHint.VECTORBT,
        metadata={"version": "1.0.0", "tags": {}},
    )

    code = generate_vectorbt_strategy_code(strategy_ir)
    namespace: dict[str, object] = {}
    exec(code, namespace)

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC"),
            "close": [100 + idx * 0.1 for idx in range(48)],
        }
    )

    output = namespace["generate_signals"](df, {"window": 10})
    entries, exits, indicators = output[:3]

    assert len(pd.Series(entries)) == len(df)
    assert len(pd.Series(exits)) == len(df)
    assert "baseline" in indicators
    assert len(pd.Series(indicators["baseline"])) == len(df)


def test_generate_vectorbt_strategy_code_sorts_meta_parameters_for_stable_output():
    strategy_ir = StrategyIR(
        name="Stable Meta Demo",
        parameters=[
            ParameterSpec(name="slow", value_type=ParameterValueType.INT, default=30),
            ParameterSpec(name="fast", value_type=ParameterValueType.INT, default=10),
        ],
        indicators=[],
        entry_conditions=[],
        exit_signals=[],
        confidence=0.0,
        engine_hint=EngineHint.VECTORBT,
        metadata={"version": "1.0.0", "tags": {}},
    )

    code = generate_vectorbt_strategy_code(strategy_ir)
    namespace: dict[str, object] = {}
    exec(code, namespace)

    meta = namespace["STRATEGY_META"]
    assert list(meta["parameters"].keys()) == ["fast", "slow"]
