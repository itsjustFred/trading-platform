"""
vectorbt Engine Adapter — runs Python strategies using vectorized signals.

Preferred strategy contract:
    def generate_signals(df: pd.DataFrame, params: dict):
        return entries, exits, indicators_dict

Legacy compatibility:
    def generate_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame
        DataFrame must contain 'entries' and 'exits' columns.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from runner.engines.base import BacktestConfig, EngineAdapter, resolve_strategy_parameters
from shared.constants import DATA_DIR
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit

logger = logging.getLogger(__name__)

IndicatorMap = dict[str, tuple[pd.Series, str]]
TIMEFRAME_ORDER = ["tick", "s1", "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"]
TIMEFRAME_TO_DURATION = {
    "s1": pd.Timedelta(seconds=1),
    "1m": pd.Timedelta(minutes=1),
    "5m": pd.Timedelta(minutes=5),
    "15m": pd.Timedelta(minutes=15),
    "30m": pd.Timedelta(minutes=30),
    "1h": pd.Timedelta(hours=1),
    "4h": pd.Timedelta(hours=4),
    "1d": pd.Timedelta(days=1),
    "1w": pd.Timedelta(weeks=1),
}


def _uses_kwargs_style(func) -> bool:
    """Return True if generate_signals uses keyword-argument style (not params dict)."""
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return False
    params = list(sig.parameters.values())
    if len(params) < 2:
        return False
    # Skip first arg (df/data). If second arg is named 'params' → dict style.
    if params[1].name == "params":
        return False
    # If any param beyond the first has a non-empty default → kwargs style
    return any(
        p.default is not inspect.Parameter.empty
        for p in params[1:]
    )


def _call_generate_signals(strategy_mod, df: pd.DataFrame, parameters: dict):
    """Call generate_signals with dict-style or kwargs-style depending on signature."""
    if _uses_kwargs_style(strategy_mod.generate_signals):
        # Filter to only keys the function actually accepts
        sig = inspect.signature(strategy_mod.generate_signals)
        accepted = {p.name for p in list(sig.parameters.values())[1:]}
        filtered = {k: v for k, v in parameters.items() if k in accepted}
        return strategy_mod.generate_signals(df, **filtered)
    return strategy_mod.generate_signals(df, parameters)


class VectorbtAdapter(EngineAdapter):
    """Adapter for vectorbt-style vectorized backtesting."""

    @property
    def name(self) -> str:
        return "vectorbt"

    def run(self, config: BacktestConfig, strategy_path: str) -> BacktestResult:
        strategy_mod = _load_strategy_module(strategy_path)
        if not hasattr(strategy_mod, "generate_signals"):
            raise ValueError(f"Strategy {strategy_path} must define generate_signals(df, params)")

        data_root = Path(config.data_dir) if config.data_dir else DATA_DIR
        resolved_parameters = resolve_strategy_parameters(strategy_path, config.parameters)
        all_trades: list[Trade] = []
        all_equity: list[EquityPoint] = []
        trade_counter = 0
        used_timeframes = [config.timeframe]

        for symbol in config.symbols:
            df, data_path = _load_ohlcv(
                data_root, symbol, config.timeframe, config.start_date, config.end_date
            )
            if df is None or df.empty:
                logger.warning("No data for %s/%s, skipping", symbol, config.timeframe)
                continue
            asset_class = _infer_asset_class_from_path(data_path)
            intrabar_df, intrabar_timeframe = _load_intrabar_ohlcv(
                data_root,
                symbol,
                config.timeframe,
                config.modeling_resolution,
                config.start_date,
                config.end_date,
            )
            if intrabar_timeframe and intrabar_timeframe not in used_timeframes:
                used_timeframes.append(intrabar_timeframe)

            signal_output = _call_generate_signals(strategy_mod, df, resolved_parameters)
            signals, indicators = _normalize_signal_output(signal_output, df.index)
            entries = signals["entries"]
            exits = signals["exits"]
            stop_loss_series = _find_stop_loss_series(signals, indicators)
            take_profit_series = _find_take_profit_series(signals, indicators)
            atr_series = _find_atr_series(signals, indicators)
            direction_series = _find_direction_series(signals, indicators)
            exit_price_series = _find_exit_price_series(signals, indicators)
            exit_reason_series = _find_exit_reason_series(signals, indicators)

            in_trade = False
            entry_idx = None
            equity = config.initial_capital

            for i in range(len(df)):
                if not in_trade and bool(entries.iloc[i]):
                    in_trade = True
                    entry_idx = i
                elif in_trade and bool(exits.iloc[i]):
                    in_trade = False
                    trade_counter += 1

                    entry_row = df.iloc[entry_idx]
                    exit_row = df.iloc[i]
                    direction = _resolve_direction(signals, direction_series, entry_idx)
                    entry_price = float(entry_row["close"])
                    exit_price = _resolve_exit_price(exit_row, i, exit_price_series)
                    exit_reason = _resolve_exit_reason(i, exit_reason_series)
                    entry_ts = str(entry_row.get("timestamp", entry_row.name))
                    exit_ts = str(exit_row.get("timestamp", exit_row.name))
                    trade_meta = _extract_trade_meta(signals, indicators, entry_idx, i)

                    intrabar_exit = _resolve_intrabar_exit(
                        intrabar_df=intrabar_df,
                        intrabar_timeframe=intrabar_timeframe,
                        base_timeframe=config.timeframe,
                        direction=direction,
                        entry_time=entry_ts,
                        scheduled_exit_time=exit_ts,
                        stop_price=_resolve_level_value(stop_loss_series, entry_idx),
                        take_profit_price=_resolve_level_value(take_profit_series, entry_idx),
                    )
                    if intrabar_exit is not None:
                        exit_price = intrabar_exit["price"]
                        exit_ts = intrabar_exit["time"]
                        exit_reason = intrabar_exit["reason"]
                        trade_meta["modeling_resolution"] = intrabar_exit["resolution"]

                    if config.risk_per_trade_pct is not None and config.risk_per_trade_pct > 0:
                        max_position_pct = _resolve_max_position_pct(
                            config.max_position_pct, asset_class
                        )
                        max_leverage = _resolve_max_leverage(config.max_leverage)
                        if max_leverage < max_position_pct:
                            max_leverage = max_position_pct

                        risk_amount = equity * config.risk_per_trade_pct
                        stop_dist = _resolve_stop_distance(
                            entry_idx,
                            entry_price,
                            stop_loss_series=stop_loss_series,
                            atr_series=atr_series,
                        )

                        if not stop_dist or stop_dist <= 0:
                            stop_dist = entry_price * 0.02
                            logger.warning(
                                "Risk sizing fallback for %s at bar %s: no stop_loss/ATR, using %.2f%% default stop",
                                symbol,
                                entry_idx,
                                2.0,
                            )

                        base_qty = risk_amount / stop_dist
                        required_notional = base_qty * entry_price
                        cap_notional = equity * max_position_pct
                        leverage_notional_limit = equity * max_leverage

                        if required_notional <= cap_notional:
                            final_notional = required_notional
                        elif required_notional <= leverage_notional_limit:
                            final_notional = required_notional
                            logger.info(
                                "Leverage used for %s trade %s: required_notional=%.2f exceeds cap=%.2f (max_leverage=%.2fx)",
                                symbol,
                                trade_counter + 1,
                                required_notional,
                                cap_notional,
                                max_leverage,
                            )
                        else:
                            final_notional = leverage_notional_limit
                            achievable_risk = (final_notional / entry_price) * stop_dist
                            logger.warning(
                                "Risk target underfilled for %s trade %s: target=%.2f achievable=%.2f (cap=%.2f, max_leverage=%.2fx)",
                                symbol,
                                trade_counter + 1,
                                risk_amount,
                                achievable_risk,
                                cap_notional,
                                max_leverage,
                            )

                        qty = final_notional / entry_price
                    else:
                        qty = equity / entry_price

                    if direction == "LONG":
                        pnl = (exit_price - entry_price) * qty
                    else:
                        pnl = (entry_price - exit_price) * qty

                    trade = Trade(
                        id=trade_counter,
                        symbol=symbol,
                        direction=direction,
                        entry_time=entry_ts,
                        entry_price=round(entry_price, 6),
                        quantity=round(qty, 4),
                        exits=[
                            TradeExit(
                                time=exit_ts,
                                price=round(exit_price, 6),
                                quantity=round(qty, 4),
                                pnl=round(pnl, 2),
                                reason=exit_reason,
                            )
                        ],
                        pnl=round(pnl, 2),
                        holding_bars=i - entry_idx,
                        tags=["vectorbt"],
                        meta=trade_meta,
                    )
                    all_trades.append(trade)
                    equity += pnl

        equity = config.initial_capital
        for trade in sorted(all_trades, key=lambda item: item.entry_time):
            equity += trade.pnl
            all_equity.append(EquityPoint(time=trade.entry_time, equity=round(equity, 2)))

        now = datetime.now(timezone.utc)
        if hasattr(strategy_mod, "STRATEGY_META"):
            strategy_name = strategy_mod.STRATEGY_META.get("name", config.strategy_name)
            strategy_names = [strategy_name]
        else:
            strategy_names = [config.strategy_name]

        duration_days = 0
        try:
            from datetime import date as _date

            d_start = _date.fromisoformat(config.start_date)
            d_end = _date.fromisoformat(config.end_date)
            duration_days = (d_end - d_start).days
        except (ValueError, TypeError):
            pass

        meta = BacktestMeta(
            id=f"vbt-{config.strategy_name}-{now.strftime('%Y%m%d-%H%M%S')}",
            created_at=now.isoformat(),
            engine="vectorbt",
            strategies=strategy_names,
            backtest_start=config.start_date,
            backtest_end=config.end_date,
            duration_days=duration_days,
            total_bars=sum(1 for _ in all_trades),
            data={
                "timeframes": used_timeframes,
                "symbols": config.symbols,
                "universe": "",
                "source": "yfinance",
            },
            initial_capital=config.initial_capital,
            parameters=resolved_parameters,
        )

        return BacktestResult(meta=meta, trades=all_trades, equity_curve=all_equity)


def _load_strategy_module(path: str):
    p = Path(path).resolve()
    spec = importlib.util.spec_from_file_location("strategy", str(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _normalize_signal_output(output: Any, index: pd.Index) -> tuple[pd.DataFrame, IndicatorMap]:
    if isinstance(output, pd.DataFrame):
        if "entries" not in output.columns or "exits" not in output.columns:
            raise ValueError("Strategy DataFrame output must include 'entries' and 'exits' columns")
        signals = output.copy()
        signals.index = index
        signals["entries"] = _coerce_boolean_series(signals["entries"], index)
        signals["exits"] = _coerce_boolean_series(signals["exits"], index)
        return signals, {}

    if isinstance(output, (tuple, list)):
        if len(output) == 3:
            raw_entries, raw_exits, raw_indicators = output
        elif len(output) == 2:
            raw_entries, raw_exits = output
            raw_indicators = {}
        else:
            raise ValueError(
                "Tuple/list strategy output must be (entries, exits) or (entries, exits, indicators_dict)"
            )

        signals = pd.DataFrame(index=index)
        signals["entries"] = _coerce_boolean_series(raw_entries, index)
        signals["exits"] = _coerce_boolean_series(raw_exits, index)
        return signals, _normalize_indicator_map(raw_indicators, index)

    raise TypeError(
        "generate_signals() must return a pandas DataFrame or a tuple/list of "
        "(entries, exits, indicators_dict)"
    )


def _coerce_series(value: Any, index: pd.Index) -> pd.Series:
    if isinstance(value, pd.Series):
        series = value.copy()
        if len(series) != len(index):
            raise ValueError(
                f"Indicator series length mismatch: expected {len(index)}, got {len(series)}"
            )
        series.index = index
        return series

    try:
        series = pd.Series(value, index=index)
    except ValueError as exc:
        raise ValueError(f"Could not align series to DataFrame index: {exc}") from exc

    if len(series) != len(index):
        raise ValueError(
            f"Indicator series length mismatch: expected {len(index)}, got {len(series)}"
        )
    return series


def _coerce_boolean_series(value: Any, index: pd.Index) -> pd.Series:
    return _coerce_series(value, index).fillna(False).astype(bool)


def _normalize_indicator_map(indicators: Any, index: pd.Index) -> IndicatorMap:
    if indicators is None:
        return {}
    if not isinstance(indicators, dict):
        raise TypeError("indicators_dict must be a dict mapping indicator names to aligned series")

    normalized: IndicatorMap = {}
    for name, raw_value in indicators.items():
        if raw_value is None:
            continue

        when = ""
        value = raw_value
        if isinstance(raw_value, dict) and "series" in raw_value:
            when = str(raw_value.get("when", "entry")).lower()
            value = raw_value["series"]
        elif (
            isinstance(raw_value, tuple)
            and len(raw_value) == 2
            and isinstance(raw_value[0], str)
            and raw_value[0].lower() in {"entry", "exit"}
        ):
            when = raw_value[0].lower()
            value = raw_value[1]

        normalized[str(name)] = (_coerce_series(value, index), when)
    return normalized


def _infer_indicator_when(name: str, explicit_when: str) -> str:
    if explicit_when == "exit":
        return "exit"
    if explicit_when == "entry" and not (
        name.lower().startswith("exit_") or name.lower().endswith("_at_exit")
    ):
        return explicit_when
    lowered = name.lower()
    if lowered.startswith("exit_") or lowered.endswith("_at_exit"):
        return "exit"
    return "entry"


def _indicator_meta_key(name: str) -> str:
    lowered = name.lower()
    if lowered in {"exit_price", "exit_reason"}:
        return lowered
    if lowered.startswith("exit_"):
        return name[5:]
    if lowered == "stop_loss":
        return "sl_price"
    if lowered in {"take_profit", "tp_price"}:
        return "tp_price"
    return name


def _find_indicator_series(
    signals: pd.DataFrame, indicators: IndicatorMap, *, kind: str
) -> pd.Series | None:
    if kind == "direction" and "direction" in signals.columns:
        return signals["direction"]
    if kind == "stop_loss" and "stop_loss" in signals.columns:
        return signals["stop_loss"]
    if kind == "take_profit" and "take_profit" in signals.columns:
        return signals["take_profit"]
    if kind == "take_profit" and "tp_price" in signals.columns:
        return signals["tp_price"]
    if kind == "exit_price" and "exit_price" in signals.columns:
        return signals["exit_price"]
    if kind == "exit_reason" and "exit_reason" in signals.columns:
        return signals["exit_reason"]
    if kind == "atr":
        for column in signals.columns:
            if str(column).lower().startswith("atr"):
                return signals[column]

    desired_when = "exit" if kind in {"exit_price", "exit_reason"} else "entry"
    for name, (series, explicit_when) in indicators.items():
        when = _infer_indicator_when(name, explicit_when)
        if when != desired_when:
            continue

        lowered = name.lower()
        if kind == "direction" and lowered == "direction":
            return series
        if kind == "stop_loss" and lowered in {"stop_loss", "sl_price"}:
            return series
        if kind == "take_profit" and lowered in {"take_profit", "tp_price"}:
            return series
        if kind == "exit_price" and lowered == "exit_price":
            return series
        if kind == "exit_reason" and lowered == "exit_reason":
            return series
        if kind == "atr" and lowered.startswith("atr"):
            return series
    return None


def _find_stop_loss_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="stop_loss")


def _find_take_profit_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="take_profit")


def _find_atr_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="atr")


def _find_direction_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="direction")


def _find_exit_price_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="exit_price")


def _find_exit_reason_series(signals: pd.DataFrame, indicators: IndicatorMap) -> pd.Series | None:
    return _find_indicator_series(signals, indicators, kind="exit_reason")


def _normalize_direction_value(value: Any) -> str:
    if value in ("SHORT", "short", -1):
        return "SHORT"
    return "LONG"


def _resolve_direction(
    signals: pd.DataFrame, direction_series: pd.Series | None, entry_idx: int
) -> str:
    if "direction" in signals.columns:
        return _normalize_direction_value(signals["direction"].iloc[entry_idx])
    if direction_series is not None:
        return _normalize_direction_value(direction_series.iloc[entry_idx])
    return "LONG"


def _resolve_exit_price(
    exit_row: pd.Series, exit_idx: int, exit_price_series: pd.Series | None
) -> float:
    if exit_price_series is not None:
        exit_price = exit_price_series.iloc[exit_idx]
        if pd.notna(exit_price) and float(exit_price) > 0:
            return float(exit_price)
    return float(exit_row["close"])


def _resolve_exit_reason(exit_idx: int, exit_reason_series: pd.Series | None) -> str | None:
    if exit_reason_series is None:
        return None
    exit_reason = _serialize_meta_value(exit_reason_series.iloc[exit_idx])
    if exit_reason is None:
        return None
    return str(exit_reason)


def _resolve_level_value(series: pd.Series | None, idx: int) -> float | None:
    if series is None:
        return None
    value = series.iloc[idx]
    if pd.isna(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric <= 0:
        return None
    return numeric


def _resolve_stop_distance(
    entry_idx: int,
    entry_price: float,
    *,
    stop_loss_series: pd.Series | None,
    atr_series: pd.Series | None,
) -> float | None:
    if stop_loss_series is not None:
        sl = stop_loss_series.iloc[entry_idx]
        if pd.notna(sl) and float(sl) > 0:
            return abs(entry_price - float(sl))

    if atr_series is not None:
        atr = atr_series.iloc[entry_idx]
        if pd.notna(atr) and float(atr) > 0:
            return float(atr)

    return None


def _load_ohlcv(
    data_root: Path,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame | None, Path | None]:
    """Find and load OHLCV parquet for a symbol."""
    ohlcv_root = data_root / "ohlcv"
    matches = list(ohlcv_root.rglob(f"**/{symbol}/{timeframe}.parquet"))
    if not matches:
        return None, None

    selected_path = matches[0]
    df = pd.read_parquet(selected_path)
    df.columns = [c.lower() for c in df.columns]

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        if start_date:
            df = df[df["timestamp"] >= start_date]
        if end_date:
            df = df[df["timestamp"] <= end_date]

    return df.reset_index(drop=True), selected_path


def _normalize_intrabar_resolution(value: str | None) -> str:
    lowered = str(value or "open_close").strip().lower()
    if lowered in {"", "open_close", "open-close", "open/close only", "open_close_only"}:
        return "open_close"
    if lowered in {"1-lower", "1 timeframe lower", "one-lower"}:
        return "1-lower"
    if lowered == "m1":
        return "1m"
    if lowered in {"1m", "s1", "tick"}:
        return lowered
    return "open_close"


def _resolve_intrabar_timeframe(base_timeframe: str, intrabar_resolution: str | None) -> str | None:
    normalized = _normalize_intrabar_resolution(intrabar_resolution)
    if normalized == "open_close":
        return None

    if normalized == "1-lower":
        try:
            base_index = TIMEFRAME_ORDER.index(base_timeframe)
        except ValueError:
            return None
        if base_index <= 0:
            return None
        return TIMEFRAME_ORDER[base_index - 1]

    try:
        base_index = TIMEFRAME_ORDER.index(base_timeframe)
        target_index = TIMEFRAME_ORDER.index(normalized)
    except ValueError:
        return None

    if target_index >= base_index:
        return None
    return normalized


def _load_intrabar_ohlcv(
    data_root: Path,
    symbol: str,
    base_timeframe: str,
    intrabar_resolution: str | None,
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame | None, str | None]:
    target_timeframe = _resolve_intrabar_timeframe(base_timeframe, intrabar_resolution)
    if not target_timeframe:
        return None, None

    intrabar_df, _ = _load_ohlcv(data_root, symbol, target_timeframe, start_date, end_date)
    if intrabar_df is None or intrabar_df.empty:
        logger.info(
            "Intrabar data unavailable for %s/%s at %s; falling back to open/close exits",
            symbol,
            base_timeframe,
            target_timeframe,
        )
        return None, None
    return intrabar_df, target_timeframe


def _resolve_intrabar_exit(
    *,
    intrabar_df: pd.DataFrame | None,
    intrabar_timeframe: str | None,
    base_timeframe: str,
    direction: str,
    entry_time: str,
    scheduled_exit_time: str,
    stop_price: float | None,
    take_profit_price: float | None,
) -> dict[str, Any] | None:
    if intrabar_df is None or intrabar_df.empty or intrabar_timeframe is None:
        return None
    if stop_price is None and take_profit_price is None:
        return None

    entry_ts = pd.to_datetime(entry_time, utc=True, errors="coerce")
    exit_ts = pd.to_datetime(scheduled_exit_time, utc=True, errors="coerce")
    base_duration = TIMEFRAME_TO_DURATION.get(base_timeframe, pd.Timedelta(0))

    if pd.isna(entry_ts) or pd.isna(exit_ts):
        return None

    effective_start = entry_ts + base_duration
    effective_end = exit_ts + base_duration
    if effective_end <= effective_start:
        return None

    timestamps = pd.to_datetime(intrabar_df["timestamp"], utc=True, errors="coerce")
    window = intrabar_df.loc[(timestamps >= effective_start) & (timestamps < effective_end)]
    if window.empty:
        return None

    for row in window.itertuples(index=False):
        timestamp = pd.to_datetime(getattr(row, "timestamp"), utc=True, errors="coerce")
        if pd.isna(timestamp):
            continue
        high = float(getattr(row, "high"))
        low = float(getattr(row, "low"))
        event = _match_intrabar_exit_event(
            direction=direction,
            high=high,
            low=low,
            stop_price=stop_price,
            take_profit_price=take_profit_price,
        )
        if event is None:
            continue
        event["time"] = timestamp.isoformat()
        event["resolution"] = intrabar_timeframe
        return event

    return None


def _match_intrabar_exit_event(
    *,
    direction: str,
    high: float,
    low: float,
    stop_price: float | None,
    take_profit_price: float | None,
) -> dict[str, Any] | None:
    if direction == "LONG":
        stop_hit = stop_price is not None and low <= stop_price
        tp_hit = take_profit_price is not None and high >= take_profit_price
    else:
        stop_hit = stop_price is not None and high >= stop_price
        tp_hit = take_profit_price is not None and low <= take_profit_price

    if stop_hit:
        return {"price": float(stop_price), "reason": "stop_loss"}
    if tp_hit:
        return {"price": float(take_profit_price), "reason": "take_profit"}
    return None


def _infer_asset_class_from_path(data_path: Path | None) -> str:
    if data_path is None:
        return "unknown"
    parts = data_path.parts
    try:
        ohlcv_idx = parts.index("ohlcv")
        return parts[ohlcv_idx + 2].lower()
    except (ValueError, IndexError):
        return "unknown"


def _extract_trade_meta(
    signals: pd.DataFrame,
    indicators: IndicatorMap,
    entry_idx: int,
    exit_idx: int,
) -> dict[str, Any]:
    trade_meta: dict[str, Any] = {}

    for column in signals.columns:
        col_name = str(column)
        if col_name.startswith("meta_"):
            serialized = _serialize_meta_value(signals.iloc[entry_idx][column])
            if serialized is not None:
                trade_meta[col_name[5:]] = serialized
        elif col_name.startswith("exit_meta_"):
            serialized = _serialize_meta_value(signals.iloc[exit_idx][column])
            if serialized is not None:
                trade_meta[col_name[10:]] = serialized

    if "stop_loss" in signals.columns:
        serialized = _serialize_meta_value(signals["stop_loss"].iloc[entry_idx])
        if serialized is not None:
            trade_meta.setdefault("sl_price", serialized)

    for name, (series, explicit_when) in indicators.items():
        when = _infer_indicator_when(name, explicit_when)
        sample_idx = exit_idx if when == "exit" else entry_idx
        meta_key = _indicator_meta_key(name)
        serialized = _serialize_meta_value(series.iloc[sample_idx])
        if serialized is None:
            continue
        trade_meta[meta_key] = serialized
        if meta_key == "sl_price":
            trade_meta.setdefault("sl_price", serialized)

    return trade_meta


def _serialize_meta_value(value: Any) -> Any | None:
    if value is None:
        return None

    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()

    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass

    if isinstance(value, float):
        if pd.isna(value):
            return None
        return round(value, 6)

    if isinstance(value, (int, str, bool)):
        return value

    if pd.isna(value):
        return None

    return str(value)


def _resolve_max_position_pct(explicit: float | None, asset_class: str) -> float:
    if explicit is not None and explicit > 0:
        return float(explicit)
    if asset_class in {"equity", "equities", "etf", "etfs", "stocks"}:
        return 1.0
    return 0.20


def _resolve_max_leverage(explicit: float | None) -> float:
    if explicit is not None and explicit > 0:
        return float(explicit)
    return 2.0
