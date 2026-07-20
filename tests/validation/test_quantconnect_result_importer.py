from __future__ import annotations

import csv
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from validation.importers.quantconnect_result import QuantConnectResultImporter

REPO_ROOT = Path(__file__).resolve().parents[2]


def _reference_dir() -> Path:
    configured = os.environ.get("QUANTCONNECT_REFERENCE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return REPO_ROOT / "tests/fixtures/validation/quantconnect-reference"


REFERENCE_DIR = _reference_dir()
RESULT_JSON = REFERENCE_DIR / "Sleepy Tan Coyote.json"
ORDERS_CSV = REFERENCE_DIR / "Sleepy Tan Coyote_orders.csv"
TRADES_CSV = REFERENCE_DIR / "Sleepy Tan Coyote_trades.csv"


def _order(order_id: int = 1) -> dict[str, object]:
    return {
        "type": 5,
        "id": order_id,
        "contingentId": 0,
        "brokerId": [str(order_id)],
        "symbol": {
            "value": "SPY",
            "id": "SPY R735QTJ8XC9X",
            "permtick": "SPY",
        },
        "price": 100.25,
        "priceCurrency": "USD",
        "time": "2024-01-02T15:00:00Z",
        "createdTime": "2024-01-02T15:00:00Z",
        "lastFillTime": "2024-01-02T21:00:00Z",
        "quantity": 10.0,
        "status": 3,
        "tag": "rebalance",
        "properties": {"timeInForce": {}},
        "securityType": 1,
        "direction": 0,
        "value": 1002.5,
        "orderSubmissionData": {
            "bidPrice": 100.0,
            "askPrice": 100.1,
            "lastPrice": 100.05,
        },
        "isMarketable": False,
        "priceAdjustmentMode": 1,
    }


def _closed_trade(
    qc_id: str = "closed-1",
    order_ids: list[int] | None = None,
) -> dict[str, object]:
    return {
        "id": qc_id,
        "symbols": [
            {
                "value": "SPY",
                "id": "SPY R735QTJ8XC9X",
                "permtick": "SPY",
            }
        ],
        "entryTime": "2024-01-02T21:00:00Z",
        "entryPrice": 100.25,
        "direction": 0,
        "quantity": 10.0,
        "exitTime": "2024-02-01T21:00:00Z",
        "exitPrice": 110.0,
        "profitLoss": 97.5,
        "totalFees": 2.5,
        "mae": -10.0,
        "mfe": 120.0,
        "duration": "30.00:00:00",
        "endTradeDrawdown": 22.0,
        "isWin": True,
        "orderIds": order_ids or [1],
    }


def _valid_result() -> dict[str, object]:
    return {
        "state": {
            "Name": "Minimal QC Result",
            "Status": "Completed",
            "RuntimeError": "",
            "LogCount": "1",
            "OrderCount": "1",
            "InsightCount": "0",
        },
        "algorithmConfiguration": {
            "name": "Minimal QC Result",
            "accountCurrency": "USD",
            "parameters": {},
            "startDate": "2024-01-01T00:00:00Z",
            "endDate": "2024-02-02T00:00:00Z",
        },
        "orders": {"1": _order()},
        "statistics": {
            "Total Orders": "1",
            "Total Fees": "$2.50",
            "Start Equity": "1000",
            "End Equity": "1097.50",
        },
        "runtimeStatistics": {},
        "totalPerformance": {
            "portfolioStatistics": {
                "startEquity": "1000",
                "endEquity": "1097.5",
                "totalNetProfit": "0.0975",
                "sharpeRatio": "0.5",
                "winRate": "0.6",
                "lossRate": "0.4",
            },
            "tradeStatistics": {
                "totalNumberOfTrades": 1,
                "numberOfWinningTrades": 1,
                "numberOfLosingTrades": 0,
                "winRate": "1.0",
                "lossRate": "0.0",
                "totalFees": "2.5",
            },
            "closedTrades": [_closed_trade()],
        },
        "charts": {
            "Strategy Equity": {
                "name": "Strategy Equity",
                "chartType": 0,
                "series": {
                    "Equity": {
                        "name": "Equity",
                        "unit": "$",
                        "seriesType": 2,
                        "index": 0,
                        "values": [
                            [1704067200, 1000.0, 1000.0, 1000.0, 1000.0],
                            [1706745600, 1100.0, 1100.0, 1097.5, 1097.5],
                        ],
                    }
                },
            }
        },
    }


def _write_result(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_reference_dir_uses_environment_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("QUANTCONNECT_REFERENCE_DIR", str(tmp_path))

    assert _reference_dir() == tmp_path


@pytest.mark.parametrize("configured", [None, "", "  "])
def test_reference_dir_defaults_to_repo_fixture(
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
):
    if configured is None:
        monkeypatch.delenv("QUANTCONNECT_REFERENCE_DIR", raising=False)
    else:
        monkeypatch.setenv("QUANTCONNECT_REFERENCE_DIR", configured)

    assert _reference_dir() == (
        REPO_ROOT / "tests/fixtures/validation/quantconnect-reference"
    )


def test_closed_trade_net_pnl_subtracts_fees(tmp_path: Path):
    reference = QuantConnectResultImporter().load(
        _write_result(tmp_path, _valid_result())
    )
    trade = reference.trades[0]

    assert trade.pnl == pytest.approx(97.5)
    assert trade.exits[0].pnl == pytest.approx(97.5)
    assert trade.fees == pytest.approx(2.5)
    assert trade.net_pnl == pytest.approx(95.0)


def test_non_finite_closed_trade_profit_loss_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["totalPerformance"]["closedTrades"][0]["profitLoss"] = float("nan")
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(
        ValueError,
        match=r"^closedTrade\.profitLoss must be finite$",
    ):
        importer.load(source)


def test_fractional_numeric_order_id_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["orders"]["1"]["id"] = 1.5
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match=r"^order\.id must be an integer$"):
        importer.load(source)


def test_order_numeric_value_with_trailing_text_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["orders"]["1"]["price"] = "100.25 USD"
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match=r"^order\.price must be numeric$"):
        importer.load(source)


