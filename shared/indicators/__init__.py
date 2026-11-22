"""Shared technical indicators (issue #317).

Phase P0 adds the module and its tests only; no strategy, bot, engine or
viewer code consumes it yet. See ``presets.py`` for the named parity
profiles that reproduce each existing per-strategy/bot implementation.
"""

from .core import (
    SupertrendResult,
    atr,
    atr_from_tr,
    ema,
    lagged,
    rma,
    rsi,
    sma,
    supertrend,
    true_range,
)
from .presets import PRESETS

__all__ = [
    "PRESETS",
    "SupertrendResult",
    "atr",
    "atr_from_tr",
    "ema",
    "lagged",
    "rma",
    "rsi",
    "sma",
    "supertrend",
    "true_range",
]
