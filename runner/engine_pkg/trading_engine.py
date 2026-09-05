"""The native TradeEngine: bar-by-bar order lifecycle and position accounting.

Responsibilities kept here (single class, single responsibility axis: the
bar simulation loop):
- pending-order creation/fill (market/limit/stop/stop-limit)
- stop and take-profit monitoring incl. intrabar first-trigger resolution
- position sizing via the risk engine
- partial closes, trade finalization, equity accounting

Presentation (indicator series, result payload assembly) lives in
``runner/engine_pkg/result.py``; execution-model documentation in
``runner/engine_pkg/execution_model.py``; dataclasses in
``runner/engine_pkg/orders.py``.
"""

from __future__ import annotations

import math
from typing import Any, Callable

import pandas as pd

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import TradeSignal
from runner.engine_pkg._time import to_iso_utc
from runner.engine_pkg.execution_model import ExecutionModelSettings
from runner.engine_pkg.orders import Order, OrderType, Position
from runner.engine_pkg.result import build_indicator_series, build_result
from runner.engines.base import BacktestConfig
from runner.engines.contract import flag_missing_intrabar, resolve_same_bar_exit
from shared.models import BacktestResult, EquityPoint, IndicatorSeries, Trade, TradeExit

#: Engine-level concurrent-position policy for the native engine
#: (Cleanup 16-D1, F22). Deliberately a module constant, not a strategy
#: parameter: strategy ``parameters`` must not rewrite engine risk policy.
TRADE_ENGINE_MAX_POSITIONS = 5


