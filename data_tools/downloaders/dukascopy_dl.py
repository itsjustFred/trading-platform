"""
Dukascopy Downloader using dukascopy-node CLI.

Downloads CSV via dukascopy-node, converts to Parquet, and stores under:
    ~/data/ohlcv/dukascopy/{asset_class}/{symbol}/{timeframe}.parquet
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data_tools.downloaders.base import DownloadResult, Downloader, SourceMeta
from shared.constants import DATA_DIR, resolve_executable, resolve_genesis_path

logger = logging.getLogger(__name__)

SOURCE_META_PATH = DATA_DIR / "source_meta.json"
GENESIS_PATH = resolve_genesis_path()

# Supported symbols for this project with dukascopy-node instrument keys.
SYMBOL_CONFIG: dict[str, dict[str, str]] = {
    # Forex
    "EURUSD": {"instrument": "eurusd", "asset_class": "forex"},
    "GBPUSD": {"instrument": "gbpusd", "asset_class": "forex"},
    "USDJPY": {"instrument": "usdjpy", "asset_class": "forex"},
    "AUDUSD": {"instrument": "audusd", "asset_class": "forex"},
    "USDCAD": {"instrument": "usdcad", "asset_class": "forex"},
    "USDCHF": {"instrument": "usdchf", "asset_class": "forex"},
    # Metals
    "XAUUSD": {"instrument": "xauusd", "asset_class": "metals"},
    "XAGUSD": {"instrument": "xagusd", "asset_class": "metals"},
    # Indices
    "US30": {"instrument": "usa30idxusd", "asset_class": "indices"},
    "US500": {"instrument": "usa500idxusd", "asset_class": "indices"},
    "USTEC": {"instrument": "usatechidxusd", "asset_class": "indices"},
    # Energy
    "UKOIL": {"instrument": "brentcmdusd", "asset_class": "energy"},
    "USOIL": {"instrument": "lightcmdusd", "asset_class": "energy"},
}

TIMEFRAME_MAP: dict[str, tuple[str, str]] = {
    "tick": ("tick", "tick"),
    "sec": ("s1", "sec"),
    "s1": ("s1", "sec"),
    "m1": ("m1", "1m"),
    "1m": ("m1", "1m"),
    "m5": ("m5", "5m"),
    "5m": ("m5", "5m"),
    "m15": ("m15", "15m"),
    "15m": ("m15", "15m"),
    "m30": ("m30", "30m"),
    "30m": ("m30", "30m"),
    "h1": ("h1", "1h"),
    "1h": ("h1", "1h"),
    "h4": ("h4", "4h"),
    "4h": ("h4", "4h"),
    "d1": ("d1", "1d"),
    "1d": ("d1", "1d"),
    "mn1": ("mn1", "mn1"),
}


def _normalize_genesis_timeframe(timeframe: str) -> str:
    normalized = timeframe.strip().lower()
    aliases = {
        "sec": "s1",
        "1m": "m1",
        "5m": "m5",
        "15m": "m15",
        "30m": "m30",
        "1h": "h1",
        "4h": "h4",
        "1d": "d1",
    }
    return aliases.get(normalized, normalized)


def _load_source_meta_index() -> dict:
    if SOURCE_META_PATH.exists():
        try:
            return json.loads(SOURCE_META_PATH.read_text())
        except Exception as exc:
            logger.warning("Could not read %s: %s", SOURCE_META_PATH, exc)
    return {}


def _load_genesis_index() -> dict[str, dict[str, str]]:
    if GENESIS_PATH.exists():
        try:
            payload = json.loads(GENESIS_PATH.read_text())
            if isinstance(payload, dict):
                return payload
        except Exception as exc:
            logger.warning("Could not read %s: %s", GENESIS_PATH, exc)
    return {}


def _write_source_meta_index(index: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCE_META_PATH.write_text(json.dumps(index, indent=2, sort_keys=True))


def _to_timestamp_series(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="ms", utc=True, errors="coerce")
    return pd.to_datetime(series, utc=True, errors="coerce")


def _guess_asset_class(instrument_key: str, normalized_symbol: str) -> str:
    inst = instrument_key.lower()
    sym = normalized_symbol.upper()
    if "idx" in inst:
        return "indices"
    if "cmd" in inst or sym in {"UKOIL", "USOIL"}:
        return "energy"
    if sym.startswith("XAU") or sym.startswith("XAG"):
        return "metals"
    return "forex"


def _resolve_symbol_and_instrument(raw_symbol: str) -> tuple[str, str, str]:
    key = raw_symbol.strip().upper()
    if key in SYMBOL_CONFIG:
        cfg = SYMBOL_CONFIG[key]
        return key, cfg["instrument"], cfg["asset_class"]

    # Fallback: treat user input as dukascopy-node instrument identifier.
    cleaned_instrument = "".join(ch for ch in raw_symbol.strip().lower() if ch.isalnum())
    normalized_symbol = "".join(ch for ch in key if ch.isalnum())
    if not cleaned_instrument:
        raise ValueError(f"Invalid Dukascopy symbol/instrument input: {raw_symbol!r}")
    asset_class = _guess_asset_class(
        cleaned_instrument, normalized_symbol or cleaned_instrument.upper()
    )
    return (normalized_symbol or cleaned_instrument.upper()), cleaned_instrument, asset_class


def _resolve_requested_start(
    symbol: str,
    timeframe: str,
    requested_start: str | None,
    genesis_index: dict[str, dict[str, str]],
) -> str:
    if requested_start:
        return requested_start
    symbol_data = genesis_index.get(symbol.upper(), {})
    normalized_timeframe = _normalize_genesis_timeframe(timeframe)
    return symbol_data.get(normalized_timeframe) or symbol_data.get("default") or "2000-01-01"


def _dukascopy_command(
    instrument: str,
    timeframe: str,
    price_type: str,
    requested_start: str,
    requested_end: str,
    output_dir: Path,
) -> list[str]:
    return [
        resolve_executable("dukascopy-node"),
        "-i",
        instrument,
        "-t",
        timeframe,
        "-p",
        price_type,
        "-from",
        requested_start,
        "-to",
        requested_end,
        "-f",
        "csv",
        "-dir",
        str(output_dir),
        "-v",
        "--silent",
    ]


def _run_dukascopy_export(
    tmp_root: str,
    instrument: str,
    timeframe: str,
    price_type: str,
    requested_start: str,
    requested_end: str,
) -> Path:
    export_dir = Path(tmp_root) / price_type
    export_dir.mkdir(parents=True, exist_ok=True)
    cmd = _dukascopy_command(
        instrument=instrument,
        timeframe=timeframe,
        price_type=price_type,
        requested_start=requested_start,
        requested_end=requested_end,
        output_dir=export_dir,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True)
    cmd_output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    if proc.returncode != 0:
        tail = "\n".join(cmd_output.strip().splitlines()[-15:])
        raise RuntimeError(
            f"dukascopy-node failed for {instrument} {timeframe} ({price_type}):\n{tail}"
        )

    csv_files = sorted(export_dir.glob("*.csv"))
    if not csv_files:
        raise RuntimeError(
            f"No CSV output generated by dukascopy-node for {instrument} {timeframe} ({price_type})"
        )
    return max(csv_files, key=lambda p: p.stat().st_mtime_ns)


def _load_dukascopy_csv(csv_path: Path, *, symbol: str, timeframe: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    required = {"timestamp", "open", "high", "low", "close"}
    if not required.issubset(set(df.columns)):
        raise RuntimeError(f"CSV missing OHLC columns for {symbol} {timeframe}: {list(df.columns)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0

    df["timestamp"] = _to_timestamp_series(df["timestamp"])
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp", "open", "high", "low", "close"])
    df["volume"] = df["volume"].fillna(0.0)
    return df.sort_values("timestamp").drop_duplicates(subset=["timestamp"]).reset_index(drop=True)


def _end_boundary(date_value: str) -> pd.Timestamp:
    end_ts = pd.Timestamp(date_value, tz="UTC")
    if end_ts.time() == datetime.min.time():
        end_ts += pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return end_ts


def _should_merge_bid_ask(
    timeframe: str,
    *,
    merge_tick: bool,
    merge_s1: bool,
    merge_bid_ask_legacy: bool,
) -> bool:
    normalized = _normalize_genesis_timeframe(timeframe)
    if normalized == "tick":
        return merge_tick or merge_bid_ask_legacy
    if normalized == "s1":
        return merge_s1 or merge_bid_ask_legacy
    return False


class DukascopyDownloader(Downloader):
    @property
    def source_name(self) -> str:
        return "dukascopy"

    def list_available(self, **kwargs) -> list[dict]:
        return [
            {
                "symbol": symbol,
                "instrument": cfg["instrument"],
                "asset_class": cfg["asset_class"],
                "timeframe": "tick,sec,m1,m5,m15,m30,h1,h4,d1,mn1",
            }
            for symbol, cfg in SYMBOL_CONFIG.items()
        ]

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
        sym_input = symbol
        tf = timeframe.lower()
        if tf not in TIMEFRAME_MAP:
            raise ValueError(
                f"Unsupported timeframe: {timeframe}. Use one of: {list(TIMEFRAME_MAP)}"
            )

        sym, instrument, asset_class = _resolve_symbol_and_instrument(sym_input)
        duk_tf, out_tf = TIMEFRAME_MAP[tf]
        price_type = str(kwargs.get("price_type", "bid")).lower()
        if price_type not in {"bid", "ask"}:
            raise ValueError("price_type must be 'bid' or 'ask'")
        merge_bid_ask = _should_merge_bid_ask(
            duk_tf,
            merge_tick=bool(kwargs.get("merge_tick", False)),
            merge_s1=bool(kwargs.get("merge_s1", False)),
            merge_bid_ask_legacy=bool(kwargs.get("merge_bid_ask", False)),
        )

        genesis_index = _load_genesis_index()
        requested_start = _resolve_requested_start(sym, duk_tf, start, genesis_index)
        requested_end = end or datetime.now(timezone.utc).date().isoformat()

        logger.info(
            "Starting dukascopy-node download: symbol=%s instrument=%s timeframe=%s price_type=%s merge_bid_ask=%s requested_start=%s requested_end=%s",
            sym,
            instrument,
            tf,
            price_type,
            merge_bid_ask,
            requested_start,
            requested_end,
        )

        with tempfile.TemporaryDirectory(prefix="dukascopy-node-") as tmp_dir:
            warnings: list[str] = [f"requested_range:{requested_start}->{requested_end}"]
            if merge_bid_ask:
                bid_csv = _run_dukascopy_export(
                    tmp_dir, instrument, duk_tf, "bid", requested_start, requested_end
                )
                ask_csv = _run_dukascopy_export(
                    tmp_dir, instrument, duk_tf, "ask", requested_start, requested_end
                )
                bid_df = _load_dukascopy_csv(bid_csv, symbol=sym, timeframe=tf)
                ask_df = _load_dukascopy_csv(ask_csv, symbol=sym, timeframe=tf)
                df = (
                    pd.merge(
                        bid_df,
                        ask_df,
                        on="timestamp",
                        how="inner",
                        suffixes=("_bid", "_ask"),
                    )
                    .sort_values("timestamp")
                    .reset_index(drop=True)
                )
                warnings.append("price_type:bid+ask")
                if len(df) != len(bid_df) or len(df) != len(ask_df):
                    warnings.append(
                        f"timestamp_overlap:{len(df)}/{len(bid_df)} bid rows, {len(ask_df)} ask rows"
                    )
            else:
                csv_path = _run_dukascopy_export(
                    tmp_dir, instrument, duk_tf, price_type, requested_start, requested_end
                )
                df = _load_dukascopy_csv(csv_path, symbol=sym, timeframe=tf)
                warnings.append(f"price_type:{price_type}")

            start_ts = pd.Timestamp(requested_start, tz="UTC")
            end_ts = _end_boundary(requested_end)
            df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)].reset_index(
                drop=True
            )

            if df.empty:
                raise RuntimeError(f"No data rows in requested range for {sym} {tf}")

            out_path = self._make_output_path(output_dir, asset_class, sym, out_tf)
            df.to_parquet(out_path, index=False)

        meta_key = f"{self.source_name}/{asset_class}/{sym}/{out_tf}"
        source_meta = self.get_source_meta().to_dict()
        source_meta.update(
            {
                "instrument": instrument,
                "price_type": "bid+ask" if merge_bid_ask else price_type,
                "merge_bid_ask": merge_bid_ask,
                "requested_start": requested_start,
                "requested_end": requested_end,
            }
        )
        meta_index = _load_source_meta_index()
        meta_index[meta_key] = source_meta
        _write_source_meta_index(meta_index)

        logger.info(
            "✅ %s %s: %d rows (%s → %s) saved to %s",
            sym,
            out_tf,
            len(df),
            df["timestamp"].iloc[0],
            df["timestamp"].iloc[-1],
            out_path,
        )
        return DownloadResult(
            symbol=sym,
            timeframe=out_tf,
            source=self.source_name,
            asset_class=asset_class,
            path=out_path,
            rows=len(df),
            start=str(df["timestamp"].iloc[0]),
            end=str(df["timestamp"].iloc[-1]),
            size_bytes=out_path.stat().st_size,
            warnings=warnings,
        )

    def get_source_meta(self) -> SourceMeta:
        return SourceMeta(
            name=self.source_name,
            url="https://www.dukascopy-node.app",
            license="free",
            update_frequency="manual",
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download OHLCV data from Dukascopy via dukascopy-node"
    )
    parser.add_argument(
        "--symbols", required=False, help="Comma-separated symbols (e.g. EURUSD,GBPUSD)"
    )
    parser.add_argument(
        "--timeframes",
        default="d1",
        help="Comma-separated timeframes (tick,sec,m1,m5,m15,m30,h1,h4,d1,mn1)",
    )
    parser.add_argument(
        "--price-type", default="bid", choices=["bid", "ask"], help="Price side for Dukascopy data"
    )
    parser.add_argument(
        "--merge-bid-ask",
        action="store_true",
        help="Legacy flag: merge bid and ask for high-resolution Dukascopy downloads",
    )
    parser.add_argument(
        "--merge-tick", action="store_true", help="Merge bid and ask for tick downloads"
    )
    parser.add_argument(
        "--merge-s1", action="store_true", help="Merge bid and ask for 1-second downloads"
    )
    parser.add_argument("--start", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="End date (YYYY-MM-DD)")
    parser.add_argument("--output", default=str(DATA_DIR), help="Output root directory")
    parser.add_argument("--list", action="store_true", help="List available instruments")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    dl = DukascopyDownloader()

    if args.list:
        for item in dl.list_available():
            print(f"{item['symbol']:10s} {item['instrument']:16s} {item['asset_class']}")
        return

    if not args.symbols:
        parser.error("--symbols is required (or use --list)")

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    output_dir = Path(args.output)

    completed = 0
    for sym in symbols:
        for tf in timeframes:
            try:
                result = dl.download(
                    sym,
                    tf,
                    output_dir,
                    start=args.start,
                    end=args.end,
                    price_type=args.price_type,
                    merge_tick=args.merge_tick,
                    merge_s1=args.merge_s1,
                    merge_bid_ask=args.merge_bid_ask,
                )
                completed += 1
                print(
                    f"✅ {result.symbol} {result.timeframe}: {result.rows} rows "
                    f"({result.start} → {result.end}) → {result.path}"
                )
            except Exception as exc:
                print(f"❌ {sym} {tf}: {exc}")

    print(f"\nDone: {completed} downloads completed.")


if __name__ == "__main__":
    main()
