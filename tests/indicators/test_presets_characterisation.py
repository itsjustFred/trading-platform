"""Characterisation tests: every preset must equal the existing
implementation it is meant to replace (issue #317, phase P0).

Each test loads the *current* per-strategy / per-bot function (via
``importlib`` for strategy files, which are not packages, and via normal
imports for ``signal_bots``) and asserts the preset reproduces it on the
same deterministic OHLCV frame — including the warm-up region where the
legacy code defines one. Where a legacy function legitimately differs in
warm-up (``0.0``-filled ATR, bars seeded at index 0), the test compares
the region both define and documents the difference.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from shared.indicators import core, presets

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_strategy(relative_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, REPO_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def ohlcv() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 200
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


def _series(frame: pd.DataFrame, column: str) -> pd.Series:
    return pd.to_numeric(frame[column], errors="coerce").astype(float)


def _assert_series_equal(
    actual: pd.Series, expected: pd.Series, *, start: int = 0, rtol: float = 1e-9
) -> None:
    a = pd.to_numeric(actual, errors="coerce").astype(float).iloc[start:]
    e = pd.to_numeric(expected, errors="coerce").astype(float).iloc[start:]
    pd.testing.assert_series_equal(
        a.reset_index(drop=True),
        e.reset_index(drop=True),
        check_names=False,
        rtol=rtol,
        atol=rtol,
    )


# ── ATR: the QuantConnect "Wilder" ewm variant ─────────────────────────


@pytest.mark.parametrize(
    "strategy",
    ["gold_momentum", "cross_asset_momentum", "sector_rotation"],
)
def test_atr_qc_wilder_matches_strategy_copies(strategy, ohlcv):
    module = _load_strategy(
        f"strategies/{strategy}/{strategy}.py", f"char_{strategy}"
    )
    legacy = module._wilder_atr(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 14,
    )
    preset = presets.atr_qc_wilder(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 14,
    )
    _assert_series_equal(preset, legacy)


def test_atr_qc_wilder_matches_trend_following_inline(ohlcv):
    high, low, close = (
        _series(ohlcv, "high"), _series(ohlcv, "low"), _series(ohlcv, "close")
    )
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    legacy = tr.ewm(alpha=1.0 / 20, min_periods=20, adjust=False).mean()
    preset = presets.atr_qc_wilder(high, low, close, 20)
    _assert_series_equal(preset, legacy)


def test_atr_qc_wilder_matches_stat_arb_pairs_inline(ohlcv):
    # stat_arb_pairs computes the identical ewm ATR inline when
    # use_atr_stop is on; same formula as trend_following above.
    high, low, close = (
        _series(ohlcv, "high"), _series(ohlcv, "low"), _series(ohlcv, "close")
    )
    tr = pd.concat(
        [high - low, (high - close.shift(1)).abs(), (low - close.shift(1)).abs()],
        axis=1,
    ).max(axis=1)
    legacy = tr.ewm(alpha=1.0 / 14, min_periods=14, adjust=False).mean()
    preset = presets.atr_qc_wilder(high, low, close, 14)
    _assert_series_equal(preset, legacy)


# ── ATR: MT5 iATR and the SMA-seeded Wilder (intraday_volatility_breakout)


@pytest.mark.parametrize("period", [1, 5, 14])
def test_atr_mt5_iatr_matches_intraday_volatility_breakout(period, ohlcv):
    module = _load_strategy(
        "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py",
        "char_ivb",
    )
    tr = core.true_range(
        _series(ohlcv, "high"), _series(ohlcv, "low"), _series(ohlcv, "close")
    )
    legacy = module._mt5_atr(tr, period)
    preset = presets.atr_mt5_iatr(tr, period)
    _assert_series_equal(preset, legacy)


@pytest.mark.parametrize("period", [1, 5, 14])
def test_atr_wilder_sma_seed_matches_intraday_volatility_breakout(
    period, ohlcv
):
    module = _load_strategy(
        "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py",
        "char_ivb",
    )
    tr = core.true_range(
        _series(ohlcv, "high"), _series(ohlcv, "low"), _series(ohlcv, "close")
    )
    legacy = module._wilder_atr(tr, period)
    preset = presets.atr_wilder_sma_seed(tr, period)
    _assert_series_equal(preset, legacy)


def test_atr_skipna_variants_match_with_interior_nan(ohlcv):
    """The ``_valid_values`` contract: NaN TR positions are skipped."""
    module = _load_strategy(
        "strategies/intraday_volatility_breakout/intraday_volatility_breakout.py",
        "char_ivb",
    )
    tr = core.true_range(
        _series(ohlcv, "high"), _series(ohlcv, "low"), _series(ohlcv, "close")
    )
    tr = tr.copy()
    tr.iloc[10] = np.nan
    tr.iloc[50] = np.nan
    for period in (5, 14):
        _assert_series_equal(
            presets.atr_mt5_iatr(tr, period), module._mt5_atr(tr, period)
        )
        _assert_series_equal(
            presets.atr_wilder_sma_seed(tr, period),
            module._wilder_atr(tr, period),
        )


def test_atr_wilder_sma_seed_matches_custom_swings_calc_atr(ohlcv):
    module = _load_strategy(
        "strategies/custom_swings_supertrend/custom_swings_supertrend.py",
        "char_cs",
    )
    period = 12
    legacy = pd.Series(module._calc_atr(ohlcv, period), index=ohlcv.index)
    preset = presets.atr_wilder_sma_seed(
        core.true_range(
            _series(ohlcv, "high"), _series(ohlcv, "low"),
            _series(ohlcv, "close"), first_bar="nan",
        ),
        period,
    )
    # legacy warm-up is 0.0 where the preset emits NaN; identical after.
    _assert_series_equal(preset.fillna(0.0), legacy)


# ── ATR: rolling-mean ("SMA") variants ─────────────────────────────────


def test_atr_sma_tr_matches_channel_breakout(ohlcv):
    module = _load_strategy(
        "strategies/channel_breakout/channel_breakout.py", "char_cb"
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {"atr_period": 14})
    legacy = indicators["ATR"]
    preset = presets.atr_sma_tr(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 14,
    )
    _assert_series_equal(preset, legacy)


def test_atr_sma_tr_matches_ema_crossover(ohlcv):
    module = _load_strategy(
        "strategies/ema_crossoiver_conversion_test/ema_crossoiver_conversion_test.py",
        "char_ec",
    )
    _, _, indicators, _ = module.generate_signals(ohlcv, {"atr_period": 14})
    legacy = indicators["atr_value"]
    preset = presets.atr_sma_tr(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 14,
    )
    _assert_series_equal(preset, legacy)


def test_atr_sma_tr_zero_seed_matches_pine_dema_supertrend_atr(ohlcv):
    # _calc_supertrend builds tr via np.zeros (tr[0]=0.0) then
    # rolling(period).mean(); reproduce that ATR directly.
    close_v = ohlcv["close"].to_numpy(dtype=float)
    high_v = ohlcv["high"].to_numpy(dtype=float)
    low_v = ohlcv["low"].to_numpy(dtype=float)
    tr = np.zeros(len(close_v))
    for i in range(1, len(close_v)):
        tr[i] = max(
            high_v[i] - low_v[i],
            abs(high_v[i] - close_v[i - 1]),
            abs(low_v[i] - close_v[i - 1]),
        )
    legacy = pd.Series(tr, index=ohlcv.index).rolling(10).mean()
    preset = presets.atr_sma_tr(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 10, tr_first="zero",
    )
    _assert_series_equal(preset, legacy)


# ── ATR: EMA-span variant (live bots) ──────────────────────────────────


def test_atr_ema_span_matches_ramjet(ohlcv):
    from signal_bots.ramjet.processor import RamJetSignalProcessor

    legacy = RamJetSignalProcessor._calculate_atr(ohlcv.copy(), 14)["atr"]
    preset = presets.atr_ema_span(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 14,
    )
    _assert_series_equal(preset, legacy)


def test_atr_ema_span_matches_rocketfuel(ohlcv):
    from signal_bots.rocketfuel.processor import RocketFuelSignalProcessor

    legacy = RocketFuelSignalProcessor._calculate_atr(ohlcv.copy(), 14)["atr"]
    preset = presets.atr_ema_span(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 14,
    )
    _assert_series_equal(preset, legacy)


# ── RMA on arbitrary series (volatility_breakout / qg_30min_es) ────────


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
    series = _series(ohlcv, "close")
    legacy = module._wilder_rma(series, 9)
    preset = presets.atr_wilder_rma_first(series, 9)
    _assert_series_equal(preset, legacy)


# ── RSI variants ───────────────────────────────────────────────────────


def test_rsi_wilder_matches_gold_momentum(ohlcv):
    module = _load_strategy(
        "strategies/gold_momentum/gold_momentum.py", "char_gm"
    )
    close = _series(ohlcv, "close")
    _assert_series_equal(
        presets.rsi_wilder(close, 14), module._wilder_rsi(close, 14)
    )


def test_rsi_wilder_masked_matches_post_vix_dip_buy(ohlcv):
    module = _load_strategy(
        "strategies/post_vix_dip_buy/post_vix_dip_buy.py", "char_pv"
    )
    close = _series(ohlcv, "close")
    _assert_series_equal(
        presets.rsi_wilder_masked(close, 14), module._wilder_rsi(close, 14)
    )


def test_rsi_wilder_fill50_matches_codegen_vectorbt(ohlcv):
    """Replicates the ``_calc_rsi`` source emitted by codegen_vectorbt."""
    close = _series(ohlcv, "close")
    window = 14
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    legacy = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)
    _assert_series_equal(presets.rsi_wilder_fill50(close, window), legacy)


# ── Supertrend variants ────────────────────────────────────────────────


def test_supertrend_ramjet_matches_fallback(ohlcv):
    # RamJet branches on `hasattr(frame, "ta")`: the characterised preset
    # reproduces the hand-written fallback, which only runs when the
    # undeclared pandas_ta is absent (it is absent in every supported
    # environment — it is in no dependency file).
    if importlib.util.find_spec("pandas_ta") is not None:
        pytest.skip(
            "pandas_ta is installed: RamJet takes the library branch, "
            "which this preset does not characterise"
        )
    from signal_bots.ramjet.processor import RamJetSignalProcessor

    frame = ohlcv.copy()
    legacy = RamJetSignalProcessor._calculate_supertrend(frame, 10, 3.0)
    result = presets.supertrend_ramjet(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 10, 3.0,
    )
    _assert_series_equal((result.trend == 1).astype(float),
                         legacy["supertrend"].astype(float))
    _assert_series_equal(result.ready.astype(float),
                         legacy["supertrend_ready"].astype(float))


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
    result = presets.supertrend_custom_swings(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), period, mult,
    )
    _assert_series_equal(result.trend, pd.Series(trend_arr, index=ohlcv.index))
    # legacy band arrays are 0.0-seeded; the preset emits NaN at bar 0
    # (core contract) — identical from bar 1 on.
    _assert_series_equal(
        result.lower_band, pd.Series(up_arr, index=ohlcv.index), start=1
    )
    _assert_series_equal(
        result.upper_band, pd.Series(dn_arr, index=ohlcv.index), start=1
    )
    _assert_series_equal(result.atr, legacy_atr)


def test_supertrend_pine_dema_matches_bands(ohlcv):
    module = _load_strategy(
        "strategies/pine_supertrend_dema/pine_supertrend_dema.py", "char_pd"
    )
    period, mult = 10, 3.0
    trend, lower_band, upper_band = module._calc_supertrend(
        ohlcv, period, mult
    )
    result = presets.supertrend_pine_dema(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), period, mult,
    )
    _assert_series_equal(
        result.trend, pd.Series(trend, index=ohlcv.index).astype(float)
    )
    # legacy bands are 0.0 before the loop start (bar `period`)
    _assert_series_equal(
        result.lower_band, pd.Series(lower_band, index=ohlcv.index),
        start=period,
    )
    _assert_series_equal(
        result.upper_band, pd.Series(upper_band, index=ohlcv.index),
        start=period,
    )


def test_superosc_vumanchu_matches_oscillator(ohlcv):
    module = _load_strategy(
        "strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py",
        "char_pso",
    )
    legacy = module._calc_supertrend_osc(ohlcv, 10, 8.0)
    preset = presets.superosc_vumanchu(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 10, 8.0,
    )
    _assert_series_equal(preset, legacy)


def test_supertrend_pine_superosc_bands_match(ohlcv):
    """The band/trend recursion inside ``_calc_supertrend_osc`` is the
    ``"pine"`` ratchet with a delayed start; the oscillator test above
    covers it end-to-end, this pins the raw bands too."""
    module = _load_strategy(
        "strategies/pine_superosc_vumanchu/pine_superosc_vumanchu.py",
        "char_pso",
    )
    # Recompute the legacy bands by re-running its loop via the public
    # function is not possible (only the oscillator escapes); instead
    # assert internal consistency: spt/den reproduce `osc`.
    result = presets.supertrend_pine_superosc(
        _series(ohlcv, "high"), _series(ohlcv, "low"),
        _series(ohlcv, "close"), 10, 8.0,
    )
    legacy = module._calc_supertrend_osc(ohlcv, 10, 8.0)
    spt = result.lower_band.where(result.trend == 1, result.upper_band)
    den = result.upper_band - result.lower_band
    den = den.mask(den.abs() < 1e-10, 1e-10)
    rebuilt = ((_series(ohlcv, "close") - spt) / den).clip(-1, 1)
    _assert_series_equal(rebuilt, legacy)
