from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import AsyncIterator, Iterable
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import requests

from live_runner.signal_sources.base_source import BaseSignalSource
from risk.models import TradeSignal
from shared.constants import normalize_timeframe
from shared.database import update_service_status
from shared.logging import get_logger
from shared.models import ServiceStatus

logger = get_logger("live-runner")

# Polling cadence for the REST-based signal source (seconds).
POLL_INTERVAL = 8
POLL_LIMIT = 20


class DiscordAuthenticationError(RuntimeError):
    """Raised when Discord returns a terminal authentication error (401/403)."""


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
_PRICE_LABEL_RE = re.compile(r"Price:\s*\$\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)
_DOLLAR_PRICE_RE = re.compile(r"\$\s*(\d+(?:[.,]\d+)?)")
_AT_PRICE_RE = re.compile(r"@\s*(\d+(?:\.\d+)?)")
_ACTION_BUY_RE = re.compile(r"\bbuy\b|\blong\b", re.IGNORECASE)
_ACTION_SELL_RE = re.compile(r"\bsell\b|\bshort\b|\bmomentum\s*reversal\b", re.IGNORECASE)
_MOMENTUM_REVERSAL_RE = re.compile(r"momentum\s*reversal", re.IGNORECASE)
_GOLDEN_CROSS_RE = re.compile(r"golden\s*cross", re.IGNORECASE)
_DEATH_CROSS_RE = re.compile(r"death\s*cross", re.IGNORECASE)
_OVERBOUGHT_RE = re.compile(r"overbought", re.IGNORECASE)
_OVERSOLD_RE = re.compile(r"oversold|rsi\s*<?\s*30|rocket\s*fuel\s*buy\s*signal", re.IGNORECASE)
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
    "LOSS",
    "TAKE",
    "PROFIT",
    "EXIT",
    "ENTRY",
}


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


def _first_normalized_symbol(matches: Iterable[str]) -> str | None:
    for matched in matches:
        candidate = _normalize_symbol_candidate(matched)
        if candidate:
            return candidate
    return None


def _extract_bold_symbol(content: str) -> str | None:
    return _first_normalized_symbol(re.findall(r"\*\* \$?([A-Za-z]{1,10})\*\*", content))


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


def _fallback_uppercase_symbol(content: str) -> str | None:
    for line in content.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        symbol = _first_normalized_symbol(re.findall(r"\b([A-Z]{1,10})\b", line_stripped))
        if symbol:
            return symbol
    return None


def _extract_symbol(content: str) -> str | None:
    for extract_stage in (
        _extract_bold_symbol,
        _extract_labeled_symbol,
        _extract_action_symbol,
        _extract_dollar_symbol,
        _price_prefixed_symbol,
        _fallback_uppercase_symbol,
    ):
        symbol = extract_stage(content)
        if symbol:
            return symbol
    return None


def _extract_price(content: str) -> float | None:
    for matcher in (_PRICE_LABEL_RE, _DOLLAR_PRICE_RE, _AT_PRICE_RE):
        matched = matcher.search(content)
        if matched is None:
            continue
        try:
            return float(matched.group(1).replace(",", "."))
        except ValueError:
            continue
    return None


def _is_ramjet_signal(content: str) -> bool:
    return bool(_RAMJET_OVERSOLD_RE.search(content) and _BUY_SIGNAL_RE.search(content))


def _detect_rocket_fuel_signal_type(content: str) -> str | None:
    if _MOMENTUM_REVERSAL_RE.search(content) or _ACTION_SELL_RE.search(content):
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
    if signal_type in {"momentum_reversal"}:
        return "sell"
    if signal_type in {"oversold_buy", "golden_cross"}:
        return "buy"
    if _ACTION_SELL_RE.search(content):
        return "sell"
    if _ACTION_BUY_RE.search(content):
        return "buy"
    return None


def _parse_timestamp(raw_timestamp: str | None) -> datetime:
    if not raw_timestamp:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _parse_trade_signal_from_message(
    message: Any,
    *,
    default_timeframe: str,
) -> TradeSignal | None:
    content = str(getattr(message, "content", "") or "").strip()
    if not content:
        return None

    signal_type = None
    if _is_ramjet_signal(content):
        action = "buy"
        signal_type = "ram_jet_oversold_buy"
    else:
        signal_type = _detect_rocket_fuel_signal_type(content)
        action = _resolve_action(content, signal_type=signal_type)
    if action is None:
        return None

    symbol = _extract_symbol(content)
    price = _extract_price(content)
    if not symbol or price is None or price <= 0:
        return None

    created_at = getattr(message, "created_at", None)
    raw_timestamp = created_at.isoformat() if isinstance(created_at, datetime) else None
    timestamp = _parse_timestamp(raw_timestamp)
    metadata = {
        "source": "discord",
        "discord_signal_type": signal_type or "generic",
        "discord_message_id": str(getattr(message, "id", "")),
        "discord_channel_id": str(getattr(getattr(message, "channel", None), "id", "")),
        "discord_author_id": str(getattr(getattr(message, "author", None), "id", "")),
        "discord_content": content,
        "received_at": timestamp.isoformat(),
        "timeframe": normalize_timeframe(default_timeframe) or default_timeframe,
    }
    return TradeSignal(
        action=action,
        symbol=symbol,
        entry_price=float(price),
        stop_loss=None,
        take_profit=None,
        timeframe=str(metadata["timeframe"]),
        metadata=metadata,
    )


