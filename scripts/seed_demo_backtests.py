from datetime import datetime, timezone

import pandas as pd

import sys
from pathlib import Path
# --- import-origin guard -------------------------------------------------
# Running a script puts `scripts/` on `sys.path[0]`, not the repo root, so `import shared` does not
# find the checkout this file lives in: it falls through to the installed package, which points at a
# *different* tree. Measured on 2026-10-04 - the two are genuinely different revisions (md5
# 6a72c6cb4dd4 vs 3b3f3d27182e), and the installed one has a *shorter migration set*, so a script
# run
# this way applies an older schema than the code believes and then reports success.
#
# This is the failure `continuation/merge-rehearsal.sh` exists to catch - its own header calls it
# "passed against the wrong data" - and the rehearsal guards itself while the scripts did not.
# Refused rather than silently corrected: quietly importing a different tree than the one being
# edited is the hazard, not the fix. Run from the repo root as `python -m
# scripts.seed_demo_backtests` to use this
# checkout.
import shared as _shared_package

_EXPECTED_PKG = Path(__file__).resolve().parent.parent / "shared"
_ACTUAL_PKG = Path(next(iter(_shared_package.__path__))).resolve()
if _ACTUAL_PKG != _EXPECTED_PKG:
    print(
        "refusing to run: `shared` was imported from the wrong tree.\n"
        f"  this script lives in : {Path(__file__).resolve().parent.parent}\n"
        f"  expected shared/     : {_EXPECTED_PKG}\n"
        f"  actually imported    : {_ACTUAL_PKG}\n"
        "\n"
        "Running a script puts scripts/ on sys.path[0], not the repo root, so `import shared`\n"
        "falls through to the installed package - a different revision, with a\n"
        "different migration set. To run against the checkout being edited, from the repo root:\n"
        "    python -m scripts.seed_demo_backtests\n"
        "continuation/merge-rehearsal.sh already refuses this for itself; this is the same check.",
        file=sys.stderr,
    )
    raise SystemExit(2)# --- end import-origin guard ---------------------------------------------

from shared.constants import DATA_DIR
from shared.database import init_db, save_result_to_sqlite
from shared.models import (
    BacktestMeta,
    BacktestResult,
    EquityPoint,
    IndicatorSeries,
    Trade,
    TradeExit,
)

# Demo timestamps inside the generated OHLCV window.
_TS_10 = "2024-01-03T10:00:00+00:00"
_TS_18 = "2024-01-03T18:00:00+00:00"
_TS_09 = "2024-01-03T09:00:00+00:00"
_TS_11 = "2024-01-03T11:00:00+00:00"


