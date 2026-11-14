"""Independent reconciliation of the committed MT5 goldens (issue #162).

The three goldens under ``tests/fixtures/validation/goldens/`` are importer
output from MT5 Strategy Tester reports. The MT5 summary block is stored next
to them in ``stats``. Those summary numbers come from MT5, not from the
importer, so each identity below checks the importer's trade list against an
independent source.

The identities are checked against the stored JSON, not against a fresh
import. The source ``.xlsx`` reports are not in this repository.

The net-of-fees identities are strict xfails: five on the goldens (the net sum
for all three, and the calculator win count for NAS100 and SP500). They fail
on the committed data because ``net_pnl`` has the wrong sign (see the reason on
the marker). They are written as the expected behaviour, so they XPASS and
fail the moment the bug is fixed; the markers must then be removed. The
calculator win count for EURUSD passes, because its fees are small swaps.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from validation.reference import ValidationReference
from validation.stats.calculator import compute_trade_statistics

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "validation" / "goldens"
GOLDEN_FILES = ["eurusd_h1_golden.json", "nas100_m15_golden.json", "sp500_m1_golden.json"]

_NET_SIGN_BUG = (
    "Known bug: MT5XLSXImporter (and MT5HTMLImporter) set net_pnl = pnl - fees, "
    "but fees are MT5-signed (commission and swap are negative when they cost "
    "money), so the sign is inverted. MT5's own total equals sum(pnl) + sum(fees) "
    "in every golden. Remove this marker once net_pnl is fixed."
)


@pytest.fixture(params=GOLDEN_FILES, ids=lambda name: name.removesuffix("_golden.json"))
def golden(request: pytest.FixtureRequest) -> dict:
    return json.loads((GOLDEN_DIR / request.param).read_text(encoding="utf-8"))


def _pnl(trade: dict) -> float:
    return float(trade["pnl"] or 0.0)


def _fees(trade: dict) -> float:
    return float(trade["fees"] or 0.0)


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

    def test_total_pnl_is_gross_plus_signed_fees(self, golden: dict) -> None:
        """MT5 "Nettogewinn gesamt" = sum of trade profit + sum of commission and swap."""
        gross = sum(_pnl(t) for t in golden["trades"])
        fees = sum(_fees(t) for t in golden["trades"])
        assert golden["stats"]["total_pnl"] == pytest.approx(gross + fees, abs=0.01)

    def test_winners_and_losers_are_classified_on_net_of_fees(self, golden: dict) -> None:
        """MT5 counts a trade as a winner when profit + commission + swap is positive."""
        stats = golden["stats"]
        net_winners = sum(1 for t in golden["trades"] if _pnl(t) + _fees(t) > 0)
        net_losers = sum(1 for t in golden["trades"] if _pnl(t) + _fees(t) < 0)
        assert stats["winners"] == net_winners
        assert stats["losers"] == net_losers

    def test_trade_fields_are_internally_consistent(self, golden: dict) -> None:
        for trade in golden["trades"]:
            assert trade["direction"] in ("LONG", "SHORT")
            assert trade["symbol"] == golden["symbol"]
            assert trade["entry_time"] <= trade["exits"][0]["time"]
            assert trade["exits"][0]["quantity"] == pytest.approx(trade["quantity"], rel=1e-9)


class TestKnownNetPnlBug:
    @pytest.mark.xfail(strict=True, reason=_NET_SIGN_BUG)
    def test_net_pnl_equals_mt5_net_total(self, golden: dict) -> None:
        net = sum(float(t["net_pnl"]) for t in golden["trades"])
        assert net == pytest.approx(golden["stats"]["total_pnl"], abs=0.01)

    @pytest.mark.parametrize(
        "file_name",
        [
            "eurusd_h1_golden.json",
            pytest.param(
                "nas100_m15_golden.json", marks=pytest.mark.xfail(strict=True, reason=_NET_SIGN_BUG)
            ),
            pytest.param(
                "sp500_m1_golden.json", marks=pytest.mark.xfail(strict=True, reason=_NET_SIGN_BUG)
            ),
        ],
    )
    def test_calculator_winning_trades_match_mt5(self, file_name: str) -> None:
        """The calculator classifies winners on net_pnl, so it inherits the sign bug.

        EURUSD carries only small swaps, so its classification happens to agree with
        MT5. NAS100 and SP500 carry commission and swap on most trades and do not.
        """
        golden = json.loads((GOLDEN_DIR / file_name).read_text(encoding="utf-8"))
        ref = ValidationReference.model_validate(golden)
        assert compute_trade_statistics(ref.trades).winning_trades == golden["stats"]["winners"]
