"""Hand-computed reference vectors for the Pine-exact presets (issue #317, P3).

``test_presets_characterisation.py`` pins the presets against the strategy
code that now calls them — that catches wiring breaks but cannot catch a
wrong formula, because both sides share the preset. These tests pin the
presets against literal values transcribed independently from the Pine
semantics, so a mutant (wrong seed, wrong flip reference, zero-fill warm-up,
wrong EMA step) changes the output and fails here.

Sources transcribed:

- ``ta.ema``/``ta.atr``/``ta.rma``/``ta.sma``: Pine v5/v6 reference —
  ``ta.ema`` seeds at the SMA of the first ``length`` values and steps
  ``prev + alpha * (x - prev)`` with ``alpha = 2 / (length + 1)``;
  ``ta.atr`` is ``ta.rma(ta.tr(true), length)`` with ``ta.tr(true)[0] =
  high - low``. The step form is the measured TradingView law (PyneCore
  probe m558): the algebraically equal ``alpha * x + (1 - alpha) * prev``
  drifts in floating point, which is why ``EMA_LAW_SERIES`` below was
  searched for inputs where the two forms differ bitwise.
- Manual Pine Supertrend (``up1 = nz(up[1], up)`` recursion) as used by the
  ``pine_*`` ports.
- LuxAlgo "SuperTrend Oscillator" (TradingView ``dVau7zqn``) main-oscillator
  chain as ported by the ``pine_superosc_vumanchu`` source: ``ta.atr``
  bands off ``hl2``, same-band ratchets on the previous close, direction
  flips on the current close versus the *previous* bands, NaN until the
  first break, ``clip((close - spt) / (upper - lower), -1, 1)`` with Pine
  safe division (zero width -> NaN).
- VuManChu Cipher B WaveTrend (``f_wavetrend``): ``esa = ta.ema(hlc3,
  chlen)``, ``de = ta.ema(|hlc3 - esa|, chlen)``,
  ``ci = (hlc3 - esa) / (0.015 * de)``, ``wt1 = ta.ema(ci, avg)``,
  ``wt2 = ta.sma(wt1, malen)`` — verbatim from the public Pine source
  (github.com/benso87/Private-Pine-Scripts ``Cipher B strategy.pine`` and
  the fmzquant mirror of the VuManChu original).
"""

from __future__ import annotations

import math

import pandas as pd

from shared.indicators import presets

# ── Shared deterministic frames ────────────────────────────────────────

FRAME_A_CLOSE = [
    100.0, 101.0, 99.0, 102.0, 100.5, 103.0, 101.5, 104.0,
    102.5, 105.0, 103.5, 106.0, 104.5, 107.0,
]


def _frame_a() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(FRAME_A_CLOSE), freq="h")
    close = pd.Series(FRAME_A_CLOSE, index=idx)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.8,
            "low": close - 0.6,
            "close": close,
        }
    )


