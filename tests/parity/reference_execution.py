"""INDEPENDENT bar-by-bar reference simulator for the native backtest engine.

This module is the parity reference. It deliberately imports **nothing** from
``runner/``, ``risk/`` or ``shared/`` — no platform code at all — and it is
written as a plain event loop over plain Python lists (no vectorised pandas
operations), so that an agreement between this simulator and the platform is
evidence about the platform and not a shared implementation.

Contract implemented here (see ``docs/cross-engine-backtest-contract.md`` and
``docs/sizing-semantics.md``; every line below was transcribed from those
documents and re-verified against the platform by the parity tests that use
this module):

Fills
-----
* A signal produced on bar *N* becomes a MARKET order that fills on bar *N+1*
  at bar *N+1*'s **open** (``next_bar_open``). A signal on the last bar never
  fills.
* Protective exits (stop-loss / take-profit) are evaluated per bar on the
  bar's low/high; a LONG stop triggers on ``low <= stop`` and fills **at the
  stop level itself** (a gap through the stop fills at the level, not at the
  open), mirrored for SHORT. The same applies to take-profit levels.
* Same-bar stop AND take-profit with no intrabar data resolves
  **stop-first**.
* A position is stop-checked on its own fill bar (``A-H1``): a fill bar that
  trades through the fresh level exits on the fill bar.
* A signal exit fills at the **trigger bar's close** and closes every open
  position (reason ``signal``). Open positions at the end of the frame are
  closed at the last bar's close (reason ``end_of_data``).
* An entry whose stop sits on the wrong side of the fill (LONG stop >= fill,
  SHORT stop <= fill) is skipped, as is an order that sizes to zero or more
  positions than the engine's concurrent-position policy allows.

Sizing (risk-based)
-------------------
* ``risk_per_trade_pct`` is the maximum loss per trade as a fraction of
  **realized cash** (initial capital + realized PnL). Unrealized
  mark-to-market PnL of open positions never enters sizing (``F21``).
* ``qty = equity * risk / |fill - stop|``, where ``risk`` is
  ``min(risk_per_trade_pct, max(0, max_total_exposure - current_exposure))``.
  ``current_exposure`` is the aggregate open risk across all open positions and
  pending orders, as a fraction of the same realized equity, each measured as
  ``|fill_or_reference - stop| * qty / equity``.
* The quantity is capped by the position notional limit
  ``equity * min(max_position_exposure_pct, max_leverage) / fill``.
* A stop on the wrong side of the fill skips the trade.

Costs
-----
* ``fees`` are declared explicitly. With ``fee_rate``/``fee_flat`` the
  round-trip cost is ``(entry + exit) * qty * fee_rate + fee_flat``; a
  zero-fee declaration prices 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

#: The native engine's concurrent-position policy (``TRADE_ENGINE_MAX_POSITIONS``).
ENGINE_MAX_POSITIONS = 5
#: The risk engine's aggregate open-risk ceiling (``RiskConfig.max_total_exposure``).
ENGINE_MAX_TOTAL_EXPOSURE = 0.05
#: The documented no-stop fallback distance (``RiskConfig.fallback_stop_pct``).
ENGINE_FALLBACK_STOP_PCT = 0.02

NAN = float("nan")


# ───────────────────────────── inputs ────────────────────────────────────────


@dataclass(frozen=True)
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float


@dataclass
class Signals:
    """Per-bar strategy output, in the platform's signal contract."""

    entries: list[bool]
    exits: list[bool]
    direction: list[str]  # "LONG" / "SHORT"
    stop_loss: list[float | None]
    take_profit: list[float | None]
    #: optional sizing-only stop: sizes the order, never a live protective level
    sizing_stop: list[float | None] = field(default_factory=list)


@dataclass(frozen=True)
class RefConfig:
    capital: float = 100_000.0
    #: risk_per_trade_pct (fraction of realized equity per trade)
    risk_pct: float = 0.01
    #: min(max_position_exposure_pct, max_leverage); None = no notional cap
    notional_cap: float | None = 1.0
    max_positions: int = ENGINE_MAX_POSITIONS
    max_total_exposure: float = ENGINE_MAX_TOTAL_EXPOSURE
    fee_rate: float = 0.0
    fee_flat: float = 0.0
    #: when True an entry without an explicit stop is sized from a 2%-of-fill
    #: fallback stop re-derived at the fill (the platform's current policy for
    #: stopless MARKET orders); the stop level is then that fallback level.
    stopless_fallback_at_fill: bool = True


@dataclass(frozen=True)
class RefLeg:
    time: str
    price: float
    quantity: float
    pnl: float
    reason: str


