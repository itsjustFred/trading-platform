"""Trade matcher for pairing reference and actual trades.

The matcher pairs trades from an external reference (e.g. imported from MT5)
with trades produced by the local platform based on temporal proximity,
symbol and direction.  Temporal tolerance is configurable so that small
differences in bar-timestamp resolution do not break the comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shared.models import Trade


def _normalize_timestamp(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp and return a timezone-aware UTC datetime.

    Strings ending in ``Z`` are treated as UTC.  Naive strings are assumed
    to be UTC.  All return values are converted to UTC so that timestamps
    from different time zones can be compared.
    """
    # datetime.fromisoformat does not understand "Z" in Python < 3.11
    normalized = ts.replace("Z", "+00:00")
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


class TradeMatcher:
    """Pair reference trades with actual trades.

    The matcher works greedily: it walks the ``reference_trades`` list in
    order and picks the *first* still-unmatched ``actual_trade`` that
    satisfies all criteria.  This means that when several trades share the
    same timestamps the pairing follows the original array order.
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

        Returns
        -------
        MatchResult
            Container with ``matched``, ``missing`` and ``extra`` trade lists.
        """
        tolerance_seconds: int = int(options.get("time_tolerance_seconds", 0))
        match_symbol: bool = bool(options.get("match_symbol", True))

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
            found_idx: int | None = None
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

                found_idx = idx
                break

            if found_idx is not None:
                act_data[found_idx]["matched"] = True
                act_trade = act_data[found_idx]["trade"]
                key = MatchKey(
                    entry_time=ref["entry"].isoformat().replace("+00:00", "Z"),
                    exit_time=ref["exit"].isoformat().replace("+00:00", "Z"),
                    symbol=ref["trade"].symbol,
                    direction=ref["trade"].direction,
                )
                matched.append((ref["trade"], act_trade, key))
            else:
                missing.append(ref["trade"])

        for act in act_data:
            if not act["matched"]:
                extra.append(act["trade"])

        return MatchResult(matched=matched, missing=missing, extra=extra)
