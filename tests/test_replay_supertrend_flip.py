"""Issue #168: the ``supertrend_flip`` workflow in the signal-replay backtest.

Workflow: TP1 partial exit at ``tp1_price`` -> stop to breakeven -> wait for
the first ready+green Supertrend bar -> close the remainder at the OPEN of
the bar after the first ready+red bar.

Most tests pin the state machine with a hand-specified Supertrend series
(``_stub_supertrend``), so each phase is exercised on exactly the bar the
test names. The no-lookahead tests use the real RamJet Supertrend.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from risk.config import RiskConfig
from runner.engines import signal_replay_adapter as replay_module
from runner.engines.signal_replay_adapter import ReplaySignal, SignalReplayAdapter
from runner.fees import NotionalFeeModel
from shared.trailing import parse_supertrend_flip_plan
from signal_bots.ramjet.processor import RamJetSignalProcessor, calculate_supertrend

ENTRY = 100.0
STOP = 95.0
TP1 = 110.0

FLIP_CONFIG = {
    "mode": "supertrend_flip",
    "breakeven_after_tp1": True,
    "activation_phase": "wait_for_first_green_after_tp1",
    "exit_phase": "exit_on_first_red_after_activation",
    "supertrend_period": 3,
    "supertrend_multiplier": 1.0,
}


def _frame(bars: list[tuple[float, float, float, float]], start: str = "2026-01-01T00:00:00Z"):
    """OHLC bars as ``(open, high, low, close)``, 1 minute apart."""
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(start, periods=len(bars), freq="1min"),
            "open": [bar[0] for bar in bars],
            "high": [bar[1] for bar in bars],
            "low": [bar[2] for bar in bars],
            "close": [bar[3] for bar in bars],
            "volume": [1000] * len(bars),
        }
    )


def _flat(price: float = 102.0) -> tuple[float, float, float, float]:
    """A quiet bar that touches neither the stop nor TP1 nor breakeven."""
    return (price, price + 0.5, price - 0.5, price)


#: Bar 0 is the entry bar (fill at its close, 100). Bar 1 touches TP1.
ENTRY_BAR = (100.0, 100.2, 99.8, 100.0)
TP1_BAR = (103.0, 110.5, 102.0, 108.0)


def _signal(
    *,
    trailing: dict | None = None,
    metadata: dict | None = None,
    action: str = "buy",
    take_profit: float | None = TP1,
    symbol: str = "AAPL",
    timestamp: str = "2026-01-01T00:00:00Z",
    no_stop: bool = False,
) -> ReplaySignal:
    meta = {
        "trailing_sl_config": dict(FLIP_CONFIG if trailing is None else trailing),
        "tp1_price": TP1,
        "tp1_exit_percent": 0.5,
    }
    meta.update(metadata or {})
    return ReplaySignal.from_payload(
        {
            "timestamp": timestamp,
            "symbol": symbol,
            "action": action,
            "entry_price": ENTRY,
            "stop_loss": None if no_stop else (STOP if action == "buy" else 105.0),
            "take_profit": take_profit,
            "timeframe": "1m",
            "metadata": meta,
        },
        default_timeframe="1m",
    )


def _adapter(**kwargs) -> SignalReplayAdapter:
    kwargs.setdefault("fee_params", {"fees": "zero"})
    kwargs.setdefault("slippage_params", {"slippage": "none"})
    # ``initial_capital`` is overridable: it is the lever that sizes a position
    # to a known quantity (risk % of the balance / stop distance).
    kwargs.setdefault("initial_capital", 100_000.0)
    return SignalReplayAdapter(
        risk_config=RiskConfig(
            max_risk_per_trade=0.01,
            max_total_exposure=0.5,
            max_positions=5,
            max_notional_per_trade=1.0,
        ),
        default_timeframe="1m",
        **kwargs,
    )


def _run(frames: dict[str, pd.DataFrame] | pd.DataFrame, signals=None, adapter=None):
    if isinstance(frames, pd.DataFrame):
        frames = {"AAPL": frames}
    return (adapter or _adapter()).run(
        signals=signals or [_signal()],
        ohlcv_by_symbol=frames,
        timeframe="1m",
        backtest_start="2026-01-01",
        backtest_end="2026-01-01",
    )


def _stub_supertrend(monkeypatch, states: str) -> list[tuple]:
    """Replace the Supertrend with a hand-specified series.

    ``states`` is one character per bar: ``n`` = not ready,
    ``g`` = ready+green, ``r`` = ready+red. Returns the call log.
    """
    calls: list[tuple] = []

    def _fake(df: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
        calls.append((len(df), period, multiplier))
        assert len(states) == len(df), "stub must describe every bar"
        out = df.copy()
        out["supertrend"] = [state == "g" for state in states]
        out["supertrend_ready"] = [state != "n" for state in states]
        return out

    monkeypatch.setattr(replay_module, "calculate_supertrend", _fake)
    return calls


def _times(frame: pd.DataFrame, index: int) -> str:
    return pd.Timestamp(frame["timestamp"].iloc[index]).isoformat()


# ── TP1 partial and breakeven ────────────────────────────────────────────────


def test_tp1_exits_the_configured_fraction_at_the_tp1_price(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    result = _run(frame)

    assert len(result.trades) == 1
    trade = result.trades[0]
    quantity = trade.quantity
    assert quantity > 0
    tp1_leg = trade.exits[0]
    assert tp1_leg.reason == "tp1"
    assert tp1_leg.price == TP1
    assert tp1_leg.time == _times(frame, 1)
    assert tp1_leg.quantity == pytest.approx(quantity * 0.5)
    assert tp1_leg.pnl == pytest.approx((TP1 - ENTRY) * quantity * 0.5)
    # The remainder is still open at the end of the data.
    last = trade.exits[-1]
    assert last.reason == "end_of_data"
    assert last.quantity == pytest.approx(quantity * 0.5)
    assert trade.pnl == pytest.approx(tp1_leg.pnl + last.pnl)
    assert trade.position_size == pytest.approx(quantity)
    assert trade.meta["supertrend_flip"]["tp1_time"] == _times(frame, 1)


def test_stop_moves_to_breakeven_after_tp1_and_closes_the_remainder(monkeypatch) -> None:
    breakeven_bar = (101.0, 101.5, 99.0, 99.5)  # low below entry, above the old stop
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), breakeven_bar, _flat()])
    _stub_supertrend(monkeypatch, "rrrrr")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "breakeven_stop"]
    remainder = trade.exits[1]
    assert remainder.price == ENTRY
    assert remainder.time == _times(frame, 3)
    assert remainder.pnl == pytest.approx(0.0)
    assert trade.exit_reason == "breakeven_stop"
    assert trade.stop_loss == ENTRY
    assert trade.meta["signal_stop_loss"] == STOP
    assert trade.meta["trailing_stop_updates"] == 1


def test_breakeven_stop_takes_effect_from_the_bar_after_tp1(monkeypatch) -> None:
    """Inside the TP1 bar the order of the touches is unknown: the moved stop
    is not applied to the bar that triggered it."""
    tp1_bar_dipping_below_entry = (103.0, 110.5, 99.0, 108.0)
    frame = _frame([ENTRY_BAR, tp1_bar_dipping_below_entry, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "end_of_data"]


def test_without_breakeven_the_initial_stop_protects_the_remainder(monkeypatch) -> None:
    config = {**FLIP_CONFIG, "breakeven_after_tp1": False}
    frame = _frame([ENTRY_BAR, TP1_BAR, (101.0, 101.5, 99.0, 99.5), (96.0, 96.0, 94.0, 94.5)])
    _stub_supertrend(monkeypatch, "rrrr")
    trade = _run(frame, signals=[_signal(trailing=config)]).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "stop_loss"]
    assert trade.exits[1].price == STOP
    assert trade.exits[1].time == _times(frame, 3)
    assert trade.meta["trailing_stop_updates"] == 0


def test_a_full_tp1_fraction_closes_the_whole_position_at_tp1(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat()])
    _stub_supertrend(monkeypatch, "rrr")
    trade = _run(frame, signals=[_signal(metadata={"tp1_exit_percent": 1.0})]).trades[0]

    assert len(trade.exits) == 1
    assert trade.exits[0].reason == "tp1"
    assert trade.exits[0].price == TP1
    assert trade.exits[0].quantity == pytest.approx(trade.quantity)


# ── TP1 partial quantity: whole shares, floored (#168 review decision) ──────


def test_tp1_partial_quantity_is_floored_to_whole_shares(monkeypatch) -> None:
    """The TP1 tranche is whole shares, FLOORED, like the original bot
    (``int(qty * tp1_exit_percent)``): 7 shares at a 0.5 fraction sells 3, not
    3.5."""
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    trade = _run(frame, adapter=_adapter(initial_capital=3500.0)).trades[0]

    assert trade.quantity == 7.0
    tp1_leg, last = trade.exits
    assert [leg.reason for leg in trade.exits] == ["tp1", "end_of_data"]
    assert tp1_leg.quantity == 3.0
    assert tp1_leg.pnl == pytest.approx((TP1 - ENTRY) * 3.0)
    assert last.quantity == 4.0
    assert last.pnl == pytest.approx((102.0 - ENTRY) * 4.0)
    assert trade.pnl == pytest.approx(38.0)
    assert trade.pnl_pct == pytest.approx(38.0 / (ENTRY * 7.0) * 100.0)
    assert trade.meta["supertrend_flip"]["tp1_skipped_time"] is None


def test_a_fractional_position_floors_the_partial_to_whole_shares(monkeypatch) -> None:
    """7.5 shares at a 0.5 fraction: 3.75 floors to 3 sold, 4.5 stay open."""
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    trade = _run(frame, adapter=_adapter(initial_capital=3750.0)).trades[0]

    assert trade.quantity == 7.5
    tp1_leg, last = trade.exits
    assert tp1_leg.quantity == 3.0
    assert tp1_leg.pnl == pytest.approx((TP1 - ENTRY) * 3.0)
    assert last.quantity == 4.5
    assert last.reason == "end_of_data"


def test_a_tp1_that_floors_to_zero_shares_is_skipped_like_the_original_bot(
    monkeypatch,
) -> None:
    """The original bot skips TP1 when the tranche floors to zero shares
    ("No shares to sell at TP1. Skipping."): no sell, no breakeven stop, and
    ``tp1_hit`` stays false, so the Supertrend workflow never arms and the
    whole position stays open under the ORIGINAL stop.

    1 share at a 0.5 fraction sells 0.5 shares, which is 0 whole shares.
    """
    stop_bar = (97.0, 97.5, 94.0, 94.5)
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), stop_bar, _flat()])
    _stub_supertrend(monkeypatch, "ggggg")
    trade = _run(frame, adapter=_adapter(initial_capital=500.0)).trades[0]

    assert trade.quantity == 1.0
    # The whole position leaves on the initial stop: no TP1 leg at all.
    assert [leg.reason for leg in trade.exits] == ["stop_loss"]
    assert trade.exits[0].price == STOP
    assert trade.exits[0].quantity == pytest.approx(1.0)
    assert trade.meta["final_stop_loss"] == STOP
    assert trade.meta["trailing_stop_updates"] == 0
    flip = trade.meta["supertrend_flip"]
    assert flip["tp1_time"] is None
    assert flip["tp1_skipped_time"] == _times(frame, 1)
    # The workflow never left its first phase, so no trail was ever armed.
    assert flip["phase"] == "before_tp1"
    assert flip["activation_time"] is None


def test_the_skip_is_sticky_until_the_position_closes(monkeypatch) -> None:
    """The original bot retries TP1 on every pass and skips it again; the
    replay mirrors that by keeping the position in the before-TP1 phase, so a
    later bar that touches TP1 still sells nothing and the trail still never
    arms."""
    frame = _frame([ENTRY_BAR, TP1_BAR, TP1_BAR, TP1_BAR, _flat()])
    _stub_supertrend(monkeypatch, "ggggg")
    trade = _run(frame, adapter=_adapter(initial_capital=500.0)).trades[0]

    assert [leg.reason for leg in trade.exits] == ["end_of_data"]
    flip = trade.meta["supertrend_flip"]
    assert flip["tp1_time"] is None
    assert flip["tp1_skipped_time"] == _times(frame, 1)
    assert flip["phase"] == "before_tp1"
    assert trade.meta["final_stop_loss"] == STOP


def test_excursions_and_equity_use_the_floored_quantities(monkeypatch) -> None:
    """After the partial, MFE/MAE are measured on the shares that are still
    open (4 of 7) and the equity curve books the floored legs' net PnL."""
    tp1_bar = (109.0, 110.0, 108.0, 109.5)  # touches TP1 (110) with its high
    after_tp1 = (111.0, 118.0, 99.5, 100.5)  # high 118, low 99.5 -> breakeven stop
    frame = _frame([ENTRY_BAR, tp1_bar, after_tp1])
    _stub_supertrend(monkeypatch, "rrr")
    result = _run(frame, adapter=_adapter(initial_capital=3500.0))
    trade = result.trades[0]

    assert trade.quantity == 7.0
    tp1_leg, remainder = trade.exits
    assert [leg.reason for leg in trade.exits] == ["tp1", "breakeven_stop"]
    assert (tp1_leg.quantity, remainder.quantity) == (3.0, 4.0)
    assert remainder.price == ENTRY
    assert remainder.pnl == pytest.approx(0.0)
    # Only the 4 shares still open are measured on the last bar: the full
    # 7-share reading would give (118 - 100) * 7 = 126 and (99.5 - 100) * 7.
    assert trade.mfe == pytest.approx((118.0 - ENTRY) * 4.0)
    assert trade.mfe > (110.0 - ENTRY) * 7.0  # ... and the TP1 bar's 7-share run
    assert trade.mae == pytest.approx((99.5 - ENTRY) * 4.0)
    assert trade.pnl == pytest.approx((TP1 - ENTRY) * 3.0)
    assert result.equity_curve[-1].equity == pytest.approx(3500.0 + trade.net_pnl)
    assert result.equity_curve[-1].time == trade.exits[-1].time


