"""Shared technical indicators (issue #317, phase P0).

Pure ``pd.Series`` -> ``pd.Series`` functions (same index) that every
strategy, signal bot, replay path and the viewer can share instead of
carrying private copies.

Contract (pinned by ``tests/indicators/``):

- ``smoothing`` and ``seed`` are required keyword arguments with no
  defaults: "ATR" and "EMA" mean different things on different parity
  targets (MT5 iATR is an SMA of TR, Pine/QuantConnect ATR is Wilder-RMA,
  the live bots use an EMA span), so a caller must always name the variant.
  ``first_bar`` (``true_range``/``atr``/``supertrend``) and ``flat``
  (``rsi``) are required for the same reason: each selects a different
  legacy edge-case behaviour.
- Warm-up bars are ``NaN``, never ``0.0``: a zero ATR produces zero-width
  stops. ``min_periods`` defaults to the indicator length/period; legacy
  copies that emit from bar 0 must pass ``min_periods=0`` explicitly, and
  parity presets that reproduce a legacy zero-fill say so in their
  docstring and pass the legacy warm-up explicitly.
- No internal shift: the value at bar ``t`` uses bars ``<= t`` only.
  Callers that act on the *next* bar apply ``lagged(x)`` explicitly.
- NaN handling is part of each function's documented semantics; the
  ``skipna`` ATR variants skip NaN true-range positions entirely (the
  ``_valid_values`` behaviour of ``intraday_volatility_breakout``).

Performance (issue #378): the sequential recursions behind
:func:`supertrend` (``rule="pine"``, ``"pine_dema"``, ``"pine_superosc"``
and ``"ta"``) run as pure-NumPy kernels (``*_kernel_py``) that are also
compiled with ``numba.njit(cache=True, fastmath=False)`` when numba is
importable — see :mod:`shared.indicators._numba` for the dispatch and
fallback rules (``TP_DISABLE_NUMBA=1`` forces the Python path). The two
paths are pinned bit-identical by ``tests/indicators/test_numba_parity.py``;
all NaN, dtype and index-alignment semantics live in the Python wrappers
and are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from . import _numba

#: How a recursive smoother weights history.
#: - ``"sma"``: plain rolling mean (MT5 ``iATR``, several Pine ports).
#: - ``"wilder"``: Wilder/RMA recursion ``alpha = 1 / n`` (Pine ``ta.atr``,
#:   QuantConnect ATR, ``ta.rsi`` gain/loss averages).
#: - ``"ema"``: exponential moving average ``alpha = 2 / (span + 1)``
#:   (the live bots' stop-distance ATR).
Smoothing = Literal["sma", "wilder", "ema"]

#: Where a recursive smoother starts.
#: - ``"first"``: the recursion seeds at the first observation itself
#:   (``x[0]``), the pandas ``ewm(adjust=False)`` behaviour.
#: - ``"sma"``: the recursion seeds at the SMA of the first ``n``
#:   observations (Pine ``ta.rma``/``ta.ema``, MT5-style indicators).
Seed = Literal["first", "sma"]

#: How the first true-range bar is seeded.
#: - ``"hl"``: ``high - low`` (the pandas ``max(axis=1)`` skipna result every
#:   OHLC copy produces, because the previous close is NaN).
#: - ``"zero"``: ``0.0`` (the ``np.zeros`` warm-up of the ``pine_*`` ports).
#: - ``"nan"``: ``NaN`` (excluded from ``skipna`` windows, matching the
#:   ``custom_swings_supertrend`` loop that never reads ``tr[0]``).
TrFirstBar = Literal["hl", "zero", "nan"]


def _as_float_series(series: pd.Series, name: str) -> pd.Series:
    out = pd.to_numeric(series, errors="coerce").astype(float)
    out.name = name
    return out


def _positive_int(value: object, name: str) -> int:
    """Fail closed on non-positive indicator lengths.

    The legacy copies raised (``alpha=1/period``, ``ewm(span=0)`` or
    ``rolling(0)``); silently clamping to 1 would change stop sizing and
    band widths, so every public length goes through this guard.
    """
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if result < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return result


def lagged(series: pd.Series, periods: int = 1) -> pd.Series:
    """Shift ``series`` forward by ``periods`` bars (value usable next bar)."""
    return series.shift(periods)


def true_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    first_bar: TrFirstBar,
) -> pd.Series:
    """True range: ``max(h-l, |h-prev_close|, |l-prev_close|)``.

    ``first_bar`` controls only bar 0, where the previous close is NaN:
    ``"hl"`` leaves ``high - low`` (pandas ``max(axis=1)`` skips the NaN gap
    terms), ``"zero"`` writes ``0.0`` and ``"nan"`` writes ``NaN``.
    """
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr = _as_float_series(tr, "true_range")
    if len(tr) and first_bar != "hl":
        tr.iloc[0] = 0.0 if first_bar == "zero" else np.nan
    return tr


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average; NaN until ``window`` observations exist."""
    window = _positive_int(window, "window")
    return _as_float_series(series, series.name).rolling(
        window, min_periods=window
    ).mean()


