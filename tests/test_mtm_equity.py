"""Reporting equity is MARK-TO-MARKET on every engine path.

Regression for the realized-only equity curve: drawdown / Sharpe / volatility /
Calmar were computed from a curve credited only at trade exit, so the paper
drawdown of an open position was invisible (a 40 % crash inside one long hold
reported ``max_drawdown_pct == 0``). Sizing is a separate concern and stays on
realized cash (``tests/test_engine_behavior.py::TestSizingEquityExcludesUnrealizedPnl``).

Layout:

* ``TestBuilder*`` - the shared builder (``runner/engines/mtm_equity.py``) on
  hand-built trades with independently computed expectations.
* ``TestStats*`` - ``compute_stats`` on the MTM curve vs the realized balance.
* ``TestEnginePaths`` - native (single + multi symbol), vectorbt (single + multi),
  signal replay and pynecore (CLI stubbed at the subprocess boundary), each on
  the same 40 % mid-hold crash fixture.
* ``TestPublishedDailySeries`` - ``stats.daily_returns`` / ``daily_equity_curve``
  describe the same equity path as the persisted curve.
"""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from runner.daily_equity import attach_daily_equity
from runner.engine import TradeEngine
from runner.engines.base import BacktestConfig
from runner.engines.mtm_equity import (
    BASIS_MARK_TO_MARKET,
    BASIS_MARK_TO_MARKET_PARTIAL,
    EQUITY_CURVE_BASIS_KEY,
    UNMARKED_SYMBOLS_KEY,
    build_mark_to_market_curve,
    closes_from_frame,
)
from runner.engines.native_adapter import NativeAdapter
from runner.engines.pynecore_adapter import PyneCoreAdapter
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter
from runner.stats import compute_stats, compute_stats_for_result
from shared.models import EquityPoint, Trade, TradeExit

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CAPITAL = 100_000.0


def _stamps(n: int, freq: str = "D") -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")


def _closes(values: list[float], freq: str = "D") -> pd.Series:
    return pd.Series(values, index=_stamps(len(values), freq), dtype=float)


def _trade(
    *,
    symbol: str = "AAA",
    direction: str = "LONG",
    entry_idx: int,
    entry_price: float,
    quantity: float,
    legs: list[tuple[int, float, float]],
    fees: float = 0.0,
    n: int = 20,
) -> Trade:
    """``legs`` are ``(bar index, price, quantity)``."""
    stamps = _stamps(n)
    sign = 1.0 if direction == "LONG" else -1.0
    exits = [
        TradeExit(
            time=stamps[idx].isoformat(),
            price=price,
            quantity=qty,
            pnl=sign * (price - entry_price) * qty,
        )
        for idx, price, qty in legs
    ]
    gross = sum(leg.pnl for leg in exits)
    return Trade(
        id=1,
        symbol=symbol,
        direction=direction,
        entry_time=stamps[entry_idx].isoformat(),
        entry_price=entry_price,
        quantity=quantity,
        exits=exits,
        pnl=gross,
        fees=fees,
        net_pnl=gross - fees,
    )


def _curve(trades: list[Trade], closes: dict[str, pd.Series], n: int = 20):
    return build_mark_to_market_curve(trades, closes, CAPITAL, start_time=_stamps(n)[0].isoformat())


def _equity_at(curve: list[EquityPoint], index: int) -> float:
    target = _stamps(index + 1)[index]
    return {pd.Timestamp(p.time): p.equity for p in curve}[target]


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------


