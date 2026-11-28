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
from tests.builders import make_backtest_config

TRAILING_ERROR = "trailing stops are not modelled by the native engine"


def _config(parameters: dict) -> BacktestConfig:
    return make_backtest_config(
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
    [
        {},
        {"use_trailing_sl": False},
        {"use_trailing_sl": None},
        {"use_trailing_sl": 0},
        {"use_trailing_sl": 0.0},
        {"use_trailing_sl": "false"},
        {"use_trailing_sl": "0"},
        {"use_trailing_sl": "no"},
        {"use_trailing_sl": "off"},
        {"use_trailing_sl": ""},
        {"use_trailing_sl": " FALSE "},
    ],
)
def test_validate_config_allows_absent_or_false_use_trailing_sl(parameters):
    errors = NativeAdapter().validate_config(_config(parameters))

    assert not any(TRAILING_ERROR in error for error in errors)
    assert not any("not a recognised boolean" in error for error in errors)


@pytest.mark.parametrize(
    "value",
    ["maybe", "2", "n", "enabled", [], {}, [True], float("nan"), float("inf")],
)
def test_validate_config_rejects_ambiguous_use_trailing_sl(value):
    """Values that are neither a boolean nor a known token are refused, not coerced."""
    errors = NativeAdapter().validate_config(_config({"use_trailing_sl": value}))

    assert any("not a recognised boolean" in error for error in errors)


def test_native_run_rejects_use_trailing_sl_before_loading_data(tmp_path, monkeypatch):
    strategy = tmp_path / "trailing_check.py"
    _write_strategy(strategy)

    def fail_if_data_loaded(*args, **kwargs):
        raise AssertionError("market data must not load for a refused config")

    monkeypatch.setattr(native_adapter, "load_ohlcv", fail_if_data_loaded)

    with pytest.raises(ValueError, match=TRAILING_ERROR):
        NativeAdapter().run(_config({"use_trailing_sl": True}), str(strategy))


def test_native_run_rejects_strategy_metadata_default(tmp_path, monkeypatch):
    strategy = tmp_path / "trailing_meta.py"
    _write_strategy(strategy, meta_parameters={"use_trailing_sl": True})

    def fail_if_data_loaded(*args, **kwargs):
        raise AssertionError("market data must not load for a refused config")

    monkeypatch.setattr(native_adapter, "load_ohlcv", fail_if_data_loaded)

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

    monkeypatch.setattr(native_adapter, "load_ohlcv", fail_if_data_loaded)

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


def test_strategy_parameter_default_survives_unparseable_source(tmp_path):
    """A source ast.parse cannot handle must fall back to None, not propagate.

    RecursionError/MemoryError/ValueError (e.g. null bytes) used to escape
    ``strategy_parameter_default`` and surface as a 500 in the viewer or an
    aborted CLI/grid run; the imported-signature check stays the authority.
    """
    from runner.engines.base import strategy_parameter_default

    strategy = tmp_path / "broken_signature.py"
    strategy.write_bytes(b"def generate_signals(df, use_trailing_sl=True):\n\x00")

    assert strategy_parameter_default(strategy, "use_trailing_sl") is None


@pytest.mark.parametrize(
    "error",
    [
        RecursionError("maximum recursion depth exceeded"),
        ValueError("source code string cannot contain null bytes"),
        TypeError("expected str, bytes or ast.AST"),
        MemoryError("out of memory"),
        SyntaxError("invalid syntax"),
    ],
)
def test_strategy_parameter_default_survives_ast_parse_failure(
    tmp_path, monkeypatch, error
):
    """The widened ``except`` around ``ast.parse`` must catch every failure
    class, not just SyntaxError: reverting to ``except SyntaxError`` let
    RecursionError/ValueError/TypeError/MemoryError escape as a 500.
    """
    from runner.engines import base as engines_base
    from runner.engines.base import strategy_parameter_default

    strategy = tmp_path / "fine_signature.py"
    strategy.write_text(
        "def generate_signals(df, use_trailing_sl=True):\n    pass\n",
        encoding="utf-8",
    )

    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(engines_base.ast, "parse", boom)

    assert strategy_parameter_default(strategy, "use_trailing_sl") is None


