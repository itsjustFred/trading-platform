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

from runner import portfolio_optimizer
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
        adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, 
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


# ─── Mutation hardening: annualization cadence calibration ─────────────────


class TestAnnualizationCadenceCalibration:
    def test_calendar_daily_maps_to_252(self):
        """Calendar-daily cadence must map onto the 252 trading convention.

        Kills the mutant that skips the 350-380 mapping: a calendar-daily
        curve would then annualize with 365.25 instead of 252, inflating the
        Sharpe by ~20% versus runner.stats' daily Sharpe.
        """
        times = list(range(1_700_000_000, 1_700_000_000 + 300 * 86_400, 86_400))
        equity = np.array([100_000.0 * (1 + 0.0005 * math.sin(i)) for i in range(300)])
        portfolio_optimizer._record_portfolio_timestamps(
            _curves_from_times(times), times
        )
        try:
            sharpe, _ = _portfolio_sharpe_from_weights(np.array([1.0]), equity.reshape(-1, 1))
            returns = np.diff(equity) / equity[:-1]
            per_obs = float(np.mean(returns) / (np.std(returns) + 1e-10))
            assert sharpe == pytest.approx(per_obs * math.sqrt(252), rel=1e-6), (
                "calendar-daily curves must annualize with 252, not the "
                "calendar count"
            )
        finally:
            portfolio_optimizer._LAST_PORTFOLIO_TIMESTAMPS.clear()

    def test_intraday_cadence_scales_uncapped(self):
        """Hourly cadence scales periods-per-year with no artificial ceiling.

        Kills the ceiling mutant (min(ppy, 252*4) instead of ppy): an hourly
        curve must annualize with the true calendar count (~8766), not a
        clamped 1008 — the cap understated intraday Sharpe by
        sqrt(8766/1008) ≈ 2.9x relative to the daily-resampled convention.
        """
        times = list(range(1_700_000_000, 1_700_000_000 + 300 * 3600, 3600))
        equity = np.array([100_000.0 * (1 + 0.0005 * math.sin(i)) for i in range(300)])
        portfolio_optimizer._record_portfolio_timestamps(
            _curves_from_times(times), times
        )
        try:
            equity_matrix = equity.reshape(-1, 1)
            periods = portfolio_optimizer._infer_portfolio_periods_per_year(
                equity_matrix
            )
            assert periods == pytest.approx(365.25 * 24), (
                "hourly curves must annualize with the true hourly period "
                "count (~8766) — a 1008-ish value means the 252*4 ceiling "
                "was reintroduced, 252 means the sub-daily scaling was dropped"
            )
        finally:
            portfolio_optimizer._LAST_PORTFOLIO_TIMESTAMPS.clear()


def _curves_from_times(times: list[int]):
    return [
        [
            portfolio_optimizer.StrategyEquityPoint(time=ts, equity=100_000.0)
            for ts in times
        ]
        for _ in range(2)
    ]


class TestMultiSymbolSettlement:
    """CodeRabbit round: protective exits settle for ALL symbols, not just
    the signaled one, before sizing equity is computed."""

    def test_cross_symbol_stop_out_realizes_before_sizing(self):
        """A stop-out on ANOTHER symbol must reduce the sizing balance.

        Deterministic setup: PROBE (long 1000 @ 100) stopped at 80 → realized
        −20_000; the open position is removed, so sizing for a GOLD1 signal
        sees capital − 20k realized and no PROBE MTM leg.
        """
        adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, 
            risk_config=RiskConfig(max_risk_per_trade=0.01, max_total_exposure=1.0),
            initial_capital=100_000.0,
        )
        captured: dict[str, object] = {}

        class _FakeRiskEngine:
            def calculate_position_size(self, signal, account_balance, pip_value, current_exposure):
                captured["balance"] = account_balance
                captured["positions"] = len(signal.metadata.get("current_positions", {}))
                return _FakeSized(1.0)

        adapter.risk_engine = _FakeRiskEngine()

        timestamps = pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC")

        def make_frame(closes):
            return pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "open": closes,
                    "high": [c + 1.0 for c in closes],
                    "low": [c - 1.0 for c in closes],
                    "close": closes,
                    "volume": [1000.0] * len(closes),
                }
            )

        # PROBE: crashes through the 80 stop on bar 2 (t=02:00).
        probe_frame = make_frame([100.0, 95.0, 79.0, 79.0, 79.0, 79.0])
        gold_frame = make_frame([100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
        frames = {"PROBE": probe_frame, "GOLD1": gold_frame}

        open_positions = {
            "PROBE": _OpenPosition(
                symbol="PROBE",
                direction="LONG",
                entry_time=timestamps[0],
                entry_bar_index=0,
                entry_price=100.0,
                signal_entry_price=100.0,
                quantity=1000.0,
                stop_loss=80.0,
                take_profit=None,
                initial_stop_loss=80.0,
                initial_take_profit=None,
                risk_amount=None,
                risk_percent=None,
                timeframe="1h",
                source="internal",
                signal_time=timestamps[0],
                signal_metadata={},
                last_checked_at=timestamps[0],
            )
        }

        # Post-settlement state (the run loop pops the stopped-out symbol
        # BEFORE sizing): PROBE removed, its −20k stop-out realized.
        sizing_balance = adapter._sizing_account_balance(
            open_positions={},
            realized_pnl=-20_000.0,
            frames=frames,
            as_of=timestamps[3],
        )
        assert sizing_balance == pytest.approx(80_000.0), (
            "sizing must see the realized stop-out loss exactly once; if the "
            "stopped-out position were still open in the balance, the loss "
            "would double-count (−40k total)"
        )
        # Pre-settlement state (the bug): position still open → MTM leg adds
        # another −21k, i.e. the loss double-counts.
        pre_settlement = adapter._sizing_account_balance(
            open_positions=open_positions,
            realized_pnl=-20_000.0,
            frames=frames,
            as_of=timestamps[3],
        )
        assert pre_settlement == pytest.approx(59_000.0), (
            "sanity: keeping the stopped-out position open double-counts the "
            "loss — which is exactly why the loop must settle first"
        )


class TestCoarserThanDailyCadence:
    """CodeRabbit round: weekly/biweekly cadences scale, they don't map to 252."""

    def test_weekly_cadence_annualizes_with_52(self):
        """A weekly equity curve must annualize with ~52, not 252.

        Pins the CodeRabbit-discussed boundary: mapping anything coarser than
        daily onto 252 would overstate a weekly Sharpe by sqrt(252/52) ≈ 2.2x.
        """
        times = list(range(1_700_000_000, 1_700_000_000 + 300 * 7 * 86_400, 7 * 86_400))
        portfolio_optimizer._record_portfolio_timestamps(
            _curves_from_times(times), times
        )
        try:
            equity = np.array([100_000.0 * (1 + 0.002 * math.sin(i)) for i in range(300)])
            matrix = equity.reshape(-1, 1)
            sharpe, _ = _portfolio_sharpe_from_weights(np.array([1.0]), matrix)
            returns = np.diff(equity) / equity[:-1]
            per_obs = float(np.mean(returns) / (np.std(returns) + 1e-10))
            assert sharpe == pytest.approx(per_obs * math.sqrt(52.178571), rel=1e-3), (
                "weekly curves must annualize with the weekly period count "
                "(~52), not the 252 daily convention"
            )
        finally:
            portfolio_optimizer._LAST_PORTFOLIO_TIMESTAMPS.clear()
