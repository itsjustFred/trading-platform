"""Realistic-fill semantics (owner decision 2026-11-13), per engine.

A. A bar that OPENS beyond a protective stop/target fills at its OPEN (the
   price actually available), not at the level the market skipped. An
   intrabar touch (the bar opens on the safe side) keeps filling at the level.
B. Signal-driven exits fill at the NEXT bar's open, like entries. A signal on
   the last bar has no next bar: the forced end-of-data close (at the last
   bar's close) takes the position and the trade is flagged
   ``meta["signal_exit_deferred_to_end_of_data"]``.

Covered engines: native ``TradeEngine``, ``VectorbtAdapter`` (+ its intrabar
walk), ``SignalReplayAdapter`` and PyneCore's exit-containment helper, plus
the shared ``contract`` primitives they all route through.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from risk.config import RiskConfig
from runner.engine import TradeEngine
from runner.engines import contract
from runner.engines.base import BacktestConfig
from runner.engines.pynecore_adapter import _normalize_trade_exits
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter
from runner.engines.vectorbt_adapter import VectorbtAdapter, _resolve_intrabar_exit
from shared.models import Trade, TradeExit
from tests.helpers import with_zero_fees
from tests.helpers import write_ohlcv as _write_ohlcv

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
        assert BacktestConfig(
            strategy_name="x",
            symbols=["X"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-02",
        ).signal_exit_fill == "next_open"
        for legacy in ("close", "open", "Next_Open"):
            assert BacktestConfig(
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
        "parameters": {"fees": "zero", **(parameters or {})},
    }
    payload.update(config_kwargs)
    return TradeEngine(BacktestConfig(**payload), symbol="T").run(frame, strategy_fn)


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
        result = _native(rows + [_flat(82.0)], exit_bar=3, stop=95.0)
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(103.0)
        assert trade.exits[-1].reason == "signal"

    def test_gap_through_the_stop_on_the_exit_bar_keeps_the_stop_reason(self):
        rows = [_flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0), (90.0, 91.0, 88.0, 89.0), _flat(89.0)]
        result = _native(rows, exit_bar=3, stop=95.0)
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(90.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_exit_and_entry_signalled_together_reverse_at_one_open(self):
        # Bar0 enters long (fills bar1). Bar3 signals exit AND a new entry: the
        # old position closes at bar4's open, then the new one opens at that same
        # open - the exit must not eat the fresh position.
        frame = _bars(self.ROWS + [_flat(105.0), _flat(106.0)])

        def strategy_fn(df, params):
            entries = pd.Series(False, index=df.index)
            exits = pd.Series(False, index=df.index)
            entries.iloc[0] = True
            entries.iloc[3] = True
            exits.iloc[3] = True
            return entries, exits, {"direction": pd.Series("LONG", index=df.index)}, []

        cfg = BacktestConfig(
            strategy_name="reverse",
            symbols=["T"],
            timeframe="1h",
            start_date="2024-01-01",
            end_date="2024-01-31",
            initial_capital=100_000.0,
            risk_per_trade_pct=0.01,
            max_leverage=100.0,
            parameters={"fees": "zero"},
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

    def test_legacy_open_opt_in_still_fills_at_the_signal_bar_open(self):
        result = _native(self.ROWS, exit_bar=3, signal_exit_fill="open")
        (trade,) = result.trades
        assert trade.exits[-1].price == pytest.approx(102.0)  # bar3 open
        assert pd.Timestamp(trade.exits[-1].time) == pd.Timestamp("2024-01-01 03:00", tz="UTC")

    def test_execution_model_metadata_records_the_exit_rules(self):
        result = _native(self.ROWS, exit_bar=3)
        model = result.trades[0].meta["execution_model"]
        assert model["protective_gap_fill"] == "open"
        assert model["signal_exit_fill"] == "next_open"


# --------------------------------------------------------------------------- #
# vectorbt
# --------------------------------------------------------------------------- #


def _vbt_strategy(entry_bar: int, exit_bar: int | None, *, direction: str = "LONG", stop=None, tp=None) -> str:
    exit_line = f"exits.iloc[{exit_bar}] = True" if exit_bar is not None else "pass"
    stop_line = (
        f'indicators["stop_loss"] = pd.Series({stop!r}, index=df.index)' if stop is not None else ""
    )
    tp_line = (
        f'indicators["take_profit"] = pd.Series({tp!r}, index=df.index)' if tp is not None else ""
    )
    return f'''
import pandas as pd


def generate_signals(df, params):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[{entry_bar}] = True
    {exit_line}
    indicators = {{"direction": pd.Series({direction!r}, index=df.index)}}
    {stop_line}
    {tp_line}
    return entries, exits, indicators, []
'''


def _vbt_run(tmp_path: Path, rows, strategy_text: str, *, intrabar: pd.DataFrame | None = None, **cfg):
    _write_ohlcv(tmp_path, "T", _bars(rows))
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
        "data_dir": str(tmp_path),
        "parameters": {},
    }
    if intrabar is not None:
        _write_ohlcv(tmp_path, "T", intrabar, timeframe="15m")
        payload["modeling_resolution"] = "15m"
    payload.update(cfg)
    path = tmp_path / "strat.py"
    path.write_text(strategy_text)
    return VectorbtAdapter().run(BacktestConfig(**with_zero_fees(payload)), str(path))


def _subbars(rows, per_bar: int = 4) -> pd.DataFrame:
    """Expand each hourly bar into ``per_bar`` flat-ish 15m sub-bars.

    The first sub-bar carries the bar's open, the last its close; the bar's
    high/low land on the second sub-bar so intrabar touches are real.
    """
    records = []
    ts0 = pd.Timestamp("2024-01-01", tz="UTC")
    for h, (o, hi, lo, c) in enumerate(rows):
        for k in range(per_bar):
            sub_open = o if k == 0 else (o + c) / 2
            sub_close = c if k == per_bar - 1 else (o + c) / 2
            sub_high = max(hi if k == 1 else max(sub_open, sub_close), sub_open, sub_close)
            sub_low = min(lo if k == 1 else min(sub_open, sub_close), sub_open, sub_close)
            records.append(
                {
                    "timestamp": ts0 + pd.Timedelta(hours=h, minutes=15 * k),
                    "open": sub_open,
                    "high": sub_high,
                    "low": sub_low,
                    "close": sub_close,
                    "volume": 100.0,
                }
            )
    return pd.DataFrame(records)


def _walk(direction, stop, tp, sub_rows):
    """Run the intrabar walk over hand-written 15m sub-bars (entry at 00:00)."""
    sub = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01 00:00", periods=len(sub_rows), freq="15min", tz="UTC"),
            "open": [r[0] for r in sub_rows],
            "high": [r[1] for r in sub_rows],
            "low": [r[2] for r in sub_rows],
            "close": [r[3] for r in sub_rows],
        }
    )
    return _resolve_intrabar_exit(
        intrabar_df=sub,
        intrabar_timeframe="15m",
        base_timeframe="1h",
        direction=direction,
        entry_time="2024-01-01T00:00:00+00:00",
        scheduled_exit_time="2024-01-01T03:00:00+00:00",
        stop_price=stop,
        take_profit_price=tp,
        parameters={},
    )


class TestVectorbtIntrabarWalkGaps:
    # Sub-bar 0 is the entry sub-bar; sub-bar 1 gaps.
    def test_long_gap_through_stop_fills_at_the_sub_bar_open(self):
        hit = _walk("LONG", 95.0, None, [(100, 100.5, 99.5, 100), (90, 91, 88, 89)])
        assert hit["price"] == pytest.approx(90.0)
        assert hit["reason"] == "stop_loss"

    def test_short_gap_through_stop_fills_at_the_sub_bar_open(self):
        hit = _walk("SHORT", 105.0, None, [(100, 100.5, 99.5, 100), (110, 112, 109, 111)])
        assert hit["price"] == pytest.approx(110.0)
        assert hit["reason"] == "stop_loss"

    def test_long_gap_through_target_fills_at_the_sub_bar_open(self):
        hit = _walk("LONG", 95.0, 110.0, [(100, 100.5, 99.5, 100), (115, 116, 114, 115)])
        assert hit["price"] == pytest.approx(115.0)
        assert hit["reason"] == "take_profit"

    def test_short_gap_through_target_fills_at_the_sub_bar_open(self):
        hit = _walk("SHORT", 105.0, 90.0, [(100, 100.5, 99.5, 100), (85, 86, 84, 85)])
        assert hit["price"] == pytest.approx(85.0)
        assert hit["reason"] == "take_profit"

    @pytest.mark.parametrize(
        ("direction", "stop", "tp", "sub", "expected", "reason"),
        [
            ("LONG", 95.0, None, (99, 99.5, 94, 96), 95.0, "stop_loss"),
            ("SHORT", 105.0, None, (101, 106, 100.5, 104), 105.0, "stop_loss"),
            ("LONG", 95.0, 110.0, (105, 112, 104, 111), 110.0, "take_profit"),
            ("SHORT", 105.0, 90.0, (95, 96, 88, 89), 90.0, "take_profit"),
        ],
    )
    def test_intrabar_touch_still_fills_at_the_level(
        self, direction, stop, tp, sub, expected, reason
    ):
        hit = _walk(direction, stop, tp, [(100, 100.5, 99.5, 100), sub])
        assert hit["price"] == pytest.approx(expected)
        assert hit["reason"] == reason

    def test_entry_sub_bar_is_exempt_from_the_gap_rule(self):
        # Entry fills at the first sub-bar's open (100) which trades down to 94:
        # a touch of the 95 stop, not a gap.
        hit = _walk("LONG", 95.0, None, [(100, 100.5, 94, 96)])
        assert hit["price"] == pytest.approx(95.0)

    def test_signal_exit_window_stops_at_the_exit_bar_open(self):
        # A stop touched only inside the exit bar's sub-bars: a next-open signal
        # exit filled at that bar's open, so the touch comes too late.
        sub_rows = [(100, 100.5, 99.5, 100)] * 4 + [(100, 100.5, 94, 96)] * 4
        sub = pd.DataFrame(
            {
                "timestamp": pd.date_range("2024-01-01", periods=8, freq="15min", tz="UTC"),
                "open": [r[0] for r in sub_rows],
                "high": [r[1] for r in sub_rows],
                "low": [r[2] for r in sub_rows],
                "close": [r[3] for r in sub_rows],
            }
        )
        kwargs = dict(
            intrabar_df=sub,
            intrabar_timeframe="15m",
            base_timeframe="1h",
            direction="LONG",
            entry_time="2024-01-01T00:00:00+00:00",
            scheduled_exit_time="2024-01-01T01:00:00+00:00",
            stop_price=95.0,
            take_profit_price=None,
        )
        assert _resolve_intrabar_exit(**kwargs) is not None  # legacy: through the bar
        assert _resolve_intrabar_exit(**kwargs, exit_at_open=True) is None


class TestVectorbtAdapterSignalExits:
    ROWS = [_flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0), _flat(103.0), _flat(104.0)]

    def test_signal_exit_fills_at_the_next_bar_open(self, tmp_path):
        result = _vbt_run(tmp_path, self.ROWS, _vbt_strategy(0, 3))
        (trade,) = result.trades
        assert trade.entry_price == pytest.approx(100.0)
        assert trade.exits[0].price == pytest.approx(103.0)  # bar4 open
        assert pd.Timestamp(trade.exits[0].time) == pd.Timestamp("2024-01-01 04:00", tz="UTC")
        assert "signal_exit_deferred_to_end_of_data" not in trade.meta

    def test_last_bar_signal_is_closed_by_end_of_data_and_flagged(self, tmp_path):
        result = _vbt_run(tmp_path, self.ROWS, _vbt_strategy(0, len(self.ROWS) - 1))
        (trade,) = result.trades
        assert trade.exits[0].reason == "end_of_data"
        assert trade.exits[0].price == pytest.approx(104.0)  # last close
        assert trade.meta["signal_exit_deferred_to_end_of_data"] is True

    def test_plain_end_of_data_close_is_not_flagged(self, tmp_path):
        result = _vbt_run(tmp_path, self.ROWS, _vbt_strategy(0, None))
        (trade,) = result.trades
        assert trade.exits[0].reason == "end_of_data"
        assert trade.exits[0].price == pytest.approx(104.0)
        assert "signal_exit_deferred_to_end_of_data" not in trade.meta

    def test_exit_and_entry_signalled_together_reverse_at_one_open(self, tmp_path):
        text = '''
import pandas as pd

def generate_signals(df, params):
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    entries.iloc[0] = True
    entries.iloc[3] = True
    exits.iloc[3] = True
    return entries, exits, {"direction": pd.Series("LONG", index=df.index)}, []
'''
        rows = self.ROWS + [_flat(105.0), _flat(106.0)]
        result = _vbt_run(tmp_path, rows, text)
        first, second = result.trades
        assert first.exits[0].price == pytest.approx(103.0)
        assert second.entry_price == pytest.approx(103.0)

    def test_legacy_close_opt_in_fills_on_the_signal_bar(self, tmp_path):
        result = _vbt_run(tmp_path, self.ROWS, _vbt_strategy(0, 3), signal_exit_fill="close")
        (trade,) = result.trades
        assert trade.exits[0].price == pytest.approx(102.0)  # bar3 close

    def test_end_to_end_gap_through_the_stop_with_intrabar_data(self, tmp_path):
        rows = [_flat(), _flat(), (90.0, 91.0, 88.0, 89.0), _flat(90.0), _flat(90.0)]
        result = _vbt_run(
            tmp_path, rows, _vbt_strategy(0, 4, stop=95.0), intrabar=_subbars(rows)
        )
        (trade,) = result.trades
        assert trade.exits[0].price == pytest.approx(90.0)
        assert trade.exits[0].reason == "stop_loss"

    def test_end_to_end_intrabar_touch_fills_at_the_stop(self, tmp_path):
        rows = [_flat(), _flat(), (99.0, 99.5, 94.0, 96.0), _flat(96.0), _flat(96.0)]
        result = _vbt_run(
            tmp_path, rows, _vbt_strategy(0, 4, stop=95.0), intrabar=_subbars(rows)
        )
        (trade,) = result.trades
        assert trade.exits[0].price == pytest.approx(95.0)


# --------------------------------------------------------------------------- #
# signal replay
# --------------------------------------------------------------------------- #


def _replay(frame: pd.DataFrame, *, action: str, stop: float | None, tp: float | None):
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
        },
        default_timeframe="1m",
    )
    adapter = SignalReplayAdapter(
        fee_params={"fees": "zero"},
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
            _replay_frame([100, 110, 110], [100.5, 112, 110.5], [99.5, 109, 109.5], [100, 111, 110]),
            action="sell", stop=105.0, tp=60.0,
        )
        assert trade.exits[-1].price == pytest.approx(110.0)

    def test_long_gap_through_target_fills_at_open(self):
        trade = _replay(
            _replay_frame([100, 115, 115], [100.5, 116, 115.5], [99.5, 114, 114.5], [100, 115, 115]),
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


# --------------------------------------------------------------------------- #
# PyneCore exit containment
# --------------------------------------------------------------------------- #


def _pyne_trade(direction: str, exit_price: float, *, sl=None, tp=None, exit_time="2024-01-01T02:00:00+00:00") -> Trade:
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
    def _frame(self, bar2_open: float) -> pd.DataFrame:
        return _bars([_flat(), _flat(), (bar2_open, bar2_open + 1, bar2_open - 1, bar2_open)])

    def test_violating_exit_without_a_gap_is_restated_to_the_level(self):
        trade = _pyne_trade("LONG", 93.0, sl=95.0)
        _normalize_trade_exits([trade], self._frame(97.0))  # bar opens above the stop
        assert trade.exits[-1].price == pytest.approx(95.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_violating_exit_on_a_gap_bar_is_restated_to_the_open(self):
        trade = _pyne_trade("LONG", 93.0, sl=95.0)
        _normalize_trade_exits([trade], self._frame(92.0))  # bar opens below the stop
        assert trade.exits[-1].price == pytest.approx(92.0)
        assert trade.exits[-1].reason == "stop_loss"

    def test_short_target_gap_is_restated_to_the_open(self):
        trade = _pyne_trade("SHORT", 84.0, tp=90.0)
        _normalize_trade_exits([trade], self._frame(86.0))  # opens below the target
        assert trade.exits[-1].price == pytest.approx(86.0)
        assert trade.exits[-1].reason == "take_profit"

    def test_without_a_frame_the_level_is_used(self):
        trade = _pyne_trade("LONG", 93.0, sl=95.0)
        _normalize_trade_exits([trade])
        assert trade.exits[-1].price == pytest.approx(95.0)
