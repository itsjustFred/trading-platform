"""Asset-class classification for Alpaca symbols.

Classification is by *asset class*, never by string shape alone. ``endswith("USD")``
is wrong in both directions: it misses nothing it should catch but would send any
equity ticker ending in ``USD`` to the crypto feed. A crypto pair is therefore
recognised only when its base currency is in :data:`CRYPTO_BASES`, which is an
offline allowlist (issue #122, option 2) and has to be maintained when Alpaca lists a
new coin.

Anything that cannot be classified raises :class:`UnclassifiableSymbolError`. Callers
must refuse to subscribe / fetch rather than fall back to the stock feed, because a
crypto pair routed to the stock feed silently produces no data.
"""

from __future__ import annotations

import re

AssetClass = str  # "crypto" | "stock"

# Quote currencies Alpaca lists crypto pairs against. Longest first so USDT/USDC are
# not read as "USD" with a stray trailing letter.
CRYPTO_QUOTES: tuple[str, ...] = ("USDT", "USDC", "USD", "BTC")

# Base currencies tradable on Alpaca crypto. Keep sorted.
CRYPTO_BASES: frozenset[str] = frozenset(
    {
        "AAVE", "ADA", "ARB", "AVAX", "BAT", "BCH", "BTC", "CRV", "DOGE", "DOT",
        "ETH", "GRT", "LINK", "LTC", "MKR", "PEPE", "SHIB", "SOL", "SUSHI",
        "TRUMP", "UNI", "XRP", "XTZ", "YFI",
    }
)

# US equity tickers are at most 5 letters, optionally with a class suffix (BRK.B).
_EQUITY_RE = re.compile(r"^[A-Z]{1,5}(?:[.][A-Z]{1,2})?$")
_PAIR_RE = re.compile(r"^([A-Z0-9]{2,10})[/-]([A-Z]{3,4})$")


class UnclassifiableSymbolError(ValueError):
    """The symbol is neither a recognised crypto pair nor a plausible equity ticker."""


def _split_crypto(symbol: str) -> tuple[str, str] | None:
    """Return (base, quote) for a recognised crypto pair in any supported form."""
    pair = _PAIR_RE.match(symbol)
    if pair is not None:
        base, quote = pair.groups()
        if base in CRYPTO_BASES and quote in CRYPTO_QUOTES:
            return base, quote
        return None
    for quote in CRYPTO_QUOTES:
        if symbol.endswith(quote):
            base = symbol[: -len(quote)]
            if base in CRYPTO_BASES:
                return base, quote
    return None


def classify_symbol(symbol: str) -> AssetClass:
    """Return ``"crypto"`` or ``"stock"``; raise if the symbol cannot be classified."""
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise UnclassifiableSymbolError("empty symbol")
    if _split_crypto(normalized) is not None:
        return "crypto"
    if "/" in normalized or "-" in normalized:
        raise UnclassifiableSymbolError(
            f"{normalized!r} looks like a pair but its base/quote is not a known crypto "
            "pair; refusing to route it to the stock feed"
        )
    if _EQUITY_RE.match(normalized):
        return "stock"
    raise UnclassifiableSymbolError(
        f"{normalized!r} is neither a known crypto pair nor a valid equity ticker"
    )


def canonical_crypto_symbol(symbol: str) -> str:
    """``BTCUSD`` / ``BTC-USD`` / ``btc/usd`` -> ``BTC/USD`` (Alpaca's crypto form)."""
    normalized = str(symbol or "").strip().upper()
    split = _split_crypto(normalized)
    if split is None:
        raise UnclassifiableSymbolError(f"{normalized!r} is not a known crypto pair")
    return f"{split[0]}/{split[1]}"