# ── stop before TP1 ──────────────────────────────────────────────────────────


def test_stop_before_tp1_closes_the_whole_position(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, _flat(), (97.0, 97.5, 94.0, 94.5), TP1_BAR])
    _stub_supertrend(monkeypatch, "gggg")
    trade = _run(frame).trades[0]

    assert len(trade.exits) == 1
    assert trade.exits[0].reason == "stop_loss"
    assert trade.exits[0].price == STOP
    assert trade.exits[0].time == _times(frame, 2)
    assert trade.exits[0].quantity == pytest.approx(trade.quantity)
    assert trade.meta["supertrend_flip"]["tp1_time"] is None


def test_stop_and_tp1_in_the_same_bar_follow_the_stop_first_convention(monkeypatch) -> None:
    """Same-bar SL/TP is stop-first (``resolve_same_bar_exit``): the whole
    position stops out and TP1 never happens."""
    frame = _frame([ENTRY_BAR, (100.0, 111.0, 94.0, 100.0), _flat()])
    _stub_supertrend(monkeypatch, "ggg")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["stop_loss"]
    assert trade.exits[0].price == STOP


def test_supertrend_is_ignored_before_tp1(monkeypatch) -> None:
    """Green-then-red before TP1 neither activates nor exits."""
    frame = _frame([ENTRY_BAR, _flat(), _flat(), _flat(), _flat()])
    _stub_supertrend(monkeypatch, "ggrgr")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["end_of_data"]


# ── gap fills at the open (#287) ─────────────────────────────────────────────
#
# Owner decision (same as #208 / the native engine): a bar that OPENS beyond a
# protective level gapped through it — the fill is the bar's open, the price
# actually available, never the skipped level. Intrabar touches keep filling at
# the level. The flip workflow's stop and TP1 exits follow the same rule; the
# open is the bar's first event, so a gap also beats a later same-bar touch of
# the other level.


def test_gap_through_the_stop_before_tp1_fills_at_the_open(monkeypatch) -> None:
    """Bar 2 opens at 94, below the 95 stop: the stop fills at 94, not 95."""
    gap_stop_bar = (94.0, 94.5, 93.0, 94.2)
    frame = _frame([ENTRY_BAR, _flat(), gap_stop_bar, TP1_BAR])
    _stub_supertrend(monkeypatch, "gggg")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["stop_loss"]
    assert trade.exits[0].price == 94.0
    assert trade.exits[0].time == _times(frame, 2)


def test_gap_through_tp1_fills_the_tranche_at_the_open(monkeypatch) -> None:
    """Bar 1 opens at 111, above TP1 110: the TP1 tranche fills at 111."""
    gap_tp1_bar = (111.0, 112.0, 110.5, 111.5)
    frame = _frame([ENTRY_BAR, gap_tp1_bar, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    trade = _run(frame).trades[0]

    tp1_leg, last = trade.exits
    assert tp1_leg.reason == "tp1"
    assert tp1_leg.price == 111.0
    assert tp1_leg.time == _times(frame, 1)
    assert tp1_leg.pnl == pytest.approx((111.0 - ENTRY) * tp1_leg.quantity)
    assert last.reason == "end_of_data"


def test_gap_through_tp1_with_a_full_fraction_closes_at_the_open(monkeypatch) -> None:
    gap_tp1_bar = (111.0, 112.0, 110.5, 111.5)
    frame = _frame([ENTRY_BAR, gap_tp1_bar, _flat()])
    _stub_supertrend(monkeypatch, "rrr")
    trade = _run(frame, signals=[_signal(metadata={"tp1_exit_percent": 1.0})]).trades[0]

    assert len(trade.exits) == 1
    assert trade.exits[0].reason == "tp1"
    assert trade.exits[0].price == 111.0
    assert trade.exits[0].quantity == pytest.approx(trade.quantity)


def test_gap_through_the_breakeven_stop_fills_at_the_open(monkeypatch) -> None:
    """After TP1 the stop sits at breakeven (100); a bar opening at 99 gaps
    through it and fills at 99, not 100."""
    gap_bar = (99.0, 99.5, 98.0, 98.5)
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), gap_bar, _flat()])
    _stub_supertrend(monkeypatch, "rrrrr")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "breakeven_stop"]
    remainder = trade.exits[1]
    assert remainder.price == 99.0
    assert remainder.time == _times(frame, 3)
    assert remainder.pnl == pytest.approx((99.0 - ENTRY) * remainder.quantity)


