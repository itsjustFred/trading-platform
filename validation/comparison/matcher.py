"""Trade matcher for pairing reference and actual trades.

The matcher pairs trades from an external reference (e.g. imported from MT5)
with trades produced by the local platform based on temporal proximity,
symbol and direction.  Temporal tolerance is configurable so that small
differences in bar-timestamp resolution do not break the comparison.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.optimize import linear_sum_assignment

from shared.models import Trade
from validation.comparison.tolerances import finite_float, finite_tolerance

# datetime.fromisoformat does not understand "Z" in Python < 3.11
_UTC_SUFFIX_Z = "Z"
_UTC_SUFFIX_OFFSET = "+00:00"


def _normalize_timestamp(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp and return a timezone-aware UTC datetime.

    Strings ending in ``Z`` are treated as UTC.  Naive strings are assumed
    to be UTC.  All return values are converted to UTC so that timestamps
    from different time zones can be compared.
    """
    # datetime.fromisoformat does not understand "Z" in Python < 3.11
    normalized = ts.replace(_UTC_SUFFIX_Z, _UTC_SUFFIX_OFFSET)
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _time_diff_seconds(a: datetime, b: datetime) -> float:
    """Absolute difference between two datetimes in seconds."""
    return abs((a - b).total_seconds())


class MatchKey(BaseModel):
    """Canonical key representing the attributes on which two trades matched.

    All timestamps are stored as normalized ISO-8601 UTC strings so that the
    key is serialisable and human-readable.
    """

    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)

    entry_time: str
    exit_time: str
    symbol: str
    direction: str


class MatchResult(BaseModel):
    """Result of a trade-matching run."""

    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)

    matched: list[tuple[Trade, Trade, MatchKey]] = Field(default_factory=list)
    missing: list[Trade] = Field(default_factory=list)
    extra: list[Trade] = Field(default_factory=list)


def _match_cost(
    ref: dict[str, Any],
    act: dict[str, Any],
    tolerance_seconds: int,
    *,
    time_weight: float = 1.0,
    price_weight: float = 1.0,
    quantity_weight: float = 1.0,
) -> float:
    """Cost of pairing two eligible trades (C4: lower wins ties deterministically).

    Time cost normalizes the entry+exit drift by the tolerance window;
    price/quantity costs are relative differences. All terms are unitless
    so time, price and quantity trade off against each other instead of
    array order deciding ties.
    """
    time_cost = (
        _time_diff_seconds(ref["entry"], act["entry"])
        + _time_diff_seconds(ref["exit"], act["exit"])
    ) / (2.0 * max(tolerance_seconds, 1))
    ref_price = abs(float(ref["trade"].entry_price or 0.0))
    act_price = abs(float(act["trade"].entry_price or 0.0))
    price_base = max(ref_price, act_price, 1e-12)
    price_cost = abs(ref_price - act_price) / price_base
    ref_qty = abs(float(ref["trade"].quantity or 0.0))
    act_qty = abs(float(act["trade"].quantity or 0.0))
    qty_base = max(ref_qty, act_qty, 1e-12)
    qty_cost = abs(ref_qty - act_qty) / qty_base
    return time_weight * time_cost + price_weight * price_cost + quantity_weight * qty_cost


def _canonical_key(data: dict[str, Any]) -> tuple[Any, ...]:
    """Stable sort key that makes the assignment independent of input order.

    It covers every field the cost depends on plus the trade id, so distinct
    trades keep a fixed relative order while exact duplicates (which are
    interchangeable) keep their input order through the stable sort.
    """
    trade = data["trade"]
    price = float(trade.entry_price or 0.0)
    quantity = float(trade.quantity or 0.0)
    # ``nan`` never compares, which would leave the order undefined; clamping
    # keeps the canonical order total.
    return (
        data["entry"],
        data["exit"],
        trade.symbol,
        trade.direction,
        price if math.isfinite(price) else math.inf,
        quantity if math.isfinite(quantity) else math.inf,
        trade.id,
    )


