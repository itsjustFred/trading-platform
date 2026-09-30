from __future__ import annotations

import asyncio
import threading
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from live_runner.alpaca_adapter import resolve_alpaca_credentials
from shared.logging import get_logger
from shared.models import LiveAccountConfig, LiveDeploymentConfig

logger = get_logger("live-runner")


def _is_crypto_symbol(symbol: str) -> bool:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return False
    return "/" in normalized or normalized.endswith("USD") and "-" in normalized


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_symbol(symbol: str) -> str:
    return str(symbol or "").strip().upper()


def _is_connection_limit_error(error: BaseException) -> bool:
    return "connection limit exceeded" in str(error).strip().lower()


def _serialize_bar_payload(bar: Any) -> dict[str, Any]:
    symbol = _normalize_symbol(getattr(bar, "symbol", ""))
    ts = getattr(bar, "timestamp", None)
    if isinstance(ts, str):
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    elif isinstance(ts, datetime):
        parsed = ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    return {
        "symbol": symbol,
        "timestamp": parsed.isoformat(),
        "time": float(parsed.timestamp()),
        "open": _to_float(getattr(bar, "open", None)),
        "high": _to_float(getattr(bar, "high", None)),
        "low": _to_float(getattr(bar, "low", None)),
        "close": _to_float(getattr(bar, "close", None)),
        "volume": _to_float(getattr(bar, "volume", None)),
    }


@dataclass(frozen=True)
class _CredentialKey:
    api_key: str
    secret_key: str
    paper: bool


class _StreamWorker:
    def __init__(
        self,
        *,
        kind: str,
        api_key: str,
        secret_key: str,
        paper: bool,
        bar_handler: Any,
    ) -> None:
        self.kind = kind
        self.api_key = api_key
        self.secret_key = secret_key
        self.paper = paper
        self.bar_handler = bar_handler
        self.stream: Any | None = None
        self.thread: threading.Thread | None = None
        self._running = False
        self._symbols: set[str] = set()
        self._lock = threading.Lock()

    def _create_stream(self) -> Any:
        if self.kind == "stock":
            from alpaca.data.enums import DataFeed
            from alpaca.data.live import StockDataStream

            return StockDataStream(
                self.api_key,
                self.secret_key,
                feed=DataFeed.IEX if self.paper else DataFeed.SIP,
            )
        from alpaca.data.live import CryptoDataStream

        return CryptoDataStream(self.api_key, self.secret_key)

    def start(self) -> None:
        if self._running:
            return
        self.stream = self._create_stream()
        self._running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        assert self.stream is not None
        backoff = 2.0
        while self._running:
            try:
                self.stream.run()
                backoff = 2.0
            except Exception as exc:
                if not self._running:
                    break
                if _is_connection_limit_error(exc):
                    logger.warning(
                        "Alpaca market data stream stopped due to websocket connection limit",
                        extra={
                            "data": {
                                "kind": self.kind,
                                "paper": self.paper,
                                "error": str(exc),
                            }
                        },
                    )
                    self._running = False
                    break
                logger.warning(
                    "Alpaca market data stream disconnected, retrying",
                    extra={
                        "data": {
                            "kind": self.kind,
                            "paper": self.paper,
                            "error": str(exc),
                            "retry_in_seconds": backoff,
                        }
                    },
                )
                self.stream = self._create_stream()
                with self._lock:
                    symbols = sorted(self._symbols)
                if symbols:
                    self._subscribe_symbols(symbols)
                import time

                time.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    def _subscribe_symbols(self, symbols: list[str]) -> None:
        if not symbols or self.stream is None:
            return
        self.stream.subscribe_bars(self.bar_handler, *symbols)

    def set_symbols(self, symbols: set[str]) -> None:
        normalized = {_normalize_symbol(symbol) for symbol in symbols if _normalize_symbol(symbol)}
        with self._lock:
            removed = sorted(self._symbols - normalized)
            self._symbols = normalized
        if self.stream is None:
            return
        if removed and hasattr(self.stream, "unsubscribe_bars"):
            try:
                # The symbols being dropped have to be named. In alpaca-py
                # `unsubscribe_bars(*symbols)` deletes exactly the symbols it is
                # given - "*" is the spelling for all of them - so the argument-less
                # call this used to make removed nothing, and because `subscribe_bars`
                # merges into the handler dict rather than replacing it, the
                # subscribed set could only ever grow.
                self.stream.unsubscribe_bars(*removed)
            except Exception as exc:
                # Logged rather than raised: set_symbols is called once per worker
                # from the refresh loop, so an exception here would abandon the
                # remaining workers' updates. Not raised means a websocket that is
                # already closed leaves the old subscription in place, which must
                # not pass unnoticed.
                logger.warning(
                    "Alpaca bar unsubscribe failed; the previous symbols stay subscribed",
                    extra={
                        "data": {
                            "removed_symbols": removed,
                            "current_symbols": sorted(normalized),
                            "error": str(exc),
                        }
                    },
                )
        if normalized:
            self._subscribe_symbols(sorted(normalized))

    def stop(self) -> None:
        self._running = False
        stream = self.stream
        if stream is not None and hasattr(stream, "stop"):
            try:
                stream.stop()
            except Exception:
                pass


