"""Live signal sizing bridge.

Trust boundary: signal metadata is UNTRUSTED publisher payload. It may supply
market descriptors of the trade itself (entry price, stop, take profit, ATR,
timeframe, pip value) but never policy: risk limits, position counts, exposure
state or a precomputed quantity all come from deployment/runtime configuration
and the risk engine, never from the signal.
"""

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
    config: RiskConfig | None = None,
    current_positions: int = 0,
    current_exposure: float = 0.0,
    pip_value: float | None = None,
) -> SizedOrder:
    """Size a live signal through the shared RiskEngine.

    Args:
        signal: Incoming live signal. Only its trade-descriptor metadata is
            read; policy (risk limits, quantities) never comes from metadata.
        account_balance: Account equity used for fraction-of-balance sizing.
        config: Trusted RiskConfig from deployment/runtime configuration.
            Defaults to the shared ``RiskConfig()`` defaults.
        current_positions: Open-position count from the portfolio/broker
            source of truth.
        current_exposure: Aggregate open risk as a fraction of balance, from
            the portfolio/broker source of truth.
        pip_value: Optional trusted per-symbol pip value; defaults to the
            RiskEngine's unit contract (1.0 account-currency unit per pip).

    Returns:
        The engine's ``SizedOrder``. ``signal.metadata`` is enriched with the
        resolved quantity/risk figures (including a ``risk_rejected`` flag)
        plus ``risk_config_source="deployment_runtime"``.
    """
    metadata = dict(signal.metadata or {})

    risk_config = config if config is not None else RiskConfig()
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
            "current_positions": _to_int(current_positions, default=0),
            "enforce_stop_direction": True,
        },
    )

    sized = RiskEngine(risk_config).calculate_position_size(
        signal=trade_signal,
        account_balance=float(account_balance),
        pip_value=_to_float(pip_value, default=1.0),
        current_exposure=max(0.0, float(current_exposure)),
    )

    decision = sized.decision
    # Strip untrusted quantity/policy keys so no downstream consumer can mistake
    # payload-supplied values for engine truth.
    for untrusted_key in ("explicit_qty", "risk_sizing_mode"):
        metadata.pop(untrusted_key, None)
    signal.metadata = {
        **metadata,
        "qty": float(sized.position_size),
        "risk_amount": float(sized.risk_amount),
        "risk_percent": float(sized.risk_percent),
        "risk_reward_ratio": (
            float(sized.risk_reward_ratio) if sized.risk_reward_ratio is not None else None
        ),
        "resolved_stop_loss": trade_signal.stop_loss,
        "risk_rejected": not (decision is not None and decision.accepted),
        "risk_rejection_reason": decision.rejection_reason if decision else None,
        "risk_config_source": "deployment_runtime",
    }
    return sized
