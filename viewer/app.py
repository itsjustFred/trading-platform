"""
Trade Viewer Backend — Flask API serving universal-format backtest results.

Endpoints:
    GET  /api/backtests                        — List available backtest result files
    GET  /api/backtest/<id>                    — Load a specific backtest result (raw)
    GET  /api/backtest/<id>/info               — Metadata for topbar display
    GET  /api/backtest/<id>/stats              — Compute stats for a backtest
    GET  /api/backtest/<id>/trades             — Viewer-format trade list
    GET  /api/backtest/<id>/trade/<nr>         — Single trade with OHLCV context
    GET  /api/backtest/<id>/ohlcv              — Full OHLCV data for all symbols
    GET  /api/backtest/<id>/equity             — Equity curve data
    GET  /api/compare?ids=id1,id2             — Full comparison with combined equity
    GET  /                                     — Serve the frontend

Usage:
    python -m viewer.app                         # default port 5556
    python -m viewer.app --port 9000
    python -m viewer.app --backtests ~/data/backtests
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from data_tools.catalog_builder import build_catalog, scan_ohlcv
from runner.stats import compute_stats_for_result
from shared.models import BacktestResult
from viewer.comparison import compare_backtests, compute_drawdown_pct

from shared.constants import DATA_DIR, LOG_DIR
from shared.logging import get_logger

logger = get_logger("viewer")

BACKTESTS_DIR = DATA_DIR / "backtests"
REACT_DIST = Path(__file__).parent.parent / "frontend" / "dist"
STATIC_DIR = REACT_DIST if REACT_DIST.exists() else Path(__file__).parent / "static"

STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
LOGS_DIR = LOG_DIR
TASK_LOGS_DIR = LOGS_DIR / "tasks"
ALL_LOGS_NAME = "ALL_LOGS"
_running_jobs: dict[str, dict] = {}  # job_id -> {status, result, error}

BARS_BEFORE = 60
BARS_AFTER = 25

app = Flask(__name__, static_folder=str(STATIC_DIR))
CORS(app)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _load_result(backtest_id: str) -> BacktestResult | None:
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return None
    return BacktestResult.load(str(path))


def _iso_to_unix(iso_str: str) -> float:
    """Convert ISO 8601 string to Unix timestamp (seconds)."""
    if not iso_str:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def _trade_to_viewer(trade) -> dict:
    """Convert a Universal Format Trade to viewer-friendly flat format."""
    last_exit = trade.exits[-1] if trade.exits else None
    payload = {
        "nr": trade.id,
        "symbol": trade.symbol,
        "direction": trade.direction,
        "entry_time": _iso_to_unix(trade.entry_time),
        "entry_time_iso": trade.entry_time,
        "entry_price": trade.entry_price,
        "exit_time": _iso_to_unix(last_exit.time) if last_exit else 0,
        "exit_time_iso": last_exit.time if last_exit else "",
        "exit_price": last_exit.price if last_exit else 0,
        "quantity": trade.quantity,
        "pnl": trade.pnl,
        "pnl_pct": trade.pnl_pct,
        "r_multiple": trade.r_multiple,
        "holding_bars": trade.holding_bars,
        "fees": trade.fees,
        "tags": trade.tags,
        "exits": [
            {
                "time": _iso_to_unix(e.time),
                "time_iso": e.time,
                "price": e.price,
                "quantity": e.quantity,
                "pnl": e.pnl,
                "reason": e.reason,
            }
            for e in trade.exits
        ],
    }
    if trade.meta:
        payload["meta"] = trade.meta
    return payload


def _load_ohlcv_df(symbol: str, timeframe: str) -> pd.DataFrame | None:
    """Find and load OHLCV parquet for a symbol+timeframe."""
    ohlcv_root = DATA_DIR / "ohlcv"
    matches = list(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None
    try:
        df = pd.read_parquet(matches[0])
        df.columns = [c.lower() for c in df.columns]
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    except Exception as e:
        logger.warning("Could not load OHLCV for %s/%s: %s", symbol, timeframe, e)
        return None


def _df_to_ohlcv_records(df: pd.DataFrame) -> list[dict]:
    """Convert OHLCV DataFrame to list of dicts with Unix timestamps."""
    records = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        if hasattr(ts, "timestamp"):
            unix_ts = ts.timestamp()
        else:
            unix_ts = pd.Timestamp(ts).timestamp()
        records.append(
            {
                "time": unix_ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            }
        )
    return records


def _parse_symbol_list(raw_symbols: str) -> list[str]:
    return [symbol.strip().upper() for symbol in raw_symbols.split(",") if symbol.strip()]


def _parse_timeframe_list(raw_timeframes: str) -> list[str]:
    return [
        timeframe.strip().lower() for timeframe in raw_timeframes.split(",") if timeframe.strip()
    ]


def _normalize_dukascopy_genesis_timeframe(timeframe: str) -> str:
    normalized = timeframe.strip().lower()
    aliases = {
        "sec": "s1",
        "1m": "m1",
        "5m": "m5",
        "15m": "m15",
        "30m": "m30",
        "1h": "h1",
        "4h": "h4",
        "1d": "d1",
    }
    return aliases.get(normalized, normalized)


def _load_dukascopy_genesis() -> dict[str, dict[str, str]]:
    from shared.constants import resolve_genesis_path

    genesis_path = resolve_genesis_path(DATA_DIR)
    if not genesis_path.exists():
        return {}

    try:
        data = json.loads(genesis_path.read_text())
    except Exception as e:
        logger.warning("Could not read Dukascopy genesis file %s: %s", genesis_path, e)
        return {}

    if not isinstance(data, dict):
        logger.warning("Invalid Dukascopy genesis payload in %s", genesis_path)
        return {}

    normalized: dict[str, dict[str, str]] = {}
    for raw_symbol, raw_value in data.items():
        symbol = str(raw_symbol).upper()
        if isinstance(raw_value, dict):
            timeframe_map = {
                _normalize_dukascopy_genesis_timeframe(str(timeframe)): str(genesis)
                for timeframe, genesis in raw_value.items()
                if str(genesis).strip()
            }
            if timeframe_map:
                normalized[symbol] = dict(sorted(timeframe_map.items()))
        elif str(raw_value).strip():
            normalized[symbol] = {"default": str(raw_value)}
    return normalized


def _collect_dukascopy_genesis(
    genesis_index: dict[str, dict[str, str]],
    symbols: list[str] | None = None,
    timeframes: list[str] | None = None,
) -> tuple[dict[str, dict[str, str]], str]:
    requested_symbols = [symbol.upper() for symbol in (symbols or sorted(genesis_index))]
    requested_timeframes = [
        _normalize_dukascopy_genesis_timeframe(timeframe) for timeframe in (timeframes or [])
    ]

    filtered: dict[str, dict[str, str]] = {}
    lower_bound_candidates: list[str] = []

    for symbol in requested_symbols:
        symbol_data = genesis_index.get(symbol, {})
        if not symbol_data:
            continue

        if requested_timeframes:
            timeframe_matches: dict[str, str] = {}
            for timeframe in requested_timeframes:
                genesis = symbol_data.get(timeframe) or symbol_data.get("default")
                if genesis:
                    timeframe_matches[timeframe] = genesis
                    lower_bound_candidates.append(genesis)
            if timeframe_matches:
                filtered[symbol] = timeframe_matches
        else:
            filtered[symbol] = dict(sorted(symbol_data.items()))
            lower_bound_candidates.extend(symbol_data.values())

    lower_bound = max(lower_bound_candidates) if lower_bound_candidates else ""
    return filtered, lower_bound


def _equity_to_viewer(result: BacktestResult) -> list[dict]:
    """Extract equity curve as [{time, equity}] with Unix timestamps.
    If no equity_curve in JSON, compute from trades."""
    if result.equity_curve:
        return [{"time": _iso_to_unix(e.time), "equity": e.equity} for e in result.equity_curve]
    # Compute from trades
    if not result.trades:
        return []
    capital = result.meta.initial_capital
    curve = [{"time": _iso_to_unix(result.trades[0].entry_time), "equity": capital}]
    running = capital
    for t in result.trades:
        running += t.pnl
        curve.append(
            {
                "time": _iso_to_unix(t.last_exit_time),
                "equity": round(running, 2),
            }
        )
    return curve


# ─── API ──────────────────────────────────────────────────────────────────────


def _quick_stats_and_equity(
    trades: list, initial_capital: float, start_str: str, end_str: str
) -> dict:
    """Compute lightweight stats + mini equity curve from trade list (no QuantStats needed)."""
    if not trades:
        return {
            "sharpe": 0,
            "cagr": 0,
            "max_drawdown_pct": 0,
            "net_profit_pct": 0,
            "equity_mini": [],
        }

    # Build equity curve from trades
    equity = [initial_capital]
    running = initial_capital
    wins = losses = 0
    for t in trades:
        pnl = t.get("pnl", 0)
        running += pnl
        equity.append(round(running, 2))
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    total_pnl = running - initial_capital
    net_profit_pct = round(total_pnl / initial_capital * 100, 2) if initial_capital else 0

    # Max drawdown
    peak = equity[0]
    max_dd = 0
    for val in equity:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
    max_drawdown_pct = round(max_dd * 100, 2)

    # CAGR
    cagr = 0
    try:
        if start_str and end_str:
            from datetime import datetime as _dt

            s = _dt.fromisoformat(start_str.replace("Z", "+00:00"))
            e = _dt.fromisoformat(end_str.replace("Z", "+00:00"))
            days = (e - s).days
            if days > 0 and initial_capital > 0 and running > 0:
                years = days / 365.25
                cagr = round(((running / initial_capital) ** (1 / years) - 1) * 100, 2)
    except Exception:
        pass

    # Simple Sharpe approximation from trade PnLs
    sharpe = 0
    psr_val = None
    pnls = [t.get("pnl", 0) for t in trades]
    if len(pnls) > 1:
        import statistics

        mean_pnl = statistics.mean(pnls)
        std_pnl = statistics.stdev(pnls)
        if std_pnl > 0:
            sharpe = round(mean_pnl / std_pnl * (252**0.5), 3)  # annualised
            # PSR uses per-observation (non-annualized) Sharpe
            sr_obs = mean_pnl / std_pnl
            try:
                from scipy.stats import norm as _norm

                n = len(pnls)
                if n >= 10 and sr_obs != 0:
                    m = mean_pnl
                    m2 = sum((p - m) ** 2 for p in pnls) / n
                    m3 = sum((p - m) ** 3 for p in pnls) / n
                    m4 = sum((p - m) ** 4 for p in pnls) / n
                    s = m2**0.5 if m2 > 0 else 0
                    if s > 0:
                        skew = m3 / s**3
                        kurt = m4 / s**4
                        denom = 1 - skew * sr_obs + ((kurt - 1) / 4) * sr_obs**2
                        if denom > 0:
                            z = sr_obs * (n - 1) ** 0.5 / denom**0.5
                            psr_val = round(float(_norm.cdf(z)), 4)
            except Exception:
                pass

    # Downsample equity to ~50 points for sparkline
    n = len(equity)
    if n <= 50:
        equity_mini = equity
    else:
        step = n / 50
        equity_mini = [equity[int(i * step)] for i in range(50)]
        equity_mini.append(equity[-1])

    return {
        "sharpe": sharpe,
        "cagr": cagr,
        "max_drawdown_pct": max_drawdown_pct,
        "net_profit_pct": net_profit_pct,
        "psr": psr_val,
        "equity_mini": equity_mini,
    }


@app.route("/api/backtests")
def list_backtests():
    """List all available backtest result files."""
    results = []
    if BACKTESTS_DIR.exists():
        for f in sorted(BACKTESTS_DIR.glob("*.trades.json")):
            try:
                data = json.loads(f.read_text())
                meta = data.get("meta", {})
                trades = data.get("trades", [])
                initial_capital = meta.get("initial_capital", 100000)
                start = meta.get("backtest_start", "")
                end = meta.get("backtest_end", "")

                quick = _quick_stats_and_equity(trades, initial_capital, start, end)

                results.append(
                    {
                        "id": f.stem.replace(".trades", ""),
                        "filename": f.name,
                        "strategy": (meta.get("strategies") or ["unknown"])[0],
                        "engine": meta.get("engine", "unknown"),
                        "symbols": meta.get("data", {}).get("symbols", []),
                        "timeframes": meta.get("data", {}).get("timeframes", []),
                        "trade_count": len(trades),
                        "start": start,
                        "end": end,
                        "created_at": meta.get("created_at", ""),
                        "sharpe": quick["sharpe"],
                        "cagr": quick["cagr"],
                        "max_drawdown_pct": quick["max_drawdown_pct"],
                        "net_profit_pct": quick["net_profit_pct"],
                        "psr": quick.get("psr"),
                        "equity_mini": quick["equity_mini"],
                    }
                )
            except Exception as e:
                logger.warning("Could not read %s: %s", f, e)
    return jsonify(results)


@app.route("/api/backtest/<backtest_id>")
def get_backtest(backtest_id: str):
    """Load a specific backtest result (raw format)."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": f"Backtest '{backtest_id}' not found"}), 404
    try:
        return jsonify(result.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest/<backtest_id>/info")
def get_backtest_info(backtest_id: str):
    """Metadata for topbar display."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": "Not found"}), 404
    m = result.meta
    return jsonify(
        {
            "id": m.id,
            "strategy": m.strategies[0] if m.strategies else "unknown",
            "engine": m.engine,
            "symbols": m.data.get("symbols", []),
            "timeframe": {"start": m.backtest_start, "end": m.backtest_end},
            "data_timeframe": m.data.get("timeframes", ["1h"])[0]
            if m.data.get("timeframes")
            else "1h",
            "timeframes": m.data.get("timeframes", []),
            "initial_capital": m.initial_capital,
            "created_at": m.created_at,
            "trade_count": len(result.trades),
            "parameters": m.parameters,
            "has_equity_curve": bool(result.equity_curve),
            "has_indicators": bool(result.stats.get("indicators")),
        }
    )


@app.route("/api/backtest/<backtest_id>/stats")
def get_backtest_stats(backtest_id: str):
    """Compute stats for a backtest."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": f"Backtest '{backtest_id}' not found"}), 404
    try:
        raw = compute_stats_for_result(result)
        # Normalize field names for frontend consistency
        stats = {
            "net_profit": raw.get("total_pnl", 0),
            "net_profit_pct": round(
                raw.get("total_pnl", 0) / (result.meta.initial_capital or 100000) * 100, 2
            ),
            "sharpe": raw.get("sharpe_ratio", 0),
            "sortino": raw.get("sortino_ratio", 0),
            "calmar": raw.get("calmar_ratio", 0),
            "max_drawdown_pct": round(raw.get("max_drawdown_pct", 0) * 100, 2),
            "win_rate": round(raw.get("win_rate", 0) * 100, 1),
            "profit_factor": raw.get("profit_factor", 0),
            "total_trades": raw.get("total_trades", 0),
            "winners": raw.get("winners", 0),
            "losers": raw.get("losers", 0),
            "avg_winner": raw.get("avg_winner", 0),
            "avg_loser": raw.get("avg_loser", 0),
            "avg_trade_pnl": round(raw.get("total_pnl", 0) / max(raw.get("total_trades", 1), 1), 2),
            "avg_bars_held": raw.get("avg_holding_bars", 0),
            "max_consecutive_losses": raw.get("max_consecutive_losses", 0),
            "best_trade": raw.get("avg_winner", 0),
            "worst_trade": raw.get("avg_loser", 0),
            "cagr": 0,  # computed below
            "expectancy": 0,  # computed below
        }
        # CAGR
        if result.meta.duration_days and result.meta.duration_days > 0:
            years = result.meta.duration_days / 365.25
            initial = result.meta.initial_capital or 100000
            final = initial + raw.get("total_pnl", 0)
            if initial > 0 and final > 0:
                stats["cagr"] = round(((final / initial) ** (1 / years) - 1) * 100, 2)
        # Expectancy
        wr = raw.get("win_rate", 0)
        stats["expectancy"] = round(
            wr * raw.get("avg_winner", 0) + (1 - wr) * raw.get("avg_loser", 0), 2
        )
        # PSR
        stats["psr"] = raw.get("psr")
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest/<backtest_id>/trades")
def get_backtest_trades(backtest_id: str):
    """Viewer-format trade list (flattened, unix timestamps)."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": "Not found"}), 404
    trades = [_trade_to_viewer(t) for t in result.trades]
    return jsonify(trades)


def _detect_indicators(strategy_file: str) -> list[dict]:
    """Detect indicators used by a strategy from source code with basic parameter extraction."""
    path = STRATEGIES_DIR / strategy_file
    if not path.exists():
        return []
    try:
        source = path.read_text()
    except Exception:
        return []

    import re

    found: list[dict] = []
    seen: set[tuple[str, str, int | None]] = set()

    def _add(kind: str, pane: str, period: int | None = None):
        key = (kind, pane, period)
        if key in seen:
            return
        seen.add(key)
        row = {"kind": kind, "pane": pane}
        if period is not None:
            row["period"] = period
        found.append(row)

    # vectorbt style: vbt.MA.run(..., window=20, ewm=True/False)
    for ma_call in re.finditer(r"vbt\.MA\.run\(([^)]*)\)", source, flags=re.IGNORECASE):
        args = ma_call.group(1)
        w = re.search(r"(?:window|length)\s*=\s*(\d+)", args, flags=re.IGNORECASE)
        period = int(w.group(1)) if w else 20
        if re.search(r"ewm\s*=\s*True", args, flags=re.IGNORECASE):
            _add("EMA", "overlay", period)
        else:
            _add("SMA", "overlay", period)

    # ta.* style period extraction
    for m in re.finditer(r"ta\.sma\([^,]+,\s*(\d+)\)", source, flags=re.IGNORECASE):
        _add("SMA", "overlay", int(m.group(1)))
    for m in re.finditer(r"ta\.ema\([^,]+,\s*(\d+)\)", source, flags=re.IGNORECASE):
        _add("EMA", "overlay", int(m.group(1)))
    for m in re.finditer(
        r"(?:vbt\.RSI\.run\([^)]*window\s*=\s*(\d+)|ta\.rsi\([^,]+,\s*(\d+)\))",
        source,
        flags=re.IGNORECASE,
    ):
        p = m.group(1) or m.group(2)
        _add("RSI", "panel", int(p) if p else 14)
    for m in re.finditer(
        r"(?:vbt\.ATR\.run\([^)]*window\s*=\s*(\d+)|ta\.atr\([^)]*length\s*=\s*(\d+)|ta\.atr\([^,]+,\s*[^,]+,\s*[^,]+,\s*(\d+)\))",
        source,
        flags=re.IGNORECASE,
    ):
        p = m.group(1) or m.group(2) or m.group(3)
        _add("ATR", "panel", int(p) if p else 14)
    for m in re.finditer(r"ta\.bbands\([^)]*length\s*=\s*(\d+)", source, flags=re.IGNORECASE):
        _add("BB", "overlay", int(m.group(1)))

    # Presence-based patterns
    if re.search(r"(?:vbt\.MACD\.run|ta\.macd|macd\()", source, flags=re.IGNORECASE):
        _add("MACD", "panel", None)
    if re.search(r"(?:supertrend|super_trend|ta\.supertrend)", source, flags=re.IGNORECASE):
        _add("SuperTrend", "overlay", None)

    # Fallback defaults if generic mentions exist without explicit params
    if re.search(r"\bsma\b", source, flags=re.IGNORECASE):
        _add("SMA", "overlay", 20)
    if re.search(r"\bema\b", source, flags=re.IGNORECASE):
        _add("EMA", "overlay", 12)
    if re.search(r"\brsi\b", source, flags=re.IGNORECASE):
        _add("RSI", "panel", 14)
    if re.search(r"\batr\b", source, flags=re.IGNORECASE):
        _add("ATR", "panel", 14)
    if re.search(r"(?:bbands|bollinger)", source, flags=re.IGNORECASE):
        _add("BB", "overlay", 20)

    return found


def _indicator_payload(
    name: str, pane: str, data: list[dict], color: str, line_style: int = 0
) -> dict:
    return {
        "name": name,
        "pane": pane,  # backward-compatible field
        "type": pane,  # normalized field requested by frontend/API
        "data": data,
        "color": color,
        "lineStyle": line_style,
        "style": {"color": color, "lineStyle": line_style},
    }


def _compute_indicators(
    df: pd.DataFrame, indicator_list: list[dict], ohlcv_times: list[float]
) -> list[dict]:
    """Compute indicator values from OHLCV DataFrame for given time range."""
    try:
        import pandas_ta as ta
    except ImportError:
        return []

    results = []
    seen_names: set[str] = set()
    time_set = set(ohlcv_times)

    for ind in indicator_list:
        kind = (ind.get("kind") or ind.get("name") or "").upper()
        period = int(ind.get("period")) if ind.get("period") else None
        try:
            if kind == "SMA":
                p = period or 20
                vals = ta.sma(df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"SMA({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "overlay", data, "#d29922"))
            elif kind == "EMA":
                p = period or 12
                vals = ta.ema(df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"EMA({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "overlay", data, "#f97316"))
            elif kind == "RSI":
                p = period or 14
                vals = ta.rsi(df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"RSI({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "panel", data, "#a78bfa"))
            elif kind == "ATR":
                p = period or 14
                vals = ta.atr(df["high"], df["low"], df["close"], length=p)
                if vals is not None:
                    data = _indicator_to_points(df, vals, time_set)
                    label = f"ATR({p})"
                    if data and label not in seen_names:
                        seen_names.add(label)
                        results.append(_indicator_payload(label, "panel", data, "#fb923c"))
            elif kind == "BB":
                p = period or 20
                bb = ta.bbands(df["close"], length=p, std=2)
                if bb is not None:
                    for col, label, color in [
                        (f"BBU_{p}_2.0", f"BB Upper ({p})", "#6366f1"),
                        (f"BBM_{p}_2.0", f"BB Mid ({p})", "#94a3b8"),
                        (f"BBL_{p}_2.0", f"BB Lower ({p})", "#6366f1"),
                    ]:
                        if col in bb.columns:
                            data = _indicator_to_points(df, bb[col], time_set)
                            if data and label not in seen_names:
                                seen_names.add(label)
                                results.append(
                                    _indicator_payload(
                                        label,
                                        "overlay",
                                        data,
                                        color,
                                        2 if "Mid" not in label else 0,
                                    )
                                )
            elif kind == "MACD":
                macd = ta.macd(df["close"])
                if macd is not None:
                    for col, label, color in [
                        ("MACD_12_26_9", "MACD", "#3b82f6"),
                        ("MACDs_12_26_9", "Signal", "#f97316"),
                    ]:
                        if col in macd.columns:
                            data = _indicator_to_points(df, macd[col], time_set)
                            if data and label not in seen_names:
                                seen_names.add(label)
                                results.append(_indicator_payload(label, "panel", data, color))
            elif kind == "SUPERTREND":
                st = ta.supertrend(df["high"], df["low"], df["close"], length=7, multiplier=3.0)
                if st is not None:
                    st_col = next((c for c in st.columns if c.startswith("SUPERT_")), None)
                    if st_col:
                        data = _indicator_to_points(df, st[st_col], time_set)
                        if data and "SuperTrend" not in seen_names:
                            seen_names.add("SuperTrend")
                            results.append(
                                _indicator_payload("SuperTrend", "overlay", data, "#22c55e")
                            )
        except Exception as e:
            logger.warning("Indicator %s computation failed: %s", kind, e)

    return results


def _indicator_to_points(df: pd.DataFrame, series: pd.Series, time_set: set[float]) -> list[dict]:
    """Convert a pandas Series to time/value points filtered to the OHLCV window."""
    points = []
    for i, val in series.items():
        if pd.notna(val) and i < len(df):
            ts = df.iloc[i]["timestamp"]
            unix_ts = ts.timestamp() if hasattr(ts, "timestamp") else pd.Timestamp(ts).timestamp()
            if unix_ts in time_set:
                points.append({"time": unix_ts, "value": round(float(val), 6)})
    return points


@app.route("/api/backtest/<backtest_id>/trade/<int:trade_nr>")
def get_trade_with_context(backtest_id: str, trade_nr: int):
    """Single trade with OHLCV context (BARS_BEFORE/AFTER around entry)."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": "Not found"}), 404

    trade = None
    for t in result.trades:
        if t.id == trade_nr:
            trade = t
            break
    if trade is None:
        return jsonify({"error": f"Trade #{trade_nr} not found"}), 404

    tv = _trade_to_viewer(trade)
    tf = (
        result.meta.data.get("timeframes", ["1h"])[0]
        if result.meta.data.get("timeframes")
        else "1h"
    )

    # Load OHLCV context for this trade's symbol
    ohlcv = []
    df = _load_ohlcv_df(trade.symbol, tf)
    ohlcv_start_idx = 0
    ohlcv_end_idx = 0
    if df is not None and len(df) > 0:
        entry_ts = pd.Timestamp(trade.entry_time, tz="UTC") if trade.entry_time else None
        if entry_ts is not None:
            idx = df["timestamp"].searchsorted(entry_ts)
            ohlcv_start_idx = max(0, idx - BARS_BEFORE)
            ohlcv_end_idx = min(len(df), idx + BARS_AFTER)
            ohlcv = _df_to_ohlcv_records(df.iloc[ohlcv_start_idx:ohlcv_end_idx])

    # Indicators: use embedded ones from backtest JSON, or compute from strategy
    raw = json.loads((BACKTESTS_DIR / f"{backtest_id}.trades.json").read_text())
    indicators = raw.get("indicators", [])

    if not indicators and ohlcv and df is not None:
        # Detect indicators from strategy source and compute them
        strategy_file = result.meta.strategies[0] if result.meta.strategies else ""
        # Map strategy name to file
        for sf in STRATEGIES_DIR.glob("*.py"):
            try:
                src = sf.read_text()
                if f'"name": "{strategy_file}"' in src or f"'name': '{strategy_file}'" in src:
                    strategy_file = sf.name
                    break
            except Exception:
                pass
        # Also try matching by backtest ID pattern
        if not strategy_file.endswith(".py"):
            for sf in STRATEGIES_DIR.glob("*.py"):
                if (
                    sf.stem.replace("_", "")
                    in backtest_id.replace("_", "").replace("-", "").lower()
                ):
                    strategy_file = sf.name
                    break

        detected = _detect_indicators(strategy_file)
        if detected:
            ohlcv_times = [r["time"] for r in ohlcv]
            # Use broader slice for indicator warmup (extra 50 bars before)
            warmup_start = max(0, ohlcv_start_idx - 50)
            df_slice = df.iloc[warmup_start:ohlcv_end_idx].reset_index(drop=True)
            indicators = _compute_indicators(df_slice, detected, ohlcv_times)

    # Slice embedded indicator data to OHLCV time range
    if ohlcv and indicators:
        t_start = ohlcv[0]["time"]
        t_end = ohlcv[-1]["time"]
        for ind in indicators:
            if "data" in ind:
                ind["data"] = [pt for pt in ind["data"] if t_start <= pt.get("time", 0) <= t_end]

    return jsonify(
        {
            "trade": tv,
            "ohlcv": ohlcv,
            "symbol": trade.symbol,
            "timeframe": tf,
            "indicators": indicators,
            "total_trades": len(result.trades),
        }
    )


