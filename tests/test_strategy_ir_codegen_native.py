"""Native-engine StrategyIR code generation (#318 / ADR 0001).

The native adapter consumes the same ``generate_signals(df, params)`` module
convention as the frozen vectorbt adapter, so the IR package offers a native
emitter that shares the deterministic implementation.
"""

from __future__ import annotations

import pandas as pd
import pytest

from runner.strategy_ir.builder import build_strategy_ir
from runner.strategy_ir.codegen_native import generate_native_strategy_code
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
from runner.strategy_ir.routing import resolve_codegen_route


def _sample_ir() -> StrategyIR:
    return StrategyIR(
        name="Native Demo",
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
        engine_hint=EngineHint.NATIVE,
        metadata={"version": "1.0.0", "tags": {"type": ["momentum"]}},
    )


def test_generate_native_strategy_code_is_deterministic():
    strategy_ir = _sample_ir()

    assert generate_native_strategy_code(strategy_ir) == generate_native_strategy_code(
        strategy_ir
    )


def test_generate_native_strategy_code_emits_valid_native_contract():
    code = generate_native_strategy_code(_sample_ir())

    assert "native strategy" in code.splitlines()[0]
    assert "vectorbt" not in code.splitlines()[0]

    namespace: dict[str, object] = {}
    exec(code, namespace)

    assert namespace["STRATEGY_META"]["name"] == "Native Demo"
    assert "generate_signals" in namespace

    df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC"),
            "close": [100 + idx * 0.2 for idx in range(60)],
        }
    )
    output = namespace["generate_signals"](df, {"fast": 5, "slow": 12})
    entries, exits, indicators = output[:3]
    assert len(pd.Series(entries)) == len(df)
    assert len(pd.Series(exits)) == len(df)
    assert isinstance(indicators, dict)


def test_generate_native_strategy_code_untranslatable_names_native():
    strategy_ir = StrategyIR(
        name="Prose Demo",
        indicators=[],
        entry_conditions=[
            Condition(expression="buy when the moon is full", when=ConditionWhen.ENTRY)
        ],
        exit_signals=[],
        confidence=1.0,
        engine_hint=EngineHint.NATIVE,
    )

    with pytest.raises(ValueError, match=r"native logic.*moon is full"):
        generate_native_strategy_code(strategy_ir)


def test_codegen_route_resolves_native_generator():
    route = resolve_codegen_route(
        engine="native",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"native": generate_native_strategy_code},
    )

    assert route.use_ir_generator is True
    assert route.generator_id == "native"
    assert route.generator is generate_native_strategy_code


def test_builder_maps_native_engine_hint():
    strategy_ir = build_strategy_ir(
        strategy_slug="native_hint_demo",
        engine="native",
        source_code="pass",
        markdown_doc="# Native Hint\n\n### Entry\n- Entry\n\n### Exit\n- Exit",
        strategy_meta={"name": "Native Hint", "version": "1.0.0", "parameters": {}, "tags": {}},
    )

    assert strategy_ir.engine_hint == EngineHint.NATIVE