class TestBuilderSemantics:
    def test_crash_inside_a_hold_is_visible_and_recovers(self):
        # 1000 units from bar 1 at 100; price 100 -> 60 (bars 5-9) -> 100; exit bar 15 at 100.
        close = [100.0] * 5 + [60.0] * 5 + [100.0] * 10
        trade = _trade(entry_idx=1, entry_price=100.0, quantity=1000.0, legs=[(15, 100.0, 1000.0)])
        curve, unmarked = _curve([trade], {"AAA": _closes(close)})
        assert unmarked == []
        values = [p.equity for p in curve]
        assert min(values) == pytest.approx(CAPITAL - 40.0 * 1000.0)
        assert values[-1] == pytest.approx(CAPITAL)  # realized pnl is exactly 0
        assert _equity_at(curve, 7) == pytest.approx(60_000.0)

    def test_short_position_gains_when_price_falls(self):
        close = [100.0] * 3 + [80.0] * 17
        trade = _trade(
            direction="SHORT",
            entry_idx=1,
            entry_price=100.0,
            quantity=100.0,
            legs=[(10, 80.0, 100.0)],
        )
        curve, _ = _curve([trade], {"AAA": _closes(close)})
        assert _equity_at(curve, 5) == pytest.approx(CAPITAL + 20.0 * 100.0)
        assert curve[-1].equity == pytest.approx(CAPITAL + 2000.0)

    def test_partial_exits_realize_gross_per_leg_and_mark_the_remainder(self):
        close = [100.0] * 2 + [110.0] * 4 + [90.0] * 14
        legs = [(4, 110.0, 40.0), (10, 90.0, 60.0)]  # sell 40 @110, remaining 60 @90
        trade = _trade(entry_idx=1, entry_price=100.0, quantity=100.0, legs=legs, fees=7.0)
        curve, _ = _curve([trade], {"AAA": _closes(close)})
        # bar 3: all 100 open at mark 110
        assert _equity_at(curve, 3) == pytest.approx(CAPITAL + 10.0 * 100.0)
        # bar 4: 40 realized at +10, 60 remaining marked at 110 -> +10*40 + 10*60
        assert _equity_at(curve, 4) == pytest.approx(CAPITAL + 400.0 + 600.0)
        # bar 7 (after the first leg, before the last): 40 realized + 60 marked at 90
        assert _equity_at(curve, 7) == pytest.approx(CAPITAL + 400.0 - 600.0)
        # fees are recognised only at the final exit; final == capital + net pnl
        assert curve[-1].equity == pytest.approx(CAPITAL + 400.0 - 600.0 - 7.0)

    def test_fees_booked_at_final_exit_not_before(self):
        close = [100.0] * 20
        trade = _trade(
            entry_idx=2, entry_price=100.0, quantity=10.0, legs=[(8, 100.0, 10.0)], fees=25.0
        )
        curve, _ = _curve([trade], {"AAA": _closes(close)})
        assert _equity_at(curve, 5) == pytest.approx(CAPITAL)
        assert _equity_at(curve, 8) == pytest.approx(CAPITAL - 25.0)
        assert curve[-1].equity == pytest.approx(CAPITAL - 25.0)

    def test_overlapping_positions_on_two_symbols_are_summed(self):
        a = _trade(
            symbol="AAA", entry_idx=1, entry_price=100.0, quantity=10.0, legs=[(12, 100.0, 10.0)]
        )
        b = _trade(
            symbol="BBB", entry_idx=3, entry_price=50.0, quantity=20.0, legs=[(9, 50.0, 20.0)]
        )
        closes = {
            "AAA": _closes([100.0] * 6 + [90.0] * 14),
            "BBB": _closes([50.0] * 6 + [55.0] * 14),
        }
        curve, _ = _curve([a, b], closes)
        # bar 7: AAA -10*10, BBB +5*20
        assert _equity_at(curve, 7) == pytest.approx(CAPITAL - 100.0 + 100.0)
        # bar 10: BBB closed flat (50 -> 50), AAA still marked at 90
        assert _equity_at(curve, 10) == pytest.approx(CAPITAL - 100.0)

    def test_symbol_without_bars_is_reported_unmarked_not_dropped(self):
        trade = _trade(
            symbol="ZZZ", entry_idx=1, entry_price=10.0, quantity=5.0, legs=[(6, 12.0, 5.0)]
        )
        curve, unmarked = _curve([trade], {"AAA": _closes([1.0] * 20)})
        assert unmarked == ["ZZZ"]
        assert curve[-1].equity == pytest.approx(CAPITAL + 10.0)  # realized pnl still booked
        assert _equity_at(curve, 3) == pytest.approx(CAPITAL)  # carried at entry price

    def test_bars_before_the_start_time_are_excluded(self):
        trade = _trade(entry_idx=8, entry_price=100.0, quantity=1.0, legs=[(12, 100.0, 1.0)])
        curve, _ = build_mark_to_market_curve(
            [trade], {"AAA": _closes([100.0] * 20)}, CAPITAL, start_time=_stamps(20)[5].isoformat()
        )
        assert pd.Timestamp(curve[0].time) == _stamps(20)[5]
        assert all(pd.Timestamp(p.time) >= _stamps(20)[5] for p in curve)

    def test_no_trades_yields_a_flat_curve_from_the_first_bar(self):
        curve, _ = _curve([], {"AAA": _closes([100.0] * 20)})
        assert {p.equity for p in curve} == {CAPITAL}
        assert pd.Timestamp(curve[0].time) == _stamps(20)[0]
        assert pd.Timestamp(curve[-1].time) == _stamps(20)[-1]

    def test_invalid_capital_is_rejected(self):
        for bad in (0.0, -1.0, float("nan")):
            with pytest.raises(ValueError):
                build_mark_to_market_curve([], {}, bad, start_time="2024-01-01")

    def test_curve_is_strictly_increasing_in_time(self):
        a = _trade(
            entry_idx=1, entry_price=100.0, quantity=10.0, legs=[(5, 100.0, 5.0), (5, 100.0, 5.0)]
        )
        curve, _ = _curve([a], {"AAA": _closes([100.0] * 20)})
        times = [pd.Timestamp(p.time) for p in curve]
        assert times == sorted(set(times))


