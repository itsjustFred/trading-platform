"""Live signal sizing bridge.

Trust boundary: signal metadata is UNTRUSTED publisher payload. It may supply
market descriptors of the trade itself (entry price, stop, take profit, ATR,
timeframe, pip value) but never policy: risk limits, position counts, exposure
state or a precomputed quantity all come from deployment/runtime configuration
and the risk engine, never from the signal.
"""

from __future__ import annotations

import math
from typing import Any

from risk.config import RiskConfig
from risk.engine import RiskEngine
from risk.models import SizedOrder, TradeSignal
from shared.models import LiveSignal


def _to_float(value: Any, *, default: float) -> float:
    if value is None:
        return float(default)
    if isinstance(value, bool):
        raise ValueError("numeric value must not be boolean")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"Numeric value must be finite: {value!r}")
    return parsed


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _to_float(value, default=0.0)


def _to_int(value: Any, *, default: int) -> int:
    if value is None:
        return int(default)
    if isinstance(value, bool):
        raise ValueError("integer value must not be boolean")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid integer value: {value!r}") from exc
    if not math.isfinite(parsed) or not parsed.is_integer():
        raise ValueError(f"Integer value is not integral: {value!r}")
    return int(parsed)


#: Single source precedence for live trade levels (C1: mixed sources sized
#: wrong distances). Entry falls back to theoretical_price; SL/TP accept the
#: same alias keys the daemon resolves (stop_loss/sl_price/stop_price/
#: resolved_stop_loss, take_profit/tp_price) — never bare theoretical math.
_LIVE_SL_KEYS = ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
_LIVE_TP_KEYS = ("take_profit", "tp_price")


def _first_metadata_float(metadata: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _to_optional_float(metadata.get(key))
        if value is not None:
            return value
    return None


def resolve_live_levels(signal: LiveSignal) -> tuple[float, float | None, float | None]:
    """Resolve (entry, stop, take) for a live signal under one precedence.

    Entry: metadata entry_price, else theoretical_price. SL/TP: metadata
    aliases only. One function so every sizing path geometry-matches.
    """
    metadata = signal.metadata or {}
    try:
        entry = _to_float(
            metadata["entry_price"] if "entry_price" in metadata else signal.theoretical_price,
            default=float(signal.theoretical_price),
        )
    except ValueError as exc:
        raise ValueError(f"entry_price: {exc}") from exc
    if entry <= 0:
        raise ValueError("entry_price must be > 0")
    stop = _first_metadata_float(metadata, _LIVE_SL_KEYS)
    take = _first_metadata_float(metadata, _LIVE_TP_KEYS)
    return entry, stop, take


def size_live_signal(
    signal: LiveSignal,
    *,
    account_balance: float,
    config: RiskConfig | None = None,
    current_positions: int = 0,
    current_exposure: float = 0.0,
    pip_value: float | None = None,
    reduce_only: bool = False,
    reduce_only_qty: float = 0.0,
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
        reduce_only: Pass True only from trusted code (e.g. the daemon's
            exit-only path) when the signal must flatten an existing position.
            The quantity is then taken from ``reduce_only_qty`` — never from
            signal metadata — and clamped to the engine's notional ceiling.

    Returns:
        The engine's ``SizedOrder``. ``signal.metadata`` is enriched with the
        resolved quantity/risk figures (including a ``risk_rejected`` flag)
        plus ``risk_config_source="deployment_runtime"``.
    """
    metadata = dict(signal.metadata or {})
    if not isinstance(reduce_only, bool):
        raise ValueError("reduce_only must be boolean")

    risk_config = config if config is not None else RiskConfig()
    balance = _to_float(account_balance, default=0.0)
    if balance <= 0:
        raise ValueError("account_balance must be > 0")
    if reduce_only:
        reduce_qty = _to_float(reduce_only_qty, default=0.0)
        if reduce_qty < 0:
            raise ValueError("reduce_only_qty must be >= 0")
        # Trusted flatten: honor the caller's exact position size, but still
        # apply the notional ceiling so even internal paths cannot overshoot.
        max_notional = balance * risk_config.max_notional_per_trade
        entry, _, _ = resolve_live_levels(signal)
        if entry > 0 and reduce_qty * entry > max_notional:
            reduce_qty = max_notional / entry
        for untrusted_key in ("explicit_qty", "risk_sizing_mode"):
            metadata.pop(untrusted_key, None)
        _, reduce_stop, reduce_take = resolve_live_levels(signal)
        signal.metadata = {
            **metadata,
            "qty": float(reduce_qty),
            "risk_amount": 0.0,
            "risk_percent": 0.0,
            "risk_reward_ratio": None,
            "resolved_stop_loss": reduce_stop,
            "risk_rejected": reduce_qty <= 0,
            "risk_rejection_reason": None if reduce_qty > 0 else "reduce_only_zero_qty",
            "risk_config_source": "deployment_runtime",
            "risk_sizing_mode": "reduce_only" if reduce_qty > 0 else "reduce_only_blocked",
        }
        return SizedOrder(
            signal=TradeSignal(
                action=str(signal.action),
                symbol=str(signal.symbol),
                entry_price=entry if entry > 0 else float(signal.theoretical_price),
                stop_loss=reduce_stop,
                take_profit=reduce_take,
                timeframe=str(metadata.get("timeframe") or "1m"),
            ),
            position_size=float(reduce_qty),
            risk_amount=0.0,
            risk_percent=0.0,
            risk_reward_ratio=None,
        )

    entry_price, level_stop, level_take = resolve_live_levels(signal)
    current_positions_value = _to_int(current_positions, default=0)
    if current_positions_value < 0:
        raise ValueError("current_positions must be >= 0")
    current_exposure_value = _to_float(current_exposure, default=0.0)
    if current_exposure_value < 0:
        raise ValueError("current_exposure must be >= 0")
    trade_signal = TradeSignal(
        action=str(signal.action),
        symbol=str(signal.symbol),
        entry_price=entry_price,
        stop_loss=level_stop,
        take_profit=level_take,
        timeframe=str(metadata.get("timeframe") or "1m"),
        metadata={
            "atr": metadata.get("atr"),
            "current_positions": current_positions_value,
            "enforce_stop_direction": True,
        },
    )

    sized = RiskEngine(risk_config).calculate_position_size(
        signal=trade_signal,
        account_balance=balance,
        pip_value=_to_float(pip_value, default=1.0),
        current_exposure=current_exposure_value,
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
        "stop_source": decision.metadata.get("stop_source") if decision else None,
        "stop_price_provenance": decision.metadata.get("stop_source") if decision else None,
        "risk_rejected": not (decision is not None and decision.accepted),
        "risk_rejection_reason": decision.rejection_reason if decision else None,
        "risk_config_source": "deployment_runtime",
    }
    return sized