class LiveMarketDataStreamService:
    def __init__(
        self,
        *,
        max_bars_per_symbol: int = 500,
        env_values: dict[str, str] | None = None,
    ) -> None:
        self._max_bars_per_symbol = max(10, int(max_bars_per_symbol))
        self._bars: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self._max_bars_per_symbol)
        )
        self._workers: dict[tuple[_CredentialKey, str], _StreamWorker] = {}
        self._lock = threading.Lock()
        # C1: file-first credential source so quotes resolve the same
        # credentials as execution (never process-env while trading on file).
        self._env_values = env_values

    async def refresh_from_deployments(
        self,
        deployments: list[LiveDeploymentConfig],
        account_lookup: dict[str, LiveAccountConfig],
        *,
        env_values: dict[str, str] | None = None,
    ) -> None:
        try:
            await asyncio.to_thread(
                self._refresh_sync, deployments, account_lookup, env_values
            )
        except ModuleNotFoundError:
            logger.info("alpaca-py market data stream unavailable; install alpaca-py to enable live quotes")

    def _refresh_sync(
        self,
        deployments: list[LiveDeploymentConfig],
        account_lookup: dict[str, LiveAccountConfig],
        env_values: dict[str, str] | None = None,
    ) -> None:
        target_symbols: dict[tuple[_CredentialKey, str], set[str]] = defaultdict(set)
        for deployment in deployments:
            if str(deployment.status or "").upper() != "ACTIVE":
                continue
            account = account_lookup.get(deployment.account_id)
            if account is None or account.broker != "alpaca":
                continue
            credentials = resolve_alpaca_credentials(
                account_id=account.id,
                broker_login=account.broker_login,
                env=env_values if env_values is not None else self._env_values,
            )
            if credentials is None:
                continue
            api_key, secret_key, paper = credentials
            key = _CredentialKey(api_key=api_key, secret_key=secret_key, paper=paper)
            for raw_symbol in deployment.symbols:
                symbol = _normalize_symbol(raw_symbol)
                if not symbol:
                    continue
                stream_kind = "crypto" if _is_crypto_symbol(symbol) else "stock"
                target_symbols[(key, stream_kind)].add(symbol)

        # B2: decide under the lock, do network I/O outside it — stop/start/
        # set_symbols perform websocket I/O and must never hold the lock
        # (same pattern as _stop_sync below).
        with self._lock:
            worker_keys = set(self._workers.keys())
            target_keys = set(target_symbols.keys())
            stopped = [
                self._workers.pop(worker_key, None)
                for worker_key in worker_keys - target_keys
            ]
            to_start: list[tuple[tuple[_CredentialKey, str], set[str]]] = []
            to_update: list[tuple[_StreamWorker, set[str]]] = []
            for worker_key, symbols in target_symbols.items():
                worker = self._workers.get(worker_key)
                if worker is None:
                    to_start.append((worker_key, symbols))
                else:
                    to_update.append((worker, symbols))
        for worker in stopped:
            if worker is not None:
                worker.stop()
        for worker_key, symbols in to_start:
            credentials_key, stream_kind = worker_key
            worker = _StreamWorker(
                kind=stream_kind,
                api_key=credentials_key.api_key,
                secret_key=credentials_key.secret_key,
                paper=credentials_key.paper,
                bar_handler=self._on_bar,
            )
            worker.start()
            with self._lock:
                keep_new = worker_key not in self._workers
                if keep_new:
                    self._workers[worker_key] = worker
            if not keep_new:
                # Lost a concurrent refresh race — stop the spare worker.
                worker.stop()
                continue
            worker.set_symbols(symbols)
        for worker, symbols in to_update:
            worker.set_symbols(symbols)

    async def stop(self) -> None:
        await asyncio.to_thread(self._stop_sync)

    def _stop_sync(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop()

    async def get_quote_payload(self, symbol: str) -> dict[str, Any]:
        normalized = _normalize_symbol(symbol)
        if not normalized:
            return {"symbol": "", "latest": None, "bars": []}
        bars = await asyncio.to_thread(self._bars_for_symbol, normalized)
        latest = bars[-1] if bars else None
        return {"symbol": normalized, "latest": latest, "bars": bars}

    def _bars_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        with self._lock:
            queue = self._bars.get(symbol)
            if queue is None:
                return []
            return list(queue)

    async def ingest_manual_bars(self, bars: Iterable[dict[str, Any]]) -> None:
        await asyncio.to_thread(self._ingest_manual_bars_sync, list(bars))

    def _ingest_manual_bars_sync(self, bars: list[dict[str, Any]]) -> None:
        with self._lock:
            for bar in bars:
                symbol = _normalize_symbol(str(bar.get("symbol") or ""))
                if not symbol:
                    continue
                queue = self._bars[symbol]
                queue.append(dict(bar))

    async def _on_bar(self, bar: Any) -> None:
        payload = _serialize_bar_payload(bar)
        symbol = payload["symbol"]
        if not symbol:
            return
        # Only record bars for symbols this worker is currently subscribed to. A bar
        # can still arrive for one that was just dropped, because the broker may
        # have it in flight, and it used to be recorded regardless - leaving data
        # for an abandoned symbol in `_bars`, where a consumer cannot tell it from
        # a live one.
        with self._lock:
            current = self._symbols
        if symbol not in current:
            return
        await asyncio.to_thread(self._append_bar_sync, symbol, payload)

    def _append_bar_sync(self, symbol: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._bars[symbol].append(payload)
