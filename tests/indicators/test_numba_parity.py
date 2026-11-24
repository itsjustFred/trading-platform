"""Numba-kernel parity tests (issue #378).

The ``*_kernel_py`` reference kernels and their ``numba.njit`` compiled
copies must be **bit-identical** (``np.array_equal(..., equal_nan=True)``)
on every input shape the public functions accept: random walks with NaN
gaps, short/empty/constant series, and the characterisation fixtures from
``test_presets_characterisation``. A second group of tests pins the
dispatch contract: ``TP_DISABLE_NUMBA=1`` forces the Python kernel and a
numba failure falls back to it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from test_presets_characterisation import _clean_frame, _tick_frame

from shared.indicators import _numba, core, presets


def _assert_bit_identical(actual, expected) -> None:
    assert np.array_equal(actual, expected, equal_nan=True)


def _ohlc_arrays(n: int, seed: int, *, nan_frac: float = 0.0):
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    open_ = close + rng.normal(0.0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.2, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.2, 1.5, n)
    if nan_frac:
        mask = rng.random(n) < nan_frac
        high[mask] = np.nan
        low[mask] = np.nan
        close[mask] = np.nan
    src = (high + low) / 2.0
    atr = np.abs(rng.normal(0.5, 0.2, n))
    if nan_frac:
        atr[rng.random(n) < nan_frac] = np.nan
    return src, close, atr


_PINE_CASES = [
    # (n, seed, nan_frac, multiplier)
    (500, 1, 0.0, 3.0),
    (500, 2, 0.1, 2.0),
    (500, 3, 0.15, 3.5),
    (500, 4, 0.2, 1.5),
    (1, 5, 0.0, 2.0),
    (2, 6, 0.0, 2.0),
    (0, 7, 0.0, 2.0),
    (5, 8, 0.3, 4.0),
    (50, 9, 0.0, 2.0),
]


class TestSupertrendPineKernelParity:
    @pytest.mark.parametrize(
        "n,seed,nan_frac,multiplier", _PINE_CASES
    )
    def test_random_walks_and_edges(self, n, seed, nan_frac, multiplier):
        src, close, atr = _ohlc_arrays(n, seed, nan_frac=nan_frac)
        expected = core._supertrend_pine_kernel_py(
            src, close, atr, multiplier
        )
        actual = core._supertrend_pine_kernel_nb(
            src, close, atr, multiplier
        )
        for a, e in zip(actual, expected, strict=True):
            _assert_bit_identical(a, e)

    def test_constant_series(self):
        n = 200
        src = np.full(n, 50.0)
        close = np.full(n, 50.0)
        atr = np.full(n, 1.0)
        expected = core._supertrend_pine_kernel_py(src, close, atr, 2.0)
        actual = core._supertrend_pine_kernel_nb(src, close, atr, 2.0)
        for a, e in zip(actual, expected, strict=True):
            _assert_bit_identical(a, e)

    @pytest.mark.parametrize("frame", ["clean", "tick"])
    def test_characterisation_fixtures(self, frame):
        df = _clean_frame() if frame == "clean" else _tick_frame()
        high = pd.to_numeric(df["high"], errors="coerce").astype(float)
        low = pd.to_numeric(df["low"], errors="coerce").astype(float)
        close = pd.to_numeric(df["close"], errors="coerce").astype(float)
        result_nb = core.supertrend(
            high, low, close, 10, 3.0,
            atr_smoothing="wilder", atr_seed="sma", rule="pine",
            atr_first_bar="zero",
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TP_DISABLE_NUMBA", "1")
            result_py = core.supertrend(
                high, low, close, 10, 3.0,
                atr_smoothing="wilder", atr_seed="sma", rule="pine",
                atr_first_bar="zero",
            )
        for field in ("trend", "upper_band", "lower_band", "atr", "ready"):
            a = getattr(result_nb, field).to_numpy(dtype=float)
            e = getattr(result_py, field).to_numpy(dtype=float)
            _assert_bit_identical(a, e)


_TA_CASES = [
    (500, 11, 0.0),
    (500, 12, 0.1),
    (500, 13, 0.2),
    (1, 14, 0.0),
    (2, 15, 0.3),
    (0, 16, 0.0),
    (50, 17, 0.0),
]


class TestSupertrendTaKernelParity:
    @pytest.mark.parametrize("n,seed,nan_frac", _TA_CASES)
    def test_random_walks_and_edges(self, n, seed, nan_frac):
        src, close, atr = _ohlc_arrays(n, seed, nan_frac=nan_frac)
        expected = core._supertrend_ta_kernel_py(src, close, atr, 3.0)
        actual = core._supertrend_ta_kernel_nb(src, close, atr, 3.0)
        for a, e in zip(actual[:3], expected[:3], strict=True):
            _assert_bit_identical(a, e)
        assert actual[3] == expected[3]

    def test_all_nan_atr_first_valid_none(self):
        n = 100
        src = np.arange(n, dtype=float)
        close = np.arange(n, dtype=float)
        atr = np.full(n, np.nan)
        expected = core._supertrend_ta_kernel_py(src, close, atr, 2.0)
        actual = core._supertrend_ta_kernel_nb(src, close, atr, 2.0)
        for a, e in zip(actual[:3], expected[:3], strict=True):
            _assert_bit_identical(a, e)
        assert actual[3] == expected[3] == -1
        result = core._supertrend_ta(src, close, atr, 2.0)
        assert result[3] is None

    def test_constant_series(self):
        n = 200
        src = np.full(n, 50.0)
        close = np.full(n, 50.0)
        atr = np.full(n, 0.0)
        expected = core._supertrend_ta_kernel_py(src, close, atr, 2.0)
        actual = core._supertrend_ta_kernel_nb(src, close, atr, 2.0)
        for a, e in zip(actual[:3], expected[:3], strict=True):
            _assert_bit_identical(a, e)
        assert actual[3] == expected[3]

    @pytest.mark.parametrize("frame", ["clean", "tick"])
    def test_characterisation_fixtures(self, frame):
        df = _clean_frame() if frame == "clean" else _tick_frame()
        high = pd.to_numeric(df["high"], errors="coerce").astype(float)
        low = pd.to_numeric(df["low"], errors="coerce").astype(float)
        close = pd.to_numeric(df["close"], errors="coerce").astype(float)
        result_nb = core.supertrend(
            high, low, close, 10, 3.0,
            atr_smoothing="wilder", atr_seed="first", rule="ta",
            atr_first_bar="hl", atr_min_periods=0,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TP_DISABLE_NUMBA", "1")
            result_py = core.supertrend(
                high, low, close, 10, 3.0,
                atr_smoothing="wilder", atr_seed="first", rule="ta",
                atr_first_bar="hl", atr_min_periods=0,
            )
        for field in ("trend", "upper_band", "lower_band", "atr", "ready"):
            a = getattr(result_nb, field).to_numpy(dtype=float)
            e = getattr(result_py, field).to_numpy(dtype=float)
            _assert_bit_identical(a, e)


class TestDispatch:
    def test_disable_env_forces_python_kernel(self, monkeypatch):
        calls = {"py": 0, "nb": 0}
        real_py = core._supertrend_pine_kernel_py

        def py_spy(*args):
            calls["py"] += 1
            return real_py(*args)

        def nb_spy(*args):
            calls["nb"] += 1
            raise RuntimeError("numba kernel must not run")

        monkeypatch.setenv("TP_DISABLE_NUMBA", "1")
        monkeypatch.setattr(core, "_supertrend_pine_kernel_py", py_spy)
        monkeypatch.setattr(core, "_supertrend_pine_kernel_nb", nb_spy)
        src, close, atr = _ohlc_arrays(100, 31)
        core._supertrend_pine(src, close, atr, 2.0)
        assert calls == {"py": 1, "nb": 0}

    @pytest.mark.skipif(
        _numba._load_njit() is None, reason="numba unavailable"
    )
    def test_numba_kernel_used_by_default(self, monkeypatch):
        calls = {"nb": 0}
        real_nb = core._supertrend_pine_kernel_nb

        def nb_spy(*args):
            calls["nb"] += 1
            return real_nb(*args)

        monkeypatch.delenv("TP_DISABLE_NUMBA", raising=False)
        monkeypatch.setattr(core, "_supertrend_pine_kernel_nb", nb_spy)
        src, close, atr = _ohlc_arrays(100, 32)
        core._supertrend_pine(src, close, atr, 2.0)
        assert calls["nb"] == 1

    def test_numba_compile_error_falls_back_and_marks_failed(
        self, monkeypatch
    ):
        """A NumbaError (typing/compile failure) recurs on every input, so
        the kernel is marked failed for the process after one fallback."""
        numba_errors = pytest.importorskip("numba.core.errors")
        assert _numba._load_njit() is not None

        def nb_broken(*args):
            raise numba_errors.NumbaError("simulated compile failure")

        monkeypatch.delenv("TP_DISABLE_NUMBA", raising=False)
        _numba._failed.discard("_supertrend_pine_kernel_py")
        src, close, atr = _ohlc_arrays(50, 33)
        expected = core._supertrend_pine_kernel_py(src, close, atr, 2.0)
        out = _numba.call(
            core._supertrend_pine_kernel_py, nb_broken, src, close, atr, 2.0
        )
        for a, e in zip(out, expected, strict=True):
            _assert_bit_identical(a, e)
        assert "_supertrend_pine_kernel_py" in _numba._failed
        _numba._failed.discard("_supertrend_pine_kernel_py")

    def test_object_dtype_input_does_not_disable_kernel(
        self, monkeypatch
    ):
        """Object-dtype arrays (pandas nullable ``Float64``/object columns)
        are coerced to float64 for the numba dispatch: they compile the one
        float64 signature instead of raising a per-signature TypingError
        that used to disable the kernel for the whole process."""
        calls = {"nb": 0}
        real_nb = core._supertrend_ta_kernel_nb

        def nb_spy(*args):
            calls["nb"] += 1
            return real_nb(*args)

        monkeypatch.delenv("TP_DISABLE_NUMBA", raising=False)
        monkeypatch.setattr(core, "_supertrend_ta_kernel_nb", nb_spy)
        _numba._failed.discard("_supertrend_ta_kernel_py")
        src, close, atr = _ohlc_arrays(100, 37)

        def as_object(a):
            return a.astype(object)

        out_object = core._supertrend_ta(
            as_object(src), as_object(close), as_object(atr), 3.0
        )
        out_float = core._supertrend_ta(src, close, atr, 3.0)
        assert calls["nb"] == 2
        assert "_supertrend_ta_kernel_py" not in _numba._failed
        for a, e in zip(out_object[:3], out_float[:3], strict=True):
            _assert_bit_identical(a, e)
        assert out_object[3] == out_float[3]

    def test_input_error_on_both_paths_is_not_marked_failed(
        self, monkeypatch
    ):
        """A failure the Python kernel reproduces is an input error: it is
        re-raised and must NOT disable numba for later valid calls."""

        def nb_broken(*args):
            raise ZeroDivisionError("simulated input error")

        def py_broken(*args):
            raise ZeroDivisionError("simulated input error")

        monkeypatch.delenv("TP_DISABLE_NUMBA", raising=False)
        _numba._failed.discard("py_broken")
        with pytest.raises(ZeroDivisionError):
            _numba.call(py_broken, nb_broken, 1)
        assert "py_broken" not in _numba._failed

    def test_non_numba_nb_failure_falls_back_without_disabling(
        self, monkeypatch
    ):
        """A non-NumbaError nb failure on input the Python kernel accepts
        falls back for that call but does not disable the kernel."""

        def nb_broken(*args):
            raise RuntimeError("simulated numba runtime failure")

        monkeypatch.delenv("TP_DISABLE_NUMBA", raising=False)
        _numba._failed.discard("_supertrend_pine_kernel_py")
        src, close, atr = _ohlc_arrays(50, 34)
        expected = core._supertrend_pine_kernel_py(src, close, atr, 2.0)
        out = _numba.call(
            core._supertrend_pine_kernel_py, nb_broken, src, close, atr, 2.0
        )
        for a, e in zip(out, expected, strict=True):
            _assert_bit_identical(a, e)
        assert "_supertrend_pine_kernel_py" not in _numba._failed

    def test_numba_unavailable_falls_back_to_python(self, monkeypatch):
        def nb_unavailable(*args):
            raise _numba._NumbaUnavailable("simulated")

        monkeypatch.delenv("TP_DISABLE_NUMBA", raising=False)
        _numba._failed.discard("_supertrend_pine_kernel_py")
        src, close, atr = _ohlc_arrays(50, 35)
        expected = core._supertrend_pine_kernel_py(src, close, atr, 2.0)
        out = _numba.call(
            core._supertrend_pine_kernel_py,
            nb_unavailable,
            src,
            close,
            atr,
            2.0,
        )
        for a, e in zip(out, expected, strict=True):
            _assert_bit_identical(a, e)
        assert "_supertrend_pine_kernel_py" not in _numba._failed

    def test_non_positive_period_rejected_before_dispatch(self):
        """``period=0`` must fail as a clean ValueError before either kernel
        runs — previously it surfaced as a ZeroDivisionError inside the
        recursion and permanently disabled the numba path."""
        _numba._failed.discard("_supertrend_pine_kernel_py")
        high = pd.Series(np.full(50, 51.0))
        low = pd.Series(np.full(50, 49.0))
        close = pd.Series(np.full(50, 50.0))
        with pytest.raises(ValueError, match="positive integer"):
            presets.supertrend_pine_ta(high, low, close, 0, 2.0)
        assert "_supertrend_pine_kernel_py" not in _numba._failed
