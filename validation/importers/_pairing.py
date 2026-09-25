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
    groups: dict[str, PositionFills],
    order: list[str],
    position_id: str,
    row: dict[str, Any],
    direction: str,
) -> None:
    if position_id not in groups:
        groups[position_id] = PositionFills(position_id=position_id)
        order.append(position_id)
    if direction == "in":
        groups[position_id].entries.append(row)
    else:
        groups[position_id].exits.append(row)


def _shared_ids(
    rows: Sequence[dict[str, Any]],
    position_id_of: Callable[[dict[str, Any]], str],
    is_trade_row: Callable[[dict[str, Any]], bool],
) -> bool:
    counts: dict[str, int] = {}
    for row in rows:
        if not is_trade_row(row):
            continue
        position_id = position_id_of(row)
        if position_id:
            counts[position_id] = counts.get(position_id, 0) + 1
    return any(count > 1 for count in counts.values())


def group_position_fills(
    rows: Sequence[dict[str, Any]],
    position_id_of: Callable[[dict[str, Any]], str],
    direction_of: Callable[[dict[str, Any]], str],
    is_trade_row: Callable[[dict[str, Any]], bool],
    quantity_of: Callable[[dict[str, Any]], Any] | None = None,
) -> list[PositionFills]:
    """Group fills by stable position id or by the report deal sequence."""
    relevant = [
        row
        for row in rows
        if is_trade_row(row) and direction_of(row) in ("in", "out")
    ]
    use_shared_ids = _shared_ids(relevant, position_id_of, lambda _row: True)
    groups: dict[str, PositionFills] = {}
    order: list[str] = []
    remaining: list[float] = []
    next_id = [0]

    for row in relevant:
        direction = direction_of(row)
        position_id = position_id_of(row)
        if use_shared_ids:
            if not position_id:
                next_id[0] += 1
                position_id = (
                    f"orphan-{next_id[0]}"
                    if direction == "out"
                    else f"unresolved-{next_id[0]}"
                )
            _add_to_group(groups, order, position_id, row, direction)
            continue

        if quantity_of is not None:
            quantity = _quantity(quantity_of(row))
        else:
            quantity = _row_quantity(row, None)
        if direction == "in":
            for index, open_quantity in enumerate(remaining):
                if open_quantity <= _quantity_tolerance(open_quantity):
                    continue
                break
            else:
                index = len(remaining)
                remaining.append(0.0)
                next_id[0] += 1
            remaining[index] += quantity
            position_id = f"position-{index + 1}"
        else:
            position_id = ""
            for index, open_quantity in enumerate(remaining):
                if open_quantity > _quantity_tolerance(open_quantity):
                    remaining[index] = max(0.0, open_quantity - quantity)
                    position_id = f"position-{index + 1}"
                    break
            if not position_id:
                next_id[0] += 1
                position_id = f"orphan-{next_id[0]}"
        _add_to_group(groups, order, position_id, row, direction)

    result = [groups[position_id] for position_id in order]
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