def test_closed_trade_without_symbol_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["totalPerformance"]["closedTrades"][0]["symbols"] = []
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match=r"^closedTrade\.symbols must not be empty$"):
        importer.load(source)


def test_empty_closed_trade_id_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["totalPerformance"]["closedTrades"][0]["id"] = ""
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match=r"^closedTrade\.id must not be empty$"):
        importer.load(source)


def test_empty_closed_trade_order_ids_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["totalPerformance"]["closedTrades"][0]["orderIds"] = []
    payload["statistics"]["Total Fees"] = "$3.50"
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(
        ValueError,
        match=r"^closedTrade\.orderIds must not be empty$",
    ):
        importer.load(source)


def test_duplicate_closed_trade_order_ids_fail_closed(tmp_path: Path):
    payload = _valid_result()
    payload["totalPerformance"]["closedTrades"][0]["orderIds"] = [1, 1]
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(
        ValueError,
        match=r"^closedTrade\.orderIds must not contain duplicates$",
    ):
        importer.load(source)


def test_duplicate_closed_trade_id_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["state"]["OrderCount"] = "2"
    payload["orders"]["2"] = _order(2)
    payload["statistics"]["Total Orders"] = "2"
    payload["statistics"]["Total Fees"] = "$5.00"
    payload["totalPerformance"]["tradeStatistics"]["totalNumberOfTrades"] = 2
    payload["totalPerformance"]["tradeStatistics"]["numberOfWinningTrades"] = 2
    payload["totalPerformance"]["tradeStatistics"]["totalFees"] = "5.0"
    payload["totalPerformance"]["closedTrades"] = [
        _closed_trade(),
        _closed_trade(order_ids=[2]),
    ]
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match=r"^duplicate closedTrade\.id: closed-1$"):
        importer.load(source)


@pytest.fixture(scope="module")
def golden_reference():
    assert RESULT_JSON.is_file(), f"Missing external QC reference: {RESULT_JSON}"
    return QuantConnectResultImporter().load(
        RESULT_JSON,
        orders_csv=ORDERS_CSV,
        trades_csv=TRADES_CSV,
    )


def test_real_export_normalizes_orders_clusters_and_provenance(golden_reference):
    ref = golden_reference
    raw = ref.raw_metadata
    orders = raw["orders"]
    clusters = raw["rebalance_clusters"]

    assert ref.source_framework == "lean"
    assert ref.source_path == str(RESULT_JSON.resolve())
    assert ref.allow_empty is False
    assert raw["source_framework"] == "quantconnect"
    assert raw["source_path"] == str(RESULT_JSON.resolve())
    assert raw["source_timezone"] == "UTC"
    assert raw["schema_version"] == "quantconnect-result-v1"

    assert len(orders) == 264
    assert len({order["id"] for order in orders}) == 264
    assert {order["type"] for order in orders} == {5}
    assert {order["type_label"] for order in orders} == {"Market On Close"}
    assert {order["status"] for order in orders} == {3}
    assert {order["status_label"] for order in orders} == {"Filled"}
    assert orders[0]["broker_ids"] == ["1"]
    assert orders[0]["currency"] == "USD"
    assert orders[0]["fill_time"] == "2016-01-04T21:00:00Z"

    assert len(clusters) == 84
    assert clusters[0]["submission_time"] == "2016-01-04T15:00:00Z"
    assert clusters[-1]["submission_time"] == "2024-12-02T15:00:00Z"
    assert raw["rebalance_cluster_summary"] == {
        "count": 84,
        "minimum_size": 1,
        "maximum_size": 8,
        "size_distribution": {1: 17, 2: 16, 3: 17, 4: 15, 5: 15, 6: 1, 7: 1, 8: 2},
    }
    assert all(cluster["order_ids"] == sorted(cluster["order_ids"]) for cluster in clusters)


