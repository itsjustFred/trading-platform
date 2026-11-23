"""Rebuild the cached fixtures of the published-strategy parity tests.

Downloads daily data with yfinance, stores the raw download under
``<data_dir>/ohlcv/yfinance/...`` (gitignored, catalog layout) and writes the
small gzip CSV fixtures to the vendor-data cache the parity tests read
(``$PARITY_VENDOR_DATA_DIR/published``, or ``$DATA_DIR/parity_vendor/published``
when unset — see ``tests/parity/vendor_data.py``). Vendor data is licensed to
be queried, not redistributed, so the fixtures are cached, not committed; a
missing file skips on pull requests and fails when
``REQUIRE_PARITY_VENDOR_DATA`` is set (pushes to main).

Total-return handling: ``SPY``/``EFA``/``AGG``/``BIL``/``VEU`` fixtures hold
DIVIDEND-ADJUSTED prices (``Adj Close`` scale factor applied to open/high/low/
close, a total-return series).  ``GSPC`` (S&P 500 price index, 1928-) and
``SP500TR`` (S&P 500 total return index, 1988-) are index levels; ``IRX`` is the
13-week T-bill yield in percent (used only by the report's cash-leg estimate).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of reading a different tree's constants and writing the cache to a
# path that tree resolves elsewhere. See tests/test_script_import_origin.py; run as:
#     python -m scripts.build_parity_published_fixtures
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("build_parity_published_fixtures")
# --- end import-origin guard ---
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

from shared.constants import DATA_DIR  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _vendor_out() -> Path:
    """The ``published/`` directory inside the parity vendor-data cache."""
    override = (os.environ.get("PARITY_VENDOR_DATA_DIR") or "").strip()
    root = Path(override).expanduser() if override else DATA_DIR / "parity_vendor"
    return root / "published"

#: symbol -> (yfinance ticker, first date kept, ohlc?)
SPECS = {
    "SPY": ("SPY", "1993-01-01", True),
    "EFA": ("EFA", "2001-01-01", False),
    "VEU": ("VEU", "2007-01-01", False),
    "AGG": ("AGG", "2003-01-01", False),
    "BIL": ("BIL", "2007-01-01", False),
    "GSPC": ("^GSPC", "1928-01-01", False),
    "SP500TR": ("^SP500TR", "1988-01-01", False),
    "IRX": ("^IRX", "1960-01-01", False),  # 13-week T-bill yield, percent (report only)
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
    factor = (raw["Adj Close"] / raw["Close"]) if "Adj Close" in raw and name not in ("GSPC", "SP500TR", "IRX") else 1.0
    out = pd.DataFrame({"timestamp": raw.index.strftime("%Y-%m-%d")})
    if ohlc:
        for col in ("Open", "High", "Low", "Close"):
            out[col.lower()] = (raw[col] * factor).round(4).to_numpy()
    else:
        out["close"] = (raw["Close"] * factor).round(4).to_numpy()
    return out


def _fixture_valid(path: Path) -> bool:
    """Cheap presence+shape check for --skip-if-valid."""
    if not path.is_file():
        return False
    try:
        frame = pd.read_csv(path, nrows=5)
        return {"timestamp", "close"} <= set(frame.columns)
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.environ.get("TRADING_DATA_DIR", "data"))
    parser.add_argument(
        "--skip-if-valid",
        action="store_true",
        help="exit 0 without network access when every fixture is already cached",
    )
    args = parser.parse_args()
    out_dir = _vendor_out()
    if args.skip_if_valid and all(
        _fixture_valid(out_dir / f"{name.lower()}_1d.csv.gz") for name in SPECS
    ):
        print("all parity published fixtures are cached and valid; nothing to do")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, (ticker, start, ohlc) in SPECS.items():
        frame = build(name, ticker, start, ohlc, Path(args.data_dir))
        target = out_dir / f"{name.lower()}_1d.csv.gz"
        frame.to_csv(target, index=False, compression="gzip")
        print(
            name, len(frame), frame["timestamp"].iloc[0],
            frame["timestamp"].iloc[-1], "->", target,
        )


if __name__ == "__main__":
    main()
