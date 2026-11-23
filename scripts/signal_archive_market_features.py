"""Compute market features for archived Discord signals (issue #308).

``live_runner/signal_archive.py`` records every raw Discord signal plus the
cheap receipt-time features (clock, content shape). The market features the
issue names - ATR percentile, distance to SMA/VWAP, gap, trend regime - need
OHLCV, which the signal path deliberately never fetches. This script is the
offline half: it reads the append-only ``signal_log`` JSONL archive, joins each
parsed signal to the local OHLCV parquet store by symbol and timeframe, and
writes a derived ``discord_signal_features-<day>.jsonl`` sidecar per archive
day.

The sidecar is *derived* data: it is regenerated whole per input file on every
run, so re-running is idempotent and the archive itself is never touched (the
archive is append-only - a line is never rewritten).

Point-in-time correctness: only bars that were **closed** at
``message_created_at`` are fed to ``market_features_from_frame``. A bar whose
interval still contains the signal timestamp is excluded, so the features
never see data the signal could not have seen. Bid/ask spread is not computed
- it needs quotes, which the parquet store does not carry (documented in
``docs/signal_bots.md``).

    python -m scripts.signal_archive_market_features
    python -m scripts.signal_archive_market_features --since 2026-11-01 --lookback-bars 250

Reads only; writes only under ``<data>/signal_log/``.
"""

from __future__ import annotations

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of computing features against another tree's data. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.signal_archive_market_features
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("signal_archive_market_features")
# --- end import-origin guard ---

import argparse
import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pandas as pd

from live_runner.signal_archive import (
    ARCHIVE_DIR_NAME,
    ARCHIVE_FILE_PREFIX,
    ARCHIVE_SUFFIX,
    market_features_from_frame,
    utcnow_iso,
)
from live_runner.signal_sources.execution_timing import timeframe_seconds
from runner.engines.contract import normalize_ohlcv_frame, select_ohlcv_path
from shared.atomic_io import write_text_atomically
from shared.constants import DATA_DIR, normalize_timeframe
from shared.logging import get_logger, route_console_logs_to_stderr

logger = get_logger("signal-archive-features")

FEATURES_FILE_PREFIX = "discord_signal_features-"
FEATURES_SCHEMA_VERSION = 1

#: Bars of history fed to ``market_features_from_frame``. The ATR percentile
#: rank and the cumulative VWAP are computed over the frame they are given, so
#: the window must be fixed or the same signal would score differently
#: depending on how much history the parquet happens to hold. 250 bars covers
#: the slowest feature input (SMA50) roughly five times over.
DEFAULT_LOOKBACK_BARS = 250


def _archive_day(path: Path) -> str:
    """The ``YYYY-MM-DD`` an archive filename carries, or ""."""
    name = path.name
    if not (name.startswith(ARCHIVE_FILE_PREFIX) and name.endswith(ARCHIVE_SUFFIX)):
        return ""
    return name[len(ARCHIVE_FILE_PREFIX) : -len(ARCHIVE_SUFFIX)]


def _archive_files(signal_log_dir: Path, since: str = "", until: str = "") -> list[Path]:
    """Daily archive files in scope, oldest first."""
    files = sorted(
        path
        for path in signal_log_dir.glob(f"{ARCHIVE_FILE_PREFIX}*{ARCHIVE_SUFFIX}")
        if _archive_day(path)
    )
    if since:
        files = [p for p in files if _archive_day(p) >= since]
    if until:
        files = [p for p in files if _archive_day(p) <= until]
    return files


def _iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Parse a JSONL archive file, skipping lines that do not parse."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "Signal archive file unreadable; skipping",
            extra={"data": {"path": str(path), "error": str(exc)}},
        )
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # A crash mid-write can leave a partial line; the archive contract
            # says readers skip it.
            continue
        if isinstance(record, dict):
            yield record


