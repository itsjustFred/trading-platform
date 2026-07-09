"""
Yahoo Finance downloader for US equity OHLCV data.

Stores data under canonical path:
  {output_dir}/ohlcv/yfinance/equity/US/{symbol}/{timeframe}.parquet

Supported timeframes:
  1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w (accepts alias: 1wk)

yfinance constraints:
  - 1m only for roughly the last 30 days
  - Other intraday intervals are limited to around 60 days
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

from data_tools.downloaders.base import DownloadResult, Downloader, SourceMeta
from shared.constants import DATA_DIR, normalize_timeframe

logger = logging.getLogger(__name__)

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
    "h4": "4h",
    "4h": "4h",
    "d1": "1d",
    "1d": "1d",
    "w1": "1w",
    "1w": "1w",
    "1wk": "1w",
}

YF_INTERVAL_MAP: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "60m",
    "4h": "60m",
    "1d": "1d",
    "1w": "1wk",
}


def _normalize_supported_timeframe(value: str) -> str:
    normalized = str(value or "").strip().lower()
    canonical = TIMEFRAME_MAP.get(normalized)
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


def _clip_intraday_window(
    *,
    timeframe: str,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
) -> tuple[pd.Timestamp | None, pd.Timestamp, list[str]]:
    now = pd.Timestamp.now(tz="UTC")
    resolved_end = end or now
    warnings: list[str] = []

    window_days: int | None = None
    if timeframe == "1m":
        window_days = 30
    elif timeframe in {"5m", "15m", "30m", "1h", "4h"}:
        window_days = 60

    if window_days is None:
        return start, resolved_end, warnings

    min_start = resolved_end - pd.Timedelta(days=window_days)
    if start is not None and start < min_start:
        warnings.append(
            (
                f"Requested {timeframe} start {start.date()} clipped to {min_start.date()} "
                f"due to yfinance intraday limit (~{window_days} days)."
            )
        )
        return min_start, resolved_end, warnings
    return start, resolved_end, warnings


class YFinanceDownloader(Downloader):
    @property
    def source_name(self) -> str:
        return "yfinance"

    def list_available(self, **kwargs) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "*",
                "asset_class": "equity",
                "region": "US",
                "timeframes": sorted(set(TIMEFRAME_MAP.values())),
                "description": "Yahoo Finance US equity OHLCV",
            }
        ]

    def _make_us_equity_output_path(
        self,
        output_dir: Path,
        symbol: str,
        timeframe: str,
    ) -> Path:
        path = output_dir / "ohlcv" / self.source_name / "equity" / "US" / symbol / f"{timeframe}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

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

        canonical_tf = _normalize_supported_timeframe(normalize_timeframe(timeframe))
        yf_interval = YF_INTERVAL_MAP[canonical_tf]
        out_path = self._make_us_equity_output_path(output_dir, normalized_symbol, canonical_tf)

        parsed_start = _to_utc_timestamp(start)
        parsed_end = _to_utc_timestamp(end)
        if parsed_start and parsed_end and parsed_start > parsed_end:
            raise ValueError("start must be <= end")

        bounded_start, bounded_end, warnings = _clip_intraday_window(
            timeframe=canonical_tf,
            start=parsed_start,
            end=parsed_end,
        )
        resolved_start = (
            bounded_start.strftime("%Y-%m-%d")
            if bounded_start is not None
            else self._resolve_resume_start(out_path, None)
        )
        if resolved_start:
            resume_start = _to_utc_timestamp(resolved_start)
            adjusted_resume, _, resume_warnings = _clip_intraday_window(
                timeframe=canonical_tf,
                start=resume_start,
                end=bounded_end,
            )
            warnings.extend(resume_warnings)
            resolved_start = adjusted_resume.strftime("%Y-%m-%d") if adjusted_resume is not None else ""
        resolved_end = bounded_end.strftime("%Y-%m-%d")

        logger.info(
            "Downloading yfinance OHLCV",
            extra={
                "data": {
                    "symbol": normalized_symbol,
                    "timeframe": canonical_tf,
                    "interval": yf_interval,
                    "start": resolved_start,
                    "end": resolved_end,
                }
            },
        )

        df = yf.download(
            tickers=normalized_symbol,
            start=resolved_start,
            end=resolved_end,
            interval=yf_interval,
            auto_adjust=False,
            progress=False,
            threads=False,
            group_by="column",
        )
        if df is None or df.empty:
            raise RuntimeError(
                f"No yfinance data returned for {normalized_symbol} ({canonical_tf}) in requested range."
            )

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                str(column[0]) if isinstance(column, tuple) and len(column) > 0 else str(column)
                for column in df.columns
            ]
        df.columns = [str(col).strip().lower() for col in df.columns]

        df = df.reset_index()
        df.columns = [str(col).strip().lower() for col in df.columns]
        ts_column = None
        for candidate in ("datetime", "date", "timestamp"):
            if candidate in df.columns:
                ts_column = candidate
                break
        if ts_column is None:
            raise RuntimeError("yfinance payload missing datetime column")

        rename_map = {ts_column: "timestamp"}
        for key in ("open", "high", "low", "close", "volume"):
            if key in df.columns:
                rename_map[key] = key
            elif key.capitalize() in df.columns:
                rename_map[key.capitalize()] = key
        df = df.rename(columns=rename_map)

        keep = [column for column in ("timestamp", "open", "high", "low", "close", "volume") if column in df.columns]
        if "timestamp" not in keep or "open" not in keep:
            raise RuntimeError("yfinance payload missing required OHLCV columns")
        df = df[keep]
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")

        for column in ("open", "high", "low", "close", "volume"):
            if column in df.columns:
                df[column] = pd.to_numeric(df[column], errors="coerce")

        df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        if df.empty:
            raise RuntimeError(f"yfinance returned only empty/NaN rows for {normalized_symbol} ({canonical_tf})")

        if canonical_tf == "4h":
            resampled = (
                df.set_index("timestamp")
                .resample("4h")
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna(subset=["open", "high", "low", "close"])
                .reset_index()
            )
            df = resampled

        combined = self._merge_and_store_single_file(out_path, df)
        return DownloadResult(
            symbol=normalized_symbol,
            timeframe=canonical_tf,
            source=self.source_name,
            asset_class="equity",
            path=out_path,
            rows=len(combined),
            start=str(combined["timestamp"].iloc[0]),
            end=str(combined["timestamp"].iloc[-1]),
            size_bytes=out_path.stat().st_size,
            warnings=warnings,
        )

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url="https://finance.yahoo.com",
            license="free",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Download OHLCV data from Yahoo Finance")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols (e.g. AAPL,MSFT)")
    parser.add_argument(
        "--timeframes",
        default="1d",
        help="Comma-separated timeframes (1m,5m,15m,30m,1h,4h,1d,1w)",
    )
    parser.add_argument("--start", default="", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=str(DATA_DIR), help="Output data directory")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    downloader = YFinanceDownloader()
    output_dir = Path(args.output).expanduser().resolve()
    symbols = [item.upper() for item in _parse_csv_list(args.symbols)]
    timeframes = _parse_csv_list(args.timeframes)
    if not symbols:
        parser.error("--symbols must include at least one symbol")
    if not timeframes:
        parser.error("--timeframes must include at least one timeframe")

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
                print(
                    f"✅ {result.symbol} {result.timeframe}: {result.rows} rows → {result.path}{warning_suffix}"
                )
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
