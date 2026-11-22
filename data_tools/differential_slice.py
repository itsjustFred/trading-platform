"""The differential reference slice: window, generation and validation.

``tests/test_differential_reference_strategies.py`` runs three textbook
strategies on the native and vectorbt engines and compares them, trade by
trade, against reference simulators written in the test module. Most of its
scenarios are synthetic, but a handful run on a short **real** EURUSD 15m slice,
because a float rolling mean flips the cross bar on real quotes in ways a
hand-built series cannot reproduce.

That slice used to be a committed fixture. It is not, for two reasons, and both
are worth understanding before trusting the code below:

* Redistribution. The repository is public. Vendor market data is licensed to
  *query* it, not to ship a derivative of it inside a git history that anyone
  can clone, so the slice is generated on demand instead of committed.
* Provenance was mislabelled. The committed slice was documented as "Dukascopy
  bid, resampled from the local 1m store". It was not: bar for bar it is an
  **MT5 export** (``$TRADING_DATA_DIR/ohlcv/mt5audit/forex/EURUSD_MT5``), a
  different broker's feed of the same market - close in price (it shares several
  of the window's daily highs and lows) but not the same quotes, and with
  integer tick-count volumes where Dukascopy reports float volumes. No
  downloader in this repo can reproduce it. The slice is therefore regenerated
  here from Dukascopy's bid minute candles, which *is* reproducible, and the
  values differ from the removed file accordingly. Everything downstream of
  this module compares an engine against a reference on the same frame, so a
  different quote series validates the same contract.

The contract this module owns, and that both the fetch script and the test
module depend on:

* the window is fixed (``SLICE_WINDOW_START``/``SLICE_WINDOW_END``), so two
  runs over the same dates produce byte-identical output;
* the CSV schema is fixed (:data:`SLICE_COLUMNS`);
* the row count is fixed (:data:`EXPECTED_SLICE_ROWS`) and is *checked*, so a
  silently truncated download cannot pass for a complete one;
* the cached file resolves from :data:`SLICE_PATH_ENV` with a default under the
  platform's data directory - never inside ``tests/``, so nothing in the test
  tree can be mistaken for a source of truth again.

Usage::

    python -m scripts.fetch_differential_slice                 # generate the slice
    python -m scripts.fetch_differential_slice --skip-if-valid  # reuse a valid cached slice
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from data_tools.resample import resample_ohlcv
from shared.atomic_io import write_text_atomically
from shared.constants import DATA_DIR

logger = logging.getLogger(__name__)

# ── the slice's identity ─────────────────────────────────────────────────────

#: Symbol, instrument key and offer side the slice is built from.
SLICE_SYMBOL = "EURUSD"
SLICE_INSTRUMENT = "eurusd"
#: Dukascopy minute candles are the finest published candle frame; 15m is derived.
SLICE_SOURCE_TIMEFRAME = "m1"
OFFER_SIDE = "bid"

#: First bar of the slice (inclusive).
SLICE_WINDOW_START = "2026-02-20T00:00:00+00:00"
#: End of the window, exclusive: the last bar starts at 05:45 on 2026-03-02.
SLICE_WINDOW_END = "2026-03-02T06:00:00+00:00"

#: Timeframe of the slice itself; also the resample rule applied to the 1m data.
SLICE_TIMEFRAME = "15m"

#: Expected bar count. The window spans six full trading days (2026-02-20,
#: 02-23..02-27) plus the first six hours of the next one, i.e. 6*96 + 24.
#: A fetch that returns anything else is truncated, and must not be written.
EXPECTED_SLICE_ROWS = 600

#: Canonical CSV column order; ``timestamp`` is ISO-8601 UTC with a ``Z`` suffix.
SLICE_COLUMNS: tuple[str, ...] = ("timestamp", "open", "high", "low", "close", "volume")

#: Filename and cache location of the slice.
SLICE_FILENAME = "eurusd_15m_slice.csv"
SLICE_CACHE_SUBDIR = "differential_reference"

#: Environment variable that overrides the cache path. Set by CI to a path it
#: can restore from ``actions/cache``, and by anyone who wants the slice
#: somewhere else.
SLICE_PATH_ENV = "DIFFERENTIAL_REAL_SLICE_CSV"

#: Environment variable that turns a missing slice into a failure instead of a
#: skip, so a test run can never quietly drop the real-data scenarios.
REQUIRE_SLICE_ENV = "REQUIRE_REAL_DATA_SLICE"

#: Prices are quantised back to the source resolution (EURUSD: 5dp) and volumes
#: to 2dp, the resolution Dukascopy's ``bi5`` records carry. Rounding is not
#: cosmetic: without it the float32 parse noise (``658.120006``) leaks into the
#: CSV and two runs of the same download differ in the last digits.
PRICE_DECIMALS = 5
VOLUME_DECIMALS = 2

#: Weekday filter. Dukascopy's daily candle files carry placeholder bars for
#: the non-trading weekend - flat prices, zero or placeholder volume - so a
#: naive resample of the whole window would inflate the slice from 600 to 1056
#: bars and hand the engines a series of fake flat bars. The slice covers
#: trading days only, which is also what the removed MT5-sourced fixture did.
_TRADING_WEEKDAY_MAX = 4  # Monday..Friday (pandas: Monday == 0)

#: Ceiling for the whole-window retry pause. Dukascopy's rate limiting is
#: per-address and has been measured lasting minutes; the pause doubles per
#: attempt and stops here, so a throttled run is patient but still bounded.
MAX_RETRY_PAUSE_SECONDS = 300.0


class SliceValidationError(ValueError):
    """The slice does not satisfy the contract above (fail loudly, never skip)."""


@dataclass(frozen=True, slots=True)
class SliceWindow:
    """The fixed fetch window, parsed."""

    start: datetime
    end: datetime

    @classmethod
    def from_constants(cls) -> SliceWindow:
        return cls(
            start=datetime.fromisoformat(SLICE_WINDOW_START),
            end=datetime.fromisoformat(SLICE_WINDOW_END),
        )

    def contains(self, stamps: pd.Series) -> pd.Series:
        """Mask of the stamps inside ``[start, end)``."""
        return (stamps >= self.start) & (stamps < self.end)


# ── where the slice lives ────────────────────────────────────────────────────


def default_slice_path() -> Path:
    """The cache path used when nothing overrides it.

    Under the platform's data directory (``DATA_DIR``/``TRADING_DATA_DIR``),
    never inside ``tests/``: a fixture inside the test tree reads as a source of
    truth, which is how the redistributable CSV came to be committed.
    """
    return DATA_DIR / SLICE_CACHE_SUBDIR / SLICE_FILENAME


def resolve_slice_path(env: Mapping[str, str] | None = None) -> Path:
    """The slice path: ``SLICE_PATH_ENV`` when set, else :func:`default_slice_path`.

    ``~`` is expanded so an operator can point at a file in their home
    directory on any OS, and a relative override keeps its meaning relative to
    the working directory rather than being silently anchored elsewhere.
    """
    source = os.environ if env is None else env
    override = (source.get(SLICE_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return default_slice_path()


def _is_true(value: str | None) -> bool:
    """Whether a ``REQUIRE_SLICE_ENV``-style flag is set."""
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def slice_required(env: Mapping[str, str] | None = None) -> bool:
    """Whether a missing slice must fail the run instead of skipping it."""
    source = os.environ if env is None else env
    return _is_true(source.get(REQUIRE_SLICE_ENV))


# ── generation ───────────────────────────────────────────────────────────────


def _trading_days_only(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop Dukascopy's weekend placeholder bars (see :data:`_TRADING_WEEKDAY_MAX`)."""
    if frame.empty:
        return frame
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    kept = frame.loc[stamps.dt.weekday <= _TRADING_WEEKDAY_MAX]
    return kept.reset_index(drop=True)


