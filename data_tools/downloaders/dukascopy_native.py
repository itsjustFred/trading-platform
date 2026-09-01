"""
Native Python Dukascopy Downloader — async .bi5 fetcher with LZMA decompression.

Downloads tick/candle data directly from Dukascopy's datafeed servers,
eliminating the Node.js CLI dependency.

Output: Parquet files at ~/data/ohlcv/dukascopy/{asset_class}/{symbol}/{timeframe}.parquet

Usage:
    python -m data_tools.downloaders.dukascopy_native --symbols EURUSD --timeframes m1 \\
        --start 2024-01-01 --end 2024-01-04

Binary formats:
    Tick  (20 bytes): >3i2f  →  [ms_delta, ask_raw, bid_raw, ask_vol, bid_vol]
    Candle (24 bytes): >5if  →  [seconds, open, close, low, high, volume]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import lzma
import struct
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable

import pandas as pd

from data_tools.downloaders.base import Downloader, DownloadResult, SourceMeta
from shared.atomic_io import write_text_atomically
from shared.constants import DATA_DIR, resolve_genesis_path

logger = logging.getLogger(__name__)


class DownloadCancelled(RuntimeError):
    """Raised when a controlled download is cancelled by the caller."""


@dataclass(frozen=True, slots=True)
class FetchResult:
    dt: datetime
    data: bytes
    throttle_signal: bool = False
    cooldown_required: bool = False

URL_ROOT = "https://datafeed.dukascopy.com/datafeed"

MAX_BATCH_PAUSE_MS = 10_000
HARD_COOLDOWN_SECONDS = 15.0

TICK_STRUCT = struct.Struct(">3i2f")  # 20 bytes
CANDLE_STRUCT = struct.Struct(">5if")  # 24 bytes

INSTRUMENTS_JSON = Path(__file__).parent.parent / "config" / "dukascopy_instruments.json"
SOURCE_META_PATH = DATA_DIR / "source_meta.json"
GENESIS_PATH = resolve_genesis_path()

# ─── Instrument metadata ────────────────────────────────────────────────────

_instrument_cache: dict[str, dict] | None = None


def _load_instrument_meta() -> dict[str, dict]:
    global _instrument_cache
    if _instrument_cache is not None:
        return _instrument_cache
    if INSTRUMENTS_JSON.exists():
        _instrument_cache = json.loads(INSTRUMENTS_JSON.read_text())
    else:
        _instrument_cache = {}
    return _instrument_cache


def _get_decimal_factor(instrument: str) -> int:
    meta = _load_instrument_meta()
    entry = meta.get(instrument.lower(), {})
    return int(entry.get("decimal_factor", 1))


# ─── Symbol/instrument resolution ───────────────────────────────────────────

SYMBOL_CONFIG: dict[str, dict[str, str]] = {
    "EURUSD": {"instrument": "eurusd", "asset_class": "forex"},
    "GBPUSD": {"instrument": "gbpusd", "asset_class": "forex"},
    "USDJPY": {"instrument": "usdjpy", "asset_class": "forex"},
    "AUDUSD": {"instrument": "audusd", "asset_class": "forex"},
    "USDCAD": {"instrument": "usdcad", "asset_class": "forex"},
    "USDCHF": {"instrument": "usdchf", "asset_class": "forex"},
    "NZDUSD": {"instrument": "nzdusd", "asset_class": "forex"},
    "EURGBP": {"instrument": "eurgbp", "asset_class": "forex"},
    "EURJPY": {"instrument": "eurjpy", "asset_class": "forex"},
    "GBPJPY": {"instrument": "gbpjpy", "asset_class": "forex"},
    "EURCHF": {"instrument": "eurchf", "asset_class": "forex"},
    "EURCAD": {"instrument": "eurcad", "asset_class": "forex"},
    "EURAUD": {"instrument": "euraud", "asset_class": "forex"},
    "GBPCAD": {"instrument": "gbpcad", "asset_class": "forex"},
    "GBPCHF": {"instrument": "gbpchf", "asset_class": "forex"},
    "AUDJPY": {"instrument": "audjpy", "asset_class": "forex"},
    "AUDCAD": {"instrument": "audcad", "asset_class": "forex"},
    "AUDCHF": {"instrument": "audchf", "asset_class": "forex"},
    "AUDNZD": {"instrument": "audnzd", "asset_class": "forex"},
    "CADJPY": {"instrument": "cadjpy", "asset_class": "forex"},
    "CHFJPY": {"instrument": "chfjpy", "asset_class": "forex"},
    "NZDJPY": {"instrument": "nzdjpy", "asset_class": "forex"},
    "NZDCAD": {"instrument": "nzdcad", "asset_class": "forex"},
    "NZDCHF": {"instrument": "nzdchf", "asset_class": "forex"},
    "CADCHF": {"instrument": "cadchf", "asset_class": "forex"},
    # Metals
    "XAUUSD": {"instrument": "xauusd", "asset_class": "metals"},
    "XAGUSD": {"instrument": "xagusd", "asset_class": "metals"},
    # Indices
    "US30": {"instrument": "usa30idxusd", "asset_class": "indices"},
    "SP500": {"instrument": "usa500idxusd", "asset_class": "indices"},
    "US500": {"instrument": "usa500idxusd", "asset_class": "indices"},
    "NAS100": {"instrument": "usatechidxusd", "asset_class": "indices"},
    "US100": {"instrument": "usatechidxusd", "asset_class": "indices"},
    "USTEC": {"instrument": "usatechidxusd", "asset_class": "indices"},
    "USA30IDXUSD": {"instrument": "usa30idxusd", "asset_class": "indices"},
    "USA500IDXUSD": {"instrument": "usa500idxusd", "asset_class": "indices"},
    "USATECHIDXUSD": {"instrument": "usatechidxusd", "asset_class": "indices"},
    "DEUIDXEUR": {"instrument": "deuidxeur", "asset_class": "indices"},
    "GBRIDXGBP": {"instrument": "gbridxgbp", "asset_class": "indices"},
    "AUSIDXAUD": {"instrument": "ausidxaud", "asset_class": "indices"},
    "FRAIDXEUR": {"instrument": "fraidxeur", "asset_class": "indices"},
    "ESPIDXEUR": {"instrument": "espidxeur", "asset_class": "indices"},
    "HKGIDXHKD": {"instrument": "hkgidxhkd", "asset_class": "indices"},
    "EUSIDXEUR": {"instrument": "eusidxeur", "asset_class": "indices"},
    "CHEIDXCHF": {"instrument": "cheidxchf", "asset_class": "indices"},
    "JPNIDXJPY": {"instrument": "jpnidxjpy", "asset_class": "indices"},
    # Energy
    "UKOIL": {"instrument": "brentcmdusd", "asset_class": "energy"},
    "USOIL": {"instrument": "lightcmdusd", "asset_class": "energy"},
    "BRENTCMDUSD": {"instrument": "brentcmdusd", "asset_class": "energy"},
    "LIGHTCMDUSD": {"instrument": "lightcmdusd", "asset_class": "energy"},
    # Volatility
    "DOLLARIDXUSD": {"instrument": "dollaridxusd", "asset_class": "volatility"},
    "VOLIDXUSD": {"instrument": "volidxusd", "asset_class": "volatility"},
}

TIMEFRAME_MAP: dict[str, str] = {
    "tick": "tick",
    "sec": "s1",
    "s1": "s1",
    "m1": "m1",
    "1m": "m1",
    "m5": "m5",
    "5m": "m5",
    "m15": "m15",
    "15m": "m15",
    "m30": "m30",
    "30m": "m30",
    "h1": "h1",
    "1h": "h1",
    "h4": "h4",
    "4h": "h4",
    "d1": "d1",
    "1d": "d1",
    "mn1": "mn1",
}

# Map normalized timeframes to pandas resample rules
TF_RESAMPLE_RULE: dict[str, str] = {
    "s1": "1s",
    "m1": "1min",
    "m5": "5min",
    "m15": "15min",
    "m30": "30min",
    "h1": "1h",
    "h4": "4h",
    "d1": "1D",
    "mn1": "1MS",
}

# Dukascopy server provides data at these granularities:
#   tick: hourly .bi5 files with ticks
#   minute candles: daily .bi5 files
#   hourly candles: monthly .bi5 files
#   daily candles: yearly .bi5 files
# We always download the finest available and resample up.
# For tick and s1, we download tick data (hourly files).
# For m1 to mn1, we download minute candle data (daily files) and resample.
DOWNLOAD_STRATEGY: dict[str, str] = {
    "tick": "tick",
    "s1": "tick",
    "m1": "minute",
    "m5": "minute",
    "m15": "minute",
    "m30": "minute",
    "h1": "minute",
    "h4": "minute",
    "d1": "minute",
    "mn1": "minute",
}


def _normalize_genesis_timeframe(timeframe: str) -> str:
    normalized = timeframe.strip().lower()
    aliases = {
        "sec": "s1", "1m": "m1", "5m": "m5", "15m": "m15",
        "30m": "m30", "1h": "h1", "4h": "h4", "1d": "d1",
    }
    return aliases.get(normalized, normalized)


def _guess_asset_class(instrument_key: str, normalized_symbol: str) -> str:
    inst = instrument_key.lower()
    sym = normalized_symbol.upper()
    if "idx" in inst:
        return "indices"
    if "cmd" in inst or sym in {"UKOIL", "USOIL"}:
        return "energy"
    if sym.startswith("XAU") or sym.startswith("XAG"):
        return "metals"
    return "forex"


def _resolve_symbol_and_instrument(raw_symbol: str) -> tuple[str, str, str]:
    key = raw_symbol.strip().upper()
    if key in SYMBOL_CONFIG:
        cfg = SYMBOL_CONFIG[key]
        return key, cfg["instrument"], cfg["asset_class"]
    cleaned_instrument = "".join(ch for ch in raw_symbol.strip().lower() if ch.isalnum())
    normalized_symbol = "".join(ch for ch in key if ch.isalnum())
    if not cleaned_instrument:
        raise ValueError(f"Invalid Dukascopy symbol/instrument input: {raw_symbol!r}")
    asset_class = _guess_asset_class(
        cleaned_instrument, normalized_symbol or cleaned_instrument.upper()
    )
    return (normalized_symbol or cleaned_instrument.upper()), cleaned_instrument, asset_class


# ─── URL generation ─────────────────────────────────────────────────────────


def generate_tick_urls(
    instrument: str,
    start_dt: datetime,
    end_dt: datetime,
    price_type: str = "both",
) -> list[tuple[datetime, str]]:
    """Generate hourly tick-data URLs. CRITICAL: months are 0-based in URLs.

    Hourly tick files contain both offer sides per record, so ``price_type``
    only documents the requested side; ``bid``/``ask`` map to the same shared
    file. Anything else raises to keep callers honest (DV-C2).
    """
    normalized_price_type = str(price_type or "both").strip().lower()
    if normalized_price_type not in {"bid", "ask", "both"}:
        raise ValueError("price_type must be one of: bid, ask, both")

    urls = []
    current = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
    while current < end_dt:
        month_0 = current.month - 1  # 0-based months!
        url = (
            f"{URL_ROOT}/{instrument.upper()}"
            f"/{current.year}/{month_0:02d}/{current.day:02d}"
            f"/{current.hour:02d}h_ticks.bi5"
        )
        urls.append((current, url))
        current += timedelta(hours=1)
    return urls


def generate_minute_urls(
    instrument: str, start_dt: datetime, end_dt: datetime, price_type: str = "bid"
) -> list[tuple[datetime, str]]:
    """Generate daily minute-candle URLs. Months are 0-based."""
    urls = []
    current = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
    end_day = datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=timezone.utc)
    pt = price_type.upper()
    while current <= end_day:
        month_0 = current.month - 1
        url = (
            f"{URL_ROOT}/{instrument.upper()}"
            f"/{current.year}/{month_0:02d}/{current.day:02d}"
            f"/{pt}_candles_min_1.bi5"
        )
        urls.append((current, url))
        current += timedelta(days=1)
    return urls


# ─── Async download engine ──────────────────────────────────────────────────


def _looks_like_html(data: bytes) -> bool:
    """Check if response is HTML error page instead of binary data."""
    if len(data) < 20:
        return False
    head = data[:100].lower()
    return b"<html" in head or b"<!doctype" in head


def _emit_log_message(
    message: str,
    *,
    level: str = "info",
    log_callback: Callable[[str], None] | None = None,
) -> None:
    log_method = getattr(logger, level, logger.info)
    log_method(message)
    if log_callback is not None:
        prefix = level.upper()
        log_callback(f"{prefix}  {message}")


def _report_progress(
    callback: Callable[[float, str], None] | None,
    percent: float,
    message: str,
) -> None:
    if callback is None:
        return
    callback(max(0.0, min(100.0, round(percent, 2))), message)


async def _wait_for_control(
    pause_event: Event | None,
    stop_event: Event | None,
) -> None:
    if stop_event is not None and stop_event.is_set():
        raise DownloadCancelled("Download cancelled by user.")
    while pause_event is not None and pause_event.is_set():
        if stop_event is not None and stop_event.is_set():
            raise DownloadCancelled("Download cancelled by user.")
        await asyncio.sleep(1)


async def _sleep_with_control(
    delay_seconds: float,
    pause_event: Event | None,
    stop_event: Event | None,
) -> None:
    remaining = max(delay_seconds, 0.0)
    while remaining > 0:
        await _wait_for_control(pause_event, stop_event)
        step = min(0.25, remaining)
        await asyncio.sleep(step)
        remaining -= step


def _looks_like_invalid_bi5(data: bytes) -> bool:
    if not data or _looks_like_html(data):
        return True
    try:
        lzma.decompress(data)
    except lzma.LZMAError:
        return True
    return False


async def _fetch_one(
    session: Any,
    dt: datetime,
    url: str,
    semaphore: asyncio.Semaphore,
    max_retries: int = 3,
    *,
    pause_event: Event | None = None,
    stop_event: Event | None = None,
) -> FetchResult:
    """Download a single .bi5 file with rate-limit awareness."""
    throttle_signal = False
    for attempt in range(max_retries + 1):
        await _wait_for_control(pause_event, stop_event)
        async with semaphore:
            try:
                async with session.get(url, timeout=30) as resp:
                    data = await resp.read()

                    if resp.status == 404:
                        return FetchResult(dt=dt, data=b"", throttle_signal=throttle_signal)

                    if resp.status in {403, 429}:
                        throttle_signal = True

                    if resp.status != 200:
                        if attempt < max_retries:
                            await _sleep_with_control(2 ** attempt, pause_event, stop_event)
                            continue
                        logger.warning("HTTP %d for %s after %d retries", resp.status, url, max_retries)
                        return FetchResult(
                            dt=dt,
                            data=b"",
                            throttle_signal=throttle_signal,
                            cooldown_required=throttle_signal,
                        )

                    if _looks_like_invalid_bi5(data):
                        is_weekday = dt.weekday() < 5
                        if is_weekday:
                            throttle_signal = True
                        if is_weekday and attempt < max_retries:
                            await _sleep_with_control(2 ** attempt, pause_event, stop_event)
                            continue
                        return FetchResult(
                            dt=dt,
                            data=b"",
                            throttle_signal=throttle_signal,
                            cooldown_required=throttle_signal and is_weekday,
                        )

                    return FetchResult(dt=dt, data=data, throttle_signal=throttle_signal)
            except (asyncio.TimeoutError, OSError) as exc:
                if attempt < max_retries:
                    logger.debug("Retry %d/%d for %s: %s", attempt + 1, max_retries, url, exc)
                    await _sleep_with_control(2 ** attempt, pause_event, stop_event)
                    continue
                logger.warning("Failed to fetch %s after %d retries: %s", url, max_retries, exc)
                return FetchResult(dt=dt, data=b"", throttle_signal=throttle_signal)

    return FetchResult(dt=dt, data=b"", throttle_signal=throttle_signal)


async def download_bi5_files(
    urls: list[tuple[datetime, str]],
    batch_size: int = 10,
    max_retries: int = 3,
    *,
    pipeline_batches: int = 1,
    pause_ms: int = 1000,
    report_progress: Callable[[float, str], None] | None = None,
    progress_unit: str = "files",
    stop_event: Event | None = None,
    pause_event: Event | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> list[tuple[datetime, bytes]]:
    """Download .bi5 files asynchronously in controllable batches.

    ``batch_size`` is the number of files fetched per batch window and
    ``max_retries`` the per-file retry count; both are positional-compatible
    so call sites cannot pass them twice. ``pipeline_batches`` multiplies the
    in-flight window (``batch_size * pipeline_batches``).
    """
    import aiohttp

    effective_batch_size = max(1, int(batch_size))
    effective_pipeline_batches = max(1, int(pipeline_batches))
    window_size = effective_batch_size * effective_pipeline_batches
    base_pause_ms = max(0, int(pause_ms))
    current_pause_ms = base_pause_ms
    semaphore = asyncio.Semaphore(window_size)
    results: list[tuple[datetime, bytes]] = []
    total = len(urls)
    completed = 0

    unit = str(progress_unit or "files").strip().lower() or "files"

    if total == 0:
        _report_progress(report_progress, 100.0, f"No {unit} to download")
        return []

    connector = aiohttp.TCPConnector(limit=window_size, force_close=False)
    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        try:
            for batch_index, offset in enumerate(range(0, total, window_size), start=1):
                await _wait_for_control(pause_event, stop_event)
                batch_started_at_cap = current_pause_ms >= MAX_BATCH_PAUSE_MS
                batch = urls[offset : offset + window_size]
                tasks = [
                    asyncio.create_task(
                        _fetch_one(
                            session,
                            dt,
                            url,
                            semaphore,
                            max_retries,
                            pause_event=pause_event,
                            stop_event=stop_event,
                        )
                    )
                    for dt, url in batch
                ]
                try:
                    batch_results = await asyncio.gather(*tasks)
                except DownloadCancelled:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()

                batch_throttled = False
                cooldown_required = False
                for result in batch_results:
                    results.append((result.dt, result.data))
                    batch_throttled = batch_throttled or result.throttle_signal
                    cooldown_required = cooldown_required or result.cooldown_required
                    completed += 1
                    _report_progress(
                        report_progress,
                        (completed / total) * 100.0,
                        f"Downloaded {completed}/{total} {unit}",
                    )

                if batch_throttled:
                    next_pause_ms = min(
                        current_pause_ms * 2 if current_pause_ms > 0 else 1000,
                        MAX_BATCH_PAUSE_MS,
                    )
                    if next_pause_ms == MAX_BATCH_PAUSE_MS and current_pause_ms != MAX_BATCH_PAUSE_MS:
                        _emit_log_message(
                            (
                                "Potential Dukascopy rate-limit detected in batch "
                                f"{batch_index}; backoff cap reached at {MAX_BATCH_PAUSE_MS} ms."
                            ),
                            level="warning",
                            log_callback=log_callback,
                        )
                    current_pause_ms = next_pause_ms
                    _emit_log_message(
                        (
                            "Potential Dukascopy rate-limit detected in batch "
                            f"{batch_index}; increasing pause to {current_pause_ms} ms."
                        ),
                        level="warning",
                        log_callback=log_callback,
                    )

                if batch_started_at_cap and cooldown_required:
                    _emit_log_message(
                        (
                            "Dukascopy still throttled requests after the 10000 ms backoff cap; "
                            f"starting {int(HARD_COOLDOWN_SECONDS)}s cooldown and resetting pause."
                        ),
                        level="warning",
                        log_callback=log_callback,
                    )
                    await _sleep_with_control(HARD_COOLDOWN_SECONDS, pause_event, stop_event)
                    current_pause_ms = base_pause_ms
                    continue

                has_more_batches = completed < total
                if not has_more_batches:
                    continue

                await _sleep_with_control(current_pause_ms / 1000.0, pause_event, stop_event)
        except DownloadCancelled:
            raise

    return results


# ─── LZMA decompression + binary parsing ────────────────────────────────────


def decompress_bi5(data: bytes) -> bytes:
    """Decompress LZMA .bi5 data. Returns empty bytes on failure."""
    if not data:
        return b""
    try:
        return lzma.decompress(data)
    except lzma.LZMAError:
        return b""


def parse_ticks(
    raw: bytes, hour_start: datetime, decimal_factor: int
) -> list[tuple[datetime, float, float, float, float]]:
    """Parse raw tick binary: (timestamp, ask, bid, ask_vol, bid_vol)."""
    ticks = []
    size = TICK_STRUCT.size
    for offset in range(0, len(raw) - size + 1, size):
        ms, ask_raw, bid_raw, ask_vol, bid_vol = TICK_STRUCT.unpack_from(raw, offset)
        ts = hour_start + timedelta(milliseconds=ms)
        ticks.append((
            ts,
            ask_raw / decimal_factor,
            bid_raw / decimal_factor,
            round(ask_vol, 6),
            round(bid_vol, 6),
        ))
    return ticks


def parse_candles(
    raw: bytes, day_start: datetime, decimal_factor: int
) -> list[tuple[datetime, float, float, float, float, float]]:
    """Parse raw candle binary: (timestamp, open, high, low, close, volume).

    Note: Dukascopy candle order is [seconds, open, close, low, high, volume]
    — we reorder to standard OHLCV.
    """
    candles = []
    size = CANDLE_STRUCT.size
    for offset in range(0, len(raw) - size + 1, size):
        seconds, open_raw, close_raw, low_raw, high_raw, volume = (
            CANDLE_STRUCT.unpack_from(raw, offset)
        )
        ts = day_start + timedelta(seconds=seconds)
        candles.append((
            ts,
            open_raw / decimal_factor,
            high_raw / decimal_factor,
            low_raw / decimal_factor,
            close_raw / decimal_factor,
            round(volume, 6),
        ))
    return candles


# ─── Data assembly ──────────────────────────────────────────────────────────


def _assemble_tick_dataframe(
    bi5_results: list[tuple[datetime, bytes]], decimal_factor: int
) -> pd.DataFrame:
    """Decompress and parse tick data into a DataFrame."""
    all_ticks: list[tuple] = []
    for hour_start, data in sorted(bi5_results, key=lambda x: x[0]):
        raw = decompress_bi5(data)
        if raw:
            ticks = parse_ticks(raw, hour_start, decimal_factor)
            all_ticks.extend(ticks)

    if not all_ticks:
        return pd.DataFrame(columns=["timestamp", "ask", "bid", "ask_vol", "bid_vol"])

    df = pd.DataFrame(all_ticks, columns=["timestamp", "ask", "bid", "ask_vol", "bid_vol"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("timestamp").reset_index(drop=True)


def _assemble_candle_dataframe(
    bi5_results: list[tuple[datetime, bytes]], decimal_factor: int
) -> pd.DataFrame:
    """Decompress and parse minute candle data into a DataFrame."""
    all_candles: list[tuple] = []
    for day_start, data in sorted(bi5_results, key=lambda x: x[0]):
        raw = decompress_bi5(data)
        if raw:
            candles = parse_candles(raw, day_start, decimal_factor)
            all_candles.extend(candles)

    if not all_candles:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df[df["volume"] > 0]  # filter flat/zero-volume candles
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def _normalize_offer_side(value: str | None) -> str:
    normalized = str(value or "bid").strip().lower()
    if normalized not in {"bid", "ask", "both"}:
        raise ValueError("offer_side must be one of: bid, ask, both")
    return normalized


def _split_tick_sides(tick_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a shared tick frame into (bid, ask) frames keyed by timestamp.

    Dukascopy hourly tick files already contain both sides per record, so the
    merged pipeline derives each side from the same fetch instead of pulling
    the identical URL twice (DV-C2).
    """
    if tick_df.empty:
        empty = tick_df.copy()
        return empty, empty.copy()

    bid = tick_df[["timestamp", "bid", "bid_vol"]].copy()
    ask = tick_df[["timestamp", "ask", "ask_vol"]].copy()
    return bid.reset_index(drop=True), ask.reset_index(drop=True)


