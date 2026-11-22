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

import logging
import math
from typing import Any, Callable

import pandas as pd

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import SizedOrder, TradeSignal
from runner.engine_pkg._time import to_iso_utc
from runner.engine_pkg.execution_model import ExecutionModelSettings
from runner.engine_pkg.orders import Order, OrderType, Position
from runner.engine_pkg.result import build_indicator_series, build_result
from runner.engines._shared import resolve_ask_high_low, resolve_bar_spread
from runner.engines.base import BacktestConfig
from runner.engines.contract import flag_missing_intrabar, resolve_same_bar_exit
from runner.fees import resolve_fee_model
from shared.models import (
    BacktestResult,
    EquityPoint,
    IndicatorSeries,
    Trade,
    TradeExit,
    normalize_direction_value,
)

logger = logging.getLogger(__name__)

#: Engine-level concurrent-position policy for the native engine
#: (Cleanup 16-D1, F22). Deliberately a module constant, not a strategy
#: parameter: strategy ``parameters`` must not rewrite engine risk policy.
TRADE_ENGINE_MAX_POSITIONS = 5


def _tp_sort_key(item: tuple[str, float]) -> tuple[int, int, str]:
    """Order TP candidates: plain ``tp`` first, then ``tpN`` by numeric N (#154)."""
    name = item[0]
    if name == "tp":
        return (0, 0, name)
    return (1, int(name[2:]), name)


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
        # Sizing capital (Cleanup 16-D1, F21 / decision record D2; #262):
        # position sizing draws on REALIZED CASH only — ``initial_capital``
        # plus the net PnL of closed trades. The mark-to-market
        # ``self.equity`` stays the *reporting* equity (it feeds the equity
        # curve) and must never reach a sizing decision: an open position's
        # paper profit is not spendable, and an unrealized drawdown is not a
        # realized loss. The basis is decided by D2; the references are
        # consistent with it but cannot distinguish the two, because none of
        # them ever sizes while a position is open — MT5 risks a fixed cash
        # amount and reads no account value at all (the repo's MT5 sizing
        # fallback starts at the capital and is credited only when a trade
        # closes), and the vectorbt runner holds one position at a time
        # (``in_trade``) with no partial exit, so it credits ``equity`` with
        # ``pnl - fees`` only at the close and never sizes against an open
        # mark. Realized cash equals mark-to-market there.
        self._sizing_equity = float(config.initial_capital)
        self._trade_counter = 0
        self._signals_frame: pd.DataFrame | None = None
        self._intrabar = self._prepare_intrabar(intrabar_data)
        self._progress_callback = progress_callback
        self._progress_start = float(progress_start)
        self._progress_end = float(progress_end)
        configured_risk_pct = float(self.config.risk_per_trade_pct or 0.0)
        self._risk_enabled = configured_risk_pct > 0
        # B4 (16-E remainder): BacktestConfig folds the deprecated alias
        # into max_position_exposure_pct at construction (with warning), so
        # only the canonical field is read here. The silent `or 0.05`
        # fallback is gone — unset means the RiskConfig default, named
        # explicitly.
        _position_cap = self.config.max_position_exposure_pct
        self._max_position_exposure_pct = (
            float(_position_cap) if _position_cap is not None else None
        )
        max_total_exposure = RiskConfig().max_total_exposure
        max_leverage = float(self.config.max_leverage or 1.0)
        self._max_leverage = max_leverage

        def risk_config(
            *, total_exposure: float, max_positions: int
        ) -> RiskConfig:
            # Risk-policy provenance (Cleanup 16-D1, F22): the engine-level risk
            # policy is engine configuration only. It is intentionally NOT read
            # from ``config.parameters`` — strategy parameters must not silently
            # rewrite engine risk policy (undocumented cross-talk). Set
            # ``risk_per_trade_pct`` / ``max_position_pct`` on the config instead.
            return RiskConfig(
                max_risk_per_trade=configured_risk_pct if configured_risk_pct > 0 else 0.01,
                max_total_exposure=total_exposure,
                max_positions=max_positions,
                default_sl_atr_multiplier=2.0,
                max_notional_per_trade=1.0,
                allow_fallback_stop=bool(self.config.allow_fallback_stop),
                reject_missing_stop=bool(self.config.reject_missing_stop),
            )

        self._risk_engine = RiskEngine(
            risk_config(
                total_exposure=max_total_exposure,
                max_positions=TRADE_ENGINE_MAX_POSITIONS,
            )
        )
        # #279: the fixed-risk protocol gets its own engine instance so its
        # aggregate-exposure and position limits can be configured
        # independently (``fixed_risk_max_total_exposure`` /
        # ``fixed_risk_max_positions``); ``None`` keeps the same defaults the
        # pct path uses.
        self._fixed_risk_engine = RiskEngine(
            risk_config(
                total_exposure=(
                    float(self.config.fixed_risk_max_total_exposure)
                    if self.config.fixed_risk_max_total_exposure is not None
                    else max_total_exposure
                ),
                max_positions=(
                    int(self.config.fixed_risk_max_positions)
                    if self.config.fixed_risk_max_positions is not None
                    else TRADE_ENGINE_MAX_POSITIONS
                ),
            )
        )
        # The SizedOrder from the most recent ``_calculate_position_size``
        # engine call (None when no engine sized it). Callers read it
        # immediately - it carries the realized (possibly clamped) risk
        # amount and the rejection reason that would otherwise be discarded.
        self._last_sizing: SizedOrder | None = None

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
            elif lower.startswith("tp") and lower[2:].isdecimal() and pd.notna(row.get(key)):
                tp_candidates.append((lower, float(row.get(key))))

        # Plain "tp" (take_profit / tp_price) first, then numbered levels by
        # their numeric suffix so tp10 follows tp9, not tp1.
        tp_candidates.sort(key=_tp_sort_key)
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
        # No direction column => long-only strategy. A present-but-unknown
        # value (NaN, 0, "BUY", ...) raises rather than silently trading LONG.
        if "direction" not in row.index:
            return "LONG"
        return normalize_direction_value(row["direction"])

    def _refresh_sizing_equity(self) -> None:
        """Recompute the spendable sizing cash from the realized ledger.

        ``_close_partial`` books exit legs gross and ``_finalize_position``
        backs the whole round-trip fee out only when the last leg closes, so a
        position's realized cash is not fully spendable until it is finalized.
        What is held back is exactly the cost the fee model has ALREADY
        incurred for every still-open position, and nothing it has not:

        - the ENTRY leg on the position's original quantity (plus the
          per-trade surcharge, owed from the moment the position was
          opened) — which is why a position with no exit yet holds its entry
          fee back too, and
        - the EXIT fee of every leg that has already closed, priced at that
          leg's own fill price.

        The exit leg of the still-open remainder is a FUTURE cost and is
        deliberately not held back: sizing may not be reduced by a fee the
        position has not incurred (and may never incur at that price). At
        finalization the ledger charges ``fee_for_trade``, which is exactly
        the entry leg plus every exit leg, so the held-back estimate and the
        charged figure meet there.
        """
        owed = 0.0
        fee_model = None
        for position in self.positions:
            # A finalized position's fees are already in ``_realized_pnl``;
            # an open one (with or without exits) still owes its entry leg.
            if position.remaining_qty <= 1e-9:
                continue
            if fee_model is None:
                fee_model = resolve_fee_model(self.config.parameters)
            owed += fee_model.entry_fee(
                float(position.entry_price), float(position.quantity)
            )
            for item in position.exits:
                owed += fee_model.exit_fee(
                    float(item.price or 0.0), float(item.quantity or 0.0)
                )
        self._sizing_equity = self.config.initial_capital + self._realized_pnl - owed

    def _current_risk_exposure(
        self,
        reference_price: float,
        exclude_order: Order | None = None,
        *,
        pip: float = 1.0,
    ) -> float:
        # F21 (D2): the aggregate-risk denominator is the same realized cash
        # the sizing call uses — never the mark-to-market reporting equity.
        # ``pip`` is the caller's ``pip_value`` contract (account-currency P&L
        # of one unit per 1.0 price move): open risk is measured in account
        # currency, so a position's ``|entry - stop| * qty`` must be scaled by
        # it — the fixed-risk path passes ``tick_value / tick_size`` (e.g.
        # 1e5 for FX), the pct path keeps the share convention 1.0. A position
        # sized by the fixed-risk protocol carries ``sizing_risk_per_unit``
        # (the declared distance x pip, so an ATR ``stop_distance`` is not
        # re-measured against ``|entry - stop|``) and is summed directly.
        if self._sizing_equity <= 0:
            return 0.0

        risk_exposure = 0.0

        for position in self.positions:
            per_unit = (position.meta or {}).get("sizing_risk_per_unit")
            if per_unit is not None:
                risk_exposure += (
                    float(position.remaining_qty) * float(per_unit)
                ) / float(self._sizing_equity)
                continue
            stop_loss = position.stop_loss
            if stop_loss is None:
                stop_loss = position.meta.get("sizing_stop_loss")
            if stop_loss is None:
                raise ValueError(
                    "Cannot compute exposure for a stopless open position"
                )
            risk_distance = abs(float(position.entry_price) - float(stop_loss))
            risk_exposure += (
                risk_distance * float(position.remaining_qty) * pip
            ) / float(self._sizing_equity)

        for order in self.pending_orders:
            if exclude_order is not None and order is exclude_order:
                continue
            per_unit = getattr(order, "_sizing_risk_per_unit", None)
            if per_unit is not None:
                risk_exposure += (
                    float(order.quantity) * float(per_unit)
                ) / float(self._sizing_equity)
                continue
            stop_loss = order.stop_loss
            if stop_loss is None:
                stop_loss = getattr(order, "_sizing_stop_loss", None)
            if stop_loss is None:
                trigger_price = (
                    order.limit_price
                    if order.order_type == OrderType.LIMIT
                    else order.stop_price
                )
                if trigger_price is not None:
                    direction_sign = 1.0 if order.direction == "LONG" else -1.0
                    risk_distance = abs(float(reference_price) - float(trigger_price))
                    if risk_distance <= 0:
                        risk_distance = (
                            float(reference_price)
                            * self._risk_engine.config.fallback_stop_pct
                        )
                    stop_loss = float(reference_price) - direction_sign * risk_distance
            if stop_loss is None:
                raise ValueError(
                    "Cannot compute exposure for a stopless pending order"
                )
            risk_distance = abs(float(reference_price) - float(stop_loss))
            risk_exposure += (
                risk_distance * float(order.quantity) * pip
            ) / float(self._sizing_equity)

        return max(0.0, risk_exposure)

    def _free_margin(self, mark_price: float) -> float:
        """MT5-style free margin: equity − used margin, marked at ``mark_price``.

        ``equity`` is mark-to-market — deliberately NOT the realized-cash
        sizing basis: D2 governs what may be *risked*, while the margin
        check governs what the broker would *accept* (#347, decisions on
        #267/#240/#329). The balance leg is ``_sizing_equity`` — realized
        cash net of every fee already incurred, including the entry leg of
        still-open positions, which is what MT5 has already charged — plus
        the floating PnL at ``mark_price``. Used margin is each open
        position's notional at the same mark divided by the
        account/asset maximum leverage (``_max_leverage`` — 1x when the
        broker maximum is unknown); pending orders hold no margin, as on
        MT5.
        """
        if not math.isfinite(mark_price) or mark_price <= 0:
            return 0.0
        leverage = self._max_leverage
        equity = float(self._sizing_equity)
        used_margin = 0.0
        for position in self.positions:
            qty = float(position.remaining_qty or 0.0)
            if qty <= 1e-9:
                continue
            entry = float(position.entry_price)
            if position.direction == "LONG":
                equity += (mark_price - entry) * qty
            else:
                equity += (entry - mark_price) * qty
            used_margin += qty * mark_price / leverage
        return equity - used_margin

    def _free_margin_quantity_cap(self, entry_price: float) -> float:
        """Largest quantity the free-margin leverage rule affords (unfloored).

        A broker accepts an order whose margin (notional / max leverage)
        fits in free margin, i.e. ``notional <= free_margin * max_leverage``.
        """
        free_margin = self._free_margin(entry_price)
        # Fail closed: a NaN free margin (e.g. a NaN mark on an open position)
        # must not silently lift the leverage ceiling — ``nan <= 0`` is False
        # and ``min(qty, nan)`` returns ``qty``.
        if not math.isfinite(free_margin) or free_margin <= 0:
            return 0.0
        return free_margin * self._max_leverage / entry_price

    def _apply_notional_caps(
        self,
        shares: float,
        *,
        entry_price: float,
        exposure_limit: float | None,
        lot_step: float,
        cap_by_leverage: bool = True,
    ) -> float:
        """Apply the two notional ceilings to a sized quantity (#347).

        The per-position exposure cap stays a sizing rule on realized cash
        (D2). The leverage/margin check is MT5-style free margin: when it is
        the binding constraint the quantity is clamped down and floored to
        ``lot_step`` — to zero when no free margin remains, in which case
        the order is skipped (logged), never refused.
        ``cap_by_leverage=False`` is the
        fixed-risk ``cap_fixed_risk_by_leverage=false`` opt-out.
        """
        exposure_qty_cap = (
            self._sizing_equity * exposure_limit / float(entry_price)
            if exposure_limit is not None
            else math.inf
        )
        shares = min(shares, exposure_qty_cap)
        if cap_by_leverage:
            leverage_cap = self._free_margin_quantity_cap(float(entry_price))
            if lot_step > 0 and leverage_cap < shares:
                leverage_cap = math.floor(leverage_cap / lot_step + 1e-12) * lot_step
            shares = min(shares, leverage_cap)
        return max(shares, 0.0)

    def _fixed_risk_terms(
        self,
        *,
        entry_price: float,
        stop_loss_price: float | None,
        stop_distance: float | None,
    ) -> tuple[float, float, float, float] | None:
        """``(declared_risk, risk_distance, tick_value, tick_size)`` when the
        fixed-risk branch sizes this call, else ``None``.

        Mirrors the branch guard in ``_calculate_position_size`` exactly —
        ``fixed_risk_amount`` configured, a sizing stop present, and a
        positive declared amount and distance — so callers never attribute
        fixed-risk metadata (per-unit declared risk, exposure-clamp
        proportionality) to an order the pct path actually sized.
        """
        params = self.config.parameters
        fixed_risk_amount = params.get("fixed_risk_amount")
        if fixed_risk_amount is None:
            fixed_risk_amount = params.get("Fixed_Risk_Amount")
        if fixed_risk_amount is None or stop_loss_price is None:
            return None
        risk_amount = float(fixed_risk_amount)
        if not (math.isfinite(risk_amount) and risk_amount > 0):
            # A configured fixed-risk amount that is NaN/inf/non-positive is
            # a config bug, not "unset" — silently falling through to the
            # pct path (or sizing off inf) would hide it. Fail closed like
            # the C1 missing-sizing raise.
            raise ValueError(
                f"fixed_risk_amount must be a finite positive number, got "
                f"{fixed_risk_amount!r}"
            )
        # MT5 parity: an explicit stop distance (e.g. ATR-based) sizes from
        # it directly; otherwise the distance derives from |entry - stop|.
        if stop_distance is not None and stop_distance > 0:
            risk_distance = float(stop_distance)
        else:
            risk_distance = abs(float(entry_price) - float(stop_loss_price))
        if not (risk_amount > 0 and risk_distance > 0):
            return None
        tick_value = float(params.get("tick_value", 1.0) or 1.0)
        tick_size = float(params.get("tick_size", 1.0) or 1.0)
        return risk_amount, risk_distance, tick_value, tick_size

    def _record_fixed_risk_sizing(
        self,
        order: Order,
        *,
        entry_price: float,
        stop_loss_price: float | None,
        stop_distance: float | None,
    ) -> None:
        """Stamp the fixed-risk sizing facts onto ``order`` after a size > 0.

        ``_sizing_risk_per_unit`` is the declared risk per unit quantity in
        account currency (``risk_distance * tick_value / tick_size``) — the
        aggregate-exposure sum uses it instead of re-measuring
        ``|entry - stop|``, which drifts from the declared distance when the
        strategy sized on ``stop_distance``. ``_equity_proportional`` stays
        False only while the declared amount fits the exposure budget
        unclamped; a clamp-bound quantity is a function of the sizing equity
        and must be re-scaled by the shared-equity replay.
        """
        terms = self._fixed_risk_terms(
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            stop_distance=stop_distance,
        )
        if terms is None:
            order._equity_proportional = True
            order._sizing_risk_per_unit = None
            return
        declared, risk_distance, tick_value, tick_size = terms
        order._sizing_risk_per_unit = risk_distance * (tick_value / tick_size)
        sized = self._last_sizing
        # Clamped (``risk_amount`` below the declared amount) means the
        # quantity is a function of the sizing equity - proportional.
        order._equity_proportional = bool(
            sized is not None and sized.risk_amount < declared
        )

    def _calculate_position_size(
        self,
        *,
        entry_price: float,
        stop_loss_price: float | None,
        direction: str,
        take_profit_price: float | None = None,
        stop_distance: float | None = None,
        exclude_order: Order | None = None,
    ) -> float:
        self._last_sizing = None
        # #347: two different denominators now. The per-position exposure cap
        # stays a sizing rule on realized cash (D2); the leverage/margin
        # check is MT5-style and judges the order against FREE MARGIN
        # (equity − used margin), clamping to the broker maximum instead of
        # refusing. ``self._max_leverage`` is the broker maximum for the
        # account/asset; unknown → 1x.
        exposure_limit = self._max_position_exposure_pct
        lot_step = float(self.config.parameters.get("lot_step", 0.0) or 0.0)
        min_lot = float(self.config.parameters.get("min_lot", 0.0) or 0.0)
        max_lot = float(self.config.parameters.get("max_lot", 0.0) or 0.0)
        terms = self._fixed_risk_terms(
            entry_price=float(entry_price),
            stop_loss_price=stop_loss_price,
            stop_distance=stop_distance,
        )

        pending_positions = sum(
            order is not exclude_order for order in self.pending_orders
        )
        signal = TradeSignal(
            action="buy" if str(direction).upper() == "LONG" else "sell",
            symbol=self.symbol,
            entry_price=float(entry_price),
            stop_loss=stop_loss_price,
            take_profit=take_profit_price,
            timeframe=self.config.timeframe,
            metadata={
                # C2: pending orders count toward max_positions — a
                # pending order is committed intent, not free capacity.
                "current_positions": len(self.positions) + pending_positions,
                "pending_positions": pending_positions,
            },
        )

        if terms is not None:
            risk_amount, risk_distance, tick_value, tick_size = terms
            pip_factor = tick_value / tick_size
            # #279: the fixed-risk branch used to return before the risk
            # engine, skipping max_positions, the aggregate exposure
            # ceiling and input validation. It now sizes through
            # ``calculate_position_size`` via ``risk_amount_override`` on
            # ``_fixed_risk_engine`` (its own configurable exposure/position
            # limits); ``pip_value = tick_value / tick_size`` is the engine's
            # unit contract (account-currency P&L of one size unit per 1.0
            # price move). The accepted size is then recomputed in the MT5
            # ``(risk / distance) * (tick_size / tick_value)`` order - the
            # same value as the engine's ``risk / (distance * pip)`` up to
            # float reassociation, but bit-identical to the legacy/MT5
            # formula the parity protocol pins.
            sized_order = self._fixed_risk_engine.calculate_position_size(
                signal=signal,
                # F21 (D2): realized cash, same basis as the pct path.
                account_balance=float(self._sizing_equity),
                pip_value=pip_factor,
                # Open risk is measured in account currency, so the exposure
                # sum scales |entry - stop| * qty by the same pip factor.
                current_exposure=self._current_risk_exposure(
                    float(entry_price),
                    exclude_order=exclude_order,
                    pip=pip_factor,
                ),
                # The per-position notional ceiling stays in
                # ``_apply_notional_caps`` below: the engine's notional
                # unit (qty * price * pip — true contract notional for
                # lot-denominated symbols) differs from the native cap's
                # qty * price convention, so an engine-level ceiling here
                # would silently tighten MT5-parity sizing. ``inf`` is
                # the documented uncapped value.
                max_notional_per_trade=math.inf,
                risk_amount_override=risk_amount,
                stop_distance_override=(
                    float(stop_distance)
                    if stop_distance is not None and stop_distance > 0
                    else None
                ),
            )
            self._last_sizing = sized_order
            if (
                sized_order.decision is None
                or not sized_order.decision.accepted
                or sized_order.position_size <= 0
            ):
                # Engine rejection (max_positions / max_total_exposure)
                # or a non-positive size — the same zero the leverage
                # clamp produces; ``_last_sizing`` keeps the rejection
                # reason for the caller's skip log.
                return 0.0
            shares = (float(sized_order.risk_amount) / risk_distance) * (
                tick_size / tick_value
            )
            if lot_step > 0:
                shares = math.floor((shares / lot_step) + 1e-12) * lot_step
            if min_lot > 0:
                shares = max(shares, min_lot)
            if max_lot > 0:
                shares = min(shares, max_lot)
            return self._apply_notional_caps(
                shares,
                entry_price=float(entry_price),
                exposure_limit=exposure_limit,
                lot_step=lot_step,
                cap_by_leverage=bool(
                    self.config.parameters.get(
                        "cap_fixed_risk_by_leverage", True
                    )
                ),
            )

        if self._risk_enabled:
            sized_order = self._risk_engine.calculate_position_size(
                signal=signal,
                # F21 (D2): realized cash, NOT the mark-to-market
                # ``self.equity`` — an open position's paper PnL must not
                # change the size of the next entry.
                account_balance=float(self._sizing_equity),
                pip_value=1.0,
                current_exposure=self._current_risk_exposure(
                    float(entry_price), exclude_order=exclude_order
                ),
                # #347: only the realized-cash exposure cap goes into the
                # risk engine; the leverage/margin ceiling is applied
                # afterwards on free margin. With no exposure cap there is
                # no engine-level ceiling — math.inf is the documented
                # "uncapped" value (the post-cap is the real leverage bound).
                max_notional_per_trade=(
                    exposure_limit if exposure_limit is not None else math.inf
                ),
            )
            self._last_sizing = sized_order
            shares = float(sized_order.position_size)
        else:
            # C1 (owner decision): HARD ERROR without explicit sizing —
            # no silent full-equity default. Without config there is no trade.
            raise ValueError(
                "Risk sizing is not configured (risk_per_trade_pct unset and "
                "no fixed_risk_amount); refusing the full-equity default. "
                "Set risk_per_trade_pct or fixed_risk_amount explicitly."
            )

        # F21 (D2): the per-position notional cap stays denominated in
        # realized sizing cash; #347: the leverage/margin cap is free margin.
        return self._apply_notional_caps(
            shares,
            entry_price=float(entry_price),
            exposure_limit=exposure_limit,
            lot_step=lot_step,
        )

    @staticmethod
    def _to_iso(value: Any) -> str:
        return to_iso_utc(value)

    def _bar_spread(self, row: pd.Series) -> float:
        """Per-bar spread surcharge in price units (ask = open + spread).

        Opt-in via the ``spread_price_col`` strategy parameter naming a data
        column with the spread in PRICE units (e.g. converted from MT5
        spread points at ingest). Default off (0.0): all existing behavior
        is unchanged. Only LONG market fills pay the spread (buys lift the
        ask); SHORT fills and stop fills stay on the bid-side data, matching
        MT5 order semantics tick-for-tick (verified: platform long entries
        equaled MT5 entries exactly after adding open-side spread).
        """
        return resolve_bar_spread(row, self.config.parameters.get("spread_price_col"))

    def _create_order(self, row: pd.Series, i: int, direction: str) -> None:
        entry_price = float(row["close"])
        stop_loss, tp_levels = self._extract_levels(row)
        # A-M3: sizing-only protective levels (e.g. mean_reversion_rsi_bb
        # ``sizing_sl_price``) size the position but must NEVER become a live
        # intrabar stop — exit detection only honors real stop_loss/sl_price.
        sizing_only_stop: float | None = None
        for sizing_key in ("sizing_sl_price", "sizing_stop", "sizing_sl"):
            try:
                raw_sizing = row.get(sizing_key)
            except (AttributeError, KeyError):
                raw_sizing = None
            if raw_sizing is not None and pd.notna(raw_sizing):
                try:
                    sizing_only_stop = float(raw_sizing)
                except (TypeError, ValueError):
                    sizing_only_stop = None
                break
        stop_distance_raw = row.get("stop_distance")
        stop_distance = float(stop_distance_raw) if pd.notna(stop_distance_raw) else None
        take_profit = float(tp_levels[0]["price"]) if tp_levels else None
        size_from_stop = stop_loss if stop_loss is not None else sizing_only_stop
        exposure_stop = size_from_stop
        if exposure_stop is None and self._risk_enabled and self.config.allow_fallback_stop:
            direction_sign = 1.0 if direction == "LONG" else -1.0
            fallback_distance = (
                float(stop_distance)
                if stop_distance is not None and stop_distance > 0
                else entry_price * self._risk_engine.config.fallback_stop_pct
            )
            exposure_stop = entry_price - direction_sign * fallback_distance
        qty = self._calculate_position_size(
            entry_price=entry_price,
            stop_loss_price=size_from_stop,
            direction=direction,
            take_profit_price=take_profit,
            stop_distance=stop_distance,
        )
        if qty <= 0:
            # #347: a zero size is usually the free-margin leverage clamp
            # (no free margin remains); an engine rejection names its reason
            # (max_positions / max_total_exposure) — log the skip like the
            # fill-time skips instead of dropping the order silently.
            rejection = (
                self._last_sizing.decision.rejection_reason
                if self._last_sizing is not None
                and self._last_sizing.decision is not None
                else None
            )
            logger.warning(
                "Skipping %s %s order at %s: sized quantity %.6g <= 0%s",
                self.symbol,
                direction,
                self._to_iso(row["timestamp"]),
                qty,
                f" ({rejection})" if rejection else "",
            )
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
        order._sizing_stop_loss = exposure_stop
        order._sizing_only_stop = stop_loss is None and sizing_only_stop is not None
        # A-M4 (D5): fixed-amount sizing is absolute-$ (not equity
        # proportional) only while the declared amount fits the exposure
        # budget unclamped — a clamp-bound quantity is a function of the
        # sizing equity, so the shared-equity replay must re-scale it. The
        # declared per-unit risk rides along for the aggregate-exposure sum.
        self._record_fixed_risk_sizing(
            order,
            entry_price=entry_price,
            stop_loss_price=size_from_stop,
            stop_distance=stop_distance,
        )
        # F21 (D2): the realized-cash basis this quantity was computed from. It
        # travels with the order because the quantity that survives to the fill
        # is not always recomputed there — a MARKET order whose only stop is the
        # sizing fallback keeps its creation-time estimate (D4), so re-reading
        # the ledger at fill time could record a basis the quantity never saw.
        order._sizing_equity = float(self._sizing_equity)
        self.pending_orders.append(order)

    def _open_position(self, order: Order, fill_price: float, fill_time: str, i: int) -> None:
        # A-M4 (D5): record the equity basis at fill time so the adapter-level
        # shared-equity replay can rescale multi-symbol trades onto one
        # running portfolio equity (position size is linear in equity, so the
        # per-unit coefficient quantity/equity_at_entry replays exactly).
        position_meta = dict(getattr(order, "meta", None) or {})
        # The basis is the equity the order was SIZED from (realized cash,
        # F21/D2), not the mark-to-market reporting equity: the multi-symbol
        # replay recovers the per-unit coefficient quantity / equity_at_entry
        # from it, and ``replay_shared_equity`` accumulates realized PnL only.
        # It is the basis of the quantity that actually survived to this fill —
        # the fill-time ledger when the order was re-sized at fill, the
        # creation-time one when a fallback-sized MARKET order kept its
        # creation-time quantity (D4).
        position_meta["equity_at_entry"] = round(
            float(getattr(order, "_sizing_equity", self._sizing_equity)), 2
        )
        sizing_stop = getattr(order, "_sizing_stop_loss", None)
        if sizing_stop is not None:
            position_meta["sizing_stop_loss"] = float(sizing_stop)
        sizing_risk_per_unit = getattr(order, "_sizing_risk_per_unit", None)
        if sizing_risk_per_unit is not None:
            # The declared per-unit risk (account currency) the fixed-risk
            # protocol sized this position with; the aggregate-exposure sum
            # prefers it over re-measuring |entry - stop|.
            position_meta["sizing_risk_per_unit"] = float(sizing_risk_per_unit)
        # A-M4 (D5): transient proportionality flag from _create_order
        # (fixed-amount sizing must not be re-scaled by the shared replay).
        position_meta["equity_proportional_sizing"] = bool(
            getattr(order, "_equity_proportional", True)
        )
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
                meta=position_meta,
            )
        )
        # F21 (D2): the position's entry leg is owed from this fill, so the
        # spendable cash drops now — a run with no exit yet must still hold it
        # back from the next entry.
        self._refresh_sizing_equity()

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
                # Spread-aware market fill: LONGs lift the ask (open + spread),
                # SHORTs hit the bid (open). Matches MT5 fills tick-for-tick
                # when the spread column carries per-bar ask-bid in price units.
                if order.direction == "LONG":
                    fill_price = bar_open + self._bar_spread(row)
                else:
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
                if order.stop_loss is None and getattr(order, "_sizing_stop_loss", None) is None:
                    direction_sign = 1.0 if order.direction == "LONG" else -1.0
                    fallback_distance = (
                        float(order.stop_distance)
                        if order.stop_distance is not None and order.stop_distance > 0
                        else float(fill_price) * self._risk_engine.config.fallback_stop_pct
                    )
                    order._sizing_stop_loss = (
                        float(fill_price) - direction_sign * fallback_distance
                    )
                if (
                    order.order_type == OrderType.MARKET
                    and order.stop_distance is not None
                    and order.stop_distance > 0
                    and not getattr(order, "_sizing_only_stop", False)
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
                    # A-H5 parity: forward the strategy's explicit stop_distance
                    # (e.g. ATR-based) so trigger fills size from the same
                    # distance as MARKET fills instead of |fill - stop|.
                    order.quantity = self._calculate_position_size(
                        entry_price=float(fill_price),
                        stop_loss_price=order.stop_loss,
                        direction=order.direction,
                        take_profit_price=(
                            float(order.take_profit_levels[0]["price"])
                            if order.take_profit_levels
                            else None
                        ),
                        stop_distance=order.stop_distance,
                        exclude_order=order,
                    )
                    # F21 (D2): the quantity was just recomputed from the
                    # fill-time ledger, so that is now the basis to record.
                    order._sizing_equity = float(self._sizing_equity)
                    if order.quantity <= 0:
                        # Usually the free-margin leverage clamp (no free
                        # margin remains) — log like the creation skip
                        # instead of dropping the fill silently.
                        rejection = (
                            self._last_sizing.decision.rejection_reason
                            if self._last_sizing is not None
                            and self._last_sizing.decision is not None
                            else None
                        )
                        logger.warning(
                            "Skipping %s %s order at %s: fill-time sized "
                            "quantity %.6g <= 0%s",
                            order.symbol,
                            order.direction,
                            fill_time,
                            order.quantity,
                            f" ({rejection})" if rejection else "",
                        )
                        continue
                    self._record_fixed_risk_sizing(
                        order,
                        entry_price=float(fill_price),
                        stop_loss_price=order.stop_loss,
                        stop_distance=order.stop_distance,
                    )
                if order.order_type == OrderType.MARKET:
                    sizing_stop = order.stop_loss
                    if sizing_stop is None and getattr(order, "_sizing_only_stop", False):
                        sizing_stop = getattr(order, "_sizing_stop_loss", None)
                    if sizing_stop is not None:
                        try:
                            sizing_stop_value = float(sizing_stop)
                        except (TypeError, ValueError) as exc:
                            logger.warning(
                                "Skipping market order with invalid sizing stop at fill: %s",
                                exc,
                            )
                            continue
                        wrong_side = (
                            order.direction == "LONG"
                            and sizing_stop_value >= float(fill_price)
                        ) or (
                            order.direction != "LONG"
                            and sizing_stop_value <= float(fill_price)
                        )
                        if (
                            not math.isfinite(sizing_stop_value)
                            or math.isclose(
                                sizing_stop_value, float(fill_price), abs_tol=1e-12
                            )
                            or wrong_side
                        ):
                            logger.warning(
                                "Skipping market order with invalid sizing stop at fill"
                            )
                            continue
                        take_profit = (
                            float(order.take_profit_levels[0]["price"])
                            if order.take_profit_levels
                            else None
                        )
                        order.quantity = self._calculate_position_size(
                            entry_price=float(fill_price),
                            stop_loss_price=sizing_stop_value,
                            direction=order.direction,
                            take_profit_price=take_profit,
                            stop_distance=order.stop_distance,
                            exclude_order=order,
                        )
                        # F21 (D2): same as the LIMIT/STOP re-size above — the
                        # surviving quantity is the fill-time one, so the
                        # fill-time basis is the one to record.
                        order._sizing_equity = float(self._sizing_equity)
                        if order.quantity <= 0:
                            # Usually the free-margin leverage clamp (no free
                            # margin remains) — log like the creation skip
                            # instead of dropping the fill silently.
                            rejection = (
                                self._last_sizing.decision.rejection_reason
                                if self._last_sizing is not None
                                and self._last_sizing.decision is not None
                                else None
                            )
                            logger.warning(
                                "Skipping %s %s order at %s: fill-time sized "
                                "quantity %.6g <= 0%s",
                                order.symbol,
                                order.direction,
                                fill_time,
                                order.quantity,
                                f" ({rejection})" if rejection else "",
                            )
                            continue
                        self._record_fixed_risk_sizing(
                            order,
                            entry_price=float(fill_price),
                            stop_loss_price=sizing_stop_value,
                            stop_distance=order.stop_distance,
                        )
                # A-C2 inverted-stop guard (mirrors vectorbt_adapter): a stop
                # on the wrong side of the entry (LONG stop >= entry, SHORT
                # stop <= entry) would produce an instantly-triggered,
                # sign-flipped fantasy exit. Skip the trade loudly instead of
                # compounding garbage into equity. Checked against the final
                # fill price, like vectorbt checks against its final entry.
                if order.stop_loss is not None:
                    inverted = (
                        order.direction == "LONG"
                        and float(order.stop_loss) >= float(fill_price)
                    ) or (
                        order.direction != "LONG"
                        and float(order.stop_loss) <= float(fill_price)
                    )
                    if inverted:
                        logger.warning(
                            "Skipping %s %s order created at %s: %s stop %.6f is on "
                            "the wrong side of fill %.6f",
                            order.symbol,
                            order.order_type.value,
                            order.created_time,
                            order.direction,
                            float(order.stop_loss),
                            float(fill_price),
                        )
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
        # #84: exit legs accumulate gross; ``_finalize_position`` backs the
        # fees out, so this running realized figure is net (F21/D2).
        self._realized_pnl += pnl
        # F21 (D2): cash becomes spendable the moment it is realized, net of
        # the round-trip fees the still-open position will owe.
        self._refresh_sizing_equity()

    def _finalize_position(self, position: Position, exit_index: int) -> None:
        if position.remaining_qty > 1e-9:
            return
        if not position.exits:
            return

        self._trade_counter += 1
        pnl = sum(float(item.pnl or 0.0) for item in position.exits)
        # #84: explicit pluggable fee model — missing fee/slippage config is
        # a hard error, never silent gross. Partial exits are priced at the
        # quantity-weighted average exit price (exact for the notional
        # model, one call for any model).
        fee_model = resolve_fee_model(self.config.parameters)
        total_exit_qty = sum(float(item.quantity or 0.0) for item in position.exits)
        exit_notional = sum(
            float(item.price or 0.0) * float(item.quantity or 0.0)
            for item in position.exits
        )
        avg_exit_price = exit_notional / total_exit_qty if total_exit_qty > 0 else 0.0
        fees = fee_model.fee_for_trade(
            float(position.entry_price), avg_exit_price, float(position.quantity)
        )
        net_pnl = pnl - fees
        # #84: exit legs accumulated gross into _realized_pnl — back the
        # costs out here so the equity path is net.
        self._realized_pnl -= fees
        # F21 (D2): the spendable sizing cash follows the NET realized figure
        # (the fee model is part of the realized path), so it is refreshed
        # here as well as at every partial close.
        self._refresh_sizing_equity()
        take_profit = None
        if position.take_profit_levels:
            first_level = position.take_profit_levels[0]
            if first_level.get("price") is not None:
                take_profit = float(first_level["price"])

        sizing_stop = position.meta.get("sizing_stop_loss")
        risk_reference = position.stop_loss or sizing_stop or position.entry_price * 0.98
        risk_per_share = abs(position.entry_price - float(risk_reference))
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
            stop_loss=position.stop_loss,
            take_profit=take_profit,
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

        # D4 (Phase 3, A-M1 — implemented): unified Fill-zum-Trigger-Close
        # for signal exits — the exit fills at the trigger bar's close by
        # default, exactly like the vectorbt adapter (which keeps its exit
        # leg unshifted). signal_exit_fill="open" stays as an MT5-parity
        # opt-in (first tick inside the exit minute ~= bar open).
        # Closing a SHORT buys the ask (open + spread); closing a LONG
        # sells the bid. Verified tick-for-tick: MT5 long time-exits ==
        # 22:55 bar open, short time-exits == 22:55 bar open + spread.
        fill_at_open = (
            str(getattr(self.config, "signal_exit_fill", "close")).lower()
            == "open"
        )
        spread = self._bar_spread(row) if fill_at_open else 0.0
        base_exit = float(row["open"]) if fill_at_open else float(row["close"])
        time_iso = self._to_iso(row["timestamp"])
        next_positions: list[Position] = []

        for position in self.positions:
            exit_price = base_exit + (spread if position.direction == "SHORT" else 0.0)
            self._close_partial(
                position,
                price=exit_price,
                qty=position.remaining_qty,
                time_iso=time_iso,
                reason=reason,
            )
            self._finalize_position(position, i)
            if position.remaining_qty > 1e-9:
                next_positions.append(position)

        self.positions = next_positions

    def _flag_same_bar_fallback(
        self,
        position: Position,
        bar_time: pd.Timestamp,
        next_time: pd.Timestamp | None,
    ) -> None:
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
            if position.direction == "SHORT":
                sub_ask_high, sub_ask_low = self._ask_bar(row)
                if sub_ask_high is not None:
                    sub_high = sub_ask_high
                if sub_ask_low is not None:
                    sub_low = sub_ask_low

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

    def _ask_bar(self, row: pd.Series) -> tuple[float | None, float | None]:
        """Per-bar ask-side high/low in price units (opt-in).

        MT5 triggers SHORT stops/TPs on the ASK, not the bid: a short stop
        fills when ask-high >= stop. Enable via the ``ask_ohlc`` strategy
        parameter (default False — legacy bid-only behavior unchanged).
        Reads explicit ``ask_high``/``ask_low`` data columns when present
        (exact, e.g. Dukascopy bid/ask M1); otherwise falls back to
        bid high/low + ``spread_price_col`` spread when configured.
        Returns (None, None) when neither is available.
        """
        if not bool(self.config.parameters.get("ask_ohlc", False)):
            return None, None
        return resolve_ask_high_low(row, self._bar_spread(row))

    def _record_bar_excursion(
        self,
        position: Position,
        row: pd.Series,
        *,
        favorable_cap: float | None = None,
        adverse_cap: float | None = None,
    ) -> None:
        """Record this bar's maximum favourable and adverse excursion.

        `favorable_cap` and `adverse_cap` bound one side each. They are supplied
        only for stop and take-profit exits, because those fills are levels
        *inside* the bar: movement past the level never belonged to the position, and
        crediting it overstates MFE on a take-profit and MAE on a stop - on exactly
        the trades where the exit determined the outcome.

        **Capping both sides at once is worse than not capping at all**, and the
        first version of this fix did exactly that by clamping the bar symmetrically
        to the fill. Measured on a LONG whose bar ran 90 to 115 with entry near 100:

            take-profit at 110: MFE correctly capped at +5000, but MAE reported
                -500 when the bar's low alone is -5000
            stop at 95:         MAE correctly capped at -1000, but MFE reported
                +200 when the bar's high alone is +3000

        Every stop-out then reports a near-zero MFE and every take-profit a near-zero
        MAE, so each trade looks like a monotone excursion and the two metrics stop
        carrying independent information - the bias traded places instead of going
        away. The bar's other side is real: under the engine's own intrabar model
        the position traded there before the exit level was reached, and discarding
        that is the same error pointed the other way.

        Two caps can be in force at once, and both are correct: a bar can fill
        several take-profit levels and then stop out the remainder. The caller
        passes the *furthest level it actually reached* as the favourable cap and
        the stop as the adverse cap; anything past either never belonged to the
        position.

        A signal or time exit is deliberately not clamped at all. It fills at the
        bar's close under Fill-zum-Trigger-Close, so the position existed for the
        whole bar and the bar's range genuinely is its excursion;
        `test_trade_engine_mae_includes_signal_exit_bar_excursion` pins that, and it
        is a decision rather than an oversight. The cap follows the engine's own exit
        model: the price passed in is the same fill the close uses, spread
        convention included, so excursion and exit cannot disagree.
        """
        qty = float(position.remaining_qty)
        if qty <= 0:
            return
        if position.direction == "LONG":
            high = float(row["high"])
            low = float(row["low"])
            if favorable_cap is not None:
                high = min(high, favorable_cap)
            if adverse_cap is not None:
                low = max(low, adverse_cap)
            favorable = (high - position.entry_price) * qty
            adverse = (low - position.entry_price) * qty
        else:
            ask_high, ask_low = self._ask_bar(row)
            ref_high = ask_high if ask_high is not None else float(row["high"])
            ref_low = ask_low if ask_low is not None else float(row["low"])
            if favorable_cap is not None:
                ref_low = max(ref_low, favorable_cap)
            if adverse_cap is not None:
                ref_high = min(ref_high, adverse_cap)
            favorable = (position.entry_price - ref_low) * qty
            adverse = (position.entry_price - ref_high) * qty
        position.mfe = max(position.mfe, favorable)
        position.mae = min(position.mae, adverse)

    def _check_stops(
        self,
        row: pd.Series,
        i: int,
        next_time: pd.Timestamp | None,
        *,
        only_entry_index: int | None = None,
    ) -> None:
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
        ask_high, ask_low = self._ask_bar(row)
        for position in self.positions:
            if only_entry_index is not None and position.entry_index != only_entry_index:
                remaining_positions.append(position)
                continue
            if position.direction == "LONG":
                stop_hit = position.stop_loss is not None and bar_low <= float(position.stop_loss)
            else:
                short_high = ask_high if ask_high is not None else bar_high
                stop_hit = position.stop_loss is not None and short_high >= float(
                    position.stop_loss
                )

            pending_tp = [
                level for level in position.take_profit_levels if not bool(level.get("filled"))
            ]
            if position.direction == "LONG":
                tp_hit = any(bar_high >= float(level["price"]) for level in pending_tp)
            else:
                short_low = ask_low if ask_low is not None else bar_low
                tp_hit = any(short_low <= float(level["price"]) for level in pending_tp)

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

            # The excursion for this bar is recorded once the exit event is known, so
            # a stop or take-profit exit is credited only as far as it actually got.
            # A position that stays open (first_event is None) keeps full-bar
            # recording.
            #
            # One cap per side, because a bar can do both: fill several take-profit
            # levels and then stop out whatever is left. The favourable cap is the
            # *furthest level actually reached*, not the first pending one - a
            # two-level ladder at 105 and 115 that fills both is credited with 115,
            # and using the first level understated MFE five-fold on exactly the
            # trades the cap exists to describe correctly. The adverse cap is the
            # stop, but only when a stop actually fires for the remainder.
            excursion_favorable_cap: float | None = None
            excursion_adverse_cap: float | None = None
            if first_event == "stop" and position.stop_loss is not None:
                excursion_adverse_cap = float(position.stop_loss)
                if position.direction != "LONG":
                    excursion_adverse_cap += self._bar_spread(row)
            elif first_event == "tp":
                spread = self._bar_spread(row) if position.direction != "LONG" else 0.0
                reached = [
                    float(level["price"])
                    for level in pending_tp
                    if (
                        bar_high >= float(level["price"])
                        if position.direction == "LONG"
                        else bar_low <= float(level["price"])
                    )
                ]
                if reached:
                    furthest = (
                        max(reached) if position.direction == "LONG" else min(reached)
                    )
                    excursion_favorable_cap = furthest + spread
                # A ladder that does not close the whole position leaves a
                # remainder, and the loop below stops it out. That part of the
                # position exited at the stop, so the adverse side is capped too.
                # Gated on a stop actually existing, unlike the favourable cap -
                # gating the whole block on one made a take-profit with no stop
                # configured record the full bar.
                if stop_hit and position.stop_loss is not None:
                    excursion_adverse_cap = float(position.stop_loss)
                    if position.direction != "LONG":
                        excursion_adverse_cap += self._bar_spread(row)
            self._record_bar_excursion(
                position,
                row,
                favorable_cap=excursion_favorable_cap,
                adverse_cap=excursion_adverse_cap,
            )

            if first_event == "stop" and position.stop_loss is not None:
                # D4 (Phase 3, A-M1 exit convention): the unified convention
                # is Fill-zum-Trigger-Close for SIGNAL exits; stop/TP legs
                # keep LEVEL fills (the stop distance is the sizing risk
                # unit — re-pricing stops to the close would silently void
                # the risk model). A-H7: SHORT stop fills buy the ask, so
                # they carry the spread like entries and signal exits do;
                # LONG stops sell the bid (level as-is).
                stop_fill = float(position.stop_loss)
                if position.direction != "LONG":
                    stop_fill += self._bar_spread(row)
                self._close_partial(
                    position,
                    price=stop_fill,
                    qty=position.remaining_qty,
                    time_iso=time_iso,
                    reason="stop_loss",
                )
            elif first_event == "tp":
                # A-H7: SHORT TP fills buy the ask (level + spread), LONG TP
                # fills sell the bid (level as-is) — same side convention as
                # stop fills, entries and signal exits.
                tp_spread = self._bar_spread(row) if position.direction != "LONG" else 0.0
                for level in pending_tp:
                    trigger_price = float(level["price"])
                    if position.direction == "LONG":
                        reached = bar_high >= trigger_price
                    else:
                        reached = bar_low <= trigger_price
                    if not reached:
                        continue

                    fraction = float(level.get("fraction", 0.0))
                    qty = position.quantity * fraction
                    if level is pending_tp[-1]:
                        qty = position.remaining_qty

                    self._close_partial(
                        position,
                        price=trigger_price + tp_spread,
                        qty=qty,
                        time_iso=time_iso,
                        reason=str(level.get("name") or "take_profit"),
                    )
                    level["filled"] = True

                    if position.remaining_qty <= 1e-9:
                        break

                if position.remaining_qty > 1e-9 and stop_hit and position.stop_loss is not None:
                    trailing_fill = float(position.stop_loss)
                    if position.direction != "LONG":
                        trailing_fill += self._bar_spread(row)
                    self._close_partial(
                        position,
                        price=trailing_fill,
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
                # A-L5: SHORT exits trigger on the ASK when ask_ohlc is
                # enabled — MAE/MFE must be measured on the same side the
                # position would actually trade, not on bid high/low.
                ask_high, ask_low = self._ask_bar(row)
                ref_high = ask_high if ask_high is not None else float(row["high"])
                ref_low = ask_low if ask_low is not None else float(row["low"])
                unrealized += (position.entry_price - close_price) * position.remaining_qty
                high_pnl = (position.entry_price - ref_low) * position.remaining_qty
                low_pnl = (position.entry_price - ref_high) * position.remaining_qty

            position.mfe = max(position.mfe, high_pnl)
            position.mae = min(position.mae, low_pnl)

        self.equity = self.config.initial_capital + self._realized_pnl + unrealized
        # NOTE (Cleanup 16-D1, F21 / decision record D2): ``self.equity`` is the
        # mark-to-market REPORTING equity (it feeds the equity curve); it
        # deliberately does NOT feed position sizing. Sizing draws on
        # ``self._sizing_equity`` (realized cash only), so an unrealized
        # drawdown cannot deflate new sizes and unrealized profit cannot
        # compound into them either.
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

        # Indicator warmup gate. When
        # config.warmup_start is set, bars before start_date feed the
        # indicators only — no orders, no exits, no equity points.
        # A-M11: a warmup window without a parseable start_date must fail
        # loudly — silently trading the warmup bars (trade_from=None) is
        # worse than refusing the run.
        trade_from = None
        if str(getattr(self.config, "warmup_start", "") or "").strip():
            trade_from = pd.to_datetime(
                getattr(self.config, "start_date", ""), utc=True, errors="coerce"
            )
            if pd.isna(trade_from):
                raise ValueError(
                    "warmup_start is set but start_date "
                    f"({getattr(self.config, 'start_date', '')!r}) is empty or "
                    "unparseable — the warmup gate cannot derive trade_from"
                )

        total_bars = len(frame)
        progress_span = max(0.0, self._progress_end - self._progress_start)
        # Avoid noisy per-bar callbacks: at most once per 2% and at least every 1000 bars.
        by_percent = max(1, total_bars // 50)
        step_bars = max(1000, by_percent)
        next_emit = 0

        for i in range(total_bars):
            row = frame.iloc[i]
            if trade_from is not None and pd.to_datetime(
                row["timestamp"], utc=True
            ) < trade_from:
                continue  # warmup bar: indicators only, no trading
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
            # A-H1: the fill bar itself can trade through the fresh stop/TP.
            # Positions opened above fill at this bar's open and were never
            # stop-checked on it (the pre-fill _check_stops only saw older
            # positions). Re-run the check scoped to just-opened positions so
            # fill-bar breakouts exit on the fill bar instead of leaking a
            # full extra bar of phantom exposure.
            # D4 (A-M1): this scoped re-check reuses _check_stops detection
            # + pricing, so fill-bar exits follow the same convention with no
            # special case here.
            self._check_stops(row, i, next_time, only_entry_index=i)

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