def test_a_gap_past_the_stop_beats_a_later_same_bar_tp1_touch(monkeypatch) -> None:
    """The open is the bar's first event: a bar that opens below the stop and
    later trades up through TP1 still stops out — at the open."""
    gap_and_recover = (94.0, 111.5, 93.0, 105.0)
    frame = _frame([ENTRY_BAR, _flat(), gap_and_recover, _flat()])
    _stub_supertrend(monkeypatch, "gggg")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["stop_loss"]
    assert trade.exits[0].price == 94.0
    assert trade.exits[0].time == _times(frame, 2)
    assert trade.meta["supertrend_flip"]["tp1_time"] is None


def test_a_gap_through_tp1_fills_the_tranche_then_stops_the_remainder(
    monkeypatch,
) -> None:
    """The bar opens ABOVE TP1 (111 > 110): the TP1 tranche fills at the open,
    and the remainder's still-working pre-TP1 stop (95) fires later in the
    same bar when its low reaches 94 — live the remainder sits in an OTO
    whose initial stop works all bar and the breakeven amendment only takes
    effect next bar (#176). The tranche fill is not a gap through the stop,
    so the remainder exits AT the stop level, not the open."""
    gap_tp1_then_stop = (111.0, 112.0, 94.0, 105.0)
    frame = _frame([ENTRY_BAR, gap_tp1_then_stop, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "stop_loss"]
    assert trade.exits[0].price == 111.0
    assert trade.exits[0].time == _times(frame, 1)
    remainder = trade.exits[1]
    assert remainder.price == STOP
    assert remainder.time == _times(frame, 1)
    assert remainder.pnl == pytest.approx((STOP - ENTRY) * remainder.quantity)


def test_a_gap_through_tp1_spares_the_remainder_above_the_stop(
    monkeypatch,
) -> None:
    """Same gap, but the bar's low (96) stays above the pre-TP1 stop (95):
    the remainder survives the TP1 bar and the dead-bar rule applies."""
    gap_tp1_above_stop = (111.0, 112.0, 96.0, 105.0)
    frame = _frame([ENTRY_BAR, gap_tp1_above_stop, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "end_of_data"]
    assert trade.exits[0].price == 111.0
    assert trade.exits[0].time == _times(frame, 1)


def test_an_open_exactly_at_the_level_fills_at_that_price(monkeypatch) -> None:
    """Open == stop counts as a gap, but the fill price is identical either
    way: the level."""
    at_level_bar = (95.0, 95.5, 94.5, 95.2)
    frame = _frame([ENTRY_BAR, _flat(), at_level_bar, _flat()])
    _stub_supertrend(monkeypatch, "gggg")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["stop_loss"]
    assert trade.exits[0].price == STOP


def test_an_intrabar_stop_touch_still_fills_at_the_level(monkeypatch) -> None:
    """No gap (the bar opens above the stop): the intrabar touch keeps the
    level fill — the pre-#287 behaviour is unchanged off-gap."""
    touch_bar = (96.0, 96.5, 94.0, 95.5)
    frame = _frame([ENTRY_BAR, _flat(), touch_bar, _flat()])
    _stub_supertrend(monkeypatch, "gggg")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["stop_loss"]
    assert trade.exits[0].price == STOP


# ── activation and flip exit ─────────────────────────────────────────────────


def test_no_activation_while_not_ready_or_red_then_exit_after_first_red(monkeypatch) -> None:
    # bars:   0 entry, 1 TP1, 2 n, 3 r, 4 n, 5 g (activates), 6 g, 7 r (flip), 8 fill
    fill_bar = (104.0, 105.0, 101.0, 101.5)
    frame = _frame([ENTRY_BAR, TP1_BAR, *[_flat()] * 6, fill_bar])
    _stub_supertrend(monkeypatch, "nnnrnggrg")
    trade = _run(frame).trades[0]

    flip = trade.meta["supertrend_flip"]
    assert flip["activation_time"] == _times(frame, 5)
    assert flip["flip_bar_time"] == _times(frame, 7)
    assert [leg.reason for leg in trade.exits] == ["tp1", "supertrend_flip"]
    flip_leg = trade.exits[1]
    # Filled at the OPEN of the bar after the flip bar, not at the flip close.
    assert flip_leg.time == _times(frame, 8)
    assert flip_leg.price == 104.0
    assert flip_leg.quantity == pytest.approx(trade.quantity * 0.5)
    assert trade.exit_reason == "supertrend_flip"
    assert trade.holding_bars == 8


def test_red_while_waiting_for_green_does_not_exit(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrrr")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "end_of_data"]
    assert trade.meta["supertrend_flip"]["activation_time"] is None


def test_the_tp1_bar_close_does_not_activate_the_trail(monkeypatch) -> None:
    """Decided (review of #168): the trail activates from the bar AFTER the
    TP1 bar, never on the TP1 bar's own close.

    Nothing that happens at or inside the TP1 bar after the TP1 fill may move
    or trigger the trailing stop, so the TP1 bar's Supertrend close is not
    evaluated at all: a green close there does not arm the workflow, and a red
    close there cannot flip it. Here bar 1 (the TP1 bar) closes green and bar 2
    closes red — evaluating bar 1 would activate, and bar 2 would exit at bar
    3's open.
    """
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), (104.0, 105.0, 101.0, 101.5), _flat()])
    _stub_supertrend(monkeypatch, "rgrrr")
    trade = _run(frame).trades[0]

    flip = trade.meta["supertrend_flip"]
    assert flip["tp1_time"] == _times(frame, 1)
    assert flip["activation_time"] is None
    assert flip["flip_bar_time"] is None
    assert [leg.reason for leg in trade.exits] == ["tp1", "end_of_data"]