def _merge_tick_sides(bid_df: pd.DataFrame, ask_df: pd.DataFrame) -> pd.DataFrame:
    """Join side frames back onto a single bid+ask frame with explicit prefixes."""
    if bid_df.empty and ask_df.empty:
        return pd.DataFrame(
            columns=["timestamp", "ask", "bid", "ask_vol", "bid_vol"]
        )

    if ask_df.empty:
        # Keep the bid side without inventing ask values (DV-C2: no silent
        # substitution of bid data into ask columns).
        merged = bid_df.rename(columns={})
        merged["ask"] = pd.Series(dtype="float64")
        merged["ask_vol"] = pd.Series(dtype="float64")
        return (
            merged[["timestamp", "ask", "bid", "ask_vol", "bid_vol"]]
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"])
            .reset_index(drop=True)
        )

    if bid_df.empty:
        merged = ask_df.copy()
        merged["bid"] = pd.Series(dtype="float64")
        merged["bid_vol"] = pd.Series(dtype="float64")
        return (
            merged[["timestamp", "ask", "bid", "ask_vol", "bid_vol"]]
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"])
            .reset_index(drop=True)
        )

    merged = pd.merge(
        bid_df,
        ask_df,
        on="timestamp",
        how="inner",
        suffixes=("_dup", "_dup"),
    )
    merged = merged.rename(
        columns={
            "bid_dup": "bid",
            "bid_vol_dup": "bid_vol",
            "ask_dup": "ask",
            "ask_vol_dup": "ask_vol",
        }
    )
    missing = {"bid", "bid_vol", "ask", "ask_vol"} - set(merged.columns)
    if missing:
        raise RuntimeError(
            f"Merged tick frame is missing side columns after join: {sorted(missing)}"
        )
    return (
        merged[["timestamp", "ask", "bid", "ask_vol", "bid_vol"]]
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"])
        .reset_index(drop=True)
    )


