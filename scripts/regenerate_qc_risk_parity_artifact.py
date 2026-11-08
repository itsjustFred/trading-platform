#!/usr/bin/env python3
"""Regenerate the platform artifact for the QuantConnect RiskParityStrategy report."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# isort: split

# --- import-origin guard ---
# `shared` must resolve to this checkout, not the installed package. The mechanism lives in
# scripts/_import_origin.py; only the invocation is per script. scripts/ goes on sys.path - never
# the repo root, which would repair `import shared` and defeat the guard - so a direct run is
# refused (exit 2) instead of mutating another tree's schema. See
# tests/test_script_import_origin.py; run as:
#     python -m scripts.regenerate_qc_risk_parity_artifact
import sys as _sys
from pathlib import Path as _path

_scripts_dir = str(_path(__file__).resolve().parent)
if _scripts_dir not in _sys.path:
    _sys.path.insert(0, _scripts_dir)
import _import_origin as _guard

_guard.ensure_own_checkout("regenerate_qc_risk_parity_artifact")
# --- end import-origin guard ---
# The guard module performs `import shared` itself, so this line is no longer read at run time -
# it is the anchor `tests/test_script_import_origin.py` scans for when it builds the roster of
# scripts that must carry the guard. Kept deliberately, and silenced rather than removed:
# dropping it would take this script off the roster and out of the gate that keeps it alive.
import shared  # noqa: F401
from scripts import run_risk_parity_backtest as risk_parity
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

DEFAULT_OUTPUT = Path("data/backtests/risk_parity_quantconnect_reimplementation.json")
DEFAULT_SOURCE = Path("ohlcv/yfinance/equity/US")


def _load_local_prices(data_dir: Path, source: Path, start: str, end: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = data_dir / source
    close_columns: dict[str, pd.Series] = {}
    open_columns: dict[str, pd.Series] = {}
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")

    for ticker in risk_parity.TICKERS:
        path = root / ticker / "1d.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Missing local Risk Parity data for {ticker}: {path}")
        frame = pd.read_parquet(path)
        frame.columns = [str(col).lower() for col in frame.columns]
        required = {"timestamp", "close", "open"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"{path} missing columns: {', '.join(sorted(missing))}")
        timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        closes = pd.to_numeric(frame["close"], errors="coerce")
        opens = pd.to_numeric(frame["open"], errors="coerce")
        close_series = pd.Series(closes.values, index=timestamps).dropna().sort_index()
        open_series = pd.Series(opens.values, index=timestamps).dropna().sort_index()
        close_series = close_series[(close_series.index >= start_ts) & (close_series.index <= end_ts)]
        open_series = open_series[(open_series.index >= start_ts) & (open_series.index <= end_ts)]
        if close_series.empty:
            raise ValueError(f"No usable local Risk Parity rows for {ticker}: {path}")
        close_columns[ticker] = close_series
        open_columns[ticker] = open_series

    prices = pd.DataFrame(close_columns).dropna(how="all")
    open_prices = pd.DataFrame(open_columns).reindex(prices.index)
    prices.index = prices.index.tz_convert(None)
    open_prices.index = open_prices.index.tz_convert(None)
    return prices, open_prices


def _to_iso(value: Any) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat()


def _build_result(result: dict[str, Any], prices: pd.DataFrame, *, start: str, end: str, output: Path, source: Path) -> BacktestResult:
    trades: list[Trade] = []
    for idx, raw in enumerate(result["trades"], start=1):
        entry_price = float(raw["entry_price"])
        exit_price = raw.get("exit_price")
        if exit_price is None:
            exit_price = entry_price
        pnl = float(raw.get("pnl") or 0.0)
        quantity = float(raw.get("quantity") or 0.0)
        trades.append(
            Trade(
                id=idx,
                symbol=str(raw.get("symbol") or "PORTFOLIO"),
                direction=str(raw.get("direction") or "LONG"),
                entry_time=_to_iso(raw["entry_time"]),
                entry_price=entry_price,
                quantity=quantity,
                exits=[
                    TradeExit(
                        time=_to_iso(raw["exit_time"]),
                        price=float(exit_price),
                        quantity=quantity,
                        pnl=pnl,
                        reason="rebalance",
                    )
                ],
                pnl=pnl,
                net_pnl=pnl,
                position_size=float(raw.get("position_size") or 0.0),
                tags=["qc-risk-parity-reimplementation"],
                meta=dict(raw.get("meta") or {}),
            )
        )

    portfolio_value = result["portfolio_value"]
    equity_curve = [
        EquityPoint(time=_to_iso(ts), equity=round(float(value), 6))
        for ts, value in portfolio_value.dropna().items()
    ]
    now = datetime.now(timezone.utc).isoformat()
    stats = dict(result["stats"])
    stats.setdefault("total_pnl", stats.get("net_profit"))
    stats["net_profit"] = stats.get("net_profit")
    stats["local_price_start"] = _to_iso(prices.index.min())
    stats["local_price_end"] = _to_iso(prices.index.max())

    return BacktestResult(
        meta=BacktestMeta(
            id=output.stem,
            created_at=now,
            engine="python-reimplementation",
            strategies=["RiskParityStrategy"],
            backtest_start=start,
            backtest_end=end,
            duration_days=(pd.Timestamp(end) - pd.Timestamp(start)).days,
            total_bars=int(prices.count().sum()),
            data={
                "symbols": list(risk_parity.TICKERS),
                "timeframes": ["1d"],
                "source": str(source),
                "price_start": _to_iso(prices.index.min()),
                "price_end": _to_iso(prices.index.max()),
            },
            initial_capital=1_000_000.0,
            parameters={
                "target_volatility": 0.10,
                "max_leverage": 1.5,
                "lookback_volatility": 60,
                "lookback_correlation": 120,
                "min_weight": 0.02,
                "max_weight": 0.30,
            },
            tags=["qc-parity", "risk-parity"],
            notes="Python reimplementation of strategy-comparisons/risk_parity/risk_parity_strategy.py using local daily parquet data.",
        ),
        trades=trades,
        equity_curve=equity_curve,
        stats=stats,
    )


def regenerate(*, data_dir: Path, source: Path, output: Path, start: str, end: str,
               history_start: str = "2015-08-04") -> BacktestResult:
    prices, open_prices = _load_local_prices(data_dir, source, history_start, end)

    def local_prices(_: str, __: str, history_start: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
        return prices.copy(), open_prices.copy()

    original = risk_parity.download_prices
    risk_parity.download_prices = local_prices
    try:
        result = risk_parity.run_backtest(start_date=start, end_date=end, initial_cash=1_000_000.0)
    finally:
        risk_parity.download_prices = original

    backtest = _build_result(result, prices, start=start, end=end, output=output, source=source)
    backtest.save(output)
    return backtest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--start", default="2016-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--history-start", default="2015-08-04",
                        help="Local price history start (QC data start; A-M6 SMA200 gate origin)")
    args = parser.parse_args()

    result = regenerate(
        data_dir=args.data_dir,
        source=args.source,
        output=args.output,
        start=args.start,
        end=args.end,
        history_start=args.history_start,
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "total_trades": len(result.trades),
                "stats": result.stats,
                "data": result.meta.data,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
