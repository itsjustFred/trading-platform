"""
Base classes for data downloaders.

All downloaders implement the Downloader ABC so the catalog builder and
cache manager can treat them uniformly.

Usage:
    from data_tools.downloaders.base import Downloader, DownloadResult
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    """Result of a single download operation."""

    symbol: str
    timeframe: str
    source: str
    asset_class: str
    path: Path  # where the parquet was saved
    rows: int
    start: str  # ISO date of first row
    end: str  # ISO date of last row
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

    name: str  # e.g. "histdata", "dolthub"
    url: str = ""  # base URL
    license: str = "unknown"  # "free", "commercial", "academic"
    update_frequency: str = "manual"  # "daily", "weekly", "manual"
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

    def _make_output_path(
        self, output_dir: Path, asset_class: str, symbol: str, timeframe: str
    ) -> Path:
        """Build standardised output path: {output_dir}/ohlcv/{source}/{asset_class}/{symbol}/{tf}.parquet"""
        path = (
            output_dir / "ohlcv" / self.source_name / asset_class / symbol / f"{timeframe}.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _resolve_resume_start(self, output_path: Path, requested_start: str | None) -> str | None:
        """Return requested start or latest stored timestamp for smart resume."""
        if requested_start:
            return requested_start

        latest_ts = self._read_latest_timestamp(output_path)
        if latest_ts is None:
            return None

        resume_start = latest_ts.date().isoformat()
        logger.info("Smart resume enabled for %s from %s", output_path, resume_start)
        return resume_start

    def _merge_and_store_single_file(self, output_path: Path, new_data: pd.DataFrame) -> pd.DataFrame:
        """Persist data in a single parquet file per symbol/timeframe.

        If the file exists, it is loaded and merged with incoming rows.
        Duplicate timestamps are removed (keeping newest row), data is sorted,
        and the target parquet is replaced atomically: the merged frame is
        written to a temporary file in the same directory and moved into place
        with :func:`os.replace`, so a crash mid-write can never truncate or
        corrupt the previously stored canonical data.
        """
        normalized_new = self._normalize_timestamps(new_data)

        if output_path.exists():
            try:
                existing_data = pd.read_parquet(output_path)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Could not read existing parquet '{output_path}': {exc}") from exc
            normalized_existing = self._normalize_timestamps(existing_data)
            combined = pd.concat([normalized_existing, normalized_new], ignore_index=True, sort=False)
        else:
            combined = normalized_new.copy()

        if "timestamp" in combined.columns:
            combined = (
                combined.drop_duplicates(subset=["timestamp"], keep="last")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
        else:
            combined = combined.drop_duplicates().reset_index(drop=True)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = output_path.with_name(f"{output_path.name}.tmp-{os.getpid()}")
        try:
            try:
                combined.to_parquet(temp_path, index=False)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Could not write parquet '{output_path}': {exc}") from exc
            os.replace(temp_path, output_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        return combined

    def _read_latest_timestamp(self, output_path: Path) -> pd.Timestamp | None:
        """Read max timestamp from an existing parquet without loading full OHLCV payload.

        Row-group max statistics are only a *lower bound* for the newest stored
        timestamp: writers may disable statistics, drop them per row group, or
        (for a file that was appended out of order) report a max below the
        actual newest row. The statistics are therefore used as a fast path and
        are always cross-checked against the last row of the final row group;
        whichever value is newer wins, so resume can never rewind to a stale
        point and re-download data that is already stored.
        """
        if not output_path.exists():
            return None

        try:
            parquet_file = pq.ParquetFile(output_path)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Could not open parquet '{output_path}': {exc}") from exc

        metadata = parquet_file.metadata
        if metadata is None or metadata.num_rows == 0 or metadata.num_row_groups == 0:
            return None

        columns = list(parquet_file.schema_arrow.names)
        timestamp_column = self._select_timestamp_column(columns)
        if timestamp_column is None:
            return None

        column_index = columns.index(timestamp_column)

        stats_latest: pd.Timestamp | None = None
        for row_group_index in range(metadata.num_row_groups - 1, -1, -1):
            stats = metadata.row_group(row_group_index).column(column_index).statistics
            if stats is None or not getattr(stats, "has_min_max", False):
                continue
            latest = pd.to_datetime(stats.max, utc=True, errors="coerce")
            if not pd.isna(latest):
                stats_latest = pd.Timestamp(latest)
                break

        row_group = parquet_file.read_row_group(metadata.num_row_groups - 1, columns=[timestamp_column])
        if row_group.num_rows == 0:
            return stats_latest

        latest_value = row_group.column(0)[row_group.num_rows - 1].as_py()
        row_latest = pd.to_datetime(latest_value, utc=True, errors="coerce")
        if pd.isna(row_latest):
            return stats_latest

        row_latest = pd.Timestamp(row_latest)
        if stats_latest is None or row_latest > stats_latest:
            return row_latest
        return stats_latest

    @staticmethod
    def _select_timestamp_column(columns: list[str]) -> str | None:
        lowered = {column.lower(): column for column in columns}
        for candidate in ("timestamp", "datetime", "date", "time"):
            if candidate in lowered:
                return lowered[candidate]
        return None

    @staticmethod
    def _normalize_timestamps(df: pd.DataFrame) -> pd.DataFrame:
        normalized = df.copy()
        if "timestamp" in normalized.columns:
            normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True, errors="coerce")
            normalized = normalized.dropna(subset=["timestamp"])
        return normalized
