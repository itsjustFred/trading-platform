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

Validation is exact: every fixture must contain precisely the expected row
count with the expected first and last timestamps (the ``END`` window is
fixed, so the counts cannot drift). A truncated or empty yfinance answer is
never written — ``--skip-if-valid`` re-runs the same checks, so a partial
cache is refetched. Writes are atomic (temp file + rename).

Exit status 3 means a download failed (CI warns and skips the real-data
parity tests on pull requests); validation failures and any other error exit
1 — they are never a skip (the #334 contract).
"""
from __future__ import annotations

import argparse
import os
import sys
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

#: Exit status that means "a download failed" — CI warns and skips on PRs.
DOWNLOAD_FAILED_EXIT = 3


class FixtureDownloadError(RuntimeError):
    """The yfinance download produced nothing usable; maps to exit 3."""


class FixtureValidationError(RuntimeError):
    """A downloaded frame failed validation; exit status is 1, never a skip."""


def _vendor_root() -> Path:
    override = (os.environ.get("PARITY_VENDOR_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return DATA_DIR / "parity_vendor"


def _vendor_out() -> Path:
    """The ``published/`` directory inside the parity vendor-data cache."""
    return _vendor_root() / "published"


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

#: Per-fixture completeness contract: fixture stem -> (first timestamp,
#: last timestamp, exact row count). The window is fixed, so these counts are
#: stable; they were verified against live yfinance downloads. Note the
#: adjusted series (``Adj Close`` factor) are re-scaled by yfinance after
#: every new distribution — the drift is uniform and <= 3e-4, so counts and
#: dates do not move but prices can shift slightly between fetches.
EXPECTED: dict[str, tuple[str, str, int]] = {
    "spy_1d": ("1993-01-29", "2025-12-30", 8287),
    "efa_1d": ("2001-08-27", "2025-12-30", 6122),
    "veu_1d": ("2007-03-08", "2025-12-30", 4735),
    "agg_1d": ("2003-09-29", "2025-12-30", 5600),
    "bil_1d": ("2007-05-30", "2025-12-30", 4678),
    "gspc_1d": ("1928-01-03", "2025-12-30", 24615),
    "sp500tr_1d": ("1988-01-04", "2025-12-30", 9571),
    "irx_1d": ("1960-01-04", "2025-12-30", 16483),
}

_PRICE_COLUMNS = ("open", "high", "low", "close")


def _expected_columns(ohlc: bool) -> list[str]:
    return ["timestamp", *_PRICE_COLUMNS] if ohlc else ["timestamp", "close"]


def _validate(stem: str, frame: pd.DataFrame, ohlc: bool) -> None:
    """Exact completeness: row count, first and last timestamps, no NaNs."""
    want_first, want_last, want_rows = EXPECTED[stem]
    if list(frame.columns) != _expected_columns(ohlc):
        raise FixtureValidationError(
            f"{stem}: bad columns {list(frame.columns)}"
        )
    if len(frame) != want_rows:
        raise FixtureValidationError(
            f"{stem}: {len(frame)} rows, expected exactly {want_rows}"
        )
    first = str(frame["timestamp"].iloc[0])[:10]
    last = str(frame["timestamp"].iloc[-1])[:10]
    if first != want_first or last != want_last:
        raise FixtureValidationError(
            f"{stem}: window {first}..{last} does not cover "
            f"{want_first}..{want_last}"
        )
    if frame[_expected_columns(ohlc)[1:]].isna().any().any():
        raise FixtureValidationError(f"{stem}: NaN prices in fixture")


def build(name: str, ticker: str, start: str, ohlc: bool, data_dir: Path) -> pd.DataFrame:
    try:
        raw = yf.download(ticker, start=start, end=END, auto_adjust=False, progress=False)
    except Exception as exc:
        raise FixtureDownloadError(f"{name}: yfinance download failed: {exc}") from exc
    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.dropna(subset=["Close"])
    if raw.empty:
        raise FixtureDownloadError(f"{name}: yfinance returned no rows for {ticker}")
    target = data_dir / "ohlcv" / "yfinance" / "equity" / "US" / name
    target.mkdir(parents=True, exist_ok=True)
    full = raw.reset_index().rename(columns=str.lower)
    full = full.rename(columns={"date": "timestamp"})
    full["timestamp"] = pd.to_datetime(full["timestamp"], utc=True)
    full.to_parquet(target / "1d.parquet", index=False)
    unadjusted = name in ("GSPC", "SP500TR", "IRX")
    factor = (
        1.0
        if unadjusted or "Adj Close" not in raw
        else raw["Adj Close"] / raw["Close"]
    )
    out = pd.DataFrame({"timestamp": raw.index.strftime("%Y-%m-%d")})
    if ohlc:
        for col in ("Open", "High", "Low", "Close"):
            out[col.lower()] = (raw[col] * factor).round(4).to_numpy()
    else:
        out["close"] = (raw["Close"] * factor).round(4).to_numpy()
    return out


def _write_frame(path: Path, frame: pd.DataFrame) -> Path:
    """Atomic write: temp file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    frame.to_csv(tmp, index=False, compression="gzip")
    tmp.replace(path)
    return path


def _fixture_valid(path: Path, stem: str, ohlc: bool) -> bool:
    """Full validation for --skip-if-valid: a cached file must pass the same
    completeness checks a fresh download does, or it is refetched."""
    if not path.is_file():
        return False
    try:
        frame = pd.read_csv(path)
        _validate(stem, frame, ohlc)
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        default=None,
        help="where the raw yfinance download is stored (default: the "
        "parity vendor cache root, so nothing lands in the checkout's data/)",
    )
    parser.add_argument(
        "--skip-if-valid",
        action="store_true",
        help="exit 0 without network access when every fixture is already cached",
    )
    args = parser.parse_args()
    out_dir = _vendor_out()
    specs = {
        f"{name.lower()}_1d": (name, ticker, start, ohlc)
        for name, (ticker, start, ohlc) in SPECS.items()
    }
    if args.skip_if_valid and all(
        _fixture_valid(out_dir / f"{stem}.csv.gz", stem, spec[3])
        for stem, spec in specs.items()
    ):
        print("all parity published fixtures are cached and valid; nothing to do")
        return 0
    # The raw download defaults to the vendor cache root (or TRADING_DATA_DIR
    # when set) — never the checkout's data/ unless explicitly asked.
    data_dir = Path(
        args.data_dir
        or os.environ.get("TRADING_DATA_DIR")
        or _vendor_root()
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for stem, (name, ticker, start, ohlc) in specs.items():
            frame = build(name, ticker, start, ohlc, data_dir)
            _validate(stem, frame, ohlc)
            target = _write_frame(out_dir / f"{stem}.csv.gz", frame)
            print(
                name, len(frame), frame["timestamp"].iloc[0],
                frame["timestamp"].iloc[-1], "->", target,
            )
    except FixtureDownloadError as exc:
        print(f"warning: parity published fixture download failed: {exc}", file=sys.stderr)
        return DOWNLOAD_FAILED_EXIT
    except FixtureValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Code bugs must fail loudly, never skip.
        print(f"error: parity published fixture fetch crashed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
