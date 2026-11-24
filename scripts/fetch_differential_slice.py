#!/usr/bin/env python3
"""Generate the real-data slice the differential engine tests run on.

The differential suite (``tests/test_differential_reference_strategies.py``)
uses one short real EURUSD 15m slice. It used to be a committed CSV; the
repository is public and vendor market data is licensed to be queried, not
redistributed through a cloneable git history, so the slice is generated here
and cached instead. See ``data_tools/differential_slice.py`` for the window,
the schema and the validation contract, and for what the removed fixture
actually was.

Run it from the repository root, as a module::

    python -m scripts.fetch_differential_slice

The slice lands in ``$DATA_DIR/differential_reference/eurusd_15m_slice.csv``
(``DATA_DIR``/``TRADING_DATA_DIR``) unless ``DIFFERENTIAL_REAL_SLICE_CSV``
names another path. It is cached, not committed: CI restores it from
``actions/cache`` and only downloads when the cache misses, which is why
``--skip-if-valid`` exists.

Deterministic and verifiable: the window is fixed, the resample rules are the
platform's own (``data_tools/resample.py``), and the written file is validated
against the row-count/OHLC contract before it lands. A run that cannot produce
a valid slice exits non-zero, because a truncated slice that looks complete is
the failure mode that would quietly narrow the suite. Exit status 3 specifically
means the Dukascopy download failed, which is the signal CI uses to warn and
skip the real-data scenarios instead of failing the job; any other non-zero
status remains a hard failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of reading a different tree's constants and writing the slice to a
# path that tree resolves elsewhere. See tests/test_script_import_origin.py; run as:
#     python -m scripts.fetch_differential_slice
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("fetch_differential_slice")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from data_tools.differential_slice import (
    OFFER_SIDE,
    SLICE_PATH_ENV,
    SLICE_SOURCE_TIMEFRAME,
    SLICE_SYMBOL,
    SLICE_TIMEFRAME,
    SLICE_WINDOW_END,
    SLICE_WINDOW_START,
    SliceDownloadError,
    SliceValidationError,
    fetch_slice_frame,
    read_slice,
    resolve_slice_path,
    write_slice,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.fetch_differential_slice",
        description=(
            "Download the fixed EURUSD window, resample it to 15m and write the "
            "differential reference slice as CSV."
        ),
    )
    parser.add_argument(
        "--out",
        "--output",
        dest="out",
        default=None,
        help=(
            "where to write the slice (default: $%s, else the platform data dir)"
            % SLICE_PATH_ENV
        ),
    )
    parser.add_argument(
        "--skip-if-valid",
        action="store_true",
        help=(
            "exit 0 without touching the network when the destination already holds a "
            "valid slice. Used by CI behind actions/cache, where a cache hit is an "
            "unchanged slice and a miss is a fresh download."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        help="daily files fetched per batch window (default: 4; Dukascopy rate-limits hard)",
    )
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=1000,
        help="pause between batches in milliseconds (default: 1000)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="per-file retries inside the downloader (default: 3)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=4,
        help=(
            "whole-window retries on top of the per-file retries (default: 4). Dukascopy "
            "answers a burst from one address with HTTP 429 for minutes at a time, which no "
            "per-file count can out-wait"
        ),
    )
    parser.add_argument(
        "--retry-pause-seconds",
        type=float,
        default=45.0,
        help=(
            "pause before the second whole-window attempt (default: 45). The pause doubles "
            "per attempt, capped at 300s"
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="only report errors")
    return parser


def _configure_logging(quiet: bool) -> None:
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(levelname)s  %(message)s",
        stream=sys.stdout,
    )


def _plan(path: Path, skip_if_valid: bool) -> tuple[bool, str]:
    """Whether the existing file can be reused, and why."""
    if not skip_if_valid or not path.is_file():
        return False, ""
    try:
        read_slice(path)
    except SliceValidationError as exc:
        return False, f"the cached slice at {path} is not usable ({exc}); downloading again"
    return True, f"reusing the valid cached slice at {path}"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _configure_logging(args.quiet)

    # ``--out`` wins over the environment; both win over the data-dir default.
    destination = (
        resolve_slice_path({SLICE_PATH_ENV: args.out}) if args.out else resolve_slice_path()
    )
    reuse, message = _plan(destination, args.skip_if_valid)
    if message:
        print(message)
    if reuse:
        return 0

    try:
        frame = asyncio.run(
            fetch_slice_frame(
                batch_size=args.batch_size,
                pause_ms=args.pause_ms,
                max_retries=args.max_retries,
                attempts=args.attempts,
                retry_pause_seconds=args.retry_pause_seconds,
                log_callback=print if not args.quiet else None,
            )
        )
    except SliceDownloadError as exc:
        # CI turns this status into a warning plus a skipped real-data slice;
        # it is deliberately not the generic validation failure below.
        print(
            f"warning: Dukascopy slice download failed: {exc}; "
            "the real-data tests will skip when no cache exists",
            file=sys.stderr,
        )
        return 3
    except SliceValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        written = write_slice(destination, frame)
    except SliceValidationError as exc:
        # A run that cannot produce a valid slice is a failed run, and CI must see it as one.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"wrote {written.name}: {len(frame)} {SLICE_TIMEFRAME} {SLICE_SYMBOL} {OFFER_SIDE} bars "
        f"({SLICE_WINDOW_START} → {SLICE_WINDOW_END}, resampled from {SLICE_SOURCE_TIMEFRAME}) "
        f"→ {written}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
