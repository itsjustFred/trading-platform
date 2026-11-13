"""Trade-by-trade parity of the native engine with ``backtesting.py`` reference runs.

The reference trade lists in ``tests/parity/fixtures/ref_parity_*_trades.csv`` were produced by the
*original* ``backtesting.py`` implementations in ``scripts/reference_parity`` (see the generator for
how, and why the data is synthetic). This module needs no reference library: it re-runs the three
ported strategies through the platform's native engine on the committed fixture data and compares.

Every difference the comparison finds is either asserted exactly here as a *documented semantic
difference* (docs/reports/parity-reference-libraries.md) or is an engine bug. None was found.

* ``bracket_long`` / ``bracket_short`` (zero commission, no signal exit): every field matches,
  including the exits whose bar opens beyond the stop/target (both fill at the OPEN; touches fill
  at the level) - the fixture deliberately contains such gaps.
* ``smacross_zero_fee``: entries, signal exits (both fill at the NEXT bar's open since the
  2026-11-13 platform decision), quantities, PnL and final equity all match.
* ``smacross_all_in`` (0.2 % commission): entries and exits match; only the sizing/commission model
  differs (documented difference D2).
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pytest

from runner.engines.base import BacktestConfig
from runner.engines.native_adapter import NativeAdapter

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = json.loads((FIXTURES / "ref_parity_summary.json").read_text(encoding="utf-8"))
CASH = float(MANIFEST["cash"])
FRACTIONAL_UNIT = float(MANIFEST["fractional_unit"])
SYMBOL = "SYN_1d"

#: slug -> (strategy dir, max_position_exposure_pct, engine fee rate)
RUNS = {
    "smacross_all_in": ("ref_sma_cross_all_in", 1.0, 0.002),
    "smacross_zero_fee": ("ref_sma_cross_all_in", 1.0, 0.0),
    "bracket_long": ("ref_bracket_long", 0.5, 0.0),
    "bracket_short": ("ref_bracket_short", 0.5, 0.0),
}


@dataclass(frozen=True)
class EngineRun:
    trades: list
    final_equity: float  # initial capital + sum of net PnL (realized, flat at the end)


def _bars() -> pd.DataFrame:
    frame = pd.read_csv(FIXTURES / "ref_parity_daily.csv")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    return frame


def _reference(slug: str) -> pd.DataFrame:
    frame = pd.read_csv(FIXTURES / f"ref_parity_{slug}_trades.csv")
    frame["entry_time"] = pd.to_datetime(frame["entry_time"], utc=True)
    frame["exit_time"] = pd.to_datetime(frame["exit_time"], utc=True)
    return frame


def _run_engine(slug: str, tmp_path: Path) -> EngineRun:
    strategy, position_pct, fee_rate = RUNS[slug]
    frame = _bars()
    target = tmp_path / "data" / "ohlcv" / "test_source" / "equities" / SYMBOL
    target.mkdir(parents=True)
    frame.to_parquet(target / "1d.parquet", index=False)
    parameters = (
        {"fee_rate": fee_rate, "fee_flat_per_trade": 0.0} if fee_rate else {"fees": "zero"}
    )
    config = BacktestConfig(
        strategy_name="ref_parity",
        symbols=[SYMBOL],
        timeframe="1d",
        start_date=str(frame["timestamp"].iloc[0].date()),
        end_date=str(frame["timestamp"].iloc[-1].date()),
        initial_capital=CASH,
        # high enough that the notional cap (max_position_exposure_pct) is what sizes
        risk_per_trade_pct=1.0,
        max_position_exposure_pct=position_pct,
        max_leverage=1.0,
        data_dir=str(tmp_path / "data"),
        parameters=parameters,
    )
    path = REPO_ROOT / "strategies" / strategy / f"{strategy}.py"
    result = NativeAdapter().run(config, str(path))
    trades = sorted(result.trades, key=lambda trade: pd.Timestamp(trade.entry_time))
    return EngineRun(trades, CASH + sum(float(t.net_pnl) for t in trades))


@pytest.fixture(scope="module")
def runs():
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        out = {}
        for slug in RUNS:
            sub = root / slug
            sub.mkdir()
            out[slug] = _run_engine(slug, sub)
        yield out


def _ts(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


# ───────────────────────────── fixture integrity ─────────────────────────────


@pytest.mark.parametrize("slug", RUNS)
def test_manifest_matches_the_committed_trade_lists(slug):
    ref = _reference(slug)
    meta = MANIFEST["runs"][slug]
    assert meta["invariant_violations"] == []
    assert len(ref) == meta["trades"]
    assert ref["exit_kind"].value_counts().to_dict() == meta["exit_kinds"]
    assert "end_of_data" not in set(ref["exit_kind"])
    assert CASH + ref["net_pnl"].sum() == pytest.approx(meta["final_equity"], rel=1e-9)
    assert len(_bars()) == MANIFEST["bars"]


@pytest.mark.parametrize("slug", RUNS)
def test_reference_trades_are_consistent_with_the_bars(slug):
    """The stored reference prices are the bars' own prices (guards a hand-edited fixture)."""
    bars = _bars().set_index("timestamp")
    ref = _reference(slug)
    for _, row in ref.iterrows():
        assert row["entry_price"] == pytest.approx(bars.loc[row["entry_time"], "open"], abs=1e-9)
        if row["exit_kind"] == "signal":
            assert row["exit_price"] == pytest.approx(bars.loc[row["exit_time"], "open"], abs=1e-9)
        else:
            assert row["exit_price"] == pytest.approx(_protective_fill(row, bars), abs=1e-9)


