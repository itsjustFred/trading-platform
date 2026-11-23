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
is always required there. Incomplete downloads — missing sessions, missing
rows, a truncated window — are download failures too (exit 3): the source did
not deliver the data. Structural contract violations (bad columns,
unparseable timestamps, bars on unexpected days) and any other error exit 1 —
they are never a skip.
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
    sessions_in_range,
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

#: Dukascopy USA500 CFD 1-minute candles resampled to 5-minute regular-session
#: bars, 2026-03-20..2026-05-15 (docs/reports/parity-web-strategies.md).
USA500_INSTRUMENT = "usa500idxusd"
USA500_START = datetime(2026, 3, 20, tzinfo=timezone.utc)
USA500_END = datetime(2026, 5, 15, tzinfo=timezone.utc)
USA500_FIXTURE = "usa500_5m_rth"

_REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


class ParityDownloadError(RuntimeError):
    """The download itself produced nothing usable; maps to exit 3.

    Only genuine download/network failures get this status — CI warns and
    skips the real-data parity tests on pull requests. Anything else (a
    validation failure, a code bug) must exit 1 so it can never masquerade
    as a skip (the #334 contract).
    """


class ParityFetchError(RuntimeError):
    """A downloaded frame failed validation; exit status is 1, never a skip."""


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


def _daily_window_sessions(start: str, end: str) -> tuple:
    """Expected exchange sessions for a ``[start, end)`` yfinance window."""
    first = pd.Timestamp(start).date()
    # ``end`` is exclusive for yfinance, so the last expected session is the
    # day before it.
    last = (pd.Timestamp(end) - pd.Timedelta(days=1)).date()
    return sessions_in_range(first, last)


def _validate_daily(stem: str, frame, start: str, end: str) -> None:
    if list(frame.columns) != ["timestamp", *_REQUIRED_COLUMNS[1:]]:
        raise ParityFetchError(f"{stem}: bad columns {list(frame.columns)}")
    expected = _daily_window_sessions(start, end)
    if not expected:
        raise ParityFetchError(f"{stem}: no exchange sessions in {start}..{end}")
    # Exact completeness: a throttled or truncated yfinance answer must never
    # be cached as valid — every scheduled session needs exactly one daily
    # bar. Missing data is a download failure (exit 3), not a contract
    # violation: the source simply did not deliver the window.
    if len(frame) != len(expected):
        raise ParityDownloadError(
            f"{stem}: {len(frame)} rows, expected exactly {len(expected)} "
            f"(one per exchange session {expected[0]}..{expected[-1]})"
        )
    first = pd.Timestamp(frame["timestamp"].iloc[0]).date()
    last = pd.Timestamp(frame["timestamp"].iloc[-1]).date()
    if first != expected[0] or last != expected[-1]:
        raise ParityDownloadError(
            f"{stem}: window {first}..{last} does not cover "
            f"{expected[0]}..{expected[-1]}"
        )


def _fetch_daily(stem: str, ticker: str, start: str, end: str):
    import yfinance as yf

    try:
        raw = yf.download(ticker, start=start, end=end, auto_adjust=False, progress=False)
    except Exception as exc:
        raise ParityDownloadError(f"{stem}: yfinance download failed: {exc}") from exc
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.dropna(subset=["Close"])
    if raw.empty:
        raise ParityDownloadError(f"{stem}: yfinance returned no rows for {ticker}")
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
    _validate_daily(stem, frame, start, end)
    return frame


def _rth_bars(minute) -> pd.DataFrame:
    """Resample 1m to 5m and keep complete US regular-session days.

    Regular session = 09:30-16:00 America/New_York on exchange sessions
    (``shared.market_calendar``). Sessions with fewer bars than a full session
    are partial edge sessions and are dropped; ``_validate_usa500`` then
    requires every scheduled session in the window to be present with its
    exact bar count, so a throttled Dukascopy day can never be cached.
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


def _expected_rth_bars(day) -> int:
    """Five-minute bars a complete regular session on ``day`` must contain."""
    span = session_close_utc(day) - session_open_utc(day)
    return int(span / pd.Timedelta(minutes=5))


def _validate_usa500(frame) -> None:
    """Exact completeness: every scheduled session in the window, exact bars.

    Dukascopy answers 403/429-throttled days with empty bytes that the
    downloader silently skips, so a partial download used to pass a
    ``>= MIN_USA500_ROWS`` check and get cached. Now each session day must
    appear with exactly its full RTH bar count (78 on a regular day) and no
    unexpected days may appear — 40 sessions x 78 bars = 3120 rows for the
    2026-03-20..2026-05-15 window.
    """
    if list(frame.columns) != ["timestamp", *_REQUIRED_COLUMNS[1:]]:
        raise ParityFetchError(f"{USA500_FIXTURE}: bad columns {list(frame.columns)}")
    stamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if stamps.isna().any():
        raise ParityFetchError(f"{USA500_FIXTURE}: unparseable timestamps")
    days = stamps.dt.tz_convert("America/New_York").dt.date
    counts = days.value_counts()
    expected = sessions_in_range(USA500_START.date(), USA500_END.date())
    if not expected:
        raise ParityFetchError(f"{USA500_FIXTURE}: no exchange sessions in window")
    for day in expected:
        want = _expected_rth_bars(day)
        got = int(counts.get(day, 0))
        if got != want:
            # Missing session data is an incomplete download (exit 3): the
            # downloader drops days whose .bi5 fetch failed after retries.
            raise ParityDownloadError(
                f"{USA500_FIXTURE}: session {day} has {got} bars, expected "
                f"exactly {want} — the Dukascopy download is incomplete"
            )
    extra = sorted(set(counts.index) - set(expected))
    if extra:
        raise ParityFetchError(
            f"{USA500_FIXTURE}: bars outside the expected sessions: {extra[:5]}"
        )


async def _fetch_usa500() -> pd.DataFrame:
    try:
        minute = await download_minute_candles(
            USA500_INSTRUMENT,
            USA500_START,
            USA500_END,
            side="bid",
            pause_ms=1000,
            max_retries=3,
        )
    except Exception as exc:
        raise ParityDownloadError(f"{USA500_INSTRUMENT}: Dukascopy download failed: {exc}") from exc
    if minute.empty:
        raise ParityDownloadError("usa500idxusd: Dukascopy returned no minute candles")
    return _rth_bars(minute)


def _fixture_valid(path: Path, stem: str) -> bool:
    """Full validation for --skip-if-valid: a cached file must pass the same
    completeness checks a fresh download does, or it is refetched — a partial
    file written before the exact checks existed can never be reused."""
    if not path.is_file():
        return False
    try:
        frame = pd.read_csv(path, parse_dates=["timestamp"])
        if stem == USA500_FIXTURE:
            _validate_usa500(frame)
        else:
            _, start, end = DAILY_SPECS[stem]
            _validate_daily(stem, frame, start, end)
        return True
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
    if args.skip_if_valid and all(
        _fixture_valid(fixture_path(s), s) for s in stems
    ):
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
    except ParityDownloadError as exc:
        logger.error("parity web data download failed: %s", exc)
        return DOWNLOAD_FAILED_EXIT
    except ParityFetchError as exc:
        # A contract violation is a failed run, never a skip (#334 contract).
        logger.error("parity web data validation failed: %s", exc)
        return 1
    except Exception as exc:
        # Code bugs (resampler, calendar, imports) must fail loudly too.
        logger.exception("parity web data fetch crashed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
