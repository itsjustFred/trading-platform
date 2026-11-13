from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import requests

from live_runner.credential_env import discord_bot_env_key
from live_runner.signal_sources.base_source import BaseSignalSource
from risk.models import TradeSignal
from shared.constants import normalize_timeframe
from shared.database import get_service, update_service_status
from shared.logging import get_logger
from shared.models import ServiceStatus

logger = get_logger("live-runner")

# Polling cadence for the REST-based signal source (seconds).
POLL_INTERVAL = 8
POLL_LIMIT = 20

# Retention window for the cumulative seen-set, in message ids. It must span
# several consecutive POLL_LIMIT pages so a burst larger than POLL_LIMIT can
# still drain across polls, and it must be wide enough that a repeat poll of
# the live window (POLL_LIMIT ids) can never re-admit a pruned id.
SEEN_ID_RETENTION = POLL_LIMIT * 10


class DiscordAuthenticationError(RuntimeError):
    """Raised when Discord returns a terminal authentication error (401)."""


class DiscordChannelForbiddenError(RuntimeError):
    """Raised when Discord returns 403 for one channel (B2).

    Missing channel access, not a dead token: the channel is skipped while
    the source keeps polling the rest.
    """

    def __init__(self, channel_id: str, message: str = "") -> None:
        super().__init__(message or f"Discord channel forbidden (403): {channel_id}")
        self.channel_id = channel_id


#: Maximum backfill pages per poll when a gap is detected (B2: bursts beyond
#: POLL_LIMIT and 429-windows must not lose signals incl. exits).
MAX_BACKFILL_PAGES = 5

#: ``auth_scheme`` values that mean "a Developer Portal BOT token" — sent with
#: the ``Bot `` prefix.
DISCORD_BOT_AUTH_SCHEMES = frozenset({"bot"})
#: ``auth_scheme`` values that mean "a USER/ACCOUNT (self-bot) token" — sent
#: exactly as supplied. ``raw`` is the historical operator spelling (#81: tokens
#: copied out of Discord devtools), ``account`` the explicit one; they are the
#: same credential kind and are both accepted.
DISCORD_ACCOUNT_AUTH_SCHEMES = frozenset({"raw", "account"})
#: Every accepted ``auth_scheme``. Anything else raises instead of being guessed.
DISCORD_AUTH_SCHEMES = DISCORD_BOT_AUTH_SCHEMES | DISCORD_ACCOUNT_AUTH_SCHEMES
#: Last-resort scheme when nothing is configured anywhere. "raw" is the
#: recorded owner decision for the existing operator deployment; a mismatch
#: with the actual token is NOT silently corrected — Discord answers 401 and
#: :class:`DiscordAuthenticationError` fails the source closed.
DEFAULT_DISCORD_AUTH_SCHEME = "raw"


class DiscordRateLimitError(RuntimeError):
    """Raised when Discord returns a 429 rate limit response."""

    def __init__(self, channel_id: str, retry_after: float) -> None:
        super().__init__(
            f"Discord rate limited channel {channel_id}; retry after {retry_after}s"
        )
        self.channel_id = channel_id
        self.retry_after = retry_after


