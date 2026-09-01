"""SQL construction helpers.

Operational code must never build SQL by raw f-string interpolation
(DV-B27).  These helpers quote identifiers and validate literals so that a
malicious or malformed symbol/table name fails fast with ``ValueError``
instead of reaching the SQL engine.
"""

from __future__ import annotations

import re

# Symbols we accept as SQL literals: alphanumeric, dot, dash, underscore.
_SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_EMPTY_IDENTIFIER_MSG = "SQL identifier must not be empty"


def quote_identifier(name: str) -> str:
    """Return *name* as a backtick-quoted SQL identifier.

    Raises
    ------
    ValueError
        When *name* is empty.
    """
    text = str(name).strip()
    if not text:
        raise ValueError(_EMPTY_IDENTIFIER_MSG)
    return f"`{text.replace('`', '``')}`"


def validate_symbol(symbol: str) -> str:
    """Validate *symbol* as a safe SQL literal value.

    Accepts alphanumeric characters plus ``.``, ``-`` and ``_`` (ticker and
    index symbol conventions).  Anything else — in particular quotes,
    whitespace and semicolons — raises ``ValueError``.
    """
    text = str(symbol).strip()
    if not _SAFE_SYMBOL.match(text):
        raise ValueError(f"unsafe SQL symbol: {symbol!r}")
    return text


def quote_literal(symbol: str) -> str:
    """Return *symbol* as a single-quoted SQL literal.

    The symbol must match the safe-symbol pattern; quoting arbitrary strings
    is intentionally not supported (use parameterized queries for those).
    """
    return f"'{validate_symbol(symbol)}'"
