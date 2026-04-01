from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from risk.models import TradeSignal
from shared.constants import normalize_timeframe
from shared.logging import get_logger

from live_runner.signal_sources.base_source import BaseSignalSource

logger = get_logger("live-runner")

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


def _extract_symbol(content: str) -> str | None:
    def normalize_candidate(candidate: str) -> str | None:
        normalized = candidate.strip().strip("*$").upper()
        if not normalized or normalized in _SKIP_WORDS:
            return None
        return normalized

    for matched in re.findall(r"\*\*\$?([A-Za-z]{1,10})\*\*", content):
        candidate = normalize_candidate(matched)
        if candidate:
            return candidate

    for pattern in (
        r"(?:ticker|symbol|stock)\s*[:\-]\s*\**\$?([A-Za-z]{1,10})\**",
        r"(?:for|on)\s+\**\$?([A-Za-z]{1,10})\**",
    ):
        for matched in re.finditer(pattern, content, re.IGNORECASE):
            candidate = normalize_candidate(matched.group(1))
            if candidate:
                return candidate

    for matched in re.findall(r"\$([A-Za-z]{1,10})\b", content):
        candidate = normalize_candidate(matched)
        if candidate:
            return candidate

    ticker_price = re.search(r"\b([A-Za-z]{1,10})\s+\$\d+", content)
    if ticker_price:
        candidate = normalize_candidate(ticker_price.group(1))
        if candidate:
            return candidate

    for line in content.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        for matched in re.findall(r"\b([A-Z]{1,10})\b", line_stripped):
            candidate = normalize_candidate(matched)
            if candidate:
                return candidate
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


class DiscordSignalSource(BaseSignalSource):
    def __init__(
        self,
        *,
        token: str | None = None,
        channel_id: str | None = None,
        channel_ids: set[str] | None = None,
        timeframe: str = "1h",
    ) -> None:
        self.token = (token or os.environ.get("DISCORD_BOT_TOKEN", "")).strip()
        resolved_channel_ids: set[str] = set(channel_ids or set())
        fallback_channel_id = str(channel_id or os.environ.get("DISCORD_CHANNEL_ID", "")).strip()
        if fallback_channel_id:
            resolved_channel_ids.add(fallback_channel_id)
        self.channel_ids = {value for value in resolved_channel_ids if value}
        self.timeframe = normalize_timeframe(timeframe) or timeframe
        self._client: Any | None = None
        self._queue: asyncio.Queue[TradeSignal] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._connected = False
        self._run_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        if self._connected:
            return
        if not self.token:
            raise ValueError("Missing DISCORD_BOT_TOKEN")
        if not self.channel_ids:
            raise ValueError("Missing DISCORD_CHANNEL_ID")

        try:
            import discord
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "discord.py is required for DiscordSignalSource. Install package 'discord.py'."
            ) from exc

        intents = discord.Intents.default()
        intents.message_content = True

        source = self

        class _SignalClient(discord.Client):
            async def on_ready(self) -> None:
                logger.info(
                    "Discord signal source connected",
                    extra={
                        "data": {
                            "channel_ids": sorted(source.channel_ids),
                            "bot_user": str(self.user),
                        }
                    },
                )
                source._ready.set()

            async def on_message(self, message: Any) -> None:
                if getattr(message.author, "bot", False):
                    return
                channel_id = str(getattr(message.channel, "id", ""))
                if channel_id not in source.channel_ids:
                    return
                parsed = _parse_trade_signal_from_message(
                    message,
                    default_timeframe=source.timeframe,
                )
                if parsed is None:
                    return
                await source._queue.put(parsed)

        self._client = _SignalClient(intents=intents)
        self._run_task = asyncio.create_task(
            self._client.start(self.token),
            name="discord-source-client",
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
        except Exception:
            await self.disconnect()
            raise
        self._connected = True

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close()
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
        self._connected = False
        self._run_task = None

    async def listen_signals(self) -> AsyncIterator[TradeSignal]:
        if not self._connected:
            await self.connect()
        while True:
            if self._run_task is not None and self._run_task.done():
                exception = self._run_task.exception()
                if exception is not None:
                    raise RuntimeError("Discord client task terminated") from exception
                raise RuntimeError("Discord client task terminated unexpectedly")
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
