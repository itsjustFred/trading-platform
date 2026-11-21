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
        _assert_close(
            true_range(high, low, close, first_bar="hl"), [1.0, 1.5, 1.5]
        )

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
        tr = true_range(high, low, close, first_bar="hl")
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
            ema(pd.Series([1.0, 2.0, 3.0, 4.0]), 2, seed="first",
                min_periods=0),
            [1.0, 5 / 3, 23 / 9, 95 / 27],
        )

    def test_seed_sma_hand_computed(self):
        # span=3 -> alpha=1/2; seed = SMA(1,2,3)=2 at index 2
        # y3 = 0.5*4 + 0.5*2 = 3; y4 = 0.5*5 + 0.5*3 = 4
        _assert_close(
            ema(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0]), 3, seed="sma"),
            [np.nan, np.nan, 2.0, 3.0, 4.0],
        )

    def test_seed_first_min_periods_zero_matches_ewm(self):
        s = pd.Series(np.arange(30, dtype=float) + 1)
        _assert_close(
            ema(s, 5, seed="first", min_periods=0),
            s.ewm(span=5, adjust=False).mean(),
            rtol=1e-15,
        )

    def test_seed_first_default_min_periods_is_span(self):
        s = pd.Series(np.arange(30, dtype=float) + 1)
        _assert_close(
            ema(s, 5, seed="first"),
            s.ewm(span=5, min_periods=5, adjust=False).mean(),
            rtol=1e-15,
        )
        assert ema(s, 5, seed="first").iloc[:4].isna().all()


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

    def test_seed_first_min_periods_zero_from_bar_zero(self):
        s = pd.Series([2.0, 4.0, 6.0, 8.0, 10.0])
        out = rma(s, 3, seed="first", min_periods=0)
        assert out.iloc[0] == pytest.approx(2.0)
        _assert_close(
            out,
            [2.0, 8 / 3, 34 / 9, 140 / 27, 550 / 81],
        )

    def test_seed_first_default_min_periods_is_length(self):
        s = pd.Series(np.arange(30, dtype=float) + 1)
        _assert_close(
            rma(s, 4, seed="first"),
            s.ewm(alpha=0.25, min_periods=4, adjust=False).mean(),
            rtol=1e-15,
        )
        assert rma(s, 4, seed="first").iloc[:3].isna().all()

    def test_seed_first_skipna_emits_from_first_valid(self):
        # Regression: seed="first" must not wait for `length` valid bars;
        # that guard is only for seed="sma".
        out = rma(pd.Series([1.0, 2.0, 4.0]), 3, seed="first", skipna=True)
        _assert_close(out, [1.0, 4.0 / 3.0, 20.0 / 9.0])


class TestAtr:
    def test_sma_smoothed_hand_computed(self):
        high = pd.Series([5.0, 6.0, 7.0, 8.0])
        low = pd.Series([4.0, 5.0, 6.0, 7.0])
        close = pd.Series([4.5, 5.5, 6.5, 7.5])
        tr = true_range(high, low, close, first_bar="hl")  # [1, 1.5, 1.5, 1.5]
        _assert_close(
            atr(high, low, close, 2, smoothing="sma", seed="sma",
                first_bar="hl"),
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
            atr(high, low, close, 2, smoothing="wilder", seed="sma",
                first_bar="hl"),
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

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"smoothing": "sma", "seed": "sma", "skipna": True,
              "min_periods": 2}, "min_periods"),
            ({"smoothing": "wilder", "seed": "sma", "skipna": True,
              "min_periods": 2}, "min_periods"),
            ({"smoothing": "sma", "seed": "sma", "skipna": False,
              "min_periods": 2}, "min_periods"),
        ],
    )
    def test_min_periods_rejected_where_ignored(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            atr_from_tr(pd.Series([1.0, 2.0, 3.0]), 2, **kwargs)

    def test_rma_rejects_min_periods_for_sma_seed_and_skipna(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0])
        with pytest.raises(ValueError, match="min_periods"):
            rma(s, 2, seed="sma", min_periods=1)
        with pytest.raises(ValueError, match="min_periods"):
            rma(s, 2, seed="first", skipna=True, min_periods=1)


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
        out = rsi(close, 3, min_periods=0, flat="mask")
        assert pd.isna(out.iloc[0])
        assert (out.iloc[1:] == 50.0).all()
        # strictly rising: no losses -> 100 from the first defined bar
        up = pd.Series(np.arange(20, dtype=float))
        out_up = rsi(up, 3, min_periods=0, flat="mask")
        assert (out_up.iloc[1:] == 100.0).all()

    def test_fill50_masks_warmup_and_flat(self):
        close = pd.Series(np.full(10, 5.0))
        out = rsi(close, 3, min_periods=3, flat="fill50")
        assert (out == 50.0).all()
        up = pd.Series(np.arange(20, dtype=float))
        out_up = rsi(up, 3, min_periods=3, flat="fill50")
        # codegen semantics: warm-up AND the no-loss rs=NaN case both fill 50
        assert (out_up == 50.0).all()

    def test_default_min_periods_is_period(self):
        s = _ohlc(40, seed=5)[2]
        _assert_close(
            rsi(s, 14, flat="none"),
            rsi(s, 14, min_periods=14, flat="none"),
        )

    def test_flat_is_required(self):
        with pytest.raises(TypeError):
            rsi(pd.Series([1.0, 2.0, 3.0]), 2)


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
            atr_first_bar="hl",
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
            atr_first_bar="hl",
        )
        valid = result.lower_band.notna() & result.upper_band.notna()
        assert (result.lower_band[valid] < result.upper_band[valid]).all()
        assert result.trend.dropna().isin([-1.0, 1.0]).all()

    def test_ta_ready_false_when_atr_never_valid(self):
        high, low, close = _ohlc(15, seed=9)
        result = supertrend(
            high, low, close, 10, 3.0,
            atr_smoothing="wilder", atr_seed="first", rule="ta",
            atr_first_bar="hl", atr_min_periods=20,
        )
        assert result.ready.notna().all()
        assert not result.ready.any()
        assert result.upper_band.isna().all()
        assert result.lower_band.isna().all()

    def test_supplied_atr_must_be_index_aligned(self):
        high, low, close = _ohlc(20, seed=4)
        bad_atr = pd.Series(1.0, index=close.index[:-1])
        with pytest.raises(ValueError, match="index-aligned"):
            supertrend(
                high, low, close, 5, 2.0,
                atr_smoothing="wilder", atr_seed="sma", rule="ta",
                atr_first_bar="hl", atr=bad_atr,
            )


