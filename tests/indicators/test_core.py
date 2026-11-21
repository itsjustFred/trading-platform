"""Hand-computed vectors and property tests for ``shared.indicators.core``.

The vectors below are computed by hand (see the arithmetic in each
test); the property tests pin the module contract from the issue:
warm-up bars are NaN (never 0), there is no internal shift, and NaN
inputs are handled per each function's documented semantics.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from shared.indicators.core import (
    atr,
    atr_from_tr,
    ema,
    lagged,
    rma,
    rsi,
    sma,
    supertrend,
    true_range,
)


def _ohlc(n: int = 60, seed: int = 7) -> tuple[pd.Series, pd.Series, pd.Series]:
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    open_ = close + rng.normal(0.0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.2, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.2, 1.5, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    return (
        pd.Series(high, index=idx),
        pd.Series(low, index=idx),
        pd.Series(close, index=idx),
    )


def _assert_close(actual: pd.Series, expected, rtol: float = 1e-12) -> None:
    expected = pd.Series(np.asarray(expected, dtype=float), index=actual.index)
    pd.testing.assert_series_equal(
        actual, expected, check_names=False, rtol=rtol, atol=rtol
    )


class TestTrueRange:
    def test_hand_computed(self):
        high = pd.Series([5.0, 6.0, 7.0])
        low = pd.Series([4.0, 5.0, 6.0])
        close = pd.Series([4.5, 5.5, 6.5])
        # bar0: prev close NaN -> high-low = 1
        # bar1: max(1, |6-4.5|=1.5, |5-4.5|=0.5) = 1.5
        # bar2: max(1, |7-5.5|=1.5, |6-5.5|=0.5) = 1.5
        _assert_close(true_range(high, low, close), [1.0, 1.5, 1.5])

    @pytest.mark.parametrize(
        ("first_bar", "expected"),
        [
            ("hl", [1.0, 1.5, 1.5]),
            ("zero", [0.0, 1.5, 1.5]),
            ("nan", [np.nan, 1.5, 1.5]),
        ],
    )
    def test_first_bar_seeds(self, first_bar, expected):
        high = pd.Series([5.0, 6.0, 7.0])
        low = pd.Series([4.0, 5.0, 6.0])
        close = pd.Series([4.5, 5.5, 6.5])
        _assert_close(
            true_range(high, low, close, first_bar=first_bar), expected
        )

    def test_nan_inputs_skip_gap_terms(self):
        high = pd.Series([5.0, np.nan, 7.0])
        low = pd.Series([4.0, 5.0, 6.0])
        close = pd.Series([4.5, 5.5, np.nan])
        tr = true_range(high, low, close)
        # bar1: h-l and |h-prev_close| are NaN; |low - prev_close| = |5-4.5|
        # is the only finite term -> 0.5
        assert tr.iloc[1] == pytest.approx(0.5)
        # bar2: prev_close is close[1]=5.5 (the NaN close[2] is irrelevant);
        # max(1, |7-5.5|=1.5, |6-5.5|=0.5) = 1.5
        assert tr.iloc[2] == pytest.approx(1.5)


class TestSma:
    def test_hand_computed(self):
        _assert_close(sma(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3),
                      [np.nan, np.nan, 2.0, 3.0, 4.0])

    def test_window_one_is_identity(self):
        s = pd.Series([1.0, np.nan, 3.0])
        _assert_close(sma(s, 1), s)


class TestEma:
    def test_seed_first_hand_computed(self):
        # span=2 -> alpha=2/3; y[i] = (2*x[i] + y[i-1]) / 3
        # y0=1, y1=(4+1)/3=5/3, y2=(6+5/3)/3=23/9, y3=(8+23/9)/3=95/27
        _assert_close(
            ema(pd.Series([1.0, 2.0, 3.0, 4.0]), 2, seed="first"),
            [1.0, 5 / 3, 23 / 9, 95 / 27],
        )

    def test_seed_sma_hand_computed(self):
        # span=3 -> alpha=1/2; seed = SMA(1,2,3)=2 at index 2
        # y3 = 0.5*4 + 0.5*2 = 3; y4 = 0.5*5 + 0.5*3 = 4
        _assert_close(
            ema(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3, seed="sma"),
            [np.nan, np.nan, 2.0, 3.0, 4.0],
        )

    def test_seed_first_matches_ewm(self):
        s = pd.Series(np.arange(30, dtype=float) + 1)
        _assert_close(
            ema(s, 5, seed="first"),
            s.ewm(span=5, adjust=False).mean(),
            rtol=1e-15,
        )


class TestRma:
    def test_seed_sma_hand_computed(self):
        # length=3 -> alpha=1/3; seed = mean(2,4,6)=4 at index 2
        # y3 = (4*2 + 8)/3 = 16/3; y4 = (16/3*2 + 10)/3 = 62/9
        _assert_close(
            rma(pd.Series([2.0, 4.0, 6.0, 8.0, 10.0]), 3, seed="sma"),
            [np.nan, np.nan, 4.0, 16 / 3, 62 / 9],
        )

    def test_seed_first_min_periods_matches_ewm(self):
        s = pd.Series(np.arange(30, dtype=float) + 1)
        _assert_close(
            rma(s, 4, seed="first", min_periods=4),
            s.ewm(alpha=0.25, min_periods=4, adjust=False).mean(),
            rtol=1e-15,
        )

    def test_seed_first_no_min_periods_from_bar_zero(self):
        s = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0])
        out = rma(s, 3, seed="first")
        assert out.iloc[0] == pytest.approx(2.0)
        _assert_close(
            out,
            [2.0, 8 / 3, 34 / 9, 140 / 27, 550 / 81],
        )


class TestAtr:
    def test_sma_smoothed_hand_computed(self):
        high = pd.Series([5.0, 6.0, 7.0, 8.0])
        low = pd.Series([4.0, 5.0, 6.0, 7.0])
        close = pd.Series([4.5, 5.5, 6.5, 7.5])
        tr = true_range(high, low, close)  # [1, 1.5, 1.5, 1.5]
        _assert_close(
            atr(high, low, close, 2, smoothing="sma", seed="sma"),
            [np.nan, 1.25, 1.5, 1.5],
        )
        assert tr.tolist() == pytest.approx([1.0, 1.5, 1.5, 1.5])

    def test_wilder_sma_seed_hand_computed(self):
        high = pd.Series([5.0, 6.0, 7.0, 8.0])
        low = pd.Series([4.0, 5.0, 6.0, 7.0])
        close = pd.Series([4.5, 5.5, 6.5, 7.5])
        # tr = [1, 1.5, 1.5, 1.5]; SMA seed over tr[0..1] = 1.25 at idx1;
        # y2 = (1.25*1 + 1.5)/2 = 1.375; y3 = (1.375 + 1.5)/2 = 1.4375
        _assert_close(
            atr(high, low, close, 2, smoothing="wilder", seed="sma"),
            [np.nan, 1.25, 1.375, 1.4375],
        )

    def test_first_bar_zero_counts_in_sma_window(self):
        high = pd.Series([5.0, 6.0, 7.0])
        low = pd.Series([4.0, 5.0, 6.0])
        close = pd.Series([4.5, 5.5, 6.5])
        # tr with first_bar="zero": [0, 1.5, 1.5]; rolling(2).mean ->
        # [nan, 0.75, 1.5]
        _assert_close(
            atr(
                high, low, close, 2,
                smoothing="sma", seed="sma", first_bar="zero",
            ),
            [np.nan, 0.75, 1.5],
        )

    def test_skipna_skips_nan_tr_positions(self):
        tr = pd.Series([1.0, np.nan, 3.0, 4.0, 5.0])
        out = atr_from_tr(tr, 2, smoothing="sma", seed="sma", skipna=True)
        # valid values [1,3,4,5]; window 2 -> means at positions 2,3,4
        _assert_close(out, [np.nan, np.nan, 2.0, 3.5, 4.5])
        out_w = atr_from_tr(tr, 2, smoothing="wilder", seed="sma", skipna=True)
        # seed mean(1,3)=2 at pos2; y3=(2+4)/2=3; y4=(3+5)/2=4
        _assert_close(out_w, [np.nan, np.nan, 2.0, 3.0, 4.0])


class TestRsi:
    def test_hand_computed_wilder(self):
        # period=2, ewm alpha=1/2, min_periods=2:
        # close=[10,11,10,12,11]; gain=[nan,1,0,2,0]; loss=[nan,0,1,0,1]
        # avg_gain (first emit idx2): [_,_,0.5,1.25,0.625]
        # avg_loss:                     [_,_,0.5,0.25,0.625]
        # rs: idx2=1->50, idx3=5->83.33, idx4=1->50
        close = pd.Series([10.0, 11.0, 10.0, 12.0, 11.0])
        _assert_close(
            rsi(close, 2, min_periods=2, flat="none"),
            [np.nan, np.nan, 50.0, 100 - 100 / 6, 50.0],
        )

    def test_flat_none_stays_nan(self):
        close = pd.Series(np.full(10, 5.0))
        out = rsi(close, 3, min_periods=3, flat="none")
        assert out.isna().all()

    def test_flat_mask_reports_50_and_100(self):
        close = pd.Series(np.full(10, 5.0))
        out = rsi(close, 3, min_periods=None, flat="mask")
        assert pd.isna(out.iloc[0])
        assert (out.iloc[1:] == 50.0).all()
        # strictly rising: no losses -> 100 from the first defined bar
        up = pd.Series(np.arange(20, dtype=float))
        out_up = rsi(up, 3, min_periods=None, flat="mask")
        assert (out_up.iloc[1:] == 100.0).all()

    def test_fill50_masks_warmup_and_flat(self):
        close = pd.Series(np.full(10, 5.0))
        out = rsi(close, 3, min_periods=3, flat="fill50")
        assert (out == 50.0).all()
        up = pd.Series(np.arange(20, dtype=float))
        out_up = rsi(up, 3, min_periods=3, flat="fill50")
        # codegen semantics: warm-up AND the no-loss rs=NaN case both fill 50
        assert (out_up == 50.0).all()


class TestSupertrend:
    def test_hand_computed_pine_rule(self):
        # 3 bars, period=1, multiplier=1, SMA ATR -> atr == tr == [2,2,2]
        # src = hl2 = [9,10,11]
        # i=1: raw_low=8, raw_high=12; prev bands NaN -> lower=8, upper=12;
        #       trend stays +1 (close[1]=10 not < NaN)
        # i=2: raw_low=9, raw_high=13; close[1]=10 > lower[1]=8 ->
        #       lower=max(9,8)=9; close[1]=10 < upper[1]=12 ->
        #       upper=min(13,12)=12; close[2]=11 not < lower[1]=8 -> +1
        idx = pd.RangeIndex(3)
        high = pd.Series([10.0, 11.0, 12.0], index=idx)
        low = pd.Series([8.0, 9.0, 10.0], index=idx)
        close = pd.Series([9.0, 10.0, 11.0], index=idx)
        result = supertrend(
            high, low, close, 1, 1.0,
            atr_smoothing="sma", atr_seed="sma", rule="pine",
        )
        _assert_close(result.trend, [1.0, 1.0, 1.0])
        _assert_close(result.lower_band, [np.nan, 8.0, 9.0])
        _assert_close(result.upper_band, [np.nan, 12.0, 12.0])
        _assert_close(result.atr, [2.0, 2.0, 2.0])
        assert result.ready.tolist() == [False, True, True]

    def test_bands_ordered_and_trend_binary(self):
        high, low, close = _ohlc(80, seed=3)
        result = supertrend(
            high, low, close, 5, 2.0,
            atr_smoothing="wilder", atr_seed="sma", rule="pine",
        )
        valid = result.lower_band.notna() & result.upper_band.notna()
        assert (result.lower_band[valid] < result.upper_band[valid]).all()
        assert result.trend.dropna().isin([-1.0, 1.0]).all()


class TestLagged:
    def test_shift(self):
        s = pd.Series([1.0, 2.0, 3.0])
        _assert_close(lagged(s), [np.nan, 1.0, 2.0])
        _assert_close(lagged(s, 2), [np.nan, np.nan, 1.0])


# ── Property tests ─────────────────────────────────────────────────────


def _prefix_property(fn, *args, **kwargs) -> None:
    """Appending future bars must not change past indicator values."""
    high, low, close = _ohlc(80, seed=11)
    full = fn(high, low, close, *args, **kwargs)
    cut = 50
    prefix = fn(high.iloc[:cut], low.iloc[:cut], close.iloc[:cut], *args, **kwargs)
    if isinstance(full, pd.Series):
        pd.testing.assert_series_equal(
            full.iloc[:cut], prefix, check_names=False, rtol=1e-12, atol=1e-12
        )
    else:
        for field in ("trend", "upper_band", "lower_band", "atr", "ready"):
            pd.testing.assert_series_equal(
                getattr(full, field).iloc[:cut],
                getattr(prefix, field),
                check_names=False,
                rtol=1e-12,
                atol=1e-12,
            )


class TestNoLookahead:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"smoothing": "sma", "seed": "sma"},
            {"smoothing": "wilder", "seed": "sma"},
            {"smoothing": "wilder", "seed": "first", "min_periods": 14},
            {"smoothing": "ema", "seed": "first"},
            {"smoothing": "wilder", "seed": "sma", "skipna": True},
        ],
    )
    def test_atr(self, kwargs):
        _prefix_property(atr, 14, **kwargs)

    @pytest.mark.parametrize("seed", ["first", "sma"])
    def test_rma(self, seed):
        high, low, close = _ohlc(80, seed=11)
        s = close
        full = rma(s, 10, seed=seed)
        prefix = rma(s.iloc[:50], 10, seed=seed)
        pd.testing.assert_series_equal(full.iloc[:50], prefix, rtol=1e-12, atol=1e-12)

    @pytest.mark.parametrize("seed", ["first", "sma"])
    def test_ema(self, seed):
        s = _ohlc(80, seed=11)[2]
        full = ema(s, 10, seed=seed)
        prefix = ema(s.iloc[:50], 10, seed=seed)
        pd.testing.assert_series_equal(full.iloc[:50], prefix, rtol=1e-12, atol=1e-12)

    @pytest.mark.parametrize("flat", ["none", "mask", "fill50"])
    def test_rsi(self, flat):
        s = _ohlc(80, seed=11)[2]
        full = rsi(s, 14, min_periods=14, flat=flat)
        prefix = rsi(s.iloc[:50], 14, min_periods=14, flat=flat)
        pd.testing.assert_series_equal(full.iloc[:50], prefix, rtol=1e-12, atol=1e-12)

    @pytest.mark.parametrize("rule", ["pine", "pine_dema", "pine_superosc", "ta"])
    def test_supertrend(self, rule):
        high, low, close = _ohlc(80, seed=11)
        _prefix_property(
            supertrend, 10, 3.0,
            atr_smoothing="wilder", atr_seed="sma", rule=rule,
        )

    def test_true_range_and_lagged(self):
        high, low, close = _ohlc(80, seed=11)
        tr_full = true_range(high, low, close)
        tr_pre = true_range(high.iloc[:50], low.iloc[:50], close.iloc[:50])
        pd.testing.assert_series_equal(tr_full.iloc[:50], tr_pre, rtol=1e-12, atol=1e-12)
        assert lagged(close).iloc[50] == close.iloc[49]


class TestWarmupNaN:
    """Warm-up bars are NaN, never 0 — per variant contract."""

    def test_sma(self):
        s = pd.Series(np.arange(30, dtype=float))
        assert sma(s, 10).iloc[:9].isna().all()
        assert sma(s, 10).iloc[9:].notna().all()

    def test_rma_seed_sma(self):
        s = pd.Series(np.arange(30, dtype=float))
        assert rma(s, 10, seed="sma").iloc[:9].isna().all()
        assert rma(s, 10, seed="sma").iloc[9:].notna().all()

    def test_atr_qc_variant(self):
        high, low, close = _ohlc(60)
        out = atr(high, low, close, 14, smoothing="wilder", seed="first",
                  min_periods=14)
        assert out.iloc[:13].isna().all()
        assert out.iloc[13:].notna().all()

    def test_atr_sma_variant(self):
        high, low, close = _ohlc(60)
        out = atr(high, low, close, 14, smoothing="sma", seed="sma")
        assert out.iloc[:13].isna().all()
        assert out.iloc[13:].notna().all()

    def test_rsi_min_periods(self):
        _, _, close = _ohlc(60)
        out = rsi(close, 14, min_periods=14, flat="none")
        # delta[0] is NaN, so the 14th non-NaN gain/loss lands at index 14
        assert out.iloc[:14].isna().all()
        assert out.iloc[14:].notna().all()

    def test_supertrend_direction_never_zero_filled(self):
        high, low, close = _ohlc(60)
        for rule in ("pine", "pine_dema", "pine_superosc", "ta"):
            result = supertrend(
                high, low, close, 10, 3.0,
                atr_smoothing="wilder", atr_seed="sma", rule=rule,
            )
            assert not (result.lower_band == 0.0).any()
            assert not (result.upper_band == 0.0).any()
