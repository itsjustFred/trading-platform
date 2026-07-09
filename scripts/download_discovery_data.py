#!/usr/bin/env python3
"""Download OHLCV data needed by the latest discovered grid trials.

Canonical output path:
  data/ohlcv/yfinance/crypto/USDT/<DISCOVERY_SYMBOL>/<timeframe>.parquet

This deliberately stores crypto symbols without the Yahoo ticker hyphen so the
existing runner can load them by the discovery symbol names (BTCUSDT, ETHUSDT, ...).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_PATH = Path("/mnt/block-volume-data/discovered_strategies/discovery_20260618_0752.json")
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
MIN_ROWS = {"1h": 100, "4h": 100, "1d": 250}
YF_SYMBOLS = {
    "BTCUSDT": "BTC-USD",
    "ETHUSDT": "ETH-USD",
    "XRPUSDT": "XRP-USD",
    "SOLUSDT": "SOL-USD",
    "ADAUSDT": "ADA-USD",
}
TIMEFRAME_DEFAULT_START = {"1h": "2026-04-19", "4h": "2026-04-19", "1d": "2023-01-01"}


def load_trials() -> list[dict[str, Any]]:
    with DISCOVERY_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def out_path(symbol: str, timeframe: str) -> Path:
    return DATA_DIR / "ohlcv" / "yfinance" / "crypto" / "USDT" / symbol / f"{timeframe}.parquet"


def existing_is_sufficient(path: Path, timeframe: str) -> bool:
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
        return len(df) >= MIN_ROWS.get(timeframe, 100)
    except Exception:
        return False


def download_one(symbol: str, timeframe: str, retries: int = 4) -> dict[str, Any]:
    path = out_path(symbol, timeframe)
    if existing_is_sufficient(path, timeframe):
        df = pd.read_parquet(path)
        return {"symbol": symbol, "timeframe": timeframe, "status": "cached", "rows": len(df), "path": str(path)}

    yf_symbol = YF_SYMBOLS.get(symbol, symbol.replace("USDT", "-USD"))
    start = TIMEFRAME_DEFAULT_START.get(timeframe, "2026-04-19")
    end = (datetime.now(timezone.utc) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    last_error = ""

    for attempt in range(1, retries + 1):
        try:
            df = yf.download(
                tickers=yf_symbol,
                start=start,
                end=end,
                interval={"1h": "60m", "4h": "60m", "1d": "1d"}[timeframe],
                auto_adjust=False,
                progress=False,
                threads=False,
                group_by="column",
            )
            if df is None or df.empty:
                raise RuntimeError(f"yfinance returned no rows for {yf_symbol}/{timeframe}")
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [str(col[0]) if isinstance(col, tuple) else str(col) for col in df.columns]
            df = df.reset_index()
            df.columns = [str(col).strip().lower() for col in df.columns]
            ts_col = next((c for c in ("datetime", "date", "timestamp") if c in df.columns), None)
            if ts_col is None:
                raise RuntimeError(f"yfinance payload missing timestamp for {yf_symbol}")
            df = df.rename(columns={ts_col: "timestamp"})
            for col in ("open", "high", "low", "close", "volume"):
                if col not in df.columns:
                    if col == "volume":
                        df[col] = 0
                    else:
                        raise RuntimeError(f"yfinance payload missing {col} for {yf_symbol}")
            df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            for col in ("open", "high", "low", "close", "volume"):
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
            if timeframe == "4h":
                df = (
                    df.set_index("timestamp")
                    .resample("4h")
                    .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
                    .dropna(subset=["open", "high", "low", "close"])
                    .reset_index()
                )
            df = df.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
            if df.empty:
                raise RuntimeError(f"yfinance returned only empty/NaN rows for {yf_symbol}/{timeframe}")

            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(path, index=False)
            return {"symbol": symbol, "timeframe": timeframe, "status": "downloaded", "rows": len(df), "path": str(path)}
        except Exception as exc:
            last_error = str(exc)
            time.sleep(min(30 * attempt, 120))
    return {"symbol": symbol, "timeframe": timeframe, "status": "failed", "error": last_error, "path": str(path)}


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    trials = load_trials()
    unique = {(t["symbol"], t["timeframe"]) for t in trials}
    results: list[dict[str, Any]] = []
    for symbol, timeframe in sorted(unique):
        result = download_one(symbol, timeframe)
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "requested_combos": len(unique),
        "results": results,
        "failed": [r for r in results if r["status"] == "failed"],
    }
    (LOG_DIR / "grid_data_download_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    if summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
