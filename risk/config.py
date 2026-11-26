from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# All exposure values are FRACTIONS OF ACCOUNT EQUITY:
#   - max_risk_per_trade: maximum loss on a single trade (as a fraction of
#     account balance) if the stop is hit. Bounded to [0, 1].
#   - max_total_exposure: aggregate open risk across all positions (same unit).
#     Bounded to [0, 1].
#   - max_notional_per_trade: upper bound on a single position's notional value
#     as a fraction of account balance. Values above 1 are leverage and are
#     bounded per sizing path by the broker's maximum leverage for the account
#     and asset (#240); an undeterminable broker maximum fails closed to 1x —
#     see ``effective_max_notional_per_trade``.
# Sizing quantities (SizedOrder.position_size) are in ACCOUNT-CURRENCY UNITS of
# the traded symbol (shares/lots). Never mix the two unit domains; conversions
# happen only inside RiskEngine.calculate_position_size.
UNIT_FRACTION = "fraction_of_balance"
UNIT_QUANTITY = "account_currency_units_of_symbol"


def _settings_float(
    value: Any, *, field_name: str, minimum: float | None = None
) -> float:
    """Coerce one deployment setting to a finite float.

    Kept separate from ``RiskConfig._finite_float``: the deployment-settings
    messages ("Invalid <field>: <value>") name the offending value for an
    operator fixing a live deployment row, and the numeric minimum is checked
    before the field's own range rule runs in ``__post_init__``.
    """
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return parsed


def _settings_int(value: Any, *, field_name: str, minimum: int | None = None) -> int:
    """Coerce one deployment setting to an integer (see ``_settings_float``)."""
    if isinstance(value, bool):
        raise ValueError(f"Invalid {field_name}: {value!r}")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {field_name}: {value!r}") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"{field_name} must be an integer")
    parsed = int(numeric)
    if minimum is not None and parsed < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return parsed