def _ohlc(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    return (
        df["high"].astype(float),
        df["low"].astype(float),
        df["close"].astype(float),
    )


def _assert_close(actual, expected, *, msg: str = "") -> None:
    """Element-wise exact-or-NaN comparison against literal vectors."""
    assert len(actual) == len(expected), msg
    for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
        if isinstance(e, float) and math.isnan(e):
            assert math.isnan(float(a)), f"{msg} bar {i}: expected NaN, got {a}"
        else:
            assert float(a) == float(e), f"{msg} bar {i}: expected {e}, got {a}"


# ── ta.ema law: SMA seed + prev + alpha * (x - prev) ───────────────────

EMA_LAW_SERIES = [
    515908.805880605, -158856.83833831002, -482166.4994140733,
    22549.44273721706, -190131.7250991714, 567597.1780695452,
    -393374.5478421451, -46806.09169528843, 166764.07891006232,
    816225.7703906703, 9373.711634780513, -436324.3112005923,
    511608.40831444785,
]
# span=5 -> alpha=1/3. Chosen so `prev + alpha*(x-prev)` and
# `alpha*x + (1-alpha)*prev` differ in the last bits on bars 11-13; the
# literals below were computed with the Pine step form only.
EMA_LAW_EXPECTED = [
    math.nan, math.nan, math.nan, math.nan, -58539.36284674653,
    150172.81745868403, -31009.6376415923, -36275.122326157674,
    31404.611419248984, 293011.6644097227, 198465.68015140865,
    -13130.98363259167, 161782.14701642148,
]


def test_ema_pine_ta_uses_pine_step_law() -> None:
    series = pd.Series(EMA_LAW_SERIES, dtype=float)
    out = presets.ema_pine_ta(series, 5)
    _assert_close(out.tolist(), EMA_LAW_EXPECTED, msg="ema_pine_ta(5)")


def test_ema_pine_ta_small_vector() -> None:
    df = _frame_a()
    _, _, close = _ohlc(df)
    out = presets.ema_pine_ta(close, 3)
    _assert_close(
        out.tolist(),
        [
            math.nan, math.nan, 100.0, 101.0, 100.75, 101.875,
            101.6875, 102.84375, 102.671875, 103.8359375,
            103.66796875, 104.833984375, 104.6669921875,
            105.83349609375,
        ],
        msg="ema_pine_ta(3)",
    )
    # Warm-up is NaN, never 0 or a first-seeded value.
    assert out.iloc[:2].isna().all()


def test_dema_pine_ta_reference_vector() -> None:
    df = _frame_a()
    _, _, close = _ohlc(df)
    out = presets.dema_pine_ta(close, 3)
    _assert_close(
        out.tolist(),
        [
            math.nan, math.nan, math.nan, math.nan, 100.91666666666667,
            102.52083333333334, 101.91666666666667, 103.53645833333334,
            102.93229166666667, 104.54817708333334, 103.94010416666667,
            105.55305989583334, 104.94303385416667, 106.55476888020834,
        ],
        msg="dema_pine_ta(3)",
    )


# ── ta.atr: ta.rma(ta.tr(true)) with SMA seed ──────────────────────────

ATR_3_EXPECTED = [
    math.nan, math.nan, 1.9333333333333276, 2.5555555555555505,
    2.4037037037036986, 2.7024691358024646, 2.5016460905349747,
    2.7677640603566487, 2.545176040237764, 2.796784026825175,
    2.564522684550115, 2.8096817897000754, 2.5731211931333817,
    2.8154141287555867,
]


def test_atr_pine_ta_reference_vector() -> None:
    df = _frame_a()
    high, low, close = _ohlc(df)
    out = presets.atr_pine_ta(high, low, close, 3)
    _assert_close(out.tolist(), ATR_3_EXPECTED, msg="atr_pine_ta(3)")
    # ta.tr(true) bar 0 is high - low = 1.4 (not 0, not a gap term).
    assert out.iloc[:2].isna().all()


# ── VuManChu WaveTrend ─────────────────────────────────────────────────

WT1_EXPECTED = [
    math.nan, math.nan, math.nan, math.nan, math.nan, math.nan,
    math.nan, math.nan, 31.746031746031747, 66.997760249294,
    33.498880124647, 69.26603530642063, 34.63301765321032,
    70.35290622111789,
]
WT2_EXPECTED = [
    math.nan, math.nan, math.nan, math.nan, math.nan, math.nan,
    math.nan, math.nan, math.nan, 49.371895997662875,
    50.2483201869705, 51.38245771553382, 51.949526479815475,
    52.492961937164104,
]


def test_wavetrend_vumanchu_reference_vector() -> None:
    df = _frame_a()
    high, low, close = _ohlc(df)
    wt1, wt2 = presets.wavetrend_vumanchu(high, low, close, 4, 3, 2)
    _assert_close(wt1.tolist(), WT1_EXPECTED, msg="wt1")
    _assert_close(wt2.tolist(), WT2_EXPECTED, msg="wt2")


def test_wavetrend_zero_denominator_is_nan_not_floor() -> None:
    """Pine safe division: ``0.015 * de == 0`` yields NaN, not a 1e-10 floor."""
    idx = pd.date_range("2024-01-01", periods=10, freq="h")
    flat = 100.0
    df = pd.DataFrame(
        {
            "open": flat,
            "high": flat,
            "low": flat,
            "close": flat,
        },
        index=idx,
    )
    high, low, close = _ohlc(df)
    wt1, _ = presets.wavetrend_vumanchu(high, low, close, 3, 2, 2)
    # hlc3 is constant -> esa == hlc3 -> de == 0 -> ci is NaN from the
    # first valid de onward, so every wt1 is NaN (never a floor artifact).
    assert wt1.isna().all()


# ── LuxAlgo SuperTrend Oscillator chain ────────────────────────────────

# 21-bar frame: rally, then a sharp drop that flips the direction bearish.
SUPEROSC_CLOSE = [
    *FRAME_A_CLOSE,
    106.0, 103.0, 100.0, 96.0, 92.0, 95.0, 99.0,
]
SUPEROSC_OSC = [
    math.nan, math.nan, math.nan, math.nan, math.nan, 1.0,
    0.42549371633752275,
    0.7063317734695831, 0.4974692006494131, 0.8226217319763547,
    0.5827168017551343, 0.9793612053479147, 0.6954804260767706, 1.0,
    0.4843668700594809, 0.1830377386836283, -1.0, -0.5,
    -0.7230996356305552, -0.4380117218388886, -0.07128254991484494,
]


def _superosc_frame() -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(SUPEROSC_CLOSE), freq="h")
    close = pd.Series(SUPEROSC_CLOSE, index=idx)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
        }
    )