def _valid_positions(values: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """(values, positions) of the non-NaN entries, in order."""
    arr = values.to_numpy(dtype=float)
    positions = np.flatnonzero(~np.isnan(arr))
    return arr[positions], positions


def _rma_valid(
    values: pd.Series, length: int, *, seed: Seed
) -> pd.Series:
    """Wilder RMA over the non-NaN positions only (NaN bars are skipped).

    ``seed="sma"`` reproduces ``intraday_volatility_breakout._wilder_atr``:
    SMA over the first ``length`` valid values, then
    ``(prev * (n - 1) + x) / n`` stepping only across valid positions.
    ``seed="first"`` seeds the recursion at the first valid value itself and
    emits from that bar — there is no ``length``-bar warm-up for that seed.
    """
    length = _positive_int(length, "length")
    out_values = np.full(len(values), np.nan)
    valid, positions = _valid_positions(values)
    if seed == "sma":
        if len(valid) < length:
            return pd.Series(
                out_values, index=values.index, name=values.name, dtype=float
            )
        previous = values.iloc[positions[:length]].mean()
        out_values[positions[length - 1]] = previous
        start = length
    else:
        if len(valid) == 0:
            return pd.Series(
                out_values, index=values.index, name=values.name, dtype=float
            )
        previous = valid[0]
        out_values[positions[0]] = previous
        start = 1
    for k in range(start, len(valid)):
        position = positions[k]
        previous = (
            (previous * (length - 1)) + values.iloc[position]
        ) / length
        out_values[position] = previous
    return pd.Series(
        out_values, index=values.index, name=values.name, dtype=float
    )


def _sma_valid(values: pd.Series, window: int) -> pd.Series:
    """SMA over the last ``window`` *valid* values (``_mt5_atr`` semantics)."""
    window = _positive_int(window, "window")
    out_values = np.full(len(values), np.nan)
    valid, positions = _valid_positions(values)
    if len(valid) < window:
        return pd.Series(
            out_values, index=values.index, name=values.name, dtype=float
        )
    window_means = sliding_window_view(valid, window).mean(axis=1)
    out_values[positions[window - 1 :]] = window_means
    return pd.Series(
        out_values, index=values.index, name=values.name, dtype=float
    )


def _ema_valid(
    values: pd.Series, span: int, *, seed: Seed, min_periods: int | None
) -> pd.Series:
    """EMA recursion ``alpha = 2 / (span + 1)`` over non-NaN positions.

    ``min_periods`` counts *valid* observations in this skipna path (the
    pandas ``ewm`` path counts rows instead; the two agree on
    NaN-free input).
    """
    span = _positive_int(span, "span")
    alpha = 2.0 / (span + 1)
    valid, positions = _valid_positions(values)
    need = span if min_periods is None else max(0, int(min_periods))
    out_values = np.full(len(values), np.nan)
    if seed == "sma":
        if len(valid) < max(span, need):
            return pd.Series(
                out_values, index=values.index, name=values.name, dtype=float
            )
        previous = valid[:span].mean()
        if span >= need:
            out_values[positions[span - 1]] = previous
        start = span
    else:
        if len(valid) == 0:
            return pd.Series(
                out_values, index=values.index, name=values.name, dtype=float
            )
        previous = valid[0]
        if need <= 1:
            out_values[positions[0]] = previous
        start = 1
    for k in range(start, len(valid)):
        previous = alpha * valid[k] + (1.0 - alpha) * previous
        if k + 1 >= need:
            out_values[positions[k]] = previous
    return pd.Series(
        out_values, index=values.index, name=values.name, dtype=float
    )


def rma(
    series: pd.Series,
    length: int,
    *,
    seed: Seed,
    min_periods: int | None = None,
    skipna: bool = False,
) -> pd.Series:
    """Wilder smoothing ``alpha = 1 / length`` (a.k.a. RMA / ``ta.rma``).

    ``seed="first"`` reproduces ``series.ewm(alpha=1/length,
    adjust=False, min_periods=min_periods)``: the recursion seeds at the
    first observation and NaN inputs carry the previous value forward.
    ``seed="sma"`` seeds at the SMA of the first ``length`` values (Pine
    ``ta.rma``); NaN positions are skipped and get NaN output.
    ``skipna=True`` forces the skip-NaN valid-window recursion for both
    seeds (the ``intraday_volatility_breakout`` contract).

    ``min_periods`` defaults to ``length`` for ``seed="first"`` on the
    pandas ``ewm`` path. It is rejected for ``seed="sma"`` and for
    ``skipna=True``: those paths define warm-up by the valid positions
    themselves, so a separate ``min_periods`` would be silently ignored.
    """
    values = _as_float_series(series, series.name)
    length = _positive_int(length, "length")
    if seed == "sma" and min_periods is not None:
        raise ValueError("min_periods is not supported for seed='sma'")
    if skipna and min_periods is not None:
        raise ValueError("min_periods is not supported with skipna=True")
    if seed == "first" and not skipna:
        return values.ewm(
            alpha=1.0 / length,
            min_periods=length if min_periods is None else int(min_periods),
            adjust=False,
        ).mean()
    return _rma_valid(values, length, seed=seed)


def ema(
    series: pd.Series,
    span: int,
    *,
    seed: Seed,
    min_periods: int | None = None,
    skipna: bool = False,
) -> pd.Series:
    """Exponential moving average ``alpha = 2 / (span + 1)``.

    ``seed="first"`` reproduces ``series.ewm(span=span, adjust=False,
    min_periods=min_periods)``. ``seed="sma"`` seeds at the SMA of the
    first ``span`` values (Pine ``ta.ema``). ``skipna=True`` uses the
    valid-window recursion. ``min_periods`` defaults to ``span``.
    """
    values = _as_float_series(series, series.name)
    span = _positive_int(span, "span")
    if seed == "first" and not skipna:
        return values.ewm(
            span=span,
            min_periods=span if min_periods is None else int(min_periods),
            adjust=False,
        ).mean()
    return _ema_valid(values, span, seed=seed, min_periods=min_periods)


def atr_from_tr(
    true_range: pd.Series,
    period: int,
    *,
    smoothing: Smoothing,
    seed: Seed,
    min_periods: int | None = None,
    skipna: bool = False,
) -> pd.Series:
    """ATR from a precomputed true-range series.

    ``smoothing`` selects the smoother (see :data:`Smoothing`); ``seed``
    selects first-value vs SMA seeding for the recursive smoothers and is
    ignored for ``"sma"``. ``skipna=True`` reproduces the
    ``intraday_volatility_breakout`` valid-window semantics: NaN true-range
    positions are skipped entirely and produce NaN output. ``min_periods``
    is forwarded to the recursive smoothers; it is rejected wherever the
    selected path would ignore it (``"sma"`` always, and ``"wilder"`` with
    ``skipna=True``).
    """
    values = _as_float_series(true_range, "atr")
    period = _positive_int(period, "period")
    if skipna:
        if smoothing == "sma":
            if min_periods is not None:
                raise ValueError(
                    "min_periods is not supported for smoothing='sma'"
                )
            return _sma_valid(values, period)
        if smoothing == "wilder":
            if min_periods is not None:
                raise ValueError(
                    "min_periods is not supported for smoothing='wilder' "
                    "with skipna=True"
                )
            return _rma_valid(values, period, seed=seed)
        return _ema_valid(values, period, seed=seed, min_periods=min_periods)
    if smoothing == "sma":
        if min_periods is not None:
            raise ValueError(
                "min_periods is not supported for smoothing='sma'"
            )
        return sma(values, period)
    if smoothing == "wilder":
        return rma(values, period, seed=seed, min_periods=min_periods)
    return ema(values, period, seed=seed, min_periods=min_periods)


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    *,
    smoothing: Smoothing,
    seed: Seed,
    first_bar: TrFirstBar,
    min_periods: int | None = None,
    skipna: bool = False,
) -> pd.Series:
    """Average True Range over ``high``/``low``/``close``.

    See :func:`atr_from_tr` for the smoothing/seed/skipna contract;
    ``first_bar`` selects the bar-0 true-range seed (see
    :func:`true_range`).
    """
    tr = true_range(high, low, close, first_bar=first_bar)
    return atr_from_tr(
        tr,
        period,
        smoothing=smoothing,
        seed=seed,
        min_periods=min_periods,
        skipna=skipna,
    )


