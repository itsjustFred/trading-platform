#!/usr/bin/env python3
"""Fetch the vendor market data the web-strategy parity tests run on.

The parity fixtures (``tests/parity/test_*_parity.py``) used to be committed
gzip CSVs under ``tests/parity/fixtures/parity/``. Vendor market data is
licensed to be queried, not redistributed through a cloneable git history, so
the files are generated here and cached instead — the same policy
``scripts/fetch_differential_slice.py`` applies to the differential real-data
slice. See ``tests/parity/vendor_data.py`` for the cache layout and the
skip/fail contract the tests enforce.

Run it from the repository root, as a module::

    python -m scripts.fetch_parity_web_data

The files land in ``$PARITY_VENDOR_DATA_DIR`` (or ``$DATA_DIR/parity_vendor``
when unset) under the ``web/`` suite directory, as ``<name>.csv.gz`` with the
same schema the committed fixtures had. ``--skip-if-valid`` exits 0 without
network access when every file is already present and valid, which is what CI
uses on a cache hit. Exit status 3 specifically means a download failed, the
signal CI uses to warn and skip the real-data parity scenarios on pull
requests; on push to main the fetch step fails on a download error so the data
is always required there. Any other non-zero status is a hard failure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of reading a different tree's constants and writing the cache to a
# path that tree resolves elsewhere. See tests/test_script_import_origin.py; run as:
#     python -m scripts.fetch_parity_web_data
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("fetch_parity_web_data")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import pandas as pd  # noqa: E402

import shared  # noqa: F401
from data_tools.downloaders.dukascopy_native import (  # noqa: E402
    download_minute_candles,
)
from data_tools.resample import resample_ohlcv  # noqa: E402
from shared.constants import DATA_DIR  # noqa: E402
from shared.market_calendar import (  # noqa: E402
    is_session,
    session_close_utc,
    session_open_utc,
)

logger = logging.getLogger("fetch_parity_web_data")

#: Suite directory under the vendor cache root (see tests/parity/vendor_data.py).
SUITE = "web"

#: Exit status that means "a download failed" — CI warns and skips on PRs.
DOWNLOAD_FAILED_EXIT = 3

#: yfinance daily fixtures: fixture stem -> (ticker, first kept date, exclusive end).
#: SPY/GLD are raw (unadjusted) OHLCV, 2005-01-03..2023-12-29 — the window the
#: committed fixtures covered (docs/reports/parity-web-strategies.md).
DAILY_SPECS: dict[str, tuple[str, str, str]] = {
    "spy_1d": ("SPY", "2005-01-01", "2024-01-01"),
    "gld_1d": ("GLD", "2005-01-01", "2024-01-01"),
}
MIN_DAILY_ROWS = 4000

#: Dukascopy USA500 CFD 1-minute candles resampled to 5-minute regular-session
#: bars, 2026-03-20..2026-05-15 (docs/reports/parity-web-strategies.md).
USA500_INSTRUMENT = "usa500idxusd"
USA500_START = datetime(2026, 3, 20, tzinfo=timezone.utc)
USA500_END = datetime(2026, 5, 15, tzinfo=timezone.utc)
USA500_FIXTURE = "usa500_5m_rth"
MIN_USA500_ROWS = 2000

_REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


class ParityFetchError(RuntimeError):
    """A download or validation failed; exit status maps to DOWNLOAD_FAILED_EXIT."""


def vendor_root() -> Path:
    """The cache root the tests read (``PARITY_VENDOR_DATA_DIR`` or DATA_DIR)."""
    import os

    override = (os.environ.get("PARITY_VENDOR_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return DATA_DIR / "parity_vendor"


def fixture_path(stem: str) -> Path:
    return vendor_root() / SUITE / f"{stem}.csv.gz"


def _write_frame(path: Path, frame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    frame.to_csv(tmp, index=False, compression="gzip")
    tmp.replace(path)
    return path


def _validate_daily(stem: str, frame) -> None:
    if list(frame.columns) != ["timestamp", *_REQUIRED_COLUMNS[1:]]:
        raise ParityFetchError(f"{stem}: bad columns {list(frame.columns)}")
    if len(frame) < MIN_DAILY_ROWS:
        raise ParityFetchError(f"{stem}: {len(frame)} rows, expected >= {MIN_DAILY_ROWS}")
    first = pd.Timestamp(frame["timestamp"].iloc[0])
    last = pd.Timestamp(frame["timestamp"].iloc[-1])
    if first > pd.Timestamp("2005-02-01", tz="UTC") or last < pd.Timestamp(
        "2023-12-01", tz="UTC"
    ):
        raise ParityFetchError(f"{stem}: window {first}..{last} does not cover 2005-2023")


def _fetch_daily(stem: str, ticker: str, start: str, end: str):
    import yfinance as yf

    raw = yf.download(ticker, start=start, end=end, auto_adjust=False, progress=False)
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.dropna(subset=["Close"])
    if raw.empty:
        raise ParityFetchError(f"{stem}: yfinance returned no rows for {ticker}")
    frame = pd.DataFrame(
        {
            "timestamp": raw.index.strftime("%Y-%m-%dT%H:%M:%SZ"),
            # yfinance returns float32-priced columns; round away the
            # representation noise so the cached CSV stays clean.
            "open": raw["Open"].round(4),
            "high": raw["High"].round(4),
            "low": raw["Low"].round(4),
            "close": raw["Close"].round(4),
            "volume": raw["Volume"],
        }
    )
    _validate_daily(stem, frame)
    return frame


def _rth_bars(minute) -> pd.DataFrame:
    """Resample 1m to 5m and keep complete US regular-session days.

    Regular session = 09:30-16:00 America/New_York on exchange sessions
    (``shared.market_calendar``). Sessions with fewer bars than the modal
    full-session count are partial edge sessions and are dropped, matching the
    "38 full sessions, 2 partial dropped" fixture contract.
    """
    frame = resample_ohlcv(
        minute.rename(columns={c: c.lower() for c in minute.columns}), "5m"
    )
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    et = stamps.dt.tz_convert("America/New_York")
    minutes = et.dt.hour * 60 + et.dt.minute
    on_rth_clock = (minutes >= 9 * 60 + 30) & (minutes < 16 * 60)
    days = et.dt.date
    keep = pd.Series(False, index=frame.index)
    for day in pd.unique(days):
        if not is_session(day):
            continue
        mask = (days == day) & on_rth_clock
        # Session bounds in UTC via the shared calendar (DST-correct).
        open_utc = session_open_utc(day)
        close_utc = session_close_utc(day)
        in_session = (stamps >= open_utc) & (stamps < close_utc)
        day_bars = int((mask & in_session).sum())
        # A full RTH session has 78 five-minute bars; keep only complete ones.
        if day_bars >= 78:
            keep |= mask & in_session
    out = frame.loc[keep, list(_REQUIRED_COLUMNS)].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).dt.strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return out.reset_index(drop=True)


def _validate_usa500(frame) -> None:
    if len(frame) < MIN_USA500_ROWS:
        raise ParityFetchError(
            f"{USA500_FIXTURE}: {len(frame)} rows, expected >= {MIN_USA500_ROWS}"
        )
    if list(frame.columns) != ["timestamp", *_REQUIRED_COLUMNS[1:]]:
        raise ParityFetchError(f"{USA500_FIXTURE}: bad columns {list(frame.columns)}")


async def _fetch_usa500() -> pd.DataFrame:
    minute = await download_minute_candles(
        USA500_INSTRUMENT,
        USA500_START,
        USA500_END,
        side="bid",
        pause_ms=1000,
        max_retries=3,
    )
    if minute.empty:
        raise ParityFetchError("usa500idxusd: Dukascopy returned no minute candles")
    return _rth_bars(minute)


def _fixture_valid(path: Path) -> bool:
    """Cheap presence+shape check for --skip-if-valid (full validation on write)."""
    if not path.is_file():
        return False
    try:
        frame = pd.read_csv(path, nrows=5)
        return {"timestamp", "open", "high", "low", "close", "volume"} <= set(frame.columns)
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-if-valid",
        action="store_true",
        help="exit 0 without network access when every fixture is already cached",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    stems = [*DAILY_SPECS, USA500_FIXTURE]
    if args.skip_if_valid and all(_fixture_valid(fixture_path(s)) for s in stems):
        logger.info("all parity web fixtures are cached and valid; nothing to do")
        return 0

    try:
        for stem, (ticker, start, end) in DAILY_SPECS.items():
            frame = _fetch_daily(stem, ticker, start, end)
            _write_frame(fixture_path(stem), frame)
            logger.info("%s: %d rows -> %s", stem, len(frame), fixture_path(stem))

        usa500 = asyncio.run(_fetch_usa500())
        _validate_usa500(usa500)
        _write_frame(fixture_path(USA500_FIXTURE), usa500)
        logger.info("%s: %d rows -> %s", USA500_FIXTURE, len(usa500), fixture_path(USA500_FIXTURE))
    except Exception as exc:  # download or validation failure
        logger.error("parity web data fetch failed: %s", exc)
        return DOWNLOAD_FAILED_EXIT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
