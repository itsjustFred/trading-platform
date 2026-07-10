"""
Histdata Downloader — free Forex minute/tick data from histdata.com.

Downloads CSV archives, converts to Parquet, stores under
~/data/ohlcv/histdata/forex/{SYMBOL}/{timeframe}.parquet

Available pairs (subset): EURUSD, GBPUSD, USDJPY, USDCHF, AUDUSD,
    NZDUSD, USDCAD, EURGBP, EURJPY, GBPJPY, XAUUSD, XAGUSD

Data is delivered as 1-minute bars (M1). We can resample to higher timeframes.

Usage:
    python -m data_tools.downloaders.histdata_dl --symbol EURUSD --years 2020-2025
    python -m data_tools.downloaders.histdata_dl --list
"""

from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from data_tools.downloaders.base import Downloader, DownloadResult, SourceMeta
from shared.constants import DATA_DIR

logger = logging.getLogger(__name__)

# histdata.com serves M1 ASCII data for these pairs (free tier)
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
    "EURAUD",
    "EURCHF",
    "GBPCHF",
    "GBPAUD",
    "AUDCAD",
    "AUDCHF",
    "AUDJPY",
    "AUDNZD",
    "CADCHF",
    "CADJPY",
    "CHFJPY",
    "NZDJPY",
    "NZDCAD",
    "NZDCHF",
    "SGDJPY",
]

# Timeframe resampling map
RESAMPLE_MAP = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
}

BASE_URL = (
    "https://www.histdata.com/download-free-forex-historical-data/?/ascii/1-minute-bar-quotes"
)
DOWNLOAD_URL = "https://www.histdata.com/get.php"


class HistdataDownloader(Downloader):
    """Downloads free Forex minute data from histdata.com."""

    @property
    def source_name(self) -> str:
        return "histdata"

    def list_available(self, **kwargs) -> list[dict]:
        return [
            {"symbol": pair, "timeframe": "1m", "asset_class": "forex"} for pair in AVAILABLE_PAIRS
        ]

    def download(
        self,
        symbol: str,
        timeframe: str,
        output_dir: Path,
        *,
        start: str | None = None,
        end: str | None = None,
        years: list[int] | None = None,
        **kwargs,
    ) -> DownloadResult:
        symbol = symbol.upper()
        if symbol not in AVAILABLE_PAIRS:
            raise ValueError(f"{symbol} not available. Use: {AVAILABLE_PAIRS}")

        if timeframe not in RESAMPLE_MAP:
            raise ValueError(f"Unsupported timeframe {timeframe}. Use: {list(RESAMPLE_MAP.keys())}")

        out_path = self._make_output_path(output_dir, "forex", symbol, timeframe)
        effective_start = self._resolve_resume_start(out_path, start)

        # Determine years to download
        if years is None:
            start_year = int(effective_start[:4]) if effective_start else 2020
            end_year = int(end[:4]) if end else datetime.now().year
            years = list(range(start_year, end_year + 1))

        logger.info("Downloading %s from histdata.com for years %s", symbol, years)

        all_dfs = []
        for year in years:
            df = self._download_year(symbol, year)
            if df is not None and not df.empty:
                all_dfs.append(df)
                logger.info("  %s %d: %d bars", symbol, year, len(df))

        if not all_dfs:
            raise RuntimeError(f"No data downloaded for {symbol}")

        combined = (
            pd.concat(all_dfs)
            .sort_values("timestamp")
            .drop_duplicates("timestamp")
            .reset_index(drop=True)
        )

        # Apply date filters
        if effective_start:
            combined = combined[combined["timestamp"] >= effective_start]
        if end:
            combined = combined[combined["timestamp"] <= end]

        # Resample if not 1m
        if timeframe != "1m":
            combined = self._resample(combined, timeframe)

        if combined.empty:
            raise RuntimeError(f"No data for {symbol} after applying filters")

        combined = self._merge_and_store_single_file(out_path, combined)

        return DownloadResult(
            symbol=symbol,
            timeframe=timeframe,
            source=self.source_name,
            asset_class="forex",
            path=out_path,
            rows=len(combined),
            start=str(combined["timestamp"].iloc[0]),
            end=str(combined["timestamp"].iloc[-1]),
            size_bytes=out_path.stat().st_size,
        )

    def _download_year(self, symbol: str, year: int) -> pd.DataFrame | None:
        """Download one year of M1 data from histdata.com.

        histdata.com requires a POST with specific form data + referer.
        The response is a ZIP containing a CSV.
        """
        try:
            session = requests.Session()
            # Step 1: GET the page to obtain cookies/tokens
            page_url = f"{BASE_URL}/{symbol.lower()}/{year}"
            resp = session.get(page_url, timeout=30)
            if resp.status_code != 200:
                logger.warning(
                    "Could not access page for %s/%d (status %d)", symbol, year, resp.status_code
                )
                return None

            # Extract form token (tk value) from page
            import re

            tk_match = re.search(r'id="tk"\s+value="([^"]+)"', resp.text)
            if not tk_match:
                logger.warning("No download token found for %s/%d", symbol, year)
                return None

            tk = tk_match.group(1)

            # Step 2: POST to download
            data = {
                "tk": tk,
                "date": str(year),
                "datemonth": f"{year}01",
                "platform": "ASCII",
                "timeframe": "M1",
            }
            headers = {"Referer": page_url}
            resp = session.post(DOWNLOAD_URL, data=data, headers=headers, timeout=60)

            if resp.status_code != 200 or len(resp.content) < 100:
                logger.warning("Download failed for %s/%d", symbol, year)
                return None

            # Step 3: Unzip and parse CSV
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                csv_name = zf.namelist()[0]
                with zf.open(csv_name) as f:
                    # histdata M1 format: YYYYMMDD HHMMSS;OPEN;HIGH;LOW;CLOSE;VOLUME
                    df = pd.read_csv(
                        f,
                        sep=";",
                        header=None,
                        names=["datetime", "open", "high", "low", "close", "volume"],
                    )

            df["timestamp"] = pd.to_datetime(
                df["datetime"].str.strip(), format="%Y%m%d %H%M%S", utc=True
            )
            df = df[["timestamp", "open", "high", "low", "close", "volume"]]
            return df

        except Exception as e:
            logger.warning("Error downloading %s/%d: %s", symbol, year, e)
            return None

    def _resample(self, df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        """Resample 1m data to a higher timeframe."""
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
            url="https://www.histdata.com",
            license="free",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Download Forex data from histdata.com")
    parser.add_argument("--symbol", help="Symbol to download (e.g. EURUSD)")
    parser.add_argument("--timeframe", default="1h", help="Target timeframe (default: 1h)")
    parser.add_argument("--years", help="Year range, e.g. 2020-2025")
    parser.add_argument("--output", default=str(DATA_DIR), help="Output directory")
    parser.add_argument("--list", action="store_true", help="List available pairs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    dl = HistdataDownloader()

    if args.list:
        for item in dl.list_available():
            print(f"  {item['symbol']:10s}  {item['asset_class']}")
        return

    if not args.symbol:
        parser.error("--symbol required (or use --list)")

    years = None
    if args.years:
        parts = args.years.split("-")
        years = list(range(int(parts[0]), int(parts[1]) + 1))

    result = dl.download(args.symbol, args.timeframe, Path(args.output), years=years)
    print(f"✅ {result.symbol} {result.timeframe}: {result.rows} bars → {result.path}")


if __name__ == "__main__":
    main()