def test_real_export_normalizes_closed_trades_and_fee_reconciliation(golden_reference):
    ref = golden_reference
    raw = ref.raw_metadata

    assert len(ref.trades) == 236
    assert [trade.id for trade in ref.trades] == list(range(1, 237))
    assert {trade.direction for trade in ref.trades} == {"LONG"}
    assert sum(trade.pnl > 0 for trade in ref.trades) == 160
    assert sum(trade.pnl < 0 for trade in ref.trades) == 76

    first = ref.trades[0]
    assert first.symbol == "EEM"
    assert first.entry_time == "2016-01-04T21:00:00Z"
    assert first.exits[0].time == "2016-02-01T21:00:00Z"
    assert first.fees == pytest.approx(32.375)
    assert first.mae == pytest.approx(-2363.73)
    assert first.mfe == pytest.approx(54.07)
    assert first.meta["qc_closed_trade_id"] == "be88484b-1b12-4abc-8d16-9e85c2d3c704"
    assert first.meta["order_ids"] == [4, 6]
    assert first.meta["duration"] == "28.00:00:00"

    fees = raw["fee_reconciliation"]
    assert fees["portfolio_total_fees"] == pytest.approx(2698.00)
    assert fees["closed_trade_total_fees"] == pytest.approx(2616.03)
    assert fees["fee_remainder"] == pytest.approx(81.97)
    assert fees["unreferenced_order_count"] == 20
    assert len(fees["unreferenced_order_ids"]) == 20


def test_real_export_derives_unreferenced_order_fees_from_source_quantities(
    golden_reference,
):
    payload = json.loads(RESULT_JSON.read_text(encoding="utf-8"), parse_float=Decimal)
    source_orders = {
        int(order["id"]): order for order in payload["orders"].values()
    }
    referenced_order_ids = {
        int(order_id)
        for trade in payload["totalPerformance"]["closedTrades"]
        for order_id in trade["orderIds"]
    }
    unreferenced_order_ids = sorted(set(source_orders) - referenced_order_ids)
    rate_per_unit = Decimal("0.005")
    minimum_fee = Decimal("1.00")
    expected_breakdown = []
    for order_id in unreferenced_order_ids:
        quantity = Decimal(str(source_orders[order_id]["quantity"]))
        derived_fee = max(minimum_fee, abs(quantity) * rate_per_unit)
        expected_breakdown.append(
            {
                "order_id": order_id,
                "quantity": format(quantity, "f"),
                "derived_fee": format(derived_fee, "f"),
            }
        )

    fees = golden_reference.raw_metadata["fee_reconciliation"]
    derivation = fees["unreferenced_order_fee_derivation"]
    expected_total = sum(
        (Decimal(item["derived_fee"]) for item in expected_breakdown),
        Decimal("0"),
    )
    observed_remainder = Decimal("2698.00") - Decimal("2616.030")

    assert unreferenced_order_ids == [
        34,
        35,
        36,
        221,
        223,
        226,
        227,
        229,
        241,
        246,
        248,
        249,
        250,
        252,
        254,
        255,
        257,
        259,
        262,
        264,
    ]
    assert derivation["evidence_classification"] == "derived_strong"
    assert derivation["formula"] == "max(1 USD, abs(quantity) * 0.005 USD)"
    assert derivation["rate_per_unit"] == "0.005"
    assert derivation["minimum_fee"] == "1.00"
    assert derivation["currency"] == "USD"
    assert derivation["quantity_source"] == "orders[].quantity"
    assert "brokerage_fee_model_class" not in derivation
    assert derivation["derived_order_fees"] == expected_breakdown
    assert expected_total == Decimal("81.9700")
    assert Decimal(derivation["derived_total"]) == expected_total
    assert Decimal(derivation["portfolio_minus_closed_trade_remainder"]) == (
        observed_remainder
    )
    assert Decimal(derivation["reconciliation_delta"]) == Decimal("0")


