"""Paper-readiness validation for strategy backtest alignment.

This package provides models and a validator that determine whether the
platform's backtest output is sufficiently aligned with a reference backtest
to justify moving to paper trading.
"""

from validation.readiness.models import (
    PaperReadinessLevel,
    ReadinessCheck,
    ReadinessConfig,
    ReadinessReport,
)
from validation.readiness.validator import ReadinessValidator

__all__ = [
    "PaperReadinessLevel",
    "ReadinessCheck",
    "ReadinessConfig",
    "ReadinessReport",
    "ReadinessValidator",
]
