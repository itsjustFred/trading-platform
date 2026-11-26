"""Engine-name -> adapter dispatch shared by ``runner.cli`` and ``runner.backtest_grid``.

Previously duplicated verbatim in both modules (the grid copy existed only to
avoid importing ``runner.cli``, which itself imports the grid). Adapters are
imported lazily inside ``get_engine`` so selecting an engine does not pull in
every engine's dependencies.

Callers bind the name with ``from runner.engines.registry import get_engine``,
so patching ``runner.engines.registry.get_engine`` does NOT propagate: tests
and callers must patch the name on the importing module
(``runner.cli.get_engine``, ``runner.backtest_grid.get_engine``, ...), which
is where the existing monkeypatch sites already point.
"""

from __future__ import annotations

from runner.engines.base import EngineAdapter, warn_if_engine_frozen


def get_engine(name: str) -> EngineAdapter:
    """Get an engine adapter by name."""
    warn_if_engine_frozen(name, stacklevel=2)
    if name == "native":
        from runner.engines.native_adapter import NativeAdapter

        return NativeAdapter()
    elif name == "vectorbt":
        from runner.engines.vectorbt_adapter import VectorbtAdapter

        return VectorbtAdapter()
    elif name == "lean":
        from runner.engines.lean_adapter import LeanAdapter

        return LeanAdapter()
    elif name == "pynecore":
        from runner.engines.pynecore_adapter import PyneCoreAdapter

        return PyneCoreAdapter()
    else:
        raise ValueError(f"Unknown engine: {name}. Available: native, vectorbt, lean, pynecore")