class _DiscordMessageAdapter:
    """Minimal adapter exposing the attributes the parser relies on."""

    __slots__ = ("content", "id", "created_at", "channel", "author")

    def __init__(self, message: dict[str, Any]) -> None:
        self.content = str(message.get("content") or "")
        self.id = str(message.get("id"))
        self.created_at = _parse_timestamp(message.get("timestamp"))
        self.channel = SimpleNamespace(id=str(message.get("channel_id")))
        author = message.get("author") or {}
        self.author = SimpleNamespace(id=str(author.get("id")))


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
    ) -> None:
        normalized_name = str(bot_name or "discord").strip()
        self.bot_name = normalized_name or "discord"
        self.service_id = str(service_id or "").strip() or None
        self.source_name = f"discord_{self.bot_name.lower()}"
        self.token = (token or os.environ.get("DISCORD_BOT_TOKEN", "")).strip()
        resolved_channel_ids: set[str] = set(channel_ids or set())
        fallback_channel_id = str(channel_id or os.environ.get("DISCORD_CHANNEL_ID", "")).strip()
        if fallback_channel_id:
            resolved_channel_ids.add(fallback_channel_id)
        self.channel_ids = {value for value in resolved_channel_ids if value}
        self.timeframe = normalize_timeframe(timeframe) or timeframe
        self._queue: asyncio.Queue[TradeSignal] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._connected = False
        self._paused = False
        self._run_task: asyncio.Task[None] | None = None
        self._seen_ids: dict[str, set[str]] = {}
        self._last_seen_message_ids: dict[str, str] = {}

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
        try:
            await asyncio.to_thread(
                update_service_status,
                self.service_id,
                status,
                last_activity=last_activity,
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

    async def _fetch_messages(self, channel_id: str) -> list[dict[str, Any]]:
        headers = {"authorization": self.token}
        url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit={POLL_LIMIT}"

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

        if response.status_code in (401, 403):
            raise DiscordAuthenticationError(
                f"Discord auth failed ({response.status_code}) for {channel_id}"
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

    async def _poll_once(self, channel_id: str, *, emit: bool = True) -> None:
        messages = await self._fetch_messages(channel_id)
        if not messages:
            return
        seen = self._seen_ids.setdefault(channel_id, set())
        # Messages arrive newest-first; iterate reversed so the queue receives
        # them in chronological (oldest -> newest) order.
        last_seen = self._last_seen_message_ids.get(channel_id, "")
        for message in reversed(messages):
            message_id = str(message.get("id"))
            if message_id in seen:
                continue
            if emit and last_seen and int(message_id) <= int(last_seen):
                seen.add(message_id)
                continue
            seen.add(message_id)
            if not emit:
                continue
            wrapped = _DiscordMessageAdapter(message)
            parsed = _parse_trade_signal_from_message(
                wrapped,
                default_timeframe=self.timeframe,
            )
            if parsed is None:
                continue
            parsed.metadata["source"] = self.source_name
            parsed.metadata["discord_bot_name"] = self.bot_name
            if self.service_id:
                parsed.metadata["service_id"] = self.service_id
            await self._set_service_status(
                ServiceStatus.RUNNING,
                last_activity=datetime.now(timezone.utc),
            )
            await self._queue.put(parsed)
        newest = str(messages[0].get("id"))
        if not last_seen or int(newest) > int(last_seen):
            self._last_seen_message_ids[channel_id] = newest

    async def _prime_cursors(self, channel_ids: Iterable[str]) -> None:
        for channel_id in channel_ids:
            if channel_id in self._last_seen_message_ids:
                continue
            try:
                messages = await self._fetch_messages(channel_id)
            except DiscordAuthenticationError:
                raise
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
                return
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
            try:
                await asyncio.sleep(max(rate_limit_retry_after, POLL_INTERVAL))
            except asyncio.CancelledError:
                raise

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
        try:
            await self._prime_cursors(self.channel_ids)
            await self._poll_once(next(iter(self.channel_ids)))
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
            ServiceStatus.RUNNING,
            last_activity=datetime.now(timezone.utc),
        )
        self._run_task = asyncio.create_task(
            self._poll_loop(),
            name=f"discord-source-poll-{self.bot_name.lower()}",
        )

    async def disconnect(self, *, failed: bool = True) -> None:
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
