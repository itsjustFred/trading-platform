from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from runner.engines.base import BacktestConfig
from shared.models import (
    BacktestMeta,
    BacktestResult,
    EquityPoint,
    IndicatorSeries,
    Trade,
    TradeExit,
)


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


@dataclass
class Order:
    symbol: str
    direction: str
    order_type: OrderType
    created_index: int
    created_time: str
    quantity: float
    limit_price: float | None = None
    stop_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    take_profit_levels: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Position:
    symbol: str
    direction: str
    entry_index: int
    entry_time: str
    entry_price: float
    quantity: float
    remaining_qty: float
    stop_loss: float | None = None
    take_profit_levels: list[dict[str, Any]] = field(default_factory=list)
    exits: list[TradeExit] = field(default_factory=list)
    mae: float = 0.0
    mfe: float = 0.0


class TradeEngine:
    def __init__(
        self,
        config: BacktestConfig,
        *,
        symbol: str,
        intrabar_data: pd.DataFrame | None = None,
    ):
        self.config = config
        self.symbol = symbol
        self.equity = config.initial_capital
        self.positions: list[Position] = []
        self.pending_orders: list[Order] = []
        self.closed_trades: list[Trade] = []
        self.equity_curve: list[EquityPoint] = []
        self.drawings: list[dict[str, Any]] = []
        self._indicator_series: list[IndicatorSeries] = []
        self._realized_pnl = 0.0
        self._trade_counter = 0
        self._signals_frame: pd.DataFrame | None = None
        self._intrabar = self._prepare_intrabar(intrabar_data)

    @staticmethod
    def _prepare_intrabar(intrabar_data: pd.DataFrame | None) -> pd.DataFrame | None:
        if intrabar_data is None or intrabar_data.empty:
            return None
        frame = intrabar_data.copy()
        frame.columns = [str(col).lower() for col in frame.columns]
        if "timestamp" not in frame.columns:
            return None
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).sort_values("timestamp")
        return frame

    @staticmethod
    def _coerce_bool_series(series: Any, index: pd.Index) -> pd.Series:
        if isinstance(series, pd.Series):
            coerced = series.reindex(index).fillna(False)
        else:
            coerced = pd.Series(series, index=index)
            coerced = coerced.reindex(index).fillna(False)
        return coerced.astype(bool)

    @staticmethod
    def _coerce_float_series(series: Any, index: pd.Index) -> pd.Series:
        if isinstance(series, pd.Series):
            values = pd.to_numeric(series.reindex(index), errors="coerce")
        else:
            values = pd.to_numeric(pd.Series(series, index=index), errors="coerce")
            values = values.reindex(index)
        return values

    @staticmethod
    def _uses_kwargs_style(func: Callable[..., Any]) -> bool:
        try:
            import inspect

            sig = inspect.signature(func)
            params = list(sig.parameters.values())
        except (ValueError, TypeError):
            return False

        if len(params) < 2:
            return False
        if params[1].name == "params":
            return False
        return any(p.default is not p.empty for p in params[1:])

    def _call_strategy(self, strategy_fn: Callable[..., Any], data: pd.DataFrame) -> Any:
        if self._uses_kwargs_style(strategy_fn):
            import inspect

            sig = inspect.signature(strategy_fn)
            accepted = {p.name for p in list(sig.parameters.values())[1:]}
            kwargs = {k: v for k, v in self.config.parameters.items() if k in accepted}
            return strategy_fn(data, **kwargs)
        return strategy_fn(data, self.config.parameters)

    def _extract_levels(self, row: pd.Series) -> tuple[float | None, list[dict[str, Any]]]:
        stop_loss = row.get("stop_loss")
        stop_loss = float(stop_loss) if pd.notna(stop_loss) else None

        tp_levels: list[dict[str, Any]] = []
        tp_candidates: list[tuple[str, float]] = []
        for key in row.index:
            lower = str(key).lower()
            if lower == "take_profit" and pd.notna(row.get(key)):
                tp_candidates.append(("tp", float(row.get(key))))
            elif lower.startswith("tp") and lower[2:].isdigit() and pd.notna(row.get(key)):
                tp_candidates.append((lower, float(row.get(key))))

        tp_candidates.sort(key=lambda item: item[0])
        if tp_candidates:
            default_fraction = 1.0 / len(tp_candidates)
            for name, price in tp_candidates:
                fraction_key = f"{name}_pct"
                fraction = row.get(fraction_key)
                fraction_val = float(fraction) if pd.notna(fraction) else default_fraction
                tp_levels.append(
                    {
                        "name": name,
                        "price": float(price),
                        "fraction": max(0.0, min(1.0, fraction_val)),
                        "filled": False,
                    }
                )

        return stop_loss, tp_levels

    def _resolve_direction(self, row: pd.Series) -> str:
        direction = str(row.get("direction", "LONG")).upper()
        return "SHORT" if direction == "SHORT" else "LONG"

    def _calculate_position_size(self, entry_price: float, stop_loss_price: float | None) -> float:
        max_leverage = float(self.config.max_leverage or 1.0)

        if self.config.risk_per_trade_pct and stop_loss_price:
            risk_amount = self.equity * float(self.config.risk_per_trade_pct)
            distance = abs(entry_price - stop_loss_price)
            if distance > 0:
                shares = risk_amount / distance
            else:
                shares = risk_amount / (entry_price * 0.02)
        else:
            shares = self.equity / entry_price

        max_value = self.equity * max_leverage
        shares = min(shares, max_value / entry_price)
        return max(shares, 0.0)

    @staticmethod
    def _to_iso(value: Any) -> str:
        ts = pd.Timestamp(value, tz="UTC") if not isinstance(value, pd.Timestamp) else value
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.isoformat()

    def _create_order(self, row: pd.Series, i: int, direction: str) -> None:
        entry_price = float(row["close"])
        stop_loss, tp_levels = self._extract_levels(row)
        qty = self._calculate_position_size(entry_price, stop_loss)
        if qty <= 0:
            return

        created_time = self._to_iso(row["timestamp"])
        order = Order(
            symbol=self.symbol,
            direction=direction,
            order_type=OrderType.MARKET,
            created_index=i,
            created_time=created_time,
            quantity=qty,
            stop_loss=stop_loss,
            take_profit_levels=tp_levels,
        )
        self.pending_orders.append(order)

    def _open_position(self, order: Order, fill_price: float, fill_time: str, i: int) -> None:
        self.positions.append(
            Position(
                symbol=order.symbol,
                direction=order.direction,
                entry_index=i,
                entry_time=fill_time,
                entry_price=float(fill_price),
                quantity=float(order.quantity),
                remaining_qty=float(order.quantity),
                stop_loss=order.stop_loss,
                take_profit_levels=[dict(level) for level in order.take_profit_levels],
            )
        )

    def _fill_pending_orders(self, row: pd.Series, i: int) -> None:
        if not self.pending_orders:
            return

        remaining: list[Order] = []
        bar_open = float(row["open"])
        bar_high = float(row["high"])
        bar_low = float(row["low"])
        fill_time = self._to_iso(row["timestamp"])

        for order in self.pending_orders:
            filled = False
            fill_price = bar_open

            if order.order_type == OrderType.MARKET:
                filled = i > order.created_index
                fill_price = bar_open
            elif order.order_type == OrderType.LIMIT:
                if order.direction == "LONG" and order.limit_price is not None:
                    filled = bar_low <= order.limit_price
                    fill_price = order.limit_price
                elif order.direction == "SHORT" and order.limit_price is not None:
                    filled = bar_high >= order.limit_price
                    fill_price = order.limit_price
            elif order.order_type == OrderType.STOP:
                if order.direction == "LONG" and order.stop_price is not None:
                    filled = bar_high >= order.stop_price
                    fill_price = order.stop_price
                elif order.direction == "SHORT" and order.stop_price is not None:
                    filled = bar_low <= order.stop_price
                    fill_price = order.stop_price
            elif order.order_type == OrderType.STOP_LIMIT:
                trigger = False
                if order.direction == "LONG" and order.stop_price is not None:
                    trigger = bar_high >= order.stop_price
                    filled = trigger and order.limit_price is not None and bar_low <= order.limit_price
                elif order.direction == "SHORT" and order.stop_price is not None:
                    trigger = bar_low <= order.stop_price
                    filled = trigger and order.limit_price is not None and bar_high >= order.limit_price
                fill_price = order.limit_price if order.limit_price is not None else bar_open

            if filled:
                if order.order_type == OrderType.MARKET and order.stop_loss is not None:
                    order.quantity = self._calculate_position_size(float(fill_price), order.stop_loss)
                    if order.quantity <= 0:
                        continue
                self._open_position(order, float(fill_price), fill_time, i)
            else:
                remaining.append(order)

        self.pending_orders = remaining

    def _close_partial(
        self,
        position: Position,
        price: float,
        qty: float,
        time_iso: str,
        reason: str,
    ) -> None:
        qty = max(0.0, min(qty, position.remaining_qty))
        if qty <= 0:
            return

        if position.direction == "LONG":
            pnl = (price - position.entry_price) * qty
        else:
            pnl = (position.entry_price - price) * qty

        position.exits.append(
            TradeExit(
                time=time_iso,
                price=float(price),
                quantity=float(qty),
                pnl=float(pnl),
                reason=reason,
            )
        )
        position.remaining_qty -= qty
        self._realized_pnl += pnl

    def _finalize_position(self, position: Position, exit_index: int) -> None:
        if position.remaining_qty > 1e-9:
            return

        self._trade_counter += 1
        pnl = sum(float(item.pnl or 0.0) for item in position.exits)
        fees = 0.0
        net_pnl = pnl - fees
        risk_per_share = abs(position.entry_price - (position.stop_loss or position.entry_price * 0.98))
        risk_amount = risk_per_share * position.quantity
        r_multiple = (net_pnl / risk_amount) if risk_amount > 0 else None

        last_exit = position.exits[-1]
        pnl_pct = (net_pnl / (position.entry_price * position.quantity)) * 100 if position.quantity > 0 else 0.0

        trade = Trade(
            id=self._trade_counter,
            symbol=position.symbol,
            direction=position.direction,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            quantity=position.quantity,
            exits=position.exits,
            pnl=float(pnl),
            pnl_pct=float(pnl_pct),
            fees=fees,
            net_pnl=float(net_pnl),
            r_multiple=float(r_multiple) if r_multiple is not None else None,
            mae=float(position.mae),
            mfe=float(position.mfe),
            holding_bars=max(0, int(exit_index - position.entry_index)),
            meta={"exit_reason": last_exit.reason or "signal"},
            tags=["native"],
        )
        self.closed_trades.append(trade)

    def _close_position(self, row: pd.Series, i: int, reason: str) -> None:
        if not self.positions:
            return

        close_price = float(row["close"])
        time_iso = self._to_iso(row["timestamp"])
        next_positions: list[Position] = []

        for position in self.positions:
            self._close_partial(
                position,
                price=close_price,
                qty=position.remaining_qty,
                time_iso=time_iso,
                reason=reason,
            )
            self._finalize_position(position, i)
            if position.remaining_qty > 1e-9:
                next_positions.append(position)

        self.positions = next_positions

    def _intrabar_window(self, bar_time: pd.Timestamp, next_time: pd.Timestamp | None) -> pd.DataFrame | None:
        if self._intrabar is None or self._intrabar.empty:
            return None
        if next_time is None:
            return self._intrabar[self._intrabar["timestamp"] >= bar_time]
        return self._intrabar[
            (self._intrabar["timestamp"] >= bar_time) & (self._intrabar["timestamp"] < next_time)
        ]

    def _intrabar_first_trigger(
        self,
        position: Position,
        stop_price: float | None,
        tp_price: float | None,
        bar_time: pd.Timestamp,
        next_time: pd.Timestamp | None,
    ) -> str | None:
        if stop_price is None and tp_price is None:
            return None

        window = self._intrabar_window(bar_time, next_time)
        if window is None or window.empty:
            return None

        for _, row in window.iterrows():
            sub_high = float(row["high"])
            sub_low = float(row["low"])

            stop_hit = False
            tp_hit = False
            if position.direction == "LONG":
                stop_hit = stop_price is not None and sub_low <= stop_price
                tp_hit = tp_price is not None and sub_high >= tp_price
            else:
                stop_hit = stop_price is not None and sub_high >= stop_price
                tp_hit = tp_price is not None and sub_low <= tp_price

            if stop_hit and tp_hit:
                return "stop"
            if stop_hit:
                return "stop"
            if tp_hit:
                return "tp"

        return None

    def _check_stops(self, row: pd.Series, i: int, next_time: pd.Timestamp | None) -> None:
        if not self.positions:
            return

        bar_high = float(row["high"])
        bar_low = float(row["low"])
        bar_time = pd.Timestamp(row["timestamp"])
        if bar_time.tzinfo is None:
            bar_time = bar_time.tz_localize("UTC")
        else:
            bar_time = bar_time.tz_convert("UTC")
        time_iso = self._to_iso(row["timestamp"])

        remaining_positions: list[Position] = []
        for position in self.positions:
            if position.direction == "LONG":
                stop_hit = position.stop_loss is not None and bar_low <= float(position.stop_loss)
            else:
                stop_hit = position.stop_loss is not None and bar_high >= float(position.stop_loss)

            pending_tp = [
                level for level in position.take_profit_levels if not bool(level.get("filled"))
            ]
            if position.direction == "LONG":
                tp_hit = any(bar_high >= float(level["price"]) for level in pending_tp)
            else:
                tp_hit = any(bar_low <= float(level["price"]) for level in pending_tp)

            first_tp_price = float(pending_tp[0]["price"]) if pending_tp else None
            first_event = None
            if stop_hit and tp_hit:
                if self.config.modeling_resolution and self.config.modeling_resolution != self.config.timeframe:
                    first_event = self._intrabar_first_trigger(
                        position,
                        position.stop_loss,
                        first_tp_price,
                        bar_time,
                        next_time,
                    )
                if first_event is None:
                    first_event = "stop"
            elif stop_hit:
                first_event = "stop"
            elif tp_hit:
                first_event = "tp"

            if first_event == "stop" and position.stop_loss is not None:
                self._close_partial(
                    position,
                    price=float(position.stop_loss),
                    qty=position.remaining_qty,
                    time_iso=time_iso,
                    reason="stop_loss",
                )
            elif first_event == "tp":
                for level in pending_tp:
                    level_price = float(level["price"])
                    if position.direction == "LONG":
                        reached = bar_high >= level_price
                    else:
                        reached = bar_low <= level_price
                    if not reached:
                        continue

                    fraction = float(level.get("fraction", 0.0))
                    qty = position.quantity * fraction
                    if level is pending_tp[-1]:
                        qty = position.remaining_qty

                    self._close_partial(
                        position,
                        price=level_price,
                        qty=qty,
                        time_iso=time_iso,
                        reason=str(level.get("name") or "take_profit"),
                    )
                    level["filled"] = True

                    if position.remaining_qty <= 1e-9:
                        break

                if position.remaining_qty > 1e-9 and stop_hit and position.stop_loss is not None:
                    self._close_partial(
                        position,
                        price=float(position.stop_loss),
                        qty=position.remaining_qty,
                        time_iso=time_iso,
                        reason="stop_loss",
                    )

            self._finalize_position(position, i)
            if position.remaining_qty > 1e-9:
                remaining_positions.append(position)

        self.positions = remaining_positions

    def _update_equity(self, row: pd.Series, i: int) -> None:
        close_price = float(row["close"])
        unrealized = 0.0

        for position in self.positions:
            if position.direction == "LONG":
                unrealized += (close_price - position.entry_price) * position.remaining_qty
                high_pnl = (float(row["high"]) - position.entry_price) * position.remaining_qty
                low_pnl = (float(row["low"]) - position.entry_price) * position.remaining_qty
            else:
                unrealized += (position.entry_price - close_price) * position.remaining_qty
                high_pnl = (position.entry_price - float(row["low"])) * position.remaining_qty
                low_pnl = (position.entry_price - float(row["high"])) * position.remaining_qty

            position.mfe = max(position.mfe, high_pnl)
            position.mae = min(position.mae, low_pnl)

        self.equity = self.config.initial_capital + self._realized_pnl + unrealized
        self.equity_curve.append(
            EquityPoint(time=self._to_iso(row["timestamp"]), equity=round(float(self.equity), 2))
        )

    def _normalize_strategy_output(
        self, output: Any, data: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series, dict[str, Any], list[dict[str, Any]]]:
        index = data.index

        if isinstance(output, pd.DataFrame):
            if "entries" not in output.columns or "exits" not in output.columns:
                raise ValueError("DataFrame strategy output requires 'entries' and 'exits' columns")
            self._signals_frame = output.copy()
            entries = self._coerce_bool_series(output["entries"], index)
            exits = self._coerce_bool_series(output["exits"], index)
            return entries, exits, {}, []

        if not isinstance(output, (tuple, list)):
            raise TypeError("Strategy output must be DataFrame or tuple/list")

        if len(output) == 4:
            entries, exits, indicators, drawings = output
        elif len(output) == 3:
            entries, exits, indicators = output
            drawings = []
        elif len(output) == 2:
            entries, exits = output
            indicators = {}
            drawings = []
        else:
            raise ValueError("Strategy tuple output must be (entries, exits[, indicators, drawings])")

        self._signals_frame = pd.DataFrame(index=index)
        self._signals_frame["entries"] = self._coerce_bool_series(entries, index)
        self._signals_frame["exits"] = self._coerce_bool_series(exits, index)

        if isinstance(indicators, dict):
            for key, value in indicators.items():
                if isinstance(value, pd.Series) and len(value) == len(index):
                    self._signals_frame[key] = value.values

        return (
            self._signals_frame["entries"],
            self._signals_frame["exits"],
            indicators if isinstance(indicators, dict) else {},
            drawings if isinstance(drawings, list) else [],
        )

    def _build_indicator_series(self, indicators: dict[str, Any], data: pd.DataFrame) -> list[IndicatorSeries]:
        points_time = [self._to_iso(ts) for ts in data["timestamp"]]
        output: list[IndicatorSeries] = []

        for name, value in (indicators or {}).items():
            if isinstance(value, tuple) and len(value) == 2:
                series, pane = value
                pane_type = str(pane)
            else:
                series = value
                pane_type = "overlay"

            if not isinstance(series, pd.Series):
                continue
            numeric = pd.to_numeric(series.reindex(data.index), errors="coerce")
            data_points = [
                {"time": points_time[idx], "value": float(val)}
                for idx, val in enumerate(numeric)
                if pd.notna(val)
            ]
            if not data_points:
                continue
            output.append(IndicatorSeries(name=str(name), type=pane_type, data=data_points, style={}))

        return output

    def _build_result(self) -> BacktestResult:
        created_at = datetime.now(timezone.utc).isoformat()
        meta = BacktestMeta(
            id=f"native-{self.config.strategy_name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
            created_at=created_at,
            engine="native",
            strategies=[self.config.strategy_name],
            backtest_start=self.config.start_date,
            backtest_end=self.config.end_date,
            data={
                "symbols": [self.symbol],
                "timeframes": [self.config.timeframe],
                "source": "ohlcv",
            },
            initial_capital=self.config.initial_capital,
            parameters=self.config.parameters,
        )
        return BacktestResult(
            meta=meta,
            trades=self.closed_trades,
            equity_curve=self.equity_curve,
            indicators=self._indicator_series,
            drawings=self.drawings,
        )

    def run(self, data: pd.DataFrame, strategy_fn: Callable[..., Any]) -> BacktestResult:
        if data is None or data.empty:
            return self._build_result()

        frame = data.copy()
        frame.columns = [str(col).lower() for col in frame.columns]
        if "timestamp" not in frame.columns:
            if isinstance(frame.index, pd.DatetimeIndex):
                frame = frame.reset_index().rename(columns={"index": "timestamp"})
            else:
                raise ValueError("DataFrame must include a timestamp column")

        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).reset_index(drop=True)

        signal_output = self._call_strategy(strategy_fn, frame)
        entries, exits, indicators, drawings = self._normalize_strategy_output(signal_output, frame)
        self._indicator_series = self._build_indicator_series(indicators, frame)
        self.drawings = drawings

        for i in range(len(frame)):
            row = frame.iloc[i]
            next_time = frame.iloc[i + 1]["timestamp"] if i + 1 < len(frame) else None
            signal_row = self._signals_frame.iloc[i] if self._signals_frame is not None else pd.Series()
            merged_row = pd.concat([row, signal_row])

            self._check_stops(row, i, next_time)
            self._fill_pending_orders(row, i)

            if bool(entries.iloc[i]):
                direction = self._resolve_direction(merged_row)
                self._create_order(merged_row, i, direction)

            if bool(exits.iloc[i]):
                self._close_position(row, i, reason="signal")

            self._update_equity(row, i)

        if self.positions:
            last_row = frame.iloc[-1]
            self._close_position(last_row, len(frame) - 1, reason="end_of_data")
            self._update_equity(last_row, len(frame) - 1)

        return self._build_result()
