"""
HuggingFace Downloader — datasets from HuggingFace Hub.

Downloads Parquet datasets via the HuggingFace Datasets API / direct file download.
Many financial datasets are available, often already in Parquet format.

Examples:
  - edarchimbaud/timeseries-1d-sp500 (S&P 500 daily OHLCV)
  - edarchimbaud/timeseries-1h-all-crypto (crypto 1h)
  - alexbrockman/equity-bars (equity tick data)

Usage:
    python -m data_tools.downloaders.huggingface_dl --dataset edarchimbaud/timeseries-1d-sp500 \\
        --symbol AAPL --output ~/data

    python -m data_tools.downloaders.huggingface_dl --list
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from data_tools.downloaders.base import Downloader, DownloadResult, SourceMeta
from shared.constants import DATA_DIR

logger = logging.getLogger(__name__)

HF_API = "https://huggingface.co/api/datasets"

# Well-known financial datasets on HuggingFace
KNOWN_DATASETS = [
    {
        "dataset": "edarchimbaud/timeseries-1d-sp500",
        "description": "S&P 500 daily OHLCV",
        "asset_class": "equity",
        "timeframe": "1d",
    },
    {
        "dataset": "edarchimbaud/timeseries-1h-all-crypto",
        "description": "Crypto 1h OHLCV (all major pairs)",
        "asset_class": "crypto",
        "timeframe": "1h",
    },
]


class HuggingFaceDownloader(Downloader):
    """Downloads financial datasets from HuggingFace Hub."""

    def __init__(self, token: str | None = None):
        self.token = token or os.environ.get("HF_TOKEN", "")

    @property
    def source_name(self) -> str:
        return "huggingface"

    def list_available(self, **kwargs) -> list[dict]:
        return [
            {
                "symbol": "*",
                "dataset": d["dataset"],
                "description": d["description"],
                "asset_class": d["asset_class"],
                "timeframe": d["timeframe"],
            }
            for d in KNOWN_DATASETS
        ]

    def download(
        self,
        symbol: str,
        timeframe: str,
        output_dir: Path,
        *,
        start: str | None = None,
        end: str | None = None,
        dataset: str = "edarchimbaud/timeseries-1d-sp500",
        asset_class: str = "equity",
        **kwargs,
    ) -> DownloadResult:
        symbol = symbol.upper()
        logger.info("Downloading %s from HuggingFace dataset %s", symbol, dataset)
        out_path = self._make_output_path(output_dir, asset_class, symbol, timeframe)
        effective_start = self._resolve_resume_start(out_path, start)

        # Try using the datasets library first (more reliable)
        df = self._download_via_api(dataset, symbol)

        if df is None or df.empty:
            raise RuntimeError(f"No data for {symbol} in {dataset}")

        # Normalize columns
        df.columns = [c.lower() for c in df.columns]
        col_map = {}
        for c in df.columns:
            if c in ("date", "datetime", "time"):
                col_map[c] = "timestamp"
        df = df.rename(columns=col_map)

        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # Filter by date
        if "timestamp" in df.columns:
            if effective_start:
                df = df[df["timestamp"] >= effective_start]
            if end:
                df = df[df["timestamp"] <= end]

        keep = [
            c for c in ["timestamp", "open", "high", "low", "close", "volume"] if c in df.columns
        ]
        df = df[keep].dropna(subset=["open"]).sort_values("timestamp").reset_index(drop=True)

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

    def _download_via_api(self, dataset: str, symbol: str) -> pd.DataFrame | None:
        """Download dataset via HuggingFace API (parquet files)."""
        try:
            # List parquet files in the dataset
            headers = {}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"

            api_url = f"{HF_API}/{dataset}/parquet"
            resp = requests.get(api_url, headers=headers, timeout=30)

            if resp.status_code != 200:
                logger.warning("HF API returned %d for %s", resp.status_code, dataset)
                return self._download_via_hub_lib(dataset, symbol)

            parquet_info = resp.json()

            # Download parquet files
            all_dfs = []
            # parquet_info can be a dict of split->files or a list
            files = []
            if isinstance(parquet_info, dict):
                for split_files in parquet_info.values():
                    if isinstance(split_files, list):
                        files.extend(split_files)
            elif isinstance(parquet_info, list):
                files = parquet_info

            for file_url in files[:5]:  # limit to first 5 files to avoid huge downloads
                url = file_url if isinstance(file_url, str) else file_url.get("url", "")
                if not url:
                    continue
                try:
                    df = pd.read_parquet(url)
                    # Filter for symbol if there's a symbol column
                    sym_cols = [c for c in df.columns if c.lower() in ("symbol", "ticker", "sym")]
                    if sym_cols:
                        df = df[df[sym_cols[0]].str.upper() == symbol]
                    if not df.empty:
                        all_dfs.append(df)
                except Exception as e:
                    logger.debug("Could not read parquet %s: %s", url, e)

            if all_dfs:
                return pd.concat(all_dfs, ignore_index=True)
            return None

        except Exception as e:
            logger.warning("HF API download failed: %s", e)
            return self._download_via_hub_lib(dataset, symbol)

    def _download_via_hub_lib(self, dataset: str, symbol: str) -> pd.DataFrame | None:
        """Fallback: try using the `datasets` library if installed."""
        try:
            from datasets import load_dataset

            ds = load_dataset(dataset, split="train", streaming=True)
            rows = []
            for row in ds:
                sym_val = row.get("symbol", row.get("ticker", ""))
                if str(sym_val).upper() == symbol:
                    rows.append(row)
                if len(rows) >= 100_000:
                    break
            if rows:
                return pd.DataFrame(rows)
        except ImportError:
            logger.debug("`datasets` library not installed, skipping hub fallback")
        except Exception as e:
            logger.debug("Hub lib fallback failed: %s", e)
        return None

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url="https://huggingface.co",
            license="varies",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Download from HuggingFace")
    parser.add_argument("--symbol", help="Symbol to download")
    parser.add_argument("--timeframe", default="1d")
    parser.add_argument("--dataset", default="edarchimbaud/timeseries-1d-sp500")
    parser.add_argument("--asset-class", default="equity")
    parser.add_argument("--start", help="Start date")
    parser.add_argument("--end", help="End date")
    parser.add_argument("--output", default=str(DATA_DIR))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    dl = HuggingFaceDownloader()

    if args.list:
        for item in dl.list_available():
            print(f"  {item['dataset']:45s}  {item['description']}")
        return

    if not args.symbol:
        parser.error("--symbol required (or use --list)")

    result = dl.download(
        args.symbol,
        args.timeframe,
        Path(args.output),
        dataset=args.dataset,
        asset_class=args.asset_class,
        start=args.start,
        end=args.end,
    )
    print(f"✅ {result.symbol}: {result.rows} rows → {result.path}")


if __name__ == "__main__":
    main()
