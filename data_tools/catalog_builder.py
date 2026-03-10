"""
Scan ~/data/ and produce catalog.json — a machine-readable manifest of all
available datasets.

Supports source_meta, validation status, and storage mode fields (Phase 3).

Usage:
    python -m data_tools.catalog_builder              # print to stdout
    python -m data_tools.catalog_builder --output ~/data/catalog.json
    python -m data_tools.catalog_builder --validate   # include validation results
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

from shared.constants import DATA_DIR

CATALOG_FILE = "catalog.json"

# Path to persisted source metadata (written by downloaders)
SOURCE_META_FILE = "source_meta.json"
TIMEFRAME_PATTERN = re.compile(
    r"^(tick|(?:\d+)?(?:s|sec|m|min|h|d|w|mo)|[smhdw]\d+)$", re.IGNORECASE
)


def _load_source_meta(data_dir: Path) -> dict[str, dict]:
    """Load source metadata index if it exists.

    The file maps dataset keys (e.g. 'dukascopy/forex/XAUUSD/1h') to metadata
    dicts with fields: url, downloaded_at, license, update_frequency.
    """
    meta_path = data_dir / SOURCE_META_FILE
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s: %s", meta_path, exc)
    return {}


def _load_catalog_cache(data_dir: Path) -> dict[str, dict]:
    """Load the previous catalog to reuse unchanged file metadata."""
    cache_path = data_dir / CATALOG_FILE
    if not cache_path.exists():
        return {}

    try:
        payload = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read %s: %s", cache_path, exc)
        return {}

    cached_entries = payload.get("ohlcv", []) if isinstance(payload, dict) else []
    cache: dict[str, dict] = {}
    for entry in cached_entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if path:
            cache[str(path)] = entry
    return cache


def _detect_storage(parquet_path: Path, data_dir: Path) -> str:
    """Determine storage mode: local, cached, or remote."""
    cache_dir = data_dir / "cache"
    if str(parquet_path).startswith(str(cache_dir)):
        return "cached"
    return "local"


def _run_validation(parquet_path: Path, symbol: str, timeframe: str) -> dict | None:
    """Run data validation checks and return summary dict."""
    try:
        from data_tools.validate import validate_parquet

        result = validate_parquet(parquet_path)
        return {
            "last_checked": datetime.now(timezone.utc).isoformat(),
            "status": result.status,
            "gaps": len(result.gaps),
            "duplicates": result.duplicates,
            "warnings": [
                w
                for w in [
                    f"null_values: {result.null_counts}"
                    if any(v > 0 for v in result.null_counts.values())
                    else None,
                    f"ohlc_violations: {result.ohlc_violations}"
                    if result.ohlc_violations > 0
                    else None,
                    f"zero_volume: {result.zero_volume_pct:.1f}%"
                    if result.zero_volume_pct > 5.0
                    else None,
                ]
                if w is not None
            ],
        }
    except (OSError, ValueError, KeyError) as exc:
        logger.debug("Validation skipped for %s: %s", parquet_path, exc)
        return None


def _looks_like_timeframe(value: str) -> bool:
    return bool(TIMEFRAME_PATTERN.match(value.strip()))


def _format_catalog_date(value: object) -> str | None:
    if value is None:
        return None
    try:
        timestamp = pd.to_datetime(value, utc=True)
    except (ValueError, TypeError):
        return None
    if pd.isna(timestamp):
        return None
    return timestamp.date().isoformat()


def parse_ohlcv_path(parquet_path: Path, ohlcv_root: Path) -> dict | None:
    """Parse an OHLCV parquet path into source/asset_class/symbol/timeframe fields."""
    rel = parquet_path.relative_to(ohlcv_root)
    parts = rel.parts
    if len(parts) < 4:
        logger.warning("Unexpected path depth: %s", rel)
        return None

    source = parts[0]
    asset_class = parts[1]
    region: str | None = None

    if len(parts) >= 5:
        region = parts[-3]
        symbol = parts[-2]
        timeframe = parquet_path.stem
    else:
        stem = parquet_path.stem
        if _looks_like_timeframe(stem):
            symbol = parts[-2]
            timeframe = stem
        else:
            region = parts[-2]
            symbol = stem
            timeframe = "1d"

    entry = {
        "source": source,
        "asset_class": asset_class,
        "symbol": symbol,
        "timeframe": timeframe,
        "path": str(parquet_path),
    }
    if region:
        entry["region"] = region
    return entry


def _select_timestamp_column(columns: list[str]) -> str | None:
    lowered = {column.lower(): column for column in columns}
    for candidate in ("timestamp", "datetime", "date", "time"):
        if candidate in lowered:
            return lowered[candidate]
    return None


def _read_row_group_stat(
    metadata: pq.FileMetaData,
    column_index: int,
    row_group_index: int,
    *,
    boundary: str,
) -> object | None:
    stats = metadata.row_group(row_group_index).column(column_index).statistics
    if stats is None or not getattr(stats, "has_min_max", False):
        return None
    return stats.min if boundary == "min" else stats.max


def read_parquet_summary(parquet_path: Path) -> dict:
    """Read parquet metadata and timestamp bounds without loading the full file."""
    stat = parquet_path.stat()
    parquet_file = pq.ParquetFile(parquet_path)
    metadata = parquet_file.metadata
    columns = list(parquet_file.schema_arrow.names)
    summary = {
        "rows": metadata.num_rows,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "columns": columns,
    }

    timestamp_column = _select_timestamp_column(columns)
    if not timestamp_column or metadata.num_rows == 0 or metadata.num_row_groups == 0:
        return summary

    column_index = columns.index(timestamp_column)
    start_value = _read_row_group_stat(metadata, column_index, 0, boundary="min")
    end_value = _read_row_group_stat(
        metadata,
        column_index,
        metadata.num_row_groups - 1,
        boundary="max",
    )

    if start_value is None:
        first_group = parquet_file.read_row_group(0, columns=[timestamp_column])
        if first_group.num_rows > 0:
            start_value = first_group.column(0)[0].as_py()
    if end_value is None:
        last_group = parquet_file.read_row_group(
            metadata.num_row_groups - 1,
            columns=[timestamp_column],
        )
        if last_group.num_rows > 0:
            end_value = last_group.column(0)[last_group.num_rows - 1].as_py()

    summary["start"] = _format_catalog_date(start_value)
    summary["end"] = _format_catalog_date(end_value)
    return summary


def scan_ohlcv(data_dir: Path, include_validation: bool = False) -> list[dict]:
    """Scan ohlcv/ subdirectory and return dataset entries."""
    entries = []
    ohlcv_root = data_dir / "ohlcv"
    if not ohlcv_root.exists():
        return entries

    source_meta_index = _load_source_meta(data_dir)
    catalog_cache = _load_catalog_cache(data_dir)

    for parquet_file in sorted(ohlcv_root.rglob("*.parquet")):
        entry = parse_ohlcv_path(parquet_file, ohlcv_root)
        if entry is None:
            continue

        source = entry["source"]
        asset_class = entry["asset_class"]
        symbol = entry["symbol"]
        timeframe = entry["timeframe"]
        entry["format"] = "parquet"
        entry["storage"] = _detect_storage(parquet_file, data_dir)

        try:
            stat = parquet_file.stat()
            cached_entry = catalog_cache.get(str(parquet_file))
            if (
                cached_entry
                and cached_entry.get("size_bytes") == stat.st_size
                and cached_entry.get("mtime_ns") == stat.st_mtime_ns
            ):
                for key in ("rows", "size_bytes", "mtime_ns", "columns", "start", "end"):
                    if key in cached_entry:
                        entry[key] = cached_entry[key]
            else:
                entry.update(read_parquet_summary(parquet_file))
        except (OSError, pd.errors.EmptyDataError, ValueError) as e:
            logger.warning("Could not read %s: %s", parquet_file, e)
            try:
                stat = parquet_file.stat()
                entry["size_bytes"] = stat.st_size
                entry["mtime_ns"] = stat.st_mtime_ns
            except OSError:
                entry["size_bytes"] = 0
            entry["rows"] = None
            entry["error"] = str(e)

        # Source metadata (from downloaders)
        meta_key = f"{source}/{asset_class}/{symbol}/{timeframe}"
        if meta_key in source_meta_index:
            entry["source_meta"] = source_meta_index[meta_key]

        # Validation (optional, can be slow for large datasets)
        if include_validation:
            validation = _run_validation(parquet_file, symbol, timeframe)
            if validation:
                entry["validation"] = validation

        entries.append(entry)

    return entries


def scan_universes(data_dir: Path) -> list[dict]:
    """Scan universes/ subdirectory."""
    entries = []
    uni_dir = data_dir / "universes"
    if not uni_dir.exists():
        return entries

    for f in sorted(uni_dir.iterdir()):
        if f.is_file():
            entries.append(
                {
                    "name": f.stem,
                    "path": str(f),
                    "format": f.suffix.lstrip("."),
                    "size_bytes": f.stat().st_size,
                }
            )
    return entries


def scan_fundamentals(data_dir: Path) -> list[dict]:
    """Scan fundamentals/ subdirectory for non-OHLCV datasets."""
    entries = []
    fund_root = data_dir / "fundamentals"
    if not fund_root.exists():
        return entries

    for parquet_file in sorted(fund_root.rglob("*.parquet")):
        rel = parquet_file.relative_to(fund_root)
        parts = rel.parts
        source = parts[0] if len(parts) > 1 else "unknown"
        category = parts[1] if len(parts) > 2 else "general"
        table_name = parquet_file.stem

        entry = {
            "source": source,
            "category": category,
            "table": table_name,
            "path": str(parquet_file),
            "format": "parquet",
            "size_bytes": parquet_file.stat().st_size,
        }
        try:
            df = pd.read_parquet(parquet_file)
            entry["rows"] = len(df)
            entry["columns"] = list(df.columns)
        except (OSError, pd.errors.EmptyDataError, ValueError) as e:
            entry["rows"] = None
            entry["error"] = str(e)

        entries.append(entry)
    return entries


def build_catalog(data_dir: Path | None = None, include_validation: bool = False) -> dict:
    """Build the full catalog dict."""
    root = data_dir or DATA_DIR

    ohlcv = scan_ohlcv(root, include_validation=include_validation)

    # Scan fundamentals
    fundamentals = scan_fundamentals(root)

    # Build summary
    symbols = sorted(set(e["symbol"] for e in ohlcv))
    sources = sorted(set(e["source"] for e in ohlcv))
    timeframes = sorted(set(e["timeframe"] for e in ohlcv))

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(root),
        "summary": {
            "total_datasets": len(ohlcv) + len(fundamentals),
            "symbols": len(symbols),
            "sources": sources,
            "timeframes": timeframes,
            "total_size_bytes": sum(e.get("size_bytes", 0) for e in ohlcv)
            + sum(e.get("size_bytes", 0) for e in fundamentals),
        },
        "ohlcv": ohlcv,
        "fundamentals": fundamentals,
        "universes": scan_universes(root),
    }
    return catalog


def main():
    parser = argparse.ArgumentParser(description="Build data catalog for ~/data/")
    parser.add_argument("--output", "-o", type=str, help="Write catalog to file (default: stdout)")
    parser.add_argument("--data-dir", type=str, help="Override data directory")
    parser.add_argument(
        "--validate", action="store_true", help="Include validation results (slower)"
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    catalog = build_catalog(data_dir, include_validation=args.validate)

    out = json.dumps(catalog, indent=2, default=str)

    if args.output:
        Path(args.output).write_text(out)
        logger.info("Catalog written to %s (%d datasets)", args.output, len(catalog["ohlcv"]))
    else:
        print(out)


if __name__ == "__main__":
    main()