@dataclass(frozen=True)
class BollingerBands:
    """Bollinger Bands: ``mid`` is the SMA, ``upper``/``lower`` are
    ``mid +/- std_mult * std`` with the population-window sample std
    (pandas ``rolling.std``, ddof=1)."""

    mid: pd.Series
    upper: pd.Series
    lower: pd.Series


def bbands(
    close: pd.Series, window: int, *, std_mult: float
) -> BollingerBands:
    """Bollinger Bands over ``close``; all three legs are NaN during warm-up."""
    window = _positive_int(window, "window")
    values = _as_float_series(close, close.name)
    mid = sma(values, window)
    std = values.rolling(window, min_periods=window).std()
    return BollingerBands(
        mid=mid,
        upper=mid + float(std_mult) * std,
        lower=mid - float(std_mult) * std,
    )


@dataclass(frozen=True)
class MacdResult:
    """MACD: ``macd`` is the fast/slow EMA spread, ``signal`` its EMA and
    ``histogram`` their difference. EMA legs are first-seeded but keep the
    ADR warm-up: values are NaN until each leg has its span of observations."""

    macd: pd.Series
    signal: pd.Series
    histogram: pd.Series


def macd(
    close: pd.Series, fast: int, slow: int, signal: int
) -> MacdResult:
    """MACD(fast, slow, signal) with first-seeded EMA legs and NaN warm-up."""
    values = _as_float_series(close, close.name)
    fast_span = _positive_int(fast, "fast")
    slow_span = _positive_int(slow, "slow")
    signal_span = _positive_int(signal, "signal")
    line = ema(values, fast_span, seed="first") - ema(
        values, slow_span, seed="first"
    )
    sig = ema(line, signal_span, seed="first")
    return MacdResult(macd=line, signal=sig, histogram=line - sig)