class TradeEngine:
    def __init__(
        self,
        config: BacktestConfig,
        *,
        symbol: str,
        intrabar_data: pd.DataFrame | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
        progress_start: float = 10.0,
        progress_end: float = 90.0,
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
        self._execution_model = ExecutionModelSettings.from_config(config)
        self._realized_pnl = 0.0
        self._trade_counter = 0
        self._signals_frame: pd.DataFrame | None = None
        self._intrabar = self._prepare_intrabar(intrabar_data)
        self._progress_callback = progress_callback
        self._progress_start = float(progress_start)
        self._progress_end = float(progress_end)
        configured_risk_pct = float(self.config.risk_per_trade_pct or 0.0)
        self._risk_enabled = configured_risk_pct > 0
        max_total_exposure = float(self.config.max_position_pct or 0.05)
        # Risk-policy provenance (Cleanup 16-D1, F22): the engine-level risk
        # policy is engine configuration only. It is intentionally NOT read
        # from ``config.parameters`` — strategy parameters must not silently
        # rewrite engine risk policy (undocumented cross-talk). Set
        # ``risk_per_trade_pct`` / ``max_position_pct`` on the config instead.
        self._risk_engine = RiskEngine(
            RiskConfig(
                max_risk_per_trade=configured_risk_pct if configured_risk_pct > 0 else 0.01,
                max_total_exposure=max_total_exposure,
                max_positions=TRADE_ENGINE_MAX_POSITIONS,
                default_sl_atr_multiplier=2.0,
            )
        )

    def _emit_progress(self, percent: float, message: str) -> None:
        if self._progress_callback is None:
            return
        bounded = max(0.0, min(100.0, float(percent)))
        self._progress_callback(round(bounded, 2), message)

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
        if pd.isna(stop_loss):
            stop_loss = row.get("sl_price")
        stop_loss = float(stop_loss) if pd.notna(stop_loss) else None

        tp_levels: list[dict[str, Any]] = []
        tp_candidates: list[tuple[str, float]] = []
        for key in row.index:
            lower = str(key).lower()
            if lower in {"take_profit", "tp_price"} and pd.notna(row.get(key)):
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

    def _current_risk_exposure(self, reference_price: float) -> float:
        if self.equity <= 0:
            return 0.0

        risk_exposure = 0.0

        for position in self.positions:
            stop_price = (
                float(position.stop_loss)
                if position.stop_loss is not None
                else float(position.entry_price) * 0.98
            )
            risk_distance = abs(float(position.entry_price) - stop_price)
            risk_exposure += (risk_distance * float(position.remaining_qty)) / float(self.equity)

        for order in self.pending_orders:
            stop_price = (
                float(order.stop_loss)
                if order.stop_loss is not None
                else float(reference_price) * 0.98
            )
            risk_distance = abs(float(reference_price) - stop_price)
            risk_exposure += (risk_distance * float(order.quantity)) / float(self.equity)

        return max(0.0, risk_exposure)

    def _calculate_position_size(
        self,
        *,
        entry_price: float,
        stop_loss_price: float | None,
        direction: str,
        take_profit_price: float | None = None,
    ) -> float:
        max_leverage = float(self.config.max_leverage or 1.0)
        fixed_risk_amount = self.config.parameters.get("fixed_risk_amount")
        if fixed_risk_amount is None:
            fixed_risk_amount = self.config.parameters.get("Fixed_Risk_Amount")

        if fixed_risk_amount is not None and stop_loss_price is not None:
            risk_amount = float(fixed_risk_amount)
            risk_distance = abs(float(entry_price) - float(stop_loss_price))
            if risk_amount > 0 and risk_distance > 0:
                tick_value = float(self.config.parameters.get("tick_value", 1.0) or 1.0)
                tick_size = float(self.config.parameters.get("tick_size", 1.0) or 1.0)
                lot_step = float(self.config.parameters.get("lot_step", 0.0) or 0.0)
                min_lot = float(self.config.parameters.get("min_lot", 0.0) or 0.0)
                max_lot = float(self.config.parameters.get("max_lot", 0.0) or 0.0)
                shares = (risk_amount / risk_distance) * (tick_size / tick_value)
                if lot_step > 0:
                    shares = math.floor((shares / lot_step) + 1e-12) * lot_step
                if min_lot > 0:
                    shares = max(shares, min_lot)
                if max_lot > 0:
                    shares = min(shares, max_lot)
                if bool(self.config.parameters.get("cap_fixed_risk_by_leverage", True)):
                    max_value = self.equity * max_leverage
                    shares = min(shares, max_value / entry_price)
                return max(shares, 0.0)

        if self._risk_enabled:
            signal = TradeSignal(
                action="buy" if str(direction).upper() == "LONG" else "sell",
                symbol=self.symbol,
                entry_price=float(entry_price),
                stop_loss=stop_loss_price,
                take_profit=take_profit_price,
                timeframe=self.config.timeframe,
                metadata={
                    "current_positions": len(self.positions),
                    "pending_positions": len(self.pending_orders),
                },
            )
            sized_order = self._risk_engine.calculate_position_size(
                signal=signal,
                account_balance=float(self.equity),
                pip_value=1.0,
                current_exposure=self._current_risk_exposure(float(entry_price)),
            )
            shares = float(sized_order.position_size)
        else:
            shares = self.equity / entry_price

        max_value = self.equity * max_leverage
        shares = min(shares, max_value / entry_price)
        return max(shares, 0.0)

    @staticmethod
    def _to_iso(value: Any) -> str:
        return to_iso_utc(value)

    def _create_order(self, row: pd.Series, i: int, direction: str) -> None:
        entry_price = float(row["close"])
        stop_loss, tp_levels = self._extract_levels(row)
        stop_distance_raw = row.get("stop_distance")
        stop_distance = float(stop_distance_raw) if pd.notna(stop_distance_raw) else None
        take_profit = float(tp_levels[0]["price"]) if tp_levels else None
        qty = self._calculate_position_size(
            entry_price=entry_price,
            stop_loss_price=stop_loss,
            direction=direction,
            take_profit_price=take_profit,
        )
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
            stop_distance=stop_distance,
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
                order_type=order.order_type.value,
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
                    filled = (
                        trigger and order.limit_price is not None and bar_low <= order.limit_price
                    )
                elif order.direction == "SHORT" and order.stop_price is not None:
                    trigger = bar_low <= order.stop_price
                    filled = (
                        trigger and order.limit_price is not None and bar_high >= order.limit_price
                    )
                fill_price = order.limit_price if order.limit_price is not None else bar_open

            if filled:
                if (
                    order.order_type == OrderType.MARKET
                    and order.stop_distance is not None
                    and order.stop_distance > 0
                ):
                    distance = float(order.stop_distance)
                    order.stop_loss = (
                        float(fill_price) - distance
                        if order.direction == "LONG"
                        else float(fill_price) + distance
                    )
                if (
                    order.order_type in {OrderType.LIMIT, OrderType.STOP, OrderType.STOP_LIMIT}
                ):
                    # LIMIT/STOP/STOP_LIMIT orders fill at their trigger price,
                    # so they are re-sized from that trigger price instead of
                    # keeping the creation-price estimate (the creation-price
                    # sizing on the signal bar is only the pending-order
                    # estimate; market orders fill at the next bar's open and
                    # are re-sized there).
                    order.quantity = self._calculate_position_size(
                        entry_price=float(fill_price),
                        stop_loss_price=order.stop_loss,
                        direction=order.direction,
                        take_profit_price=(
                            float(order.take_profit_levels[0]["price"])
                            if order.take_profit_levels
                            else None
                        ),
                    )
                    if order.quantity <= 0:
                        continue
                if order.order_type == OrderType.MARKET and order.stop_loss is not None:
                    # MARKET: no stop → no risk-based re-size; the creation
                    # estimate (signal-bar close) is the documented fallback
                    # for stopless market entries.
                    take_profit = (
                        float(order.take_profit_levels[0]["price"])
                        if order.take_profit_levels
                        else None
                    )
                    order.quantity = self._calculate_position_size(
                        entry_price=float(fill_price),
                        stop_loss_price=order.stop_loss,
                        direction=order.direction,
                        take_profit_price=take_profit,
                    )
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
        if not position.exits:
            return

        self._trade_counter += 1
        pnl = sum(float(item.pnl or 0.0) for item in position.exits)
        fees = 0.0
        net_pnl = pnl - fees
        risk_per_share = abs(
            position.entry_price - (position.stop_loss or position.entry_price * 0.98)
        )
        risk_amount = risk_per_share * position.quantity
        r_multiple = (net_pnl / risk_amount) if risk_amount > 0 else None

        last_exit = position.exits[-1]
        pnl_pct = (
            (net_pnl / (position.entry_price * position.quantity)) * 100
            if position.quantity > 0
            else 0.0
        )

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
            meta={
                "exit_reason": last_exit.reason or "signal",
                "execution_model": self._execution_model.to_dict(),
                "entry_fill": {
                    "order_type": position.order_type,
                    "price_side": self._execution_model.entry_price_side,
                    "timestamp": position.entry_time,
                    "price": position.entry_price,
                },
                **dict(getattr(position, "meta", {}) or {}),
            },
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

    def _flag_same_bar_fallback(self, position: Position, bar_time: pd.Timestamp, next_time: pd.Timestamp | None) -> None:
        """Record the stop-first fallback flag on the position's future trade.

        Decision Record (Cleanup 16-A): when intrabar data was requested but
        could not order the two same-bar events, the stop-first tie-break is
        used and the result must be flagged. The flag lands in
        ``trade.meta["same_bar_priority"]`` via ``_finalize_position``; when
        the intrabar walk DID resolve the chronology the chronological event
        already won and no flag is recorded.

        The caller only reaches this method when the intrabar walk produced no
        ordered event, so a fallback ALWAYS applies here; the reason records
        whether sub-bars existed at all.
        """
        window = self._intrabar_window(bar_time, next_time)
        has_sub_bars = window is not None and not window.empty
        flag = flag_missing_intrabar(
            intrabar_resolution=self._execution_model.intrabar_resolution,
            intrabar_coverage=has_sub_bars,
            reason="intrabar_data_missing" if not has_sub_bars else "intrabar_unresolved",
        )
        if flag is not None:
            position.meta["same_bar_priority"] = flag

    def _intrabar_window(
        self, bar_time: pd.Timestamp, next_time: pd.Timestamp | None
    ) -> pd.DataFrame | None:
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

            # Conformance (Cleanup 16-A, F8): with intrabar data available the
            # actual chronological event wins — the shared helper classifies
            # each intrabar step in time order, so the first event seen is the
            # first event that happened, not the stop-first tie-break.
            event = resolve_same_bar_exit(
                direction=position.direction,
                high=sub_high,
                low=sub_low,
                stop_price=stop_price,
                take_profit_price=tp_price,
            )
            if event is not None:
                return event

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
                if self._execution_model.intrabar_resolution is not None:
                    first_event = self._intrabar_first_trigger(
                        position,
                        position.stop_loss,
                        first_tp_price,
                        bar_time,
                        next_time,
                    )
                if first_event is None:
                    # Conformance (Cleanup 16-A, F8): intrabar data was
                    # requested but could not order the two events — fall
                    # back to the stop-first tie-break and flag the trade so
                    # the ambiguity is visible downstream.
                    first_event = "stop"
                    self._flag_same_bar_fallback(position, bar_time, next_time)
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
            raise ValueError(
                "Strategy tuple output must be (entries, exits[, indicators, drawings])"
            )

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

    def _build_indicator_series(
        self, indicators: dict[str, Any], data: pd.DataFrame
    ) -> list[IndicatorSeries]:
        return build_indicator_series(indicators, data)

    def _build_result(self) -> BacktestResult:
        return build_result(
            config=self.config,
            symbol=self.symbol,
            closed_trades=self.closed_trades,
            equity_curve=self.equity_curve,
            indicator_series=self._indicator_series,
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

        total_bars = len(frame)
        progress_span = max(0.0, self._progress_end - self._progress_start)
        # Avoid noisy per-bar callbacks: at most once per 2% and at least every 1000 bars.
        by_percent = max(1, total_bars // 50)
        step_bars = max(1000, by_percent)
        next_emit = 0

        for i in range(total_bars):
            row = frame.iloc[i]
            next_time = frame.iloc[i + 1]["timestamp"] if i + 1 < total_bars else None
            signal_row = (
                self._signals_frame.iloc[i] if self._signals_frame is not None else pd.Series()
            )
            # F20: overlay the signal values onto the OHLCV row ELEMENT-WISE
            # (aligned by label). The previous ``pd.concat([row, signal_row])``
            # stacked the two Series vertically, producing a row of length
            # len(row) + len(signal_row) with duplicated labels — OHLCV lookups
            # became ambiguous and colliding signal names ("close", "high", …)
            # silently corrupted or crashed entry extraction.
            merged_row = row.combine_first(signal_row)

            self._check_stops(row, i, next_time)
            self._fill_pending_orders(row, i)

            if bool(entries.iloc[i]):
                direction = self._resolve_direction(merged_row)
                self._create_order(merged_row, i, direction)

            if bool(exits.iloc[i]):
                self._close_position(row, i, reason="signal")

            self._update_equity(row, i)

            if i >= next_emit or i == total_bars - 1:
                completed = i + 1
                bar_percent = int((completed / total_bars) * 100)
                mapped = self._progress_start + progress_span * (completed / total_bars)
                self._emit_progress(
                    mapped,
                    f"Running strategy: {completed}/{total_bars} bars ({bar_percent}%)",
                )
                next_emit = i + step_bars

        if self.positions:
            last_row = frame.iloc[-1]
            self._close_position(last_row, total_bars - 1, reason="end_of_data")
            self._update_equity(last_row, total_bars - 1)

        return self._build_result()