class TestBuilderThinning:
    def test_flat_runs_are_thinned_to_their_end_points_only(self):
        trade = _trade(entry_idx=15, entry_price=100.0, quantity=1.0, legs=[(17, 101.0, 1.0)])
        curve, _ = _curve([trade], {"AAA": _closes([100.0] * 20)})
        times = [pd.Timestamp(p.time) for p in curve]
        # bars 0..14 are flat with nothing open: only the first and the last
        # flat bar before the entry survive.
        assert times[0] == _stamps(20)[0]
        assert len([t for t in times if t < _stamps(20)[15]]) == 2
        assert _stamps(20)[14] in times

    def test_thinning_is_lossless_for_the_daily_step_function(self):
        close = [100.0] * 6 + [70.0] * 4 + [100.0] * 10
        trade = _trade(entry_idx=7, entry_price=100.0, quantity=500.0, legs=[(12, 100.0, 500.0)])
        thinned, _ = _curve([trade], {"AAA": _closes(close)})
        full = pd.Series({pd.Timestamp(p.time): p.equity for p in thinned}).reindex(
            _stamps(20), method="ffill"
        )
        expected = np.array(
            [CAPITAL + (close[i] - 100.0) * 500.0 if 7 <= i < 12 else CAPITAL for i in range(20)]
        )
        np.testing.assert_allclose(full.to_numpy(), expected, atol=0.011)

    def test_open_positions_are_never_thinned(self):
        trade = _trade(entry_idx=2, entry_price=100.0, quantity=1.0, legs=[(18, 100.0, 1.0)])
        curve, _ = _curve([trade], {"AAA": _closes([100.0] * 20)})
        times = {pd.Timestamp(p.time) for p in curve}
        assert all(_stamps(20)[i] in times for i in range(2, 19))


def test_closes_from_frame_handles_timestamp_column_and_datetime_index():
    stamps = _stamps(3)
    column = pd.DataFrame({"timestamp": stamps, "close": [1.0, 2.0, 3.0]})
    indexed = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=stamps.tz_localize(None))
    assert closes_from_frame(column).tolist() == [1.0, 2.0, 3.0]
    assert closes_from_frame(indexed).index.tz is not None
    assert closes_from_frame(pd.DataFrame()) is None
    assert closes_from_frame(None) is None


# --------------------------------------------------------------------------
# Stats on the curve
# --------------------------------------------------------------------------


def _realized_curve(trades: list[Trade]) -> list[EquityPoint]:
    """The OLD (pre-fix) curve: realized net PnL credited at exit time only."""
    running = CAPITAL
    curve = [EquityPoint(time=_stamps(20)[0].isoformat(), equity=running)]
    for trade in sorted(trades, key=lambda t: t.last_exit_time):
        running += trade.net_pnl
        curve.append(EquityPoint(time=trade.last_exit_time, equity=running))
    return curve