@app.route("/api/backtest/<backtest_id>/equity")
def get_equity_curve(backtest_id: str):
    """Equity curve data for chart."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_equity_to_viewer(result))


@app.route("/api/backtest/<backtest_id>/ohlcv")
def get_backtest_ohlcv(backtest_id: str):
    """Full OHLCV data for all symbols in this backtest."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": "Not found"}), 404

    symbols = result.meta.data.get("symbols", [])
    tf = (
        result.meta.data.get("timeframes", ["1h"])[0]
        if result.meta.data.get("timeframes")
        else "1h"
    )

    ohlcv_data = {}
    for symbol in symbols[:10]:
        df = _load_ohlcv_df(symbol, tf)
        if df is not None:
            ohlcv_data[symbol] = _df_to_ohlcv_records(df)

    return jsonify(ohlcv_data)


@app.route("/api/compare")
def compare_backtests_endpoint():
    """Full comparison with combined equity, drawdowns, correlations."""
    ids_param = request.args.get("ids", "")
    if not ids_param:
        return jsonify({"error": "Provide ?ids=id1,id2,id3"}), 400

    ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    strategies = []

    for bt_id in ids:
        result = _load_result(bt_id)
        if result is None:
            continue
        try:
            stats = compute_stats_for_result(result)
            eq = _equity_to_viewer(result)
            eq_pairs = [[e["time"], e["equity"]] for e in eq]
            eq_vals = [e["equity"] for e in eq]
            dd = compute_drawdown_pct(eq_vals)
            dd_pairs = [[eq[i]["time"], round(dd[i], 4)] for i in range(len(dd))]

            strategies.append(
                {
                    "id": bt_id,
                    "name": f"{result.meta.strategies[0] if result.meta.strategies else 'unknown'} ({result.meta.engine})",
                    "equity_curve": eq_pairs,
                    "drawdown_curve": dd_pairs,
                    "stats": stats,
                    "trades": {
                        "total_trades": len(result.trades),
                        "winning_trades": sum(1 for t in result.trades if t.pnl > 0),
                        "losing_trades": sum(1 for t in result.trades if t.pnl <= 0),
                    },
                }
            )
        except Exception as e:
            logger.warning("Could not load %s for comparison: %s", bt_id, e)

    comparison = compare_backtests(strategies)
    return jsonify(comparison)