def test_the_activation_uses_the_first_close_after_the_tp1_bar(monkeypatch) -> None:
    """Activation (and the flip decided from it) run on the bars after TP1."""
    # bars: 0 entry, 1 TP1 (green, never evaluated), 2 green -> activates,
    #       3 red -> flip, 4 fill at the open
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat(), (104.0, 105.0, 101.0, 101.5)])
    _stub_supertrend(monkeypatch, "rggrg")
    trade = _run(frame).trades[0]

    flip = trade.meta["supertrend_flip"]
    assert flip["tp1_time"] == _times(frame, 1)
    assert flip["activation_time"] == _times(frame, 2)
    assert flip["flip_bar_time"] == _times(frame, 3)
    flip_leg = trade.exits[-1]
    assert flip_leg.reason == "supertrend_flip"
    assert flip_leg.time == _times(frame, 4)
    assert flip_leg.price == 104.0


def test_no_bar_inside_the_tp1_bar_moves_or_triggers_the_trail(monkeypatch) -> None:
    """Both halves of the decision: the breakeven stop moved on the TP1 bar is
    not applied to that bar, and its close is not read for the workflow."""
    tp1_bar_dipping_below_entry_and_closing_green = (103.0, 110.5, 99.0, 108.0)
    frame = _frame(
        [ENTRY_BAR, tp1_bar_dipping_below_entry_and_closing_green, _flat(), _flat(), _flat()]
    )
    _stub_supertrend(monkeypatch, "rgrrr")
    trade = _run(frame).trades[0]

    flip = trade.meta["supertrend_flip"]
    assert flip["tp1_time"] == _times(frame, 1)
    assert flip["activation_time"] is None
    assert [leg.reason for leg in trade.exits] == ["tp1", "end_of_data"]
    assert trade.meta["final_stop_loss"] == ENTRY


def test_first_red_right_after_activation_is_the_flip_bar(monkeypatch) -> None:
    """Red directly after the activating green bar exits at the next open."""
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrgrg")
    trade = _run(frame).trades[0]

    flip = trade.meta["supertrend_flip"]
    assert flip["activation_time"] == _times(frame, 2)
    assert flip["flip_bar_time"] == _times(frame, 3)
    assert trade.exits[-1].time == _times(frame, 4)


def test_not_ready_after_activation_does_not_exit(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrgnn")
    trade = _run(frame).trades[0]

    assert [leg.reason for leg in trade.exits] == ["tp1", "end_of_data"]


def test_flip_exit_fills_at_the_open_before_the_stop_is_evaluated(monkeypatch) -> None:
    """The market close goes out at the open; the fill bar's later range
    (here a dip through breakeven) is never evaluated against the stop."""
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat(), (103.0, 103.5, 90.0, 91.0)])
    _stub_supertrend(monkeypatch, "rrgrr")
    trade = _run(frame).trades[0]

    assert trade.exits[-1].reason == "supertrend_flip"
    assert trade.exits[-1].price == 103.0
    # The run-level signal_exit_fill="close" describes ordinary signal exits;
    # the flip leg is the one that fills at the next open, and says so per trade.
    assert trade.meta["supertrend_flip"]["flip_exit_fill"] == "next_bar_open"