def slice_window() -> SliceWindow:
    """The parsed fetch window."""
    return SliceWindow.from_constants()


def slice_grid() -> pd.DatetimeIndex:
    """The 15m trading-day bar stamps the slice must contain.

    Derived from the window rather than from a download, so the expected row
    count is a property of the contract and not of whatever a fetch happens to
    return. Pinned by the tests.
    """
    grid = pd.date_range(
        start=slice_window().start,
        end=slice_window().end - pd.Timedelta(minutes=15),
        freq="15min",
        tz="UTC",
    )
    return grid[grid.weekday <= _TRADING_WEEKDAY_MAX]


def slice_from_minute_bars(minute: pd.DataFrame) -> pd.DataFrame:
    """Turn raw 1m candle bars into the slice frame.

    The whole derivation, as a pure function of its input: bound to the fixed
    window, drop Dukascopy's weekend placeholder bars, resample with the
    platform's own 15m rules (``data_tools/resample.py``: open=first, high=max,
    low=min, close=last, volume=sum) and project onto the slice schema. Kept
    separate from the download so the transformation is testable without a
    network, and so there is exactly one description of what a slice is.
    """
    window = slice_window()
    if minute.empty:
        raise SliceValidationError(
            f"no {SLICE_SOURCE_TIMEFRAME} {OFFER_SIDE} candles for {SLICE_SYMBOL} in "
            f"{SLICE_WINDOW_START} → {SLICE_WINDOW_END}"
        )
    stamps = pd.to_datetime(minute["timestamp"], utc=True)
    bounded = minute.loc[window.contains(stamps)]
    if bounded.empty:
        raise SliceValidationError(
            f"no bar falls inside {SLICE_WINDOW_START} → {SLICE_WINDOW_END}"
        )
    trading = _trading_days_only(bounded)
    if trading.empty:
        raise SliceValidationError(
            "every bar inside the window is outside a trading day, which cannot be right"
        )
    resampled = resample_ohlcv(trading, SLICE_TIMEFRAME)
    return normalise_slice_frame(resampled)


