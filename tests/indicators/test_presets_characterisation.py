"""Characterisation tests: every preset must equal the existing
implementation it is meant to replace (issue #317, phase P0).

Each test loads the *current* per-strategy / per-bot function (via
``importlib`` for strategy files, which are not packages, and via normal
imports for ``signal_bots``) or execs the real ``codegen_vectorbt`` output,
and asserts the preset reproduces it on the same deterministic OHLCV frame.
Every test runs against two fixtures:

- ``clean``: a smooth random walk;
- ``tick``: prices quantised to a 0.25 tick with flat runs, zero-range
  bars (zero-width Supertrend bands / denominator-floor coverage), and one
  interior NaN OHLC row.

``exact=True`` is used only where the legacy copy is itself the same pure
pandas/NumPy formula; everything else keeps the documented tolerance.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from runner.strategy_ir import (
    Condition,
    IndicatorRef,
    ParameterSpec,
    ParameterValueType,
    StrategyIR,
    generate_vectorbt_strategy_code,
)
from shared.indicators import core, presets

REPO_ROOT = Path(__file__).resolve().parents[2]
TICK = 0.25


def _load_strategy(relative_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clean_frame(n: int = 220, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    open_ = close + rng.normal(0.0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.2, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.2, 1.5, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    return pd.DataFrame(
        {
            "open": pd.Series(open_, index=idx),
            "high": pd.Series(high, index=idx),
            "low": pd.Series(low, index=idx),
            "close": pd.Series(close, index=idx),
        }
    )


def _quantize(values: np.ndarray) -> np.ndarray:
    return np.round(values / TICK) * TICK


def _tick_frame(n: int = 220, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.0, 0.8, n))
    close = _quantize(close)
    open_ = close + rng.choice([-TICK, 0.0, TICK], size=n)
    high = np.maximum(open_, close) + rng.choice(
        [TICK, 0.5, 0.75, 1.0], size=n
    )
    low = np.minimum(open_, close) - rng.choice(
        [TICK, 0.5, 0.75, 1.0], size=n
    )
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    df = pd.DataFrame(
        {
            "open": pd.Series(_quantize(open_), index=idx),
            "high": pd.Series(_quantize(high), index=idx),
            "low": pd.Series(_quantize(low), index=idx),
            "close": pd.Series(close, index=idx),
        }
    )

    # Flat RSI run: constant close for long enough to produce all-zero
    # gain/loss windows (the 50/100 mask and the fill50 path).
    flat_price = float(df["close"].iloc[39])
    df.loc[df.index[40:56], "close"] = flat_price
    df.loc[df.index[40:56], "open"] = flat_price
    df.loc[df.index[40:56], "high"] = flat_price + TICK
    df.loc[df.index[40:56], "low"] = flat_price - TICK

    # Zero-range run: high == low == close for a stretch longer than the
    # ATR periods used below, so SMA ATR reaches exactly 0.0 and the
    # Supertrend bands collapse to zero width (denominator-floor path).
    zero_price = float(df["close"].iloc[118])
    df.loc[df.index[119:150], ["open", "high", "low", "close"]] = zero_price

    # One interior NaN OHLC row outside the engineered runs.
    df.loc[df.index[170], ["open", "high", "low", "close"]] = np.nan

    df["high"] = np.maximum(df["high"], df["close"])
    df["low"] = np.minimum(df["low"], df["close"])
    return df


@pytest.fixture(params=["clean", "tick"], scope="module")
def ohlcv(request) -> pd.DataFrame:
    if request.param == "clean":
        return _clean_frame()
    return _tick_frame()


def _series(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce").astype(float)


def _assert_series_equal(
    actual: pd.Series,
    expected: pd.Series,
    *,
    start: int = 0,
    rtol: float = 1e-9,
    exact: bool = False,
) -> None:
    a = pd.to_numeric(actual, errors="coerce").astype(float).iloc[start:]
    e = pd.to_numeric(expected, errors="coerce").astype(float).iloc[start:]
    kwargs = {"check_names": False}
    if exact:
        kwargs["check_exact"] = True
    else:
        kwargs.update({"rtol": rtol, "atol": rtol})
    pd.testing.assert_series_equal(
        a.reset_index(drop=True), e.reset_index(drop=True), **kwargs
    )


def _ohlc(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    return (
        _series(frame, "high"),
        _series(frame, "low"),
        _series(frame, "close"),
    )


def _generated_signals(indicator_name: str, window: int):
    ir = StrategyIR(
        name="indicator_characterisation",
        indicators=[IndicatorRef(name=indicator_name, params={"window": window})],
        entry_conditions=[Condition(expression="crossover")],
        parameters=[
            ParameterSpec(
                name="window",
                value_type=ParameterValueType.INT,
                default=window,
            )
        ],
    )
    namespace: dict = {}
    exec(compile(generate_vectorbt_strategy_code(ir), "<generated>", "exec"), namespace)
    return namespace["generate_signals"]


# ── ATR: the QuantConnect "Wilder" ewm variant ─────────────────────────


@pytest.mark.parametrize(
    "strategy",
    ["gold_momentum", "cross_asset_momentum", "sector_rotation"],
)
def test_atr_qc_wilder_matches_strategy_copies(strategy, ohlcv):
    """P1: the strategies now call the preset directly; pin the wiring."""
    module = _load_strategy(
        f"strategies/{strategy}/{strategy}.py", f"char_{strategy}"
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {})
    high, low, close = _ohlc(ohlcv)
    preset = presets.atr_qc_wilder(high, low, close, 14)
    _assert_series_equal(preset, indicators["ATR"], exact=True)
    _assert_series_equal(
        preset, _legacy_qc_wilder_atr(ohlcv, 14), exact=True
    )


def test_atr_qc_wilder_matches_trend_following(ohlcv):
    module = _load_strategy(
        "strategies/trend_following/trend_following.py", "char_tf"
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {"atr_period": 20})
    high, low, close = _ohlc(ohlcv)
    preset = presets.atr_qc_wilder(high, low, close, 20)
    _assert_series_equal(preset, indicators["ATR"], exact=True)
    _assert_series_equal(
        preset, _legacy_qc_wilder_atr(ohlcv, 20), exact=True
    )


def test_atr_qc_wilder_matches_stat_arb_pairs(ohlcv):
    module = _load_strategy(
        "strategies/stat_arb_pairs/stat_arb_pairs.py", "char_sab"
    )
    _, _, indicators, _ = module.generate_signals(
        ohlcv, {"use_atr_stop": True, "atr_period": 14}
    )
    high, low, close = _ohlc(ohlcv)
    preset = presets.atr_qc_wilder(high, low, close, 14)
    _assert_series_equal(preset, indicators["ATR"], exact=True)
    _assert_series_equal(
        preset, _legacy_qc_wilder_atr(ohlcv, 14), exact=True
    )


# ── ATR: MT5 iATR and the SMA-seeded Wilder (intraday_volatility_breakout)


@pytest.mark.parametrize("period", [1, 5, 14])
def test_atr_mt5_iatr_matches_intraday_volatility_breakout(period, ohlcv):
    module = _load_strategy(
        "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py",
        "char_ivb",
    )
    high, low, close = _ohlc(ohlcv)
    tr = core.true_range(high, low, close, first_bar="hl")
    legacy = module._mt5_atr(tr, period)
    preset = presets.atr_mt5_iatr(tr, period)
    _assert_series_equal(preset, legacy, exact=True)


@pytest.mark.parametrize("period", [1, 5, 14])
def test_atr_wilder_sma_seed_matches_intraday_volatility_breakout(
    period, ohlcv
):
    module = _load_strategy(
        "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py",
        "char_ivb",
    )
    high, low, close = _ohlc(ohlcv)
    tr = core.true_range(high, low, close, first_bar="hl")
    legacy = module._wilder_atr(tr, period)
    preset = presets.atr_wilder_sma_seed(tr, period)
    _assert_series_equal(preset, legacy, exact=True)


def test_atr_skipna_variants_match_with_interior_nan(ohlcv):
    """The ``_valid_values`` contract: NaN TR positions are skipped."""
    module = _load_strategy(
        "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py",
        "char_ivb",
    )
    high, low, close = _ohlc(ohlcv)
    tr = core.true_range(high, low, close, first_bar="hl")
    tr = tr.copy()
    tr.iloc[10] = np.nan
    tr.iloc[50] = np.nan
    for period in (5, 14):
        _assert_series_equal(
            presets.atr_mt5_iatr(tr, period),
            module._mt5_atr(tr, period),
            exact=True,
        )
        _assert_series_equal(
            presets.atr_wilder_sma_seed(tr, period),
            module._wilder_atr(tr, period),
            exact=True,
        )


# ── ATR: rolling-mean ("SMA") variants ─────────────────────────────────


def test_atr_sma_tr_matches_channel_breakout(ohlcv):
    module = _load_strategy(
        "strategies/channel_breakout/channel_breakout.py", "char_cb"
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {"atr_period": 14})
    high, low, close = _ohlc(ohlcv)
    preset = presets.atr_sma_tr(high, low, close, 14, tr_first="hl")
    _assert_series_equal(preset, indicators["ATR"], exact=True)


def test_atr_sma_tr_matches_ema_crossover(ohlcv):
    module = _load_strategy(
        "strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py",
        "char_ec",
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {"atr_period": 14})
    high, low, close = _ohlc(ohlcv)
    preset = presets.atr_sma_tr(high, low, close, 14, tr_first="hl")
    _assert_series_equal(preset, indicators["atr_value"], exact=True)


# ── ATR/EMA: EMA-span variant (live bots) ──────────────────────────────


def _legacy_tr_parts(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """Shared TR construction used by the frozen pre-P1 references."""
    h_l = high - low
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            h_l,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr, h_l


def _legacy_qc_wilder_atr(
    frame: pd.DataFrame, period: int
) -> pd.Series:
    """Frozen copy of the deleted strategy ``_wilder_atr`` copies."""
    high, low, close = _ohlc(frame)
    tr, _ = _legacy_tr_parts(high, low, close)
    return tr.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()


def _legacy_ema_span_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """Frozen copy of the pre-P1 ``_calculate_atr`` TR+``ewm(span)`` body
    shared by RamJet and RocketFuel."""
    high, low, close = _ohlc(df)
    tr, _ = _legacy_tr_parts(high, low, close)
    return tr.ewm(span=period, adjust=False).mean()


def test_atr_ema_span_matches_ramjet(ohlcv):
    from signal_bots.ramjet.processor import RamJetSignalProcessor

    frame = RamJetSignalProcessor._prepare_market_data(ohlcv)
    legacy = RamJetSignalProcessor._calculate_atr(frame, 14)["atr"]
    high, low, close = _ohlc(frame)
    preset = presets.atr_ema_span(high, low, close, 14)
    _assert_series_equal(preset, legacy, exact=True)
    _assert_series_equal(
        legacy, _legacy_ema_span_atr(frame, 14), exact=True
    )


def test_atr_ema_span_matches_rocketfuel(ohlcv):
    from signal_bots.rocketfuel.processor import RocketFuelSignalProcessor

    frame = ohlcv.copy()
    legacy = RocketFuelSignalProcessor._calculate_atr(frame, 14)["atr"]
    high, low, close = _ohlc(ohlcv)
    preset = presets.atr_ema_span(high, low, close, 14)
    _assert_series_equal(preset, legacy, exact=True)
    _assert_series_equal(
        legacy, _legacy_ema_span_atr(frame, 14), exact=True
    )


# ── RMA/EMA on arbitrary series ────────────────────────────────────────


@pytest.mark.parametrize(
    "strategy,module_attr",
    [
        ("volatility_breakout", "volatility_breakout"),
        ("qg_30min_es_breakout_strategy", "qg_30min_es_breakout_strategy"),
    ],
)
def test_rma_first_seed_matches_strategy_copies(strategy, module_attr, ohlcv):
    module = _load_strategy(
        f"strategies/{strategy}/{module_attr}.py", f"char_{module_attr}"
    )
    close = _series(ohlcv, "close")
    legacy = module._wilder_rma(close, 9)
    preset = presets.atr_wilder_rma_first(close, 9)
    _assert_series_equal(preset, legacy, exact=True)


def test_ema_first_seed_matches_trend_following(ohlcv):
    module = _load_strategy(
        "strategies/trend_following/trend_following.py", "char_tf_ema"
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {"atr_period": 20})
    close = _series(ohlcv, "close")
    for label, span in (("EMA_Fast", 20), ("EMA_Mid", 50), ("EMA_Slow", 200)):
        _assert_series_equal(
            presets.ema_first_seed(close, span),
            indicators[label],
            exact=True,
        )


def test_ema_first_seed_matches_codegen_vectorbt(ohlcv):
    close = _series(ohlcv, "close")
    generate = _generated_signals("EMA_Fast", 20)
    indicators = generate(ohlcv.assign(close=close), {"window": 20})[2]
    _assert_series_equal(
        presets.ema_first_seed(close, 20), indicators["EMA_Fast"], exact=True
    )


# ── RSI variants ───────────────────────────────────────────────────────


def _legacy_wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    """Frozen copy of the deleted ``gold_momentum._wilder_rsi``."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()
    avg_loss = loss.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def test_rsi_wilder_matches_gold_momentum(ohlcv):
    """P1: gold_momentum calls the preset directly; pin the wiring."""
    module = _load_strategy(
        "strategies/gold_momentum/gold_momentum.py", "char_gm"
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {})
    close = _series(ohlcv, "close")
    preset_rsi = presets.rsi_wilder(close, 14)
    _assert_series_equal(preset_rsi, indicators["RSI"], exact=True)
    _assert_series_equal(
        preset_rsi, _legacy_wilder_rsi(close, 14), exact=True
    )
    # The MACD legs moved to ``ema_first_seed`` in the same swap.
    for label, span in (("EMA_Fast", 12), ("EMA_Slow", 26)):
        _assert_series_equal(
            presets.ema_first_seed(close, span),
            indicators[label],
            exact=True,
        )
    _assert_series_equal(
        presets.ema_first_seed(indicators["MACD_Line"], 9),
        indicators["MACD_Signal"],
        exact=True,
    )


