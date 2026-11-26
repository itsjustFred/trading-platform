"""Deterministic native-engine strategy code generation from StrategyIR.

The native engine consumes the same ``generate_signals(df, params)`` module
convention as the frozen vectorbt adapter (ADR 0001 / #318), so the emitted
module is produced by the shared emitter in ``codegen_vectorbt`` — only the
engine label differs. The vectorbt-named module is kept because the emitter
predates the consolidation; new callers should prefer this native entry
point.
"""

from __future__ import annotations

from runner.strategy_ir.codegen_vectorbt import _generate_strategy_module
from runner.strategy_ir.models import StrategyIR


def generate_native_strategy_code(strategy_ir: StrategyIR) -> str:
    """Generate deterministic native-compatible strategy code from StrategyIR."""
    return _generate_strategy_module(strategy_ir, engine="native")
