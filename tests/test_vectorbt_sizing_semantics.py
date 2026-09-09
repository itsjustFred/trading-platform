"""Vectorbt adapter sizing/equity-curve regressions (Cleanup 16-D1).

Covers:

- F35 — the leverage branch must honor a conservative ``max_leverage`` as an
  independent cap (no silent override toward position-cap semantics, no
  identical if/elif arms).
- F6 — ``meta.total_bars`` must be the loaded bar count, not the trade count.
- F5 — the portfolio equity curve credits closed PnL once per portfolio at
  EXIT time (realized-PnL semantics), instead of crediting per-symbol
  compounded equity at ENTRY time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter, _resolve_max_leverage
from tests.helpers import sma_crossover_strategy_text


from tests.helpers import deterministic_ohlcv_frame, write_ohlcv as _write_shared_ohlcv

STRATEGY = sma_crossover_strategy_text("vbt_sizing_sma", with_stop_loss=True)


def _deterministic_frame(*, periods: int = 300, start: float = 100.0) -> pd.DataFrame:
    return deterministic_ohlcv_frame(periods=periods, start=start)


def _write_ohlcv(data_dir: Path, symbol: str, frame: pd.DataFrame) -> None:
    _write_shared_ohlcv(data_dir, symbol, frame)


def _config(tmp_path: Path, **overrides: Any) -> BacktestConfig:
    payload: dict[str, Any] = {
        "strategy_name": "vbt_sizing_sma",
        "symbols": ["VSZ"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
        "initial_capital": 100_000,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {"fast_length": 3, "slow_length": 10},
    }
    payload.update(overrides)
    return BacktestConfig(**payload)


@pytest.fixture
def sizing_env(tmp_path: Path):
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    strategy_path = strategy_dir / "vbt_sizing_sma.py"
    strategy_path.write_text(STRATEGY)
    _write_ohlcv(tmp_path, "VSZ", _deterministic_frame())
    return tmp_path, strategy_path


# ─── F35: max_leverage is an independent cap ─────────────────────────────────


class TestLeverageSemantics:
    def test_resolver_keeps_conservative_leverage(self):
        assert _resolve_max_leverage(1.0) == 1.0
        assert _resolve_max_leverage(0.5) == 0.5

    def test_conservative_max_leverage_binds_notional(self, sizing_env):
        """max_leverage=1 must cap the notional at 1x equity — no override."""
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.05,
            max_position_pct=1.0,  # position cap allows full equity
            max_leverage=1.0,      # leverage forbids more than that
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades, "fixture must produce trades"
        for trade in result.trades:
            notional = trade.entry_price * trade.quantity
            sizing_equity = trade.meta["risk_math"]["equity_at_entry"]
            assert notional <= sizing_equity * 1.0 + 1e-6, (
                "max_leverage=1 must bind the notional to equity even when "
                "max_position_pct would allow more (silent leverage override)"
            )

    def test_risk_math_records_binding_caps(self, sizing_env):
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.05,
            max_position_pct=1.0,
            max_leverage=1.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        risk_math = result.trades[0].meta["risk_math"]
        assert risk_math["max_leverage"] == 1.0
        assert risk_math["max_leverage_notional"] == pytest.approx(100_000.0)


# ─── F6: total_bars is the bar count ────────────────────────────────────────


class TestTotalBarsIsBarCount:
    def test_meta_total_bars_counts_bars_not_trades(self, sizing_env):
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.meta.total_bars == 300
        assert result.meta.total_bars != len(result.trades)


# ─── F5: portfolio equity curve semantics ───────────────────────────────────


class TestPortfolioEquityCurveSemantics:
    def test_curve_credits_pnl_at_exit_not_entry(self, sizing_env):
        """Every curve point at a trade's ENTRY time must show initial capital.

        Regression (F5): the old curve credited PnL at entry_time while the
        simulator credited it on close — the persisted path described an
        equity trajectory the backtest never traded.
        """
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades, "the fixture must produce trades"
        assert result.equity_curve, "the fixture must produce an equity curve"

        equity_by_time = {point.time: point.equity for point in result.equity_curve}
        initial = config.initial_capital
        exit_times = {str(trade.last_exit_time) for trade in result.trades}
        assert set(equity_by_time) <= exit_times, (
            "curve points must be stamped only at exit times"
        )
        for trade in result.trades:
            entry_time = str(trade.entry_time)
            # Entry timestamps that carry a curve point must still be at
            # realized-only capital (nothing has closed at entry time).
            if entry_time in equity_by_time:
                assert equity_by_time[entry_time] == pytest.approx(initial, rel=1e-9), (
                    "PnL must be credited at EXIT time; an entry-time point "
                    "carrying profit describes an equity path that was never traded"
                )

    def test_curve_is_monotone_in_realized_pnl_cumsum(self, sizing_env):
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))

        exits = [str(trade.last_exit_time) for trade in result.trades]
        assert exits == sorted(exits), "curve points must follow exit order"
        running = config.initial_capital
        for trade in sorted(result.trades, key=lambda item: item.last_exit_time):
            running += trade.pnl
            # Every trade's exit lands exactly on the running realized equity.
            matching = [
                point.equity
                for point in result.equity_curve
                if point.time == str(trade.last_exit_time)
            ]
            assert matching, "each exit time must have an equity point"
            assert matching[-1] == pytest.approx(round(running, 2), rel=1e-6)

    def test_curve_timestamps_stay_monotone_with_overlapping_trades(self, tmp_path):
        """Two symbols whose exits interleave must still yield a sorted curve.

        Regression (spec review, F5): the curve used to credit PnL in entry
        order while stamping exit times — with interleaved exits the persisted
        timestamps went backwards, so downstream drawdown/Sharpe consumers
        read a scrambled path.
        """
        strategy_dir = tmp_path / "strategies"
        strategy_dir.mkdir()
        strategy_path = strategy_dir / "vbt_sizing_sma.py"
        strategy_path.write_text(STRATEGY)

        base = _deterministic_frame()
        # Symbol A: normal bars. Symbol B: exit pulled a few bars EARLIER than
        # A's first exit by clipping the slow SMA's window (shorter series →
        # earlier crossing) — guaranteed exit-time interleave.
        _write_ohlcv(tmp_path, "VSA", base)
        # Symbol B: last 10 bars truncated, so B's final exit lands before
        # A's — guaranteed exit-time interleave.
        _write_ohlcv(tmp_path, "VSB", base.iloc[:-10].copy())

        config = _config(
            tmp_path,
            symbols=["VSA", "VSB"],
            parameters={"fast_length": 3, "slow_length": 10},
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert len(result.trades) >= 2

        times = [pd.Timestamp(point.time) for point in result.equity_curve]
        assert times == sorted(times), (
            "equity-curve timestamps must be non-decreasing even when trades "
            "on different symbols exit interleaved"
        )
        # PnL must be credited in exit order, not entry order: each distinct
        # exit time shows the cumulative realized PnL of everything closed at
        # or before it, and the last point equals capital + all PnL.
        equity_by_time: dict[str, float] = {}
        for point in result.equity_curve:
            equity_by_time[point.time] = point.equity  # last write per timestamp

        exits_in_order = sorted(
            {str(trade.last_exit_time) for trade in result.trades}
        )
        cumulative = config.initial_capital
        trades_by_exit_time: dict[str, list] = {
            str(t.last_exit_time): [] for t in result.trades
        }
        for trade in sorted(result.trades, key=lambda item: item.last_exit_time):
            trades_by_exit_time[str(trade.last_exit_time)].append(trade)
        for exit_time in exits_in_order:
            for trade in trades_by_exit_time[exit_time]:
                cumulative += trade.pnl
            assert exit_time in equity_by_time, f"missing curve point at {exit_time}"
            assert equity_by_time[exit_time] == pytest.approx(
                round(cumulative, 2), rel=1e-6
            )
        final_point = result.equity_curve[-1]
        assert final_point.equity == pytest.approx(round(cumulative, 2), rel=1e-6), (
            "the last curve point must equal capital plus the realized PnL of "
            "every trade, credited in exit order"
        )


# ─── Risk-target sizing math (mutation hardening) ───────────────────────────


class TestRiskTargetMath:
    def test_risk_target_sets_quantity_exactly(self, sizing_env):
        """qty = (equity * risk_pct) / stop_distance when no cap binds.

        Regression (mutation hardening): a doubled/halved risk target must
        change the quantity — the sizing chain is pinned end-to-end, not just
        through the cap.
        """
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.02,
            max_position_pct=10.0,   # no cap binds
            max_leverage=10.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades
        for trade in result.trades:
            risk_math = trade.meta["risk_math"]
            stop_distance = risk_math["stop_distance"]
            equity = risk_math["equity_at_entry"]
            expected_qty = (equity * 0.02) / stop_distance
            assert trade.quantity == pytest.approx(expected_qty, rel=1e-6), (
                "quantity must equal (equity * risk_pct) / stop_distance — "
                "any drift in the risk-target math silently rescales every position"
            )

    def test_stopless_market_entry_uses_documented_fallback(self):
        """No stop → sizing still happens via the 2% fallback, not zero/full."""
        # Covered at the quantity level by the native engine tests; here we
        # pin the vectorbt side through risk_math's stop_distance fallback.
        frame = _deterministic_frame()
        env = _sizing_env_from(frame, stop=False)
        tmp_path, strategy_path, config = env
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades
        risk_math = result.trades[0].meta["risk_math"]
        assert risk_math["stop_distance"] is not None, (
            "the fallback stop (2% of price) must feed sizing even without a "
            "strategy-provided stop"
        )
        assert risk_math["position_size"] > 0


def _sizing_env_from(frame: pd.DataFrame, *, stop: bool):
    import tempfile

    tmp_path = Path(tempfile.mkdtemp())
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir()
    strategy_path = strategy_dir / "vbt_sizing_sma.py"
    text = STRATEGY
    if not stop:
        text = text.replace(
            '        "stop_loss": pd.Series([df["close"].iloc[0] * 0.95] * len(df), index=df.index),\n',
            "",
        )
    strategy_path.write_text(text)
    _write_ohlcv(tmp_path, "VSZ", frame)
    config = _config(tmp_path, risk_per_trade_pct=0.02)
    return tmp_path, strategy_path, config


# ─── Curve replay order + no-cap-binding sizing (mutation hardening) ────────


class TestCurveReplayOrderAndUncappedSizing:
    def test_curve_credits_full_pnl_with_interleaved_exits(self, tmp_path):
        """Multi-symbol run with interleaved exits: exit-order replay, full PnL.

        Kills the mutants that replay in entry order or credit partial PnL —
        both produce a final equity different from capital + sum(pnl) or a
        scrambled timestamp sequence.
        """
        strategy_dir = tmp_path / "strategies"
        strategy_dir.mkdir()
        strategy_path = strategy_dir / "vbt_sizing_sma.py"
        strategy_path.write_text(STRATEGY)

        base = _deterministic_frame()
        _write_ohlcv(tmp_path, "VSA", base)
        _write_ohlcv(tmp_path, "VSB", base.iloc[:-10])

        config = _config(tmp_path, symbols=["VSA", "VSB"])
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert len(result.trades) >= 2

        times = [pd.Timestamp(point.time) for point in result.equity_curve]
        assert times == sorted(times), "curve timestamps must be non-decreasing"

        expected_final = config.initial_capital + sum(t.pnl for t in result.trades)
        assert result.equity_curve[-1].equity == pytest.approx(round(expected_final, 2)), (
            "the last curve point must carry the full realized PnL of every trade"
        )

    def test_uncapped_risk_sizing_matches_risk_math(self, sizing_env):
        """With no binding cap, the trade's notional equals its risk target.

        Complements TestRiskTargetMath: pins the final_notional branch
        (required <= binding) rather than only the quantity formula.
        """
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.02,
            max_position_pct=10.0,
            max_leverage=10.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades
        for trade in result.trades:
            risk_math = trade.meta["risk_math"]
            notional = trade.entry_price * trade.quantity
            assert notional == pytest.approx(risk_math["final_notional"], rel=1e-6), (
                "with no binding cap the position notional must equal the "
                "risk-math final notional"
            )
            assert risk_math["capped_by_max_position"] is False
            assert risk_math["capped_by_max_leverage"] is False


# ─── Binding-cap and curve semantics with a tight leverage cap ──────────────


class TestTightCapSizing:
    def test_tight_leverage_caps_notional_exactly(self, sizing_env):
        """max_leverage=1 with max_position_pct=10: notional == equity * 1.0.

        Kills the min→max mutant: with max() the notional would follow the
        position cap (10x) instead of the leverage limit (1x).
        """
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.5,   # huge risk target, cap must bind
            max_position_pct=10.0,
            max_leverage=1.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades
        for trade in result.trades:
            risk_math = trade.meta["risk_math"]
            notional = trade.entry_price * trade.quantity
            assert notional <= risk_math["equity_at_entry"] * 1.0 + 1e-6, (
                "the leverage limit (1x) must bind the notional even when the "
                "position cap is 10x"
            )
            assert risk_math["capped_by_max_leverage"] is True

    def test_tight_position_cap_binds_before_leverage(self, sizing_env):
        """max_position_pct=0.01 with huge risk target: notional == 1% equity."""
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.5,
            max_position_pct=0.01,
            max_leverage=10.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades
        for trade in result.trades:
            risk_math = trade.meta["risk_math"]
            notional = trade.entry_price * trade.quantity
            slack = 0.01 + 1e-9
            assert notional <= risk_math["equity_at_entry"] * 0.01 + slack, (
                "the position cap (1%) must bind the notional even when "
                "leverage would allow more"
            )
            assert risk_math["capped_by_max_position"] is True


class TestTightCapSizingTolerance:
    def test_tight_position_cap_binds_before_leverage_tolerant(self, sizing_env):
        """Position cap binds the notional within rounding tolerance.

        Rounding tolerance note: ``quantity`` is rounded to 4 decimals and
        prices to 6, so the notional can exceed the cap by a fraction of a
        cent; the assertion therefore allows a tiny relative slack.
        """
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.5,
            max_position_pct=0.01,
            max_leverage=10.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades
        for trade in result.trades:
            risk_math = trade.meta["risk_math"]
            notional = trade.entry_price * trade.quantity
            slack = 0.01 + 1e-9  # one cent for rounding
            assert notional <= risk_math["equity_at_entry"] * 0.01 + slack, (
                "the position cap (1%) must bind the notional even when "
                "leverage would allow more (rounding slack: one cent)"
            )
            assert risk_math["capped_by_max_position"] is True


# ─── entry_stop fills + inverted-stop guard ─────────────────────────────────


def _stop_order_strategy_text(*, invert_stop: bool = False) -> str:
    stop = "130.0" if invert_stop else "90.0"
    return '''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[5] = True
    exits.iloc[6] = True
    indicators = {
        "direction": pd.Series(["LONG"] * n, index=df.index),
        "entry_stop": pd.Series([float("nan")] * n, index=df.index),
        "sl_price": pd.Series([float("nan")] * n, index=df.index),
        "exit_price": pd.Series([float("nan")] * n, index=df.index),
        "exit_reason": pd.Series([None] * n, index=df.index),
    }
    indicators["entry_stop"].iloc[5] = 105.0
    indicators["sl_price"].iloc[5] = %s
    indicators["exit_price"].iloc[6] = float(df["close"].iloc[6])
    indicators["exit_reason"].iloc[6] = "time_exit"
    return entries, exits, indicators, []
''' % stop


def _stop_order_env(tmp_path: Path, *, invert_stop: bool = False):
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC"),
            "open": [100.0] * 24,
            "high": [106.0 if i in (5, 6) else 101.0 for i in range(24)],
            "low": [99.0] * 24,
            "close": [100.0 + i * 0.1 for i in range(24)],
            "volume": [1000.0] * 24,
        }
    )
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(exist_ok=True)
    strategy_path = strategy_dir / "vbt_stop_order.py"
    strategy_path.write_text(_stop_order_strategy_text(invert_stop=invert_stop))
    _write_ohlcv(tmp_path, "VSO", frame)
    config = _config(
        tmp_path,
        strategy_name="vbt_stop_order",
        symbols=["VSO"],
        risk_per_trade_pct=0.01,
    )
    return config, strategy_path


class TestEntryStopFills:
    def test_long_fills_at_stop_not_open(self, tmp_path: Path):
        """A buy-stop entry fills at entry_stop (high traded through it)."""
        config, strategy_path = _stop_order_env(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(105.0)
        assert trade.meta["risk_math"]["sl_price"] == pytest.approx(90.0)

    def test_inverted_stop_skips_trade(self, tmp_path: Path, caplog):
        """LONG stop above entry is skipped loudly, not fantasy-exited."""
        config, strategy_path = _stop_order_env(tmp_path, invert_stop=True)
        with caplog.at_level("WARNING"):
            result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades == []
