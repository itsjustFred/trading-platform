"""Benchmark the numba indicator kernels against their Python reference (issue #378).

Measures, on synthetic OHLC data (default 100k bars):

- first-call cost of each ``*_kernel_nb`` dispatcher (numba compilation,
  ``cache=True`` so this is paid once per source change), and
- warm per-call latency of the Python kernel vs the numba kernel.

Writes nothing; prints a table to stdout. Run from the repo root:

    python -m scripts.bench_indicator_kernels [--bars 100000] [--reps 5]
"""

from __future__ import annotations

import argparse
import time

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.bench_indicator_kernels
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("bench_indicator_kernels")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import numpy as np

import shared  # noqa: F401
from shared.indicators import core, presets


def _bench(fn, *args, reps: int) -> tuple[float, float]:
    """Return (first_call_seconds, best_warm_seconds)."""
    t0 = time.perf_counter()
    fn(*args)
    first = time.perf_counter() - t0
    best = float("inf")
    for _ in range(reps):
        t0 = time.perf_counter()
        fn(*args)
        best = min(best, time.perf_counter() - t0)
    return first, best


def _synthetic_ohlc(n: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0.0, 1.0, n))
    open_ = close + rng.normal(0.0, 0.3, n)
    high = np.maximum(open_, close) + rng.uniform(0.2, 1.5, n)
    low = np.minimum(open_, close) - rng.uniform(0.2, 1.5, n)
    return high, low, close


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=int, default=100_000)
    parser.add_argument("--reps", type=int, default=5)
    args = parser.parse_args()

    n = args.bars
    high, low, close = _synthetic_ohlc(n)
    src = (high + low) / 2.0
    atr = np.abs(np.random.default_rng(3).normal(1.0, 0.3, n))

    cases = [
        (
            "supertrend pine (same)",
            core._supertrend_pine_kernel_py,
            core._supertrend_pine_kernel_nb,
            (src, close, atr, 3.0, 0, 1),
        ),
        (
            "supertrend pine (cross)",
            core._supertrend_pine_kernel_py,
            core._supertrend_pine_kernel_nb,
            (src, close, atr, 3.0, 1, 14),
        ),
        (
            "supertrend ta",
            core._supertrend_ta_kernel_py,
            core._supertrend_ta_kernel_nb,
            (src, close, atr, 3.0),
        ),
        (
            "custom_swings tr",
            presets._custom_swings_tr_kernel_py,
            presets._custom_swings_tr_kernel_nb,
            (high, low, close),
        ),
        (
            "custom_swings atr",
            presets._custom_swings_atr_kernel_py,
            presets._custom_swings_atr_kernel_nb,
            (atr, 12, 1.0),
        ),
    ]

    print(f"bars={n} reps={args.reps}")
    print(f"{'kernel':<28} {'py ms':>10} {'nb first ms':>12} {'nb warm ms':>12} {'speedup':>8}")
    for name, py_fn, nb_fn, fn_args in cases:
        if nb_fn is None:
            print(f"{name:<28} {'n/a (numba unavailable)':>40}")
            continue
        _, py_best = _bench(py_fn, *fn_args, reps=args.reps)
        nb_first, nb_best = _bench(nb_fn, *fn_args, reps=args.reps)
        speedup = py_best / nb_best if nb_best else float("nan")
        print(
            f"{name:<28} {py_best * 1e3:>10.3f} {nb_first * 1e3:>12.1f} "
            f"{nb_best * 1e3:>12.3f} {speedup:>7.1f}x"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
