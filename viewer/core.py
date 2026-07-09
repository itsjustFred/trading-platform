"""
Shared viewer helper logic used by the FastAPI backend in `viewer/main.py`.

This module keeps strategy/backtest/data/task utilities and legacy helper
functions that are consumed by the FastAPI routes.
"""

from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from pydantic import ValidationError

from data_tools import cache_manager
from data_tools.catalog_builder import build_catalog, scan_ohlcv
from runner.auto_agent import DEFAULT_MODEL_NAME, SUPPORTED_MODEL_NAMES, run_auto_agent
from runner.llm_providers import DEFAULT_LLM_PROVIDER_NAME, SUPPORTED_LLM_PROVIDER_NAMES
from runner.optimizer import OptimizationConfig, run_optimization as execute_optimization
from runner.stats import compute_stats, compute_stats_for_result
from shared.models import BacktestResult, EquityPoint, PortfolioStats, Trade
from shared.strategy_registry import (
    DEFAULT_STRATEGIES_DIR,
    StrategyEntry,
    iter_strategy_files,
    load_strategy_meta,
    matches_strategy_identifier,
    strategy_documentation_path,
    strategy_relative_file,
    strategy_slug_for_path,
    read_strategy_documentation,
    update_strategy_display_name,
)
from viewer.comparison import compare_backtests, compute_drawdown_pct

from shared.constants import DATA_DIR, LOG_DIR, OPTIMIZATIONS_DIR
from shared.logging import get_logger

logger = get_logger("viewer")

BACKTESTS_DIR = DATA_DIR / "backtests"
REACT_DIST = Path(__file__).parent.parent / "frontend" / "dist"
STATIC_DIR = REACT_DIST if REACT_DIST.exists() else Path(__file__).parent / "static"

STRATEGIES_DIR = DEFAULT_STRATEGIES_DIR
PORTFOLIOS_DIR = DATA_DIR / "portfolios"
GRID_RESULTS_DIR = DATA_DIR / "grid_results"
LOGS_DIR = LOG_DIR
TASK_LOGS_DIR = LOGS_DIR / "tasks"
ALL_LOGS_NAME = "ALL_LOGS"
_running_jobs: dict[str, dict] = {}  # job_id -> {status, result, error}

BARS_BEFORE = 60
BARS_AFTER = 25
_OHLCV_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]
_OHLCV_DF_CACHE_LOCK = threading.RLock()
_OHLCV_DF_CACHE: dict[
    tuple[str, str],
    tuple[tuple[tuple[str, int, int], ...], pd.DataFrame],
] = {}


class _RequestPlaceholder:
    def __getattr__(self, item: str) -> Any:
        raise RuntimeError(
            "Flask request context is unavailable. "
            "Use FastAPI endpoints in viewer.main instead."
        )


def jsonify(payload: Any):
    return payload


def send_from_directory(*args: Any, **kwargs: Any):
    raise RuntimeError("Flask static serving is unavailable. Use viewer.main static middleware.")


