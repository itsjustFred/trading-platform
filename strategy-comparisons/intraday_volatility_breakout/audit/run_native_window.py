"""Run the native engine on an MT5 audit window and score it against the tester trades.

Reproduces the "Platform-Backtests" table of the audit README: the MT5 M1 CSV export of
the tester run is the price feed (server clock -> UTC through ``shared.broker_clock``),
the MT5-logged daily ATR is injected (``Stop / Stop_ATR_Multiplier`` of the EA's
``Levels calculated`` line), and the native engine runs with fixed-risk sizing from the
ATR stop distance and the bid/ask spread model (``spread_price_col``).

  .venv/bin/python strategy-comparisons/intraday_volatility_breakout/audit/run_native_window.py \
      --ref-dir data/audit_ref --symbols nas100 sp500 [--signal-exit-fill next_open|close] \
      [--strategy-file <older strategy .py to compare against>]

Needs the references from ``parse_mt5_reference.py`` (``--ref-dir``) and the tester CSV/log
under ``/home/ubuntu/trading-platform-references`` (override with ``--references``).
Prints per symbol: matched trades, same entry/exit minute and exit price vs the MT5 tester.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import sys
import tempfile

import pandas as pd

REPO = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
for entry in (str(REPO), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import score_parity as sp  # noqa: E402
from runner.engine import TradeEngine  # noqa: E402
from runner.engines.base import BacktestConfig  # noqa: E402
from shared.broker_clock import resolve_broker_clock  # noqa: E402

DEFAULT_STRATEGY = REPO / "strategies" / "intraday_volatility_breakout" / "intraday_volatility_breakout.py"
DEFAULT_REFERENCES = pathlib.Path("/home/ubuntu/trading-platform-references")
CASES = {
    "eurusd": ("mt5-eurusd-h1", "EURUSD_M1_*.csv", "eurusd_h1.log", "dukascopy_mt5_forex"),
    "nas100": ("mt5-nas100-m15", "USATECH.IDX_M1_*.csv", "nas100_m15.log", "dukascopy_mt5_index"),
    "sp500": ("mt5-sp500-m1", "USA500.IDX_M1_*.csv", "sp500_m1.log", "dukascopy_mt5_index"),
}
STOP_ATR_MULTIPLIER = 0.25
LEVELS_RE = re.compile(
    r"(\d{4}\.\d{2}\.\d{2}) \d{2}:\d{2}:\d{2}\s+Levels calculated \|.*\| Stop: ([\d.]+)"
)


def _load_strategy(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location("ivb_audit_strategy", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _logged_atr(log_path: pathlib.Path) -> pd.Series:
    """Daily ATR the EA used (one ``Levels calculated`` line per trading day)."""
    text = log_path.read_bytes().decode("utf-16", errors="replace")
    atr = {
        pd.Timestamp(day.replace(".", "-")): float(stop) / STOP_ATR_MULTIPLIER
        for day, stop in LEVELS_RE.findall(text)
    }
    return pd.Series(atr).sort_index()


def _load_feed(csv_path: pathlib.Path, clock) -> pd.DataFrame:
    raw = pd.read_csv(csv_path, sep="\t", dtype={"<OPEN>": str})
    raw.columns = [c.strip("<>").lower() for c in raw.columns]
    server = pd.to_datetime(raw["date"] + " " + raw["time"], format="%Y.%m.%d %H:%M:%S")
    decimals = int(raw["open"].str.split(".").str[1].str.len().max())
    frame = pd.DataFrame(
        {
            "timestamp": clock.to_utc(pd.DatetimeIndex(server)),
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "volume": raw["tickvol"].astype(float),
            "spread_price": raw["spread"].astype(float) * 10.0**-decimals,
        }
    )
    return frame.reset_index(drop=True)


def _inject_atr(mod, atr_by_day: pd.Series, period: int):
    """Make the strategy's ``atr_period`` ATR the EA's logged one (MT5's history is longer)."""
    original = mod._daily_atr

    def patched(true_range, window, smoothing):
        if window != period:
            return original(true_range, window, smoothing)
        index = pd.DatetimeIndex(pd.to_datetime(true_range.index)).tz_localize(None).normalize()
        values = atr_by_day.reindex(index).shift(-1)  # the strategy shifts it back by one day
        return pd.Series(values.to_numpy(), index=true_range.index)

    mod._daily_atr = patched


def run_case(key: str, args) -> tuple[dict, pd.DataFrame]:
    folder, csv_glob, log_name, clock_name = CASES[key]
    clock = resolve_broker_clock(clock_name)
    ref_dir = args.references / folder
    csv_path = next(ref_dir.glob(csv_glob))
    frame = _load_feed(csv_path, clock)
    mod = _load_strategy(args.strategy_file)
    params = mod.STRATEGY_META["parameters"].copy()
    _inject_atr(mod, _logged_atr(ref_dir / log_name), int(params["atr_period"]))
    params.update({"broker_clock": clock_name, "confirmation_timeframe": "15m"})

    def strategy_fn(df, _params):
        return mod.generate_signals(df, params)

    config = BacktestConfig(
        strategy_name="ivb-audit-window",
        symbols=["T"],
        timeframe="1m",
        start_date=str(frame["timestamp"].min().date()),
        end_date=str(frame["timestamp"].max().date()),
        initial_capital=100_000.0,
        risk_per_trade_pct=0.01,
        max_leverage=100.0,
        modeling_resolution="main_timeframe",
        signal_exit_fill=args.signal_exit_fill,
        parameters={
            "fees": "zero",
            "fixed_risk_amount": 500.0,
            "spread_price_col": "spread_price",
        },
    )
    result = TradeEngine(config, symbol="T").run(frame, strategy_fn)

    trades = []
    for trade in result.trades:
        last = trade.exits[-1]
        trades.append(
            {
                "direction": trade.direction,
                "entry_time": trade.entry_time,
                "entry_price": trade.entry_price,
                "quantity": trade.quantity / sp.CONTRACT[key],  # engine units -> lots
                "meta": {"sl_price": (trade.meta or {}).get("sl_price")},
                "exits": [{"time": last.time, "price": last.price, "reason": last.reason}],
            }
        )
    with tempfile.TemporaryDirectory() as tmp:
        artifact = pathlib.Path(tmp) / "pf.json"
        artifact.write_text(json.dumps({"trades": trades}), encoding="utf-8")
        pf = sp.load_platform(artifact, key, clock_name)
    mt5 = sp.load_mt5(args.ref_dir, key)
    first, last = clock.to_server_time(pd.DatetimeIndex([frame["timestamp"].min(), frame["timestamp"].max()]))
    window = (mt5["entry_server"] >= first) & (mt5["entry_server"] <= last)
    mt5 = mt5[window].reset_index(drop=True)
    summary, merged = sp.score(key, mt5, pf)
    return summary, merged


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ref-dir", type=pathlib.Path, required=True)
    parser.add_argument("--references", type=pathlib.Path, default=DEFAULT_REFERENCES)
    parser.add_argument("--symbols", nargs="*", default=list(CASES), choices=list(CASES))
    parser.add_argument("--signal-exit-fill", default="next_open")
    parser.add_argument("--strategy-file", type=pathlib.Path, default=DEFAULT_STRATEGY)
    args = parser.parse_args()

    rows = [run_case(key, args)[0] for key in args.symbols]
    print(pd.DataFrame(rows).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
