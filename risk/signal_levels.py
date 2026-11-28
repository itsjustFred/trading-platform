"""The SL/TP level aliases of signal metadata, in one place.

Signal metadata is an UNTRUSTED publisher payload: a stop or a take profit may
arrive under any of several alias spellings, and which one a publisher used
depends on the producer. This module is the single answer to three questions
the live path kept answering in a copy per caller:

* which spellings exist (:data:`SL_METADATA_KEYS`, :data:`TP_METADATA_KEYS` —
  one ordered tuple per level, canonical spelling first);
* which one wins (:func:`resolve_signal_level` — the first alias the payload
  actually carries, never the payload's insertion order);
* how readable a present value must be (:class:`LevelParsing`).

Callers on the money path: ``risk.live.resolve_live_levels`` (sizing and the
levels the broker order carries), the Alpaca adapter's
``_resolve_broker_sl_tp``/``_round_bracket_levels_in_place`` and the cTrader
adapter's ``_build_new_order_request``. They disagree on strictness, so the
strictness is an explicit parameter rather than a merge-time accident; the
difference and the recommended direction (fail closed: an unreadable level must
never read as "no level") are recorded in the PR that introduced this module
(G19).

Error messages here are operator-visible — they surface as a refused order —
so each parsing mode keeps the text its callers already raised. Unifying that
text is a separate, deliberate change.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from shared.money import parse_money

#: Stop-loss aliases of signal metadata, most canonical first.
#:
#: ``resolved_stop_loss`` is last on purpose: the risk engine stamps it after
#: sizing resolved a fallback stop (ATR/percent), so it names the stop the
#: position was actually sized on. A publisher that sets it directly is
#: answering a question the platform answers itself, but reading it keeps the
#: broker order's stop identical to the one sizing used.
SL_METADATA_KEYS: Final[tuple[str, ...]] = (
    "stop_loss",
    "sl_price",
    "stop_price",
    "resolved_stop_loss",
)

#: Take-profit aliases of signal metadata, most canonical first.
TP_METADATA_KEYS: Final[tuple[str, ...]] = ("take_profit", "tp_price")


class LevelParsing(Enum):
    """How readable a present SL/TP metadata value must be.

    Both members resolve the same alias order and both refuse a value that
    cannot be read at all; they differ on whether a value ``float()`` accepts
    is a *level* — and in the error text, which each mode's callers already
    raised.

    ``MONEY`` : the value is held to ``shared.money.parse_money``: a boolean, a
      non-numeric value and a non-finite number all raise, because a crude
      ``float()`` is how a malformed broker field used to degrade into ``0.0``
      (issue #280). Error text: ``"<key>: unreadable money value: <value>"``.
    ``ANY_FLOAT`` : any value ``float()`` accepts resolves, ``True`` → ``1.0``
      and ``nan``/``inf`` included; only a value ``float()`` itself refuses
      raises. Error text: ``"Invalid <key>: <value>"``.

    The callers that parse ``ANY_FLOAT`` compensate downstream — they refuse
    non-finite and non-positive levels before an order is built — but they do
    NOT compensate for a boolean, which reaches them as the price ``1.0``.
    Switching them to ``MONEY`` is the recommended direction; until the
    maintainer decides it, the difference is pinned by tests.
    """

    MONEY = "money"
    ANY_FLOAT = "any_float"


@dataclass(frozen=True)
class SignalLevel:
    """One resolved level: its price, and the alias key it was read from.

    ``price`` is ``None`` exactly when the payload carries none of the level's
    aliases with a value — the resolver never invents a level — and ``key`` is
    ``None`` in that case too, so an absent level and a level read from a key
    are distinguishable without inspecting the payload again.
    """

    price: float | None = None
    key: str | None = None


def _parse_present_level(key: str, value: Any, parsing: LevelParsing) -> float:
    """Read a present value as the level *key* carries, or name the key and raise.

    A value that cannot be read is an error, never a skip: the next alias must
    not silently take over, and "the payload is unreadable" must not collapse
    into "the payload has no level" (an unparseable SL must never mean no stop).
    """
    if parsing is LevelParsing.MONEY:
        try:
            return parse_money(value)
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from exc
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {key}: {value!r}") from exc


def resolve_signal_level(
    metadata: Mapping[str, Any] | None,
    keys: Sequence[str],
    *,
    parsing: LevelParsing,
) -> SignalLevel:
    """Resolve one level from signal metadata under one alias allowlist.

    ``keys`` is one ordered alias tuple of this module; the first key the
    payload carries with a non-``None`` value decides, so precedence never
    depends on the payload's key order. A later alias is read only when the
    earlier one is absent, and a present but unreadable value raises rather
    than letting the later one through.

    Returns a :class:`SignalLevel`: the price (``None`` when no alias carried a
    value) and the key it was read from, so a caller can report which alias a
    level actually came from. ``parsing`` selects how readable a present value
    must be (see :class:`LevelParsing`).

    What a caller does with an absent level is its policy: the risk engine may
    resolve a fallback stop, the daemon and both adapters refuse the order.
    """
    if metadata is None:
        return SignalLevel(None, None)
    for key in keys:
        if key not in metadata:
            continue
        value = metadata.get(key)
        if value is None:
            continue
        return SignalLevel(_parse_present_level(key, value, parsing), key)
    return SignalLevel(None, None)
