"""Named parity presets for :mod:`shared.indicators.core` (issue #317, P0).

Every preset reproduces *one existing implementation* in the repo,
byte-for-byte on NaN-free input (characterisation tests in
``tests/indicators/test_presets_characterisation.py`` pin this). The
preset name says which variant it is; the docstring says which file it
was taken from. Nothing here is a recommendation — P1+ decides which
preset (if any) replaces which copy.

ATR smoothing legend (the four meanings of "ATR" in the repo):

- ``wilder`` + ``seed="first"`` + ``min_periods=n``: pandas
  ``ewm(alpha=1/n, min_periods=n)`` seeded at ``TR[0]`` — the
  QuantConnect-ported strategies.
- ``wilder`` + ``seed="sma"`` + ``skipna``: SMA seed over the first ``n``
  valid TR values, then Wilder recursion — Pine ``ta.atr`` and the
  ``intraday_volatility_breakout`` / ``custom_swings_supertrend`` loops.
- ``sma``: rolling mean of TR — MT5 ``iATR`` (valid-window variant) and
  the ``pine_*`` ports (plain rolling variant, ``TR[0]`` seeded ``0`` or
  ``h-l``).
- ``ema`` + ``seed="first"``: ``ewm(span=n)`` seeded at ``TR[0]`` — the
  live bots' stop-distance ATR.
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from .core import (
    SupertrendResult,
    TrFirstBar,
    atr,
    atr_from_tr,
    rma,
    rsi,
    supertrend,
)

# ── ATR presets ─────────────────────────────────────────────────────────


def atr_qc_wilder(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    """``tr.ewm(alpha=1/n, min_periods=n, adjust=False)`` seeded at TR[0].

    Reproduces ``_wilder_atr`` in
    ``strategies/gold_momentum``, ``strategies/cross_asset_momentum``,
    ``strategies/sector_rotation`` and the inline copies in
    ``strategies/trend_following`` / ``strategies/stat_arb_pairs``.
    """
    return atr(
        high,
        low,
        close,
        period,
        smoothing="wilder",
        seed="first",
        min_periods=period,
    )


def atr_mt5_iatr(true_range: pd.Series, period: int) -> pd.Series:
    """MT5 ``iATR``: SMA over the last ``period`` *valid* TR values.

    Reproduces ``_mt5_atr`` in
    ``strategies/intraday_volatility_breakout`` (NaN TR positions are
    skipped, not counted).
    """
    return atr_from_tr(
        true_range, period, smoothing="sma", seed="sma", skipna=True
    )


def atr_wilder_sma_seed(true_range: pd.Series, period: int) -> pd.Series:
    """Wilder ATR with an SMA seed over the first ``period`` valid TRs.

    Reproduces ``_wilder_atr`` in
    ``strategies/intraday_volatility_breakout`` (and, with a NaN first
    bar, ``_calc_atr`` in ``strategies/custom_swings_supertrend``).
    """
    return atr_from_tr(
        true_range, period, smoothing="wilder", seed="sma", skipna=True
    )


def atr_sma_tr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    *,
    tr_first: TrFirstBar = "hl",
) -> pd.Series:
    """``true_range(...).rolling(period).mean()`` — the ``pine_*`` ports.

    Reproduces the stop/ATR series of ``strategies/channel_breakout`` and
    ``strategies/ema_crossoiver_conversion_test`` (``tr_first="hl"``)
    and the band/stop ATRs inside ``strategies/pine_supertrend_dema`` and
    ``strategies/pine_superosc_vumanchu`` (``tr_first="zero"``: their
    ``np.zeros`` TR seeds bar 0 with ``0.0``, which *is* counted in the
    first rolling window).
    """
    return atr(
        high,
        low,
        close,
        period,
        smoothing="sma",
        seed="sma",
        first_bar=tr_first,
    )


def atr_ema_span(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    """``tr.ewm(span=period, adjust=False)`` — EMA-span "ATR".

    Reproduces ``RamJetSignalProcessor._calculate_atr`` and
    ``RocketFuelSignalProcessor._calculate_atr`` (``signal_bots/``).
    Values exist from bar 0 (no warm-up mask) — a legacy behaviour the
    presets preserve; new code should not copy it.
    """
    return atr(
        high, low, close, period, smoothing="ema", seed="first"
    )


def atr_wilder_rma_first(series: pd.Series, length: int) -> pd.Series:
    """``series.ewm(alpha=1/n, adjust=False)`` with no warm-up mask.

    Reproduces ``_wilder_rma`` in ``strategies/volatility_breakout`` and
    ``strategies/qg_30min_es_breakout_strategy`` (applied there to TR,
    DM and DX series, not only to prices).
    """
    return rma(series, length, seed="first")


# ── RSI presets ────────────────────────────────────────────────────────


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI, NaN warm-up, no flat-window masking.

    Reproduces ``_wilder_rsi`` in ``strategies/gold_momentum``.
    """
    return rsi(close, period, min_periods=period, flat="none")


