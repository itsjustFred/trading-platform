from __future__ import annotations

from typing import Any

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import SizedOrder, TradeSignal
from shared.models import LiveSignal


def _to_float(value: Any, *, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any, *, default: int) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def size_live_signal(
    signal: LiveSignal,
    *,
    account_balance: float,
) -> SizedOrder:
    metadata = dict(signal.metadata or {})

    explicit_qty = _to_float(metadata.get("explicit_qty"), default=0.0)
    if explicit_qty > 0:
        trade_signal = TradeSignal(
            action=str(signal.action),
            symbol=str(signal.symbol),
            entry_price=_to_float(
                metadata.get("entry_price"),
                default=float(signal.theoretical_price),
            ),
            stop_loss=_to_optional_float(metadata.get("stop_loss")),
            take_profit=_to_optional_float(metadata.get("take_profit")),
            timeframe=str(metadata.get("timeframe") or "1m"),
            metadata={
                "atr": metadata.get("atr"),
                "current_positions": _to_int(metadata.get("current_positions"), default=0),
                "pending_positions": _to_int(metadata.get("pending_positions"), default=0),
            },
        )
        sized = SizedOrder(
            signal=trade_signal,
            position_size=float(explicit_qty),
            risk_amount=0.0,
            risk_percent=0.0,
            risk_reward_ratio=None,
        )
        signal.metadata = {
            **metadata,
            "qty": float(explicit_qty),
            "risk_amount": 0.0,
            "risk_percent": 0.0,
            "risk_reward_ratio": None,
            "resolved_stop_loss": trade_signal.stop_loss,
        }
        return sized

    config = RiskConfig(
        max_risk_per_trade=_to_float(metadata.get("max_risk_per_trade"), default=0.01),
        max_total_exposure=_to_float(metadata.get("max_total_exposure"), default=0.05),
        max_positions=_to_int(metadata.get("max_positions"), default=5),
        default_sl_atr_multiplier=_to_float(
            metadata.get("default_sl_atr_multiplier"),
            default=2.0,
        ),
    )
    risk_engine = RiskEngine(config)

    trade_signal = TradeSignal(
        action=str(signal.action),
        symbol=str(signal.symbol),
        entry_price=_to_float(metadata.get("entry_price"), default=float(signal.theoretical_price)),
        stop_loss=_to_optional_float(metadata.get("stop_loss")),
        take_profit=_to_optional_float(metadata.get("take_profit")),
        timeframe=str(metadata.get("timeframe") or "1m"),
        metadata={
            "atr": metadata.get("atr"),
            "current_positions": _to_int(metadata.get("current_positions"), default=0),
            "pending_positions": _to_int(metadata.get("pending_positions"), default=0),
        },
    )

    pip_value = _to_float(metadata.get("pip_value"), default=1.0)
    current_exposure = _to_float(metadata.get("current_exposure"), default=0.0)

    sized = risk_engine.calculate_position_size(
        signal=trade_signal,
        account_balance=float(account_balance),
        pip_value=pip_value,
        current_exposure=current_exposure,
    )

    signal.metadata = {
        **metadata,
        "qty": float(sized.position_size),
        "risk_amount": float(sized.risk_amount),
        "risk_percent": float(sized.risk_percent),
        "risk_reward_ratio": (
            float(sized.risk_reward_ratio) if sized.risk_reward_ratio is not None else None
        ),
        "resolved_stop_loss": trade_signal.stop_loss,
    }
    return sized