_RAMJET_OVERSOLD_RE = re.compile(r"is now oversold", re.IGNORECASE)
_BUY_SIGNAL_RE = re.compile(r"buy signal", re.IGNORECASE)
_PRICE_LABEL_RE = re.compile(
    r"Price:\s*\$\s*(?P<price>\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
_DOLLAR_PRICE_RE = re.compile(r"\$\s*(?P<price>\d[\d,]*(?:\.\d+)?)")
_AT_PRICE_RE = re.compile(r"@\s*(?P<price>\d[\d,]*(?:\.\d+)?)")
# The mirror of _ACTION_SELL_RE, and the same hazard in the other direction: a
# buy is the fallback, so an incidental "buy" opens a real position. Matched
# bare, "could buy here" and - worse - "do not buy" both read as a BUY signal.
#
# An explicit buy directive is the action word next to "signal", or the action
# word immediately followed by a ticker - which is the shape
# _extract_action_symbol already recognises, and what the real RamJet channel
# emits ("RAM JET BUY AAPL @ 100.0"). Anchoring to the start of the message
# instead would have broken every real RamJet signal.
#
# CORRECTION. This comment used to add: "the ticker is required rather than a
# position: 'do not buy' and 'could buy here' have no ticker after the word and
# so resolve to nothing." **That was wrong, and wrong in the way that matters** -
# it credited this regex with work it does not do. Measured:
#
#   text              this regex   captured ticker   normalizes to   price
#   "could buy here"  matches      "here"           HERE            None
#   "do not buy this" matches      "this"           THIS            None
#
# Neither "here" nor "this" is in _SKIP_WORDS, and both are 1-10 letters, so
# both are perfectly good ticker candidates by this regex's own definition. What
# actually rejects them is further down, in _reads_as_buy_directive: the
# **negation veto** for "do not buy", and the **price requirement** for "could
# buy here" ("a stop-word list cannot enumerate every English word that might
# follow 'buy', but a price is unambiguous").
#
# Why it was worth correcting rather than deleting: a developer tightening this
# regex to match the stated rationale - requiring a real ticker shape, or
# anchoring harder - would have been reinforcing a protection that lives
# elsewhere, while the two guards that do the work went unremarked. Mutation
# testing agrees: deleting the price requirement is caught, and deleting the
# negation veto is caught, while five other mutations of this function and its
# neighbours survive precisely because those two are load-bearing.
_ACTION_BUY_RE = re.compile(
    r"\b(?:buy|long)\s+signal\b"
    r"|\b(?:buy|long)\s+\**\$?[A-Za-z]{1,10}\b",
    re.IGNORECASE,
)
# Captures the word after buy/long so it can be run through the same
# _SKIP_WORDS filter the symbol extractors use. A regex cannot know that "here"
# is not a ticker, but _normalize_symbol_candidate already knows.
_ACTION_BUY_TICKER_RE = re.compile(
    r"\b(?:buy|long)\s+\**\$?([A-Za-z]{1,10})\b",
    re.IGNORECASE,
)
# An action word only decides direction when the message is actually a signal.
# Matched bare, "sellers are short-term weak" inside an unambiguous buy message
# flipped it to a sell - and a sell a bot accepts is a real short position.
#
# Two shapes count as an explicit sell signal: the action word next to the word
# "signal" ("sell signal", "signal: SELL"), or a momentum-reversal phrase. A
# bare trailing action word after a symbol and price is also a signal, but only
# when the word is the *directive* ("SELL EURUSD"), not a noun phrase describing
# the market ("sellers are weak", "short covering"). `_explicit_sell_action`
# below keeps that distinction; requiring "signal" alone would have silently
# dropped the plain "SELL EURUSD" form, which is a regression, not a tightening.
# One definition of the explicit sell forms, and the only one. This rule used to
# be spelled out twice - here and in _EXPLICIT_SELL_SIGNAL_RE - unguarded in both.
# That duplication is what let "signal is short of resistance" through while the
# explicit copy was being tightened: a second, unfixed copy of the same rule.
#
# Guards on the "signal is short" alternative, both load-bearing:
#   `(?![-\w])`  `\bshort\b` matches the "short" inside "short-term", because a
#     hyphen is a word boundary, so "oversold buy signal. Signal is short-term."
#     was vetoed as a noun and then let straight back in as an instruction - a
#     real short opened on a BUY message.
#   _SELL_PROSE_TAIL  "short of resistance" and "short covering" are prose that
#     continues past the word, not an instruction that stops at it.
#
# The "sell signal" alternative is deliberately unguarded: it is an instruction in
# every phrasing seen, and guarding it dropped real signals.
_SELL_PROSE_TAIL = (
    r"(?!\s+(?:of|term|covering|sellers?|positions?|at|into|near|below|above))"
)
_EXPLICIT_SELL_SIGNAL_RE = re.compile(
    r"\b(?:sell|short)\s+signal\b"
    r"|\bsignal\s*(?:is|:)\s*\b(?:sell|short)(?![-\w])" + _SELL_PROSE_TAIL + r"\b",
    re.IGNORECASE,
)
# A leading "sell"/"short" only counts when the message says what it acts on.
# Without requiring a target, "short covering underway" is a directive aimed at a
# ticker called COVERING, because any two-to-ten-letter noun passed as a symbol.
_LEADING_SELL_DIRECTIVE_RE = re.compile(
    r"^\s*(?:sell|short)(?![-\w])\b\s*[:\-]?\s*\**\$?(?P<target>[A-Za-z]{2,10})\b",
    re.IGNORECASE,
)
_ACTION_SELL_RE = re.compile(
    _EXPLICIT_SELL_SIGNAL_RE.pattern + r"|\bmomentum\s*reversal\b",
    re.IGNORECASE,
)
# Noun-phrase and participle forms that describe the market rather than issue an
# instruction. Checked before _ACTION_SELL_RE so "sellers", "short-term" and
# "short covering" cannot be read as a directive.
_SELL_AS_NOUN_RE = re.compile(
    r"\bsellers?\b"
    r"|\bshorts\b"
    r"|\bshort[-\s]term\b"
    r"|\bshort\s+covering\b"
    r"|\bcovering\b",
    re.IGNORECASE,
)
_MOMENTUM_REVERSAL_RE = re.compile(r"momentum\s*reversal", re.IGNORECASE)
_GOLDEN_CROSS_RE = re.compile(r"golden\s*cross", re.IGNORECASE)
_DEATH_CROSS_RE = re.compile(r"death\s*cross", re.IGNORECASE)
_OVERBOUGHT_RE = re.compile(r"overbought", re.IGNORECASE)
_OVERSOLD_RE = re.compile(
    r"oversold"
    r"|rsi\s*<?\s*30"
    r"|rsi\s+(?:has\s+)?crossed\s+(?:below|under)\s*30"
    r"|\bis\s+down\s+significantly\b"
    r"|rocket\s*fuel\s*buy\s*signal"
    r"|nitro\s+watchlist\s*[\u2014\-]\s*buy\s+signal",
    re.IGNORECASE,
)
_SKIP_WORDS = {
    "RSI",
    "DMA",
    "BUY",
    "SELL",
    "NOT",
    "THE",
    "HAS",
    "NOW",
    "AND",
    "FOR",
    "ARE",
    "ALL",
    "NEW",
    "GET",
    "SET",
    "ADD",
    "WARNING",
    "ALERT",
    "CROSS",
    "OVER",
    "UNDER",
    "ROCKET",
    "FUEL",
    "SIGNAL",
    "MOMENTUM",
    "REVERSAL",
    "DEATH",
    "GOLDEN",
    "OVERBOUGHT",
    "OVERSOLD",
    "PRICE",
    "HIGH",
    "LOW",
    "OPEN",
    "CLOSE",
    "STOP",
    "COVERING",
    "SELLERS",
    "TERM",
    "UNDERWAY",
    "STEPPING",
    "LOSS",
    "TAKE",
    "PROFIT",
    "EXIT",
    "ENTRY",
}


def _normalize_author_ids(author_ids: Iterable[str] | None) -> set[str] | None:
    """Normalize a trusted-author allowlist; None means "no gate configured"."""
    if author_ids is None:
        return None
    return {str(author_id).strip().lower() for author_id in author_ids if str(author_id).strip()}


def _safe_message_id_int(message_id: str) -> int | None:
    try:
        return int(message_id)
    except ValueError:
        return None


def _is_discord_auth_error(exc: BaseException | None) -> bool:
    if exc is None:
        return False
    name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    if "loginfailure" in name:
        return True
    return any(
        marker in message
        for marker in (
            "improper token has been passed",
            "invalid token",
            "token is invalid",
            "unauthorized",
            "401",
            "authentication failed",
        )
    )


def _normalize_symbol_candidate(candidate: str) -> str | None:
    normalized = candidate.strip().strip("*$").upper()
    if not normalized or normalized in _SKIP_WORDS:
        return None
    return normalized


def _has_leading_sell_directive(content: str) -> bool:
    """True for "SELL EURUSD", false for "short covering underway".

    The captured word goes through the same normalizer as every other symbol
    candidate, which is why COVERING and friends are in _SKIP_WORDS. Position
    alone cannot settle this: "short covering underway" also starts with "short".
    """
    match = _LEADING_SELL_DIRECTIVE_RE.match(content)
    if match is None:
        return False
    return _normalize_symbol_candidate(match.group("target")) is not None


def _first_normalized_symbol(matches: Iterable[str]) -> str | None:
    for matched in matches:
        candidate = _normalize_symbol_candidate(matched)
        if candidate:
            return candidate
    return None


def _extract_bold_symbol(content: str) -> str | None:
    # Accept both "** $TICKER**" and "**TICKER**". The signal bots are not
    # consistent about the space, and requiring one silently dropped every
    # Nitro alert, which formats its ticker as "**BE** is down significantly".
    # The capture is letters-only and immediately followed by "**", so a
    # multi-word bold header such as "**NITRO WATCHLIST — BUY SIGNAL**" cannot
    # match.
    return _first_normalized_symbol(
        re.findall(r"\*\*\s*\$?([A-Za-z]{1,10})\s*\*\*", content)
    )


def _extract_labeled_symbol(content: str) -> str | None:
    for pattern in (
        r"(?:ticker|symbol|stock)\s*[:\-]\s*\**\$?([A-Za-z]{1,10})\**",
        r"(?:for|on)\s+\**\$?([A-Za-z]{1,10})\**",
    ):
        symbol = _first_normalized_symbol(
            matched.group(1) for matched in re.finditer(pattern, content, re.IGNORECASE)
        )
        if symbol:
            return symbol
    return None


def _extract_action_symbol(content: str) -> str | None:
    action_symbol = re.search(
        r"(?i:\b(?:buy|sell|long|short)\b)\s+"
        r"(?:(?i:signal)\s+)?\**\$?([A-Z]{1,10})\**\b",
        content,
    )
    if action_symbol:
        return _normalize_symbol_candidate(action_symbol.group(1))
    return None


def _extract_dollar_symbol(content: str) -> str | None:
    return _first_normalized_symbol(re.findall(r"\$([A-Za-z]{1,10})\b", content))


def _price_prefixed_symbol(content: str) -> str | None:
    ticker_price = re.search(r"\b([A-Za-z]{1,10})\s+\$\d+", content)
    if ticker_price:
        return _normalize_symbol_candidate(ticker_price.group(1))
    return None


def _extract_symbol(content: str) -> str | None:
    for extract_stage in (
        _extract_bold_symbol,
        _extract_labeled_symbol,
        _extract_action_symbol,
        _extract_dollar_symbol,
        _price_prefixed_symbol,
    ):
        symbol = extract_stage(content)
        if symbol:
            return symbol
    return None


def _extract_price(content: str) -> float | None:
    """Extract the first plausible price from a Discord message.

    Thousands separators are normalized to the decimal point they actually are
    ("1,234.56" -> 1234.56); a comma that does not form a strict
    thousands group (e.g. "$12,34") leaves the candidate malformed and is
    rejected rather than reinterpreted as a decimal point, which used to turn
    $1,234 into a 1000x-wrong entry price.
    """
    for matcher in (_PRICE_LABEL_RE, _DOLLAR_PRICE_RE, _AT_PRICE_RE):
        matched = matcher.search(content)
        if matched is None:
            continue
        raw_price = matched.group("price")
        if not _is_well_formed_price_token(raw_price):
            continue
        try:
            price = float(raw_price.replace(",", ""))
        except ValueError:
            continue
        if price <= 0:
            continue
        return price
    return None


def _is_well_formed_price_token(raw_price: str) -> bool:
    """Reject tokens where a comma splits digits outside strict thousands groups
    ("12,34"); accepting them as a smaller number corrupts sizing."""
    if "," not in raw_price:
        return True
    whole = raw_price.split(".", 1)[0]
    parts = whole.split(",")
    if len(parts[0]) > 3 or not parts[0].isdigit():
        return False
    return all(len(part) == 3 and part.isdigit() for part in parts[1:])


# A negation between here and the action word cancels the instruction.
#
# This is the buy-side mirror of _SELL_AS_NOUN_RE, and it was missing: the buy
# predicate only ever checked that a ticker followed the action word, so "this
# is not a buy signal", "I should not long here" and "Do not buy this yet" all
# opened a long. The predicate's own docstring claimed "do not buy" did not read
# as a signal, and it did.
#
# `[^.!?\n,;]` is load-bearing. A negation in one clause must not cancel an
# instruction in the next: "do not sell, buy instead" is an instruction to buy,
# and a window that crossed the comma turned it into no signal at all.
_BUY_NEGATED_RE = re.compile(
    r"\b(?:do\s*n[o']?t|don'?t|never|should\s*n[o']?t|shouldn'?t|wouldn'?t|"
    r"would\s*n[o']?t|can'?t|cannot|won'?t|avoid|wait\s+on|not)\b"
    r"[^.!?\n,;]{0,30}?\b(?:buy|long|enter|add)\b",
    re.IGNORECASE,
)


def _reads_as_buy_directive(content: str) -> bool:
    """True when the message issues a buy, rather than merely mentioning one.

    Same hazard as the sell side, in the direction that opens a position: matched
    bare, "could buy here" and "do not buy" both read as a BUY signal. A negated
    instruction parsed as a trade is the clearest failure this parser can make.

    The ticker requirement is what separates "BUY AAPL" (a signal, and what the
    real RamJet channel emits) from "could buy here" (commentary). The captured
    word goes through _normalize_symbol_candidate so it obeys the same _SKIP_WORDS
    list as every other symbol extractor - duplicating that knowledge in a regex
    would let the two drift.
    """
    if _ACTION_BUY_RE.search(content) is None:
        return False
    if _BUY_NEGATED_RE.search(content):
        return False
    explicit = re.search(
        r"\b(?:buy|long)\s+signal\b", content, re.IGNORECASE
    )
    if explicit:
        return True
    match = _ACTION_BUY_TICKER_RE.search(content)
    if match is None:
        return False
    if _normalize_symbol_candidate(match.group(1)) is None:
        return False
    # A real entry carries a price, and the parser needs one anyway - without it
    # the message is discarded two lines later. Keying on that is what separates
    # "BUY AAPL @ 100.0" from "could buy here": a stop-word list cannot enumerate
    # every English word that might follow "buy", but a price is unambiguous.
    # Deliberately structural rather than a longer word list, which would grow
    # with every phrasing someone happens to use.
    return _extract_price(content) is not None


def _reads_as_sell_directive(content: str) -> bool:
    """True when the message issues a sell, rather than merely mentioning one.

    A noun phrase ("sellers are weak", "short covering") describes the market;
    only a directive ("SELL EURUSD", "sell signal") is an instruction. Keeping
    this in one place matters because the detector and the action resolver both
    need the same verdict - when they disagreed, a buy message with "sellers" in
    it was typed as a sell.
    """
    if _SELL_AS_NOUN_RE.search(content):
        # The noun form only disqualifies an incidental match. An explicit
        # "sell signal" alongside it is still an instruction.
        if _EXPLICIT_SELL_SIGNAL_RE.search(content):
            return True
        # "SELL EURUSD, sellers stepping in" is a directive: the message opens
        # with the action and the noun phrases only describe the market
        # afterwards. Vetoing those dropped real exit signals, which costs as
        # much as the false positives this guard exists to stop.
        if _has_leading_sell_directive(content):
            return True
        return False
    if _has_leading_sell_directive(content):
        return True
    return bool(_ACTION_SELL_RE.search(content))


def _is_ramjet_signal(content: str) -> bool:
    return bool(_RAMJET_OVERSOLD_RE.search(content) and _BUY_SIGNAL_RE.search(content))


def _detect_rocket_fuel_signal_type(content: str) -> str | None:
    if _MOMENTUM_REVERSAL_RE.search(content) or _reads_as_sell_directive(content):
        return "momentum_reversal"
    if _GOLDEN_CROSS_RE.search(content):
        return "golden_cross"
    if _DEATH_CROSS_RE.search(content):
        return "death_cross_warning"
    if _OVERBOUGHT_RE.search(content):
        return "overbought_alert"
    if _OVERSOLD_RE.search(content):
        return "oversold_buy"
    return None


def _resolve_action(content: str, *, signal_type: str | None) -> str | None:
    if signal_type in {"overbought_alert", "death_cross_warning"}:
        return None
    # A buy-side negation vetoes the inferred BUY before it is applied.
    #
    # The signal_type branch used to return "buy" straight away, so the negation
    # guard never ran for it: "**EURUSD** is now oversold / Do not buy this yet"
    # resolved to a long, because "oversold" was enough on its own. An explicit
    # instruction not to trade is stronger evidence than an inferred reading of
    # the same message, and it is the direction that opens a position.
    #
    # Scoped to the BUY side on purpose. An earlier version delegated to
    # `_reads_as_sell_directive` to preserve an exit, and that defeated the exit
    # it was protecting: "momentum reversal ... do not buy, sellers in control"
    # dropped to no signal at all, because the noun guard vetoes "sellers" before
    # the momentum-reversal alternative is reached. A position that should be
    # closed stayed open. Refusing to open is the safe direction; refusing to
    # exit is not.
    if signal_type in {"momentum_reversal"}:
        # Checked before the buy-side negation veto, and deliberately not gated by
        # it. "momentum reversal ... do not buy" is an instruction to close, and a
        # clause like "sellers in control" is commentary about it. Dropping those
        # would leave a position open, which is the one outcome that is not the
        # safe direction: refusing to open is conservative, refusing to exit is
        # not.
        return "sell"
    if _BUY_NEGATED_RE.search(content):
        return None
    if signal_type in {"oversold_buy", "golden_cross"}:
        return "buy"
    if _reads_as_sell_directive(content):
        return "sell"
    if _reads_as_buy_directive(content):
        return "buy"
    return None


def _parse_timestamp(raw_timestamp: str | None) -> datetime | None:
    """Read a Discord timestamp, or ``None`` when it cannot be read.

    Fail closed. This used to return ``datetime.now(timezone.utc)`` for both a
    missing *and* an unparseable value, which silently stamped every signal as
    "received just now": an unparseable timestamp therefore made a message
    look brand new, defeating every age check downstream
    (``signal_is_fresh`` and the daemon's pre-wait age gate both read
    ``received_at``). An age that cannot be established is unknown, not zero,
    so the caller must decide what to do -- and the callers omit
    ``received_at`` entirely so the gate refuses visibly instead of trading on
    an assumed age.
    """
    if not raw_timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


# Consecutive author-rejected messages before the service is called unhealthy.
# Low enough that a single impersonation attempt cannot fail a healthy bot, high
# enough that a systematically wrong allowlist is caught on the first burst of
# real messages rather than days later.
#: Statuses only an operator sets. This source reports status on a best-effort basis and must
#: never move a service out of one of them (connect() used to write RUNNING over STOPPED).
_OPERATOR_SERVICE_STATUSES = frozenset({ServiceStatus.PAUSED, ServiceStatus.STOPPED})
_OPERATOR_SERVICE_STATUS_VALUES = frozenset(
    status.value for status in _OPERATOR_SERVICE_STATUSES
)


def _service_status_value(status: ServiceStatus | str) -> str:
    return str(status.value if isinstance(status, ServiceStatus) else status).strip().lower()


AUTHOR_REJECTION_STREAK_THRESHOLD = 3

# A coroutine produced by a sync caller. The running loop is the daemon's, so the
# work still happens; with no loop (a synchronous test) it is discarded with a
# clear message rather than raising "never awaited".
_observed_tasks: set[asyncio.Task[Any]] = set()


def _schedule(coro: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        logger.warning(
            "Author-rejection observer produced work with no running loop; "
            "discarded (synchronous caller)"
        )
        return
    task = loop.create_task(coro)
    _observed_tasks.add(task)
    task.add_done_callback(_observed_tasks.discard)


def _resolve_message_author_id(message: Any) -> str:
    """The author id of a message, lowercased, or "" when it cannot be read."""
    return str(getattr(getattr(message, "author", None), "id", "") or "").strip().lower()


def _resolve_message_channel_id(message: Any) -> str:
    """The channel id of a message, or "" when it cannot be read."""
    return str(getattr(getattr(message, "channel", None), "id", ""))


def _reject_untrusted_author(
    message: Any,
    author_id: str,
    on_author_rejection: Callable[[str], Awaitable[None] | None] | None,
) -> None:
    """Record and log one message refused by the author allowlist.

    Extracted from the parser so the gate itself reads as one decision. The two
    halves are deliberately ordered and both are load-bearing: a caller that can
    only schedule (the parser is synchronous) must still have the rejection
    counted before the message is dropped, or a burst of impersonation attempts
    leaves no trace an operator can act on.
    """
    if on_author_rejection is not None:
        result = on_author_rejection(author_id or "<missing>")
        if inspect.isawaitable(result):
            _schedule(result)
    logger.warning(
        "Discord message rejected: author is not a trusted signal source",
        extra={
            "data": {
                "discord_author_id": author_id,
                "discord_channel_id": _resolve_message_channel_id(message),
            }
        },
    )


def _build_signal_metadata(
    message: Any,
    content: str,
    *,
    signal_type: str | None,
    default_timeframe: str,
) -> dict[str, Any]:
    """The provenance the daemon needs to age-check and audit one parsed signal."""
    metadata: dict[str, Any] = {
        "source": "discord",
        "discord_signal_type": signal_type or "generic",
        "discord_message_id": str(getattr(message, "id", "")),
        "discord_channel_id": _resolve_message_channel_id(message),
        "discord_author_id": str(getattr(getattr(message, "author", None), "id", "")),
        "discord_content": content,
        "timeframe": normalize_timeframe(default_timeframe) or default_timeframe,
    }
    created_at = getattr(message, "created_at", None)
    raw_timestamp = created_at.isoformat() if isinstance(created_at, datetime) else None
    timestamp = _parse_timestamp(raw_timestamp)
    if timestamp is not None:
        metadata["received_at"] = timestamp.isoformat()
    else:
        # No fabricated receipt time. The key is left absent so the daemon's
        # pre-wait age gate -- which knows it is on the Discord path and so
        # requires the key -- refuses with an operator-visible warning rather
        # than trading a message whose age nobody can establish.
        metadata["discord_received_at_unreadable"] = True
    return metadata


def _parse_trade_signal_from_message(
    message: Any,
    *,
    default_timeframe: str,
    trusted_author_ids: Iterable[str] | None = None,
    on_author_rejection: Callable[[str], Awaitable[None] | None] | None = None,
    on_author_accepted: Callable[[], Any] | None = None,
) -> TradeSignal | None:
    """Parse a Discord message into a trade signal.

    The author/bot-ID check is the source gate (campaign Decision Record):
    when a trusted-author set is configured, only messages authored by one of
    those IDs may become a signal. Every other author — including channel
    members impersonating the signal bot — is dropped.
    """
    content = str(getattr(message, "content", "") or "").strip()
    if not content:
        return None

    trusted = _normalize_author_ids(trusted_author_ids)
    if trusted is not None:
        author_id = _resolve_message_author_id(message)
        if not author_id or author_id not in trusted:
            _reject_untrusted_author(message, author_id, on_author_rejection)
            return None

    if on_author_accepted is not None:
        on_author_accepted()

    signal_type = None
    if _is_ramjet_signal(content):
        # Still subject to the negation guard. This branch sets the action
        # outright, so "oversold buy signal - do not buy this yet" was a long on
        # the RamJet path while the RocketFuel path correctly refused it. Two bots
        # reading the same message in opposite directions is exactly the
        # inconsistency the one shared predicate exists to prevent.
        if _BUY_NEGATED_RE.search(content):
            return None
        action = "buy"
        signal_type = "ram_jet_oversold_buy"
    else:
        signal_type = _detect_rocket_fuel_signal_type(content)
        action = _resolve_action(content, signal_type=signal_type)  # type: ignore[assignment]  # name reused with a wider type across branches
    if action is None:
        return None

    symbol = _extract_symbol(content)
    price = _extract_price(content)
    if not symbol or price is None or price <= 0:
        return None

    metadata = _build_signal_metadata(
        message,
        content,
        signal_type=signal_type,
        default_timeframe=default_timeframe,
    )
    return TradeSignal(
        action=action,  # type: ignore[arg-type]  # value validated at runtime / narrowed by an earlier guard
        symbol=symbol,
        entry_price=float(price),
        stop_loss=None,
        take_profit=None,
        timeframe=str(metadata["timeframe"]),
        metadata=metadata,
    )


class _DiscordMessageAdapter:
    """Minimal adapter exposing the attributes the parser relies on.

    ``created_at`` is ``None`` when the message carries no readable timestamp;
    the parser turns that into a signal with no ``received_at`` rather than a
    fabricated one.
    """

    __slots__ = ("content", "id", "created_at", "channel", "author")

    def __init__(self, message: dict[str, Any]) -> None:
        self.content = str(message.get("content") or "")
        self.id = str(message.get("id"))
        self.created_at = _parse_timestamp(message.get("timestamp"))
        self.channel = SimpleNamespace(id=str(message.get("channel_id")))
        author = message.get("author") or {}
        self.author = SimpleNamespace(id=str(author.get("id")))


def _resolve_auth_scheme(bot_name: str, auth_scheme: str | None) -> str:
    """Resolve and validate the credential scheme for one Discord bot.

    Returns the scheme name from ``DISCORD_AUTH_SCHEMES``; raises ``ValueError``
    for anything else.

    An unrecognised scheme must never fall through to "raw" or "bot" and poll with
    the wrong credential, so this raises rather than defaulting. Kept out of
    ``DiscordSignalSource.__init__`` because the precedence chain, the vocabulary
    and the refusal are one decision, and reading them interleaved with attribute
    assignment hid that.
    """
    per_bot_scheme_key = discord_bot_env_key(bot_name, "AUTH_SCHEME")
    scheme = str(
        auth_scheme
        or os.environ.get(per_bot_scheme_key, "")
        or os.environ.get("DISCORD_AUTH_SCHEME", "")
        or DEFAULT_DISCORD_AUTH_SCHEME
    ).strip().lower()
    if scheme not in DISCORD_AUTH_SCHEMES:
        raise ValueError(
            f"Unsupported Discord auth_scheme '{scheme}': unknown "
            f"discord_auth_scheme; expected one of "
            f"{sorted(DISCORD_AUTH_SCHEMES)}"
        )
    if scheme in DISCORD_ACCOUNT_AUTH_SCHEMES:
        logger.warning(
            "Discord source configured with an ACCOUNT (user) token. That is a "
            "self-bot: it violates Discord's Terms of Service and can get the "
            "account terminated. Use a Developer Portal bot token for unattended "
            "trading.",
            extra={"data": {"bot_name": bot_name, "auth_scheme": scheme}},
        )
    return scheme


class DiscordSignalSource(BaseSignalSource):
    def __init__(
        self,
        *,
        bot_name: str = "discord",
        token: str | None = None,
        channel_id: str | None = None,
        channel_ids: set[str] | None = None,
        timeframe: str = "1h",
        service_id: str | None = None,
        trusted_author_ids: Iterable[str] | str | None = None,
        auth_scheme: str | None = None,
    ) -> None:
        normalized_name = str(bot_name or "discord").strip()
        self.bot_name = normalized_name or "discord"
        self.service_id = str(service_id or "").strip() or None
        self.source_name = f"discord_{self.bot_name.lower()}"
        self.token = (token or os.environ.get("DISCORD_BOT_TOKEN", "")).strip()
        # Discord accepts two different credentials and they are NOT
        # interchangeable: an application/bot token needs the "Bot " prefix, while
        # a user (account) token must be sent raw. Either mismatch is answered
        # with HTTP 401, so the scheme is explicit and never guessed, and a
        # wrong scheme fails the source closed rather than polling as anonymous.
        #
        # Vocabulary (union of both sides, same two credential kinds):
        #   "bot"             -> Developer Portal bot token, sent "Bot <token>"
        #   "raw" / "account" -> user (account/self-bot) token, sent verbatim.
        # "raw" is the operator spelling from #81 (tokens copied out of Discord
        # devtools), "account" the explicit one; both name the same credential.
        # Precedence: explicit argument -> per-bot env -> global env -> default,
        # so a per-bot scheme can never be shadowed by a global one.
        self.auth_scheme = _resolve_auth_scheme(self.bot_name, auth_scheme)
        resolved_channel_ids: set[str] = set(channel_ids or set())
        fallback_channel_id = str(channel_id or os.environ.get("DISCORD_CHANNEL_ID", "")).strip()
        if fallback_channel_id:
            resolved_channel_ids.add(fallback_channel_id)
        self.channel_ids = {value for value in resolved_channel_ids if value}
        self.timeframe = normalize_timeframe(timeframe) or timeframe
        # Decision Record: ingestion trusts one explicitly configured bot/author
        # ID set; the author check is the source gate. No silent open gate.
        if trusted_author_ids is None:
            raise ValueError(
                "DiscordSignalSource requires an explicit trusted_author_ids "
                "allowlist; refusing to ingest signals from unverified authors"
            )
        if isinstance(trusted_author_ids, str):
            trusted_author_ids = {trusted_author_ids}
        self.trusted_author_ids = _normalize_author_ids(trusted_author_ids) or set()
        if not self.trusted_author_ids:
            raise ValueError(
                "DiscordSignalSource requires a non-empty trusted_author_ids "
                "allowlist; refusing to ingest signals from unverified authors"
            )
        self._queue: asyncio.Queue[TradeSignal] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._connected = False
        self._paused = False
        self._run_task: asyncio.Task[None] | None = None
        self._seen_ids: dict[str, set[str]] = {}
        self._last_seen_message_ids: dict[str, str] = {}
        # Author rejections, and the distinct authors that produced them.
        #
        # A mis-configured allowlist is otherwise silent: every message is
        # rejected with a per-message WARNING, the service still reports
        # connected, and the deployment still reports ACTIVE while never trading.
        # That is indistinguishable from a strategy that stopped firing - the
        # same signature as the market-calendar staleness budget and the stray
        # `return` fixed elsewhere in this branch. Counting them gives an
        # operator something to check, and lets the daemon log it once instead of
        # once per message.
        self._author_rejection_count = 0
        self._rejected_author_ids: set[str] = set()
        self._consecutive_author_rejections = 0
        self._author_rejection_alerted = False

    def _record_author_rejection(self, author_id: str) -> Awaitable[None] | None:
        """Count the rejection synchronously; only the status write is deferred.

        The caller is a synchronous parser, so it can only *schedule* an
        observer. Doing the counting inside the coroutine made the streak race
        the loop: three rapid messages all read a streak of zero, because none of
        the scheduled bodies had run yet. Counting here - in the caller's own
        frame - makes the state immediately consistent, and only the DB write is
        allowed to lag.
        """
        self._author_rejection_count += 1
        self._consecutive_author_rejections += 1
        self._rejected_author_ids.add(str(author_id))
        if self._consecutive_author_rejections >= AUTHOR_REJECTION_STREAK_THRESHOLD:
            # The decision is made HERE, in the caller's frame, and the streak is
            # latched here too. The coroutine below runs on a later pass of the
            # event loop, and by then `_record_author_accepted` may have reset the
            # latch - so re-testing `_author_rejection_alerted` inside it flagged a
            # service FAILED for a streak that no longer existed, stopping a bot
            # that had just accepted a message from its real author. That is the
            # allowlist proving itself correct and being failed for it.
            if self._author_rejection_alerted:
                return None
            self._author_rejection_alerted = True
            consecutive = self._consecutive_author_rejections
            return self._flag_author_rejection_streak(consecutive_rejections=consecutive)
        return None

    def _record_author_accepted(self) -> None:
        """An accepted message proves the allowlist is right for this bot.

        It ends any rejection streak and un-latches the failure alert, so
        scattered impersonation attempts across a day can never accumulate into a
        false alarm.

        When the alert had latched, the service was left FAILED and nothing ever
        put it back: recovery only happens in `_recover_failed_services`, which
        runs on daemon *start*. So an operator who fixed the allowlist saw the
        service stay red on the dashboard until they restarted something, with
        no record that it had healed. The status write is scheduled for the same
        reason the rejection write is - the caller is a synchronous parser - and
        the latch is cleared first, so this fires at most once per streak.
        """
        was_alerted = self._author_rejection_alerted
        self._consecutive_author_rejections = 0
        self._author_rejection_alerted = False
        if was_alerted:
            _schedule(
                self._record_author_recovery(
                    rejected_count=self._author_rejection_count,
                    rejected_author_ids=sorted(self._rejected_author_ids),
                )
            )
            self._author_rejection_count = 0
            self._rejected_author_ids = set()

    async def _record_author_recovery(
        self,
        *,
        rejected_count: int,
        rejected_author_ids: list[str],
    ) -> None:
        """Log that a trusted author was accepted after a rejection alert.

        Log only: the alert never changes the service status (see
        `_flag_author_rejection_streak`), so there is nothing to clear. The rejected
        authors go in the log because they are what the operator needs to check the
        allowlist against.
        """
        logger.info(
            "Discord bot accepted a message from a trusted author; the author "
            "allowlist is working again after a run of rejections",
            extra={
                "data": {
                    "service_id": str(self.service_id or ""),
                    "bot_name": str(self.bot_name or ""),
                    "rejected_before_recovery": rejected_count,
                    "rejected_author_ids": rejected_author_ids,
                }
            },
        )

    async def _flag_author_rejection_streak(self, *, consecutive_rejections: int) -> None:
        """Note that a message was dropped because its author is not trusted.

        Deliberately not logging each rejection here: the caller already logs
        every one, and a busy channel would drown the log. This exists so the
        *pattern* - every message rejected - is visible.

        Once a run of consecutive rejections clears the threshold an ERROR alert is
        logged (once per streak) with the rejected author ids attached, so a wrong
        allowlist does not look like a strategy that simply stopped firing. The
        service status is NOT changed: the rejected authors are untrusted by
        definition, and letting them flip the status would let any channel member
        take the bot out of service.

        `consecutive_rejections` is passed in rather than read from self, because
        by the time this runs the loop may have already accepted a message and
        reset the streak. The count reported is the one that triggered the alert.
        """
        logger.error(
            "Discord bot rejected every recent message for authorship; "
            "the trusted_author_ids allowlist is probably wrong for this bot",
            extra={
                "data": {
                    "service_id": str(self.service_id or ""),
                    "bot_name": str(self.bot_name or ""),
                    "rejected_count": self._author_rejection_count,
                    "consecutive_rejections": consecutive_rejections,
                    "rejected_author_ids": sorted(self._rejected_author_ids),
                    "trusted_author_ids": sorted(self.trusted_author_ids),
                }
            },
        )
        # Deliberately NOT marking the service FAILED. The rejected messages come from
        # authors the allowlist does not trust - by definition untrusted channel members -
        # so letting three of them flip the service status would hand any member of the
        # channel the ability to take the bot out of service (the daemon stops
        # every discord service that is not RUNNING). The refusal itself is already correct; this alert is the signal for
        # the operator, who checks the logged `rejected_author_ids` against the allowlist.

    def author_rejection_summary(self) -> dict[str, Any]:
        """How many messages were dropped for authorship, and by whom.

        An operator who has just set ``DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS``
        needs to know whether the value is right. A non-zero count with a single
        distinct author usually means the allowlist is wrong for that bot, and
        that bot is currently refusing every signal it receives.
        """
        return {
            "rejected_count": int(self._author_rejection_count),
            "rejected_author_ids": sorted(self._rejected_author_ids),
        }

    @property
    def is_connected(self) -> bool:
        return self._connected and self._run_task is not None and not self._run_task.done()

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def _set_service_status(
        self,
        status: ServiceStatus | str,
        *,
        last_activity: datetime | None = None,
    ) -> None:
        if not self.service_id:
            return
        # Never overwrite PAUSED or STOPPED. Those are operator intent, and it has to
        # survive whatever the process last observed - a message arriving after the
        # pause, a connector failing, an author allowlist turning out to be wrong.
        #
        # The guard originally covered only RUNNING, which left the nastier case
        # open: within a sync tick of the operator pausing a service, a
        # FAILED write landed on the PAUSED row. `_recover_failed_services` then
        # found FAILED - not PAUSED - and reset it to RUNNING on the next daemon
        # start, so the process had undone a decision the operator had just made.
        # A status that an inbound message can flip is not a record of intent.
        try:
            # The PAUSED read is inside the try, not merely next to it. A
            # transient database error here would otherwise propagate out of a
            # method whose whole job is to report status without failing, and
            # take the caller's loop with it - this is called from the poll path,
            # so an unreachable database would stop the bot rather than just
            # leaving a status unwritten. Found by the CodeRabbit review.
            current = await asyncio.to_thread(get_service, self.service_id)
            if (
                current is not None
                and current.status in _OPERATOR_SERVICE_STATUSES
                and _service_status_value(status) not in _OPERATOR_SERVICE_STATUS_VALUES
            ):
                return
            # `respect_operator_intent` repeats the check inside the write itself, so a
            # PAUSE/STOP that commits between the read above and this write still wins.
            await asyncio.to_thread(
                update_service_status,
                self.service_id,
                status,
                last_activity=last_activity,
                respect_operator_intent=True,
            )
        except Exception as exc:
            logger.warning(
                "Failed to update discord service status",
                extra={
                    "data": {
                        "service_id": self.service_id,
                        "status": str(status),
                        "error": str(exc),
                    }
                },
            )

    def _auth_header(self) -> str:
        """Build the Authorization header for the configured credential type.

        A token that already carries a scheme prefix is left alone so operators
        can paste a fully-formed header value. A credential/scheme mismatch is
        NOT auto-corrected: Discord answers 401 and
        :class:`DiscordAuthenticationError` stops the source.
        """
        token = str(self.token or "").strip()
        if self.auth_scheme in DISCORD_BOT_AUTH_SCHEMES:
            if token.lower().startswith("bot "):
                return token
            return f"Bot {token}"
        # Account (self-bot) token, both spellings: sent exactly as supplied.
        return token

    #: Branch-side spelling of :meth:`_auth_header`; both names are kept so
    #: either call site keeps working.
    def _authorization_header(self) -> str:
        """Alias of :meth:`_auth_header`."""
        return self._auth_header()

    async def observe_channel_authors(
        self,
        channel_id: str,
        *,
        pages: int = 3,
    ) -> list[dict[str, Any]]:
        """Read recent messages and report who authored them.

        Read-only, and deliberately outside the trading path: it builds no
        signal, mutates no state, and submits nothing. It exists because
        ``DISCORD_BOT_<NAME>_TRUSTED_AUTHOR_IDS`` is operator-set and the mapping
        from author id to bot is not derivable from the repository - guessing it
        wrong refuses that bot's real messages and silently stops it trading.

        So the value can be *observed* rather than guessed. Note this is a
        historical message list, not the configured allowlist: a human or an
        unrelated bot posting in the same channel will appear here too, which is
        exactly why the allowlist is an operator decision rather than something
        this should infer and apply by itself.

        Returns one row per distinct author, most active first. ``is_bot`` is
        included because signal bots post as bots and a human interjection should
        not be trusted even when it is the loudest voice in the channel.
        """
        counts: dict[str, dict[str, Any]] = {}
        before: str | None = None
        for _ in range(max(1, int(pages))):
            messages = await self._fetch_messages(channel_id, before=before)
            if not messages:
                break
            for message in messages:
                # `_fetch_messages` returns the raw REST dicts; the ingest path
                # wraps them in _DiscordMessageAdapter, this path does not need
                # to. Reading them as dicts is what the source actually returned.
                author = message.get("author") or {}
                author_id = str(author.get("id") or "").strip().lower()
                if not author_id:
                    continue
                row = counts.setdefault(
                    author_id,
                    {
                        "author_id": author_id,
                        "username": str(author.get("username", "") or ""),
                        "is_bot": bool(author.get("bot", False)),
                        "message_count": 0,
                    },
                )
                row["message_count"] += 1
            oldest = min(
                (str(m.get("id", "")) for m in messages if m.get("id")),
                default="",
            )
            before = oldest or None
            if not before:
                break
        return sorted(
            counts.values(), key=lambda row: (-row["message_count"], row["author_id"])
        )

    async def _fetch_messages(
        self,
        channel_id: str,
        *,
        before: str | None = None,
        after: str | None = None,
        limit: int = POLL_LIMIT,
    ) -> list[dict[str, Any]]:
        headers = {"Authorization": self._auth_header()}
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit={limit}"
        if before:
            url += f"&before={before}"
        if after:
            url += f"&after={after}"

        def _do_request() -> requests.Response:
            return requests.get(url, headers=headers, timeout=10)

        try:
            response = await asyncio.to_thread(_do_request)
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "Discord REST request failed",
                extra={
                    "data": {
                        "bot_name": self.bot_name,
                        "service_id": self.service_id,
                        "channel_id": channel_id,
                        "error": str(exc),
                    }
                },
            )
            return []

        if response.status_code == 401:
            raise DiscordAuthenticationError(
                f"Discord auth failed (401) for {channel_id}"
            )
        if response.status_code == 403:
            # B2: 403 is per-channel (missing access), not a dead token —
            # skip the channel, never kill the whole source.
            raise DiscordChannelForbiddenError(
                channel_id, f"Discord channel forbidden (403) for {channel_id}"
            )
        if response.status_code == 429:
            try:
                retry_after = float(response.json().get("retry_after", 5.0))
            except (ValueError, AttributeError, TypeError):
                retry_after = 5.0
            logger.warning(
                "Discord REST rate limited",
                extra={
                    "data": {
                        "bot_name": self.bot_name,
                        "service_id": self.service_id,
                        "channel_id": channel_id,
                        "retry_after": retry_after,
                    }
                },
            )
            raise DiscordRateLimitError(channel_id, retry_after)
        if response.status_code != 200:
            logger.warning(
                "Discord REST returned non-200",
                extra={
                    "data": {
                        "bot_name": self.bot_name,
                        "service_id": self.service_id,
                        "channel_id": channel_id,
                        "status_code": response.status_code,
                    }
                },
            )
            return []

        try:
            body = response.json()
        except ValueError as exc:
            logger.warning(
                "Discord REST returned non-JSON body",
                extra={"data": {"channel_id": channel_id, "error": str(exc)}},
            )
            return []

        return body if isinstance(body, list) else []

    def _page_reaches_cursor(
        self, messages: list[dict[str, Any]], last_seen: str
    ) -> bool:
        """Whether a newest-first page reaches back to the tracked cursor."""
        if not last_seen:
            return True
        last_int = _safe_message_id_int(last_seen)
        if last_int is None:
            return True
        for message in messages:
            oldest_int = _safe_message_id_int(str(message.get("id")))
            if oldest_int is not None and oldest_int <= last_int:
                return True
        return False

    async def _poll_once(self, channel_id: str, *, emit: bool = True) -> None:
        first_page = await self._fetch_messages(channel_id)
        if not first_page:
            return
        seen = self._seen_ids.setdefault(channel_id, set())
        last_seen = self._last_seen_message_ids.get(channel_id, "")
        # B2: bursts beyond POLL_LIMIT (and 429-windows) must not lose
        # signals incl. exits — paginate backwards with `before` until a
        # page overlaps the tracked cursor (bounded by MAX_BACKFILL_PAGES).
        # A short page proves Discord returned everything (no gap), so only
        # full pages trigger another fetch — otherwise REST traffic doubles
        # on every quiet poll straight into the next 429.
        pages = [first_page]
        while (
            len(pages[-1]) >= POLL_LIMIT
            and not self._page_reaches_cursor(pages[-1], last_seen)
            and len(pages) <= MAX_BACKFILL_PAGES
        ):
            oldest = str(pages[-1][-1].get("id"))
            if _safe_message_id_int(oldest) is None:
                break
            older = await self._fetch_messages(channel_id, before=oldest)
            if not older:
                break
            pages.append(older)
        # Messages arrive newest-first; iterate oldest page first, reversed,
        # so the queue receives them in chronological (oldest -> newest) order.
        for page in reversed(pages):
            for message in reversed(page):
                await self._emit_polled_message(channel_id, message, seen, emit=emit)
        newest = str(first_page[0].get("id"))
        self._log_page_gap(channel_id, last_seen, first_page)
        # B2: poison-message guard — a non-numeric id must never crash the
        # cursor update into a tight crash loop (seen-set dedupes anyway).
        newest_int = _safe_message_id_int(newest)
        last_int = _safe_message_id_int(last_seen)
        if not last_seen or (
            newest_int is not None and (last_int is None or newest_int > last_int)
        ):
            self._last_seen_message_ids[channel_id] = newest
        self._prune_seen_ids(channel_id, seen)

    async def _emit_polled_message(
        self,
        channel_id: str,
        message: dict[str, Any],
        seen: set[str],
        *,
        emit: bool,
    ) -> None:
        message_id = str(message.get("id"))
        if message_id in seen:
            return
        # Dedupe is the cumulative seen-set's job alone. The monotonic
        # cursor must NOT filter emits: after a burst larger than
        # POLL_LIMIT the cursor sits above messages that were never
        # fetched, and a cursor-based filter would drop them (the exact
        # bug this class exists to prevent). The cursor only seeds `seen`
        # at priming time.
        seen.add(message_id)
        if not emit:
            return
        wrapped = _DiscordMessageAdapter(message)
        parsed = _parse_trade_signal_from_message(
            wrapped,
            default_timeframe=self.timeframe,
            trusted_author_ids=self.trusted_author_ids,
            on_author_rejection=self._record_author_rejection,
            on_author_accepted=self._record_author_accepted,
        )
        if parsed is None:
            return
        parsed.metadata["source"] = self.source_name
        parsed.metadata["discord_bot_name"] = self.bot_name
        # Sonar round: the polled channel is the source of truth — backfill
        # it when the message object carries no channel id of its own.
        parsed.metadata.setdefault("discord_channel_id", channel_id)
        if self.service_id:
            parsed.metadata["service_id"] = self.service_id
        await self._set_service_status(
            ServiceStatus.RUNNING,
            last_activity=datetime.now(timezone.utc),
        )
        await self._queue.put(parsed)

    def _prune_seen_ids(self, channel_id: str, seen: set[str]) -> None:
        """Bound the cumulative seen-set (pre-mortem mitigation).

        The seen-set dedupes emits by itself, so it can NOT be pruned against
        the monotonic cursor: on a quiet channel the newest message id IS the
        cursor, and dropping ids at/below it re-admits that message on the very
        next poll (a duplicate emit). Instead the set is bounded by a retention
        window in message count — wide enough to keep the burst-drain
        guarantee (several consecutive POLL_LIMIT pages) while still bounded.
        Non-numeric ids are always kept: they cannot be ordered against the
        window.
        """
        if len(seen) <= SEEN_ID_RETENTION:
            return
        # Newest-first window; non-numeric ids cannot be ordered against it
        # and are always kept.
        numeric = sorted(
            (message_id for message_id in seen if _safe_message_id_int(message_id)),
            key=lambda message_id: int(message_id),
        )
        survivors = set(numeric[-SEEN_ID_RETENTION:])
        survivors.update(
            message_id
            for message_id in seen
            if _safe_message_id_int(message_id) is None
        )
        seen.intersection_update(survivors)

    def _log_page_gap(
        self,
        channel_id: str,
        last_seen: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Surface skipped history instead of silently skipping it.

        The REST poll fetches at most POLL_LIMIT messages per call. When the
        page does not reach back to the last seen message, messages between
        the page boundary and the cursor were never fetched. The snowflake id
        distance is NOT a message count (ids are not contiguous), so the log
        reports the boundary ids rather than an invented count; ops can query
        Discord's audit log for the exact volume if needed.
        """
        if not last_seen:
            return
        oldest = str(messages[-1].get("id"))
        try:
            if int(oldest) <= int(last_seen):
                # The page overlaps history we already tracked: nothing skipped.
                return
        except ValueError:
            return
        logger.warning(
            "Discord poll gap detected; messages between the last seen id and "
            "the oldest fetched id were not polled",
            extra={
                "data": {
                    "bot_name": self.bot_name,
                    "service_id": self.service_id,
                    "channel_id": channel_id,
                    "last_seen_message_id": last_seen,
                    "oldest_fetched_message_id": oldest,
                    "poll_limit": POLL_LIMIT,
                }
            },
        )

    async def _prime_cursors(self, channel_ids: Iterable[str]) -> None:
        for channel_id in channel_ids:
            if channel_id in self._last_seen_message_ids:
                continue
            try:
                messages = await self._fetch_messages(channel_id)
            except DiscordAuthenticationError:
                raise
            except DiscordChannelForbiddenError as exc:
                logger.warning(
                    "Discord channel skipped at priming (forbidden)",
                    extra={
                        "data": {
                            "bot_name": self.bot_name,
                            "service_id": self.service_id,
                            "channel_id": channel_id,
                            "error": str(exc),
                        }
                    },
                )
                continue
            except Exception as exc:
                logger.warning(
                    "Discord cursor priming failed",
                    extra={
                        "data": {
                            "bot_name": self.bot_name,
                            "service_id": self.service_id,
                            "channel_id": channel_id,
                            "error": str(exc),
                        }
                    },
                )
                continue
            if not messages:
                continue
            seen = self._seen_ids.setdefault(channel_id, set())
            for message in messages:
                message_id = str(message.get("id"))
                seen.add(message_id)
            self._last_seen_message_ids[channel_id] = str(messages[0].get("id"))

    async def _poll_loop(self) -> None:
        while not self._paused:
            rate_limit_retry_after = 0.0
            for channel_id in self.channel_ids:
                try:
                    await self._poll_once(channel_id)
                except DiscordAuthenticationError:
                    raise
                except DiscordChannelForbiddenError as exc:
                    # B2: one forbidden channel skips itself; the source
                    # keeps polling the rest.
                    logger.warning(
                        "Discord channel skipped (forbidden)",
                        extra={
                            "data": {
                                "bot_name": self.bot_name,
                                "service_id": self.service_id,
                                "channel_id": channel_id,
                                "error": str(exc),
                            }
                        },
                    )
                    continue
                except DiscordRateLimitError as exc:
                    rate_limit_retry_after = max(rate_limit_retry_after, exc.retry_after)
                    logger.warning(
                        "Discord poll iteration failed",
                        extra={
                            "data": {
                                "bot_name": self.bot_name,
                                "service_id": self.service_id,
                                "channel_id": channel_id,
                                "error": str(exc),
                            }
                        },
                    )
                except Exception as exc:
                    logger.warning(
                        "Discord poll iteration failed",
                        extra={
                            "data": {
                                "bot_name": self.bot_name,
                                "service_id": self.service_id,
                                "channel_id": channel_id,
                                "error": str(exc),
                            }
                        },
                    )
            await asyncio.sleep(max(rate_limit_retry_after, POLL_INTERVAL))

    async def connect(self) -> None:
        if self._connected:
            return
        if not self.token:
            raise ValueError("Missing Discord token")
        if not self.channel_ids:
            raise ValueError("Missing Discord channel id")
        self._paused = False
        self._ready.clear()
        for channel_id in self.channel_ids:
            self._seen_ids.setdefault(channel_id, set())

        # Verify auth with an initial poll before starting the background loop.
        # B2: poll EVERY channel up front (a single-channel prime swallowed
        # the other channels' signals). The service reports RUNNING only when
        # the initial poll actually succeeded — a failed start reports FAILED
        # with backoff instead of a healthy-looking RUNNING.
        initial_ok = False
        try:
            await self._prime_cursors(self.channel_ids)
            for channel_id in sorted(self.channel_ids):
                try:
                    await self._poll_once(channel_id)
                except DiscordChannelForbiddenError:
                    continue
            initial_ok = True
        except DiscordAuthenticationError as exc:
            await self.disconnect(failed=True)
            message = f"Discord bot {self.bot_name} auth failed: Invalid token"
            logger.warning(
                message,
                extra={
                    "data": {
                        "bot_name": self.bot_name,
                        "service_id": self.service_id,
                        "error": str(exc),
                    }
                },
            )
            raise DiscordAuthenticationError(message) from exc
        except Exception as exc:
            logger.warning(
                "Initial discord poll failed; continuing with background loop",
                extra={
                    "data": {
                        "bot_name": self.bot_name,
                        "service_id": self.service_id,
                        "error": str(exc),
                    }
                },
            )

        self._connected = True
        self._ready.set()
        await self._set_service_status(
            ServiceStatus.RUNNING if initial_ok else ServiceStatus.FAILED,
            last_activity=datetime.now(timezone.utc),
        )
        self._run_task = asyncio.create_task(
            self._poll_loop(),
            name=f"discord-source-poll-{self.bot_name.lower()}",
        )

    async def disconnect(self, *, failed: bool = False) -> None:
        """Tear the source down.

        ``failed`` defaults to ``False``: an ordinary teardown (the daemon's ``finally``, a
        cancellation, a reconnect) says nothing about the service's health and must not mark
        it FAILED. Callers that *know* the teardown is a failure (an authentication error)
        pass ``failed=True``; runtime failures in ``listen_signals`` write FAILED themselves.
        """
        self._connected = False
        self._ready.clear()
        if self._run_task is not None:
            run_task = self._run_task
            if not run_task.done():
                run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run_task
            self._run_task = None
        if self._paused:
            await self._set_service_status(ServiceStatus.PAUSED)
        elif failed:
            await self._set_service_status(ServiceStatus.FAILED)

    async def pause(self) -> None:
        self._paused = True
        await self.disconnect(failed=False)
        await self._set_service_status(ServiceStatus.PAUSED)

    async def reconnect(self) -> None:
        await self.disconnect(failed=False)
        self._paused = False
        await self.connect()

    async def listen_signals(self) -> AsyncIterator[TradeSignal]:
        if self._paused:
            return
        if not self._connected:
            await self.connect()
        while not self._paused:
            if self._run_task is not None and self._run_task.done():
                exception = self._run_task.exception()
                if exception is not None:
                    if _is_discord_auth_error(exception):
                        message = f"Discord bot {self.bot_name} auth failed: Invalid token"
                        logger.warning(
                            message,
                            extra={
                                "data": {
                                    "bot_name": self.bot_name,
                                    "service_id": self.service_id,
                                    "error": str(exception),
                                }
                            },
                        )
                        await self._set_service_status(ServiceStatus.FAILED)
                        raise DiscordAuthenticationError(message) from exception
                    await self._set_service_status(ServiceStatus.FAILED)
                    raise RuntimeError("Discord client task terminated") from exception
                await self._set_service_status(ServiceStatus.FAILED)
                raise RuntimeError("Discord client task terminated unexpectedly")
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
