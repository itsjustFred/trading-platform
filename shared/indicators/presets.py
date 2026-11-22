"""Named parity presets for :mod:`shared.indicators.core` (issue #317, P0).

Every preset reproduces *one existing implementation* in the repo; the
characterisation tests in ``tests/indicators/test_presets_characterisation.py``
pin the parity (bit-exact where the legacy copy is itself a pure pandas/NumPy
formula, tolerance only where a docstring says so). The preset name says
which variant it is; the docstring says which file it was taken from.
Nothing here is a recommendation — P1+ decides which preset (if any)
replaces which copy.

ATR smoothing legend (the four meanings of "ATR" in the repo):

- ``wilder`` + ``seed="first"`` + ``min_periods=n``: pandas
  ``ewm(alpha=1/n, min_periods=n)`` seeded at ``TR[0]`` — the
  QuantConnect-ported strategies.
- ``wilder`` + ``seed="sma"`` + ``skipna``: SMA seed over the first ``n``
  valid TR values, then Wilder recursion — Pine ``ta.atr`` and the
  ``intraday_volatility_breakout`` loop.
- ``sma``: rolling mean of TR — MT5 ``iATR`` (valid-window variant) and
  the legacy ``atr_sma_tr`` parity path (plain rolling variant,
  ``TR[0]`` seeded ``0`` or ``h-l``). The ``pine_*`` ports now use
  :func:`atr_pine_ta` (``wilder`` + ``seed="sma"`` + ``first_bar="hl"``).
- ``ema`` + ``seed="first"``: ``ewm(span=n)`` seeded at ``TR[0]`` — the
  live bots' stop-distance ATR. Pine ``ta.ema`` is ``ema_pine_ta``
  (``seed="sma"``).
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .core import (
    SupertrendResult,
    TrFirstBar,
    atr,
    atr_from_tr,
    ema,
    rma,
    rsi,
    sma,
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
        first_bar="hl",
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
    ``strategies/intraday_volatility_breakout``. ``custom_swings_supertrend``
    uses the same Wilder maths but a different operand order and a
    zero-filled warm-up; :func:`supertrend_custom_swings` carries that exact
    parity path instead of routing it through this preset.
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
    tr_first: TrFirstBar,
) -> pd.Series:
    """``true_range(...).rolling(period).mean()`` — the legacy SMA-ATR path.

    Reproduces the stop/ATR series of ``strategies/channel_breakout``
    (``tr_first="hl"``). The ``pine_*`` ports moved to
    :func:`atr_pine_ta` (Pine ``ta.atr`` is Wilder/RMA, not a rolling SMA).
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


