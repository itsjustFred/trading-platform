"""Score platform IntradayVolatilityBreakout artifacts against the MT5 tester trades.

Pairs every MT5 tester trade with the platform trade of the same *server* trading
day and direction (the EA takes at most one trade per day), converting platform
UTC entry times onto the broker's server clock with ``shared.broker_clock``.

  .venv/bin/python strategy-comparisons/intraday_volatility_breakout/audit/score_parity.py \
      --ref-dir <dir with mt5_trades_*.csv from parse_mt5_reference.py> \
      [--artifact eurusd=data/backtests/intraday_volatility_breakout_eurusd_h1.json ...] \
      [--clock eurusd=dukascopy_mt5_forex ...] [--out <dir for per-trade CSVs>]

Per symbol it prints: trades on each side, matched / mt5_only / pf_only, how many
matched trades also share the exact entry minute / exit minute / exit price, and PnL in *price units x lots x
contract* on both sides (the MT5 log runs are USD-denominated; using the same
formula for the platform keeps currency conversion out of the trade comparison).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import pandas as pd

REPO = pathlib.Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from shared.broker_clock import resolve_broker_clock  # noqa: E402

CONTRACT = {"eurusd": 100_000.0, "nas100": 1.0, "sp500": 1.0}
# Exit prices count as equal within this many price units (MT5 logs 3-5 decimals).
EXIT_PRICE_TOLERANCE = 1e-6
DEFAULT_ARTIFACT = {
    "eurusd": "data/backtests/intraday_volatility_breakout_eurusd_h1.json",
    "nas100": "data/backtests/intraday_volatility_breakout_nas100_m15.json",
    "sp500": "data/backtests/intraday_volatility_breakout_sp500_m1.json",
}
DEFAULT_CLOCK = {
    "eurusd": "dukascopy_mt5_forex",
    "nas100": "dukascopy_mt5_index",
    "sp500": "dukascopy_mt5_index",
}


def _pairs(values: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or key not in CONTRACT:
            raise SystemExit(f"expected <{'|'.join(CONTRACT)}>=<value>, got {item!r}")
        out[key] = value
    return out


def load_mt5(ref_dir: pathlib.Path, key: str) -> pd.DataFrame:
    mt5 = pd.read_csv(ref_dir / f"mt5_trades_{key}.csv")
    mt5["entry_server"] = pd.to_datetime(mt5["entry_time"], format="%Y.%m.%d %H:%M:%S")
    mt5["exit_server"] = pd.to_datetime(mt5["exit_time"], format="%Y.%m.%d %H:%M:%S")
    mt5["pnl_px"] = mt5["pnl"].astype(float)
    return mt5


def load_platform(path: pathlib.Path, key: str, clock_name: str) -> pd.DataFrame:
    clock = resolve_broker_clock(clock_name)
    if clock is None:
        raise SystemExit("a broker clock is required to put platform trades on server time")
    trades = json.loads(path.read_text(encoding="utf-8"))["trades"]
    rows = []
    for trade in trades:
        exit_ = trade["exits"][-1]
        rows.append(
            {
                "dir": str(trade["direction"]).upper(),
                "entry_utc": pd.Timestamp(trade["entry_time"]),
                "exit_utc": pd.Timestamp(exit_["time"]),
                "entry": float(trade["entry_price"]),
                "exit": float(exit_["price"]),
                "qty": float(trade["quantity"]),
                "sl": (trade.get("meta") or {}).get("sl_price"),
                "reason": exit_.get("reason"),
            }
        )
    pf = pd.DataFrame(
        rows, columns=["dir", "entry_utc", "exit_utc", "entry", "exit", "qty", "sl", "reason"]
    )
    pf["entry_server"] = clock.to_server_time(pf["entry_utc"])
    pf["exit_server"] = clock.to_server_time(pf["exit_utc"])
    sign = pf["dir"].map({"LONG": 1.0, "SHORT": -1.0})
    pf["pnl_px"] = (pf["exit"] - pf["entry"]) * sign * pf["qty"] * CONTRACT[key]
    return pf


def score(key: str, mt5: pd.DataFrame, pf: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    for frame in (mt5, pf):
        frame["date"] = frame["entry_server"].dt.normalize()
        frame["n"] = frame.groupby(["date", "dir"]).cumcount()
    merged = mt5.merge(
        pf, on=["date", "dir", "n"], how="outer", suffixes=("_mt5", "_pf"), indicator=True
    )
    both = merged[merged["_merge"] == "both"]
    same_minute = both["entry_server_mt5"].dt.floor("min") == both["entry_server_pf"].dt.floor(
        "min"
    )
    same_exit_minute = both["exit_server_mt5"].dt.floor("min") == both["exit_server_pf"].dt.floor(
        "min"
    )
    same_exit_price = (both["exit_mt5"] - both["exit_pf"]).abs() <= EXIT_PRICE_TOLERANCE
    summary = {
        "symbol": key,
        "mt5_trades": len(mt5),
        "pf_trades": len(pf),
        "matched": len(both),
        "mt5_only": int((merged["_merge"] == "left_only").sum()),
        "pf_only": int((merged["_merge"] == "right_only").sum()),
        "matched_same_entry_minute": int(same_minute.sum()),
        "matched_same_exit_minute": int(same_exit_minute.sum()),
        "matched_same_exit_price": int(same_exit_price.sum()),
        "mt5_pnl": round(float(mt5["pnl_px"].sum()), 2),
        "pf_pnl": round(float(pf["pnl_px"].sum()), 2),
        "matched_pnl_delta": round(float((both["pnl_px_pf"] - both["pnl_px_mt5"]).sum()), 2),
    }
    summary["pnl_delta"] = round(summary["pf_pnl"] - summary["mt5_pnl"], 2)
    return summary, merged


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ref-dir", type=pathlib.Path, required=True)
    parser.add_argument("--artifact", action="append", help="<symbol>=<artifact json>")
    parser.add_argument("--clock", action="append", help="<symbol>=<broker clock name>")
    parser.add_argument("--symbols", nargs="*", default=list(CONTRACT))
    parser.add_argument(
        "--out", type=pathlib.Path, help="write <symbol>_pairs.csv with every pair / orphan"
    )
    args = parser.parse_args()
    artifacts = {**DEFAULT_ARTIFACT, **_pairs(args.artifact)}
    clocks = {**DEFAULT_CLOCK, **_pairs(args.clock)}

    rows = []
    for key in args.symbols:
        mt5 = load_mt5(args.ref_dir, key)
        pf = load_platform(pathlib.Path(artifacts[key]), key, clocks[key])
        summary, merged = score(key, mt5, pf)
        rows.append(summary)
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            merged.to_csv(args.out / f"{key}_pairs.csv", index=False)
    print(pd.DataFrame(rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