# ─── Strategy Endpoints ──────────────────────────────────────────────────────


@app.route("/api/strategies")
def list_strategies():
    """List all strategies by scanning strategy files for STRATEGY_META."""
    strategies = []
    if not STRATEGIES_DIR.exists():
        return jsonify(strategies)

    for f in sorted(STRATEGIES_DIR.glob("*.py")):
        if f.name.startswith("__"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("strat", str(f))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            meta = getattr(mod, "STRATEGY_META", None)
            if meta is None:
                continue

            # Find backtests using this strategy
            strat_name = meta.get("name", f.stem)
            bt_count = 0
            best_sharpe = None
            if BACKTESTS_DIR.exists():
                for bt_file in BACKTESTS_DIR.glob("*.trades.json"):
                    try:
                        bt_data = json.loads(bt_file.read_text())
                        bt_meta = bt_data.get("meta", {})
                        bt_strats = bt_meta.get("strategies", [])
                        if strat_name in bt_strats:
                            bt_count += 1
                    except Exception:
                        pass

            tags = meta.get("tags", {})
            strategies.append(
                {
                    "name": strat_name,
                    "version": meta.get("version", "1.0.0"),
                    "file": f.name,
                    "tags": tags,
                    "type": tags.get("type", []),
                    "source": tags.get("source", ""),
                    "asset_class": tags.get("asset_class", []),
                    "complexity": tags.get("complexity", ""),
                    "engine": "pynecore" if f.name.startswith("pynecore_") else "vectorbt",
                    "backtest_count": bt_count,
                    "best_sharpe": best_sharpe,
                }
            )
        except Exception as e:
            logger.warning("Could not load strategy %s: %s", f.name, e)

    return jsonify(strategies)


@app.route("/api/strategies/<name>")
def get_strategy(name: str):
    """Strategy details + all backtests and portfolios using it."""
    # Find strategy file
    strat_file = None
    strat_meta = None
    for f in STRATEGIES_DIR.glob("*.py"):
        if f.name.startswith("__"):
            continue
        try:
            spec = importlib.util.spec_from_file_location("strat", str(f))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            meta = getattr(mod, "STRATEGY_META", None)
            if meta and meta.get("name") == name:
                strat_file = f
                strat_meta = meta
                break
        except Exception:
            continue

    if strat_meta is None:
        return jsonify({"error": f"Strategy '{name}' not found"}), 404

    # Find related backtests
    backtests = []
    if BACKTESTS_DIR.exists():
        for bt_file in sorted(BACKTESTS_DIR.glob("*.trades.json")):
            try:
                bt_data = json.loads(bt_file.read_text())
                bt_meta = bt_data.get("meta", {})
                if name in bt_meta.get("strategies", []):
                    bt_id = bt_file.stem.replace(".trades", "")
                    trades = bt_data.get("trades", [])
                    total_pnl = sum(t.get("pnl", 0) for t in trades)
                    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
                    backtests.append(
                        {
                            "id": bt_id,
                            "engine": bt_meta.get("engine", ""),
                            "symbols": bt_meta.get("data", {}).get("symbols", []),
                            "timeframes": bt_meta.get("data", {}).get("timeframes", []),
                            "backtest_start": bt_meta.get("backtest_start", ""),
                            "backtest_end": bt_meta.get("backtest_end", ""),
                            "trade_count": len(trades),
                            "total_pnl": round(total_pnl, 2),
                            "win_rate": round(wins / len(trades) * 100, 1) if trades else 0,
                            "created_at": bt_meta.get("created_at", ""),
                        }
                    )
            except Exception:
                pass

    # Find related portfolios
    portfolios = []
    if PORTFOLIOS_DIR.exists():
        for pf_file in PORTFOLIOS_DIR.glob("*.json"):
            try:
                pf_data = json.loads(pf_file.read_text())
                pf_strats = [s.get("strategy_name", "") for s in pf_data.get("strategies", [])]
                if name in pf_strats:
                    portfolios.append(
                        {
                            "id": pf_data.get("id", pf_file.stem),
                            "name": pf_data.get("name", ""),
                        }
                    )
            except Exception:
                pass

    tags = strat_meta.get("tags", {})
    return jsonify(
        {
            "name": name,
            "version": strat_meta.get("version", "1.0.0"),
            "file": strat_file.name if strat_file else "",
            "tags": tags,
            "type": tags.get("type", []),
            "source": tags.get("source", ""),
            "link": tags.get("link", ""),
            "asset_class": tags.get("asset_class", []),
            "complexity": tags.get("complexity", ""),
            "engine": "pynecore"
            if (strat_file and strat_file.name.startswith("pynecore_"))
            else "vectorbt",
            "backtests": backtests,
            "portfolios": portfolios,
        }
    )


# ─── Dolthub Auto-Pull ────────────────────────────────────────────────────────

_DOLTHUB_REPOS = Path.home() / "data" / "dolthub"


def _dolthub_pull():
    """Non-blocking dolt pull in all Dolthub repos. Logs warnings on failure."""
    if not _DOLTHUB_REPOS.is_dir():
        return
    for repo in ("stocks", "earnings", "options", "rates"):
        repo_path = _DOLTHUB_REPOS / repo
        if not (repo_path / ".dolt").is_dir():
            continue
        try:
            subprocess.run(
                ["dolt", "pull"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as e:
            app.logger.warning("dolt pull %s failed: %s", repo, e)


# ─── Backtest Run Endpoints ──────────────────────────────────────────────────


@app.route("/api/backtests/run", methods=["POST"])
def run_backtest():
    """Start a backtest asynchronously."""
    # Auto-pull Dolthub data (non-blocking, best-effort)
    try:
        threading.Thread(target=_dolthub_pull, daemon=True).start()
    except Exception:
        pass
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    strategy = data.get("strategy", "")
    engine = data.get("engine", "vectorbt")
    symbols = data.get("symbols", [])
    timeframe = data.get("timeframe", "1d")
    start = data.get("start", "")
    end = data.get("end", "")
    capital = data.get("capital", 100000)
    risk_pct = data.get("risk_per_trade_pct")
    max_position_pct = data.get("max_position_pct")
    max_leverage = data.get("max_leverage")
    params = data.get("parameters", {})

    if not strategy or not symbols:
        return jsonify({"error": "strategy and symbols required"}), 400

    job_id = str(uuid.uuid4())[:8]
    _running_jobs[job_id] = {"status": "running", "result": None, "error": None}

    def _run():
        try:
            cmd = [
                sys.executable,
                "-m",
                "runner.cli",
                "run",
                "--engine",
                engine,
                "--strategy",
                f"strategies/{strategy}",
                "--symbols",
                *symbols,
                "--timeframe",
                timeframe,
                "--capital",
                str(capital),
            ]
            if start:
                cmd.extend(["--start", start])
            if end:
                cmd.extend(["--end", end])
            if params:
                cmd.extend(["--params", json.dumps(params)])
            if risk_pct is not None:
                cmd.extend(["--risk-pct", str(risk_pct)])
            if max_position_pct is not None:
                cmd.extend(["--max-position-pct", str(max_position_pct)])
            if max_leverage is not None:
                cmd.extend(["--max-leverage", str(max_leverage)])

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(Path(__file__).parent.parent),
            )
            if proc.returncode == 0:
                # Extract output path from runner output
                output = proc.stdout + proc.stderr
                _running_jobs[job_id]["status"] = "completed"
                _running_jobs[job_id]["result"] = output
            else:
                _running_jobs[job_id]["status"] = "failed"
                _running_jobs[job_id]["error"] = proc.stderr or proc.stdout
        except Exception as e:
            _running_jobs[job_id]["status"] = "failed"
            _running_jobs[job_id]["error"] = str(e)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/backtests/run/<job_id>/status")
def get_backtest_run_status(job_id: str):
    """Check status of a running backtest."""
    job = _running_jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ─── Portfolio Endpoints ─────────────────────────────────────────────────────


@app.route("/api/portfolios")
def list_portfolios():
    """List all portfolios."""
    portfolios = []
    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(PORTFOLIOS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            cs = data.get("combined_stats", {})

            # Build mini equity from combined_equity
            equity_full = data.get("combined_equity", [])
            if equity_full:
                vals = [e.get("equity", e) if isinstance(e, dict) else e for e in equity_full]
                n = len(vals)
                if n <= 50:
                    equity_mini = vals
                else:
                    step = n / 50
                    equity_mini = [vals[int(i * step)] for i in range(50)]
                    equity_mini.append(vals[-1])
            else:
                equity_mini = []

            # Convert decimal stats to percentages for frontend consistency
            cagr_raw = cs.get("cagr", 0) or 0
            md_raw = cs.get("max_drawdown", 0) or 0
            net_profit = cs.get("net_profit", 0) or 0
            capital = data.get("account_capital", 100000) or 100000

            portfolios.append(
                {
                    "id": data.get("id", f.stem),
                    "name": data.get("name", ""),
                    "account_capital": capital,
                    "strategy_count": len(data.get("strategies", [])),
                    "sharpe": round(cs.get("sharpe", 0) or 0, 3),
                    "cagr": round(cagr_raw * 100, 2),  # decimal → percent
                    "max_drawdown_pct": round(abs(md_raw) * 100, 2),  # decimal → percent (positive)
                    "net_profit": round(net_profit, 2),
                    "net_profit_pct": round(net_profit / capital * 100, 2) if capital else 0,
                    "equity_mini": equity_mini,
                    "created_at": data.get("created_at", ""),
                }
            )
        except Exception as e:
            logger.warning("Could not read portfolio %s: %s", f.name, e)
    return jsonify(portfolios)


@app.route("/api/portfolios", methods=["POST"])
def create_portfolio():
    """Create a new portfolio with risk-sized trade simulation."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    name = data.get("name", "Untitled")
    account_capital = data.get("account_capital", 100000.0)
    strat_configs = data.get("strategies", [])

    if not strat_configs:
        return jsonify({"error": "At least one strategy required"}), 400

    # Load all trades from referenced backtests
    strategy_returns = []
    strategy_info = []

    import pandas as pd

    for sc in strat_configs:
        bt_id = sc.get("backtest_id", "")
        strat_name_input = sc.get("name", "")
        # Treat risk_per_trade_pct as a leverage factor relative to the original backtest.
        # Default 1.0 means "Same risk as backtest". 2.0 means "Double risk".
        risk_factor = sc.get("risk_per_trade_pct", 1.0)

        # If no backtest_id but strategy name given, find latest backtest
        if not bt_id and strat_name_input and BACKTESTS_DIR.exists():
            norm_input = strat_name_input.lower().replace("_", " ").replace("-", " ")
            candidates = []
            for f in BACKTESTS_DIR.glob("*.trades.json"):
                try:
                    d = json.loads(f.read_text())
                    m = d.get("meta", {})
                    strategies = m.get("strategies", [])
                    if any(
                        s.lower().replace("_", " ").replace("-", " ") == norm_input
                        for s in strategies
                    ):
                        candidates.append((m.get("created_at", ""), f.stem.replace(".trades", "")))
                except Exception:
                    pass
            if candidates:
                candidates.sort(reverse=True)
                bt_id = candidates[0][1]
            else:
                return jsonify(
                    {"error": f"No backtest found for strategy '{strat_name_input}'"}
                ), 404

        result = _load_result(bt_id)
        if result is None:
            return jsonify({"error": f"Backtest '{bt_id}' not found"}), 404

        if not result.equity_curve:
            continue

        strat_name = result.meta.strategies[0] if result.meta.strategies else bt_id
        strategy_info.append(
            {
                "backtest_id": bt_id,
                "strategy_name": strat_name,
                "risk_per_trade_pct": risk_factor,
                "max_positions": sc.get("max_positions", 99),
            }
        )

        # Convert equity curve to daily returns
        df = pd.DataFrame([e.to_dict() for e in result.equity_curve])
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df.set_index("time", inplace=True)
        # Resample to daily to ensure alignment
        daily_equity = df["equity"].resample("D").last().ffill()
        daily_rets = daily_equity.pct_change().fillna(0.0)

        # Apply risk factor
        weighted_rets = daily_rets * risk_factor
        weighted_rets.name = strat_name
        strategy_returns.append(weighted_rets)

    if not strategy_returns:
        return jsonify({"error": "No valid strategies found"}), 400

    # Combine returns
    combined_df = pd.concat(strategy_returns, axis=1).fillna(0.0)
    portfolio_daily_rets = combined_df.sum(axis=1)

    # Reconstruct equity curve
    current_equity = account_capital
    combined_equity_points = []
    # Initial point (optional, or start from first date)
    # combined_equity_points.append(EquityPoint(time=portfolio_daily_rets.index[0].isoformat(), equity=account_capital))

    for ts, ret in portfolio_daily_rets.items():
        current_equity *= 1 + ret
        combined_equity_points.append(EquityPoint(time=ts.isoformat(), equity=current_equity))

    # Compute stats
    from runner.stats import compute_stats

    stats_result = compute_stats(
        trades=[], initial_capital=account_capital, equity_curve=combined_equity_points
    )

    # Calculate contributions (approximate via cumulative sum of weighted returns)
    total_pnl = current_equity - account_capital
    contributions = []

    # Calculate individual cumulative returns for contribution
    for col in combined_df.columns:
        # This is an approximation of contribution to PnL
        # PnL contribution ~= Sum(Daily_Ret) * Capital? No, compounding makes it complex.
        # Simple attribution: Sum of daily returns relative to total sum of daily returns?
        # Or: Re-run equity curve for just this strategy?
        # Let's use simple cumulative sum of returns as proxy for "Effort"
        cum_ret = combined_df[col].sum()
        # But we need dollar contribution.
        # Let's roughly estimate:
        # We can't easily decouple PnL in a compounded portfolio without running them individually.
        # But we CAN run them individually with the risk factor!

        # Run isolated equity for this strategy
        s_eq = account_capital
        for r in combined_df[col]:
            s_eq *= 1 + r
        s_pnl = s_eq - account_capital

        contributions.append(
            {
                "strategy_name": col,
                "pnl_contribution": round(s_pnl, 2),
                "pnl_contribution_pct": 0,  # Placeholder
            }
        )

    # Normalize contribution pct
    sum_abs_pnl = sum(abs(c["pnl_contribution"]) for c in contributions)
    for c in contributions:
        # This is % of total absolute PnL moved, or % of Net Profit?
        # Usually % of Net Profit.
        if total_pnl != 0:
            c["pnl_contribution_pct"] = round(c["pnl_contribution"] / total_pnl * 100, 1)
        else:
            c["pnl_contribution_pct"] = 0

    now = datetime.now(timezone.utc)
    portfolio_id = f"{name.lower().replace(' ', '-')}-{now.strftime('%Y%m%d%H%M%S')}"

    portfolio = {
        "id": portfolio_id,
        "name": name,
        "created_at": now.isoformat(),
        "account_capital": account_capital,
        "strategies": strategy_info,
        "combined_stats": {
            "sharpe": stats_result.get("sharpe_ratio", 0),
            "sortino": stats_result.get("sortino_ratio", 0),
            "cagr": stats_result.get("cagr", 0),
            "max_drawdown": stats_result.get("max_drawdown_pct", 0),
            "net_profit": stats_result.get("total_pnl", 0),
            "win_rate": stats_result.get("win_rate", 0),
            "profit_factor": stats_result.get("profit_factor", 0),
            "correlation_matrix": corr_matrix,
            # Add other useful stats
            "calmar": stats_result.get("calmar_ratio", 0),
            "total_return": stats_result.get("total_return", 0),
            "max_drawdown_duration": stats_result.get("max_drawdown_duration", 0),
        },
        "combined_equity": combined_equity,
        "strategy_contributions": contributions,
        "trades": all_trades_timed,
    }

    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    out_path.write_text(json.dumps(portfolio, indent=2))

    return jsonify(portfolio), 201


@app.route("/api/portfolios/<portfolio_id>")
def get_portfolio(portfolio_id: str):
    """Get portfolio details."""
    path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    if not path.exists():
        return jsonify({"error": "Portfolio not found"}), 404
    return jsonify(json.loads(path.read_text()))


@app.route("/api/portfolios/<portfolio_id>", methods=["DELETE"])
def delete_portfolio(portfolio_id: str):
    """Delete a portfolio."""
    path = PORTFOLIOS_DIR / f"{portfolio_id}.json"
    if not path.exists():
        return jsonify({"error": "Portfolio not found"}), 404
    path.unlink()
    return jsonify({"status": "deleted"})


@app.route("/api/portfolios/compare")
def compare_portfolios():
    """Compare multiple portfolios."""
    ids_param = request.args.get("ids", "")
    if not ids_param:
        return jsonify({"error": "Provide ?ids=id1,id2"}), 400

    ids = [i.strip() for i in ids_param.split(",") if i.strip()]
    portfolios = []

    for pid in ids:
        path = PORTFOLIOS_DIR / f"{pid}.json"
        if path.exists():
            portfolios.append(json.loads(path.read_text()))

    return jsonify({"portfolios": portfolios})


# ─── Log Endpoints ───────────────────────────────────────────────────────────


def _available_log_files() -> list[Path]:
    if not LOGS_DIR.exists():
        return []
    return sorted(LOGS_DIR.glob("*.log"))


def _normalize_log_levels(raw_levels: str) -> set[str]:
    aliases = {
        "WARN": "WARNING",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "ERR": "ERROR",
        "INFO": "INFO",
        "DEBUG": "DEBUG",
    }
    normalized: set[str] = set()
    for level in raw_levels.split(","):
        candidate = level.strip().upper()
        if not candidate:
            continue
        normalized.add(aliases.get(candidate, candidate))
    return normalized


def _parse_log_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_log_level(line: str, payload: dict | None = None) -> str | None:
    if isinstance(payload, dict):
        for key in ("level", "severity"):
            value = payload.get(key)
            if isinstance(value, str):
                normalized = _normalize_log_levels(value)
                return next(iter(normalized), value.upper())

    upper_line = line.upper()
    for candidate in ("ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
        if candidate in upper_line:
            normalized = _normalize_log_levels(candidate)
            return next(iter(normalized), candidate)
    return None


def _collect_log_entries(paths: list[Path]) -> list[dict]:
    entries: list[dict] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines):
            payload: dict | None = None
            timestamp: datetime | None = None
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    payload = parsed
                    timestamp = _parse_log_timestamp(parsed.get("timestamp"))
            except json.JSONDecodeError:
                payload = None
            entries.append(
                {
                    "line": line,
                    "timestamp": timestamp,
                    "level": _extract_log_level(line, payload),
                    "path": path.name,
                    "line_number": line_number,
                }
            )
    return entries


def _warning_message_from_entry(entry: dict) -> str | None:
    messages: list[str] = []
    validation = entry.get("validation")
    if isinstance(validation, dict):
        warnings = validation.get("warnings")
        if isinstance(warnings, list):
            messages.extend(str(item).strip() for item in warnings if str(item).strip())

        gaps = validation.get("gaps")
        if isinstance(gaps, list) and gaps:
            first_gap = gaps[0] if isinstance(gaps[0], dict) else None
            if first_gap and first_gap.get("start") and first_gap.get("end"):
                messages.append(
                    f"{len(gaps)} data gaps detected (first: {first_gap['start']} -> {first_gap['end']})"
                )
            else:
                messages.append(f"{len(gaps)} data gaps detected")
        elif (
            isinstance(gaps, int)
            and gaps > 0
            and not any("gap" in message.lower() for message in messages)
        ):
            messages.append(f"{gaps} data gaps detected")

        errors = validation.get("errors")
        if isinstance(errors, list):
            messages.extend(str(item).strip() for item in errors if str(item).strip())

    if entry.get("error"):
        messages.append(str(entry["error"]).strip())

    seen: set[str] = set()
    unique_messages = []
    for message in messages:
        if message and message not in seen:
            seen.add(message)
            unique_messages.append(message)
    return " | ".join(unique_messages) if unique_messages else None


@app.route("/api/logs")
def list_logs():
    """List available log files."""
    log_files = _available_log_files()
    total_size = sum(path.stat().st_size for path in log_files) if log_files else 0
    latest_modified = (
        max(
            datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc) for path in log_files
        ).isoformat()
        if log_files
        else None
    )
    logs = [
        {
            "name": ALL_LOGS_NAME,
            "size_bytes": total_size,
            "modified": latest_modified,
        }
    ]
    for f in log_files:
        logs.append(
            {
                "name": f.name,
                "size_bytes": f.stat().st_size,
                "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return jsonify(logs)


@app.route("/api/logs/<log_name>")
def get_log(log_name: str):
    """Get last N lines of a log file, optionally filtered by level."""
    if log_name == ALL_LOGS_NAME:
        paths = _available_log_files()
    else:
        path = LOGS_DIR / log_name
        paths = [path] if path.exists() else []

    if log_name != ALL_LOGS_NAME and not paths:
        return jsonify({"error": "Log file not found"}), 404

    lines_count = request.args.get("lines", 200, type=int)
    level_filter = _normalize_log_levels(request.args.get("level", ""))
    search = request.args.get("search", "")

    try:
        entries = _collect_log_entries(paths)
        if log_name == ALL_LOGS_NAME:
            entries.sort(
                key=lambda item: (
                    item["timestamp"] is None,
                    item["timestamp"] or datetime.max.replace(tzinfo=timezone.utc),
                    item["path"],
                    item["line_number"],
                )
            )

        if level_filter:
            entries = [entry for entry in entries if entry.get("level") in level_filter]

        if search:
            entries = [entry for entry in entries if search.lower() in entry["line"].lower()]

        total_lines = len(entries)
        recent_entries = entries[-lines_count:] if len(entries) > lines_count else entries
        return jsonify(
            {
                "name": log_name,
                "lines": [entry["line"] for entry in recent_entries],
                "total_lines": total_lines,
            }
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Benchmark Endpoint ─────────────────────────────────────────────────────


@app.route("/api/benchmark/<symbol>")
def get_benchmark(symbol: str):
    """Get Buy & Hold equity curve for a symbol (benchmark comparison)."""
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    timeframe = request.args.get("timeframe", "1d")

    df = _load_ohlcv_df(symbol, timeframe)
    # Fallback: try lower timeframes and resample to daily
    if df is None and timeframe == "1d":
        for fallback_tf in ["4h", "1h", "15m"]:
            df = _load_ohlcv_df(symbol, fallback_tf)
            if df is not None:
                # Resample to daily
                if "timestamp" in df.columns:
                    df = df.set_index("timestamp")
                df = (
                    df.resample("1D")
                    .agg(
                        {
                            "open": "first",
                            "high": "max",
                            "low": "min",
                            "close": "last",
                            "volume": "sum",
                        }
                    )
                    .dropna()
                    .reset_index()
                )
                df.rename(
                    columns={"index": "timestamp"} if "index" in df.columns else {}, inplace=True
                )
                if "timestamp" not in df.columns and df.index.name == "timestamp":
                    df = df.reset_index()
                timeframe = "1d"
                logger.info("Benchmark %s: using %s resampled to 1d", symbol, fallback_tf)
                break
    if df is None:
        return jsonify({"error": f"No data for {symbol}/{timeframe}"}), 404

    if start and "timestamp" in df.columns:
        df = df[df["timestamp"] >= start]
    if end and "timestamp" in df.columns:
        df = df[df["timestamp"] <= end]

    if df.empty:
        return jsonify({"error": "No data in range"}), 404

    # Buy & Hold equity: invest $100k at first close
    initial = 100000.0
    first_close = float(df.iloc[0]["close"])
    shares = initial / first_close

    equity = []
    for _, row in df.iterrows():
        ts = row["timestamp"]
        unix_ts = ts.timestamp() if hasattr(ts, "timestamp") else pd.Timestamp(ts).timestamp()
        equity.append(
            {
                "time": unix_ts,
                "equity": round(float(row["close"]) * shares, 2),
            }
        )

    return jsonify(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "equity": equity,
        }
    )


# ─── Symbol Catalog ──────────────────────────────────────────────────────────


@app.route("/api/symbols")
def list_symbols():
    """Return available symbols grouped by category for autocomplete."""
    catalog_path = DATA_DIR / "catalog.json"
    if not catalog_path.exists():
        return jsonify({"symbols": [], "categories": {}})

    import json

    with open(catalog_path) as f:
        catalog = json.load(f)

    categories: dict[str, list[str]] = {}
    seen: set[str] = set()
    for ds in catalog.get("ohlcv", []):
        sym = ds.get("symbol", "")
        if not sym or sym in seen:
            continue
        seen.add(sym)

        # Derive category from path for better grouping
        path = str(ds.get("path", "")).lower()
        if "/metals/" in path:
            cat = "metals"
        elif "/indices/" in path:
            cat = "indices"
        elif "/energy/" in path:
            cat = "energy"
        elif "/forex/" in path:
            cat = "forex"
        elif "/equity/" in path or "/stocks/" in path:
            cat = "equity"
        elif "/etf/" in path:
            cat = "etf"
        elif "/crypto/" in path:
            cat = "crypto"
        else:
            cat = ds.get("asset_class", "other")

        categories.setdefault(cat, []).append(sym)

    for cat in categories:
        categories[cat].sort()

    return jsonify(
        {
            "symbols": sorted(seen),
            "categories": categories,
        }
    )


# ─── Data Management ─────────────────────────────────────────────────────────

DATA_TASKS = {}


def _categorize_path(path_str: str) -> str:
    """Derive asset category from file path."""
    p = path_str.lower()
    # Symbol-level overrides (files in wrong directory)
    if "/xauusd/" in p:
        return "Metals"
    if "/metals/" in p:
        return "Metals"
    if "/indices/" in p or "/index/" in p:
        return "Indices"
    if "/energy/" in p:
        return "Energy"
    if "/forex/" in p:
        return "Forex"
    if "/equity/" in p or "/stocks/" in p:
        return "Equities"
    if "/etf/" in p:
        return "ETFs"
    if "/crypto/" in p:
        return "Crypto"
    if "/options/" in p:
        return "Options"
    if "/rates/" in p:
        return "Rates"
    if "/earnings/" in p:
        return "Earnings"
    return "Other"


def _scan_ohlcv_filesystem() -> list[dict]:
    """Scan ~/data/ohlcv/ when no catalog cache is available."""
    return scan_ohlcv(DATA_DIR)


def _load_catalog_ohlcv_entries() -> tuple[list[dict], bool]:
    """Load cached OHLCV catalog entries if catalog.json is available."""
    catalog_path = DATA_DIR / "catalog.json"
    if not catalog_path.exists():
        return [], False

    try:
        with open(catalog_path) as f:
            catalog = json.load(f)
    except Exception:
        return [], False

    return catalog.get("ohlcv", []), True


def _dir_size_bytes(root: Path) -> int:
    """Compute recursive directory size in bytes."""
    total = 0
    for dirpath, _, filenames in os.walk(root):
        for filename in filenames:
            fp = Path(dirpath) / filename
            try:
                total += fp.stat().st_size
            except OSError:
                continue
    return total


def _refresh_catalog() -> None:
    """Rebuild the data catalog after file mutations."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    catalog = build_catalog(DATA_DIR)
    (DATA_DIR / "catalog.json").write_text(json.dumps(catalog, indent=2, default=str))


def _task_log_path(task_id: str) -> Path:
    return TASK_LOGS_DIR / f"task_{task_id}.log"


def _read_task_log(log_file: str | Path, *, max_bytes: int | None = None) -> str:
    path = Path(log_file)
    if not path.exists():
        return ""

    if max_bytes is None:
        return path.read_text(encoding="utf-8", errors="replace")

    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        return handle.read().decode("utf-8", errors="replace")


def _append_task_log(log_file: Path, message: str) -> None:
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(message)


def _write_task_log_line(
    handle,
    task_id: str,
    message: str,
    *,
    stream: str,
    log_level: str = "info",
) -> None:
    if not message.endswith("\n"):
        message = f"{message}\n"
    handle.write(message)
    handle.flush()

    text = message.rstrip()
    if not text:
        return

    log_method = getattr(logger, log_level, logger.info)
    log_method(
        text,
        extra={"data": {"task_id": task_id, "stream": stream, "source": "data-task"}},
    )


def _start_data_task(task_id: str, cmd: list[str], *, total_items: int = 0) -> dict:
    """Launch a background data-management subprocess with streamed task logs."""
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    DATA_TASKS[task_id] = {
        "status": "starting",
        "log_file": str(log_file),
        "total_items": total_items,
    }

    def run_proc() -> None:
        started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        command_display = " ".join(str(part) for part in cmd)

        try:
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{started_at}] Starting command: {command_display}",
                    stream="task",
                )

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=os.getcwd(),
                    env=env,
                    text=True,
                    bufsize=1,
                )
                DATA_TASKS[task_id] = {
                    "status": "running",
                    "pid": proc.pid,
                    "log_file": str(log_file),
                    "total_items": total_items,
                }

                if proc.stdout is not None:
                    for line in proc.stdout:
                        _write_task_log_line(handle, task_id, line.rstrip("\n"), stream="stdout")

                returncode = proc.wait()
                status = "completed" if returncode == 0 else "failed"

                if returncode == 0:
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] Refreshing catalog...",
                        stream="task",
                    )
                    try:
                        _refresh_catalog()
                    except Exception as exc:
                        status = "failed"
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] "
                            f"Catalog refresh failed: {exc}",
                            stream="task",
                            log_level="error",
                        )

                DATA_TASKS[task_id].update({"status": status, "returncode": returncode})
        except Exception as exc:
            DATA_TASKS[task_id].update({"status": "failed", "error": str(exc)})
            _append_task_log(
                log_file,
                f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] Task failed: {exc}\n",
            )
            logger.error(
                "Task failed: %s",
                exc,
                extra={"data": {"task_id": task_id, "stream": "task", "source": "data-task"}},
            )

    thread = threading.Thread(target=run_proc, daemon=True)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def _dolthub_repo_status(repo_path: Path) -> tuple[dict, str]:
    if repo_path.exists():
        return {"status": "ok"}, ""
    return {"status": "warning"}, "Repository directory is missing"