def _load_custom_swings_supertrend():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "strategies"
        / "custom_swings_supertrend"
        / "custom_swings_supertrend.py"
    )
    spec = importlib.util.spec_from_file_location("custom_swings_supertrend", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trailing_fixture_df():
    import numpy as np
    import pandas as pd

    close = np.concatenate(
        [
            np.linspace(100, 80, 120),
            np.linspace(80, 130, 60),
            np.linspace(130, 150, 60),
            np.linspace(150, 110, 60),
        ]
    )
    return pd.DataFrame(
        {
            "open": np.roll(close, 1),
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": 1000.0,
        }
    )


def _signal_frame(result):
    entries, exits, indicators, _ = result
    return entries, exits, indicators


def _signals_equal(first, second) -> bool:
    entries_a, exits_a, indicators_a = first
    entries_b, exits_b, indicators_b = second
    return (
        entries_a.equals(entries_b)
        and exits_a.equals(exits_b)
        and indicators_a.keys() == indicators_b.keys()
        and all(indicators_a[key].equals(indicators_b[key]) for key in indicators_a)
    )


def test_kwargs_style_false_string_does_not_trail_or_take_profit():
    """Regression: the native engine calls generate_signals kwargs-style, where
    the legacy params-dict coercion never ran — a string "false" stayed
    truthy. The strategy must now coerce its boolean switches on every call.
    """
    module = _load_custom_swings_supertrend()
    df = _trailing_fixture_df()
    base = dict(
        st_period=3,
        st_multiplier=2.0,
        ema_fast=3,
        ema_slow=5,
        ema_filter_enabled=False,
        use_rrr=False,
        risk_reward=1.0,
        use_breakeven=False,
        use_trailing_sl=False,
        trade_direction="long",
    )

    # Sanity: the fixture produces a real trade so the comparison is meaningful.
    entries, _, _, _ = module.generate_signals(df, **base)
    assert int(entries.sum()) > 0

    def run(**overrides):
        return _signal_frame(module.generate_signals(df, **{**base, **overrides}))

    # "false" must behave exactly like False — before the fix it stayed a
    # truthy string and enabled fixed-RR take-profit exits.
    assert _signals_equal(run(use_rrr="false"), run(use_rrr=False))
    assert _signals_equal(run(use_rrr="true"), run(use_rrr=True))
    assert not _signals_equal(run(use_rrr=True), run(use_rrr=False))

    # The same coercion is observable for the other two switches on this
    # fixture (True vs False changes the signals), so pin them the same way —
    # dropping either ``_coerce_bool`` line flips these to unequal.
    assert _signals_equal(run(use_breakeven="false"), run(use_breakeven=False))
    assert _signals_equal(run(use_breakeven="0"), run(use_breakeven=False))
    assert not _signals_equal(run(use_breakeven=True), run(use_breakeven=False))
    assert _signals_equal(
        run(ema_filter_enabled="false"), run(ema_filter_enabled=False)
    )
    assert not _signals_equal(
        run(ema_filter_enabled=True), run(ema_filter_enabled=False)
    )


def test_kwargs_style_switches_pass_through_coerce_bool():
    """Every boolean kwarg must reach ``_coerce_bool`` with its raw value.

    ``use_trailing_sl`` itself cannot be observed in this strategy's signals:
    ``tp_hit`` is only set on the bar that first touches ``tp_target``, and
    that same bar always exits (fixed-RR take-profit or stop), so the
    trailing branch is unreachable on every engine — ``True`` and ``False``
    produce identical frames. The spy below pins the next best thing: each
    kwarg's raw token still flows through ``_coerce_bool`` on the kwargs
    path, so deleting e.g. ``use_trailing_sl = _coerce_bool(use_trailing_sl)``
    drops its token from the recorded calls. (A bare ``_coerce_bool(x)`` call
    whose result is discarded would still pass — that residual is inherent
    to the parameter being a no-op; flagged to the maintainer.)
    """
    module = _load_custom_swings_supertrend()
    df = _trailing_fixture_df()
    calls = []
    real_coerce = module._coerce_bool

    def spy(value):
        calls.append(value)
        return real_coerce(value)

    module._coerce_bool = spy
    try:
        module.generate_signals(
            df,
            st_period=3,
            st_multiplier=2.0,
            ema_fast=3,
            ema_slow=5,
            ema_filter_enabled="",
            use_rrr="0",
            risk_reward=1.0,
            use_breakeven="no",
            use_trailing_sl="off",
            trade_direction="long",
        )
    finally:
        module._coerce_bool = real_coerce

    # One distinct false-ish token per switch: each must appear among the
    # values handed to _coerce_bool exactly when its coercion line runs.
    for token in ("", "0", "no", "off"):
        assert token in calls
