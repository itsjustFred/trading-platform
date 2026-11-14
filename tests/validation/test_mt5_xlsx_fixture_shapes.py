"""Hand-computed expectations for the synthetic MT5 XLSX fixtures (issue #162).

The fixtures are built by ``scripts/generate_mt5_xlsx_fixtures.py`` from invented
numbers. Every expected value below was worked out from the deal rows in that
script (entry/exit prices, volumes, profit, commission, swap), not copied from
importer output. If you change a fixture row, change the expectation here too.

Arithmetic used throughout (EURUSD, 1 lot = 100 000 units):

* LONG pnl  = (exit - entry) * lots * 100 000
* SHORT pnl = (entry - exit) * lots * 100 000

Commission and swap are MT5-signed: a cost is negative. ``Trade.fees`` is the
signed sum of commission and swap over every fill of the position.

Positions in ``hedged_partial_open.xlsx`` (scope = symbol + entry side):

* P1 LONG 0.10 round trip, closed by TP.
* P2 LONG 0.20, closed by two exits (0.12 + 0.08).
* P3 SHORT 0.10, opened while P2 is open (hedge), closed by SL.
* P4 SHORT 0.20, exit 0.12 only: partial, residual 0.08.
* P5 LONG 0.05, no exit: open.

Positions in ``position_id_shared.xlsx`` (the Trade column repeats the position id):

* 1001 LONG: entries 0.10 + 0.05, exits 0.10 + 0.05.
* 1002 SHORT: entry 0.20, exit 0.20.
* 1003 LONG: entry 0.10, exit 0.04: partial, residual 0.06.
* 1004 LONG: entry 0.05 opened while 1003 is still open, exit 0.05.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from validation.importers.mt5_xlsx import MT5XLSXImporter
from validation.reference import ValidationReference

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "validation" / "mt5_xlsx"
HEDGED = FIXTURE_DIR / "hedged_partial_open.xlsx"
SHARED = FIXTURE_DIR / "position_id_shared.xlsx"


@pytest.fixture(scope="module")
def hedged() -> ValidationReference:
    return MT5XLSXImporter().load(HEDGED)


@pytest.fixture(scope="module")
def shared() -> ValidationReference:
    return MT5XLSXImporter().load(SHARED)


def _exits(trade) -> list[tuple[str, float, float, float | None, str | None]]:
    return [(e.time, e.price, e.quantity, e.pnl, e.reason) for e in trade.exits]


class TestHedgedPartialOpenReport:
    """``hedged_partial_open.xlsx``: hedging, split exit, partial and open positions."""

    def test_report_header(self, hedged: ValidationReference) -> None:
        assert hedged.source_framework == "mt5_xlsx"
        assert hedged.strategy_name == "Synthetic Hedge Partial Fixture"
        assert hedged.symbol == "EURUSD"
        assert hedged.timeframe == "h1"
        assert hedged.raw_metadata["parsed_meta"]["initial_capital"] == 10000.0

    def test_three_closed_trades_in_report_order(self, hedged: ValidationReference) -> None:
        assert [t.direction for t in hedged.trades] == ["LONG", "LONG", "SHORT"]
        assert [t.entry_time for t in hedged.trades] == [
            "2024-01-02T08:00:00Z",
            "2024-01-03T08:00:00Z",
            "2024-01-03T09:00:00Z",
        ]

    def test_p1_round_trip(self, hedged: ValidationReference) -> None:
        t = hedged.trades[0]
        assert t.symbol == "EURUSD"
        assert t.entry_price == pytest.approx(1.1000)
        assert t.quantity == pytest.approx(0.10)
        assert _exits(t) == [
            (
                "2024-01-02T12:00:00Z",
                pytest.approx(1.1050),
                pytest.approx(0.10),
                pytest.approx(50.00),
                "take_profit",
            )
        ]
        # (1.1050 - 1.1000) * 0.10 * 100 000 = 50.00; fees -0.70 - 0.70 = -1.40
        assert t.pnl == pytest.approx(50.00)
        assert t.fees == pytest.approx(-1.40)
        assert t.stop_loss == pytest.approx(1.0950)
        assert t.take_profit == pytest.approx(1.1050)
        assert t.exit_reason == "take_profit"
        assert t.tags == ["Breakout Long"]

    def test_p2_split_exit_keeps_each_fill(self, hedged: ValidationReference) -> None:
        t = hedged.trades[1]
        assert t.entry_price == pytest.approx(1.1010)
        assert t.quantity == pytest.approx(0.20)
        assert [(e.price, e.quantity, e.pnl, e.reason) for e in t.exits] == [
            (pytest.approx(1.1040), pytest.approx(0.12), pytest.approx(36.00), None),
            (pytest.approx(1.1030), pytest.approx(0.08), pytest.approx(16.00), "take_profit"),
        ]
        # (1.1040 - 1.1010) * 0.12 * 100 000 = 36.00; (1.1030 - 1.1010) * 0.08 * 100 000 = 16.00
        assert t.pnl == pytest.approx(52.00)
        # fees: entry -1.40; exits -0.84 and -0.56
        assert t.fees == pytest.approx(-2.80)
        assert t.stop_loss == pytest.approx(1.0980)
        assert t.take_profit is None
        # Last exit's comment decides the trade's exit reason.
        assert t.exit_reason == "take_profit"

    def test_p3_hedge_short_is_not_merged_into_open_long(self, hedged: ValidationReference) -> None:
        t = hedged.trades[2]
        assert t.direction == "SHORT"
        assert t.entry_price == pytest.approx(1.1020)
        assert t.quantity == pytest.approx(0.10)
        assert _exits(t) == [
            (
                "2024-01-03T10:00:00Z",
                pytest.approx(1.1035),
                pytest.approx(0.10),
                pytest.approx(-15.00),
                "stop_loss",
            )
        ]
        # (1.1020 - 1.1035) * 0.10 * 100 000 = -15.00
        assert t.pnl == pytest.approx(-15.00)
        # fees: entry -0.70; exit -0.70 commission and -0.30 swap = -1.70
        assert t.fees == pytest.approx(-1.70)
        assert t.stop_loss == pytest.approx(1.1050)
        assert t.exit_reason == "stop_loss"

    def test_long_quantity_is_p1_plus_p2_only(self, hedged: ValidationReference) -> None:
        """The hedge short must not add volume to the open long P2 (0.10 + 0.20 = 0.30)."""
        longs = [t for t in hedged.trades if t.direction == "LONG"]
        assert sum(t.quantity for t in longs) == pytest.approx(0.30)

    def test_partial_position_yields_no_trade(self, hedged: ValidationReference) -> None:
        evidence = hedged.raw_metadata["position_fills"]
        assert evidence["partial_position_count"] == 1
        assert evidence["partial_position_ids"] == ["position-4"]
        p4 = next(p for p in evidence["positions"] if p["position_id"] == "position-4")
        assert p4["status"] == "partial"
        assert p4["entry_quantity"] == pytest.approx(0.20)
        assert p4["exit_quantity"] == pytest.approx(0.12)
        assert p4["residual_quantity"] == pytest.approx(0.08)
        assert p4["residual_exit_quantity"] == pytest.approx(0.0)
        # P4 and P5 were both opened on 2024-01-04; neither may appear as a trade.
        assert all(not t.entry_time.startswith("2024-01-04") for t in hedged.trades)

    def test_open_position_yields_no_trade(self, hedged: ValidationReference) -> None:
        evidence = hedged.raw_metadata["position_fills"]
        assert evidence["open_only_count"] == 1
        p5 = next(p for p in evidence["positions"] if p["position_id"] == "position-5")
        assert p5["status"] == "open"
        assert p5["entry_quantity"] == pytest.approx(0.05)
        assert p5["exit_quantity"] == pytest.approx(0.0)
        assert p5["residual_quantity"] == pytest.approx(0.05)

    def test_position_counts(self, hedged: ValidationReference) -> None:
        evidence = hedged.raw_metadata["position_fills"]
        assert evidence["closed_position_count"] == 3
        assert evidence["closed_position_ids"] == ["position-1", "position-2", "position-3"]
        assert evidence["partial_position_count"] == 1
        assert evidence["open_position_count"] == 2  # open + partial
        assert evidence["open_only_count"] == 1
        assert evidence["orphan_exit_count"] == 0
        assert evidence["residual_position_count"] == 2  # P4 0.08 + P5 0.05
        assert evidence["residual_exit_count"] == 0

    def test_summary_block_is_parsed(self, hedged: ValidationReference) -> None:
        assert hedged.stats["total_trades"] == 3
        assert hedged.stats["winners"] == 2
        assert hedged.stats["losers"] == 1
        assert hedged.stats["win_rate"] == pytest.approx(66.67)
        assert hedged.stats["total_deals"] == 10

    def test_total_pnl_and_total_return_match_hand_reconciliation(
        self, hedged: ValidationReference
    ) -> None:
        """MT5 "Nettogewinn gesamt" = every out-deal profit + every commission + swap.

        Out-deal profits: 50 + 36 + 16 - 15 + 24 = 111.00 (P4's 24.00 is partial, not a trade).
        Commission + swap over all fills: P1 -1.40, P2 -2.80, P3 -1.70, P4 -2.24, P5 -0.35 = -8.49.
        111.00 - 8.49 = 102.51, which equals the final balance 10102.51 minus the deposit.
        """
        assert hedged.stats["total_pnl"] == pytest.approx(111.00 - 8.49)
        assert hedged.stats["total_pnl"] == pytest.approx(102.51)
        assert hedged.stats["total_return"] == pytest.approx(102.51 / 10000.0)
        assert hedged.stats["total_return"] == pytest.approx(0.010251)

    def test_trade_pnl_sum_excludes_partial_and_open_positions(
        self, hedged: ValidationReference
    ) -> None:
        # 50.00 + 52.00 - 15.00 = 87.00 over the three closed trades only.
        assert sum(t.pnl for t in hedged.trades) == pytest.approx(87.00)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known bug: importer sets net_pnl = pnl - fees, but fees are MT5-signed "
            "(commission and swap are negative when they cost money). Net should be "
            "pnl + fees. Expected net for P1 is 50.00 - 1.40 = 48.60, not 51.40."
        ),
    )
    def test_net_pnl_is_gross_plus_signed_fees(self, hedged: ValidationReference) -> None:
        assert hedged.trades[0].net_pnl == pytest.approx(48.60)


class TestPositionIdSharedReport:
    """``position_id_shared.xlsx``: the Trade column repeats the position id."""

    def test_one_trade_per_position_id(self, shared: ValidationReference) -> None:
        assert [t.meta["position_id"] for t in shared.trades] == ["1001", "1002", "1004"]
        assert [t.direction for t in shared.trades] == ["LONG", "SHORT", "LONG"]

    def test_split_entries_aggregate_and_keep_first_entry_price(
        self, shared: ValidationReference
    ) -> None:
        t = shared.trades[0]
        assert t.entry_time == "2024-02-01T09:00:00Z"
        # Importer convention: entry_price is the first entry fill (0.10 @ 1.1000;
        # the second entry is 0.05 @ 1.1010).
        assert t.entry_price == pytest.approx(1.1000)
        assert t.quantity == pytest.approx(0.15)
        assert t.tags == ["Entry A"]

    def test_split_exits_keep_price_quantity_and_pnl(self, shared: ValidationReference) -> None:
        t = shared.trades[0]
        assert [(e.price, e.quantity, e.pnl) for e in t.exits] == [
            (pytest.approx(1.1040), pytest.approx(0.10), pytest.approx(40.00)),
            (pytest.approx(1.1030), pytest.approx(0.05), pytest.approx(15.00)),
        ]
        assert t.pnl == pytest.approx(55.00)
        # fees: -0.50 -0.25 (entries) and -0.50 -0.25 (exits)
        assert t.fees == pytest.approx(-1.50)
        assert t.exit_reason == "take_profit"

    def test_short_round_trip(self, shared: ValidationReference) -> None:
        t = shared.trades[1]
        assert t.entry_price == pytest.approx(1.1100)
        assert t.quantity == pytest.approx(0.20)
        # (1.1100 - 1.1080) * 0.20 * 100 000 = 40.00
        assert t.pnl == pytest.approx(40.00)
        assert t.fees == pytest.approx(-2.00)
        assert t.exit_reason == "take_profit"

    def test_overlapping_same_side_position_is_kept_separate(
        self, shared: ValidationReference
    ) -> None:
        """1004 opens while 1003 still has 0.06 open. Shared-id grouping keeps it apart.

        A sequential walker would merge the 0.05 entry into 1003 and close 1003's
        residual instead, so 1004 would have no trade.
        """
        t = shared.trades[2]
        assert t.entry_time == "2024-02-01T18:30:00Z"
        assert t.entry_price == pytest.approx(1.1070)
        assert t.quantity == pytest.approx(0.05)
        # (1.1080 - 1.1070) * 0.05 * 100 000 = 5.00; fees -0.25 -0.25
        assert t.pnl == pytest.approx(5.00)
        assert t.fees == pytest.approx(-0.50)

    def test_partial_repeated_id_yields_no_trade(self, shared: ValidationReference) -> None:
        evidence = shared.raw_metadata["position_fills"]
        assert evidence["partial_position_ids"] == ["1003"]
        p = next(p for p in evidence["positions"] if p["position_id"] == "1003")
        assert p["status"] == "partial"
        assert p["entry_quantity"] == pytest.approx(0.10)
        assert p["exit_quantity"] == pytest.approx(0.04)
        assert p["residual_quantity"] == pytest.approx(0.06)
        assert evidence["closed_position_count"] == 3
        assert evidence["orphan_exit_count"] == 0

    def test_summary_block_is_parsed(self, shared: ValidationReference) -> None:
        assert shared.stats["total_trades"] == 3
        assert shared.stats["winners"] == 3
        assert shared.stats["losers"] == 0
        assert shared.stats["win_rate"] == pytest.approx(100.0)
        assert shared.stats["total_deals"] == 10

    def test_total_pnl_matches_hand_reconciliation(self, shared: ValidationReference) -> None:
        """MT5 net = every out-deal profit + every commission over all fills.

        Profits: 40 (1001) + 15 (1001) + 40 (1002) + 4 (1003 partial) + 5 (1004) = 104.00.
        Commission: -1.50 (1001) -2.00 (1002) -0.70 (1003) -0.50 (1004) = -4.70.
        104.00 - 4.70 = 99.30.
        """
        assert shared.stats["total_pnl"] == pytest.approx(99.30)
        assert shared.stats["total_return"] == pytest.approx(0.00993)