def _protective_fill(row, bars: pd.DataFrame) -> float:
    """backtesting.py's rule: the OPEN when the bar opens beyond the level, else the level."""
    open_price = float(bars.loc[row["exit_time"], "open"])
    stop = row["exit_kind"] == "stop_loss"
    level = float(row["stop_level" if stop else "take_profit_level"])
    long_side = row["side"] == "LONG"
    # a long stop / short target lies below the open when it is gapped through, a short stop /
    # long target above it
    below = long_side == stop
    return min(open_price, level) if below else max(open_price, level)


def _stopped_at_entry(row) -> bool:
    """The entry bar opens through the trade's own stop (gap past the stop level before the fill).

    backtesting.py fills the entry at that open and stops it out at the same open: a zero-PnL
    round trip (documented difference D5).  The platform refuses the entry instead ("invalid sizing
    stop at fill"), so the round trip does not exist on its side.
    """
    level = float(row["stop_level"])
    if row["side"] == "LONG":
        return float(row["entry_price"]) <= level
    return float(row["entry_price"]) >= level


def _comparable(ref: pd.DataFrame) -> pd.DataFrame:
    return ref[[not _stopped_at_entry(row) for _, row in ref.iterrows()]].reset_index(drop=True)


def _opened_through(row, bars: pd.DataFrame) -> bool:
    return _protective_fill(row, bars) == float(bars.loc[row["exit_time"], "open"]) != float(
        row["stop_level" if row["exit_kind"] == "stop_loss" else "take_profit_level"]
    )


# ──────────────────────── exact parity: bracket strategies ───────────────────


@pytest.mark.parametrize("slug", ["bracket_long", "bracket_short"])
def test_bracket_trades_match_the_reference_exactly(runs, slug):
    full = _reference(slug)
    ref = _comparable(full)
    engine = runs[slug]
    assert len(full) == MANIFEST["runs"][slug]["trades"]
    assert len(engine.trades) == len(ref), "only the stopped-at-entry round trips may be absent"
    for number, (trade, (_, row)) in enumerate(zip(engine.trades, ref.iterrows())):
        context = f"{slug} trade {number}"
        assert trade.direction == row["side"], context
        assert _ts(trade.entry_time) == row["entry_time"], context
        assert trade.entry_price == pytest.approx(row["entry_price"], abs=1e-9), context
        # the reference quantizes to 1e-8 shares (FractionalBacktest)
        qty_tol = 2e3 * FRACTIONAL_UNIT
        assert trade.quantity == pytest.approx(row["quantity"], abs=qty_tol), context
        assert len(trade.exits) == 1, context
        leg = trade.exits[0]
        assert _ts(leg.time) == row["exit_time"], context
        assert leg.price == pytest.approx(row["exit_price"], abs=1e-9), context
        expected_reason = {"stop_loss": "stop_loss", "take_profit": "tp"}[row["exit_kind"]]
        assert leg.reason == expected_reason, context
        assert trade.net_pnl == pytest.approx(row["net_pnl"], abs=1e-4), context
    assert engine.final_equity == pytest.approx(MANIFEST["runs"][slug]["final_equity"], abs=1e-4)


@pytest.mark.parametrize("slug", ["bracket_long", "bracket_short"])
def test_gap_through_exits_fill_at_the_open_and_touches_at_the_level(runs, slug):
    """The fixture contains stops/targets the exit bar opens through (D3 is now agreed, not excluded).

    Pins that the engine fill equals the bar OPEN on every such exit and the LEVEL on every
    intrabar touch, for stops and targets alike, and that the manifest lists exactly those trades.
    """
    bars = _bars().set_index("timestamp")
    full = _reference(slug)
    ref = _comparable(full)
    through = [i for i, (_, row) in enumerate(ref.iterrows()) if _opened_through(row, bars)]
    listed = [
        f"{int(row['trade'])}:{row['exit_kind']}"
        for _, row in full.iterrows()
        if _opened_through(row, bars)
    ]
    assert listed == MANIFEST["runs"][slug]["gap_through_exits"]
    assert len(through) >= 2
    kinds = {ref.iloc[i]["exit_kind"] for i in through}
    assert kinds == {"stop_loss", "take_profit"}, "fixture must gap through both a stop and a target"
    for i, trade in enumerate(runs[slug].trades):
        row = ref.iloc[i]
        leg = trade.exits[0]
        level = float(row["stop_level" if row["exit_kind"] == "stop_loss" else "take_profit_level"])
        open_price = float(bars.loc[row["exit_time"], "open"])
        expected = open_price if i in through else level
        assert leg.price == pytest.approx(expected, abs=1e-9), f"{slug} trade {i}"


