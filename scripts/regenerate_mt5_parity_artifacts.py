#!/usr/bin/env python3
"""Regenerate platform artifacts for the IntradayVolatilityBreakout MT5 reports."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runner.daily_equity import attach_daily_equity
from runner.engines.base import resolve_strategy_parameters
from runner.stats import compute_stats_for_result
from shared.constants import DATA_DIR, normalize_timeframe
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit
from strategies.intraday_volatility_breakout.intraday_volatility_breakout import generate_signals

STRATEGY_PATH = Path("strategies/intraday_volatility_breakout/intraday_volatility_breakout.py")

MT5_SYMBOL_NAS100 = "USATECH.IDX"
MT5_SYMBOL_SP500 = "USA500.IDX"

COMPARISON_CASES: tuple[dict[str, Any], ...] = (
    {
        "label": "EURUSD_H1",
        "symbol": "EURUSD",
        "execution_symbol": "EURUSD",
        "report_timeframe": "1h",
        "execution_timeframe": "1m",
        "start": "2018-01-01",
        "end": "2026-06-23",
        "output": Path("data/backtests/intraday_volatility_breakout_eurusd_h1.json"),
        "use_native_daily_ohlcv": True,
    },
    {
        "label": "NAS100_M15",
        "symbol": MT5_SYMBOL_NAS100,
        "execution_symbol": "USTEC",
        "report_timeframe": "15m",
        "execution_timeframe": "1m",
        "start": "2018-01-01",
        "end": "2026-06-23",
        "output": Path("data/backtests/intraday_volatility_breakout_nas100_m15.json"),
    },
    {
        "label": "SP500_M1",
        "symbol": MT5_SYMBOL_SP500,
        "execution_symbol": "US500",
        "report_timeframe": "1m",
        "execution_timeframe": "1m",
        "start": "2018-01-01",
        "end": "2026-06-23",
        "output": Path("data/backtests/intraday_volatility_breakout_sp500_m1.json"),
    },
)

MT5_CONTRACT_SIZES: dict[str, float] = {
    "EURUSD": 100_000.0,
    MT5_SYMBOL_NAS100: 1.0,
    MT5_SYMBOL_SP500: 1.0,
}

MT5_LOT_STEPS: dict[str, float] = {
    "EURUSD": 0.01,
    MT5_SYMBOL_NAS100: 1.0,
    MT5_SYMBOL_SP500: 1.0,
}


def _candidate_ohlcv_paths(data_dir: Path, symbol: str, timeframe: str) -> list[Path]:
    normalized_timeframe = normalize_timeframe(timeframe)
    stems = {normalized_timeframe}
    if normalized_timeframe == "1m":
        stems.update({"m1", "m1_bid", "m1_ask", "1m_bid", "1m_ask"})
    elif normalized_timeframe == "1h":
        stems.update({"h1", "h1_bid", "h1_ask", "1h_bid", "1h_ask"})
    elif normalized_timeframe == "1d":
        stems.update({"d1", "d1_bid", "d1_ask", "1d_bid", "1d_ask"})

    matches: list[Path] = []
    root = data_dir / "ohlcv"
    for stem in sorted(stems):
        matches.extend(sorted(root.rglob(f"**/{symbol}/{stem}.parquet")))
    return sorted(dict.fromkeys(matches))


def _source_priority(path: Path, timeframe: str) -> int:
    normalized_timeframe = normalize_timeframe(timeframe)
    if path.stem == normalized_timeframe:
        return 0
    if path.stem == f"{normalized_timeframe}_bid":
        return 1
    if path.stem == f"{normalized_timeframe}_ask":
        return 2
    if normalized_timeframe == "1m" and path.stem == "m1_bid":
        return 3
    if normalized_timeframe == "1m" and path.stem == "m1_ask":
        return 4
    return 10


def _normalize_ohlcv_frame(df: pd.DataFrame, source_path: Path, timeframe: str) -> pd.DataFrame:
    frame = df.copy()
    frame.columns = [str(col).lower() for col in frame.columns]

    frame["timestamp"] = pd.to_datetime(frame.get("timestamp"), utc=True, errors="coerce")
    for col in ["open", "high", "low", "close", "volume"]:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    for prefix in ("bid", "ask"):
        for col in ("open", "high", "low", "close", "volume"):
            source = f"{prefix}_{col}"
            if source in frame.columns:
                frame[source] = pd.to_numeric(frame[source], errors="coerce")

    for prefix in ("bid", "ask"):
        prefixed = {f"{prefix}_{col}": col for col in ("open", "high", "low", "close", "volume")}
        if all(col in frame.columns for col in list(prefixed)[:4]):
            for source, target in prefixed.items():
                if source not in frame.columns:
                    continue
                source_values = pd.to_numeric(frame[source], errors="coerce")
                if target in frame.columns:
                    frame[target] = frame[target].fillna(source_values)
                else:
                    frame[target] = source_values

    required = ["timestamp", "open", "high", "low", "close"]
    missing = [col for col in required if col not in frame.columns]
    if missing:
        raise ValueError(f"{source_path} is missing required OHLCV columns: {', '.join(missing)}")

    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
    frame["_source_path"] = str(source_path)
    frame["_source_priority"] = _source_priority(source_path, timeframe)
    return frame


def _load_ohlcv(data_dir: Path, symbol: str, timeframe: str, start: str, end: str) -> pd.DataFrame:
    matches = _candidate_ohlcv_paths(data_dir, symbol, timeframe)
    if not matches:
        raise FileNotFoundError(f"No OHLCV parquet for {symbol}/{timeframe} under {data_dir / 'ohlcv'}")

    frames: list[pd.DataFrame] = []
    for path in matches:
        try:
            candidate = _normalize_ohlcv_frame(pd.read_parquet(path), path, timeframe)
        except Exception as exc:
            print(f"Skipping unusable OHLCV candidate {path}: {exc}", file=sys.stderr)
            continue
        candidate = candidate.dropna(subset=["timestamp"])
        candidate = candidate[candidate["timestamp"] >= pd.Timestamp(start, tz="UTC")]
        candidate = candidate[candidate["timestamp"] <= pd.Timestamp(end, tz="UTC")]
        candidate = candidate.dropna(subset=["open", "high", "low", "close"])
        if not candidate.empty:
            frames.append(candidate)

    if not frames:
        raise FileNotFoundError(
            f"No valid OHLCV rows for {symbol}/{timeframe} from {start} to {end} under {data_dir / 'ohlcv'}"
        )

    frame = pd.concat(frames, ignore_index=True, sort=False)
    frame = frame.sort_values(["timestamp", "_source_priority"], kind="mergesort")
    frame = frame.groupby("timestamp", sort=True, as_index=False).first()
    frame = frame[frame["timestamp"] >= pd.Timestamp(start, tz="UTC")]
    frame = frame[frame["timestamp"] <= pd.Timestamp(end, tz="UTC")]
    return frame.reset_index(drop=True)


def _load_daily_ohlcv(data_dir: Path, symbol: str, start: str, end: str) -> pd.DataFrame:
    return _load_ohlcv(data_dir, symbol, "1d", start, end)


def _to_iso(value: Any) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat()


def _conversion_series(data_dir: Path, start: str, end: str) -> pd.Series:
    frame = _load_ohlcv(data_dir, "EURUSD", "1m", start, end)
    ts = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    series = pd.Series(close.values, index=ts).dropna().sort_index()
    return series[~series.index.duplicated(keep="first")]


def _price_unit_value(symbol: str, timestamp: Any, conversion: pd.Series | None) -> float:
    contract_size = MT5_CONTRACT_SIZES.get(symbol, 1.0)
    if conversion is None or conversion.empty:
        return contract_size
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    pos = conversion.index.searchsorted(ts)
    if pos >= len(conversion):
        pos = len(conversion) - 1
    rate = float(conversion.iloc[pos])
    if rate <= 0:
        return contract_size
    return contract_size / rate


def _side_price(row: pd.Series, side: str, field: str) -> float:
    prefix = "ask" if side == "LONG" else "bid"
    value = row.get(f"{prefix}_{field}")
    if pd.notna(value):
        return float(value)
    return float(row[field])


def _opposite_side_price(row: pd.Series, side: str, field: str) -> float:
    prefix = "bid" if side == "LONG" else "ask"
    value = row.get(f"{prefix}_{field}")
    if pd.notna(value):
        return float(value)
    return float(row[field])


def _opposite_side_series(frame: pd.DataFrame, side: str, field: str) -> pd.Series:
    prefix = "bid" if side == "LONG" else "ask"
    column = f"{prefix}_{field}"
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce").fillna(pd.to_numeric(frame[field], errors="coerce"))
    return pd.to_numeric(frame[field], errors="coerce")


def _round_mt5_lots(symbol: str, lots: float) -> float:
    step = MT5_LOT_STEPS.get(symbol, 0.0)
    if step <= 0:
        return max(lots, 0.0)
    return max(math.floor((lots / step) + 1e-12) * step, 0.0)


def _build_trades(
    *,
    case: dict[str, Any],
    df: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    indicators: dict[str, Any],
    capital: float,
    conversion: pd.Series | None = None,
) -> list[Trade]:
    entry_indices = list(entries[entries].index)
    exit_indices = list(exits[exits].index)
    exit_cursor = 0
    trades: list[Trade] = []
    equity = float(capital)
    fixed_risk_amount = float(indicators.get("fixed_risk_amount", 500.0) or 500.0)
    direction = indicators.get("direction", pd.Series(index=df.index, dtype=object))
    sl_price = pd.to_numeric(indicators.get("sl_price", pd.Series(index=df.index)), errors="coerce")
    stop_distance_raw = indicators.get("stop_distance", pd.Series(index=df.index))
    if not isinstance(stop_distance_raw, pd.Series):
        stop_distance_raw = pd.Series(stop_distance_raw, index=df.index)
    stop_distance_series = pd.to_numeric(stop_distance_raw, errors="coerce")

    for entry_index in entry_indices:
        fill_index = int(entry_index) + 1
        if fill_index >= len(df):
            continue
        while exit_cursor < len(exit_indices) and exit_indices[exit_cursor] <= fill_index:
            exit_cursor += 1
        if exit_cursor >= len(exit_indices):
            break

        signal_row = df.iloc[int(entry_index)]
        entry_row = df.iloc[fill_index]
        signal_close = float(signal_row["close"])
        signal_stop_loss = sl_price.iloc[int(entry_index)] if int(entry_index) < len(sl_price) else float("nan")
        side = str(direction.iloc[int(entry_index)] if int(entry_index) < len(direction) else "LONG").upper()
        if side != "SHORT":
            side = "LONG"
        entry_price = _side_price(entry_row, side, "close")
        stop_loss = signal_stop_loss
        signal_stop_distance = (
            stop_distance_series.iloc[int(entry_index)]
            if int(entry_index) < len(stop_distance_series)
            else float("nan")
        )
        has_valid_stop_distance = (
            pd.notna(signal_stop_distance)
            and math.isfinite(float(signal_stop_distance))
            and float(signal_stop_distance) > 0
        )
        if has_valid_stop_distance:
            distance = float(signal_stop_distance)
            stop_loss = entry_price - distance if side == "LONG" else entry_price + distance
        elif pd.notna(signal_stop_loss):
            stop_distance_from_signal = abs(signal_close - float(signal_stop_loss))
            stop_loss = entry_price - stop_distance_from_signal if side == "LONG" else entry_price + stop_distance_from_signal

        exit_index = int(exit_indices[exit_cursor])
        exit_reason = "signal"
        exit_price = _opposite_side_price(df.iloc[exit_index], side, "close")

        if pd.notna(stop_loss):
            stop = float(stop_loss)
            if side == "LONG":
                lows = _opposite_side_series(df, side, "low").iloc[fill_index + 1 : exit_index + 1]
                hit = lows[lows <= stop]
            else:
                highs = _opposite_side_series(df, side, "high").iloc[fill_index + 1 : exit_index + 1]
                hit = highs[highs >= stop]
            if not hit.empty:
                exit_index = int(hit.index[0])
                exit_price = stop
                exit_reason = "stop_loss"

        risk_stop_distance = abs(entry_price - float(stop_loss)) if pd.notna(stop_loss) else entry_price * 0.02
        risk_unit_value = _price_unit_value(str(case["symbol"]), entry_row["timestamp"], conversion)
        qty = fixed_risk_amount / (risk_stop_distance * risk_unit_value) if risk_stop_distance > 0 else equity / entry_price
        qty = _round_mt5_lots(str(case["symbol"]), qty)
        if qty <= 0:
            continue
        pnl_unit_value = _price_unit_value(str(case["symbol"]), df.iloc[exit_index]["timestamp"], conversion)
        pnl = (exit_price - entry_price) * qty * pnl_unit_value if side == "LONG" else (entry_price - exit_price) * qty * pnl_unit_value
        exit_time = _to_iso(df.iloc[exit_index]["timestamp"])
        trade = Trade(
            id=len(trades) + 1,
            symbol=str(case["symbol"]),
            direction=side,
            entry_time=_to_iso(entry_row["timestamp"]),
            entry_price=round(entry_price, 6),
            quantity=round(qty, 4),
            exits=[
                TradeExit(
                    time=exit_time,
                    price=round(exit_price, 6),
                    quantity=round(qty, 4),
                    pnl=round(pnl, 2),
                    reason=exit_reason,
                )
            ],
            pnl=round(pnl, 2),
            net_pnl=round(pnl, 2),
            holding_bars=max(0, exit_index - fill_index),
            tags=["mt5-parity-fast"],
            meta={
                "execution_symbol": case["execution_symbol"],
                "execution_timeframe": case["execution_timeframe"],
                "sl_price": round(float(stop_loss), 6) if pd.notna(stop_loss) else None,
                "fixed_risk_amount": fixed_risk_amount,
                "contract_size": MT5_CONTRACT_SIZES.get(str(case["symbol"]), 1.0),
                "risk_unit_value": round(risk_unit_value, 6),
                "pnl_unit_value": round(pnl_unit_value, 6),
            },
        )
        trades.append(trade)
        equity += pnl
        exit_cursor += 1

    return trades


def _build_equity_curve(trades: list[Trade], capital: float) -> list[EquityPoint]:
    equity = float(capital)
    points: list[EquityPoint] = []
    for trade in trades:
        equity += float(trade.pnl)
        points.append(EquityPoint(time=trade.last_exit_time, equity=round(equity, 2)))
    return points


def regenerate_case(
    case: dict[str, Any],
    *,
    data_dir: Path,
    capital: float,
    risk_pct: float | None,
    conversion: pd.Series | None = None,
) -> dict[str, Any]:
    output = Path(case["output"])
    params = resolve_strategy_parameters(STRATEGY_PATH, {})
    params.update(
        {
            "comparison_label": case["label"],
            "reference_symbol": case["symbol"],
            "reference_timeframe": case["report_timeframe"],
            "session_timezone": "Europe/Athens",
        }
    )
    df = _load_ohlcv(
        data_dir,
        str(case["execution_symbol"]),
        str(case["execution_timeframe"]),
        str(case["start"]),
        str(case["end"]),
    )
    signal_params = dict(params)
    daily_ohlcv: pd.DataFrame | None = None
    if case.get("use_native_daily_ohlcv"):
        daily_ohlcv = _load_daily_ohlcv(
            data_dir,
            str(case["execution_symbol"]),
            str(case["start"]),
            str(case["end"]),
        )
        signal_params["daily_ohlcv"] = daily_ohlcv
    entries, exits, indicators, drawings = generate_signals(df, signal_params)
    indicators["fixed_risk_amount"] = params.get("fixed_risk_amount", params.get("Fixed_Risk_Amount", 500.0))
    trades = _build_trades(
        case=case,
        df=df,
        entries=entries,
        exits=exits,
        indicators=indicators,
        capital=capital,
        conversion=conversion,
    )

    now = datetime.now(timezone.utc).isoformat()
    result = BacktestResult(
        meta=BacktestMeta(
            id=output.stem,
            created_at=now,
            engine="native",
            strategies=["intraday_volatility_breakout"],
            backtest_start=str(case["start"]),
            backtest_end=str(case["end"]),
            total_bars=len(df),
            data={
                "symbols": [case["symbol"]],
                "execution_symbols": [case["execution_symbol"]],
                "timeframes": [case["report_timeframe"]],
                "execution_timeframes": [case["execution_timeframe"]],
                "daily_timeframes": ["1d"] if daily_ohlcv is not None else [],
                "daily_sources": (
                    sorted({str(path) for path in daily_ohlcv["_source_path"].dropna().unique()})
                    if daily_ohlcv is not None and "_source_path" in daily_ohlcv
                    else []
                ),
                "source": "ohlcv",
            },
            initial_capital=capital,
            parameters=params,
            tags=["mt5-parity"],
        ),
        trades=trades,
        equity_curve=_build_equity_curve(trades, capital),
        drawings=drawings,
    )
    attach_daily_equity(result, start=str(case["start"]), end=str(case["end"]))
    result.stats = {**result.stats, **compute_stats_for_result(result)}
    output.parent.mkdir(parents=True, exist_ok=True)
    result.save(output)

    return {
        **case,
        "result": {
            "status": "ok",
            "output": str(output),
            "total_trades": len(trades),
            "total_pnl": result.stats.get("total_pnl"),
            "bars": len(df),
            "sources": sorted(str(path) for path in df["_source_path"].dropna().unique()) if "_source_path" in df else [],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--capital", type=float, default=100_000.0)
    parser.add_argument("--risk-pct", type=float, default=0.01)
    args = parser.parse_args()

    conversion = _conversion_series(args.data_dir, "2018-01-01", "2026-06-23")
    summary = [
        regenerate_case(case, data_dir=args.data_dir, capital=args.capital, risk_pct=args.risk_pct, conversion=conversion)
        for case in COMPARISON_CASES
    ]
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