class TestLagged:
    def test_shift(self):
        s = pd.Series([1.0, 2.0, 3.0])
        _assert_close(lagged(s), [np.nan, 1.0, 2.0])
        _assert_close(lagged(s, 2), [np.nan, np.nan, 1.0])


# ── Property tests ─────────────────────────────────────────────────────


def _property_frames(n: int = 60) -> dict[str, tuple[pd.Series, ...]]:
    high, low, close = _ohlc(n, seed=11)
    clean = (high, low, close)
    nan_positions = [7, 23, 24]
    h_nan = high.copy()
    l_nan = low.copy()
    c_nan = close.copy()
    h_nan.iloc[nan_positions] = np.nan
    l_nan.iloc[nan_positions] = np.nan
    c_nan.iloc[nan_positions] = np.nan
    return {"clean": clean, "nan": (h_nan, l_nan, c_nan)}


def _assert_no_lookahead(make, inputs: tuple[pd.Series, ...]) -> None:
    """Appending future bars must not change past indicator values."""
    full = make(*inputs)
    n = len(inputs[0])
    for cut in range(1, n):
        prefix_inputs = tuple(x.iloc[:cut] for x in inputs)
        prefix = make(*prefix_inputs)
        if isinstance(full, pd.Series):
            pd.testing.assert_series_equal(
                full.iloc[:cut], prefix, check_names=False,
                rtol=1e-12, atol=1e-12,
            )
            continue
        for field in ("trend", "upper_band", "lower_band", "atr", "ready"):
            pd.testing.assert_series_equal(
                getattr(full, field).iloc[:cut],
                getattr(prefix, field),
                check_names=False,
                rtol=1e-12,
                atol=1e-12,
            )


_ATR_CASES = [
    ("sma", "sma", False, None),
    ("sma", "first", False, None),
    ("sma", "sma", True, None),
    ("sma", "first", True, None),
    ("wilder", "first", False, None),
    ("wilder", "first", False, 7),
    ("wilder", "sma", False, None),
    ("wilder", "first", True, None),
    ("wilder", "sma", True, None),
    ("ema", "first", False, None),
    ("ema", "first", False, 7),
    ("ema", "sma", False, None),
    ("ema", "sma", False, 7),
    ("ema", "first", True, None),
    ("ema", "first", True, 7),
    ("ema", "sma", True, None),
    ("ema", "sma", True, 7),
]

_RMA_CASES = [
    ("first", False, None),
    ("first", False, 7),
    ("first", False, 0),
    ("first", True, None),
    ("sma", False, None),
    ("sma", True, None),
]

_EMA_CASES = [
    ("first", False, None),
    ("first", False, 7),
    ("first", False, 0),
    ("first", True, None),
    ("first", True, 7),
    ("sma", False, None),
    ("sma", False, 7),
    ("sma", True, None),
    ("sma", True, 7),
]

_RSI_CASES = [
    ("none", None),
    ("none", 7),
    ("mask", None),
    ("mask", 7),
    ("fill50", None),
    ("fill50", 7),
]

_SUPERTREND_ATR_CASES = [
    ("wilder", "sma", None, False),
    ("wilder", "first", None, False),
    ("wilder", "first", 20, False),
    ("ema", "first", None, False),
    ("ema", "sma", None, False),
    ("sma", "sma", None, False),
    ("sma", "sma", None, True),
]


