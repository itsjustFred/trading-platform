"""Shared trade-position pairing for MT5 report importers.

MT5 Strategy Tester reports describe a *position* as a sequence of fills in
the Trades/Deals table:

* one or more ``in`` rows (entry fills; a position scaled into over several
  orders has several ``in`` rows that share the position id but differ in
  their ``Auftrag`` order id),
* one or more ``out`` rows (partial or full exits).

Both the HTML and the XLSX importer must reconstruct the *same* positions
from the same report (DV-B43).  This module holds the shared pairing rule so
the two importers cannot drift apart:

* Rows are grouped by the position id (``Trade`` column, falling back to
  ``Auftrag`` only when the report has no ``Trade`` column).
* Every grouped position becomes exactly one
  :class:`~shared.models.Trade` with one :class:`~shared.models.TradeExit`
  per ``out`` fill — never dropped, never silently truncated.
* A group with no ``in`` rows (orphan exits, e.g. a report that starts
  mid-position) and a group with no ``out`` rows (still-open position) are
  skipped: they cannot be reconstructed into a closed trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence


@dataclass
class PositionFills:
    """All fills of one reconstructed position, in report order."""

    position_id: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    exits: list[dict[str, Any]] = field(default_factory=list)


def group_position_fills(
    rows: Sequence[dict[str, Any]],
    position_id_of: Callable[[dict[str, Any]], str],
    direction_of: Callable[[dict[str, Any]], str],
    is_trade_row: Callable[[dict[str, Any]], bool],
) -> list[PositionFills]:
    """Group report *rows* into per-position entry/exit fill lists.

    Parameters
    ----------
    rows:
        Report rows in report order (already reduced to trade-relevant rows).
        The order is load-bearing: callers that derive position ids on the fly
        (e.g. the XLSX deal walker) rely on every ``in`` row arriving before
        the ``out`` rows that close it — reshuffling the rows silently
        reassigns exits to the wrong position.
    position_id_of:
        Returns the position id of a row ("" when the row has none).
    direction_of:
        Returns ``"in"``, ``"out"`` or anything else for a row.
    is_trade_row:
        False for non-trade rows (balance, summary) which are skipped.
    """
    groups: dict[str, PositionFills] = {}
    order: list[str] = []
    for row in rows:
        if not is_trade_row(row):
            continue
        direction = direction_of(row)
        if direction not in ("in", "out"):
            continue
        pid = position_id_of(row)
        if not pid:
            continue
        if pid not in groups:
            groups[pid] = PositionFills(position_id=pid)
            order.append(pid)
        if direction == "in":
            groups[pid].entries.append(row)
        else:
            groups[pid].exits.append(row)

    return [groups[pid] for pid in order]


def closed_positions(fills: list[PositionFills]) -> list[PositionFills]:
    """Keep only positions that have at least one entry and one exit fill."""
    return [f for f in fills if f.entries and f.exits]


def open_position_summary(fills: list[PositionFills]) -> dict[str, Any]:
    """Explicit open-position accounting (C4: no silent survivorship bias).

    Positions with entries but no exits are still open at report end — they
    must be counted and identified in the import metadata, never silently
    dropped alongside genuinely closed trades.
    """
    open_fills = [f for f in fills if f.entries and not f.exits]
    return {
        "open_position_count": len(open_fills),
        "open_position_ids": [f.position_id for f in open_fills],
    }
