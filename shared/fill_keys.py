"""Canonical spellings for the timestamps that end up inside dedupe / idempotency keys.

A key is compared byte for byte, so two spellings of one instant (``...Z`` vs ``...+00:00``, an
aware datetime vs a naive one, ``10:00:00+00:00`` vs ``12:00:00+02:00``) are two different keys:
the same broker fill or producer signal is then counted twice. The reverse failure is just as
real - rounding timestamps to a coarse unit merges two genuine fills that happened in the same
second - so the canonical form here **normalises the representation, never the precision**.

Rules:

* The result is the UTC instant rendered by ``datetime.isoformat()``. Sub-second digits are
  kept exactly as given (microseconds, ``datetime``'s native resolution) and omitted only when
  they are zero, which is how an existing ``...+00:00`` key already looked.
* A naive datetime or string is read as UTC. Every writer in this repository stamps UTC and
  SQLite round-trips drop the tzinfo, so "naive" has only ever meant "UTC with the zone lost".
* Anything that is not a datetime or an ISO-8601 string yields ``None``. Callers decide what
  to do then (key on the verbatim text, or refuse): this module never invents an instant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def canonical_utc_timestamp(value: Any) -> str | None:
    """Return the canonical UTC ISO-8601 text for ``value``, or ``None`` if it is not a time."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()