@dataclass(slots=True)
class RiskConfig:
    max_risk_per_trade: float = 0.01
    max_total_exposure: float = 0.05
    max_positions: int = 5
    default_sl_atr_multiplier: float = 2.0
    fallback_stop_pct: float = 0.02
    max_notional_per_trade: float = 0.20
    allow_fallback_stop: bool = True
    reject_missing_stop: bool = False

    def __post_init__(self) -> None:
        self.max_risk_per_trade = self._finite_float(
            self.max_risk_per_trade, "max_risk_per_trade"
        )
        self.max_total_exposure = self._finite_float(
            self.max_total_exposure, "max_total_exposure"
        )
        self.max_positions = self._positive_int(self.max_positions, "max_positions")
        self.default_sl_atr_multiplier = self._finite_float(
            self.default_sl_atr_multiplier, "default_sl_atr_multiplier"
        )
        self.fallback_stop_pct = self._finite_float(
            self.fallback_stop_pct, "fallback_stop_pct"
        )
        self.max_notional_per_trade = self._finite_float(
            self.max_notional_per_trade, "max_notional_per_trade"
        )
        self.allow_fallback_stop = self._bool(self.allow_fallback_stop, "allow_fallback_stop")
        self.reject_missing_stop = self._bool(self.reject_missing_stop, "reject_missing_stop")

        if not 0 < self.max_risk_per_trade <= 1:
            raise ValueError("max_risk_per_trade must be > 0 and <= 1")
        if not 0 < self.max_total_exposure <= 1:
            raise ValueError("max_total_exposure must be > 0 and <= 1")
        if self.max_positions <= 0:
            raise ValueError("max_positions must be > 0")
        if self.default_sl_atr_multiplier <= 0:
            raise ValueError("default_sl_atr_multiplier must be > 0")
        if self.fallback_stop_pct <= 0 or self.fallback_stop_pct >= 1:
            raise ValueError("fallback_stop_pct must be between 0 and 1")
        if not 0 < self.max_notional_per_trade:
            raise ValueError("max_notional_per_trade must be > 0")

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any] | None) -> RiskConfig:
        """Build the RiskConfig a deployment's ``settings`` describe.

        The one place that maps deployment settings to ``RiskConfig``
        (improvement-run G03): the daemon used to re-type every default as its
        own literal, so a changed dataclass default silently stopped applying
        to live deployments. Missing keys fall back to the dataclass field
        defaults; ``risk_per_trade_pct`` is the documented alias for
        ``max_risk_per_trade`` (the canonical key wins when both are present).

        Values are coerced with deployment-settings messages ("Invalid
        <field>: <value>" for a non-numeric value) and then validated by
        ``__post_init__`` — a value that passes the numeric minimum can still
        be rejected by the field's own range rule.
        """
        values = settings or {}
        defaults = cls()
        return cls(
            max_risk_per_trade=_settings_float(
                values.get(
                    "max_risk_per_trade",
                    values.get("risk_per_trade_pct", defaults.max_risk_per_trade),
                ),
                field_name="max_risk_per_trade",
                minimum=0.0,
            ),
            max_total_exposure=_settings_float(
                values.get("max_total_exposure", defaults.max_total_exposure),
                field_name="max_total_exposure",
                minimum=0.0,
            ),
            max_positions=_settings_int(
                values.get("max_positions", defaults.max_positions),
                field_name="max_positions",
                minimum=1,
            ),
            default_sl_atr_multiplier=_settings_float(
                values.get(
                    "default_sl_atr_multiplier",
                    defaults.default_sl_atr_multiplier,
                ),
                field_name="default_sl_atr_multiplier",
                minimum=0.0,
            ),
            fallback_stop_pct=_settings_float(
                values.get("fallback_stop_pct", defaults.fallback_stop_pct),
                field_name="fallback_stop_pct",
                minimum=0.0,
            ),
            max_notional_per_trade=_settings_float(
                values.get(
                    "max_notional_per_trade", defaults.max_notional_per_trade
                ),
                field_name="max_notional_per_trade",
                minimum=0.0,
            ),
            allow_fallback_stop=values.get(
                "allow_fallback_stop", defaults.allow_fallback_stop
            ),
            reject_missing_stop=values.get(
                "reject_missing_stop", defaults.reject_missing_stop
            ),
        )

    @staticmethod
    def _finite_float(value: Any, field_name: str) -> float:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be numeric")
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} must be numeric") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"{field_name} must be finite")
        return parsed

    @staticmethod
    def _positive_int(value: Any, field_name: str) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{field_name} must be an integer")
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if not math.isfinite(numeric) or not numeric.is_integer():
            raise ValueError(f"{field_name} must be an integer")
        return int(numeric)

    @staticmethod
    def _bool(value: object, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        raise ValueError(f"{field_name} must be boolean")


def effective_max_leverage(broker_max_leverage: float | None) -> float:
    """The broker's maximum leverage for sizing, fail-closed to 1x (#240).

    ``broker_max_leverage`` is the broker's maximum leverage for the account
    and asset; ``None`` or any unusable value means the maximum cannot be
    determined and fails closed to 1x — never a guessed ceiling.
    """
    leverage = 1.0
    if broker_max_leverage is not None and not isinstance(broker_max_leverage, bool):
        try:
            candidate = float(broker_max_leverage)
        except (TypeError, ValueError, OverflowError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            leverage = candidate
    return leverage


def effective_max_notional_per_trade(
    configured: float, broker_max_leverage: float | None
) -> float:
    """Effective per-trade notional ceiling (#240): ``min(configured, broker max)``.

    ``configured`` is ``RiskConfig.max_notional_per_trade`` — a fraction of
    account equity that may exceed 1 (leverage). ``broker_max_leverage`` is the
    broker's maximum leverage for the account and asset; ``None`` or any
    unusable value means the maximum cannot be determined and fails closed to
    1x — never a guessed ceiling. The result is always finite and positive for
    a valid ``RiskConfig``; callers pass it as the ``max_notional_per_trade``
    caller ceiling of ``RiskEngine.calculate_position_size``, which re-validates
    it.
    """
    return min(float(configured), effective_max_leverage(broker_max_leverage))
