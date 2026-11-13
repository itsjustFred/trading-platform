"""Rebuild the committed fixtures of the published-strategy parity tests.

Downloads daily data with yfinance, stores the raw download under
``<data_dir>/ohlcv/yfinance/...`` (gitignored, catalog layout) and writes the
small gzip CSV fixtures to ``tests/parity/fixtures/published``.

Total-return handling: ``SPY``/``EFA``/``AGG``/``BIL``/``VEU`` fixtures hold
DIVIDEND-ADJUSTED prices (``Adj Close`` scale factor applied to open/high/low/
close, a total-return series).  ``GSPC`` (S&P 500 price index, 1928-) and
``SP500TR`` (S&P 500 total return index, 1988-) are index levels.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "tests" / "parity" / "fixtures" / "published"

#: symbol -> (yfinance ticker, first date kept, ohlc?)
SPECS = {
    "SPY": ("SPY", "1993-01-01", True),
    "EFA": ("EFA", "2001-01-01", False),
    "VEU": ("VEU", "2007-01-01", False),
    "AGG": ("AGG", "2003-01-01", False),
    "BIL": ("BIL", "2007-01-01", False),
    "GSPC": ("^GSPC", "1928-01-01", False),
    "SP500TR": ("^SP500TR", "1988-01-01", False),
}
END = "2025-12-31"


def build(name: str, ticker: str, start: str, ohlc: bool, data_dir: Path) -> pd.DataFrame:
    raw = yf.download(ticker, start=start, end=END, auto_adjust=False, progress=False)
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.dropna(subset=["Close"])
    target = data_dir / "ohlcv" / "yfinance" / "equity" / "US" / name
    target.mkdir(parents=True, exist_ok=True)
    full = raw.reset_index().rename(columns=str.lower)
    full = full.rename(columns={"date": "timestamp"})
    full["timestamp"] = pd.to_datetime(full["timestamp"], utc=True)
    full.to_parquet(target / "1d.parquet", index=False)
    factor = (raw["Adj Close"] / raw["Close"]) if "Adj Close" in raw and name not in ("GSPC", "SP500TR") else 1.0
    out = pd.DataFrame({"timestamp": raw.index.strftime("%Y-%m-%d")})
    if ohlc:
        for col in ("Open", "High", "Low", "Close"):
            out[col.lower()] = (raw[col] * factor).round(4).to_numpy()
    else:
        out["close"] = (raw["Close"] * factor).round(4).to_numpy()
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR", "data"))
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (ticker, start, ohlc) in SPECS.items():
        frame = build(name, ticker, start, ohlc, Path(args.data_dir))
        frame.to_csv(OUT / f"{name.lower()}_1d.csv.gz", index=False, compression="gzip")
        print(name, len(frame), frame["timestamp"].iloc[0], frame["timestamp"].iloc[-1])


if __name__ == "__main__":
    main()
