"""Regression tests for portfolio-optimizer and signal-replay equity paths.

Covers Cleanup 16-D1 findings:

- F36 — the portfolio "sharpe" must be annualized per the documented
  definition (per-observation mean/std scaled by sqrt(periods per year)),
  not a raw mean/std of per-point returns.
- F39 — signal-replay sizing must account for the unrealized drawdown of
  open positions (sizing equity = realized cash + unrealized MTM), so the
  sizing path and the reported equity path measure the same capital.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from runner.engines.signal_replay_adapter import (
    ReplaySignal,
    SignalReplayAdapter,
    _OpenPosition,
)
from runner.portfolio_optimizer import _portfolio_sharpe_from_weights
from risk.config import RiskConfig


# ─── F36: portfolio sharpe is annualized ────────────────────────────────────


class TestPortfolioSharpeAnnualized:
    def test_daily_curve_sharpe_is_annualized(self):
        """252 daily points of constant per-day drift must give sqrt(252) * sr_obs."""
        rng_values = [1 + 0.001 * math.sin(i) + 0.0002 * i for i in range(253)]
        times = list(range(1_700_000_000, 1_700_000_000 + 253 * 86_400, 86_400))
        equity = np.array([100_000.0 * v for v in rng_values])
        matrix = equity.reshape(-1, 1)
        weights = np.array([1.0])

        sharpe, _ = _portfolio_sharpe_from_weights(weights, matrix)

        returns = np.diff(equity) / equity[:-1]
        per_obs = float(np.mean(returns) / (np.std(returns) + 1e-10))
        assert sharpe == pytest.approx(per_obs * math.sqrt(252), rel=1e-6), (
            "portfolio sharpe must be annualized (per-observation Sharpe "
            "scaled by sqrt(periods per year)), not the raw per-observation value"
        )

    def test_not_just_equal_to_raw_mean_over_std(self):
        """Guard against regressing to the raw ratio: annualization changes scale."""
        equity = np.array([100_000.0 * (1 + 0.001 * math.sin(i)) for i in range(63)])
        sharpe, _ = _portfolio_sharpe_from_weights(np.array([1.0]), equity.reshape(-1, 1))
        returns = np.diff(equity) / equity[:-1]
        raw = float(np.mean(returns) / (np.std(returns) + 1e-10))
        assert abs(sharpe - raw) > 1e-6


# ─── F39: replay sizing sees unrealized drawdown ────────────────────────────


class _FakeSized:
    def __init__(self, quantity: float):
        self.position_size = quantity
        self.risk_amount = 100.0
        self.risk_percent = 0.01


class TestReplaySizingEquity:
    def test_sizing_balance_includes_unrealized_loss_of_open_position(self, monkeypatch):
        """Sizing equity = initial capital + realized PnL + unrealized MTM PnL.

        Deterministic setup: initial 100k; one open LONG at 100.0 (qty 1000)
        now marked at 90.0 → unrealized −10_000. The next sizing call must see
        an account balance of 90_000, not the realized-only 100_000.
        """
        adapter = SignalReplayAdapter(
            risk_config=RiskConfig(max_risk_per_trade=0.01, max_total_exposure=1.0),
            initial_capital=100_000.0,
        )
        captured: dict[str, float] = {}

        class _FakeRiskEngine:
            def calculate_position_size(self, signal, account_balance, pip_value, current_exposure):
                captured["balance"] = account_balance
                return _FakeSized(1.0)

        adapter.risk_engine = _FakeRiskEngine()

        timestamps = pd.date_range("2024-01-01", periods=3, freq="h", tz="UTC")
        frame = pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [100.0, 90.0, 90.0],
                "high": [101.0, 91.0, 91.0],
                "low": [99.0, 89.0, 89.0],
                "close": [100.0, 90.0, 90.0],
                "volume": [1000.0] * 3,
            }
        )
        signal = ReplaySignal(
            timestamp=timestamps[2],
            symbol="PROBE",
            action="buy",
            entry_price=90.0,
        )
        # Open position from the earlier signal, now marked at 90 → −10k.
        adapter._open_probe_position = None
        open_positions = {
            "PROBE": _OpenPosition(
                symbol="PROBE",
                direction="LONG",
                entry_time=timestamps[0],
                entry_bar_index=0,
                entry_price=100.0,
                signal_entry_price=100.0,
                quantity=1000.0,
                stop_loss=None,
                take_profit=None,
                initial_stop_loss=None,
                initial_take_profit=None,
                risk_amount=None,
                risk_percent=None,
                timeframe="1h",
                source="internal",
                signal_time=timestamps[0],
                signal_metadata={},
                last_checked_at=timestamps[1],
            )
        }
        realized_pnl = 0.0  # nothing closed yet

        sizing_balance = adapter._sizing_account_balance(
            open_positions=open_positions,
            realized_pnl=realized_pnl,
            frames={"PROBE": frame},
            as_of=timestamps[2],
        )

        assert sizing_balance == pytest.approx(90_000.0), (
            "sizing must mark open positions to market: 100k capital + 0 "
            "realized − 10k unrealized = 90k; using realized-only 100k would "
            "size during drawdown as if nothing were lost"
        )
