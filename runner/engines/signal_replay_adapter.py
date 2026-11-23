from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import TradeSignal as RiskTradeSignal
from risk.models import realized_risk_amount, realized_risk_percent
from runner.engines import contract
from runner.engines.contract import resolve_same_bar_exit
from runner.fees import resolve_fee_model
from runner.slippage import BarSpreadSlippage, resolve_slippage_model
from runner.stats import compute_stats_for_result
from shared.constants import DATA_DIR, VALID_TIMEFRAMES, normalize_timeframe
from shared.models import BacktestMeta, BacktestResult, EquityPoint, Trade, TradeExit
from shared.trailing import (
    REPLAY_SUPPORTED_TRAILING_MODES,
    TRAILING_MODE_SUPERTREND_FLIP,
    TRAILING_SL_CONFIG_METADATA_KEY,
    SupertrendFlipPlan,
    parse_supertrend_flip_plan,
    tp1_partial_quantity,
    validate_trailing_sl_config,
)
from signal_bots.ramjet.processor import calculate_supertrend
from signal_bots.registry import get_signal_processor

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float, str], None]


def _to_iso(value: pd.Timestamp) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat()


def _to_timestamp(value: Any) -> pd.Timestamp:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Invalid timestamp value: {value!r}")
    return pd.Timestamp(ts)


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc
    if pd.isna(numeric):
        return None
    return numeric


def _coerce_action(value: Any) -> str:
    action = str(value or "").strip().lower()
    if action in {"buy", "long"}:
        return "buy"
    if action in {"sell", "short"}:
        return "sell"
    raise ValueError(f"Unsupported signal action '{value}'")


@dataclass(slots=True)
class ReplaySignal:
    timestamp: pd.Timestamp
    symbol: str
    action: str
    entry_price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    timeframe: str = "1h"
    source: str = "internal"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], default_timeframe: str = "1h") -> "ReplaySignal":
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}

        timeframe = normalize_timeframe(
            str(payload.get("timeframe") or metadata.get("timeframe") or default_timeframe)
        ) or default_timeframe

        source = str(payload.get("source") or metadata.get("source") or "internal").strip().lower()
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("Signal symbol is required")

        timestamp = _to_timestamp(payload.get("timestamp"))
        action = _coerce_action(payload.get("action"))
        entry_price = float(payload.get("entry_price"))
        if not math.isfinite(entry_price) or entry_price <= 0:
            raise ValueError("entry_price must be finite and > 0")

        stop_loss = _to_optional_float(payload.get("stop_loss"))
        take_profit = _to_optional_float(payload.get("take_profit"))
        if stop_loss is not None and (not math.isfinite(stop_loss) or stop_loss <= 0):
            raise ValueError("stop_loss must be finite and > 0")
        if take_profit is not None and (not math.isfinite(take_profit) or take_profit <= 0):
            raise ValueError("take_profit must be finite and > 0")

        return cls(
            timestamp=timestamp,
            symbol=symbol,
            action=action,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            timeframe=timeframe,
            source=source or "internal",
            metadata=dict(metadata),
        )


#: ``supertrend_flip`` phases (#168), in order.
_FLIP_PHASE_BEFORE_TP1 = "before_tp1"
_FLIP_PHASE_AWAIT_GREEN = "await_green"
_FLIP_PHASE_ACTIVE = "active"
#: Fill convention of the flip leg (the only exit that is not at a level or at
#: the signal bar's close): decided at the flip bar's close, filled at the next
#: bar's open. Recorded per trade so the run-level ``signal_exit_fill`` is not
#: read as covering it.
FLIP_EXIT_FILL = "next_bar_open"


@dataclass(slots=True)
class _FlipState:
    """Progress of one ``supertrend_flip`` position through its workflow."""

    plan: SupertrendFlipPlan
    phase: str = _FLIP_PHASE_BEFORE_TP1
    #: Set at the close of the first red bar after activation; the remainder
    #: is then closed at the OPEN of the next bar (next-open fill model).
    exit_pending: bool = False
    tp1_time: pd.Timestamp | None = None
    #: Bar that touched the TP1 level while the whole-share tranche floored to
    #: zero shares, so TP1 was skipped there (the original bot's answer).
    tp1_skipped_time: pd.Timestamp | None = None
    activation_time: pd.Timestamp | None = None
    flip_bar_time: pd.Timestamp | None = None

    def to_meta(self) -> dict[str, Any]:
        def iso(value: pd.Timestamp | None) -> str | None:
            return _to_iso(value) if value is not None else None

        return {
            "tp1_price": self.plan.tp1_price,
            "tp1_exit_fraction": self.plan.tp1_exit_fraction,
            "breakeven_after_tp1": self.plan.breakeven_after_tp1,
            "supertrend_period": self.plan.supertrend_period,
            "supertrend_multiplier": self.plan.supertrend_multiplier,
            "phase": self.phase,
            "tp1_time": iso(self.tp1_time),
            "tp1_skipped_time": iso(self.tp1_skipped_time),
            "activation_time": iso(self.activation_time),
            "flip_bar_time": iso(self.flip_bar_time),
            "flip_exit_fill": FLIP_EXIT_FILL,
        }


def _unchecked_window(
    frame: pd.DataFrame, position: "_OpenPosition", end_timestamp: pd.Timestamp
) -> pd.DataFrame:
    """Bars after the position's last checked bar, up to and including ``end_timestamp``."""
    return frame[
        (frame["timestamp"] > position.last_checked_at) & (frame["timestamp"] <= end_timestamp)
    ]


@dataclass(slots=True)
class _OpenPosition:
    symbol: str
    direction: str  # LONG/SHORT
    entry_time: pd.Timestamp
    entry_bar_index: int
    entry_price: float
    signal_entry_price: float
    quantity: float
    stop_loss: float | None
    take_profit: float | None
    initial_stop_loss: float | None
    initial_take_profit: float | None
    risk_amount: float | None
    risk_percent: float | None
    timeframe: str
    source: str
    signal_time: pd.Timestamp
    signal_metadata: dict[str, Any]
    last_checked_at: pd.Timestamp
    trailing_updates: int = 0
    mae: float = 0.0
    mfe: float = 0.0
    #: ``supertrend_flip`` workflow state; ``None`` for every other mode.
    flip: _FlipState | None = None
    #: Exits already taken from this position (TP1 partial). ``quantity`` is
    #: what is still open; the trade's entry quantity is ``quantity`` plus the
    #: partial quantities.
    partial_exits: list[TradeExit] = field(default_factory=list)


def _net_pnl_of(trade: Trade) -> float:
    """Net realized PnL of a closed trade (#84: never silently gross)."""
    if trade.net_pnl is not None:
        return float(trade.net_pnl)
    return float(trade.pnl) - float(trade.fees or 0.0)