def test_superosc_luxalgo_reference_vector() -> None:
    """Direction stays NaN until the first band break, then flips 1 -> 0."""
    df = _superosc_frame()
    high, low, close = _ohlc(df)
    osc = presets.superosc_luxalgo(high, low, close, 3, 2.0)
    _assert_close(osc.tolist(), SUPEROSC_OSC, msg="superosc")
    first_valid = next(i for i, v in enumerate(osc.tolist()) if not math.isnan(v))
    assert first_valid == 5


def test_superosc_zero_width_bands_yield_nan() -> None:
    """Flat prices -> atr 0 -> upper == lower -> safe division gives NaN."""
    idx = pd.date_range("2024-01-01", periods=12, freq="h")
    df = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        index=idx,
    )
    high, low, close = _ohlc(df)
    osc = presets.superosc_luxalgo(high, low, close, 3, 2.0)
    assert osc.isna().all()


# ── Manual Pine Supertrend: the up1-vs-lower[i] flip discriminator ─────

# period=1, mult=1 keeps ta.atr equal to TR, so every band is hand-checkable.
# At bar 2 the previous lower band (up1) is 9.5 while the *current* lower
# band ratchets to 10.0 and close is 9.8: the Pine rule
# ``close < up1`` stays bullish, while a mutant comparing ``close < lower[i]``
# would flip bearish. The literals below fail on that mutant.
ST_B_HIGH = [10.0, 10.5, 11.5, 12.0]
ST_B_LOW = [9.0, 10.5, 10.5, 11.0]
ST_B_CLOSE = [9.5, 10.5, 9.8, 11.5]
ST_B_TREND = [1.0, 1.0, 1.0, 1.0]
ST_B_UPPER = [10.5, 10.5, 12.0, 12.0]
ST_B_LOWER = [8.5, 9.5, 10.0, 9.3]


def test_supertrend_pine_flip_compares_reference_band() -> None:
    idx = pd.date_range("2024-01-01", periods=4, freq="h")
    df = pd.DataFrame(
        {
            "open": pd.Series(ST_B_CLOSE, index=idx),
            "high": pd.Series(ST_B_HIGH, index=idx),
            "low": pd.Series(ST_B_LOW, index=idx),
            "close": pd.Series(ST_B_CLOSE, index=idx),
        }
    )
    high, low, close = _ohlc(df)
    result = presets.supertrend_pine_ta(high, low, close, 1, 1.0)
    _assert_close(result.trend.tolist(), ST_B_TREND, msg="trend")
    _assert_close(result.upper_band.tolist(), ST_B_UPPER, msg="upper")
    _assert_close(result.lower_band.tolist(), ST_B_LOWER, msg="lower")
    # Sanity: the divergence the vector pins — bar 2 close sits strictly
    # between the reference band (9.5) and the current lower band (10.0).
    assert ST_B_LOWER[2] > ST_B_CLOSE[2] > ST_B_LOWER[1]