def _scan_dolthub_repos() -> list[dict]:
    """Scan ~/data/dolthub repos and return size metadata."""
    repos_root = DATA_DIR / "dolthub"
    if not repos_root.exists():
        return []

    repos: list[dict] = []
    for repo_name in ("stocks", "earnings", "options", "rates"):
        repo_path = repos_root / repo_name
        if not repo_path.exists():
            continue
        size = _dir_size_bytes(repo_path)
        try:
            updated_at = datetime.fromtimestamp(
                repo_path.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        except Exception:
            updated_at = None
        repos.append(
            {
                "repo": repo_name,
                "symbol": repo_name,
                "timeframe": "repo",
                "path": str(repo_path),
                "source": "dolthub",
                "size_bytes": size,
                "last_updated": updated_at,
                "is_repo": True,
            }
        )
    return repos


@app.route("/api/data/overview")
def data_overview():
    """Return summary of available data categories."""
    all_entries, used_catalog = _load_catalog_ohlcv_entries()
    if not used_catalog:
        all_entries = _scan_ohlcv_filesystem()

    categories: dict[str, dict] = {}
    for entry in all_entries:
        cat = _categorize_path(str(entry.get("path", "")))
        categories.setdefault(
            cat, {"count": 0, "size_mb": 0, "symbols": set(), "start": None, "end": None}
        )
        categories[cat]["count"] += 1
        size = entry.get("size_bytes", 0)
        if size == 0:
            try:
                p = Path(entry.get("path", ""))
                if p.exists():
                    size = p.stat().st_size
            except Exception:
                pass
        categories[cat]["size_mb"] += size / (1024 * 1024)
        categories[cat]["symbols"].add(entry.get("symbol"))
        start = entry.get("start")
        end = entry.get("end")
        if start and (categories[cat]["start"] is None or start < categories[cat]["start"]):
            categories[cat]["start"] = start
        if end and (categories[cat]["end"] is None or end > categories[cat]["end"]):
            categories[cat]["end"] = end

    # Add Dolthub repos as dedicated category (real repo sizes, not parquet exports)
    dolthub_repos = _scan_dolthub_repos()
    if dolthub_repos:
        categories["Dolthub Repos"] = {
            "count": len(dolthub_repos),
            "size_mb": sum(r.get("size_bytes", 0) for r in dolthub_repos) / (1024 * 1024),
            "symbols": {r.get("repo") for r in dolthub_repos},
            "start": None,
            "end": None,
        }

    result = []
    for name, data in sorted(categories.items()):
        result.append(
            {
                "name": name,
                "symbol_count": len(data["symbols"]),
                "file_count": data["count"],
                "total_size_mb": round(data["size_mb"], 2),
                "start": data["start"],
                "end": data["end"],
            }
        )
    return jsonify({"categories": result})


@app.route("/api/data/category/<category_name>")
def data_category_details(category_name):
    """Return detailed symbol list for a category."""
    target = category_name.lower()

    if target == "dolthub repos":
        repos = []
        for repo in _scan_dolthub_repos():
            validation, validation_message = _dolthub_repo_status(Path(repo.get("path", "")))
            repos.append(
                {
                    "symbol": repo.get("repo"),
                    "timeframe": "repo",
                    "path": repo.get("path"),
                    "size_mb": round(repo.get("size_bytes", 0) / (1024 * 1024), 2),
                    "start": None,
                    "end": None,
                    "source": "dolthub",
                    "repo": True,
                    "last_updated": repo.get("last_updated"),
                    "validation": validation,
                    "warning_message": validation_message,
                    "validation_message": validation_message,
                }
            )
        return jsonify({"symbols": sorted(repos, key=lambda x: x["symbol"] or "")})

    all_entries, used_catalog = _load_catalog_ohlcv_entries()
    if not used_catalog:
        all_entries = _scan_ohlcv_filesystem()

    symbols = []
    for entry in all_entries:
        cat = _categorize_path(str(entry.get("path", ""))).lower()
        if cat == target:
            size = entry.get("size_bytes", 0)
            if size == 0:
                try:
                    p = Path(entry.get("path", ""))
                    if p.exists():
                        size = p.stat().st_size
                except Exception:
                    pass
            symbol_entry = {
                "symbol": entry.get("symbol"),
                "timeframe": entry.get("timeframe"),
                "path": entry.get("path"),
                "size_mb": round(size / (1024 * 1024), 2),
                "start": entry.get("start"),
                "end": entry.get("end"),
                "source": entry.get("source"),
            }
            if entry.get("validation") is not None:
                symbol_entry["validation"] = entry.get("validation")
            if entry.get("error") is not None:
                symbol_entry["error"] = entry.get("error")
            warning_message = _warning_message_from_entry(entry)
            if warning_message is not None:
                symbol_entry["warning_message"] = warning_message
                symbol_entry["validation_message"] = warning_message
            if entry.get("repo"):
                symbol_entry["repo"] = True
            if entry.get("last_updated") is not None:
                symbol_entry["last_updated"] = entry.get("last_updated")
            symbols.append(symbol_entry)
    return jsonify(
        {
            "symbols": sorted(
                symbols, key=lambda item: (item.get("symbol") or "", item.get("timeframe") or "")
            )
        }
    )


@app.route("/api/data/download", methods=["POST"])
def data_download():
    """Start a background download task."""
    data = request.get_json()
    source = data.get("source", "dukascopy")
    symbols = data.get("symbols", "")  # comma separated
    timeframes = data.get("timeframes", "1m")
    start = data.get("start", "")
    end = data.get("end", "")
    price_type = data.get("price_type", "bid")
    merge_tick = bool(data.get("merge_tick", data.get("merge_bid_ask", False)))
    merge_s1 = bool(data.get("merge_s1", data.get("merge_bid_ask", False)))
    sym_list = _parse_symbol_list(symbols)
    tf_list = [
        _normalize_dukascopy_genesis_timeframe(timeframe)
        for timeframe in _parse_timeframe_list(timeframes)
    ]

    if source == "dukascopy" and start:
        genesis_index = _load_dukascopy_genesis()
        too_early: dict[str, str] = {}
        for symbol in sym_list:
            symbol_data = genesis_index.get(symbol, {})
            for timeframe in tf_list:
                genesis = symbol_data.get(timeframe) or symbol_data.get("default")
                if genesis and start < genesis:
                    too_early[f"{symbol}/{timeframe}"] = genesis
        if too_early:
            details = ", ".join(
                f"{symbol_tf} >= {genesis}" for symbol_tf, genesis in sorted(too_early.items())
            )
            return jsonify(
                {
                    "error": f"Start date {start} precedes Dukascopy genesis for: {details}",
                }
            ), 400

    task_id = str(uuid.uuid4())

    cmd = []
    if source == "dukascopy":
        # python -m data_tools.downloaders.dukascopy_dl --symbols ...
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.dukascopy_dl",
            "--symbols",
            symbols,
            "--timeframes",
            timeframes,
        ]
        if start:
            cmd.extend(["--start", start])
        if end:
            cmd.extend(["--end", end])
        if price_type:
            cmd.extend(["--price-type", str(price_type)])
        if merge_tick:
            cmd.append("--merge-tick")
        if merge_s1:
            cmd.append("--merge-s1")
    elif source == "yfinance":
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.yfinance_dl",
            "--symbols",
            symbols,
        ]
    elif source == "dolthub":
        # python -m data_tools.dolthub_manager update
        cmd = [sys.executable, "-u", "-m", "data_tools.dolthub_manager", "update"]
    else:
        return jsonify({"error": "Unknown source"}), 400

    # Count total items for progress tracking
    total_items = max(len(sym_list) * len(tf_list), 1)
    return jsonify(_start_data_task(task_id, cmd, total_items=total_items))


