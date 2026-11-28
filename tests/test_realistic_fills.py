"""Realistic-fill semantics (owner decision 2026-11-13), per engine.

A. A bar that OPENS beyond a protective stop/target fills at its OPEN (the
   price actually available), not at the level the market skipped. An
   intrabar touch (the bar opens on the safe side) keeps filling at the level.
B. Signal-driven exits fill at the NEXT bar's open, like entries. A signal on
   the last bar has no next bar: the forced end-of-data close (at the last
   bar's close) takes the position and the trade is flagged
   ``meta["signal_exit_deferred_to_end_of_data"]``.

Covered engines: native ``TradeEngine`` (the only engine under development,
ADR ``docs/adr/0002-native-single-strategy-engine.md``), ``SignalReplayAdapter``
(separate product, already gap-aware) and the shared ``contract`` primitives.
The frozen vectorbt/pynecore adapters keep their level fills and legacy
signal-exit convention and REFUSE ``signal_exit_fill="next_open"`` outright
(fail loud, not warn-and-run), so their gap/next-open coverage is intentionally
absent here — their containment is pinned at the level in
tests/test_engine_contract.py.
"""

from __future__ import annotations

import pandas as pd
import pytest

from risk.config import RiskConfig
from runner.engine import TradeEngine
from runner.engines import contract
from runner.engines.base import BacktestConfig
from runner.engines.contract import default_signal_exit_fill_for_engine
from runner.engines.lean_adapter import (
    LeanAdapter,
)
from runner.engines.lean_adapter import (
    _frozen_signal_exit_fill as _lean_frozen_fill,
)
from runner.engines.pynecore_adapter import (
    PyneCoreAdapter,
    _normalize_trade_exits,
)
from runner.engines.pynecore_adapter import (
    _frozen_signal_exit_fill as _pyne_frozen_fill,
)
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter
from runner.engines.vectorbt_adapter import (
    VectorbtAdapter,
)
from runner.engines.vectorbt_adapter import (
    _frozen_signal_exit_fill as _vbt_frozen_fill,
)
from shared.models import Trade, TradeExit
from tests.builders import make_backtest_config

# --------------------------------------------------------------------------- #
# shared primitives
# --------------------------------------------------------------------------- #


