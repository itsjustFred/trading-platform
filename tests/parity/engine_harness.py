"""Engine plumbing for the web-strategy parity tests.

Everything in this module IS platform code (``runner.engines``): it loads a
ported strategy and runs it through the native engine on a committed fixture,
then normalises the trades into the same shape the INDEPENDENT reference
simulator in ``tests/parity/reference_execution.py`` produces, so a test can
compare the two trade by trade without either side importing the other.

The reference simulators in this package import nothing from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter
from tests.parity.reference_execution import Bar, RefLeg, RefTrade

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[2]
STRATEGIES_DIR = REPO_ROOT / "strategies"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "parity"

#: ``RiskConfig.max_total_exposure`` — the engine's aggregate open-risk ceiling.
MAX_TOTAL_EXPOSURE = 0.05


@dataclass
class EngineRun:
    trades: list[RefTrade]
    curve: list[tuple[str, float]]
    warnings: list[str]
    run_summary: dict
    #: trades flagged ``signal_exit_deferred_to_end_of_data``
    deferred_to_end: int = 0


def load_fixture(name: str) -> list[Bar]:
    return _read_bars(FIXTURES_DIR / name)


def _read_bars(path: Path) -> list[Bar]:
    return pd.read_csv(path, parse_dates=["timestamp"])


def write_data_dir(tmp_path: Path, frame: pd.DataFrame, *, symbol: str, timeframe: str) -> Path:
    """Materialise a frame in the canonical ``{data_dir}/ohlcv/...`` layout."""
    data_dir = tmp_path / "data"
    target = data_dir / "ohlcv" / "test_source" / "equities" / symbol
    target.mkdir(parents=True, exist_ok=True)
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp")
    out.to_parquet(target / f"{timeframe}.parquet", index=False)
    return data_dir


def build_config(
    data_dir: Path,
    *,
    timeframe: str,
    start: str,
    end: str,
    risk_pct: float,
    params: dict | None = None,
    position_pct: float | None = None,
    leverage: float | None = 1.0,
    modeling_resolution: str = "main_timeframe",
    capital: float = 100_000.0,
    fee_rate: float = 0.0,
    fee_flat: float = 0.0,
    signal_exit_fill: str = "next_open",
) -> BacktestConfig:
    parameters = dict(params or {})
    if fee_rate or fee_flat:
        parameters["fee_rate"] = fee_rate
        parameters["fee_flat_per_trade"] = fee_flat
    else:
        parameters["fees"] = "zero"
    return BacktestConfig(
        strategy_name="parity",
        symbols=[_symbol_for(timeframe)],
        timeframe=timeframe,
        start_date=start,
        end_date=end,
        initial_capital=capital,
        risk_per_trade_pct=risk_pct,
        max_position_exposure_pct=position_pct,
        max_leverage=leverage,
        modeling_resolution=modeling_resolution,
        signal_exit_fill=signal_exit_fill,
        data_dir=str(data_dir),
        parameters=parameters,
    )


def _symbol_for(timeframe: str) -> str:
    return f"SYN_{timeframe}"


def run_engine(
    strategy_path: str | Path,
    frame: pd.DataFrame,
    *,
    timeframe: str,
    start: str,
    end: str,
    risk_pct: float = 0.01,
    params: dict | None = None,
    position_pct: float | None = None,
    leverage: float | None = 1.0,
    modeling_resolution: str = "main_timeframe",
    capital: float = 100_000.0,
    fee_rate: float = 0.0,
    fee_flat: float = 0.0,
    signal_exit_fill: str = "next_open",
    tmp_path: Path | None = None,
) -> EngineRun:
    """Run the native engine over ``frame`` and return normalised trades."""
    if tmp_path is None:  # pragma: no cover - every caller uses a tmp_path
        raise ValueError("tmp_path is required")
    data_dir = write_data_dir(tmp_path, frame, symbol=_symbol_for(timeframe), timeframe=timeframe)
    config = build_config(
        data_dir,
        timeframe=timeframe,
        start=start,
        end=end,
        risk_pct=risk_pct,
        params=params,
        position_pct=position_pct,
        leverage=leverage,
        modeling_resolution=modeling_resolution,
        capital=capital,
        fee_rate=fee_rate,
        fee_flat=fee_flat,
        signal_exit_fill=signal_exit_fill,
    )
    result = NativeAdapter().run(config, str(strategy_path))
    trades = [_normalise(trade) for trade in result.trades]
    warnings = [
        line
        for line in (result.meta.data.get("warnings") or [])
        if isinstance(line, str)
    ]
    return EngineRun(
        trades=sorted(trades, key=lambda item: item.entry_time),
        curve=[(_iso(point.time), float(point.equity)) for point in result.equity_curve],
        warnings=warnings,
        run_summary={},
        deferred_to_end=sum(
            bool((t.meta or {}).get("signal_exit_deferred_to_end_of_data")) for t in result.trades
        ),
    )


def _normalise(trade) -> RefTrade:
    legs = [
        RefLeg(
            time=_iso(leg.time),
            price=float(leg.price),
            quantity=float(leg.quantity),
            pnl=float(leg.pnl or 0.0),
            reason=str(leg.reason or ""),
        )
        for leg in trade.exits
    ]
    return RefTrade(
        entry_time=_iso(trade.entry_time),
        direction=str(trade.direction),
        entry_price=float(trade.entry_price),
        quantity=float(trade.quantity),
        legs=legs,
        pnl=float(trade.pnl),
        fees=float(trade.fees or 0.0),
        net_pnl=float(trade.net_pnl),
        exit_reason=str((trade.meta or {}).get("exit_reason") or legs[-1].reason if legs else ""),
        mae=float(trade.mae or 0.0),
        mfe=float(trade.mfe or 0.0),
        entry_index=0,
    )


def _iso(value) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    else:
        stamp = stamp.tz_convert("UTC")
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def frame_from_fixture(name: str) -> pd.DataFrame:
    frame = pd.read_csv(FIXTURES_DIR / name, parse_dates=["timestamp"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def bar_list(frame: pd.DataFrame) -> list[Bar]:
    return [
        Bar(
            time=_iso(row["timestamp"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
        )
        for _, row in frame.iterrows()
    ]