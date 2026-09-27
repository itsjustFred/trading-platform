"""Strategy IR package."""

from runner.strategy_ir.builder import build_strategy_ir
from runner.strategy_ir.codegen_vectorbt import generate_vectorbt_strategy_code
from runner.strategy_ir.models import (
    Condition,
    ConditionSide,
    ConditionWhen,
    Drawing,
    DrawingKind,
    DrawingPanel,
    EngineHint,
    ExitSignal,
    ExitSignalKind,
    IndicatorRef,
    ParameterSpec,
    ParameterValueType,
    RawExpression,
    RawExpressionLanguage,
    RawExpressionPurpose,
    StateMachine,
    StateTransition,
    StrategyIR,
)
from runner.strategy_ir.normalizer import normalize_strategy_meta_payload
from runner.strategy_ir.routing import CodegenRoute, resolve_codegen_route

__all__ = [
    "Condition",
    "ConditionSide",
    "ConditionWhen",
    "Drawing",
    "DrawingKind",
    "DrawingPanel",
    "EngineHint",
    "ExitSignal",
    "ExitSignalKind",
    "IndicatorRef",
    "ParameterSpec",
    "ParameterValueType",
    "RawExpression",
    "RawExpressionLanguage",
    "RawExpressionPurpose",
    "StateMachine",
    "StateTransition",
    "StrategyIR",
    "build_strategy_ir",
    "generate_vectorbt_strategy_code",
    "normalize_strategy_meta_payload",
    "CodegenRoute",
    "resolve_codegen_route",
]