class TestStatsOnMarkToMarketCurve:
    def _scenario(self):
        close = [100.0] * 5 + [60.0] * 5 + [100.0] * 10
        trades = [
            _trade(entry_idx=1, entry_price=100.0, quantity=1000.0, legs=[(15, 100.0, 1000.0)])
        ]
        curve, _ = _curve(trades, {"AAA": _closes(close)})
        return trades, curve

    def test_max_drawdown_sees_the_open_position_loss(self):
        trades, curve = self._scenario()
        realized = compute_stats(trades, CAPITAL, equity_curve=_realized_curve(trades))
        marked = compute_stats(trades, CAPITAL, equity_curve=curve)
        assert realized["max_drawdown_pct"] == 0.0  # the bug: blind to the crash
        assert marked["max_drawdown_pct"] == pytest.approx(-0.40, abs=1e-6)

    def test_balance_drawdown_is_published_next_to_equity_drawdown(self):
        trades, curve = self._scenario()
        stats = compute_stats(trades, CAPITAL, equity_curve=curve)
        assert stats["max_drawdown_realized_pct"] == 0.0
        assert stats["max_drawdown_pct"] <= stats["max_drawdown_realized_pct"]

    def test_balance_drawdown_replays_net_pnl_in_exit_order(self):
        losers = [
            _trade(
                entry_idx=1, entry_price=100.0, quantity=100.0, legs=[(3, 90.0, 100.0)]
            ),  # -1000
            _trade(
                entry_idx=4, entry_price=100.0, quantity=100.0, legs=[(6, 150.0, 100.0)]
            ),  # +5000
            _trade(
                entry_idx=7, entry_price=100.0, quantity=100.0, legs=[(9, 50.0, 100.0)]
            ),  # -5000
        ]
        stats = compute_stats(losers, CAPITAL, equity_curve=_realized_curve(losers))
        # balance: 100k -> 99k -> 104k -> 99k : peak 104k, trough 99k
        assert stats["max_drawdown_realized_pct"] == pytest.approx(
            -(104_000 - 99_000) / 104_000, abs=1e-6
        )

    def test_balance_drawdown_is_none_without_trades(self):
        curve = [EquityPoint(time=_stamps(3)[i].isoformat(), equity=CAPITAL + i) for i in range(3)]
        assert compute_stats([], CAPITAL, equity_curve=curve)["max_drawdown_realized_pct"] is None

    def test_sharpe_and_volatility_inputs_reflect_the_crash(self):
        trades, curve = self._scenario()
        realized = compute_stats(trades, CAPITAL, equity_curve=_realized_curve(trades))
        marked = compute_stats(trades, CAPITAL, equity_curve=curve)
        # A realized curve is a flat line (zero variance, ratio undefined/0); the
        # marked curve has a real -40 % / +66 % round trip.
        assert marked["sortino_ratio"] != realized["sortino_ratio"]
        assert marked["calmar_ratio"] == pytest.approx(0.0, abs=1e-9)  # no return over the window
        assert marked["max_drawdown_duration"] > realized["max_drawdown_duration"]


# --------------------------------------------------------------------------
# Engine paths: 40 % crash inside a single hold
# --------------------------------------------------------------------------

BARS = 40
CRASH = (12, 20)  # bars [12, 20) trade at 60


def _crash_frame(
    *, bars: int = BARS, base: float = 100.0, crash: tuple[int, int] = CRASH
) -> pd.DataFrame:
    close = np.full(bars, base)
    close[crash[0] : crash[1]] = base * 0.6
    stamps = pd.date_range("2024-01-01", periods=bars, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1.0,
        }
    )


HOLD_STRATEGY = """
import pandas as pd

STRATEGY_META = {"name": "hold_through_crash", "version": "1.0"}

def generate_signals(df, params):
    n = len(df)
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[1] = True
    exits.iloc[n - 4] = True
    # No exit stop: the engines size from their documented fallback stop (2 %)
    # without ever exiting on it, so the position is fully invested (capped at
    # 100 % notional) and rides the crash.
    indicators = {"direction": pd.Series("LONG", index=df.index)}
    return entries, exits, indicators, []
"""


