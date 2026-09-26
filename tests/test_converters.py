"""Round-trip fixtures for LEAN/Stockkit converters (C6)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _lean_order(
    order_id: int,
    time: str,
    qty: float,
    price: float,
    tag: str = "",
    fee: float = 0.0,
) -> dict:
    order = {
        "id": order_id,
        "time": time,
        "quantity": qty,
        "price": price,
        "tag": tag,
        "symbol": {"value": "AAPL 2T"},
    }
    if fee:
        order["fee"] = fee
    return order


def _lean_payload(orders: list[dict]) -> dict:
    return {
        "orders": {str(order["id"]): order for order in orders},
        "profitLoss": {},
    }


class TestLeanPairing:
    def test_partial_fills_close_proportionally(self, tmp_path: Path) -> None:
        from runner.converters.lean_converter import convert_lean_result

        payload = _lean_payload(
            [
                _lean_order(1, "2024-01-01T10:00:00Z", 10.0, 100.0, tag="Entry"),
                _lean_order(2, "2024-01-01T11:00:00Z", -4.0, 101.0, tag="Exit"),
                _lean_order(3, "2024-01-01T12:00:00Z", -6.0, 102.0, tag="Exit"),
            ]
        )
        path = tmp_path / "lean.json"
        path.write_text(json.dumps(payload))
        result = convert_lean_result(path)

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.quantity == 10.0
        assert [exit.quantity for exit in trade.exits] == [4.0, 6.0]
        # (102*6 + 101*4)/10 = 101.6 avg exit; (101.6-100)*10 = 16.0
        assert trade.pnl == 16.0

    def test_orphan_exit_skipped_not_paired(self, tmp_path: Path, caplog) -> None:
        import logging

        from runner.converters.lean_converter import convert_lean_result

        payload = _lean_payload(
            [_lean_order(1, "2024-01-01T11:00:00Z", -5.0, 101.0, tag="Exit")]
        )
        path = tmp_path / "lean.json"
        path.write_text(json.dumps(payload))
        with caplog.at_level(logging.WARNING, logger="runner.converters.lean_converter"):
            result = convert_lean_result(path)
        assert result.trades == []
        assert any("orphan exit" in record.message for record in caplog.records)

    def test_fee_passthrough(self, tmp_path: Path) -> None:
        from runner.converters.lean_converter import convert_lean_result

        payload = _lean_payload(
            [
                _lean_order(1, "2024-01-01T10:00:00Z", 10.0, 100.0, tag="Entry", fee=1.0),
                _lean_order(2, "2024-01-01T11:00:00Z", -10.0, 101.0, tag="Exit", fee=1.0),
            ]
        )
        path = tmp_path / "lean.json"
        path.write_text(json.dumps(payload))
        (trade,) = convert_lean_result(path).trades
        assert trade.fees == 2.0

    def test_list_orders_with_same_timestamp_sort_numeric_ids(self, tmp_path: Path) -> None:
        from runner.converters.lean_converter import convert_lean_result

        payload = {
            "orders": [
                _lean_order(10, "2024-01-01T10:00:00Z", -10.0, 101.0, tag="Exit"),
                _lean_order(2, "2024-01-01T10:00:00Z", 10.0, 100.0, tag="Entry"),
            ],
            "profitLoss": {},
        }
        path = tmp_path / "lean.json"
        path.write_text(json.dumps(payload))
        result = convert_lean_result(path)
        assert len(result.trades) == 1
        assert result.trades[0].quantity == 10.0

    def test_meta_carries_real_bounds(self, tmp_path: Path) -> None:
        from runner.converters.lean_converter import convert_lean_result

        payload = _lean_payload(
            [
                _lean_order(1, "2024-01-01T10:00:00Z", 10.0, 100.0, tag="Entry"),
                _lean_order(2, "2024-01-05T11:00:00Z", -10.0, 101.0, tag="Exit"),
            ]
        )
        path = tmp_path / "lean.json"
        path.write_text(json.dumps(payload))
        meta = convert_lean_result(path).meta
        assert meta.backtest_start.startswith("2024-01-01")
        assert meta.backtest_end.startswith("2024-01-05")
        assert meta.initial_capital == 100000.0


def test_converter_output_preserves_existing_file_on_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    from runner.converters.cli import cmd_convert

    source = tmp_path / "lean.json"
    source.write_text(
        json.dumps(
            _lean_payload([_lean_order(1, "2024-01-01T00:00:00Z", 1, 100)])
        ),
        encoding="utf-8",
    )
    output = tmp_path / "result.json"
    output.write_text("previous", encoding="utf-8")

    def fail_write(*_args, **_kwargs):
        raise OSError("simulated converter write failure")

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="simulated"):
        cmd_convert(
            SimpleNamespace(
                engine="lean",
                input=str(source),
                name="demo",
                output=str(output),
            )
        )

    assert output.read_text(encoding="utf-8") == "previous"


class TestStockkitConverter:
    def _rows(self) -> list[dict]:
        return [
            {
                "Symbol": "AAPL",
                "Direction": "LONG",
                "Entry Date": "2024-01-01",
                "Entry Time": "10:00:00",
                "Exit Date": "2024-01-02",
                "Exit Time": "11:00:00",
                "Entry Price": "100.0",
                "Exit Price": "105.0",
                "Qty Total": "10.5",
                "Qty TP1": "4.25",
                "Qty Runner": "6.25",
                "TP1 Reached": "yes",
                "TP1 Price": "103.0",
                "Exit Reason": "runner",
                "PnL ($)": "40.0",
                "Slippage": "1.5",
            }
        ]

    def test_fractional_qty_preserved(self) -> None:
        from runner.converters.stockkit_converter import _build_trades

        (trade,) = _build_trades(self._rows(), "test")
        assert trade.quantity == 10.5
        assert [exit.quantity for exit in trade.exits] == [4.25, 6.25]

    def test_tp1_exit_has_usable_timestamp(self) -> None:
        from runner.converters.stockkit_converter import _build_trades

        (trade,) = _build_trades(self._rows(), "test")
        assert all(exit.time for exit in trade.exits)

    def test_slippage_is_meta_not_fee(self) -> None:
        from runner.converters.stockkit_converter import _build_trades

        (trade,) = _build_trades(self._rows(), "test")
        assert trade.fees == 0.0
        assert trade.meta["slippage"] == 1.5

    def test_garbage_datetime_skips_row(self) -> None:
        from runner.converters.stockkit_converter import _build_trades

        rows = self._rows()
        rows[0]["Entry Date"] = "not-a-date"
        assert _build_trades(rows, "test") == []