def test_rsi_wilder_masked_matches_post_vix_dip_buy(ohlcv):
    module = _load_strategy(
        "strategies/post_vix_dip_buy/post_vix_dip_buy.py", "char_pv"
    )
    close = _series(ohlcv, "close")
    _assert_series_equal(
        presets.rsi_wilder_masked(close, 14),
        module._wilder_rsi(close, 14),
        exact=True,
    )


def test_rsi_wilder_fill50_matches_codegen_vectorbt(ohlcv):
    close = _series(ohlcv, "close")
    generate = _generated_signals("RSI", 14)
    indicators = generate(ohlcv.assign(close=close), {"window": 14})[2]
    _assert_series_equal(
        presets.rsi_wilder_fill50(close, 14),
        indicators["RSI"],
        exact=True,
    )


# ── Supertrend variants ────────────────────────────────────────────────


def _legacy_ramjet_supertrend_reference(
    df: pd.DataFrame, period: int, multiplier: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Frozen copy of the pre-P1 ``_calculate_supertrend`` fallback loop.

    P1 deleted the hand-written recursion (and the unreachable pandas_ta
    branch); this reference keeps the bit-exactness claim checkable after
    the swap instead of only having been true at P0 time. It returns the
    direction, readiness and both ratcheted bands so NaN-band poisoning is
    pinned, not just the boolean direction.
    """
    high, low, close_series = _ohlc(df)
    hl2 = (high + low) / 2
    tr, h_l = _legacy_tr_parts(high, low, close_series)
    tr = tr.fillna(h_l)
    atr_values = tr.ewm(alpha=1 / period, adjust=False).mean()
    basic_upper = (hl2 + (multiplier * atr_values)).to_numpy()
    basic_lower = (hl2 - (multiplier * atr_values)).to_numpy()
    close = close_series.to_numpy()
    n_rows = len(close)
    final_upper = np.full(n_rows, np.nan)
    final_lower = np.full(n_rows, np.nan)
    supertrend = np.zeros(n_rows, dtype=bool)
    first_valid_idx = 0
    for i in range(n_rows):
        if not np.isnan(basic_upper[i]):
            first_valid_idx = i
            break
    if n_rows > first_valid_idx:
        final_upper[first_valid_idx] = basic_upper[first_valid_idx]
        final_lower[first_valid_idx] = basic_lower[first_valid_idx]
        supertrend[first_valid_idx] = bool(
            close[first_valid_idx] >= final_lower[first_valid_idx]
        )
    for i in range(first_valid_idx + 1, n_rows):
        if basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i - 1]
        if basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i - 1]
        prev_st = supertrend[i - 1]
        if prev_st:
            supertrend[i] = close[i] > final_lower[i]
        else:
            supertrend[i] = close[i] >= final_upper[i]
    warmup_bars = max(int(period), 1)
    ready = np.arange(n_rows) >= first_valid_idx + warmup_bars
    return supertrend, ready, final_upper, final_lower


def _assert_ramjet_supertrend_parity(
    frame: pd.DataFrame, period: int, multiplier: float
) -> None:
    """Assert processor, public replay entry point and preset all match the
    frozen legacy reference, including both ratcheted bands."""
    from signal_bots.ramjet.processor import (
        RamJetSignalProcessor,
        calculate_supertrend,
    )

    expected_st, expected_ready, expected_upper, expected_lower = (
        _legacy_ramjet_supertrend_reference(frame, period, multiplier)
    )
    expected_st_series = pd.Series(
        expected_st.astype(float), index=frame.index
    )
    expected_ready_series = pd.Series(
        expected_ready.astype(float), index=frame.index
    )

    computed = RamJetSignalProcessor._calculate_supertrend(
        frame, period, multiplier
    )
    _assert_series_equal(
        computed["supertrend"].astype(float), expected_st_series, exact=True
    )
    _assert_series_equal(
        computed["supertrend_ready"].astype(float),
        expected_ready_series,
        exact=True,
    )

    # ``calculate_supertrend`` is the public entry point the replay adapter
    # calls; it must see the same legacy NaN-band semantics on raw frames.
    public = calculate_supertrend(frame, period, multiplier)
    _assert_series_equal(
        public["supertrend"].astype(float), expected_st_series, exact=True
    )
    _assert_series_equal(
        public["supertrend_ready"].astype(float),
        expected_ready_series,
        exact=True,
    )

    high, low, close = _ohlc(frame)
    result = presets.supertrend_ramjet(high, low, close, period, multiplier)
    _assert_series_equal(
        (result.trend == 1).astype(float),
        computed["supertrend"].astype(float),
        exact=True,
    )
    _assert_series_equal(
        result.ready.astype(float),
        computed["supertrend_ready"].astype(float),
        exact=True,
    )
    _assert_series_equal(
        result.upper_band,
        pd.Series(expected_upper, index=frame.index),
        exact=True,
    )
    _assert_series_equal(
        result.lower_band,
        pd.Series(expected_lower, index=frame.index),
        exact=True,
    )


@pytest.mark.parametrize(
    "nan_kind", ["none", "close", "high", "low", "ohlc", "first_ohlc"]
)
def test_supertrend_ramjet_matches_legacy_reference(ohlcv, nan_kind):
    """P1: the processor, the public replay entry point and the preset are
    bit-identical to the deleted fallback, including NaN high/low/full-row
    inputs that ``_prepare_market_data`` would drop on the live path but the
    replay adapter can still hand to ``calculate_supertrend``."""
    from signal_bots.ramjet.processor import RamJetSignalProcessor

    frame = RamJetSignalProcessor._prepare_market_data(ohlcv)
    if nan_kind != "none":
        frame = frame.copy()
        if nan_kind == "first_ohlc":
            frame.loc[frame.index[0], ["open", "high", "low", "close"]] = np.nan
        else:
            columns = {
                "close": ["close"],
                "high": ["high"],
                "low": ["low"],
                "ohlc": ["open", "high", "low", "close"],
            }[nan_kind]
            for position in (frame.index[50], frame.index[120]):
                frame.loc[position, columns] = np.nan

    _assert_ramjet_supertrend_parity(frame, 10, 3.0)


@pytest.mark.parametrize("nan_kind", ["high", "low", "ohlc"])
def test_supertrend_ramjet_nan_band_parity(nan_kind):
    """Focused NaN-band regression: a NaN high/low/full row must follow the
    legacy ``or``-chain (a NaN basic band can be selected and poison the
    ratchet), not carry the previous bands forward."""
    from signal_bots.ramjet.processor import RamJetSignalProcessor

    frame = RamJetSignalProcessor._prepare_market_data(_clean_frame())
    columns = {
        "high": ["high"],
        "low": ["low"],
        "ohlc": ["open", "high", "low", "close"],
    }[nan_kind]
    frame.loc[frame.index[30], columns] = np.nan

    _assert_ramjet_supertrend_parity(frame, 1, 2.0)


def test_supertrend_chart_is_registered_ramjet_alias(ohlcv):
    high, low, close = _ohlc(ohlcv)
    ramjet = presets.supertrend_ramjet(high, low, close, 7, 3.0)
    chart = presets.supertrend_chart(high, low, close, 7, 3.0)
    assert presets.PRESETS["supertrend_chart"] is presets.supertrend_chart
    for field in ("trend", "upper_band", "lower_band", "atr", "ready"):
        _assert_series_equal(
            getattr(chart, field), getattr(ramjet, field), exact=True
        )


def test_supertrend_custom_swings_matches_bands(ohlcv):
    module = _load_strategy(
        "strategies/custom_swings_supertrend/custom_swings_supertrend.py",
        "char_cs",
    )
    period, mult = 12, 3.0
    legacy_atr = pd.Series(module._calc_atr(ohlcv, period), index=ohlcv.index)
    trend_arr, up_arr, dn_arr = module._calc_supertrend(
        ohlcv, legacy_atr.to_numpy(), mult
    )
    high, low, close = _ohlc(ohlcv)
    result = presets.supertrend_custom_swings(high, low, close, period, mult)
    _assert_series_equal(
        result.trend,
        pd.Series(trend_arr, index=ohlcv.index).astype(float),
        exact=True,
    )
    # legacy band arrays are 0.0-seeded; the preset emits NaN at bar 0
    # (core contract) — identical from bar 1 on.
    _assert_series_equal(
        result.lower_band,
        pd.Series(up_arr, index=ohlcv.index),
        start=1,
        exact=True,
    )
    _assert_series_equal(
        result.upper_band,
        pd.Series(dn_arr, index=ohlcv.index),
        start=1,
        exact=True,
    )
    _assert_series_equal(result.atr, legacy_atr, exact=True)


def test_supertrend_pine_dema_matches_bands(ohlcv):
    module = _load_strategy(
        "strategies/pine_supertrend_dema/pine_supertrend_dema.py", "char_pd"
    )
    period, mult = 10, 3.0
    trend, lower_band, upper_band = module._calc_supertrend(
        ohlcv, period, mult
    )
    high, low, close = _ohlc(ohlcv)
    result = presets.supertrend_pine_dema(high, low, close, period, mult)
    _assert_series_equal(
        result.trend,
        pd.Series(trend, index=ohlcv.index).astype(float),
        exact=True,
    )
    # legacy bands are 0.0 before the loop start (bar `period`)
    _assert_series_equal(
        result.lower_band,
        pd.Series(lower_band, index=ohlcv.index),
        start=period,
        exact=True,
    )
    _assert_series_equal(
        result.upper_band,
        pd.Series(upper_band, index=ohlcv.index),
        start=period,
        exact=True,
    )


def test_superosc_vumanchu_matches_oscillator(ohlcv):
    module = _load_strategy(
        "strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py",
        "char_pso",
    )
    legacy = module._calc_supertrend_osc(ohlcv, 10, 8.0)
    high, low, close = _ohlc(ohlcv)
    preset = presets.superosc_vumanchu(high, low, close, 10, 8.0)
    _assert_series_equal(preset, legacy, exact=True)
