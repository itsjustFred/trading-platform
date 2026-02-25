"""
Base classes for data downloaders.

All downloaders implement the Downloader ABC so the catalog builder and
cache manager can treat them uniformly.

Usage:
    from data_tools.downloaders.base import Downloader, DownloadResult
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    """Result of a single download operation."""

    symbol: str
    timeframe: str
    source: str
    asset_class: str
    path: Path                                 # where the parquet was saved
    rows: int
    start: str                                 # ISO date of first row
    end: str                                   # ISO date of last row
    size_bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "source": self.source,
            "asset_class": self.asset_class,
            "path": str(self.path),
            "rows": self.rows,
            "start": self.start,
            "end": self.end,
            "size_bytes": self.size_bytes,
            "warnings": self.warnings,
        }


@dataclass
class SourceMeta:
    """Metadata about a data source for catalog integration."""

    name: str                                  # e.g. "histdata", "dolthub"
    url: str = ""                              # base URL
    license: str = "unknown"                   # "free", "commercial", "academic"
    update_frequency: str = "manual"           # "daily", "weekly", "manual"
    downloaded_at: str = ""

    def to_dict(self) -> dict:
        d = {"name": self.name, "license": self.license, "update_frequency": self.update_frequency}
        if self.url:
            d["url"] = self.url
        if self.downloaded_at:
            d["downloaded_at"] = self.downloaded_at
        return d


class Downloader(ABC):
    """Abstract base class for all data downloaders.

    Each downloader knows how to fetch OHLCV data from one source and
    store it as Parquet files under ~/data/ohlcv/{source}/...
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Short identifier, e.g. 'histdata', 'dolthub', 'huggingface'."""
        ...

    @abstractmethod
    def list_available(self, **kwargs) -> list[dict]:
        """List datasets available for download.

        Returns list of dicts with at least: symbol, timeframe, asset_class.
        Additional keys depend on the source.
        """
        ...

    @abstractmethod
    def download(
        self,
        symbol: str,
        timeframe: str,
        output_dir: Path,
        *,
        start: str | None = None,
        end: str | None = None,
        **kwargs,
    ) -> DownloadResult:
        """Download OHLCV data for one symbol/timeframe and save as Parquet.

        Args:
            symbol: Instrument symbol (e.g. "EURUSD", "AAPL")
            timeframe: Target timeframe (e.g. "1m", "1h", "1d")
            output_dir: Root data directory (e.g. ~/data)
            start: Optional start date filter (ISO format)
            end: Optional end date filter (ISO format)

        Returns:
            DownloadResult with path to saved file and metadata.
        """
        ...

    def get_source_meta(self) -> SourceMeta:
        """Return metadata about this source for catalog integration."""
        return SourceMeta(
            name=self.source_name,
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )

    def _make_output_path(self, output_dir: Path, asset_class: str, symbol: str, timeframe: str) -> Path:
        """Build standardised output path: {output_dir}/ohlcv/{source}/{asset_class}/{symbol}/{tf}.parquet"""
        path = output_dir / "ohlcv" / self.source_name / asset_class / symbol / f"{timeframe}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
