"""#242: the one definition of whether the live daemon can trade a deployment.

``live_runner/daemon.py``, ``live_runner/preflight.py`` and
``live_runner/preflight_db.py`` all answer the same question about a deployment:
can anything here ever place an order for it? Before #242 each module carried its
own answer -- its own reading of "ACTIVE", its own reading of "Discord" -- so the
YAML preflight could call a row clean that the daemon then silently ignored,
which is exactly how three ACTIVE ``internal`` cTrader rows (#184 finding 1)
looked live for as long as they did.

This module holds the answer and imports nothing outside the standard library,
deliberately: the two preflights are read-only paths (``preflight_db`` opens
sqlite with ``mode=ro``), and importing the daemon to reach a six-line predicate
pulled both broker adapters and the Discord stack in with it, and initialised
Sentry under the daemon's service name on every preflight run.
"""

from __future__ import annotations

#: Signal sources the daemon has a runner for. Discord is the only one today; a
#: new source becomes tradeable by being added here and nowhere else. Every
#: routing gate in the daemon and both preflights ask this set.
TRADEABLE_SIGNAL_SOURCES: frozenset[str] = frozenset({"discord"})

#: The statuses a live deployment can hold. ``shared.models`` pins them on the
#: pydantic model; the daemon additionally has to survive a stored status that
#: is none of them, so it normalises to the ``UNKNOWN`` bucket below.
KNOWN_DEPLOYMENT_STATUSES: frozenset[str] = frozenset({"ACTIVE", "PAUSED", "STOPPED"})

#: The DB column default and the pydantic model default for "no source chosen".
DEFAULT_SIGNAL_SOURCE = "internal"


def normalize_deployment_status(status: object) -> str:
    """The status the daemon acts on: a known one, or ``UNKNOWN``.

    The DB is written by operators, other processes and migrations, so a stored
    status cannot be assumed to be one of the three. Everything unknown is one
    bucket rather than a per-value passthrough, so the daemon never has to decide
    whether an unrecognised status might mean "active".
    """
    normalized_status = str(status or "").upper()
    if normalized_status in KNOWN_DEPLOYMENT_STATUSES:
        return normalized_status
    return "UNKNOWN"


def normalize_signal_source(signal_source: object) -> str:
    """The stored/configured source exactly as the daemon's routing gates read it.

    The gates compare ``str(deployment.signal_source or "internal").lower()``, so a
    value the router looks at is the same value this policy judges -- and the
    preflights report a row in the same spelling. Anything falsy is the
    ``internal`` default: an empty DB column means "no runner". There is no
    ``strip()``, because the daemon does not strip the stored value either:
    `` discord`` is not Discord to it, and a preflight that trimmed it would
    call such a row the clean match it is not.
    """
    return str(signal_source or DEFAULT_SIGNAL_SOURCE).lower()


def signal_source_can_trade(signal_source: object) -> bool:
    """Whether a deployment on this source is dispatched at all."""
    return normalize_signal_source(signal_source) in TRADEABLE_SIGNAL_SOURCES


def untradeable_signal_source_reason(signal_source: object) -> str | None:
    """Why an ACTIVE deployment on this source can never trade, or None.

    ``None`` means the daemon has a runner for it. An ``internal`` source is the
    default and gets the runner-missing reason; a stored value that is neither
    ``internal`` nor ``discord`` is reported as the unknown it is, because it is
    a configuration error the operator has to see spelled out.
    """
    source = normalize_signal_source(signal_source)
    if source in TRADEABLE_SIGNAL_SOURCES:
        return None
    if source == DEFAULT_SIGNAL_SOURCE:
        return (
            "signal source 'internal' has no live strategy runner; "
            "only Discord signals are traded"
        )
    return f"signal source '{source}' is neither 'internal' nor 'discord' and has no runner"