class _NoopRouteApp:
    def __init__(self) -> None:
        self.logger = logger
        self.config: dict[str, Any] = {}

    def route(self, *args: Any, **kwargs: Any):
        def _decorator(func):
            return func

        return _decorator

    def run(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Flask runtime is removed. Start the app via uvicorn viewer.main:app")


request = _RequestPlaceholder()
app = _NoopRouteApp()


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _load_result(backtest_id: str) -> BacktestResult | None:
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return None
    return BacktestResult.load(str(path))


def _optimization_path(optimization_id: str) -> Path:
    normalized_id = str(optimization_id or "").strip()
    if normalized_id.endswith(".json"):
        normalized_id = normalized_id[:-5]
    return OPTIMIZATIONS_DIR / f"{normalized_id}.json"


def _load_optimization_payload(optimization_id: str) -> dict[str, Any] | None:
    path = _optimization_path(optimization_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _optimization_summary_payload(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "id": payload.get("id", path.stem),
        "filename": path.name,
        "created_at": payload.get("created_at"),
        "base_backtest_id": payload.get("base_backtest_id"),
        "target_metric": payload.get("target_metric"),
        "n_trials": payload.get("n_trials"),
        "train_split_pct": payload.get("train_split_pct"),
        "best_value": payload.get("best_value"),
        "best_params": payload.get("best_params", {}),
        "effective_best_parameters": payload.get("effective_best_parameters", {}),
        "symbols": payload.get("symbols", []),
        "best_symbols": payload.get("best_symbols", []),
        "optimization_mode": payload.get("optimization_mode", "parameter"),
        "symbol_universes": payload.get("symbol_universes", []),
        "train_period": payload.get("train_period", {}),
        "test_period": payload.get("test_period", {}),
        "train_stats": payload.get("train_stats", {}),
        "test_stats": payload.get("test_stats", {}),
    }


def _strategy_entries() -> list[StrategyEntry]:
    entries: list[StrategyEntry] = []
    if not STRATEGIES_DIR.exists():
        return entries

    for path in iter_strategy_files(STRATEGIES_DIR):
        try:
            meta = load_strategy_meta(path, STRATEGIES_DIR)
        except Exception as exc:
            logger.warning("Skipping strategy %s due to import error: %s", path, exc)
            continue

        if not meta:
            continue

        entries.append(
            StrategyEntry(
                slug=strategy_slug_for_path(path, STRATEGIES_DIR),
                file_path=path,
                relative_file=strategy_relative_file(path, STRATEGIES_DIR),
                directory=path.parent,
                documentation_path=strategy_documentation_path(path, STRATEGIES_DIR),
                meta=meta,
            )
        )

    return sorted(entries, key=lambda entry: entry.slug)


def _strategy_entry(identifier: str) -> StrategyEntry | None:
    normalized = str(identifier or "").strip()
    if not normalized:
        return None
    for entry in _strategy_entries():
        if matches_strategy_identifier(entry, normalized):
            return entry
    return None


def _strategy_path(identifier: str) -> Path | None:
    entry = _strategy_entry(identifier)
    if entry is not None:
        return entry.file_path
    candidate = (STRATEGIES_DIR / identifier).resolve()
    return candidate if candidate.exists() else None


def _strategy_labels(identifier: str) -> tuple[str, str, str]:
    entry = _strategy_entry(identifier)
    slug = entry.slug if entry is not None else (identifier or "unknown")
    display_name = entry.display_name if entry is not None else slug
    relative_file = entry.relative_file if entry is not None else ""
    return slug, display_name, relative_file


def _backtest_info_payload(result: BacktestResult) -> dict:
    meta = result.meta
    raw_strategy = meta.strategies[0] if meta.strategies else "unknown"
    strategy_slug, strategy_display_name, strategy_file = _strategy_labels(raw_strategy)
    notes = (meta.notes or "").strip()
    return {
        "id": meta.id,
        "title": notes or strategy_display_name,
        "notes": notes,
        "strategy": raw_strategy,
        "strategy_slug": strategy_slug,
        "strategy_display_name": strategy_display_name,
        "strategy_file": strategy_file,
        "engine": meta.engine,
        "symbols": meta.data.get("symbols", []),
        "timeframe": {"start": meta.backtest_start, "end": meta.backtest_end},
        "data_timeframe": meta.data.get("timeframes", ["1h"])[0]
        if meta.data.get("timeframes")
        else "1h",
        "timeframes": meta.data.get("timeframes", []),
        "initial_capital": meta.initial_capital,
        "created_at": meta.created_at,
        "trade_count": len(result.trades),
        "parameters": meta.parameters,
        "has_equity_curve": bool(result.equity_curve),
        "has_indicators": bool(result.stats.get("indicators")),
    }


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


def _normalize_equity_points(points: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for point in points:
        raw_time = point.get("time")
        unix_time = raw_time if isinstance(raw_time, (int, float)) else _iso_to_unix(str(raw_time))
        if unix_time <= 0:
            continue
        equity_value = _safe_float(point.get("equity"), float("nan"))
        if not math.isfinite(equity_value):
            continue
        normalized.append({"time": float(unix_time), "equity": round(equity_value, 2)})

    normalized.sort(key=lambda item: item["time"])
    collapsed: list[dict] = []
    for point in normalized:
        if collapsed and point["time"] == collapsed[-1]["time"]:
            collapsed[-1] = point
        else:
            collapsed.append(point)
    return collapsed


def _equity_curve_from_trades(result: BacktestResult) -> list[dict]:
    if not result.trades:
        return []

    initial_capital = _safe_float(result.meta.initial_capital, 0.0)
    ordered_trades = sorted(
        result.trades,
        key=lambda trade: (
            _iso_to_unix(trade.last_exit_time or trade.entry_time),
            _iso_to_unix(trade.entry_time),
            trade.id,
        ),
    )

    first_trade_time = _iso_to_unix(ordered_trades[0].entry_time or ordered_trades[0].last_exit_time)
    points: list[dict] = []
    if first_trade_time > 0:
        points.append({"time": first_trade_time, "equity": round(initial_capital, 2)})

    running_equity = initial_capital
    for trade in ordered_trades:
        event_time = _iso_to_unix(trade.last_exit_time or trade.entry_time)
        if event_time <= 0:
            continue
        running_equity += _safe_float(trade.pnl, 0.0)
        points.append({"time": event_time, "equity": round(running_equity, 2)})

    return _normalize_equity_points(points)


def _trade_to_viewer(trade) -> dict:
    """Convert a Universal Format Trade to viewer-friendly flat format."""
    last_exit = trade.exits[-1] if trade.exits else None
    trade_meta = trade.meta if isinstance(trade.meta, dict) else {}
    risk_math = trade_meta.get("risk_math") if isinstance(trade_meta.get("risk_math"), dict) else {}

    position_size = trade.position_size
    if position_size is None:
        position_size = risk_math.get("position_size")

    risk_amount = trade.risk_amount
    if risk_amount is None:
        risk_amount = risk_math.get("risk_target_usd")

    risk_percent = trade.risk_percent
    if risk_percent is None:
        direct_percent = risk_math.get("risk_target_pct")
        if direct_percent is not None:
            risk_percent = direct_percent
        else:
            equity_at_entry = _safe_float(risk_math.get("equity_at_entry"), 0.0)
            target_risk_usd = _safe_float(risk_math.get("risk_target_usd"), 0.0)
            if equity_at_entry > 0 and target_risk_usd > 0:
                risk_percent = target_risk_usd / equity_at_entry

    exit_reason = trade.exit_reason or (last_exit.reason if last_exit else None)
    if exit_reason is None and isinstance(trade_meta.get("exit_reason"), str):
        exit_reason = str(trade_meta.get("exit_reason"))

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
        "position_size": position_size,
        "risk_amount": risk_amount,
        "risk_percent": risk_percent,
        "exit_reason": exit_reason,
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
    normalized_symbol = str(symbol or "").strip()
    normalized_timeframe = str(timeframe or "").strip()
    cache_key = (normalized_symbol.upper(), normalized_timeframe.lower())
    matches = sorted(ohlcv_root.rglob(f"**/{normalized_symbol}/{normalized_timeframe}.parquet"))
    if not matches:
        with _OHLCV_DF_CACHE_LOCK:
            _OHLCV_DF_CACHE.pop(cache_key, None)
        return None
    signature_parts: list[tuple[str, int, int]] = []
    for path in matches:
        try:
            stat = path.stat()
        except OSError:
            continue
        signature_parts.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
    signature: tuple[tuple[str, int, int], ...] = tuple(signature_parts)
    with _OHLCV_DF_CACHE_LOCK:
        cached_entry = _OHLCV_DF_CACHE.get(cache_key)
        if cached_entry is not None:
            cached_signature, cached_df = cached_entry
            if cached_signature == signature:
                return cached_df.copy(deep=False)

    frames: list[pd.DataFrame] = []
    for path in matches:
        try:
            try:
                df = pd.read_parquet(path, columns=_OHLCV_COLUMNS)
            except (ValueError, KeyError):
                df = pd.read_parquet(path)
        except (ValueError, KeyError, pd.errors.EmptyDataError, FileNotFoundError, OSError) as exc:
            logger.warning("Could not load OHLCV segment for %s/%s from %s: %s", symbol, timeframe, path, exc)
            continue
        df.columns = [str(column).lower() for column in df.columns]
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
            df = df.dropna(subset=["timestamp"])
        if not df.empty:
            frames.append(df)

    if not frames:
        return None
    if len(frames) == 1:
        merged = frames[0].copy()
    else:
        merged = pd.concat(frames, ignore_index=True, sort=False)

    if "timestamp" in merged.columns:
        merged = (
            merged.dropna(subset=["timestamp"])
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )
    else:
        merged = merged.drop_duplicates().reset_index(drop=True)

    with _OHLCV_DF_CACHE_LOCK:
        _OHLCV_DF_CACHE[cache_key] = (signature, merged)
    return merged.copy(deep=False)


def _infer_symbol_data_category(symbol: str) -> str:
    symbol_upper = symbol.upper()
    entries, used_catalog = _load_catalog_ohlcv_entries()
    if not used_catalog:
        entries = _scan_ohlcv_filesystem()

    for entry in entries:
        entry_symbol = str(entry.get("symbol", "")).upper()
        if entry_symbol != symbol_upper:
            continue
        return _categorize_path(str(entry.get("path", "")))

    if symbol_upper in {"XAUUSD", "XAGUSD"}:
        return "Metals"
    if len(symbol_upper) == 6 and symbol_upper.isalpha():
        return "Forex"
    if 1 <= len(symbol_upper) <= 5 and symbol_upper.isalpha():
        return "Equities"
    if symbol_upper.endswith("USD") and len(symbol_upper) > 6:
        return "Crypto"
    return "Other"


def _missing_ohlcv_details(
    symbol: str,
    timeframe: str,
    *,
    backtest_start: str = "",
    backtest_end: str = "",
) -> dict[str, Any]:
    category = _infer_symbol_data_category(symbol)
    category_lower = category.lower()

    normalized_timeframe = str(timeframe or "").strip().lower()
    intraday_timeframes = {"1m", "5m", "15m", "30m", "1h"}

    if category_lower in {"forex", "metals", "indices", "energy"}:
        suggested_source = "dukascopy"
        alternatives = ["yfinance"]
    elif category_lower in {"equities", "etfs"}:
        if normalized_timeframe in intraday_timeframes:
            suggested_source = "alpaca"
            alternatives = ["huggingface", "yfinance"]
        else:
            suggested_source = "yfinance"
            alternatives = ["dolthub", "alpaca"]
    else:
        suggested_source = "dukascopy"
        alternatives = ["yfinance", "dolthub"]

    return {
        "code": "MISSING_OHLCV",
        "message": f"Missing OHLCV data for {symbol} {timeframe}",
        "symbol": symbol,
        "timeframe": timeframe,
        "category": category,
        "suggested_source": suggested_source,
        "alternative_sources": alternatives,
        "download_request": {
            "source": suggested_source,
            "symbols": symbol,
            "timeframes": timeframe,
            "start": backtest_start,
            "end": backtest_end,
        },
    }


def _preferred_ohlcv_column(df: pd.DataFrame, base_name: str) -> str | None:
    candidates = [base_name, f"bid_{base_name}", f"ask_{base_name}"]
    columns = set(df.columns)
    for candidate in candidates:
        if candidate not in columns:
            continue
        numeric = pd.to_numeric(df[candidate], errors="coerce")
        if np.isfinite(numeric).any():
            return candidate
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def _df_to_ohlcv_records(df: pd.DataFrame) -> list[dict]:
    """Convert OHLCV DataFrame to list of dicts with Unix timestamps."""
    if df is None or df.empty or "timestamp" not in df.columns:
        return []

    open_col = _preferred_ohlcv_column(df, "open")
    high_col = _preferred_ohlcv_column(df, "high")
    low_col = _preferred_ohlcv_column(df, "low")
    close_col = _preferred_ohlcv_column(df, "close")
    volume_col = _preferred_ohlcv_column(df, "volume")
    if not open_col or not high_col or not low_col or not close_col:
        return []

    timestamp_series = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    timestamp_values = timestamp_series.array.asi8.astype("float64", copy=False)
    nat_sentinel = float(np.iinfo(np.int64).min)
    timestamp_values[timestamp_values == nat_sentinel] = np.nan
    unit = str(getattr(timestamp_series.dtype, "unit", "ns") or "ns").lower()
    unit_scale = {
        "s": 1.0,
        "ms": 1_000.0,
        "us": 1_000_000.0,
        "ns": 1_000_000_000.0,
    }.get(unit, 1_000_000_000.0)
    unix_time = timestamp_values / unit_scale

    open_series = pd.to_numeric(df[open_col], errors="coerce")
    high_series = pd.to_numeric(df[high_col], errors="coerce")
    low_series = pd.to_numeric(df[low_col], errors="coerce")
    close_series = pd.to_numeric(df[close_col], errors="coerce")
    if volume_col:
        volume_series = pd.to_numeric(df[volume_col], errors="coerce")
    else:
        volume_series = pd.Series(0.0, index=df.index, dtype="float64")

    valid_mask = (
        np.isfinite(unix_time)
        & (unix_time > 0)
        & np.isfinite(open_series)
        & np.isfinite(high_series)
        & np.isfinite(low_series)
        & np.isfinite(close_series)
    )
    if not valid_mask.any():
        return []

    normalized = pd.DataFrame(
        {
            "time": unix_time[valid_mask],
            "open": open_series[valid_mask].astype(float),
            "high": high_series[valid_mask].astype(float),
            "low": low_series[valid_mask].astype(float),
            "close": close_series[valid_mask].astype(float),
            "volume": volume_series[valid_mask].fillna(0.0).astype(float),
        }
    )
    if normalized.empty:
        return []

    normalized = normalized.drop_duplicates(subset=["time"], keep="last").sort_values("time")
    normalized["volume"] = normalized["volume"].round().astype(int)
    return normalized.to_dict(orient="records")


def _drawing_timestamp_to_unix(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass

    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return float(pd.Timestamp(ts).timestamp())


def _coerce_finite_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return numeric


def _drawing_matches_symbol(drawing: dict[str, Any], symbol: str) -> bool:
    drawing_symbol = drawing.get("symbol")
    if drawing_symbol in (None, ""):
        return True
    return str(drawing_symbol).upper() == symbol.upper()


def _drawing_to_viewer(drawing: dict[str, Any]) -> dict[str, Any] | None:
    drawing_type = str(drawing.get("type", "")).strip()
    payload = dict(drawing)
    if drawing_type == "marker":
        unix_time = _drawing_timestamp_to_unix(drawing.get("time"))
        if unix_time is None:
            logger.warning("Skipping drawing marker with invalid time: %s", drawing)
            return None
        payload["time"] = unix_time
        return payload

    if drawing_type == "line":
        start_time = _drawing_timestamp_to_unix(drawing.get("start_time"))
        end_time = _drawing_timestamp_to_unix(drawing.get("end_time"))
        if start_time is None or end_time is None:
            logger.warning("Skipping drawing line with invalid timestamps: %s", drawing)
            return None
        payload["start_time"] = start_time
        payload["end_time"] = end_time
        start_value = payload.get("start_value", payload.get("val1"))
        end_value = payload.get("end_value", payload.get("val2"))
        start_numeric = _coerce_finite_float(start_value)
        end_numeric = _coerce_finite_float(end_value)
        if start_numeric is None or end_numeric is None:
            logger.warning("Skipping drawing line with invalid values: %s", drawing)
            return None
        payload["start_value"] = start_numeric
        payload["end_value"] = end_numeric
        payload["val1"] = start_numeric
        payload["val2"] = end_numeric
        return payload

    if drawing_type == "line_with_breaks":
        points = drawing.get("data", [])
        if not isinstance(points, list) or not points:
            logger.warning("Skipping line_with_breaks drawing with invalid data: %s", drawing)
            return None
        points_by_time: dict[float, dict[str, Any]] = {}
        for point in points:
            if not isinstance(point, dict):
                continue
            unix_time = _drawing_timestamp_to_unix(point.get("time"))
            if unix_time is None:
                continue
            normalized_point = dict(point)
            normalized_point["time"] = unix_time
            raw_value = normalized_point.get("value")
            if raw_value is None:
                normalized_point["value"] = None
            else:
                normalized_point["value"] = _coerce_finite_float(raw_value)
            points_by_time[unix_time] = normalized_point
        normalized_points = [points_by_time[key] for key in sorted(points_by_time.keys())]
        if not normalized_points:
            logger.warning("Skipping line_with_breaks drawing without valid points: %s", drawing)
            return None
        payload["data"] = normalized_points
        payload["start_time"] = normalized_points[0]["time"]
        payload["end_time"] = normalized_points[-1]["time"]
        return payload

    if drawing_type == "box":
        start_time = _drawing_timestamp_to_unix(drawing.get("start_time"))
        end_time = _drawing_timestamp_to_unix(drawing.get("end_time"))
        if start_time is None or end_time is None:
            logger.warning("Skipping drawing box with invalid timestamps: %s", drawing)
            return None
        payload["start_time"] = start_time
        payload["end_time"] = end_time
        return payload

    logger.warning("Skipping unsupported drawing type %r", drawing_type)
    return None


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


DUKASCOPY_COVERAGE_TIMEFRAMES: tuple[str, ...] = (
    "tick",
    "s1",
    "m1",
    "m5",
    "m15",
    "m30",
    "h1",
    "h4",
    "d1",
    "mn1",
)


def _load_dukascopy_genesis() -> dict[str, dict[str, str]]:
    from shared.constants import resolve_genesis_path

    genesis_path = resolve_genesis_path(DATA_DIR)
    if not genesis_path.exists():
        return {}

    try:
        data = json.loads(genesis_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
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
) -> dict[str, dict[str, dict[str, str | bool | None]]]:
    requested_symbols_raw = symbols or sorted(genesis_index)
    requested_symbols: list[str] = []
    seen_symbols: set[str] = set()
    for symbol in requested_symbols_raw:
        normalized = str(symbol).strip().upper()
        if not normalized or normalized in seen_symbols:
            continue
        seen_symbols.add(normalized)
        requested_symbols.append(normalized)

    requested_timeframes_raw = list(timeframes or DUKASCOPY_COVERAGE_TIMEFRAMES)
    requested_timeframes: list[str] = []
    seen_timeframes: set[str] = set()
    for timeframe in requested_timeframes_raw:
        normalized = _normalize_dukascopy_genesis_timeframe(str(timeframe))
        if not normalized or normalized in seen_timeframes:
            continue
        seen_timeframes.add(normalized)
        requested_timeframes.append(normalized)

    coverage: dict[str, dict[str, dict[str, str | bool | None]]] = {}
    for symbol in requested_symbols:
        symbol_data = genesis_index.get(symbol, {})
        timeframe_map: dict[str, dict[str, str | bool | None]] = {}
        for timeframe in requested_timeframes:
            raw_start = symbol_data.get(timeframe)
            start = str(raw_start).strip() if raw_start is not None else ""
            timeframe_map[timeframe] = {
                "available": bool(start),
                "start": start or None,
            }
        coverage[symbol] = timeframe_map

    return coverage


def _equity_to_viewer(result: BacktestResult) -> list[dict]:
    """Extract equity curve as [{time, equity}] with Unix timestamps.
    If no equity_curve in JSON, compute from trades."""
    if result.equity_curve:
        cached_curve = _normalize_equity_points(
            [{"time": point.time, "equity": point.equity} for point in result.equity_curve]
        )
        if cached_curve:
            return cached_curve
    return _equity_curve_from_trades(result)


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(numeric):
        return default
    return numeric


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_stats_from_result(result: BacktestResult) -> dict:
    """Use persisted runner stats when available, otherwise compute canonical fallbacks."""
    canonical_keys = {"total_pnl", "sharpe_ratio", "cagr", "max_drawdown_pct"}
    cached_stats = dict(result.stats or {})
    if canonical_keys.issubset(cached_stats):
        return cached_stats

    computed = compute_stats_for_result(result)
    return {**cached_stats, **computed}


# ─── API ──────────────────────────────────────────────────────────────────────


def _quick_stats_and_equity(
    trades: list,
    initial_capital: float,
    start_str: str,
    end_str: str,
    equity_curve: list[dict] | None = None,
    cached_stats: dict | None = None,
) -> dict:
    """Build lightweight list-card stats from persisted runner stats when available."""
    if not trades and not equity_curve:
        return {
            "sharpe": 0,
            "cagr": 0,
            "max_drawdown_pct": 0,
            "net_profit": 0,
            "net_profit_pct": 0,
            "p_value": 1.0,
            "psr": None,
            "equity_mini": [],
        }

    cached_stats = dict(cached_stats or {})
    equity_points: list[EquityPoint] = []
    if equity_curve:
        try:
            equity_points = [
                point if isinstance(point, EquityPoint) else EquityPoint.from_dict(point)
                for point in equity_curve
            ]
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Could not parse cached equity curve: %s", exc)
            equity_points = []

    required_cached_keys = {
        "total_pnl",
        "sharpe_ratio",
        "cagr",
        "max_drawdown_pct",
        "psr",
        "p_value",
    }
    derived_stats = dict(cached_stats)
    if not required_cached_keys.issubset(derived_stats):
        try:
            trade_models = [
                trade if isinstance(trade, Trade) else Trade.from_dict(trade)
                for trade in trades
            ]
            derived_stats = compute_stats(
                trades=trade_models,
                initial_capital=initial_capital,
                equity_curve=equity_points or None,
            )
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Could not compute canonical quick stats: %s", exc)
            derived_stats = dict(cached_stats)

    if equity_points:
        equity = [point.equity for point in equity_points]
        total_pnl = _safe_float(
            derived_stats.get("total_pnl"),
            equity[-1] - equity[0] if len(equity) > 1 else 0.0,
        )
    else:
        equity = [initial_capital]
        running = initial_capital
        for trade in trades:
            pnl = _safe_float(trade.get("pnl", 0))
            running += pnl
            equity.append(round(running, 2))
        total_pnl = running - initial_capital

    net_profit_pct = round(total_pnl / initial_capital * 100, 2) if initial_capital else 0

    if "max_drawdown_pct" in derived_stats:
        max_drawdown_pct = round(abs(_safe_float(derived_stats.get("max_drawdown_pct"))) * 100, 2)
    else:
        peak = equity[0]
        max_dd = 0.0
        for value in equity:
            if value > peak:
                peak = value
            dd = (peak - value) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        max_drawdown_pct = round(max_dd * 100, 2)

    cagr = round(_safe_float(derived_stats.get("cagr")) * 100, 2)
    sharpe = round(_safe_float(derived_stats.get("sharpe_ratio")), 3)

    psr_val = derived_stats.get("psr")
    p_value = round(_safe_float(derived_stats.get("p_value"), 1.0), 6)

    n = len(equity)
    equity_mini = _downsample_numeric_minmax([float(v) for v in equity if isinstance(v, (int, float))], limit=50)

    return {
        "sharpe": sharpe,
        "cagr": cagr,
        "max_drawdown_pct": max_drawdown_pct,
        "net_profit": round(total_pnl, 2),
        "net_profit_pct": net_profit_pct,
        "psr": psr_val,
        "p_value": p_value,
        "equity_mini": equity_mini,
    }


def _downsample_numeric_minmax(values: list[float], *, limit: int) -> list[float]:
    if not values:
        return []
    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        normalized_limit = 50
    normalized_limit = max(1, min(normalized_limit, 2000))
    if len(values) <= normalized_limit:
        return list(values)

    bucket_count = max(1, (normalized_limit - 1) // 2)
    n = len(values)
    step = n / bucket_count

    sampled: list[float] = []
    for bucket in range(bucket_count):
        start = int(bucket * step)
        end = int((bucket + 1) * step)
        if bucket == bucket_count - 1:
            end = n
        if start >= n:
            break
        if end <= start:
            end = min(n, start + 1)
        window = values[start:end]
        if not window:
            continue
        min_val = min(window)
        max_val = max(window)
        if min_val == max_val:
            sampled.append(float(min_val))
        else:
            sampled.append(float(min_val))
            sampled.append(float(max_val))

    sampled.append(float(values[-1]))
    if len(sampled) > normalized_limit:
        sampled = sampled[: normalized_limit - 1] + [float(values[-1])]
    return sampled


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

                quick = _quick_stats_and_equity(
                    trades,
                    initial_capital,
                    start,
                    end,
                    equity_curve=data.get("equity_curve", []),
                    cached_stats=data.get("stats", {}),
                )
                raw_strategy = (meta.get("strategies") or ["unknown"])[0]
                strategy_slug, strategy_display_name, strategy_file = _strategy_labels(raw_strategy)
                notes = str(meta.get("notes", "") or "").strip()

                results.append(
                    {
                        "id": f.stem.replace(".trades", ""),
                        "filename": f.name,
                        "title": notes or strategy_display_name,
                        "notes": notes,
                        "strategy": raw_strategy,
                        "strategy_slug": strategy_slug,
                        "strategy_display_name": strategy_display_name,
                        "strategy_file": strategy_file,
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
                        "net_profit": quick["net_profit"],
                        "net_profit_pct": quick["net_profit_pct"],
                        "psr": quick.get("psr"),
                        "p_value": quick.get("p_value", 1.0),
                        "equity_mini": quick["equity_mini"],
                    }
                )
            except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError) as e:
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
        logger.exception("Failed to serialize backtest '%s': %s", backtest_id, e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtests/<backtest_id>", methods=["PATCH"])
def rename_backtest(backtest_id: str):
    """Update the display title stored in meta.notes for a backtest."""
    path = BACKTESTS_DIR / f"{backtest_id}.trades.json"
    if not path.exists():
        return jsonify({"error": f"Backtest '{backtest_id}' not found"}), 404

    data = request.get_json()
    if not isinstance(data, dict) or "name" not in data:
        return jsonify({"error": "JSON body with 'name' is required"}), 400

    updated_name = str(data.get("name", "") or "").strip()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("meta", {})
    payload["meta"]["notes"] = updated_name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    result = BacktestResult.load(path)
    return jsonify(_backtest_info_payload(result))


@app.route("/api/backtest/<backtest_id>/info")
def get_backtest_info(backtest_id: str):
    """Metadata for topbar display."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": "Not found"}), 404
    return jsonify(_backtest_info_payload(result))


@app.route("/api/backtest/<backtest_id>/stats")
def get_backtest_stats(backtest_id: str):
    """Compute stats for a backtest."""
    result = _load_result(backtest_id)
    if result is None:
        return jsonify({"error": f"Backtest '{backtest_id}' not found"}), 404
    try:
        raw = _canonical_stats_from_result(result)
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
            "cagr": round(_safe_float(raw.get("cagr")) * 100, 2),
            "expectancy": 0,  # computed below
        }
        # Expectancy
        wr = raw.get("win_rate", 0)
        stats["expectancy"] = round(
            wr * raw.get("avg_winner", 0) + (1 - wr) * raw.get("avg_loser", 0), 2
        )
        # PSR / p-value
        stats["psr"] = raw.get("psr")
        stats["p_value"] = raw.get("p_value", 1.0)
        return jsonify(stats)
    except Exception as e:
        logger.exception("Failed to compute stats for '%s': %s", backtest_id, e)
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
    path = _strategy_path(strategy_file)
    if path is None or not path.exists():
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
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
        except (ValueError, KeyError, pd.errors.EmptyDataError) as e:
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
    if df is None or len(df) == 0:
        details = _missing_ohlcv_details(
            trade.symbol,
            tf,
            backtest_start=str(result.meta.backtest_start or ""),
            backtest_end=str(result.meta.backtest_end or ""),
        )
        return jsonify({"error": details["message"], "detail": details}), 404

    ohlcv_start_idx = 0
    ohlcv_end_idx = 0
    entry_ts = pd.Timestamp(trade.entry_time, tz="UTC") if trade.entry_time else None
    if entry_ts is not None:
        idx = df["timestamp"].searchsorted(entry_ts)
        ohlcv_start_idx = max(0, idx - BARS_BEFORE)
        ohlcv_end_idx = min(len(df), idx + BARS_AFTER)
        ohlcv = _df_to_ohlcv_records(df.iloc[ohlcv_start_idx:ohlcv_end_idx])

    # Indicators: use embedded ones from backtest JSON, or compute from strategy
    indicators = [indicator.to_dict() for indicator in result.indicators]
    for ind in indicators:
        points = ind.get("data") if isinstance(ind, dict) else None
        if not isinstance(points, list):
            continue
        normalized_points: list[dict] = []
        for point in points:
            if not isinstance(point, dict):
                continue
            raw_time = point.get("time")
            unix_time = (
                float(raw_time) if isinstance(raw_time, (int, float)) else _iso_to_unix(str(raw_time))
            )
            if unix_time <= 0:
                continue
            normalized_point = dict(point)
            normalized_point["time"] = unix_time
            normalized_points.append(normalized_point)
        ind["data"] = normalized_points

    drawings = [
        viewer_drawing
        for drawing in result.drawings
        if _drawing_matches_symbol(drawing, trade.symbol)
        for viewer_drawing in [_drawing_to_viewer(drawing)]
        if viewer_drawing is not None
    ]

    if not indicators and ohlcv and df is not None:
        # Detect indicators from strategy source and compute them
        strategy_identifier = result.meta.strategies[0] if result.meta.strategies else ""
        detected = _detect_indicators(strategy_identifier)
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
            "drawings": drawings,
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


    comparison = compare_backtests(strategies)
    return jsonify(comparison)


# ─── Strategy Endpoints ──────────────────────────────────────────────────────


def _strategy_backtests(entry: StrategyEntry) -> list[dict]:
    backtests: list[dict] = []
    if not BACKTESTS_DIR.exists():
        return backtests

    for bt_file in sorted(BACKTESTS_DIR.glob("*.trades.json")):
        try:
            bt_data = json.loads(bt_file.read_text(encoding="utf-8"))
            bt_meta = bt_data.get("meta", {})
            bt_strategies = bt_meta.get("strategies", [])
            if not any(matches_strategy_identifier(entry, strategy_name) for strategy_name in bt_strategies):
                continue

            trades = bt_data.get("trades", [])
            total_pnl = sum(t.get("pnl", 0) for t in trades)
            wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
            notes = str(bt_meta.get("notes", "") or "").strip()
            raw_strategy = bt_strategies[0] if bt_strategies else entry.slug
            strategy_slug, strategy_display_name, _ = _strategy_labels(raw_strategy)
            backtests.append(
                {
                    "id": bt_file.stem.replace(".trades", ""),
                    "title": notes or strategy_display_name,
                    "notes": notes,
                    "strategy_slug": strategy_slug,
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
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
            pass
    return backtests


def _strategy_portfolios(entry: StrategyEntry) -> list[dict]:
    portfolios: list[dict] = []
    if not PORTFOLIOS_DIR.exists():
        return portfolios

    for pf_file in PORTFOLIOS_DIR.glob("*.json"):
        try:
            pf_data = json.loads(pf_file.read_text(encoding="utf-8"))
            strategies = pf_data.get("strategies", [])
            if not any(
                matches_strategy_identifier(entry, str(strategy.get("strategy_name", "")))
                for strategy in strategies
            ):
                continue
            portfolios.append(
                {
                    "id": pf_data.get("id", pf_file.stem),
                    "name": pf_data.get("name", ""),
                }
            )
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    return portfolios


def _extract_ast_parameters(file_path: Path) -> dict:
    """Extract default parameters from a strategy file via AST analysis.

    Priority order (first non-empty wins):
      1. Keyword arguments with defaults from ``generate_signals`` signature
         (forward-looking for ``def generate_signals(df, rsi_window=14)`` style).
      2. ``params.get("key", default)`` calls inside ``generate_signals`` body
         (covers all existing strategies).
      3. Top-level ALL_CAPS scalar constant assignments (legacy fallback).
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (FileNotFoundError, OSError, SyntaxError):
        return {}

    # --- locate generate_signals FunctionDef ---
    gen_sig_func: ast.FunctionDef | None = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "generate_signals":
            gen_sig_func = node
            break

    # --- Pattern B: kwargs with defaults in signature ---
    kwargs_params: dict = {}
    if gen_sig_func is not None:
        args_obj = gen_sig_func.args
        # defaults align to the LAST n positional args
        defaults = args_obj.defaults
        all_args = args_obj.args
        n_defaults = len(defaults)
        for i, default_node in enumerate(defaults):
            arg = all_args[len(all_args) - n_defaults + i]
            name = arg.arg
            if name in ("df", "data", "params", "self", "cls"):
                continue
            if isinstance(default_node, ast.Constant):
                val = default_node.value
                if isinstance(val, (int, float, str, bool)):
                    kwargs_params[name] = val
        # also check kwonly args
        for arg, default_node in zip(args_obj.kwonlyargs, args_obj.kw_defaults):
            if default_node is None:
                continue
            name = arg.arg
            if isinstance(default_node, ast.Constant):
                val = default_node.value
                if isinstance(val, (int, float, str, bool)):
                    kwargs_params[name] = val

    if kwargs_params:
        return kwargs_params

    # --- Pattern A: params.get("key", default) in function body ---
    get_params: dict = {}
    if gen_sig_func is not None:
        # Determine the name of the params variable (second positional arg)
        params_var = "params"
        if len(gen_sig_func.args.args) >= 2:
            params_var = gen_sig_func.args.args[1].arg

        for node in ast.walk(gen_sig_func):
            # Match: <params_var>.get("key", default)
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "get":
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != params_var:
                continue
            if len(node.args) < 2:
                continue
            key_node, default_node = node.args[0], node.args[1]
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            if isinstance(default_node, ast.Constant):
                val = default_node.value
                if isinstance(val, (int, float, str, bool)):
                    get_params[key_node.value] = val
            elif isinstance(default_node, ast.UnaryOp) and isinstance(default_node.op, ast.USub):
                # Handle negative numbers like -0.02
                if isinstance(default_node.operand, ast.Constant):
                    val = default_node.operand.value
                    if isinstance(val, (int, float)):
                        get_params[key_node.value] = -val

    if get_params:
        return get_params

    # --- Pattern C (legacy fallback): ALL_CAPS top-level constants ---
    caps_params: dict = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            name = target.id
            if not name or not name.replace("_", "").isalpha():
                continue
            if name != name.upper():
                continue
            if name in {"STRATEGY_META", "TRUE", "FALSE", "NONE"}:
                continue
            if isinstance(node.value, ast.Constant):
                val = node.value.value
                if isinstance(val, (int, float, str)) and not isinstance(val, bool):
                    caps_params[name] = val
    return caps_params


def _strategy_payload(entry: StrategyEntry) -> dict:
    tags = entry.meta.get("tags", {})
    backtests = _strategy_backtests(entry)
    parameters = entry.meta.get("parameters", {})
    if not isinstance(parameters, dict) or not parameters:
        parameters = _extract_ast_parameters(entry.file_path)
    return {
        "slug": entry.slug,
        "name": entry.display_name,
        "version": entry.meta.get("version", "1.0.0"),
        "file": entry.relative_file,
        "created_at": entry.created_at,
        "tags": tags,
        "type": tags.get("type", []),
        "source": tags.get("source", ""),
        "link": tags.get("link", ""),
        "source_url": entry.meta.get("source_url", ""),
        "notes": entry.meta.get("notes", ""),
        "asset_class": tags.get("asset_class", []),
        "complexity": tags.get("complexity", ""),
        "engine": entry.engine,
        "parameters": parameters if isinstance(parameters, dict) else {},
        "backtest_count": len(backtests),
        "best_sharpe": None,
        "documentation_markdown": read_strategy_documentation(entry),
        "backtests": backtests,
        "portfolios": _strategy_portfolios(entry),
    }


@app.route("/api/strategies")
def list_strategies():
    """List all strategies by scanning strategy files for STRATEGY_META."""
    strategies = []
    if not STRATEGIES_DIR.exists():
        return jsonify(strategies)

    for entry in _strategy_entries():
        try:
            payload = _strategy_payload(entry)
            payload.pop("documentation_markdown", None)
            payload.pop("backtests", None)
            payload.pop("portfolios", None)
            strategies.append(payload)
        except (ValueError, TypeError, KeyError, OSError) as e:
            logger.warning("Could not load strategy %s: %s", entry.relative_file, e)

    return jsonify(strategies)


@app.route("/api/strategies/<path:name>", methods=["GET"])
def get_strategy(name: str):
    """Strategy details + all backtests and portfolios using it."""
    entry = _strategy_entry(name)
    if entry is None:
        return jsonify({"error": f"Strategy '{name}' not found"}), 404
    return jsonify(_strategy_payload(entry))


@app.route("/api/strategies/<path:name>", methods=["PATCH"])
def rename_strategy(name: str):
    """Rename the display name in STRATEGY_META for a strategy."""
    entry = _strategy_entry(name)
    if entry is None:
        return jsonify({"error": f"Strategy '{name}' not found"}), 404

    data = request.get_json()
    if not isinstance(data, dict) or "name" not in data:
        return jsonify({"error": "JSON body with 'name' is required"}), 400

    updated_name = str(data.get("name", "") or "").strip()
    if not updated_name:
        return jsonify({"error": "Strategy name cannot be empty"}), 400

    update_strategy_display_name(entry.file_path, updated_name, STRATEGIES_DIR)
    refreshed = _strategy_entry(entry.slug)
    if refreshed is None:
        return jsonify({"error": f"Strategy '{name}' could not be reloaded"}), 500
    return jsonify(_strategy_payload(refreshed))


# ─── Dolthub Auto-Pull ────────────────────────────────────────────────────────

_DOLTHUB_REPOS = Path.home() / "data" / "dolthub"


def _dolthub_pull():
    """Non-blocking dolt pull in all Dolthub repos. Logs warnings on failure."""
    if not _DOLTHUB_REPOS.is_dir():
        return
    if shutil.which("dolt") is None:
        app.logger.info("Skipping dolthub pull: optional 'dolt' binary not installed")
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
        except (subprocess.SubprocessError, OSError) as exc:
            app.logger.warning("dolt pull %s failed: %s", repo, exc)


# ─── Backtest Run Endpoints ──────────────────────────────────────────────────


@app.route("/api/backtests/run", methods=["POST"])
def run_backtest():
    """Start a backtest asynchronously."""
    # Auto-pull Dolthub data (non-blocking, best-effort)
    try:
        threading.Thread(target=_dolthub_pull, daemon=True).start()
    except RuntimeError:
        pass
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    strategy = data.get("strategy", "")
    engine = data.get("engine", "native")
    symbols = data.get("symbols", [])
    timeframe = data.get("timeframe", "1d")
    start = data.get("start", "")
    end = data.get("end", "")
    capital = data.get("capital", 100000)
    risk_pct = data.get("risk_per_trade_pct")
    max_position_pct = data.get("max_position_pct")
    max_leverage = data.get("max_leverage")
    modeling_resolution = str(data.get("modeling_resolution", data.get("intrabar_resolution", "open_close")) or "open_close")
    params = data.get("parameters", {})

    if not strategy or not symbols:
        return jsonify({"error": "strategy and symbols required"}), 400
    if not isinstance(params, dict):
        return jsonify({"error": "'parameters' must be an object"}), 400
    strategy_path = _strategy_path(strategy)
    if strategy_path is None:
        return jsonify({"error": f"Strategy '{strategy}' not found"}), 404

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
                str(strategy_path),
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
            if modeling_resolution:
                cmd.extend(["--intrabar-resolution", modeling_resolution])

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
        except (subprocess.SubprocessError, OSError) as e:
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


@app.route("/api/optimizations/run", methods=["POST"])
def run_optimization_job():
    """Start an Optuna optimization as a background task."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    try:
        config = OptimizationConfig.model_validate(data)
    except ValidationError as exc:
        return jsonify({"error": "Invalid optimization payload", "details": exc.errors()}), 400

    task_id = str(uuid.uuid4())[:8]
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name="Parameter optimization",
        description=(
            f"Optimize {config.base_backtest_id} for {config.target_metric} "
            f"across {config.n_trials} trials."
        ),
        kind="optimization",
        source="optuna",
        total_items=config.n_trials,
        log_file=log_file,
        control_mode="thread",
        supports_control=False,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    def _run() -> None:
        optuna_logger = logging.getLogger("optuna")
        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            log_handler = _ThreadBoundTaskLogHandler(
                task_id=task_id,
                handle=handle,
                source="optimization",
            )
            log_handler.setFormatter(
                logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
            )
            optuna_logger.addHandler(log_handler)
            try:
                _set_task_status(task_id, "running")
                _write_task_log_line(
                    handle,
                    task_id,
                    (
                        f"[{_task_timestamp()}] Starting optimization for "
                        f"{config.base_backtest_id} ({config.target_metric})."
                    ),
                    stream="task",
                    source="optimization",
                )

                def report_progress(percent: float, message: str) -> None:
                    _update_task_progress(
                        task_id,
                        percent=percent,
                        current=message,
                        total=config.n_trials,
                    )
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream="task",
                        source="optimization",
                    )

                payload = execute_optimization(config, progress_callback=report_progress)
                _update_task_fields(
                    task_id,
                    optimization_id=payload.get("id"),
                    optimization_file=payload.get("file"),
                )
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Optimization saved to {payload.get('file', '')}.",
                    stream="task",
                    source="optimization",
                )
                _mark_task_finished(task_id, status="completed")
            except Exception as exc:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Optimization failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="optimization",
                )
                _write_task_traceback(
                    handle,
                    task_id,
                    traceback.format_exc(),
                    source="optimization",
                )
                _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            finally:
                optuna_logger.removeHandler(log_handler)

    thread = threading.Thread(target=_run, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return jsonify({"job_id": task_id, "status": "starting"}), 202


@app.route("/api/optimizations")
def list_optimizations():
    """List saved optimization results."""
    results: list[dict[str, Any]] = []
    if not OPTIMIZATIONS_DIR.exists():
        return jsonify(results)

    for path in sorted(OPTIMIZATIONS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            results.append(_optimization_summary_payload(payload, path))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            logger.warning("Could not read optimization %s: %s", path, exc)

    results.sort(
        key=lambda item: (
            item.get("created_at") or "",
            item.get("id") or "",
        ),
        reverse=True,
    )
    return jsonify(results)


@app.route("/api/optimizations/<optimization_id>")
def get_optimization(optimization_id: str):
    """Return the full persisted optimization payload."""
    try:
        payload = _load_optimization_payload(optimization_id)
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        logger.exception("Failed to load optimization '%s': %s", optimization_id, exc)
        return jsonify({"error": str(exc)}), 500

    if payload is None:
        return jsonify({"error": f"Optimization '{optimization_id}' not found"}), 404
    return jsonify(payload)


# ─── Portfolio Endpoints ─────────────────────────────────────────────────────


@app.route("/api/portfolios")
def list_portfolios():
    """List all portfolios."""
    portfolios = []
    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)
    for f in sorted(PORTFOLIOS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            cs = PortfolioStats.from_dict(data.get("combined_stats", {})).to_dict()

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
                    "psr": cs.get("psr"),
                    "p_value": round(_safe_float(cs.get("p_value"), 1.0), 6),
                    "equity_mini": equity_mini,
                    "created_at": data.get("created_at", ""),
                }
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError) as e:
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
    all_trades_timed = []

    import pandas as pd

    for sc in strat_configs:
        bt_id = sc.get("backtest_id", "")
        strat_name_input = sc.get("name", "")
        # Treat risk_per_trade_pct as a leverage factor relative to the original backtest.
        # Default 1.0 means "Same risk as backtest". 2.0 means "Double risk".
        risk_factor = sc.get("risk_per_trade_pct", 1.0)

        # If no backtest_id but strategy name given, find latest backtest
        if not bt_id and strat_name_input and BACKTESTS_DIR.exists():
            strategy_match = _strategy_entry(strat_name_input)
            norm_input = strat_name_input.lower().replace("_", " ").replace("-", " ")
            candidates = []
            for f in BACKTESTS_DIR.glob("*.trades.json"):
                try:
                    d = json.loads(f.read_text())
                    m = d.get("meta", {})
                    strategies = m.get("strategies", [])
                    matches_input = any(
                        s.lower().replace("_", " ").replace("-", " ") == norm_input
                        for s in strategies
                    )
                    if strategy_match is not None:
                        matches_input = any(
                            matches_strategy_identifier(strategy_match, strategy_name)
                            for strategy_name in strategies
                        )
                    if matches_input:
                        candidates.append((m.get("created_at", ""), f.stem.replace(".trades", "")))
                except (json.JSONDecodeError, KeyError, OSError):
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

        for trade in result.trades:
            all_trades_timed.append(
                {
                    "time": trade.last_exit_time,
                    "pnl": round(trade.pnl * risk_factor, 2),
                    "strategy": strat_name,
                    "symbol": trade.symbol,
                    "direction": trade.direction,
                }
            )

    if not strategy_returns:
        return jsonify({"error": "No valid strategies found"}), 400

    # Combine returns
    combined_df = pd.concat(strategy_returns, axis=1).fillna(0.0)
    portfolio_daily_rets = combined_df.sum(axis=1)
    if len(combined_df.columns) > 1:
        corr_matrix = combined_df.corr().round(4).fillna(0.0).to_dict()
    else:
        corr_matrix = {column: {column: 1.0} for column in combined_df.columns}

    # Reconstruct equity curve
    current_equity = account_capital
    combined_equity_points = []
    # Initial point (optional, or start from first date)
    # combined_equity_points.append(EquityPoint(time=portfolio_daily_rets.index[0].isoformat(), equity=account_capital))

    for ts, ret in portfolio_daily_rets.items():
        current_equity *= 1 + ret
        combined_equity_points.append(EquityPoint(time=ts.isoformat(), equity=current_equity))

    stats_result = compute_stats(
        trades=[], initial_capital=account_capital, equity_curve=combined_equity_points
    )

    # Calculate contributions (approximate via cumulative sum of weighted returns)
    total_pnl = current_equity - account_capital
    contributions = []

    # Calculate individual cumulative returns for contribution
    for col in combined_df.columns:
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
    combined_equity = [point.to_dict() for point in combined_equity_points]
    all_trades_timed.sort(key=lambda trade: trade.get("time", ""))

    portfolio = {
        "id": portfolio_id,
        "name": name,
        "created_at": now.isoformat(),
        "account_capital": account_capital,
        "strategies": strategy_info,
        "combined_stats": PortfolioStats(
            sharpe=stats_result.get("sharpe_ratio", 0),
            sortino=stats_result.get("sortino_ratio", 0),
            cagr=stats_result.get("cagr", 0),
            max_drawdown=stats_result.get("max_drawdown_pct", 0),
            net_profit=stats_result.get("total_pnl", 0),
            win_rate=stats_result.get("win_rate", 0),
            profit_factor=stats_result.get("profit_factor", 0),
            calmar=stats_result.get("calmar_ratio", 0),
            total_return=stats_result.get("total_return", 0),
            max_drawdown_duration=stats_result.get("max_drawdown_duration", 0),
            psr=stats_result.get("psr"),
            p_value=stats_result.get("p_value", 1.0),
            correlation_matrix=corr_matrix,
        ).to_dict(),
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
    payload = json.loads(path.read_text())
    payload["combined_stats"] = PortfolioStats.from_dict(payload.get("combined_stats", {})).to_dict()
    return jsonify(payload)


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
        if not path.exists():
            continue
        try:
            portfolios.append(json.loads(path.read_text()))
        except Exception as exc:
            logger.warning("Could not read portfolio %s: %s", path, exc)
    return jsonify({"portfolios": portfolios})


@app.route("/api/grid-results")
def list_grid_results():
    """List all grid test results."""
    GRID_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for path in sorted(GRID_RESULTS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
            results.append({
                "id": path.stem,
                "created_at": payload.get("created_at", ""),
                "strategy": payload.get("strategy", ""),
                "symbols": payload.get("symbols", []),
                "timeframes": payload.get("timeframes", []),
                "best_score": payload.get("best_score", 0.0),
                "total_combinations": payload.get("total_combinations", 0),
            })
        except Exception as exc:
            logger.warning("Could not read grid result %s: %s", path, exc)
    return jsonify({"results": results})


@app.route("/api/grid-results/<result_id>")
def get_grid_result(result_id: str):
    """Get grid test result details."""
    path = GRID_RESULTS_DIR / f"{result_id}.json"
    if not path.exists():
        return jsonify({"error": "Grid result not found"}), 404
    return jsonify(json.loads(path.read_text()))


@app.route("/api/grid-results/<result_id>", methods=["DELETE"])
def delete_grid_result(result_id: str):
    """Delete grid test result."""
    path = GRID_RESULTS_DIR / f"{result_id}.json"
    if not path.exists():
        return jsonify({"error": "Grid result not found"}), 404
    path.unlink()
    return jsonify({"deleted": True})


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
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
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
        logger.exception("Failed to read log '%s': %s", log_name, e)
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

def _categorize_path(path_str: str) -> str:
    """Derive asset category from file path."""
    p = path_str.replace("\\", "/").lower()
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
    except (json.JSONDecodeError, OSError):
        return [], False

    entries = catalog.get("ohlcv", [])
    if not isinstance(entries, list):
        return [], True
    return [dict(entry) for entry in entries if isinstance(entry, dict)], True


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


DATA_TASKS: dict[str, dict[str, Any]] = {}
DATA_TASKS_LOCK = threading.Lock()
ACTIVE_TASK_STATUSES = {"starting", "running", "paused"}
FINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}


def _task_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


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


class _ThreadBoundTaskLogHandler(logging.Handler):
    def __init__(self, *, task_id: str, handle, source: str) -> None:
        super().__init__()
        self.task_id = task_id
        self.log_handle = handle
        self.source = source
        self.thread_id = threading.get_ident()

    def emit(self, record: logging.LogRecord) -> None:
        if threading.get_ident() != self.thread_id:
            return

        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()

        _write_task_log_line(
            self.log_handle,
            self.task_id,
            message,
            stream="stdout",
            log_level=record.levelname.lower(),
            source=self.source,
        )


def _write_task_log_line(
    handle,
    task_id: str,
    message: str,
    *,
    stream: str,
    log_level: str = "info",
    source: str = "data-task",
    ingest_task_progress: bool = True,
) -> None:
    if not message.endswith("\n"):
        message = f"{message}\n"
    handle.write(message)
    handle.flush()

    text = message.rstrip()
    if not text:
        return

    if ingest_task_progress:
        _ingest_task_log_line(task_id, text)

    log_method = getattr(logger, log_level, logger.info)
    log_method(
        text,
        extra={"data": {"task_id": task_id, "stream": stream, "source": source}},
    )


def _write_task_traceback(
    handle,
    task_id: str,
    traceback_text: str,
    *,
    source: str = "data-task",
    log_level: str = "error",
) -> None:
    content = (traceback_text or "").rstrip()
    if not content:
        return
    _write_task_log_line(
        handle,
        task_id,
        f"[{_task_timestamp()}] Full traceback:",
        stream="stderr",
        log_level=log_level,
        source=source,
    )
    for line in content.splitlines():
        _write_task_log_line(
            handle,
            task_id,
            line,
            stream="stderr",
            log_level=log_level,
            source=source,
        )


def _new_task_progress(total_items: int = 0) -> dict[str, Any]:
    return {
        "completed": 0,
        "failed": 0,
        "total": max(total_items, 0),
        "percent": 0.0,
        "current": "",
    }


def _create_task_record(
    task_id: str,
    *,
    name: str,
    description: str,
    kind: str,
    source: str,
    total_items: int = 0,
    log_file: Path,
    control_mode: str,
    supports_control: bool = True,
) -> dict[str, Any]:
    now = _task_now_iso()
    return {
        "id": task_id,
        "name": name,
        "description": description,
        "kind": kind,
        "source": source,
        "status": "starting",
        "log_file": str(log_file),
        "total_items": total_items,
        "progress": 0.0,
        "progress_detail": _new_task_progress(total_items),
        "pause_event": threading.Event(),
        "stop_event": threading.Event(),
        "supports_control": supports_control,
        "control_mode": control_mode,
        "created_at": now,
        "updated_at": now,
    }


def _get_task(task_id: str) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        return DATA_TASKS.get(task_id)


def _snapshot_task(task_id: str) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        return dict(task) if task is not None else None


def _update_task_fields(task_id: str, **updates: Any) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return None
        task.update(updates)
        task["updated_at"] = _task_now_iso()
        return dict(task)


def _set_task_status(task_id: str, status: str) -> dict[str, Any] | None:
    return _update_task_fields(task_id, status=status)


def _update_task_progress(
    task_id: str,
    *,
    percent: float | None = None,
    current: str | None = None,
    total: int | None = None,
) -> dict[str, Any] | None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return None

        detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
        if total is not None:
            detail["total"] = max(total, 0)
        elif not detail.get("total"):
            detail["total"] = max(int(task.get("total_items", 0) or 0), 0)

        if current is not None:
            detail["current"] = current

        next_percent = float(task.get("progress", 0.0) or 0.0)
        if percent is not None:
            next_percent = max(0.0, min(100.0, round(percent, 2)))

        detail["percent"] = next_percent
        task["progress"] = next_percent
        task["progress_detail"] = detail
        task["updated_at"] = _task_now_iso()
        return dict(task)


def _ingest_task_log_line(task_id: str, text: str) -> None:
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return

        detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
        detail["total"] = max(int(detail.get("total") or task.get("total_items", 0) or 0), 0)
        detail["current"] = text

        if text.startswith("✅"):
            detail["completed"] = int(detail.get("completed", 0) or 0) + 1
        elif text.startswith("❌"):
            detail["failed"] = int(detail.get("failed", 0) or 0) + 1

        total = detail["total"]
        if total > 0 and (text.startswith("✅") or text.startswith("❌")):
            done = min(int(detail.get("completed", 0) or 0) + int(detail.get("failed", 0) or 0), total)
            detail["percent"] = round(min(100.0, done / total * 100.0), 2)
            task["progress"] = detail["percent"]
        else:
            detail["percent"] = float(task.get("progress", detail.get("percent", 0.0)) or 0.0)

        task["progress_detail"] = detail
        task["updated_at"] = _task_now_iso()


def _task_progress_payload(task: dict[str, Any], log_content: str = "") -> dict[str, Any]:
    detail = dict(task.get("progress_detail") or _new_task_progress(task.get("total_items", 0)))
    detail["total"] = max(int(detail.get("total") or task.get("total_items", 0) or 0), 0)

    if not detail.get("current") and log_content:
        lines = [line.strip() for line in log_content.splitlines() if line.strip()]
        if lines:
            detail["current"] = lines[-1]

    if detail["total"] > 0 and detail.get("completed", 0) == 0 and detail.get("failed", 0) == 0 and log_content:
        detail["completed"] = log_content.count("✅")
        detail["failed"] = log_content.count("❌")

    percent = float(task.get("progress", detail.get("percent", 0.0)) or 0.0)
    if detail["total"] > 0 and percent == 0.0:
        done = int(detail.get("completed", 0) or 0) + int(detail.get("failed", 0) or 0)
        if done > 0:
            percent = min(100.0, done / detail["total"] * 100.0)

    if task.get("status") == "completed":
        percent = 100.0

    detail["percent"] = round(percent, 2)
    return detail


def _task_controls_payload(task: dict[str, Any]) -> dict[str, bool]:
    status = task.get("status", "unknown")
    supports_control = bool(task.get("supports_control", False))
    return {
        "can_pause": supports_control and status in {"starting", "running"},
        "can_resume": supports_control and status == "paused",
        "can_cancel": supports_control and status not in FINAL_TASK_STATUSES,
    }


def _serialize_task(
    task_id: str,
    task: dict[str, Any],
    *,
    include_log: bool = False,
    max_log_bytes: int = 2048,
) -> dict[str, Any]:
    log_content = (
        _read_task_log(task.get("log_file", ""), max_bytes=max_log_bytes)
        if include_log and task.get("log_file")
        else ""
    )
    payload = {
        "id": task_id,
        "name": task.get("name"),
        "description": task.get("description"),
        "kind": task.get("kind", "data"),
        "source": task.get("source"),
        "status": task.get("status", "unknown"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "progress": _task_progress_payload(task, log_content),
        "log": log_content,
    }
    payload.update(_task_controls_payload(task))
    return payload


def _mark_task_finished(
    task_id: str,
    *,
    status: str,
    returncode: int | None = None,
    error: str | None = None,
) -> dict[str, Any] | None:
    updates: dict[str, Any] = {"status": status, "finished_at": _task_now_iso()}
    if returncode is not None:
        updates["returncode"] = returncode
    if error is not None:
        updates["error"] = error
    task = _update_task_fields(task_id, **updates)
    if status == "completed":
        _update_task_progress(task_id, percent=100.0)
        task = _snapshot_task(task_id)
    return task


def _wait_for_task_resume(task_id: str) -> bool:
    task = _get_task(task_id)
    if task is None:
        return False

    pause_event = task.get("pause_event")
    stop_event = task.get("stop_event")
    if pause_event is None or stop_event is None:
        return True

    was_paused = False
    while pause_event.is_set():
        was_paused = True
        _set_task_status(task_id, "paused")
        if stop_event.wait(0.25):
            return False

    if was_paused:
        _set_task_status(task_id, "running")
    return not stop_event.is_set()


def _task_log_message(
    task_id: str,
    message: str,
    *,
    stream: str = "task",
    log_level: str = "info",
    source: str = "data-task",
) -> None:
    task = _get_task(task_id)
    log_file = Path(task.get("log_file", _task_log_path(task_id)) if task else _task_log_path(task_id))
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("a", encoding="utf-8", buffering=1) as handle:
        _write_task_log_line(
            handle,
            task_id,
            f"[{_task_timestamp()}] {message}",
            stream=stream,
            log_level=log_level,
            source=source,
        )


def _refresh_catalog_for_task(
    task_id: str,
    *,
    source: str = "data-task",
    cache_invalidation_reason: str | None = None,
) -> bool:
    _task_log_message(task_id, "Refreshing catalog...", source=source)
    try:
        if cache_invalidation_reason:
            removed = cache_manager.invalidate_cache(DATA_DIR, reason=cache_invalidation_reason)
            _task_log_message(
                task_id,
                f"Cache invalidated ({cache_invalidation_reason}): {removed} entries removed.",
                source=source,
            )
        _refresh_catalog()
    except (json.JSONDecodeError, FileNotFoundError, OSError) as exc:
        _task_log_message(task_id, f"Catalog refresh failed: {exc}", log_level="error", source=source)
        _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
        return False
    return True


def _start_data_task(
    task_id: str,
    cmd: list[str],
    *,
    total_items: int = 0,
    name: str = "Background task",
    description: str = "",
    kind: str = "data",
    source: str = "system",
) -> dict:
    """Launch a background data-management subprocess with streamed task logs."""
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name=name,
        description=description,
        kind=kind,
        source=source,
        total_items=total_items,
        log_file=log_file,
        control_mode="process",
        supports_control=True,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    def run_proc() -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        command_display = " ".join(str(part) for part in cmd)
        pause_event = record["pause_event"]
        stop_event = record["stop_event"]

        try:
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                )

                if stop_event.is_set():
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled before launch.",
                        stream="task",
                        source="data-task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                if pause_event.is_set() and not _wait_for_task_resume(task_id):
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled while paused.",
                        stream="task",
                        source="data-task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=os.getcwd(),
                    env=env,
                    text=True,
                    bufsize=1,
                )
                _update_task_fields(task_id, status="running", pid=proc.pid, process=proc)

                if proc.stdout is not None:
                    for line in proc.stdout:
                        _write_task_log_line(handle, task_id, line.rstrip("\n"), stream="stdout")

                returncode = proc.wait()
                if stop_event.is_set():
                    _mark_task_finished(task_id, status="cancelled", returncode=returncode)
                    return

                status = "completed" if returncode == 0 else "failed"
                if returncode == 0:
                    cache_reason = _task_cache_invalidation_reason(source)
                    if _refresh_catalog_for_task(
                        task_id,
                        cache_invalidation_reason=cache_reason,
                    ):
                        _mark_task_finished(task_id, status="completed", returncode=returncode)
                        return

                _mark_task_finished(task_id, status=status, returncode=returncode)
        except (subprocess.SubprocessError, OSError) as exc:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            _append_task_log(
                log_file,
                f"[{_task_timestamp()}] Task failed: {exc}\n",
            )
            logger.exception(
                "Task failed: %s",
                exc,
                extra={"data": {"task_id": task_id, "stream": "task", "source": "data-task"}},
            )
        finally:
            _update_task_fields(task_id, process=None)

    thread = threading.Thread(target=run_proc, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def _start_dukascopy_task(
    task_id: str,
    cmd: list[str],
    *,
    symbols: list[str],
    timeframes: list[str],
    start: str | None,
    end: str | None,
    price_type: str,
    merge_tick: bool,
    merge_s1: bool,
    batch_size: int,
    pause_ms: int,
    total_items: int,
    name: str,
    description: str,
) -> dict[str, Any]:
    from data_tools.downloaders.dukascopy_native import DownloadCancelled, DukascopyNativeDownloader

    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name=name,
        description=description,
        kind="data",
        source="dukascopy",
        total_items=total_items,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    def run_download() -> None:
        command_display = " ".join(str(part) for part in cmd)
        pause_event = record["pause_event"]
        stop_event = record["stop_event"]
        downloader = DukascopyNativeDownloader()
        refresh_needed = False

        try:
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                )

                if stop_event.is_set():
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled before launch.",
                        stream="task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                completed_items = 0
                failed_items = 0
                first_error: str | None = None
                total = max(total_items, 1)

                def task_log(message: str, *, level: str = "info") -> None:
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream="stdout",
                        log_level=level,
                        source="data-task",
                    )

                combinations = [(symbol, timeframe) for symbol in symbols for timeframe in timeframes]
                for symbol, timeframe in combinations:
                    if not _wait_for_task_resume(task_id):
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled while paused.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    if stop_event.is_set():
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    _set_task_status(task_id, "running")
                    item_label = f"{symbol} {timeframe}"
                    base_completed = completed_items + failed_items
                    used_native_backend = False

                    def report_progress(pct: float, message: str) -> None:
                        overall = ((base_completed + (max(0.0, min(pct, 100.0)) / 100.0)) / total) * 100.0
                        _update_task_progress(
                            task_id,
                            percent=overall,
                            current=f"{item_label}: {message}",
                            total=total,
                        )

                    try:
                        result = downloader.download(
                            symbol,
                            timeframe,
                            DATA_DIR,
                            start=start,
                            end=end,
                            price_type=price_type,
                            merge_tick=merge_tick,
                            merge_s1=merge_s1,
                            batch_size=batch_size,
                            pause_ms=pause_ms,
                            pause_event=pause_event,
                            stop_event=stop_event,
                            report_progress=report_progress,
                            log_callback=task_log,
                        )
                        refresh_needed = True
                        completed_items += 1
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"✅ {result.symbol} {result.timeframe}: {result.rows} rows ({result.start} → {result.end}) → {result.path}",
                            stream="stdout",
                            source="data-task",
                        )
                    except DownloadCancelled:
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return
                    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                        failed_items += 1
                        if first_error is None:
                            first_error = f"{symbol} {timeframe}: {exc}"
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"❌ {symbol} {timeframe}: {exc}",
                            stream="stdout",
                            log_level="error",
                            source="data-task",
                        )
                        _write_task_traceback(
                            handle,
                            task_id,
                            traceback.format_exc(),
                            source="data-task",
                        )
                        logger.exception(
                            "Dukascopy download failed for %s %s: %s",
                            symbol,
                            timeframe,
                            exc,
                            extra={
                                "data": {
                                    "task_id": task_id,
                                    "symbol": symbol,
                                    "timeframe": timeframe,
                                    "source": "data-task",
                                    "downloader": "dukascopy-native",
                                }
                            },
                        )

                if refresh_needed and not _refresh_catalog_for_task(
                    task_id,
                    cache_invalidation_reason="post_download",
                ):
                    return

                final_status = "failed" if failed_items > 0 else "completed"
                returncode = 1 if failed_items > 0 else 0
                _write_task_log_line(
                    handle,
                    task_id,
                    f"Done: {completed_items} downloads completed.",
                    stream="stdout",
                    source="data-task",
                )
                _mark_task_finished(
                    task_id,
                    status=final_status,
                    returncode=returncode,
                    error=first_error if failed_items > 0 else None,
                )
        except Exception as exc:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Task failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="data-task",
                )
                _write_task_traceback(
                    handle,
                    task_id,
                    traceback.format_exc(),
                    source="data-task",
                )
            logger.exception(
                "Dukascopy task failed: %s",
                exc,
                extra={"data": {"task_id": task_id, "stream": "task", "source": "data-task"}},
            )

    thread = threading.Thread(target=run_download, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def _start_jforex_task(
    task_id: str,
    cmd: list[str],
    *,
    source_label: str,
    allow_native_fallback: bool | None = None,
    symbols: list[str],
    timeframes: list[str],
    start: str | None,
    end: str | None,
    offer_side: str,
    merge_tick: bool,
    merge_s1: bool,
    batch_size: int,
    pause_ms: int,
    query_timeout_seconds: int,
    total_items: int,
    name: str,
    description: str,
) -> dict[str, Any]:
    from data_tools.downloaders.dukascopy_native import DownloadCancelled, DukascopyNativeDownloader
    from data_tools.downloaders.jforex_dl import JForexDownloader

    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    record = _create_task_record(
        task_id,
        name=name,
        description=description,
        kind="data",
        source=source_label,
        total_items=total_items,
        log_file=log_file,
        control_mode="thread",
        supports_control=True,
    )
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    def run_download() -> None:
        command_display = " ".join(str(part) for part in cmd)
        pause_event = record["pause_event"]
        stop_event = record["stop_event"]
        jforex_downloader = JForexDownloader()
        fallback_downloader: DukascopyNativeDownloader | None = None
        fallback_enabled = (
            bool(allow_native_fallback)
            if allow_native_fallback is not None
            else str(source_label or "").strip().lower() == "dukascopy"
        )
        refresh_needed = False

        try:
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Starting command: {command_display}",
                    stream="task",
                )

                if stop_event.is_set():
                    _write_task_log_line(
                        handle,
                        task_id,
                        f"[{_task_timestamp()}] Task cancelled before launch.",
                        stream="task",
                    )
                    _mark_task_finished(task_id, status="cancelled", returncode=-9)
                    return

                completed_items = 0
                failed_items = 0
                first_error: str | None = None
                total = max(total_items, 1)

                def task_log(message: str, *, level: str = "info") -> None:
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream="stdout",
                        log_level=level,
                        source="data-task",
                    )

                combinations = [(symbol, timeframe) for symbol in symbols for timeframe in timeframes]
                for symbol, timeframe in combinations:
                    if not _wait_for_task_resume(task_id):
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled while paused.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    if stop_event.is_set():
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return

                    _set_task_status(task_id, "running")
                    item_label = f"{symbol} {timeframe}"
                    base_completed = completed_items + failed_items
                    used_native_backend = False

                    def report_progress(pct: float, message: str) -> None:
                        overall = ((base_completed + (max(0.0, min(pct, 100.0)) / 100.0)) / total) * 100.0
                        _update_task_progress(
                            task_id,
                            percent=overall,
                            current=f"{item_label}: {message}",
                            total=total,
                        )

                    try:
                        result = jforex_downloader.download(
                            symbol,
                            timeframe,
                            DATA_DIR,
                            start=start,
                            end=end,
                            offer_side=offer_side,
                            query_timeout_seconds=query_timeout_seconds,
                        )
                    except DownloadCancelled:
                        _write_task_log_line(
                            handle,
                            task_id,
                            f"[{_task_timestamp()}] Task cancelled by user.",
                            stream="task",
                        )
                        _mark_task_finished(task_id, status="cancelled", returncode=-9)
                        return
                    except (
                        ModuleNotFoundError,
                        ImportError,
                        OSError,
                        RuntimeError,
                        TimeoutError,
                        ValueError,
                        KeyError,
                        json.JSONDecodeError,
                    ) as exc:
                        jforex_traceback = traceback.format_exc()
                        fallback_reason = (
                            _jforex_fallback_reason(exc)
                            if fallback_enabled
                            else None
                        )
                        if fallback_reason is not None:
                            if fallback_downloader is None:
                                fallback_downloader = DukascopyNativeDownloader()
                            used_native_backend = True
                            _write_task_log_line(
                                handle,
                                task_id,
                                (
                                    f"ℹ️ {symbol} {timeframe}: JForex not available (reason: {fallback_reason}), "
                                    f"using HTTP downloader. Original error: {exc}"
                                ),
                                stream="stdout",
                                log_level="info",
                                source="data-task",
                            )
                            _write_task_traceback(
                                handle,
                                task_id,
                                jforex_traceback,
                                source="data-task",
                                log_level="warning",
                            )
                            logger.warning(
                                "JForex download failed for %s %s; attempting native fallback: %s",
                                symbol,
                                timeframe,
                                exc,
                                extra={
                                    "data": {
                                        "task_id": task_id,
                                        "symbol": symbol,
                                        "timeframe": timeframe,
                                        "source": "data-task",
                                        "downloader": "jforex",
                                        "fallback": "dukascopy-native",
                                    }
                                },
                            )
                            try:
                                result = fallback_downloader.download(
                                    symbol,
                                    timeframe,
                                    DATA_DIR,
                                    start=start,
                                    end=end,
                                    offer_side=offer_side,
                                    merge_tick=merge_tick,
                                    merge_s1=merge_s1,
                                    batch_size=batch_size,
                                    pause_ms=pause_ms,
                                    pause_event=pause_event,
                                    stop_event=stop_event,
                                    report_progress=report_progress,
                                    log_callback=task_log,
                                )
                            except DownloadCancelled:
                                _write_task_log_line(
                                    handle,
                                    task_id,
                                    f"[{_task_timestamp()}] Task cancelled by user.",
                                    stream="task",
                                )
                                _mark_task_finished(task_id, status="cancelled", returncode=-9)
                                return
                            except (
                                OSError,
                                RuntimeError,
                                TimeoutError,
                                ValueError,
                                KeyError,
                                json.JSONDecodeError,
                            ) as fallback_exc:
                                failed_items += 1
                                if first_error is None:
                                    first_error = (
                                        f"{symbol} {timeframe}: JForex failed ({exc}); "
                                        f"native fallback failed ({fallback_exc})"
                                    )
                                _write_task_log_line(
                                    handle,
                                    task_id,
                                    (
                                        f"❌ {symbol} {timeframe}: JForex failed ({exc}); "
                                        f"native fallback also failed ({fallback_exc})"
                                    ),
                                    stream="stdout",
                                    log_level="error",
                                    source="data-task",
                                )
                                _write_task_traceback(
                                    handle,
                                    task_id,
                                    traceback.format_exc(),
                                    source="data-task",
                                )
                                logger.exception(
                                    "Native fallback failed for %s %s after JForex failure",
                                    symbol,
                                    timeframe,
                                    extra={
                                        "data": {
                                            "task_id": task_id,
                                            "symbol": symbol,
                                            "timeframe": timeframe,
                                            "source": "data-task",
                                            "downloader": "dukascopy-native",
                                        }
                                    },
                                )
                                continue
                        else:
                            failed_items += 1
                            if first_error is None:
                                first_error = f"{symbol} {timeframe}: {exc}"
                            _write_task_log_line(
                                handle,
                                task_id,
                                f"❌ {symbol} {timeframe}: {exc}",
                                stream="stdout",
                                log_level="error",
                                source="data-task",
                            )
                            _write_task_traceback(
                                handle,
                                task_id,
                                jforex_traceback,
                                source="data-task",
                            )
                            logger.exception(
                                "JForex download failed for %s %s: %s",
                                symbol,
                                timeframe,
                                exc,
                                extra={
                                    "data": {
                                        "task_id": task_id,
                                        "symbol": symbol,
                                        "timeframe": timeframe,
                                        "source": "data-task",
                                        "downloader": "jforex",
                                    }
                                },
                            )
                            continue

                    refresh_needed = True
                    completed_items += 1
                    backend_suffix = (
                        " (backend:native-http)"
                        if used_native_backend
                        else " (backend:jforex)"
                    )
                    _write_task_log_line(
                        handle,
                        task_id,
                        (
                            f"✅ {result.symbol} {result.timeframe}: {result.rows} rows "
                            f"({result.start} → {result.end}) → {result.path}{backend_suffix}"
                        ),
                        stream="stdout",
                        source="data-task",
                    )

                if refresh_needed and not _refresh_catalog_for_task(
                    task_id,
                    cache_invalidation_reason="post_download",
                ):
                    return

                final_status = "failed" if failed_items > 0 else "completed"
                returncode = 1 if failed_items > 0 else 0
                _write_task_log_line(
                    handle,
                    task_id,
                    f"Done: {completed_items} downloads completed.",
                    stream="stdout",
                    source="data-task",
                )
                _mark_task_finished(
                    task_id,
                    status=final_status,
                    returncode=returncode,
                    error=first_error if failed_items > 0 else None,
                )
        except Exception as exc:
            _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
            with log_file.open("a", encoding="utf-8", buffering=1) as handle:
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Task failed: {exc}",
                    stream="stderr",
                    log_level="error",
                    source="data-task",
                )
                _write_task_traceback(
                    handle,
                    task_id,
                    traceback.format_exc(),
                    source="data-task",
                )
            logger.exception(
                "JForex task failed: %s",
                exc,
                extra={"data": {"task_id": task_id, "stream": "task", "source": "data-task"}},
            )

    thread = threading.Thread(target=run_download, daemon=True)
    _update_task_fields(task_id, thread=thread)
    thread.start()
    return {"task_id": task_id, "status": "starting"}


def _queue_agent_task(task_id: str, payload: dict) -> dict:
    """Register the autonomous conversion agent before the worker thread launches."""
    TASK_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = _task_log_path(task_id)
    provider = str(payload.get("provider") or DEFAULT_LLM_PROVIDER_NAME).strip().lower() or DEFAULT_LLM_PROVIDER_NAME
    model = str(payload.get("model") or "").strip() or None
    record = _create_task_record(
        task_id,
        name="Strategy conversion",
        description=(
            f"{payload.get('strategy_name') or 'Convert strategy into the repository format'} "
            f"(provider={provider}{f', model={model}' if model else ''})"
        ),
        kind="agent_conversion",
        source="agent",
        total_items=0,
        log_file=log_file,
        control_mode="thread",
        supports_control=False,
    )
    record["provider"] = provider
    record["model"] = model
    with DATA_TASKS_LOCK:
        DATA_TASKS[task_id] = record

    return {"job_id": task_id, "task_id": task_id, "status": record["status"]}


def _launch_agent_task(task_id: str, payload: dict) -> None:
    task = _get_task(task_id)
    log_file = Path(task.get("log_file", _task_log_path(task_id)) if task else _task_log_path(task_id))
    provider = str(payload.get("provider") or DEFAULT_LLM_PROVIDER_NAME).strip().lower() or DEFAULT_LLM_PROVIDER_NAME
    model = str(payload.get("model") or "").strip() or None

    def run_agent() -> None:
        started_at = datetime.now(timezone.utc)
        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            try:
                _write_task_log_line(
                    handle,
                    task_id,
                    (
                        f"[{_task_timestamp()}] Starting strategy conversion with "
                        f"provider={provider} model={model or 'default'}."
                    ),
                    stream="task",
                    source="agent-task",
                )
                _set_task_status(task_id, "running")
                _update_task_fields(task_id, provider=provider, model=model, started_at=started_at.isoformat())

                def task_log(message: str, *, stream: str = "task", level: str = "info") -> None:
                    _write_task_log_line(
                        handle,
                        task_id,
                        message,
                        stream=stream,
                        log_level=level,
                        source="agent-task",
                    )

                with redirect_stdout(handle), redirect_stderr(handle):
                    result = run_auto_agent(
                        payload["code"],
                        strategy_name=payload.get("strategy_name"),
                        requested_engine=payload.get("engine"),
                        provider=provider,
                        model=model,
                        symbols=payload.get("symbols"),
                        timeframe=payload.get("timeframe"),
                        start=payload.get("start"),
                        end=payload.get("end"),
                        capital=_safe_float(payload.get("capital"), 100000.0),
                        log=task_log,
                    )
                duration_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
                _update_task_fields(
                    task_id,
                    result=result,
                    provider=provider,
                    model=model,
                    duration_seconds=round(duration_seconds, 3),
                )
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] Strategy conversion completed successfully.",
                    stream="task",
                    source="agent-task",
                )
                _mark_task_finished(task_id, status="completed", returncode=0)
            except Exception as exc:
                duration_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
                traceback_text = traceback.format_exc()
                _write_task_log_line(
                    handle,
                    task_id,
                    f"[{_task_timestamp()}] ❌ Strategy conversion failed: {exc}",
                    stream="task",
                    log_level="error",
                    source="agent-task",
                )
                _write_task_traceback(handle, task_id, traceback_text, source="agent-task")
                _update_task_fields(
                    task_id,
                    provider=provider,
                    model=model,
                    duration_seconds=round(duration_seconds, 3),
                )
                _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
                logger.exception(
                    "Strategy conversion failed: %s",
                    exc,
                    extra={"data": {"task_id": task_id, "stream": "task", "source": "agent-task"}},
                )

    thread = threading.Thread(target=run_agent, daemon=True, name=f"agent-convert-{task_id[:8]}")
    _update_task_fields(task_id, thread=thread)
    try:
        thread.start()
    except Exception as exc:
        traceback_text = traceback.format_exc()
        with log_file.open("a", encoding="utf-8", buffering=1) as handle:
            _write_task_log_line(
                handle,
                task_id,
                f"[{_task_timestamp()}] ❌ Failed to launch strategy conversion worker: {exc}",
                stream="task",
                log_level="error",
                source="agent-task",
            )
            _write_task_traceback(handle, task_id, traceback_text, source="agent-task")
        _mark_task_finished(task_id, status="failed", returncode=1, error=str(exc))
        logger.exception(
            "Failed to launch strategy conversion worker: %s",
            exc,
            extra={"data": {"task_id": task_id, "stream": "task", "source": "agent-task"}},
        )


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
        except (OSError, ValueError):
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
            except OSError:
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
                except OSError:
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


def _task_symbol_summary(symbols: list[str]) -> str:
    if not symbols:
        return "all symbols"
    if len(symbols) == 1:
        return symbols[0]
    if len(symbols) <= 3:
        return ", ".join(symbols)
    return f"{symbols[0]} +{len(symbols) - 1}"


def _task_timeframe_summary(timeframes: list[str]) -> str:
    if not timeframes:
        return "default timeframe"
    if len(timeframes) == 1:
        return timeframes[0]
    return ", ".join(timeframes[:3]) + ("…" if len(timeframes) > 3 else "")


def _download_task_labels(source: str, symbols: list[str], timeframes: list[str]) -> tuple[str, str]:
    symbol_label = _task_symbol_summary(symbols)
    timeframe_label = _task_timeframe_summary(timeframes)
    if source == "dukascopy":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"Dukascopy download for {symbol_label} on {timeframe_label}.",
        )
    if source == "jforex":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"JForex download for {symbol_label} on {timeframe_label}.",
        )
    if source == "yfinance":
        return (
            f"Download {symbol_label}",
            f"Yahoo Finance download for {symbol_label}.",
        )
    if source == "huggingface":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"HuggingFace download for {symbol_label} on {timeframe_label}.",
        )
    if source == "alpaca":
        return (
            f"Download {symbol_label} {timeframe_label}",
            f"Alpaca download for {symbol_label} on {timeframe_label}.",
        )
    return ("Sync Dolthub repos", "Refresh Dolthub repositories and rebuild the data catalog.")


def _task_action_response(task_id: str):
    task = _snapshot_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(_serialize_task(task_id, task, include_log=True))


def _task_cache_invalidation_reason(source: str) -> str | None:
    source_key = str(source or "").strip().lower()
    if source_key == "resample":
        return "post_resample"
    if source_key in {
        "dukascopy",
        "jforex",
        "yfinance",
        "huggingface",
        "alpaca",
        "dolthub",
        "harvest",
        "download",
    }:
        return "post_download"
    return None


def _is_jforex_unavailable_error(exc: Exception) -> bool:
    return _jforex_fallback_reason(exc) is not None


def _jforex_fallback_reason(exc: Exception) -> str | None:
    if isinstance(exc, ModuleNotFoundError):
        missing_name = str(getattr(exc, "name", "") or "").lower()
        if "jpype" in missing_name or "dukascopy" in missing_name:
            return f"missing python module: {missing_name or 'unknown'}"

    message = str(exc).lower()
    availability_reason_by_fragment = {
        "jpype is required": "JPype dependency missing",
        "no module named 'jpype'": "JPype dependency missing",
        "failed to import 'com.dukascopy'": "JForex SDK classes unavailable",
        "classnotfoundexception": "JForex SDK classes unavailable",
        "com.dukascopy": "JForex SDK classes unavailable",
        "failed to start jvm": "Java/JVM startup failed",
        "could not resolve jforex sdk classpath": "JForex SDK classpath unavailable",
        "jforex sdk classpath": "JForex SDK classpath unavailable",
        "jvm shared library": "Java/JVM runtime unavailable",
        "jvm dll not found": "Java/JVM runtime unavailable",
        "no jvm shared library file": "Java/JVM runtime unavailable",
        "nullpointerexception": "JForex runtime classloader failure",
        "jforex sdk request execution failed": "JForex request execution failed",
        "jforex connection timed out": "JForex connection timed out",
        "jforex data request timed out": "JForex data request timed out",
        "jforex history query failed": "JForex historical query failed",
        "interval from [": "JForex historical interval invalid",
        "not valid for period": "JForex historical interval invalid",
        "dukascopy credentials": "Dukascopy credentials missing or invalid",
    }
    for fragment, reason in availability_reason_by_fragment.items():
        if fragment in message:
            return reason

    return None


def _signal_process(task: dict[str, Any], sig: int) -> None:
    process = task.get("process")
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        os.kill(process.pid, sig)
    except ProcessLookupError:
        return


@app.route("/api/data/download", methods=["POST"])
def data_download():
    """Start a background download task."""
    data = request.get_json() or {}
    source = data.get("source", "dukascopy")
    symbols = data.get("symbols", "")  # comma separated
    timeframes = data.get("timeframes", "1m")
    start = data.get("start", "")
    end = data.get("end", "")
    price_type = data.get("price_type", "bid")
    merge_tick = bool(data.get("merge_tick", data.get("merge_bid_ask", False)))
    merge_s1 = bool(data.get("merge_s1", data.get("merge_bid_ask", False)))
    batch_size = _safe_int(data.get("batch_size", data.get("concurrency", 10)), 10)
    pause_ms = _safe_int(data.get("pause_ms", 1000), 1000)
    sym_list = _parse_symbol_list(symbols)
    tf_list = [
        _normalize_dukascopy_genesis_timeframe(timeframe)
        for timeframe in _parse_timeframe_list(timeframes)
    ]

    if batch_size < 1 or batch_size > 50:
        return jsonify({"error": "batch_size must be between 1 and 50"}), 400
    if pause_ms < 0 or pause_ms > 60000:
        return jsonify({"error": "pause_ms must be between 0 and 60000"}), 400

    if source == "dukascopy" and start:
        genesis_index = _load_dukascopy_genesis()
        too_early: dict[str, str] = {}
        for symbol in sym_list:
            symbol_data = genesis_index.get(symbol, {})
            for timeframe in tf_list:
                genesis = symbol_data.get(timeframe)
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
    name, description = _download_task_labels(source, sym_list, tf_list)

    cmd: list[str]
    if source == "dukascopy":
        # Accept explicit offer_side (bid|ask|both) for Dukascopy/JForex
        offer_side = str(data.get("offer_side", data.get("price_type", "bid"))).strip().lower()
        if offer_side not in {"bid", "ask", "both"}:
            return jsonify({"error": "offer_side must be one of: bid, ask, both"}), 400

        normalized_symbols = ",".join(sym_list)
        normalized_timeframes = ",".join(tf_list)

        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.jforex_dl",
            "--symbols",
            normalized_symbols,
            "--timeframes",
            normalized_timeframes,
            "--query-timeout-seconds",
            str(900),
        ]
        if start:
            cmd.extend(["--start", start])
        if end:
            cmd.extend(["--end", end])
        if offer_side:
            cmd.extend(["--price-type", str(offer_side)])
        if merge_tick:
            cmd.append("--merge-tick")
        if merge_s1:
            cmd.append("--merge-s1")

        total_items = max(len(sym_list) * len(tf_list), 1)
        return jsonify(
            _start_jforex_task(
                task_id,
                cmd,
                source_label="dukascopy",
                symbols=sym_list,
                timeframes=tf_list,
                start=start or None,
                end=end or None,
                price_type=str(offer_side),
                merge_tick=merge_tick,
                merge_s1=merge_s1,
                batch_size=batch_size,
                pause_ms=pause_ms,
                query_timeout_seconds=900,
                total_items=total_items,
                name=name,
                description=description,
            )
        )

    if source == "yfinance":
        cmd = [
            sys.executable,
            "-u",
            "-m",
            "data_tools.downloaders.yfinance_dl",
            "--symbols",
            symbols,
            "--timeframes",
            timeframes,
        ]
        if start:
            cmd.extend(["--start", start])
        if end:
            cmd.extend(["--end", end])
    elif source in {"huggingface", "alpaca"}:
        module_name = {
            "huggingface": "data_tools.downloaders.huggingface_dl",
            "alpaca": "data_tools.downloaders.alpaca_dl",
        }[source]
        cmd = [
            sys.executable,
            "-u",
            "-m",
            module_name,
            "--symbols",
            symbols,
            "--timeframes",
            timeframes,
        ]
        if start:
            cmd.extend(["--start", start])
        if end:
            cmd.extend(["--end", end])
    elif source == "dolthub":
        cmd = [sys.executable, "-u", "-m", "data_tools.dolthub_manager", "update"]
    else:
        return jsonify({"error": "Unknown source"}), 400

    total_items = max(len(sym_list) * len(tf_list), 1)
    return jsonify(
        _start_data_task(
            task_id,
            cmd,
            total_items=total_items,
            name=name,
            description=description,
            source=source,
        )
    )


@app.route("/api/agent/convert", methods=["POST"])
def agent_convert():
    """Start the autonomous strategy conversion worker."""
    data = request.get_json() or {}
    code = data.get("code", "")
    if not isinstance(code, str) or not code.strip():
        return jsonify({"error": "A non-empty 'code' field is required"}), 400
    provider = str(data.get("provider", "")).strip().lower() or DEFAULT_LLM_PROVIDER_NAME
    if provider not in SUPPORTED_LLM_PROVIDER_NAMES:
        return jsonify({"error": f"Unsupported provider '{provider}'"}), 400

    model = str(data.get("model", "")).strip() or None
    if provider == "nim":
        model = model or DEFAULT_MODEL_NAME
        if model not in SUPPORTED_MODEL_NAMES:
            return jsonify({"error": f"Unsupported model '{model}' for provider '{provider}'"}), 400

    payload = {
        "code": code,
        "strategy_name": str(data.get("strategy_name", "")).strip() or None,
        "engine": str(data.get("engine", "")).strip().lower() or None,
        "provider": provider,
        "model": model,
        "symbols": [
            str(symbol).strip().upper()
            for symbol in data.get("symbols", [])
            if str(symbol).strip()
        ]
        if isinstance(data.get("symbols"), list)
        else None,
        "timeframe": str(data.get("timeframe", "")).strip().lower() or None,
        "start": str(data.get("start", "")).strip() or None,
        "end": str(data.get("end", "")).strip() or None,
        "capital": data.get("capital", 100000.0),
    }

    task_id = str(uuid.uuid4())
    response = jsonify(_queue_agent_task(task_id, payload))
    response.status_code = 202
    response.call_on_close(lambda: _launch_agent_task(task_id, payload))
    return response


@app.route("/api/data/dukascopy/genesis")
def get_dukascopy_genesis():
    """Return discovered Dukascopy genesis dates for one or more symbols."""
    requested_symbols = _parse_symbol_list(request.args.get("symbols", ""))
    requested_timeframes = _parse_timeframe_list(request.args.get("timeframes", ""))
    genesis_index = _load_dukascopy_genesis()
    coverage = _collect_dukascopy_genesis(
        genesis_index,
        symbols=requested_symbols or None,
        timeframes=requested_timeframes or None,
    )
    return jsonify(
        {
            "source": "dukascopy",
            "coverage": coverage,
        }
    )


@app.route("/api/data/resample", methods=["POST"])
def data_resample():
    """Start a background resample task."""
    data = request.get_json() or {}
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
    return jsonify(
        _start_data_task(
            task_id,
            cmd,
            total_items=1,
            name="Resample data",
            description=f"Resample {Path(source_path).name or source_path} into {target_tfs}.",
            source="resample",
        )
    )


@app.route("/api/data/tasks")
def list_data_tasks():
    """Return recent data tasks with progress and control metadata."""
    with DATA_TASKS_LOCK:
        snapshots = [(task_id, dict(task)) for task_id, task in DATA_TASKS.items()]
    payload = [
        _serialize_task(task_id, task, include_log=True, max_log_bytes=768)
        for task_id, task in snapshots
    ]
    payload.sort(
        key=lambda item: (
            item.get("created_at") or "",
            item.get("id") or "",
        ),
    )
    return jsonify({"tasks": payload})


@app.route("/api/data/tasks/<task_id>")
def get_data_task(task_id):
    """Get status of a background task."""
    task = _snapshot_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(_serialize_task(task_id, task, include_log=True))


@app.route("/api/data/tasks/<task_id>/pause", methods=["POST"])
def pause_data_task(task_id):
    """Pause a controllable background task."""
    task = _get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    if not task.get("supports_control", False):
        return jsonify({"error": "Task cannot be paused"}), 409
    if task.get("status") in FINAL_TASK_STATUSES:
        return _task_action_response(task_id)

    task["pause_event"].set()
    if task.get("control_mode") == "process":
        _signal_process(task, signal.SIGSTOP)
    _set_task_status(task_id, "paused")
    _task_log_message(task_id, "Task paused by user.")
    return _task_action_response(task_id)


@app.route("/api/data/tasks/<task_id>/resume", methods=["POST"])
def resume_data_task(task_id):
    """Resume a paused background task."""
    task = _get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    if not task.get("supports_control", False):
        return jsonify({"error": "Task cannot be resumed"}), 409
    if task.get("status") in FINAL_TASK_STATUSES:
        return _task_action_response(task_id)

    task["pause_event"].clear()
    if task.get("control_mode") == "process":
        _signal_process(task, signal.SIGCONT)
    _set_task_status(task_id, "running")
    _task_log_message(task_id, "Task resumed by user.")
    return _task_action_response(task_id)


@app.route("/api/data/tasks/<task_id>/cancel", methods=["POST"])
def cancel_data_task(task_id):
    """Cancel a controllable background task."""
    task = _get_task(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    if not task.get("supports_control", False):
        return jsonify({"error": "Task cannot be cancelled"}), 409
    if task.get("status") in FINAL_TASK_STATUSES:
        return _task_action_response(task_id)

    task["stop_event"].set()
    task["pause_event"].clear()
    if task.get("control_mode") == "process":
        process = task.get("process")
        if process is not None and process.poll() is None:
            process.kill()
    _set_task_status(task_id, "cancelled")
    _task_log_message(task_id, "Task cancelled by user.")
    return _task_action_response(task_id)


@app.route("/api/data/tasks/<task_id>/logs")
def get_data_task_logs(task_id):
    """Return the full task log for a background data-management job."""
    task = _snapshot_task(task_id)
    log_file = _task_log_path(task_id)
    if task is None and not log_file.exists():
        return jsonify({"error": "Task not found"}), 404

    return jsonify(
        {
            "logs": _read_task_log(task.get("log_file", log_file) if task else log_file),
            "status": task.get("status", "unknown") if task else "unknown",
        }
    )


@app.route("/api/data/tasks/<task_id>", methods=["DELETE"])
def delete_data_task(task_id):
    """Remove a finished task from the in-memory task registry."""
    with DATA_TASKS_LOCK:
        task = DATA_TASKS.get(task_id)
        if task is None:
            return jsonify({"error": "Task not found"}), 404
        if task.get("status") not in FINAL_TASK_STATUSES:
            return jsonify({"error": "Only completed, failed, or cancelled tasks can be removed"}), 409
        del DATA_TASKS[task_id]
    return jsonify({"task_id": task_id, "status": "deleted"})


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
    except (OSError, ValueError):
        return jsonify({"error": "Invalid path"}), 400

    if p.exists():
        try:
            p.unlink()
            # Update catalog
            _refresh_catalog()
            return jsonify({"status": "deleted"})
        except (FileNotFoundError, OSError) as e:
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
