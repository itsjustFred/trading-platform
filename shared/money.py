"""Strict parsing for broker money fields (qty, prices, cash, equity, PnL).

One helper for every live-path money read, so an unreadable value can never
silently become ``0.0`` — the coercion that made a malformed broker quantity
read as "flat" and a malformed stop price read as "no stop" (issue #280).

Two entry points:

* :func:`parse_money` — a required money field. ``None``, an unparseable
  value, a boolean or a non-finite number raises ``ValueError``.
* :func:`parse_optional` — a money field that may legitimately be absent.
  ``None`` maps to ``None``; anything else is held to :func:`parse_money`'s
  standard, so garbage still raises rather than degrading to "absent".
"""

from __future__ import annotations

import math
from typing import Any


class MoneyParseError(ValueError):
    """A broker money field that is present but not readable as a finite float.

    A ``ValueError`` subclass so every existing ``except ValueError`` keeps
    working, but distinct enough for a caller to catch *only* parse failures:
    ``reconcile_executions`` skips one malformed order per pass without also
    swallowing programming errors or database-layer ``ValueError``s as
    "unreadable order data".
    """


def parse_money(value: Any) -> float:
    """``value`` as a finite float, or ``MoneyParseError`` when it is not readable.

    ``None`` is unreadable for a required money field: a broker that omits a
    quantity, price or balance has not told us zero, it has told us nothing.
    Booleans are rejected — ``True`` is not a price — and non-finite results
    (``nan``, ``inf``) are rejected because they poison every comparison
    downstream.
    """
    if value is None or isinstance(value, bool):
        raise MoneyParseError(f"unreadable money value: {value!r}")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MoneyParseError(f"unreadable money value: {value!r}") from exc
    if not math.isfinite(parsed):
        raise MoneyParseError(f"non-finite money value: {value!r}")
    return parsed


def parse_optional(value: Any) -> float | None:
    """``value`` as a finite float, ``None`` when the field is absent.

    Absence (``None``) is the only tolerated non-answer: a present but
    unreadable value still raises, so "the broker sent garbage" can never be
    confused with "the broker sent nothing".
    """
    if value is None:
        return None
    return parse_money(value)
