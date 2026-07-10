"""
Discover earliest available Dukascopy dates ("genesis") for one or more symbols.

The search uses bounded single-day probes only and runs per timeframe:
1. Coarse search over years starting from 2020 using 5-year jumps.
2. Binary search over months inside the start year.
3. Binary search over days inside the start month.

Each probe downloads a single .bi5 file from Dukascopy's datafeed servers
using the native async downloader; the data is kept in memory and discarded.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from data_tools.downloaders.dukascopy_native import (
    DOWNLOAD_STRATEGY,
    TIMEFRAME_MAP,
    _resolve_symbol_and_instrument,
    download_bi5_files,
    generate_minute_urls,
    generate_tick_urls,
)
from shared.constants import DATA_DIR

logger = logging.getLogger(__name__)

VERBOSE_LOGS = False
DISCOVERY_TIMEFRAMES = ("d1", "h4", "h1", "m30", "m15", "m5", "m1", "s1", "tick")
REFERENCE_GENESIS = {
    "EURUSD": {
        "tick": "2003-05-04",
    },
    "XAUUSD": {
        "tick": "2003-05-04",
    },
}


def _looks_like_empty_response(output: str) -> bool:
    lowered = output.lower()
    return any(hint in lowered for hint in ("no data", "empty", "no csv", "0 rows", "not found"))


def _progress(message: str, *, verbose_only: bool = False) -> None:
    if verbose_only and not VERBOSE_LOGS:
        return
    print(message)


def _is_valid_probe_csv(csv_path: Path) -> bool:
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return False

    try:
        df = pd.read_csv(csv_path, nrows=5)
    except (pd.errors.EmptyDataError, ValueError):
        return False

    if df.empty:
        return False

    cols = {str(col).strip().lower() for col in df.columns}
    has_time = bool({"timestamp", "time", "datetime"} & cols)
    has_price = bool({"open", "high", "low", "close", "bid", "ask", "price"} & cols) or any(
        "bid" in col or "ask" in col or "price" in col for col in cols
    )
    return has_time and has_price


def _canonical_discovery_timeframe(timeframe: str) -> str:
    timeframe_key = timeframe.lower()
    if timeframe_key not in TIMEFRAME_MAP:
        raise ValueError(
            f"Unsupported Dukascopy timeframe {timeframe!r}. Use one of: {sorted(TIMEFRAME_MAP)}"
        )
    return TIMEFRAME_MAP[timeframe_key]


def _parse_timeframes(raw: str | None) -> list[str]:
    if raw is None:
        return list(DISCOVERY_TIMEFRAMES)

    ordered: list[str] = []
    for value in raw.split(","):
        cleaned = value.strip()
        if not cleaned:
            continue
        canonical = _canonical_discovery_timeframe(cleaned)
        if canonical not in ordered:
            ordered.append(canonical)
    return ordered or list(DISCOVERY_TIMEFRAMES)


def probe_single_day(
    symbol: str,
    probe_date: date,
    *,
    timeframe: str = "tick",
    price_type: str = "bid",
) -> bool:
    """Return True when Dukascopy provides data for the given symbol/day.

    Downloads a single .bi5 file from Dukascopy's datafeed servers (no external
    tools required) and returns True when it contains non-empty data.
    """
    timeframe_key = timeframe.lower()
    if timeframe_key not in TIMEFRAME_MAP:
        raise ValueError(
            f"Unsupported Dukascopy timeframe {timeframe!r}. Use one of: {sorted(TIMEFRAME_MAP)}"
        )

    _, instrument, _ = _resolve_symbol_and_instrument(symbol)
    dukascopy_tf = TIMEFRAME_MAP[timeframe_key]

    # Probe at 10:00 UTC to stay clear of session open/close edges.
    probe_dt = datetime(
        probe_date.year, probe_date.month, probe_date.day, 10, tzinfo=timezone.utc
    )
    end_dt = probe_dt + timedelta(hours=1)

    strategy = DOWNLOAD_STRATEGY.get(dukascopy_tf, "minute")
    if strategy == "tick":
        urls = generate_tick_urls(instrument, probe_dt, end_dt)
    else:
        urls = generate_minute_urls(instrument, probe_dt, end_dt, price_type=price_type)

    if not urls:
        return False

    try:
        results = asyncio.run(
            download_bi5_files(urls[:1], batch_size=1, pause_ms=0, max_retries=1)
        )
        return any(len(data) > 0 for _, data in results)
    except Exception:
        logger.debug("Probe failed for %s %s on %s", symbol, timeframe, probe_date)
        return False


def _previous_weekday(candidate: date) -> date:
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _year_probe_date(year: int) -> date:
    today = date.today()
    candidate = min(date(year, 12, 31), today)
    return _previous_weekday(candidate)


def _month_probe_date(year: int, month: int) -> date:
    today = date.today()
    candidate = min(date(year, month, calendar.monthrange(year, month)[1]), today)
    return _previous_weekday(candidate)


def _year_has_data(symbol: str, year: int, *, timeframe: str, price_type: str) -> bool:
    return probe_single_day(
        symbol,
        _year_probe_date(year),
        timeframe=timeframe,
        price_type=price_type,
    )


def _year_has_data_logged(symbol: str, year: int, *, timeframe: str, price_type: str) -> bool:
    has_data = _year_has_data(symbol, year, timeframe=timeframe, price_type=price_type)
    _progress(
        f"[year] {symbol} {timeframe} {year}: {'hit' if has_data else 'miss'}", verbose_only=True
    )
    return has_data


def _month_has_data(
    symbol: str,
    year: int,
    month: int,
    *,
    timeframe: str,
    price_type: str,
) -> bool:
    return probe_single_day(
        symbol,
        _month_probe_date(year, month),
        timeframe=timeframe,
        price_type=price_type,
    )


def _month_has_data_logged(
    symbol: str,
    year: int,
    month: int,
    *,
    timeframe: str,
    price_type: str,
) -> bool:
    has_data = _month_has_data(symbol, year, month, timeframe=timeframe, price_type=price_type)
    _progress(
        f"[month] {symbol} {timeframe} {year}-{month:02d}: {'hit' if has_data else 'miss'}",
        verbose_only=True,
    )
    return has_data


def _day_has_data_logged(
    symbol: str,
    probe_day: date,
    *,
    timeframe: str,
    price_type: str,
) -> bool:
    has_data = probe_single_day(
        symbol,
        probe_day,
        timeframe=timeframe,
        price_type=price_type,
    )
    _progress(
        f"[day] {symbol} {timeframe} {probe_day.isoformat()}: {'hit' if has_data else 'miss'}",
        verbose_only=True,
    )
    return has_data


def _find_start_year(
    symbol: str,
    *,
    timeframe: str,
    price_type: str,
    start_year: int = 2020,
    min_year: int = 1990,
) -> int:
    current_year = date.today().year
    year = min(start_year, current_year)
    _progress(f"Progress {symbol} {timeframe}: locating start year")

    if _year_has_data_logged(symbol, year, timeframe=timeframe, price_type=price_type):
        lower_bound = min_year - 1
        while year - 5 >= min_year:
            candidate = year - 5
            if _year_has_data_logged(symbol, candidate, timeframe=timeframe, price_type=price_type):
                year = candidate
                continue
            lower_bound = candidate
            break

        for candidate in range(max(min_year, lower_bound + 1), year + 1):
            if _year_has_data_logged(symbol, candidate, timeframe=timeframe, price_type=price_type):
                return candidate
    else:
        for candidate in range(year + 1, current_year + 1):
            if _year_has_data_logged(symbol, candidate, timeframe=timeframe, price_type=price_type):
                return candidate

    raise RuntimeError(f"Could not determine a Dukascopy start year for {symbol} ({timeframe})")


def _find_start_month(
    symbol: str,
    year: int,
    *,
    timeframe: str,
    price_type: str,
) -> int:
    low = 1
    high = 12
    candidate: int | None = None
    _progress(f"Progress {symbol} {timeframe}: locating start month in {year}")

    while low <= high:
        mid = (low + high) // 2
        if _month_has_data_logged(symbol, year, mid, timeframe=timeframe, price_type=price_type):
            candidate = mid
            high = mid - 1
        else:
            low = mid + 1

    if candidate is None:
        for month in range(1, 13):
            if _month_has_data_logged(
                symbol, year, month, timeframe=timeframe, price_type=price_type
            ):
                return month
        raise RuntimeError(
            f"Could not determine a Dukascopy start month for {symbol} in {year} ({timeframe})"
        )

    earliest = candidate
    for month in range(candidate - 1, 0, -1):
        if _month_has_data_logged(symbol, year, month, timeframe=timeframe, price_type=price_type):
            earliest = month
    return earliest


def _find_start_day(
    symbol: str,
    year: int,
    month: int,
    *,
    timeframe: str,
    price_type: str,
) -> int:
    last_day = calendar.monthrange(year, month)[1]
    low = 1
    high = last_day
    candidate: int | None = None
    _progress(f"Progress {symbol} {timeframe}: locating start day in {year}-{month:02d}")

    while low <= high:
        mid = (low + high) // 2
        if _day_has_data_logged(
            symbol,
            date(year, month, mid),
            timeframe=timeframe,
            price_type=price_type,
        ):
            candidate = mid
            high = mid - 1
        else:
            low = mid + 1

    if candidate is None:
        for day in range(1, last_day + 1):
            if _day_has_data_logged(
                symbol,
                date(year, month, day),
                timeframe=timeframe,
                price_type=price_type,
            ):
                return day
        raise RuntimeError(
            f"Could not determine a Dukascopy start day for {symbol} in {year}-{month:02d} ({timeframe})"
        )

    earliest = candidate
    for day in range(candidate - 1, 0, -1):
        if _day_has_data_logged(
            symbol,
            date(year, month, day),
            timeframe=timeframe,
            price_type=price_type,
        ):
            earliest = day
    return earliest


def discover_symbol_genesis(
    symbol: str,
    *,
    timeframe: str = "tick",
    price_type: str = "bid",
    start_year: int = 2020,
    min_year: int = 1990,
) -> str:
    """Discover the earliest Dukascopy date available for a symbol/timeframe."""
    normalized_symbol, _, _ = _resolve_symbol_and_instrument(symbol)
    canonical_timeframe = _canonical_discovery_timeframe(timeframe)
    start_year_found = _find_start_year(
        normalized_symbol,
        timeframe=canonical_timeframe,
        price_type=price_type,
        start_year=start_year,
        min_year=min_year,
    )
    start_month = _find_start_month(
        normalized_symbol,
        start_year_found,
        timeframe=canonical_timeframe,
        price_type=price_type,
    )
    start_day = _find_start_day(
        normalized_symbol,
        start_year_found,
        start_month,
        timeframe=canonical_timeframe,
        price_type=price_type,
    )
    return date(start_year_found, start_month, start_day).isoformat()


def discover_symbol_timeframe_genesis(
    symbol: str,
    *,
    timeframes: list[str] | tuple[str, ...] = DISCOVERY_TIMEFRAMES,
    price_type: str = "bid",
    start_year: int = 2020,
    min_year: int = 1990,
) -> dict[str, str]:
    normalized_symbol, _, _ = _resolve_symbol_and_instrument(symbol)
    results: dict[str, str] = {}
    for timeframe in timeframes:
        canonical_timeframe = _canonical_discovery_timeframe(timeframe)
        results[canonical_timeframe] = discover_symbol_genesis(
            normalized_symbol,
            timeframe=canonical_timeframe,
            price_type=price_type,
            start_year=start_year,
            min_year=min_year,
        )
    return dict(sorted(results.items()))


def discover_genesis_for_symbols(
    symbols: list[str],
    *,
    timeframes: list[str] | tuple[str, ...] = DISCOVERY_TIMEFRAMES,
    price_type: str = "bid",
    start_year: int = 2020,
    min_year: int = 1990,
) -> dict[str, dict[str, str]]:
    """Discover genesis dates for multiple Dukascopy symbols/timeframes."""
    results: dict[str, dict[str, str]] = {}
    for raw_symbol in symbols:
        normalized_symbol, _, _ = _resolve_symbol_and_instrument(raw_symbol)
        results[normalized_symbol] = discover_symbol_timeframe_genesis(
            normalized_symbol,
            timeframes=timeframes,
            price_type=price_type,
            start_year=start_year,
            min_year=min_year,
        )
    return dict(sorted(results.items()))


def load_genesis_index(output_path: Path) -> dict[str, dict[str, str]]:
    if not output_path.exists():
        return {}

    try:
        payload = json.loads(output_path.read_text())
    except Exception as exc:
        logger.warning("Could not read existing genesis index %s: %s", output_path, exc)
        return {}

    if not isinstance(payload, dict):
        logger.warning("Ignoring malformed genesis index in %s", output_path)
        return {}

    normalized: dict[str, dict[str, str]] = {}
    for symbol, timeframe_map in payload.items():
        if not isinstance(timeframe_map, dict):
            logger.warning("Ignoring non-mapping genesis entry for %s in %s", symbol, output_path)
            continue

        normalized_symbol = str(symbol).upper()
        normalized_timeframes: dict[str, str] = {}
        for timeframe, genesis in timeframe_map.items():
            if not genesis:
                continue
            try:
                canonical_timeframe = _canonical_discovery_timeframe(str(timeframe))
            except ValueError:
                logger.warning(
                    "Ignoring unsupported timeframe %r for %s in %s",
                    timeframe,
                    normalized_symbol,
                    output_path,
                )
                continue
            normalized_timeframes[canonical_timeframe] = str(genesis)

        if normalized_timeframes:
            normalized[normalized_symbol] = dict(sorted(normalized_timeframes.items()))

    return normalized


def save_genesis_index(results: dict[str, dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        str(symbol).upper(): dict(sorted((data or {}).items()))
        for symbol, data in sorted(results.items())
    }
    output_path.write_text(json.dumps(normalized, indent=2, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover Dukascopy genesis dates")
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated Dukascopy symbols/instruments, e.g. EURUSD,XAUUSD",
    )
    parser.add_argument(
        "--timeframes",
        default=",".join(DISCOVERY_TIMEFRAMES),
        help="Comma-separated timeframes to probe (default: d1,h4,h1,m30,m15,m5,m1,s1,tick)",
    )
    parser.add_argument(
        "--timeframe",
        default=None,
        help="Deprecated single-timeframe alias. If set, overrides --timeframes.",
    )
    parser.add_argument(
        "--price-type",
        default="bid",
        choices=["bid", "ask"],
        help="Price side for Dukascopy probes (default: bid)",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=2020,
        help="Initial coarse-search year (default: 2020)",
    )
    parser.add_argument(
        "--min-year",
        type=int,
        default=1990,
        help="Lowest year to consider during coarse search (default: 1990)",
    )
    parser.add_argument(
        "--output",
        default=str(DATA_DIR / "dukascopy_genesis.json"),
        help="Output JSON path (default: $TRADING_DATA_DIR/dukascopy_genesis.json)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-discover existing symbol/timeframe entries and overwrite saved genesis dates",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log detailed binary-search probe steps while discovering genesis dates",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    global VERBOSE_LOGS
    VERBOSE_LOGS = args.verbose

    symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
    if not symbols:
        parser.error("At least one symbol is required")

    timeframe_input = args.timeframe if args.timeframe else args.timeframes
    timeframes = _parse_timeframes(timeframe_input)
    output_path = Path(args.output).expanduser()
    results = load_genesis_index(output_path)
    failures: list[tuple[str, str, str]] = []

    for symbol in symbols:
        normalized_symbol, _, _ = _resolve_symbol_and_instrument(symbol)
        symbol_results = dict(results.get(normalized_symbol, {}))
        for timeframe in timeframes:
            existing_genesis = symbol_results.get(timeframe)
            if existing_genesis and not args.force:
                print(
                    f"Skipping {normalized_symbol} {timeframe}, already known: {existing_genesis}"
                )
                continue
            try:
                _progress(f"Discovering {normalized_symbol} {timeframe}...")
                genesis = discover_symbol_genesis(
                    normalized_symbol,
                    timeframe=timeframe,
                    price_type=args.price_type,
                    start_year=args.start_year,
                    min_year=args.min_year,
                )
                symbol_results[timeframe] = genesis
                results[normalized_symbol] = dict(sorted(symbol_results.items()))
                save_genesis_index(results, output_path)
                ref = REFERENCE_GENESIS.get(normalized_symbol, {}).get(timeframe)
                if ref and genesis != ref:
                    print(
                        f"⚠️  {normalized_symbol} {timeframe}: discovered {genesis}, reference expects {ref}"
                    )
                print(f"✅ {normalized_symbol} {timeframe}: {genesis}")
            except Exception as exc:
                failures.append((normalized_symbol, timeframe, str(exc)))
                print(f"❌ {normalized_symbol} {timeframe}: {exc}")
        if symbol_results:
            results[normalized_symbol] = dict(sorted(symbol_results.items()))

    save_genesis_index(results, output_path)
    print(f"\nSaved {len(results)} symbol genesis map(s) to {output_path}")

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