def atr_pine_ta(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    """Pine ``ta.atr(period)`` — Wilder/RMA, SMA seed, ``high-low`` bar 0.

    This is the ATR the ``pine_*`` ports were always meant to compute; the
    legacy copies used a rolling SMA of a zero-seeded true range instead.
    """
    return atr(
        high,
        low,
        close,
        period,
        smoothing="wilder",
        seed="sma",
        first_bar="hl",
    )


def atr_ema_span(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int
) -> pd.Series:
    """``tr.ewm(span=period, adjust=False)`` — EMA-span "ATR".

    Reproduces ``RamJetSignalProcessor._calculate_atr`` and
    ``RocketFuelSignalProcessor._calculate_atr`` (``signal_bots/``).
    Values exist from bar 0 (no warm-up mask) — a legacy behaviour the
    presets preserve via ``min_periods=0``; new code should not copy it.
    """
    return atr(
        high,
        low,
        close,
        period,
        smoothing="ema",
        seed="first",
        first_bar="hl",
        min_periods=0,
    )


def atr_wilder_rma_first(series: pd.Series, length: int) -> pd.Series:
    """``series.ewm(alpha=1/n, adjust=False)`` with no warm-up mask.

    Reproduces ``_wilder_rma`` in ``strategies/volatility_breakout`` and
    ``strategies/qg_30min_es_breakout_strategy`` (applied there to TR,
    DM and DX series, not only to prices). ``min_periods=0`` preserves the
    legacy emit-from-bar-0 behaviour explicitly.
    """
    return rma(series, length, seed="first", min_periods=0)


def ema_first_seed(series: pd.Series, span: int) -> pd.Series:
    """``series.ewm(span=span, adjust=False)`` with no warm-up mask.

    Reproduces the ``EMA_*`` series in ``strategies/trend_following`` and
    the ``close.ewm(span=..., adjust=False)`` line emitted by
    ``runner/strategy_ir/codegen_vectorbt.py`` for EMA indicators.
    ``min_periods=0`` preserves the legacy emit-from-bar-0 behaviour
    explicitly; new code should not copy it.
    """
    return ema(series, span, seed="first", min_periods=0)


def ema_pine_ta(series: pd.Series, length: int) -> pd.Series:
    """Pine ``ta.ema(source, length)`` — SMA seed, NaN warm-up.

    The ``pine_*`` ports used ``ewm(span=length, adjust=False)`` seeded at
    the first value; TradingView seeds the recursion at the SMA of the
    first ``length`` values instead.
    """
    return ema(series, length, seed="sma")


def dema_pine_ta(series: pd.Series, length: int) -> pd.Series:
    """Double EMA ``2 * ta.ema(src, l) - ta.ema(ta.ema(src, l), l)``.

    The ``pine_supertrend_dema`` port used two first-seeded ``ewm`` calls;
    TradingView's double-EMA uses the SMA-seeded ``ta.ema`` at both stages.
    """
    first = ema_pine_ta(series, length)
    second = ema_pine_ta(first, length)
    return (2.0 * first - second).rename(series.name)


# ── RSI presets ────────────────────────────────────────────────────────


def rsi_wilder(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI, NaN warm-up, no flat-window masking.

    Reproduces ``_wilder_rsi`` in ``strategies/gold_momentum``.
    """
    return rsi(close, period, min_periods=period, flat="none")


def rsi_wilder_masked(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI from bar 1, flat windows masked to 50/100.

    Reproduces ``_wilder_rsi`` in ``strategies/post_vix_dip_buy``.
    ``min_periods=0`` preserves the legacy no-warm-up ``ewm`` call.
    """
    return rsi(close, period, min_periods=0, flat="mask")


def rsi_wilder_fill50(close: pd.Series, period: int) -> pd.Series:
    """Wilder RSI, NaN warm-up filled with 50.

    Reproduces ``_calc_rsi`` emitted by
    ``runner/strategy_ir/codegen_vectorbt.py``.
    """
    return rsi(close, period, min_periods=period, flat="fill50")


# ── Supertrend / oscillator presets ────────────────────────────────────


def supertrend_pine_ta(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> SupertrendResult:
    """TradingView-exact manual Supertrend for the ``pine_*`` strategies.

    ``ta.atr(period)`` is Wilder/RMA with an SMA seed over the first
    ``period`` valid true ranges and ``ta.tr(true)`` first-bar ``high-low``;
    bands stay NaN through warm-up, ratchet on the previous close, and the
    direction flips on the *current* close versus the previous-band
    references (``up1``/``dn1``), exactly as the manual Pine Supertrend
    does.
    """
    return supertrend(
        high,
        low,
        close,
        period,
        multiplier,
        atr_smoothing="wilder",
        atr_seed="sma",
        rule="pine",
        atr_first_bar="hl",
        atr_skipna=False,
    )


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
    is the legacy ``supertrend_ready`` marker. ``atr_min_periods=0`` keeps
    the legacy emit-from-bar-0 ``ewm`` ATR.
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
        atr_first_bar="hl",
        atr_min_periods=0,
    )


def supertrend_chart(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 7,
    multiplier: float = 3.0,
) -> SupertrendResult:
    """Viewer overlay Supertrend (was ``pandas_ta.ta.supertrend(7, 3)``).

    Same ``"ta"`` band recursion and Wilder-RMA ATR as
    :func:`supertrend_ramjet` — pandas_ta was never importable in a
    supported environment, so there is no library output to preserve; the
    chart plots the active band (``lower_band`` in an uptrend,
    ``upper_band`` otherwise) masked to ``ready`` bars. Its outputs are the
    RamJet semantics, including the legacy NaN-band ``or``-chain.
    """
    return supertrend_ramjet(high, low, close, period, multiplier)


def supertrend_custom_swings(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> SupertrendResult:
    """Pine ``ta.atr`` Supertrend of ``strategies/custom_swings_supertrend``.

    The legacy ``_calc_atr``/``_calc_supertrend`` claimed ``ta.atr`` parity
    but seeded TR with ``0.0`` and ratcheted bands around ``hl2`` through
    warm-up; this preset uses the real ``ta.atr`` and NaN bands instead.
    ``result.lower_band``/``upper_band`` correspond to the legacy
    ``up_band``/``dn_band``.
    """
    return supertrend_pine_ta(high, low, close, period, multiplier)


def supertrend_pine_dema(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> SupertrendResult:
    """Pine ``ta.atr`` Supertrend of ``strategies/pine_supertrend_dema``.

    The legacy ``_calc_supertrend`` used a rolling SMA of a zero-seeded TR
    and the suspected opposite-band ratchet; this preset is the
    TradingView-faithful manual recursion.
    """
    return supertrend_pine_ta(high, low, close, period, multiplier)


def superosc_luxalgo(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
) -> pd.Series:
    """LuxAlgo ``SuperTrend Oscillator`` main oscillator.

    Reproduces the ``upper``/``lower``/``trend``/``Spt``/``osc`` chain of
    the ``pine_superosc_vumanchu`` source (manual Pine conversion from
    indikatoren-repo): ``ta.atr`` bands off ``hl2``, same-band ratchets on
    the previous close, direction flips on the current close versus the
    *previous* bands (1 = bullish/lower, 0 = bearish/upper, NaN until the
    first flip), and ``clip((close - Spt) / (upper - lower), -1, 1)`` with
    Pine safe division (zero width -> NaN).

    Source caveat: community ports of the public LuxAlgo script
    (TradingView ``dVau7zqn``) flip the direction against the *current*
    bar's bands; this preset keeps the previous-band flip because that is
    what the strategy's own source computes. Whether the indikatoren-repo
    Pine original matches LuxAlgo or the legacy Python is an open question
    for the maintainer (#317 P3).
    """
    atr_v = atr_pine_ta(high, low, close, period).to_numpy(dtype=float)
    hl2_v = ((high + low) / 2.0).to_numpy(dtype=float)
    close_v = close.to_numpy(dtype=float)
    n = len(close_v)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    trend = np.full(n, np.nan)
    prev_upper = np.nan
    prev_lower = np.nan
    prev_trend = np.nan
    for i in range(n):
        a = atr_v[i]
        src_i = hl2_v[i]
        close_i = close_v[i]
        close_prev = close_v[i - 1] if i > 0 else np.nan
        raw_up = (
            src_i + multiplier * a
            if not (np.isnan(src_i) or np.isnan(a))
            else np.nan
        )
        raw_lo = (
            src_i - multiplier * a
            if not (np.isnan(src_i) or np.isnan(a))
            else np.nan
        )
        if (
            not np.isnan(raw_up)
            and not np.isnan(prev_upper)
            and not np.isnan(close_prev)
            and close_prev < prev_upper
        ):
            upper[i] = min(raw_up, prev_upper)
        else:
            upper[i] = raw_up
        if (
            not np.isnan(raw_lo)
            and not np.isnan(prev_lower)
            and not np.isnan(close_prev)
            and close_prev > prev_lower
        ):
            lower[i] = max(raw_lo, prev_lower)
        else:
            lower[i] = raw_lo
        if (
            not np.isnan(close_i)
            and not np.isnan(prev_upper)
            and close_i > prev_upper
        ):
            trend[i] = 1.0
        elif (
            not np.isnan(close_i)
            and not np.isnan(prev_lower)
            and close_i < prev_lower
        ):
            trend[i] = 0.0
        else:
            trend[i] = prev_trend
        prev_upper = upper[i]
        prev_lower = lower[i]
        prev_trend = trend[i]
    spt = trend * lower + (1.0 - trend) * upper
    width = upper - lower
    with np.errstate(divide="ignore", invalid="ignore"):
        osc = np.where(width == 0, np.nan, (close_v - spt) / width)
    osc = np.clip(osc, -1.0, 1.0)
    return pd.Series(osc, index=close.index, name="superosc", dtype=float)


def wavetrend_vumanchu(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    channel_len: int,
    avg_len: int,
    ma_len: int,
) -> tuple[pd.Series, pd.Series]:
    """VuManChu WaveTrend ``(wt1, wt2)`` on Pine ``ta.ema``/``ta.sma``.

    Verbatim from the public Cipher B Pine source (``f_wavetrend``):
    ``esa``/``de`` use the SMA-seeded ``ta.ema`` at ``channel_len``;
    ``wt1`` is ``ta.ema(ci, avg_len)`` and ``wt2`` is ``ta.sma(wt1,
    ma_len)``. Division by a zero ``0.015 * de`` follows Pine safe
    division and yields NaN.
    """
    hlc3 = (high + low + close) / 3.0
    esa = ema_pine_ta(hlc3, channel_len)
    de = ema_pine_ta((hlc3 - esa).abs(), channel_len)
    den = 0.015 * de
    ci = (hlc3 - esa) / den.where(den != 0)
    wt1 = ema_pine_ta(ci, avg_len)
    wt2 = sma(wt1, ma_len)
    return wt1.rename("wt1"), wt2.rename("wt2")


# ── Registry of presets ────────────────────────────────────────────────

PRESETS: dict[str, Callable] = {
    "atr_qc_wilder": atr_qc_wilder,
    "atr_mt5_iatr": atr_mt5_iatr,
    "atr_wilder_sma_seed": atr_wilder_sma_seed,
    "atr_sma_tr": atr_sma_tr,
    "atr_pine_ta": atr_pine_ta,
    "atr_ema_span": atr_ema_span,
    "atr_wilder_rma_first": atr_wilder_rma_first,
    "ema_first_seed": ema_first_seed,
    "ema_pine_ta": ema_pine_ta,
    "dema_pine_ta": dema_pine_ta,
    "rsi_wilder": rsi_wilder,
    "rsi_wilder_masked": rsi_wilder_masked,
    "rsi_wilder_fill50": rsi_wilder_fill50,
    "supertrend_ramjet": supertrend_ramjet,
    "supertrend_chart": supertrend_chart,
    "supertrend_pine_ta": supertrend_pine_ta,
    "supertrend_custom_swings": supertrend_custom_swings,
    "supertrend_pine_dema": supertrend_pine_dema,
    "superosc_luxalgo": superosc_luxalgo,
    "wavetrend_vumanchu": wavetrend_vumanchu,
}
