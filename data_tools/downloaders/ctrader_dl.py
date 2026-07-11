"""
cTrader Downloader — tick/minute data from cTrader via cTrader Open API.

Ported from: github.com/itsjustFred/ML_tick_data_prepare

Downloads tick data or time-bars from cTrader-compatible brokers and converts
to Parquet. Supports configurable timeframes via resampling.

This is a simplified port — the original repo also had feature engineering
(now handled by trading_ml) and gap analysis (now in validate.py).

Usage:
    python -m data_tools.downloaders.ctrader_dl --symbol EURUSD --timeframe 1h \\
        --start 2024-01-01 --end 2025-12-31 --output ~/data

    python -m data_tools.downloaders.ctrader_dl --list
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data_tools.downloaders.base import Downloader, DownloadResult, SourceMeta
from shared.constants import DATA_DIR, normalize_timeframe

logger = logging.getLogger(__name__)

# Pairs typically available via cTrader
AVAILABLE_PAIRS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
    "XAUUSD",
    "XAGUSD",
    "US500",
    "US100",
    "US30",
    "GER40",
    "UK100",
]

RESAMPLE_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
}


class CTraderDownloader(Downloader):
    """Downloads data from cTrader Open API or CSV exports.

    Since cTrader Open API requires authentication and a running cTrader instance,
    this downloader also supports importing CSV exports from cTrader's built-in
    data export feature.
    """

    def __init__(self, csv_dir: str | Path | None = None):
        """
        Args:
            csv_dir: Directory containing cTrader CSV exports. If provided,
                     reads from local files instead of API.
        """
        self.csv_dir = Path(csv_dir) if csv_dir else None

    @property
    def source_name(self) -> str:
        return "ctrader"

    def list_available(self, **kwargs) -> list[dict]:
        if self.csv_dir and self.csv_dir.exists():
            # List locally available CSVs
            items = []
            for csv_file in self.csv_dir.glob("*.csv"):
                parts = csv_file.stem.split("_")
                if len(parts) >= 2:
                    items.append(
                        {
                            "symbol": parts[0],
                            "timeframe": "tick" if "tick" in csv_file.stem.lower() else "1m",
                            "asset_class": "forex",
                            "file": str(csv_file),
                        }
                    )
            return items

        return [
            {"symbol": pair, "timeframe": "tick", "asset_class": "forex"}
            for pair in AVAILABLE_PAIRS
        ]

    def download(
        self,
        symbol: str,
        timeframe: str,
        output_dir: Path,
        *,
        start: str | None = None,
        end: str | None = None,
        csv_file: str | None = None,
        **kwargs,
    ) -> DownloadResult:
        symbol = symbol.upper()
        timeframe = normalize_timeframe(timeframe)

        if timeframe not in RESAMPLE_MAP and timeframe != "tick":
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        # Load from CSV export
        if csv_file:
            df = self._load_csv(Path(csv_file))
        elif self.csv_dir:
            # Find matching CSV
            matches = list(self.csv_dir.glob(f"{symbol}*.csv"))
            if not matches:
                raise FileNotFoundError(f"No CSV found for {symbol} in {self.csv_dir}")
            df = self._load_csv(matches[0])
        else:
            raise RuntimeError(
                "cTrader API download not implemented. "
                "Provide --csv-dir or --csv-file for CSV import mode."
            )

        if df.empty:
            raise RuntimeError(f"No data loaded for {symbol}")

        # Determine asset class
        asset_class = "forex"
        if symbol in ("US500", "US100", "US30", "GER40", "UK100"):
            asset_class = "index"
        out_path = self._make_output_path(output_dir, asset_class, symbol, timeframe)
        effective_start = self._resolve_resume_start(out_path, start)

        # Filter dates
        if "timestamp" in df.columns:
            if effective_start:
                df = df[df["timestamp"] >= effective_start]
            if end:
                df = df[df["timestamp"] <= end]

        # Resample if needed
        if timeframe != "tick" and timeframe != "1m":
            df = self._resample(df, timeframe)

        combined = self._merge_and_store_single_file(out_path, df)

        return DownloadResult(
            symbol=symbol,
            timeframe=timeframe,
            source=self.source_name,
            asset_class=asset_class,
            path=out_path,
            rows=len(combined),
            start=str(combined["timestamp"].iloc[0]) if "timestamp" in combined.columns and len(combined) > 0 else "",
            end=str(combined["timestamp"].iloc[-1]) if "timestamp" in combined.columns and len(combined) > 0 else "",
            size_bytes=out_path.stat().st_size,
        )

    def _load_csv(self, path: Path) -> pd.DataFrame:
        """Load cTrader CSV export.

        cTrader exports have various formats — try common ones.
        """
        logger.info("Loading cTrader CSV: %s", path)

        # Try common separators and formats
        for sep in [",", ";", "\t"]:
            try:
                df = pd.read_csv(path, sep=sep)
                if len(df.columns) >= 5:
                    break
            except Exception:
                continue
        else:
            raise ValueError(f"Could not parse {path}")

        # Normalize column names
        df.columns = [c.strip().lower() for c in df.columns]

        # Try to find timestamp column
        ts_col = None
        for candidate in ["timestamp", "datetime", "date", "time", "date/time"]:
            if candidate in df.columns:
                ts_col = candidate
                break

        if ts_col is None and len(df.columns) >= 6:
            # Assume first column is timestamp
            ts_col = df.columns[0]

        if ts_col:
            df = df.rename(columns={ts_col: "timestamp"})
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        # Map OHLCV columns
        for old, new in [
            ("o", "open"),
            ("h", "high"),
            ("l", "low"),
            ("c", "close"),
            ("v", "volume"),
        ]:
            if old in df.columns and new not in df.columns:
                df = df.rename(columns={old: new})

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        keep = [
            c for c in ["timestamp", "open", "high", "low", "close", "volume"] if c in df.columns
        ]
        return df[keep].dropna(subset=["open"]).sort_values("timestamp").reset_index(drop=True)

    def _resample(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        rule = RESAMPLE_MAP[timeframe]
        df = df.set_index("timestamp")
        resampled = (
            df.resample(rule)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna(subset=["open"])
            .reset_index()
        )
        return resampled

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url="https://ctrader.com",
            license="free",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Import cTrader CSV data")
    parser.add_argument("--symbol", help="Symbol to import")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--csv-dir", help="Directory with cTrader CSV exports")
    parser.add_argument("--csv-file", help="Single CSV file to import")
    parser.add_argument("--start", help="Start date")
    parser.add_argument("--end", help="End date")
    parser.add_argument("--output", default=str(DATA_DIR))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    dl = CTraderDownloader(csv_dir=args.csv_dir)

    if args.list:
        for item in dl.list_available():
            print(f"  {item['symbol']:10s}  {item.get('file', item['timeframe'])}")
        return

    if not args.symbol:
        parser.error("--symbol required")

    result = dl.download(
        args.symbol,
        args.timeframe,
        Path(args.output),
        csv_file=args.csv_file,
        start=args.start,
        end=args.end,
    )
    print(f"✅ {result.symbol}: {result.rows} rows → {result.path}")


if __name__ == "__main__":
    main()
