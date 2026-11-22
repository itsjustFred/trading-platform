"""Benchmark the Supertrend ``"ta"`` recursion and the optional numba kernel.

This is the reproducible harness for the #317 Numba evaluation. It does not
add a dependency: ``numba`` is already locked transitively via ``vectorbt``.
When numba is unavailable the script still times the current Python kernel.
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd

# isort: split
# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. Run as:
#     python -m scripts.bench_indicators_numba
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("bench_indicators_numba")
# --- end import-origin guard ---
import shared  # noqa: F401
from shared.indicators import core

try:
    from numba import njit
except Exception:  # pragma: no cover - optional dependency path
    njit = None


if njit is not None:

    @njit
    def _supertrend_ta_nb(
        src: np.ndarray,
        close: np.ndarray,
        atr_values: np.ndarray,
        multiplier: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
        n = close.shape[0]
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
            if trend[i - 1] > 0:
                trend[i] = 1.0 if close[i] > lower[i] else -1.0
            else:
                trend[i] = 1.0 if close[i] >= upper[i] else -1.0
        return trend, upper, lower, first_valid


def _arrays(bars: int, period: int, seed: int) -> tuple[np.ndarray, ...]:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=bars, freq="h")
    close = pd.Series(100.0 + np.cumsum(rng.normal(0.0, 1.0, bars)), index=idx)
    high = pd.Series(
        close.to_numpy() + rng.uniform(0.2, 1.5, bars), index=idx
    )
    low = pd.Series(
        close.to_numpy() - rng.uniform(0.2, 1.5, bars), index=idx
    )
    tr = core.true_range(high, low, close, first_bar="hl")
    atr = core.atr_from_tr(
        tr,
        period,
        smoothing="wilder",
        seed="first",
        min_periods=0,
    )
    src = ((high + low) / 2.0).to_numpy(dtype=float)
    return src, close.to_numpy(dtype=float), atr.to_numpy(dtype=float)


def _best_ms(fn, repeats: int) -> float:
    best = float("inf")
    for _ in range(repeats):
        started = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - started) * 1000.0)
    return best


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    return bool(np.array_equal(a, b, equal_nan=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=200_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--period", type=int, default=14)
    parser.add_argument("--multiplier", type=float, default=3.0)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    src, close, atr = _arrays(args.bars, args.period, args.seed)

    def run_python() -> None:
        core._supertrend_ta(src, close, atr, args.multiplier)

    python_ms = _best_ms(run_python, args.repeats)
    print(f"python _supertrend_ta: {python_ms:.1f} ms")

    if njit is None:
        print("numba unavailable; skipping @njit comparison")
        return

    # Compile/warm the kernel once, then verify it matches the Python loop.
    py_trend, py_upper, py_lower, py_first = core._supertrend_ta(
        src, close, atr, args.multiplier
    )
    nb_trend, nb_upper, nb_lower, nb_first = _supertrend_ta_nb(
        src, close, atr, args.multiplier
    )
    ok = (
        py_first == nb_first
        and _same(py_trend, nb_trend)
        and _same(py_upper, nb_upper)
        and _same(py_lower, nb_lower)
    )
    print(f"numba parity: {'ok' if ok else 'MISMATCH'}")

    def run_numba() -> None:
        _supertrend_ta_nb(src, close, atr, args.multiplier)

    numba_ms = _best_ms(run_numba, args.repeats)
    print(f"numba _supertrend_ta: {numba_ms:.1f} ms")
    print(f"speedup: {python_ms / numba_ms:.1f}x")


if __name__ == "__main__":
    main()