def rsi_wilder_masked(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI from bar 1, flat windows masked to 50/100.

    Reproduces ``_wilder_rsi`` in ``strategies/post_vix_dip_buy``.
    """
    return rsi(close, period, min_periods=None, flat="mask")


def rsi_wilder_fill50(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI, NaN warm-up filled with 50.

    Reproduces ``_calc_rsi`` emitted by
    ``runner/strategy_ir/codegen_vectorbt.py``.
    """
    return rsi(close, period, min_periods=period, flat="fill50")


# ── Supertrend presets ─────────────────────────────────────────────────


def supertrend_ramjet(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> SupertrendResult:
    """RamJet/pandas_ta-flavoured Supertrend (the fallback branch).

    Reproduces ``RamJetSignalProcessor._calculate_supertrend`` /
    ``calculate_supertrend`` in ``signal_bots/ramjet/processor.py`` *when
    ``pandas_ta`` is not installed* — which is always, since it is
    declared in no dependency file. ``result.trend == 1`` maps to the
    legacy ``supertrend`` bool column (``True`` = green); ``result.ready``
    is the legacy ``supertrend_ready`` marker.
    """
    return supertrend(
        high,
        low,
        close,
        period,
        multiplier,
        atr_smoothing="wilder",
        atr_seed="first",
        rule="ta",
    )


def supertrend_custom_swings(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> SupertrendResult:
    """Pine-style Supertrend of ``strategies/custom_swings_supertrend``.

    The legacy ``_calc_atr`` returns ``0.0`` during warm-up and the
    ``_calc_supertrend`` loop consumes it, so the bands ratchet around
    ``hl2`` before the ATR is real. The preset passes the same
    zero-filled ATR explicitly; ``result.lower_band``/``upper_band``
    correspond to the legacy ``up_band``/``dn_band``.
    """
    atr_values = atr(
        high,
        low,
        close,
        period,
        smoothing="wilder",
        seed="sma",
        first_bar="nan",
        skipna=True,
    ).fillna(0.0)
    return supertrend(
        high,
        low,
        close,
        period,
        multiplier,
        atr_smoothing="wilder",
        atr_seed="sma",
        rule="pine",
        atr=atr_values,
    )


def supertrend_pine_dema(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> SupertrendResult:
    """Supertrend of ``strategies/pine_supertrend_dema`` (cross-condition).

    Reproduces ``_calc_supertrend`` there, including the suspected port
    bug: each band only ratchets while the previous close is beyond the
    *opposite* band, and the TR seeds bar 0 with ``0.0`` inside the first
    rolling-ATR window.
    """
    return supertrend(
        high,
        low,
        close,
        period,
        multiplier,
        atr_smoothing="sma",
        atr_seed="sma",
        rule="pine_dema",
        atr_first_bar="zero",
    )


def supertrend_pine_superosc(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> SupertrendResult:
    """Band/trend recursion inside ``_calc_supertrend_osc``.

    Same band ratchet as the ``"pine"`` rule but the loop starts one bar
    after the first valid ATR (the legacy ``range(length, ...)`` start)
    and the TR seeds bar 0 with ``0.0``. The strategy consumes the
    oscillator built on top — see :func:`superosc_vumanchu`.
    """
    return supertrend(
        high,
        low,
        close,
        period,
        multiplier,
        atr_smoothing="sma",
        atr_seed="sma",
        rule="pine_superosc",
        atr_first_bar="zero",
    )


def superosc_vumanchu(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> pd.Series:
    """The ``_calc_supertrend_osc`` oscillator of ``pine_superosc_vumanchu``.

    ``clip((close - spt) / (upper - lower), -1, 1)`` with a ``1e-10``
    denominator floor, ``spt`` the active band.
    """
    result = supertrend_pine_superosc(high, low, close, period, multiplier)
    spt = result.lower_band.where(result.trend == 1, result.upper_band)
    band_width = result.upper_band - result.lower_band
    den = band_width.mask(band_width.abs() < 1e-10, 1e-10)
    osc = ((close - spt) / den).clip(-1, 1)
    return osc.astype(float)


# ── Registry of presets ────────────────────────────────────────────────

PRESETS: dict[str, Callable] = {
    "atr_qc_wilder": atr_qc_wilder,
    "atr_mt5_iatr": atr_mt5_iatr,
    "atr_wilder_sma_seed": atr_wilder_sma_seed,
    "atr_sma_tr": atr_sma_tr,
    "atr_ema_span": atr_ema_span,
    "atr_wilder_rma_first": atr_wilder_rma_first,
    "rsi_wilder": rsi_wilder,
    "rsi_wilder_masked": rsi_wilder_masked,
    "rsi_wilder_fill50": rsi_wilder_fill50,
    "supertrend_ramjet": supertrend_ramjet,
    "supertrend_custom_swings": supertrend_custom_swings,
    "supertrend_pine_dema": supertrend_pine_dema,
    "supertrend_pine_superosc": supertrend_pine_superosc,
    "superosc_vumanchu": superosc_vumanchu,
}
