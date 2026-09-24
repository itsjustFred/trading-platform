"""
HuggingFace OHLCV downloader for mito0o852/OHLCV-1m.

The upstream dataset is published as monthly parquet files:
  https://huggingface.co/datasets/mito0o852/OHLCV-1m/resolve/main/data/ohlcv_YYYY-MM.parquet

Canonical output path:
  {output_dir}/ohlcv/huggingface/equity/US/{symbol}/{timeframe}.parquet
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data_tools.downloaders.base import DownloadResult, Downloader, SourceMeta
from shared.constants import DATA_DIR, normalize_timeframe

logger = logging.getLogger(__name__)

DATASET_ID = "mito0o852/OHLCV-1m"
MONTHLY_URL_TEMPLATE = (
    "https://huggingface.co/datasets/mito0o852/OHLCV-1m/resolve/main/data/ohlcv_{year:04d}-{month:02d}.parquet"
)
#: Host allowlist for monthly parquet URLs (B3: unpinned remote reads must
#: stay on the known dataset host).
HF_ALLOWED_HOSTS = frozenset({"huggingface.co"})
#: Fail-closed past N gaps (B3): more failed months than this aborts the
#: download instead of selling a holey dataset as covered.
HF_MAX_FAILED_MONTHS = 3


def _check_month_url(url: str) -> None:
    """Reject monthly URLs outside the allowlisted dataset host (B3)."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host.lower() not in HF_ALLOWED_HOSTS:
        raise ValueError(f"HuggingFace monthly URL host not allowlisted: {host!r}")
DEFAULT_START_DATE = "1994-10-01"
HF_COVERAGE_END = "2025-07-31"

TIMEFRAME_MAP: dict[str, str] = {
    "m1": "1m",
    "1m": "1m",
    "m5": "5m",
    "5m": "5m",
    "m15": "15m",
    "15m": "15m",
    "m30": "30m",
    "30m": "30m",
    "h1": "1h",
    "1h": "1h",
    "d1": "1d",
    "1d": "1d",
}

RESAMPLE_RULES: dict[str, str] = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "1d": "1d",
}


def _normalize_supported_timeframe(value: str) -> str:
    normalized = str(value or "").strip().lower()
    canonical = TIMEFRAME_MAP.get(normalized) or TIMEFRAME_MAP.get(normalize_timeframe(normalized))
    if not canonical:
        raise ValueError(
            f"Unsupported timeframe '{value}'. Supported: {sorted(set(TIMEFRAME_MAP.values()))}"
        )
    return canonical


def _to_utc_timestamp(value: str | None) -> pd.Timestamp | None:
    if value is None or not str(value).strip():
        return None
    ts = pd.to_datetime(value, utc=True, errors="raise")
    if isinstance(ts, pd.DatetimeIndex):
        if len(ts) == 0:
            return None
        return pd.Timestamp(ts[0])
    return pd.Timestamp(ts)


