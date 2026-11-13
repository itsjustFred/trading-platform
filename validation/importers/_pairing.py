"""Shared position-fill pairing and evidence for MT5 report importers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

PositionFillStatus = Literal["closed", "partial", "open", "orphan"]


@dataclass
class PositionFills:
    """All fills of one reconstructed position, in report order."""

    position_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    exits: list[dict[str, Any]] = field(default_factory=list)
    status: PositionFillStatus = "open"
    entry_quantity: float = 0.0
    exit_quantity: float = 0.0
    residual_quantity: float = 0.0
    residual_exit_quantity: float = 0.0


def _quantity(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0.0:
        return 0.0
    return number


def _row_quantity(
    row: dict[str, Any],
    quantity_of: Callable[[dict[str, Any]], Any] | None,
) -> float:
    if quantity_of is not None:
        return _quantity(quantity_of(row))
    for key in ("quantity", "qty", "volume", "Volumen", "volumen"):
        if key in row:
            return _quantity(row[key])
    return 0.0


def _quantity_tolerance(quantity: float) -> float:
    return max(1e-9, abs(quantity) * 1e-9)


def classify_position_fills(
    fills: Sequence[PositionFills],
    quantity_of: Callable[[dict[str, Any]], Any] | None = None,
) -> list[PositionFills]:
    """Classify each position from its entry and exit quantities."""
    for position in fills:
        if quantity_of is None and position.entry_quantity == position.exit_quantity == 0.0:
            position.entry_quantity = sum(_row_quantity(row, None) for row in position.entries)
            position.exit_quantity = sum(_row_quantity(row, None) for row in position.exits)
        elif quantity_of is not None:
            position.entry_quantity = sum(
                _row_quantity(row, quantity_of) for row in position.entries
            )
            position.exit_quantity = sum(
                _row_quantity(row, quantity_of) for row in position.exits
            )

        entry_quantity = position.entry_quantity
        exit_quantity = position.exit_quantity
        tolerance = _quantity_tolerance(max(entry_quantity, exit_quantity))
        position.residual_quantity = max(0.0, entry_quantity - exit_quantity)
        position.residual_exit_quantity = max(0.0, exit_quantity - entry_quantity)

        if not position.entries and position.exits:
            position.status = "orphan"
        elif position.entries and not position.exits:
            position.status = "open"
        elif position.entries and exit_quantity < entry_quantity - tolerance:
            position.status = "partial"
        else:
            position.status = "closed"
    return list(fills)


def _add_to_group(
    groups: dict[object, PositionFills],
    order: list[object],
    group_key: object,
    position_id: str,
    row: dict[str, Any],
    direction: str,
) -> None:
    if group_key not in groups:
        groups[group_key] = PositionFills(position_id=position_id)
        order.append(group_key)
    if direction == "in":
        groups[group_key].entries.append(row)
    else:
        groups[group_key].exits.append(row)


def _pairing_scope(
    row: dict[str, Any],
    scope_of: Callable[[dict[str, Any]], object] | None,
) -> object:
    if scope_of is None:
        return None
    scope = scope_of(row)
    try:
        hash(scope)
    except TypeError as exc:
        raise TypeError("pairing scope must be hashable") from exc
    return scope


def _shared_ids(
    rows: Sequence[dict[str, Any]],
    position_id_of: Callable[[dict[str, Any]], str],
    is_trade_row: Callable[[dict[str, Any]], bool],
    scope_of: Callable[[dict[str, Any]], object] | None,
) -> bool:
    counts: dict[tuple[object, str], int] = {}
    for row in rows:
        if not is_trade_row(row):
            continue
        position_id = position_id_of(row)
        if position_id:
            key = (_pairing_scope(row, scope_of), position_id)
            counts[key] = counts.get(key, 0) + 1
    return any(count > 1 for count in counts.values())


def entry_side_scope(
    row: dict[str, Any],
    symbol_of: Callable[[dict[str, Any]], object],
    deal_type_of: Callable[[dict[str, Any]], object],
    direction_of: Callable[[dict[str, Any]], object],
) -> tuple[str, str]:
    direction = str(direction_of(row) or "").strip().lower()
    deal_type = str(deal_type_of(row) or "").strip().lower()
    if deal_type.startswith("buy"):
        deal_side = "buy"
    elif deal_type.startswith("sell"):
        deal_side = "sell"
    else:
        deal_side = ""
    if direction == "in":
        entry_side = deal_side
    elif deal_side == "sell":
        entry_side = "buy"
    elif deal_side == "buy":
        entry_side = "sell"
    else:
        entry_side = ""
    return str(symbol_of(row) or "").strip().upper(), entry_side


def group_position_fills(
    rows: Sequence[dict[str, Any]],
    position_id_of: Callable[[dict[str, Any]], str],
    direction_of: Callable[[dict[str, Any]], str],
    is_trade_row: Callable[[dict[str, Any]], bool],
    quantity_of: Callable[[dict[str, Any]], Any] | None = None,
    scope_of: Callable[[dict[str, Any]], object] | None = None,
) -> list[PositionFills]:
    """Group fills by stable position id or by the report deal sequence."""
    relevant = [
        row
        for row in rows
        if is_trade_row(row) and direction_of(row) in ("in", "out")
    ]
    use_shared_ids = _shared_ids(relevant, position_id_of, lambda _row: True, scope_of)
    groups: dict[object, PositionFills] = {}
    order: list[object] = []
    remaining_by_scope: dict[object, list[float]] = {}
    position_ids_by_scope: dict[object, list[str]] = {}
    next_generated_id = 0
    next_position_id = 0
    next_orphan_id = 0

    for row in relevant:
        direction = direction_of(row)
        position_id = position_id_of(row)
        scope = _pairing_scope(row, scope_of)
        if use_shared_ids:
            if not position_id:
                next_generated_id += 1
                position_id = (
                    f"orphan-{next_generated_id}"
                    if direction == "out"
                    else f"unresolved-{next_generated_id}"
                )
                group_key: object = ("generated", next_generated_id)
            else:
                group_key = (scope, position_id)
            _add_to_group(groups, order, group_key, position_id, row, direction)
            continue

        if quantity_of is not None:
            quantity = _quantity(quantity_of(row))
        else:
            quantity = _row_quantity(row, None)
        remaining = remaining_by_scope.setdefault(scope, [])
        position_ids = position_ids_by_scope.setdefault(scope, [])
        if direction == "in":
            # `index` is used by the loop's `else` arm ("no slot had capacity")
            # and then again below, so B007's "not used within the loop body"
            # does not realise this loop's body is only a scan for the first
            # reusable slot - see `position_id = position_ids[index]` below.
            for index, open_quantity in enumerate(remaining):  # noqa: B007
                if open_quantity <= _quantity_tolerance(open_quantity):
                    continue
                break
            else:
                index = len(remaining)
                next_position_id += 1
                position_id = f"position-{next_position_id}"
                remaining.append(0.0)
                position_ids.append(position_id)
            position_id = position_ids[index]
            remaining[index] += quantity
        else:
            position_id = ""
            for index, open_quantity in enumerate(remaining):
                if open_quantity > _quantity_tolerance(open_quantity):
                    remaining[index] = max(0.0, open_quantity - quantity)
                    position_id = position_ids[index]
                    break
            if not position_id:
                next_orphan_id += 1
                position_id = f"orphan-{next_orphan_id}"
        _add_to_group(groups, order, position_id, position_id, row, direction)

    result = [groups[group_key] for group_key in order]
    return classify_position_fills(result, quantity_of)


def _is_exact_closed(position: PositionFills) -> bool:
    return position.status == "closed" and position.residual_quantity <= _quantity_tolerance(
        max(position.entry_quantity, position.exit_quantity)
    ) and position.residual_exit_quantity <= _quantity_tolerance(
        max(position.entry_quantity, position.exit_quantity)
    )


def closed_positions(fills: Sequence[PositionFills]) -> list[PositionFills]:
    """Return only fully closed positions without residual exit quantity."""
    return [position for position in classify_position_fills(fills) if _is_exact_closed(position)]


def position_fill_evidence(fills: Sequence[PositionFills]) -> list[dict[str, Any]]:
    """Return serialisable evidence for every classified position."""
    return [
        {
            "position_id": position.position_id,
            "status": position.status,
            "entry_count": len(position.entries),
            "exit_count": len(position.exits),
            "entry_quantity": position.entry_quantity,
            "exit_quantity": position.exit_quantity,
            "residual_quantity": position.residual_quantity,
            "residual_exit_quantity": position.residual_exit_quantity,
        }
        for position in classify_position_fills(fills)
    ]


def _has_residual(item: dict[str, Any], field: str) -> bool:
    scale = max(item["entry_quantity"], item["exit_quantity"])
    return item[field] > _quantity_tolerance(scale)


def position_fill_summary(fills: Sequence[PositionFills]) -> dict[str, Any]:
    """Return counts and detailed residual/orphan evidence for an import."""
    evidence = position_fill_evidence(fills)
    by_status = {
        status: [item for item in evidence if item["status"] == status]
        for status in ("closed", "partial", "open", "orphan")
    }
    residual = [item for item in evidence if _has_residual(item, "residual_quantity")]
    residual_exits = [
        item for item in evidence if _has_residual(item, "residual_exit_quantity")
    ]
    return {
        "closed_position_count": len(by_status["closed"]),
        "partial_position_count": len(by_status["partial"]),
        "open_position_count": len(by_status["open"])
        + len(by_status["partial"]),
        "open_position_count_including_partial": len(by_status["open"])
        + len(by_status["partial"]),
        "open_only_count": len(by_status["open"]),
        "orphan_exit_count": len(by_status["orphan"]),
        "closed_position_ids": [item["position_id"] for item in by_status["closed"]],
        "partial_position_ids": [item["position_id"] for item in by_status["partial"]],
        "open_position_ids": [item["position_id"] for item in by_status["open"]]
        + [item["position_id"] for item in by_status["partial"]],
        "open_only_position_ids": [item["position_id"] for item in by_status["open"]],
        "orphan_position_ids": [item["position_id"] for item in by_status["orphan"]],
        "residual_position_count": len(residual),
        "residual_positions": residual,
        "residual_exit_count": len(residual_exits),
        "residual_exits": residual_exits,
        "orphan_exits": by_status["orphan"],
        "positions": evidence,
    }


def open_position_summary(fills: Sequence[PositionFills]) -> dict[str, Any]:
    """Return legacy open-position keys plus quantity-aware evidence."""
    summary = position_fill_summary(fills)
    return {
        "open_position_count": summary["open_position_count"],
        "open_only_count": summary["open_only_count"],
        "open_position_count_including_partial": summary[
            "open_position_count_including_partial"
        ],
        "open_position_ids": summary["open_position_ids"],
        "open_only_position_ids": summary["open_only_position_ids"],
        "partial_position_count": summary["partial_position_count"],
        "orphan_exit_count": summary["orphan_exit_count"],
        "residual_position_count": summary["residual_position_count"],
        "residual_positions": summary["residual_positions"],
        "residual_exit_count": summary["residual_exit_count"],
        "residual_exits": summary["residual_exits"],
        "orphan_exits": summary["orphan_exits"],
        "positions": summary["positions"],
    }