class TestContractPrimitives:
    @pytest.mark.parametrize(
        ("direction", "kind", "level", "open_", "gapped"),
        [
            ("LONG", "stop", 95.0, 90.0, True),
            ("LONG", "stop", 95.0, 95.0, True),  # open AT the level: same price
            ("LONG", "stop", 95.0, 99.0, False),
            ("SHORT", "stop", 105.0, 110.0, True),
            ("SHORT", "stop", 105.0, 101.0, False),
            ("LONG", "tp", 110.0, 115.0, True),
            ("LONG", "tp", 110.0, 105.0, False),
            ("SHORT", "tp", 90.0, 85.0, True),
            ("SHORT", "tp", 90.0, 95.0, False),
        ],
    )
    def test_gap_classification(self, direction, kind, level, open_, gapped):
        assert (
            contract.level_gapped_at_open(
                direction=direction, kind=kind, level=level, bar_open=open_
            )
            is gapped
        )
        fill = contract.level_fill_price(
            direction=direction, kind=kind, level=level, bar_open=open_
        )
        assert fill == (open_ if gapped else level)

    def test_unknown_direction_or_kind_raises(self):
        with pytest.raises(ValueError):
            contract.level_gapped_at_open(
                direction="FLAT", kind="stop", level=1.0, bar_open=1.0
            )
        with pytest.raises(ValueError):
            contract.level_gapped_at_open(
                direction="LONG", kind="trail", level=1.0, bar_open=1.0
            )

    def test_signal_exit_fill_default_is_next_open(self):
        assert make_backtest_config(
            strategy_name="x",
            symbols=["X"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
        ).signal_exit_fill == "next_open"
        for legacy in ("close", "Next_Open"):
            assert make_backtest_config(
                strategy_name="x",
                symbols=["X"],
                timeframe="1h",
                start_date="2024-01-01",
                end_date="2024-01-02",
                signal_exit_fill=legacy,
            ).signal_exit_fill == legacy.lower()


# --------------------------------------------------------------------------- #
# native TradeEngine
# --------------------------------------------------------------------------- #


def _bars(rows: list[tuple[float, float, float, float]], *, spread: float = 0.0) -> pd.DataFrame:
    data: dict[str, object] = {
        "timestamp": pd.date_range("2024-01-01", periods=len(rows), freq="h", tz="UTC"),
        "open": [r[0] for r in rows],
        "high": [r[1] for r in rows],
        "low": [r[2] for r in rows],
        "close": [r[3] for r in rows],
        "volume": [1000.0] * len(rows),
    }
    if spread:
        data["spread_price"] = [spread] * len(rows)
    return pd.DataFrame(data)


def _flat(price: float = 100.0) -> tuple[float, float, float, float]:
    return (price, price + 0.5, price - 0.5, price)


def _native(
    rows,
    *,
    direction: str = "LONG",
    stop: float | None = None,
    take_profit: float | None = None,
    exit_bar: int | None = None,
    entry_bar: int = 0,
    spread: float = 0.0,
    parameters: dict | None = None,
    extra_indicators: dict | None = None,
    **config_kwargs,
):
    """Entry signal on ``entry_bar`` (fills at the next open) with optional levels."""
    frame = _bars(rows, spread=spread)

    def strategy_fn(df: pd.DataFrame, params: dict):
        entries = pd.Series(False, index=df.index)
        exits = pd.Series(False, index=df.index)
        entries.iloc[entry_bar] = True
        if exit_bar is not None:
            exits.iloc[exit_bar] = True
        indicators = {"direction": pd.Series(direction, index=df.index)}
        if stop is not None:
            indicators["stop_loss"] = pd.Series(stop, index=df.index)
        if take_profit is not None:
            indicators["take_profit"] = pd.Series(take_profit, index=df.index)
        for key, value in (extra_indicators or {}).items():
            indicators[key] = pd.Series(value, index=df.index)
        return entries, exits, indicators, []

    payload = {
        "strategy_name": "realistic-fills",
        "symbols": ["T"],
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "initial_capital": 100_000.0,
        "risk_per_trade_pct": 0.01,
        "max_leverage": 100.0,
        "modeling_resolution": "main_timeframe",
        "parameters": {"fees": "zero", "slippage": "none", **(parameters or {})},
    }
    payload.update(config_kwargs)
    return TradeEngine(make_backtest_config(**payload), symbol="T").run(frame, strategy_fn)


class TestNativeGapThroughProtectiveLevels:
    def test_long_gap_through_stop_fills_at_open(self):
        # Entry fills at bar1 open 100; bar2 opens at 90, below the 95 stop.
        result = _native(
            [_flat(), _flat(), (90.0, 91.0, 88.0, 89.0), _flat(90.0)], stop=95.0
        )
        (trade,) = result.trades
        assert trade.entry_price == pytest.approx(100.0)
        assert trade.exits[-1].price == pytest.approx(90.0)  # NOT the 95 level
        assert trade.exits[-1].reason == "stop_loss"
        assert pd.Timestamp(trade.exits[-1].time) == pd.Timestamp("2024-01-01 02:00", tz="UTC")

    def test_short_gap_through_stop_fills_at_open(self):
        result = _native(
            [_flat(), _flat(), (110.0, 112.0, 109.0, 111.0), _flat(110.0)],
            direction="SHORT",
            stop=105.0,
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(110.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_long_gap_through_target_fills_at_open(self):
        result = _native(
            [_flat(), _flat(), (115.0, 116.0, 114.0, 115.0), _flat(115.0)],
            take_profit=110.0,
            stop=95.0,
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(115.0)  # better than the level
        assert trade.exits[-1].reason in {"tp", "take_profit"}

    def test_short_gap_through_target_fills_at_open(self):
        result = _native(
            [_flat(), _flat(), (85.0, 86.0, 84.0, 85.0), _flat(85.0)],
            direction="SHORT",
            take_profit=90.0,
            stop=105.0,
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(85.0)
        assert trade.exits[-1].reason in {"tp", "take_profit"}

    def test_short_gap_fill_pays_the_spread(self):
        # SHORT exits buy the ask: the gap fill is open + spread.
        result = _native(
            [_flat(), _flat(), (110.0, 112.0, 109.0, 111.0), _flat(110.0)],
            direction="SHORT",
            stop=105.0,
            spread=0.4,
            parameters={"spread_price_col": "spread_price"},
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(110.4)

    def test_gap_beats_a_later_intrabar_target_on_the_same_bar(self):
        # Opens below the stop AND later trades up through the target: the open
        # comes first, the stop (at the open) wins - no same-bar ambiguity.
        result = _native(
            [_flat(), _flat(), (90.0, 125.0, 88.0, 120.0), _flat(90.0)],
            stop=95.0,
            take_profit=110.0,
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(90.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_gap_excursion_is_capped_at_the_open(self):
        # The bar that gaps the stop printed 88 afterwards; the position was out
        # at 90, so MAE is the loss at 90, not at 88.
        result = _native(
            [_flat(), _flat(), (90.0, 91.0, 88.0, 89.0), _flat(90.0)], stop=95.0
        )
        (trade,) = result.trades
        qty = trade.quantity
        assert trade.mae == pytest.approx((90.0 - 100.0) * qty)


class TestNativeIntrabarTouchesStillFillAtTheLevel:
    def test_long_stop_touched_intrabar_fills_at_the_stop(self):
        result = _native(
            [_flat(), _flat(), (99.0, 99.5, 94.0, 96.0), _flat(96.0)], stop=95.0
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(95.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_short_stop_touched_intrabar_fills_at_the_stop(self):
        result = _native(
            [_flat(), _flat(), (101.0, 106.0, 100.5, 104.0), _flat(104.0)],
            direction="SHORT",
            stop=105.0,
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(105.0)

    def test_long_target_touched_intrabar_fills_at_the_target(self):
        result = _native(
            [_flat(), _flat(), (105.0, 112.0, 104.0, 111.0), _flat(111.0)],
            take_profit=110.0,
            stop=95.0,
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(110.0)

    def test_short_target_touched_intrabar_fills_at_the_target(self):
        result = _native(
            [_flat(), _flat(), (95.0, 96.0, 88.0, 89.0), _flat(89.0)],
            direction="SHORT",
            take_profit=90.0,
            stop=105.0,
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(90.0)

    def test_open_exactly_at_the_level_fills_at_that_price(self):
        result = _native(
            [_flat(), _flat(), (95.0, 96.0, 93.0, 94.0), _flat(94.0)], stop=95.0
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(95.0)

    def test_entry_bar_is_never_a_gap(self):
        # The entry fills at bar1's open (100); the very same bar trades down
        # through the 95 stop. That is an intrabar touch (level fill), not a gap.
        result = _native(
            [_flat(), (100.0, 100.5, 94.0, 95.5), _flat(96.0)], stop=95.0
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(95.0)
        assert pd.Timestamp(trade.exits[-1].time) == pd.Timestamp("2024-01-01 01:00", tz="UTC")


class TestNativeSignalExitNextOpen:
    ROWS = [_flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0), _flat(103.0), _flat(104.0)]

    def test_signal_exit_fills_at_the_next_bar_open(self):
        result = _native(self.ROWS, exit_bar=3)
        (trade,) = result.trades
        assert trade.entry_price == pytest.approx(100.0)  # bar1 open
        assert trade.exits[-1].price == pytest.approx(103.0)  # bar4 OPEN, not bar3 close
        assert trade.exits[-1].reason == "signal"
        assert pd.Timestamp(trade.exits[-1].time) == pd.Timestamp("2024-01-01 04:00", tz="UTC")
        assert "signal_exit_deferred_to_end_of_data" not in trade.meta

    def test_short_signal_exit_pays_the_spread_at_the_next_open(self):
        result = _native(
            self.ROWS,
            direction="SHORT",
            exit_bar=3,
            spread=0.2,
            parameters={"spread_price_col": "spread_price"},
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(103.2)

    def test_the_exit_bar_range_does_not_stop_out_a_position_already_closed_at_the_open(self):
        # Bar4 opens at 103, then crashes through the stop at 95. The signal exit
        # already filled at the open, so the later crash is not the position's.
        rows = [_flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0), (103.0, 103.5, 80.0, 82.0)]
        result = _native([*rows, _flat(82.0)], exit_bar=3, stop=95.0)
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(103.0)
        assert trade.exits[-1].reason == "signal"

    def test_gap_through_the_stop_on_the_exit_bar_keeps_the_stop_reason(self):
        rows = [
            _flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0),
            (90.0, 91.0, 88.0, 89.0), _flat(89.0),
        ]
        result = _native(rows, exit_bar=3, stop=95.0)
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(90.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_exit_and_entry_signalled_together_reverse_at_one_open(self):
        # Bar0 enters long (fills bar1). Bar3 signals exit AND a new entry: the
        # old position closes at bar4's open, then the new one opens at that same
        # open - the exit must not eat the fresh position.
        frame = _bars([*self.ROWS, _flat(105.0), _flat(106.0)])

        def strategy_fn(df, params):
            entries = pd.Series(False, index=df.index)
            exits = pd.Series(False, index=df.index)
            entries.iloc[0] = True
            entries.iloc[3] = True
            exits.iloc[3] = True
            return entries, exits, {"direction": pd.Series("LONG", index=df.index)}, []

        cfg = make_backtest_config(
            strategy_name="reverse",
            symbols=["T"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-31",
            initial_capital=100_000.0,
            risk_per_trade_pct=0.01,
            max_leverage=100.0,
            parameters={"fees": "zero", "slippage": "none"},
        )
        result = TradeEngine(cfg, symbol="T").run(frame, strategy_fn)
        first, second = result.trades
        assert first.exits[-1].price == pytest.approx(103.0)  # bar4 open
        assert second.entry_price == pytest.approx(103.0)  # same open
        assert second.exits[-1].reason == "end_of_data"

    def test_last_bar_signal_is_closed_by_end_of_data_and_flagged(self):
        # Signal on the final bar: there is no next open. The end-of-data close
        # (at the last bar's close) takes it, flagged.
        result = _native(self.ROWS, exit_bar=len(self.ROWS) - 1)
        (trade,) = result.trades
        assert trade.exits[-1].reason == "end_of_data"
        assert trade.exits[-1].price == pytest.approx(104.0)  # last close
        assert trade.meta["signal_exit_deferred_to_end_of_data"] is True

    def test_plain_end_of_data_close_is_not_flagged(self):
        result = _native(self.ROWS)
        (trade,) = result.trades
        assert trade.exits[-1].reason == "end_of_data"
        assert "signal_exit_deferred_to_end_of_data" not in trade.meta

    def test_legacy_close_opt_in_still_fills_on_the_signal_bar(self):
        result = _native(self.ROWS, exit_bar=3, signal_exit_fill="close")
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(102.0)  # bar3 close
        assert pd.Timestamp(trade.exits[-1].time) == pd.Timestamp("2024-01-01 03:00", tz="UTC")

    def test_open_mode_is_removed_and_fails_loudly(self):
        # Issue #153: filling a signal exit at the signal bar's own open is a lookahead.
        with pytest.raises(ValueError, match=r"removed.*next_open"):
            _native(self.ROWS, exit_bar=3, signal_exit_fill="open")

    def test_execution_model_metadata_records_the_exit_rules(self):
        result = _native(self.ROWS, exit_bar=3)
        model = result.trades[0].meta["execution_model"]
        assert model["protective_gap_fill"] == "open"
        assert model["signal_exit_fill"] == "next_open"


class TestNativeGapEdgeCases:
    """Review follow-ups: ladder remainders, ask-side gaps, trailed stops."""

    def test_gapped_tp1_remainder_is_closed_by_the_parked_signal_exit_at_the_same_open(self):
        # Bar3 opens at 110: beyond tp1 (105, gapped) but below tp2 (120). tp1
        # fills half the position at the open; the parked signal exit (bar2's
        # exit signal) claims the remainder at that SAME open.
        rows = [
            _flat(100.0), _flat(100.0), _flat(101.0),
            (110.0, 112.0, 109.0, 111.0), _flat(111.0),
        ]
        result = _native(
            rows,
            stop=95.0,
            exit_bar=2,
            extra_indicators={"tp1": 105.0, "tp2": 120.0},
        )
        (trade,) = result.trades
        assert len(trade.exits) == 2
        tp_leg, signal_leg = trade.exits
        assert tp_leg.price == pytest.approx(110.0)  # gap open, not the 105 level
        assert tp_leg.reason == "tp1"
        assert signal_leg.price == pytest.approx(110.0)
        assert signal_leg.reason == "signal"
        assert pd.Timestamp(signal_leg.time) == pd.Timestamp("2024-01-01 03:00", tz="UTC")

    def test_tp_ladder_with_only_the_first_level_gapped(self):
        # Same gap, no signal exit: tp1 fills at the open, tp2 (120) is never
        # reached, and the remainder runs to the end-of-data close.
        rows = [
            _flat(100.0), _flat(100.0), _flat(101.0),
            (110.0, 112.0, 109.0, 111.0), _flat(111.0), _flat(111.0),
        ]
        result = _native(
            rows,
            stop=95.0,
            extra_indicators={"tp1": 105.0, "tp2": 120.0},
        )
        (trade,) = result.trades
        assert trade.exits[0].price == pytest.approx(110.0)
        assert trade.exits[0].reason == "tp1"
        assert trade.exits[-1].reason == "end_of_data"
        # The ladder's first level took its 1/2 fraction; the remainder is half.
        assert trade.exits[0].quantity == pytest.approx(trade.quantity / 2)

    def test_tp_ladder_remainder_stops_out_at_the_level_on_the_same_bar(self):
        # Bar2 opens at 110 — beyond tp1 (105, gapped) but nowhere near the
        # 95 stop — then trades down through it. tp1 fills half at the open;
        # the remainder stops out at the LEVEL (the open was not beyond the
        # stop, so there is no gap fill on this branch).
        rows = [
            _flat(100.0), _flat(100.0),
            (110.0, 112.0, 94.0, 108.0), _flat(108.0),
        ]
        result = _native(
            rows,
            stop=95.0,
            extra_indicators={"tp1": 105.0, "tp2": 120.0},
        )
        (trade,) = result.trades
        assert len(trade.exits) == 2
        tp_leg, stop_leg = trade.exits
        assert tp_leg.price == pytest.approx(110.0)
        assert tp_leg.reason == "tp1"
        assert stop_leg.price == pytest.approx(95.0)
        assert stop_leg.reason == "stop_loss"
        assert stop_leg.quantity == pytest.approx(trade.quantity / 2)

    def test_tp_ladder_remainder_gap_fills_at_the_next_open(self):
        # The remainder after a partial TP is a normal open position: a later
        # bar that OPENS beyond the stop gap-fills the remainder at the open.
        rows = [
            _flat(100.0), _flat(100.0),
            (100.0, 106.0, 99.0, 104.0),   # tp1 (105) fills half at the level
            (90.0, 91.0, 88.0, 89.0),      # opens beyond the 95 stop: gap
            _flat(89.0),
        ]
        result = _native(
            rows,
            stop=95.0,
            extra_indicators={"tp1": 105.0, "tp2": 120.0},
        )
        (trade,) = result.trades
        assert len(trade.exits) == 2
        tp_leg, stop_leg = trade.exits
        assert tp_leg.price == pytest.approx(105.0)
        assert tp_leg.reason == "tp1"
        assert stop_leg.price == pytest.approx(90.0)  # the open, not the 95 level
        assert stop_leg.reason == "stop_loss"
        assert stop_leg.quantity == pytest.approx(trade.quantity / 2)

    def test_short_ask_open_gap_fills_at_the_ask_open(self):
        # ask_ohlc: the SHORT stop triggers on the ask. Bid open 104.7 alone is
        # NOT beyond the 105 stop, but ask open (104.7 + 0.4 = 105.1) is — a
        # gap the bid-only view would miss. Fill is the ask open.
        rows = [
            _flat(100.0), _flat(100.0),
            (104.7, 105.3, 104.5, 105.0), _flat(105.0),
        ]
        result = _native(
            rows,
            direction="SHORT",
            stop=105.0,
            spread=0.4,
            parameters={"spread_price_col": "spread_price", "ask_ohlc": True},
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(105.1)
        assert trade.exits[-1].reason == "stop_loss"


# --------------------------------------------------------------------------- #
# signal replay
# --------------------------------------------------------------------------- #


def _replay(
    frame: pd.DataFrame,
    *,
    action: str,
    stop: float | None,
    tp: float | None,
    metadata: dict | None = None,
):
    signal = ReplaySignal.from_payload(
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "symbol": "AAPL",
            "action": action,
            "entry_price": 100.0,
            "stop_loss": stop,
            "take_profit": tp,
            "timeframe": "1m",
            "source": "test",
            **({"metadata": metadata} if metadata else {}),
        },
        default_timeframe="1m",
    )
    adapter = SignalReplayAdapter(
        fee_params={"fees": "zero"},
        slippage_params={"slippage": "none"},
        risk_config=RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.5,
            max_positions=5,
            default_sl_atr_multiplier=2.0,
        ),
        initial_capital=100000.0,
        default_timeframe="1m",
    )
    result = adapter.run(
        signals=[signal],
        ohlcv_by_symbol={"AAPL": frame},
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )
    assert len(result.trades) == 1
    return result.trades[0]


def _replay_frame(opens, highs, lows, closes) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01T00:00:00Z", periods=len(opens), freq="1min"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000] * len(opens),
        }
    )


class TestSignalReplayGaps:
    # Bar0 fills the signal at its close (100); bar1 is the first protective bar.
    def test_long_gap_through_stop_fills_at_open(self):
        trade = _replay(
            _replay_frame([100, 90, 90], [100.5, 91, 90.5], [99.5, 88, 89.5], [100, 89, 90]),
            action="buy", stop=95.0, tp=140.0,
        )
        assert trade.exit_reason == "stop_loss"
        assert trade.exits[-1].price == pytest.approx(90.0)

    def test_short_gap_through_stop_fills_at_open(self):
        trade = _replay(
            _replay_frame(
                [100, 110, 110], [100.5, 112, 110.5], [99.5, 109, 109.5], [100, 111, 110]
            ),
            action="sell", stop=105.0, tp=60.0,
        )
        assert trade.exits[-1].price == pytest.approx(110.0)

    def test_long_gap_through_target_fills_at_open(self):
        trade = _replay(
            _replay_frame(
                [100, 115, 115], [100.5, 116, 115.5], [99.5, 114, 114.5], [100, 115, 115]
            ),
            action="buy", stop=90.0, tp=110.0,
        )
        assert trade.exit_reason == "take_profit"
        assert trade.exits[-1].price == pytest.approx(115.0)

    def test_short_gap_through_target_fills_at_open(self):
        trade = _replay(
            _replay_frame([100, 85, 85], [100.5, 86, 85.5], [99.5, 84, 84.5], [100, 85, 85]),
            action="sell", stop=120.0, tp=90.0,
        )
        assert trade.exits[-1].price == pytest.approx(85.0)

    @pytest.mark.parametrize(
        ("action", "stop", "tp", "bar", "expected"),
        [
            ("buy", 95.0, 140.0, (99.0, 99.5, 94.0, 96.0), 95.0),
            ("sell", 105.0, 60.0, (101.0, 106.0, 100.5, 104.0), 105.0),
            ("buy", 90.0, 110.0, (105.0, 112.0, 104.0, 111.0), 110.0),
            ("sell", 120.0, 90.0, (95.0, 96.0, 88.0, 89.0), 90.0),
        ],
    )
    def test_intrabar_touch_still_fills_at_the_level(self, action, stop, tp, bar, expected):
        o, h, lo, c = bar
        trade = _replay(
            _replay_frame([100, o, c], [100.5, h, c + 0.2], [99.5, lo, c - 0.2], [100, c, c]),
            action=action, stop=stop, tp=tp,
        )
        assert trade.exits[-1].price == pytest.approx(expected)

    def test_gapped_trailing_stop_fills_at_the_open(self):
        # Bar1's close (103) trails the 98 stop up to ~101.97; bar2 then OPENS
        # at 100.5, beyond the trailed stop — a gap, so the fill is the open,
        # and the reason stays trailing_stop.
        trade = _replay(
            _replay_frame(
                [100.0, 100.5, 100.5],
                [100.8, 103.5, 100.8],
                [99.8, 100.2, 100.0],
                [100.0, 103.0, 100.6],
            ),
            action="buy",
            stop=98.0,
            tp=140.0,
            metadata={"trailing_sl_config": {"mode": "percent", "percent": 0.01}},
        )
        assert trade.exit_reason == "trailing_stop"
        assert trade.exits[-1].price == pytest.approx(100.5)
        assert trade.meta["trailing_stop_updates"] >= 1


# --------------------------------------------------------------------------- #
# PyneCore exit containment
# --------------------------------------------------------------------------- #


def _pyne_trade(
    direction: str,
    exit_price: float,
    *,
    sl=None,
    tp=None,
    exit_time="2024-01-01T02:00:00+00:00",
) -> Trade:
    return Trade(
        id=1,
        symbol="T",
        direction=direction,
        entry_time="2024-01-01T01:00:00+00:00",
        entry_price=100.0,
        quantity=1.0,
        exits=[TradeExit(time=exit_time, price=exit_price, quantity=1.0, pnl=0.0, reason="signal")],
        pnl=0.0,
        meta={"sl_price": sl, "tp_price": tp},
    )


class TestPyneCoreContainment:
    """Frozen engine: containment restates to the recorded LEVEL only — the
    2026-11-13 gap→open restatement was reverted (frozen engines get no
    behaviour changes)."""

    def test_violating_exit_is_restated_to_the_level_even_on_a_gap_bar(self):
        # The exit bar opens below the 95 stop (a gap), but the frozen
        # containment still restates to the level, not the open.
        trade = _pyne_trade("LONG", 93.0, sl=95.0)
        _normalize_trade_exits([trade])
        assert trade.exits[-1].price == pytest.approx(95.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_short_target_violation_is_restated_to_the_level(self):
        trade = _pyne_trade("SHORT", 84.0, tp=90.0)
        _normalize_trade_exits([trade])
        assert trade.exits[-1].price == pytest.approx(90.0)
        assert trade.exits[-1].reason == "take_profit"

    def test_exit_inside_the_levels_is_untouched(self):
        trade = _pyne_trade("LONG", 97.0, sl=95.0, tp=105.0)
        _normalize_trade_exits([trade])
        assert trade.exits[-1].price == pytest.approx(97.0)
        assert trade.exits[-1].reason == "signal"


# --------------------------------------------------------------------------- #
# frozen engines refuse next_open (fail loud, not warn-and-run)
# --------------------------------------------------------------------------- #


class TestFrozenEnginesRefuseNextOpen:
    def _config(self, fill: str) -> BacktestConfig:
        return make_backtest_config(
            strategy_name="frozen",
            symbols=["T"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
            signal_exit_fill=fill,
        )

    @pytest.mark.parametrize("guard", [_vbt_frozen_fill, _pyne_frozen_fill, _lean_frozen_fill])
    def test_next_open_is_refused(self, guard):
        with pytest.raises(ValueError, match="next_open"):
            guard(self._config("next_open"))

    @pytest.mark.parametrize("guard", [_vbt_frozen_fill, _pyne_frozen_fill, _lean_frozen_fill])
    def test_legacy_close_fill_passes_through(self, guard):
        # "open" was removed (issue #153): BacktestConfig rejects it before the
        # guard runs, so only the legacy "close" fill remains to pass through.
        config = self._config("close")
        assert guard(config) is config

    @pytest.mark.parametrize("adapter", [VectorbtAdapter, PyneCoreAdapter, LeanAdapter])
    def test_run_refuses_next_open_before_touching_data(self, adapter, tmp_path):
        # The guard runs before data loading, so a next_open config fails loud
        # even with no OHLCV on disk.
        strategy = tmp_path / "strat.py"
        strategy.write_text("def generate_signals(df, params):\n    pass\n")
        with pytest.raises(ValueError, match="next_open"):
            adapter().run(self._config("next_open"), str(strategy))

    def test_default_signal_exit_fill_for_engine(self):
        assert default_signal_exit_fill_for_engine("native") == "next_open"
        assert default_signal_exit_fill_for_engine("vectorbt") == "close"
        assert default_signal_exit_fill_for_engine("pynecore") == "close"
        assert default_signal_exit_fill_for_engine("lean") == "close"


# --------------------------------------------------------------------------- #
# cross-engine: the gap is the bar's first event (review of #208, P2)
# --------------------------------------------------------------------------- #

# (direction, stop, tp, gap bar OHLC, expected fill, expected kind). Each bar
# OPENS beyond one level and later trades through the other: the open came
# first, so the gapped level wins at the open in every covered engine - also when the
# gap is the favourable one (target) and the later touch is the stop, where a
# stop-first range test would book a loss at the stop instead.
_GAP_FIRST_CASES = [
    ("LONG", 95.0, 110.0, (120.0, 125.0, 90.0, 100.0), 120.0, "tp"),
    ("SHORT", 105.0, 90.0, (80.0, 110.0, 75.0, 100.0), 80.0, "tp"),
    ("LONG", 95.0, 110.0, (90.0, 125.0, 88.0, 120.0), 90.0, "stop"),
    ("SHORT", 105.0, 90.0, (110.0, 112.0, 75.0, 80.0), 110.0, "stop"),
]
_REASONS = {"tp": {"tp", "take_profit"}, "stop": {"stop_loss"}}


class TestGapIsTheFirstEvent:
    @pytest.mark.parametrize(("direction", "stop", "tp", "bar", "fill", "kind"), _GAP_FIRST_CASES)
    def test_contract_primitive(self, direction, stop, tp, bar, fill, kind):
        o, h, lo, _ = bar
        assert contract.resolve_protective_exit(
            direction=direction, bar_open=o, high=h, low=lo, stop_price=stop, take_profit_price=tp
        ) == (kind, True)
        # The entry bar/step cannot be a gap: the range test (stop-first) decides.
        assert contract.resolve_protective_exit(
            direction=direction,
            bar_open=o,
            high=h,
            low=lo,
            stop_price=stop,
            take_profit_price=tp,
            allow_gap=False,
        ) == ("stop", False)

    @pytest.mark.parametrize(("direction", "stop", "tp", "bar", "fill", "kind"), _GAP_FIRST_CASES)
    def test_native(self, direction, stop, tp, bar, fill, kind):
        result = _native(
            [_flat(), _flat(), bar, _flat(bar[3])], direction=direction, stop=stop, take_profit=tp
        )
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(fill)
        assert trade.exits[-1].reason in _REASONS[kind]


    @pytest.mark.parametrize(("direction", "stop", "tp", "bar", "fill", "kind"), _GAP_FIRST_CASES)
    def test_signal_replay(self, direction, stop, tp, bar, fill, kind):
        o, h, lo, c = bar
        trade = _replay(
            _replay_frame([100, o, c], [100.5, h, c + 0.2], [99.5, lo, c - 0.2], [100, c, c]),
            action="buy" if direction == "LONG" else "sell",
            stop=stop,
            tp=tp,
        )
        assert trade.exits[-1].price == pytest.approx(fill)
        assert trade.exit_reason in _REASONS[kind]


# --------------------------------------------------------------------------- #
# SHORT protective exits use the ask side (native, #472)
# --------------------------------------------------------------------------- #

_SPREAD = 0.4
# (stop, tp, bar, expected fill). The level is ask-denominated: it triggers
# when the ASK reaches it and fills AT it, so no spread is added on top.
# A gap is judged on the ask side too — a bid open below the target is not a
# gap when the ask open is still above it.
_SHORT_SPREAD_CASES = [
    (105.0, None, (110.0, 112.0, 109.0, 111.0), 110.0 + _SPREAD),  # ask gaps stop
    (105.0, None, (101.0, 106.0, 100.5, 104.0), 105.0),  # ask touches stop
    (120.0, 90.0, (95.0, 96.0, 88.0, 89.0), 90.0),  # ask touches target
    (120.0, 90.0, (89.4, 90.0, 88.0, 89.0), 89.8),  # ask gaps target
]


class TestShortProtectiveFillsUseTheAskSide:
    @pytest.mark.parametrize(("stop", "tp", "bar", "fill"), _SHORT_SPREAD_CASES)
    def test_native(self, stop, tp, bar, fill):
        result = _native(
            [_flat(), _flat(), bar, _flat(bar[3])],
            direction="SHORT",
            stop=stop,
            take_profit=tp,
            spread=_SPREAD,
            parameters={"spread_price_col": "spread_price"},
        )
        (trade,) = result.trades
        assert trade.entry_price == pytest.approx(100.0)  # SHORT entry hits the bid
        assert trade.exits[-1].price == pytest.approx(fill)


