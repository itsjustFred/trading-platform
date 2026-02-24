"""
vectorbt Engine Adapter — runs Python strategies using vectorbt for vectorized backtesting.

Strategy files must define a function:
    def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame
        Takes OHLCV DataFrame, returns DataFrame with 'entries' and 'exits' boolean columns.

This is intentionally simple — for complex strategies, use LEAN.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from runner.engines.base import BacktestConfig, EngineAdapter
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("TRADING_DATA_DIR", Path.home() / "data"))


class VectorbtAdapter(EngineAdapter):
    """Adapter for vectorbt-based vectorized backtesting."""

    @property
    def name(self) -> str:
        return "vectorbt"

    def run(self, config: BacktestConfig, strategy_path: str) -> BacktestResult:
        # Load strategy module
        strategy_mod = _load_strategy_module(strategy_path)
        if not hasattr(strategy_mod, "generate_signals"):
            raise ValueError(f"Strategy {strategy_path} must define generate_signals(df, params)")

        data_root = Path(config.data_dir) if config.data_dir else DATA_DIR
        all_trades: list[Trade] = []
        all_equity: list[EquityPoint] = []
        trade_counter = 0

        for symbol in config.symbols:
            # Find OHLCV data
            df = _load_ohlcv(data_root, symbol, config.timeframe, config.start_date, config.end_date)
            if df is None or df.empty:
                logger.warning("No data for %s/%s, skipping", symbol, config.timeframe)
                continue

            # Generate signals
            signals = strategy_mod.generate_signals(df, config.parameters)
            entries = signals["entries"].astype(bool)
            exits = signals["exits"].astype(bool)

            # Walk through signals to build trades
            in_trade = False
            entry_idx = None

            for i in range(len(df)):
                if not in_trade and entries.iloc[i]:
                    in_trade = True
                    entry_idx = i
                elif in_trade and exits.iloc[i]:
                    in_trade = False
                    trade_counter += 1

                    entry_row = df.iloc[entry_idx]
                    exit_row = df.iloc[i]

                    # Determine direction from strategy or default LONG
                    direction = "LONG"
                    if "direction" in signals.columns:
                        d = signals["direction"].iloc[entry_idx]
                        if d in ("SHORT", "short", -1):
                            direction = "SHORT"

                    entry_price = float(entry_row["close"])
                    exit_price = float(exit_row["close"])
                    qty = config.initial_capital / entry_price  # simplified: all-in

                    if direction == "LONG":
                        pnl = (exit_price - entry_price) * qty
                    else:
                        pnl = (entry_price - exit_price) * qty

                    entry_ts = str(entry_row.get("timestamp", entry_row.name))
                    exit_ts = str(exit_row.get("timestamp", exit_row.name))

                    trade = Trade(
                        id=trade_counter,
                        symbol=symbol,
                        direction=direction,
                        entry_time=entry_ts,
                        entry_price=round(entry_price, 6),
                        quantity=round(qty, 4),
                        exits=[TradeExit(
                            time=exit_ts,
                            price=round(exit_price, 6),
                            quantity=round(qty, 4),
                            pnl=round(pnl, 2),
                        )],
                        pnl=round(pnl, 2),
                        holding_bars=i - entry_idx,
                        tags=["vectorbt"],
                    )
                    all_trades.append(trade)

        # Build equity curve
        equity = config.initial_capital
        for t in sorted(all_trades, key=lambda x: x.entry_time):
            equity += t.pnl
            all_equity.append(EquityPoint(time=t.entry_time, equity=round(equity, 2)))

        now = datetime.now(timezone.utc)
        meta = BacktestMeta(
            id=f"vbt-{config.strategy_name}-{now.strftime('%Y%m%d-%H%M%S')}",
            strategy=config.strategy_name,
            engine="vectorbt",
            created_at=now.isoformat(),
            timeframe_start=config.start_date,
            timeframe_end=config.end_date,
            initial_capital=config.initial_capital,
            symbols=config.symbols,
            data_timeframe=config.timeframe,
        )

        return BacktestResult(meta=meta, trades=all_trades, equity_curve=all_equity)


def _load_strategy_module(path: str):
    """Dynamically load a Python strategy file."""
    p = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("strategy", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_ohlcv(
    data_root: Path, symbol: str, timeframe: str,
    start_date: str, end_date: str,
) -> pd.DataFrame | None:
    """Find and load OHLCV parquet for a symbol."""
    ohlcv_root = data_root / "ohlcv"
    matches = list(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None

    df = pd.read_parquet(matches[0])
    df.columns = [c.lower() for c in df.columns]

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if start_date:
            df = df[df["timestamp"] >= start_date]
        if end_date:
            df = df[df["timestamp"] <= end_date]

    return df.reset_index(drop=True)