def rsi(
    close: pd.Series,
    period: int,
    *,
    min_periods: int | None = None,
    flat: Literal["none", "mask", "fill50"],
) -> pd.Series:
    """Wilder RSI ``100 - 100 / (1 + avg_gain / avg_loss)``.

    ``min_periods`` is forwarded to the gain/loss ``ewm`` calls and defaults
    to ``period`` (NaN warm-up). Pass ``0`` for the legacy variants that emit
    from the first valid delta.

    ``flat`` selects the zero-range edge-case handling, which differs
    across the existing copies:

    - ``"none"``: ``avg_loss == 0`` becomes NaN in ``rs`` and RSI stays
      NaN (``gold_momentum._wilder_rsi``).
    - ``"mask"``: an all-flat window (no gain and no loss) reports 50 and
      a no-loss window with gains reports 100 (``post_vix_dip_buy``).
    - ``"fill50"``: every NaN RSI (warm-up and flat windows) becomes 50
      (``runner/strategy_ir/codegen_vectorbt._calc_rsi``).
    """
    values = _as_float_series(close, "rsi")
    period = _positive_int(period, "period")
    delta = values.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    mp = period if min_periods is None else int(min_periods)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=mp, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=mp, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = _as_float_series(out, "rsi")
    if flat == "mask":
        all_zero = (avg_gain == 0) & (avg_loss == 0)
        out = out.mask(all_zero.fillna(False), 50.0)
        out = out.mask(((avg_loss == 0) & (avg_gain > 0)).fillna(False), 100.0)
    elif flat == "fill50":
        out = out.fillna(50.0)
    return out