def test_breakeven_stop_before_the_flip_bar_wins(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), (101.0, 101.5, 99.5, 100.5), _flat()])
    _stub_supertrend(monkeypatch, "rrgrr")
    trade = _run(frame).trades[0]

    # The red flip bar itself touches the breakeven stop intrabar, which
    # happens before its close is known.
    assert trade.exits[-1].reason == "breakeven_stop"
    assert trade.exits[-1].time == _times(frame, 3)
    assert trade.meta["supertrend_flip"]["flip_bar_time"] is None


def test_flip_on_the_last_bar_is_handled_as_end_of_data(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), (102.0, 102.5, 101.0, 101.25)])
    _stub_supertrend(monkeypatch, "rrgr")
    trade = _run(frame).trades[0]

    assert trade.meta["supertrend_flip"]["flip_bar_time"] == _times(frame, 3)
    last = trade.exits[-1]
    # Never filled at a next open that does not exist.
    assert last.reason == "end_of_data"
    assert last.price == 101.25
    assert last.time == _times(frame, 3)


def test_a_split_walk_keeps_the_pending_flip_exit(monkeypatch) -> None:
    """A signal on another symbol at the flip bar splits the bar walk exactly
    between the flip decision and its fill; the result must not change."""
    bars = [ENTRY_BAR, TP1_BAR, _flat(), _flat(), (104.0, 104.5, 103.0, 103.5), _flat()]
    frame = _frame(bars)
    _stub_supertrend(monkeypatch, "rrgrrr")
    alone = _run({"AAPL": frame, "MSFT": _frame([_flat()] * 6)}).trades
    split = _run(
        {"AAPL": frame, "MSFT": _frame([_flat()] * 6)},
        signals=[
            _signal(),
            _signal(
                symbol="MSFT",
                trailing={"mode": "none"},
                take_profit=None,
                timestamp=_times(frame, 3),
            ),
        ],
    ).trades
    aapl_alone = next(trade for trade in alone if trade.symbol == "AAPL")
    aapl_split = next(trade for trade in split if trade.symbol == "AAPL")
    assert [leg.to_dict() for leg in aapl_split.exits] == [
        leg.to_dict() for leg in aapl_alone.exits
    ]
    assert aapl_split.exits[-1].time == _times(frame, 4)
    assert aapl_split.exits[-1].price == 104.0


# ── accounting ───────────────────────────────────────────────────────────────


def test_fees_are_priced_once_at_the_quantity_weighted_exit(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat(), (104.0, 104.5, 103.0, 103.5)])
    _stub_supertrend(monkeypatch, "rrgrr")
    model = NotionalFeeModel(rate=0.001, flat_per_trade=1.0)
    result = _run(frame, adapter=_adapter(fee_model=model))
    trade = result.trades[0]

    quantity = trade.quantity
    tp1_qty, rest_qty = trade.exits[0].quantity, trade.exits[1].quantity
    assert tp1_qty + rest_qty == pytest.approx(quantity)
    avg_exit = (TP1 * tp1_qty + 104.0 * rest_qty) / quantity
    expected_fees = round(model.fee_for_trade(ENTRY, avg_exit, quantity), 2)
    assert trade.fees == expected_fees
    assert trade.pnl == pytest.approx((TP1 - ENTRY) * tp1_qty + (104.0 - ENTRY) * rest_qty)
    assert trade.net_pnl == pytest.approx(round(trade.pnl - expected_fees, 2))
    assert trade.pnl_pct == pytest.approx(trade.pnl / (ENTRY * quantity) * 100.0)
    # The equity path books the trade's net PnL once, at its last exit.
    assert result.equity_curve[-1].equity == pytest.approx(100_000.0 + trade.net_pnl)
    assert result.equity_curve[-1].time == trade.exits[-1].time


def test_sizing_balance_counts_the_realized_tp1_partial(monkeypatch) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat(), _flat()])
    _stub_supertrend(monkeypatch, "rrrr")
    adapter = _adapter()
    result = _run(frame, adapter=adapter)
    trade = result.trades[0]

    # Rebuild the open position as it stood after TP1 and price the balance.
    position = replay_module._OpenPosition(
        symbol="AAPL",
        direction="LONG",
        entry_time=pd.Timestamp(frame["timestamp"].iloc[0]),
        entry_bar_index=0,
        entry_price=ENTRY,
        signal_entry_price=ENTRY,
        quantity=trade.quantity * 0.5,
        stop_loss=ENTRY,
        take_profit=TP1,
        initial_stop_loss=STOP,
        initial_take_profit=TP1,
        risk_amount=None,
        risk_percent=None,
        timeframe="1m",
        source="internal",
        signal_time=pd.Timestamp(frame["timestamp"].iloc[0]),
        signal_metadata={},
        last_checked_at=pd.Timestamp(frame["timestamp"].iloc[1]),
        partial_exits=[trade.exits[0]],
    )
    balance = adapter._sizing_account_balance(
        open_positions={"AAPL": position},
        realized_pnl=0.0,
        frames={"AAPL": frame},
        as_of=pd.Timestamp(frame["timestamp"].iloc[2]),
    )
    tp1_pnl = (TP1 - ENTRY) * trade.quantity * 0.5
    open_pnl = (102.0 - ENTRY) * trade.quantity * 0.5
    assert balance == pytest.approx(100_000.0 + tp1_pnl + open_pnl)


