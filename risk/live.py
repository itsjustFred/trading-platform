"""Live trade levels and entry-gate rules shared by the live path.

This module holds no sizing function. Live sizing has exactly one entry point,
``live_runner.daemon.size_live_signal_for_submission`` (reached through
``prepare_order_with_risk``), which sizes with the deployment's ``RiskConfig``
through ``RiskEngine`` (#125 removed the unused second entry point
``size_live_signal``).

Trust boundary: signal metadata is UNTRUSTED publisher payload. It may supply
market descriptors of the trade itself (entry price, stop, take profit, ATR,
timeframe) but never policy or instrument facts: risk limits, position counts,
exposure state, a precomputed quantity and the pip value all come from
deployment/runtime configuration, the broker and the risk engine, never from
the signal (#125: a payload pip value scaled the size directly).

:func:`resolve_live_levels` is the single source of the live entry/SL/TP
precedence. The daemon's submission path (``live_runner.daemon``) sizes and
submits from it, so the size and the broker levels cannot disagree.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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


#: Single source precedence for live trade levels (C1: mixed sources sized
#: wrong distances). Entry falls back to theoretical_price; SL/TP accept the
#: same alias keys the daemon resolves (stop_loss/sl_price/stop_price/
#: resolved_stop_loss, take_profit/tp_price) — never bare theoretical math.
_LIVE_SL_KEYS = ("stop_loss", "sl_price", "stop_price", "resolved_stop_loss")
_LIVE_TP_KEYS = ("take_profit", "tp_price")


def _first_metadata_float(metadata: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        try:
            value = _to_optional_float(metadata.get(key))
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from exc
        if value is not None:
            return value
    return None


def resolve_live_levels(signal: LiveSignal) -> tuple[float, float | None, float | None]:
    """Resolve (entry, stop, take) for a live signal under one precedence.

    Entry: metadata entry_price, else theoretical_price. SL/TP: metadata
    aliases only, first present non-None alias wins. One function so every
    sizing path geometry-matches (#125 maintainer decision).

    A missing stop or take profit resolves to ``None``; what a caller does with
    that is its policy (the risk engine may resolve a fallback stop, the daemon
    applies its bracket guard). A present but malformed level (non-numeric,
    boolean, non-finite or ``<= 0``) raises ``ValueError`` naming the key.
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
    if stop is not None and stop <= 0:
        raise ValueError("stop_loss must be > 0")
    if take is not None and take <= 0:
        raise ValueError("take_profit must be > 0")
    return entry, stop, take


#: Defaults for the per-deployment entry-deviation gate settings (#125).
DEFAULT_MAX_ENTRY_DEVIATION_SL_FRACTION = 0.25
DEFAULT_MAX_ENTRY_QUOTE_AGE_SECONDS = 10.0


@dataclass(frozen=True)
class EntryGateSettings:
    """Validated entry-deviation gate settings of one deployment."""

    max_deviation_sl_fraction: float
    max_quote_age_seconds: float


def _gate_setting(settings: Mapping[str, Any], key: str, default: float) -> float:
    if key not in settings:
        return default
    raw = settings[key]
    if raw is None or isinstance(raw, bool):
        raise ValueError(f"{key} must be a number, got {raw!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite, got {raw!r}")
    return value


def entry_gate_settings(settings: Mapping[str, Any] | None) -> EntryGateSettings:
    """Validate a deployment's entry-deviation gate settings (#125).

    ``settings`` is the deployment's ``settings`` mapping (trusted config),
    never signal metadata. Used at deployment creation and again at dispatch,
    so a bad value is refused up front and can never silently disable the gate:

    * ``max_entry_deviation_sl_fraction``: within ``[0, 1]`` (default 0.25). A
      fraction of the SL distance, so ``25`` meant as 25 % is an error.
    * ``max_entry_quote_age_seconds``: finite and ``> 0`` (default 10).

    Raises ``ValueError`` naming the key.
    """
    values = settings or {}
    fraction = _gate_setting(
        values, "max_entry_deviation_sl_fraction", DEFAULT_MAX_ENTRY_DEVIATION_SL_FRACTION
    )
    if not 0.0 <= fraction <= 1.0:
        raise ValueError(
            "max_entry_deviation_sl_fraction must be within [0, 1] (a fraction of "
            f"the SL distance, 0.25 = 25 %), got {fraction!r}"
        )
    max_age = _gate_setting(
        values, "max_entry_quote_age_seconds", DEFAULT_MAX_ENTRY_QUOTE_AGE_SECONDS
    )
    if max_age <= 0:
        raise ValueError(f"max_entry_quote_age_seconds must be > 0, got {max_age!r}")
    return EntryGateSettings(
        max_deviation_sl_fraction=fraction, max_quote_age_seconds=max_age
    )


def entry_deviation_sl_fraction(
    action: str,
    *,
    entry_price: float,
    stop_loss: float,
    quote_price: float,
) -> float:
    """How far the live quote has moved from the signal entry toward the stop.

    Expressed as a fraction of the SL distance ``|entry - stop|`` so one
    threshold works across instruments (#125). Positive means the quote moved
    TOWARD the stop (down for a buy, up for a sell); negative means it moved
    away from the stop (in the trade's favour). ``0.25`` means a quarter of the
    stop distance has been used up before the order is sent.

    ``quote_price`` is the side the order would fill against: the ask for a
    buy, the bid for a sell. The caller picks it.
    """
    if action not in {"buy", "sell"}:
        raise ValueError(f"Unsupported action for entry deviation: {action!r}")
    entry = _to_float(entry_price, default=0.0)
    stop = _to_float(stop_loss, default=0.0)
    quote = _to_float(quote_price, default=0.0)
    if entry <= 0 or stop <= 0 or quote <= 0:
        raise ValueError("entry, stop and quote prices must be > 0")
    distance = entry - stop
    if distance == 0:
        raise ValueError("stop distance must be non-zero")
    if (action == "buy") != (distance > 0):
        raise ValueError(
            f"stop_loss {stop!r} is on the wrong side of entry {entry!r} for a {action}"
        )
    # (entry - quote) / (entry - stop) has the right sign for both sides: for
    # a buy both terms are positive when the quote fell toward the stop, for a
    # sell both are negative when the quote rose toward it.
    return (entry - quote) / distance
