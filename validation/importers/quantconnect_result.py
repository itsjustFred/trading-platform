"""Importer for QuantConnect/LEAN result JSON exports."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import Any, Mapping

from shared.models import Trade, TradeExit
from validation.importers.base import BaseImporter
from validation.reference import ValidationReference

_ORDER_TYPE_LABELS = {
    0: "Market",
    1: "Limit",
    2: "Stop Market",
    3: "Stop Limit",
    4: "Market On Open",
    5: "Market On Close",
    6: "Option Exercise",
    7: "Limit If Touched",
    8: "Combination Market",
    9: "Combination Limit",
    10: "Combination Leg Limit",
    11: "Trailing Stop",
}

_ORDER_STATUS_LABELS = {
    0: "New",
    1: "Submitted",
    2: "Partially Filled",
    3: "Filled",
    4: "Canceled",
    5: "None",
    6: "Invalid",
    7: "Cancel Pending",
    8: "Update Submitted",
}

_ORDER_DIRECTIONS = {-1: "Hold", 0: "Buy", 1: "Sell"}
_TRADE_DIRECTIONS = {0: "LONG", 1: "SHORT"}
_UTC_OFFSET = "+00:00"
_CURRENCY_PREFIXES = frozenset({"$", "€", "£"})
_CSV_NUMERIC_FIELDS = frozenset(
    {
        "id",
        "price",
        "quantity",
        "value",
        "entry_price",
        "exit_price",
        "pnl",
        "fees",
        "mae",
        "mfe",
        "drawdown",
    }
)
_CSV_TIME_FIELDS = frozenset({"time", "entry_time", "exit_time"})


def _required_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _required_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"{name} must be an integer")
    return int(number)


def _float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _symbol_value(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("value") or value.get("Value") or "")
    return str(value or "")


def _symbol_details(value: Any) -> tuple[str, str, str]:
    if not isinstance(value, Mapping):
        return str(value or ""), "", ""
    return (
        str(value.get("value") or value.get("Value") or ""),
        str(value.get("id") or value.get("ID") or ""),
        str(value.get("permtick") or value.get("Permtick") or ""),
    )


def _snake_case(name: str) -> str:
    result: list[str] = []
    for index, character in enumerate(name):
        if character == " ":
            result.append("_")
            continue
        if character.isupper():
            previous = name[index - 1] if index else ""
            following = name[index + 1] if index + 1 < len(name) else ""
            starts_word = previous.islower() or previous.isdigit()
            ends_acronym = previous.isupper() and following.islower()
            if result and result[-1] != "_" and (starts_word or ends_acronym):
                result.append("_")
            result.append(character.lower())
            continue
        result.append(character)
    return "".join(result)


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    text = str(value).strip().replace(",", "")
    if text[:1] in _CURRENCY_PREFIXES:
        text = text[1:].strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not number.is_finite():
        raise ValueError(f"{name} must be finite")
    return number


def _stat_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if not isinstance(value, str):
        return deepcopy(value)
    text = value.strip()
    if not text:
        return ""
    is_percent = text.endswith("%")
    try:
        number = _decimal(text[:-1] if is_percent else text, "statistic")
    except ValueError:
        return text
    if is_percent:
        number /= Decimal("100")
    return float(number)


def _sorted_copy(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _sorted_copy(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list):
        return [_sorted_copy(item) for item in value]
    return deepcopy(value)


def _canonical_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text.replace(" UTC", _UTC_OFFSET)
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + _UTC_OFFSET
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y %H:%M:%S",
            "%m/%d/%Y %I:%M:%S %p",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    rendered = parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")
    return rendered.replace(_UTC_OFFSET, "Z")


def _header_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _normal_string(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normal_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _normal_ids(value: Any) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(sorted(_int(item, "order id") for item in value))
    text = str(value or "").strip()
    if text.startswith(r"\t"):
        text = text[2:]
    tokens = text.replace(",", " ").split()
    return tuple(sorted(_int(item, "order id") for item in tokens))


def _decimal_export_matches(json_value: Any, csv_value: Any) -> bool:
    try:
        expected = _decimal(json_value, "JSON value")
        exported = _decimal(csv_value, "CSV value")
    except ValueError:
        return False
    exponent = exported.as_tuple().exponent
    quantum = Decimal(1).scaleb(exponent)
    return exported in {
        expected.quantize(quantum, rounding=ROUND_HALF_UP),
        expected.quantize(quantum, rounding=ROUND_DOWN),
    }


class QuantConnectResultImporter(BaseImporter):
    """Load a completed QuantConnect result export without inventing evidence."""

    def load(
        self,
        source: str | Path,
        **kwargs: Any,
    ) -> ValidationReference:
        source_path = Path(source).expanduser()
        if not source_path.is_file():
            raise FileNotFoundError(f"QuantConnect result not found: {source_path}")

        try:
            source_bytes = source_path.read_bytes()
            payload = json.loads(source_bytes, parse_float=Decimal)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"Invalid QuantConnect result JSON: {source_path}") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("QuantConnect result JSON root must be an object")

        state = _required_mapping(payload.get("state"), "state")
        if state.get("Status") != "Completed":
            raise ValueError("QuantConnect result status must be Completed")
        if str(state.get("RuntimeError") or "").strip():
            raise ValueError("QuantConnect result RuntimeError is not empty")

        orders_mapping = _required_mapping(payload.get("orders"), "orders")
        if not orders_mapping:
            raise ValueError("orders must not be empty")
        orders, order_comparison_rows, source_order_quantities = (
            self._normalize_orders(orders_mapping)
        )

        total_performance = _required_mapping(
            payload.get("totalPerformance"), "totalPerformance"
        )
        closed_trades = _required_list(
            total_performance.get("closedTrades"), "closedTrades"
        )
        if not closed_trades:
            raise ValueError("closedTrades must not be empty")
        normalized_trades, comparison_trades = self._normalize_trades(closed_trades)

        portfolio_statistics = _required_mapping(
            total_performance.get("portfolioStatistics"), "portfolioStatistics"
        )
        trade_statistics = _required_mapping(
            total_performance.get("tradeStatistics"), "tradeStatistics"
        )
        statistics = _required_mapping(payload.get("statistics"), "statistics")
        configuration = _required_mapping(
            payload.get("algorithmConfiguration", {}), "algorithmConfiguration"
        )

        clusters, cluster_summary = self._submission_clusters(orders)
        equity_evidence = self._strategy_equity_evidence(payload.get("charts"))
        fee_reconciliation = self._fee_reconciliation(
            statistics, normalized_trades, orders, source_order_quantities
        )

        csv_verification = {
            "comparison": "normalized_multiset",
            "orders": "not_provided",
            "trades": "not_provided",
        }
        orders_csv = kwargs.get("orders_csv")
        if orders_csv is not None:
            self._verify_csv(
                Path(orders_csv),
                order_comparison_rows,
                "orders",
            )
            csv_verification["orders"] = "matched"
        trades_csv = kwargs.get("trades_csv")
        if trades_csv is not None:
            self._verify_csv(Path(trades_csv), comparison_trades, "trades")
            csv_verification["trades"] = "matched"

        canonical_stats = {
            _snake_case(str(key)): _stat_value(value)
            for key, value in sorted(portfolio_statistics.items(), key=lambda item: str(item[0]))
        }
        canonical_stats["metric_basis"] = "portfolio_statistics"

        resolved_source = str(source_path.resolve())
        symbols = sorted({trade.symbol for trade in normalized_trades})
        strategy_name = str(
            state.get("Name")
            or configuration.get("name")
            or source_path.stem
        )
        settings_value = configuration.get("parameters", {})
        settings = dict(_required_mapping(settings_value, "algorithmConfiguration.parameters"))

        raw_metadata = {
            "source_framework": "quantconnect",
            "source_path": resolved_source,
            "source_timezone": "UTC",
            "schema_version": "quantconnect-result-v1",
            "orders": orders,
            "rebalance_clusters": clusters,
            "rebalance_cluster_summary": cluster_summary,
            "portfolio_statistics": _sorted_copy(portfolio_statistics),
            "trade_statistics": _sorted_copy(trade_statistics),
            "metric_bases": {
                "canonical_stats": "portfolio_statistics",
                "closed_trades": "trade_statistics",
            },
            "strategy_equity_evidence": equity_evidence,
            "csv_verification": csv_verification,
            "fee_reconciliation": fee_reconciliation,
            "known_limitations": {
                "fill_events_observable": False,
                "fill_event_count": "not_observable",
                "log_export_limit_bytes": 10 * 1024,
            },
        }

        return ValidationReference(
            strategy_name=strategy_name,
            source_framework="lean",
            symbol=",".join(symbols),
            timeframe="unknown",
            settings=_sorted_copy(settings),
            trades=normalized_trades,
            stats=canonical_stats,
            raw_metadata=raw_metadata,
            source_path=resolved_source,
            source_hash=hashlib.sha256(source_bytes).hexdigest(),
            allow_empty=False,
        )

    def _normalize_orders(
        self, raw_orders: Mapping[str, Any]
    ) -> tuple[
        list[dict[str, Any]], list[dict[str, Any]], dict[int, Decimal]
    ]:
        normalized: list[dict[str, Any]] = []
        comparison_rows: list[dict[str, Any]] = []
        source_quantities: dict[int, Decimal] = {}
        seen_ids: set[int] = set()
        for _, raw_value in sorted(raw_orders.items(), key=lambda item: str(item[0])):
            raw = _required_mapping(raw_value, "order")
            order_id = _int(raw.get("id"), "order.id")
            if order_id in seen_ids:
                raise ValueError(f"duplicate order id: {order_id}")
            seen_ids.add(order_id)
            type_code = _int(raw.get("type"), f"order {order_id} type")
            status_code = _int(raw.get("status"), f"order {order_id} status")
            direction_code = _int(raw.get("direction", -1), f"order {order_id} direction")
            symbol, symbol_id, permanent_ticker = _symbol_details(raw.get("symbol"))
            broker_ids_value = raw.get("brokerId", [])
            if isinstance(broker_ids_value, list):
                broker_ids = sorted(str(value) for value in broker_ids_value)
            elif broker_ids_value in (None, ""):
                broker_ids = []
            else:
                broker_ids = [str(broker_ids_value)]
            price = _decimal(raw.get("price", 0), "order.price")
            quantity = _decimal(raw.get("quantity", 0), "order.quantity")
            source_quantities[order_id] = quantity
            value = _decimal(raw.get("value", 0), "order.value")
            order = {
                "id": order_id,
                "type": type_code,
                "type_label": _ORDER_TYPE_LABELS.get(type_code, f"Unknown ({type_code})"),
                "status": status_code,
                "status_label": _ORDER_STATUS_LABELS.get(
                    status_code, f"Unknown ({status_code})"
                ),
                "direction": direction_code,
                "direction_label": _ORDER_DIRECTIONS.get(
                    direction_code, f"Unknown ({direction_code})"
                ),
                "contingent_id": _int(raw.get("contingentId", 0), "order.contingentId"),
                "broker_ids": broker_ids,
                "symbol": symbol,
                "symbol_id": symbol_id,
                "permanent_ticker": permanent_ticker,
                "price": float(price),
                "currency": str(raw.get("priceCurrency") or ""),
                "time": _canonical_time(raw.get("time")),
                "created_time": _canonical_time(raw.get("createdTime")),
                "fill_time": _canonical_time(raw.get("lastFillTime")),
                "quantity": float(quantity),
                "tag": str(raw.get("tag") or ""),
                "properties": _sorted_copy(raw.get("properties", {})),
                "security_type": _int(raw.get("securityType", 0), "order.securityType"),
                "value": float(value),
                "order_submission_data": _sorted_copy(raw.get("orderSubmissionData")),
                "is_marketable": bool(raw.get("isMarketable", False)),
                "price_adjustment_mode": _int(
                    raw.get("priceAdjustmentMode", 0), "order.priceAdjustmentMode"
                ),
            }
            normalized.append(order)
            comparison_rows.append(
                {
                    "id": order_id,
                    "time": order["time"],
                    "symbol": symbol,
                    "price": price,
                    "quantity": quantity,
                    "type": order["type_label"],
                    "status": order["status_label"],
                    "value": value,
                    "tag": order["tag"],
                    "direction": order["direction_label"],
                }
            )
        normalized.sort(key=lambda order: order["id"])
        comparison_rows.sort(key=lambda order: order["id"])
        return normalized, comparison_rows, source_quantities

    def _normalize_trades(
        self, raw_trades: list[Any]
    ) -> tuple[list[Trade], list[dict[str, Any]]]:
        prepared: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []
        for value in raw_trades:
            raw = _required_mapping(value, "closedTrade")
            symbols = _required_list(raw.get("symbols"), "closedTrade.symbols")
            if not symbols:
                raise ValueError("closedTrade.symbols must not be empty")
            symbol_names = tuple(sorted(_symbol_value(symbol) for symbol in symbols))
            if any(not symbol for symbol in symbol_names):
                raise ValueError("closedTrade.symbols must not contain empty symbols")
            key = (
                _canonical_time(raw.get("entryTime")),
                _canonical_time(raw.get("exitTime")),
                ",".join(symbol_names),
                str(raw.get("id") or ""),
            )
            prepared.append((key, raw))

        trades: list[Trade] = []
        comparison_rows: list[dict[str, Any]] = []
        seen_qc_ids: set[str] = set()
        for index, (_, raw) in enumerate(sorted(prepared, key=lambda item: item[0]), start=1):
            symbols = sorted(
                _symbol_value(symbol)
                for symbol in _required_list(raw.get("symbols"), "closedTrade.symbols")
            )
            symbol = ",".join(symbols)
            direction_code = _int(raw.get("direction"), "closedTrade.direction")
            direction = _TRADE_DIRECTIONS.get(direction_code)
            if direction is None:
                raise ValueError(f"unsupported closedTrade direction: {direction_code}")
            entry_time = _canonical_time(raw.get("entryTime"))
            exit_time = _canonical_time(raw.get("exitTime"))
            entry_price = _float(raw.get("entryPrice"), "closedTrade.entryPrice")
            exit_price = _float(raw.get("exitPrice"), "closedTrade.exitPrice")
            quantity = _float(raw.get("quantity"), "closedTrade.quantity")
            pnl = _float(raw.get("profitLoss"), "closedTrade.profitLoss")
            fees = _float(raw.get("totalFees"), "closedTrade.totalFees")
            mae = _float(raw.get("mae"), "closedTrade.mae")
            mfe = _float(raw.get("mfe"), "closedTrade.mfe")
            order_ids = list(_normal_ids(raw.get("orderIds", [])))
            if not order_ids:
                raise ValueError("closedTrade.orderIds must not be empty")
            if len(order_ids) != len(set(order_ids)):
                raise ValueError("closedTrade.orderIds must not contain duplicates")
            drawdown = _float(
                raw.get("endTradeDrawdown", 0), "closedTrade.endTradeDrawdown"
            )
            source_qc_id = str(raw.get("id") or "")
            qc_id = source_qc_id.strip()
            if not qc_id:
                raise ValueError("closedTrade.id must not be empty")
            if qc_id in seen_qc_ids:
                raise ValueError(f"duplicate closedTrade.id: {qc_id}")
            seen_qc_ids.add(qc_id)
            duration = str(raw.get("duration") or "")
            is_win = _normal_bool(raw.get("isWin"))
            trade = Trade(
                id=index,
                symbol=symbol,
                direction=direction,
                entry_time=entry_time,
                entry_price=entry_price,
                quantity=quantity,
                exits=[
                    TradeExit(
                        time=exit_time,
                        price=exit_price,
                        quantity=quantity,
                        pnl=pnl,
                        fees=fees,
                    )
                ],
                pnl=pnl,
                fees=fees,
                net_pnl=pnl - fees,
                mae=mae,
                mfe=mfe,
                meta={
                    "qc_closed_trade_id": source_qc_id,
                    "order_ids": order_ids,
                    "duration": duration,
                    "end_trade_drawdown": drawdown,
                    "is_win": is_win,
                },
            )
            trades.append(trade)
            comparison_rows.append(
                {
                    "id": source_qc_id,
                    "entry_time": entry_time,
                    "symbols": symbol,
                    "exit_time": exit_time,
                    "direction": direction,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "quantity": quantity,
                    "pnl": pnl,
                    "fees": fees,
                    "mae": mae,
                    "mfe": mfe,
                    "drawdown": drawdown,
                    "duration": duration,
                    "is_win": is_win,
                    "order_ids": tuple(order_ids),
                }
            )
        return trades, comparison_rows

    def _submission_clusters(
        self, orders: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        grouped: dict[str, list[int]] = defaultdict(list)
        for order in orders:
            grouped[order["time"]].append(order["id"])
        clusters = [
            {
                "submission_time": submission_time,
                "order_ids": sorted(order_ids),
                "order_count": len(order_ids),
            }
            for submission_time, order_ids in sorted(grouped.items())
        ]
        sizes = [cluster["order_count"] for cluster in clusters]
        distribution = Counter(sizes)
        summary = {
            "count": len(clusters),
            "minimum_size": min(sizes),
            "maximum_size": max(sizes),
            "size_distribution": dict(sorted(distribution.items())),
        }
        return clusters, summary

    def _strategy_equity_evidence(self, charts_value: Any) -> dict[str, Any]:
        charts = _required_mapping(charts_value, "charts")
        chart = _required_mapping(charts.get("Strategy Equity"), "charts.Strategy Equity")
        series = _required_mapping(chart.get("series"), "charts.Strategy Equity.series")
        equity = _required_mapping(series.get("Equity"), "Strategy Equity Equity series")
        raw_points = _required_list(equity.get("values"), "Strategy Equity values")
        points: list[list[Any]] = []
        for raw_point in raw_points:
            if not isinstance(raw_point, list) or len(raw_point) != 5:
                raise ValueError("Strategy Equity points must contain timestamp and OHLC")
            points.append(
                [
                    _int(raw_point[0], "equity timestamp"),
                    _float(raw_point[1], "equity open"),
                    _float(raw_point[2], "equity high"),
                    _float(raw_point[3], "equity low"),
                    _float(raw_point[4], "equity close"),
                ]
            )
        points.sort(key=lambda point: tuple(point))
        return {
            "point_count": len(points),
            "point_schema": ["unix_time_seconds", "open", "high", "low", "close"],
            "first_point": deepcopy(points[0]) if points else None,
            "last_point": deepcopy(points[-1]) if points else None,
            "points": points,
        }

    def _fee_reconciliation(
        self,
        statistics: Mapping[str, Any],
        trades: list[Trade],
        orders: list[dict[str, Any]],
        source_order_quantities: Mapping[int, Decimal],
    ) -> dict[str, Any]:
        if "Total Fees" not in statistics:
            raise ValueError("statistics.Total Fees is required for fee reconciliation")
        portfolio_total = _decimal(statistics["Total Fees"], "statistics.Total Fees")
        closed_total = sum(
            (_decimal(trade.fees or 0, "closed trade fees") for trade in trades),
            Decimal("0"),
        )
        all_order_ids = {order["id"] for order in orders}
        referenced_order_ids = {
            order_id
            for trade in trades
            for order_id in trade.meta.get("order_ids", [])
        }
        unknown_order_ids = sorted(referenced_order_ids.difference(all_order_ids))
        if unknown_order_ids:
            raise ValueError(
                f"closedTrades reference unknown order IDs: {unknown_order_ids}"
            )
        referenced_ids = sorted(referenced_order_ids)
        unreferenced_ids = sorted(all_order_ids.difference(referenced_ids))
        rate_per_unit = Decimal("0.005")
        minimum_fee = Decimal("1.00")
        derived_order_fees: list[dict[str, Any]] = []
        derived_total = Decimal("0")
        for order_id in unreferenced_ids:
            quantity = source_order_quantities[order_id]
            derived_fee = max(minimum_fee, abs(quantity) * rate_per_unit)
            derived_total += derived_fee
            derived_order_fees.append(
                {
                    "order_id": order_id,
                    "quantity": format(quantity, "f"),
                    "derived_fee": format(derived_fee, "f"),
                }
            )
        fee_remainder = portfolio_total - closed_total
        reconciliation_delta = derived_total - fee_remainder
        if reconciliation_delta != Decimal("0"):
            raise ValueError(
                "derived unreferenced-order fees do not reconcile with "
                "portfolio total minus closed-trade total: "
                f"derived={format(derived_total, 'f')}, "
                f"remainder={format(fee_remainder, 'f')}"
            )
        return {
            "portfolio_total_fees": float(portfolio_total),
            "closed_trade_total_fees": float(closed_total),
            "fee_remainder": float(fee_remainder),
            "referenced_order_count": len(referenced_ids),
            "referenced_order_ids": referenced_ids,
            "unreferenced_order_count": len(unreferenced_ids),
            "unreferenced_order_ids": unreferenced_ids,
            "unreferenced_order_fee_derivation": {
                "evidence_classification": "derived_strong",
                "formula": "max(1 USD, abs(quantity) * 0.005 USD)",
                "rate_per_unit": format(rate_per_unit, "f"),
                "minimum_fee": format(minimum_fee, "f"),
                "currency": "USD",
                "quantity_source": "orders[].quantity",
                "derived_order_fees": derived_order_fees,
                "derived_total": format(derived_total, "f"),
                "portfolio_minus_closed_trade_remainder": format(
                    fee_remainder, "f"
                ),
                "reconciliation_delta": format(reconciliation_delta, "f"),
            },
        }

    def _verify_csv(
        self,
        path: Path,
        json_rows: list[dict[str, Any]],
        kind: str,
    ) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"QuantConnect {kind} CSV not found: {path}")
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError(f"{kind} CSV does not match result JSON")
            csv_rows = [dict(row) for row in reader]
        if len(csv_rows) != len(json_rows):
            raise ValueError(f"{kind} CSV does not match result JSON")

        aliases = self._csv_aliases(kind)
        mapped_headers = {
            header: aliases.get(_header_key(header)) for header in reader.fieldnames
        }
        comparable_headers = {
            header: field for header, field in mapped_headers.items() if field is not None
        }
        if not comparable_headers:
            raise ValueError(f"{kind} CSV does not match result JSON")
        required_fields = {
            "orders": {
                "time",
                "symbol",
                "price",
                "quantity",
                "type",
                "status",
                "value",
                "tag",
            },
            "trades": {
                "entry_time",
                "symbols",
                "exit_time",
                "direction",
                "entry_price",
                "exit_price",
                "quantity",
                "pnl",
                "fees",
                "mae",
                "mfe",
                "drawdown",
                "is_win",
                "order_ids",
            },
        }[kind]
        missing_fields = sorted(
            required_fields - set(comparable_headers.values())
        )
        if missing_fields:
            raise ValueError(
                f"{kind} CSV missing required columns: {', '.join(missing_fields)}"
            )

        remaining = list(json_rows)
        for csv_row in csv_rows:
            match_index = next(
                (
                    index
                    for index, json_row in enumerate(remaining)
                    if self._csv_row_matches(csv_row, json_row, comparable_headers)
                ),
                None,
            )
            if match_index is None:
                raise ValueError(f"{kind} CSV does not match result JSON")
            remaining.pop(match_index)
        if remaining:
            raise ValueError(f"{kind} CSV does not match result JSON")

    def _csv_aliases(self, kind: str) -> dict[str, str]:
        common = {
            "id": "id",
            "time": "time",
            "symbol": "symbol",
            "symbols": "symbols",
            "price": "price",
            "quantity": "quantity",
            "direction": "direction",
        }
        if kind == "orders":
            common.update(
                {
                    "type": "type",
                    "status": "status",
                    "value": "value",
                    "tag": "tag",
                }
            )
        else:
            common.update(
                {
                    "entrytime": "entry_time",
                    "exittime": "exit_time",
                    "entryprice": "entry_price",
                    "exitprice": "exit_price",
                    "pl": "pnl",
                    "profitloss": "pnl",
                    "fees": "fees",
                    "totalfees": "fees",
                    "mae": "mae",
                    "mfe": "mfe",
                    "drawdown": "drawdown",
                    "endtradedrawdown": "drawdown",
                    "duration": "duration",
                    "iswin": "is_win",
                    "orderids": "order_ids",
                }
            )
        return common

    def _csv_row_matches(
        self,
        csv_row: Mapping[str, Any],
        json_row: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> bool:
        for header, field in headers.items():
            csv_value = csv_row.get(header, "")
            json_value = json_row.get(field, "")
            if not self._csv_field_matches(field, csv_value, json_value):
                return False
        return True

    def _csv_field_matches(self, field: str, csv_value: Any, json_value: Any) -> bool:
        if field in _CSV_NUMERIC_FIELDS:
            return _decimal_export_matches(json_value, csv_value)
        if field in _CSV_TIME_FIELDS:
            return _canonical_time(csv_value) == _canonical_time(json_value)
        if field == "order_ids":
            try:
                return _normal_ids(csv_value) == _normal_ids(json_value)
            except ValueError:
                return False
        if field == "is_win":
            return _normal_bool(csv_value) == _normal_bool(json_value)
        if field == "direction":
            return self._csv_directions_match(csv_value, json_value)
        return _normal_string(csv_value) == _normal_string(json_value)

    @staticmethod
    def _csv_directions_match(csv_value: Any, json_value: Any) -> bool:
        equivalents = {"BUY": "LONG", "SELL": "SHORT"}
        csv_direction = _normal_string(csv_value).upper()
        json_direction = _normal_string(json_value).upper()
        return equivalents.get(csv_direction, csv_direction) == equivalents.get(
            json_direction, json_direction
        )
