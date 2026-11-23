"""Native engine refusal for strategy trailing-stop parameters.

Maintainer decision (2026-11-23): until the strategies are rebuilt, the native
engine must refuse ``use_trailing_sl=true`` instead of silently computing a
fixed stop. These tests pin the refusal at the adapter boundary and prove it
happens before market data is loaded.
"""

from __future__ import annotations

import pytest

from runner.engines import native_adapter
from runner.engines.base import BacktestConfig, native_strategy_parameter_errors
from runner.engines.native_adapter import NativeAdapter

TRAILING_ERROR = "trailing stops are not modelled by the native engine"


def _config(parameters: dict) -> BacktestConfig:
    return BacktestConfig(
        strategy_name="trailing_check",
        symbols=["TEST"],
        timeframe="1d",
        start_date="2024-01-01",
        end_date="2024-01-10",
        initial_capital=100_000.0,
        risk_per_trade_pct=0.01,
        parameters=parameters,
    )


def _write_strategy(path, *, meta_parameters: dict | None = None) -> None:
    meta_parameters_literal = ""
    if meta_parameters is not None:
        meta_parameters_literal = f'"parameters": {meta_parameters!r}, '
    path.write_text(
        "STRATEGY_META = {\n"
        '    "name": "trailing_check",\n'
        f"    {meta_parameters_literal}\n"
        "}\n"
        "\n"
        "def generate_signals(df, params):\n"
        "    import pandas as pd\n"
        "    entries = pd.Series(False, index=df.index)\n"
        "    exits = pd.Series(False, index=df.index)\n"
        "    return entries, exits, {}, []\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("value", [True, "true", "1", "yes", "on", 1])
def test_validate_config_rejects_truthy_use_trailing_sl(value):
    errors = NativeAdapter().validate_config(_config({"use_trailing_sl": value}))

    assert any(TRAILING_ERROR in error for error in errors)


@pytest.mark.parametrize(
    "parameters",
    [{}, {"use_trailing_sl": False}, {"use_trailing_sl": "false"}],
)
def test_validate_config_allows_absent_or_false_use_trailing_sl(parameters):
    errors = NativeAdapter().validate_config(_config(parameters))

    assert not any(TRAILING_ERROR in error for error in errors)


def test_native_run_rejects_use_trailing_sl_before_loading_data(tmp_path, monkeypatch):
    strategy = tmp_path / "trailing_check.py"
    _write_strategy(strategy)

    def fail_if_data_loaded(*args, **kwargs):
        raise AssertionError("market data must not load for a refused config")

    monkeypatch.setattr(native_adapter, "_load_ohlcv", fail_if_data_loaded)

    with pytest.raises(ValueError, match=TRAILING_ERROR):
        NativeAdapter().run(_config({"use_trailing_sl": True}), str(strategy))


def test_native_run_rejects_strategy_metadata_default(tmp_path, monkeypatch):
    strategy = tmp_path / "trailing_meta.py"
    _write_strategy(strategy, meta_parameters={"use_trailing_sl": True})

    def fail_if_data_loaded(*args, **kwargs):
        raise AssertionError("market data must not load for a refused config")

    monkeypatch.setattr(native_adapter, "_load_ohlcv", fail_if_data_loaded)

    with pytest.raises(ValueError, match=TRAILING_ERROR):
        NativeAdapter().run(_config({}), str(strategy))


def test_native_run_rejects_signature_default_true(tmp_path, monkeypatch):
    """A kwargs-style strategy default is an effective request too."""
    strategy = tmp_path / "trailing_signature.py"
    strategy.write_text(
        "def generate_signals(df, use_trailing_sl=True):\n"
        "    import pandas as pd\n"
        "    entries = pd.Series(False, index=df.index)\n"
        "    exits = pd.Series(False, index=df.index)\n"
        "    return entries, exits, {}, []\n",
        encoding="utf-8",
    )

    def fail_if_data_loaded(*args, **kwargs):
        raise AssertionError("market data must not load for a refused config")

    monkeypatch.setattr(native_adapter, "_load_ohlcv", fail_if_data_loaded)

    with pytest.raises(ValueError, match=TRAILING_ERROR):
        NativeAdapter().run(_config({}), str(strategy))


def test_preflight_rejects_literal_signature_default_without_importing(tmp_path):
    strategy = tmp_path / "trailing_signature.py"
    strategy.write_text(
        "def generate_signals(df, use_trailing_sl=True):\n"
        "    raise AssertionError('preflight must not execute the strategy')\n",
        encoding="utf-8",
    )

    errors = native_strategy_parameter_errors(strategy, {})

    assert any(TRAILING_ERROR in error for error in errors)


def test_preflight_allows_literal_false_signature_default(tmp_path):
    strategy = tmp_path / "fixed_signature.py"
    strategy.write_text(
        "def generate_signals(df, use_trailing_sl=False):\n"
        "    raise AssertionError('preflight must not execute the strategy')\n",
        encoding="utf-8",
    )

    errors = native_strategy_parameter_errors(strategy, {})

    assert not any(TRAILING_ERROR in error for error in errors)
