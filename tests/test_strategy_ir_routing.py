from __future__ import annotations

from runner.strategy_ir.models import StrategyIR
from runner.strategy_ir.routing import resolve_codegen_route


def _demo_generator(_strategy_ir: StrategyIR) -> str:
    return "# generated"


def test_resolve_codegen_route_uses_ir_generator_for_supported_engine():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
    )

    assert route.use_ir_generator is True
    assert route.first_llm_attempt == 2
    assert route.generator_id == "vectorbt"
    assert route.generator is _demo_generator
    assert route.rollout_mode == "ir-first"
    assert route.allow_legacy_fallback is True


def test_resolve_codegen_route_uses_legacy_when_engine_not_registered():
    route = resolve_codegen_route(
        engine="pynecore",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
    )

    assert route.use_ir_generator is False
    assert route.first_llm_attempt == 1
    assert route.generator is None
    assert route.allow_legacy_fallback is True
    assert "pynecore" in route.reason


def test_resolve_codegen_route_uses_legacy_when_strategy_ir_missing():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=None,
        generators={"vectorbt": _demo_generator},
    )

    assert route.use_ir_generator is False
    assert route.first_llm_attempt == 1
    assert route.generator is None
    assert route.allow_legacy_fallback is True
    assert "StrategyIR" in route.reason


def test_resolve_codegen_route_forces_legacy_mode_even_with_generator():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
        rollout_mode="legacy",
    )

    assert route.use_ir_generator is False
    assert route.first_llm_attempt == 1
    assert route.generator is None
    assert route.rollout_mode == "legacy"
    assert route.allow_legacy_fallback is True
    assert "forces legacy" in route.reason


def test_resolve_codegen_route_disables_legacy_fallback_in_ir_only_mode():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
        rollout_mode="ir-only",
    )

    assert route.use_ir_generator is True
    assert route.first_llm_attempt == 2
    assert route.generator is _demo_generator
    assert route.rollout_mode == "ir-only"
    assert route.allow_legacy_fallback is False
