"""Decide whether a viewer URL is safe for the integration suite to write into.

Separate file, and free of import-time side effects on purpose.

The suite this protects performs HTTP *while being imported* - it discovers the
strategy matrix at module scope so the tests can be parametrised. That ordering
defeats a guard written as a `skipif` mark, because marks attach to test
functions and are not evaluated until long after the import-time request has
already gone out. The original guard was exactly that, and pointing BASE_URL at
the live platform on :5556 with the opt-in set still reached it during
collection. That is the shape of the incident that put 219 rows in the
production database.

So the decision lives here, where it can be imported, read and tested without
touching a network, and the suite consults it *before* it makes a request.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

#: Ports this host serves the live platform on. Isolation cannot be *proved* from
#: a URL, and this does not pretend otherwise - it refuses the one target that is
#: known to be production rather than leaving that to inference.
PRODUCTION_VIEWER_PORTS = frozenset({"5556", "5557"})

OVERRIDE_ENV = "PERMA_ALLOW_PRODUCTION_INTEGRATION"


def override_enabled() -> bool:
    """Whether the operator has explicitly accepted writing to production.

    A second, separately named acknowledgement on purpose. The single opt-in that
    was already set is exactly what was set when the accident happened, so
    reusing it would make the guard a formality.
    """
    return os.getenv(OVERRIDE_ENV, "").strip().lower() in {"1", "true", "yes"}


def port_of(url: str) -> str | None:
    """The explicit port of a URL, or None when it carries none.

    None rather than a default on purpose: ``http://127.0.0.1/`` means port 80,
    and treating "no port" as "not production" would let a URL that might resolve
    to the live platform through on a technicality.
    """
    try:
        return str(urlparse(url).port or "") or None
    except ValueError:
        # A malformed port makes the URL untrustworthy, not safe.
        return ""


def targets_production(url: str) -> bool:
    """True when this URL must not be written to without the override."""
    if override_enabled():
        return False
    port = port_of(url)
    if port is None:
        # No explicit port: production cannot be ruled out, so refuse.
        return True
    if port == "":
        return True  # unparseable
    return port in PRODUCTION_VIEWER_PORTS


def refusal_reason(url: str) -> str:
    return (
        f"Refusing to contact {url!r}: it is the live platform on this host "
        f"(port in {sorted(PRODUCTION_VIEWER_PORTS)}), and this suite writes "
        "backtest artifacts into the viewer's DATA_DIR. That is how 219 rows once "
        "landed in the production database. Point BASE_URL at an isolated viewer, "
        f"or set {OVERRIDE_ENV}=1 if you genuinely mean to write to production."
    )