def test_fee_derivation_preserves_source_decimal_quantity(tmp_path: Path):
    source_quantity = Decimal("200.1234567890123456789")
    derived_fee = abs(source_quantity) * Decimal("0.005")
    payload = _valid_result()
    payload["orders"]["2"] = _order(2)
    payload["orders"]["2"]["quantity"] = "__SOURCE_QUANTITY__"
    payload["state"]["OrderCount"] = "2"
    payload["statistics"]["Total Orders"] = "2"
    payload["statistics"]["Total Fees"] = (
        f"${Decimal('2.5') + derived_fee}"
    )
    source = tmp_path / "precise-quantity-result.json"
    source.write_text(
        json.dumps(payload).replace(
            '"__SOURCE_QUANTITY__"', format(source_quantity, "f")
        ),
        encoding="utf-8",
    )

    reference = QuantConnectResultImporter().load(source)
    derivation = reference.raw_metadata["fee_reconciliation"][
        "unreferenced_order_fee_derivation"
    ]

    assert derivation["derived_order_fees"] == [
        {
            "order_id": 2,
            "quantity": "200.1234567890123456789",
            "derived_fee": "1.0006172839450617283945",
        }
    ]
    assert derivation["derived_total"] == "1.0006172839450617283945"


def test_real_export_keeps_metric_bases_and_equity_ohlc_separate(golden_reference):
    ref = golden_reference
    raw = ref.raw_metadata
    equity = raw["strategy_equity_evidence"]

    assert ref.stats["metric_basis"] == "portfolio_statistics"
    assert ref.stats["end_equity"] == pytest.approx(1937840.1796)
    assert ref.stats["sharpe_ratio"] == pytest.approx(0.3719)
    assert ref.stats["win_rate"] == pytest.approx(0.7182)
    assert raw["trade_statistics"]["winRate"] == "0.6780"
    assert float(raw["trade_statistics"]["totalFees"]) == pytest.approx(2616.03)
    assert raw["metric_bases"]["canonical_stats"] == "portfolio_statistics"
    assert raw["metric_bases"]["closed_trades"] == "trade_statistics"

    assert equity["point_count"] == 5471
    assert equity["point_schema"] == [
        "unix_time_seconds",
        "open",
        "high",
        "low",
        "close",
    ]
    assert equity["first_point"] == [
        1451624400,
        1000000.0,
        1000000.0,
        1000000.0,
        1000000.0,
    ]
    assert equity["last_point"][-1] == pytest.approx(1937840.1796)
    assert len(equity["points"]) == 5471
    assert "equity_curve" not in raw


def test_real_csv_exports_match_json_as_multisets(golden_reference):
    verification = golden_reference.raw_metadata["csv_verification"]
    assert verification["orders"] == "matched"
    assert verification["trades"] == "matched"
    assert verification["comparison"] == "normalized_multiset"


def test_fill_events_are_explicitly_not_observable(golden_reference):
    limitations = golden_reference.raw_metadata["known_limitations"]
    assert limitations["fill_events_observable"] is False
    assert limitations["fill_event_count"] == "not_observable"
    assert limitations["log_export_limit_bytes"] == 10 * 1024


def test_missing_file_fails_closed():
    importer = QuantConnectResultImporter()

    with pytest.raises(FileNotFoundError, match="QuantConnect result not found"):
        importer.load("/does/not/exist.json")


def test_malformed_json_fails_closed(tmp_path: Path):
    source = tmp_path / "broken.json"
    source.write_text("{not-json", encoding="utf-8")
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match="Invalid QuantConnect result JSON"):
        importer.load(source)


@pytest.mark.parametrize(
    ("state_field", "value", "message"),
    [
        ("Status", "Running", "status must be Completed"),
        ("RuntimeError", "division by zero", "RuntimeError is not empty"),
    ],
)
def test_unsuccessful_result_fails_closed(
    tmp_path: Path,
    state_field: str,
    value: str,
    message: str,
):
    payload = _valid_result()
    payload["state"][state_field] = value
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match=message):
        importer.load(source)


def test_missing_orders_fail_closed(tmp_path: Path):
    payload = _valid_result()
    payload["orders"] = {}
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match="orders must not be empty"):
        importer.load(source)


def test_duplicate_order_id_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["orders"] = {"1": _order(1), "2": _order(1)}
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match="duplicate order id: 1"):
        importer.load(source)


def test_unknown_closed_trade_order_id_fails_closed(tmp_path: Path):
    payload = _valid_result()
    payload["totalPerformance"]["closedTrades"] = [
        _closed_trade(order_ids=[1, 999])
    ]
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(
        ValueError,
        match=r"^closedTrades reference unknown order IDs: \[999\]$",
    ):
        importer.load(source)