def _prefix_ohlcv_columns(df: pd.DataFrame, side: str) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    return df.rename(
        columns={
            "open": f"{side}_open",
            "high": f"{side}_high",
            "low": f"{side}_low",
            "close": f"{side}_close",
            "volume": f"{side}_volume",
        }
    )


def _resample_side_prefixed_ohlcv(df: pd.DataFrame, timeframe: str, side: str) -> pd.DataFrame:
    if timeframe == "m1":
        return df
    base = df.rename(
        columns={
            f"{side}_open": "open",
            f"{side}_high": "high",
            f"{side}_low": "low",
            f"{side}_close": "close",
            f"{side}_volume": "volume",
        }
    )
    resampled = _resample_candles(base, timeframe)
    return _prefix_ohlcv_columns(resampled, side)


async def _download_minute_side_dataframe(
    instrument: str,
    start_dt: datetime,
    end_dt: datetime,
    *,
    side: str,
    decimal_factor: int,
    batch_size: int,
    pipeline_batches: int,
    pause_ms: int,
    max_retries: int,
    report_progress: Callable[[float, str], None] | None = None,
    stop_event: Event | None = None,
    pause_event: Event | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    urls = generate_minute_urls(instrument, start_dt, end_dt, side)
    target_unit = "days"
    _emit_log_message(
        f"Downloading {len(urls)} daily chunks for {side}...",
        log_callback=log_callback,
    )
    bi5_results = await download_bi5_files(
        urls,
        batch_size,
        max_retries,
        pipeline_batches=pipeline_batches,
        pause_ms=pause_ms,
        report_progress=report_progress,
        progress_unit=target_unit,
        stop_event=stop_event,
        pause_event=pause_event,
        log_callback=log_callback,
    )
    non_empty = sum(1 for _, d in bi5_results if d)
    _emit_log_message(
        f"Received {non_empty}/{len(urls)} non-empty {target_unit} for {side}",
        log_callback=log_callback,
    )
    return _assemble_candle_dataframe(bi5_results, decimal_factor)


def _ticks_to_ohlcv(
    tick_df: pd.DataFrame, timeframe: str, price_type: str = "bid"
) -> pd.DataFrame:
    """Resample tick data to OHLCV for a given timeframe and price type."""
    if tick_df.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    price_col = price_type.lower()
    vol_col = f"{price_col}_vol"
    rule = TF_RESAMPLE_RULE.get(timeframe)
    if not rule:
        raise ValueError(f"Cannot resample ticks to timeframe '{timeframe}'")

    indexed = tick_df.set_index("timestamp")
    resampled = indexed[price_col].resample(rule).ohlc()
    resampled.columns = ["open", "high", "low", "close"]
    resampled["volume"] = indexed[vol_col].resample(rule).sum()
    resampled = resampled.dropna(subset=["open"])
    resampled = resampled[resampled["volume"] > 0]

    result = resampled.reset_index()
    result.rename(columns={"index": "timestamp"}, inplace=True)
    if "timestamp" not in result.columns and result.index.name == "timestamp":
        result = result.reset_index()
    return result


def _resample_candles(candle_df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Resample minute candle data to higher timeframe."""
    if candle_df.empty or timeframe == "m1":
        return candle_df

    rule = TF_RESAMPLE_RULE.get(timeframe)
    if not rule:
        raise ValueError(f"Cannot resample to timeframe '{timeframe}'")

    indexed = candle_df.set_index("timestamp")
    resampled = indexed.resample(rule).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })
    resampled = resampled.dropna(subset=["open"])
    result = resampled.reset_index()
    if "timestamp" not in result.columns and result.index.name == "timestamp":
        result = result.reset_index()
    return result


# ─── Merge bid+ask for tick/s1 data ─────────────────────────────────────────


async def _download_and_assemble_merged_ticks(
    instrument: str,
    start_dt: datetime,
    end_dt: datetime,
    batch_size: int,
    max_retries: int,
    *,
    pipeline_batches: int = 1,
    pause_ms: int = 1000,
    report_progress: Callable[[float, str], None] | None = None,
    stop_event: Event | None = None,
    pause_event: Event | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> pd.DataFrame:
    """Download shared tick files and merge bid+ask into a single DataFrame.

    Dukascopy hourly tick files carry both sides per record, so each file is
    fetched once and the bid/ask columns are split out afterwards (DV-C2).
    """
    decimal_factor = _get_decimal_factor(instrument)

    urls = generate_tick_urls(instrument, start_dt, end_dt)
    _emit_log_message(
        f"Downloading {len(urls)} shared hourly tick files for bid+ask...",
        log_callback=log_callback,
    )
    bi5_results = await download_bi5_files(
        urls,
        batch_size,
        max_retries,
        pipeline_batches=pipeline_batches,
        pause_ms=pause_ms,
        report_progress=report_progress,
        progress_unit="files",
        stop_event=stop_event,
        pause_event=pause_event,
        log_callback=log_callback,
    )
    shared_df = _assemble_tick_dataframe(bi5_results, decimal_factor)

    if shared_df.empty:
        return pd.DataFrame(
            columns=["timestamp", "ask", "bid", "ask_vol", "bid_vol"]
        )

    bid_df, ask_df = _split_tick_sides(shared_df)
    _report_progress(report_progress, 95.0, "Merging bid/ask data")
    return _merge_tick_sides(bid_df, ask_df)


# ─── Genesis helpers ─────────────────────────────────────────────────────────


def _load_genesis_index() -> dict[str, dict[str, str]]:
    if GENESIS_PATH.exists():
        try:
            payload = json.loads(GENESIS_PATH.read_text())
            if isinstance(payload, dict):
                return payload
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s", GENESIS_PATH, exc)
    return {}


def _load_source_meta_index() -> dict:
    if SOURCE_META_PATH.exists():
        try:
            return json.loads(SOURCE_META_PATH.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s", SOURCE_META_PATH, exc)
    return {}


def _write_source_meta_index(index: dict) -> None:
    """Persist the source-meta index atomically.

    The catalog reads source_meta.json on every scan; writing it in place
    means a crash mid-write truncates metadata for the whole catalog.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    write_text_atomically(SOURCE_META_PATH, json.dumps(index, indent=2, sort_keys=True))


def _resolve_requested_start(
    symbol: str, timeframe: str, requested_start: str | None,
    genesis_index: dict[str, dict[str, str]],
) -> str:
    if requested_start:
        return requested_start
    symbol_data = genesis_index.get(symbol.upper(), {})
    normalized = _normalize_genesis_timeframe(timeframe)
    return symbol_data.get(normalized) or symbol_data.get("default") or "2000-01-01"


# ─── Downloader class ───────────────────────────────────────────────────────


class DukascopyNativeDownloader(Downloader):
    """Pure Python async Dukascopy downloader — no Node.js dependency."""

    @property
    def source_name(self) -> str:
        return "dukascopy"

    def list_available(self, **kwargs) -> list[dict]:
        return [
            {
                "symbol": symbol,
                "instrument": cfg["instrument"],
                "asset_class": cfg["asset_class"],
                "timeframe": "tick,s1,m1,m5,m15,m30,h1,h4,d1,mn1",
            }
            for symbol, cfg in SYMBOL_CONFIG.items()
        ]

    def download(
        self,
        symbol: str,
        timeframe: str,
        output_dir: Path,
        *,
        start: str | None = None,
        end: str | None = None,
        **kwargs,
    ) -> DownloadResult:
        """Synchronous wrapper around the async download pipeline."""
        return asyncio.run(self._download_async(
            symbol, timeframe, output_dir,
            start=start, end=end, **kwargs,
        ))

    async def _download_async(
        self,
        symbol: str,
        timeframe: str,
        output_dir: Path,
        *,
        start: str | None = None,
        end: str | None = None,
        **kwargs,
    ) -> DownloadResult:
        tf_key = timeframe.lower()
        normalized_tf = TIMEFRAME_MAP.get(tf_key)
        if not normalized_tf:
            raise ValueError(
                f"Unsupported timeframe: {timeframe}. Use one of: {list(TIMEFRAME_MAP)}"
            )
        storage_tf_map = {
            "tick": "tick",
            "s1": "s1",
            "m1": "1m",
            "m5": "5m",
            "m15": "15m",
            "m30": "30m",
            "h1": "1h",
            "h4": "4h",
            "d1": "1d",
            "mn1": "mn1",
        }
        out_tf = storage_tf_map.get(normalized_tf, normalized_tf)

        sym, instrument, asset_class = _resolve_symbol_and_instrument(symbol)
        out_path = self._make_output_path(output_dir, asset_class, sym, out_tf)
        effective_start = self._resolve_resume_start(out_path, start)
        offer_side = _normalize_offer_side(kwargs.get("offer_side", kwargs.get("price_type", "bid")))
        price_type = "bid" if offer_side == "both" else offer_side

        merge_tick = bool(kwargs.get("merge_tick", False))
        merge_s1 = bool(kwargs.get("merge_s1", False))
        merge_bid_ask = (
            (normalized_tf == "tick" and merge_tick)
            or (normalized_tf == "s1" and merge_s1)
        )
        batch_size = int(kwargs.get("batch_size", kwargs.get("concurrency", 10)))
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        pipeline_batches = int(kwargs.get("pipeline_batches", 1))
        if pipeline_batches < 1:
            raise ValueError("pipeline_batches must be >= 1")
        pause_ms = int(kwargs.get("pause_ms", 1000))
        if pause_ms < 0:
            raise ValueError("pause_ms must be >= 0")
        max_retries = int(kwargs.get("max_retries", 3))
        stop_event = kwargs.get("stop_event")
        pause_event = kwargs.get("pause_event")
        report_progress = kwargs.get("report_progress")
        log_callback = kwargs.get("log_callback")

        genesis_index = _load_genesis_index()
        requested_start = _resolve_requested_start(sym, normalized_tf, effective_start, genesis_index)
        requested_end = end or datetime.now(timezone.utc).date().isoformat()

        start_dt = datetime.fromisoformat(requested_start).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(requested_end).replace(tzinfo=timezone.utc)
        if end_dt.hour == 0 and end_dt.minute == 0:
            end_dt = end_dt.replace(hour=23, minute=59, second=59)

        decimal_factor = _get_decimal_factor(instrument)
        strategy = DOWNLOAD_STRATEGY[normalized_tf]
        if offer_side == "both" and strategy == "tick":
            merge_bid_ask = True

        _emit_log_message(
            "Native download: symbol=%s instrument=%s tf=%s strategy=%s range=%s→%s "
            "offer_side=%s merge=%s batch_size=%s pipeline_batches=%s pause_ms=%s"
            % (
                sym,
                instrument,
                normalized_tf,
                strategy,
                requested_start,
                requested_end,
                offer_side,
                merge_bid_ask,
                batch_size,
                pipeline_batches,
                pause_ms,
            ),
            log_callback=log_callback,
        )
        _report_progress(report_progress, 0.0, "Preparing download")

        if strategy == "tick":
            if merge_bid_ask:
                _emit_log_message("Downloading merged hourly tick files...", log_callback=log_callback)
                df = await _download_and_assemble_merged_ticks(
                    instrument,
                    start_dt,
                    end_dt,
                    batch_size,
                    max_retries,
                    pipeline_batches=pipeline_batches,
                    pause_ms=pause_ms,
                    report_progress=report_progress,
                    stop_event=stop_event,
                    pause_event=pause_event,
                    log_callback=log_callback,
                )
                warnings = ["offer_side:both", "price_type:bid+ask"]
            else:
                urls = generate_tick_urls(instrument, start_dt, end_dt)
                _emit_log_message(
                    f"Downloading {len(urls)} hourly tick files...",
                    log_callback=log_callback,
                )
                bi5_results = await download_bi5_files(
                    urls,
                    batch_size,
                    max_retries,
                    pipeline_batches=pipeline_batches,
                    pause_ms=pause_ms,
                    report_progress=(
                        (lambda pct, msg: _report_progress(report_progress, pct * 0.9, msg))
                        if report_progress
                        else None
                    ),
                    progress_unit="files",
                    stop_event=stop_event,
                    pause_event=pause_event,
                    log_callback=log_callback,
                )
                non_empty = sum(1 for _, d in bi5_results if d)
                _emit_log_message(
                    f"Received {non_empty}/{len(urls)} non-empty files",
                    log_callback=log_callback,
                )
                _report_progress(report_progress, 95.0, "Assembling tick data")
                df = _assemble_tick_dataframe(bi5_results, decimal_factor)
                warnings = [f"offer_side:{offer_side}", f"price_type:{price_type}"]

            if df.empty:
                raise RuntimeError(f"No tick data for {sym} in range {requested_start} → {requested_end}")

            if normalized_tf == "tick":
                pass  # keep raw ticks
            elif normalized_tf == "s1":
                df = _ticks_to_ohlcv(df, "s1", price_type)
            else:
                df = _ticks_to_ohlcv(df, normalized_tf, price_type)

        elif strategy == "minute":
            if offer_side == "both":
                bid_df = await _download_minute_side_dataframe(
                    instrument,
                    start_dt,
                    end_dt,
                    side="bid",
                    decimal_factor=decimal_factor,
                    batch_size=batch_size,
                    pipeline_batches=pipeline_batches,
                    pause_ms=pause_ms,
                    max_retries=max_retries,
                    report_progress=(
                        (lambda pct, msg: _report_progress(report_progress, pct * 0.45, f"Bid {msg}"))
                        if report_progress
                        else None
                    ),
                    stop_event=stop_event,
                    pause_event=pause_event,
                    log_callback=log_callback,
                )
                ask_df = await _download_minute_side_dataframe(
                    instrument,
                    start_dt,
                    end_dt,
                    side="ask",
                    decimal_factor=decimal_factor,
                    batch_size=batch_size,
                    pipeline_batches=pipeline_batches,
                    pause_ms=pause_ms,
                    max_retries=max_retries,
                    report_progress=(
                        (
                            lambda pct, msg: _report_progress(
                                report_progress, 45.0 + (pct * 0.45), f"Ask {msg}"
                            )
                        )
                        if report_progress
                        else None
                    ),
                    stop_event=stop_event,
                    pause_event=pause_event,
                    log_callback=log_callback,
                )
                _report_progress(report_progress, 95.0, "Merging bid/ask candle data")
                if bid_df.empty or ask_df.empty:
                    raise RuntimeError(
                        f"No bid/ask candle data for {sym} in range {requested_start} → {requested_end}"
                    )
                bid_prefixed = _prefix_ohlcv_columns(bid_df, "bid")
                ask_prefixed = _prefix_ohlcv_columns(ask_df, "ask")
                if normalized_tf != "m1":
                    bid_prefixed = _resample_side_prefixed_ohlcv(bid_prefixed, normalized_tf, "bid")
                    ask_prefixed = _resample_side_prefixed_ohlcv(ask_prefixed, normalized_tf, "ask")
                df = (
                    bid_prefixed.merge(ask_prefixed, on="timestamp", how="inner")
                    .sort_values("timestamp")
                    .drop_duplicates(subset=["timestamp"])
                    .reset_index(drop=True)
                )
                warnings = ["offer_side:both", "price_type:bid+ask"]
            else:
                df = await _download_minute_side_dataframe(
                    instrument,
                    start_dt,
                    end_dt,
                    side=price_type,
                    decimal_factor=decimal_factor,
                    batch_size=batch_size,
                    pipeline_batches=pipeline_batches,
                    pause_ms=pause_ms,
                    max_retries=max_retries,
                    report_progress=(
                        (lambda pct, msg: _report_progress(report_progress, pct * 0.9, msg))
                        if report_progress
                        else None
                    ),
                    stop_event=stop_event,
                    pause_event=pause_event,
                    log_callback=log_callback,
                )
                _report_progress(report_progress, 95.0, "Assembling candle data")
                warnings = [f"offer_side:{offer_side}", f"price_type:{price_type}"]

                if df.empty:
                    raise RuntimeError(
                        f"No candle data for {sym} in range {requested_start} → {requested_end}"
                    )

                if normalized_tf != "m1":
                    df = _resample_candles(df, normalized_tf)

        await _wait_for_control(pause_event, stop_event)

        df = self._merge_and_store_single_file(out_path, df)
        warnings.append(f"requested_range:{requested_start}->{requested_end}")
        _report_progress(report_progress, 100.0, f"Saved {out_path.name}")

        # Update source meta
        meta_key = f"{self.source_name}/{asset_class}/{sym}/{out_tf}"
        source_meta = self.get_source_meta().to_dict()
        source_meta.update({
            "instrument": instrument,
            "offer_side": offer_side,
            "price_type": "bid+ask" if (merge_bid_ask or offer_side == "both") else price_type,
            "merge_bid_ask": merge_bid_ask,
            "requested_start": requested_start,
            "requested_end": requested_end,
            "downloader": "native",
        })
        meta_index = _load_source_meta_index()
        meta_index[meta_key] = source_meta
        _write_source_meta_index(meta_index)

        return DownloadResult(
            symbol=sym,
            timeframe=out_tf,
            source=self.source_name,
            asset_class=asset_class,
            path=out_path,
            rows=len(df),
            start=str(df["timestamp"].iloc[0]) if "timestamp" in df.columns and len(df) > 0 else "",
            end=str(df["timestamp"].iloc[-1]) if "timestamp" in df.columns and len(df) > 0 else "",
            size_bytes=out_path.stat().st_size,
            warnings=warnings,
        )

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url="https://datafeed.dukascopy.com",
            license="free",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


# ─── CLI entry point ────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download OHLCV data from Dukascopy (native Python, no Node.js)"
    )
    parser.add_argument(
        "--symbols", required=False, help="Comma-separated symbols (e.g. EURUSD,GBPUSD)"
    )
    parser.add_argument(
        "--timeframes",
        default="d1",
        help="Comma-separated timeframes (tick,s1,m1,m5,m15,m30,h1,h4,d1,mn1)",
    )
    parser.add_argument(
        "--offer-side",
        "--price-type",
        dest="offer_side",
        default="bid",
        choices=["bid", "ask", "both"],
        help="Price side selection (bid, ask, both). Default: bid",
    )
    parser.add_argument(
        "--merge-tick", action="store_true",
        help="Merge bid and ask for tick downloads",
    )
    parser.add_argument(
        "--merge-s1", action="store_true",
        help="Merge bid and ask for s1 downloads",
    )
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=str(DATA_DIR), help="Output root directory")
    parser.add_argument("--list", action="store_true", help="List available instruments")
    parser.add_argument(
        "--batch-size",
        "--concurrency",
        dest="batch_size",
        type=int,
        default=10,
        help="URLs per batch / max concurrent fetches (default: 10)",
    )
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=1000,
        help="Pause between batches in milliseconds (default: 1000)",
    )
    parser.add_argument(
        "--pipeline-batches",
        type=int,
        default=1,
        help=(
            "Number of batches to keep in flight before applying the inter-batch pause "
            "(default: 1; raise to 2-4 for faster bulk downloads)"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(message)s",
        stream=sys.stdout,
    )

    dl = DukascopyNativeDownloader()

    if args.list:
        for item in dl.list_available():
            print(f"{item['symbol']:20s} {item['instrument']:20s} {item['asset_class']}")
        return

    if not args.symbols:
        parser.error("--symbols is required (or use --list)")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    output_dir = Path(args.output)

    completed = 0
    for sym in symbols:
        for tf in timeframes:
            try:
                result = dl.download(
                    sym, tf, output_dir,
                    start=args.start,
                    end=args.end,
                    offer_side=args.offer_side,
                    merge_tick=args.merge_tick,
                    merge_s1=args.merge_s1,
                    batch_size=args.batch_size,
                    pipeline_batches=args.pipeline_batches,
                    pause_ms=args.pause_ms,
                )
                completed += 1
                print(
                    f"✅ {result.symbol} {result.timeframe}: {result.rows} rows "
                    f"({result.start} → {result.end}) → {result.path}"
                )
            except Exception as exc:
                print(f"❌ {sym} {tf}: {exc}")

    print(f"\nDone: {completed} downloads completed.")


if __name__ == "__main__":
    main()
