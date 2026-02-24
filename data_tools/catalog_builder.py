"""
Scan ~/data/ and produce catalog.json — a machine-readable manifest of all
available datasets.

Usage:
    python -m data_tools.catalog_builder              # print to stdout
    python -m data_tools.catalog_builder --output ~/data/catalog.json
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


def scan_ohlcv(data_dir: Path) -> list[dict]:
    """Scan ohlcv/ subdirectory and return dataset entries."""
    entries = []
    ohlcv_root = data_dir / "ohlcv"
    if not ohlcv_root.exists():
        return entries

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


def build_catalog(data_dir: Path | None = None) -> dict:
    """Build the full catalog dict."""
    root = data_dir or DATA_DIR

    ohlcv = scan_ohlcv(root)

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
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    catalog = build_catalog(data_dir)

    out = json.dumps(catalog, indent=2, default=str)

    if args.output:
        Path(args.output).write_text(out)
        logger.info("Catalog written to %s (%d datasets)", args.output, len(catalog["ohlcv"]))
    else:
        print(out)


if __name__ == "__main__":
    main()