def test_remainder_after_tp1_carries_no_stop_risk_at_breakeven(monkeypatch) -> None:
    position = replay_module._OpenPosition(
        symbol="AAPL",
        direction="LONG",
        entry_time=pd.Timestamp("2026-01-01T00:00:00Z"),
        entry_bar_index=0,
        entry_price=ENTRY,
        signal_entry_price=ENTRY,
        quantity=10.0,
        stop_loss=ENTRY,
        take_profit=TP1,
        initial_stop_loss=STOP,
        initial_take_profit=TP1,
        risk_amount=None,
        risk_percent=None,
        timeframe="1m",
        source="internal",
        signal_time=pd.Timestamp("2026-01-01T00:00:00Z"),
        signal_metadata={},
        last_checked_at=pd.Timestamp("2026-01-01T00:00:00Z"),
    )
    assert SignalReplayAdapter._position_risk_amount(position) == 0.0


# ── validation (fail closed, no invented defaults) ───────────────────────────


def _without(key: str) -> dict:
    return {name: value for name, value in FLIP_CONFIG.items() if name != key}


@pytest.mark.parametrize(
    ("trailing", "metadata", "match"),
    [
        (_without("supertrend_period"), {}, "supertrend_period"),
        ({**FLIP_CONFIG, "supertrend_period": 0}, {}, "supertrend_period"),
        ({**FLIP_CONFIG, "supertrend_period": 2.5}, {}, "supertrend_period"),
        (_without("supertrend_multiplier"), {}, "supertrend_multiplier"),
        ({**FLIP_CONFIG, "supertrend_multiplier": -1.0}, {}, "supertrend_multiplier"),
        (_without("breakeven_after_tp1"), {}, "breakeven_after_tp1"),
        ({**FLIP_CONFIG, "breakeven_after_tp1": "yes"}, {}, "breakeven_after_tp1"),
        ({**FLIP_CONFIG, "exit_phase": "exit_on_close_below_ema"}, {}, "exit_phase"),
        ({**FLIP_CONFIG, "activation_phase": "immediately"}, {}, "activation_phase"),
        (FLIP_CONFIG, {"tp1_exit_percent": None}, "tp1_exit_percent"),
        (FLIP_CONFIG, {"tp1_exit_percent": 0.0}, "tp1_exit_percent"),
        (FLIP_CONFIG, {"tp1_exit_percent": 50}, "tp1_exit_percent"),
        (FLIP_CONFIG, {"tp1_price": None}, "tp1_price"),
        (FLIP_CONFIG, {"tp1_price": float("nan")}, "tp1_price"),
    ],
)
def test_incomplete_or_invalid_flip_config_rejects_the_run(trailing, metadata, match) -> None:
    frame = _frame([ENTRY_BAR, TP1_BAR, _flat()])
    signals = [_signal(trailing=trailing, metadata=metadata)]
    with pytest.raises(ValueError, match=match):
        _run(frame, signals=signals)


def test_missing_tp1_exit_percent_is_not_defaulted() -> None:
    with pytest.raises(ValueError, match="tp1_exit_percent"):
        parse_supertrend_flip_plan(
            FLIP_CONFIG,
            {"tp1_price": TP1},
            action="buy",
            take_profit=TP1,
            stop_loss=STOP,
            context="test",
        )


def test_a_flip_signal_without_a_stop_is_refused() -> None:
    """Before TP1 the stop is the only protection: a stopless flip entry would
    sit unprotected until TP1 and never be stopped out afterwards, so it is
    refused instead of traded."""
    frame = _frame([ENTRY_BAR, _flat()])
    signals = [_signal(no_stop=True)]
    with pytest.raises(ValueError, match="requires a stop_loss"):
        _run(frame, signals=signals)


def test_plan_parsing_refuses_a_missing_stop() -> None:
    with pytest.raises(ValueError, match="requires a stop_loss"):
        parse_supertrend_flip_plan(
            FLIP_CONFIG,
            {"tp1_price": TP1, "tp1_exit_percent": 0.5},
            action="buy",
            take_profit=TP1,
            stop_loss=None,
            context="test",
        )


def test_flip_workflow_is_long_only() -> None:
    frame = _frame([ENTRY_BAR, _flat()])
    signals = [_signal(action="sell", take_profit=None)]
    with pytest.raises(ValueError, match="long entries only"):
        _run(frame, signals=signals)


def test_a_take_profit_other_than_tp1_is_ambiguous_and_rejected() -> None:
    frame = _frame([ENTRY_BAR, _flat()])
    signals = [_signal(take_profit=140.0)]
    with pytest.raises(ValueError, match="differs from tp1_price"):
        _run(frame, signals=signals)


def test_plan_is_parsed_from_config_and_metadata() -> None:
    plan = parse_supertrend_flip_plan(
        FLIP_CONFIG,
        {"tp1_price": TP1, "tp1_exit_percent": 0.25},
        action="buy",
        take_profit=None,
        stop_loss=STOP,
        context="test",
    )
    assert plan.tp1_price == TP1
    assert plan.tp1_exit_fraction == 0.25
    assert plan.breakeven_after_tp1 is True
    assert plan.supertrend_period == 3
    assert plan.supertrend_multiplier == 1.0


# ── real Supertrend: reuse and no lookahead ──────────────────────────────────


def _trend_series(n: int = 60, seed: int = 7) -> pd.DataFrame:
    """Up-trend, a sharp sell-off, then noise: produces green and red runs."""
    rng = np.random.default_rng(seed)
    closes = [100.0]
    for i in range(1, n):
        drift = 0.6 if i < 30 else (-1.5 if i < 38 else 0.1)
        closes.append(closes[-1] + drift + rng.normal(0, 0.3))
    bars = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else close
        bars.append((open_, max(open_, close) + 0.4, min(open_, close) - 0.4, close))
    return _frame(bars)