@app.route("/api/data/dukascopy/genesis")
def get_dukascopy_genesis():
    """Return discovered Dukascopy genesis dates for one or more symbols."""
    requested_symbols = _parse_symbol_list(request.args.get("symbols", ""))
    requested_timeframes = _parse_timeframe_list(request.args.get("timeframes", ""))
    genesis_index = _load_dukascopy_genesis()
    filtered_genesis, lower_bound = _collect_dukascopy_genesis(
        genesis_index,
        symbols=requested_symbols or None,
        timeframes=requested_timeframes or None,
    )
    return jsonify(
        {
            "source": "dukascopy",
            "genesis": filtered_genesis,
            "lower_bound": lower_bound,
        }
    )


@app.route("/api/data/resample", methods=["POST"])
def data_resample():
    """Start a background resample task."""
    data = request.get_json()
    source_path = data.get("source_path", "")
    target_tfs = data.get("target_timeframes", "")  # comma separated

    task_id = str(uuid.uuid4())

    cmd = [
        sys.executable,
        "-u",
        "-m",
        "data_tools.resample",
        "--source",
        source_path,
        "--timeframes",
        target_tfs,
    ]
    return jsonify(_start_data_task(task_id, cmd))


def _parse_progress(log_content: str, task: dict) -> dict:
    """Parse download progress from log content."""
    completed = log_content.count("✅")
    failed = log_content.count("❌")
    total = task.get("total_items", 0)
    if total > 0:
        pct = min(100, int((completed + failed) / total * 100))
    else:
        pct = 100 if task.get("status") == "completed" else 0
    # Extract last meaningful line
    lines = [l.strip() for l in log_content.strip().split("\n") if l.strip()]
    last_line = lines[-1] if lines else ""
    return {
        "completed": completed,
        "failed": failed,
        "total": total,
        "percent": pct,
        "current": last_line,
    }