def _solve_dense(
    ref_count: int,
    act_count: int,
    feasible: list[tuple[int, int, float]],
) -> dict[int, int]:
    """Solve one connected component with ``linear_sum_assignment``.

    Infeasible cells get a big-M cost above the total of all feasible costs,
    so the solver minimises the number of infeasible assignments first and
    the feasible cost second (#131). Pairs the solver fills with the sentinel
    are dropped afterwards, and ``inf`` is never handed to the solver.
    """
    costs = [cost for _ref_pos, _act_pos, cost in feasible]
    # Twice the total plus one stays strictly above any all-feasible total
    # even when that total is large enough for "+ 1" to round away.
    big_m = 2.0 * sum(costs) + 1.0
    if not math.isfinite(big_m):
        # Only reachable with astronomically large weights: rescale by a
        # common factor, which preserves the ordering the solve depends on.
        scale = max(costs)
        costs = [cost / scale for cost in costs]
        big_m = float(len(costs) + 1)

    matrix = np.full((ref_count, act_count), big_m, dtype=float)
    for (ref_pos, act_pos, _cost), cost in zip(feasible, costs):
        matrix[ref_pos, act_pos] = cost

    rows, cols = linear_sum_assignment(matrix)
    allowed = {(ref_pos, act_pos) for ref_pos, act_pos, _cost in feasible}
    assigned: dict[int, int] = {}
    for row, col in zip(rows, cols):
        ref_pos, act_pos = int(row), int(col)
        # The solver had to fill the unmatched rows/columns with the sentinel.
        if (ref_pos, act_pos) in allowed:
            assigned[ref_pos] = act_pos
    return assigned