@dataclass
class RefTrade:
    entry_time: str
    direction: str
    entry_price: float
    quantity: float
    legs: list[RefLeg]
    pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    exit_reason: str = ""
    mae: float = 0.0
    mfe: float = 0.0
    entry_index: int = 0

    @property
    def exit_time(self) -> str:
        return self.legs[-1].time if self.legs else self.entry_time

    @property
    def exit_price(self) -> float | None:
        if not self.legs:
            return None
        notional = sum(leg.price * leg.quantity for leg in self.legs)
        qty = sum(leg.quantity for leg in self.legs)
        return notional / qty if qty else None


@dataclass
class _Position:
    direction: str
    entry_index: int
    entry_price: float
    quantity: float
    remaining: float
    stop: float | None
    take_profit: float | None
    legs: list[RefLeg] = field(default_factory=list)
    mae: float = 0.0
    mfe: float = 0.0

    @property
    def risk_distance(self) -> float:
        stop = self.stop if self.stop is not None else _fallback_stop(self.entry_price)
        return abs(float(self.entry_price) - float(stop))


@dataclass
class _Order:
    created_index: int
    direction: str
    stop: float | None
    take_profit: float | None
    sizing_stop: float | None
    quantity: float


def _fallback_stop(entry_price: float) -> float:
    return float(entry_price) * (1.0 - ENGINE_FALLBACK_STOP_PCT) if entry_price else entry_price