def _signal_fields(record: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """``(symbol, timeframe, message_created_at)`` for an archivable signal.

    Only ``discord_signal_received`` records carry the raw signal; rejected
    parses have no symbol to join on and are skipped by the caller's count.
    """
    if record.get("type") != "discord_signal_received":
        return None
    if record.get("parse_status") != "parsed":
        return None
    signal = record.get("signal")
    if not isinstance(signal, Mapping):
        return None
    symbol = str(signal.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    timeframe = normalize_timeframe(str(signal.get("timeframe") or ""))
    created_at = str(record.get("message_created_at") or "").strip()
    return symbol, timeframe, created_at


def _parse_signal_time(created_at: str) -> pd.Timestamp | None:
    try:
        moment = pd.Timestamp(created_at)
    except (ValueError, TypeError):
        return None
    if pd.isna(moment):
        return None
    if moment.tzinfo is None:
        moment = moment.tz_localize("UTC")
    return moment.tz_convert("UTC")


def _closed_bars(frame: pd.DataFrame, signal_time: pd.Timestamp, bar_seconds: int) -> pd.DataFrame:
    """Bars fully closed at ``signal_time`` - no lookahead into the forming bar."""
    work = frame.copy()
    if "timestamp" not in work.columns:
        # Some feeds index on time instead of carrying the column.
        if isinstance(work.index, pd.DatetimeIndex):
            work = work.reset_index(names="timestamp")
        else:
            return work.iloc[0:0]
    stamps = pd.to_datetime(work["timestamp"], utc=True, errors="coerce")
    work = work.loc[stamps.notna()].copy()
    work["_bar_close"] = stamps[stamps.notna()] + pd.Timedelta(seconds=bar_seconds)
    closed = work.loc[work["_bar_close"] <= signal_time]
    return closed.drop(columns=["_bar_close"])


def _frame_for_signal(
    data_root: Path,
    symbol: str,
    timeframe: str,
    signal_time: pd.Timestamp,
    *,
    lookback_bars: int,
    data_source: str = "",
) -> tuple[pd.DataFrame | None, Path | None, str | None]:
    """The lookback window of bars closed at signal time, or a skip reason."""
    bar_seconds = timeframe_seconds(timeframe)
    if bar_seconds is None:
        return None, None, "unknown_timeframe"
    path, _candidates = select_ohlcv_path(data_root, symbol, timeframe, data_source=data_source)
    if path is None:
        return None, None, "no_ohlcv"
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:  # except-kind: loop-survival
        logger.warning(
            "OHLCV parquet unreadable; signal skipped",
            extra={"data": {"path": str(path), "error": str(exc)}},
        )
        return None, path, "ohlcv_unreadable"
    try:
        frame = normalize_ohlcv_frame(frame)
        closed = _closed_bars(frame, signal_time, bar_seconds)
    except Exception as exc:  # except-kind: loop-survival
        # One malformed feed must not abort the whole archive pass; the
        # signal gets a skip record like every other per-signal failure.
        logger.warning(
            "OHLCV frame unusable; signal skipped",
            extra={"data": {"path": str(path), "error": str(exc)}},
        )
        return None, path, "features_failed"
    if closed.empty:
        return None, path, "no_closed_bars"
    window = closed.tail(max(1, int(lookback_bars)))
    if "timestamp" in window.columns:
        # market_features_from_frame reports the last bar's label from the
        # index; a datetime index makes last_bar_time the bar's open time.
        window = window.set_index("timestamp")
    return window, path, None


def enrich_record(
    record: Mapping[str, Any],
    *,
    data_root: Path,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    data_source: str = "",
) -> dict[str, Any]:
    """One ``discord_signal_market_features`` record for an archive record."""
    out: dict[str, Any] = {
        "type": "discord_signal_market_features",
        "schema_version": FEATURES_SCHEMA_VERSION,
        "computed_at": utcnow_iso(),
        "discord_message_id": str(record.get("discord_message_id") or ""),
        "message_created_at": record.get("message_created_at"),
        "bot_name": record.get("bot_name"),
        "account_id": record.get("account_id"),
        "symbol": None,
        "timeframe": None,
        "ohlcv_path": None,
        "bars_used": 0,
        "features": {},
        "skip_reason": None,
    }
    fields = _signal_fields(record)
    if fields is None:
        out["skip_reason"] = (
            "not_a_parsed_received_signal"
            if record.get("type") == "discord_signal_received"
            else "not_a_received_record"
        )
        return out
    symbol, timeframe, created_at = fields
    out["symbol"] = symbol
    out["timeframe"] = timeframe or None
    signal_time = _parse_signal_time(created_at)
    if signal_time is None:
        out["skip_reason"] = "bad_message_created_at"
        return out
    if not timeframe:
        out["skip_reason"] = "no_timeframe"
        return out
    frame, path, skip_reason = _frame_for_signal(
        data_root,
        symbol,
        timeframe,
        signal_time,
        lookback_bars=lookback_bars,
        data_source=data_source,
    )
    if path is not None:
        out["ohlcv_path"] = str(path)
    if skip_reason is not None or frame is None:
        out["skip_reason"] = skip_reason or "no_frame"
        return out
    out["bars_used"] = len(frame)
    try:
        out["features"] = market_features_from_frame(frame)
    except Exception as exc:  # except-kind: loop-survival
        # market_features_from_frame is total by contract; guard anyway so a
        # regression there costs one skip record, not the whole run.
        logger.warning(
            "Market feature computation raised; signal skipped",
            extra={"data": {"error": str(exc)}},
        )
        out["skip_reason"] = "features_failed"
        return out
    if not out["features"]:
        out["skip_reason"] = "no_features"
    return out


def _dedupe_key(record: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Dedupe identity of a record, or ``None`` when it is never deduped.

    Only ``discord_signal_received`` records dedupe: a restart can re-archive
    the same message, and one message can also be *received* by two bot
    listeners running different parsers, so the key carries the listener
    identity (``bot_name``/``account_id``) next to the message id. Every
    other record type - ``discord_signal_decision`` shares the received
    record's ``discord_message_id`` - always produces its own sidecar record.
    """
    if record.get("type") != "discord_signal_received":
        return None
    message_id = str(record.get("discord_message_id") or "")
    if not message_id:
        return None
    return (
        message_id,
        str(record.get("bot_name") or ""),
        str(record.get("account_id") or ""),
    )


def process_archive_file(
    input_path: Path,
    output_path: Path,
    *,
    data_root: Path,
    lookback_bars: int = DEFAULT_LOOKBACK_BARS,
    data_source: str = "",
    seen_keys: set[tuple[str, str, str]] | None = None,
) -> dict[str, int]:
    """Enrich one archive day file into its features sidecar. Returns counts."""
    counts = {"records": 0, "features": 0, "skipped": 0, "duplicates": 0}
    lines: list[str] = []
    seen = seen_keys if seen_keys is not None else set()
    for record in _iter_records(input_path):
        counts["records"] += 1
        key = _dedupe_key(record)
        if key is not None:
            if key in seen:
                # A restart can re-archive the same message; the sidecar is
                # keyed on the id, so the first occurrence wins.
                counts["duplicates"] += 1
                continue
            seen.add(key)
        enriched = enrich_record(
            record,
            data_root=data_root,
            lookback_bars=lookback_bars,
            data_source=data_source,
        )
        if enriched.get("features"):
            counts["features"] += 1
        else:
            counts["skipped"] += 1
        lines.append(json.dumps(enriched, sort_keys=True))
    text = "\n".join(lines)
    if text:
        text += "\n"
    write_text_atomically(output_path, text)
    return counts


def _features_path_for(input_path: Path, out_dir: Path) -> Path:
    return out_dir / f"{FEATURES_FILE_PREFIX}{_archive_day(input_path)}{ARCHIVE_SUFFIX}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data root holding ohlcv/ and signal_log/ (default: $DATA_DIR)",
    )
    parser.add_argument(
        "--signal-log-dir",
        default=None,
        help="Archive directory (default: <data-dir>/signal_log)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Sidecar directory (default: <signal-log-dir>/features)",
    )
    parser.add_argument("--since", default="", help="Only archive days >= YYYY-MM-DD")
    parser.add_argument("--until", default="", help="Only archive days <= YYYY-MM-DD")
    parser.add_argument(
        "--lookback-bars",
        type=int,
        default=DEFAULT_LOOKBACK_BARS,
        help="Bars of closed history per signal (default: %(default)s)",
    )
    parser.add_argument(
        "--data-source",
        default="",
        help="Restrict OHLCV selection to a source (see select_ohlcv_path)",
    )
    args = parser.parse_args(argv)

    route_console_logs_to_stderr()

    data_root = Path(args.data_dir).expanduser() if args.data_dir else Path(DATA_DIR)
    signal_log_dir = (
        Path(args.signal_log_dir).expanduser()
        if args.signal_log_dir
        else data_root / ARCHIVE_DIR_NAME
    )
    output_dir = (
        Path(args.output_dir).expanduser()
        if args.output_dir
        else signal_log_dir / "features"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    files = _archive_files(signal_log_dir, since=args.since, until=args.until)
    if not files:
        logger.info(
            "No signal archive files in scope",
            extra={"data": {"dir": str(signal_log_dir)}},
        )
        return 0

    totals = {"records": 0, "features": 0, "skipped": 0, "duplicates": 0, "files": 0}
    seen_keys: set[tuple[str, str, str]] = set()
    for input_path in files:
        counts = process_archive_file(
            input_path,
            _features_path_for(input_path, output_dir),
            data_root=data_root,
            lookback_bars=args.lookback_bars,
            data_source=args.data_source,
            seen_keys=seen_keys,
        )
        totals["files"] += 1
        for key in ("records", "features", "skipped", "duplicates"):
            totals[key] += counts[key]
        logger.info(
            "Signal features written",
            extra={
                "data": {
                    "input": input_path.name,
                    "output": _features_path_for(input_path, output_dir).name,
                    **counts,
                }
            },
        )
    print(
        "signal_archive_market_features: "
        f"{totals['files']} file(s), {totals['records']} record(s), "
        f"{totals['features']} with features, {totals['skipped']} skipped, "
        f"{totals['duplicates']} duplicate message id(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