def _solve_assignment(
    ref_count: int,
    act_count: int,
    feasible: list[tuple[int, int, float]],
) -> dict[int, int]:
    """Return a maximum-cardinality, minimum-cost ``ref_pos -> act_pos`` map.

    ``feasible`` holds ``(ref_pos, act_pos, cost)`` for every pair the matcher
    may pair. The bipartite graph is split into connected components first
    and each component is solved on its own dense matrix. That is exact (no
    pair crosses components, so the optimum is the union of the per-component
    optima) and keeps memory bounded by the largest component instead of
    ``ref_count * act_count``, which would be gigabytes for long trade lists.
    Components are processed in canonical position order, so the result is
    deterministic.
    """
    if ref_count == 0 or act_count == 0 or not feasible:
        return {}

    # Union-find over refs (0..ref_count-1) and acts (ref_count..).
    parent = list(range(ref_count + act_count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for ref_pos, act_pos, _cost in feasible:
        root_a, root_b = find(ref_pos), find(ref_count + act_pos)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)

    components: dict[int, list[tuple[int, int, float]]] = {}
    for pair in feasible:
        components.setdefault(find(pair[0]), []).append(pair)

    assigned: dict[int, int] = {}
    for root in sorted(components):
        pairs = components[root]
        # Local, order-preserving indices inside the component.
        refs = sorted({ref_pos for ref_pos, _act_pos, _cost in pairs})
        acts = sorted({act_pos for _ref_pos, act_pos, _cost in pairs})
        ref_local = {pos: i for i, pos in enumerate(refs)}
        act_local = {pos: i for i, pos in enumerate(acts)}
        local = [(ref_local[r], act_local[a], cost) for r, a, cost in pairs]
        for r_loc, a_loc in _solve_dense(len(refs), len(acts), local).items():
            assigned[refs[r_loc]] = acts[a_loc]
    return assigned


class TradeMatcher:
    """Pair reference trades with actual trades.

    Matching is a global minimum-cost assignment (#131): all feasible pairs
    are solved at once with ``scipy.optimize.linear_sum_assignment``, so an
    earlier reference can no longer take the only feasible partner of a later
    one.  The solve maximises the number of matched pairs first and minimises
    the blended cost second.  Feasibility is unchanged: same direction, same
    symbol when ``match_symbol`` is set, and entry *and* exit within
    ``time_tolerance_seconds``.

    Both lists are canonicalised before the solve, so the result does not
    depend on their order; exact cost ties resolve to the first actual in that
    canonical order (times, symbol, direction, price, quantity, id), which
    keeps the old deterministic tie-breaking.
    """

    @classmethod
    def match(
        cls,
        reference_trades: list[Trade],
        actual_trades: list[Trade],
        **options: Any,
    ) -> MatchResult:
        """Match two trade lists.

        Parameters
        ----------
        reference_trades:
            Trades from the external reference (ground truth).
        actual_trades:
            Trades produced by the local platform.
        **options:
            ``time_tolerance_seconds`` (int, default 0):
                Maximum allowed difference for *both* entry and exit time.
            ``match_symbol`` (bool, default True):
                Whether the symbol field must match exactly.
            ``time_weight``/``price_weight``/``quantity_weight`` (float):
                Cost-term weights for tie-breaking (default 1.0 each).

        Returns
        -------
        MatchResult
            Container with ``matched``, ``missing`` and ``extra`` trade lists.
        """
        tolerance_value = finite_tolerance(
            options.get("time_tolerance_seconds", 0), "time_tolerance_seconds"
        )
        tolerance_seconds = int(tolerance_value or 0)
        match_symbol: bool = bool(options.get("match_symbol", True))
        time_weight = finite_float(options.get("time_weight", 1.0), "time_weight")
        price_weight = finite_float(options.get("price_weight", 1.0), "price_weight")
        quantity_weight = finite_float(
            options.get("quantity_weight", 1.0), "quantity_weight"
        )
        if time_weight < 0.0 or price_weight < 0.0 or quantity_weight < 0.0:
            raise ValueError("match weights must be >= 0")

        # Pre-compute normalized timestamps so that we do not re-parse inside
        # the inner loop.
        ref_data = [
            {
                "trade": t,
                "entry": _normalize_timestamp(t.entry_time),
                "exit": _normalize_timestamp(t.last_exit_time),
            }
            for t in reference_trades
        ]
        act_data = [
            {
                "trade": t,
                "entry": _normalize_timestamp(t.entry_time),
                "exit": _normalize_timestamp(t.last_exit_time),
            }
            for t in actual_trades
        ]

        # #131: solve the whole matching problem at once instead of walking the
        # references greedily.  Both sides are sorted by trade content first so
        # the assignment cannot depend on input order; the original positions
        # are kept to report the result in input order.
        ref_order = sorted(range(len(ref_data)), key=lambda i: _canonical_key(ref_data[i]))
        act_order = sorted(range(len(act_data)), key=lambda i: _canonical_key(act_data[i]))

        feasible: list[tuple[int, int, float]] = []
        for ref_pos, ref_idx in enumerate(ref_order):
            ref = ref_data[ref_idx]
            for act_pos, act_idx in enumerate(act_order):
                act = act_data[act_idx]

                # direction is always required
                if ref["trade"].direction != act["trade"].direction:
                    continue

                if match_symbol and ref["trade"].symbol != act["trade"].symbol:
                    continue

                if _time_diff_seconds(ref["entry"], act["entry"]) > tolerance_seconds:
                    continue
                if _time_diff_seconds(ref["exit"], act["exit"]) > tolerance_seconds:
                    continue

                cost = _match_cost(
                    ref,
                    act,
                    tolerance_seconds,
                    time_weight=time_weight,
                    price_weight=price_weight,
                    quantity_weight=quantity_weight,
                )
                # A non-finite cost can only come from astronomically large
                # weights. Such a pair was never selected before either, and
                # ``inf`` must not reach the solver.
                if math.isfinite(cost):
                    feasible.append((ref_pos, act_pos, cost))

        assigned = _solve_assignment(len(ref_data), len(act_data), feasible)
        # Map back from canonical positions to input indices.
        match_by_ref: dict[int, int] = {
            ref_order[ref_pos]: act_order[act_pos] for ref_pos, act_pos in assigned.items()
        }

        matched: list[tuple[Trade, Trade, MatchKey]] = []
        missing: list[Trade] = []
        for ref_idx, ref in enumerate(ref_data):
            act_idx = match_by_ref.get(ref_idx)
            if act_idx is None:
                missing.append(ref["trade"])
                continue
            key = MatchKey(
                entry_time=ref["entry"].isoformat().replace(_UTC_SUFFIX_OFFSET, _UTC_SUFFIX_Z),
                exit_time=ref["exit"].isoformat().replace(_UTC_SUFFIX_OFFSET, _UTC_SUFFIX_Z),
                symbol=ref["trade"].symbol,
                direction=ref["trade"].direction,
            )
            matched.append((ref["trade"], act_data[act_idx]["trade"], key))

        used_acts = set(match_by_ref.values())
        extra = [act["trade"] for idx, act in enumerate(act_data) if idx not in used_acts]

        return MatchResult(matched=matched, missing=missing, extra=extra)
