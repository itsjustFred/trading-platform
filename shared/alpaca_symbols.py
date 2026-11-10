"""Classify Alpaca symbols as crypto or equity from the symbol itself (#124, review of #143).

Alpaca accepts a crypto pair with or without a slash (``BTC/USD`` and ``BTCUSD``), so a slash is
not evidence of crypto, and its absence is not evidence of an equity. A signal stored as ``BTCUSD``
must still be recognised: its fee is a ``CFEE`` to backfill, and a hard-coded ``0.0`` on it was
never a stated fee.

The rules, deliberately narrow so anything else is ``unknown`` (and readers fail closed):

* ``BASE/QUOTE`` with two alphanumeric parts is crypto - Alpaca uses the slash only for crypto.
* Without a slash, a symbol of at least six letters that ends in a known quote (``CRYPTO_QUOTES``,
  longest first) after a base of at least three letters is crypto. Every Alpaca crypto base has at
  least three letters, and no US equity ticker has six.
* One to five letters, optionally with a one- or two-letter share-class suffix (``BRK.B``,
  ``BF-B``), is a US equity - including ``USD``, an ETF named like a quote.
* Everything else (an OCC option symbol, an empty or malformed string) is ``unknown``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

#: Quote currencies Alpaca crypto pairs trade against, longest first so ``SOLUSDT`` is SOL/USDT.
CRYPTO_QUOTES: tuple[str, ...] = ("USDT", "USDC", "USD", "BTC")

_MIN_CRYPTO_BASE = 3
_EQUITY_RE = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?$")
_ALNUM_RE = re.compile(r"^[A-Z0-9]+$")

AlpacaAssetKind = Literal["crypto", "us_equity", "unknown"]


def _normalize(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def split_crypto_pair(symbol: Any) -> tuple[str, str] | None:
    """``(base, quote)`` for a crypto pair in either spelling, else ``None``."""
    text = _normalize(symbol)
    if "/" in text:
        parts = text.split("/")
        if len(parts) == 2 and all(_ALNUM_RE.match(part) for part in parts):
            return parts[0], parts[1]
        return None
    if len(text) < 2 * _MIN_CRYPTO_BASE or not _ALNUM_RE.match(text):
        return None
    for quote in CRYPTO_QUOTES:
        base = text[: -len(quote)]
        if text.endswith(quote) and len(base) >= _MIN_CRYPTO_BASE:
            return base, quote
    return None


def crypto_pair_key(symbol: Any) -> str:
    """One comparable key for both spellings: ``BTC/USD`` and ``BTCUSD`` are ``BTCUSD``."""
    return _normalize(symbol).replace("/", "")


def classify_alpaca_symbol(symbol: Any) -> AlpacaAssetKind:
    if split_crypto_pair(symbol) is not None:
        return "crypto"
    if _EQUITY_RE.match(_normalize(symbol)):
        return "us_equity"
    return "unknown"