#: Which band/flip recursion a :func:`supertrend` call runs. Each value
#: reproduces one existing implementation; see ``presets.py`` for the
#: named parity profiles and ``tests/indicators`` for the
#: characterisation evidence.
SupertrendRule = Literal["pine", "pine_dema", "pine_superosc", "ta"]


@dataclass(frozen=True)
class SupertrendResult:
    """Supertrend output.

    ``trend`` is ``+1`` (up/bullish) or ``-1`` (down/bearish), ``NaN``
    where the rule leaves the direction undefined. ``upper_band`` is the
    band on the ``hl2 + mult * atr`` side (resistance in an uptrend),
    ``lower_band`` the ``hl2 - mult * atr`` side (support). ``atr`` is the
    ATR series the bands were built from (the caller-supplied one when
    ``atr=`` was passed). ``ready`` marks bars whose direction is
    meaningful; consumers must not trade the direction while it is
    ``False``.
    """

    trend: pd.Series
    upper_band: pd.Series
    lower_band: pd.Series
    atr: pd.Series
    ready: pd.Series


def _supertrend_pine_kernel_py(
    src: np.ndarray,
    close: np.ndarray,
    atr_values: np.ndarray,
    multiplier: float,
    cross: int,
    first_i: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Pine-style band ratchet + previous-band flip (reference kernel).

    ``cross == 0`` is the ``band_condition="same"`` rule of
    ``custom_swings_supertrend`` / ``pine_superosc_vumanchu``: each band
    only ratchets while the previous close is beyond *that* band.
    ``cross == 1`` is the ``"cross"`` rule of ``pine_supertrend_dema``:
    a band only ratchets while the previous close is beyond the *opposite*
    band. ``cross`` is an int (not the ``Literal``) so the same source
    compiles under numba.

    Written with explicit comparisons instead of ``max``/``min`` so the
    NaN semantics are identical in CPython and in the numba-compiled copy
    (``b > a`` is False for NaN on both paths, matching ``max(a, b)``
    returning ``a``).
    """
    n = len(close)
    lower = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    trend = np.full(n, np.nan)
    # Both legacy Pine ports initialise trend to +1 and leave it untouched
    # before the first computed bar.
    trend[: max(first_i, 1)] = 1.0
    prev_trend = 1.0
    for i in range(first_i, n):
        a = atr_values[i]
        if np.isnan(a):
            trend[i] = prev_trend
            continue
        raw_low = src[i] - multiplier * a
        raw_high = src[i] + multiplier * a
        close_prev = close[i - 1]
        lower_prev = lower[i - 1]
        upper_prev = upper[i - 1]
        if cross == 0:
            low_ok = close_prev > lower_prev
            high_ok = close_prev < upper_prev
        else:
            low_ok = close_prev > upper_prev
            high_ok = close_prev < lower_prev
        if low_ok and not np.isnan(lower_prev) and lower_prev > raw_low:
            lower[i] = lower_prev
        else:
            lower[i] = raw_low
        if high_ok and not np.isnan(upper_prev) and upper_prev < raw_high:
            upper[i] = upper_prev
        else:
            upper[i] = raw_high
        close_i = close[i]
        if prev_trend < 0.0 and close_i > upper_prev:
            new_trend = 1.0
        elif prev_trend > 0.0 and close_i < lower_prev:
            new_trend = -1.0
        else:
            new_trend = prev_trend
        trend[i] = new_trend
        prev_trend = new_trend
    return trend, upper, lower


_supertrend_pine_kernel_nb = _numba.compile_kernel(_supertrend_pine_kernel_py)


def _supertrend_pine(
    src: np.ndarray,
    close: np.ndarray,
    atr_values: np.ndarray,
    multiplier: float,
    *,
    band_condition: Literal["same", "cross"],
    first_i: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Shared Pine-style band ratchet + previous-band flip.

    ``band_condition="same"`` is the ``custom_swings_supertrend`` /
    ``pine_superosc_vumanchu`` rule: each band only ratchets while the
    previous close is beyond *that* band. ``"cross"`` is the
    ``pine_supertrend_dema`` rule: a band only ratchets while the
    previous close is beyond the *opposite* band.
    """
    if first_i < 0:
        raise ValueError(f"first_i must be >= 0, got {first_i!r}")
    return _numba.call(
        _supertrend_pine_kernel_py,
        _supertrend_pine_kernel_nb,
        src,
        close,
        atr_values,
        multiplier,
        1 if band_condition == "cross" else 0,
        first_i,
    )


def _supertrend_ta_kernel_py(
    src: np.ndarray,
    close: np.ndarray,
    atr_values: np.ndarray,
    multiplier: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """The RamJet/pandas_ta band recursion (fail-closed pre-warm-up).

    Reference kernel for :func:`_supertrend_ta`; see its docstring for the
    semantics. Returns ``(trend, upper, lower, first_valid)`` where
    ``first_valid`` is the first bar index with a non-NaN ``basic_upper``,
    or ``-1`` when there is none (the wrapper maps that to ``None``).

    Written with explicit comparisons instead of helper calls so the same
    source compiles under numba; the NaN behaviour is identical to the
    scalar helpers it replaces: every comparison against NaN is False, so
    a NaN basic band can be selected into ``upper``/``lower`` (poisoning
    that band) and NaN operands in the direction step read bearish and
    seed the next step.
    """
    n = len(close)
    basic_upper = src + multiplier * atr_values
    basic_lower = src - multiplier * atr_values
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    trend = np.full(n, -1.0)

    first_valid = -1
    for i in range(n):
        if not np.isnan(basic_upper[i]):
            first_valid = i
            break
    if first_valid < 0:
        return trend, upper, lower, -1

    upper[first_valid] = basic_upper[first_valid]
    lower[first_valid] = basic_lower[first_valid]
    trend[first_valid] = (
        1.0 if close[first_valid] >= lower[first_valid] else -1.0
    )
    for i in range(first_valid + 1, n):
        basic_u = basic_upper[i]
        basic_l = basic_lower[i]
        upper_prev = upper[i - 1]
        lower_prev = lower[i - 1]
        close_prev = close[i - 1]
        upper[i] = (
            basic_u
            if basic_u < upper_prev or close_prev > upper_prev
            else upper_prev
        )
        lower[i] = (
            basic_l
            if basic_l > lower_prev or close_prev < lower_prev
            else lower_prev
        )
        if trend[i - 1] > 0.0:
            trend[i] = 1.0 if close[i] > lower[i] else -1.0
        else:
            trend[i] = 1.0 if close[i] >= upper[i] else -1.0
    return trend, upper, lower, first_valid


_supertrend_ta_kernel_nb = _numba.compile_kernel(_supertrend_ta_kernel_py)


def _supertrend_ta(
    src: np.ndarray,
    close: np.ndarray,
    atr_values: np.ndarray,
    multiplier: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int | None]:
    """The RamJet/pandas_ta band recursion (fail-closed pre-warm-up).

    Returns ``(trend, upper, lower, first_valid_idx)`` where ``trend`` is
    ``+1``/``-1`` and the caller derives ``ready`` from
    ``first_valid_idx + period``. ``first_valid_idx`` is ``None`` when no
    bar has a usable ATR/src pair, in which case ``ready`` must stay
    all-False. An interior NaN ATR/src is fed through the same ``or``-chain
    as the deleted RamJet fallback: a NaN basic band can be selected and
    then poisons that band, because every comparison against NaN is False.
    """
    trend, upper, lower, first_valid = _numba.call(
        _supertrend_ta_kernel_py,
        _supertrend_ta_kernel_nb,
        src,
        close,
        atr_values,
        multiplier,
    )
    return trend, upper, lower, None if first_valid < 0 else first_valid


def supertrend(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int,
    multiplier: float,
    *,
    atr_smoothing: Smoothing,
    atr_seed: Seed,
    rule: SupertrendRule,
    atr_first_bar: TrFirstBar,
    atr: pd.Series | None = None,
    atr_min_periods: int | None = None,
    atr_skipna: bool = False,
) -> SupertrendResult:
    """Supertrend bands, direction and readiness.

    ``rule`` selects the recursion (see :data:`SupertrendRule`):

    - ``"pine"``: the ``custom_swings_supertrend`` /
      ``pine_superosc_vumanchu`` band ratchet — each band only tightens
      while the previous close is beyond *that* band; flips compare the
      close against the *previous* opposite band. Runs from bar 1.
    - ``"pine_dema"``: the ``pine_supertrend_dema`` variant — a band only
      ratchets while the previous close is beyond the *opposite* band
      (the suspected port bug, preserved for parity). Runs from the first
      bar after the first valid ATR.
    - ``"pine_superosc"``: the ``pine_superosc_vumanchu`` band/trend
      recursion — same ratchet and flip as ``"pine"``, but the loop
      starts one bar after the first valid ATR (the legacy
      ``range(length, ...)`` start) and bands stay NaN until then.
    - ``"ta"``: the RamJet/pandas_ta recursion — bands reset when the raw
      band crosses or the previous close is beyond the previous band;
      direction compares the close against the *current* band; pre-warm-up
      direction is fail-closed down with ``ready=False``. If no bar has a
      usable ATR/src pair, ``ready`` is all-False; an interior NaN ATR/src
      follows the legacy ``or``-chain, so a NaN basic band may be selected
      and poison that band.

    ``atr`` may carry a precomputed ATR series (index-aligned); when
    ``None`` it is computed from ``high``/``low``/``close`` with the
    ``atr_*`` arguments, including the required ``atr_first_bar`` true-range
    seed. Passing a legacy warm-up (e.g. zero-filled) ATR is how the parity
    presets reproduce their sources bit-for-bit.
    """
    period = _positive_int(period, "period")
    multiplier = float(multiplier)
    if atr is None:
        atr_values = atr_from_tr(
            true_range(high, low, close, first_bar=atr_first_bar),
            period,
            smoothing=atr_smoothing,
            seed=atr_seed,
            min_periods=atr_min_periods,
            skipna=atr_skipna,
        )
    else:
        atr_values = _as_float_series(atr, "atr")
    if not atr_values.index.equals(close.index):
        raise ValueError("atr must be index-aligned with close")

    idx = close.index
    src = ((high + low) / 2.0).to_numpy(dtype=float)
    close_v = close.to_numpy(dtype=float)
    atr_v = atr_values.to_numpy(dtype=float)
    n = len(close_v)

    if rule == "ta":
        trend_v, upper_v, lower_v, first_valid = _supertrend_ta(
            src, close_v, atr_v, multiplier
        )
        if first_valid is None:
            ready_v = np.zeros(n, dtype=bool)
        else:
            ready_v = np.arange(n) >= first_valid + period
    else:
        band_condition: Literal["same", "cross"] = "same"
        first_i = 1
        if rule in ("pine_dema", "pine_superosc"):
            # Both legacy loops start at ``range(period, ...)`` /
            # ``range(length, ...)`` regardless of where the first valid
            # ATR lands; NaN ATR bars leave NaN bands until one is usable.
            first_i = min(period, n)
            if rule == "pine_dema":
                band_condition = "cross"
        trend_v, upper_v, lower_v = _supertrend_pine(
            src,
            close_v,
            atr_v,
            multiplier,
            band_condition=band_condition,
            first_i=first_i,
        )
        ready_v = (
            (~np.isnan(trend_v))
            & (~np.isnan(upper_v))
            & (~np.isnan(lower_v))
        )

    trend = pd.Series(trend_v, index=idx, name="supertrend_trend")
    upper = pd.Series(upper_v, index=idx, name="supertrend_upper")
    lower = pd.Series(lower_v, index=idx, name="supertrend_lower")
    ready = pd.Series(ready_v, index=idx, name="supertrend_ready")
    return SupertrendResult(
        trend=trend,
        upper_band=upper,
        lower_band=lower,
        atr=atr_values,
        ready=ready,
    )
