from datetime import datetime, timezone

import pandas as pd

# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism -
# and the reasoning - live in scripts/_import_origin.py; only the invocation is per
# script. Running this file directly puts scripts/ on sys.path[0], not the repo root,
# so `shared` never finds this checkout and the guard refuses (exit 2) rather than
# mutating the wrong tree. See tests/test_script_import_origin.py; run as:
#     python -m scripts.seed_demo_backtests
if __package__ in (None, ""):
    # `python scripts/<name>.py`, or a loader that reads this file by path (e.g.
    # importlib's spec_from_file_location, which tests/test_script_hygiene.py uses): scripts/ is
    # not necessarily on sys.path, so make this file's own directory importable. scripts/ only -
    # never the repo root, which would repair `import shared` and defeat the guard.
    import sys as _sys
    from pathlib import Path as _path

    _scripts_dir = str(_path(__file__).resolve().parent)
    if _scripts_dir not in _sys.path:
        _sys.path.insert(0, _scripts_dir)
    import _import_origin as _guard
else:  # `python -m scripts.<name>`: the repo root is on sys.path
    from scripts import _import_origin as _guard

if not _guard.is_own_checkout_file(_guard):
    _guard = _guard.load_sibling(__file__)
_guard.ensure_own_checkout("seed_demo_backtests")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
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
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        rows.append({"timestamp": t, "open": o, "high": h, "low": l, "close": c, "volume": 1000 + i})
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