def _write_symbol(data_dir: Path, symbol: str, frame: pd.DataFrame, timeframe: str = "1d") -> None:
    target = data_dir / "ohlcv" / "test_source" / "equities" / symbol
    target.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target / f"{timeframe}.parquet", index=False)


def _pyne_time(stamp: pd.Timestamp) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%S%z")


def _strategy(tmp_path: Path) -> Path:
    path = tmp_path / "hold_through_crash.py"
    path.write_text(HOLD_STRATEGY)
    return path


def _config(tmp_path: Path, symbols: list[str]) -> BacktestConfig:
    return BacktestConfig(
        strategy_name="hold_through_crash",
        symbols=symbols,
        timeframe="1d",
        start_date="2024-01-01",
        end_date="2024-02-29",
        initial_capital=CAPITAL,
        risk_per_trade_pct=1.0,
        max_position_exposure_pct=1.0,
        max_leverage=1.0,
        modeling_resolution="main_timeframe",
        signal_exit_fill="close",
        data_dir=str(tmp_path),
        parameters={"fees": "zero"},
    )


def _assert_crash_visible(result, *, exposure: float) -> None:
    """The held position's paper loss is in the curve and in the stats."""
    assert result.trades, "the strategy must hold through the crash"
    assert sum(t.pnl for t in result.trades) == pytest.approx(0.0, abs=1.0), "realized PnL is flat"
    assert result.meta.data[EQUITY_CURVE_BASIS_KEY] == BASIS_MARK_TO_MARKET
    stats = compute_stats_for_result(result)
    expected = -0.40 * exposure
    assert stats["max_drawdown_pct"] == pytest.approx(expected, abs=2e-3)
    assert stats["max_drawdown_realized_pct"] == pytest.approx(0.0, abs=1e-3)
    # the curve itself dips by the crash while the trade is open
    low = min(p.equity for p in result.equity_curve)
    assert low == pytest.approx(CAPITAL * (1 + expected), rel=3e-3)
    assert result.equity_curve[-1].equity == pytest.approx(
        CAPITAL + sum(t.net_pnl for t in result.trades), abs=0.02
    )


