"""Benchmark the Supertrend ``"ta"`` recursion and the optional numba kernel.

This is the reproducible harness for the #317/#378 Numba evaluation. It does
not add a dependency: ``numba`` is already locked transitively via
``vectorbt``. The kernels themselves live in
``shared.indicators.core`` (``_supertrend_ta_kernel_py`` reference +
``_supertrend_ta_kernel_nb`` lazy numba dispatch); when numba is
unavailable the script still times the Python kernel.
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
from shared.indicators import _numba, core


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
        core._supertrend_ta_kernel_py(src, close, atr, args.multiplier)

    python_ms = _best_ms(run_python, args.repeats)
    print(f"python _supertrend_ta: {python_ms:.1f} ms")

    if _numba._load_njit() is None:
        print("numba unavailable; skipping @njit comparison")
        return

    # Compile/warm the kernel once, then verify it matches the Python loop.
    py_result = core._supertrend_ta_kernel_py(
        src, close, atr, args.multiplier
    )
    try:
        nb_result = core._supertrend_ta_kernel_nb(
            src, close, atr, args.multiplier
        )
    except _numba._NumbaUnavailable:
        print("numba kernel failed to build; skipping @njit comparison")
        return
    ok = py_result[3] == nb_result[3] and all(
        _same(a, b) for a, b in zip(py_result[:3], nb_result[:3], strict=True)
    )
    print(f"numba parity: {'ok' if ok else 'MISMATCH'}")

    def run_numba() -> None:
        core._supertrend_ta_kernel_nb(src, close, atr, args.multiplier)

    numba_ms = _best_ms(run_numba, args.repeats)
    print(f"numba _supertrend_ta: {numba_ms:.1f} ms")
    print(f"speedup: {python_ms / numba_ms:.1f}x")


if __name__ == "__main__":
    main()
