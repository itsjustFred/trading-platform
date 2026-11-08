#!/usr/bin/env python3
"""Download the default multi-asset grid market data from Dukascopy.

Canonical requested universe:
  XAUUSD, SP500, NAS100, EURUSD, USDJPY, OIL

Dukascopy symbols are canonicalized before download:
  SP500  -> US500
  NAS100 -> USTEC
  OIL    -> USOIL

The script downloads one-minute data once per canonical symbol and resamples it
locally to 5m/15m/1h/4h/1d. Aliased symbols are stored under the requested
symbol names so ``runner.backtest_grid`` can backtest the exact requested grid.
"""
from __future__ import annotations

import argparse
import json
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data_tools.downloaders.dukascopy_native import DukascopyNativeDownloader
from data_tools.file_fingerprint import file_fingerprint, files_have_same_content
from data_tools.path_safety import build_dataset_path
from data_tools.resample import resample_file
from shared.atomic_io import atomic_replace, write_parquet_atomically
from shared.constants import (
    DATA_DIR,
    GRID_TIMEFRAME_ALIASES,
    VALID_TIMEFRAMES,
    normalize_timeframe,
)

DEFAULT_SYMBOLS = ["XAUUSD", "SP500", "NAS100", "EURUSD", "USDJPY", "OIL"]
# The canonical granularity order is owned by shared.constants; this module takes
# the subset it can actually serve rather than restating it. The old comment here
# claimed the list was "every normalized timeframe we support" and that it "must
# stay in sync", which was a hand-copied literal that had already drifted - tick,
# s1 and 1w were missing while the other tools handled all three.
#
# tick and s1 are excluded on purpose: every series here is resampled from a
# 1-minute source, and nothing can be resampled to something finer than its input.
# 1w is excluded too, but for a different reason: the same resampler produces 4h
# and 1d from 1m, and data_tools/resample.py lists 1w, so weekly bars look
# derivable and the exclusion may be an oversight. That is a decision about this
# pipeline rather than something to settle by editing a constant, so the set is
# pinned by a test and the question is left visible.
UNSUPPORTED_TIMEFRAMES = frozenset({"tick", "s1", "1w"})
TIMEFRAME_ORDER = [tf for tf in VALID_TIMEFRAMES if tf not in UNSUPPORTED_TIMEFRAMES]
# The CLI default is the served set, not a fourth copy of the same list.
DEFAULT_TIMEFRAMES = list(TIMEFRAME_ORDER)
SUPPORTED_TIMEFRAMES = frozenset(TIMEFRAME_ORDER)
DEFAULT_START = "2020-01-01"
CANONICAL_SYMBOLS = {
    "SP500": "US500",
    "NAS100": "USTEC",
    "OIL": "USOIL",
}
OIL_ALIASES = {
    "USOIL": "USOIL",
    "WTI": "USOIL",
    "CL": "USOIL",
    "UKOIL": "UKOIL",
    "BRENT": "UKOIL",
}
# One definition, in shared.constants. See the note there for why this is
# deliberately wider than TIMEFRAME_ALIASES.
TIMEFRAME_ALIASES = GRID_TIMEFRAME_ALIASES


def parse_csv_list(raw: str) -> list[str]:
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def normalize_symbol(symbol: str, oil_alias: str = "USOIL") -> str:
    cleaned = str(symbol or "").strip().upper()
    if cleaned == "OIL":
        return OIL_ALIASES.get(oil_alias.upper(), oil_alias.upper())
    return CANONICAL_SYMBOLS.get(cleaned, cleaned)


def normalize_timeframe_value(value: str) -> str:
    lowered = str(value or "").strip().lower()
    return normalize_timeframe(TIMEFRAME_ALIASES.get(lowered, lowered))