@pytest.mark.parametrize("slug", ["bracket_long", "bracket_short"])
def test_entries_gapped_through_their_own_stop_are_refused_by_the_platform(runs, slug):
    """D5: the reference opens and stops out at the same open (zero PnL); the platform has no trade."""
    full = _reference(slug)
    dead = full[[_stopped_at_entry(row) for _, row in full.iterrows()]]
    for _, row in dead.iterrows():
        assert row["entry_bar"] == row["exit_bar"] and row["exit_kind"] == "stop_loss"
        assert row["exit_price"] == pytest.approx(row["entry_price"], abs=1e-9)
        assert row["net_pnl"] == pytest.approx(0.0, abs=1e-9)
    assert len(runs[slug].trades) == len(full) - len(dead)
    entries = {_ts(trade.entry_time) for trade in runs[slug].trades}
    assert not entries & set(dead["entry_time"])
    if slug == "bracket_short":
        assert len(dead) == 2  # the fixture exercises it (the long run happens to have none)


# ───────────────── SmaCross: exact entry AND signal-exit parity ──────────────


@pytest.mark.parametrize("slug", ["smacross_zero_fee", "smacross_all_in"])
def test_smacross_entries_and_signal_exits_match_exactly(runs, slug):
    """Both frameworks fill a signal exit at the NEXT bar's open (former difference D1 is gone)."""
    bars = _bars().set_index("timestamp")
    ref = _reference(slug)
    engine = runs[slug]
    assert len(engine.trades) == len(ref) == MANIFEST["runs"][slug]["trades"]
    for number, (trade, (_, row)) in enumerate(zip(engine.trades, ref.iterrows())):
        context = f"{slug} trade {number}"
        assert trade.direction == row["side"] == "LONG", context
        # entry: both frameworks fill the next bar's open -> identical time and price
        assert _ts(trade.entry_time) == row["entry_time"], context
        assert trade.entry_price == pytest.approx(row["entry_price"], abs=1e-9), context
        # exit: both fill the bar after the trigger at its open -> identical time and price
        leg = trade.exits[0]
        assert _ts(leg.time) == row["exit_time"], context
        assert leg.price == pytest.approx(row["exit_price"], abs=1e-9), context
        assert row["exit_price"] == pytest.approx(bars.loc[row["exit_time"], "open"], abs=1e-9), context
        assert leg.reason == "signal", context


def test_zero_fee_run_is_identical_trade_by_trade_and_in_final_equity(runs):
    """No commission gross-up: same all-in sizing on realized cash -> identical quantities and PnL."""
    ref = _reference("smacross_zero_fee")
    engine = runs["smacross_zero_fee"]
    cash = CASH
    for number, (trade, (_, row)) in enumerate(zip(engine.trades, ref.iterrows())):
        assert trade.quantity == pytest.approx(cash / trade.entry_price, rel=1e-9)
        assert trade.quantity == pytest.approx(row["quantity"], rel=1e-6), number
        assert float(trade.net_pnl) == pytest.approx(row["net_pnl"], rel=1e-6, abs=1e-3), number
        cash += float(trade.net_pnl)
    assert engine.final_equity == pytest.approx(
        MANIFEST["runs"]["smacross_zero_fee"]["final_equity"], rel=1e-6
    )


def test_commission_run_differs_by_the_sizing_gross_up_and_separate_fees(runs):
    """0.2 % per order: reference sizes onto price*(1+c); platform sizes on the notional."""
    commission = MANIFEST["runs"]["smacross_all_in"]["commission"]
    ref = _reference("smacross_all_in")
    engine = runs["smacross_all_in"]
    first, row = engine.trades[0], ref.iloc[0]
    assert first.quantity == pytest.approx(CASH / first.entry_price, rel=1e-9)
    gross_up = row["entry_price"] * (1 + commission)
    assert row["quantity"] == pytest.approx(CASH / gross_up, rel=1e-6)
    assert first.quantity / row["quantity"] == pytest.approx(1 + commission, rel=1e-6)
    for trade in engine.trades:
        leg = trade.exits[0]
        expected = commission * trade.quantity * (trade.entry_price + leg.price)
        assert float(trade.fees) == pytest.approx(expected, rel=1e-9)
        assert float(trade.net_pnl) == pytest.approx(float(trade.pnl) - float(trade.fees), rel=1e-9)
    for _, row in ref.iterrows():
        expected = commission * row["quantity"] * (row["entry_price"] + row["exit_price"])
        assert row["commission"] == pytest.approx(expected, rel=1e-6)
