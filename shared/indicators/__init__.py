"""Shared technical indicators (issue #317).

Consumed by strategies, signal bots, replay/trailing paths and the viewer.
See ``presets.py`` for the named parity profiles that reproduce each
existing per-strategy/bot implementation.

Hot sequential kernels may run compiled with numba when it is importable
(issue #378); the pure Python/NumPy kernels remain the reference and the
fallback — see ``_numba.py`` and ``TP_DISABLE_NUMBA``.
"""

from .core import (
    BollingerBands,
    MacdResult,
    SupertrendResult,
    atr,
    atr_from_tr,
    bbands,
    ema,
    lagged,
    macd,
    rma,
    rsi,
    sma,
    supertrend,
    true_range,
)
from .presets import PRESETS

__all__ = [
    "BollingerBands",
    "MacdResult",
    "PRESETS",
    "SupertrendResult",
    "atr",
    "atr_from_tr",
    "bbands",
    "ema",
    "lagged",
    "macd",
    "rma",
    "rsi",
    "sma",
    "supertrend",
    "true_range",
]