def mk_ohlcv(symbol: str, base: float, source: str, asset_class: str, timeframe: str = "1h") -> None:
    ts = pd.date_range("2024-01-01T00:00:00Z", periods=220, freq="h", tz="UTC")
    rows = []
    for i, t in enumerate(ts):
        o = base + i * 0.2
        c = o + (0.4 if i % 2 == 0 else -0.3)
        high = max(o, c) + 0.3
        low = min(o, c) - 0.3
        rows.append({"timestamp": t, "open": o, "high": high, "low": low, "close": c, "volume": 1000 + i})
    df = pd.DataFrame(rows)
    path = DATA_DIR / "ohlcv" / source / asset_class / symbol / f"{timeframe}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def main() -> None:
    init_db()
    mk_ohlcv("AAPL", 150.0, "yfinance", "equity")
    mk_ohlcv("XAUUSD", 2000.0, "dukascopy", "metals")

    now = datetime.now(timezone.utc).isoformat()

    trade1 = Trade(
        id=1,
        symbol="AAPL",
        direction="LONG",
        entry_time=_TS_10,
        entry_price=160.0,
        quantity=10,
        exits=[
            TradeExit(time="2024-01-03T14:00:00+00:00", price=162.0, quantity=5, pnl=10.0, reason="tp1"),
            TradeExit(time=_TS_18, price=164.5, quantity=5, pnl=22.5, reason="tp2"),
        ],
        pnl=32.5,
        pnl_pct=2.03,
        net_pnl=32.5,
        holding_bars=8,
        meta={"SMA_Fast": 159.2, "RSI": 62.1, "Signal": "CrossUp", "Stop": 157.8},
    )

    trade1b = Trade(
        id=2,
        symbol="AAPL",
        direction="SHORT",
        entry_time="2024-01-04T10:00:00+00:00",
        entry_price=165.2,
        quantity=8,
        exits=[
            TradeExit(time="2024-01-04T15:00:00+00:00", price=163.9, quantity=8, pnl=10.4, reason="signal")
        ],
        pnl=10.4,
        pnl_pct=0.79,
        net_pnl=10.4,
        holding_bars=5,
        meta={"SMA_Fast": 165.0, "RSI": 41.2, "Signal": "CrossDown", "Stop": 166.4},
    )

    indicators1 = [
        IndicatorSeries(
            name="SMA 20",
            type="overlay",
            data=[
                {"time": _TS_09, "value": 159.0},
                {"time": _TS_10, "value": 159.2},
                {"time": _TS_11, "value": 159.5},
            ],
            style={"color": "#60a5fa"},
        ),
        IndicatorSeries(
            name="RSI 14",
            type="panel",
            data=[
                {"time": _TS_09, "value": 55.0},
                {"time": _TS_10, "value": 62.1},
                {"time": _TS_11, "value": 58.4},
            ],
            style={"color": "#a78bfa"},
        ),
    ]

    drawings1 = [
        {
            "type": "marker",
            "symbol": "AAPL",
            "time": _TS_10,
            "position": "belowBar",
            "shape": "arrowUp",
            "color": "#22c55e",
            "text": "Entry",
        },
        {
            "type": "line",
            "symbol": "AAPL",
            "start_time": _TS_10,
            "end_time": _TS_18,
            "start_value": 160.0,
            "end_value": 164.5,
            "color": "#f59e0b",
        },
        {
            "type": "line_with_breaks",
            "symbol": "AAPL",
            "data": [
                {"time": _TS_10, "value": 159.8},
                {"time": _TS_11, "value": 160.4},
                {"time": "2024-01-03T12:00:00+00:00", "value": None},
                {"time": "2024-01-03T13:00:00+00:00", "value": 161.3},
            ],
            "color": "#34d399",
        },
        {
            "type": "box",
            "symbol": "AAPL",
            "start_time": _TS_09,
            "end_time": "2024-01-03T13:00:00+00:00",
            "low": 158.8,
            "high": 161.1,
            "color": "rgba(59,130,246,0.12)",
            "border_color": "#3b82f6",
        },
    ]

    res1 = BacktestResult(
        meta=BacktestMeta(
            id="demo-equity-indicators",
            created_at=now,
            engine="native",
            strategies=["demo_equity"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            data={"symbols": ["AAPL"], "timeframes": ["1h"], "source": "yfinance"},
            initial_capital=100000,
        ),
        trades=[trade1, trade1b],
        equity_curve=[
            EquityPoint(time=_TS_10, equity=100000.0),
            EquityPoint(time=_TS_18, equity=100032.5),
        ],
        indicators=indicators1,
        drawings=drawings1,
    )
    save_result_to_sqlite(res1)

    trade2 = Trade(
        id=1,
        symbol="EURUSD",
        direction="SHORT",
        entry_time="2024-02-10T08:00:00+00:00",
        entry_price=1.092,
        quantity=10000,
        exits=[
            TradeExit(
                time="2024-02-10T12:00:00+00:00",
                price=1.088,
                quantity=10000,
                pnl=40.0,
                reason="signal",
            )
        ],
        pnl=40.0,
        pnl_pct=0.4,
        holding_bars=4,
        meta={"reason": "breakdown"},
    )
    res2 = BacktestResult(
        meta=BacktestMeta(
            id="demo-forex-missing",
            created_at=now,
            engine="native",
            strategies=["demo_fx"],
            backtest_start="2024-02-01",
            backtest_end="2024-02-28",
            data={"symbols": ["EURUSD"], "timeframes": ["1h"], "source": "dukascopy"},
            initial_capital=100000,
        ),
        trades=[trade2],
    )
    save_result_to_sqlite(res2)

    trade3 = Trade(
        id=1,
        symbol="XAUUSD",
        direction="LONG",
        entry_time="2024-01-05T06:00:00+00:00",
        entry_price=2012.0,
        quantity=2,
        exits=[TradeExit(time="2024-01-05T12:00:00+00:00", price=2018.0, quantity=2, pnl=12.0, reason="tp")],
        pnl=12.0,
        pnl_pct=0.3,
        holding_bars=6,
        meta={"volatility_regime": "high"},
    )
    res3 = BacktestResult(
        meta=BacktestMeta(
            id="demo-metals",
            created_at=now,
            engine="native",
            strategies=["demo_metals"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            data={"symbols": ["XAUUSD"], "timeframes": ["1h"], "source": "dukascopy"},
            initial_capital=100000,
        ),
        trades=[trade3],
    )
    save_result_to_sqlite(res3)

    trade4 = Trade(
        id=1,
        symbol="MSFT",
        direction="LONG",
        entry_time="2024-01-08T10:00:00+00:00",
        entry_price=380.0,
        quantity=5,
        exits=[TradeExit(time="2024-01-08T15:00:00+00:00", price=383.0, quantity=5, pnl=15.0, reason="signal")],
        pnl=15.0,
        pnl_pct=0.79,
        holding_bars=5,
        meta={"reason": "breakout"},
    )
    res4 = BacktestResult(
        meta=BacktestMeta(
            id="demo-equity-missing",
            created_at=now,
            engine="native",
            strategies=["demo_equity_missing"],
            backtest_start="2024-01-01",
            backtest_end="2024-01-31",
            data={"symbols": ["MSFT"], "timeframes": ["1h"], "source": "yfinance"},
            initial_capital=100000,
        ),
        trades=[trade4],
    )
    save_result_to_sqlite(res4)

    print("seeded demo backtests at", DATA_DIR)


if __name__ == "__main__":
    main()
