"""Regression validation suite.

Compares golden backtest references against current platform output,
producing structured delta reports for both trades and statistics.
"""

from __future__ import annotations

from validation.regression.runner import (
    RegressionConfig,
    RegressionReporter,
    RegressionResult,
    run_regression,
)

__all__ = ["RegressionResult", "RegressionReporter", "run_regression", "RegressionConfig"]
