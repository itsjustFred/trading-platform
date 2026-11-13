"""Trade matcher for pairing reference and actual trades.

The matcher pairs trades from an external reference (e.g. imported from MT5)
with trades produced by the local platform based on temporal proximity,
symbol and direction.  Temporal tolerance is configurable so that small
differences in bar-timestamp resolution do not break the comparison.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class TradeMatcher:
    """Pair reference trades with actual trades.

    The matcher walks the ``reference_trades`` list in order and picks the
    minimum-cost still-unmatched ``actual_trade`` satisfying all criteria
    (direction, symbol, time tolerance). Cost blends time drift with entry-
    price and quantity differences, so ties no longer fall to array order.
    Exact ties keep the lowest actual index (deterministic).
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
                "matched": False,
            }
            for t in actual_trades
        ]

        matched: list[tuple[Trade, Trade, MatchKey]] = []
        missing: list[Trade] = []
        extra: list[Trade] = []

        for ref in ref_data:
            best_idx: int | None = None
            best_cost = float("inf")
            for idx, act in enumerate(act_data):
                if act["matched"]:
                    continue

                # direction is always required
                if ref["trade"].direction != act["trade"].direction:
                    continue

                if match_symbol and ref["trade"].symbol != act["trade"].symbol:
                    continue

                if _time_diff_seconds(ref["entry"], act["entry"]) > tolerance_seconds:
                    continue
                if _time_diff_seconds(ref["exit"], act["exit"]) > tolerance_seconds:
                    continue

                # C4: minimum cost wins; strict < keeps the lowest index on
                # exact ties (deterministic, preserves old order behavior).
                cost = _match_cost(
                    ref,
                    act,
                    tolerance_seconds,
                    time_weight=time_weight,
                    price_weight=price_weight,
                    quantity_weight=quantity_weight,
                )
                if cost < best_cost:
                    best_cost = cost
                    best_idx = idx

            found_idx = best_idx
            if found_idx is not None:
                act_data[found_idx]["matched"] = True
                act_trade = act_data[found_idx]["trade"]
                key = MatchKey(
                    entry_time=ref["entry"].isoformat().replace(_UTC_SUFFIX_OFFSET, _UTC_SUFFIX_Z),
                    exit_time=ref["exit"].isoformat().replace(_UTC_SUFFIX_OFFSET, _UTC_SUFFIX_Z),
                    symbol=ref["trade"].symbol,
                    direction=ref["trade"].direction,
                )
                matched.append((ref["trade"], act_trade, key))
            else:
                missing.append(ref["trade"])

        extra.extend(act["trade"] for act in act_data if not act["matched"])

        return MatchResult(matched=matched, missing=missing, extra=extra)