def _timeframe_sort_key(timeframe: str) -> tuple[int, str]:
    """Stable sort key for timeframes.

    Unknown timeframes sort after all known ones instead of raising a
    ``ValueError`` (the previous ``list.index`` based sort aborted the entire
    download batch when an unmapped timeframe such as ``30m`` slipped through).
    """
    try:
        return (TIMEFRAME_ORDER.index(timeframe), timeframe)
    except ValueError:
        return (len(TIMEFRAME_ORDER), timeframe)


#: Provenance column recording which quote side fed each canonical bar (C5).
OHLCV_SOURCE_COLUMN = "ohlcv_source_side"


def normalize_ohlcv_columns(path: Path) -> None:
    """Fill canonical OHLCV columns from bid/ask columns with bid priority (C5).

    Bid wins wherever present (trades lift the bid side convention used
    downstream); ask only fills bid gaps. The winning side per bar is
    recorded in ``OHLCV_SOURCE_COLUMN`` instead of being dropped with the
    source columns.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    df = pd.read_parquet(path)
    df.columns = [c.lower() for c in df.columns]
    if "timestamp" not in df.columns:
        return
    sides = pd.Series("unresolved", index=df.index)
    for target in ("open", "high", "low", "close", "volume"):
        sides = _resolve_bid_ask_side(df, sides, target)
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = pd.NA
    df[OHLCV_SOURCE_COLUMN] = sides
    canonical = ["timestamp", "open", "high", "low", "close", "volume"]
    raw_quote_columns = {
        f"{side}_{field}"
        for side in ("bid", "ask")
        for field in ("open", "high", "low", "close", "volume")
    }
    metadata_columns = [
        column
        for column in df.columns
        if column not in canonical
        and column not in raw_quote_columns
        and column != OHLCV_SOURCE_COLUMN
    ]
    keep = [*canonical, *metadata_columns, OHLCV_SOURCE_COLUMN]
    df = df[[col for col in keep if col in df.columns]]
    write_parquet_atomically(path, df)


def _resolve_bid_ask_side(df: pd.DataFrame, sides: pd.Series, target: str) -> pd.Series:
    """Fill one canonical column with bid priority; return updated sides."""

    bid_col, ask_col = f"bid_{target}", f"ask_{target}"
    has_bid = bid_col in df.columns
    has_ask = ask_col in df.columns
    if target not in df.columns:
        if has_bid:
            df[target] = df[bid_col]
        elif has_ask:
            df[target] = df[ask_col]
        else:
            df[target] = pd.NA
            return sides
    if has_bid:
        df[target] = df[target].fillna(df[bid_col])
    if has_ask:
        df[target] = df[target].fillna(df[ask_col])
    if has_bid:
        sides = sides.mask(df[bid_col].notna(), "bid")
    if has_ask:
        sides = sides.mask(sides.eq("unresolved") & df[ask_col].notna(), "ask")
    return sides


def parquet_rows(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    return len(pd.read_parquet(path, columns=["timestamp"]))


def parquet_non_null_ohlcv(path: Path) -> int:
    if not path.exists() or path.stat().st_size == 0:
        return 0
    try:
        df = pd.read_parquet(path, columns=["open", "high", "low", "close"])
    except Exception:
        return 0
    return int(df.notna().all(axis=1).sum())


def symbol_asset_class(canonical_symbol: str) -> str:
    if canonical_symbol == "XAUUSD":
        return "metals"
    if canonical_symbol in {"US500", "USTEC"}:
        return "indices"
    if canonical_symbol in {"USOIL", "UKOIL"}:
        return "energy"
    return "forex"


def ensure_alias_copy(
    canonical_path: Path,
    requested_symbol: str,
    asset_class: str,
    output_dir: Path,
) -> Path:
    """Copy the canonical parquet under the requested symbol name.

    Equal file size is not freshness: a re-download that merges or replaces rows
    can produce a byte-identical size while the content changed (DV-B30). The
    alias is refreshed whenever the canonical file is newer than the copy.

    Assumes every writer under ``ohlcv/dukascopy/`` goes through this module:
    external restores (rsync -a, tar, git checkout) can reorder mtimes without
    changing content, which would make a stale alias look fresh.
    """
    alias_path = build_dataset_path(
        output_dir,
        "ohlcv",
        "dukascopy",
        asset_class,
        requested_symbol,
        canonical_path.name,
    )
    canonical_fingerprint = file_fingerprint(canonical_path)
    if alias_path.exists() and canonical_fingerprint is not None:
        if file_fingerprint(alias_path) == canonical_fingerprint:
            return alias_path
        if files_have_same_content(canonical_path, alias_path):
            return alias_path
    alias_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(alias_path, lambda target: shutil.copy2(canonical_path, target))
    return alias_path


def download_symbol(
    *,
    requested_symbol: str,
    canonical_symbol: str,
    timeframes: list[str],
    output_dir: Path,
    start: str,
    end: str | None,
    price_type: str,
    batch_size: int,
    pipeline_batches: int,
    pause_ms: int,
    force: bool,
) -> dict[str, Any]:
    downloader = DukascopyNativeDownloader()
    asset_class = symbol_asset_class(canonical_symbol)
    canonical_dir = build_dataset_path(
        output_dir, "ohlcv", "dukascopy", asset_class, canonical_symbol
    )
    requested_dir = build_dataset_path(
        output_dir, "ohlcv", "dukascopy", asset_class, requested_symbol
    )
    source_path = build_dataset_path(canonical_dir, "1m.parquet")
    requested_timeframes = sorted(set(timeframes), key=_timeframe_sort_key)
    known_timeframes = [tf for tf in requested_timeframes if tf in SUPPORTED_TIMEFRAMES]
    higher_timeframes = [tf for tf in known_timeframes if tf != "1m"]
    timeframe_errors: dict[str, str] = {
        tf: f"unsupported timeframe {tf!r}. Valid: {sorted(SUPPORTED_TIMEFRAMES)}"
        for tf in requested_timeframes
        if tf not in SUPPORTED_TIMEFRAMES
    }

    started_at = datetime.now(timezone.utc).isoformat()
    result: dict[str, Any] = {
        "symbol": requested_symbol,
        "canonical_symbol": canonical_symbol,
        "asset_class": asset_class,
        "requested_timeframes": requested_timeframes,
        "source": "dukascopy",
        "status": "pending",
        "started_at": started_at,
        "paths": {},
    }

    try:
        if "1m" in known_timeframes:
            if not source_path.exists() or force:
                dl_result = downloader.download(
                    canonical_symbol,
                    "1m",
                    output_dir,
                    start=start,
                    end=end,
                    offer_side=price_type,
                    batch_size=batch_size,
                    pipeline_batches=pipeline_batches,
                    pause_ms=pause_ms,
                )
                result["download"] = dl_result.to_dict() | {"path": str(dl_result.path)}
            else:
                rows = parquet_rows(source_path)
                result["download"] = {
                    "symbol": canonical_symbol,
                    "timeframe": "1m",
                    "rows": rows,
                    "path": str(source_path),
                    "status": "cached",
                }

            normalize_ohlcv_columns(source_path)
            requested_1m = ensure_alias_copy(source_path, requested_symbol, asset_class, output_dir)
            result["paths"]["1m"] = str(requested_1m)
            result["paths"]["rows_1m"] = parquet_rows(requested_1m)

        for tf in higher_timeframes:
            target_path = build_dataset_path(requested_dir, f"{tf}.parquet")
            if (
                target_path.exists()
                and not force
                and parquet_non_null_ohlcv(target_path) == parquet_rows(target_path)
            ):
                result["paths"][tf] = str(target_path)
                result["paths"][f"rows_{tf}"] = parquet_rows(target_path)
                continue
            resampled_paths = resample_file(source_path, [tf], output_dir=requested_dir)
            target_path = resampled_paths[0]
            result["paths"][tf] = str(target_path)
            result["paths"][f"rows_{tf}"] = parquet_rows(target_path)

        if timeframe_errors:
            result["timeframe_errors"] = timeframe_errors
            result["status"] = "failed" if not result["paths"] else "partial"
        else:
            result["status"] = "ok"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        if timeframe_errors:
            result["timeframe_errors"] = timeframe_errors

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Dukascopy data for the backtest grid")
    parser.add_argument(
        "--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated requested symbols"
    )
    parser.add_argument(
        "--timeframes",
        default=",".join(DEFAULT_TIMEFRAMES),
        help="Comma-separated timeframes",
    )
    parser.add_argument("--start", default=DEFAULT_START, help="Start date, default 2020-01-01")
    parser.add_argument("--end", default="", help="End date, empty = today")
    parser.add_argument("--output", type=Path, default=DATA_DIR, help="Data root directory")
    parser.add_argument("--workers", type=int, default=6, help="Parallel symbols")
    parser.add_argument(
        "--batch-size", type=int, default=20, help="Dukascopy URL batch size per symbol"
    )
    parser.add_argument(
        "--pipeline-batches",
        type=int,
        default=2,
        help="Dukascopy batches kept in flight per symbol",
    )
    parser.add_argument(
        "--pause-ms", type=int, default=500, help="Pause between Dukascopy batches"
    )
    parser.add_argument("--price-type", choices=["bid", "ask", "both"], default="bid")
    parser.add_argument("--oil-type", choices=sorted(OIL_ALIASES), default="USOIL")
    parser.add_argument("--force", action="store_true", help="Redownload 1m even if cached")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print planned downloads without fetching"
    )

    args = parser.parse_args()

    output_dir = args.output.expanduser().resolve()
    requested_symbols = parse_csv_list(args.symbols)
    symbols = [normalize_symbol(symbol, oil_alias=args.oil_type) for symbol in requested_symbols]
    timeframes = [normalize_timeframe_value(tf) for tf in parse_csv_list(args.timeframes)]
    end = args.end or None
    workers = max(1, args.workers)

    planned = [
        {
            "requested_symbol": requested,
            "canonical_symbol": normalize_symbol(requested, oil_alias=args.oil_type),
            "timeframes": timeframes,
        }
        for requested in requested_symbols
    ]
    if args.dry_run:
        print(
            json.dumps(
                {
                    "planned": planned,
                    "output_dir": str(output_dir),
                    "start": args.start,
                    "end": end,
                },
                indent=2,
            )
        )
        return

    rows: list[dict[str, Any]] = []
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    download_symbol,
                    requested_symbol=item["requested_symbol"],
                    canonical_symbol=item["canonical_symbol"],
                    timeframes=item["timeframes"],
                    output_dir=output_dir,
                    start=args.start,
                    end=end,
                    price_type=args.price_type,
                    batch_size=args.batch_size,
                    pipeline_batches=args.pipeline_batches,
                    pause_ms=args.pause_ms,
                    force=args.force,
                )
                for item in planned
            ]
            for future in as_completed(futures):
                rows.append(future.result())
    else:
        rows = [
            download_symbol(
                requested_symbol=item["requested_symbol"],
                canonical_symbol=item["canonical_symbol"],
                timeframes=item["timeframes"],
                output_dir=output_dir,
                start=args.start,
                end=end,
                price_type=args.price_type,
                batch_size=args.batch_size,
                pipeline_batches=args.pipeline_batches,
                pause_ms=args.pause_ms,
                force=args.force,
            )
            for item in planned
        ]

    rows.sort(key=lambda row: row.get("symbol", ""))
    summary = {
        "started_at": min((row.get("started_at", "") for row in rows), default=""),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "requested_symbols": symbols,
        "timeframes": timeframes,
        "start": args.start,
        "end": end,
        "output_dir": str(output_dir),
        "workers": workers,
        "pipeline_batches": args.pipeline_batches,
        "ok": sum(1 for row in rows if row.get("status") == "ok"),
        "failed": sum(1 for row in rows if row.get("status") == "failed"),
        "results": rows,
    }
    log_dir = Path.cwd() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / "grid_market_data_download_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
