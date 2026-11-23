"""Freeze notes for the #318 engine consolidation (phase S).

Selecting a frozen engine — ``vectorbt``, ``pynecore`` or the LEAN *run*
path — must emit a ``DeprecationWarning``; ``native`` and unknown names must
not. No defaults flip in phase S: the registry default stays ``vectorbt``.
"""

from __future__ import annotations

import warnings

import pytest

from runner.backtest_grid import get_engine as grid_get_engine
from runner.cli import get_engine
from runner.engines.base import (
    FROZEN_ENGINES,
    BacktestConfig,
    warn_if_engine_frozen,
)


def _warnings_for(func, *args, **kwargs):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        func(*args, **kwargs)
    return [w for w in caught if issubclass(w.category, DeprecationWarning)]


class TestEngineSelectionWarnings:
    @pytest.mark.parametrize("name", ["vectorbt", "pynecore", "lean"])
    def test_frozen_engines_warn_on_selection(self, name):
        assert name in FROZEN_ENGINES
        caught = _warnings_for(get_engine, name)
        assert len(caught) == 1
        assert "frozen" in str(caught[0].message)

    @pytest.mark.parametrize("name", ["vectorbt", "pynecore", "lean"])
    def test_grid_get_engine_warns_too(self, name):
        caught = _warnings_for(grid_get_engine, name)
        assert len(caught) == 1
        assert "frozen" in str(caught[0].message)

    def test_native_does_not_warn(self):
        assert _warnings_for(get_engine, "native") == []
        assert _warnings_for(grid_get_engine, "native") == []

    def test_unknown_engine_still_rejected(self):
        with pytest.raises(ValueError, match="Unknown engine"):
            get_engine("not-an-engine")

    def test_helper_is_a_noop_for_unfrozen_names(self):
        for name in ("native", "", "bogus"):
            assert _warnings_for(warn_if_engine_frozen, name) == []

    def test_lean_run_path_warns_but_import_result_does_not(self, tmp_path):
        """``LeanAdapter.run`` is frozen; ``import_result`` stays supported."""
        from runner.engines.lean_adapter import LeanAdapter

        adapter = LeanAdapter(lean_project_dir=tmp_path)
        config = BacktestConfig(
            strategy_name="x",
            symbols=["SYM"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
        )
        # A .json strategy_path fails fast after the warning — no subprocess.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(ValueError, match="import_result"):
                adapter.run(config, str(tmp_path / "result.json"))
        frozen = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(frozen) == 1
        assert "frozen" in str(frozen[0].message)

        # import_result never warns: LEAN import stays a supported oracle.
        missing = tmp_path / "missing.json"
        missing.write_text("{}")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                adapter.import_result(missing, config=config)
            except Exception:
                pass
        assert not [
            w for w in caught if issubclass(w.category, DeprecationWarning)
        ]

    def test_optimizer_vectorbt_backtest_path_warns(self, monkeypatch):
        """``optimizer._run_vectorbt_backtest`` is the optimizer's engine path."""
        from pathlib import Path
        from types import SimpleNamespace

        import runner.optimizer as optimizer

        class _FakeAdapter:
            def run(self, config, strategy_path):
                return SimpleNamespace(stats={}, trades=[])

        monkeypatch.setattr(optimizer, "ensure_backtest_data", lambda **kwargs: [])
        monkeypatch.setattr(optimizer, "VectorbtAdapter", _FakeAdapter)
        monkeypatch.setattr(
            optimizer, "compute_stats_for_result", lambda result: {}
        )

        config = BacktestConfig(
            strategy_name="x",
            symbols=["SYM"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
        )
        caught = _warnings_for(
            optimizer._run_vectorbt_backtest, config, Path("s.py")
        )
        assert any("frozen" in str(w.message) for w in caught)
