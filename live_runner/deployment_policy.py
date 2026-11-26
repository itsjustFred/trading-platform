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

It answers two different questions, and the difference is the point: whether the
daemon can *trade* a source (``TRADEABLE_SIGNAL_SOURCES``), and whether a stored
value is one the rest of the system *recognises* (``KNOWN_SIGNAL_SOURCES``, which
adds the ``internal`` default and the retired ``discord`` source). Neither set is
a free-standing literal: the second is derived so a stored row keeps loading and
preflighting after its runner is gone.

Maintainer decision 2026-11-26 (W07): Discord signal ingestion was removed. No
signal source has a listener runner anymore - ``internal`` deployments are
traded by the internal strategy runner on its own opt-in, and ``discord`` rows
are retired: still recognised (the live DB holds paused discord deployments that
must keep loading and listing), never dispatched, never an error.
"""

from __future__ import annotations

#: The DB column default and the pydantic model default for "no source chosen".
DEFAULT_SIGNAL_SOURCE = "internal"

#: The retired Discord signal source. Stored rows and old configs still spell it;
#: nothing ingests it anymore, so it is recognised but has no runner.
RETIRED_DISCORD_SIGNAL_SOURCE = "discord"

#: Signal sources the daemon has a signal-listener runner for. Empty since the
#: Discord ingestion removal (maintainer decision 2026-11-26): ``internal`` is
#: not a listener source - the internal strategy runner trades the deployments
#: that opted in - and ``discord`` is retired. Every routing gate in the daemon
#: and both preflights asks this set.
TRADEABLE_SIGNAL_SOURCES: frozenset[str] = frozenset()

#: The statuses a live deployment can hold. ``shared.models`` pins them on the
#: pydantic model; the daemon additionally has to survive a stored status that
#: is none of them, so it normalises to the ``UNKNOWN`` bucket below.
KNOWN_DEPLOYMENT_STATUSES: frozenset[str] = frozenset({"ACTIVE", "PAUSED", "STOPPED"})

#: The sources the DB column and the models recognise: the default plus every
#: source a stored row may still legitimately carry, retired ones included. A
#: value outside this set is a configuration error the operator has to see
#: spelled out; a retired one is recognised and simply never trades.
KNOWN_SIGNAL_SOURCES: frozenset[str] = frozenset(
    {DEFAULT_SIGNAL_SOURCE, RETIRED_DISCORD_SIGNAL_SOURCE}
)


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
    `` discord`` is not a known source to it, and a preflight that trimmed it
    would call such a row the clean match it is not.
    """
    return str(signal_source or DEFAULT_SIGNAL_SOURCE).lower()


def signal_source_can_trade(signal_source: object) -> bool:
    """Whether a deployment on this source is dispatched by a signal listener."""
    return normalize_signal_source(signal_source) in TRADEABLE_SIGNAL_SOURCES


def untradeable_signal_source_reason(signal_source: object) -> str | None:
    """Why an ACTIVE deployment on this source can never trade, or None.

    ``None`` means the daemon has a runner for it. A retired source
    (``discord``) is named as retired rather than unknown: the row is valid,
    it just has nothing left to listen to. ``internal`` is recognised and is
    refined by the internal strategy runner's own answer; an unrecognised
    stored value is reported as the configuration error it is.
    """
    source = normalize_signal_source(signal_source)
    if source in TRADEABLE_SIGNAL_SOURCES:
        return None
    if source == RETIRED_DISCORD_SIGNAL_SOURCE:
        return (
            "signal source 'discord' is retired: Discord signal ingestion was "
            "removed and nothing listens for its signals anymore"
        )
    if source == DEFAULT_SIGNAL_SOURCE:
        return (
            "signal source 'internal' is traded only by the internal strategy "
            "runner for deployments that opted in"
        )
    return f"signal source '{source}' is not a known signal source and has no runner"
