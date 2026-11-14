"""Engine plumbing for the published-strategy parity tests.

Everything here IS platform code (``runner.engines``): it writes a fixture
into the canonical data layout, runs the native engine and normalises the
trades.  The independent references live in ``ref_published.py`` and import
nothing from the platform.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "published"


@dataclass
class EngineTrade:
    symbol: str
    entry_time: str
    entry_price: float
    quantity: float
    exit_time: str
    exit_price: float
    pnl: float
    fees: float
    net_pnl: float


@dataclass
class EngineRun:
    trades: list[EngineTrade]
    curve: list[tuple[str, float]]
    stats: dict
    result: object


def load_fixture(name: str) -> pd.DataFrame:
    """Fixture as a UTC-timestamp frame with open/high/low/close columns."""
    frame = pd.read_csv(FIXTURES / f"{name.lower()}_1d.csv.gz")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    if "open" not in frame.columns:  # close-only series: flat bars
        for col in ("open", "high", "low"):
            frame[col] = frame["close"]
        frame = frame[["timestamp", "open", "high", "low", "close"]]
    frame["volume"] = 1.0
    return frame


def iso(value) -> str:
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return stamp.strftime("%Y-%m-%d")


def run_engine(
    strategy_path: str | Path,
    frames: dict[str, pd.DataFrame],
    *,
    tmp_path: Path,
    start: str,
    end: str,
    params: dict | None = None,
    fee_rate: float = 0.0,
    capital: float = 100_000.0,
) -> EngineRun:
    """Run ``strategy_path`` on daily ``frames`` ({symbol: frame}).

    Sizing: ``risk_per_trade_pct=1.0`` with a 100 % position cap and no
    leverage makes the position cap the only binding rule, i.e. the paper's
    "fully invested" allocation: ``qty = equity / signal-bar close``.
    """
    data_dir = tmp_path / "data"
    for symbol, frame in frames.items():
        target = data_dir / "ohlcv" / "test_source" / "equities" / symbol
        target.mkdir(parents=True, exist_ok=True)
        out = frame.copy()
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
        out.sort_values("timestamp").to_parquet(target / "1d.parquet", index=False)
    parameters = dict(params or {})
    if fee_rate:
        parameters["fee_rate"] = fee_rate
        parameters["fee_flat_per_trade"] = 0.0
    else:
        parameters["fees"] = "zero"
    config = BacktestConfig(
        strategy_name="parity",
        symbols=list(frames),
        timeframe="1d",
        start_date=start,
        end_date=end,
        initial_capital=capital,
        risk_per_trade_pct=1.0,
        max_position_exposure_pct=1.0,
        max_leverage=1.0,
        modeling_resolution="main_timeframe",
        signal_exit_fill="close",
        data_dir=str(data_dir),
        parameters=parameters,
    )
    result = NativeAdapter().run(config, str(strategy_path))
    trades = []
    for trade in result.trades:
        if len(trade.exits) != 1:
            raise AssertionError(f"expected exactly one exit leg, got {len(trade.exits)}")
        leg = trade.exits[0]
        trades.append(
            EngineTrade(
                symbol=str(trade.symbol),
                entry_time=iso(trade.entry_time),
                entry_price=float(trade.entry_price),
                quantity=float(trade.quantity),
                exit_time=iso(leg.time),
                exit_price=float(leg.price),
                pnl=float(trade.pnl),
                fees=float(trade.fees or 0.0),
                net_pnl=float(trade.net_pnl),
            )
        )
    trades.sort(key=lambda t: (t.entry_time, t.symbol))
    curve = [(iso(p.time), float(p.equity)) for p in result.equity_curve]
    return EngineRun(trades=trades, curve=curve, stats=dict(result.stats or {}) if hasattr(result, "stats") else {}, result=result)
