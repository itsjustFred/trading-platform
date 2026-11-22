"""Independent reconciliation of the committed MT5 goldens (issue #162).

The three goldens under ``tests/fixtures/validation/goldens/`` are importer
output from MT5 Strategy Tester reports. The MT5 summary block is stored next
to them in ``stats``. Those summary numbers come from MT5, not from the
importer, so each identity below checks the importer's trade list against an
independent source.

The identities are checked against the stored JSON; the money fields are
additionally pinned to a fresh import (``TestGoldensMatchFreshImport``). The source ``.xlsx`` reports are tracked in
``strategy-comparisons/intraday_volatility_breakout/``, and
``scripts/generate_golden_files.py`` re-imports them to regenerate the goldens,
so a golden can always be re-derived from its report.

``fees`` is a positive cost (``net_pnl == pnl - fees``); the importers negate
MT5's signed commission and swap. MT5's own totals therefore equal
``sum(pnl) - sum(fees)``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.models import Trade
from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import ValidationReference
from validation.stats.calculator import compute_trade_statistics

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "validation" / "goldens"
GOLDEN_FILES = ["eurusd_h1_golden.json", "nas100_m15_golden.json", "sp500_m1_golden.json"]
REPORT_DIR = (
    Path(__file__).resolve().parents[2]
    / "strategy-comparisons"
    / "intraday_volatility_breakout"
)
GOLDEN_REPORTS = {
    "eurusd_h1_golden.json": "EURUSD_H1.xlsx",
    "nas100_m15_golden.json": "NAS100_M15.xlsx",
    "sp500_m1_golden.json": "SP500_M1.xlsx",
}
MONEY_TOLERANCE = 0.01


@pytest.fixture(params=GOLDEN_FILES, ids=lambda name: name.removesuffix("_golden.json"))
def golden(request: pytest.FixtureRequest) -> dict:
    return json.loads((GOLDEN_DIR / request.param).read_text(encoding="utf-8"))


def _pnl(trade: dict) -> float:
    return float(trade["pnl"] or 0.0)


def _fees(trade: dict) -> float:
    return float(trade["fees"] or 0.0)


def _assert_same_money(
    fresh: float | None,
    committed: float | None,
    field: str,
    trade_id: int,
) -> None:
    assert (fresh is None) == (committed is None), (
        f"trade {trade_id}: {field}: fresh {fresh!r}, golden {committed!r}"
    )
    if fresh is not None:
        assert fresh == pytest.approx(committed, abs=MONEY_TOLERANCE), (
            f"trade {trade_id}: {field}: fresh {fresh!r}, golden {committed!r}"
        )


class TestSummaryReconciliation:
    def test_stats_trade_count_matches_trade_list(self, golden: dict) -> None:
        assert golden["stats"]["total_trades"] == len(golden["trades"])

    def test_winners_and_losers_partition_the_trades(self, golden: dict) -> None:
        stats = golden["stats"]
        assert stats["winners"] + stats["losers"] == stats["total_trades"]

    def test_every_trade_is_one_entry_and_one_exit(self, golden: dict) -> None:
        """MT5 "Anzahl Deals" counts entries and exits, so it is two per closed trade."""
        assert all(len(t["exits"]) == 1 for t in golden["trades"])
        assert golden["stats"]["total_deals"] == 2 * len(golden["trades"])

    def test_win_rate_is_winners_over_total(self, golden: dict) -> None:
        stats = golden["stats"]
        assert stats["win_rate"] == pytest.approx(
            100.0 * stats["winners"] / stats["total_trades"], abs=0.005
        )

    def test_total_pnl_is_gross_minus_fees(self, golden: dict) -> None:
        """MT5 "Nettogewinn gesamt" = sum of trade profit - sum of fees (costs)."""
        gross = sum(_pnl(t) for t in golden["trades"])
        fees = sum(_fees(t) for t in golden["trades"])
        assert golden["stats"]["total_pnl"] == pytest.approx(gross - fees, abs=0.01)

    def test_winners_and_losers_are_classified_on_net_of_fees(self, golden: dict) -> None:
        """MT5 counts a trade as a winner when profit - fees is positive."""
        stats = golden["stats"]
        net_winners = sum(1 for t in golden["trades"] if _pnl(t) - _fees(t) > 0)
        net_losers = sum(1 for t in golden["trades"] if _pnl(t) - _fees(t) < 0)
        assert stats["winners"] == net_winners
        assert stats["losers"] == net_losers

    def test_trade_fields_are_internally_consistent(self, golden: dict) -> None:
        for trade in golden["trades"]:
            assert trade["direction"] in ("LONG", "SHORT")
            assert trade["symbol"] == golden["symbol"]
            assert trade["entry_time"] <= trade["exits"][0]["time"]
            assert trade["exits"][0]["quantity"] == pytest.approx(trade["quantity"], rel=1e-9)


class TestNetPnl:
    def test_net_pnl_equals_mt5_net_total(self, golden: dict) -> None:
        net = sum(float(t["net_pnl"]) for t in golden["trades"])
        assert net == pytest.approx(golden["stats"]["total_pnl"], abs=0.01)

    @pytest.mark.parametrize(
        "file_name",
        [
            "eurusd_h1_golden.json",
            "nas100_m15_golden.json",
            "sp500_m1_golden.json",
        ],
    )
    def test_calculator_winning_trades_match_mt5(self, file_name: str) -> None:
        """The calculator classifies winners on net_pnl, which now matches MT5.

        MT5 counts a trade as a winner when profit less commission and swap is
        positive, so the three goldens (EURUSD's small swaps, NAS100's and
        SP500's commission and swap on most trades) all have to agree.
        """
        golden = json.loads((GOLDEN_DIR / file_name).read_text(encoding="utf-8"))
        ref = ValidationReference.model_validate(golden)
        assert compute_trade_statistics(ref.trades).winning_trades == golden["stats"]["winners"]


class TestGoldensMatchFreshImport:
    """The goldens must stay re-derivable from their tracked reports.

    The source ``.xlsx`` reports are tracked in
    ``strategy-comparisons/intraday_volatility_breakout/``, so each golden is
    a derived copy, not an independently maintained file. This pins the
    fields the importer owns about a trade's PnL (``fees``/``net_pnl``, and
    the per-exit ones) to a fresh import, so a hand-edited golden that drifts
    from the importer fails here instead of silently contradicting it. Not
    compared: the volatile reference fields (``imported_at``, ``source_path``)
    and fields added by later importer versions, which the committed goldens
    are still free to lag on.
    """

    @pytest.mark.parametrize("file_name", GOLDEN_FILES)
    def test_fee_fields_match_a_fresh_import(self, file_name: str) -> None:
        golden = json.loads((GOLDEN_DIR / file_name).read_text(encoding="utf-8"))
        report = REPORT_DIR / GOLDEN_REPORTS[file_name]
        assert report.is_file(), f"tracked report is missing: {report}"
        fresh: list[Trade] = MT5XLSXImporter().load(report).trades
        committed = golden["trades"]
        assert [t.id for t in fresh] == [t["id"] for t in committed]
        assert len(fresh) == len(committed)
        for trade, expected in zip(fresh, committed, strict=True):
            _assert_same_money(trade.fees, expected["fees"], "fees", trade.id)
            _assert_same_money(trade.pnl, expected["pnl"], "pnl", trade.id)
            _assert_same_money(trade.net_pnl, expected["net_pnl"], "net_pnl", trade.id)
            assert len(trade.exits) == len(expected["exits"])
            for exit_, exit_expected in zip(trade.exits, expected["exits"], strict=True):
                # Costs live on Trade.fees only, never on a TradeExit.
                _assert_same_money(exit_.fees, exit_expected["fees"], "exits[].fees", trade.id)
                _assert_same_money(exit_.pnl, exit_expected["pnl"], "exits[].pnl", trade.id)
