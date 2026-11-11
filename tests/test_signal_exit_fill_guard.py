"""Issue #153: ``signal_exit_fill="open"`` requires time-based signal exits.

Open-fill takes the exit price from the open of the bar that produced the exit
signal. That is only sound for a time-based exit, where the decision to leave
predates the bar; for an exit computed from the trigger bar (close, high/low,
indicator) the open precedes the information that triggered it — a lookahead.

A strategy therefore declares ``STRATEGY_META["signal_exits_time_based"] = True``
and the run fails loudly without it. The rule is one shared function called by
the native, vectorbt and pynecore adapters; the default ``"close"`` convention
needs no declaration. These tests pin all three engine paths plus the shared
rule itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines import native_adapter, pynecore_adapter
from runner.engines._shared import (
    SIGNAL_EXITS_TIME_BASED_META_KEY,
    require_time_based_signal_exits,
    require_time_based_signal_exits_for_path,
)
from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from runner.engines.pynecore_adapter import PyneCoreAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from tests.helpers import with_zero_fees
from tests.helpers import write_ohlcv as _write_ohlcv


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2024-01-01 00:00", "2024-01-01 01:00", "2024-01-01 02:00"],
                utc=True,
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [100.5, 101.5, 102.5],
            "low": [99.5, 100.5, 101.5],
            "close": [100.2, 101.2, 102.2],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )


def _strategy_text(declared: bool, name: str = "guard_demo") -> str:
    flag = (
        f", '{SIGNAL_EXITS_TIME_BASED_META_KEY}': True" if declared else ""
    )
    return (
        "import pandas as pd\n"
        f"STRATEGY_META = {{'name': '{name}', 'version': '1.0'{flag}}}\n"
        "def generate_signals(df, params):\n"
        "    n = len(df)\n"
        "    entries = pd.Series([i == 0 for i in range(n)], index=df.index)\n"
        "    exits = pd.Series([i == n - 1 for i in range(n)], index=df.index)\n"
        "    return entries, exits, {}\n"
    )


def _write_strategy(tmp_path: Path, declared: bool, name: str = "guard_demo") -> str:
    path = tmp_path / f"{name}.py"
    path.write_text(_strategy_text(declared, name), encoding="utf-8")
    return str(path)


def _config(tmp_path: Path, **overrides: Any) -> BacktestConfig:
    payload: dict[str, Any] = {
        "strategy_name": "guard_demo",
        "symbols": ["GUARD"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-02",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    payload.update(overrides)
    return BacktestConfig(**with_zero_fees(payload))


@pytest.fixture
def vbt_env(tmp_path: Path):
    _write_ohlcv(tmp_path, "GUARD", _frame(), timeframe="1h")
    return tmp_path


@pytest.fixture
def native_env(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(native_adapter, "_load_ohlcv", lambda *_a, **_k: _frame())
    return tmp_path


class TestNativePath:
    def test_open_fill_without_declaration_fails(self, native_env):
        path = _write_strategy(native_env, declared=False)
        config = _config(native_env, signal_exit_fill="open")

        with pytest.raises(ValueError, match="signal_exits_time_based"):
            NativeAdapter().run(config, path)

    def test_open_fill_with_declaration_runs(self, native_env):
        path = _write_strategy(native_env, declared=True)
        config = _config(native_env, signal_exit_fill="open")

        result = NativeAdapter().run(config, path)

        assert len(result.trades) == 1
        # Declared open-fill keeps the MT5-parity price: the trigger bar's open.
        assert result.trades[0].exits[0].price == pytest.approx(102.0)

    def test_close_fill_needs_no_declaration(self, native_env):
        path = _write_strategy(native_env, declared=False)
        config = _config(native_env)  # default signal_exit_fill="close"

        result = NativeAdapter().run(config, path)

        assert len(result.trades) == 1
        assert result.trades[0].exits[0].price == pytest.approx(102.2)


class TestVectorbtPath:
    def test_open_fill_without_declaration_fails(self, vbt_env):
        path = _write_strategy(vbt_env, declared=False)
        config = _config(vbt_env, signal_exit_fill="open")

        with pytest.raises(ValueError, match="signal_exits_time_based"):
            VectorbtAdapter().run(config, path)

    def test_open_fill_with_declaration_runs(self, vbt_env):
        path = _write_strategy(vbt_env, declared=True)
        config = _config(vbt_env, signal_exit_fill="open")

        result = VectorbtAdapter().run(config, path)

        assert len(result.trades) == 1
        assert result.trades[0].exits[0].price == pytest.approx(102.0)

    def test_close_fill_needs_no_declaration(self, vbt_env):
        path = _write_strategy(vbt_env, declared=False)
        config = _config(vbt_env)

        result = VectorbtAdapter().run(config, path)

        assert len(result.trades) == 1
        assert result.trades[0].exits[0].price == pytest.approx(102.2)


class TestPynecorePath:
    @pytest.fixture(autouse=True)
    def _stop_before_the_cli(self, monkeypatch):
        """Make the guard's placement observable without a pyne binary.

        The guard must run before the CLI lookup, so an undeclared open-fill
        run reports the config error; a run that gets past it hits this
        sentinel instead of depending on whether pyne is installed.
        """

        def _sentinel() -> str:
            raise RuntimeError("pyne guard passed")

        monkeypatch.setattr(pynecore_adapter, "_resolve_pyne_bin", _sentinel)

    def test_open_fill_without_declaration_fails(self, tmp_path):
        path = _write_strategy(tmp_path, declared=False)
        config = _config(tmp_path, signal_exit_fill="open")

        with pytest.raises(ValueError, match="signal_exits_time_based"):
            PyneCoreAdapter().run(config, path)

    def test_open_fill_with_declaration_passes_the_guard(self, tmp_path):
        path = _write_strategy(tmp_path, declared=True)
        config = _config(tmp_path, signal_exit_fill="open")

        with pytest.raises(RuntimeError, match="pyne guard passed"):
            PyneCoreAdapter().run(config, path)

    def test_close_fill_needs_no_declaration(self, tmp_path):
        path = _write_strategy(tmp_path, declared=False)
        config = _config(tmp_path)

        with pytest.raises(RuntimeError, match="pyne guard passed"):
            PyneCoreAdapter().run(config, path)


class TestSharedRule:
    def _config(self, fill: str) -> BacktestConfig:
        return BacktestConfig(
            strategy_name="guard_demo",
            symbols=["GUARD"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            initial_capital=100_000.0,
            risk_per_trade_pct=0.01,
            signal_exit_fill=fill,
        )

    def test_declaration_must_be_the_boolean_true(self):
        config = self._config("open")
        # A truthy string is a typo, not an opt-in: it must fail loudly.
        with pytest.raises(ValueError, match="signal_exits_time_based"):
            require_time_based_signal_exits(
                config, {"name": "guard_demo", SIGNAL_EXITS_TIME_BASED_META_KEY: "true"}
            )

    def test_missing_meta_fails_for_open_and_passes_for_close(self):
        with pytest.raises(ValueError, match="signal_exits_time_based"):
            require_time_based_signal_exits(self._config("open"), None)
        # close-fill is unaffected even with no metadata at all
        require_time_based_signal_exits(self._config("close"), None)

    def test_error_names_the_strategy_and_the_flag(self):
        with pytest.raises(ValueError) as excinfo:
            require_time_based_signal_exits(
                self._config("open"), {"name": "guard_demo"}
            )
        message = str(excinfo.value)
        assert "guard_demo" in message
        assert SIGNAL_EXITS_TIME_BASED_META_KEY in message

    def test_path_helper_reads_static_meta(self, tmp_path):
        undeclared = _write_strategy(tmp_path, declared=False, name="no_flag")
        declared = _write_strategy(tmp_path, declared=True, name="with_flag")
        config = self._config("open")

        with pytest.raises(ValueError, match="signal_exits_time_based"):
            require_time_based_signal_exits_for_path(config, undeclared)
        require_time_based_signal_exits_for_path(config, declared)
