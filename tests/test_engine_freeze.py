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
from runner.engines.base import FROZEN_ENGINES, warn_if_engine_frozen


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