def test_empty_closed_trades_fail_closed(tmp_path: Path):
    payload = _valid_result()
    payload["totalPerformance"]["closedTrades"] = []
    source = _write_result(tmp_path, payload)
    importer = QuantConnectResultImporter()

    with pytest.raises(ValueError, match="closedTrades must not be empty"):
        importer.load(source)


@pytest.mark.parametrize("csv_kind", ["orders", "trades"])
def test_csv_mismatch_fails_closed(tmp_path: Path, csv_kind: str):
    source = _write_result(tmp_path, _valid_result())
    if csv_kind == "orders":
        csv_path = tmp_path / "bad_orders.csv"
        fieldnames = ["Time", "Symbol", "Price", "Quantity", "Type", "Status", "Value", "Tag"]
        row = {
            "Time": "2024-01-02T15:00:00Z",
            "Symbol": "QQQ",
            "Price": "100.25",
            "Quantity": "10",
            "Type": "Market On Close",
            "Status": "Filled",
            "Value": "1002.5",
            "Tag": "rebalance",
        }
    else:
        csv_path = tmp_path / "bad_trades.csv"
        fieldnames = [
            "Entry Time",
            "Symbols",
            "Exit Time",
            "Direction",
            "Entry Price",
            "Exit Price",
            "Quantity",
            "P&L",
            "Fees",
            "MAE",
            "MFE",
            "Drawdown",
            "IsWin",
            "Order Ids",
        ]
        row = {
            "Entry Time": "2024-01-02T21:00:00Z",
            "Symbols": "QQQ",
            "Exit Time": "2024-02-01T21:00:00Z",
            "Direction": "Buy",
            "Entry Price": "100.25",
            "Exit Price": "110",
            "Quantity": "10",
            "P&L": "97.5",
            "Fees": "2.5",
            "MAE": "-10",
            "MFE": "120",
            "Drawdown": "22",
            "IsWin": "1",
            "Order Ids": "1",
        }
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)

    kwargs = {f"{csv_kind}_csv": csv_path}
    importer = QuantConnectResultImporter()
    with pytest.raises(ValueError, match=f"{csv_kind} CSV does not match result JSON"):
        importer.load(source, **kwargs)


def test_trades_csv_missing_fees_fails_closed(tmp_path: Path):
    source = _write_result(tmp_path, _valid_result())
    csv_path = tmp_path / "trades.csv"
    fieldnames = [
        "Entry Time",
        "Symbols",
        "Exit Time",
        "Direction",
        "Entry Price",
        "Exit Price",
        "Quantity",
        "P&L",
        "MAE",
        "MFE",
        "Drawdown",
        "IsWin",
        "Order Ids",
    ]
    row = {
        "Entry Time": "2024-01-02T21:00:00Z",
        "Symbols": "SPY",
        "Exit Time": "2024-02-01T21:00:00Z",
        "Direction": "Buy",
        "Entry Price": "100.25",
        "Exit Price": "110",
        "Quantity": "10",
        "P&L": "97.5",
        "MAE": "-10",
        "MFE": "120",
        "Drawdown": "22",
        "IsWin": "1",
        "Order Ids": "1",
    }
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)

    importer = QuantConnectResultImporter()
    with pytest.raises(
        ValueError,
        match="trades CSV missing required columns: fees",
    ):
        importer.load(source, trades_csv=csv_path)


def test_orders_csv_accepts_qc_decimal_truncation_but_rejects_material_change(
    tmp_path: Path,
):
    source = _write_result(tmp_path, _valid_result())
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            '"value": 1002.5', '"value": 103828.272132545', 1
        ),
        encoding="utf-8",
    )
    csv_path = tmp_path / "orders.csv"
    fieldnames = ["Time", "Symbol", "Price", "Quantity", "Type", "Status", "Value", "Tag"]
    row = {
        "Time": "2024-01-02T15:00:00Z",
        "Symbol": "SPY",
        "Price": "100.25",
        "Quantity": "10",
        "Type": "Market On Close",
        "Status": "Filled",
        "Value": "103828.27213254",
        "Tag": "rebalance",
    }
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)

    reference = QuantConnectResultImporter().load(source, orders_csv=csv_path)

    assert reference.raw_metadata["orders"][0]["value"] == pytest.approx(
        103828.272132545
    )
    json.dumps(reference.raw_metadata)

    row["Value"] = "103828.27213253"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)

    importer = QuantConnectResultImporter()
    with pytest.raises(ValueError, match="orders CSV does not match result JSON"):
        importer.load(source, orders_csv=csv_path)