@app.route("/api/data/tasks/<task_id>")
def get_data_task(task_id):
    """Get status of a background task."""
    task = DATA_TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404

    log_content = (
        _read_task_log(task.get("log_file", ""), max_bytes=2048) if "log_file" in task else ""
    )

    return jsonify(
        {
            "id": task_id,
            "status": task.get("status"),
            "log": log_content,
            "progress": _parse_progress(log_content, task),
        }
    )


@app.route("/api/data/tasks/<task_id>/logs")
def get_data_task_logs(task_id):
    """Return the full task log for a background data-management job."""
    task = DATA_TASKS.get(task_id)
    log_file = _task_log_path(task_id)
    if task is None and not log_file.exists():
        return jsonify({"error": "Task not found"}), 404

    return jsonify(
        {
            "logs": _read_task_log(task.get("log_file", log_file) if task else log_file),
            "status": task.get("status", "unknown") if task else "unknown",
        }
    )


@app.route("/api/data/symbol", methods=["DELETE"])
def delete_symbol_data():
    """Delete a specific data file."""
    path_str = request.args.get("path", "")
    if not path_str:
        return jsonify({"error": "Path required"}), 400

    p = Path(path_str)
    # Security check: must be inside DATA_DIR
    try:
        p = p.resolve()
        # resolve DATA_DIR too
        dd = DATA_DIR.resolve()
        if not str(p).startswith(str(dd)):
            return jsonify({"error": "Access denied"}), 403
    except Exception:
        return jsonify({"error": "Invalid path"}), 400

    if p.exists():
        try:
            p.unlink()
            # Update catalog
            _refresh_catalog()
            return jsonify({"status": "deleted"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "File not found"}), 404


# ─── Frontend ─────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/<path:path>")
def static_files(path):
    # Try serving the file directly (JS, CSS, images, etc.)
    full_path = Path(STATIC_DIR) / path
    if full_path.exists() and full_path.is_file():
        return send_from_directory(str(STATIC_DIR), path)
    # SPA fallback: return index.html for React Router
    return send_from_directory(str(STATIC_DIR), "index.html")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Trade Viewer Server")
    parser.add_argument("--port", type=int, default=5556, help="Port (default: 5556)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--backtests", type=str, help="Backtests directory override")
    args = parser.parse_args()

    global BACKTESTS_DIR
    if args.backtests:
        BACKTESTS_DIR = Path(args.backtests)

    logger.info("Serving backtests from %s", BACKTESTS_DIR)
    logger.info("Trade Viewer: http://localhost:%d", args.port)
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