class SignalReplayAdapter:
    """Replay external signals through OHLCV bars (C2 fill-policy delta).

    Documented differences vs the bar engines (native/vectorbt), by design:
    fills happen at the fill bar's CLOSE located via ``searchsorted(left)``
    (no next-open/spread model, no ``signal_exit_fill``/``spread_price_col``
    handling); bars with NaN OHLC are dropped loudly (count-logged, never
    silently skipped); protective exits settle across ALL open positions per
    signal, not just the signaled symbol.

    Exception (#168): the ``supertrend_flip`` exit fills at the OPEN of the
    bar after the red flip bar, because the flip is only known at that bar's
    close. If the flip bar is the last bar of the data the position is closed
    like every other open position at the end of the data. See
    :meth:`_check_flip_bar` for the full per-bar ordering.

    Gap fills (#287): inside the ``supertrend_flip`` workflow the stop and the
    TP1 tranche fill at the bar's OPEN when the bar opens beyond the level
    (``contract.resolve_protective_exit``); intrabar touches keep the level
    fill. The non-flip protective path still fills at the level — that path's
    gap handling lands with #208.
    """

    def __init__(
        self,
        *,
        risk_config: RiskConfig | None = None,
        initial_capital: float = 100_000.0,
        default_timeframe: str = "1h",
        signal_processor_name: str | None = None,
        signal_processor_config: dict[str, Any] | None = None,
        fee_model: Any | None = None,
        fee_params: dict[str, Any] | None = None,
        slippage_model: Any | None = None,
        slippage_params: dict[str, Any] | None = None,
        asset_class_by_symbol: dict[str, str] | None = None,
    ) -> None:
        self.risk_config = risk_config or RiskConfig()
        self.risk_engine = RiskEngine(self.risk_config)
        self.initial_capital = float(initial_capital)
        self.default_timeframe = normalize_timeframe(default_timeframe) or default_timeframe
        # #84: explicit fee model (no silent gross); resolved at run() so
        # per-run metadata can also declare costs.
        self._fee_model = fee_model
        self._fee_params = dict(fee_params or {})
        # #130: fill-price slippage model(s), resolved per symbol at run()
        # (asset class can differ across replayed symbols). ``None`` asset
        # classes record an explicit ``none`` — never a silent gross default.
        self._slippage_model = slippage_model
        self._slippage_params = dict(slippage_params or {})
        self._asset_class_by_symbol = {
            str(symbol).strip().upper(): str(asset_class or "")
            for symbol, asset_class in (asset_class_by_symbol or {}).items()
        }
        self._slippage_models: dict[str, Any] = {}
        self._slippage_meta: dict[str, dict[str, Any]] = {}
        self.signal_processor_name = str(signal_processor_name or "").strip().lower()
        self.signal_processor = (
            get_signal_processor(self.signal_processor_name, signal_processor_config)
            if self.signal_processor_name
            else None
        )

    @staticmethod
    def _prepare_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
        frame = df.copy()
        frame.columns = [str(column).lower() for column in frame.columns]
        if "timestamp" not in frame.columns:
            raise ValueError("OHLCV frame requires timestamp column")

        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame["open"] = pd.to_numeric(frame.get("open"), errors="coerce")
        frame["high"] = pd.to_numeric(frame.get("high"), errors="coerce")
        frame["low"] = pd.to_numeric(frame.get("low"), errors="coerce")
        frame["close"] = pd.to_numeric(frame.get("close"), errors="coerce")
        before = len(frame)
        frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
        dropped = before - len(frame)
        if dropped:
            # C2: NaN-OHLC drops are count-logged, never silent.
            logger.warning(
                "Dropped %d bars with non-numeric OHLC during replay prep",
                dropped,
            )
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        return frame

    @staticmethod
    def _find_fill_bar(
        frame: pd.DataFrame, timestamp: pd.Timestamp
    ) -> tuple[int, pd.Series] | None:
        idx = int(frame["timestamp"].searchsorted(timestamp, side="left"))
        if idx >= len(frame):
            return None
        return idx, frame.iloc[idx]

    @staticmethod
    def _position_risk_amount(position: _OpenPosition) -> float:
        # C2: no invented stops (same rule as the native engine's exposure
        # math) — a stopless position contributes nothing here instead of a
        # 2% fantasy distance.
        stop_loss = position.stop_loss
        if stop_loss is None:
            return 0.0
        return abs(position.entry_price - stop_loss) * position.quantity

    def _current_exposure(self, open_positions: dict[str, _OpenPosition], equity: float) -> float:
        if equity <= 0:
            return 0.0
        risk_amount = sum(
            self._position_risk_amount(position) for position in open_positions.values()
        )
        return max(0.0, risk_amount / equity)

    def _sizing_account_balance(
        self,
        *,
        open_positions: dict[str, _OpenPosition],
        realized_pnl: float,
        frames: dict[str, pd.DataFrame],
        as_of: pd.Timestamp,
    ) -> float:
        """Account balance used for position sizing (Cleanup 16-D1, F39).

        Sizing-time equity must measure the same capital as the reporting
        equity path: realized cash PLUS the mark-to-market PnL of open
        positions at ``as_of``. The previous realized-only balance sized new
        entries as if an open drawdown had not happened — optimistic sizing
        during drawdown, and a sizing-time equity path that diverged from the
        reported one.
        """
        unrealized = 0.0
        for symbol, position in open_positions.items():
            # #168: a TP1 partial exit is realized but its trade is not closed
            # yet; its gross PnL belongs to the balance now (costs are priced
            # once, when the trade closes — same as the native engine).
            for partial in position.partial_exits:
                unrealized += float(partial.pnl or 0.0)
            frame = frames.get(symbol)
            if frame is None or frame.empty:
                continue
            bars = frame[frame["timestamp"] <= as_of]
            if bars.empty:
                continue
            mark = float(bars.iloc[-1]["close"])
            if position.direction == "LONG":
                unrealized += (mark - position.entry_price) * position.quantity
            else:
                unrealized += (position.entry_price - mark) * position.quantity
        return max(float(self.initial_capital) + float(realized_pnl) + unrealized, 1.0)

    @staticmethod
    def _update_excursions(position: _OpenPosition, bar: pd.Series) -> None:
        high = float(bar["high"])
        low = float(bar["low"])
        if position.direction == "LONG":
            favorable = (high - position.entry_price) * position.quantity
            adverse = (low - position.entry_price) * position.quantity
        else:
            favorable = (position.entry_price - low) * position.quantity
            adverse = (position.entry_price - high) * position.quantity
        position.mfe = max(position.mfe, favorable)
        position.mae = min(position.mae, adverse)

    def _apply_trailing_stop(self, position: _OpenPosition, bar: pd.Series) -> None:
        trailing_cfg = position.signal_metadata.get("trailing_sl_config")
        if not isinstance(trailing_cfg, dict):
            return

        synthetic_signal = RiskTradeSignal(
            action="buy" if position.direction == "LONG" else "sell",
            symbol=position.symbol,
            entry_price=position.entry_price,
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            timeframe=position.timeframe,
            metadata={"trailing_sl_config": dict(trailing_cfg)},
        )
        next_stop = self.risk_engine.apply_trailing_stop(
            synthetic_signal,
            market_price=float(bar["close"]),
        )
        if next_stop is None:
            return
        if position.stop_loss is None or abs(float(next_stop) - float(position.stop_loss)) > 1e-12:
            position.stop_loss = float(next_stop)
            position.trailing_updates += 1

    def _slippage_for(self, symbol: str) -> Any:
        """Resolved slippage model for ``symbol`` (#130, lazy like fees)."""
        key = str(symbol or "").strip().upper()
        model = self._slippage_models.get(key)
        if model is None:
            if self._slippage_model is not None:
                model = self._slippage_model
            else:
                model = resolve_slippage_model(
                    dict(self._slippage_params),
                    asset_class=self._asset_class_by_symbol.get(key, ""),
                )
            self._slippage_models[key] = model
        return model

    def _slip(
        self, price: float, side: str, symbol: str, bar: Any = None
    ) -> float:
        """Adverse slippage adjustment on one replay fill price (#130).

        ``side`` is the trader's side: entries buy on LONG / sell on SHORT;
        exits are the mirror. Applied at every fill choke point so the
        recorded price, PnL and equity path carry the same cost.
        """
        return float(
            self._slippage_for(symbol).slip_price(float(price), side, bar)
        )

    def _close_position(
        self,
        position: _OpenPosition,
        *,
        exit_time: pd.Timestamp,
        exit_price: float,
        exit_reason: str,
        exit_bar_index: int,
        bar: pd.Series | None = None,
    ) -> Trade:
        # #130: the exit leg slips adversely before PnL is computed — a
        # LONG exit sells lower, a SHORT exit buys higher.
        exit_price = self._slip(
            exit_price,
            "sell" if position.direction == "LONG" else "buy",
            position.symbol,
            bar,
        )
        if position.direction == "LONG":
            pnl = (exit_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - exit_price) * position.quantity
        final_exit = TradeExit(
            time=_to_iso(exit_time),
            price=float(exit_price),
            quantity=float(position.quantity),
            pnl=float(pnl),
            reason=exit_reason,
        )
        # #168: a position with partial exits becomes ONE trade with all its
        # exit legs (the Trade model's multi-exit form, as the native engine
        # records it). Quantity is the entry quantity, PnL the sum over legs,
        # and fees are priced once at the quantity-weighted average exit price
        # (exact for the notional model). Without partials every figure is
        # computed exactly as before.
        exits = [*position.partial_exits, final_exit]
        trade_quantity = float(position.quantity)
        fee_exit_price = float(exit_price)
        if position.partial_exits:
            pnl = sum(float(item.pnl or 0.0) for item in exits)
            trade_quantity = sum(float(item.quantity) for item in exits)
            fee_exit_price = (
                sum(float(item.price) * float(item.quantity) for item in exits) / trade_quantity
            )

        risk_amount = float(position.risk_amount or 0.0)
        gross_notional = position.entry_price * trade_quantity
        pnl_pct = (pnl / gross_notional) * 100.0 if gross_notional > 0 else 0.0
        r_multiple = (pnl / risk_amount) if risk_amount > 0 else None
        signal_price = float(position.signal_entry_price)
        # #130: since the fill-price slippage model landed, entry_price
        # already carries the model's adverse adjustment, so this
        # signal-vs-fill difference now includes BOTH the signal's own
        # price gap AND the resolved slippage model — it is no longer a
        # pure execution-slippage measurement.
        slippage = float(position.entry_price - signal_price)
        slippage_bps = (slippage / signal_price) * 10_000 if signal_price > 0 else 0.0
        fill_delay_seconds = int(
            max(0.0, (position.entry_time - position.signal_time).total_seconds())
        )
        tags = ["signal-replay"]
        if position.source:
            tags.append(str(position.source))

        # #84: price the fill through the active fee model (set in run();
        # direct _close_position callers in tests must set _active_fee_model).
        fee_model = getattr(self, "_active_fee_model", None)
        if fee_model is None:
            fee_model = resolve_fee_model(dict(self._fee_params))
            self._active_fee_model = fee_model
        fees = round(
            float(
                fee_model.fee_for_trade(
                    float(position.entry_price), fee_exit_price, trade_quantity
                )
            ),
            2,
        )

        return Trade(
            id=0,
            symbol=position.symbol,
            direction=position.direction,
            entry_time=_to_iso(position.entry_time),
            entry_price=float(position.entry_price),
            quantity=trade_quantity,
            exits=exits,
            pnl=float(pnl),
            position_size=trade_quantity,
            risk_amount=float(position.risk_amount) if position.risk_amount is not None else None,
            risk_percent=(
                float(position.risk_percent) if position.risk_percent is not None else None
            ),
            exit_reason=exit_reason,
            pnl_pct=float(pnl_pct),
            fees=fees,
            net_pnl=round(float(pnl) - fees, 2),
            stop_loss=position.stop_loss,
            take_profit=position.take_profit,
            r_multiple=float(r_multiple) if r_multiple is not None else None,
            mae=float(position.mae),
            mfe=float(position.mfe),
            holding_bars=max(0, int(exit_bar_index - position.entry_bar_index)),
            tags=tags,
            meta={
                "signal_timestamp": _to_iso(position.signal_time),
                "fill_timestamp": _to_iso(position.entry_time),
                "fill_delay_seconds": fill_delay_seconds,
                "signal_source": position.source,
                "signal_price": signal_price,
                "fill_price": float(position.entry_price),
                "slippage": slippage,
                "slippage_bps": slippage_bps,
                "signal_stop_loss": position.initial_stop_loss,
                "signal_take_profit": position.initial_take_profit,
                "final_stop_loss": position.stop_loss,
                "final_take_profit": position.take_profit,
                "trailing_stop_updates": position.trailing_updates,
                "signal_metadata": position.signal_metadata,
                **(
                    {"supertrend_flip": position.flip.to_meta()}
                    if position.flip is not None
                    else {}
                ),
            },
        )

    def _check_protective_exit_bar(
        self,
        position: _OpenPosition,
        bar: pd.Series,
        index: int,
    ) -> Trade | None:
        """Process one bar of the protective-exit walk.

        Conformance (Cleanup 16-A, F8): bars are walked chronologically and
        classified through the shared same-bar policy; within a bar the
        stop-first tie-break applies when both levels are hit.
        """
        self._update_excursions(position, bar)
        event = resolve_same_bar_exit(
            direction=position.direction,
            high=float(bar["high"]),
            low=float(bar["low"]),
            stop_price=float(position.stop_loss) if position.stop_loss is not None else None,
            take_profit_price=(
                float(position.take_profit) if position.take_profit is not None else None
            ),
        )
        if event is None:
            self._apply_trailing_stop(position, bar)
            return None
        if event == "stop":
            exit_price = (
                float(position.stop_loss)
                if position.stop_loss is not None
                else float(bar["close"])
            )
            reason = "trailing_stop" if position.trailing_updates > 0 else "stop_loss"
        else:
            exit_price = (
                float(position.take_profit)
                if position.take_profit is not None
                else float(bar["close"])
            )
            reason = "take_profit"
        return self._close_position(
            position,
            exit_time=pd.Timestamp(bar["timestamp"]),
            exit_price=exit_price,
            exit_reason=reason,
            exit_bar_index=int(index),
            bar=bar,
        )

    def _process_protective_exits(
        self,
        *,
        position: _OpenPosition,
        frame: pd.DataFrame,
        end_timestamp: pd.Timestamp,
    ) -> Trade | None:
        if position.flip is not None:
            return self._process_flip_exits(
                position=position, frame=frame, end_timestamp=end_timestamp
            )
        if position.stop_loss is None and position.take_profit is None:
            position.last_checked_at = end_timestamp
            return None

        window = _unchecked_window(frame, position, end_timestamp)
        if window.empty:
            position.last_checked_at = end_timestamp
            return None

        for index, bar in window.iterrows():
            trade = self._check_protective_exit_bar(position, bar, int(index))
            if trade is not None:
                return trade
            position.last_checked_at = pd.Timestamp(bar["timestamp"])

        position.last_checked_at = end_timestamp
        return None

    # ── supertrend_flip workflow (#168) ─────────────────────────────────

    def _supertrend_states(
        self, frame: pd.DataFrame, plan: SupertrendFlipPlan
    ) -> pd.DataFrame:
        """Supertrend ``supertrend``/``supertrend_ready`` columns for ``frame``.

        Computed with the RamJet processor's own implementation, once per
        frame and parameter set. The computation is a forward recursion, so
        the row of bar ``N`` depends on bars ``0..N`` only: evaluating the
        whole frame at once is free of lookahead (pinned by a prefix test).
        """
        cache: dict[tuple[int, int, float], tuple[pd.DataFrame, pd.DataFrame]] | None
        cache = getattr(self, "_supertrend_cache", None)
        if cache is None:
            cache = {}
            self._supertrend_cache = cache
        key = (id(frame), plan.supertrend_period, plan.supertrend_multiplier)
        cached = cache.get(key)
        if cached is not None and cached[0] is frame:
            return cached[1]
        computed = calculate_supertrend(
            frame[["open", "high", "low", "close"]],
            plan.supertrend_period,
            plan.supertrend_multiplier,
        )
        if "supertrend" not in computed.columns or "supertrend_ready" not in computed.columns:
            raise ValueError("Supertrend computation returned no direction/readiness columns")
        states = computed[["supertrend", "supertrend_ready"]]
        cache[key] = (frame, states)
        return states

    def _process_flip_exits(
        self,
        *,
        position: _OpenPosition,
        frame: pd.DataFrame,
        end_timestamp: pd.Timestamp,
    ) -> Trade | None:
        """Walk the bars up to ``end_timestamp`` through the flip workflow."""
        assert position.flip is not None
        window = _unchecked_window(frame, position, end_timestamp)
        if window.empty:
            position.last_checked_at = end_timestamp
            return None
        states = self._supertrend_states(frame, position.flip.plan)
        for index, bar in window.iterrows():
            trade = self._check_flip_bar(position, bar, int(index), states.loc[index])
            if trade is not None:
                return trade
            position.last_checked_at = pd.Timestamp(bar["timestamp"])
        position.last_checked_at = end_timestamp
        return None

    def _check_flip_bar(
        self,
        position: _OpenPosition,
        bar: pd.Series,
        index: int,
        supertrend: pd.Series,
    ) -> Trade | None:
        """Process one bar of a ``supertrend_flip`` position.

        Chronological order inside the bar:

        1. Open: a flip exit decided at the previous bar's close fills here,
           at the open. Nothing else is evaluated on this bar.
        2. Intrabar: before TP1, the stop and the TP1 level go through
           ``contract.resolve_protective_exit`` — the gap-at-open check first
           (#287: a bar opening beyond a level fills at the open), then the
           adapter's shared same-bar policy (stop-first when both levels are
           touched — the whole position then stops out and TP1 does not
           happen). After TP1 only the stop is checked, with the same gap
           rule. A stop moved to breakeven on the TP1 bar takes effect from
           the next bar: inside the TP1 bar the order of the touches is
           unknown, the same rule the ``percent`` trail follows.
        3. Close: the Supertrend of this bar (from bars up to and including
           it) is evaluated once TP1 has happened — from the bar AFTER the
           TP1 bar, never on the TP1 bar's own close: nothing that happens at
           or inside the TP1 bar after the TP1 fill may move or trigger the
           trail. Not ready counts as neither green nor red. Waiting for green:
           green activates. Active: red marks the exit for the next bar's open.
        """
        flip = position.flip
        assert flip is not None
        bar_time = pd.Timestamp(bar["timestamp"])

        if flip.exit_pending:
            open_price = float(bar["open"])
            self._update_excursions(position, pd.Series({"high": open_price, "low": open_price}))
            return self._close_position(
                position,
                exit_time=bar_time,
                exit_price=open_price,
                exit_reason="supertrend_flip",
                exit_bar_index=index,
                bar=bar,
            )

        self._update_excursions(position, bar)
        bar_open = float(bar["open"])
        high = float(bar["high"])
        low = float(bar["low"])
        stop_price = float(position.stop_loss) if position.stop_loss is not None else None
        if flip.phase == _FLIP_PHASE_BEFORE_TP1:
            return self._check_flip_bar_before_tp1(
                position,
                bar=bar,
                bar_time=bar_time,
                index=index,
                bar_open=bar_open,
                high=high,
                low=low,
                stop_price=stop_price,
            )
        # #287: the open is the bar's first event — a bar that opens beyond the
        # stop gapped through it and fills at the open, beating any later
        # same-bar touch (mirrors ``resolve_protective_exit`` / native
        # ``_check_stops``). Intrabar touches keep the level fill.
        stop_event, gapped = contract.resolve_protective_exit(
            direction=position.direction,
            bar_open=bar_open,
            high=high,
            low=low,
            stop_price=stop_price,
            take_profit_price=None,
        )
        if stop_event == "stop":
            assert stop_price is not None  # a hit is only reported for a set level
            return self._close_flip_at_stop(
                position,
                bar,
                bar_time,
                index,
                exit_price=bar_open if gapped else stop_price,
            )

        ready = bool(supertrend["supertrend_ready"])
        if not ready:
            return None
        green = bool(supertrend["supertrend"])
        if flip.phase == _FLIP_PHASE_AWAIT_GREEN:
            if green:
                flip.phase = _FLIP_PHASE_ACTIVE
                flip.activation_time = bar_time
        elif flip.phase == _FLIP_PHASE_ACTIVE and not green:
            flip.exit_pending = True
            flip.flip_bar_time = bar_time
        return None

    def _check_flip_bar_before_tp1(
        self,
        position: _OpenPosition,
        *,
        bar: pd.Series,
        bar_time: pd.Timestamp,
        index: int,
        bar_open: float,
        high: float,
        low: float,
        stop_price: float | None,
    ) -> Trade | None:
        """Intrabar step of a flip position that has not reached TP1 yet."""
        flip = position.flip
        assert flip is not None
        # #287: gap checked first — a bar that opens beyond the stop or TP1
        # fills at the open; otherwise the shared stop-first same-bar policy.
        event, gapped = contract.resolve_protective_exit(
            direction=position.direction,
            bar_open=bar_open,
            high=high,
            low=low,
            stop_price=stop_price,
            take_profit_price=flip.plan.tp1_price,
        )
        if event == "stop":
            assert stop_price is not None  # a hit is only reported for a set level
            return self._close_flip_at_stop(
                position,
                bar,
                bar_time,
                index,
                exit_price=bar_open if gapped else stop_price,
            )
        if event != "tp":
            return None
        tp1_level = float(flip.plan.tp1_price)
        trade = self._take_tp1(
            position,
            bar,
            bar_time,
            index,
            fill_price=bar_open if gapped else tp1_level,
        )
        if trade is not None:
            return trade
        # A gap through TP1 makes the open the bar's first event, but the
        # bar's remaining range is still live for the remainder: live keeps
        # the remainder's ORIGINAL stop working all bar (the breakeven
        # amendment only takes effect on the next bar, #176), so a later
        # same-bar trade through that stop exits the remainder at the stop
        # level. The fill cannot itself be a gap — the open already sat
        # beyond TP1, on the stop's safe side. An intrabar (non-gap) TP1
        # fill needs no check: ``resolve_same_bar_exit`` only returns "tp"
        # when the stop was not touched.
        if gapped and stop_price is not None:
            stopped = (
                low <= stop_price
                if position.direction == "LONG"
                else high >= stop_price
            )
            if stopped:
                return self._close_position(
                    position,
                    exit_time=bar_time,
                    exit_price=float(stop_price),
                    exit_reason="stop_loss",
                    exit_bar_index=index,
                    bar=bar,
                )
        # The TP1 bar after the TP1 fill is dead for the workflow: its close is
        # not read (no activation, no flip) and its range is not checked against
        # the stop moved on it — the next bar's intrabar pass is the first one
        # that sees that stop. The Supertrend evaluation starts on the bar after.
        return None

    def _take_tp1(
        self,
        position: _OpenPosition,
        bar: pd.Series,
        bar_time: pd.Timestamp,
        index: int,
        *,
        fill_price: float,
    ) -> Trade | None:
        """Exit the whole-share TP1 tranche; returns a trade if nothing is left.

        ``fill_price`` is the TP1 level, or the bar's open when the bar gapped
        through it (#287).

        The tranche is ``quantity * tp1_exit_fraction`` floored to whole shares
        — the original bot's arithmetic (``shared.trailing.tp1_partial_quantity``),
        so the replay never sells shares the bot would not, and a live TP1
        tranche order (whole shares only) matches the replayed one.
        """
        flip = position.flip
        assert flip is not None
        plan = flip.plan
        exit_quantity = tp1_partial_quantity(position.quantity, plan.tp1_exit_fraction)
        if exit_quantity <= 0.0:
            # The original bot skips TP1 here ("No shares to sell at TP1.
            # Skipping."): no sell, no breakeven stop, and ``tp1_hit`` stays
            # false, so the Supertrend workflow is never armed. Mirrored: the
            # position stays in the before-TP1 phase under its ORIGINAL stop,
            # and the TP1 level keeps being re-checked on the following bars
            # exactly like the bot re-checks it on every pass. The event is
            # reported once, at the bar that first reached TP1 and sold
            # nothing — every later bar only repeats it.
            if flip.tp1_skipped_time is None:
                flip.tp1_skipped_time = bar_time
            return None
        flip.phase = _FLIP_PHASE_AWAIT_GREEN
        flip.tp1_time = bar_time
        if exit_quantity >= float(position.quantity):
            # Full close routes through _close_position, which applies the
            # adverse slippage itself — pass the pre-slippage fill price.
            return self._close_position(
                position,
                exit_time=bar_time,
                exit_price=fill_price,
                exit_reason="tp1",
                exit_bar_index=index,
                bar=bar,
            )
        # #130: the TP1 partial leg is a fill like any other — it slips
        # adversely (LONG sells lower, SHORT buys higher) before PnL books.
        tp1_fill = self._slip(
            fill_price,
            "sell" if position.direction == "LONG" else "buy",
            position.symbol,
            bar,
        )
        if position.direction == "LONG":
            pnl = (tp1_fill - position.entry_price) * exit_quantity
        else:
            pnl = (position.entry_price - tp1_fill) * exit_quantity
        position.partial_exits.append(
            TradeExit(
                time=_to_iso(bar_time),
                price=float(tp1_fill),
                quantity=exit_quantity,
                pnl=float(pnl),
                reason="tp1",
            )
        )
        position.quantity = float(position.quantity) - exit_quantity
        if plan.breakeven_after_tp1:
            breakeven = float(position.entry_price)
            if position.stop_loss is None or abs(breakeven - float(position.stop_loss)) > 1e-12:
                position.stop_loss = breakeven
                # trailing_updates counts every stop move, so for a flip position
                # it is the breakeven move (the only stop move the workflow makes).
                position.trailing_updates += 1
        return None

    def _close_flip_at_stop(
        self,
        position: _OpenPosition,
        bar: pd.Series,
        bar_time: pd.Timestamp,
        index: int,
        *,
        exit_price: float,
    ) -> Trade:
        flip = position.flip
        assert flip is not None and position.stop_loss is not None
        at_breakeven = (
            flip.phase != _FLIP_PHASE_BEFORE_TP1
            and flip.plan.breakeven_after_tp1
            and abs(float(position.stop_loss) - float(position.entry_price)) <= 1e-12
        )
        return self._close_position(
            position,
            exit_time=bar_time,
            exit_price=exit_price,
            exit_reason="breakeven_stop" if at_breakeven else "stop_loss",
            exit_bar_index=index,
            bar=bar,
        )

    def _apply_signal_processor(
        self,
        signal: ReplaySignal,
        frame: pd.DataFrame,
    ) -> ReplaySignal | None:
        if self.signal_processor is None:
            return signal

        context = frame[frame["timestamp"] <= signal.timestamp]
        if context.empty:
            context = frame.iloc[:1]
        raw_signal = {
            "symbol": signal.symbol,
            "action": signal.action,
            "timestamp": signal.timestamp.isoformat(),
            "entry_price": signal.entry_price,
            "signal_price": signal.entry_price,
            "timeframe": signal.timeframe,
            "metadata": dict(signal.metadata),
        }
        try:
            processed = self.signal_processor.process_signal(raw_signal, context)
        except ValueError:
            return None

        processed_metadata = dict(signal.metadata)
        processed_metadata.update(dict(processed.metadata or {}))
        if self.signal_processor_name:
            processed_metadata["signal_processor"] = self.signal_processor_name
        return ReplaySignal(
            timestamp=signal.timestamp,
            symbol=signal.symbol,
            action=_coerce_action(processed.action),
            entry_price=float(processed.entry_price),
            stop_loss=processed.stop_loss,
            take_profit=processed.take_profit,
            timeframe=processed.timeframe or signal.timeframe,
            source=signal.source,
            metadata=processed_metadata,
        )

    def run(
        self,
        *,
        signals: list[ReplaySignal],
        ohlcv_by_symbol: dict[str, pd.DataFrame],
        timeframe: str = "1h",
        backtest_start: str = "",
        backtest_end: str = "",
        metadata: dict[str, Any] | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> BacktestResult:
        def emit(percent: float, message: str) -> None:
            if progress_callback is None:
                return
            bounded = max(0.0, min(100.0, float(percent)))
            progress_callback(round(bounded, 2), message)

        emit(5.0, "Preparing replay inputs...")
        # #84: missing fee/slippage config is a hard error, never silent gross.
        fee_model = self._fee_model
        if fee_model is None:
            merged_fee_params = dict(self._fee_params)
            if metadata and isinstance(metadata.get("fee_params"), dict):
                merged_fee_params.update(metadata["fee_params"])
            fee_model = resolve_fee_model(merged_fee_params)
        self._active_fee_model = fee_model
        # #130: resolve the fill-price slippage model per symbol (asset
        # class can differ across replayed symbols) and record each in the
        # run metadata — mandatory even when the model is ``none``.
        slippage_params = dict(self._slippage_params)
        if metadata and isinstance(metadata.get("slippage_params"), dict):
            slippage_params.update(metadata["slippage_params"])
        self._slippage_params = slippage_params
        self._slippage_models = {}
        self._slippage_meta = {}
        # #168: per-run Supertrend cache (keyed by frame identity).
        self._supertrend_cache = {}
        normalized_timeframe = normalize_timeframe(timeframe) or self.default_timeframe
        frames = self._prepare_replay_frames(ohlcv_by_symbol)
        if not frames:
            raise ValueError("No OHLCV data available for replay")
        for replay_symbol, replay_frame in frames.items():
            asset_class = self._asset_class_by_symbol.get(replay_symbol, "")
            model = self._slippage_model or resolve_slippage_model(
                slippage_params, asset_class=asset_class
            )
            self._slippage_models[replay_symbol] = model
            # #130: same price-basis rule as the native adapter —
            # "bid_ask" only when the bar-spread model can use the ask
            # columns; fixed/none models fill on the canonical (bid)
            # series even when ask columns happen to be present.
            has_two_sided = any(
                str(col).startswith("ask_") for col in replay_frame.columns
            )
            price_basis = (
                "bid_ask"
                if has_two_sided and isinstance(model, BarSpreadSlippage)
                else ("bid" if has_two_sided else "single_price")
            )
            self._slippage_meta[replay_symbol] = {
                **model.describe(),
                "price_basis": price_basis,
            }

        replay_signals = self._select_replay_signals(signals, frames)
        if not replay_signals:
            raise ValueError("No replayable signals after symbol/OHLCV normalization")

        open_positions: dict[str, _OpenPosition] = {}
        closed_trades: list[Trade] = []
        realized_pnl = 0.0

        total_signals = len(replay_signals)
        for index, signal in enumerate(replay_signals, start=1):
            frame = frames[signal.symbol]
            effective_signal = self._apply_signal_processor(signal, frame)
            if effective_signal is None:
                emit(10.0 + ((index / total_signals) * 80.0), _REPLAY_PROGRESS_MESSAGE)
                continue
            # #150 fail closed at signal time: a signal whose trailing mode
            # replay cannot execute (unknown) is rejected — it is not traded,
            # instead of being traded with the requested trailing silently
            # dropped. This aborts the run on purpose: a config error must not
            # produce a backtest result that silently differs from the
            # strategy's intent.
            trailing_cfg = (effective_signal.metadata or {}).get(
                TRAILING_SL_CONFIG_METADATA_KEY
            )
            trailing_mode = validate_trailing_sl_config(
                trailing_cfg,
                supported=REPLAY_SUPPORTED_TRAILING_MODES,
                context=f"signal replay ({effective_signal.symbol})",
            )
            # #168: the supertrend_flip workflow needs all of its parameters;
            # a missing or invalid one rejects the signal the same way.
            flip_plan = (
                parse_supertrend_flip_plan(
                    trailing_cfg,
                    effective_signal.metadata,
                    action=effective_signal.action,
                    take_profit=effective_signal.take_profit,
                    stop_loss=effective_signal.stop_loss,
                    context=f"signal replay ({effective_signal.symbol})",
                )
                if trailing_mode == TRAILING_MODE_SUPERTREND_FLIP
                else None
            )
            # Settle protective exits for ALL open positions up to this
            # signal's fill time (CodeRabbit round): previously only the
            # signaled symbol's position was settled, so a stop-out on another
            # symbol stayed unrealized in the sizing balance until an unrelated
            # signal for that symbol arrived — inflating sizing equity during
            # multi-symbol drawdowns.
            settled_symbols: list[str] = []
            for symbol, position in open_positions.items():
                if symbol == signal.symbol:
                    continue
                protected_exit = self._process_protective_exits(
                    position=position,
                    frame=frames[symbol],
                    end_timestamp=effective_signal.timestamp,
                )
                if protected_exit is not None:
                    closed_trades.append(protected_exit)
                    realized_pnl += _net_pnl_of(protected_exit)
                    settled_symbols.append(symbol)
            for symbol in settled_symbols:
                open_positions.pop(symbol, None)

            current_position = open_positions.get(signal.symbol)
            if current_position is not None:
                protected_exit = self._process_protective_exits(
                    position=current_position,
                    frame=frame,
                    end_timestamp=effective_signal.timestamp,
                )
                if protected_exit is not None:
                    closed_trades.append(protected_exit)
                    realized_pnl += _net_pnl_of(protected_exit)
                    open_positions.pop(signal.symbol, None)
                    current_position = None

            filled = self._find_fill_bar(frame, effective_signal.timestamp)
            if filled is None:
                continue
            fill_index, fill_bar = filled
            raw_fill_price = float(fill_bar["close"])
            fill_time = pd.Timestamp(fill_bar["timestamp"])
            target_direction = "LONG" if effective_signal.action == "buy" else "SHORT"
            # #130: the entry leg slips adversely (buy pays up, sell receives
            # down) before sizing and before the position records its price.
            fill_price = self._slip(
                raw_fill_price,
                "buy" if target_direction == "LONG" else "sell",
                signal.symbol,
                fill_bar,
            )
            exit_only = bool(effective_signal.metadata.get("exit_only"))

            if current_position is not None:
                if current_position.direction == target_direction:
                    current_position.last_checked_at = fill_time
                    emit(10.0 + ((index / total_signals) * 80.0), _REPLAY_PROGRESS_MESSAGE)
                    continue
                flip_trade = self._close_position(
                    current_position,
                    exit_time=fill_time,
                    # Raw bar close: _close_position applies the adverse
                    # slippage for the EXIT side itself (bar passed through).
                    exit_price=raw_fill_price,
                    exit_reason="signal_flip",
                    exit_bar_index=int(fill_index),
                    bar=fill_bar,
                )
                closed_trades.append(flip_trade)
                realized_pnl += _net_pnl_of(flip_trade)
                open_positions.pop(signal.symbol, None)
                current_position = None
                if exit_only:
                    emit(10.0 + ((index / total_signals) * 80.0), _REPLAY_PROGRESS_MESSAGE)
                    continue
            elif exit_only:
                emit(10.0 + ((index / total_signals) * 80.0), _REPLAY_PROGRESS_MESSAGE)
                continue

            account_balance = self._sizing_account_balance(
                open_positions=open_positions,
                realized_pnl=realized_pnl,
                frames=frames,
                as_of=fill_time,
            )
            exposure = self._current_exposure(open_positions, account_balance)
            risk_signal = RiskTradeSignal(
                action="buy" if target_direction == "LONG" else "sell",
                symbol=effective_signal.symbol,
                entry_price=fill_price,
                stop_loss=effective_signal.stop_loss,
                take_profit=effective_signal.take_profit,
                timeframe=effective_signal.timeframe or normalized_timeframe,
                metadata={
                    **dict(effective_signal.metadata or {}),
                    "current_positions": len(open_positions),
                },
            )
            sized = self.risk_engine.calculate_position_size(
                signal=risk_signal,
                account_balance=account_balance,
                pip_value=1.0,
                current_exposure=exposure,
            )
            quantity = float(sized.position_size)
            if quantity <= 0:
                emit(10.0 + ((index / total_signals) * 80.0), _REPLAY_PROGRESS_MESSAGE)
                continue

            opened = _OpenPosition(
                symbol=effective_signal.symbol,
                direction=target_direction,
                entry_time=fill_time,
                entry_bar_index=int(fill_index),
                entry_price=fill_price,
                signal_entry_price=float(effective_signal.entry_price),
                quantity=quantity,
                stop_loss=risk_signal.stop_loss,
                take_profit=risk_signal.take_profit,
                initial_stop_loss=risk_signal.stop_loss,
                initial_take_profit=risk_signal.take_profit,
                # #84: store the realized stop-loss amount, not the policy
                # figure (they differ on notional-cap).
                risk_amount=float(realized_risk_amount(sized)),
                risk_percent=float(realized_risk_percent(sized, account_balance)),
                timeframe=risk_signal.timeframe,
                source=effective_signal.source,
                signal_time=effective_signal.timestamp,
                signal_metadata=dict(effective_signal.metadata),
                last_checked_at=fill_time,
                flip=_FlipState(plan=flip_plan) if flip_plan is not None else None,
            )
            open_positions[signal.symbol] = opened
            emit(10.0 + ((index / total_signals) * 80.0), _REPLAY_PROGRESS_MESSAGE)

        emit(92.0, "Finalizing open positions...")
        for symbol, position in open_positions.items():
            frame = frames[symbol]
            protected_exit = self._process_protective_exits(
                position=position,
                frame=frame,
                end_timestamp=pd.Timestamp(frame["timestamp"].iloc[-1]),
            )
            if protected_exit is not None:
                closed_trades.append(protected_exit)
                realized_pnl += _net_pnl_of(protected_exit)
                continue
            last_index = len(frame) - 1
            last_bar = frame.iloc[last_index]
            self._update_excursions(position, last_bar)
            closed = self._close_position(
                position,
                exit_time=pd.Timestamp(last_bar["timestamp"]),
                exit_price=float(last_bar["close"]),
                exit_reason="end_of_data",
                exit_bar_index=last_index,
                bar=last_bar,
            )
            closed_trades.append(closed)
            realized_pnl += _net_pnl_of(closed)

        closed_trades.sort(
            key=lambda trade: (trade.last_exit_time, trade.entry_time, trade.symbol),
        )
        for idx, trade in enumerate(closed_trades, start=1):
            trade.id = idx

        running_equity = float(self.initial_capital)
        curve_start = pd.to_datetime(
            backtest_start or (closed_trades[0].entry_time if closed_trades else ""),
            utc=True,
            errors="coerce",
        )
        if pd.isna(curve_start):
            curve_start = pd.Timestamp.now(tz="UTC")
        equity_curve: list[EquityPoint] = [
            EquityPoint(time=pd.Timestamp(curve_start).isoformat(), equity=round(running_equity, 2))
        ]
        for trade in closed_trades:
            # #84: the equity path accumulates NET pnl.
            running_equity += _net_pnl_of(trade)
            point = EquityPoint(
                time=trade.last_exit_time,
                equity=round(running_equity, 2),
            )
            if equity_curve[-1].time == point.time:
                equity_curve[-1] = point
            else:
                equity_curve.append(point)

        signal_sources = self._collect_signal_sources(replay_signals)
        tags = [
            "signal-replay",
            "signal_replay",
            *[f"source:{source}" for source in signal_sources],
        ]
        created_at = datetime.now(timezone.utc).isoformat()
        inferred_start = backtest_start or replay_signals[0].timestamp.date().isoformat()
        inferred_end = backtest_end or replay_signals[-1].timestamp.date().isoformat()
        meta = BacktestMeta(
            id=f"signal-replay-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}",
            created_at=created_at,
            engine="signal-replay",
            strategies=["Signal Replay"],
            backtest_start=str(inferred_start),
            backtest_end=str(inferred_end),
            data={
                "symbols": sorted(frames.keys()),
                "timeframes": [normalized_timeframe],
                "source": ",".join(signal_sources) if signal_sources else "internal",
                # #130: mandatory slippage-model record per symbol.
                "slippage": {"models": self._slippage_meta},
            },
            initial_capital=float(self.initial_capital),
            risk_per_trade_pct=float(self.risk_config.max_risk_per_trade),
            max_position_exposure_pct=float(self.risk_config.max_notional_per_trade),
            max_leverage=None,
            modeling_resolution=normalized_timeframe,
            signal_exit_fill="close",
            allow_fallback_stop=self.risk_config.allow_fallback_stop,
            reject_missing_stop=self.risk_config.reject_missing_stop,
            require_explicit_column_roles=False,
            parameters={
                "max_risk_per_trade": float(self.risk_config.max_risk_per_trade),
                "max_total_exposure": float(self.risk_config.max_total_exposure),
                "max_positions": int(self.risk_config.max_positions),
                "signal_processor": self.signal_processor_name or None,
                **(metadata or {}),
            },
            tags=tags,
            notes="Signal Replay",
        )
        result = BacktestResult(meta=meta, trades=closed_trades, equity_curve=equity_curve)
        result.stats = compute_stats_for_result(result)
        emit(100.0, f"Completed - {len(closed_trades)} trades found")
        return result

    def _prepare_replay_frames(
        self, ohlcv_by_symbol: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for symbol, frame in ohlcv_by_symbol.items():
            normalized_symbol = str(symbol or "").strip().upper()
            if not normalized_symbol:
                continue
            prepared = self._prepare_ohlcv(frame)
            if prepared.empty:
                continue
            frames[normalized_symbol] = prepared
        return frames

    @staticmethod
    def _select_replay_signals(
        signals: list[ReplaySignal], frames: dict[str, pd.DataFrame]
    ) -> list[ReplaySignal]:
        return sorted(
            [signal for signal in signals if signal.symbol in frames],
            key=lambda signal: (signal.timestamp, signal.symbol, signal.action),
        )

    def _build_risk_signal(
        self,
        *,
        effective_signal: ReplaySignal,
        target_direction: str,
        fill_price: float,
        normalized_timeframe: str,
        open_positions: dict[str, _OpenPosition],
    ) -> RiskTradeSignal:
        return RiskTradeSignal(
            action="buy" if target_direction == "LONG" else "sell",
            symbol=effective_signal.symbol,
            entry_price=fill_price,
            stop_loss=effective_signal.stop_loss,
            take_profit=effective_signal.take_profit,
            timeframe=effective_signal.timeframe or normalized_timeframe,
            metadata={
                **dict(effective_signal.metadata or {}),
                "current_positions": len(open_positions),
            },
        )

    @staticmethod
    def _collect_signal_sources(replay_signals: list[ReplaySignal]) -> list[str]:
        return sorted(
            {
                str(signal.source or "internal").lower()
                for signal in replay_signals
                if str(signal.source or "").strip()
            }
        )


_REPLAY_PROGRESS_MESSAGE = "Replaying signals..."


class SignalReplayEngine:
    """Higher-level replay engine with optional yfinance auto-download fallback."""

    def __init__(
        self,
        *,
        data_dir: str | Path | None = None,
        risk_config: RiskConfig | None = None,
        default_timeframe: str = "1h",
        signal_processor_name: str | None = None,
        fee_model: Any | None = None,
        fee_params: dict[str, Any] | None = None,
        slippage_model: Any | None = None,
        slippage_params: dict[str, Any] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir or DATA_DIR).expanduser().resolve()
        self.risk_config = risk_config or RiskConfig()
        self.default_timeframe = normalize_timeframe(default_timeframe) or "1h"
        self.signal_processor_name = str(signal_processor_name or "").strip().lower()
        # #84: explicit fee declaration, forwarded to the inner adapter.
        self._fee_model = fee_model
        self._fee_params = dict(fee_params or {})
        # #130: fill-price slippage declaration + per-symbol asset classes
        # learned while loading OHLCV, forwarded to the inner adapter.
        self._slippage_model = slippage_model
        self._slippage_params = dict(slippage_params or {})
        self._asset_class_by_symbol: dict[str, str] = {}

    @staticmethod
    def _infer_replay_timeframe(
        signals: list[ReplaySignal],
        fallback: str,
    ) -> str:
        declared = [
            normalized
            for signal in signals
            if (normalized := normalize_timeframe(signal.timeframe)) in VALID_TIMEFRAMES
        ]
        if declared:
            order = {
                "tick": 0,
                "s1": 1,
                "1m": 2,
                "5m": 3,
                "15m": 4,
                "30m": 5,
                "1h": 6,
                "4h": 7,
                "1d": 8,
                "1w": 9,
            }
            return min(declared, key=lambda value: order.get(value, len(order)))
        has_intraday = any(
            any((signal.timestamp.hour, signal.timestamp.minute, signal.timestamp.second))
            for signal in signals
        )
        if has_intraday:
            return "1m"
        if signals:
            return "1d"
        return fallback

    def _load_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame | None:
        normalized_symbol = str(symbol or "").strip().upper()
        normalized_timeframe = normalize_timeframe(timeframe) or "1d"
        if not normalized_symbol:
            return None

        ohlcv_root = self.data_dir / "ohlcv"
        matches = sorted(ohlcv_root.rglob(f"**/{normalized_symbol}/{normalized_timeframe}.parquet"))
        if not matches:
            return None
        # #130: classify the instrument from the OHLCV layout so the
        # slippage defaults apply per asset class (first match wins, same
        # convention as the native adapter).
        self._asset_class_by_symbol[normalized_symbol] = contract.ohlcv_path_metadata(
            matches[0]
        )["asset_class"]

        frames: list[pd.DataFrame] = []
        for path in matches:
            try:
                frame = pd.read_parquet(path)
            except (ValueError, OSError):
                continue
            frame.columns = [str(column).lower() for column in frame.columns]
            frame = contract.normalize_ohlcv_frame(frame)
            if "timestamp" not in frame.columns:
                continue
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
            frame = frame.dropna(subset=["timestamp"])
            if not frame.empty:
                frames.append(frame)

        if not frames:
            return None

        merged = pd.concat(frames, ignore_index=True, sort=False)
        merged = merged.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
        merged = merged.reset_index(drop=True)
        # Conformance (Cleanup 16-A, F9): shared date-filter convention — a
        # date-only end boundary is inclusive of the full day. This must be
        # the ONLY date filter on the frame (a bare-date string comparison
        # would truncate at midnight and drop the end day).
        merged = contract.apply_date_filter(merged, start=start, end=end)
        if merged.empty:
            return None
        return merged

    def _download_yfinance(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: str | None,
        end: str | None,
    ) -> None:
        from data_tools.downloaders.yfinance_dl import YFinanceDownloader

        downloader = YFinanceDownloader()
        downloader.download(
            symbol=symbol,
            timeframe=timeframe,
            output_dir=self.data_dir,
            start=start or None,
            end=end or None,
        )

    def run_backtest(
        self,
        signals: list[dict[str, Any]],
        signal_processor: str | None,
        symbols: list[str],
        start_date: str,
        end_date: str,
        initial_capital: float,
        *,
        timeframe: str | None = None,
        auto_download: bool = True,
        progress_callback: ProgressCallback | None = None,
        fee_model: Any | None = None,
        fee_params: dict[str, Any] | None = None,
        slippage_model: Any | None = None,
        slippage_params: dict[str, Any] | None = None,
    ) -> BacktestResult:
        if not signals:
            raise ValueError("signals must not be empty")

        replay_signals = [
            ReplaySignal.from_payload(signal, default_timeframe=self.default_timeframe)
            for signal in signals
        ]
        resolved_timeframe = normalize_timeframe(timeframe or "") or self._infer_replay_timeframe(
            replay_signals,
            self.default_timeframe,
        )
        selected_symbols = sorted(
            {
                str(symbol or "").strip().upper()
                for symbol in (symbols or [signal.symbol for signal in replay_signals])
                if str(symbol or "").strip()
            }
        )
        if not selected_symbols:
            raise ValueError("symbols must not be empty")

        ohlcv_by_symbol: dict[str, pd.DataFrame] = {}
        for symbol in selected_symbols:
            frame = self._load_ohlcv(
                symbol=symbol,
                timeframe=resolved_timeframe,
                start=start_date,
                end=end_date,
            )
            if frame is None and auto_download:
                self._download_yfinance(
                    symbol=symbol,
                    timeframe=resolved_timeframe,
                    start=start_date,
                    end=end_date,
                )
                frame = self._load_ohlcv(
                    symbol=symbol,
                    timeframe=resolved_timeframe,
                    start=start_date,
                    end=end_date,
                )
            if frame is None:
                raise ValueError(f"Missing OHLCV for {symbol} ({resolved_timeframe})")
            ohlcv_by_symbol[symbol] = frame

        processor_name = str(signal_processor or self.signal_processor_name or "").strip().lower()
        merged_fee_params = dict(self._fee_params)
        if fee_params:
            merged_fee_params.update(fee_params)
        merged_slippage_params = dict(self._slippage_params)
        if slippage_params:
            merged_slippage_params.update(slippage_params)
        adapter = SignalReplayAdapter(
            risk_config=self.risk_config,
            initial_capital=float(initial_capital),
            default_timeframe=resolved_timeframe,
            signal_processor_name=processor_name or None,
            fee_model=fee_model if fee_model is not None else self._fee_model,
            fee_params=merged_fee_params,
            slippage_model=(
                slippage_model if slippage_model is not None else self._slippage_model
            ),
            slippage_params=merged_slippage_params,
            asset_class_by_symbol=dict(self._asset_class_by_symbol),
        )
        result = adapter.run(
            signals=replay_signals,
            ohlcv_by_symbol=ohlcv_by_symbol,
            timeframe=resolved_timeframe,
            backtest_start=start_date,
            backtest_end=end_date,
            metadata={
                "signal_source": "signal_replay",
                "signal_processor": processor_name or None,
            },
            progress_callback=progress_callback,
        )
        result.meta.tags = sorted(
            {
                *[str(tag) for tag in (result.meta.tags or []) if str(tag).strip()],
                "signal_replay",
                "signal-replay",
            }
        )
        return result