class TestNoLookahead:
    @pytest.mark.parametrize("frame_kind", ["clean", "nan"])
    def test_atr(self, frame_kind):
        high, low, close = _property_frames()[frame_kind]
        for smoothing, seed, skipna, min_periods in _ATR_CASES:
            _assert_no_lookahead(
                lambda h, lo, c: atr(
                    h, lo, c, 14,
                    smoothing=smoothing, seed=seed, first_bar="hl",
                    min_periods=min_periods, skipna=skipna,
                ),
                (high, low, close),
            )

    @pytest.mark.parametrize("frame_kind", ["clean", "nan"])
    @pytest.mark.parametrize("seed,skipna,min_periods", _RMA_CASES)
    def test_rma(self, frame_kind, seed, skipna, min_periods):
        _, _, close = _property_frames()[frame_kind]
        _assert_no_lookahead(
            lambda c: rma(c, 10, seed=seed, min_periods=min_periods,
                          skipna=skipna),
            (close,),
        )

    @pytest.mark.parametrize("frame_kind", ["clean", "nan"])
    @pytest.mark.parametrize("seed,skipna,min_periods", _EMA_CASES)
    def test_ema(self, frame_kind, seed, skipna, min_periods):
        _, _, close = _property_frames()[frame_kind]
        _assert_no_lookahead(
            lambda c: ema(c, 10, seed=seed, min_periods=min_periods,
                          skipna=skipna),
            (close,),
        )

    @pytest.mark.parametrize("frame_kind", ["clean", "nan"])
    @pytest.mark.parametrize("flat,min_periods", _RSI_CASES)
    def test_rsi(self, frame_kind, flat, min_periods):
        _, _, close = _property_frames()[frame_kind]
        _assert_no_lookahead(
            lambda c: rsi(c, 14, min_periods=min_periods, flat=flat),
            (close,),
        )

    @pytest.mark.parametrize("frame_kind", ["clean", "nan"])
    @pytest.mark.parametrize(
        "rule", ["pine", "pine_dema", "pine_superosc", "ta"]
    )
    @pytest.mark.parametrize(
        "atr_smoothing,atr_seed,atr_min_periods,atr_skipna",
        _SUPERTREND_ATR_CASES,
    )
    def test_supertrend(
        self, frame_kind, rule, atr_smoothing, atr_seed,
        atr_min_periods, atr_skipna,
    ):
        high, low, close = _property_frames()[frame_kind]
        _assert_no_lookahead(
            lambda h, lo, c: supertrend(
                h, lo, c, 10, 3.0,
                atr_smoothing=atr_smoothing, atr_seed=atr_seed,
                rule=rule, atr_first_bar="hl",
                atr_min_periods=atr_min_periods, atr_skipna=atr_skipna,
            ),
            (high, low, close),
        )

    @pytest.mark.parametrize("frame_kind", ["clean", "nan"])
    def test_true_range_sma_and_lagged(self, frame_kind):
        high, low, close = _property_frames()[frame_kind]
        _assert_no_lookahead(
            lambda h, lo, c: true_range(h, lo, c, first_bar="hl"),
            (high, low, close),
        )
        _assert_no_lookahead(lambda c: sma(c, 10), (close,))
        full = lagged(close)
        for cut in range(1, len(close)):
            prefix = lagged(close.iloc[:cut])
            pd.testing.assert_series_equal(
                full.iloc[:cut], prefix, check_names=False,
                rtol=1e-12, atol=1e-12,
            )


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
                  first_bar="hl", min_periods=14)
        assert out.iloc[:13].isna().all()
        assert out.iloc[13:].notna().all()

    def test_atr_sma_variant(self):
        high, low, close = _ohlc(60)
        out = atr(high, low, close, 14, smoothing="sma", seed="sma",
                  first_bar="hl")
        assert out.iloc[:13].isna().all()
        assert out.iloc[13:].notna().all()

    def test_rsi_min_periods(self):
        _, _, close = _ohlc(60)
        out = rsi(close, 14, min_periods=14, flat="none")
        # delta[0] is NaN, so the 14th non-NaN gain/loss lands at index 14
        assert out.iloc[:14].isna().all()
        assert out.iloc[14:].notna().all()

    def test_ready_is_false_wherever_bands_are_nan(self):
        high, low, close = _ohlc(80, seed=6)
        for rule in ("pine", "pine_dema", "pine_superosc", "ta"):
            result = supertrend(
                high, low, close, 10, 3.0,
                atr_smoothing="wilder", atr_seed="first", rule=rule,
                atr_first_bar="hl", atr_min_periods=15,
            )
            bandless = result.upper_band.isna() | result.lower_band.isna()
            assert not (result.ready & bandless).any()

    def test_supertrend_direction_never_zero_filled(self):
        high, low, close = _ohlc(60)
        for rule in ("pine", "pine_dema", "pine_superosc", "ta"):
            result = supertrend(
                high, low, close, 10, 3.0,
                atr_smoothing="wilder", atr_seed="sma", rule=rule,
                atr_first_bar="hl",
            )
            assert not (result.lower_band == 0.0).any()
            assert not (result.upper_band == 0.0).any()
            assert result.trend.dropna().isin([-1.0, 1.0]).all()
