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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("TRADING_DATA_DIR", Path.home() / "data"))

# Path to persisted source metadata (written by downloaders)
SOURCE_META_FILE = "source_meta.json"


def _load_source_meta(data_dir: Path) -> dict[str, dict]:
    """Load source metadata index if it exists.

    The file maps dataset keys (e.g. 'dukascopy/forex/XAUUSD/1h') to metadata
    dicts with fields: url, downloaded_at, license, update_frequency.
    """
    meta_path = data_dir / SOURCE_META_FILE
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except Exception as exc:
            logger.warning("Could not read %s: %s", meta_path, exc)
    return {}


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
                w for w in [
                    f"null_values: {result.null_counts}" if any(v > 0 for v in result.null_counts.values()) else None,
                    f"ohlc_violations: {result.ohlc_violations}" if result.ohlc_violations > 0 else None,
                    f"zero_volume: {result.zero_volume_pct:.1f}%" if result.zero_volume_pct > 5.0 else None,
                ] if w is not None
            ],
        }
    except Exception as exc:
        logger.debug("Validation skipped for %s: %s", parquet_path, exc)
        return None


def scan_ohlcv(data_dir: Path, include_validation: bool = False) -> list[dict]:
    """Scan ohlcv/ subdirectory and return dataset entries."""
    entries = []
    ohlcv_root = data_dir / "ohlcv"
    if not ohlcv_root.exists():
        return entries

    source_meta_index = _load_source_meta(data_dir)

    for parquet_file in sorted(ohlcv_root.rglob("*.parquet")):
        rel = parquet_file.relative_to(ohlcv_root)
        parts = rel.parts
        # Structure: {source}/{asset_class}/[{region}/]{symbol}/{tf}.parquet
        # Symbol is always the parent directory of the parquet file
        if len(parts) < 4:
            logger.warning("Unexpected path depth: %s", rel)
            continue

        source = parts[0]
        asset_class = parts[1]
        symbol = parquet_file.parent.name
        timeframe = parquet_file.stem

        entry: dict = {
            "source": source,
            "asset_class": asset_class,
            "symbol": symbol,
            "timeframe": timeframe,
            "path": str(parquet_file),
            "format": "parquet",
            "storage": _detect_storage(parquet_file, data_dir),
        }

        try:
            df = pd.read_parquet(parquet_file)
            entry["rows"] = len(df)
            entry["size_bytes"] = parquet_file.stat().st_size

            if "timestamp" in df.columns:
                ts = pd.to_datetime(df["timestamp"])
                entry["start"] = str(ts.min())
                entry["end"] = str(ts.max())

            entry["columns"] = list(df.columns)
        except Exception as e:
            logger.warning("Could not read %s: %s", parquet_file, e)
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
            entries.append({
                "name": f.stem,
                "path": str(f),
                "format": f.suffix.lstrip("."),
                "size_bytes": f.stat().st_size,
            })
    return entries


def build_catalog(data_dir: Path | None = None, include_validation: bool = False) -> dict:
    """Build the full catalog dict."""
    root = data_dir or DATA_DIR

    ohlcv = scan_ohlcv(root, include_validation=include_validation)

    # Build summary
    symbols = sorted(set(e["symbol"] for e in ohlcv))
    sources = sorted(set(e["source"] for e in ohlcv))
    timeframes = sorted(set(e["timeframe"] for e in ohlcv))

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(root),
        "summary": {
            "total_datasets": len(ohlcv),
            "symbols": len(symbols),
            "sources": sources,
            "timeframes": timeframes,
            "total_size_bytes": sum(e.get("size_bytes", 0) for e in ohlcv),
        },
        "ohlcv": ohlcv,
        "universes": scan_universes(root),
    }
    return catalog


def main():
    parser = argparse.ArgumentParser(description="Build data catalog for ~/data/")
    parser.add_argument("--output", "-o", type=str, help="Write catalog to file (default: stdout)")
    parser.add_argument("--data-dir", type=str, help="Override data directory")
    parser.add_argument("--validate", action="store_true", help="Include validation results (slower)")
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
