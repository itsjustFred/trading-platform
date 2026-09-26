"""
Migrate existing CSV data to the central ~/data/ directory as Parquet.

Sources:
  1. custom_lean_integration/data/ — XAUUSD (Dukascopy), US equities H1/M15/daily
  2. stockkit_ai_bot/data/*.csv    — 76 US equity 5-min bars (yfinance)
  3. custom_lean_integration/universe_sp500.txt → ~/data/universes/

Usage:
    python -m data_tools.migrate_csv              # dry-run (default)
    python -m data_tools.migrate_csv --execute    # actually migrate
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data_tools.path_safety import build_dataset_path, ensure_dataset_path_within
from shared.atomic_io import write_parquet_atomically, write_text_atomically
from shared.constants import DATA_DIR

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

LEAN_DATA = Path.home() / "Desktop" / "custom_lean_integration" / "data"
STOCKKIT_DATA = Path.home() / "Desktop" / "stockkit_ai_bot" / "data"
LEAN_UNIVERSE = Path.home() / "Desktop" / "custom_lean_integration" / "universe_sp500.txt"


def migrate_dukascopy_xauusd(dry_run: bool, force: bool = False) -> int:
    """Migrate XAUUSD H1 and M15 CSVs from lean integration."""
    count = 0
    patterns = {
        "XAUUSD_H1_*.csv": "1h",
        "XAUUSD_M15_*.csv": "15m",
    }
    for pattern, tf in patterns.items():
        for csv_path in LEAN_DATA.glob(pattern):
            dest = build_dataset_path(
                DATA_DIR, "ohlcv", "dukascopy", "forex", "XAUUSD", f"{tf}.parquet"
            )
            count += _convert_ohlcv(csv_path, dest, dry_run, force=force, timestamp_col="timestamp")
    return count


def migrate_lean_equities_h1(dry_run: bool, force: bool = False) -> int:
    """Migrate US equity H1 CSVs (730d files)."""
    count = 0
    for csv_path in LEAN_DATA.glob("*_H1_730d.csv"):
        symbol = csv_path.name.split("_")[0]
        dest = build_dataset_path(
            DATA_DIR, "ohlcv", "yfinance", "equity", "US", symbol, "1h.parquet"
        )
        count += _convert_ohlcv(csv_path, dest, dry_run, force=force, timestamp_col="timestamp")
    return count


def migrate_lean_equities_m15(dry_run: bool, force: bool = False) -> int:
    """Migrate US equity M15 CSVs."""
    count = 0
    for csv_path in LEAN_DATA.glob("*_M15_*.csv"):
        name = csv_path.name
        if name.startswith("XAUUSD"):
            continue  # handled by dukascopy migration
        symbol = name.split("_")[0]
        dest = build_dataset_path(
            DATA_DIR, "ohlcv", "yfinance", "equity", "US", symbol, "15m.parquet"
        )
        count += _convert_ohlcv(csv_path, dest, dry_run, force=force, timestamp_col="timestamp")
    return count


def migrate_lean_equities_daily(dry_run: bool, force: bool = False) -> int:
    """Migrate daily equity CSVs from lean integration/data/equity/."""
    count = 0
    equity_dir = LEAN_DATA / "equity"
    if not equity_dir.exists():
        return 0
    for csv_path in equity_dir.glob("*_daily.csv"):
        symbol = csv_path.name.replace("_daily.csv", "")
        dest = build_dataset_path(
            DATA_DIR, "ohlcv", "yfinance", "equity", "US", symbol, "1d.parquet"
        )
        count += _convert_ohlcv(csv_path, dest, dry_run, force=force)
    return count


def migrate_stockkit_5m(dry_run: bool, force: bool = False) -> int:
    """Migrate stockkit 5-min equity CSVs."""
    count = 0
    for csv_path in STOCKKIT_DATA.glob("*.csv"):
        # Skip non-OHLCV files
        if csv_path.name.startswith("bot_state") or csv_path.parent.name != "data":
            continue
        symbol = csv_path.stem
        if not re.match(r"^[A-Z]{1,5}$", symbol):
            continue
        dest = build_dataset_path(
            DATA_DIR, "ohlcv", "yfinance", "equity", "US", symbol, "5m.parquet"
        )
        count += _convert_ohlcv(csv_path, dest, dry_run, force=force, datetime_col="Datetime")
    return count


def migrate_universe(dry_run: bool, force: bool = False) -> int:
    """Copy SP500 universe file."""
    if not LEAN_UNIVERSE.exists():
        return 0
    dest = build_dataset_path(DATA_DIR, "universes", "sp500_changes.txt")
    if dest.exists() and not force:
        logger.info("[SKIP] %s (already exists)", dest)
        return 0
    if dry_run:
        logger.info("[DRY] %s → %s", LEAN_UNIVERSE, dest)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    if dest.exists() and force:
        backup = dest.with_name(
            f"{dest.stem}.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}{dest.suffix}"
        )
        shutil.copy2(dest, backup)
        logger.info("Backed up %s → %s", dest, backup)
    shutil.copy2(LEAN_UNIVERSE, dest)
    logger.info("Copied %s → %s", LEAN_UNIVERSE, dest)
    return 1


def _looks_like_timestamp(val: str) -> bool:
    """Check if a string looks like a timestamp (starts with digit, contains '-')."""
    return len(val) > 8 and val[0].isdigit() and "-" in val


def _source_fingerprint(csv_path: Path) -> str:
    """Short SHA256 + row estimate identifying the migration source (B3)."""
    import hashlib

    digest = hashlib.sha256()
    rows = 0
    try:
        with csv_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        with csv_path.open("r", encoding="utf-8", errors="replace") as handle:
            rows = sum(1 for _ in handle) - 1
    except OSError:
        return "unreadable"
    return f"sha256:{digest.hexdigest()[:16]} rows~{max(rows, 0)}"


def _migration_metadata_path(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.migration.json")


def _read_migration_metadata(dest: Path) -> dict:
    metadata_path = _migration_metadata_path(dest)
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_migration_metadata(
    dest: Path,
    *,
    csv_path: Path,
    source_fingerprint: str,
    rows: int,
) -> None:
    write_text_atomically(
        _migration_metadata_path(dest),
        json.dumps(
            {
                "source": str(csv_path),
                "source_fingerprint": source_fingerprint,
                "rows": rows,
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        ),
    )


def _convert_ohlcv(
    csv_path: Path,
    dest: Path,
    dry_run: bool,
    timestamp_col: str | None = None,
    datetime_col: str | None = None,
    force: bool = False,
) -> int:
    """Convert a single OHLCV CSV to Parquet."""
    dest = ensure_dataset_path_within(dest.parent, dest.name)
    if dest.exists() and not force:
        metadata = _read_migration_metadata(dest)
        current_fingerprint = _source_fingerprint(csv_path)
        recorded_fingerprint = str(metadata.get("source_fingerprint") or "")
        if not recorded_fingerprint:
            raise RuntimeError(
                f"Migration artifact {dest} has no source fingerprint; refusing "
                "to skip an unverifiable legacy artifact. Re-run with --force "
                "to replace it after reviewing the source."
            )
        if recorded_fingerprint != current_fingerprint:
            raise RuntimeError(
                f"Migration artifact {dest} was created from a different source "
                "fingerprint; refusing to skip stale data. Re-run with --force "
                "to replace it after reviewing the source change."
            )
        logger.info("[SKIP] %s (already exists)", dest)
        return 0

    if dry_run:
        logger.info("[DRY] %s → %s", csv_path.name, dest)
        return 1

    try:
        df = pd.read_csv(csv_path)

        # Normalize column names to lowercase
        df.columns = [c.lower().strip() for c in df.columns]

        # Handle yfinance multi-header format: first col "price" is actually datetime,
        # rows contain "Ticker"/symbol name and "Datetime" label as metadata
        if "price" in df.columns and len(df) > 2:
            first_val = str(df.iloc[0].get("price", ""))
            if first_val in ("Ticker", "Datetime") or first_val.isupper():
                # Skip metadata rows (Ticker row + Datetime row)
                skip = 0
                for r in range(min(3, len(df))):
                    val = str(df.iloc[r].get("price", ""))
                    if not _looks_like_timestamp(val):
                        skip = r + 1
                    else:
                        break
                df = df.iloc[skip:].reset_index(drop=True)
            df = df.rename(columns={"price": "timestamp"})

        # Determine timestamp column
        ts_col = None
        if timestamp_col and timestamp_col.lower() in df.columns:
            ts_col = timestamp_col.lower()
        elif datetime_col and datetime_col.lower() in df.columns:
            ts_col = datetime_col.lower()
        elif "datetime" in df.columns:
            ts_col = "datetime"
        elif "timestamp" in df.columns:
            ts_col = "timestamp"
        elif "date" in df.columns:
            ts_col = "date"

        # Convert OHLCV columns to numeric, drop rows that can't be converted
        for col in ("open", "high", "low", "close", "volume"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])

        if ts_col:
            df[ts_col] = pd.to_datetime(df[ts_col], utc=True, errors="coerce")
            df = df.rename(columns={ts_col: "timestamp"})

        # Ensure standard OHLCV columns exist
        for col in ("open", "high", "low", "close"):
            if col not in df.columns:
                logger.warning("Missing column '%s' in %s", col, csv_path)
                return 0

        # Select and order columns
        keep = ["timestamp"] if "timestamp" in df.columns else []
        keep += ["open", "high", "low", "close"]
        if "volume" in df.columns:
            keep.append("volume")
        df = df[keep]

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and force:
            # B3: --force overwrites back up the previous artifact with a
            # timestamped backup instead of destroying it.
            backup = dest.with_name(
                f"{dest.stem}.bak.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}{dest.suffix}"
            )
            import shutil

            shutil.copy2(dest, backup)
            logger.info("Backed up %s → %s", dest, backup)
        source_fingerprint = _source_fingerprint(csv_path)
        write_parquet_atomically(dest, df)
        _write_migration_metadata(
            dest,
            csv_path=csv_path,
            source_fingerprint=source_fingerprint,
            rows=len(df),
        )
        logger.info(
            "Converted %s → %s (%d rows, source %s)",
            csv_path.name,
            dest,
            len(df),
            source_fingerprint,
        )
        return 1

    except Exception as e:
        logger.error("Failed to convert %s: %s", csv_path, e)
        return 0


def main():
    parser = argparse.ArgumentParser(description="Migrate CSV data to ~/data/ as Parquet")
    parser.add_argument(
        "--execute", action="store_true", help="Actually perform the migration (default: dry-run)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-migrate existing destinations (backs the previous artifact up as *.bak.<ts>)",
    )
    args = parser.parse_args()

    dry_run = not args.execute
    if dry_run:
        logger.info("=== DRY RUN (use --execute to actually migrate) ===\n")

    total = 0
    total += migrate_dukascopy_xauusd(dry_run, force=args.force)
    total += migrate_lean_equities_h1(dry_run, force=args.force)
    total += migrate_lean_equities_m15(dry_run, force=args.force)
    total += migrate_lean_equities_daily(dry_run, force=args.force)
    total += migrate_stockkit_5m(dry_run, force=args.force)
    total += migrate_universe(dry_run, force=args.force)

    action = "Would migrate" if dry_run else "Migrated"
    logger.info("\n%s %d files total.", action, total)


if __name__ == "__main__":
    main()
