"""DV-B43: split fills and multi-exit positions must survive import.

The two MT5 importers must agree on the same report structure:

* One position can be *entered* through several fills (several ``in`` rows
  with the same position/``Trade`` id but different ``Auftrag`` order ids).
* One position can be *exited* through several fills (several ``out`` rows
  with the same position id).
* The importer must reconstruct ONE :class:`~shared.models.Trade` per
  position with the aggregated entry quantity, one :class:`TradeExit` per
  exit fill and ``pnl`` equal to the sum of the exit PnLs — instead of
  dropping everything that does not pair 1:1.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from validation.importers.mt5_html import MT5HTMLImporter
from validation.importers.mt5_xlsx import MT5XLSXImporter


def _split_fill_html() -> Path:
    return Path(__file__).resolve().parents[2] / "tests/fixtures/validation/mt5_html/split_fills.html"


class TestHTMLSplitFills:
    def test_split_entry_and_exit_become_one_trade(self) -> None:
        ref = MT5HTMLImporter().load(_split_fill_html())
        # Position 1 = two entry fills + two exit fills -> ONE trade.
        # Position 2 = plain in/out pair -> ONE trade.
        assert len(ref.trades) == 2

    def test_aggregated_entry_quantity(self) -> None:
        ref = MT5HTMLImporter().load(_split_fill_html())
        first = ref.trades[0]
        assert first.direction == "LONG"
        assert first.quantity == pytest.approx(0.15)

    def test_pnl_is_sum_of_exit_fills(self) -> None:
        ref = MT5HTMLImporter().load(_split_fill_html())
        first = ref.trades[0]
        assert len(first.exits) == 2
        assert first.pnl == pytest.approx(300.0 + 250.0)

    def test_each_exit_keeps_its_own_price_and_pnl(self) -> None:
        ref = MT5HTMLImporter().load(_split_fill_html())
        first = ref.trades[0]
        exit_prices = sorted(e.price for e in first.exits)
        exit_pnls = sorted(e.pnl or 0.0 for e in first.exits)
        assert exit_prices == pytest.approx([1.0880, 1.0900])
        assert exit_pnls == pytest.approx([250.0, 300.0])

    def test_plain_pair_still_one_trade(self) -> None:
        ref = MT5HTMLImporter().load(_split_fill_html())
        second = ref.trades[1]
        assert second.direction == "SHORT"
        assert second.quantity == pytest.approx(0.10)
        assert second.pnl == pytest.approx(-250.0)
        assert len(second.exits) == 1

    def test_fees_sum_over_all_fills(self) -> None:
        ref = MT5HTMLImporter().load(_split_fill_html())
        first = ref.trades[0]
        # -0.50 (entry 1) -0.25 (entry 2) -0.75 (exit 1) -0.50 (exit 2)
        assert first.fees == pytest.approx(-2.00)

    def test_importers_agree_on_same_position_sequence(self) -> None:
        """Both importers must produce the same positions from the same report."""
        html_ref = MT5HTMLImporter().load(_split_fill_html())
        xlsx_ref = MT5XLSXImporter().load(_split_fill_xlsx())

        assert len(html_ref.trades) == len(xlsx_ref.trades) == 2
        for html_trade, xlsx_trade in zip(html_ref.trades, xlsx_ref.trades):
            assert html_trade.direction == xlsx_trade.direction
            assert html_trade.quantity == pytest.approx(xlsx_trade.quantity)
            assert html_trade.pnl == pytest.approx(xlsx_trade.pnl)
            assert len(html_trade.exits) == len(xlsx_trade.exits)


def _split_fill_xlsx() -> Path:
    return Path(__file__).resolve().parents[2] / "tests/fixtures/validation/mt5_xlsx_split_fills.xlsx"


class TestXLSXSplitFills:
    def test_split_entry_and_exit_become_one_trade(self) -> None:
        ref = MT5XLSXImporter().load(_split_fill_xlsx())
        assert len(ref.trades) == 2

    def test_aggregated_quantity_and_summed_pnl(self) -> None:
        ref = MT5XLSXImporter().load(_split_fill_xlsx())
        first = ref.trades[0]
        assert first.quantity == pytest.approx(0.15)
        assert first.pnl == pytest.approx(550.0)
        assert len(first.exits) == 2

    def test_positional_walker_does_not_drop_split_fills(self) -> None:
        """Regression: the old in→out walker silently dropped fills."""
        ref = MT5XLSXImporter().load(_split_fill_xlsx())
        # Both positions present, nothing dropped.
        quantities = sorted(t.quantity for t in ref.trades)
        assert quantities == pytest.approx([0.10, 0.15])

    def test_deal_numbered_reports_pair_split_fills(self, tmp_path: Path) -> None:
        """Real MT5 reports carry a unique deal id per row (docs/mt5_xlsx_schema.md).

        Grouping by that id would strand every entry with no exit; the deal
        sequence must be used instead, so an in/in/out/out sequence becomes
        ONE position with the aggregated entry quantity and both exits —
        the same reconstruction the HTML importer produces.
        """
        deals = pd.DataFrame(
            [
                ["Trades"] + [None] * 11,
                ["Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen", "Preis", "Auftrag",
                 "Kommission", "Swap", "Gewinn", "Kontostand", "Kommentar"],
                ["2024.01.03 08:00:00", 2, "EURUSD", "buy", "in", "0.10", 1.085, 1001, -0.50, 0, 0, 100000, "Entry"],
                ["2024.01.03 09:00:00", 3, "EURUSD", "buy", "in", "0.05", 1.086, 1002, -0.25, 0, 0, 100000, "Add"],
                ["2024.01.03 16:00:00", 4, "EURUSD", "sell", "out", "0.10", 1.088, 1003, -0.75, 0, 300.0, 100300, "Partial TP"],
                ["2024.01.03 17:00:00", 5, "EURUSD", "sell", "out", "0.05", 1.090, 1004, -0.50, 0, 250.0, 100550, "TP 1.09000"],
                ["2024.01.04 08:00:00", 6, "EURUSD", "sell", "in", "0.10", 1.092, 1005, -0.50, 0, 0, 100550, "Entry Short"],
                ["2024.01.04 16:00:00", 7, "EURUSD", "buy", "out", "0.10", 1.097, 1006, -0.50, 0, -250.0, 100300, "SL 1.09700"],
            ]
        )
        orders = pd.DataFrame(
            [
                ["Orders"] + [None] * 9,
                ["Auftrag", "Zeit", "Symbol", "Typ", "Richtung", "Volumen", "Preis", "S / L", "T / P", "Kommentar"],
                [1001, "2024.01.03 08:00:00", "EURUSD", "buy", "in", "0.10", 1.085, 1.08, 1.095, "Entry"],
                [1002, "2024.01.03 09:00:00", "EURUSD", "buy", "in", "0.05", 1.086, 1.08, 1.095, "Add"],
                [1003, "2024.01.03 16:00:00", "EURUSD", "sell", "out", "0.10", 1.088, None, None, "Partial TP"],
                [1004, "2024.01.03 17:00:00", "EURUSD", "sell", "out", "0.05", 1.090, None, None, "TP 1.09000"],
                [1005, "2024.01.04 08:00:00", "EURUSD", "sell", "in", "0.10", 1.092, 1.097, 1.085, "Entry Short"],
                [1006, "2024.01.04 16:00:00", "EURUSD", "buy", "out", "0.10", 1.097, None, None, "SL 1.09700"],
            ]
        )
        path = tmp_path / "deal_numbered.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            orders.to_excel(writer, index=False, header=False, startrow=0)
            deals.to_excel(writer, index=False, header=False, startrow=20)

        ref = MT5XLSXImporter().load(path)
        assert len(ref.trades) == 2
        first = ref.trades[0]
        assert first.quantity == pytest.approx(0.15)
        assert first.pnl == pytest.approx(550.0)
        assert len(first.exits) == 2
        assert ref.trades[1].quantity == pytest.approx(0.10)


def _build_split_fill_xlsx(path: Path) -> Path:
    """Deterministic in-repo builder so the binary fixture stays reproducible."""
    orders = pd.DataFrame(
        [
            ["Orders"] + [None] * 9,
            ["Auftrag", "Zeit", "Symbol", "Typ", "Richtung", "Volumen", "Preis", "S / L", "T / P", "Kommentar"],
            [1001, "2024.01.03 08:00:00", "EURUSD", "buy", "in", "0.10", 1.085, 1.08, 1.095, "Entry"],
            [1002, "2024.01.03 09:00:00", "EURUSD", "buy", "in", "0.05", 1.086, 1.08, 1.095, "Add"],
            [1003, "2024.01.03 16:00:00", "EURUSD", "sell", "out", "0.10", 1.088, None, None, "Partial TP"],
            [1004, "2024.01.03 17:00:00", "EURUSD", "sell", "out", "0.05", 1.090, None, None, "TP 1.09000"],
            [1005, "2024.01.04 08:00:00", "EURUSD", "sell", "in", "0.10", 1.092, 1.097, 1.085, "Entry Short"],
            [1006, "2024.01.04 16:00:00", "EURUSD", "buy", "out", "0.10", 1.097, None, None, "SL 1.09700"],
        ]
    )
    trades = pd.DataFrame(
        [
            ["Trades"] + [None] * 11,
            ["Zeit", "Trade", "Symbol", "Typ", "Richtung", "Volumen", "Preis", "Auftrag",
             "Kommission", "Swap", "Gewinn", "Kontostand", "Kommentar"],
            ["2024.01.03 08:00:00", 1, "EURUSD", "buy", "in", "0.10", 1.085, 1001, -0.50, 0, 0, 100000, "Entry"],
            ["2024.01.03 09:00:00", 1, "EURUSD", "buy", "in", "0.05", 1.086, 1002, -0.25, 0, 0, 100000, "Add"],
            ["2024.01.03 16:00:00", 1, "EURUSD", "sell", "out", "0.10", 1.088, 1003, -0.75, 0, 300.0, 100300, "Partial TP"],
            ["2024.01.03 17:00:00", 1, "EURUSD", "sell", "out", "0.05", 1.090, 1004, -0.50, 0, 250.0, 100550, "TP 1.09000"],
            ["2024.01.04 08:00:00", 2, "EURUSD", "sell", "in", "0.10", 1.092, 1005, -0.50, 0, 0, 100550, "Entry Short"],
            ["2024.01.04 16:00:00", 2, "EURUSD", "buy", "out", "0.10", 1.097, 1006, -0.50, 0, -250.0, 100300, "SL 1.09700"],
        ]
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        orders.to_excel(writer, index=False, header=False, startrow=0)
        trades.to_excel(writer, index=False, header=False, startrow=20)
    return path


def test_split_fill_xlsx_fixture_is_deterministic(tmp_path: Path) -> None:
    built = _build_split_fill_xlsx(tmp_path / "split.xlsx")
    ref_a = MT5XLSXImporter().load(built)
    ref_b = MT5XLSXImporter().load(_split_fill_xlsx())
    assert len(ref_a.trades) == len(ref_b.trades)
    for a, b in zip(ref_a.trades, ref_b.trades):
        assert a.direction == b.direction
        assert a.quantity == pytest.approx(b.quantity)
        assert a.pnl == pytest.approx(b.pnl)
