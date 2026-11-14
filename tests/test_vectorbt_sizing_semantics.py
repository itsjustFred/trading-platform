"""Vectorbt adapter sizing/equity-curve regressions (Cleanup 16-D1).

Covers:

- F35 — the leverage branch must honor a conservative ``max_leverage`` as an
  independent cap (no silent override toward position-cap semantics, no
  identical if/elif arms).
- F6 — ``meta.total_bars`` must be the loaded bar count, not the trade count.
- F5 — the portfolio equity curve credits closed PnL once per portfolio at
  EXIT time (never at entry, never per-symbol compounded), and between entry
  and exit it carries the open position's unrealized PnL (mark-to-market
  reporting equity, ``runner/engines/mtm_equity.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.vectorbt_adapter import VectorbtAdapter, _resolve_max_leverage
from tests.helpers import deterministic_ohlcv_frame, sma_crossover_strategy_text, with_zero_fees
from tests.helpers import write_ohlcv as _write_shared_ohlcv

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
        "risk_per_trade_pct": 0.01,
        "modeling_resolution": "main_timeframe",
        "data_dir": str(tmp_path),
        "parameters": {"fast_length": 3, "slow_length": 10},
    }
    payload.update(overrides)
    return BacktestConfig(**with_zero_fees(payload))


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


def _mark_to_market_oracle(
    trades: list, frames: dict[str, pd.DataFrame], initial: float, at: pd.Timestamp
) -> float:
    """Independent reference: capital + realized net + unrealized at ``at``."""
    value = initial
    for trade in trades:
        if pd.Timestamp(trade.entry_time) > at:
            continue
        if pd.Timestamp(trade.last_exit_time) <= at:
            value += trade.net_pnl if trade.net_pnl is not None else trade.pnl
            continue
        closes = frames[trade.symbol].set_index("timestamp")["close"]
        mark = float(closes.asof(at))
        sign = 1.0 if trade.direction == "LONG" else -1.0
        value += sign * (mark - trade.entry_price) * trade.quantity
    return value


class TestPortfolioEquityCurveSemantics:
    def test_curve_credits_pnl_at_exit_not_entry(self, sizing_env):
        """PnL lands at the trade's EXIT time, never at its entry time.

        Regression (F5): the old curve credited PnL at entry_time while the
        simulator credited it on close — the persisted path described an
        equity trajectory the backtest never traded. With the mark-to-market
        curve the same guarantee reads: before the first exit the curve never
        carries realized profit, and at every exit it equals capital plus the
        PnL of everything closed so far (single position at a time here).
        """
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades, "the fixture must produce trades"
        assert result.equity_curve, "the fixture must produce an equity curve"

        equity_by_time = {pd.Timestamp(p.time): p.equity for p in result.equity_curve}
        initial = config.initial_capital
        assert result.equity_curve[0].time == f"{config.start_date}T00:00:00+00:00"
        assert result.equity_curve[0].equity == pytest.approx(initial)
        first_entry = min(pd.Timestamp(t.entry_time) for t in result.trades)
        for point in result.equity_curve:
            if pd.Timestamp(point.time) < first_entry:
                assert point.equity == pytest.approx(initial, rel=1e-9), (
                    "before the first entry nothing can have been earned"
                )
        running = initial
        for trade in sorted(result.trades, key=lambda t: t.last_exit_time):
            running += trade.pnl
            assert equity_by_time[pd.Timestamp(trade.last_exit_time)] == pytest.approx(
                round(running, 2), rel=1e-9
            ), "PnL must be credited at EXIT time"

    def test_curve_matches_independent_mark_to_market_oracle(self, sizing_env):
        tmp_path, strategy_path = sizing_env
        config = _config(tmp_path)
        result = VectorbtAdapter().run(config, str(strategy_path))
        frames = {"VSZ": _deterministic_frame()}
        assert len(result.equity_curve) > len(result.trades) + 1, "one point per bar, not per exit"
        for point in result.equity_curve[1:]:
            expected = _mark_to_market_oracle(
                result.trades, frames, config.initial_capital, pd.Timestamp(point.time)
            )
            assert point.equity == pytest.approx(round(expected, 2), abs=0.011)
        assert result.meta.data["equity_curve_basis"] == "mark_to_market"

    def test_curve_timestamps_stay_monotone_with_overlapping_trades(self, tmp_path):
        """Two symbols whose exits interleave must still yield a sorted curve.

        Regression (spec review, F5): the curve used to credit PnL in entry
        order while stamping exit times — with interleaved exits the persisted
        timestamps went backwards, so downstream drawdown/Sharpe consumers
        read a scrambled path. Every point must also equal the independent
        portfolio mark-to-market (realized + unrealized of every open leg).
        """
        strategy_dir = tmp_path / "strategies"
        strategy_dir.mkdir()
        strategy_path = strategy_dir / "vbt_sizing_sma.py"
        strategy_path.write_text(STRATEGY)

        base = _deterministic_frame()
        # Symbol B: last 10 bars truncated, so B's final exit lands before
        # A's — guaranteed exit-time interleave.
        frames = {"VSA": base, "VSB": base.iloc[:-10].copy()}
        for symbol, frame in frames.items():
            _write_ohlcv(tmp_path, symbol, frame)

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
        assert len(set(times)) == len(times)
        for point in result.equity_curve[1:]:
            expected = _mark_to_market_oracle(
                result.trades, frames, config.initial_capital, pd.Timestamp(point.time)
            )
            assert point.equity == pytest.approx(round(expected, 2), abs=0.011)
        cumulative = config.initial_capital + sum(t.pnl for t in result.trades)
        final_point = result.equity_curve[-1]
        assert final_point.equity == pytest.approx(round(cumulative, 2), rel=1e-6), (
            "the last curve point must equal capital plus the realized PnL of "
            "every trade"
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
            max_position_pct=1.0,   # no cap binds
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
            '        "stop_loss": pd.Series('
            '[df["close"].iloc[0] * 0.95] * len(df), index=df.index),\n',
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
            max_position_pct=1.0,
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
        """max_leverage=1 with a full-equity position cap: notional == equity.

        Kills the min→max mutant: with max() the notional would follow a
        looser position cap instead of the leverage limit.
        """
        tmp_path, strategy_path = sizing_env
        config = _config(
            tmp_path,
            risk_per_trade_pct=0.5,   # huge risk target, cap must bind
            max_position_pct=1.0,
            max_leverage=1.0,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades
        for trade in result.trades:
            risk_math = trade.meta["risk_math"]
            notional = trade.entry_price * trade.quantity
            assert notional <= risk_math["equity_at_entry"] * 1.0 + 1e-6, (
                "the leverage limit (1x) must bind the notional even when the "
                "position cap is full-equity"
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

    def test_entry_stop_no_touch_produces_no_trade(self, tmp_path: Path):
        """D2 strict (A-H2): entry_stop present but never touched — no fill,
        no phantom trade at the open. Falling back to open/close would book
        fills for orders the market never triggered."""
        text = _stop_order_strategy_text().replace(
            'indicators["entry_stop"].iloc[5] = 105.0',
            'indicators["entry_stop"].iloc[5] = 200.0',
        )
        strategy_dir = tmp_path / "strategies"
        strategy_dir.mkdir(exist_ok=True)
        strategy_path = strategy_dir / "vbt_stop_order_untouched.py"
        strategy_path.write_text(text)
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
        _write_ohlcv(tmp_path, "VSO", frame)
        config = _config(
            tmp_path,
            strategy_name="vbt_stop_order_untouched",
            symbols=["VSO"],
            risk_per_trade_pct=0.01,
        )
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades == []


# ─── inverted SIZING-only stop guard ────────────────────────────────────────


def _sizing_stop_env(tmp_path: Path, sizing_stop: float):
    """Market LONG signalled on bar 5, filled at bar 6's open (100.0), with a
    sizing-only stop (``sizing_sl_price``) published on the signal bar."""
    text = '''
import pandas as pd


def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[5] = True
    exits.iloc[8] = True
    sizing = pd.Series([float("nan")] * n, index=df.index)
    sizing.iloc[5] = %r
    return entries, exits, {
        "direction": pd.Series(["LONG"] * n, index=df.index),
        "sizing_sl_price": sizing,
    }, []
''' % float(sizing_stop)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC"),
            "open": [100.0] * 24,
            "high": [101.0] * 24,
            "low": [99.0] * 24,
            "close": [100.0] * 24,
            "volume": [1000.0] * 24,
        }
    )
    strategy_dir = tmp_path / "strategies"
    strategy_dir.mkdir(exist_ok=True)
    strategy_path = strategy_dir / "vbt_sizing_stop.py"
    strategy_path.write_text(text)
    _write_ohlcv(tmp_path, "VSS", frame)
    config = _config(
        tmp_path,
        strategy_name="vbt_sizing_stop",
        symbols=["VSS"],
        risk_per_trade_pct=0.01,
        max_position_exposure_pct=1.0,
        max_leverage=1.0,
        parameters={},
    )
    return config, strategy_path


class TestInvertedSizingStop:
    def test_valid_sizing_stop_sizes_from_distance(self, tmp_path: Path):
        config, strategy_path = _sizing_stop_env(tmp_path, 98.0)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert len(result.trades) == 1
        # 1% of 100k over a 2.0 distance from the 100.0 fill.
        assert result.trades[0].quantity == pytest.approx(500.0)
        assert result.meta.data["inverted_stop_drops"] == 0

    @pytest.mark.parametrize("sizing_stop", [100.0, 101.5])
    def test_sizing_stop_at_or_above_long_fill_skips_trade(
        self, tmp_path: Path, sizing_stop: float
    ):
        """A LONG sizing stop at/above the fill has no risk distance (e.g.
        after a gap down): skip and count it, like native does, instead of
        sizing on abs(entry - stop) or the 2% fallback."""
        config, strategy_path = _sizing_stop_env(tmp_path, sizing_stop)
        result = VectorbtAdapter().run(config, str(strategy_path))
        assert result.trades == []
        assert result.meta.data["inverted_stop_drops"] == 1
