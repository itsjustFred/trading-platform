"""Convert raw Dukascopy m1 .bi5 day files into the two-sided dataset frames.

Ported from the out-of-repo pipeline (``~/tools/dukascopy-dl/convert.py``) so
the weekly update runs entirely in-repo. Conventions are unchanged:

- Flat filler minutes (volume 0 and O=H=L=C) are dropped.
- Higher timeframes are resampled from M1, left-closed and left-labelled,
  aligned to 00:00 UTC (H4 bars start at 00/04/08/12/16/20 UTC). Bins without
  any traded minute are omitted.
- W1 bars run Sunday 00:00 UTC to the next Sunday 00:00 UTC and are labelled
  with the starting Sunday.
- BID/ASK are outer-joined on timestamp; each side keeps its own OHLCV columns
  (``bid_open`` … ``ask_volume``), zstd-compressed parquet.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# Dukascopy datafeed instrument code -> (canonical symbol, decimal factor).
# The canonical names and factors mirror manifest.json in the published dataset.
INSTRUMENTS: dict[str, tuple[str, int]] = {
    "EURUSD": ("EURUSD", 100000),
    "GBPUSD": ("GBPUSD", 100000),
    "USDJPY": ("USDJPY", 1000),
    "USDCHF": ("USDCHF", 100000),
    "USDCAD": ("USDCAD", 100000),
    "AUDUSD": ("AUDUSD", 100000),
    "USATECHIDXUSD": ("NAS100", 1000),
    "USA500IDXUSD": ("SP500", 1000),
    "USA30IDXUSD": ("US30", 1000),
    "DEUIDXEUR": ("DAX", 1000),
    "EUSIDXEUR": ("EURO50", 1000),
    "GBRIDXGBP": ("FTSE100", 1000),
    "XAUUSD": ("XAUUSD", 1000),
    "XAGUSD": ("XAGUSD", 1000),
    "BRENTCMDUSD": ("BRENT", 1000),
    "LIGHTCMDUSD": ("CRUDE", 1000),
}

CANONICAL_SYMBOLS: tuple[str, ...] = tuple(
    dict.fromkeys(name for name, _ in INSTRUMENTS.values())
)

REC = np.dtype([("s", ">i4"), ("o", ">i4"), ("c", ">i4"), ("l", ">i4"), ("h", ">i4"), ("v", ">f4")])
# Native-endian view of REC: pandas/numpy take ops reject big-endian buffers.
REC_NATIVE = REC.newbyteorder("<")
AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
TIMEFRAMES = ("M1", "M5", "M15", "H1", "H4", "D1", "W1")
RULES = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D", "W1": "W1"}
assert set(RULES) == set(TIMEFRAMES) - {"M1"}, "RULES must cover every non-M1 timeframe"
OHLCV = ("open", "high", "low", "close", "volume")
SIDE_COLUMNS = [f"{side}_{c}" for side in ("bid", "ask") for c in OHLCV]
M1_COLUMNS = ["timestamp", *SIDE_COLUMNS]


def _empty_side_m1() -> pd.DataFrame:
    df = pd.DataFrame(columns=["timestamp", *OHLCV])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


def decode_bi5_day(raw: bytes, day: date, factor: int) -> pd.DataFrame:
    """Decode one LZMA-compressed daily m1 candle .bi5 payload to a side frame.

    ``raw`` is the decompressed record stream; empty input yields an empty
    frame. Raises on a truncated payload.
    """
    if not raw:
        return _empty_side_m1()
    if len(raw) % REC.itemsize:
        raise ValueError(f"{day}: truncated payload ({len(raw)} bytes)")
    a = np.frombuffer(raw, dtype=REC).byteswap().view(REC_NATIVE)
    day0 = np.datetime64(f"{day.isoformat()}T00:00:00", "s")
    digits = round(math.log10(factor))
    df = pd.DataFrame({
        "timestamp": day0 + a["s"].astype("timedelta64[s]"),
        "open": a["o"], "high": a["h"], "low": a["l"], "close": a["c"],
        "volume": a["v"].astype(np.float64),
    })
    flat = (df.volume == 0) & (df.open == df.high) & (df.high == df.low) & (df.low == df.close)
    df = df.loc[~flat].copy()
    for c in ("open", "high", "low", "close"):
        df[c] = (df[c] / factor).round(digits)
    df["volume"] = df["volume"].round(8)
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_localize("UTC")
    return df.sort_values("timestamp").reset_index(drop=True)


def _bi5_file_date(path: Path) -> date:
    return date(int(path.parent.parent.name), int(path.parent.name), int(path.stem))


def load_m1_side(
    side_dir: Path,
    factor: int,
    *,
    start: date | None = None,
    end: date | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Load per-side M1 frames from a raw ``<SYM>/<SIDE>/<Y>/<M>/<D>.bi5`` tree.

    ``start``/``end`` bound the *file dates* read (``end`` exclusive), so an
    incremental run decodes only the days it fetched. Returns the frame plus
    the sorted list of days whose file was absent or empty.
    """
    import lzma

    parts, missing = [], []
    for f in sorted(side_dir.glob("*/*/*.bi5")):
        day = _bi5_file_date(f)
        if start is not None and day < start:
            continue
        if end is not None and day >= end:
            continue
        raw = f.read_bytes()
        if not raw:
            missing.append(day.isoformat())
            continue
        buf = lzma.decompress(raw)
        parts.append(decode_bi5_day(buf, day, factor))
    if not parts:
        return _empty_side_m1(), missing
    df = pd.concat(parts, ignore_index=True)
    df = df.sort_values("timestamp")
    if df.timestamp.duplicated().any():
        raise ValueError(f"{side_dir}: duplicate timestamps")
    bad = (df.high < df[["open", "close", "low"]].max(axis=1)) | (
        df.low > df[["open", "close"]].min(axis=1)
    )
    if bad.any():
        raise ValueError(f"{side_dir}: {int(bad.sum())} bars with inconsistent OHLC")
    return df.reset_index(drop=True), missing


