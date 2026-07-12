#!/usr/bin/env python3
"""Continuable MT5 parity diagnostics for IntradayVolatilityBreakout."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from validation.importers.mt5_xlsx import MT5XLSXImporter
from strategies.intraday_volatility_breakout.intraday_volatility_breakout import generate_signals
from runner.engines.base import resolve_strategy_parameters
from scripts.regenerate_mt5_parity_artifacts import COMPARISON_CASES, STRATEGY_PATH, _load_ohlcv

MT5_SOURCE_TIMEZONE = "Europe/Athens"


@dataclass(frozen=True)
class CasePaths:
    label: str
    mt5: Path
    platform: Path


MT5_REPORTS = {
    "EURUSD_H1": Path("strategy-comparisons/intraday_volatility_breakout/EURUSD_H1.xlsx"),
    "NAS100_M15": Path("strategy-comparisons/intraday_volatility_breakout/NAS100_M15.xlsx"),
    "SP500_M1": Path("strategy-comparisons/intraday_volatility_breakout/SP500_M1.xlsx"),
}


def _case_by_label() -> dict[str, dict[str, Any]]:
    return {str(case["label"]): dict(case) for case in COMPARISON_CASES}


def _platform_path(label: str) -> Path:
    return Path(_case_by_label()[label]["output"])


def _load_platform(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def summary(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    for label, mt5_path in MT5_REPORTS.items():
        ref = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        platform = _load_platform(_platform_path(label))
        mt5_pnl = float(ref.stats.get("total_pnl") or 0.0)
        platform_pnl = float(platform.get("stats", {}).get("total_pnl") or 0.0)
        mt5_dirs: dict[str, int] = {}
        platform_dirs: dict[str, int] = {}
        for trade in ref.trades:
            mt5_dirs[trade.direction] = mt5_dirs.get(trade.direction, 0) + 1
        for trade in platform.get("trades", []):
            platform_dirs[str(trade.get("direction"))] = platform_dirs.get(str(trade.get("direction")), 0) + 1
        print(
            f"{label}: mt5_trades={len(ref.trades)} platform_trades={len(platform.get('trades', []))} "
            f"trade_delta={len(platform.get('trades', [])) - len(ref.trades)} "
            f"mt5_pnl={mt5_pnl:.2f} platform_pnl={platform_pnl:.2f} pnl_delta={platform_pnl - mt5_pnl:.2f}"
        )
        print(f"  dirs mt5={mt5_dirs} platform={platform_dirs}")


def entry_minutes(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    for label, mt5_path in MT5_REPORTS.items():
        ref = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        times = pd.to_datetime([trade.entry_time for trade in ref.trades], utc=True, errors="coerce")
        minutes = pd.Series(times.hour * 60 + times.minute)
        print(f"\n{label} entry minutes")
        print(minutes.value_counts().sort_index().to_string())


def stop_atr(args: argparse.Namespace) -> None:
    importer = MT5XLSXImporter()
    cases = _case_by_label()
    params = resolve_strategy_parameters(STRATEGY_PATH, {"session_timezone_offset_hours": args.session_offset})
    for label, mt5_path in MT5_REPORTS.items():
        ref = importer.load(mt5_path, source_timezone=MT5_SOURCE_TIMEZONE)
        case = cases[label]
        df = _load_ohlcv(args.data_dir, case["execution_symbol"], case["execution_timeframe"], case["start"], case["end"])
        entries, exits, indicators, _ = generate_signals(df, params)
        mt5_rows: list[dict[str, Any]] = []
        our_atr = indicators["daily_atr"]
        our_stop_distance = abs(pd.to_numeric(indicators["sl_price"], errors="coerce") - df["close"])
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        by_ts = pd.Series(df.index, index=ts)
        for trade in ref.trades[: args.limit]:
            sl = trade.meta.get("stop_loss")
            if sl is None:
                continue
            entry_ts = pd.Timestamp(trade.entry_time)
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.tz_localize("UTC")
            else:
                entry_ts = entry_ts.tz_convert("UTC")
            idx = by_ts.index.searchsorted(entry_ts)
            if idx >= len(by_ts):
                continue
            row_index = int(by_ts.iloc[idx])
            mt5_stop_distance = abs(float(trade.entry_price) - float(sl))
            mt5_atr = mt5_stop_distance / 0.25
            mt5_rows.append(
                {
                    "entry_time": trade.entry_time,
                    "dir": trade.direction,
                    "mt5_entry": trade.entry_price,
                    "mt5_sl": sl,
                    "mt5_stop": mt5_stop_distance,
                    "mt5_atr": mt5_atr,
                    "our_time": ts.iloc[row_index].isoformat(),
                    "our_close": float(df["close"].iloc[row_index]),
                    "our_atr": float(our_atr.iloc[row_index]) if pd.notna(our_atr.iloc[row_index]) else None,
                    "our_stop_at_entry_signal": float(our_stop_distance.iloc[row_index]) if pd.notna(our_stop_distance.iloc[row_index]) else None,
                }
            )
        print(f"\n{label} first {len(mt5_rows)} MT5 stop-implied ATR rows")
        for row in mt5_rows:
            ratio = row["our_atr"] / row["mt5_atr"] if row["our_atr"] and row["mt5_atr"] else None
            our_atr_text = f"{row['our_atr']:.6g}" if row["our_atr"] is not None else "None"
            ratio_text = f"{ratio:.3f}" if ratio is not None else "None"
            print(
                f"{row['entry_time']} {row['dir']} mt5_atr={row['mt5_atr']:.6g} "
                f"our_atr={our_atr_text} ratio={ratio_text} "
                f"mt5_entry={row['mt5_entry']} our_close={row['our_close']}"
            )


def signal_counts(args: argparse.Namespace) -> None:
    cases = _case_by_label()
    for label, case in cases.items():
        df = _load_ohlcv(args.data_dir, case["execution_symbol"], case["execution_timeframe"], case["start"], case["end"])
        print(f"\n{label}")
        for offset in args.offsets:
            for use_close in (True, False):
                params = resolve_strategy_parameters(
                    STRATEGY_PATH,
                    {
                        "session_timezone_offset_hours": offset,
                        "use_candle_close": use_close,
                    },
                )
                entries, exits, indicators, _ = generate_signals(df, params)
                dirs = indicators["direction"].dropna().value_counts().to_dict()
                print(f"offset={offset:+g} use_close={use_close} entries={int(entries.sum())} dirs={dirs}")




def data_quality(args: argparse.Namespace) -> None:
    cases = _case_by_label()
    importer = MT5XLSXImporter()
    for label, case in cases.items():
        df = _load_ohlcv(args.data_dir, case["execution_symbol"], case["execution_timeframe"], case["start"], case["end"])
        ohlc_cols = ["open", "high", "low", "close"]
        print(f"\n{label} data quality {case['execution_symbol']} {case['execution_timeframe']} rows={len(df)}")
        print("time", df["timestamp"].min(), df["timestamp"].max())
        for col in ohlc_cols:
            print(col, "nan", int(pd.to_numeric(df[col], errors="coerce").isna().sum()))
        ref = importer.load(MT5_REPORTS[label], source_timezone=MT5_SOURCE_TIMEZONE)
        times = pd.to_datetime([trade.entry_time for trade in ref.trades], utc=True, errors="coerce")
        ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        ts_index = pd.Series(df.index, index=ts)
        for offset in args.offsets:
            found = 0
            found_valid = 0
            price_diffs = []
            for trade, mt5_time in zip(ref.trades, times, strict=False):
                query_time = mt5_time - pd.Timedelta(hours=offset)
                pos = ts_index.index.searchsorted(query_time)
                if pos >= len(ts_index):
                    continue
                row_index = int(ts_index.iloc[pos])
                row_time = ts.iloc[row_index]
                if abs((row_time - query_time).total_seconds()) > args.tolerance_seconds:
                    continue
                found += 1
                close = pd.to_numeric(pd.Series([df["close"].iloc[row_index]]), errors="coerce").iloc[0]
                if pd.notna(close):
                    found_valid += 1
                    price_diffs.append(float(close) - float(trade.entry_price))
            avg_diff = sum(price_diffs) / len(price_diffs) if price_diffs else None
            print(
                f"mt5_time_minus_offset={offset:+g}h matched={found}/{len(ref.trades)} "
                f"valid_close={found_valid} avg_close_minus_mt5_entry={avg_diff}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("/mnt/block-volume-data"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("summary").set_defaults(func=summary)
    sub.add_parser("entry-minutes").set_defaults(func=entry_minutes)
    p_stop = sub.add_parser("stop-atr")
    p_stop.add_argument("--session-offset", type=float, default=4.0)
    p_stop.add_argument("--limit", type=int, default=8)
    p_stop.set_defaults(func=stop_atr)
    p_counts = sub.add_parser("signal-counts")
    p_counts.add_argument("--offsets", type=float, nargs="+", default=[0, 1, 2, 3, 4])
    p_counts.set_defaults(func=signal_counts)
    p_quality = sub.add_parser("data-quality")
    p_quality.add_argument("--offsets", type=float, nargs="+", default=[-4, -3, -2, -1, 0, 1, 2, 3, 4])
    p_quality.add_argument("--tolerance-seconds", type=int, default=60)
    p_quality.set_defaults(func=data_quality)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