def normalise_slice_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Project onto :data:`SLICE_COLUMNS`, in order, quantised to source resolution."""
    missing = set(SLICE_COLUMNS) - set(frame.columns)
    if missing:
        raise SliceValidationError(f"frame is missing required columns: {sorted(missing)}")
    projected = frame.loc[:, list(SLICE_COLUMNS)].copy()
    projected["timestamp"] = pd.to_datetime(projected["timestamp"], utc=True)
    for column in ("open", "high", "low", "close"):
        projected[column] = pd.to_numeric(projected[column]).round(PRICE_DECIMALS)
    projected["volume"] = pd.to_numeric(projected["volume"]).round(VOLUME_DECIMALS)
    return projected.sort_values("timestamp").reset_index(drop=True)


async def fetch_slice_frame(
    *,
    batch_size: int = 4,
    pipeline_batches: int = 1,
    pause_ms: int = 1000,
    max_retries: int = 3,
    attempts: int = 4,
    retry_pause_seconds: float = 45.0,
    log_callback=None,
) -> pd.DataFrame:
    """Download the window, resample it to 15m and return the slice frame.

    ``attempts`` is an *outer* bounded retry around the whole download, separate
    from the per-file retry/backoff inside
    :func:`data_tools.downloaders.dukascopy_native.download_bi5_files`.
    Dukascopy answers a burst of requests from one address with HTTP 429 for
    minutes at a time, which no per-file retry count can out-wait; the outer
    loop gives the whole window a fresh start instead of half a day of bars, and
    its pause doubles per attempt up to :data:`MAX_RETRY_PAUSE_SECONDS`. Every
    attempt is still bounded, and a run that cannot produce a valid slice raises
    rather than writing a partial file.
    """
    from data_tools.downloaders.dukascopy_native import download_minute_candles

    window = slice_window()
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            minute = await download_minute_candles(
                SLICE_INSTRUMENT,
                window.start,
                window.end,
                side=OFFER_SIDE,
                batch_size=batch_size,
                pipeline_batches=pipeline_batches,
                pause_ms=pause_ms,
                max_retries=max_retries,
                log_callback=log_callback,
            )
            return slice_from_minute_bars(minute)
        except (SliceValidationError, OSError) as exc:
            last_error = exc
            if attempt >= max(1, attempts):
                break
            pause = min(
                retry_pause_seconds * 2 ** (attempt - 1), MAX_RETRY_PAUSE_SECONDS
            )
            logger.warning(
                "slice fetch attempt %d/%d failed (%s); retrying in %.0fs",
                attempt,
                attempts,
                exc,
                pause,
            )
            await asyncio.sleep(pause)
    raise SliceValidationError(f"could not generate the slice: {last_error}")


def slice_csv_text(frame: pd.DataFrame) -> str:
    """The slice as CSV text: fixed column order and ``Z``-suffixed UTC stamps."""
    return frame.to_csv(index=False, date_format="%Y-%m-%dT%H:%M:%SZ")


def write_slice(path: Path, frame: pd.DataFrame) -> Path:
    """Validate the frame and write it atomically, then return ``path``.

    The validation is not a formality: a truncated or schema-drifted write would
    be indistinguishable from a good slice once it sits in the cache, and the
    tests downstream only see the file.
    """
    validate_slice_frame(frame, source=str(path))
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomically(path, slice_csv_text(frame))
    return path


# ── loading and validation ───────────────────────────────────────────────────


def validate_slice_frame(frame: pd.DataFrame, *, source: str = "slice") -> None:
    """Fail loudly on anything the differential real-data tests cannot use.

    The checks are the ones the scenarios depend on: a complete window, a
    strictly increasing UTC clock (the engines and the reference both walk the
    frame in order), finite prices and internally consistent bars. A malformed
    cached file must be an error, not a skip - a skipped real-data scenario is a
    silently narrower test.
    """
    if not isinstance(frame, pd.DataFrame):
        raise SliceValidationError(f"{source}: expected a DataFrame, got {type(frame).__name__}")

    missing = set(SLICE_COLUMNS) - set(frame.columns)
    if missing:
        raise SliceValidationError(f"{source}: missing columns {sorted(missing)}")

    rows = len(frame)
    if rows != EXPECTED_SLICE_ROWS:
        raise SliceValidationError(
            f"{source}: {rows} rows, expected {EXPECTED_SLICE_ROWS} "
            f"(the window {SLICE_WINDOW_START} → {SLICE_WINDOW_END} on trading days)"
        )

    stamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if stamps.isna().any():
        bad = int(stamps.isna().sum())
        raise SliceValidationError(f"{source}: {bad} timestamp(s) that are not parseable UTC")
    if not stamps.is_monotonic_increasing or stamps.duplicated().any():
        raise SliceValidationError(f"{source}: timestamps are not strictly increasing UTC")

    # ``notna`` is not enough: a CSV field of ``inf`` parses as a float and would pass it,
    # so finiteness is checked explicitly for prices and volume alike.
    prices = frame.loc[:, ["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    finite = np.isfinite(prices.to_numpy(dtype=float))
    if not bool(finite.all()):
        counts = {column: int(n) for column, n in zip(prices.columns, (~finite).sum(axis=0), strict=True)}
        raise SliceValidationError(f"{source}: non-finite OHLC values: {counts}")
    if not bool((prices > 0).all().all()):
        raise SliceValidationError(f"{source}: non-positive prices are not valid quotes")

    # The bar must contain its body: high >= max(open, close) and low <= min(open,
    # close). Compared on the open/close pair only - comparing against all four
    # columns makes the check vacuous by construction.
    body = prices[["open", "close"]].max(axis=1).to_numpy()
    foot = prices[["open", "close"]].min(axis=1).to_numpy()
    broken_high = np.flatnonzero(prices["high"].to_numpy() < body)
    broken_low = np.flatnonzero(prices["low"].to_numpy() > foot)
    if broken_high.size:
        at = frame["timestamp"].iloc[int(broken_high[0])]
        raise SliceValidationError(f"{source}: high < max(open, close) at {at}")
    if broken_low.size:
        at = frame["timestamp"].iloc[int(broken_low[0])]
        raise SliceValidationError(f"{source}: low > min(open, close) at {at}")

    volume = pd.to_numeric(frame["volume"], errors="coerce").to_numpy(dtype=float)
    if not bool(np.isfinite(volume).all()) or bool((volume < 0).any()):
        raise SliceValidationError(f"{source}: volume must be finite and non-negative")


def read_slice(path: Path) -> pd.DataFrame:
    """Load the slice from disk, validated. Raises :class:`SliceValidationError`."""
    path = Path(path).expanduser()
    try:
        frame = pd.read_csv(path)
    except (OSError, ValueError) as exc:
        raise SliceValidationError(f"{path}: could not be read as CSV ({exc})") from exc
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    validate_slice_frame(frame, source=str(path))
    return frame.loc[:, list(SLICE_COLUMNS)].reset_index(drop=True)