def _round(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


# ───────────────────────────── the event loop ───────────────────────────────


class ReferenceSimulator:
    """Bar-by-bar reimplementation of the documented native-engine contract."""

    def __init__(self, config: RefConfig):
        self.cfg = config
        self.realized = config.capital
        self.positions: list[_Position] = []
        self.pending: list[_Order] = []
        self.trades: list[RefTrade] = []
        self.skipped: list[str] = []
        self.curve: list[float] = []
        self.realized_curve: list[float] = []
        self._bars: list[Bar] = []

    # -- sizing --------------------------------------------------------------

    def _open_exposure(self, reference_price: float, exclude_pending: _Order | None) -> float:
        """Aggregate open risk as a fraction of realized equity."""
        if self.realized <= 0:
            return 0.0
        exposure = 0.0
        for position in self.positions:
            stop = position.stop if position.stop is not None else _fallback_stop(position.entry_price)
            distance = abs(float(position.entry_price) - float(stop))
            exposure += distance * float(position.remaining) / float(self.realized)
        for order in self.pending:
            if order is exclude_pending:
                continue
            stop = order.stop if order.stop is not None else order.sizing_stop
            if stop is None:
                # a stopless pending order: the platform derives a fallback
                # distance from the order's own reference price
                distance = float(reference_price) * ENGINE_FALLBACK_STOP_PCT
                if distance <= 0:
                    distance = ENGINE_FALLBACK_STOP_PCT
                stop = reference_price - distance
            distance = abs(float(reference_price) - float(stop))
            exposure += distance * float(order.quantity) / float(self.realized)
        return max(0.0, exposure)

    def _risk_fraction(self, reference_price: float, exclude_pending: _Order | None) -> float:
        exposure = self._open_exposure(reference_price, exclude_pending)
        remaining = max(0.0, self.cfg.max_total_exposure - exposure)
        return min(self.cfg.risk_pct, remaining)

    def size(self, entry_price: float, stop: float | None, exclude_pending: _Order | None) -> float:
        """Risk-based quantity, capped by the position notional limit."""
        positions_and_pending = len(self.positions) + len(self.pending)
        if positions_and_pending >= self.cfg.max_positions:
            return 0.0
        risk = self._risk_fraction(entry_price, exclude_pending)
        if risk <= 0:
            return 0.0
        if stop is None:
            stop = entry_price * (1.0 - ENGINE_FALLBACK_STOP_PCT)
        distance = abs(float(entry_price) - float(stop))
        if distance <= 0 or not math.isfinite(distance):
            raise ValueError("entry price and stop must differ for risk sizing")
        qty = self.realized * risk / distance
        if self.cfg.notional_cap is not None:
            qty = min(qty, self.realized * self.cfg.notional_cap / float(entry_price))
        return max(0.0, qty)

    # -- order lifecycle -----------------------------------------------------

    def _create_order(self, i: int, sig: Signals) -> None:
        direction = sig.direction[i]
        stop = sig.stop_loss[i]
        sizing_stop = stop
        if stop is None and i < len(sig.sizing_stop):
            sizing_stop = sig.sizing_stop[i]
        close = float(self._bars[i].close)
        qty = self.size(close, sizing_stop, exclude_pending=None)
        if qty <= 0:
            self.skipped.append(f"bar {i}: sized to zero at creation")
            return
        self.pending.append(
            _Order(
                created_index=i,
                direction=direction,
                stop=stop,
                take_profit=sig.take_profit[i],
                sizing_stop=sizing_stop,
                quantity=qty,
            )
        )

    def _fill_pending(self, i: int) -> None:
        bar = self._bars[i]
        fill = float(bar.open)
        if not math.isfinite(fill) or fill <= 0:
            raise ValueError(f"bar {i}: fill price {fill!r} is not finite and > 0")
        remaining: list[_Order] = []
        for order in self.pending:
            if order.created_index >= i:
                remaining.append(order)
                continue
            stop = order.stop
            if stop is None and self.cfg.stopless_fallback_at_fill:
                stop = fill * (1.0 - ENGINE_FALLBACK_STOP_PCT)
            wrong_side = stop is not None and (
                (order.direction == "LONG" and stop >= fill)
                or (order.direction != "LONG" and stop <= fill)
            )
            if wrong_side:
                self.skipped.append(
                    f"bar {i}: {order.direction} stop {stop:.6f} on the wrong side of fill {fill:.6f}"
                )
                continue
            qty = self.size(fill, stop, exclude_pending=order)
            if qty <= 0:
                self.skipped.append(f"bar {i}: sized to zero at fill")
                continue
            self.positions.append(
                _Position(
                    direction=order.direction,
                    entry_index=i,
                    entry_price=fill,
                    quantity=qty,
                    remaining=qty,
                    stop=stop,
                    take_profit=order.take_profit,
                )
            )
        self.pending = remaining

    # -- exits ---------------------------------------------------------------

    def _protective_exit(self, i: int, position: _Position, *, only_new: bool) -> None:
        bar = self._bars[i]
        if only_new and position.entry_index != i:
            return
        if position.direction == "LONG":
            stop_hit = position.stop is not None and bar.low <= position.stop
        else:
            stop_hit = position.stop is not None and bar.high >= position.stop
        tp_hit = (
            position.take_profit is not None
            and (
                bar.high >= position.take_profit
                if position.direction == "LONG"
                else bar.low <= position.take_profit
            )
        )
        # stop-first when the bar touches both levels and no intrabar data exists
        if stop_hit:
            self._record_excursion(bar, position, adverse_cap=position.stop)
            self._close_partial(position, i, position.stop, position.remaining, "stop_loss")
        elif tp_hit:
            self._record_excursion(bar, position, favorable_cap=position.take_profit)
            self._close_partial(
                position, i, position.take_profit, position.remaining, "take_profit"
            )
        else:
            self._record_excursion(bar, position)

    def _record_excursion(
        self,
        bar: Bar,
        position: _Position,
        *,
        favorable_cap: float | None = None,
        adverse_cap: float | None = None,
    ) -> None:
        qty = float(position.remaining)
        if qty <= 0:
            return
        high, low = float(bar.high), float(bar.low)
        if position.direction == "LONG":
            if favorable_cap is not None:
                high = min(high, favorable_cap)
            if adverse_cap is not None:
                low = max(low, adverse_cap)
            favorable = (high - position.entry_price) * qty
            adverse = (low - position.entry_price) * qty
        else:
            if favorable_cap is not None:
                low = max(low, favorable_cap)
            if adverse_cap is not None:
                high = min(high, adverse_cap)
            favorable = (position.entry_price - low) * qty
            adverse = (position.entry_price - high) * qty
        position.mfe = max(position.mfe, favorable)
        position.mae = min(position.mae, adverse)

    def _close_partial(
        self, position: _Position, i: int, price: float, qty: float, reason: str
    ) -> None:
        qty = max(0.0, min(qty, float(position.remaining)))
        if qty <= 0:
            return
        if position.direction == "LONG":
            pnl = (price - position.entry_price) * qty
        else:
            pnl = (position.entry_price - price) * qty
        position.legs.append(RefLeg(self._bars[i].time, float(price), float(qty), float(pnl), reason))
        position.remaining -= qty

    def _finalize(self, position: _Position) -> None:
        if position.remaining > 1e-9 or not position.legs:
            return
        pnl = sum(leg.pnl for leg in position.legs)
        # the round-trip fee is priced on the quantity-weighted average exit
        total_qty = sum(leg.quantity for leg in position.legs)
        notional = sum(leg.price * leg.quantity for leg in position.legs)
        avg_exit = notional / total_qty if total_qty > 0 else 0.0
        fees = (abs(position.entry_price) + abs(avg_exit)) * total_qty * self.cfg.fee_rate + (
            self.cfg.fee_flat
        )
        net = pnl - fees
        self.realized += net
        self.trades.append(
            RefTrade(
                entry_time=self._bars[position.entry_index].time,
                direction=position.direction,
                entry_price=position.entry_price,
                quantity=position.quantity,
                legs=list(position.legs),
                pnl=float(pnl),
                fees=float(fees),
                net_pnl=float(net),
                exit_reason=position.legs[-1].reason,
                mae=position.mae,
                mfe=position.mfe,
                entry_index=position.entry_index,
            )
        )

    def _signal_exit(self, i: int) -> None:
        bar = self._bars[i]
        for position in list(self.positions):
            self._close_partial(position, i, float(bar.close), position.remaining, "signal")
            self._finalize(position)
        self.positions = [p for p in self.positions if p.remaining > 1e-9]

    # -- main loop ------------------------------------------------------------

    def _reported_equity(self, bar: Bar) -> float:
        """Realized cash plus unrealized mark-to-market of open positions."""
        unrealized = 0.0
        for position in self.positions:
            if position.direction == "LONG":
                unrealized += (bar.close - position.entry_price) * position.remaining
            else:
                unrealized += (position.entry_price - bar.close) * position.remaining
        return self.realized + unrealized

    def run(self, bars: Sequence[Bar], sig: Signals) -> list[RefTrade]:
        self._bars = list(bars)
        self.curve: list[float] = []
        self.realized_curve = []
        n = len(self._bars)
        for i in range(n):
            bar = self._bars[i]
            if not (
                math.isfinite(bar.open) and math.isfinite(bar.high) and math.isfinite(bar.low)
            ):
                continue  # an unpriced bar cannot be traded on

            # 1) protective exits on the positions carried into this bar
            for position in list(self.positions):
                self._protective_exit(i, position, only_new=False)
                self._finalize(position)
            self.positions = [p for p in self.positions if p.remaining > 1e-9]

            # 2) fill the pending MARKET orders at this bar's open
            if self.pending:
                self._fill_pending(i)

            # 3) a fill bar can itself trade through the fresh level (A-H1)
            for position in list(self.positions):
                self._protective_exit(i, position, only_new=True)
                self._finalize(position)
            self.positions = [p for p in self.positions if p.remaining > 1e-9]

            # 4) new entry signal -> pending MARKET order (one per bar)
            if sig.entries[i]:
                self._create_order(i, sig)

            # 5) signal exit at the trigger bar's close
            if sig.exits[i] and self.positions:
                self._signal_exit(i)

            # 6) reported equity: realized cash + unrealized mark-to-market
            self.curve.append(_round(self._reported_equity(bar), 2))
            self.realized_curve.append(_round(self.realized, 2))

        if self.positions:
            last = self._bars[-1]
            for position in list(self.positions):
                self._close_partial(position, n - 1, float(last.close), position.remaining, "end_of_data")
                self._finalize(position)
            self.positions = []
            # the engine re-prices the final bar with the position closed, so
            # the last curve point reports realized cash only
            self.curve[-1] = _round(self.realized, 2)
            self.realized_curve[-1] = _round(self.realized, 2)
        return self.trades

    # -- reporting -----------------------------------------------------------

    @property
    def curve_by_time(self) -> dict[str, float]:
        return {bar.time: value for bar, value in zip(self._bars, self.curve)}

    @property
    def realized_by_time(self) -> dict[str, float]:
        """Realized cash at the end of each bar (the engine's published curve basis)."""
        return {bar.time: value for bar, value in zip(self._bars, self.realized_curve)}

    @property
    def realized_equity(self) -> float:
        """Sizing basis: initial capital plus realized net PnL (no mark-to-market)."""
        return self.realized


def parse_bars(rows: Sequence[tuple[str, float, float, float, float]]) -> list[Bar]:
    return [Bar(time=str(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4])) for r in rows]


def read_bars_csv(path) -> list[Bar]:
    """Read a committed fixture (``timestamp,open,high,low,close,volume``)."""
    import csv

    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        bars = []
        for row in reader:
            bars.append(
                Bar(
                    time=str(row["timestamp"]),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                )
            )
    return bars


__all__ = [
    "Bar",
    "ENGINE_FALLBACK_STOP_PCT",
    "ENGINE_MAX_POSITIONS",
    "ENGINE_MAX_TOTAL_EXPOSURE",
    "RefConfig",
    "RefLeg",
    "RefTrade",
    "ReferenceSimulator",
    "Signals",
    "parse_bars",
    "read_bars_csv",
]