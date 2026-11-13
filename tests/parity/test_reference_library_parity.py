"""Trade-by-trade parity of the native engine with ``backtesting.py`` reference runs.

The reference trade lists in ``tests/parity/fixtures/ref_parity_*_trades.csv`` were produced by the
*original* ``backtesting.py`` implementations in ``scripts/reference_parity`` (see the generator for
how, and why the data is synthetic). This module needs no reference library: it re-runs the three
ported strategies through the platform's native engine on the committed fixture data and compares.

Every difference the comparison finds is either asserted exactly here as a *documented semantic
difference* (docs/reports/parity-reference-libraries.md) or is an engine bug. None was found.

* ``bracket_long`` / ``bracket_short`` (zero commission, no signal exit): every field matches.
* ``smacross_zero_fee``: entries match; signal exits differ by the exit-fill convention, which is
  pinned exactly (platform: trigger-bar close; reference: next bar's open).
* ``smacross_all_in`` (0.2 % commission): additionally the sizing/commission model differs.
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
        elif row["exit_kind"] == "stop_loss":
            assert row["exit_price"] == pytest.approx(row["stop_level"], abs=1e-9)
        else:
            assert row["exit_price"] == pytest.approx(row["take_profit_level"], abs=1e-9)


# ──────────────────────── exact parity: bracket strategies ───────────────────


@pytest.mark.parametrize("slug", ["bracket_long", "bracket_short"])
def test_bracket_trades_match_the_reference_exactly(runs, slug):
    ref = _reference(slug)
    engine = runs[slug]
    assert len(engine.trades) == len(ref) == MANIFEST["runs"][slug]["trades"]
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


# ─────────────── documented differences: SmaCross signal exits ───────────────


@pytest.mark.parametrize("slug", ["smacross_zero_fee", "smacross_all_in"])
def test_smacross_entries_match_and_exits_follow_the_documented_conventions(runs, slug):
    bars = _bars().set_index("timestamp")
    stamps = list(bars.index)
    position = {stamp: i for i, stamp in enumerate(stamps)}
    ref = _reference(slug)
    engine = runs[slug]
    assert len(engine.trades) == len(ref) == MANIFEST["runs"][slug]["trades"]
    for number, (trade, (_, row)) in enumerate(zip(engine.trades, ref.iterrows())):
        context = f"{slug} trade {number}"
        assert trade.direction == row["side"] == "LONG", context
        # entry: both frameworks fill the next bar's open -> identical time and price
        assert _ts(trade.entry_time) == row["entry_time"], context
        assert trade.entry_price == pytest.approx(row["entry_price"], abs=1e-9), context
        # exit: platform = trigger-bar close, reference = next bar's open (one bar later)
        leg = trade.exits[0]
        ref_exit = position[row["exit_time"]]
        assert _ts(leg.time) == stamps[ref_exit - 1], context
        assert leg.price == pytest.approx(bars.iloc[ref_exit - 1]["close"], abs=1e-9), context
        assert row["exit_price"] == pytest.approx(bars.iloc[ref_exit]["open"], abs=1e-9), context
        assert leg.reason == "signal", context


def test_zero_fee_sizing_is_all_in_on_realized_cash_in_both_frameworks(runs):
    """Same sizing rule on both sides: quantity == realized cash / entry price.

    The first trade's quantity is identical (no divergence has accumulated yet); later trades
    differ only because the exit convention changed the realized cash they are sized on, so each
    side is checked against its *own* running cash.
    """
    ref = _reference("smacross_zero_fee")
    engine = runs["smacross_zero_fee"]
    assert engine.trades[0].quantity == pytest.approx(ref.iloc[0]["quantity"], abs=1e-6)
    cash_engine = cash_ref = CASH
    for trade, (_, row) in zip(engine.trades, ref.iterrows()):
        assert trade.quantity == pytest.approx(cash_engine / trade.entry_price, rel=1e-9)
        assert row["quantity"] == pytest.approx(cash_ref / row["entry_price"], rel=1e-6)
        cash_engine += float(trade.net_pnl)
        cash_ref += float(row["net_pnl"])


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