class TestEnginePaths:
    def test_native_single_symbol(self, tmp_path):
        _write_symbol(tmp_path, "CRA", _crash_frame())
        result = NativeAdapter().run(_config(tmp_path, ["CRA"]), str(_strategy(tmp_path)))
        _assert_crash_visible(result, exposure=1.0)

    def test_native_curve_equals_the_trade_engines_own_per_bar_equity(self, tmp_path):
        """The adapter's curve (rebuilt from trades) is the engine's bar-by-bar MTM equity."""
        frame = _crash_frame()
        _write_symbol(tmp_path, "CRA", frame)
        config = _config(tmp_path, ["CRA"])
        strategy_path = _strategy(tmp_path)
        result = NativeAdapter().run(config, str(strategy_path))

        import importlib.util

        spec = importlib.util.spec_from_file_location("hold_mod", strategy_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = TradeEngine(config.model_copy(update={"symbols": ["CRA"]}), symbol="CRA")
        engine.run(frame.copy(), module.generate_signals)
        engine_curve = {pd.Timestamp(p.time): p.equity for p in engine.equity_curve}
        adapter_curve = {pd.Timestamp(p.time): p.equity for p in result.equity_curve}
        shared = [t for t in adapter_curve if t in engine_curve]
        assert len(shared) > len(result.trades) + 5
        for stamp in shared:
            assert adapter_curve[stamp] == pytest.approx(engine_curve[stamp], abs=0.011), stamp
        # and in between thinned flat stretches the engine curve is constant
        assert min(engine_curve.values()) == pytest.approx(min(adapter_curve.values()), abs=0.011)

    def test_native_multi_symbol_portfolio(self, tmp_path):
        _write_symbol(tmp_path, "CRA", _crash_frame())
        _write_symbol(tmp_path, "CRB", _crash_frame(crash=(0, 0)))  # never crashes
        result = NativeAdapter().run(_config(tmp_path, ["CRA", "CRB"]), str(_strategy(tmp_path)))
        assert len(result.trades) == 2
        assert result.meta.data[EQUITY_CURVE_BASIS_KEY] == BASIS_MARK_TO_MARKET
        stats = compute_stats_for_result(result)
        # Shared equity: each leg is sized from the same pre-entry equity, so
        # both open at ~100 % notional of 100k - the crashed leg loses 40 % of
        # its own notional, i.e. at least a -30 % portfolio drawdown.
        assert stats["max_drawdown_pct"] <= -0.30
        assert stats["max_drawdown_realized_pct"] == pytest.approx(0.0, abs=1e-3)

    def test_vectorbt_single_symbol(self, tmp_path):
        _write_symbol(tmp_path, "CRA", _crash_frame())
        result = VectorbtAdapter().run(_config(tmp_path, ["CRA"]), str(_strategy(tmp_path)))
        _assert_crash_visible(result, exposure=1.0)

    def test_vectorbt_multi_symbol_portfolio(self, tmp_path):
        _write_symbol(tmp_path, "CRA", _crash_frame())
        _write_symbol(tmp_path, "CRB", _crash_frame(crash=(0, 0)))
        result = VectorbtAdapter().run(_config(tmp_path, ["CRA", "CRB"]), str(_strategy(tmp_path)))
        assert len(result.trades) == 2
        assert result.meta.data[EQUITY_CURVE_BASIS_KEY] == BASIS_MARK_TO_MARKET
        stats = compute_stats_for_result(result)
        assert stats["max_drawdown_pct"] <= -0.30

    def test_native_and_vectorbt_report_the_same_drawdown(self, tmp_path):
        _write_symbol(tmp_path, "CRA", _crash_frame())
        native = NativeAdapter().run(_config(tmp_path, ["CRA"]), str(_strategy(tmp_path)))
        vbt = VectorbtAdapter().run(_config(tmp_path, ["CRA"]), str(_strategy(tmp_path)))
        assert compute_stats_for_result(native)["max_drawdown_pct"] == pytest.approx(
            compute_stats_for_result(vbt)["max_drawdown_pct"], abs=2e-3
        )

    def test_signal_replay(self, tmp_path):
        frame = _crash_frame()
        stamps = frame["timestamp"]
        signals = [
            ReplaySignal.from_payload(
                {
                    "timestamp": stamps[1].isoformat(),
                    "symbol": "CRA",
                    "action": "buy",
                    "entry_price": 100.0,
                    # far stop: the replay engine exits on its fallback stop
                    # otherwise, which the crash would hit
                    "stop_loss": 10.0,
                }
            ),
            ReplaySignal.from_payload(
                {
                    "timestamp": stamps[BARS - 4].isoformat(),
                    "symbol": "CRA",
                    "action": "sell",
                    "entry_price": 100.0,
                    "metadata": {"exit_only": True},
                }
            ),
        ]
        adapter = SignalReplayAdapter(fee_params={"fees": "zero"}, initial_capital=CAPITAL)
        result = adapter.run(signals=signals, ohlcv_by_symbol={"CRA": frame}, timeframe="1d")
        assert len(result.trades) == 1
        trade = result.trades[0]
        exposure = trade.quantity * 100.0 / CAPITAL
        assert exposure > 0.005
        assert trade.pnl == pytest.approx(0.0, abs=1.0)
        assert result.meta.data[EQUITY_CURVE_BASIS_KEY] == BASIS_MARK_TO_MARKET
        stats = result.stats
        assert stats["max_drawdown_pct"] == pytest.approx(-0.40 * exposure, abs=2e-4)
        assert stats["max_drawdown_realized_pct"] == pytest.approx(0.0, abs=1e-4)

    def test_pynecore_with_stubbed_cli(self, tmp_path):
        frame = _crash_frame()
        _write_symbol(tmp_path, "CRA", frame)
        stamps = frame["timestamp"]
        trade_csv = (
            "Trade #,Bar Index,Type,Signal,Date/Time,Price USD,Contracts,"
            "Profit USD,Profit %,Cumulative profit USD,Cumulative profit %,"
            "Run-up USD,Run-up %,Drawdown USD,Drawdown %\n"
            f"1,1,Entry long,Long,{_pyne_time(stamps[1])},100.0,100.0,0.0,0.00,0.0,"
            "0.00,0.0,0.0,0.0,0.0\n"
            f"1,{BARS - 4},Exit long,Close,{_pyne_time(stamps[BARS - 4])},100.0,"
            "100.0,0.0,0.00,0.0,0.00,0.0,0.0,0.0,0.0\n"
        )
        stats_csv = (
            "Metric,All USD,All %,Long USD,Long %,Short USD,Short %\nNet profit,0,0,0,0,0,0\n"
        )

        def fake_run(cmd, **kwargs):
            Path(cmd[cmd.index("--trade") + 1]).write_text(trade_csv)
            Path(cmd[cmd.index("--strat") + 1]).write_text(stats_csv)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with ExitStack() as stack:
            stack.enter_context(
                patch("runner.engines.pynecore_adapter.shutil.which", return_value="/usr/bin/pyne")
            )
            stack.enter_context(
                patch("runner.engines.pynecore_adapter.subprocess.run", side_effect=fake_run)
            )
            result = PyneCoreAdapter().run(_config(tmp_path, ["CRA"]), str(_strategy(tmp_path)))
        assert len(result.trades) == 1
        exposure = result.trades[0].quantity * 100.0 / CAPITAL
        assert exposure > 0.0
        assert result.meta.data[EQUITY_CURVE_BASIS_KEY] == BASIS_MARK_TO_MARKET
        stats = compute_stats_for_result(result)
        assert stats["max_drawdown_pct"] == pytest.approx(-0.40 * exposure, abs=2e-3)

    def test_missing_mark_data_is_reported_in_meta(self, tmp_path):
        """A traded symbol without bars degrades loudly, never silently."""
        trade = _trade(
            symbol="ZZZ", entry_idx=1, entry_price=10.0, quantity=5.0, legs=[(6, 12.0, 5.0)]
        )
        _, unmarked = _curve([trade], {"AAA": _closes([1.0] * 20)})
        from runner.engines.mtm_equity import describe_basis

        data = describe_basis(unmarked)
        assert data[EQUITY_CURVE_BASIS_KEY] == BASIS_MARK_TO_MARKET_PARTIAL
        assert data[UNMARKED_SYMBOLS_KEY] == ["ZZZ"]


class TestSizingIsUntouched:
    def test_reporting_equity_does_not_feed_sizing(self, tmp_path):
        """Entries after a drawdown are still sized from REALIZED cash."""
        frame = _crash_frame()
        _write_symbol(tmp_path, "CRA", frame)
        config = _config(tmp_path, ["CRA"])
        result = NativeAdapter().run(config, str(_strategy(tmp_path)))
        (trade,) = result.trades
        # fully invested at the entry bar's open from the full 100k of cash
        assert trade.quantity == pytest.approx(CAPITAL / trade.entry_price, rel=1e-6)


# --------------------------------------------------------------------------
# Published daily series
# --------------------------------------------------------------------------


class TestPublishedDailySeries:
    def test_daily_returns_describe_the_same_path_as_the_curve(self, tmp_path):
        _write_symbol(tmp_path, "CRA", _crash_frame())
        config = _config(tmp_path, ["CRA"])
        result = NativeAdapter().run(config, str(_strategy(tmp_path)))
        attach_daily_equity(result, start=config.start_date, end=config.end_date)

        returns = [row["return"] for row in result.stats["daily_returns"]]
        compounded = np.cumprod([1.0 + r for r in returns])
        peak = np.maximum.accumulate(np.concatenate([[1.0], compounded]))[1:]
        published_drawdown = float(np.min(compounded / peak - 1.0))
        stats = compute_stats_for_result(result)
        assert published_drawdown == pytest.approx(-0.40, abs=2e-3)
        assert published_drawdown == pytest.approx(stats["max_drawdown_pct"], abs=2e-3)
        assert result.meta.data["daily_equity_curve"], "daily curve must still be published"
        assert min(p["equity"] for p in result.meta.data["daily_equity_curve"]) == pytest.approx(
            CAPITAL * 0.6, rel=2e-3
        )