def test_reused_supertrend_is_the_processor_implementation() -> None:
    frame = _trend_series()
    via_processor = RamJetSignalProcessor._calculate_supertrend(frame, 3, 1.0)
    via_public = calculate_supertrend(frame, 3, 1.0)
    pd.testing.assert_frame_equal(via_processor, via_public)


def test_supertrend_row_depends_only_on_bars_up_to_it() -> None:
    """Prefix property: the value at bar N computed on bars 0..N equals the
    value at bar N computed on the whole series. The replay evaluates the
    whole frame once, so this is what makes that lookahead-free."""
    frame = _trend_series()
    full = calculate_supertrend(frame, 3, 1.0)
    for end in range(1, len(frame) + 1):
        prefix = calculate_supertrend(frame.iloc[:end], 3, 1.0)
        assert bool(prefix["supertrend"].iloc[-1]) == bool(full["supertrend"].iloc[end - 1])
        assert bool(prefix["supertrend_ready"].iloc[-1]) == bool(
            full["supertrend_ready"].iloc[end - 1]
        )


def _real_flip_trade(frame: pd.DataFrame):
    tp1 = float(frame["close"].iloc[5]) + 0.5
    signal = ReplaySignal.from_payload(
        {
            "timestamp": pd.Timestamp(frame["timestamp"].iloc[3]).isoformat(),
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": float(frame["close"].iloc[3]),
            "stop_loss": float(frame["close"].iloc[3]) - 20.0,
            "take_profit": tp1,
            "timeframe": "1m",
            "metadata": {
                "trailing_sl_config": dict(FLIP_CONFIG),
                "tp1_price": tp1,
                "tp1_exit_percent": 0.5,
            },
        },
        default_timeframe="1m",
    )
    return _run(frame, signals=[signal]).trades[0]


def test_real_supertrend_runs_the_whole_workflow() -> None:
    frame = _trend_series()
    trade = _real_flip_trade(frame)
    flip = trade.meta["supertrend_flip"]
    assert [leg.reason for leg in trade.exits] == ["tp1", "supertrend_flip"]
    assert flip["activation_time"] is not None
    flip_index = int(frame.index[frame["timestamp"] == pd.Timestamp(flip["flip_bar_time"])][0])
    states = calculate_supertrend(frame, 3, 1.0)
    assert bool(states["supertrend_ready"].iloc[flip_index])
    assert not bool(states["supertrend"].iloc[flip_index])
    assert trade.exits[-1].price == float(frame["open"].iloc[flip_index + 1])


def test_changing_future_bars_never_changes_an_earlier_decision() -> None:
    frame = _trend_series()
    baseline = _real_flip_trade(frame)
    flip_time = pd.Timestamp(baseline.meta["supertrend_flip"]["flip_bar_time"])
    flip_index = int(frame.index[frame["timestamp"] == flip_time][0])

    # Rewrite everything after the flip bar except the fill bar's open: the
    # fill bar's range/close and all later bars. Nothing may change.
    mutated = frame.copy()
    fill = flip_index + 1
    mutated.loc[fill, ["high", "low", "close"]] = [500.0, 1.0, 2.0]
    mutated.loc[fill + 1 :, ["open", "high", "low", "close"]] = [400.0, 900.0, 1.0, 3.0]
    after = _real_flip_trade(mutated)

    assert [leg.to_dict() for leg in after.exits] == [leg.to_dict() for leg in baseline.exits]
    assert after.meta["supertrend_flip"] == baseline.meta["supertrend_flip"]

    # And the activation decision only depends on bars up to it: cutting the
    # data right after the activation bar keeps the same activation.
    activation_time = pd.Timestamp(baseline.meta["supertrend_flip"]["activation_time"])
    activation_index = int(frame.index[frame["timestamp"] == activation_time][0])
    truncated = _real_flip_trade(frame.iloc[: activation_index + 1].copy())
    assert truncated.meta["supertrend_flip"]["activation_time"] == activation_time.isoformat()
    assert truncated.exits[-1].reason == "end_of_data"


# ── ramjet end-to-end ────────────────────────────────────────────────────────


def test_ramjet_processor_with_supertrend_flip_runs_in_replay() -> None:
    """The ramjet default can be switched back to supertrend_flip for replay."""
    frame = _trend_series(n=80)
    adapter = _adapter(
        signal_processor_name="ramjet",
        signal_processor_config={
            "trailing_mode": "supertrend_flip",
            "supertrend_period": 3,
            "supertrend_multiplier": 1.0,
            "atr_period": 5,
            "sl_multiplier": 2.0,
            "tp1_multiplier": 1.0,
        },
    )
    signal = ReplaySignal.from_payload(
        {
            "timestamp": pd.Timestamp(frame["timestamp"].iloc[10]).isoformat(),
            "symbol": "AAPL",
            "action": "buy",
            "entry_price": float(frame["close"].iloc[10]),
            "timeframe": "1m",
        },
        default_timeframe="1m",
    )
    trade = _run(frame, signals=[signal], adapter=adapter).trades[0]
    flip = trade.meta["supertrend_flip"]
    assert flip["supertrend_period"] == 3
    assert flip["tp1_price"] == pytest.approx(trade.meta["signal_take_profit"])
    assert trade.exits[0].reason == "tp1"
    # Half the position, floored to whole shares (the original bot's tranche).
    assert trade.exits[0].quantity == math.floor(trade.quantity * 0.5)
    assert trade.exits[0].quantity + trade.exits[-1].quantity == pytest.approx(trade.quantity)
