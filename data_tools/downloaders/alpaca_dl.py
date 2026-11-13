"""
Alpaca historical stock bars downloader.

Canonical output path:
  {output_dir}/ohlcv/alpaca/equity/US/{symbol}/{timeframe}.parquet
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data_tools.downloaders.base import Downloader, DownloadResult, SourceMeta
from data_tools.path_safety import build_dataset_path
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
    "d1": "1d",
    "1d": "1d",
}

ALPACA_MAX_LIMIT = 10_000
ALPACA_PY_REQUIRED_MESSAGE = "alpaca-py is required. Install dependency 'alpaca-py'."


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


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


class AlpacaDownloader(Downloader):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self.api_key = str(
            api_key
            or os.environ.get("ALPACA_API_KEY")
            or os.environ.get("APCA_API_KEY_ID")
            or ""
        ).strip()
        self.secret_key = str(
            secret_key
            or os.environ.get("ALPACA_SECRET_KEY")
            or os.environ.get("APCA_API_SECRET_KEY")
            or ""
        ).strip()

    @property
    def source_name(self) -> str:
        return "alpaca"

    def _build_client(self):
        if not self.api_key or not self.secret_key:
            raise RuntimeError(
                "Missing Alpaca credentials. Set ALPACA_API_KEY/APCA_API_KEY_ID and "
                "ALPACA_SECRET_KEY/APCA_API_SECRET_KEY."
            )
        try:
            from alpaca.data.historical import StockHistoricalDataClient
        except ImportError as exc:
            raise RuntimeError(ALPACA_PY_REQUIRED_MESSAGE) from exc
        return StockHistoricalDataClient(api_key=self.api_key, secret_key=self.secret_key)

    def _to_alpaca_timeframe(self, timeframe: str):
        try:
            from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        except ImportError as exc:
            raise RuntimeError(ALPACA_PY_REQUIRED_MESSAGE) from exc

        if timeframe == "1m":
            return TimeFrame.Minute
        if timeframe == "5m":
            return TimeFrame(5, TimeFrameUnit.Minute)
        if timeframe == "15m":
            return TimeFrame(15, TimeFrameUnit.Minute)
        if timeframe == "30m":
            return TimeFrame(30, TimeFrameUnit.Minute)
        if timeframe == "1h":
            return TimeFrame.Hour
        if timeframe == "1d":
            return TimeFrame.Day
        raise ValueError(f"Unsupported Alpaca timeframe '{timeframe}'")

    def list_available(self, **kwargs) -> list[dict[str, Any]]:
        return [
            {
                "symbol": "*",
                "description": "US equities historical bars",
                "asset_class": "equity",
                "region": "US",
                "timeframes": sorted(set(TIMEFRAME_MAP.values())),
            }
        ]

    def _make_us_equity_output_path(self, output_dir: Path, symbol: str, timeframe: str) -> Path:
        path = build_dataset_path(
            output_dir,
            "ohlcv",
            self.source_name,
            "equity",
            "US",
            symbol,
            f"{timeframe}.parquet",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _normalize_response_frame(
        self,
        frame: pd.DataFrame,
        *,
        symbol: str,
        start: pd.Timestamp | None,
        end: pd.Timestamp | None,
    ) -> pd.DataFrame:
        if frame is None or frame.empty:
            return pd.DataFrame()

        working = frame.reset_index()
        working.columns = [str(column).strip().lower() for column in working.columns]

        if "symbol" in working.columns:
            working = working[working["symbol"].astype(str).str.upper() == symbol]
        if working.empty:
            return pd.DataFrame()

        timestamp_column = None
        for candidate in ("timestamp", "time", "datetime", "t"):
            if candidate in working.columns:
                timestamp_column = candidate
                break
        if timestamp_column is None:
            return pd.DataFrame()

        rename_map = {
            timestamp_column: "timestamp",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "volume": "volume",
        }
        working = working.rename(columns=rename_map)

        required = {"timestamp", "open", "high", "low", "close"}
        if not required.issubset(set(working.columns)):
            return pd.DataFrame()
        if "volume" not in working.columns:
            working["volume"] = float("nan")

        working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True, errors="coerce")
        for column in ("open", "high", "low", "close", "volume"):
            working[column] = pd.to_numeric(working[column], errors="coerce")

        working = working.dropna(subset=["timestamp", "open", "high", "low", "close"])
        if start is not None:
            working = working[working["timestamp"] >= start]
        if end is not None:
            working = working[working["timestamp"] <= end]
        if working.empty:
            return pd.DataFrame()

        return (
            working[["timestamp", "open", "high", "low", "close", "volume"]]
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )

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

        parsed_start = _to_utc_timestamp(self._resolve_resume_start(out_path, start) or start)
        parsed_end = _to_utc_timestamp(end)
        if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
            raise ValueError("start must be <= end")

        try:
            from alpaca.data.requests import StockBarsRequest
        except ImportError as exc:
            raise RuntimeError(ALPACA_PY_REQUIRED_MESSAGE) from exc

        client = self._build_client()
        alpaca_tf = self._to_alpaca_timeframe(canonical_tf)

        page_token: str | None = None
        seen_tokens: set[str] = set()
        pages: list[pd.DataFrame] = []

        while True:
            # No `page_token=` here: it is not a field of `StockBarsRequest` in the
            # pinned alpaca-py, so pydantic silently dropped it and every request
            # in this loop was page one. The repeated-token guard below is what
            # stops the loop, and a response that ever *does* carry a usable
            # `next_page_token` still advances it (see `get_stock_bars`, which
            # accepts the params object only).
            request = StockBarsRequest(
                symbol_or_symbols=[normalized_symbol],
                timeframe=alpaca_tf,
                start=parsed_start.to_pydatetime() if parsed_start is not None else None,
                end=parsed_end.to_pydatetime() if parsed_end is not None else None,
                limit=ALPACA_MAX_LIMIT,
            )
            response = client.get_stock_bars(request)
            frame = getattr(response, "df", None)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                pages.append(
                    self._normalize_response_frame(
                        frame,
                        symbol=normalized_symbol,
                        start=parsed_start,
                        end=parsed_end,
                    )
                )

            next_page_token = str(getattr(response, "next_page_token", "") or "").strip()
            if not next_page_token:
                break
            if next_page_token == page_token or next_page_token in seen_tokens:
                logger.warning("Stopping Alpaca pagination due to repeated page token.")
                break
            seen_tokens.add(next_page_token)
            page_token = next_page_token

        frames = [frame for frame in pages if isinstance(frame, pd.DataFrame) and not frame.empty]
        if not frames:
            raise RuntimeError(
                f"No Alpaca bars returned for {normalized_symbol} {canonical_tf}"
                + (
                    f" in range {parsed_start.date()} → {parsed_end.date()}"
                    if parsed_start is not None and parsed_end is not None
                    else ""
                )
            )

        combined_new = (
            pd.concat(frames, ignore_index=True)
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )
        stored = self._merge_and_store_single_file(out_path, combined_new)

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
        )

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url="https://alpaca.markets",
            license="commercial",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Download OHLCV data from Alpaca historical bars")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols (e.g. AAPL,MSFT)")
    parser.add_argument(
        "--timeframes",
        default="1d",
        help="Comma-separated timeframes (1m,5m,15m,30m,1h,1d)",
    )
    parser.add_argument("--start", default="", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=str(DATA_DIR), help="Output data directory")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    downloader = AlpacaDownloader()
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
                print(f"✅ {result.symbol} {result.timeframe}: {result.rows} rows → {result.path}")
            except Exception as exc:
                message = f"{symbol} {timeframe}: {exc}"
                failures.append(message)
                print(f"❌ {message}")

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