def _month_sequence(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[int, int]]:
    months: list[tuple[int, int]] = []
    cursor = pd.Timestamp(year=start.year, month=start.month, day=1, tz="UTC")
    limit = pd.Timestamp(year=end.year, month=end.month, day=1, tz="UTC")
    while cursor <= limit:
        months.append((int(cursor.year), int(cursor.month)))
        if cursor.month == 12:
            cursor = pd.Timestamp(year=cursor.year + 1, month=1, day=1, tz="UTC")
        else:
            cursor = pd.Timestamp(year=cursor.year, month=cursor.month + 1, day=1, tz="UTC")
    return months


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _first_matching_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {column.lower(): column for column in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


class HuggingFaceDownloader(Downloader):
    @property
    def source_name(self) -> str:
        return "huggingface"

    def list_available(self, **kwargs) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "*",
                "dataset": DATASET_ID,
                "description": "US stocks 1m OHLCV monthly parquet files",
                "asset_class": "equity",
                "region": "US",
                "timeframes": sorted(RESAMPLE_RULES.keys()),
                "coverage_end": HF_COVERAGE_END,
            }
        ]

    def _make_us_equity_output_path(self, output_dir: Path, symbol: str, timeframe: str) -> Path:
        path = output_dir / "ohlcv" / self.source_name / "equity" / "US" / symbol / f"{timeframe}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _normalize_month_frame(
        self,
        frame: pd.DataFrame,
        *,
        symbol: str,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
    ) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()

        working = frame.copy()
        working.columns = [str(column).strip() for column in working.columns]
        timestamp_column = _first_matching_column(
            list(working.columns),
            ("timestamp", "datetime", "date", "time"),
        )
        if timestamp_column is None:
            return pd.DataFrame()

        symbol_column = _first_matching_column(
            list(working.columns),
            ("symbol", "ticker", "sym"),
        )
        if symbol_column is not None:
            working = working[working[symbol_column].astype(str).str.upper() == symbol]
        if working.empty:
            return pd.DataFrame()

        rename_map: dict[str, str] = {timestamp_column: "timestamp"}
        for canonical in ("open", "high", "low", "close", "volume"):
            column = _first_matching_column(list(working.columns), (canonical,))
            if column is not None:
                rename_map[column] = canonical
        working = working.rename(columns=rename_map)
        required = {"timestamp", "open", "high", "low", "close"}
        if not required.issubset(set(working.columns)):
            return pd.DataFrame()

        if "volume" not in working.columns:
            working["volume"] = 0

        working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            working[column] = pd.to_numeric(working[column], errors="coerce")

        working = working.dropna(subset=["timestamp", "open", "high", "low", "close"])
        if working.empty:
            return pd.DataFrame()

        working = working[
            (working["timestamp"] >= start_ts)
            & (working["timestamp"] <= end_ts)
        ]
        if working.empty:
            return pd.DataFrame()

        return (
            working[["timestamp", "open", "high", "low", "close", "volume"]]
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )

    def _resample(self, frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        if frame.empty:
            return frame
        if timeframe == "1m":
            return frame
        from data_tools.resample import resample_with_origin

        return resample_with_origin(frame, RESAMPLE_RULES[timeframe])

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
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required")

        canonical_tf = _normalize_supported_timeframe(timeframe)
        out_path = self._make_us_equity_output_path(output_dir, normalized_symbol, canonical_tf)

        resume_start = self._resolve_resume_start(out_path, start) or DEFAULT_START_DATE
        parsed_start = _to_utc_timestamp(resume_start)
        parsed_end = _to_utc_timestamp(end) or _to_utc_timestamp(HF_COVERAGE_END)
        coverage_end = _to_utc_timestamp(HF_COVERAGE_END)
        if parsed_start is None or parsed_end is None or coverage_end is None:
            raise RuntimeError("Could not resolve HuggingFace date boundaries")

        warnings: list[str] = []
        if parsed_end > coverage_end:
            warnings.append(
                f"Requested end clipped to {HF_COVERAGE_END} (HuggingFace dataset coverage limit)."
            )
            parsed_end = coverage_end

        if parsed_start > parsed_end:
            raise ValueError(
                f"Requested range is outside HuggingFace coverage ({DEFAULT_START_DATE} → {HF_COVERAGE_END})."
            )

        months = _month_sequence(parsed_start, parsed_end)
        monthly_frames: list[pd.DataFrame] = []
        failed_months: list[str] = []

        for index, (year, month) in enumerate(months, start=1):
            month_label = f"{year:04d}-{month:02d}"
            logger.info("HuggingFace download progress %s/%s: %s", index, len(months), month_label)
            url = MONTHLY_URL_TEMPLATE.format(year=year, month=month)
            _check_month_url(url)
            try:
                month_frame = pd.read_parquet(url)
            except Exception as exc:
                # A failed month must stay visible: silently skipping it makes
                # the dataset look covered while the month is missing (DV-B16).
                failed_months.append(month_label)
                warnings.append(f"month {month_label}: download failed ({exc})")
                continue

            normalized = self._normalize_month_frame(
                month_frame,
                symbol=normalized_symbol,
                start_ts=parsed_start,
                end_ts=parsed_end,
            )
            if not normalized.empty:
                monthly_frames.append(normalized)

        # B3: fail closed past N gaps — a holey dataset must not sell as
        # covered (warnings alone hid multi-month holes).
        if len(failed_months) > HF_MAX_FAILED_MONTHS:
            raise RuntimeError(
                f"HuggingFace download for {normalized_symbol}: {len(failed_months)} "
                f"months failed ({', '.join(failed_months)}); refusing a holey dataset."
            )

        if not monthly_frames:
            raise RuntimeError(
                f"No {normalized_symbol} rows found in {DATASET_ID} for {parsed_start.date()} → {parsed_end.date()}."
            )

        combined_months = (
            pd.concat(monthly_frames, ignore_index=True)
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )
        prepared = self._resample(combined_months, canonical_tf)
        if prepared.empty:
            raise RuntimeError(
                f"HuggingFace download produced no rows for {normalized_symbol} ({canonical_tf})."
            )

        stored = self._merge_and_store_single_file(out_path, prepared)
        if failed_months:
            warnings.append(
                f"failed months ({len(failed_months)}): {', '.join(failed_months)}"
            )
        return DownloadResult(
            symbol=normalized_symbol,
            timeframe=canonical_tf,
            source=self.source_name,
            asset_class="equity",
            path=out_path,
            rows=len(stored),
            start=str(stored["timestamp"].iloc[0]),
            end=str(stored["timestamp"].iloc[-1]),
            size_bytes=out_path.stat().st_size,
            warnings=warnings,
        )

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url=f"https://huggingface.co/datasets/{DATASET_ID}",
            license="varies",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Download OHLCV data from HuggingFace monthly parquet files")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols (e.g. AAPL,MSFT)")
    parser.add_argument(
        "--timeframes",
        default="1m",
        help="Comma-separated timeframes (1m,5m,15m,30m,1h,1d)",
    )
    parser.add_argument("--start", default=DEFAULT_START_DATE, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=HF_COVERAGE_END, help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=str(DATA_DIR), help="Output data directory")
    parser.add_argument("--list", action="store_true", help="List source metadata and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    downloader = HuggingFaceDownloader()

    if args.list:
        for item in downloader.list_available():
            print(
                f"{item['dataset']} | {item['description']} | tf={','.join(item['timeframes'])} | end={item['coverage_end']}"
            )
        return

    symbols = [item.upper() for item in _parse_csv_list(args.symbols)]
    timeframes = _parse_csv_list(args.timeframes)
    if not symbols:
        parser.error("--symbols must include at least one symbol")
    if not timeframes:
        parser.error("--timeframes must include at least one timeframe")

    output_dir = Path(args.output).expanduser().resolve()
    failures: list[str] = []
    for symbol in symbols:
        for timeframe in timeframes:
            try:
                result = downloader.download(
                    symbol=symbol,
                    timeframe=timeframe,
                    output_dir=output_dir,
                    start=args.start or None,
                    end=args.end or None,
                )
                warning_suffix = f" warnings={len(result.warnings)}" if result.warnings else ""
                print(f"✅ {result.symbol} {result.timeframe}: {result.rows} rows → {result.path}{warning_suffix}")
                for warning in result.warnings:
                    print(f"⚠️  {warning}")
            except Exception as exc:
                message = f"{symbol} {timeframe}: {exc}"
                failures.append(message)
                print(f"❌ {message}")

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