def resample_side(m1: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a one-sided M1 frame; ``rule`` is a pandas offset or ``W1``."""
    m1 = m1.copy()
    m1["timestamp"] = pd.to_datetime(m1["timestamp"], utc=True)
    s = m1.set_index("timestamp")
    if rule == "W1":
        ts = s.index
        week = ts.normalize() - pd.to_timedelta((ts.dayofweek + 1) % 7, unit="D")
        out = s.groupby(week).agg(AGG)
        out.index.name = "timestamp"
    else:
        out = s.resample(rule, closed="left", label="left").agg(AGG)
        out = out.dropna(subset=["open"])
    out["volume"] = out["volume"].round(8)
    return out.reset_index()


def combine_sides(bid: pd.DataFrame, ask: pd.DataFrame) -> pd.DataFrame:
    """Outer-join per-side frames into the published two-sided column layout."""
    for df in (bid, ask):
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    joined = bid.merge(
        ask,
        on="timestamp",
        how="outer",
        validate="one_to_one",
        suffixes=("_bid", "_ask"),
    )
    rename = {f"{c}_{side}": f"{side}_{c}" for side in ("bid", "ask") for c in OHLCV}
    joined = joined.rename(columns=rename).sort_values("timestamp")
    return joined.reset_index(drop=True)[M1_COLUMNS]


def frames_for(bid_m1: pd.DataFrame, ask_m1: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """All published timeframe frames for one symbol from per-side M1 data."""
    return {
        tf: combine_sides(
            bid_m1 if tf == "M1" else resample_side(bid_m1, RULES[tf]),
            ask_m1 if tf == "M1" else resample_side(ask_m1, RULES[tf]),
        )
        for tf in TIMEFRAMES
    }


def split_combined_m1(combined: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a stored two-sided M1 frame back into per-side M1 frames.

    Rows where a side's ``open`` is NaN simply did not trade that minute on
    that side, so dropping them loses nothing the resamplers could use.
    """
    for side in ("bid", "ask"):
        cols = [f"{side}_{c}" for c in OHLCV]
        na = combined[cols].isna().sum(axis=1)
        if ((na > 0) & (na < len(OHLCV))).any():
            raise ValueError(f"combined M1 has partial-side NaN rows ({side})")
    bid = combined.loc[combined["bid_open"].notna()].copy()
    ask = combined.loc[combined["ask_open"].notna()].copy()

    def _side(frame: pd.DataFrame, side: str) -> pd.DataFrame:
        out = frame[["timestamp", *[f"{side}_{c}" for c in OHLCV]]].rename(
            columns={f"{side}_{c}": c for c in OHLCV}
        )
        return out.reset_index(drop=True)

    return _side(bid, "bid"), _side(ask, "ask")


def week_floor(ts: pd.Timestamp) -> pd.Timestamp:
    """UTC instant of the Sunday 00:00 opening the W1 bin containing ``ts``."""
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.normalize() - pd.to_timedelta((ts.dayofweek + 1) % 7, unit="D")


def bin_floor(tf: str, ts: pd.Timestamp) -> pd.Timestamp:
    """Start of the ``tf`` bin containing ``ts`` (UTC, left-labelled)."""
    if tf == "W1":
        return week_floor(ts)
    rule = RULES[tf]
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    if rule == "1D":
        return ts.normalize()
    return ts.floor(rule)


def file_entry(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"rows": 0}
    return {
        "rows": len(df),
        "first": str(df.timestamp.iloc[0]),
        "last": str(df.timestamp.iloc[-1]),
    }
